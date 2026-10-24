# -*- coding: utf-8 -*-
"""
P6 分段总结 / 事件 / 锚点生成

源实现要点：
  * 一次触发 = 一次调用：从上次总结位置切一片（上限 MAX_PER_RUN 楼）整片喂给模型；
  * 产出「事件」与「锚点（已确立的事实）」两类记忆，并抽取实体/互动对/地点；
  * 生成调用有超时保护（源实现 SUMMARY_GENERATION_TIMEOUT_MS = 180s，超时即失败重试）；
  * 注入侧用 estimateTokens 做预算裁剪，章节结构固定为：
        <剧情记忆>
        [定了的事] 已确立的事实
        [印象深的事] 记得很清楚
        [其他人的事] 别人经历的类似事
        [零散记忆] 没归入事件的片段
        [新鲜记忆] 还没总结的部分
        [人物弧光]
        </剧情记忆>

预算常量（原样照抄 prompt.js）：
    CONSTRAINT_MAX=2000  ARCS_MAX=1500  EVENT_BUDGET_MAX=5000  RELATED_EVENT_MAX=500
    EVENT_EVIDENCE_MAX=4000  DISTANT_EVIDENCE_MAX=2000  UNSUMMARIZED_EVIDENCE_MAX=2000
    TOP_N_STAR=5  L0_JOINED_MAX_LENGTH=120
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .store import MemoryItem, _new_id

CONSTRAINT_MAX = 2000
ARCS_MAX = 1500
EVENT_BUDGET_MAX = 5000
RELATED_EVENT_MAX = 500
EVENT_EVIDENCE_MAX = 4000
DISTANT_EVIDENCE_MAX = 2000
UNSUMMARIZED_EVIDENCE_MAX = 2000
TOTAL_BUDGET_MAX = 12000
TOP_N_STAR = 5
L0_JOINED_MAX_LENGTH = 120

MAX_PER_RUN = 400          # 源实现 buildIncrementalSlice(maxPerRun = 100)
                           # 本机调大：150 太小，一次触发只推进 150 楼，
                           # 压缩只能删到游标为止 → 会留下远多于「保留最近」的条数
BATCH_FLOORS = MAX_PER_RUN  # 兼容旧字段名
SUMMARY_MAX_CHARS = 3000   # 源实现：提示词要求 3000 字内
DEFAULT_TIMEOUT = 180      # 源实现 SUMMARY_GENERATION_TIMEOUT_MS = 180_000
MAX_RETRIES = 1


def estimate_tokens(text: str) -> int:
    """估算 token：中文按字计，其余按 4 字符 1 token。"""
    if not text:
        return 0
    s = str(text)
    zh = len(re.findall(r"[\u4e00-\u9fff]", s))
    return int(zh + (len(s) - zh) / 4) + (1 if (len(s) - zh) % 4 else 0)


SUMMARY_SYSTEM_PROMPT = (
    "你是一个剧情记忆整理助手，负责为长篇角色扮演整理可长期检索的剧情记忆。"
    "你必须只输出 JSON，不要输出任何解释、不要用代码块包裹。"
)

SUMMARY_PROMPT_TEMPLATE = """请阅读下面第 {start}~{end} 楼的对话，整理成剧情记忆。

对话每行开头的 #编号 是楼层号；方括号里是该条消息真实发生的时间，例如：
  #12 [2026-10-14 23:13] 天依：主人你回来啦～
这段对话可能横跨好几天，请以每条消息自己标注的时间为准，不要把整段都当成同一天。

要求：
1. 只抽取值得长期记住的内容，忽略寒暄和无关闲聊。
2. 分三类输出：
   - events：本段实际发生的剧情事件（谁对谁做了什么、结果如何），每条 summary 不超过 {max_chars} 字；
   - anchors：已经确立的稳定事实（人物身份、关系、世界观设定、重要承诺），这类不会随剧情推进失效。
     每条额外给出 predicate（属性名，如「身份」「归属」「立场」）、value（属性值）、
     trend（相对已有事实的变化，只能取：新增 / 更新 / 未变）；
   - arcs：主要角色的走向，只写本段里状态有明显变化的角色。每条给出 name（角色名）、
     stage（当前阶段，如「初识」「试探」「信任」「决裂」）、trajectory（一句话描述该角色到目前为止的变化轨迹）。
