# -*- coding: utf-8 -*-
"""心情爆表时，让 AI 自己去 QQ空间 发一条说说。

流程（全部在一次专用调用里完成，回复内容不进对话）：
  1. 某个聊天里 精力 / 情欲 / 心情 同时达到阈值 -> 按权重抽一次
  2. 抽中后，按该聊天的提示词拼 system，再注入一段「写条说说」的要求（可在面板改）
  3. 要求模型把正文包在 <qzone>...</qzone> 里，返回后剪出来
  4. 图片沿用「让 AI 自己挑表情包」的逻辑：解析 <meme /> 条件 -> 挑图 -> 和正文一起发
  5. 发布走 qzone.QzoneClient
"""

import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_TAG = "qzone"

DEFAULT_PROMPT = """【发说说】你刚刚在这段对话里被哄得很开心（精力 {energy}，情欲 {lust}，心情 {mood}）。
把这份心情写成一条 QQ空间 说说：就像平时说话那样，一两句到三五句，不要写成日记、不要报数据、不要提这是任务。
把说说正文包在 <{tag}> 和 </{tag}> 之间，标签外不要写任何东西。想配表情包就按上面的表情包规则写 <meme />。"""

# 每个会话上次发说说的时刻（内存态，重启即清）
_LAST: Dict[str, float] = {}
# 进程内共用的客户端（登录态可复用）
_CLIENTS: Dict[int, Any] = {}


def _cfg(bot: Any, key: str, default: Any = None) -> Any:
    return (bot.config or {}).get(key, default)


def get_client(bot: Any) -> Any:
    """按 bot 实例复用同一个空间客户端。"""
    import qzone
    c = _CLIENTS.get(id(bot))
    if c is None:
        c = qzone.QzoneClient(bot)
        _CLIENTS[id(bot)] = c
    return c


def _num(bot: Any, key: str, default: float) -> float:
    try:
        return float((bot.config or {}).get(key, default))
    except Exception:
        return float(default)


def roll_trigger(bot: Any, chat_id: Any, state: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """判断这次要不要发。返回 (是否发, 原因说明)。"""
    if not _cfg(bot, "qzone_enabled", False):
        return False, "功能未开启"
    try:
        import mood as _MO
        st = state if isinstance(state, dict) else _MO.current(bot.config)
    except Exception:
        st = state or {}
    need = {"energy": _num(bot, "qzone_trigger_energy_min", 90),
            "lust": _num(bot, "qzone_trigger_lust_min", 90),
            "mood": _num(bot, "qzone_trigger_mood_min", 80)}
    got = {k: float(st.get(k) or 0) for k in need}
    bad = [k for k in need if need[k] > 0 and got[k] < need[k]]
    if bad:
        return False, "未达阈值（%s）" % ",".join("%s %g<%g" % (k, got[k], need[k]) for k in bad)
    key = "%s" % (chat_id,)
    cool = _num(bot, "qzone_post_cooldown_min", 180) * 60.0
    if cool > 0 and key in _LAST and time.time() - _LAST[key] < cool:
        left = (cool - (time.time() - _LAST[key])) / 60.0
        return False, "冷却中（还剩 %.0f 分钟）" % left
    weight = max(0.0, min(100.0, _num(bot, "qzone_post_weight", 50)))
    if random.uniform(0, 100) >= weight:
        return False, "抽取未命中（权重 %g%%）" % weight
    _LAST[key] = time.time()
    return True, "命中，开始写说说"


def build_inject(bot: Any, state: Dict[str, Any], hint: str = "") -> str:
    tag = str(_cfg(bot, "qzone_block_tag", DEFAULT_TAG) or DEFAULT_TAG).strip() or DEFAULT_TAG
    tpl = str(_cfg(bot, "qzone_post_prompt", "") or "").strip() or DEFAULT_PROMPT
    try:
        import mood as _MO
        word = str((state or {}).get("word") or "")
        desc = str((state or {}).get("desc") or "")
    except Exception:
        word = desc = ""
    return (tpl.replace("{tag}", tag)
               .replace("{energy}", str(state.get("energy", "")))
               .replace("{lust}", str(state.get("lust", "")))
               .replace("{mood}", str(state.get("mood", "")))
               .replace("{word}", word)
               .replace("{desc}", desc)
               .replace("{hint}", str(hint or "")))


def extract_block(text: str, tag: str = DEFAULT_TAG) -> Tuple[str, str]:
    """剪出 <tag>...</tag> 里的正文，返回 (去掉该块的全文, 正文)。"""
    t = str(tag or DEFAULT_TAG).strip() or DEFAULT_TAG
    m = re.search(r"<%s>(.*?)</%s>" % (re.escape(t), re.escape(t)), text or "", re.S | re.I)
    if not m:
        return (text or "").strip(), ""
    body = (m.group(1) or "").strip()
    rest = ((text or "")[:m.start()] + (text or "")[m.end():]).strip()
    return rest, body


def _pick_images(bot: Any, reply: str, chat_type: str, chat_id: Any) -> List[str]:
    """按「AI 自己挑表情包」的逻辑取图，返回本地路径列表。"""
    if not _cfg(bot, "qzone_send_images", True):
        return []
    st = getattr(bot, "sticker", None)
    if st is None:
        return []
    try:
        import sticker_steal as _SS
        _, jobs = _SS.extract_meme_jobs(reply, max_count=1)
        if not jobs:
            return []
        return st.pick_for_send(jobs, chat_type=chat_type, chat_id=chat_id) or []
    except Exception as e:
        try:
            import asyncio
            asyncio.get_event_loop().create_task(
                bot.log("WARN", "[空间] 取表情包失败：%r" % (e,)))
        except Exception:
            pass
        return []


async def maybe_post(bot: Any, *, chat_type: str, chat_id: Any, system_prompt: str,
                     call_llm: Any, hint: str = "", state: Optional[Dict[str, Any]] = None) -> bool:
    """走完一次「判断 -> 专用调用 -> 剪块 -> 配图 -> 发布」。不抛异常。"""
    try:
        ok, why = roll_trigger(bot, chat_id, state)
        if not ok:
            if why != "功能未开启":
                await bot.log("INFO", "[空间] 本次不发：%s" % why)
            return False
        try:
            import mood as _MO
            st = state if isinstance(state, dict) else _MO.current(bot.config)
        except Exception:
            st = state or {}
        tag = str(_cfg(bot, "qzone_block_tag", DEFAULT_TAG) or DEFAULT_TAG).strip() or DEFAULT_TAG
        messages = [{"role": "system", "content": str(system_prompt or "")},
                    {"role": "user", "content": build_inject(bot, st, hint)}]
        reply = await call_llm(messages)
        _, body = extract_block(reply or "", tag)
        if not body:
            await bot.log("WARN", "[空间] 模型没按 <%s> 格式返回，本次不发：%s"
                          % (tag, str(reply or "")[:120]))
            return False
        imgs = _pick_images(bot, reply or "", chat_type, chat_id)
        client = get_client(bot)
        r = await client.publish(body, imgs)
        if r.get("ok"):
            await bot.log("INFO", "[空间] 已发说说（%d 字%s）：%s"
                          % (len(body), "，带图 %d 张" % len(imgs) if imgs else "", body[:60]))
            return True
        await bot.log("WARN", "[空间] 发布失败 code=%s HTTP=%s：%s"
                      % (r.get("code"), r.get("status"), str(r.get("message") or "")[:400]))
        return False
    except Exception as e:
        try:
            await bot.log("WARN", "[空间] 专用调用异常：%r" % (e,))
        except Exception:
            pass
        return False
