# -*- coding: utf-8 -*-
"""消息分段发送（魂 · 消息分段发送）

把**即将发出的**一条回复按规则拆成多条小消息，按「打字节奏」的随机间隔依次发出，
模拟真人一条条打字的感觉。

⚠️ 只影响「怎么发」，不影响任何记录：
   调用方照旧把完整原文写进聊天记录 / 数据库 / 上下文，这里只负责把发送动作拆开。
   也就是说上下文永远是那条连续的长文本。

分割规则用注册表管理，以后加规则只要 register_rule() 一行，UI 下拉里再加一个 option 即可。
"""
from __future__ import annotations

import asyncio
import random
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 配置默认值
DEFAULTS: Dict[str, Any] = {
    "seg_enabled": False,          # 总开关
    "seg_scope": "both",           # private / group / both
    "seg_rule": "newline",         # 分割规则名（见 SPLIT_RULES）
    "seg_min_len": 0,              # 片段短于这个字数就并到上一条（0 = 不合并）
    "seg_base_ms": 300,            # 每条之间的基础停顿（毫秒）
    "seg_per_char_ms": 45,         # 每个字再加多少毫秒（越长停越久）
    "seg_jitter": 0.35,            # 随机浮动 ±35%
    "seg_min_ms": 150,             # 停顿下限
    "seg_max_ms": 4000,            # 停顿上限（防止长文停太久）
    "seg_debug": False,
}


# ---------------------------------------------------------------- 分割规则
def _split_newline(text: str) -> List[str]:
    """按换行拆（默认规则）。"""
    return [p.strip() for p in re.split(r"\n+", text) if p.strip()]


def _split_blankline(text: str) -> List[str]:
    """只在空行处拆（一段内部的换行保留）。"""
    return [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]


def _split_sentence(text: str) -> List[str]:
    """按句末标点拆，过短的句子并到上一句。"""
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?…~～])", text) if p.strip()]
    out: List[str] = []
    for p in parts:
        if out and len(p) <= 6:
            out[-1] = out[-1] + p
        else:
            out.append(p)
    return out


#: name -> (界面显示名, 切分函数)。以后加规则：register_rule() 或直接在这里加一项。
SPLIT_RULES: Dict[str, Tuple[str, Callable[[str], List[str]]]] = {
    "newline": ("按换行分割", _split_newline),
    "blankline": ("按空行分割", _split_blankline),
    "sentence": ("按句子分割", _split_sentence),
}


def register_rule(name: str, label: str, fn: Callable[[str], List[str]]) -> None:
    """注册一个新的分割规则（以后扩展用）。"""
    SPLIT_RULES[str(name)] = (str(label), fn)


def rule_label(name: str) -> str:
    return SPLIT_RULES.get(str(name), ("", _split_newline))[0]


def split_text(text: str, rule: str = "newline", min_len: int = 0) -> List[str]:
    """按规则把文本切成多条；切不出来就原样返回一条。"""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not t:
        return []
    fn = SPLIT_RULES.get(str(rule), SPLIT_RULES["newline"])[1]
    try:
        parts = [p for p in (fn(t) or []) if p and p.strip()]
    except Exception:
        parts = [t]
    if not parts:
        return [t]
    if min_len and min_len > 0:
        merged: List[str] = []
        for p in parts:
            if merged and len(p) < int(min_len):
                merged[-1] = merged[-1] + "\n" + p
            else:
                merged.append(p)
        parts = merged
    return parts


# ---------------------------------------------------------------- 打字节奏
def delay_ms(chunk: str, cfg: Optional[Dict[str, Any]] = None, rnd: Optional[float] = None) -> int:
    """算这条片段发完后要停多久：基础值 + 每字增量，再乘一个随机浮动，最后夹在上下限内。"""
    c = dict(DEFAULTS)
    c.update(cfg or {})

    def _f(k, d=0.0):
        try:
            return float(c.get(k, d) or 0)
        except Exception:
            return float(d)

    ms = _f("seg_base_ms", 300) + _f("seg_per_char_ms", 45) * len(chunk or "")
    jit = _f("seg_jitter", 0.35)
    if jit > 0:
        r = random.random() if rnd is None else float(rnd)
        ms *= 1.0 + (r * 2.0 - 1.0) * jit
    hi = _f("seg_max_ms", 4000)
    lo = _f("seg_min_ms", 150)
    if hi > 0:
        ms = min(ms, hi)
    if lo > 0:
        ms = max(ms, lo)
    return int(max(0, ms))


def plan(text: str, cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """返回 [{'text': 片段, 'delay_ms': 发完这条后停多久}, ...]，最后一条 delay 为 0。"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    try:
        min_len = int(c.get("seg_min_len") or 0)
    except Exception:
        min_len = 0
    parts = split_text(text, str(c.get("seg_rule") or "newline"), min_len)
    out = []
    for i, p in enumerate(parts):
        out.append({"text": p, "delay_ms": 0 if i == len(parts) - 1 else delay_ms(p, c)})
    return out


def should_split(cfg: Optional[Dict[str, Any]], scope: str) -> bool:
    c = cfg or {}
    if not c.get("seg_enabled"):
        return False
    sc = str(c.get("seg_scope") or "both")
    return sc == "both" or sc == str(scope)


# ---------------------------------------------------------------- 发送
async def send_segmented(text: str, send: Callable, cfg: Optional[Dict[str, Any]] = None,
                         scope: str = "both", images: Optional[List[str]] = None,
                         log: Optional[Callable] = None) -> int:
    """按配置分段发送。send(text, images) 是实际发送函数（async）。

    返回实际发出的条数。图片跟最后一条一起发。
    注意：这里**不写任何记录**，调用方该怎么记就怎么记（记完整原文）。
    """
    c = dict(DEFAULTS)
    c.update(cfg or {})
    steps = plan(text, c)
    if not steps:
        return 0
    if len(steps) == 1 and not (images or None):
        await send(steps[0]["text"], images)
        return 1
    n = 0
    for i, st in enumerate(steps):
        im = (images or None) if i == len(steps) - 1 else None
        await send(st["text"], im)
        n += 1
        if c.get("seg_debug") and log:
            try:
                log("[分段] 第 %d/%d 条 %d 字，停 %dms" % (i + 1, len(steps), len(st["text"]), st["delay_ms"]))
            except Exception:
                pass
        if st["delay_ms"] > 0:
            await asyncio.sleep(st["delay_ms"] / 1000.0)
    return n
