# -*- coding: utf-8 -*-
"""\u90ca\u72fc\uff08DG-Lab Coyote\uff09\u8fde\u63a5\uff1a\u901a\u8fc7 DG-Lab Coyote Game Hub \u7684 HTTP API \u63a7\u5236\u8bbe\u5907\u3002
\u72ec\u7acb\u6a21\u5757\uff0c\u53ea\u8bfb\u5199 config.json \u7684 coyote \u6bb5\u3002\u5e26\u5b89\u5168\u9650\u5236\uff1a\u5f3a\u5ea6/\u65f6\u957f/\u51b7\u5374/\u6bcf\u5c0f\u65f6\u4e0a\u9650\u3002
"""
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_JSON = Path("config.json")

DEFAULTS = {
    "enabled": False,
    "base_url": "http://127.0.0.1:8920",
    "client_id": "all",
    "verify_ssl": False,
    "timeout": 8,
    "max_strength": 20,      # \u5355\u6b21\u6700\u9ad8\u5f3a\u5ea6\uff08Hub \u4e0a\u9650 40\uff09
    "max_time": 5000,        # \u5355\u6b21\u6700\u957f\u65f6\u957f ms\uff08Hub \u4e0a\u9650 30000\uff09
    "cooldown": 10,          # \u4e24\u6b21\u64cd\u4f5c\u6700\u77ed\u95f4\u9694\uff08\u79d2\uff09
    "max_per_hour": 20,      # \u6bcf\u5c0f\u65f6\u4e0a\u9650
    "allow_ai_strength": True,   # \u5141\u8bb8 AI \u76f4\u63a5\u6539\u5f3a\u5ea6
    "notify_console": True,
}

_state = {"last_action": 0.0, "hour_hits": [], "last_error": "", "last_ok": "",
          "last_payload": "", "log": [], "conn": None, "client_strength": None,
          "client_limit": None, "pulse_id": "", "fire_count": 0}
_logger = None


def set_logger(fn):
    global _logger
    _logger = fn


def _log(level, msg):
    line = "%s %s %s" % (level, time.strftime("%H:%M:%S"), msg)
    _state["log"].append(line)
    del _state["log"][:-120]
    try:
        print("[coyote] " + line, flush=True)
    except Exception:
        pass
    if _logger:
        try:
            _logger(level, "[\u90ca\u72fc] " + msg)
        except Exception:
            pass


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        d = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("coyote"), dict):
            for k, v in d["coyote"].items():
                if k in DEFAULTS:
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def save_cfg(part):
    try:
        d = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    cur = d.get("coyote") if isinstance(d.get("coyote"), dict) else {}
    cur.update({k: v for k, v in (part or {}).items() if k in DEFAULTS})
    d["coyote"] = cur
    tmp = str(CONFIG_JSON) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_JSON)
    return cur


def _base():
    return str(load_cfg().get("base_url") or "").rstrip("/")


