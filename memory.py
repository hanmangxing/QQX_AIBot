# -*- coding: utf-8 -*-
"""
QQbotX 长期记忆库（向量版）

设计参考酒馆插件的记忆分层思路（逐楼锚点 + 结构化记忆 + 向量召回），
但完全独立实现，不依赖酒馆运行时。

- 记忆条目 entry：把聊天记录抽取成结构化条目（事件/事实/人物/状态/关系），落盘 memory.json；
- 向量索引：调用可配置的嵌入服务把条目转成向量，落盘 memory_vectors.json；
- 召回 recall()：用当前用户输入做余弦相似度检索，返回 top-k 供 Prompt 注入；
- 目的：治上下文溢出 —— 只注入「相关记忆 + 最近 N 条原文」，而不是把全部历史塞进 Prompt。
"""
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("QQBotCore")

MEMORY_FILE = Path("memory.json")
MEMORY_VECTOR_FILE = Path("memory_vectors.json")

MEMORY_KIND_LABEL = {
    "event": "事件",
    "fact": "事实",
    "character": "人物",
    "state": "状态",
    "relation": "关系",
}

DEFAULT_MEMORY_PROMPT = """你是一个剧情记忆整理助手。请从下面的对话片段中，抽取值得长期记住的信息。

要求：
1. 只记录有原文依据的内容，不推测、不补写尚未发生的结局；
2. 按条目输出，每条含 kind / title / text / keywords：
   - kind：event(发生过的事件) / fact(稳定设定或事实) / character(人物特征) / state(当前状态：地点、情绪、承诺、未了之事) / relation(两人关系变化)
   - title：不超过 16 字的短标题
   - text：一到两句完整陈述，保留关键细节（地点、称呼、承诺、未了之事）
   - keywords：2-5 个便于检索的词
3. 与「已有记忆」重复或只是延续的，合并为一条并写清后续发展，不要重复占位；
4. 只输出 JSON，不要任何解释。格式：
{"entries":[{"kind":"event","title":"...","text":"...","keywords":["...","..."]}]}
没有值得记录的内容时输出 {"entries":[]}。"""


# ---------------- 工具函数 ----------------
def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def _tokens(text: str) -> set:
    """粗分词：ASCII 单词 + 中文二元组，用于关键词兜底检索。"""
    s = (text or "").lower()
    out = set()
    for w in re.findall(r"[a-z0-9_]+", s):
        if len(w) > 1:
            out.add(w)
    for seg in re.findall(r"[\u4e00-\u9fff]+", s):
        if len(seg) == 1:
            out.add(seg)
        for i in range(len(seg) - 1):
            out.add(seg[i:i + 2])
    return out