3. 每条 event / anchor 都要给出：
   - floor：这条内容主要来自第几楼（整数，直接填那个 # 编号）。若它确实横跨多楼、无法归到某一楼，
     就填它最先出现的楼层号。注意：如果这段对话跨了几天，不同条目要按各自实际发生的楼层填，
     不要所有条目都填同一个楼层。
   - title：6~14 字的短标题
   - summary：精炼叙述
   - entities：只填**人物**——真实的人、拟人角色、AI 角色、用户本人。同一个人固定用同一个名字，
     不要一会儿写全名一会儿写简称。物品、工具、软件、作品（歌名 / 书名 / 番剧名）、地点、机构，
     以及「主人」「大家」这类称谓，一律不要放进 entities，改放进 keywords 或 locations。
   - interactions：人物之间的互动对，形如 [["甲","乙"]]；只要这条里两个人有对话或互相提及就记一对，
     不要留空——没有互动对的人物不会出现在人物关系图里。
   - locations：地点
   - keywords：便于检索的关键词
   - importance：0~1 的重要度
4. 不要编造对话里没有的信息。三类都可以为空数组，但不要省略字段。

{existing_hint}
输出 JSON（严格按此结构，不要多字段）：
{{"events":[{{"floor":1,"title":"","summary":"","entities":[],"interactions":[],"locations":[],"keywords":[],"importance":0.5}}],
  "anchors":[{{"floor":1,"title":"","summary":"","entities":[],"interactions":[],"locations":[],"keywords":[],"importance":0.8,"predicate":"","value":"","trend":"新增"}}],
  "arcs":[{{"name":"","stage":"","trajectory":"","keywords":[],"importance":0.6}}]}}