def api(method, path, body=None, timeout=None):
    cfg = load_cfg()
    base = _base()
    if not base:
        _state["last_error"] = "\u672a\u914d\u7f6e Hub \u5730\u5740"
        return None
    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    ctx = None
    if url.lower().startswith("https") and not cfg.get("verify_ssl"):
        ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=float(timeout or cfg.get("timeout") or 8), context=ctx) as r:
            txt = r.read().decode("utf-8", "replace")
        try:
            return json.loads(txt)
        except Exception:
            return {"status": 1, "raw": txt}
    except urllib.error.HTTPError as e:
        _state["last_error"] = "HTTP %s" % e.code
        return {"status": 0, "code": "HTTP_%s" % e.code, "message": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        _state["last_error"] = "%s: %s" % (type(e).__name__, e)
        return None


def game_info():
    """GET /api/v2/game/{clientId} \u2014\u2014 \u8fde\u901a\u6027\u4e0e\u5f53\u524d\u72b6\u6001"""
    cfg = load_cfg()
    r = api("GET", "/api/v2/game/%s" % (cfg.get("client_id") or "all"))
    if isinstance(r, dict) and r.get("status") == 1:
        cs = r.get("clientStrength") or {}
        _state["conn"] = True
        _state["client_strength"] = cs.get("strength")
        _state["client_limit"] = cs.get("limit")
        _state["pulse_id"] = str(r.get("currentPulseId") or "")
        return r
    _state["conn"] = False
    return r


def pulse_list():
    cfg = load_cfg()
    r = api("GET", "/api/v2/game/%s/pulse_list" % (cfg.get("client_id") or "all"))
    if isinstance(r, dict):
        return r.get("pulseList") or []
    return []


def _rate_ok(cfg):
    now = time.time()
    _state["hour_hits"] = [t for t in _state["hour_hits"] if now - t < 3600]
    if len(_state["hour_hits"]) >= int(cfg.get("max_per_hour") or 20):
        return False, "\u672c\u5c0f\u65f6\u5df2\u8fbe\u4e0a\u9650\uff08%s \u6b21\uff09" % cfg.get("max_per_hour")
    cd = float(cfg.get("cooldown") or 0)
    if cd and now - float(_state["last_action"] or 0) < cd:
        return False, "\u51b7\u5374\u4e2d\uff0c\u8fd8\u9700 %.0f \u79d2" % (cd - (now - _state["last_action"]))
    return True, ""


def fire(strength=10, ms=None, override=None, pulse_id=None, source="\u624b\u52a8"):
    """POST /api/v2/game/{clientId}/action/fire\uff08\u5e26\u5b89\u5168\u9650\u5236\uff09"""
    cfg = load_cfg()
    if not cfg.get("enabled"):
        return False, "\u90ca\u72fc\u8fde\u63a5\u672a\u542f\u7528"
    ok, why = _rate_ok(cfg)
    if not ok:
        _log("WARN", "\u62d2\u7edd\u5f00\u706b\uff1a%s" % why)
        return False, why
    try:
        st = int(float(strength))
    except Exception:
        return False, "\u5f3a\u5ea6\u4e0d\u5408\u6cd5"
    cap = int(cfg.get("max_strength") or 20)
    if st < 0:
        return False, "\u5f3a\u5ea6\u4e0d\u80fd\u4e3a\u8d1f"
    if st > cap:
        _log("WARN", "\u8bf7\u6c42\u5f3a\u5ea6 %d \u8d85\u8fc7\u4e0a\u9650 %d\uff0c\u5df2\u622a\u65ad" % (st, cap))
        st = cap
    try:
        tm = int(float(ms)) if ms is not None else int(cfg.get("max_time") or 5000)
    except Exception:
        tm = int(cfg.get("max_time") or 5000)
    tcap = int(cfg.get("max_time") or 5000)
    if tm > tcap:
        tm = tcap
    body = {"strength": st, "time": tm}
    if override is not None:
        body["override"] = bool(override)
    if pulse_id:
        body["pulseId"] = str(pulse_id)
    cid = cfg.get("client_id") or "all"
    r = api("POST", "/api/v2/game/%s/action/fire" % cid, body)
    _state["last_action"] = time.time()
    _state["hour_hits"].append(time.time())
    _state["last_payload"] = json.dumps(body, ensure_ascii=False)
    if isinstance(r, dict) and r.get("status") == 1:
        _state["fire_count"] += 1
        _state["last_ok"] = "\u5f3a\u5ea6 %d / %dms" % (st, tm)
        _log("INFO", "\u5f00\u706b\u6210\u529f\uff08%s\uff09\uff1a%s" % (source, _state["last_ok"]))
        return True, "\u5df2\u5411 %s \u53d1\u9001\u5f00\u706b\uff1a\u5f3a\u5ea6 %d\uff0c%d \u6beb\u79d2" % (cid, st, tm)
    msg = (r or {}).get("message") or _state["last_error"] or "\u672a\u77e5\u9519\u8bef"
    _log("ERROR", "\u5f00\u706b\u5931\u8d25\uff1a%s" % msg)
    return False, "\u5f00\u706b\u5931\u8d25\uff1a%s" % msg


def set_strength(add=None, sub=None, set_to=None, random_add=None):
    """POST /api/v2/game/{clientId}/strength"""
    cfg = load_cfg()
    if not cfg.get("enabled"):
        return False, "\u90ca\u72fc\u8fde\u63a5\u672a\u542f\u7528"
    if not cfg.get("allow_ai_strength", True):
        return False, "\u5f53\u524d\u8bbe\u7f6e\u4e0d\u5141\u8bb8\u6539\u5f3a\u5ea6"
    cap = int(cfg.get("max_strength") or 20)
    body = {}
    try:
        if set_to is not None:
            v = max(0, min(int(float(set_to)), cap))
            body["strength"] = {"set": v}
        elif add is not None:
            body["strength"] = {"add": abs(int(float(add)))}
        elif sub is not None:
            body["strength"] = {"sub": abs(int(float(sub)))}
        if random_add is not None:
            body["randomStrength"] = {"add": abs(int(float(random_add)))}
    except Exception:
        return False, "\u5f3a\u5ea6\u53c2\u6570\u4e0d\u5408\u6cd5"
    if not body:
        return False, "\u6ca1\u6709\u8981\u6539\u7684\u5f3a\u5ea6"
    ok, why = _rate_ok(cfg)
    if not ok:
        return False, why
    cid = cfg.get("client_id") or "all"
    r = api("POST", "/api/v2/game/%s/strength" % cid, body)
    _state["last_action"] = time.time()
    _state["hour_hits"].append(time.time())
    if isinstance(r, dict) and r.get("status") == 1:
        _log("INFO", "\u5f3a\u5ea6\u8c03\u6574\u6210\u529f\uff1a%s" % json.dumps(body, ensure_ascii=False))
        return True, "\u5df2\u8c03\u6574\u5f3a\u5ea6"
    msg = (r or {}).get("message") or _state["last_error"] or "\u672a\u77e5\u9519\u8bef"
    return False, "\u8c03\u6574\u5931\u8d25\uff1a%s" % msg


# ---------------- AI \u6807\u7b7e ----------------
_TAG_FIRE = re.compile(r"<coyote_fire\b([^>]*?)/?>", re.I)
_TAG_STR = re.compile(r"<coyote_strength\b([^>]*?)/?>", re.I)
_ATTR = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def _attrs(s):
    return {k.lower(): v for k, v in _ATTR.findall(s or "")}


def prompt_block():
    """\u7ed9 AI \u7684\u8bf4\u660e\uff08\u53ea\u5728\u542f\u7528\u65f6\u8fd4\u56de\uff09"""
    cfg = load_cfg()
    if not cfg.get("enabled"):
        return ""
    lines = ["<coyote_control>",
             "\u4f60\u53ef\u4ee5\u901a\u8fc7\u4e0b\u9762\u7684\u6807\u7b7e\u64cd\u4f5c\u5bf9\u65b9\u8eab\u4e0a\u7684\u90ca\u72fc\u8bbe\u5907\uff08\u6807\u7b7e\u4e0d\u4f1a\u53d1\u7ed9\u5bf9\u65b9\uff0c\u53ea\u4f5c\u4e3a\u64cd\u4f5c\u6307\u4ee4\uff09\uff1a",
             '1. \u4e00\u952e\u5f00\u706b\uff1a<coyote_fire strength="\u5f3a\u5ea6" time="\u6beb\u79d2" />',
             "   \u5f3a\u5ea6 1~%d\uff0c\u65f6\u957f\u6700\u957f %d \u6beb\u79d2\u3002" % (cfg.get("max_strength"), cfg.get("max_time"))]
    if cfg.get("allow_ai_strength", True):
        lines.append('2. \u8c03\u6574\u5f3a\u5ea6\uff1a<coyote_strength add="\u589e\u91cf" /> \u6216 <coyote_strength sub="\u51cf\u91cf" /> \u6216 <coyote_strength set="\u76ee\u6807\u503c" />')
    lines.append("\u53ea\u5728\u5bf9\u65b9\u660e\u786e\u540c\u610f\u6216\u4f60\u4eec\u7684\u89d2\u8272\u5173\u7cfb\u786e\u5b9a\u4f1a\u8fd9\u4e48\u505a\u65f6\u4f7f\u7528\uff0c\u4e0d\u8981\u968f\u610f\u53d1\u3002")
    lines.append("</coyote_control>")
    return "\n".join(lines)


def extract(text, source="AI"):
    """\u4ece\u56de\u590d\u91cc\u62bd\u6807\u7b7e\u5e76\u6267\u884c\uff0c\u8fd4\u56de\uff08\u5254\u9664\u540e\u7684\u6587\u672c, \u6267\u884c\u7ed3\u679c\u5217\u8868\uff09"""
    results = []

    def _do_fire(m):
        at = _attrs(m.group(1))
        try:
            st = int(float(at.get("strength", 10)))
        except Exception:
            st = 10
        try:
            tm = int(float(at["time"])) if at.get("time") else None
        except Exception:
            tm = None
        ov = None
        if "override" in at:
            ov = str(at["override"]).lower() in ("1", "true", "yes")
        ok, msg = fire(st, tm, ov, at.get("pulse"), source)
        results.append({"tag": "coyote_fire", "ok": ok, "message": msg})
        return ""

    def _do_str(m):
        at = _attrs(m.group(1))
        ok, msg = set_strength(add=at.get("add"), sub=at.get("sub"),
                               set_to=at.get("set"), random_add=at.get("random"))
        results.append({"tag": "coyote_strength", "ok": ok, "message": msg})
        return ""

    out = _TAG_FIRE.sub(_do_fire, text or "")
    out = _TAG_STR.sub(_do_str, out)
    out = re.sub(r"[ \t]*\n{3,}", "\n\n", out).strip()
    return out, results


def status():
    cfg = load_cfg()
    return {
        "config": cfg,
        "conn": _state["conn"],
        "client_strength": _state["client_strength"],
        "client_limit": _state["client_limit"],
        "pulse_id": _state["pulse_id"],
        "fire_count": _state["fire_count"],
        "hour_hits": len([t for t in _state["hour_hits"] if time.time() - t < 3600]),
        "last_ok": _state["last_ok"],
        "last_error": _state["last_error"],
        "last_payload": _state["last_payload"],
        "log": _state["log"][-60:],
    }


def self_test():
    cfg = load_cfg()
    info = game_info()
    return ["base_url=%s client_id=%s enabled=%s" % (cfg.get("base_url"), cfg.get("client_id"), cfg.get("enabled")),
            "conn=%s strength=%s limit=%s" % (_state["conn"], _state["client_strength"], _state["client_limit"]),
            "info_status=%s err=%s" % ((info or {}).get("status"), _state["last_error"])]
