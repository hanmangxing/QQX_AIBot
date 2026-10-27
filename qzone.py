# -*- coding: utf-8 -*-
"""QQ空间（说说）对接：登录态、发说说、传图。

登录态不碰账号密码：向协议端（NapCat / SnowLuma 等 OneBot v11 实现）要
user.qzone.qq.com 的 Cookie，解出 uin / skey / p_skey，再用 p_skey 算 g_tk。
只要 WebUI 本身连着协议端，这里就能直接拿到权限，不需要额外配置。

接口形态参考 astrbot_plugin_qzone（MIT，作者 Zhalslar）的实现思路，按本项目风格重写。
"""

import asyncio
import base64
import json
import re
import time
from typing import Any, Dict, List, Optional

import aiohttp

DOMAIN = "user.qzone.qq.com"
BASE_URL = "https://user.qzone.qq.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

PUBLISH_URL = BASE_URL + "/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
UPLOAD_URL = BASE_URL + "/proxy/domain/up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
UPLOAD_URL_ALT = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
LIST_URL = BASE_URL + "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"


def gtk2(p_skey: str) -> str:
    """用 p_skey 算 g_tk（QQ空间各处接口的通用令牌）。"""
    h = 5381
    for ch in str(p_skey or ""):
        h += (h << 5) + ord(ch)
    return str(h & 0x7FFFFFFF)


def _code_of(text: str) -> Optional[int]:
    """宽松取回包里的 code / ret。空间部分接口返回的是松 JSON，标准解析会失败。"""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            for k in ("code", "ret", "subcode"):
                if k in obj:
                    return int(obj[k]) if str(obj[k]).lstrip("-").isdigit() else None
    except Exception:
        pass
    m = re.search(r'["\']?(?:code|ret)["\']?\s*[:=]\s*(-?\d+)', text or "")
    return int(m.group(1)) if m else None


