# -*- coding: utf-8 -*-
"""秘书网关（群聊模式的「要不要插话」前置判断）

一条**完整的**群消息进来后，按顺序过三道：

1. **复读**：群里最近几条大家都在复读同一句 → 直接跟着发那句（不叫大模型）。
2. **关键词**：这条消息命中了配置的关键词 → 直接交给群聊模式的主对话 API。
3. **秘书网关模型**：把最近 N 条上下文 + 这条消息发给秘书模型，让它给两个分：
   - `score` 想插话的倾向分（0-10），跟配置的 Yes/No 权重比；
   - `mood` 当前群里的气氛和时机（0-10）。
   两个分都过线 → Yes，交给主对话 API；否则 No → 沉默，一个字都不发。

权重口径：阈值 = No / (Yes + No)。
  Yes 6 : No 4 → 阈值 0.4（倾向分 ≥ 4 分就插话，爱说话）
  Yes 1 : No 9 → 阈值 0.9（倾向分要 ≥ 9 分才插话，很矜持）
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULTS: Dict[str, Any] = {
    "sec_enabled": False,          # 总开关
    "sec_base_url": "",            # 秘书网关 API（OpenAI 兼容）
    "sec_key": "",
    "sec_model": "",
    "sec_timeout": 60,
    "sec_temperature": 0.3,
    "sec_ctx_count": 10,           # 发给秘书的上下文条数
    "sec_yes_weight": 6,           # Yes 权重
    "sec_no_weight": 4,            # No 权重
    "sec_mood_threshold": 0.5,     # 气氛/时机分要过多少才算合适
    "sec_keywords": [],            # 命中就直接给主对话（不等秘书）
    "sec_reply_to_bot_enabled": True,  # 有人回复了 bot 自己的消息 → 直接放行
    "sec_repeat_enabled": True,    # 复读检测
    "sec_repeat_window": 6,        # 看最近几条
    "sec_repeat_need": 3,          # 至少几条一样才算复读
    "sec_repeat_min_len": 2,       # 太短的不算复读
    "sec_repeat_cooldown": 120,    # 同一句复读后的冷却秒数（0=不冷却）
    "sec_debug": False,
}

PROMPT = """你是群聊里的「秘书」，只干一件事：判断现在该不该插话。

群里最近的消息（从旧到新）：
{ctx}

刚收到的新消息：
{msg}

请给两个 0~10 的整数分：
1. score：想接这句话的倾向分。有意思、有人问你、话题正热 → 高分；只是别人互相闲聊、跟你无关 → 低分。
   本次参考权重 Yes:No = {yes}:{no}，也就是倾向分达到 {need} 分以上才值得插话。
2. mood：现在群里的气氛和时机分。适合插话 → 高分；正在刷屏、吵架、道别、发公告、别人私聊、话题已结束 → 低分。

