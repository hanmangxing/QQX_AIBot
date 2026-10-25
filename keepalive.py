# -*- coding: utf-8 -*-
"""NapCat \u4fdd\u6d3b\uff1a\u68c0\u6d4b\u6389\u7ebf -> L1~L5 \u5206\u7ea7\u91cd\u767b\u3002
\u72ec\u7acb\u6a21\u5757\uff0c\u53ea\u8bfb\u5199 config.json \u7684 keepalive \u6bb5\uff0c\u4e0d\u4f9d\u8d56 app.py \u5185\u90e8\u5b9e\u73b0\u3002
"""
import asyncio
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

NAP = Path(r"C:\ai\NapCat.Shell.Windows.Node")
WEBUI_JSON = NAP / "napcat" / "config" / "webui.json"
LIVE_JSON = Path("napcat_webui_live.json")   # app.py 抓到 NapCat 实际 WebUI 地址后写这里（端口可能漂移）
QR_PNG = NAP / "napcat" / "cache" / "qrcode.png"     # 扫码登录阶段 NapCat 会把登录二维码写到这里
CONSOLE_LOG = Path("napcat_console.log")
CONFIG_JSON = Path("config.json")

DEFAULTS = {
    "enabled": False,
    "check_interval": 30,        # \u68c0\u6d4b\u95f4\u9694\uff08\u79d2\uff09
    "offline_confirm": 3,        # \u8fde\u7eed N \u6b21\u786e\u8ba4\u624d\u5224\u6389\u7ebf
    "qq": "",                    # \u8981\u4fdd\u6d3b\u7684 QQ \u53f7
    "password": "",              # L3 密码（本地保存）
    "quick_account": "",         # 快速登录 QQ 号（留空则用 qq）
    "quick_password": "",        # 快速登录回退密码（可选，票据失效时用）
    "accounts": [],              # 多账号档案 [{qq, password}]
    "l1_enabled": True, "l1_retries": 2,   # \u91cd\u8fde + get_status
    "l2_enabled": True, "l2_retries": 1,   # WebUI \u89e6\u53d1\u5feb\u901f\u767b\u5f55
    "l3_enabled": True, "l3_retries": 1,   # 密码 / 快速登录
    "l4_enabled": True, "l4_retries": 1,   # 重启 NapCat 进程（带 -q）
    "l5_enabled": True,                    # \u626b\u7801\u515c\u5e95\u544a\u8b66
    "cooldown": 120,             # \u7ea7\u95f4\u51b7\u5374\uff08\u79d2\uff09
    "max_per_hour": 6,           # \u6bcf\u5c0f\u65f6\u91cd\u767b\u4e0a\u9650
    "notify_private": True,      # \u79c1\u804a\u901a\u77e5\u81ea\u5df1
}

_state = {
    "stage": 0, "fail": 0, "offline_hits": 0, "online": None,
    "last_action": "", "last_action_at": 0.0, "hour_hits": [],
    "last_error": "", "recover_count": 0, "log": [], "quick_list": [], "login": {},
    "captcha": {},
    "verify_busy_at": 0.0,       # 前端验证弹窗还开着时的心跳：这段时间保活不重启 NapCat
}
_logger = None
_restart_fn = None
_stop_fn = None
_suspended = False     # SnowLuma（空协议）模式下整体挂起保活循环


def set_suspended(flag):
    """挂起/恢复保活：协议端切到 SnowLuma 时挂起，切回 NapCat 时恢复。"""
    global _suspended
    _suspended = bool(flag)


def is_suspended() -> bool:
    return bool(_suspended)


def set_logger(fn):
    global _logger
    _logger = fn


def set_process_hooks(restart_fn, stop_fn=None):
    """\u7531 app.py \u6ce8\u5165\u542f\u505c\u80fd\u529b\uff0c\u907f\u514d\u53cc\u91cd\u7ba1\u7406\u8fdb\u7a0b\u3002"""
    global _restart_fn, _stop_fn
    _restart_fn, _stop_fn = restart_fn, stop_fn


def _log(level, msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    _state["log"].append("%s %s" % (level, line))
    del _state["log"][:-120]
    try:
        print("[keepalive] %s %s" % (level, msg), flush=True)
    except Exception:
        pass
    if _logger:
        try:
            _logger(level, "[NapCat\u4fdd\u6d3b] " + msg)
        except Exception:
            pass


# ---------------- \u914d\u7f6e ----------------
def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        d = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            if isinstance(d.get("keepalive"), dict):
                for k, v in d["keepalive"].items():
                    if k in DEFAULTS:
                        cfg[k] = v
            # 顶层 napcat_cmd（重启 NapCat 要用，不在 keepalive 段里）
            _cmd = str(d.get("napcat_cmd") or "").strip()
            if _cmd:
                cfg["napcat_cmd"] = _cmd
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
    cur = d.get("keepalive") if isinstance(d.get("keepalive"), dict) else {}
    cur.update({k: v for k, v in (part or {}).items() if k in DEFAULTS})
    d["keepalive"] = cur
    tmp = str(CONFIG_JSON) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_JSON)
    return cur