对话：
{transcript}"""


def _msg_text(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        for k in ("content", "text", "message", "raw_message"):
            v = msg.get(k)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, list):
                return " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in v)
    return ""


def _msg_name(msg: Any, default: str = "") -> str:
    if isinstance(msg, dict):
        for k in ("sender_name", "name", "nickname", "user"):
            v = msg.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return default


def _msg_ts_text(msg: Any) -> str:
    """消息自己的发生时间，形如 "[2026-10-14 23:13] "；取不到就返回空串。"""
    if not isinstance(msg, dict):
        return ""
    for k in ("ts", "time", "timestamp", "time_ts", "created_at"):
        try:
            v = float(msg.get(k) or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            import datetime as _dt
            return "[" + _dt.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M") + "] "
    return ""

def _msg_ts_value(msg: Any) -> float:
    """消息自己的发生时间（epoch 秒）；取不到返回 0。"""
    if not isinstance(msg, dict):
        return 0.0
    for k in ("ts", "time", "timestamp", "time_ts", "created_at"):
        try:
            v = float(msg.get(k) or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0.0


def _merge_sender_alias(names: Any, sender_name: str = "") -> List[str]:
    """把「寒芒」这类对发送者显示名的简写归并回完整显示名，
    否则人物档案里会多出一个同名同人的空角色。"""
    s = str(sender_name or "").strip()
    out: List[str] = []
    for n in (names or []):
        t = str(n or "").strip()
        if not t:
            continue
        if s and t != s and min(len(t), len(s)) >= 2 and (s.startswith(t) or t.startswith(s)):
            t = s
        if t not in out:
            out.append(t)
    return out


def build_transcript(chunk: Sequence[Any], start_floor: int = 0) -> str:
    lines = []
    for i, m in enumerate(chunk):
        name = _msg_name(m, "用户" if i % 2 == 0 else "角色")
        text = _msg_text(m).strip()
        if not text:
            continue
        lines.append(f"#{start_floor + i + 1} {_msg_ts_text(m)}{name}：{text}")
    return "\n".join(lines)


def build_summary_prompt(chunk: Sequence[Any], existing: Optional[Sequence[Dict[str, Any]]] = None,
                         start_floor: int = 0, sender_name: str = "") -> List[Dict[str, str]]:
    """构造总结用的 chat messages。"""
    existing_hint = ""
    if existing:
        titles = [str(e.get("title") or "") for e in existing[:20] if e.get("title")]
        if titles:
            existing_hint = "已经记住的条目（不要重复）：" + "、".join(titles) + "\n"
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        start=start_floor + 1, end=start_floor + len(chunk),
        max_chars=SUMMARY_MAX_CHARS, existing_hint=existing_hint,
        transcript=build_transcript(chunk, start_floor))
    return [{"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}]


def _strip_json(raw: str) -> str:
    s = str(raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        return s[i:j + 1]
    return s


def parse_summary(raw: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把模型输出解析成 (events, anchors, arcs)。容错：字段缺失/类型不对都能兜住。"""
    try:
        data = json.loads(_strip_json(raw))
    except Exception:
        return [], [], []
    if not isinstance(data, dict):
        return [], [], []

    def norm_list(v: Any) -> List[str]:
        if isinstance(v, str):
            v = re.split(r"[，,、;；]", v)
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    def norm_pairs(v: Any) -> List[List[str]]:
        out = []
        if isinstance(v, list):
            for p in v:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    a, b = str(p[0]).strip(), str(p[1]).strip()
                    if a and b:
                        out.append([a, b])
        return out

    def build(kind: str, rows: Any) -> List[Dict[str, Any]]:
        out = []
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            summary = str(r.get("summary") or r.get("text") or "").strip()
            title = str(r.get("title") or "").strip()
            if not summary and not title:
                continue
            if len(summary) > SUMMARY_MAX_CHARS:
                summary = summary[:SUMMARY_MAX_CHARS]
            try:
                imp = float(r.get("importance"))
            except (TypeError, ValueError):
                imp = 0.8 if kind == "anchor" else 0.5
            try:
                flo = int(float(r.get("floor")))
            except (TypeError, ValueError):
                flo = 0
            row = {
                "kind": kind, "title": title, "summary": summary, "content": summary,
                "floor": flo,
                "entities": norm_list(r.get("entities")),
                "interactions": norm_pairs(r.get("interactions")),
                "locations": norm_list(r.get("locations")),
                "keywords": norm_list(r.get("keywords")),
                "importance": max(0.0, min(1.0, imp)),
                "source": "auto_summary",
            }
            if kind == "anchor":
                # 世界状态事实图谱：主体 | 谓词 | 值 | 趋势
                row["predicate"] = str(r.get("predicate") or "").strip()
                row["value"] = str(r.get("value") or "").strip()
                row["trend"] = str(r.get("trend") or "").strip() or "未变"
            out.append(row)
        return out

    def build_arcs(rows: Any) -> List[Dict[str, Any]]:
        """角色弧光：name + stage + trajectory。"""
        out = []
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            name = str(r.get("name") or r.get("character") or "").strip()
            traj = str(r.get("trajectory") or r.get("summary") or r.get("text") or "").strip()
            if not name or not traj:
                continue
            try:
                imp = float(r.get("importance"))
            except (TypeError, ValueError):
                imp = 0.6
            traj = traj[:SUMMARY_MAX_CHARS]
            out.append({
                "kind": "arc", "name": name, "title": name,
                "stage": str(r.get("stage") or "").strip(),
                "trajectory": traj, "summary": traj, "content": traj,
                "entities": [name] + [x for x in norm_list(r.get("entities")) if x != name],
                "interactions": norm_pairs(r.get("interactions")),
                "locations": norm_list(r.get("locations")),
                "keywords": norm_list(r.get("keywords")),
                "importance": max(0.0, min(1.0, imp)),
                "source": "auto_summary",
            })
        return out

    return (build("event", data.get("events")),
            build("anchor", data.get("anchors")),
            build_arcs(data.get("arcs")))


