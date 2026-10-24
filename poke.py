# -*- coding: utf-8 -*-
"""戳一戳（poke）：被戳时记下来，等下一次跟主对话 LLM 说话时捎给它。

- 私聊 / 群聊分开开关（poke_private / poke_group），总开关 poke_enabled
- 同一个人连续戳会合并计数，避免刷屏
- 取用即清空（take），保证只播报一次
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional

DEFAULTS = {
    "poke_enabled": True,
    "poke_private": True,
    "poke_group": True,
    "poke_max": 8,          # 每次最多播报几条（超出只留最近的）
    "poke_window": 900,     # 记录保留时长（秒），过期的丢掉
    "poke_note": True,      # 是否附一句「可以自然回应」的提示
    # 反戳：被戳之后有概率自己戳回去，并把「我反戳了谁」也告诉主对话
    "poke_back_enabled": True,
    "poke_back_rate": 0.5,      # 反戳概率 0~1
    "poke_back_delay": 1.5,     # 反戳延迟（秒）
    # 跟戳：群里别人被别人戳了，跟着一起去戳那个被戳的人（纯本地行为，不经过 API）
    "poke_follow_enabled": True,
    "poke_follow_rate": 0.3,    # 跟戳概率 0~1
    "poke_follow_delay": 2.0,   # 跟戳延迟（秒）
    "poke_follow_cooldown": 60, # 同一个目标多少秒内只跟一次
    # 模型主动戳人：AI 可以在回复里自己戳一下某人
    "poke_active_enabled": True,
    "poke_active_max": 1,        # 每次回复最多主动戳几个
    "poke_active_cooldown": 120, # 同一个人多少秒内只主动戳一次

}

_lock = threading.Lock()
_store: Dict[str, List[Dict[str, Any]]] = {}   # "private:123" / "group:456" -> [记录]


def _c(cfg: Any) -> Dict[str, Any]:
    c = dict(DEFAULTS)
    try:
        c.update(cfg or {})
    except Exception:
        pass
    return c


def _i(c: Dict[str, Any], k: str, d: int) -> int:
    try:
        return int(float(c.get(k, d) or 0))
    except Exception:
        return int(d)


def enabled(cfg: Any, kind: str) -> bool:
    """kind = "private" / "group"；总开关 + 对应分开关都开着才算启用。"""
    c = _c(cfg)
    if not c.get("poke_enabled"):
        return False
    return bool(c.get("poke_private" if kind == "private" else "poke_group"))


def add(cfg: Any, kind: str, chat_id: Any, who: str,
        group_id: Any = None, times: int = 1) -> bool:
    """记一条戳一戳。返回是否记下了（开关关着就不记）。"""
    if not enabled(cfg, kind):
        return False
    key = "%s:%s" % (kind, chat_id)
    now = time.time()
    with _lock:
        lst = _store.setdefault(key, [])
        # 同一个人连续戳 → 合并计数
        if lst and lst[-1].get("who") == who and now - float(lst[-1].get("ts") or 0) < 120:
            lst[-1]["times"] = int(lst[-1].get("times") or 1) + 1
            lst[-1]["ts"] = now
            lst[-1]["at"] = time.strftime("%H:%M")
        else:
            lst.append({"who": who, "times": max(1, int(times or 1)), "ts": now,
                        "at": time.strftime("%H:%M"), "kind": kind,
                        "group_id": str(group_id) if group_id else ""})
        # 过期清理
        win = _i(_c(cfg), "poke_window", 900)
        lst[:] = [r for r in lst if now - float(r.get("ts") or 0) <= win]
        mx = _i(_c(cfg), "poke_max", 8)
        if len(lst) > mx:
            del lst[:len(lst) - mx]
    return True


_follow_at: Dict[str, float] = {}      # 跟戳冷却：target -> 上次跟的时间


def _rate(c: Dict[str, Any], k: str, d: float) -> float:
    try:
        v = float(c.get(k, d) if c.get(k, d) is not None else d)
    except Exception:
        v = d
    return max(0.0, min(1.0, v))


def decide_back(cfg: Any) -> bool:
    """要不要反戳：总开关 + 反戳开关 + 概率。"""
    c = _c(cfg)
    if not (c.get("poke_enabled") and c.get("poke_back_enabled")):
        return False
    import random
    return random.random() < _rate(c, "poke_back_rate", 0.5)


def decide_follow(cfg: Any, target: Any) -> bool:
    """要不要跟戳某个目标：开关 + 概率 + 同目标冷却。"""
    c = _c(cfg)
    if not (c.get("poke_enabled") and c.get("poke_follow_enabled")):
        return False
    key = str(target)
    now = time.time()
    with _lock:
        last = float(_follow_at.get(key) or 0)
        if now - last < _i(c, "poke_follow_cooldown", 60):
            return False
    import random
    if random.random() >= _rate(c, "poke_follow_rate", 0.3):
        return False
    with _lock:
        _follow_at[key] = now
    return True


def record_out(cfg: Any, kind: str, chat_id: Any, who: str, action: str = "back") -> None:
    """记一条「它自己干的」：反戳回去。下次说话时一并告诉主对话。"""
    if not enabled(cfg, kind):
        return
    key = "%s:%s" % (kind, chat_id)
    now = time.time()
    with _lock:
        _store.setdefault(key, []).append({
            "who": str(who), "out": True, "action": action, "times": 1,
            "ts": now, "at": time.strftime("%H:%M"), "kind": kind,
        })


_ACTIVE_HEAD = """<poke_rule>
【主动戳人】你可以在回复里主动戳一下某人（也可以不戳，不用勉强）。
写法：<poke to="对方的QQ号" />

