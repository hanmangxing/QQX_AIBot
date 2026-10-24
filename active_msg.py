# -*- coding: utf-8 -*-
"""主动消息：AI 可以预排「某时间说某话」，到点自动发出；错过则自动挪时间并记录。"""
import io, json, os, re, threading, time
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILE = os.path.join(_HERE, "active_msg.json")
_lock = threading.RLock()

DEFAULTS = {
    "active_msg_enabled": True,        # 总开关
    "active_msg_prompt": True,         # 是否在提示词里告诉 AI 有这个功能
    "active_msg_max": 20,              # 最多同时挂多少条
    "active_msg_grace": 300,           # 超过预定时间多少秒算「错过」
    "active_msg_keep_done": 8,         # 完成项最多保留几条（灰色划掉）
    "active_msg_shift": True,          # 多次错过是否向 12:00 靠拢
}

_HEAD = """<active_msg_rule>
【主动消息】你可以自己排一条「到点替你说」的消息（也可以完全不排，不用勉强）。
想排的时候，在回复的最后单独写一个块：

<active_msg at="2026-10-18 07:30">到时候要说的话</active_msg>

- at 写具体时间（年月日 时:分，24 小时制）；也可以写 delay="90m" / delay="2h" 表示从现在起多久之后。
- 正文就是到时候要发出去的原话，请按你平时说话的语气写，不要写成通知或系统播报。
- 到点它会自动发到当前这个会话里。已经排好的："""

_TAIL = """
- 每次最多排 1 条；时间要写未来的时间点。这个块本身不会显示给对方，只当排期用。
</active_msg_rule>"""


def _c(cfg: Any) -> Dict[str, Any]:
    d = dict(DEFAULTS)
    if isinstance(cfg, dict):
        for k in DEFAULTS:
            if cfg.get(k) is not None:
                d[k] = cfg[k]
    return d


def _i(c: Dict[str, Any], k: str, d: int) -> int:
    try:
        return int(float(c.get(k, d)))
    except Exception:
        return d


def enabled(cfg: Any) -> bool:
    return bool(_c(cfg).get("active_msg_enabled"))


# ------------------------------------------------------------------ 存储
def _load() -> List[Dict[str, Any]]:
    try:
        with io.open(_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(rows: List[Dict[str, Any]]) -> None:
    try:
        tmp = _FILE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _FILE)
    except Exception:
        pass


def _fmt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_short(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def all_rows() -> List[Dict[str, Any]]:
    with _lock:
        return _load()


def add(cfg: Any, at_ts: float, text: str, kind: str = "private",
        chat_id: Any = "", who: str = "") -> Optional[str]:
    """排一条主动消息。返回 id（失败返回 None）。"""
    if not enabled(cfg):
        return None
    c = _c(cfg)
    text = (text or "").strip()
    if not text or not at_ts:
        return None
    with _lock:
        rows = _load()
        # 新消息创建时，把已完成的旧灰色项清理掉（保留最近几条）
        keep = max(0, _i(c, "active_msg_keep_done", 8))
        done = [r for r in rows if r.get("done")]
        if len(done) > keep:
            drop = set(id(r) for r in done[:len(done) - keep])
            rows = [r for r in rows if id(r) not in drop]
        pend = [r for r in rows if not r.get("done")]
        mx = max(1, _i(c, "active_msg_max", 20))
        if len(pend) >= mx:
            return None
        mid = "am%d%03d" % (int(time.time()), len(rows) % 1000)
        rows.append({
            "id": mid, "at": float(at_ts), "text": text,
            "kind": kind, "chat_id": str(chat_id), "who": str(who),
            "created": time.time(), "done": False, "done_at": 0,
            "miss": 0, "miss_log": [],
        })
        _save(rows)
        return mid


def pending(cfg: Any = None) -> List[Dict[str, Any]]:
    rows = [r for r in all_rows() if not r.get("done")]
    rows.sort(key=lambda r: float(r.get("at") or 0))
    return rows


def due(now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = now or time.time()
    return [r for r in pending() if float(r.get("at") or 0) <= now]


def mark_done(mid: str, ok: bool = True) -> None:
    with _lock:
        rows = _load()
        for r in rows:
            if r.get("id") == mid:
                r["done"] = True
                r["done_at"] = time.time()
                r["sent_ok"] = bool(ok)
                break
        _save(rows)


def remove(mid: str) -> bool:
    with _lock:
        rows = _load()
        left = [r for r in rows if r.get("id") != mid]
        if len(left) == len(rows):
            return False
        _save(left)
        return True


def clear(done_only: bool = True) -> int:
    with _lock:
        rows = _load()
        left = [r for r in rows if not r.get("done")] if done_only else []
        n = len(rows) - len(left)
        _save(left)
        return n


# ------------------------------------------------------------ 错过挪时间
def _next_at(now: float, hour: int, minute: int) -> float:
    """取严格晚于 now 的下一个 hour:minute。"""
    lt = time.localtime(now)
    for d in range(0, 400):
        cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1)) + d * 86400
        if cand > now + 60:
            return cand
    return now + 86400