class QzoneClient:
    """一个聊天上下文里复用的空间客户端。"""

    def __init__(self, bot: Any):
        self.bot = bot
        self._uin = ""
        self._skey = ""
        self._p_skey = ""
        self._at = 0.0
        self._lock = asyncio.Lock()

    # ---------------- 登录态 ----------------
    async def _refresh(self) -> bool:
        res = await self.bot.napcat_api("get_cookies", {"domain": DOMAIN}, timeout=20.0)
        ck = str(((res or {}).get("data") or {}).get("cookies") or "")
        if not ck:
            await self.bot.log("WARN", "[空间] 协议端没返回 Cookie，登录态获取失败")
            return False
        jar: Dict[str, str] = {}
        for part in ck.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                jar[k.strip()] = v.strip()
        uin = jar.get("uin", "")
        if uin[:1].lower() == "o":
            uin = uin[1:]
        if not uin.isdigit():
            await self.bot.log("WARN", "[空间] Cookie 里没有合法 uin")
            return False
        self._uin = uin
        self._skey = jar.get("skey", "")
        self._p_skey = jar.get("p_skey") or self._skey
        self._at = time.time()
        return True

    async def ctx(self) -> bool:
        """确保登录态可用（带 TTL，过期自动重取）。"""
        ttl = int(self.bot.config.get("qzone_cookie_ttl", 600) or 600)
        if self._uin and (ttl <= 0 or time.time() - self._at < ttl):
            return True
        async with self._lock:
            if self._uin and ttl > 0 and time.time() - self._at < ttl:
                return True
            return await self._refresh()

    @property
    def uin(self) -> str:
        return self._uin

    def _headers(self) -> Dict[str, str]:
        return {"User-Agent": UA, "referer": "%s/%s" % (BASE_URL, self._uin),
                "origin": BASE_URL, "Connection": "keep-alive"}

    def _cookies(self) -> Dict[str, str]:
        return {"uin": "o" + self._uin, "skey": self._skey, "p_skey": self._p_skey}

    # ---------------- 底层请求 ----------------
    async def _req(self, method: str, url: str, *, params=None, data=None,
                   retry: int = 0) -> Dict[str, Any]:
        if not await self.ctx():
            return {"ok": False, "code": -1, "message": "登录态不可用", "text": ""}
        timeout = float(self.bot.config.get("qzone_timeout", 25) or 25)
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                async with s.request(method, url, params=params, data=data,
                                     headers=self._headers(), cookies=self._cookies()) as r:
                    text = await r.text()
                    status = r.status
        except Exception as e:
            return {"ok": False, "code": -1, "message": "请求失败：%r" % (e,), "text": ""}

        code = _code_of(text)
        # 登录失效：HTTP 401 / code -3000 / 图片 -100
        if status == 401 or code in (-3000, -100):
            if retry < 2:
                await self.bot.log("WARN", "[空间] 登录态失效，重新取 Cookie 再试")
                self._uin = ""
                if await self.ctx():
                    return await self._req(method, url, params=params, data=data, retry=retry + 1)
        ok = (status == 200 and code == 0)
        return {"ok": ok, "code": code, "status": status, "message": "" if ok else text[:400],
                "text": text}

    # ---------------- 业务接口 ----------------
    async def upload_image(self, path: str) -> Optional[Dict[str, str]]:
        """上传一张本地图片，返回 {pic_bo, richval}；失败返回 None。"""
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception as e:
            await self.bot.log("WARN", "[空间] 读图失败 %s：%r" % (path, e))
            return None
        data = {"filename": "filename", "uploadtype": "1", "albumtype": "7",
                "skey": self._skey, "uin": self._uin, "p_skey": self._p_skey,
                "output_type": "json", "base64": "1",
                "picfile": base64.b64encode(raw).decode()}
        r = await self._req("POST", UPLOAD_URL, data=data)
        if not r.get("ok"):
            await self.bot.log("WARN", "[空间] 图片上传失败（代理通道）HTTP %s：%s"
                               % (r.get("status"), str(r.get("message") or "")[:400]))
            await self.bot.log("INFO", "[空间] 改用直连通道重试上传…")
            r = await self._req("POST", UPLOAD_URL_ALT, data=data)
        if not r.get("ok"):
            await self.bot.log("WARN", "[空间] 图片上传失败（直连通道）HTTP %s：%s"
                               % (r.get("status"), str(r.get("message") or "")[:400]))
            return None
        try:
            j = json.loads(r["text"])
            d = j.get("data") or {}
            pic_bo = str(d["url"]).split("&bo=", 1)[1]
            richval = ",{},{},{},{},{},{},,{},{},,".format(
                d["albumid"], d["lloc"], d["sloc"], d["type"],
                d["height"], d["width"], d["height"], d["width"])
            return {"pic_bo": pic_bo, "richval": richval}
        except Exception as e:
            await self.bot.log("WARN", "[空间] 图片回包解析失败：%r" % (e,))
            return None

    async def publish(self, text: str, images: Optional[List[str]] = None) -> Dict[str, Any]:
        """发一条说说。images 是本地文件路径列表，可空。"""
        body = {"syn_tweet_verson": "1", "paramstr": "1", "who": "1", "con": str(text or ""),
                "feedversion": "1", "ver": "1", "ugc_right": "1", "to_sign": "0",
                "hostuin": self._uin, "code_version": "1", "format": "json",
                "qzreferrer": "%s/%s" % (BASE_URL, self._uin)}
        if images:
            bos, richvals = [], []
            for p in images:
                up = await self.upload_image(p)
                if up:
                    bos.append(up["pic_bo"])
                    richvals.append(up["richval"])
            if bos:
                body.update(pic_bo=",".join(bos), richtype="1", richval="\t".join(richvals))
        return await self._req("POST", PUBLISH_URL,
                               params={"g_tk": gtk2(self._p_skey), "uin": self._uin}, data=body)

    async def list_recent(self, page: int = 1) -> Dict[str, Any]:
        """拉自己空间最近的说说（用来验证登录态）。"""
        params = {"uin": self._uin, "scope": 0, "view": 1, "filter": "all", "flag": 1,
                  "applist": "all", "pagenum": page, "begintime": 0, "format": "json",
                  "g_tk": gtk2(self._p_skey), "useutf8": 1, "outputhtmlfeed": 1}
        return await self._req("GET", LIST_URL, params=params)
