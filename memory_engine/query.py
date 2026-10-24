# -*- coding: utf-8 -*-
"""
P3 多源查询包构造

源实现的核心思想：检索用的不是一个 query 字符串，而是一个「查询包」——
  焦点消息(focus) + 最近若干条上下文(context) + 记忆提示(hints)，
每个来源各自带基础权重，再乘长度自适应因子，最后归一化并给焦点保底。

已核对的源常量（原样照搬）：
    FOCUS_BASE_WEIGHT          = 0.55
    CONTEXT_BASE_WEIGHTS       = [0.15, 0.30]     # 由远及近
    FOCUS_BASE_WEIGHT_R2       = 0.45
    CONTEXT_BASE_WEIGHTS_R2    = [0.10, 0.20]
    HINTS_BASE_WEIGHT          = 0.25
    LENGTH_FULL_THRESHOLD      = 50
    LENGTH_MIN_FACTOR          = 0.35
    FOCUS_MIN_NORMALIZED_WEIGHT= 0.35
    MEMORY_HINTS_PER_SOURCE_MAX= 3
    LEXICAL_TERMS_MAX          = 10
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .lexical import extract_key_terms, tokenize

FOCUS_BASE_WEIGHT = 0.55
CONTEXT_BASE_WEIGHTS = [0.15, 0.30]
FOCUS_BASE_WEIGHT_R2 = 0.45
CONTEXT_BASE_WEIGHTS_R2 = [0.10, 0.20]
HINTS_BASE_WEIGHT = 0.25
LENGTH_FULL_THRESHOLD = 50
LENGTH_MIN_FACTOR = 0.35
FOCUS_MIN_NORMALIZED_WEIGHT = 0.35
MEMORY_HINTS_PER_SOURCE_MAX = 3
LEXICAL_TERMS_MAX = 10


@dataclass
class QuerySource:
    name: str                 # focus | context-1 | context-2 | hint-i
    text: str
    weight: float = 0.0       # 归一化后的最终权重
    base_weight: float = 0.0
    floor: int = -1


@dataclass
class QueryBundle:
    sources: List[QuerySource] = field(default_factory=list)
    lexical_terms: List[str] = field(default_factory=list)
    term_weights: Dict[str, float] = field(default_factory=dict)
    length_factor: float = 1.0
    round: int = 1
    meta: Dict[str, Any] = field(default_factory=dict)

    # ---- 下游消费接口 ----
    def focus(self) -> Optional[QuerySource]:
        for s in self.sources:
            if s.name == "focus":
                return s
        return None

    def vector_text(self) -> str:
        """给嵌入模型用的合成文本（焦点在前，按权重去重拼接）。"""
        parts, seen = [], set()
        for s in sorted(self.sources, key=lambda x: -x.weight):
            t = (s.text or "").strip()
            if t and t not in seen:
                seen.add(t)
                parts.append(t)
        return "\n".join(parts)

    def source_weights(self) -> Dict[str, float]:
        return {s.name: s.weight for s in self.sources}

    def lexical_groups(self) -> List[Tuple[str, List[str], float]]:
        """给 LexicalIndex.search_groups 用：按来源分组的词表 + 权重。"""
        groups: List[Tuple[str, List[str], float]] = []
        for s in self.sources:
            terms = extract_key_terms(s.text, LEXICAL_TERMS_MAX)
            if terms:
                groups.append((s.name, terms, s.weight))
        return groups

    def describe(self) -> str:
        return " | ".join(f"{s.name}:{s.weight:.2f}" for s in self.sources)


def compute_length_factor(char_count: int,
                          full_threshold: int = LENGTH_FULL_THRESHOLD,
                          min_factor: float = LENGTH_MIN_FACTOR) -> float:
    """消息太短时按比例降权，避免「嗯」「好」这类短句主导检索。"""
    if char_count >= full_threshold:
        return 1.0
    if char_count <= 0:
        return min_factor
    return max(min_factor, char_count / float(full_threshold))


def normalize_weights(weights: Dict[str, float],
                      focus_key: str = "focus",
                      focus_min: float = FOCUS_MIN_NORMALIZED_WEIGHT) -> Dict[str, float]:
    """
    归一化到和为 1，并给焦点来源保底：
    上下文再长也不能把焦点压到 focus_min 以下，超出部分由其他来源等比承担。
    """
    pos = {k: max(0.0, float(v)) for k, v in weights.items()}
    total = sum(pos.values())
    if total <= 0:
        return {k: 0.0 for k in weights}
    norm = {k: v / total for k, v in pos.items()}
    if focus_key not in norm or focus_min <= 0:
        return norm
    if norm[focus_key] >= focus_min:
        return norm
    deficit = focus_min - norm[focus_key]
    others = {k: v for k, v in norm.items() if k != focus_key}
    others_total = sum(others.values())
    if others_total <= 0:
        norm[focus_key] = 1.0
        return norm
    scale = max(0.0, (others_total - deficit) / others_total)
    out = {k: v * scale for k, v in others.items()}
    out[focus_key] = focus_min
    return out


def _msg_text(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        for key in ("content", "text", "message", "raw_message", "summary"):
            v = msg.get(key)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, list):          # 富文本分段
                return " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in v)
    return ""


def _msg_floor(msg: Any, fallback: int) -> int:
    if isinstance(msg, dict):
        for key in ("floor", "index", "seq", "message_id"):
            v = msg.get(key)
            try:
                if v is not None:
                    return int(v)
            except (TypeError, ValueError):
                continue
    return fallback


def collect_hints(store: Any, user_id: Any,
                  max_sources: int = 3,
                  per_source: int = MEMORY_HINTS_PER_SOURCE_MAX) -> List[str]:
    """从记忆库取「记忆提示」：最近锚点的关键词 + 最近事件的标题，作为补充检索线索。"""
    if store is None or user_id is None:
        return []
    try:
        items = store.list_items(user_id, include_hidden=False)
    except Exception:
        return []
    anchors = [i for i in items if i.get("kind") == "anchor"]
    events = [i for i in items if i.get("kind") == "event"]
    anchors.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    events.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)

    hints: List[str] = []
    for it in anchors[:max_sources]:
        kws = list(it.get("keywords") or [])[:per_source]
        if not kws and it.get("title"):
            kws = [it["title"]]
        if kws:
            hints.append(" ".join(str(k) for k in kws))
    for it in events[:max_sources]:
        kws = list(it.get("entities") or [])[:per_source]
        if kws:
            hints.append(" ".join(str(k) for k in kws))
    # 去重保序
    seen, out = set(), []
    for h in hints:
        h = h.strip()
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def build_query_bundle(last_messages: Sequence[Any],
                       store: Any = None,
                       user_id: Any = None,
                       round_no: int = 1,
                       extra_terms: Optional[Sequence[str]] = None) -> QueryBundle:
    """
    构造查询包。last_messages 由旧到新（最后一条 = 焦点）。
    round_no=2 时用 R2 权重（配合 refine_query_bundle 做伪相关反馈）。
    """
    msgs = list(last_messages or [])
    bundle = QueryBundle(round=int(round_no))
    if not msgs:
        return bundle

    focus_w = FOCUS_BASE_WEIGHT if round_no == 1 else FOCUS_BASE_WEIGHT_R2
    ctx_ws = CONTEXT_BASE_WEIGHTS if round_no == 1 else CONTEXT_BASE_WEIGHTS_R2

    focus_msg = msgs[-1]
    focus_text = _msg_text(focus_msg)
    bundle.length_factor = compute_length_factor(len(focus_text))

    raw: Dict[str, Tuple[str, float, int]] = {}
    raw["focus"] = (focus_text, focus_w * bundle.length_factor,
                    _msg_floor(focus_msg, len(msgs) - 1))

    ctx = msgs[:-1][-len(ctx_ws):]           # 取最近 len(ctx_ws) 条上下文（由远及近）
    for i, m in enumerate(ctx):              # i=0 最远；ctx_ws 也是由远及近
        text = _msg_text(m)
        if not text:
            continue
        dist = len(ctx) - i                  # 1 = 距焦点最近
        w = ctx_ws[i] if i < len(ctx_ws) else ctx_ws[-1]
        raw[f"context-{dist}"] = (text, w, _msg_floor(m, len(msgs) - 1 - dist))

    hints = collect_hints(store, user_id)
    if hints:
        hint_text = "\n".join(hints)
        raw["hints"] = (hint_text, HINTS_BASE_WEIGHT, -1)

    weights = normalize_weights({k: v[1] for k, v in raw.items()})
    for name, (text, _bw, floor) in raw.items():
        bundle.sources.append(QuerySource(name=name, text=text,
                                          weight=weights.get(name, 0.0),
                                          base_weight=_bw, floor=floor))

    # 词法词表：焦点为主，其余来源补充（去重、限量）
    terms = extract_key_terms(focus_text, LEXICAL_TERMS_MAX)
    seen = set(terms)
    for s in bundle.sources:
        if s.name == "focus":
            continue
        for t in extract_key_terms(s.text, LEXICAL_TERMS_MAX // 2):
            if t not in seen and len(terms) < LEXICAL_TERMS_MAX * 2:
                seen.add(t)
                terms.append(t)
    if extra_terms:
        for t in extra_terms:
            t = str(t)
            if t and t not in seen:
                seen.add(t)
                terms.append(t)
    bundle.lexical_terms = terms
    bundle.term_weights = {t: 1.0 for t in terms}
    return bundle


def refine_query_bundle(bundle: QueryBundle,
                        anchor_hits: Optional[Sequence[Any]] = None,
                        event_hits: Optional[Sequence[Any]] = None) -> QueryBundle:
    """
    R2 轮：用第一轮的命中结果做伪相关反馈，
    把命中条目的关键词补进词表并抬权，同时切到 R2 权重。
    """
    bundle.round = 2
    boost: Dict[str, float] = {}
    for hits in (anchor_hits, event_hits):
        for h in (hits or []):
            item = h.get("item") if isinstance(h, dict) else None
            item = item or (h if isinstance(h, dict) else {})
            for kw in (list(item.get("keywords") or []) + list(item.get("entities") or [])
                       + list(item.get("locations") or [])):
                kw = str(kw).strip()
                if kw:
                    boost[kw] = boost.get(kw, 0.0) + 1.0
    if boost:
        top = sorted(boost.items(), key=lambda kv: -kv[1])[:LEXICAL_TERMS_MAX]
        for term, cnt in top:
            bundle.term_weights[term] = 1.0 + min(cnt, 3) * 0.5
            if term not in bundle.lexical_terms:
                bundle.lexical_terms.append(term)
    # 切 R2 权重
    if bundle.sources:
        n_ctx = len([x for x in bundle.sources if x.name.startswith("context-")])
        new_raw = {}
        for src in bundle.sources:
            if src.name == "focus":
                new_raw[src.name] = FOCUS_BASE_WEIGHT_R2 * bundle.length_factor
            elif src.name.startswith("context-"):
                # context-{dist}：dist=1 最近，对应 ctx_ws 的最后一个下标
                dist = int(src.name.split("-")[1])
                idx = max(0, n_ctx - dist)
                new_raw[src.name] = (CONTEXT_BASE_WEIGHTS_R2[idx]
                                     if idx < len(CONTEXT_BASE_WEIGHTS_R2)
                                     else CONTEXT_BASE_WEIGHTS_R2[-1])
            else:
                new_raw[src.name] = HINTS_BASE_WEIGHT
        nw = normalize_weights(new_raw)
        for src in bundle.sources:
            src.base_weight = new_raw.get(src.name, src.base_weight)
            src.weight = nw.get(src.name, src.weight)
    return bundle
