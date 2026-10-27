# -*- coding: utf-8 -*-
"""闲置触发 / 旧事重提：闲置越久概率越高，抽中后主动发一条；旧事重提还会在日常聊天里突发。"""
import io, json, os, random, threading, time
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_FILE = os.path.join(_HERE, "idle.json")
_lock = threading.RLock()

DEFAULTS = {
    "idle_enabled": True,
    "idle_after_hours": 6,          # 超过这么久没说话才算闲置
    "idle_base_rate": 0.2,          # 刚开始的触发概率
    "idle_step_rate": 0.03,         # 每过 step 分钟叠加多少概率（默认每小时 +3%）
    "idle_step_minutes": 60,
    "idle_check_minutes": 20,       # 多久抽一次
    "idle_night_start": 0,          # 夜间时段起（含）
    "idle_night_end": 8,            # 夜间时段止（不含）
    "idle_wake_rate": 0.2,          # 夜间「苏醒」概率
    "idle_weight_idle": 50,         # 闲置触发（偶发想念）权重
    "idle_weight_recall": 50,       # 旧事重提权重
    "idle_max_per_day": 3,          # 每天最多主动几次
    "idle_chat_recall_enabled": True,
    "idle_chat_recall_rate": 0.08,  # 日常聊天里突发旧事重提的概率
    "idle_solo_enabled": True,
    "idle_solo_ids": [],            # 单独思念：填进来的 ID 各算一份，互不干扰
    "idle_solo_cooldown_hours": 6,  # 单独思念：每个 ID 各自的冷却（小时）
    "idle_solo_max_per_day": 1,     # 单独思念：每个 ID 各自每天最多几次
    "idle_prompt_idle": (
        "【现在是你在主动找 TA 说话】\n"
        "距离你们上次聊天已经过去了一段时间，你突然有点想 TA 了。\n"
        "直接说一句主动的话就行：可以是随口问候、可以是分享此刻的心情或身边的小事、"
        "也可以像随手拍了张照片似的描述一下你眼前看到的东西。\n"
        "要求：像真人临时起意发的消息，短、自然、有你的语气，别写成通知或客服播报；"
        "别解释「我为什么发这条」，也别问「你怎么这么久没理我」。"
    ),
    "idle_prompt_recall": (
        "【现在是你在主动找 TA 说话，而且是突然想起了以前的某件事】\n"
        "下面是从你的记忆里翻出来的一些旧事：\n"
        "{memory}\n"
        "挑其中一件几天前的琐事或 TA 提过的兴趣点，像随口提起一样自然地说出来，"
        "然后顺下去聊两句。\n"
        "要求：不要刻意提问、不要像采访或查户口；就是「哎对了，之前你说过……」这种随口一提的感觉。"
        "短、自然，别解释这段提示的存在。"
    ),
    "idle_prompt_chat_recall": (
        "【突发记忆】你突然想起了以前的某件小事：\n"
        "{memory}\n"
        "在这轮回复里像随口提及一样自然带一句（可以只是半句），不要刻意提问、不要生硬转折。"
        "如果这轮实在接不上，就不用提，不要硬塞。"
    ),
}

RECALL_QUERY = "几天前聊到过的琐事 兴趣 喜好 近况 说过的事"

_KICK = {
    "idle": "（现在由你主动开口，直接说话。）",
    "recall": "（现在由你主动开口，把想起的那件事随口说出来。）",
}


def _c(cfg: Any) -> Dict[str, Any]:
    d = dict(DEFAULTS)
    if isinstance(cfg, dict):
        for k in DEFAULTS:
            if cfg.get(k) is not None:
                d[k] = cfg[k]
    return d


def _f(c: Dict[str, Any], k: str, d: float) -> float:
    try:
        return float(c.get(k, d))
    except Exception:
        return d


def _i(c: Dict[str, Any], k: str, d: int) -> int:
    try:
        return int(float(c.get(k, d)))
    except Exception:
        return d


def enabled(cfg: Any) -> bool:
    return bool(_c(cfg).get("idle_enabled"))


def _solo_key(kind: str, chat_id: Any) -> str:
    return "%s:%s" % (str(kind or "private"), str(chat_id or ""))


def solo_list(cfg: Any) -> List[Dict[str, Any]]:
    """解析 idle_solo_ids。每项可写 123456 / 私聊:123456 / 群:123456（也认 p:/g:）。"""
    c = _c(cfg)
    raw = c.get("idle_solo_ids") or []
    if isinstance(raw, str):
        raw = raw.replace(",", "\n").replace("，", "\n").split("\n")
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in (raw if isinstance(raw, (list, tuple)) else []):
        t = str(item or "").strip()
        if not t:
            continue
        kind, cid = "private", t
        low = t.lower()
        for pfx, k in (("私聊:", "private"), ("群聊:", "group"), ("群:", "group"),
                       ("private:", "private"), ("group:", "group"), ("p:", "private"), ("g:", "group")):
            if low.startswith(pfx):
                kind, cid = k, t[len(pfx):].strip()
                break
        cid = cid.strip()
        if not cid:
            continue
        key = _solo_key(kind, cid)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "chat_id": cid, "key": key})
    return out