class Summarizer:
    """分批总结器：把未总结的消息按楼层分批送给模型，落库并推进游标。"""

    def __init__(self, store: Any,
                 call_fn: Callable[[List[Dict[str, str]]], str],
                 embed_fn: Optional[Callable[[List[str]], List[List[float]]]] = None,
                 batch_floors: int = BATCH_FLOORS,
                 max_per_run: int = MAX_PER_RUN,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = MAX_RETRIES):
        self.store = store
        self.call_fn = call_fn
        self.embed_fn = embed_fn
        self.batch_floors = max(1, int(batch_floors))
        self.max_per_run = max(1, int(max_per_run))
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))

    def pending(self, user_id: Any, messages: Sequence[Any]) -> List[Any]:
        """返回游标之后尚未总结的消息。"""
        try:
            conv = self.store._conv(user_id) or {}
        except Exception:
            conv = {}
        cursor = int(conv.get("cursor") or 0)
        return list(messages[cursor:])

    def _call_with_timeout(self, messages: List[Dict[str, str]]) -> str:
        """带超时的模型调用；超时/异常按 max_retries 重试。"""
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return self.call_fn(messages)
            except Exception as e:            # 网络/超时/解析都由调用方抛出
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(min(2.0 * (attempt + 1), 5.0))
        raise RuntimeError(f"总结生成失败：{last_err}")

    def summarize_chunk(self, chunk: Sequence[Any], existing: Optional[Sequence[Dict[str, Any]]] = None,
                        start_floor: int = 0, sender_name: str = "") -> Dict[str, Any]:
        """总结单个批次，返回 {events, anchors, raw, tokens}。"""
        prompt = build_summary_prompt(chunk, existing, start_floor, sender_name)
        raw = self._call_with_timeout(prompt)
        events, anchors, arcs = parse_summary(raw)
        return {"events": events, "anchors": anchors, "arcs": arcs, "raw": raw,
                "tokens": estimate_tokens(raw)}

    def summarize(self, user_id: Any, messages: Sequence[Any], sender_name: str = "",
                  advance_cursor: bool = True, on_progress: Optional[Callable[[int, int], None]] = None
                  ) -> Dict[str, Any]:
        """
        分批总结全部未处理消息并写入记忆库。
        返回 {batches, events, anchors, cursor, errors}。
        """
        pend = self.pending(user_id, messages)
        cursor0 = 0
        try:
            cursor0 = int((self.store._conv(user_id) or {}).get("cursor") or 0)
        except Exception:
            pass
        stats = {"batches": 0, "events": 0, "anchors": 0, "arcs": 0, "cursor": cursor0, "errors": []}
        if not pend:
            return stats

        # 源实现：一次触发只发一次请求，整片（上限 max_per_run 楼）交给填表模型
        total_batches = 1
        for bi in range(total_batches):
            lo = 0
            hi = min(len(pend), self.max_per_run)
            chunk = pend[lo:hi]
            start_floor = cursor0 + lo
            try:
                res = self.summarize_chunk(chunk, self.store.list_items(user_id),
                                           start_floor, sender_name)
            except Exception as e:
                stats["errors"].append({"batch": bi, "error": str(e)})
                break                          # 失败即停，游标不前进，下次可续
            items = []
            _lo = start_floor + 1                       # 楼层号是 1 基的：第 N 楼 = messages[N-1]
            _hi = start_floor + len(chunk)
            for row in res["events"] + res["anchors"] + list(res.get("arcs") or []):
                try:
                    _f = int(row.pop("floor", 0) or 0)
                except (TypeError, ValueError):
                    _f = 0
                if _f < _lo or _f > _hi:
                    # 模型没给或给越界了：退回整批区间（旧行为）
                    row["floor_start"] = _lo
                    row["floor_end"] = _hi
                else:
                    # 模型指认了归属楼层：精确到那一楼，日期也就精确到那一条消息
                    row["floor_start"] = _f
                    row["floor_end"] = _f
                # 固化这条内容对应消息的真实时间：聊天记录被压缩后依然能显示正确日期
                try:
                    _mi = int(row["floor_start"]) - 1 - start_floor
                    if 0 <= _mi < len(chunk):
                        _tv = _msg_ts_value(chunk[_mi])
                        if _tv > 0:
                            row["msg_ts"] = _tv
                except Exception:
                    pass
                # 人名归并：模型常把发送者显示名写成简写，归并回完整名
                try:
                    row["entities"] = _merge_sender_alias(row.get("entities"), sender_name)
                    row["interactions"] = [_merge_sender_alias(_pr, sender_name)
                                           for _pr in (row.get("interactions") or [])]
                    if row.get("kind") == "arc" and row.get("name"):
                        _nm = _merge_sender_alias([row.get("name")], sender_name)
                        if _nm:
                            row["name"] = _nm[0]
                            row["title"] = _nm[0]
                except Exception:
                    pass
                items.append(row)
            if items:
                saved = self.store.add_items(user_id, items, sender_name)
                self._index_vectors(saved)
                stats["events"] += len(res["events"])
                stats["anchors"] += len(res["anchors"])
                stats["arcs"] += len(res.get("arcs") or [])
            stats["batches"] += 1
            if advance_cursor:
                stats["cursor"] = start_floor + len(chunk)
                self.store.commit_cursor(user_id, stats["cursor"], sender_name)
            if on_progress:
                on_progress(bi + 1, total_batches)
        return stats

    def _index_vectors(self, items: Sequence[Dict[str, Any]]) -> None:
        if not self.embed_fn or not items:
            return
        texts = ["\n".join(str(x) for x in (i.get("title"), i.get("summary"),
                                            " ".join(i.get("keywords") or []),
                                            " ".join(i.get("entities") or [])) if x)
                 for i in items]
        try:
            vecs = self.embed_fn(texts)
        except Exception:
            return
        for it, v in zip(items, vecs):
            if v:
                try:
                    self.store.set_vector(str(it.get("id")), v)
                except Exception:
                    pass


