# -*- coding: utf-8 -*-
"""\u60c5\u7eea\u6ce8\u5165\uff1a\u628a\u81ea\u5df1\u7684\u60c5\u7eea\u72b6\u51b5 + \u7cbe\u529b/\u60c5\u6b32/\u5fc3\u60c5\u53d1\u7ed9 AI\uff0c\u8ba9\u5b83\u4ee5\u60c5\u7eea\u4e3a\u5e95\u8272\u56de\u590d\uff1b
AI \u56de\u590d\u91cc\u7528 <emotion ... /> \u5757\u56de\u4f20\u66f4\u65b0\u540e\u7684\u6570\u503c\uff0c\u8fd9\u91cc\u89e3\u6790\u5e76\u5199\u56de\u3002

\u72b6\u6001\u6587\u4ef6\uff1amood.json\uff08\u539f\u5b50\u5199\u76d8 + \u7ebf\u7a0b\u9501\uff09
"""
import json
import os
import re
import threading
import time
from pathlib import Path

_FILE = Path("mood.json")
_LOCK = threading.RLock()

_DEFAULT_PROMPT = """\u3010\u4f60\u81ea\u5df1\u7684\u60c5\u7eea\u72b6\u6001\uff08\u8fc7\u53bb\u8fd9\u6bb5\u65f6\u95f4\uff09\u3011
- \u60c5\u7eea\u72b6\u51b5\uff1a{word} \u2014\u2014 {desc}
- \u7cbe\u529b\u503c\uff1a{energy}/100
- \u60c5\u6b32\u503c\uff1a{lust}/100
- \u5fc3\u60c5\u503c\uff1a{mood}/100

\u8fd9\u4e0d\u662f\u53f0\u8bcd\uff0c\u662f\u4f60\u6b64\u523b\u7684\u5e95\u8272\u3002\u8bf7\u8ba9\u8fd9\u4e00\u6761\u56de\u590d\u5e26\u7740\u8fd9\u4e2a\u60c5\u7eea\u53bb\u53cd\u5e94\uff1a
\u5fc3\u60c5\u597d\u5c31\u653e\u677e\u3001\u8bdd\u591a\u3001\u613f\u610f\u63a5\u8bdd\uff1b\u7d2f\u5c31\u77ed\u3001\u6577\u884d\u3001\u61d2\u5f97\u5c55\u5f00\uff1b
\u70e6\u8e81\u5c31\u51b2\u4e00\u70b9\u3001\u5e26\u523a\uff1b\u60c5\u6b32\u9ad8\u5c31\u7c98\u4eba\u3001\u5e26\u70b9\u6311\u9017\u3002
\u6570\u503c\u53ea\u5f71\u54cd\u8bed\u6c14\u3001\u8282\u594f\u548c\u7528\u8bcd\uff0c\u4e0d\u8981\u628a\u6570\u5b57\u76f4\u63a5\u8bf4\u51fa\u6765\uff0c\u4e5f\u4e0d\u8981\u89e3\u91ca\u8fd9\u6bb5\u8bbe\u5b9a\u3002

\u56de\u590d\u7684\u6700\u540e\uff0c\u5fc5\u987b\u9644\u4e0a\u4e00\u6761\u66f4\u65b0\u540e\u7684\u60c5\u7eea\u5757\uff08\u6839\u636e\u8fd9\u8f6e\u5bf9\u8bdd\u5bf9\u4f60\u7684\u5f71\u54cd\u5fae\u8c03\u6570\u503c\uff0c
\u5355\u6b21\u53d8\u5316\u6700\u591a \u00b115\uff1bword \u662f\u4e00\u4e24\u4e2a\u5b57\uff0cdesc \u662f\u4e00\u53e5\u8bdd\uff09\uff1a
<emotion energy="{energy}" lust="{lust}" mood="{mood}" word="{word}" desc="\u4e00\u53e5\u8bdd\u63cf\u8ff0\u4f60\u73b0\u5728\u7684\u60c5\u7eea\u72b6\u51b5" />"""

DEFAULTS = {
    "mood_enabled": True,
    "mood_prompt": _DEFAULT_PROMPT,
}

