# -*- coding: utf-8 -*-
"""
P5 三路混合召回 + 按楼层融合

源实现结构（已在源码中核对）：recallAnchors / recallEvents / buildEventIndex /
fuseByFloor / computeSegmentWeights / computeR2Weights / weightedAverageVectors /
clampMinNormalizedWeight / getLastMessages。

三路各自的职责：
  词法路  精确词面命中 —— 人名、地名、专有名词，向量容易漏
  向量路  语义近似命中 —— 换了说法的同一件事
  图扩散  结构关联命中 —— 同一批角色/同地点/时间接近，但措辞完全不同
三路各自归一后加权融合，再按楼层对齐去重（fuseByFloor），跑 R1/R2 两轮。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .diffusion import cosine as _cosine, diffuse_from_seeds
from .lexical import LexicalIndex
from .query import QueryBundle, build_query_bundle, refine_query_bundle

# 三路默认权重（可在构造时覆盖）
PATH_WEIGHTS = {"lexical": 0.30, "vector": 0.45, "graph": 0.25}
ANCHOR_PRIOR = 0.15          # 锚点先验加成（长期事实应更容易被想起）
SEED_TOP_N = 3               # 取前 N 条命中作为图扩散种子
DEFAULT_TOPK = 8


@dataclass
class RecallHit:
    id: str
    item: Dict[str, Any]
    floor: int = 0
    score: float = 0.0
    sources: Dict[str, float] = field(default_factory=dict)
    round: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "item": self.item, "floor": self.floor,
                "score": round(self.score, 6),
                "sources": {k: round(v, 6) for k, v in self.sources.items()},
                "round": self.round}


def weighted_average_vectors(vectors: Sequence[Optional[Sequence[float]]],
                             weights: Sequence[float]) -> Optional[List[float]]:
    """按权重平均多条向量，得到查询向量。"""
    acc: Optional[List[float]] = None
    total_w = 0.0
    for v, w in zip(vectors, weights):
        if not v:
            continue
        w = float(w)
        if w <= 0:
            continue
        if acc is None:
            acc = [0.0] * len(v)
        if len(v) != len(acc):
            continue
        for i, x in enumerate(v):
            acc[i] += x * w
        total_w += w
    if acc is None or total_w <= 0:
        return None
    return [x / total_w for x in acc]


def compute_segment_weights(floor: int, cursor: int, span: int = 200) -> float:
    """分段权重：离当前进度越近的剧情权重越高。"""
    dist = abs(int(cursor) - int(floor))
    if span <= 0:
        return 1.0
    return max(0.0, 1.0 - dist / float(span))


# 各路原始分的绝对定标参数。
# 原来是「除以本路最高分」，那等于不管问什么都必有一路送出 1.0 满分命中——
# 实测问「用 Python 写一个快速排序」比问「恋人关系确认」融合分还高（1.53 vs 1.32），
# 排序信号完全反了。改成按原始分的绝对尺度映射到 0~1，问不相关时整路都接近 0。
_LEX_HALF = 0.25                       # 词法：v/(v+0.25) 饱和，原始 0.23 -> 0.48
_VEC_LOW, _VEC_SPAN = 0.45, 0.30       # 向量：cos 0.45 -> 0，0.75 -> 1
_GRAPH_LOW, _GRAPH_SPAN = 0.30, 0.40   # 图谱：共现扩散分 0.30 -> 0，0.70 -> 1


def _scale_lexical(scores: Dict[str, float]) -> Dict[str, float]:
    """词法路：饱和曲线，原始分多低就多低，不再被拉成 1.0。"""
    return {k: v / (v + _LEX_HALF) for k, v in scores.items() if v > 0}


def _scale_unit(scores: Dict[str, float], low: float, span: float) -> Dict[str, float]:
    """线性定标：低于 low 的直接算 0（不相关），高于 low+span 的封顶 1。"""
    out: Dict[str, float] = {}
    for k, v in scores.items():
        x = (v - low) / span
        if x > 0:
            out[k] = min(1.0, x)
    return out


def _normalize_scores(scores: Dict[str, float]) -> Dict[str, float]:
    """保留旧接口（按最高分归一化），已不再用于召回打分。"""
    if not scores:
        return {}
    top = max(scores.values())
    if top <= 0:
        return {}
    return {k: v / top for k, v in scores.items()}


def fuse_by_floor(hits: Iterable[RecallHit]) -> List[RecallHit]:
    """
    按楼层对齐去重：
    同一楼层被多路命中时合并分数与来源，避免同一段剧情重复占位。
    不同楼层但同一记忆条目，按 id 去重取最高分。
    """
    by_id: Dict[str, RecallHit] = {}
    for h in hits:
        prev = by_id.get(h.id)
        if prev is None:
            by_id[h.id] = RecallHit(h.id, h.item, h.floor, h.score, dict(h.sources), h.round)
            continue
        # 合并来源，分数取「最高分 + 其余路径的折半贡献」
        merged_src = dict(prev.sources)
        for k, v in h.sources.items():
            merged_src[k] = max(merged_src.get(k, 0.0), v)
        prev.score = max(prev.score, h.score) + 0.5 * min(prev.score, h.score)
        prev.sources = merged_src
    out = sorted(by_id.values(), key=lambda x: -x.score)
    return out


class RecallEngine:
    """把 store 里的记忆条目做三路混合召回。"""

    def __init__(self, store: Any,
                 embed_fn: Optional[Callable[[List[str]], List[List[float]]]] = None,
                 path_weights: Optional[Dict[str, float]] = None,
                 anchor_prior: float = ANCHOR_PRIOR):
        self.store = store
        self.embed_fn = embed_fn
        self.path_weights = dict(path_weights or PATH_WEIGHTS)
        self.anchor_prior = float(anchor_prior)
        self._idx_cache: Dict[str, Tuple[int, LexicalIndex]] = {}

    # ---------- 索引 ----------
    def _items(self, user_id: Any) -> List[Dict[str, Any]]:
        try:
            return [i for i in self.store.list_items(user_id, include_hidden=False)]
        except Exception:
            return []

    def _index(self, user_id: Any) -> LexicalIndex:
        items = self._items(user_id)
        key = str(user_id)
        sig = len(items)
        cached = self._idx_cache.get(key)
        if cached and cached[0] == sig:
            return cached[1]
        idx = LexicalIndex()
        for it in items:
            text = "\n".join(str(x) for x in (
                it.get("title"), it.get("summary"), it.get("content"),
                " ".join(it.get("keywords") or []), " ".join(it.get("entities") or []),
                " ".join(it.get("locations") or [])) if x)
            idx.add_document(str(it.get("id")), text,
                             floor=int(it.get("floor_start") or 0), meta={"kind": it.get("kind")})
        self._idx_cache[key] = (sig, idx)
        return idx

    def invalidate(self, user_id: Any = None) -> None:
        if user_id is None:
            self._idx_cache.clear()
        else:
            self._idx_cache.pop(str(user_id), None)

    # ---------- 单轮三路 ----------
    def _path_lexical(self, idx: LexicalIndex, bundle: QueryBundle) -> Dict[str, float]:
        groups = bundle.lexical_groups()
        if not groups:
            return {}
        res = idx.search_groups(groups, top_k=60)
        return _scale_lexical({d: s for d, s, _ in res})

    def _path_vector(self, items: Sequence[Dict[str, Any]], bundle: QueryBundle
                     ) -> Tuple[Dict[str, float], Optional[List[float]]]:
        if not self.embed_fn:
            return {}, None
        srcs = [s for s in bundle.sources if (s.text or "").strip()]
        if not srcs:
            return {}, None
        try:
            vecs = self.embed_fn([s.text for s in srcs])
        except Exception:
            return {}, None
        qv = weighted_average_vectors(vecs, [s.weight for s in srcs])
        if not qv:
            return {}, None
        raw: Dict[str, float] = {}
        for it in items:
            v = self.store.get_vector(str(it.get("id"))) if hasattr(self.store, "get_vector") else it.get("vec")
            if not v:
                continue
            c = _cosine(qv, v)
            if c > 0:
                raw[str(it.get("id"))] = c
        return _scale_unit(raw, _VEC_LOW, _VEC_SPAN), qv

    def _path_graph(self, items: Sequence[Dict[str, Any]],
                    seeds: Sequence[str], vectors: Dict[str, Sequence[float]],
                    query_vector: Optional[Sequence[float]]) -> Dict[str, float]:
        if not seeds:
            return {}
        try:
            hits = diffuse_from_seeds(list(seeds), list(items), vectors, query_vector)
        except Exception:
            return {}
        return _scale_unit({h["id"]: h["score"] for h in hits}, _GRAPH_LOW, _GRAPH_SPAN)

    def _vectors_map(self, items: Sequence[Dict[str, Any]]) -> Dict[str, Sequence[float]]:
        out: Dict[str, Sequence[float]] = {}
        for it in items:
            v = self.store.get_vector(str(it.get("id"))) if hasattr(self.store, "get_vector") else it.get("vec")
            if v:
                out[str(it.get("id"))] = v
        return out

    def _one_round(self, user_id: Any, items: Sequence[Dict[str, Any]],
                   idx: LexicalIndex, bundle: QueryBundle, round_no: int,
                   use_graph: bool = True) -> List[RecallHit]:
        lex = self._path_lexical(idx, bundle)
        vec, qv = self._path_vector(items, bundle)
        vec_map = self._vectors_map(items)
        seeds = [d for d, _ in sorted({**lex, **vec}.items(),
                                      key=lambda kv: -kv[1])[:SEED_TOP_N]]
        graph = self._path_graph(items, seeds, vec_map, qv) if use_graph else {}

        by_id = {str(it.get("id")): it for it in items}
        cursor = 0
        try:
            conv = self.store._conv(user_id) or {}
            cursor = int(conv.get("cursor") or 0)
        except Exception:
            pass

        scores: Dict[str, float] = {}
        src_detail: Dict[str, Dict[str, float]] = {}
        for iid in set(lex) | set(vec) | set(graph):
            it = by_id.get(iid)
            if not it:
                continue
            seg = compute_segment_weights(int(it.get("floor_start") or 0), cursor)
            s = (self.path_weights.get("lexical", 0) * lex.get(iid, 0.0)
                 + self.path_weights.get("vector", 0) * vec.get(iid, 0.0)
                 + self.path_weights.get("graph", 0) * graph.get(iid, 0.0))
            if s <= 0:
                continue
            s *= (0.5 + 0.5 * seg)                     # 越近的剧情小幅加成
            if it.get("kind") == "anchor":
                s += self.anchor_prior * float(it.get("importance") or 0.5)
            scores[iid] = s
            src_detail[iid] = {"lexical": lex.get(iid, 0.0), "vector": vec.get(iid, 0.0),
                               "graph": graph.get(iid, 0.0), "segment": seg}
        hits = [RecallHit(iid, by_id[iid], int(by_id[iid].get("floor_start") or 0),
                          sc, src_detail[iid], round_no)
                for iid, sc in scores.items()]
        hits.sort(key=lambda h: -h.score)
        return hits

    # ---------- 对外 ----------
    def recall(self, user_id: Any, last_messages: Sequence[Any],
               top_k: int = DEFAULT_TOPK, min_score: float = 0.0,
               rounds: int = 2, use_graph: bool = True) -> List[Dict[str, Any]]:
        items = self._items(user_id)
        if not items:
            return []
        idx = self._index(user_id)
        bundle = build_query_bundle(last_messages, self.store, user_id, round_no=1)
        if not bundle.sources:
            return []
        hits = self._one_round(user_id, items, idx, bundle, 1, use_graph)
        if rounds >= 2 and hits:
            refine_query_bundle(
                bundle,
                anchor_hits=[h.to_dict() for h in hits if h.item.get("kind") == "anchor"],
                event_hits=[h.to_dict() for h in hits if h.item.get("kind") == "event"])
            hits2 = self._one_round(user_id, items, idx, bundle, 2, use_graph)
            hits = fuse_by_floor(list(hits) + list(hits2))
        else:
            hits = fuse_by_floor(hits)
        out = [h.to_dict() for h in hits if h.score >= min_score]
        return out[:top_k]

    def build_prompt_block(self, user_id: Any, last_messages: Sequence[Any],
                           top_k: int = DEFAULT_TOPK, max_chars: int = 1200) -> str:
        """把召回结果拼成可注入 Prompt 的记忆块（带预算裁剪）。"""
        hits = self.recall(user_id, last_messages, top_k=top_k)
        if not hits:
            return ""
        lines, used = [], 0
        for h in hits:
            it = h["item"]
            head = f"[{it.get('kind')}] {it.get('title') or ''}".strip()
            body = (it.get("summary") or it.get("content") or "").strip()
            seg = f"{head}：{body}" if body else head
            if used + len(seg) > max_chars and lines:
                break
            lines.append(seg)
            used += len(seg)
        if not lines:
            return ""
        return "【相关记忆】\n" + "\n".join(f"- {x}" for x in lines)
