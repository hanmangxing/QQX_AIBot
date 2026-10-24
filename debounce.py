# -*- coding: utf-8 -*-
"""消息防抖（「魂」页签 · 第一个选项卡）
把用户短时间内连发的多条文本合并成一条"完整消息"再交给 AI，避免 AI 抢答。

功能对齐 AstrBot 插件 astrbot_plugin_debounce（作者 Jason.Joestar）：
缓冲区累积多条消息 → 判断器打分 → 分数 >= 阈值就放行 → 否则继续等，超时兜底放行；
回复前又收到新消息时可以取消本次回复。
本文件按功能重写，未复制其代码（其原版是 ONNX 小模型；这里用免依赖内置判断器 + 可切本机小模型）。

判断口径：score >= threshold 视为"这句话说完了"（放行），否则继续缓冲等待。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable, Dict, Optional, Tuple

# ---------------------------------------------------------------- 默认配置
DEFAULTS: Dict[str, Any] = {
    "debounce_enabled": False,
    "debounce_scope": "both",          # private / group / both
    "debounce_threshold": 0.5,         # 0~1，越高越严格（越容易继续等）
    "debounce_timeout": 30,            # 秒；0 = 不超时（一直等到说完整）
    "debounce_cancel_on_new": True,    # 回复发出前又收到新消息 → 本次回复作废
    "debounce_debug": False,           # 打详细日志
    "debounce_judge": "local",         # local（项目自带小模型，默认）/ builtin（内置规则）/ model（本机接口小模型）
    "debounce_model_base_url": "http://127.0.0.1:11434/v1",
    "debounce_model_key": "ollama",
    "debounce_model_name": "qwen2.5:0.5b-instruct",
    "debounce_model_timeout": 20,
    "debounce_min_chars": 0,           # 小于这个字数直接放行（0 = 不启用）
}

# 以这些词结尾 = 话多半没说完
_TAIL_WAIT = (
    "如果", "要是", "假如", "因为", "所以", "但是", "不过", "然后", "而且", "并且",
    "还有", "以及", "或者", "就是", "那个", "这个", "我的", "你的", "他的", "她的",
    "跟", "和", "与", "对", "给", "从", "到", "让", "把", "被", "在", "是", "有",
    "没", "不", "很", "太", "更", "最", "又", "再", "还", "也", "就", "才", "先",
)
# 以这些字结尾 = 一般说完了
_TAIL_SEND = ("吗", "呢", "吧", "啊", "呀", "哦", "嗯", "哈", "啦", "嘛", "了", "么", "咯", "噢")
_PUNCT_END = "。！？!?…～~"
_PUNCT_PAUSE = "，,、；;：:"


def builtin_score(text: str) -> float:
    """内置免依赖判断器：返回「这句话说完了」的分数 0~1。

    规则来源：句末标点 / 连接词收尾 / 语气词收尾 / 长度 / 是否以标点开头（续写）。
    """
    s = (text or "").strip()
    if not s:
        return 0.0
    score = 0.45
    tail = s[-1]
    if tail in _PUNCT_END:
        score += 0.35
    elif tail in _PUNCT_PAUSE:
        score -= 0.28
    elif any(s.endswith(w) for w in _TAIL_WAIT):
        score -= 0.30
    if any(s.endswith(w) for w in _TAIL_SEND):
        score += 0.12
    n = len(s)
    if n <= 2:
        score -= 0.22
    elif n >= 12:
        score += 0.10
    if n >= 30:
        score += 0.08
    if re.search(r"[。！？!?]\s*$", s):
        score += 0.05
    if re.match(r"^[，,。.！!？?…～~]", s):
        score -= 0.15          # 以标点开头，多半是上一条的续写
    if re.search(r"[，,][^，,。！？!?]{0,3}$", s):
        score -= 0.10          # 逗号后只有很短一截，像是被打断
    return max(0.0, min(1.0, round(score, 3)))


_MODEL_PROMPT = (
    "你是聊天消息的「防抖判断器」。判断下面这条消息是否已经把一句话说完整了。\n"
    "说完了 = 可以回复；没说完 = 用户还会接着说，需要再等。\n"
    "只输出一个 0 到 1 之间的小数（越接近 1 表示越完整），不要输出任何其他内容。\n\n"
    "消息：{text}"
)


def local_score(text: str) -> Optional[float]:
    """项目自带小模型打分（本地 CPU 推理，不产生任何 API 调用）；不可用返回 None。"""
    try:
        import debounce_model
        return debounce_model.predict_send(text)
    except Exception:
        return None


async def model_score(text: str, cfg: Dict[str, Any]) -> Optional[float]:
    """本机小模型打分；失败返回 None（调用方回退到内置判断器）。"""
    base = str(cfg.get("debounce_model_base_url") or "").strip().rstrip("/")
    if not base:
        return None
    url = base + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    key = str(cfg.get("debounce_model_key") or "")
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {
        "model": str(cfg.get("debounce_model_name") or ""),
        "messages": [{"role": "user", "content": _MODEL_PROMPT.format(text=text)}],
        "temperature": 0,
        "max_tokens": 16,
        "stream": False,
    }
    try:
        import aiohttp
        _to = float(cfg.get("debounce_model_timeout") or 20)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_to)) as sess:
            async with sess.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
        content = ""
        try:
            content = (((data or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except Exception:
            content = ""
        if not str(content).strip():
            # 思维链模型 content 可能为空，从 reasoning 里捞
            try:
                msg = ((data or {}).get("choices") or [{}])[0].get("message") or {}
                content = msg.get("reasoning_content") or msg.get("reasoning") or ""
            except Exception:
                content = ""
        m = re.search(r"(\d+(?:\.\d+)?)", str(content))
        if not m:
            return None
        v = float(m.group(1))
        if v > 1.0:
            v = v / 100.0 if v <= 100.0 else 1.0
        return max(0.0, min(1.0, round(v, 3)))
    except Exception:
        return None


class Debouncer:
    """按会话缓冲消息，判断"说完了"才放行。"""

    def __init__(self,
                 get_config: Callable[[], Dict[str, Any]],
                 log: Optional[Callable[[str, str], Any]] = None,
                 replay: Optional[Callable[[str, Dict[str, Any], str], Any]] = None):
        self._get_config = get_config
        self._log = log
        self._replay = replay
        self._buf: Dict[str, Dict[str, Any]] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._pending: Dict[str, str] = {}
        self._discard: set = set()
        self.stats = {"fed": 0, "held": 0, "released": 0, "timeout": 0, "canceled": 0}

    # ------------------------------------------------------------ 基础
    def cfg(self) -> Dict[str, Any]:
        c = dict(DEFAULTS)
        try:
            c.update(self._get_config() or {})
        except Exception:
            pass
        return c

    def enabled_for(self, message_type: str) -> bool:
        c = self.cfg()
        if not c.get("debounce_enabled"):
            return False
        scope = str(c.get("debounce_scope") or "both")
        if scope == "both":
            return message_type in ("private", "group")
        return message_type == scope

    async def _logf(self, level: str, msg: str):
        if not self._log:
            return
        try:
            await self._log(level, msg)
        except Exception:
            pass

    # ------------------------------------------------------------ 判断
    async def judge(self, text: str, cfg: Optional[Dict[str, Any]] = None) -> Tuple[float, str]:
        """返回 (分数, 来源)。来源：builtin / local / model / local->builtin / model->builtin。"""
        c = cfg or self.cfg()
        b = builtin_score(text)
        mode = str(c.get("debounce_judge") or "local")
        if mode == "builtin":
            return b, "builtin"
        if mode == "local":
            lo = await asyncio.to_thread(local_score, text)
            if lo is None:
                return b, "local->builtin"
            # 小模型擅长判"半句话"，规则擅长判明显完整的句子，两者取较高分更稳
            return max(b, lo), "local"
        m = await model_score(text, c)
        if m is None:
            return b, "model->builtin"
        return m, "model"

    # ------------------------------------------------------------ 主流程
    async def feed(self, sid: str, text: str, meta: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        """投喂一条文本。返回 ('send'|'wait', 合并后的文本)。"""
        c = self.cfg()
        text = (text or "").strip()
        if not text:
            return "send", ""
        self.stats["fed"] += 1
        try:
            _min = int(c.get("debounce_min_chars") or 0)
        except Exception:
            _min = 0
        if _min and len(text) < _min:
            self._pending[sid] = text
            return "send", text

        # 回复还没发出去又来新消息 → 作废上一次回复（cancel_on_new_message）
        if c.get("debounce_cancel_on_new") and sid in self._pending:
            self._discard.add(sid)
            self.stats["canceled"] += 1
            if c.get("debounce_debug"):
                await self._logf("INFO", f"[防抖] 上一条还没回复完就来了新消息，本次回复作废：{sid}")

        buf = self._buf.get(sid)
        if buf is None:
            buf = {"text": "", "t": 0.0, "n": 0, "meta": dict(meta or {})}
            self._buf[sid] = buf
        buf["text"] = (buf["text"] + text) if buf["text"] else text
        buf["t"] = time.time()
        buf["n"] += 1
        if meta:
            buf["meta"] = dict(meta)

        thr = float(c.get("debounce_threshold") or 0.5)
        score, src = await self.judge(buf["text"], c)
        merged = buf["text"]
        if c.get("debounce_debug"):
            await self._logf("INFO", f"[防抖] 第{buf['n']}条 · 累计{len(merged)}字 · 完整度 {score}（{src}）· 阈值 {thr}")
        if score >= thr:
            self._buf.pop(sid, None)
            self._cancel_timer(sid)
            self._pending[sid] = merged
            self.stats["released"] += 1
            await self._logf("INFO", f"[防抖] 放行（完整度 {score}，合并 {buf['n']} 条 / {len(merged)} 字）")
            return "send", merged

        self.stats["held"] += 1
        await self._logf("INFO", f"[防抖] 收到第 {buf['n']} 条，判断还没说完（完整度 {score}），先等一等…")
        self._start_timer(sid, c)
        return "wait", merged

    # ------------------------------------------------------------ 超时
    def _start_timer(self, sid: str, c: Dict[str, Any]):
        self._cancel_timer(sid)
        try:
            t = int(c.get("debounce_timeout") or 0)
        except Exception:
            t = 0
        if t <= 0:
            return
        self._tasks[sid] = asyncio.create_task(self._timeout(sid, t))

    def _cancel_timer(self, sid: str):
        task = self._tasks.pop(sid, None)
        if task and not task.done():
            task.cancel()

    async def _timeout(self, sid: str, t: int):
        try:
            await asyncio.sleep(t)
        except asyncio.CancelledError:
            return
        self._tasks.pop(sid, None)
        buf = self._buf.pop(sid, None)
        if not buf or not buf.get("text"):
            return
        self.stats["timeout"] += 1
        self._pending[sid] = buf["text"]
        await self._logf("INFO", f"[防抖] 等够 {t}s 没等到后续，直接放行（{len(buf['text'])} 字）")
        if self._replay:
            try:
                await self._replay(sid, buf.get("meta") or {}, buf["text"])
            except Exception as e:
                await self._logf("ERROR", f"[防抖] 超时回放失败：{e}")

    # ------------------------------------------------------------ 取消回复
    def should_discard(self, sid: str) -> bool:
        return sid in self._discard

    def clear_discard(self, sid: str):
        self._discard.discard(sid)

    def mark_replied(self, sid: str):
        self._pending.pop(sid, None)
        self._discard.discard(sid)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "stats": dict(self.stats),
            "buffers": {k: {"text": v.get("text", ""), "n": v.get("n", 0)}
                        for k, v in self._buf.items()},
            "pending": dict(self._pending),
        }

    async def shutdown(self):
        for sid in list(self._tasks):
            self._cancel_timer(sid)
        self._buf.clear()
