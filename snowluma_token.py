# -*- coding: utf-8 -*-
"""SnowLuma 协议端：安装目录推导 + OneBot accessToken 自动拉取。

设计目标：安装地址不写死。用户只在控制台左下角填 SnowLuma 的「启动命令」
（安装地址），这里就按那个地址推出安装目录，再把 OneBot 的 accessToken 拉回来，
供 OneBot 客户端连接时自动拼进 URL（SnowLuma 的 OneBot 网络默认开鉴权，
不带 token 握手会 401）。

拉取两条路线，先文件后接口：
  1) 直接读 <安装目录>/config/onebot_<uin>.json 里的 networks.*Servers[].accessToken
  2) 读不到时走 SnowLuma 自带 WebUI 的 HTTP 接口：
       POST /api/login        {password, totp?, recoveryCode?} -> {token}
       GET  /api/qq-list                                        -> {list:[{uin,nickname}]}
       GET  /api/config/<uin>                                   -> {config}
     /api/* 除 /api/login、/api/ui/public 外都要会话 token，鉴权头是
     x-snowluma-token 或 Authorization: Bearer <token>；缺失即 401。

本模块只用标准库，不 import app / bot，避免循环依赖。
"""
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path


def _cfg_path():
    """定位 config.json：优先与 snowluma_token.py 同目录（打包成 exe 时退回当前目录）。"""
    try:
        _p = Path(__file__).resolve().with_name("config.json")
        if _p.exists():
            return _p
    except Exception:
        pass
    return Path("config.json")


CONFIG_JSON = _cfg_path()
DEFAULT_WS_URL = "ws://127.0.0.1:3001"
DEFAULT_WS_PORT = 3001
DEFAULT_HTTP_PORT = 3000
DEFAULT_DIR = r"C:\ai\SnowLuma"

_cache = {"at": 0.0, "data": {}}
_TTL = 5.0


def load_cfg():
    """读 config.json（只看 SnowLuma 相关字段）。"""
    try:
        d = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def snowluma_dir(cfg=None):
    """推出 SnowLuma 的安装目录：显式配置 > 从「启动命令」反推 > 默认路径。

    左下角填的是启动脚本（如 ...\\_sl_dl2\\launcher.bat），取它所在目录，再逐级
    向上找带 config/ 或 index.mjs / package.json 的那一层——所以不管装在哪个盘、
    启动脚本在不在根目录，都能定位到安装根。
    """
    cfg = cfg if isinstance(cfg, dict) else load_cfg()
    _explicit = str(cfg.get("snowluma_dir") or "").strip().strip('"')
    if _explicit:
        return _explicit
    _cmd = str(cfg.get("snowluma_cmd") or "").strip().strip('"').strip("'")
    _base = ""
    if _cmd:
        try:
            _p = Path(_cmd)
            _base = str(_p.parent if _p.suffix else _p)
        except Exception:
            _base = ""
    _cands = []
    if _base:
        try:
            _cur = Path(_base)
            for _ in range(4):
                _cands.append(str(_cur))
                if _cur.parent == _cur:
                    break
                _cur = _cur.parent
        except Exception:
            pass
    _cands.append(DEFAULT_DIR)
    for _c in _cands:
        try:
            if ((Path(_c) / "config").is_dir()
                    or (Path(_c) / "index.mjs").exists()
                    or (Path(_c) / "package.json").exists()):
                return _c
        except Exception:
            continue
    return _base or DEFAULT_DIR


def config_dir(cfg=None):
    return Path(snowluma_dir(cfg)) / "config"


def onebot_files(cfg=None):
    """SnowLuma 的 OneBot 配置：config/onebot_<uin>.json（每个账号一份）。"""
    try:
        return sorted(config_dir(cfg).glob("onebot_*.json"))
    except Exception:
        return []


def pick_token(net, kind, default_port):
    """从 networks.<kind> 里挑 (port, accessToken)。

    注意 SnowLuma 的字段名是 accessToken（NapCat 才是 token），且没有 enable 开关，
    所以不能沿用 NapCat 那套判断。
    """
    _first = None
    for s in (net.get(kind) or []):
        if not isinstance(s, dict):
            continue
        try:
            _p = int(s.get("port") or default_port)
        except Exception:
            _p = default_port
        _t = str(s.get("accessToken") or s.get("token") or "")
        if _first is None:
            _first = (_p, _t)
        if _t:
            return _p, _t
    return _first or (default_port, "")


def _pack(net, uin, source):
    _wp, _wt = pick_token(net, "wsServers", DEFAULT_WS_PORT)
    _hp, _ht = pick_token(net, "httpServers", DEFAULT_HTTP_PORT)
    return {"ws_port": _wp, "ws_token": _wt,
            "http_port": _hp, "http_token": _ht,
            "uin": uin, "source": source}