# ------------------------------------------------------------------ 状态
def _load() -> Dict[str, Any]:
    try:
        with io.open(_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(st: Dict[str, Any]) -> None:
    try:
        tmp = _FILE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _FILE)
    except Exception:
        pass


def state() -> Dict[str, Any]:
    with _lock:
        return _load()


def touch(cfg: Any, kind: str, chat_id: Any, who: str = "") -> None:
    """记一下「刚说过话」，顺便记住该往哪儿主动发；单独思念的 ID 另外记一份。"""
    if not enabled(cfg):
        return
    _now = time.time()
    with _lock:
        st = _load()
        st["last_chat_ts"] = _now
        st["kind"] = str(kind)
        st["chat_id"] = str(chat_id)
        st["who"] = str(who or "")
        try:
            for _it in solo_list(cfg):
                if _it["kind"] == str(kind) and _it["chat_id"] == str(chat_id):
                    _solo = st.get("solo") or {}
                    _e = _solo.get(_it["key"]) or {}
                    _e["last_chat_ts"] = _now
                    _e["kind"] = _it["kind"]
                    _e["chat_id"] = _it["chat_id"]
                    _e["who"] = str(who or "")
                    _solo[_it["key"]] = _e
                    st["solo"] = _solo
                    break
        except Exception:
            pass
        _save(st)


def mark_fired(mode: str, key: Optional[str] = None) -> None:
    """记一次「已经发过了」。key 非空 = 单独思念，各记各的。"""
    _now = time.time()
    _day = time.strftime("%Y-%m-%d")
    if key:
        with _lock:
            st = _load()
            solo = st.get("solo") or {}
            e = solo.get(str(key)) or {}
            e["last_fire_ts"] = _now
            e["last_mode"] = str(mode)
            if e.get("day") != _day:
                e["day"] = _day
                e["day_count"] = 0
            e["day_count"] = int(e.get("day_count") or 0) + 1
            e["log"] = (e.get("log") or [])[-19:] + [{"at": time.strftime("%m-%d %H:%M"), "mode": str(mode)}]
            solo[str(key)] = e
            st["solo"] = solo
            _save(st)
        return
    with _lock:
        st = _load()
        st["last_fire_ts"] = _now
        st["last_mode"] = str(mode)
        day = time.strftime("%Y-%m-%d")
        if st.get("day") != day:
            st["day"] = day
            st["day_count"] = 0
        st["day_count"] = int(st.get("day_count") or 0) + 1
        logs = st.get("log") or []
        logs.append({"at": time.strftime("%m-%d %H:%M"), "mode": str(mode)})
        st["log"] = logs[-20:]
        _save(st)


def note(msg: str) -> None:
    """记一条抽选过程（面板上能看到）。"""
    with _lock:
        st = _load()
        logs = st.get("roll_log") or []
        logs.append({"at": time.strftime("%m-%d %H:%M:%S"), "msg": str(msg)[:160]})
        st["roll_log"] = logs[-30:]
        _save(st)


def _bump_day(st: Dict[str, Any]) -> int:
    day = time.strftime("%Y-%m-%d")
    if st.get("day") != day:
        st["day"] = day
        st["day_count"] = 0
    return int(st.get("day_count") or 0)