def _strip_json(raw: str) -> Optional[dict]:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 包裹与前后废话）。"""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s[:4].lower() == "json":
            s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


# ---------------- 嵌入客户端 ----------------
class EmbeddingClient:
    """可配置的嵌入服务客户端。兼容 OpenAI 形态 data[].embedding，也兼容 {"embeddings": [[...]]}。"""

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "", timeout: float = 20.0):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.timeout = float(timeout or 20.0)

    def is_configured(self) -> bool:
        return bool(self.base_url and self.model)

    def endpoint(self) -> str:
        if self.base_url.endswith("/embeddings"):
            return self.base_url
        if "/v1" in self.base_url:
            return self.base_url + "/embeddings"
        return self.base_url + "/v1/embeddings"

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not self.is_configured():
            raise RuntimeError("嵌入服务未配置（缺少接口地址或模型名）")
        texts = [str(t) for t in texts]
        if not texts:
            return []
        payload = json.dumps({"model": self.model, "input": texts}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.endpoint(), data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", "Bearer " + self.api_key)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise RuntimeError("嵌入服务返回 HTTP %s: %s" % (e.code, detail))
        except Exception as e:
            raise RuntimeError("嵌入服务请求失败: %s: %s" % (type(e).__name__, e))
        vecs = self._parse(body)
        if len(vecs) != len(texts):
            raise RuntimeError("嵌入返回条数不匹配：期望 %d，实际 %d" % (len(texts), len(vecs)))
        return vecs

    @staticmethod
    def _parse(body) -> List[List[float]]:
        if isinstance(body, dict):
            if isinstance(body.get("data"), list):
                items = [d for d in body["data"] if isinstance(d, dict)]
                items.sort(key=lambda d: d.get("index", 0))
                out = []
                for d in items:
                    emb = d.get("embedding")
                    if isinstance(emb, list) and emb:
                        out.append([float(x) for x in emb])
                if out:
                    return out
            if isinstance(body.get("embeddings"), list):
                return [[float(x) for x in v] for v in body["embeddings"] if isinstance(v, list)]
        raise RuntimeError("无法识别的嵌入响应格式")

    def test(self) -> Dict[str, Any]:
        try:
            vecs = self.embed(["连通性测试"])
            return {"ok": True, "dim": len(vecs[0]) if vecs else 0,
                    "model": self.model, "endpoint": self.endpoint(), "message": "连接正常"}
        except Exception as e:
            return {"ok": False, "dim": 0, "model": self.model,
                    "endpoint": self.endpoint(), "message": str(e)}


# ---------------- 记忆库 ----------------
class MemoryStore:
    """结构化长期记忆 + 向量索引。所有变更落盘并可通过 on_event 广播给前端。"""

    def __init__(self, path: Path = MEMORY_FILE, vector_path: Path = MEMORY_VECTOR_FILE,
                 embedder: Optional[EmbeddingClient] = None,
                 on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None):
        self.path = Path(path)
        self.vector_path = Path(vector_path)
        self.embedder = embedder
        self.on_event = on_event
        self.data: Dict[str, Dict[str, Any]] = {}
        self.vectors: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.load()

    # ---------- 内部工具 ----------
    @staticmethod
    def _key(user_id) -> str:
        return str(user_id)

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def _coerce_uid(k):
        try:
            return int(str(k))
        except (ValueError, TypeError):
            return str(k)

    @staticmethod
    def _entry_text(e: Dict[str, Any]) -> str:
        parts = [str(e.get("title") or ""), str(e.get("text") or "")]
        kws = e.get("keywords") or []
        if kws:
            parts.append("、".join(str(k) for k in kws))
        return "\n".join(p for p in parts if p)

    @classmethod
    def _ensure_entry(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(raw.get("kind") or "fact").strip().lower()
        if kind not in MEMORY_KIND_LABEL:
            kind = "fact"
        kws = raw.get("keywords") or []
        if isinstance(kws, str):
            kws = [k.strip() for k in re.split(r"[,，、;；\s]+", kws) if k.strip()]
        src = raw.get("source_ids") or []
        if isinstance(src, str):
            src = [src]
        entry = {
            "id": str(raw.get("id") or cls._new_id()),
            "kind": kind,
            "title": str(raw.get("title") or "").strip(),
            "text": str(raw.get("text") or "").strip(),
            "keywords": [str(k).strip() for k in kws if str(k).strip()][:8],
            "source_ids": [str(s) for s in src][:40],
            "created_at": raw.get("created_at") or time.time(),
            "updated_at": raw.get("updated_at") or time.time(),
        }
        if raw.get("edited"):
            entry["edited"] = True
        if not entry["title"]:
            entry["title"] = entry["text"][:16] or "未命名记忆"
        return entry

    def _notify(self, action: str, payload: Dict[str, Any]):
        cb = self.on_event
        if not cb:
            return
        try:
            import asyncio
            res = cb(action, payload)
            if asyncio.iscoroutine(res):
                try:
                    asyncio.get_running_loop().create_task(res)
                except RuntimeError:
                    pass
        except Exception as e:
            logger.warning("[MemoryStore] 事件通知失败: %s" % e)

    # ---------- 磁盘读写 ----------
    def load(self):
        self.data = {}
        self.vectors = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if not isinstance(v, dict):
                            continue
                        self.data[str(k)] = {
                            "user_id": v.get("user_id", self._coerce_uid(k)),
                            "sender_name": v.get("sender_name", ""),
                            "cursor": int(v.get("cursor", 0) or 0),
                            "updated_at": v.get("updated_at", 0),
                            "entries": [self._ensure_entry(e) for e in (v.get("entries") or []) if isinstance(e, dict)],
                        }
            except Exception as e:
                logger.warning("[MemoryStore] 读取 %s 失败: %s，将以空记忆启动" % (self.path.name, e))
        if self.vector_path.exists():
            try:
                raw = json.loads(self.vector_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.vectors = raw
            except Exception as e:
                logger.warning("[MemoryStore] 读取 %s 失败: %s" % (self.vector_path.name, e))

    def _save(self):
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning("[MemoryStore] 保存 %s 失败: %s" % (self.path.name, e))

    def _save_vectors(self):
        try:
            tmp = self.vector_path.with_name(self.vector_path.name + ".tmp")
            tmp.write_text(json.dumps(self.vectors, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.vector_path)
        except Exception as e:
            logger.warning("[MemoryStore] 保存 %s 失败: %s" % (self.vector_path.name, e))

    # ---------- 查询 ----------
    def _conv(self, user_id, create: bool = False, sender_name: str = ""):
        key = self._key(user_id)
        conv = self.data.get(key)
        if conv is None and create:
            conv = {"user_id": self._coerce_uid(user_id), "sender_name": sender_name or "",
                    "cursor": 0, "updated_at": time.time(), "entries": []}
            self.data[key] = conv
        if conv is not None and sender_name:
            conv["sender_name"] = sender_name
        return conv

    def _vec_conv(self, user_id, create: bool = False):
        key = self._key(user_id)
        conv = self.vectors.get(key)
        if conv is None and create:
            conv = {"model": "", "items": {}}
            self.vectors[key] = conv
        return conv

    def _vector_items(self, user_id) -> Dict[str, Any]:
        with self._lock:
            conv = self._vec_conv(user_id)
            return dict(conv.get("items") or {}) if conv else {}

    def list_entries(self, user_id) -> List[Dict[str, Any]]:
        with self._lock:
            conv = self._conv(user_id)
            return [dict(e) for e in (conv["entries"] if conv else [])]

    def get_entry(self, user_id, entry_id: str) -> Optional[Dict[str, Any]]:
        for e in self.list_entries(user_id):
            if str(e.get("id")) == str(entry_id):
                return e
        return None

    def stats(self, user_id, total_messages: int = 0) -> Dict[str, Any]:
        with self._lock:
            conv = self._conv(user_id)
            entries = conv["entries"] if conv else []
            cursor = int(conv.get("cursor", 0) or 0) if conv else 0
            items = (self._vec_conv(user_id) or {}).get("items") or {}
        return {
            "user_id": self._coerce_uid(user_id),
            "sender_name": (conv or {}).get("sender_name", ""),
            "count": len(entries),
            "vector_count": sum(1 for e in entries if (items.get(e["id"]) or {}).get("v")),
            "cursor": cursor,
            "pending": max(0, int(total_messages) - cursor),
            "total_messages": int(total_messages),
            "updated_at": (conv or {}).get("updated_at", 0),
        }

    def list_conversations(self, totals: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
        totals = totals or {}
        with self._lock:
            out = []
            for key, conv in self.data.items():
                entries = conv.get("entries") or []
                out.append({
                    "user_id": conv.get("user_id", self._coerce_uid(key)),
                    "sender_name": conv.get("sender_name", ""),
                    "count": len(entries),
                    "cursor": int(conv.get("cursor", 0) or 0),
                    "total_messages": int(totals.get(key, 0) or 0),
                    "updated_at": conv.get("updated_at", 0),
                })
            out.sort(key=lambda c: c.get("updated_at", 0) or 0, reverse=True)
            return out

    # ---------- 变更 ----------
    def add_entry(self, user_id, kind: str = "fact", title: str = "", text: str = "",
                  keywords=None, source_ids=None, sender_name: str = "") -> Dict[str, Any]:
        entry = self._ensure_entry({"kind": kind, "title": title, "text": text,
                                    "keywords": keywords or [], "source_ids": source_ids or []})
        with self._lock:
            conv = self._conv(user_id, create=True, sender_name=sender_name)
            conv["entries"].append(entry)
            conv["updated_at"] = time.time()
            uid = conv.get("user_id", self._coerce_uid(user_id))
        self._save()
        self._notify("memory_add", {"user_id": uid, "entry": entry})
        return entry

    def update_entry(self, user_id, entry_id: str, **fields) -> Optional[Dict[str, Any]]:
        with self._lock:
            conv = self._conv(user_id)
            if not conv:
                return None
            target = None
            for e in conv["entries"]:
                if str(e.get("id")) == str(entry_id):
                    target = e
                    break
            if target is None:
                return None
            for key in ("kind", "title", "text"):
                if key in fields and fields[key] is not None:
                    target[key] = str(fields[key]).strip()
            if "keywords" in fields and fields["keywords"] is not None:
                kws = fields["keywords"]
                if isinstance(kws, str):
                    kws = re.split(r"[,，、;；\s]+", kws)
                target["keywords"] = [str(k).strip() for k in kws if str(k).strip()][:8]
            if target.get("kind") not in MEMORY_KIND_LABEL:
                target["kind"] = "fact"
            if not target.get("title"):
                target["title"] = str(target.get("text", ""))[:16] or "未命名记忆"
            target["edited"] = True
            target["updated_at"] = time.time()
            conv["updated_at"] = time.time()
            uid = conv.get("user_id", self._coerce_uid(user_id))
            snapshot = dict(target)
        self._save()
        self._notify("memory_update", {"user_id": uid, "entry": snapshot})
        return snapshot

    def delete_entry(self, user_id, entry_id: str) -> bool:
        with self._lock:
            conv = self._conv(user_id)
            if not conv:
                return False
            before = len(conv["entries"])
            conv["entries"] = [e for e in conv["entries"] if str(e.get("id")) != str(entry_id)]
            changed = len(conv["entries"]) != before
            if changed:
                conv["updated_at"] = time.time()
                vconv = self._vec_conv(user_id)
                if vconv and vconv.get("items"):
                    vconv["items"].pop(str(entry_id), None)
                    self._save_vectors()
            uid = conv.get("user_id", self._coerce_uid(user_id))
        if changed:
            self._save()
            self._notify("memory_delete", {"user_id": uid, "entry_id": entry_id})
        return changed

    def clear(self, user_id) -> bool:
        with self._lock:
            conv = self._conv(user_id)
            if not conv:
                return False
            had = len(conv.get("entries") or []) > 0
            conv["entries"] = []
            conv["cursor"] = 0
            conv["updated_at"] = time.time()
            self.vectors.pop(self._key(user_id), None)
            uid = conv.get("user_id", self._coerce_uid(user_id))
        self._save()
        self._save_vectors()
        self._notify("memory_clear", {"user_id": uid})
        return had

    # ---------- 向量索引 ----------
    def index_entries(self, user_id, force: bool = False) -> Dict[str, Any]:
        emb = self.embedder
        if emb is None or not emb.is_configured():
            return {"indexed": 0, "total": 0, "ok": False, "message": "嵌入服务未配置，请先在记忆库面板填写接口地址与模型名"}
        entries = self.list_entries(user_id)
        if not entries:
            return {"indexed": 0, "total": 0, "ok": True, "message": "该用户还没有记忆条目"}
        with self._lock:
            items = dict((self._vec_conv(user_id) or {}).get("items") or {})
        todo = []
        for e in entries:
            h = _text_hash(self._entry_text(e))
            cur = items.get(e["id"]) or {}
            if force or cur.get("h") != h or not cur.get("v"):
                todo.append((e, h))
        if not todo:
            return {"indexed": 0, "total": len(entries), "ok": True, "message": "全部 %d 条记忆已有最新向量" % len(entries)}
        try:
            vecs = emb.embed([self._entry_text(e) for e, _ in todo])
        except Exception as e:
            return {"indexed": 0, "total": len(entries), "ok": False, "message": str(e)}
        with self._lock:
            vconv = self._vec_conv(user_id, create=True)
            vconv["model"] = emb.model
            alive = {e["id"] for e in entries}
            vconv["items"] = {k: v for k, v in (vconv.get("items") or {}).items() if k in alive}
            for (e, h), v in zip(todo, vecs):
                vconv["items"][e["id"]] = {"v": v, "h": h}
            self._save_vectors()
        return {"indexed": len(todo), "total": len(entries), "ok": True,
                "dim": len(vecs[0]) if vecs else 0,
                "message": "已为 %d 条记忆建立向量（共 %d 条）" % (len(todo), len(entries))}

    def _score(self, user_id, query: str, entries: List[Dict[str, Any]]) -> Dict[str, float]:
        """向量相似度为主、关键词命中兜底。"""
        scores: Dict[str, float] = {}
        qvec = None
        emb = self.embedder
        if emb is not None and emb.is_configured():
            try:
                qvec = emb.embed([query])[0]
            except Exception as e:
                logger.warning("[MemoryStore] 查询向量失败，回退关键词检索: %s" % e)
        items = self._vector_items(user_id)
        qtok = _tokens(query)
        for e in entries:
            vscore = 0.0
            if qvec is not None:
                v = (items.get(e["id"]) or {}).get("v")
                if v:
                    vscore = _cosine(qvec, v)
            kscore = 0.0
            if qtok:
                hay = self._entry_text(e).lower()
                hit = sum(1 for t in qtok if t in hay)
                kscore = min(1.0, hit / max(3.0, len(qtok) / 3.0))
            scores[e["id"]] = max(vscore, kscore * 0.7)
        return scores

    def recall(self, user_id, query: str, top_k: int = 5, min_score: float = 0.25,
               extra_user_ids: Optional[list] = None) -> List[Dict[str, Any]]:
        """召回记忆。

        extra_user_ids 非空时，把这些会话的记忆也一起参与召回（跨会话召回），
        命中的条目会带 from_user_id / is_other 两个标记，方便调用方区分来源。
        """
        q = (query or "").strip()
        if not q:
            return []
        targets = [user_id]
        for u in (extra_user_ids or []):
            if u is None or str(u) == "":
                continue
            if any(str(u) == str(t) for t in targets):
                continue
            targets.append(u)
        scored: List[Dict[str, Any]] = []
        for u in targets:
            entries = self.list_entries(u)
            if not entries:
                continue
            scores = self._score(u, q, entries)
            for e in entries:
                s = scores.get(e["id"], 0.0)
                if s < float(min_score):
                    continue
                item = dict(e)
                item["score"] = round(s, 4)
                item["from_user_id"] = self._coerce_uid(u)
                item["is_other"] = str(u) != str(user_id)
                scored.append(item)
        # 先按阈值过滤再取前 top_k（旧实现是「先截断再过滤」，
        # 会导致排名靠前但不过阈值的条目把名额占掉，命中数偏少）
        scored.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return scored[:max(1, int(top_k))]

    def other_user_ids(self, user_id, allow: Optional[list] = None) -> List[Any]:
        """同一记忆库里除 user_id 之外的其他会话 UID。

        allow 非空时只返回其中的（白名单模式）；allow 为空表示全部共享。
        """
        allow_set = {str(a).strip() for a in (allow or []) if str(a).strip()}
        out: List[Any] = []
        for key, conv in (self.data or {}).items():
            uid = (conv or {}).get("user_id", self._coerce_uid(key))
            if str(uid) == str(user_id) or str(key) == str(user_id):
                continue
            if allow_set and str(uid) not in allow_set and str(key) not in allow_set:
                continue
            out.append(uid)
        return out

    def search(self, user_id, query: str, top_k: int = 30) -> List[Dict[str, Any]]:
        """面板搜索：有向量按相似度排，否则关键词；空查询返回全部。"""
        q = (query or "").strip()
        entries = self.list_entries(user_id)
        if not q:
            entries.sort(key=lambda e: e.get("updated_at", 0) or 0, reverse=True)
            return entries[:max(1, int(top_k))] if top_k else entries
        scores = self._score(user_id, q, entries)
        ranked = sorted(entries, key=lambda e: scores.get(e["id"], 0.0), reverse=True)
        out = []
        for e in ranked[:max(1, int(top_k))]:
            s = scores.get(e["id"], 0.0)
            if s <= 0:
                continue
            item = dict(e)
            item["score"] = round(s, 4)
            out.append(item)
        return out

    # ---------- 增量总结游标 ----------
    def pending_messages(self, user_id, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        with self._lock:
            conv = self._conv(user_id, create=True)
            cursor = int(conv.get("cursor", 0) or 0)
        if cursor >= len(messages):
            return []
        return list(messages[cursor:])

    def commit_cursor(self, user_id, cursor: int, sender_name: str = ""):
        with self._lock:
            conv = self._conv(user_id, create=True, sender_name=sender_name)
            conv["cursor"] = max(int(conv.get("cursor", 0) or 0), int(cursor))
            conv["updated_at"] = time.time()
        self._save()

    def add_entries_bulk(self, user_id, entries: List[Dict[str, Any]], sender_name: str = "") -> List[Dict[str, Any]]:
        added = []
        with self._lock:
            conv = self._conv(user_id, create=True, sender_name=sender_name)
            for raw in entries:
                if not isinstance(raw, dict):
                    continue
                entry = self._ensure_entry(raw)
                if not entry["text"]:
                    continue
                conv["entries"].append(entry)
                added.append(entry)
            conv["updated_at"] = time.time()
            uid = conv.get("user_id", self._coerce_uid(user_id))
        if added:
            self._save()
            self._notify("memory_add", {"user_id": uid, "count": len(added)})
        return added


# ---------------- 记忆抽取（L0） ----------------
def build_memory_messages(chunk: List[Dict[str, Any]], existing: List[Dict[str, Any]],
                          extra_prompt: str = "") -> List[Dict[str, str]]:
    """把待总结的对话片段 + 已有记忆标题，组装成抽取记忆的 LLM 请求。"""
    lines = []
    for m in chunk:
        role = "用户" if m.get("role") == "user" else "AI"
        content = str(m.get("content", "")).strip()
        if content:
            lines.append("%s：%s" % (role, content))
    known = [("· [%s] %s" % (MEMORY_KIND_LABEL.get(e.get("kind", "fact"), "事实"), e.get("title", ""))) for e in existing[-40:]]
    known_block = "\n".join(known) if known else "（暂无）"
    body = "【已有记忆（避免重复，可合并续写）】\n%s\n\n【待整理的对话片段】\n%s" % (known_block, "\n".join(lines))
    system = (extra_prompt or "").strip() or DEFAULT_MEMORY_PROMPT
    return [{"role": "system", "content": system}, {"role": "user", "content": body}]


def parse_memory_entries(raw_text: str) -> List[Dict[str, Any]]:
    obj = _strip_json(raw_text)
    if not obj:
        return []
    items = obj.get("entries")
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, dict) and str(it.get("text") or "").strip():
            out.append(it)
    return out


# ==================== 记忆引擎接入（自动生成，勿手改）====================
try:
    from memory_engine.bridge import install_into_memory_store as _install_lwb
    _install_lwb(MemoryStore)
except Exception as _lwb_err:      # 引擎缺失时保持原样，不影响启动
    import sys as _sys
    print(f"[lwb] 记忆引擎未接入：{_lwb_err}", file=_sys.stderr)
# ==================== 接入结束 ====================