def from_config_file(cfg=None):
    """路线 1：直接读 SnowLuma 自己的 config/onebot_<uin>.json。"""
    _out = {"ws_port": DEFAULT_WS_PORT, "ws_token": "",
            "http_port": DEFAULT_HTTP_PORT, "http_token": "",
            "uin": "", "source": ""}
    for f in onebot_files(cfg):
        try:
            d = json.loads(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        net = d.get("networks") if isinstance(d.get("networks"), dict) else {}
        m = re.search(r"onebot_(\d+)", f.name)
        _r = _pack(net, (m.group(1) if m else ""), "config 文件")
        _r["file"] = str(f)
        if _r["ws_token"] or _r["http_token"]:
            return _r
        if not _out["uin"]:
            _out = _r
    return _out


# ---------------- 路线 2：SnowLuma 自带 WebUI 的 HTTP 接口 ----------------
def webui_base(cfg=None):
    """SnowLuma 自带 WebUI 的地址（端口读它自己 config/runtime.json，默认 5099）。"""
    cfg = cfg if isinstance(cfg, dict) else load_cfg()
    _port, _host = 5099, "127.0.0.1"
    try:
        c = json.loads((config_dir(cfg) / "runtime.json").read_text(encoding="utf-8")) or {}
        _port = int(c.get("webuiPort") or 5099)
        _h = str(c.get("webuiHost") or "").strip()
        if _h and _h not in ("0.0.0.0", "::", "::0"):
            _host = _h
    except Exception:
        pass
    return "http://%s:%d" % (_host, _port)


def _webui_call(path, token="", body=None, timeout=4.0, cfg=None):
    _hdrs = {"Content-Type": "application/json"}
    if token:
        _hdrs["x-snowluma-token"] = token
        _hdrs["Authorization"] = "Bearer " + token
    _req = urllib.request.Request(
        webui_base(cfg).rstrip("/") + path,
        data=(json.dumps(body).encode("utf-8") if body is not None else None),
        method=("POST" if body is not None else "GET"),
        headers=_hdrs)
    with urllib.request.urlopen(_req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def webui_login(cfg=None):
    """用配置里的 WebUI 密码换会话 token；没配密码就返回空串。"""
    cfg = cfg if isinstance(cfg, dict) else load_cfg()
    _pw = str(cfg.get("snowluma_webui_password") or "")
    if not _pw:
        return ""
    try:
        r = _webui_call("/api/login", "", {"password": _pw}, timeout=6.0, cfg=cfg)
        return str((r or {}).get("token") or "")
    except Exception:
        return ""


def from_webui(cfg=None):
    """路线 2：走 SnowLuma 自带 WebUI 的接口拉 OneBot 配置。"""
    cfg = cfg if isinstance(cfg, dict) else load_cfg()
    _out = {"ws_port": DEFAULT_WS_PORT, "ws_token": "",
            "http_port": DEFAULT_HTTP_PORT, "http_token": "",
            "uin": "", "source": ""}
    _tok = str(cfg.get("snowluma_webui_session") or "").strip()
    for _round in (0, 1):
        if _round == 1:
            _tok = webui_login(cfg)      # 会话过期/没会话 -> 用密码换一个
            if not _tok:
                return _out
        try:
            _list = (_webui_call("/api/qq-list", _tok, cfg=cfg) or {}).get("list") or []
        except Exception:
            continue
        for a in _list:
            _uin = str((a or {}).get("uin") or "")
            if not _uin:
                continue
            try:
                _cfgd = (_webui_call("/api/config/" + _uin, _tok, cfg=cfg) or {}).get("config") or {}
            except Exception:
                continue
            _net = _cfgd.get("networks") if isinstance(_cfgd.get("networks"), dict) else {}
            _r = _pack(_net, _uin, "WebUI 接口")
            _r["webui_session"] = _tok
            if _r["ws_token"] or _r["http_token"]:
                return _r
            if not _out["uin"]:
                _out = _r
        return _out
    return _out


def tokens(cfg=None, force=False):
    """先 config 文件、再 WebUI 接口；结果缓存几秒，避免每次连接都读盘/发请求。"""
    _now = time.time()
    if not force and _cache["data"] and (_now - _cache["at"]) < _TTL:
        return _cache["data"]
    _d = {}
    try:
        _d = from_config_file(cfg)
    except Exception:
        _d = {}
    if not (_d.get("ws_token") or _d.get("http_token")):
        try:
            _w = from_webui(cfg)
            if _w.get("ws_token") or _w.get("http_token"):
                _d = _w
        except Exception:
            pass
    _cache["at"] = _now
    _cache["data"] = _d
    return _d


def ws_url_with_token(url="", cfg=None):
    """把 OneBot WS 地址补成带 access_token 的形式。

    - 地址里已有 access_token 但和刚读到的不一致（SnowLuma 换过 token）-> 用新的覆盖
    - 读不到 token -> 原样返回（保留手填地址，不擅自改动）
    """
    _cfg = cfg if isinstance(cfg, dict) else load_cfg()
    _u = str(url or "").strip() or str(_cfg.get("snowluma_ws_url") or "").strip() or DEFAULT_WS_URL
    _t = str(tokens(_cfg).get("ws_token") or "")
    if not _t:
        return _u
    try:
        _p = urllib.parse.urlsplit(_u)
        _q = [(k, v) for k, v in urllib.parse.parse_qsl(_p.query, keep_blank_values=True)
              if k != "access_token"]
        _q.append(("access_token", _t))
        return urllib.parse.urlunsplit((_p.scheme, _p.netloc, _p.path,
                                        urllib.parse.urlencode(_q), _p.fragment))
    except Exception:
        return _u