# ------------------------------------------------------------------ 判定
def rate_now(cfg: Any, idle_seconds: float) -> float:
    """闲置越久概率越高：基础概率 + 每 step 分钟 +step_rate，封顶 1。"""
    if idle_seconds < _f(cfg, "idle_after_hours", 6) * 3600:
        return 0.0  # _IDLE_BELOW
    c = _c(cfg)
    base = _f(c, "idle_base_rate", 0.2)
    step = _f(c, "idle_step_rate", 0.01)
    smin = max(1, _i(c, "idle_step_minutes", 20))
    over = max(0.0, float(idle_seconds) - _f(c, "idle_after_hours", 6) * 3600)
    p = base + step * int(over // (smin * 60))
    return max(0.0, min(1.0, p))


def is_night(cfg: Any, now: Optional[float] = None) -> bool:
    c = _c(cfg)
    h = time.localtime(now or time.time()).tm_hour
    a, b = _i(c, "idle_night_start", 0), _i(c, "idle_night_end", 8)
    if a == b:
        return False
    return (a <= h < b) if a < b else (h >= a or h < b)


def pick_mode(cfg: Any, rnd=None) -> str:
    c = _c(cfg)
    wi = max(0, _i(c, "idle_weight_idle", 50))
    wr = max(0, _i(c, "idle_weight_recall", 50))
    if wi + wr <= 0:
        return "idle"
    r = (rnd or random.random)() * (wi + wr)
    return "recall" if r >= wi else "idle"


def should_fire(cfg: Any, now: Optional[float] = None, rnd=None) -> Optional[Dict[str, Any]]:
    """到点抽一次。返回 None = 还没到闲置门槛；返回 dict 带 fire 标志。"""
    c = _c(cfg)
    if not c.get("idle_enabled"):
        return None
    now = now or time.time()
    rnd = rnd or random.random
    with _lock:
        st = _load()
    last = float(st.get("last_chat_ts") or 0)
    if not last or not st.get("kind") or not st.get("chat_id"):
        return None
    idle_s = now - last
    if idle_s < _f(c, "idle_after_hours", 6) * 3600:
        return None
    # 名单里的 ID 走单独通道，不再占用全局这一条（避免同一个人被发两次）
    try:
        for _it in solo_list(c):
            if _it["kind"] == str(st.get("kind") or "") and _it["chat_id"] == str(st.get("chat_id") or ""):
                return None
    except Exception:
        pass
    with _lock:
        st = _load()
        used = _bump_day(st)
        _save(st)
    if used >= max(0, _i(c, "idle_max_per_day", 3)):
        return None
    p = rate_now(c, idle_s)
    hit = rnd() < p
    mode = pick_mode(c, rnd) if hit else ""
    night = is_night(c, now)
    woke = True
    if hit and night:
        woke = rnd() < _f(c, "idle_wake_rate", 0.2)
    return {
        "fire": bool(hit and woke), "hit": bool(hit), "p": p, "mode": mode,
        "night": night, "woke": woke, "kind": st.get("kind"), "chat_id": st.get("chat_id"),
        "who": st.get("who"), "idle_hours": round(idle_s / 3600.0, 2),
    }


def should_fire_solo(cfg: Any, now: Optional[float] = None, rnd=None) -> List[Dict[str, Any]]:
    """单独思念：每个填进来的 ID 各算一次，各自冷却、各自计数，互不干扰。"""
    c = _c(cfg)
    out: List[Dict[str, Any]] = []
    if not c.get("idle_enabled") or not c.get("idle_solo_enabled"):
        return out
    items = solo_list(cfg)
    if not items:
        return out
    now = now or time.time()
    rnd = rnd or random.random
    with _lock:
        st = _load()
    solo = st.get("solo") or {}
    day = time.strftime("%Y-%m-%d")
    night = is_night(c, now)
    gate = _f(c, "idle_after_hours", 6) * 3600
    cool = max(0.0, _f(c, "idle_solo_cooldown_hours", 6)) * 3600
    cap = max(0, _i(c, "idle_solo_max_per_day", 1))
    for it in items:
        e = solo.get(it["key"]) or {}
        last = float(e.get("last_chat_ts") or 0)
        if not last:
            continue  # 这个 ID 还没说过话
        idle_s = now - last
        if idle_s < gate:
            continue
        used = int(e.get("day_count") or 0) if e.get("day") == day else 0
        if used >= cap:
            continue
        _lf = float(e.get("last_fire_ts") or 0)
        if _lf and (now - _lf) < cool:
            continue  # 单独冷却没到
        p = rate_now(c, idle_s)
        hit = rnd() < p
        mode = pick_mode(c, rnd) if hit else ""
        woke = True
        if hit and night:
            woke = rnd() < _f(c, "idle_wake_rate", 0.2)
        out.append({
            "fire": bool(hit and woke), "hit": bool(hit), "p": p, "mode": mode,
            "night": night, "woke": woke, "kind": it["kind"], "chat_id": it["chat_id"],
            "who": e.get("who") or "", "idle_hours": round(idle_s / 3600.0, 2),
            "solo": True, "key": it["key"], "used_today": used,
        })
    return out


# ------------------------------------------------------------------ 提示词
def prompt_idle(cfg: Any) -> str:
    c = _c(cfg)
    return str(c.get("idle_prompt_idle") or DEFAULTS["idle_prompt_idle"])


def prompt_recall(cfg: Any, memory_block: str) -> str:
    c = _c(cfg)
    tpl = str(c.get("idle_prompt_recall") or DEFAULTS["idle_prompt_recall"])
    mem = (memory_block or "").strip() or "（这次没翻到合适的旧事，就随口说点别的吧。）"
    return tpl.replace("{memory}", mem)


def prompt_chat_recall(cfg: Any, memory_block: str) -> str:
    c = _c(cfg)
    tpl = str(c.get("idle_prompt_chat_recall") or DEFAULTS["idle_prompt_chat_recall"])
    mem = (memory_block or "").strip()
    if not mem:
        mem = "（这次没翻到具体记录，就凭你记得的印象，随口提一句以前聊过的小事。）"
    return tpl.replace("{memory}", mem)


def recall_query() -> str:
    return RECALL_QUERY


def kick(cfg: Any, mode: str) -> str:
    return _KICK.get(mode or "idle", _KICK["idle"])


def chat_recall_hit(cfg: Any, rnd=None) -> bool:
    c = _c(cfg)
    if not (c.get("idle_enabled") and c.get("idle_chat_recall_enabled")):
        return False
    return (rnd or random.random)() < max(0.0, min(1.0, _f(c, "idle_chat_recall_rate", 0.08)))


def _solo_status(c: Dict[str, Any], st: Dict[str, Any]) -> List[Dict[str, Any]]:
    """单独思念每个 ID 的当前状态（面板上显示用）。"""
    now = time.time()
    solo = st.get("solo") or {}
    day = time.strftime("%Y-%m-%d")
    gate = _f(c, "idle_after_hours", 6) * 3600
    out: List[Dict[str, Any]] = []
    for it in solo_list(c):
        e = solo.get(it["key"]) or {}
        last = float(e.get("last_chat_ts") or 0)
        idle_s = (now - last) if last else 0
        _lf = float(e.get("last_fire_ts") or 0)
        out.append({
            "key": it["key"], "kind": it["kind"], "chat_id": it["chat_id"],
            "who": e.get("who") or "", "idle_hours": round(idle_s / 3600.0, 2),
            "rate_now": round(rate_now(c, idle_s), 4),
            "day_count": int(e.get("day_count") or 0) if e.get("day") == day else 0,
            "last_fire": _lf, "ready": bool(last and idle_s >= gate),
        })
    return out


def status(cfg: Any) -> Dict[str, Any]:
    c = _c(cfg)
    with _lock:
        st = _load()
    last = float(st.get("last_chat_ts") or 0)
    idle_s = (time.time() - last) if last else 0
    return {
        "enabled": bool(c.get("idle_enabled")), "last_chat": last, "idle_hours": round(idle_s / 3600.0, 2),
        "rate_now": round(rate_now(c, idle_s), 4), "night": is_night(c), "kind": st.get("kind"),
        "chat_id": st.get("chat_id"), "day_count": int(st.get("day_count") or 0),
        "last_fire": float(st.get("last_fire_ts") or 0), "last_mode": st.get("last_mode") or "",
        "log": st.get("log") or [], "roll_log": st.get("roll_log") or [],
        "solo": _solo_status(c, st),
    }


def seed_from_history(history_file: str = "history.json", cfg: Any = None) -> float:
    """从聊天记录里回填「上一次说话时间」（比现有记录更近才更新）；单独思念的 ID 也各回填一份。

    服务一开就能知道已经闲置多久，而不是从 0 开始算。
    """
    try:
        import json as _json
        from pathlib import Path as _Path
        _p = _Path(history_file)
        if not _p.is_absolute():
            _p = _Path(__file__).resolve().parent / history_file
        if not _p.exists():
            return 0.0
        d = _json.loads(_p.read_text(encoding="utf-8")) or {}
    except Exception:
        return 0.0
    best = 0.0
    for _uid, rec in (d.items() if isinstance(d, dict) else []):
        try:
            for _m in ((rec or {}).get("messages") or []):
                try:
                    _v = float(_m.get("ts") or 0)
                except (TypeError, ValueError, AttributeError):
                    _v = 0.0
                if _v > best:
                    best = _v
        except Exception:
            continue
    if best <= 0:
        return 0.0
    with _lock:
        st = state()
        if best > float(st.get("last_chat_ts") or 0):
            st["last_chat_ts"] = best
            st["kind"] = st.get("kind") or ""
        try:
            _solo = st.get("solo") or {}
            for _it in solo_list(cfg):
                if _it["kind"] != "private":
                    continue  # 群聊记录不在 history.json 里
                _rec = d.get(str(_it["chat_id"])) or {}
                _bu = 0.0
                for _m in (_rec.get("messages") or []):
                    try:
                        _v = float(_m.get("ts") or 0)
                    except Exception:
                        _v = 0.0
                    if _v > _bu:
                        _bu = _v
                if _bu <= 0:
                    continue
                _e = _solo.get(_it["key"]) or {}
                if _bu > float(_e.get("last_chat_ts") or 0):
                    _e["last_chat_ts"] = _bu
                    _e["kind"] = _it["kind"]
                    _e["chat_id"] = _it["chat_id"]
                    _solo[_it["key"]] = _e
            st["solo"] = _solo
        except Exception:
            pass
        _save(st)
    return best