# ---------------------------------------------------------------- 注入块装配
SECTION_ORDER = [
    ("constraints", "[定了的事] 已确立的事实", CONSTRAINT_MAX),
    ("direct",      "[印象深的事] 记得很清楚", EVENT_BUDGET_MAX),
    ("related",     "[其他人的事] 别人经历的类似事", RELATED_EVENT_MAX),
    ("distant",     "[零散记忆] 没归入事件的片段", DISTANT_EVIDENCE_MAX),
    ("recent",      "[新鲜记忆] 还没总结的部分", UNSUMMARIZED_EVIDENCE_MAX),
    ("arcs",        "[人物弧光]", ARCS_MAX),
]


def build_memory_block(constraints: Optional[Sequence[str]] = None,
                       direct_events: Optional[Sequence[Dict[str, Any]]] = None,
                       related_events: Optional[Sequence[Dict[str, Any]]] = None,
                       distant: Optional[Sequence[str]] = None,
                       recent: Optional[Sequence[str]] = None,
                       arcs: Optional[Sequence[Dict[str, Any]]] = None,
                       total_budget: int = TOTAL_BUDGET_MAX) -> str:
    """
    按源实现固定的章节顺序与预算装配 <剧情记忆> 注入块。
    预算裁剪逐节独立，超预算的条目直接跳过（不做截断），保证语义完整。
    """
    sections: List[str] = []
    used_total = 0

    def add(section: str, lines: List[str], budget: int) -> None:
        nonlocal used_total
        if not lines:
            return
        kept, used = [], 0
        for ln in lines:
            cost = estimate_tokens(ln)
            if used + cost > budget:
                break
            if used_total + used + cost > total_budget:
                break
            kept.append(ln)
            used += cost
        if kept:
            used_total += used
            sections.append(section + "\n" + "\n".join(kept))

    if constraints:
        add(SECTION_ORDER[0][1], [f"- {c}" for c in constraints], CONSTRAINT_MAX)

    ev_lines = []
    for i, ev in enumerate(direct_events or [], 1):
        t = f"{i}. {ev.get('title') or ev.get('summary','')[:12]}"
        s = (ev.get("summary") or "").strip()
        ev_lines.append(f"{t}\n {s}" if s else t)
    add(SECTION_ORDER[1][1], ev_lines, EVENT_BUDGET_MAX)

    rel_lines = [f"- {e.get('title') or (e.get('summary') or '')[:20]}"
                 for e in (related_events or [])]
    add(SECTION_ORDER[2][1], rel_lines, RELATED_EVENT_MAX)

    add(SECTION_ORDER[3][1], [f"- {d}" for d in (distant or [])], DISTANT_EVIDENCE_MAX)
    add(SECTION_ORDER[4][1], [f"- {r}" for r in (recent or [])], UNSUMMARIZED_EVIDENCE_MAX)

    arc_lines = [f"- {a.get('name')}：{a.get('trajectory') or a.get('summary','')}"
                 for a in (arcs or []) if a.get("name")]
    add(SECTION_ORDER[5][1], arc_lines, ARCS_MAX)

    if not sections:
        return ""
    return "<剧情记忆>\n\n" + "\n\n".join(sections) + "\n\n</剧情记忆>"
