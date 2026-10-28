# -*- coding: utf-8 -*-
"""
P9 接入层：把记忆引擎挂到 QQX_AIBot 现有 MemoryStore 上。

设计原则（重要）：
  * 不改 memory.py 的既有代码结构 —— 用模块级安装器 install_into_memory_store()
    包装 __init__ 并把公开方法重绑到本引擎，旧方法被遮蔽但不删除，出问题可一键摘掉；
  * 保持 app.py / bot.py 依赖的全部旧签名（list_entries / add_entry / recall /
    index_entries / stats / list_conversations / pending_messages / commit_cursor /
    add_entries_bulk / search / clear / update_entry / delete_entry）；
  * 旧属性 embedder / on_event 原样保留（先调原 __init__ 再挂引擎）；
  * 数据仍是同一个 memory.json，加载时自动迁移 entries → items，并写回兼容镜像，
    使旧代码即使直接读盘也能看到内容。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .diffusion import cosine as _cosine
from .generate import Summarizer, build_memory_block, estimate_tokens, TOP_N_STAR
from .query import build_query_bundle
from .recall import RecallEngine
from .state2 import StateEngine
from .store import LwbStore
from .store import ALL_SCOPE as _ALL_SCOPE, GLOBAL_SCOPE as _GLOBAL_SCOPE
from .tasks import TASKS_FILE, TaskScheduler, TaskStore


def _item_to_entry(it: Dict[str, Any]) -> Dict[str, Any]:
    """新模型 -> 旧 entry 形状（兼容旧前端与旧调用方）。"""
    return {
        "id": it.get("id"),
        "kind": it.get("kind") or "event",
        "title": it.get("title") or "",
        "text": it.get("summary") or it.get("content") or "",
        "summary": it.get("summary") or "",
        "content": it.get("content") or "",
        "keywords": list(it.get("keywords") or []),
        "entities": list(it.get("entities") or []),
        "interactions": list(it.get("interactions") or []),
        "locations": list(it.get("locations") or []),
        "importance": it.get("importance", 0.5),
        "floor_start": it.get("floor_start", 0),
        "floor_end": it.get("floor_end", 0),
        "time_ts": it.get("time_ts"),
        "created_at": it.get("created_at"),
        "updated_at": it.get("updated_at"),
        "hidden": bool(it.get("hidden")),
        "source": it.get("source"),
        # 事实图谱 / 角色弧光字段
        "predicate": it.get("predicate") or "",
        "value": it.get("value") or "",
        "trend": it.get("trend") or "",
        "name": it.get("name") or "",
        "stage": it.get("stage") or "",
        "trajectory": it.get("trajectory") or "",
    }


def _entry_to_item(e: Dict[str, Any]) -> Dict[str, Any]:
    """旧 entry 形状 -> 新模型。"""
    kind = str(e.get("kind") or "event")
    return {
        "id": e.get("id"),
        "kind": kind,
        "title": e.get("title") or "",
        "summary": e.get("summary") or e.get("text") or "",
        "content": e.get("content") or e.get("text") or "",
        "keywords": list(e.get("keywords") or []),
        "entities": list(e.get("entities") or []),
        "interactions": list(e.get("interactions") or []),
        "locations": list(e.get("locations") or []),
        "importance": e.get("importance", 0.5),
        "floor_start": e.get("floor_start", 0),
        "floor_end": e.get("floor_end", 0),
        "hidden": bool(e.get("hidden")),
        "source": e.get("source") or "manual",
        # 事实图谱 / 角色弧光字段
        "predicate": e.get("predicate") or "",
        "value": e.get("value") or "",
        "trend": e.get("trend") or "",
        "name": e.get("name") or "",
        "stage": e.get("stage") or "",
        "trajectory": e.get("trajectory") or "",
    }


class LwbMemory:
    """长期记忆引擎的统一门面。"""

    def __init__(self, path: Path | str = Path("memory.json"),
                 vector_path: Path | str = Path("memory_vectors.json"),
                 embedder: Any = None,
                 tasks_path: Path | str = TASKS_FILE,
                 path_weights: Optional[Dict[str, float]] = None):
        self.store = LwbStore(path, vector_path)
        self.embedder = embedder
        self.engine = RecallEngine(self.store, embed_fn=self._embed, path_weights=path_weights)
        self.summarizer = Summarizer(self.store, self._call_llm, embed_fn=self._embed)
        self.state = StateEngine()
        self.tasks = TaskScheduler(TaskStore(tasks_path))
        self._call_fn: Optional[Callable[[List[Dict[str, str]]], str]] = None

    # ---------- 基础设施 ----------
    def set_llm_caller(self, fn: Optional[Callable[[List[Dict[str, str]]], str]]) -> None:
        """由业务侧注入主对话 API 调用器（总结生成要用）。"""
        self._call_fn = fn

    def _call_llm(self, messages: List[Dict[str, str]]) -> str:
        if not self._call_fn:
            raise RuntimeError("未注入 LLM 调用器：请先 set_llm_caller(fn)")
        return self._call_fn(messages)

    def _embed(self, texts: List[str]) -> List[List[float]]:
        if not self.embedder:
            return []
        try:
            if hasattr(self.embedder, "embed"):
                return self.embedder.embed(texts) or []
            if callable(self.embedder):
                return self.embedder(texts) or []
        except Exception:
            return []
        return []

    # ---------- 旧接口：存储 ----------
    def list_entries(self, user_id: Any) -> List[Dict[str, Any]]:
        if str(user_id) == _ALL_SCOPE:
            out: List[Dict[str, Any]] = []
            for uid, conv in self.store.conversations.items():
                if str(uid) == _ALL_SCOPE:
                    continue
                for it in (conv.get("items") or []):
                    e = _item_to_entry(it)
                    e["scope"] = str(uid)
                    out.append(e)
            out.sort(key=lambda e: e.get("updated_at") or e.get("created_at") or 0, reverse=True)
            return out
        return [_item_to_entry(i) for i in self.store.list_items(user_id)]

    def get_entry(self, user_id: Any, entry_id: str) -> Optional[Dict[str, Any]]:
        it = self.store.get_item(user_id, entry_id)
        return _item_to_entry(it) if it else None

    def add_entry(self, user_id: Any, kind: str = "fact", title: str = "", text: str = "",
                  sender_name: str = "", **extra) -> Dict[str, Any]:
        row = _entry_to_item({"kind": kind, "title": title, "text": text, **extra})
        saved = self.store.add_item(user_id, row, sender_name)
        self.engine.invalidate(user_id)
        self._index_one(user_id, saved)
        return _item_to_entry(saved)

    def add_entries_bulk(self, user_id: Any, entries: List[Dict[str, Any]],
                         sender_name: str = "") -> List[Dict[str, Any]]:
        rows = [_entry_to_item(e) for e in (entries or [])]
        saved = self.store.add_items(user_id, rows, sender_name)
        self.engine.invalidate(user_id)
        self._index_many(user_id, saved)
        return [_item_to_entry(s) for s in saved]

    def update_entry(self, user_id: Any, entry_id: str, **fields) -> Optional[Dict[str, Any]]:
        patch = {}
        for k, v in fields.items():
            if k == "text":
                patch["summary"] = v; patch["content"] = v
            else:
                patch[k] = v
        out = self.store.update_item(user_id, entry_id, **patch)
        self.engine.invalidate(user_id)
        if out:
            self._index_one(user_id, out)
        return _item_to_entry(out) if out else None

    def delete_entry(self, user_id: Any, entry_id: str) -> bool:
        ok = self.store.delete_item(user_id, entry_id)
        self.engine.invalidate(user_id)
        return ok

    def clear(self, user_id: Any) -> bool:
        ok = self.store.clear(user_id)
        self.engine.invalidate(user_id)
        return ok

    def stats(self, user_id: Any, total_messages: int = 0) -> Dict[str, Any]:
        s = self.store.stats(user_id)
        return {
            "total": s["total"], "events": s["events"], "anchors": s["anchors"],
            "hidden": s["hidden"], "vectorized": s["vectorized"],
            "cursor": s["cursor"], "total_messages": int(total_messages or 0),
            "pending": max(0, int(total_messages or 0) - s["cursor"]),
        }

    def list_conversations(self, totals: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
        rows = self.store.list_conversations()
        for c in rows:
            if totals and str(c["user_id"]) in totals:
                c["total_messages"] = totals[str(c["user_id"])]
        return rows

    def pending_messages(self, user_id: Any, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        conv = self.store._conv(user_id) or {}
        cursor = int(conv.get("cursor") or 0)
        return list(messages[cursor:])

    def commit_cursor(self, user_id: Any, cursor: int, sender_name: str = "") -> None:
        self.store.commit_cursor(user_id, cursor, sender_name)

    # ---------- 向量 ----------
    def _index_one(self, user_id: Any, item: Dict[str, Any]) -> None:
        self._index_many(user_id, [item])

    def _index_many(self, user_id: Any, items: Sequence[Dict[str, Any]]) -> None:
        if not self.embedder or not items:
            return
        from .store import MemoryItem
        texts = [MemoryItem.from_dict(i).text_for_index() for i in items]
        vecs = self._embed(texts)
        for it, v in zip(items, vecs):
            if v:
                self.store.set_vector(str(it.get("id")), v)

    def index_entries(self, user_id: Any, force: bool = False) -> Dict[str, Any]:
        items = self.store.list_items(user_id)
        if not items:
            return {"indexed": 0, "total": 0, "vectorized": 0, "ok": True,
                    "message": "该用户还没有记忆条目"}
        todo = [i for i in items if force or not self.store.get_vector(str(i.get("id")))]
        if not todo:
            s = self.store.stats(user_id)
            return {"indexed": 0, "total": s["total"], "vectorized": s["vectorized"],
                    "ok": True, "message": "全部 %d 条记忆已有最新向量" % s["total"]}
        try:
            self._index_many(user_id, todo)
        except Exception as e:
            s = self.store.stats(user_id)
            return {"indexed": 0, "total": s["total"], "vectorized": s["vectorized"],
                    "ok": False, "message": "建立向量失败：%s" % e}
        s = self.store.stats(user_id)
        return {"indexed": len(todo), "total": s["total"], "vectorized": s["vectorized"],
                "ok": True,
                "message": "已为 %d 条记忆建立向量（共 %d 条）" % (len(todo), s["total"])}

    # ---------- 检索 ----------
    def recall(self, user_id: Any, query: Any, top_k: int = 5,
               min_score: float = 0.25,
               extra_user_ids: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        """旧签名兼容：query 为字符串时按单条消息处理；也可直接传消息列表。

        extra_user_ids 非空时，把这些会话也一起召回（跨会话召回）。
        每个会话各自跑一遍完整的三路混合检索，再按分数合并排序，
        命中条目会带 from_user_id / is_other 两个标记，方便调用方区分来源。
        """
        msgs = [{"content": query, "floor": 0}] if isinstance(query, str) else list(query or [])
        targets = [user_id]
        for u in (extra_user_ids or []):
            if u is None or str(u) == "":
                continue
            if any(str(u) == str(t) for t in targets):
                continue
            targets.append(u)
        merged: List[Dict[str, Any]] = []
        for u in targets:
            try:
                hits = self.recall_hybrid(u, msgs, top_k=top_k)
            except Exception:
                hits = []
            for h in hits:
                if h["score"] < min_score and min_score > 0:
                    continue
                e = _item_to_entry(h["item"])
                e["score"] = h["score"]
                e["sources"] = h["sources"]
                e["from_user_id"] = u
                e["is_other"] = str(u) != str(user_id)
                merged.append(e)
        merged.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return merged[:max(1, int(top_k))]

    def other_user_ids(self, user_id: Any,
                       allow: Optional[Sequence[Any]] = None) -> List[Any]:
        """同一记忆库里除 user_id 之外的其他会话 UID。

        allow 非空时只返回其中的（白名单模式）；allow 为空表示全部共享。
        """
        allow_set = {str(a).strip() for a in (allow or []) if str(a).strip()}
        out: List[Any] = []
        try:
            rows = self.store.list_conversations()
        except Exception:
            rows = []
        for c in rows:
            uid = (c or {}).get("user_id")
            if uid is None or str(uid) == "" or str(uid) == str(user_id):
                continue
            if allow_set and str(uid) not in allow_set:
                continue
            out.append(uid)
        return out

    def recall_hybrid(self, user_id: Any, last_messages: Sequence[Any],
                      top_k: int = 8, rounds: int = 2) -> List[Dict[str, Any]]:
        return self.engine.recall(user_id, last_messages, top_k=top_k, rounds=rounds)

    def search(self, user_id: Any, query: str, top_k: int = 30) -> List[Dict[str, Any]]:
        idx = self.engine._index(user_id)
        from .lexical import extract_key_terms
        terms = extract_key_terms(str(query or ""), 10)
        hits = idx.search(terms, top_k=top_k)
        by_id = {str(i.get("id")): i for i in self.store.list_items(user_id)}
        out = []
        for did, score in hits:
            it = by_id.get(did)
            if it:
                e = _item_to_entry(it)
                e["score"] = score
                out.append(e)
        return out

    def _hit_characters(self, user_id: Any, last_messages: Sequence[Any],
                        limit: int = 6) -> List[str]:
        """从最近几条消息里认出出场角色：别名表 + 记忆库实体词表双向匹配。"""
        if not last_messages:
            return []
        buf = []
        for m in last_messages:
            if isinstance(m, dict):
                buf.append(str(m.get("content") or m.get("text") or m.get("message") or ""))
            else:
                buf.append(str(m))
        text = " ".join(buf)
        if not text.strip():
            return []
        from .insight import list_characters
        aliases = self.aliases()
        out: List[str] = []
        for c in list_characters(self.store.list_items(user_id), aliases):
            nm = str(c.get("name") or "")
            if nm and nm in text and nm not in out:
                out.append(nm)
        for al, main in aliases.items():
            if al in text and main not in out:
                out.append(main)
        out.sort(key=lambda n: text.count(n), reverse=True)
        return out[:limit]

    def build_memory_block(self, user_id: Any, last_messages: Sequence[Any],
                           top_k: int = 8, max_chars: int = 1200) -> str:
        """混合召回 + 章节结构装配的注入块。"""
        hits = self.recall_hybrid(user_id, last_messages, top_k=top_k)
        events = [h["item"] for h in hits if h["item"].get("kind") == "event"]
        anchors = [h["item"] for h in hits if h["item"].get("kind") == "anchor"]
        constraints = [a.get("summary") or a.get("title") for a in anchors if (a.get("summary") or a.get("title"))]
        # 人物弧光：优先用召回到的，不足再从库里按重要度补足 TOP_N_STAR 条
        arcs = [h["item"] for h in hits if h["item"].get("kind") == "arc"]
        if len(arcs) < TOP_N_STAR:
            seen_ids = {str(a.get("id")) for a in arcs}
            extra = [i for i in self.store.list_items(user_id, kind="arc")
                     if str(i.get("id")) not in seen_ids]
            extra.sort(key=lambda x: x.get("importance") or 0, reverse=True)
            arcs.extend(extra[:TOP_N_STAR - len(arcs)])
        # 按当前对话动态注入：只补本轮出场角色的事件 / 关系 / 相关事实，不灌无关角色
        hit_names = self._hit_characters(user_id, last_messages)
        char_events: List[Dict[str, Any]] = []
        char_facts: List[str] = []
        if hit_names:
            seen_ids = {str(h["item"].get("id")) for h in hits}
            for nm in hit_names:
                try:
                    prof = self.character_profile(user_id, nm, limit=12)
                except Exception:
                    continue
                for e in (prof.get("events") or [])[:5]:
                    if str(e.get("id")) not in seen_ids:
                        seen_ids.add(str(e.get("id")))
                        char_events.append(e)
                for a in (prof.get("anchors") or [])[:4]:
                    pred, val = str(a.get("predicate") or ""), str(a.get("value") or "")
                    if not pred or not val or pred == "—":
                        continue
                    # 召回已经给过的锚点不再重复补
                    if val in " ".join(list(constraints) + char_facts):
                        continue
                    char_facts.append(f"{a.get('subject') or nm}的{pred}：{val}")
                rels = prof.get("relations") or []
                if rels:
                    char_facts.append(nm + "常来往：" + "、".join(
                        f"{r['name']}({r['count']})" for r in rels[:4]))
        constraints = list(constraints) + char_facts
        direct = events[:5]
        related = events[5:] + char_events
        task_block = self.tasks.build_task_prompt()
        blk = build_memory_block(constraints=constraints, direct_events=direct,
                                 related_events=related, arcs=arcs)
        if not blk and not task_block:
            return ""
        parts = [p for p in (blk, task_block) if p]
        text = "\n\n".join(parts)
        return text[:max_chars * 3] if max_chars else text

    # ---------- 生成 ----------
    def summarize(self, user_id: Any, messages: Sequence[Any], sender_name: str = "",
                  advance_cursor: bool = True) -> Dict[str, Any]:
        res = self.summarizer.summarize(user_id, messages, sender_name, advance_cursor)
        self.engine.invalidate(user_id)
        return res

    # ---------- 状态变量 ----------
    def apply_state_message(self, message_id: str, content: str, floor: int = 0) -> Dict[str, Any]:
        return self.state.apply_message(message_id, content, floor)

    def state_snapshot(self) -> Dict[str, Any]:
        return self.state.snapshot()

    def state_semantic_log(self, limit: int = 50) -> List[str]:
        return self.state.semantic_log(limit)

    # ---------- 可视化聚合（人物关系 / 世界状态 / 人物档案） ----------
    def _alias_path(self):
        import json as _json  # noqa: F401
        try:
            return Path(self.store.path).with_name("lwb_aliases.json")
        except Exception:
            return Path("lwb_aliases.json")

    def aliases(self) -> Dict[str, str]:
        """别名映射：别名 -> 角色主名。首次访问时从 lwb_aliases.json 读。"""
        cur = getattr(self, "_aliases", None)
        if cur is None:
            import json as _json
            cur = {}
            try:
                raw = _json.loads(self._alias_path().read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cur = {str(k): str(v) for k, v in raw.items()}
            except Exception:
                cur = {}
            self._aliases = cur
        return dict(cur)

    def set_aliases(self, mapping: Dict[str, str]) -> Dict[str, str]:
        """保存别名映射；别名与主名相同的条目会被丢弃。"""
        import json as _json
        self._aliases = {str(k).strip(): str(v).strip()
                         for k, v in (mapping or {}).items()
                         if str(k).strip() and str(v).strip() and str(k).strip() != str(v).strip()}
        try:
            self._alias_path().write_text(
                _json.dumps(self._aliases, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return dict(self._aliases)

    def _override_path(self):
        try:
            return Path(self.store.path).with_name("lwb_overrides.json")
        except Exception:
            return Path("lwb_overrides.json")

    def overrides(self) -> Dict[str, str]:
        """关系档位手动覆盖：{"甲|乙": "亲密"}。自动算出的档位优先级更低。"""
        cur = getattr(self, "_overrides", None)
        if cur is None:
            import json as _json
            cur = {}
            try:
                raw = _json.loads(self._override_path().read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cur = {str(k): str(v) for k, v in raw.items()}
            except Exception:
                cur = {}
            self._overrides = cur
        return dict(cur)

    def set_overrides(self, mapping: Dict[str, str]) -> Dict[str, str]:
        """保存关系档位覆盖；值不在 7 档里的条目会被丢弃。"""
        import json as _json
        from .insight import RELATION_TIERS
        self._overrides = {str(k).strip(): str(v).strip()
                           for k, v in (mapping or {}).items()
                           if str(k).strip() and str(v).strip() in RELATION_TIERS}
        try:
            self._override_path().write_text(
                _json.dumps(self._overrides, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return dict(self._overrides)

    def insight(self, user_id: Any, top_n: int = 12) -> Dict[str, Any]:
        """三个视图的数据：人物关系图 / 世界状态事实图谱 / 角色列表。"""
        from .insight import build_overview
        return build_overview(self.store.list_items(user_id), self.aliases(),
                              top_n=top_n, overrides=self.overrides())

    def character_profile(self, user_id: Any, name: str, limit: int = 60) -> Dict[str, Any]:
        """某个角色的人物档案：重要事件 + 相关事实 + 角色弧光 + 关系对象。"""
        from .insight import build_profile
        return build_profile(self.store.list_items(user_id), name, self.aliases(), limit=limit)

    def state_variables(self) -> Dict[str, Any]:
        return self.state.variables


# ---------------------------------------------------------------- 安装器
def install_into_memory_store(cls: type) -> type:
    """
    把 LwbMemory 挂到已有 MemoryStore 类上：
      * 包装 __init__：先执行原初始化（保留 embedder / on_event 等属性），再挂 self.lwb；
      * 重绑公开方法到 self.lwb，旧实现仍在原类里，删除本安装器即可完全还原。
    """
    orig_init = cls.__init__

    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        orig_init(self, *args, **kwargs)
        path = getattr(self, "path", None) or Path("memory.json")
        vpath = getattr(self, "vector_path", None) or Path("memory_vectors.json")
        embedder = getattr(self, "embedder", None)
        self.lwb = LwbMemory(path, vpath, embedder=embedder)
        self.engine = self.lwb.engine
        self.state = self.lwb.state
        self.tasks = self.lwb.tasks

    cls.__init__ = __init__  # type: ignore[assignment]

    delegates = [
        "list_entries", "get_entry", "add_entry", "add_entries_bulk", "update_entry",
        "delete_entry", "clear", "stats", "list_conversations", "pending_messages",
        "commit_cursor", "index_entries", "recall", "search", "other_user_ids",
    ]
    for name in delegates:
        if not hasattr(LwbMemory, name):
            continue

        def make(nm: str):  # type: ignore[no-untyped-def]
            def method(self, *a, **kw):  # type: ignore[no-untyped-def]
                return getattr(self.lwb, nm)(*a, **kw)
            method.__name__ = nm
            return method

        setattr(cls, name, make(name))

    # 新增能力
    cls.recall_hybrid = lambda self, *a, **kw: self.lwb.recall_hybrid(*a, **kw)
    cls.build_lwb_block = lambda self, *a, **kw: self.lwb.build_memory_block(*a, **kw)
    cls.summarize_lwb = lambda self, *a, **kw: self.lwb.summarize(*a, **kw)
    cls.set_llm_caller = lambda self, fn: self.lwb.set_llm_caller(fn)
    cls.state_snapshot = lambda self, *a, **kw: self.lwb.state_snapshot(*a, **kw)
    cls.state_semantic_log = lambda self, *a, **kw: self.lwb.state_semantic_log(*a, **kw)
    cls.apply_state_message = lambda self, *a, **kw: self.lwb.apply_state_message(*a, **kw)
    cls.state_variables = lambda self: self.lwb.state_variables()
    cls.lwb_insight = lambda self, *a, **kw: self.lwb.insight(*a, **kw)
    cls.lwb_profile = lambda self, *a, **kw: self.lwb.character_profile(*a, **kw)
    cls.lwb_aliases = lambda self: self.lwb.aliases()
    cls.lwb_set_aliases = lambda self, *a, **kw: self.lwb.set_aliases(*a, **kw)
    cls.lwb_overrides = lambda self: self.lwb.overrides()
    cls.lwb_set_overrides = lambda self, *a, **kw: self.lwb.set_overrides(*a, **kw)
    return cls


# ==================== 重排序 / L0 锚点抽取 接入（自动生成）====================
# 说明：recall 融合后的结果再走一次 lwb_rerank_api 重排；L0 锚点抽取走 lwb_l0_api。
# 未配置对应槽位时自动降级为原行为，不会报错。

def _lwb_set_rerank_caller(self, fn) -> None:
    self._rerank_fn = fn


def _lwb_set_anchor_caller(self, fn) -> None:
    self._anchor_fn = fn


def _lwb_rerank(self, query: str, items, top_k: int):
    """对召回结果做一次重排序；失败或无调用器时原样返回前 top_k。"""
    fn = getattr(self, "_rerank_fn", None)
    if not fn or not items:
        return list(items)[:top_k]
    try:
        docs = [str(i.get("summary") or i.get("content") or i.get("title") or "")
                for i in items]
        scores = fn(query or "", docs)
        if not scores or len(scores) != len(items):
            return list(items)[:top_k]
        order = sorted(range(len(items)), key=lambda i: -float(scores[i]))
        out = []
        for idx in order[:top_k]:
            it = dict(items[idx])
            it["rerank_score"] = round(float(scores[idx]), 6)
            out.append(it)
        return out
    except Exception:
        return list(items)[:top_k]


_orig_recall_hybrid = LwbMemory.recall_hybrid


def _recall_hybrid_reranked(self, user_id, last_messages, top_k: int = 8, **kw):
    """先宽召回，再用重排序模型精排到 top_k。"""
    try:
        wide = max(int(top_k) * 3, int(top_k))
    except Exception:
        wide = top_k
    items = _orig_recall_hybrid(self, user_id, last_messages, top_k=wide, **kw)
    if not isinstance(items, list) or not items:
        return items
    if getattr(self, "_rerank_fn", None) is None:
        return items[:top_k]
    q = ""
    try:
        msgs = list(last_messages)[-2:]
        q = " ".join(str(m.get("content") or m.get("text") or "") for m in msgs
                     if isinstance(m, dict))
    except Exception:
        q = ""
    return self._rerank(q, items, top_k)


LWB_L0_SYSTEM = (
    "你是剧情记忆抽取器。从给定对话片段中抽取值得长期记住的「锚点」（人物设定、关系、"
    "长期承诺、重要物品、地点、能力变化等）。只输出 JSON，不要解释。"
)
LWB_L0_SCHEMA = (
    '{"anchors":[{"floor":1,"title":"简短标题","summary":"一句话概括","content":"细节",'
    '"keywords":["词"],"entities":["人名"],"locations":["地点"],"importance":0.0}]}'
)


def _lwb_extract_anchors(self, user_id, messages, sender_name: str = "", chunk_size: int = 0):
    """走 lwb_l0_api 抽取锚点并入库；未配置时返回 []。

    整批一次请求（chunk_size=0）——150 条对话的文本量一次请求完全够，
    分段反而会让模型看不到全局、把同一件事重复抽好几遍。传 chunk_size>0 才分段。
    """
    fn = getattr(self, "_anchor_fn", None)
    if not fn or not messages:
        return []
    from .generate import (build_transcript, _strip_json,
                           _msg_ts_value, _merge_sender_alias)
    import json as _json
    msgs = list(messages)
    size = int(chunk_size or 0)
    spans = ([(s, msgs[s:s + size]) for s in range(0, len(msgs), size)]
             if size > 0 else [(0, msgs)])
    saved = []
    for start, part in spans:
        transcript = build_transcript(part, start)
        if not transcript.strip():
            continue
        prompt = (f"对话片段：\n{transcript}\n\n"
                  f"每行开头的 #编号 是楼层号；方括号里是该条消息真实发生的时间。"
                  f"这段对话可能横跨好几天，请以每条消息自己标注的时间为准，不要把整段都当成同一天。\n"
                  f"每条锚点额外给出 floor：这条内容主要来自第几楼（整数，直接填那个 # 编号）；"
                  f"若它确实横跨多楼、无法归到某一楼，就填它最先出现的楼层号。"
                  f"跨天时不同锚点要按各自实际发生的楼层填，不要都填同一个楼层。\n"
                  f"请抽取锚点，严格按此 JSON 结构输出：\n{LWB_L0_SCHEMA}")
        try:
            raw = fn([{"role": "system", "content": LWB_L0_SYSTEM},
                      {"role": "user", "content": prompt}])
        except Exception:
            continue
        try:
            data = _json.loads(_strip_json(raw) or "{}")
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        anchors = data.get("anchors") or data.get("items") or []
        for a in anchors:
            if not isinstance(a, dict):
                continue
            try:
                _lo = int(start) + 1          # 楼层号 1 基：第 N 楼 = messages[N-1]
                _hi = int(start) + len(part)
                try:
                    _f = int(float(a.get("floor")))
                except (TypeError, ValueError):
                    _f = 0
                if _f < _lo or _f > _hi:
                    _fs, _fe = _lo, _hi       # 没给或越界：退回这一批的楼层区间
                else:
                    _fs, _fe = _f, _f         # 指认了归属楼层：精确到那一楼
                _tsv = 0.0
                try:
                    _mi = int(_fs) - 1 - int(start)
                    if 0 <= _mi < len(part):
                        _tsv = _msg_ts_value(part[_mi])
                except Exception:
                    _tsv = 0.0
                row = {
                    "kind": "anchor",
                    "title": str(a.get("title") or "")[:120],
                    "summary": str(a.get("summary") or ""),
                    "content": str(a.get("content") or ""),
                    "keywords": a.get("keywords") or [],
                    "entities": a.get("entities") or [],
                    "locations": a.get("locations") or [],
                    "predicate": str(a.get("predicate") or ""),
                    "value": str(a.get("value") or ""),
                    "trend": str(a.get("trend") or "新增"),
                    "importance": float(a.get("importance") or 0.6),
                    "floor_start": _fs,
                    "floor_end": _fe,
                    "msg_ts": _tsv,
                }
                try:
                    row["entities"] = _merge_sender_alias(row.get("entities"), sender_name)
                except Exception:
                    pass
                item = self.store.add_item(user_id, row)
                if item:
                    saved.append(item)
                    try:
                        self._index_one(user_id, item)
                    except Exception:
                        pass
            except Exception:
                continue
    if saved:
        try:
            self.engine.invalidate(user_id)
        except Exception:
            pass
    return saved

LwbMemory.set_rerank_caller = _lwb_set_rerank_caller
LwbMemory.set_anchor_caller = _lwb_set_anchor_caller
LwbMemory._rerank = _lwb_rerank
LwbMemory.extract_anchors = _lwb_extract_anchors
LwbMemory.recall_hybrid = _recall_hybrid_reranked
# ==================== 结束 ====================
