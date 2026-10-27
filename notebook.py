# -*- coding: utf-8 -*-
"""
笔记本（Notebook）—— AI 可读写的独立知识库。

设计参照酒馆插件 astrbot_plugin_angel_memory 的 note 体系：
  · 独立存储，跟记忆锚点库完全分开（不污染自动抽取的时间线与召回排序）
  · 两类条目：note（成体系的 Markdown 笔记）/ memo（单条要点速记）
  · 每条有整型 short_id，AI 用短 ID 引用，人看着也清爽
  · 正文支持按标题分节，AI 可 offset/limit 分页展开

存储：notebook.json（原子写盘 + 线程锁），与 config.json 同目录。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

# 单次展开正文的字符上限（与插件 angel_note_read 的 MAX_CHARS 对齐）
MAX_READ_CHARS = 30000

# 条目类型
KIND_NOTE = "note"   # 成体系的 Markdown 笔记
KIND_MEMO = "memo"   # 单条要点速记

# memo 的记忆类型（沿用插件的四分类）
MEMO_TYPES = ("knowledge", "skill", "emotional", "event")
MEMO_TYPE_LABEL = {
    "knowledge": "认知",
    "skill": "方法",
    "emotional": "情感",
    "event": "事件",
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _norm(s: Any) -> str:
    return str(s or "").strip()


def _terms(query: str) -> List[str]:
    """把检索词拆成若干独立词。支持空格 / | / 、/ ，/ 逗号 分隔。"""
    q = _norm(query).lower()
    if not q:
        return []
    parts = re.split(r"[\s|、,，;；]+", q)
    return [p for p in (p.strip() for p in parts) if p]


def _bigrams(s: str) -> set:
    """中文友好的二元字组，用于在精确子串匹配不到时兜底召回。"""
    t = re.sub(r"\s+", "", _norm(s).lower())
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


class NotebookStore:
    """笔记本存储。线程安全，原子写盘。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"seq": 0, "items": []}
        self._load()

    # ------------------------------------------------------------ 持久化

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("items"), list):
                self._data = {"seq": int(d.get("seq") or 0), "items": d["items"]}
        except FileNotFoundError:
            pass
        except Exception:
            # 文件损坏时不覆盖，另存一份备份后从空开始
            try:
                os.replace(self.path, self.path + ".corrupt_%d" % int(time.time()))
            except Exception:
                pass
        self._ensure_ids()

    def _ensure_ids(self) -> None:
        """补齐缺失的 short_id。

        注意：**已分配过的编号永不改动**。AI 会把「ID: 3」写进自己的上下文，
        删一条就整体重排会让旧编号指向另一篇笔记，比留空号危险得多。
        """
        seq = int(self._data.get("seq") or 0)
        used = [int(it["short_id"]) for it in self._data["items"]
                if isinstance(it.get("short_id"), int)]
        if used:
            seq = max(seq, max(used))
        for it in self._data["items"]:
            if not isinstance(it.get("short_id"), int) or it["short_id"] <= 0:
                seq += 1
                it["short_id"] = seq
        self._data["seq"] = seq

    def _save(self) -> None:
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".nb_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise

    # ------------------------------------------------------------ 增删改查

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            items = self._data["items"]
            return {
                "total": len(items),
                "notes": sum(1 for i in items if i.get("kind") == KIND_NOTE),
                "memos": sum(1 for i in items if i.get("kind") == KIND_MEMO),
                "chars": sum(len(str(i.get("content") or "")) for i in items),
            }

    def list_items(self, kind: str = "", scope: str = "") -> List[Dict[str, Any]]:
        """列出条目（不含正文，省流量）。scope 为空表示全部。"""
        with self._lock:
            out = []
            for it in self._data["items"]:
                if kind and it.get("kind") != kind:
                    continue
                if scope and str(it.get("scope") or "") not in (scope, "global"):
                    continue
                row = dict(it)
                body = str(row.pop("content", "") or "")
                row["chars"] = len(body)
                row["preview"] = body[:120].replace("\n", " ")
                out.append(row)
            return out

    def get(self, nid: Any) -> Optional[Dict[str, Any]]:
        """按 short_id（整型）或 id（nb_xxxx）取一条。"""
        key = _norm(nid)
        with self._lock:
            for it in self._data["items"]:
                if str(it.get("short_id")) == key or str(it.get("id")) == key:
                    return dict(it)
        return None

    def create(self, kind: str, title: str, content: str, tags: Optional[List[str]] = None,
               memory_type: str = "knowledge", reasoning: str = "",
               scope: str = "global", source: str = "ai") -> Dict[str, Any]:
        """新增一条。note 与 memo 共用底层，只是字段侧重不同。"""
        kind = KIND_NOTE if str(kind) == KIND_NOTE else KIND_MEMO
        title = _norm(title) or ("未命名笔记" if kind == KIND_NOTE else "未命名速记")
        content = _norm(content)
        if not content:
            raise ValueError("内容不能为空")
        tags = [t for t in (_norm(t) for t in (tags or [])) if t]
        with self._lock:
            self._data["seq"] = int(self._data.get("seq") or 0) + 1
            nid = "nb_%06d" % self._data["seq"]
            item = {
                "id": nid,
                "short_id": self._data["seq"],
                "kind": kind,
                "title": title[:200],
                "content": content,
                "tags": tags,
                "memory_type": memory_type if memory_type in MEMO_TYPES else "knowledge",
                "reasoning": _norm(reasoning),
                "scope": _norm(scope) or "global",
                "source": _norm(source) or "ai",
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._data["items"].append(item)
            self._save()
            return dict(item)

    def update(self, nid: Any, **fields) -> Optional[Dict[str, Any]]:
        """改一条。只认白名单字段，content 变了就刷新 updated_at。"""
        allow = {"title", "content", "tags", "memory_type", "reasoning", "scope", "kind"}
        with self._lock:
            for it in self._data["items"]:
                if str(it.get("short_id")) != _norm(nid) and str(it.get("id")) != _norm(nid):
                    continue
                for k, v in fields.items():
                    if k not in allow or v is None:
                        continue
                    if k == "tags":
                        it[k] = [t for t in (_norm(t) for t in (v if isinstance(v, list) else [])) if t]
                    elif k == "kind":
                        it[k] = KIND_NOTE if str(v) == KIND_NOTE else KIND_MEMO
                    elif k == "memory_type":
                        it[k] = v if v in MEMO_TYPES else "knowledge"
                    else:
                        it[k] = _norm(v) if k != "title" else _norm(v)[:200]
                it["updated_at"] = _now()
                self._save()
                return dict(it)
        return None

    def delete(self, nid: Any) -> bool:
        with self._lock:
            before = len(self._data["items"])
            self._data["items"] = [
                it for it in self._data["items"]
                if str(it.get("short_id")) != _norm(nid) and str(it.get("id")) != _norm(nid)
            ]
            if len(self._data["items"]) == before:
                return False
            self._save()
            return True

    # ------------------------------------------------------------ 检索

    def _score(self, query: str, item: Dict[str, Any]) -> float:
        """关键词打分：标题 > 标签 > 正文，精确命中为主、二元字组兜底。"""
        terms = _terms(query)
        if not terms:
            return 0.0
        title = _norm(item.get("title")).lower()
        tags = " ".join(_norm(t).lower() for t in (item.get("tags") or []))
        body = _norm(item.get("content")).lower()
        score = 0.0
        for t in terms:
            if not t:
                continue
            if t in title:
                score += 3.0
            if t in tags:
                score += 2.5
            n = body.count(t)
            if n:
                score += 1.0 + 0.3 * min(n, 5)
        if score <= 0.0:
            # 精确子串没中，用二元字组兜底（中文同义/换词场景）
            qg = _bigrams(query)
            for field, w in ((title, 1.2), (tags, 1.0), (body, 0.6)):
                if not field:
                    continue
                fg = _bigrams(field)
                if not fg or not qg:
                    continue
                ov = len(qg & fg) / float(len(qg))
                if ov > 0:
                    score += w * ov
        return score

    def search(self, query: str, limit: int = 50, scope: str = "",
               min_score: float = 0.0) -> List[Dict[str, Any]]:
        """检索条目，返回带 score 的列表（含正文，供 AI 阅读）。"""
        with self._lock:
            rows = []
            for it in self._data["items"]:
                if scope and str(it.get("scope") or "") not in (scope, "global"):
                    continue
                sc = self._score(query, it)
                if sc > 0:
                    row = dict(it)
                    row["score"] = round(sc, 4)
                    rows.append(row)
        rows.sort(key=lambda r: (-r["score"], r.get("short_id", 0)))
        try:
            _ms = float(min_score or 0)
        except Exception:
            _ms = 0.0
        if _ms > 0:  # 阈值过滤：0 = 不过滤，越高越严格
            rows = [r for r in rows if float(r.get("score") or 0) >= _ms]
        return rows[:max(1, int(limit))]

    # ------------------------------------------------------------ 正文分节 / 分页

    @staticmethod
    def sections(content: str) -> List[Dict[str, Any]]:
        """按 Markdown 标题把正文切成节，返回 [{level,title,line_start,line_end}]。"""
        lines = str(content or "").splitlines()
        heads = []
        for i, ln in enumerate(lines, start=1):
            m = re.match(r"^(#{1,6})\s+(.*)$", ln)
            if m:
                heads.append({"level": len(m.group(1)), "title": m.group(2).strip(), "line": i})
        out = []
        for idx, h in enumerate(heads):
            end = heads[idx + 1]["line"] - 1 if idx + 1 < len(heads) else len(lines)
            out.append({"level": h["level"], "title": h["title"],
                        "line_start": h["line"], "line_end": max(h["line"], end)})
        return out

    def read(self, nid: Any, offset: int = 1, limit: Optional[int] = None,
             max_chars: int = MAX_READ_CHARS) -> str:
        """展开正文，offset/limit 分页，附带分节目录与续读提示。"""
        it = self.get(nid)
        if it is None:
            return "错误：未找到 short_id=%s 对应的笔记。" % _norm(nid)
        lines = str(it.get("content") or "").splitlines()
        total = len(lines)
        start = max(1, int(offset or 1))
        if start > total:
            return ("[笔记本]\nshort_id: %s\ntitle: %s\ntotal_lines: %d\noffset: %d\n内容已到末尾，无更多行。"
                    % (it.get("short_id"), it.get("title"), total, start))

        line_limit = int(limit) if limit else total
        picked, chars, idx, cnt = [], 0, start - 1, 0
        while idx < total and cnt < line_limit:
            ln = lines[idx]
            if chars + len(ln) + 1 > max_chars and picked:
                break
            picked.append(ln)
            chars += len(ln) + 1
            idx += 1
            cnt += 1
        end = start + len(picked) - 1

        head = [
            "[笔记本]",
            "short_id: %s" % it.get("short_id"),
            "kind: %s" % it.get("kind"),
            "title: %s" % it.get("title"),
            "tags: %s" % (", ".join(it.get("tags") or []) or "-"),
            "total_lines: %d" % total,
            "returned: L%d-%d" % (start, end),
            "has_more: %s" % ("true (继续读取请用 offset=%d)" % (end + 1) if end < total else "false"),
        ]
        secs = self.sections(it.get("content") or "")
        if secs:
            head.append("sections: " + " | ".join("L%d %s" % (s["line_start"], s["title"]) for s in secs))
        return "\n".join(head) + "\n\n" + "\n".join(picked)

    # ------------------------------------------------------------ 注入

    def inject_block(self, query: str, top_k: int = 3, preview: int = 300,
                     scope: str = "", min_score: float = 0.0) -> str:
        """检索相关笔记，拼成注入主对话的提示块（每条只给预览，全文让 AI 自己展开）。"""
        hits = self.search(query, limit=max(1, int(top_k)), scope=scope, min_score=min_score)
        if not hits:
            return ""
        lines = []
        for h in hits:
            body = re.sub(r"\s+", " ", str(h.get("content") or "")).strip()
            if len(body) > preview:
                body = body[:preview] + "…"
            tag = ("[%s] " % "/".join(h.get("tags") or [])) if h.get("tags") else ""
            lines.append("- 【%s #%s】%s %s%s"
                         % ("笔记" if h.get("kind") == KIND_NOTE else "速记",
                            h.get("short_id"), h.get("title"), tag, body))
        return ("【笔记本｜与当前话题相关的既有笔记】\n" + "\n".join(lines) +
                "\n（以上是你自己记下的笔记。需要看某篇全文，调用 angel_note_read 传 note_short_id 展开。）")