_DEF_STATE = {
    "word": "\u5e73\u9759",
    "desc": "\u6ca1\u4ec0\u4e48\u8d77\u4f0f\uff0c\u5c31\u90a3\u6837\u3002",
    "energy": 60,
    "lust": 20,
    "mood": 70,
    "updated_at": 0.0,
    "log": [],
}


def _c(cfg):
    return cfg if isinstance(cfg, dict) else {}


def _f(cfg, key, d=0.0):
    try:
        return float(_c(cfg).get(key, d))
    except (TypeError, ValueError):
        return float(d)


def _i(cfg, key, d=0):
    try:
        return int(float(_c(cfg).get(key, d)))
    except (TypeError, ValueError):
        return int(d)


def enabled(cfg) -> bool:
    return _c(cfg).get("mood_enabled", True) is not False


def _load() -> dict:
    try:
        if _FILE.exists():
            d = json.loads(_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                out = dict(_DEF_STATE)
                out.update(d)
                return out
    except Exception:
        pass
    return dict(_DEF_STATE)


def _write(st: dict) -> None:
    try:
        p = _FILE if _FILE.is_absolute() else Path(__file__).resolve().parent / _FILE
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def state() -> dict:
    with _LOCK:
        return _load()


def _clamp(v, lo=0, hi=100):
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def current(cfg=None) -> dict:
    st = state()
    return {
        "word": str(st.get("word") or _DEF_STATE["word"]),
        "desc": str(st.get("desc") or _DEF_STATE["desc"]),
        "energy": _clamp(st.get("energy"), 0, 100) if st.get("energy") is not None else 60,
        "lust": _clamp(st.get("lust"), 0, 100) if st.get("lust") is not None else 20,
        "mood": _clamp(st.get("mood"), 0, 100) if st.get("mood") is not None else 70,
        "updated_at": float(st.get("updated_at") or 0),
    }


def set_values(cfg=None, word=None, desc=None, energy=None, lust=None, mood=None, note="") -> dict:
    """\u624b\u52a8/\u81ea\u52a8\u66f4\u65b0\u5f53\u524d\u60c5\u7eea\u3002\u53ea\u6539\u4f20\u5165\u7684\u5b57\u6bb5\u3002"""
    with _LOCK:
        st = _load()
        if word is not None and str(word).strip():
            st["word"] = str(word).strip()[:12]
        if desc is not None and str(desc).strip():
            st["desc"] = str(desc).strip()[:200]
        for k, v in (("energy", energy), ("lust", lust), ("mood", mood)):
            if v is None:
                continue
            cv = _clamp(v)
            if cv is not None:
                st[k] = cv
        st["updated_at"] = time.time()
        if note:
            lg = list(st.get("log") or [])
            lg.append({"at": time.strftime("%m-%d %H:%M"), "msg": str(note)[:120]})
            st["log"] = lg[-20:]
        _write(st)
    return current(cfg)


def prompt_block(cfg) -> str:
    """\u62fc\u51fa\u8981\u6ce8\u5165\u5230\u7cfb\u7edf\u63d0\u793a\u8bcd\u91cc\u7684\u60c5\u7eea\u6bb5\u3002"""
    if not enabled(cfg):
        return ""
    cur = current(cfg)
    tpl = str(_c(cfg).get("mood_prompt") or "").strip() or _DEFAULT_PROMPT
    try:
        return tpl.format(word=cur["word"], desc=cur["desc"], energy=cur["energy"],
                          lust=cur["lust"], mood=cur["mood"]).strip()
    except Exception:
        return (_DEFAULT_PROMPT.format(word=cur["word"], desc=cur["desc"], energy=cur["energy"],
                                       lust=cur["lust"], mood=cur["mood"])).strip()


_TAG = re.compile(r"<emotion\b([^>]*?)(?:/>|>([\s\S]*?)</emotion\s*>)", re.IGNORECASE)
_KV = re.compile(r"(\w+)\s*[:=\uff1a]\s*(-?\d+|[^\s,;\uff0c\uff1b]+)")


def _parse_body(body: str) -> dict:
    """\u5185\u5bb9\u5f0f\uff1aJSON \u4f18\u5148\uff0c\u5176\u6b21 \u7cbe\u529b:62 \u60c5\u6b32:35 \u5fc3\u60c5:58 \u70e6|\u63cf\u8ff0\u3002"""
    d = {}
    s = (body or "").strip()
    if not s:
        return d
    try:
        j = json.loads(s)
        if isinstance(j, dict):
            return {str(k).lower(): v for k, v in j.items()}
    except Exception:
        pass
    m = re.search(r"([^\n|]+)\|([^\n]*)", s)
    if m:
        d["word"] = m.group(1).strip()
        d["desc"] = m.group(2).strip()
    for k, v in _KV.findall(s):
        d.setdefault(k.lower(), v)
    if "word" in d:
        # 把混进 word 里的「键:值」片段清掉，只留最后一个词
        _w = re.sub(r"[^\s,;\uff0c\uff1b|]*\s*[:=\uff1a]\s*[^\s,;\uff0c\uff1b|]+", " ", str(d["word"]))
        _w = _w.replace("|", " ").strip()
        _toks = _w.split()
        d["word"] = _toks[-1] if _toks else ""
    return d


_ALIAS = {"energy": "energy", "\u7cbe\u529b": "energy", "\u7cbe\u529b\u503c": "energy",
          "lust": "lust", "\u60c5\u6b32": "lust", "\u60c5\u6b32\u503c": "lust",
          "mood": "mood", "\u5fc3\u60c5": "mood", "\u5fc3\u60c5\u503c": "mood",
          "word": "word", "\u60c5\u7eea": "word", "\u60c5\u7eea\u72b6\u51b5": "word",
          "desc": "desc", "\u63cf\u8ff0": "desc"}


def extract(text: str):
    """\u4ece AI \u56de\u590d\u91cc\u62bd\u51fa\u60c5\u7eea\u5757\u3002\u8fd4\u56de (clean_text, updated_dict)\u3002"""
    s = str(text or "")
    upd = {}
    for m in _TAG.finditer(s):
        attrs, body = m.group(1) or "", m.group(2) or ""
        d = {}
        for k, v in re.findall(r'(\w+)\s*=\s*"([^"]*)"', attrs):
            d[k.lower()] = v
        for k, v in re.findall(r"(\w+)\s*=\s*'([^']*)'", attrs):
            d.setdefault(k.lower(), v)
        if not d:
            d = _parse_body(body)
        for k, v in d.items():
            kk = _ALIAS.get(str(k).lower())
            if kk:
                upd[kk] = v
    if upd:
        s = _TAG.sub("", s).strip()
    return s, upd


def save_parsed(upd: dict, note: str = "\u6a21\u578b\u56de\u4f20\u66f4\u65b0") -> dict:
    """\u628a extract \u62ff\u5230\u7684\u503c\u5199\u56de\u3002"""
    if not upd:
        return current()
    kw = {}
    for k in ("word", "desc"):
        if upd.get(k) not in (None, ""):
            kw[k] = upd[k]
    for k in ("energy", "lust", "mood"):
        if upd.get(k) not in (None, ""):
            kw[k] = upd[k]
    if not kw:
        return current()
    old = current()
    chg = []
    for k in ("energy", "lust", "mood"):
        if k in kw:
            nv = _clamp(kw[k])
            if nv is not None and nv != old[k]:
                chg.append("%s %s\u2192%s" % ({"energy": "\u7cbe\u529b", "lust": "\u60c5\u6b32", "mood": "\u5fc3\u60c5"}[k], old[k], nv))
    if kw.get("word") and kw["word"] != old["word"]:
        chg.append("\u60c5\u7eea %s\u2192%s" % (old["word"], kw["word"]))
    return set_values(None, note=("%s\uff1a%s" % (note, "\u3001".join(chg)) if chg else note), **kw)


def status(cfg) -> dict:
    cur = current(cfg)
    st = state()
    return {
        "enabled": enabled(cfg),
        "word": cur["word"],
        "desc": cur["desc"],
        "energy": cur["energy"],
        "lust": cur["lust"],
        "mood": cur["mood"],
        "updated_at": cur["updated_at"],
        "updated_text": (time.strftime("%m-%d %H:%M", time.localtime(cur["updated_at"]))
                         if cur["updated_at"] else "\u8fd8\u6ca1\u66f4\u65b0\u8fc7"),
        "log": list(st.get("log") or [])[-12:],
    }