def reschedule_missed(cfg: Any, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """服务启动/轮询时调用：把「已经过了时间但没发出去」的排期往后挪。

    第一次错过：挪到下一个未来的同一钟点。
    第二次起：每次再向正午 12:00 靠拢 1 小时（7:00→8:00，19:00→18:00）。
    每次错过都记一条 miss_log。
    """
    now = now or time.time()
    c = _c(cfg)
    grace = max(0, _i(c, "active_msg_grace", 300))
    moved = []
    with _lock:
        rows = _load()
        changed = False
        for r in rows:
            if r.get("done"):
                continue
            at = float(r.get("at") or 0)
            if at > now - grace:
                continue
            lt = time.localtime(at)
            hour, minute = lt.tm_hour, lt.tm_min
            miss = int(r.get("miss") or 0) + 1
            if miss >= 2 and c.get("active_msg_shift", True):
                shift = min(miss - 1, 12)
                if hour < 12:
                    hour = min(12, hour + shift)
                else:
                    hour = max(12, hour - shift)
            new_at = _next_at(now, hour, minute)
            r["miss"] = miss
            r["miss_log"] = (r.get("miss_log") or [])[-9:] + [{
                "at": _fmt(at), "found": _fmt(now),
                "miss": miss, "new": _fmt(new_at),
            }]
            r["at"] = new_at
            moved.append({"id": r.get("id"), "old": _fmt(at), "new": _fmt(new_at), "miss": miss})
            changed = True
        if changed:
            _save(rows)
    return moved


# ------------------------------------------------------------ 提示词
def prompt_block(cfg: Any) -> str:
    if not enabled(cfg) or not _c(cfg).get("active_msg_prompt", True):
        return ""
    rows = pending(cfg)
    if rows:
        lines = []
        for r in rows[:12]:
            t = (r.get("text") or "").replace("\n", " ")
            lines.append("- %s（%s）" % (_fmt_short(float(r.get("at") or 0)), t[:60]))
        body = "\n".join(lines)
    else:
        body = "（目前没有排好的）"
    return _HEAD + "\n" + body + _TAIL


# ------------------------------------------------------------ 解析 AI 回复
_RE_BLOCK = re.compile(r"<active_msg\b([^>]*)>(.*?)</active_msg>", re.S | re.I)
_RE_AT = re.compile(r'at\s*=\s*["\']([^"\']+)["\']', re.I)
_RE_DELAY = re.compile(r'delay\s*=\s*["\']([^"\']+)["\']', re.I)


def _parse_at(s: str) -> Optional[float]:
    s = s.strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M", "%m-%d %H:%M"):
        try:
            t = time.strptime(s, f)
            if t.tm_year < 2000:
                t = time.strptime("%d-%s" % (time.localtime().tm_year, s), "%Y-" + f)
            return time.mktime(t)
        except Exception:
            continue
    return None


def _parse_delay(s: str) -> Optional[float]:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|min|h|d)?\s*$", s.strip(), re.I)
    if not m:
        return None
    n = float(m.group(1))
    u = (m.group(2) or "m").lower()
    k = {"s": 1, "m": 60, "min": 60, "h": 3600, "d": 86400}[u]
    return time.time() + n * k


def extract(text: str) -> (List[Dict[str, Any]], str):
    """从 AI 回复里抠出主动消息块，返回 (排期列表, 去掉块之后的正文)。"""
    found = []
    for m in _RE_BLOCK.finditer(text or ""):
        attrs, body = m.group(1) or "", (m.group(2) or "").strip()
        if not body:
            continue
        ma, md = _RE_AT.search(attrs), _RE_DELAY.search(attrs)
        at = _parse_at(ma.group(1)) if ma else (_parse_delay(md.group(1)) if md else None)
        if at:
            found.append({"at": at, "text": body})
    clean = _RE_BLOCK.sub("", text or "").strip()
    return found, clean