只输出一行 JSON，不要解释：
{{"score": 0, "mood": 0, "reason": "一句话说明"}}"""


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", str(t or "")).strip()


def detect_repeat(recent: List[Tuple[str, str]], window: int = 6, need: int = 3,
                  min_len: int = 2) -> Optional[str]:
    """recent = [(发送者, 内容), ...]（从旧到新）。大家都在复读时返回那句原文。"""
    if window <= 0 or need <= 1:
        return None
    tail = list(recent)[-int(window):]
    if len(tail) < need:
        return None
    cnt: Dict[str, int] = {}
    raw: Dict[str, str] = {}
    for _who, txt in tail:
        n = _norm(txt)
        if not n or len(n) < int(min_len):
            continue
        cnt[n] = cnt.get(n, 0) + 1
        raw.setdefault(n, str(txt).strip())
    for n, c in sorted(cnt.items(), key=lambda kv: -kv[1]):
        if c >= int(need):
            return raw.get(n) or n
    return None


class Secretary:
    """秘书网关：复读 / 关键词 / 权重+时机判断。"""

    def __init__(self, cfg: Callable[[], Dict[str, Any]], log: Optional[Callable] = None) -> None:
        self.cfg = cfg
        self.log = log
        self.stats: Dict[str, int] = {"checked": 0, "repeat": 0, "keyword": 0,
                                      "yes": 0, "no": 0, "error": 0}
        self.last: Dict[str, Any] = {}
        self._rep_cd: Dict[str, float] = {}   # 复读冷却：key=群+归一化文本, value=上次复读时间

    # ---------------- 配置
    def _c(self) -> Dict[str, Any]:
        c = dict(DEFAULTS)
        try:
            c.update(self.cfg() or {})
        except Exception:
            pass
        return c

    def enabled(self) -> bool:
        return bool(self._c().get("sec_enabled"))

    def _f(self, c: Dict[str, Any], k: str, d: float) -> float:
        try:
            return float(c.get(k, d) or 0)
        except Exception:
            return float(d)

    def _i(self, c: Dict[str, Any], k: str, d: int) -> int:
        try:
            return int(float(c.get(k, d) or 0))
        except Exception:
            return int(d)

    def threshold(self) -> float:
        """Yes/No 权重换算出来的倾向分阈值。"""
        c = self._c()
        yes = max(0.0, self._f(c, "sec_yes_weight", 6))
        no = max(0.0, self._f(c, "sec_no_weight", 4))
        if yes + no <= 0:
            return 0.5
        return no / (yes + no)

    # ---------------- 关键词
    def hit_keyword(self, text: str) -> str:
        t = str(text or "")
        for kw in (self._c().get("sec_keywords") or []):
            k = str(kw or "").strip()
            if k and k in t:
                return k
        return ""

    # ---------------- 问秘书模型
    def _messages(self, text: str, recent: List[Tuple[str, str]]) -> List[Dict[str, str]]:
        c = self._c()
        n = max(0, self._i(c, "sec_ctx_count", 10))
        tail = list(recent)[-n:] if n else []
        ctx = "\n".join("%s：%s" % (w or "某人", str(t or "")[:200]) for w, t in tail) or "（还没有上下文）"
        yes = self._i(c, "sec_yes_weight", 6)
        no = self._i(c, "sec_no_weight", 4)
        need = self.threshold() * 10
        body = PROMPT.format(ctx=ctx, msg=str(text or "")[:600], yes=yes, no=no,
                             need=("%.1f" % need).rstrip("0").rstrip("."))
        return [{"role": "user", "content": body}]

    @staticmethod
    def parse_reply(raw: str) -> Dict[str, Any]:
        s = str(raw or "")
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
        # 兜底：从文本里抠数字
        sc = re.search(r"score[^0-9]{0,10}(\d+(?:\.\d+)?)", s, re.I)
        md = re.search(r"mood[^0-9]{0,10}(\d+(?:\.\d+)?)", s, re.I)
        return {"score": float(sc.group(1)) if sc else 0.0,
                "mood": float(md.group(1)) if md else 0.0,
                "reason": s.strip()[:120]}

    async def ask(self, text: str, recent: List[Tuple[str, str]]) -> Dict[str, Any]:
        """问秘书模型，返回 {score, mood, reason}。"""
        c = self._c()
        base = str(c.get("sec_base_url") or "").rstrip("/")
        model = str(c.get("sec_model") or "").strip()
        if not base or not model:
            raise RuntimeError("秘书网关 API 没配（Base URL / Model 为空）")
        import aiohttp
        payload = {
            "model": model,
            "messages": self._messages(text, recent),
            "temperature": self._f(c, "sec_temperature", 0.3),
            "max_tokens": 200,
        }
        headers = {"Content-Type": "application/json"}
        key = str(c.get("sec_key") or "").strip()
        if key:
            headers["Authorization"] = "Bearer " + key
        timeout = aiohttp.ClientTimeout(total=max(5.0, self._f(c, "sec_timeout", 60)))
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(base + "/chat/completions", json=payload, headers=headers) as r:
                data = await r.json(content_type=None)
        try:
            raw = data["choices"][0]["message"]["content"] or ""
            if not str(raw).strip():
                msg = data["choices"][0]["message"]
                raw = msg.get("reasoning_content") or msg.get("reasoning") or ""
        except Exception:
            raise RuntimeError("秘书网关返回格式不对：%s" % str(data)[:160])
        out = self.parse_reply(raw)
        out["raw"] = str(raw)[:400]
        return out

    # ---------------- 总入口
    async def gate(self, group_id: Any, text: str,
                   recent: List[Tuple[str, str]], mentioned: bool = False,
                   replied_to_bot: bool = False,
                   in_call: bool = False,
                   recent_human: Optional[List[Tuple[str, str]]] = None) -> Dict[str, Any]:
        """决定这条消息怎么办。

        返回 {"action": "repeat"|"direct"|"silence"|"reply"|"skip", ...}
          repeat  = 群里在复读，text 是要发的那句
          direct  = 命中关键词，直接给主对话
          reply   = 秘书说 Yes，给主对话
          silence = 秘书说 No，沉默
          skip    = 网关没开（按老样子走）
        """
        c = self._c()
        if not c.get("sec_enabled"):
            return {"action": "skip"}
        self.stats["checked"] += 1

        # 0) 被艾特：最高优先级，直接放行给主对话（不叫秘书模型）
        if mentioned:
            self.stats["mention"] = self.stats.get("mention", 0) + 1
            self.last = {"action": "direct", "mention": True, "time": time.strftime("%H:%M:%S")}
            return {"action": "direct", "mentioned": True, "reason": "被艾特了，直接交给主对话"}

        # 0.5) 被回复：有人回复了 bot 自己的消息，同样直接放行
        if replied_to_bot and c.get("sec_reply_to_bot_enabled", True):
            self.stats["replied"] = self.stats.get("replied", 0) + 1
            self.last = {"action": "direct", "replied": True, "time": time.strftime("%H:%M:%S")}
            return {"action": "direct", "replied": True, "reason": "有人回复了 bot 的消息，直接交给主对话"}

        # 1) 复读
        #    ⚠ 只看真人的发言：bot 自己复读的那条也会被写进聊天记录，
        #    若把它一起算进窗口，窗口里就永远凑得够 need 条 → 每来一条新消息
        #    都判定「群里在复读」再发一次，形成无限复读。
        #    再加一道同句冷却：同一个群、同一句在冷却期内不重复跟。
        if c.get("sec_repeat_enabled", True):
            _rep_src = recent_human if recent_human is not None else recent
            rep = detect_repeat(list(_rep_src or []),
                                self._i(c, "sec_repeat_window", 6),
                                self._i(c, "sec_repeat_need", 3),
                                self._i(c, "sec_repeat_min_len", 2))
            if rep:
                _cd = max(0, self._i(c, "sec_repeat_cooldown", 120))
                _key = "%s|%s" % (str(group_id), _norm(rep))
                _now = time.time()
                _last = float(self._rep_cd.get(_key) or 0)
                if _cd and _last and (_now - _last) < _cd:
                    self.stats["repeat_skip"] = self.stats.get("repeat_skip", 0) + 1
                    self.last = {"action": "repeat_skip", "text": rep,
                                 "wait": int(_cd - (_now - _last)),
                                 "time": time.strftime("%H:%M:%S")}
                    # 冷却中：这一句先不跟，继续往下走（关键词 / 秘书模型）
                else:
                    self._rep_cd[_key] = _now
                    if len(self._rep_cd) > 200:      # 防字典无限增长
                        for _k in sorted(self._rep_cd, key=lambda x: self._rep_cd[x])[:100]:
                            self._rep_cd.pop(_k, None)
                    self.stats["repeat"] += 1
                    self.last = {"action": "repeat", "text": rep, "time": time.strftime("%H:%M:%S")}
                    return {"action": "repeat", "text": rep, "reason": "群里在复读"}

        # 1.5) 呼唤模式：被@/被回复后的在线期内不再叫秘书模型，直接放给主对话。
        #      复读检测放在这之前 —— 复读是独立机制，不受在线状态影响。
        if in_call:
            self.stats["call_mode"] = self.stats.get("call_mode", 0) + 1
            self.last = {"action": "direct", "in_call": True, "time": time.strftime("%H:%M:%S")}
            return {"action": "direct", "in_call": True, "reason": "呼唤模式在线，直接交给主对话"}

        # 2) 关键词
        kw = self.hit_keyword(text)
        if kw:
            self.stats["keyword"] += 1
            self.last = {"action": "direct", "keyword": kw, "time": time.strftime("%H:%M:%S")}
            return {"action": "direct", "keyword": kw, "reason": "命中关键词「%s」" % kw}

        # 3) 秘书模型
        try:
            r = await self.ask(text, recent)
        except Exception as e:
            self.stats["error"] += 1
            self.last = {"action": "error", "error": str(e)[:200], "time": time.strftime("%H:%M:%S")}
            return {"action": "skip", "error": str(e)[:200]}
        score = max(0.0, min(10.0, self._f(r, "score", 0)))
        mood = max(0.0, min(10.0, self._f(r, "mood", 0)))
        need = self.threshold() * 10
        mood_need = self._f(c, "sec_mood_threshold", 0.5) * 10
        ok = (score >= need) and (mood >= mood_need)
        act = "reply" if ok else "silence"
        self.stats["yes" if ok else "no"] += 1
        info = {"action": act, "score": score, "mood": mood, "need": round(need, 2),
                "mood_need": round(mood_need, 2), "reason": str(r.get("reason") or "")[:160],
                "time": time.strftime("%H:%M:%S")}
        self.last = info
        return info

    def status(self) -> Dict[str, Any]:
        c = self._c()
        return {
            "enabled": bool(c.get("sec_enabled")),
            "configured": bool(str(c.get("sec_base_url") or "").strip() and str(c.get("sec_model") or "").strip()),
            "model": str(c.get("sec_model") or ""),
            "base_url": str(c.get("sec_base_url") or ""),
            "yes": self._i(c, "sec_yes_weight", 6),
            "no": self._i(c, "sec_no_weight", 4),
            "threshold": round(self.threshold(), 3),
            "ctx_count": self._i(c, "sec_ctx_count", 10),
            "keywords": list(c.get("sec_keywords") or []),
            "repeat_enabled": bool(c.get("sec_repeat_enabled", True)),
            "repeat_window": self._i(c, "sec_repeat_window", 6),
            "repeat_need": self._i(c, "sec_repeat_need", 3),
            "repeat_cooldown": self._i(c, "sec_repeat_cooldown", 120),
            "stats": dict(self.stats),
            "last": dict(self.last),
        }


def create_secretary(cfg: Callable[[], Dict[str, Any]],
                     log: Optional[Callable] = None) -> Secretary:
    return Secretary(cfg, log)
