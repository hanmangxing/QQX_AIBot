# -*- coding: utf-8 -*-
"""
P1 数据模型与存储层

长期记忆分成两类：
  events  剧情事件 —— 分段总结产出，绑定「楼层区间」floor_start/floor_end，
          带实体 / 互动对 / 地点 / 关键词，参与向量召回与图扩散召回。
  anchors 长期锚点 —— 稳定事实（人设、关系、世界观、重要承诺），
          不随剧情推进失效，默认常驻或按关键词激活。

存储沿用现有文件名（memory.json / memory_vectors.json），做到「原地升级、旧数据不丢」：
旧结构 {user_id: {cursor, entries, sender_name, updated_at, user_id}}
新结构 {user_id: {version:2, cursor, items:[...], sender_name, updated_at, user_id}}
迁移幂等：entries 按 kind 落到 events/anchors，cursor 原样保留，重复执行结果一致。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 2

ANCHOR_KINDS = {"anchor", "persona", "setting", "relation", "world", "fact", "preference", "promise"}
EVENT_KINDS = {"event", "plot", "scene", "progress", "dialogue", "summary"}


def _now() -> float:
    return time.time()


def _new_id(prefix: str = "m") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class MemoryItem:
    """events 与 anchors 的统一条目。"""

    id: str = field(default_factory=_new_id)
    kind: str = "event"                 # event | anchor | arc
    title: str = ""
    summary: str = ""                   # 精炼文本：参与检索与 Prompt 注入
    content: str = ""                   # 细节 / 原文摘录
    keywords: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)        # 实体（角色/物品/组织）
    interactions: List[List[str]] = field(default_factory=list)  # 互动对 [[A,B],...]
    locations: List[str] = field(default_factory=list)       # 地点
    # --- anchor：世界状态事实图谱 主体|谓词|值|趋势 ---
    predicate: str = ""                 # 属性名（身份 / 归属 / 立场 …）
    value: str = ""                     # 属性值
    trend: str = ""                     # 相对已有事实的变化：新增 / 更新 / 未变
    # --- arc：角色弧光 ---
    name: str = ""                      # 角色名
    stage: str = ""                     # 当前阶段（初识 / 试探 / 信任 / 决裂 …）
    trajectory: str = ""                # 变化轨迹
    importance: float = 0.5             # 重要度 0~1，参与注入预算排序
    floor_start: int = 0                # 起始楼层（= 消息下标）
    floor_end: int = 0                  # 结束楼层
    time_ts: float = field(default_factory=_now)   # 剧情发生时间（时间衰减用）
    msg_ts: float = 0.0                 # 该条目对应消息的真实发生时间（落库时固化，聊天记录被压缩后仍准确）
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    hidden: bool = False                # 隐藏：不注入
    source: str = "manual"              # manual | auto_summary | migrated
    vec: Optional[List[float]] = None   # 嵌入向量（也可外置到 memory_vectors.json）

    def text_for_index(self) -> str:
        """给词法索引/嵌入用的合成文本。"""
        parts = [self.title, self.summary, self.content, self.name, self.stage,
                 self.trajectory, self.predicate, self.value, " ".join(self.keywords),
                 " ".join(self.entities), " ".join(self.locations)]
        return "\n".join(p for p in parts if p)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "MemoryItem":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: v for k, v in (raw or {}).items() if k in known}
        data.setdefault("kind", "event")
        for lst in ("keywords", "entities", "locations"):
            v = data.get(lst)
            if isinstance(v, str):
                data[lst] = [s.strip() for s in v.replace("，", ",").split(",") if s.strip()]
            elif not isinstance(v, list):
                data[lst] = []
        inter = data.get("interactions")
        if not isinstance(inter, list):
            data["interactions"] = []
        else:
            data["interactions"] = [list(p) for p in inter if isinstance(p, (list, tuple)) and len(p) >= 2]
        for num in ("importance", "floor_start", "floor_end", "msg_ts", "time_ts", "created_at", "updated_at"):
            raw_v = data.get(num)
            if raw_v is None or raw_v == "":
                continue                      # 缺省时沿用 dataclass 默认值
            try:
                data[num] = float(raw_v) if num in ("importance", "msg_ts") else int(float(raw_v))
            except (TypeError, ValueError):
                data.pop(num, None)
        return cls(**data)


def _classify_kind(old_kind: str) -> str:
    k = (old_kind or "").strip().lower()
    if k in ANCHOR_KINDS:
        return "anchor"
    if k in EVENT_KINDS:
        return "event"
    if k == "arc":
        return "arc"
    return "event"


def migrate_legacy_conversation(conv: Dict[str, Any]) -> Dict[str, Any]:
    """把旧结构的单个用户会话升级为新结构（幂等）。"""
    conv = dict(conv or {})
    if conv.get("version") == SCHEMA_VERSION and isinstance(conv.get("items"), list):
        return conv

    items: List[Dict[str, Any]] = []
    for raw in conv.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        kind = _classify_kind(str(raw.get("kind") or ""))
        items.append(MemoryItem(
            id=str(raw.get("id") or _new_id()),
            kind=kind,
            title=str(raw.get("title") or ""),
            summary=str(raw.get("text") or raw.get("summary") or ""),
            content=str(raw.get("text") or raw.get("content") or ""),
            keywords=list(raw.get("keywords") or []),
            entities=list(raw.get("entities") or []),
            importance=float(raw.get("importance") or (0.8 if kind == "anchor" else 0.5)),
            time_ts=float(raw.get("created_at") or _now()),
            created_at=float(raw.get("created_at") or _now()),
            updated_at=float(raw.get("updated_at") or raw.get("created_at") or _now()),
            hidden=bool(raw.get("hidden")),
            source="migrated",
        ).to_dict())

    conv["items"] = items
    conv["version"] = SCHEMA_VERSION
    conv.pop("entries", None)
    conv.setdefault("cursor", 0)
    conv.setdefault("sender_name", "")
    conv.setdefault("updated_at", _now())
    return conv



# ---------- 记忆作用域（来源标记） ----------
ALL_SCOPE = "all"        # 只读聚合视图：跨所有来源
GLOBAL_SCOPE = "global"  # 跨会话共享桶


def _norm_scope(user_id):
    """聚合视图只读；任何写入都落到共享桶。"""
    return GLOBAL_SCOPE if str(user_id) == ALL_SCOPE else user_id


class LwbStore:
    """线程安全、原子写盘的事件/锚点存储。"""

    def __init__(self, path: Path | str = Path("memory.json"),
                 vector_path: Path | str = Path("memory_vectors.json")):
        self.path = Path(path)
        self.vector_path = Path(vector_path)
        self._lock = threading.RLock()
        self.conversations: Dict[str, Dict[str, Any]] = {}
        self.vectors: Dict[str, List[float]] = {}
        self.load()

    # ---------- 持久化 ----------
    def load(self) -> None:
        with self._lock:
            self.conversations = {}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    raw = {}
                if isinstance(raw, dict):
                    for uid, conv in raw.items():
                        if isinstance(conv, dict):
                            self.conversations[str(uid)] = migrate_legacy_conversation(conv)
            self.vectors = {}
            if self.vector_path.exists():
                try:
                    v = json.loads(self.vector_path.read_text(encoding="utf-8"))
                    if isinstance(v, dict):
                        self.vectors = {str(k): val for k, val in v.items()}
                except Exception:
                    self.vectors = {}

    def _atomic_write(self, path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def save(self) -> None:
        with self._lock:
            self._atomic_write(self.path, self.conversations)

    def save_vectors(self) -> None:
        with self._lock:
            self._atomic_write(self.vector_path, self.vectors)

    # ---------- 会话 ----------
    def _conv(self, user_id: Any, create: bool = False) -> Optional[Dict[str, Any]]:
        user_id = _norm_scope(user_id)
        key = str(user_id)
        conv = self.conversations.get(key)
        if conv is None and create:
            conv = {"version": SCHEMA_VERSION, "user_id": key, "sender_name": "",
                    "cursor": 0, "items": [], "updated_at": _now()}
            self.conversations[key] = conv
        return conv

    def touch(self, user_id: Any, sender_name: str = "") -> None:
        conv = self._conv(user_id, create=True)
        if sender_name:
            conv["sender_name"] = sender_name
        conv["updated_at"] = _now()

    # ---------- 条目 CRUD ----------
    def list_items(self, user_id: Any, kind: Optional[str] = None, include_hidden: bool = True) -> List[Dict[str, Any]]:
        if str(user_id) == ALL_SCOPE:
            agg: List[Dict[str, Any]] = []
            for _k, _c in self.conversations.items():
                if str(_k) == ALL_SCOPE:
                    continue
                for _it in (_c.get("items") or []):
                    if kind and _it.get("kind") != kind:
                        continue
                    if not include_hidden and _it.get("hidden"):
                        continue
                    agg.append(_it)
            return agg
        conv = self._conv(user_id)
        if not conv:
            return []
        out = []
        for it in conv.get("items", []):
            if kind and it.get("kind") != kind:
                continue
            if not include_hidden and it.get("hidden"):
                continue
            out.append(it)
        return out

    def get_item(self, user_id: Any, item_id: str) -> Optional[Dict[str, Any]]:
        conv = self._conv(user_id)
        if not conv:
            return None
        for it in conv.get("items", []):
            if str(it.get("id")) == str(item_id):
                return it
        return None

    def add_item(self, user_id: Any, item: Dict[str, Any] | MemoryItem, sender_name: str = "") -> Dict[str, Any]:
        data = item.to_dict() if isinstance(item, MemoryItem) else MemoryItem.from_dict(item).to_dict()
        with self._lock:
            conv = self._conv(user_id, create=True)
            if sender_name:
                conv["sender_name"] = sender_name
            conv.setdefault("items", []).append(data)
            conv["updated_at"] = _now()
            self.save()
        return data

    def add_items(self, user_id: Any, items: List[Dict[str, Any] | MemoryItem], sender_name: str = "") -> List[Dict[str, Any]]:
        out = []
        with self._lock:
            conv = self._conv(user_id, create=True)
            if sender_name:
                conv["sender_name"] = sender_name
            bucket = conv.setdefault("items", [])
            for it in items:
                data = it.to_dict() if isinstance(it, MemoryItem) else MemoryItem.from_dict(it).to_dict()
                bucket.append(data)
                out.append(data)
            conv["updated_at"] = _now()
            self.save()
        return out

    def update_item(self, user_id: Any, item_id: str, **fields) -> Optional[Dict[str, Any]]:
        with self._lock:
            conv = self._conv(user_id)
            if not conv:
                return None
            for it in conv.get("items", []):
                if str(it.get("id")) == str(item_id):
                    it.update({k: v for k, v in fields.items() if k in MemoryItem.__dataclass_fields__})
                    it["updated_at"] = _now()
                    conv["updated_at"] = _now()
                    self.save()
                    return it
        return None

    def delete_item(self, user_id: Any, item_id: str) -> bool:
        with self._lock:
            conv = self._conv(user_id)
            if not conv:
                return False
            items = conv.get("items", [])
            for i, it in enumerate(items):
                if str(it.get("id")) == str(item_id):
                    items.pop(i)
                    self.vectors.pop(str(item_id), None)
                    conv["updated_at"] = _now()
                    self.save()
                    self.save_vectors()
                    return True
        return False

    def clear(self, user_id: Any) -> bool:
        key = str(user_id)
        with self._lock:
            conv = self.conversations.pop(key, None)
            if conv:
                for it in conv.get("items", []):
                    self.vectors.pop(str(it.get("id")), None)
            self.save()
            self.save_vectors()
            return conv is not None

    # ---------- 统计 ----------
    def stats(self, user_id: Any) -> Dict[str, Any]:
        if str(user_id) == ALL_SCOPE:
            items = []
            for _k, _c in self.conversations.items():
                if str(_k) == ALL_SCOPE:
                    continue
                items.extend(_c.get("items") or [])
            events = [i for i in items if i.get("kind") == "event"]
            anchors = [i for i in items if i.get("kind") == "anchor"]
            convs = [c for k, c in self.conversations.items()
                     if str(k) not in (ALL_SCOPE, GLOBAL_SCOPE)]
            return {
                "events": len(events),
                "anchors": len(anchors),
                "total": len(items),
                "hidden": len([i for i in items if i.get("hidden")]),
                "cursor": 0,
                "vectorized": len([i for i in items if self.vectors.get(str(i.get("id")))]),
                "conversations": len(convs),
                "scope": ALL_SCOPE,
            }
        items = self.list_items(user_id)
        events = [i for i in items if i.get("kind") == "event"]
        anchors = [i for i in items if i.get("kind") == "anchor"]
        conv = self._conv(user_id) or {}
        return {
            "events": len(events),
            "anchors": len(anchors),
            "total": len(items),
            "hidden": len([i for i in items if i.get("hidden")]),
            "cursor": int(conv.get("cursor") or 0),
            "vectorized": len([i for i in items if self.vectors.get(str(i.get("id")))]),
        }

    def list_conversations(self) -> List[Dict[str, Any]]:
        out = []
        for uid, conv in self.conversations.items():
            if str(uid) in (ALL_SCOPE, GLOBAL_SCOPE):
                continue
            items = conv.get("items", [])
            out.append({
                "user_id": uid,
                "sender_name": conv.get("sender_name") or "",
                "count": len(items),
                "events": len([i for i in items if i.get("kind") == "event"]),
                "anchors": len([i for i in items if i.get("kind") == "anchor"]),
                "cursor": int(conv.get("cursor") or 0),
                "updated_at": conv.get("updated_at") or 0,
            })
        out.sort(key=lambda c: c["updated_at"], reverse=True)
        return out

    # ---------- 游标 ----------
    def commit_cursor(self, user_id: Any, cursor: int, sender_name: str = "") -> None:
        with self._lock:
            conv = self._conv(user_id, create=True)
            conv["cursor"] = int(cursor)
            if sender_name:
                conv["sender_name"] = sender_name
            conv["updated_at"] = _now()
            self.save()

    def set_vector(self, item_id: str, vec: Optional[List[float]]) -> None:
        with self._lock:
            if vec is None:
                self.vectors.pop(str(item_id), None)
            else:
                self.vectors[str(item_id)] = list(vec)
            self.save_vectors()

    def get_vector(self, item_id: str) -> Optional[List[float]]:
        return self.vectors.get(str(item_id))