# ---------------- NapCat \u63a5\u53e3 ----------------
def write_live_webui(url):
    """记下 NapCat 实际在用的 WebUI 地址（端口可能因为被占用而漂移）。"""
    try:
        u = urllib.parse.urlparse(str(url))
        _p = int(u.port or 0)
        if not _p:
            return
        LIVE_JSON.write_text(json.dumps(
            {"host": u.hostname or "127.0.0.1", "port": _p, "at": time.time()},
            ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clear_live_webui():
    """NapCat 全停之后清掉运行时端口，免得下次连到一个已经不存在的端口。"""
    try:
        LIVE_JSON.unlink()
    except Exception:
        pass


def mark_verify_busy():
    """前端验证/扫码弹窗还开着 -> 记一笔时间，保活这段时间不重启 NapCat。"""
    _state["verify_busy_at"] = time.time()
    return {"status": "ok", "at": _state["verify_busy_at"]}


def webui_info():
    """NapCat WebUI 的 host / port / token。

    端口优先用运行时抓到的实际端口（LIVE_JSON，app.py 从 NapCat 启动输出里抓），
    再退回 napcat/config/webui.json 里配的端口。NapCat 端口被占时会自己往后漂
    （6099 -> 6100），不跟随的话 QQX_AIBot 会连到另一个残留实例上。
    """
    host, port, tok = "127.0.0.1", 6099, ""
    try:
        d = json.loads(WEBUI_JSON.read_text(encoding="utf-8"))
        host = str(d.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        port = int(d.get("port") or 6099)
        tok = str(d.get("token") or "")
    except Exception:
        pass
    try:
        live = json.loads(LIVE_JSON.read_text(encoding="utf-8"))
        _lp = int(live.get("port") or 0)
        if _lp and _lp != port and time.time() - float(live.get("at") or 0) < 3600:
            port = _lp
    except Exception:
        pass
    return host, port, tok


def _post(url, body, timeout=12, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


_cred_cache = {"host": "", "port": 0, "cred": "", "at": 0.0, "fail_at": 0.0}
_CRED_TTL = 900        # Credential 缓存 15 分钟：NapCat 默认 loginRate=10/分钟，缓存久一点才不会把登录次数打满
_LOGIN_BACKOFF = 90     # 登录失败后 90 秒内不再重试（NapCat 限流恢复窗口）


def webui_login(force=False):
    """拿 WebUI Credential（带缓存 + 失败退避，避免撞 NapCat 的 loginRate 限流）"""
    import hashlib
    host, port, tok = webui_info()
    _now = time.time()
    if not force and _cred_cache["fail_at"] and _now - _cred_cache["fail_at"] < _LOGIN_BACKOFF:
        return host, port, ""      # 刚失败过，先退避，别把 NapCat 的登录次数打满
    if (not force and _cred_cache["cred"] and _cred_cache["host"] == host
            and _cred_cache["port"] == port and _now - _cred_cache["at"] < _CRED_TTL):
        return host, port, _cred_cache["cred"]
    cands = []
    try:
        c = json.loads(CONFIG_JSON.read_text(encoding="utf-8")).get("napcat_webui_token")
        if c:
            cands.append(str(c).strip())
    except Exception:
        pass
    if tok:
        cands.append(tok.strip())
    _fail_why = ""
    for t in cands:
        if not t:
            continue
        for h in (hashlib.sha256((t + ".napcat").encode()).hexdigest(), t):
            try:
                r = _post("http://%s:%d/api/auth/login" % (host, port), {"hash": h, "totpCode": ""})
                cred = ((r or {}).get("data") or {}).get("Credential")
                if cred:
                    _cred_cache.update({"host": host, "port": port, "cred": cred,
                                        "at": _now, "fail_at": 0.0})
                    return host, port, cred
                _fail_why = str(r)[:120] or "返回里没有 Credential"
            except Exception as e:
                _fail_why = "%s: %s" % (type(e).__name__, e)
                continue
    _cred_cache["fail_at"] = _now
    _state["last_error"] = "WebUI 登录失败：" + (_fail_why or "没有可用 token")
    return host, port, ""



def webui_api(path, body, timeout=10):
    """调 NapCat WebUI 接口。拿不到或失效时清缓存强制重登一次。"""
    for attempt in (0, 1):
        host, port, cred = webui_login(force=(attempt == 1))
        if not cred:
            if attempt == 0:
                _cred_cache["cred"] = ""
                continue
            _state["last_error"] = "WebUI 未取得 Credential（token 不对或登录被限流）"
            return None
        try:
            return _post("http://%s:%d%s" % (host, port, path), body, timeout,
                         {"Authorization": "Bearer " + cred})
        except Exception as e:
            _state["last_error"] = "%s: %s" % (type(e).__name__, e)
            if attempt == 0:
                _cred_cache["cred"] = ""      # 可能 Credential 过期，清掉重试一次
                continue
            return None
    return None



def onebot_http_port():
    """从协议端配置里找 HTTP server 端口与 token（SnowLuma: onebot_<uin>.json / NapCat: onebot11_*.json）"""
    # SnowLuma 的字段是 accessToken、且没有 enable 开关（跟 NapCat 的 onebot11_*.json 不同），
    # 统一交给 snowluma_token 处理；它读不到时再走下面的 NapCat 逻辑
    try:
        import snowluma_token as _slt
        _d = _slt.tokens()
        if _d.get("http_token"):
            return (int(_d.get("http_port") or 3000), str(_d.get("http_token")))
    except Exception:
        pass
    best = None
    try:
        for f in sorted((NAP / "napcat" / "config").glob("onebot11_*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            for s in (d.get("httpServers") or []):
                if s.get("enable"):
                    best = (int(s.get("port") or 3000), str(s.get("token") or ""))
            for s in (d.get("httpSseServers") or []):
                if s.get("enable") and not best:
                    best = (int(s.get("port") or 3000), str(s.get("token") or ""))
    except Exception:
        pass
    return best or (3000, "")


def ob_call(action, params=None, timeout=8):
    port, tok = onebot_http_port()
    body = {"action": action, "params": params or {}}
    if tok:
        body["access_token"] = tok
    try:
        return _post("http://127.0.0.1:%d/%s" % (port, action), body, timeout)
    except Exception as e:
        _state["last_error"] = "OneBot HTTP %s: %s" % (action, e)
        return None


def onebot_ws_port():
    """从 config.json 的 napcat_ws_url 取 OneBot WS 端口"""
    try:
        _c = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        _key = "snowluma_ws_url" if str(_c.get("qq_protocol") or "").strip().lower() == "snowluma" else "napcat_ws_url"
        u = str(_c.get(_key) or "")
        m = re.search(r":(\d+)", u)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 3001


def port_listening(port):
    """端口是否在监听（比 HTTP 探测可靠，不依赖对方开了 HTTP server）"""
    try:
        out = subprocess.run("netstat -ano", shell=True, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20).stdout or ""
        return bool(re.search(r":%d\s[^\n]*LISTENING" % int(port), out, re.IGNORECASE))
    except Exception:
        return False


def napcat_alive():
    """NapCat \u8fdb\u7a0b\u662f\u5426\u5b58\u5728"""
    try:
        out = subprocess.run('tasklist /FI "IMAGENAME eq node.exe" /FO CSV',
                             shell=True, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15).stdout or ""
        if "node.exe" in out.lower():
            return True
        out = subprocess.run('tasklist /FI "IMAGENAME eq NapCatWinBootMain.exe" /FO CSV',
                             shell=True, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15).stdout or ""
        return "napcatwinbootmain" in out.lower()
    except Exception:
        return True


def napcat_procs():
    """当前在跑的 NapCat node 进程 [{pid, cmd}]（清残留用，避免多实例抢端口）。"""
    out = []
    try:
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' } | "
              "ForEach-Object { $_.ProcessId.ToString() + '|' + $_.CommandLine }")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=25)
        for ln in (r.stdout or "").splitlines():
            ln = ln.strip()
            if "|" not in ln:
                continue
            _pid, _cmd = ln.split("|", 1)
            if "napcat" in _cmd.lower():
                out.append({"pid": int(_pid), "cmd": _cmd})
    except Exception:
        pass
    return out


def kill_napcat_procs(keep=()):
    """杀掉残留的 NapCat 进程（taskkill /T 连子进程一起），返回被杀的 pid 列表。

    keep 里的 pid 不动（比如当前 QQX_AIBot 正在用的那个实例）。
    """
    killed, failed = [], []
    for p in napcat_procs():
        if p["pid"] in keep:
            continue
        try:
            r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(p["pid"])],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=20)
            if r.returncode == 0:
                killed.append(p["pid"])
            else:
                failed.append(p["pid"])
        except Exception:
            failed.append(p["pid"])
    if killed:
        clear_live_webui()
    if failed:
        _log("WARN", "清理 NapCat 进程失败：%s" % failed)
    return killed


OFFLINE_WORDS = ("[KickedOffLine]", "账号状态变更为离线",
                 "已离线", "offline", "登录失败", "会话失效",
                 "登录态已失效", "快速登录错误")


def log_offline_hit():
    """\u4ece\u63a7\u5236\u53f0\u65e5\u5fd7\u5c3e\u90e8\u627e\u6389\u7ebf\u5173\u952e\u5b57"""
    try:
        if not CONSOLE_LOG.exists():
            return False
        with open(CONSOLE_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 20000))
            tail = f.read().decode("utf-8", "replace")
        lines = tail.strip().split("\n")[-40:]
        for ln in lines:
            low = ln.lower()
            if any(w.lower() in low for w in OFFLINE_WORDS):
                return True
    except Exception:
        pass
    return False


VERIFY_WORDS = ("验证码", "验证", "扫码", "二维码", "新设备", "needNewDevice")


def _verify_words(txt):
    """NapCat 的说法里是否表示「正在等人工验证」。"""
    t = str(txt or "")
    return any(w in t for w in VERIFY_WORDS)


def _manual_verify_pending():
    """是否正在等用户做完人工验证（短信验证码 / 刚生成的登录二维码）。

    这时候 NapCat 本来就处于"未登录"，不能当掉线去重启，否则会把用户
    正在做的验证 / 扫码打断。
    """
    cap = _state.get("captcha") or {}
    if (cap.get("sid") or cap.get("url")) and time.time() - float(cap.get("at") or 0) < 900:
        return True
    if time.time() - float(_state.get("verify_busy_at") or 0) < 120:
        return True
    q = login_qr()
    if q.get("ok") and float(q.get("age") or 999999) < 300:
        return True
    return False


def check_online():
    """在线判定：OneBot HTTP（若开了）-> 进程 + OneBot 端口监听 -> WebUI 登录态 -> 日志关键字"""
    st = ob_call("get_status")
    if isinstance(st, dict):
        if st.get("retcode") not in (0, None):
            return False
        if st.get("data"):
            return True
    li = ob_call("get_login_info")
    if isinstance(li, dict):
        d = li.get("data") or {}
        if d.get("user_id"):
            return True
        if li.get("retcode") not in (0, None):
            return False
    if not napcat_alive():
        return False
    if port_listening(onebot_ws_port()):
        return True
    # 进程活着 != 账号在线：没配 OneBot HTTP、WS 端口也没起来时，
    # 问一下 NapCat WebUI 账号到底登没登（不补这一步，掉线会一直判"无法判定"，L1~L5 永远不跑）
    ls = login_status()
    if ls:
        if ls.get("is_login"):
            return True
        if _manual_verify_pending() or _verify_words(ls.get("error")):
            return None   # 等人工验证/扫码：不算掉线，不重启 NapCat（重启会把用户正在做的验证打断，还会反复撞风控）
    if log_offline_hit():
        return False
    return None   # 无法判定



def notify_private(text):
    cfg = load_cfg()
    if not cfg.get("notify_private"):
        return
    qq = str(cfg.get("qq") or "").strip()
    if not qq:
        return
    ob_call("send_private_msg", {"user_id": int(qq), "message": text})


# ---------------- 快速登录（免扫码） ----------------
# NapCat 的免扫码机制：启动时带 -q <QQ号>，napcat.mjs 会拿本地登录票据直接
# quickLoginWithUin；票据失效时再用 NAPCAT_QUICK_PASSWORD(_MD5) 兜底，最后才二维码。
DEFAULT_NAP_BAT = r"C:\ai\NapCat.Shell.Windows.Node\napcat.bat"


def quick_account(cfg=None):
    """快速登录用的 QQ 号：quick_account 优先，其次 qq。"""
    cfg = cfg or load_cfg()
    q = str(cfg.get("quick_account") or cfg.get("qq") or "").strip()
    return q if re.fullmatch(r"\d{5,12}", q) else ""


def quick_env(cfg=None):
    """NapCat 快速登录环境变量（NapCat 会优先读 NAPCAT_QUICK_ACCOUNT）。"""
    cfg = cfg or load_cfg()
    env = {}
    q = quick_account(cfg)
    if q:
        env["NAPCAT_QUICK_ACCOUNT"] = q
    _state["login"] = login_status()
    pwd = str(cfg.get("quick_password") or cfg.get("password") or "").strip()
    if pwd:
        env["NAPCAT_QUICK_PASSWORD"] = pwd
    return env


def launch_cmd(cfg=None):
    """带 -q 快速登录参数的 NapCat 启动命令。

    napcat.bat 的内容是 `node.exe ./index.js`、不转发参数，所以指向 .bat 时
    直接拼 node.exe + index.js，保证 -q 能落到 napcat.mjs 的 process.argv 上。
    """
    cfg = cfg or load_cfg()
    cmd = str(cfg.get("napcat_cmd") or "").strip() or DEFAULT_NAP_BAT
    q = quick_account(cfg)
    if not q:
        return cmd
    if cmd.lower().endswith(".bat"):
        node = NAP / "node.exe"
        js = NAP / "index.js"
        if node.exists() and js.exists():
            return '"%s" "%s" -q %s' % (node, js, q)
    return cmd + " -q " + q


def quick_login_list():
    """本地有快速登录票据的 QQ 列表（拿不到返回 []）。"""
    out = []
    # 优先用带昵称的新接口，拿不到再退回只给 uin 的旧接口
    r = webui_api("/api/QQLogin/GetQuickLoginListNew", {})
    if isinstance(r, dict) and r.get("code") == 0 and isinstance(r.get("data"), list):
        for it in r["data"]:
            if isinstance(it, dict) and it.get("uin"):
                out.append({"qq": str(it["uin"]), "nick": str(it.get("nickName") or ""), "quick": True})
    if not out:
        r = webui_api("/api/QQLogin/GetQuickLoginList", {})
        if isinstance(r, dict) and r.get("code") == 0 and isinstance(r.get("data"), list):
            out = [{"qq": str(x), "nick": "", "quick": True} for x in r["data"] if x]
    return out


def login_qr():
    """NapCat 扫码登录阶段会把二维码写到 cache/qrcode.png；这里只回元信息，不回图片字节。"""
    try:
        if not QR_PNG.exists():
            return {"ok": False, "mtime": 0.0, "age": 0.0, "size": 0}
        st = QR_PNG.stat()
        return {"ok": True, "mtime": float(st.st_mtime),
                "age": max(0.0, time.time() - float(st.st_mtime)),
                "size": int(st.st_size)}
    except Exception as e:
        _state["last_error"] = "读登录二维码失败：%s" % e
        return {"ok": False, "mtime": 0.0, "age": 0.0, "size": 0}


def read_login_qr():
    """读登录二维码 PNG 字节；读不到返回 (None, 0.0)。"""
    try:
        if not QR_PNG.exists():
            return None, 0.0
        return QR_PNG.read_bytes(), float(QR_PNG.stat().st_mtime)
    except Exception:
        return None, 0.0


def refresh_qr():
    """让 NapCat 重新生成登录二维码，并返回新码的元信息。

    手动点「L5 扫码」时，cache/qrcode.png 往往还是上一次登录留下的旧码
    （QQ 登录码大约 2 分钟就失效），不刷新就会扫到过期码。
    """
    url = ""
    try:
        r = webui_api("/api/QQLogin/RefreshQrcode", {})
        if isinstance(r, dict) and r.get("code") == 0:
            url = str((r.get("data") or {}).get("qrcodeurl") or "")
    except Exception as e:
        _state["last_error"] = "刷新登录二维码失败：%s" % e
    # 等 NapCat 把新的 qrcode.png 写盘
    time.sleep(0.8)
    info = login_qr()
    return {"ok": bool(url) and bool(info.get("ok")), "qrcodeurl": url,
            "mtime": info.get("mtime", 0.0), "age": info.get("age", 0.0),
            "size": info.get("size", 0)}


def new_device_qr(uin="", jump_url=""):
    """拿「新设备验证」二维码（NapCat 的 GetNewDeviceQrcode）。

    NapCat 在密码/验证码登录后若返回 needNewDevice + jumpUrl，说明腾讯还要求
    新设备扫码验证；这一步没做完就不会真正登录。uin / jumpUrl 缺省时用最近一次
    L3 / 验证码登录缓存下来的那份。
    """
    cfg = load_cfg()
    nd = _state.get("new_device") or {}
    _uin = str(uin or nd.get("uin") or "").strip() or quick_account(cfg)
    _ju = str(jump_url or nd.get("jump_url") or "").strip()
    if not _uin or not _ju:
        return {"ok": False, "message": "缺少 uin 或 jumpUrl：先在保活卡片点一次「L3 密码登录」拿到新设备验证链接"}
    try:
        r = webui_api("/api/QQLogin/GetNewDeviceQrcode", {"uin": _uin, "jumpUrl": _ju})
    except Exception as e:
        return {"ok": False, "message": "取新设备验证二维码失败：%s" % e}
    if isinstance(r, dict) and r.get("code") == 0:
        d = r.get("data") or {}
        _st = str(d.get("ActionStatus") or "").upper() if isinstance(d, dict) else ""
        # 字段名是 str_url（实测：{"code":0,"data":{"uint32_guarantee_status":0,
        # "str_url":"https://accounts.qq.com/safe/scanresult?str_url=/..."}}）。
        # 它不是图片地址，而是要渲染成二维码的字符串，扫码后打开的是新设备确认页。
        url = ""
        if isinstance(d, dict):
            url = str(d.get("qrcodeurl") or d.get("str_url") or d.get("url") or "")
        else:
            url = str(d or "")
        # NapCat 即使 code=0 也可能带 ActionStatus=FAIL（例如 sig/uin-token 已过期），
        # 这时候 data 里压根没有二维码，不能当成成功。
        if _st == "FAIL" or not url:   # ActionStatus 为空或 OK 都算通过，只看有没有拿到码
            _ec = d.get("ErrorCode") if isinstance(d, dict) else ""
            _ei = str(d.get("ErrorInfo") or "").strip() if isinstance(d, dict) else ""
            msg = ("NapCat 没返回新设备验证二维码（ActionStatus=%s ErrorCode=%s %s）——"
                   "多半是这条验证链接已经过期，重新点一次「L3 密码登录」拿条新的再试"
                   % (_st or "-", _ec or "-", _ei))
            _log("WARN", msg)
            _log("WARN", "[调试] GetNewDeviceQrcode 原始返回：%s" % json.dumps(r, ensure_ascii=False)[:300])
            return {"ok": False, "message": msg, "uin": _uin, "jump_url": _ju,
                    "raw": json.dumps(r, ensure_ascii=False)[:400]}
        _state["new_device"] = {"uin": _uin, "jump_url": _ju, "qrcodeurl": url, "at": time.time()}
        _log("INFO", "已取到新设备验证二维码")
        return {"ok": True, "qrcodeurl": url, "uin": _uin, "jump_url": _ju}
    msg = str((r or {}).get("message") or r)[:200] if isinstance(r, dict) else str(r)[:200]
    _log("WARN", "取新设备验证二维码失败：%s" % msg)
    return {"ok": False, "message": msg, "uin": _uin, "jump_url": _ju}


def login_status():
    """NapCat 当前登录状态（是否已登录 / 登录阶段 / 二维码 / 错误）。"""
    r = webui_api("/api/QQLogin/CheckLoginStatus", {})
    if isinstance(r, dict) and isinstance(r.get("data"), dict):
        d = r["data"]
        return {"is_login": bool(d.get("isLogin")), "offline": bool(d.get("isOffline")),
                "phase": str(d.get("loginPhase") or ""), "core_ready": bool(d.get("coreReady")),
                "qrcode": str(d.get("qrcodeurl") or ""), "error": str(d.get("loginError") or "")}
    return {}



# ---------------- \u91cd\u767b\u52a8\u4f5c ----------------
def act_l1():
    ob_call("get_status")
    ob_call("get_login_info")
    return check_online() is True


def act_l2():
    """WebUI 快速登录：先问本地有哪些可快速登录的号，再逐个触发。"""
    cfg = load_cfg()
    qq = quick_account(cfg)
    lst = quick_login_list()
    if lst:
        _state["quick_list"] = lst
        _log("INFO", "本地可快速登录：%s" % "、".join(
            "%s%s" % (x["qq"], ("(%s)" % x["nick"]) if x["nick"] else "") for x in lst[:5]))
    uins = [x["qq"] for x in lst if x.get("quick")] or [x["qq"] for x in lst]
    if qq:
        if lst and qq not in [x["qq"] for x in lst]:
            _log("WARN", "配置的快速登录号 %s 本地没有票据，快速登录会失败"
                         "（换成有票据的号，或填「快速登录回退密码」）" % qq)
        uins = [qq] + [u for u in uins if u != qq]
    for uin in uins:
        r = webui_api("/api/QQLogin/SetQuickLogin", {"uin": uin})
        if isinstance(r, dict) and r.get("code") == 0:
            _log("INFO", "WebUI 快速登录已触发：%s" % uin)
            return True

    return False



def password_md5(pwd):
    """NapCat 的 PasswordLogin / CaptchaLogin 要的是密码的 MD5（32 位小写 hex），不是明文。"""
    import hashlib
    return hashlib.md5(str(pwd or "").encode("utf-8")).hexdigest()


def _cap_sid(sid="", url=""):
    """解析验证码会话 sid：显式传入 > 验证链接里的 > 之前 L3 存的。"""
    sid = str(sid or "").strip()
    if sid:
        return sid
    if url:
        got = _sid_of(url)
        if got:
            return got
    return str((_state.get("captcha") or {}).get("sid") or "")


def captcha_login(ticket, randstr, sid="", uin="", url=""):
    """验证码第二步：把腾讯 TCaptcha 回调拿到的 ticket / randstr 交给 NapCat 完成登录。

    sid / uin / url 由前端从弹窗里那条验证链接解出来一起传：验证码可能是 NapCat
    自己密码回退时弹的（QQX_AIBot 没调过 L3），只靠 _state 会误判成"没有拿到 sid"。
    """
    cfg = load_cfg()
    qq = (str(uin or "").strip()
          or str((_state.get("captcha") or {}).get("uin") or "").strip()
          or quick_account(cfg))
    pwd = str(cfg.get("quick_password") or cfg.get("password") or "").strip()
    _sid = _cap_sid(sid, url)
    if not qq:
        return {"ok": False, "message": "没配快速登录 QQ 号"}
    if not pwd:
        return {"ok": False, "message": "没配 QQ 密码，无法完成验证码登录（在保活卡片里填）"}
    if not ticket or not randstr:
        return {"ok": False, "message": "ticket / randstr 为空"}
    if not _sid:
        return {"ok": False, "message": "没有拿到 sid：验证链接里没带 sid，也没触发过密码登录（L3）"}
    _state["captcha"] = {"uin": qq,
                         "url": str(url or (_state.get("captcha") or {}).get("url") or ""),
                         "sid": _sid, "at": time.time()}
    _log("INFO", "正在提交验证码登录：uin=%s" % qq)
    r = webui_api("/api/QQLogin/CaptchaLogin", {
        "uin": qq, "passwordMd5": password_md5(pwd),
        "ticket": str(ticket), "randstr": str(randstr), "sid": _sid})
    if isinstance(r, dict) and r.get("code") == 0 and not (r.get("data") or {}).get("needNewDevice"):
        _log("SUCCESS", "验证码登录成功：%s" % qq)
        _state["captcha"] = {}
        return {"ok": True, "message": "验证码登录成功"}
    d = (r or {}).get("data") or {}
    if d.get("needNewDevice"):
        _ju = str(d.get("jumpUrl") or "")
        _state["new_device"] = {"uin": qq, "jump_url": _ju, "at": time.time()}
        msg = "验证通过，但腾讯还要求新设备扫码验证：%s" % _ju
        _log("WARN", msg)
        return {"ok": False, "message": msg, "need_new_device": True, "jump_url": d.get("jumpUrl") or ""}
    msg = "验证码登录失败：%s" % str((r or {}).get("message") or r)[:200]
    _log("ERROR", msg)
    return {"ok": False, "message": msg}



def act_l3():
    """密码 / 快速登录：写 autoLoginAccount + 试 WebUI 密码登录，失败回退快速登录。"""
    cfg = load_cfg()
    qq = quick_account(cfg)
    if not qq:
        _log("WARN", "L3：没配快速登录 QQ 号，跳过（在保活卡片里填一个）")
        return False
    # 让 NapCat 下次启动自动快速登录该账号
    try:
        d = json.loads(WEBUI_JSON.read_text(encoding="utf-8"))
        if str(d.get("autoLoginAccount") or "") != qq:
            d["autoLoginAccount"] = qq
            WEBUI_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            _log("INFO", "已把 NapCat autoLoginAccount 设为 %s" % qq)
    except Exception as e:
        _log("WARN", "写 autoLoginAccount 失败：%s" % e)
    pwd = str(cfg.get("quick_password") or cfg.get("password") or "").strip()
    if pwd:
        # NapCat 要 passwordMd5，不是明文密码
        r = webui_api("/api/QQLogin/PasswordLogin",
                      {"uin": qq, "passwordMd5": password_md5(pwd)})
        d = (r or {}).get("data") if isinstance(r, dict) else None
        if isinstance(r, dict) and r.get("code") == 0 and not d:
            _log("SUCCESS", "WebUI 密码登录成功：%s" % qq)
            _state["captcha"] = {}
            return True
        if isinstance(d, dict) and d.get("needCaptcha") and d.get("proofWaterUrl"):
            url = str(d["proofWaterUrl"])
            _q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

            def _qp(k):
                return str((_q.get(k) or [""])[0])

            # 把验证链接里的参数整套存下来：前端拉起腾讯验证组件要用 aid / login_appid /
            # uin，光靠前端从日志里解析容易抓错链接（新设备验证那条 jumpUrl 也长得很像）
            _state["captcha"] = {"uin": qq, "url": url, "sid": _sid_of(url),
                                 "aid": _qp("aid"), "login_appid": _qp("login_appid"),
                                 "at": time.time()}
            _log("INFO", "[调试] 验证会话参数：aid=%s login_appid=%s sid=%s uin=%s"
                 % (_qp("aid") or "-", _qp("login_appid") or "-", _sid_of(url) or "-", qq))
            _log("WARN", "密码登录需要验证码，已保存验证链接（在控制台弹窗里点「立即验证」）")
            # 完整链接写进日志：验证弹窗的参数（aid / sid / uin）都从它解析，
            # 出问题时需要拿原始链接核对，日志里只留一句「已保存」会没法排查
            _log("WARN", "[调试] proofWaterUrl 完整内容：%s" % url)
            _log("WARN", "[调试] 从链接解析出的参数：sid=%r aid=%r uin=%r"
                 % (_sid_of(url),
                    (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("aid") or [""])[0],
                    (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uin") or [""])[0]))
            return False
        if isinstance(d, dict) and d.get("needNewDevice"):
            _ju = str(d.get("jumpUrl") or "")
            # 存下来：后续要用 uin + jumpUrl 去 NapCat 换新设备验证二维码
            _state["new_device"] = {"uin": qq, "jump_url": _ju, "at": time.time()}
            _log("WARN", "密码登录需要新设备验证：%s" % _ju)
            _log("WARN", "[NapCat保活] 需要新设备验证——在控制台弹窗里点「获取验证二维码」扫码完成")
            return False
        _log("INFO", "密码登录未成功（%s），改用快速登录路径"
                     % str((r or {}).get("message") or "接口未响应")[:120])
    else:
        _log("INFO", "未配置回退密码，直接走快速登录（想用密码登录就在卡片里填 QQ 密码）")
    return act_l2()


def _sid_of(url):
    """从 proofWaterUrl 里取 sid（腾讯验证要用）。"""
    try:
        q = urllib.parse.urlparse(str(url)).query
        return urllib.parse.parse_qs(q).get("sid", [""])[0]
    except Exception:
        m = re.search(r"[?&]sid=([^&#]+)", str(url))
        return m.group(1) if m else ""



def refresh_captcha():
    """现取一条新鲜的验证会话。

    腾讯的验证会话（sid）有时效，拿旧的链接去验证会回
    「140022012 你太久没有操作，请重新登录」。所以点「立即验证」之前
    先重新触发一次密码登录，换一条全新的 proofWaterUrl 再用。
    """
    cfg = load_cfg()
    qq = quick_account(cfg)
    pwd = str(cfg.get("quick_password") or cfg.get("password") or "").strip()
    if not qq:
        return {"ok": False, "message": "没配快速登录 QQ 号，先在保活卡片里填一个"}
    if not pwd:
        return {"ok": False, "message": "没配回退密码，没法刷新验证会话"}
    r = webui_api("/api/QQLogin/PasswordLogin", {"uin": qq, "passwordMd5": password_md5(pwd)})
    d = (r or {}).get("data") if isinstance(r, dict) else None
    if isinstance(r, dict) and r.get("code") == 0 and not d:
        _state["captcha"] = {}
        _log("SUCCESS", "刷新验证会话时密码登录直接通过了：%s" % qq)
        return {"ok": True, "need_captcha": False, "message": "这次不需要验证，登录已直接通过"}
    if isinstance(d, dict) and d.get("needCaptcha") and d.get("proofWaterUrl"):
        url = str(d["proofWaterUrl"])
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

        def _qp(k):
            return str((q.get(k) or [""])[0])

        _state["captcha"] = {"uin": qq, "url": url, "sid": _sid_of(url),
                             "aid": _qp("aid"), "login_appid": _qp("login_appid"),
                             "at": time.time()}
        _log("INFO", "[调试] 已现取验证会话：aid=%s login_appid=%s sid=%s uin=%s"
             % (_qp("aid") or "-", _qp("login_appid") or "-", _sid_of(url) or "-", qq))
        return {"ok": True, "need_captcha": True, "url": url, "sid": _sid_of(url),
                "aid": _qp("aid"), "login_appid": _qp("login_appid"), "uin": qq}
    msg = "刷新验证会话失败：%s" % str((r or {}).get("message") or "接口未响应")[:120]
    _log("WARN", msg)
    return {"ok": False, "message": msg}


def act_l4():
    """重启 NapCat 进程：带 -q 快速登录参数，重启后免扫码。"""
    cfg = load_cfg()
    cmd = launch_cmd(cfg)
    env = dict(os.environ)
    env.update(quick_env(cfg))
    q = quick_account(cfg)
    _log("INFO", "重启 NapCat：%s%s" % (cmd, "（已带快速登录）" if q else "（未配快速登录号）"))
    _killed = kill_napcat_procs()
    if _killed:
        _log("INFO", "重启前清理残留 NapCat 进程：%s" % _killed)
        time.sleep(1.5)
    if _restart_fn:
        try:
            _restart_fn()
            return True
        except Exception as e:
            _log("WARN", "重启回调失败：%s" % e)
    try:
        subprocess.Popen(cmd, shell=True, cwd=str(NAP), env=env)
        return True
    except Exception as e:
        _log("ERROR", "重启 NapCat 失败：%s" % e)
        return False



def act_l5():
    # 先把登录二维码刷成新的：手动点「L5 扫码」时 cache/qrcode.png 往往是旧码
    try:
        _rq = refresh_qr()
        _log("INFO", "L5：登录二维码已刷新（%s）"
                     % ("新码已生成" if _rq.get("ok") else "刷新失败，沿用现有码"))
    except Exception as _e:
        _log("WARN", "L5：刷新登录二维码失败：%s" % _e)
    notify_private("\u3010NapCat\u4fdd\u6d3b\u3011\u81ea\u52a8\u91cd\u767b\u5931\u8d25\uff0c\u8bf7\u5230 WebUI \u626b\u7801\uff1a"
                   "http://127.0.0.1:6099/webui")
    _log("ERROR", "L5\uff1a\u6240\u6709\u81ea\u52a8\u91cd\u767b\u5931\u8d25\uff0c\u8bf7\u624b\u52a8\u626b\u7801")
    _log("INFO", "提示：在保活卡片填「快速登录 QQ 号」，重启 NapCat 时会带 -q 参数免扫码登录")
    return False


LEVELS = [(1, "l1_enabled", "l1_retries", act_l1, "\u91cd\u8fde+get_status"),
          (2, "l2_enabled", "l2_retries", act_l2, "WebUI \u5feb\u901f\u767b\u5f55"),
          (3, "l3_enabled", "l3_retries", act_l3, "密码/快速登录"),
          (4, "l4_enabled", "l4_retries", act_l4, "重启 NapCat 进程（带 -q）"),
          (5, "l5_enabled", None, act_l5, "\u626b\u7801\u515c\u5e95")]


def _rate_ok(cfg):
    now = time.time()
    _state["hour_hits"] = [t for t in _state["hour_hits"] if now - t < 3600]
    return len(_state["hour_hits"]) < int(cfg.get("max_per_hour") or 6)


def restart_webui() -> bool:
    """重启 NapCat 进程 —— 它的 6099 WebUI 是随进程走的，所以「重启 WebUI」就是重启 NapCat。
    走 act_l4()：带 -q 快速登录参数重启，重启后免扫码。"""
    _log("INFO", "手动重启 NapCat / WebUI（带快速登录）")
    return bool(act_l4())


def act_manual(level: int) -> bool:
    """手动执行某一级保活动作（面板上的按钮用）。level: 1=重连 / 3=密码登录。"""
    try:
        lv = int(level or 0)
    except Exception:
        return False
    if lv == 1:
        _log("INFO", "手动触发 L1 重连 + get_status")
        return bool(act_l1())
    if lv == 3:
        _log("INFO", "手动触发 L3 密码 / 快速登录")
        return bool(act_l3())
    if lv == 5:
        _log("INFO", "手动触发 L5：刷新登录二维码")
        return bool(refresh_qr().get("ok"))
    _log("WARN", "手动保活：不支持的等级 %s" % level)
    return False


def recover(cfg):
    """\u4ece\u5f53\u524d\u9636\u6bb5\u5f80\u4e0a\u9010\u7ea7\u91cd\u767b"""
    if not _rate_ok(cfg):
        _log("WARN", "\u672c\u5c0f\u65f6\u91cd\u767b\u5df2\u8fbe\u4e0a\u9650\uff08%s \u6b21\uff09\uff0c\u6682\u505c\u81ea\u52a8\u91cd\u767b" % cfg.get("max_per_hour"))
        return False
    _state["hour_hits"].append(time.time())
    start = max(1, int(_state["stage"] or 1))
    for lv, en_key, rt_key, fn, name in LEVELS:
        if lv < start:
            continue
        if not cfg.get(en_key, True):
            continue
        tries = int(cfg.get(rt_key) or 1) if rt_key else 1
        for i in range(max(1, tries)):
            _log("INFO", "L%d %s \u7b2c %d \u6b21\u5c1d\u8bd5" % (lv, name, i + 1))
            ok = False
            try:
                ok = bool(fn())
            except Exception as e:
                _state["last_error"] = str(e)
                _log("WARN", "L%d \u5f02\u5e38\uff1a%s" % (lv, e))
            if ok and check_online() is True:
                _state.update({"stage": 1, "fail": 0, "offline_hits": 0, "recover_count": _state["recover_count"] + 1})
                _log("INFO", "L%d \u6210\u529f\uff0c\u5df2\u56de\u5230\u5728\u7ebf" % lv)
                notify_private("\u3010NapCat\u4fdd\u6d3b\u3011\u6389\u7ebf\u540e\u5df2\u7528 L%d\uff08%s\uff09\u81ea\u52a8\u91cd\u767b\u6210\u529f" % (lv, name))
                return True
            if lv > 1:
                time.sleep(min(int(cfg.get("cooldown") or 120), 30))
        _state["stage"] = lv + 1
    _state["stage"] = 1
    _state["fail"] = _state["fail"] + 1
    return False


async def loop():
    """\u4e3b\u5faa\u73af\uff1a\u68c0\u6d4b -> \u786e\u8ba4 -> \u91cd\u767b"""
    _log("INFO", "\u4fdd\u6d3b\u5faa\u73af\u5df2\u542f\u52a8")
    while True:
        try:
            cfg = load_cfg()
            iv = max(10, int(cfg.get("check_interval") or 30))
            if _suspended:
                # 协议端是 SnowLuma：NapCat 保活整体挂起，不做任何检测/重登
                _state["online"] = None
                _state["offline_hits"] = 0
                await asyncio.sleep(iv)
                continue
            if not cfg.get("enabled"):
                _state["online"] = None
                await asyncio.sleep(iv)
                continue
            st = check_online()
            if st is True:
                if _state["online"] is False:
                    _log("INFO", "\u5df2\u6062\u590d\u5728\u7ebf")
                _state.update({"online": True, "offline_hits": 0, "stage": 1})
            elif st is False:
                _state["offline_hits"] += 1
                need = max(1, int(cfg.get("offline_confirm") or 3))
                _state["online"] = False
                if _state["offline_hits"] >= need:
                    _log("WARN", "\u68c0\u6d4b\u5230\u6389\u7ebf\uff08\u8fde\u7eed %d \u6b21\uff09\uff0c\u5f00\u59cb\u91cd\u767b" % _state["offline_hits"])
                    notify_private("\u3010NapCat\u4fdd\u6d3b\u3011\u68c0\u6d4b\u5230 QQ \u6389\u7ebf\uff0c\u6b63\u5728\u5c1d\u8bd5\u81ea\u52a8\u91cd\u767b\u2026")
                    await asyncio.get_event_loop().run_in_executor(None, recover, cfg)
                    _state["offline_hits"] = 0
            else:
                # 无法判定：置为未知，不要把旧的掉线状态一直留着
                _state["online"] = None
                _state["offline_hits"] = 0
            await asyncio.sleep(iv)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _state["last_error"] = str(e)
            _log("WARN", "\u5faa\u73af\u5f02\u5e38\uff1a%s" % e)
            await asyncio.sleep(30)


def status():
    cfg = load_cfg()
    return {
        "config": cfg,
        "suspended": bool(_suspended),
        "online": _state["online"],
        "stage": _state["stage"],
        "fail": _state["fail"],
        "offline_hits": _state["offline_hits"],
        "recover_count": _state["recover_count"],
        "last_error": _state["last_error"],
        "hour_hits": len([t for t in _state["hour_hits"] if time.time() - t < 3600]),
        "log": _state["log"][-60:],
        "webui_token_ok": bool(webui_login()[2]),
        "onebot_port": onebot_http_port()[0],
        "quick_account": quick_account(cfg),
        "quick_password_set": bool(str(cfg.get("quick_password") or cfg.get("password") or "").strip()),
        "quick_list": _state.get("quick_list") or [],
        "captcha": {k: v for k, v in (_state.get("captcha") or {}).items() if k != "at"},
        "launch_cmd": launch_cmd(cfg),
        "login": _state.get("login") or {},
        "qr": login_qr(),
    }


def self_test():
    cfg = load_cfg()
    out = ["enabled=%s interval=%s" % (cfg["enabled"], cfg["check_interval"]),
           "napcat_alive=%s online=%s" % (napcat_alive(), check_online()),
           "webui_token_ok=%s" % bool(webui_login()[2]),
           "onebot_http=%s" % (onebot_http_port(),),
           "quick_account=%s quick_password=%s" % (quick_account(cfg) or "(未配)",
                                                   "已设" if str(cfg.get("quick_password") or cfg.get("password") or "").strip() else "未设"),
           "launch_cmd=%s" % launch_cmd(cfg),
           "login=%s" % (login_status() or "(查询失败)")]
    return out