- to 填对方的 QQ 号；这个标记不会显示给对方，只会真的戳他一下。
- 每次回复最多戳 1 个，别连着戳同一个人。什么时候戳你自己拿主意，比如想催对方、想撒娇、想打个招呼。
</poke_rule>"""


def active_prompt_block(cfg: Any) -> str:
    """告诉模型它可以主动戳人。"""
    c = _c(cfg)
    if not (c.get("poke_enabled") and c.get("poke_active_enabled")):
        return ""
    return _ACTIVE_HEAD


_RE_POKE = re.compile(r"<poke\b[^>]*?to\s*=\s*[\"']?(\d{4,})[\"']?[^>]*>", re.I)
_RE_ANY = re.compile(r"</?poke\b[^>]*>", re.I)
_RE_TO = re.compile(r'to\s*=\s*["\']?(\d{4,})["\']?', re.I)
_active_at: Dict[str, float] = {}


def active_ok(cfg: Any, target: Any) -> bool:
    """主动戳的冷却：同一个人多少秒内只戳一次。"""
    c = _c(cfg)
    if not (c.get("poke_enabled") and c.get("poke_active_enabled")):
        return False
    key = str(target)
    now = time.time()
    with _lock:
        if now - float(_active_at.get(key) or 0) < _i(c, "poke_active_cooldown", 120):
            return False
        _active_at[key] = now
    return True


def extract_active(cfg: Any, text: str) -> (List[str], str):
    """从回复里抠出主动戳人的标记，返回 (可戳的 QQ 号列表, 去掉标记的正文)。"""
    if not _c(cfg).get("poke_active_enabled"):
        return [], text or ""
    mx = max(0, _i(_c(cfg), "poke_active_max", 1))
    out: List[str] = []
    for m in _RE_POKE.finditer(text or ""):
        tid = m.group(1)
        if tid not in out and len(out) < mx:
            out.append(tid)
    clean = _RE_ANY.sub("", text or "").strip()
    return out, clean


def peek(cfg: Any, kind: str, chat_id: Any) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(r) for r in _store.get("%s:%s" % (kind, chat_id), [])]


def take(cfg: Any, kind: str, chat_id: Any) -> str:
    """取走并清空该会话的待播报记录，返回可直接塞进 messages 的文本（没有就返回空串）。"""
    key = "%s:%s" % (kind, chat_id)
    with _lock:
        lst = _store.pop(key, [])
    if not lst:
        return ""
    return render(lst, _c(cfg))


def render(records: List[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None) -> str:
    c = _c(cfg or {})
    lines = []
    for r in records or []:
        who = str(r.get("who") or "某人")
        at = str(r.get("at") or "")
        n = int(r.get("times") or 1)
        stamp = ("%s " % at) if at else ""
        if r.get("out"):
            lines.append("- %s你反戳了「%s」一下" % (stamp, who))
        else:
            tail = "（连戳 %d 下）" % n if n > 1 else ""
            lines.append("- %s「%s」戳了你一下%s" % (stamp, who, tail))
    if not lines:
        return ""
    body = "\n".join(lines)
    note = ("\n（这是真实发生在 QQ 上的互动，可以自然地回应——比如装疼、撒个娇、问对方干嘛，"
            "不用特意说\u201c系统提示\u201d之类的话。）") if c.get("poke_note", True) else ""
    return "<poke_note>\n【戳一戳】刚刚有人戳了你：\n%s%s\n</poke_note>" % (body, note)


def stats() -> Dict[str, Any]:
    with _lock:
        return {"chats": len(_store),
                "pending": sum(len(v) for v in _store.values()),
                "detail": {k: len(v) for k, v in _store.items()}}


def clear(chat_id: Any = None) -> int:
    with _lock:
        if chat_id is None:
            n = sum(len(v) for v in _store.values())
            _store.clear()
            return n
        n = 0
        for k in list(_store.keys()):
            if k.endswith(":%s" % chat_id):
                n += len(_store.pop(k, []))
        return n
