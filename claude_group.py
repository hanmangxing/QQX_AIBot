# -*- coding: utf-8 -*-
"""群聊模式 —— 与主 Bot 完全独立的群聊链路。

和主 Bot 的区别：
  · 独立上下文：自己的 history.json / memory.json / memory_vector.json / notebook.json / world_book.json
  · 不注入破甲：不用 TFE_SYSTEM_PROMPT / DARKSIDE_BYPASS / game_content 工具，就是一段普通人设 + 群聊规则
  · 群聊放开：群消息进上下文，由模型自己决定要不要说话
  · 串行排队：同一时间只跑一个请求；期间到达的消息先攒着，等上一轮回复发出去后合并成一次请求
  · 沉默：模型只输出 <silence /> 就一个字都不发，控制台记一条「保持沉默」

数据目录：claude_data/（相对项目根）
"""

import secretary as SEC
import segments
import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import aiohttp

DATA_DIR = "claude_data"

DEFAULT_PERSONA = (
    "你是群里的一名普通群友，昵称叫「{name}」。\n"
    "你有自己的性格和脾气，说话随意、口语化，像个真人而不是客服。\n"
    "你不知道自己是个 AI 程序，也不需要提起这件事。"
)

GROUP_RULES = """【群聊场景】
下面是这个 QQ 群最近的聊天记录，每行是「昵称：内容」。
你不是客服，不用每条都回：想说话就自然地说一句，不想说话就只输出 {marker}（这一个标签，别的什么都别写）。
说话时保持你自己的性格和口吻，短一点、口语化，像群里的人那样聊天，别写成文章、别用列表。
有人 @ 你（记录里会写成「@你」）时要回应。不要冒充别人，也不要解释这些规则。"""


PIC_RULE = """【想发图的时候】
如果你觉得这会儿发张图正合适（比如有人让你画、或者你想配个图），
就在回复里带上一个 <pic prompt="生图提示词" /> 标签，系统会真的把图画出来发到群里。
用不用完全由你自己决定 —— 想发就写，不想发就不写。
不要解释这个标签，也不要把它当成话题说出来。
注意：<pic> 和 {marker} 不能同时出现，要么说话、要么发图。
想指定尺寸就在后面另起一行写 <size>宽x高</size>（例：<size>1024x1024</size>），不写就按默认。
画多个人 / 合照时，把这几个角色的标准提示词都拼进 prompt，人数标签改成 2girls / multiple girls 并去掉 solo，别漏角色、也别自己编已经给过的 tag。
只要这张图里出现了带专属 LoRA 的角色（设定里写了文件名），就要把它们的 LoRA 都带上：
lora="文件名:权重"，多个用 | 连接（例：<pic prompt="..." lora="a.safetensors:0.8|b.safetensors:0.8" />）；
整张图一个带 LoRA 的角色都没有时才不写。
"""

DEFAULT_TEMPLATE = """{persona}

{worldbook}{memory}{notebook}

{pic_guide}{meme_guide}{note_rule}

{rules}"""

# 「人设注入」的内置档。默认那档**不给人设**，只告诉模型它接进了一个群聊，
# 让它保持自己本来的性格 —— 不要一上来就让它 role play。
PERSONAS = {
    "attach": {
        "name": "接入群聊（不改人设）",
        "text": (
            "你现在被接进了一个 QQ 群聊，能实时看到群里的消息，也能直接发言。\n"
            "下面每行是群里最近的记录，格式是「昵称：内容」。\n"
            "保持你自己的性格、口吻和判断，不用扮演任何角色，也不用提起这段说明。"
        ),
    },
    "groupmate": {
        "name": "群友（扮演一个普通群友）",
        "text": (
            "你是群里的一名普通群友，有自己的性格和脾气，说话随意、口语化，像个真人而不是客服。\n"
            "你不知道自己是个 AI 程序，也不需要提起这件事。"
        ),
    },
    "pure": {
        "name": "🧊 纯净（只发群聊上下文）",
        "text": "",
    },
}
DEFAULT_PERSONA_ID = "attach"

INJECT_KEYS = ("worldbook", "memory", "notebook", "pic")

# 四个注入开关的默认值 —— 跟着人设走
_DEFAULT_INJECT = {"worldbook": True, "memory": True, "notebook": True, "pic": True}
PERSONA_INJECT_DEFAULT = {
    "attach": dict(_DEFAULT_INJECT),
    "groupmate": dict(_DEFAULT_INJECT),
    "pure": {"worldbook": False, "memory": False, "notebook": False, "pic": False},
}

# 纯净档：system 就这两行，人设/世界书/记忆/笔记/生图说明全都不注入
PURE_SYSTEM = """下面是一个 QQ 群聊最近的记录，每行是「昵称：内容」。
想说就在回复里直接说，不想说就只输出 {marker}。"""

# 固定规则：系统写死，面板只展示不给改
NOTE_RULE = """【笔记】
系统会自动把和你当前话题相关的笔记挑出来放在上面给你看，你不用主动去翻，也不用提起这件事。"""


async def _collect_stream(resp) -> dict:
    """把 SSE 流拼回普通 chat/completions 响应的同构结构 —— 下游解析一行都不用改。
    正文可能落在 content 或 reasoning_content（思维链模型），工具调用参数分片累加。"""
    text, reason, finish = "", "", None
    tcs, buf, seen_sse = {}, "", False
    async for chunk in resp.content.iter_any():
        buf += chunk.decode("utf-8", "ignore")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            seen_sse = True
            data = line[5:].strip()
            if data == "[DONE]":
                buf = ""
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            ch = (obj.get("choices") or [{}])[0]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            d = ch.get("delta") or ch.get("message") or {}
            if isinstance(d.get("content"), str):
                text += d["content"]
            for _k in ("reasoning_content", "reasoning"):
                if isinstance(d.get(_k), str):
                    reason += d[_k]
            for t in (d.get("tool_calls") or []):
                _i = t.get("index", 0)
                slot = tcs.setdefault(_i, {"id": "", "type": "function",
                                           "function": {"name": "", "arguments": ""}})
                if t.get("id"):
                    slot["id"] = t["id"]
                _f = t.get("function") or {}
                if _f.get("name"):
                    slot["function"]["name"] += _f["name"]
                if _f.get("arguments"):
                    slot["function"]["arguments"] += _f["arguments"]
    if not seen_sse and not text and not reason and not tcs:
        try:
            return json.loads(buf)
        except Exception:
            pass
    msg = {"role": "assistant", "content": text or None}
    if reason:
        msg["reasoning_content"] = reason
    if tcs:
        msg["tool_calls"] = [tcs[k] for k in sorted(tcs)]
    return {"choices": [{"message": msg, "finish_reason": finish or "stop"}]}

class ClaudeGroupMode:
    """独立的群聊模式引擎。"""

    def __init__(self, config: Dict[str, Any], log: Callable, send_group_msg: Callable,
                 data_dir: str = DATA_DIR, make_embedder: Optional[Callable] = None,
                 lwb_wire: Optional[Callable] = None,
                 comfy_getter: Optional[Callable] = None,
                 meme_getter: Optional[Callable] = None,
                 save_images: Optional[Callable] = None,
                 auto_summary: Optional[Callable] = None,   # 回复完自动总结（不阻塞）
                 steal_images: Optional[Callable] = None,
                 napcat_api: Optional[Callable] = None):
        self.config = config
        self.log = log
        self.send_group_msg = send_group_msg
        # 给 NapCat 发 API 请求并等回执（用来反查被回复消息是谁发的）
        self.napcat_api = napcat_api
        self.comfy_getter = comfy_getter
        self.meme_getter = meme_getter   # 表情包偷取实例，用来挑图 / 出说明
        # 图片落地函数（由 bot 注入）：async (群号, 图片段) -> [{"tag","path","mime"}, ...]
        self.save_images = save_images
        self.auto_summary = auto_summary
        # 偷图函数（由 bot 注入）：async (群号, 发图人, 图片段, 昵称) -> 收了几张
        self.steal_images = steal_images
        self._imgs: Dict[str, Dict[str, Dict[str, str]]] = {}   # 群号 -> {tag: {path,mime}}
        self._last_draw_at = 0.0
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # ---- 独立存储（延迟 import，避免和 bot.py 循环依赖）----
        from bot import HistoryStore
        from memory import MemoryStore
        from notebook import NotebookStore

        self.history = HistoryStore(path=self.data_dir / "history.json")
        self.secretary = SEC.create_secretary(lambda: self.config, self.log)
        self.memory = MemoryStore(path=self.data_dir / "memory.json",
                                  vector_path=self.data_dir / "memory_vector.json",
                                  embedder=make_embedder() if make_embedder else None)
        try:
            self.notebook = NotebookStore(str(self.data_dir / "notebook.json"))
        except Exception as e:
            print(f"[claude] 笔记本初始化失败：{e}")
            self.notebook = None
        self.world_book: List[Dict[str, Any]] = []

        # 记忆引擎的调用器（总结/锚点/重排）沿用主 Bot 那套，写进的是本模式自己的库
        if lwb_wire:
            try:
                lwb_wire(self.memory)
            except Exception as e:
                print(f"[claude] 记忆引擎挂载失败：{e}")

        self._pending: Dict[str, int] = {}      # 群号 -> 待处理消息数
        self._call_until: Dict[str, float] = {}   # 群号 -> 呼唤模式到期时间戳
        self._tasks: Dict[str, asyncio.Task] = {}
        self._sent: List[Dict[str, Any]] = []   # 最近的发送记录，给面板看
        self.stats = {"seen": 0, "replied": 0, "silent": 0, "failed": 0, "drew": 0}

    # ---------------- 配置读取 ----------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.config.get("claude_" + key, default)

    def enabled(self) -> bool:
        return bool(self._cfg("enabled", False))

    def bot_name(self) -> str:
        """留空 = 不告诉它自己叫什么，让它自己认。"""
        return str(self._cfg("bot_name") or "").strip()

    def group_allowed(self, group_id: Any) -> bool:
        raw = str(self._cfg("groups") or "").strip()
        if not raw:
            return True                      # 留空 = 所有群都生效
        ids = {x.strip() for x in re.split(r"[,，\s;；]+", raw) if x.strip()}
        return str(group_id) in ids

    # ---------------- 呼唤模式（被@/被回复后保持在线） ----------------
    def _call_window(self) -> float:
        """呼唤模式窗口（秒）。<=0 表示关掉。"""
        try:
            return max(0.0, float(self._cfg("call_mode_minutes", 5) or 0) * 60.0)
        except Exception:
            return 300.0

    def _call_left(self, group_id: Any) -> float:
        """这个群还剩多少秒在线（<=0 = 已经冷场退出）。"""
        return float(self._call_until.get(str(group_id)) or 0) - time.time()

    def _call_touch(self, group_id: Any, window: float = 0.0) -> float:
        """把在线状态续到「现在 + 窗口」；每条新消息都续一次，冷场才退。"""
        w = window if window > 0 else self._call_window()
        self._call_until[str(group_id)] = time.time() + w
        return w

    def _call_stop_words(self) -> List[str]:
        """闭嘴词列表。留空 = 没有闭嘴词。"""
        raw = str(self._cfg("call_stop_words") or "").strip()
        if not raw:
            return []
        return [x.strip() for x in re.split(r"[,，\n;；]+", raw) if x.strip()]

    def _call_hit_stop(self, text: Any) -> str:
        """这条消息命中的闭嘴词；没命中返回空串。"""
        t = str(text or "")
        if not t:
            return ""
        for w in self._call_stop_words():
            if w in t:
                return w
        return ""

    def _call_exit(self, group_id: Any) -> None:
        """立刻结束这个群的呼唤模式（下次再来消息就不在线了）。"""
        self._call_until.pop(str(group_id), None)

    def load_world_book(self, entries: List[Dict[str, Any]]):
        self.world_book = list(entries or [])

    # ---------------- 群消息入口 ----------------
    async def handle_group_event(self, data: Dict[str, Any]) -> bool:
        """群消息入口。返回 True 表示这条消息被本模式吃掉了（主 Bot 不再处理）。"""
        if not self.enabled():
            return False
        group_id = data.get("group_id")
        if group_id is None:
            return False
        if not self.group_allowed(group_id):
            return False

        sender = data.get("sender") or {}
        name = sender.get("card") or sender.get("nickname") or str(data.get("user_id"))
        self_id = data.get("self_id")
        text = self._extract_text(data, self_id)
        is_self = self_id is not None and str(data.get("user_id")) == str(self_id)

        if is_self:
            # 自己发出去的消息（NapCat 有时会回显）：只进上下文，不触发回复
            if text:
                self._append(group_id, "assistant", text, self.bot_name())
            return True

        # 群里的图 / 表情包：落地成本地文件，历史里只留 [图片#N] 占位，
        # 组装 messages 时再把图挂给模型看（模型支持多模态就能直接看）
        segs = self._extract_images(data)
        if segs and self._cfg("vision_enabled", True) and self.save_images:
            items: List[Dict[str, Any]] = []
            try:
                items = await self.save_images(group_id, segs)
            except Exception as e:
                await self.log("WARN", f"[群聊] 图片落地失败，本轮按纯文本处理：{e}")
            if items:
                self._remember_imgs(group_id, items)
                text = (text + "".join(str(i.get("tag") or "") for i in items)).strip()
                await self.log("INFO", f"[群聊] 群 {group_id} 收到 {len(items)} 张图："
                                       + "、".join(str(i.get("tag")) for i in items))
            elif segs:
                _d0 = segs[0].get("data") or {}
                await self.log("WARN", "[群聊] 群 %s 收到 %d 张图但一张都没取到字节，字段："
                                       "file=%s url=%s file_id=%s"
                               % (group_id, len(segs), str(_d0.get("file"))[:50],
                                  str(_d0.get("url"))[:80], str(_d0.get("file_id"))[:40]))

        # 表情包偷取：跟「要不要给它看」是两个开关，这里只负责把图丢进待审核池
        if segs and self.steal_images:
            try:
                await self.steal_images(group_id, data.get("user_id") or 0, segs, name)
            except Exception as e:
                await self.log("WARN", f"[偷图] 入池失败：{e}")

        if not text:
            return True     # 真没内容（图也取不到、字也没有）：本模式吃掉，不让它掉到主 Bot

        self.stats["seen"] += 1
        self._append(group_id, "user", text, name,
                     sender_id=str(data.get("user_id") or ""))

        # 【秘书网关】完整消息进来先过一道：复读 → 关键词 → 秘书模型（权重 + 气氛时机）
        try:
            # 被艾特了吗。NapCat 的 messagePostFormat 配的是 string，message 是一整串
            # CQ 码而不是数组段；两种格式都要认，只按数组取会一个 at 段都拿不到。
            _self_id = str(data.get("self_id") or "")
            _mentioned = self._detect_mentioned(data, _self_id)
            _rec = [(str(m.get("sender_name") or ("AI" if m.get("role") == "assistant" else "某人")),
                     self._flat_content(m.get("content")))
                    for m in self._recent(group_id)]
            # 复读检测专用：只喂真人的发言。bot 自己复读的那条也在聊天记录里，
            # 一起算进去会让复读永远「成立」，必须剔掉。
            _rec_human = [(str(m.get("sender_name") or "某人"),
                           self._flat_content(m.get("content")))
                          for m in self._recent(group_id)
                          if str(m.get("role") or "") != "assistant"]
            # 有人回复了 bot 自己的消息吗。同样两种格式都认（CQ 码串 / 数组段）。
            # 标准 reply 只有消息 id、没有 user_id，拿不到发送者就调 get_msg 反查。
            _replied = await self._detect_replied(data, _self_id)
            # 【呼唤模式】被@ / 被回复 -> 进入 N 分钟在线状态；在线期间每条新消息续命，
            # 冷场满 N 分钟自动退出。在线期间不经秘书模型，防抖后直接放给主对话。
            _cw = self._call_window()
            _in_call = False
            if _cw > 0 and bool(self._cfg("call_mode_enabled", True)):
                _left = self._call_left(group_id)
                if _left > 0:
                    _stop = self._call_hit_stop(text)
                    if _stop:
                        # 闭嘴词：立刻结束呼唤模式，这条起不再在线（本条按正常流程交给秘书判断）
                        self._call_exit(group_id)
                        await self.log("INFO", "[呼唤模式] 命中闭嘴词「%s」，结束呼唤模式（原本还剩 %.0f 秒）"
                                       % (_stop, _left))
                    else:
                        _in_call = True
                        self._call_touch(group_id, _cw)
                        await self.log("INFO", "[呼唤模式] 在线中（原本还剩 %.0f 秒），这条直接放行、计时续满 %.0f 分钟"
                                       % (_left, _cw / 60.0))
                elif _mentioned or _replied:
                    _in_call = True
                    self._call_touch(group_id, _cw)
                    await self.log("INFO", "[呼唤模式] 被%s，进入 %.0f 分钟在线状态"
                                   % ("@" if _mentioned else "回复", _cw / 60.0))
            if _replied and not _in_call:
                await self.log("INFO", "[秘书] 这条是回复 bot 自己消息的，直接放行（不叫秘书模型）")
            _dec = await self.secretary.gate(group_id, text, _rec, mentioned=_mentioned,
                                             replied_to_bot=_replied, recent_human=_rec_human,
                                             in_call=_in_call)
        except Exception as _e:
            _dec = {"action": "skip"}
            await self.log("WARN", f"[秘书] 判断失败，按老样子处理：{_e}")
        _act = str(_dec.get("action") or "skip")
        if _act == "repeat":
            _rt = str(_dec.get("text") or "").strip()
            if _rt:
                try:
                    await self.send_group_msg(int(group_id), _rt)
                except Exception as _e:
                    await self.log("WARN", f"[秘书] 复读发送失败：{_e}")
                self._append(group_id, "assistant", _rt, self.bot_name())
                self.stats["replied"] += 1
                self._sent.insert(0, {"group_id": str(group_id), "text": _rt, "images": 0,
                                      "time": time.strftime("%Y-%m-%d %H:%M:%S")})
                del self._sent[40:]
                await self.log("SUCCESS", f"[秘书] 群里在复读，跟着发了一条：{_rt[:40]}")
            return True
        if _act == "silence":
            self.stats["silent"] += 1
            await self.log("INFO", f"[秘书] 这条不适合插话（{_dec.get('reason') or '秘书说 No'}），保持沉默")
            return True
        if _act in ("direct", "reply"):
            await self.log("INFO", f"[秘书] 放行给主对话：{_dec.get('reason') or _act}")

        key = str(group_id)
        self._pending[key] = self._pending.get(key, 0) + 1
        self._ensure_worker(group_id)
        return True

    @staticmethod
    def _extract_text(data: Dict[str, Any], self_id: Any = None) -> str:
        """把 OneBot 消息段拼成给模型看的文本。

        关键：@ 不能丢掉 —— 群里 @ 是独立的 at 段（不是文字），如果只取 text 段，
        模型看到的就只剩「你来不来」，它根本不知道自己被叫了，于是选择沉默。
        所以 @ 到自己要变成「@你」，让模型一眼看出「这是在叫我」。
        """
        msg = data.get("message")
        if isinstance(msg, list):
            parts = []
            for seg in msg:
                if not isinstance(seg, dict):
                    continue
                tp = seg.get("type")
                sd = seg.get("data") or {}
                if tp == "text":
                    parts.append(str(sd.get("text") or ""))
                elif tp == "at":
                    qq = str(sd.get("qq") or "")
                    if qq == "all":
                        parts.append("@全体成员")
                    elif self_id is not None and qq == str(self_id):
                        parts.append("@你")
                    elif qq:
                        parts.append("@" + qq)
            t = "".join(parts).strip()
            if t:
                return t
        # message 本身就是 CQ 码串（messagePostFormat=string）时，raw_message 可能是空的，
        # 不能只认 raw_message —— 否则整条消息会被当成空消息直接吃掉。
        raw = msg if isinstance(msg, str) and msg.strip() else str(data.get("raw_message") or "")
        raw = re.sub(r"\[CQ:at,qq=all\]", "@全体成员", raw)
        if self_id is not None:
            raw = re.sub(r"\[CQ:at,qq=%s\]" % re.escape(str(self_id)), "@你", raw)
        raw = re.sub(r"\[CQ:at,qq=(\d+)\]", r"@\1", raw)
        raw = re.sub(r"\[CQ:[^\]]*\]", "", raw)
        return raw.strip()

    @staticmethod
    def _cq_unescape(v: str) -> str:
        """CQ 码参数里的实体还原：&amp; → &，&#44; → ,，&#91;/&#93; → [ ]。

        图片的 url 是腾讯 CDN 地址，里面的 & 会被转义成 &amp;，不还原就下不下来。
        """
        return (str(v).replace("&amp;", "&").replace("&#44;", ",")
                .replace("&#91;", "[").replace("&#93;", "]"))

    @classmethod
    def _cq_to_segs(cls, text: Any) -> List[Dict[str, Any]]:
        """把 [CQ:image,...] / [CQ:mface,...] 原始码解析成图片段。

        NapCat 默认 messagePostFormat=string，message 是一整串 CQ 码而不是数组，
        只按数组取会一张图都拿不到（「群里发图它收不到」就是这个原因）。
        实测格式：[CQ:image,file=XXXX.png,sub_type=0,url=https://...&amp;rkey=...]
        """
        out: List[Dict[str, Any]] = []
        for m in re.finditer(r"\[CQ:(image|mface)((?:,[^\]]*)?)\]", str(text or "")):
            kind, body = m.group(1), m.group(2) or ""
            d: Dict[str, Any] = {}
            for kv in body.split(","):
                if "=" not in kv:
                    continue
                k, v = kv.split("=", 1)
                d[k.strip()] = cls._cq_unescape(v)
            out.append({"type": kind, "data": d})
        return out

    @classmethod
    def _extract_images(cls, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从消息里捞出图片 / 表情包段。数组格式和 CQ 码字符串格式都支持。"""
        msg = data.get("message")
        out: List[Dict[str, Any]] = []
        if isinstance(msg, list):
            for seg in msg:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") in ("image", "mface"):
                    d = seg.get("data")
                    if isinstance(d, dict):
                        out.append({"type": seg.get("type"), "data": dict(d)})
        if not out:
            raw = msg if isinstance(msg, str) else data.get("raw_message")
            if isinstance(raw, str) and "CQ:" in raw:
                out = cls._cq_to_segs(raw)
        return out

    # ---------------- 消息段解析：数组格式 / CQ 码串格式 ----------------
    @staticmethod
    def _cq_all_segs(text: Any) -> List[Dict[str, Any]]:
        """把 CQ 码串解析成段列表（at / reply / image / mface / text …）。

        NapCat 的 messagePostFormat 配的是 string（见 app.py 的 auto_enable_napcat_ws），
        message 就是 `[CQ:at,qq=123]文字[CQ:reply,id=456]` 这样一整串。
        不解析成段的话，「被@」「被回复」根本判不出来。
        """
        out: List[Dict[str, Any]] = []
        for m in re.finditer(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]", str(text or "")):
            kind, body = m.group(1), m.group(2) or ""
            d: Dict[str, Any] = {}
            for kv in body.split(","):
                if "=" not in kv:
                    continue
                k, v = kv.split("=", 1)
                d[k.strip()] = ClaudeGroupMode._cq_unescape(v.strip())
            out.append({"type": kind, "data": d})
        return out

    @classmethod
    def _msg_segments(cls, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """把一条消息统一成段列表：数组格式直接用，CQ 码串解析出来，两者都有就合并。"""
        out: List[Dict[str, Any]] = []
        msg = data.get("message")
        if isinstance(msg, list):
            out = [s for s in msg if isinstance(s, dict)]
        raw = msg if isinstance(msg, str) else data.get("raw_message")
        if isinstance(raw, str) and raw:
            out = out + cls._cq_all_segs(raw)
        return out

    @classmethod
    def _detect_mentioned(cls, data: Dict[str, Any], self_id: Any) -> bool:
        """这条消息有没有 @ 到 bot 自己。"""
        sid = str(self_id or "")
        if not sid:
            return False
        for seg in cls._msg_segments(data):
            if str(seg.get("type") or "") != "at":
                continue
            qq = str((seg.get("data") or {}).get("qq") or "")
            if qq and qq == sid:
                return True
        return False

    async def _detect_replied(self, data: Dict[str, Any], self_id: Any) -> bool:
        """这条消息有没有回复 bot 自己发的消息。

        标准 reply 段 / [CQ:reply,id=...] 只有消息 id、没有 user_id，所以：
          ① 带了 user_id 就直接比；
          ② 只有 id 就调 get_msg 反查这条被回复消息是谁发的。
        """
        sid = str(self_id or "")
        if not sid:
            return False
        uid, mid = "", ""
        for seg in self._msg_segments(data):
            if str(seg.get("type") or "") != "reply":
                continue
            d = seg.get("data") or {}
            uid = str(d.get("user_id") or "")
            mid = str(d.get("id") or d.get("message_id") or "")
            break
        if uid and uid == sid:
            return True
        if not mid or not self.napcat_api:
            return False
        try:
            r = await self.napcat_api("get_msg", {"message_id": mid}, 6.0)
            d = (r or {}).get("data") or {}
            su = str(d.get("user_id") or (d.get("sender") or {}).get("user_id") or "")
            return bool(su) and su == sid
        except Exception as e:
            await self.log("WARN", f"[秘书] 反查被回复消息失败：{e}")
            return False

    def _remember_imgs(self, group_id: Any, items: List[Dict[str, Any]]):
        """记下这几张图在本地哪（tag -> 路径/mime）。只留最近 40 张，防止越攒越多。"""
        bucket = self._imgs.setdefault(str(group_id), {})
        for it in items:
            tag = str(it.get("tag") or "")
            if tag:
                bucket[tag] = {"path": str(it.get("path") or ""),
                               "mime": str(it.get("mime") or "image/jpeg")}
        if len(bucket) > 40:
            for k in list(bucket.keys())[:-40]:
                bucket.pop(k, None)

    def _imgs_for(self, group_id: Any, text: Any) -> List[Dict[str, Any]]:
        """这段记录里出现过的图（最多 4 张，按出现顺序）。"""
        bucket = self._imgs.get(str(group_id)) or {}
        if not bucket:
            return []
        blob = text if isinstance(text, str) else "\n".join(str(x) for x in (text or []))
        out, seen = [], set()
        for m in re.finditer(r"\[(?:图片|表情包)#\d+\]", blob):
            tag = m.group(0)
            if tag in seen or tag not in bucket:
                continue
            seen.add(tag)
            out.append(dict(bucket[tag]))
        return out[-4:]

    @staticmethod
    def _flat_content(c: Any) -> str:
        """预览用：多模态 content（列表）拍平成「文字（+N 张图）」。"""
        if not isinstance(c, list):
            return str(c or "")
        txt = "\n".join(str(p.get("text") or "") for p in c
                        if isinstance(p, dict) and p.get("type") == "text")
        n = sum(1 for p in c if isinstance(p, dict) and p.get("type") == "image_url")
        return txt + ("（+%d 张图）" % n if n else "")

    @staticmethod
    def _attach_imgs(messages: List[Dict[str, Any]], items: List[Dict[str, Any]]) -> int:
        """把图片转成 base64 data URI 挂到最后一条消息上（content 由 str 换成数组）。"""
        if not messages or not items:
            return 0
        last = dict(messages[-1])
        old = last.get("content")
        parts = list(old) if isinstance(old, list) else [{"type": "text", "text": str(old or "")}]
        n = 0
        for it in items:
            try:
                b = Path(str(it.get("path") or "")).read_bytes()
            except Exception:
                continue
            parts.append({"type": "image_url",
                          "image_url": {"url": "data:%s;base64,%s" % (
                              it.get("mime") or "image/jpeg", base64.b64encode(b).decode())}})
            n += 1
        if n:
            last["content"] = parts
            messages[-1] = last
        return n

    @staticmethod
    def _strip_imgs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把多模态 content 拍回纯文本，用于「中转站不吃图」时重试。"""
        out = []
        for m in messages:
            mm = dict(m)
            c = mm.get("content")
            if isinstance(c, list):
                mm["content"] = "\n".join(str(p.get("text") or "") for p in c
                                          if isinstance(p, dict) and p.get("type") == "text")
            out.append(mm)
        return out

    async def _describe_one(self, part: Dict[str, Any], stk: Any) -> str:
        """把一段 image_url 交给表情包识图模型，返回一句可读转述。
        任何一步失败都只返回说明文字，绝不抛异常 —— 它只是重试前的补救，不该把重试也搞挂。"""
        try:
            url = str(((part.get("image_url") or {}).get("url")) or "")
            if ";base64," not in url:
                return "识图模型看不了这段图片格式"
            raw = base64.b64decode(url.split(";base64,", 1)[1])
            if stk is None:
                return "没配表情包识图模型，看不到内容"
            import tempfile
            _ext = ".png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else (
                ".gif" if raw[:3] == b"GIF" else ".jpg")
            tmp = Path(tempfile.gettempdir()) / ("qqbotx_vt_%d%s" % (time.time_ns(), _ext))
            tmp.write_bytes(raw)
            try:
                d = await stk.classify(tmp)
            finally:
                try:
                    tmp.unlink()
                except Exception:
                    pass
            txt = str((d or {}).get("desc") or "").strip()
            ocr = str((d or {}).get("ocr") or "").strip()
            if txt and ocr:
                return txt + "（图上文字：" + ocr + "）"
            return txt or (("图上文字：" + ocr) if ocr else "识图模型没给出内容")
        except Exception as e:
            return "转述失败（%s）" % str(e)[:60]

    async def _transcribe_imgs(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """带图请求失败后的兜底：用表情包识图模型把图片转成文字，
        写回文本里原来的 [图片#N] 占位符，同时摘掉真实图片段，让重试变成纯文本请求。"""
        stk = None
        try:
            stk = self.meme_getter() if callable(self.meme_getter) else None
        except Exception:
            stk = None
        out = []
        for m in messages:
            mm = dict(m)
            c = mm.get("content")
            if not isinstance(c, list):
                out.append(mm)
                continue
            texts = [str(p.get("text") or "") for p in c
                     if isinstance(p, dict) and p.get("type") == "text"]
            imgs = [p for p in c if isinstance(p, dict) and p.get("type") == "image_url"]
            blob = "\n".join(texts)
            tags = re.findall(r"\[(?:图片|表情包)#\d+\]", blob)
            for idx, p in enumerate(imgs):
                desc = await self._describe_one(p, stk)
                tag = tags[idx] if idx < len(tags) else ""
                if tag and tag in blob:
                    # 写回原来的占位符：位置、编号都不动，只把内容补进去
                    blob = blob.replace(tag, tag + "｜" + desc, 1)
                else:
                    blob = blob + "\n[图片转述] " + desc
            mm["content"] = blob
            out.append(mm)
        return out
    # ---------------- 串行队列 ----------------
    def _ensure_worker(self, group_id: Any):
        key = str(group_id)
        t = self._tasks.get(key)
        if t is None or t.done():
            self._tasks[key] = asyncio.create_task(self._worker(group_id))

    async def _worker(self, group_id: Any):
        key = str(group_id)
        try:
            while self._pending.get(key):
                n = self._pending.pop(key, 0)
                await self.log("INFO", f"[群聊] 群 {key} 收到 {n} 条新消息，开始处理")
                try:
                    await self._respond(group_id)
                except Exception as e:
                    self.stats["failed"] += 1
                    await self.log("ERROR", f"[群聊] 群 {key} 回复失败：{e}")
        finally:
            self._tasks.pop(key, None)

    def pending_count(self) -> int:
        return sum(self._pending.values())

    def busy_groups(self) -> List[str]:
        return [k for k, t in self._tasks.items() if t and not t.done()]

    @staticmethod
    def _is_blocked_note(text: str) -> bool:
        """这段文本是不是「被外审拦截」（中转会把拦截原因当正文返回）。"""
        try:
            from bot import _is_prohibited_note
            return bool(_is_prohibited_note(text))
        except Exception:
            return False

    async def _respond(self, group_id: Any):
        messages = self._build_messages(group_id)
        # 【转述】勾了「用识图模型转述图片」：带图的请求先让识图模型写成文字再发
        if self._cfg("main_vision_transcribe", False) and any(isinstance(m.get("content"), list) for m in messages):
            try:
                messages = await self._transcribe_imgs(messages)
                await self.log("INFO", "[群聊] 已按设置用识图模型转述图片（主对话模型无需多模态）")
            except Exception as _e:
                await self.log("WARN", f"[群聊] 图片转述失败，按原样发送：{_e}")
        # 外审拦截：群聊这条链路以前完全没有这层判断，被拦的文案会直接发进群里。
        # 现在默认就走这套 —— 命中就不当正文、不发群、不进上下文，再按「外审拦截」
        # 设置重试；重试用尽本轮沉默（什么都不发）。
        _blk_retry = 0
        _gcfg = self.config or {}
        if str(_gcfg.get("block_action", "none") or "none").lower() == "retry":
            try:
                _blk_retry = max(1, min(10, int(_gcfg.get("block_retry_times", 2) or 2)))
            except Exception:
                _blk_retry = 2
        reply = ""
        for _t in range(_blk_retry + 1):
            try:
                reply = await self._call_api(messages)
            except Exception as e:
                # 中转站可能不吃多模态：去掉图片重试一次，别让整轮回复挂掉
                if any(isinstance(m.get("content"), list) for m in messages):
                    if bool(self.config.get("sticker_vision_fallback_transcribe", False)):
                        # 开了「转述图片」：先用识图模型把图转成文字写回占位符，再重试
                        await self.log("WARN", f"[群聊] 带图请求失败（{e}），改用识图模型转述图片后重试")
                        try:
                            reply = await self._call_api(await self._transcribe_imgs(messages))
                        except Exception as e2:
                            await self.log("WARN", f"[群聊] 转述后仍然失败（{e2}），去掉图片再试一次")
                            reply = await self._call_api(self._strip_imgs(messages))
                    else:
                        await self.log("WARN", f"[群聊] 带图请求失败（{e}），去掉图片重试一次")
                        reply = await self._call_api(self._strip_imgs(messages))
                else:
                    raise
            if reply and self._is_blocked_note(reply):
                await self.log("WARN", "[群聊] 被外审拦截（PROHIBITED_CONTENT）第 %d 次：%s"
                                      % (_t + 1, str(reply)[:160]))
                if _t < _blk_retry:
                    await self.log("INFO", "[群聊][外审拦截] 按设置重试，第 %d/%d 次" % (_t + 1, _blk_retry))
                    await asyncio.sleep(0.6)
                    continue
                await self.log("WARN", "[群聊][外审拦截] 重试用尽 → 本轮不回话（这条不会进群）")
                reply = ""
            break
        text = self.decide(reply, str(self._cfg("silence_marker") or "<silence />"))
        try:
            import idle as _ID
            _ID.touch(getattr(self, "config", None), "group", str(group_id), self.bot_name())
            if _ID.chat_recall_hit(getattr(self, "config", None)):
                _mem = await self.bot._build_memory_block(int(group_id), _ID.recall_query())
                _cr = _ID.prompt_chat_recall(getattr(self, "config", None), _mem)
                if _cr:
                    messages.append({"role": "system", "content": _cr})
                    await self.log("INFO", "[旧事重提] 这轮回复里会突发提一句以前的事")
        except Exception:
            pass
        try:
            import poke as _POKE2
            _pk2 = _POKE2.active_prompt_block(getattr(self, "config", None))
            if _pk2:
                messages.append({"role": "system", "content": _pk2})
        except Exception:
            pass
        try:
            import active_msg as _AM2
            _am_new, text = _AM2.extract(text)
            try:
                import coyote as _CY5, re as _re5, threading as _th5
                _cy_tags5 = _re5.findall(r"<coyote_(?:fire|strength)\b[^>]*?/?>", text, flags=_re5.IGNORECASE)
                if _cy_tags5:
                    text = _re5.sub(r"<coyote_(?:fire|strength)\b[^>]*?/?>\s*", "", text, flags=_re5.IGNORECASE)
                    _th5.Thread(target=lambda: _CY5.extract("\n".join(_cy_tags5), "AI"), daemon=True).start()
            except Exception:
                pass
            import mood as _MO3
            text, _md_upd3 = _MO3.extract(text)
            if _md_upd3:
                _MO3.save_parsed(_md_upd3)
            import poke as _POKE3
            _pk_list, text = _POKE3.extract_active(getattr(self, 'config', None), text)
            for _t in _pk_list:
                if _POKE3.active_ok(getattr(self, 'config', None), _t):
                    await self.bot._send_poke(_t, group_id)
                    await self.log('INFO', f'[戳一戳] 它自己戳了 {_t} 一下')
            for _it in _am_new:
                if _AM2.add(getattr(self, 'config', None), _it['at'], _it['text'], 'group', str(group_id), self.bot_name()):
                    await self.log('INFO', '[主动消息] 已排期 %s' % time.strftime('%m-%d %H:%M', time.localtime(_it['at'])))
        except Exception as _e:
            await self.log('WARN', f'[主动消息] 解析排期失败：{_e}')
        if text is None:
            self.stats["silent"] += 1
            await self.log("INFO", f"[群聊] 群 {group_id}：模型保持沉默，一个字都没发")
            return
        # 模型自己决定要不要发图：正文里带 <pic prompt="..." /> 就真出图
        send_text, images = await self._handle_pics(group_id, text)
        # 【表情包】正文里带 <meme ... />：从库里挑一张，跟正文一起发
        send_text, _memes_b, _memes_a = await self._handle_memes(group_id, send_text)
        _drew = len(images)
        _memes = list(_memes_b) + list(_memes_a)
        # 表情包独立成条：不再跟正文拼在一条消息里（关掉开关就回到「跟正文一起发」）
        _sep = bool(self._cfg("sticker_send_separate", True))
        if _memes_a and not _sep:
            images = list(images) + list(_memes_a)
        if _memes:
            self.stats["memed"] = int(self.stats.get("memed", 0)) + len(_memes)
        if not str(send_text or "").strip() and not images and not _memes_b and not _memes_a:
            self.stats["silent"] += 1
            await self.log("INFO", f"[群聊] 群 {group_id}：无可发内容，本轮不发")
            return
        # 【防抖】回复发出前又收到新消息 → 本次回复作废（cancel_on_new_message）
        # 放在发送之前：作废的这条既不发送、也不进群聊上下文
        _db2 = getattr(getattr(self, "bot", None), "debounce", None)
        _sid2 = "group:%s" % group_id
        if _db2 is not None and _db2.should_discard(_sid2):
            _db2.clear_discard(_sid2)
            await self.log("INFO", "[防抖] 这条回复还没发出去就来了新消息，本次回复作废（不发送、不入群聊上下文）")
            return
        if _memes_b:
            await self.send_group_msg(int(group_id), "", images=_memes_b)
            await self.log("INFO", f"[群聊] 先发 {len(_memes_b)} 张表情包（正文之前）")
        _seg_cfg = dict(self.config or {})
        if str(send_text or "").strip() or images:
            if segments.should_split(_seg_cfg, "group"):
                await segments.send_segmented(
                    send_text,
                    lambda _t, _im: self.send_group_msg(int(group_id), _t, images=_im),
                    cfg=_seg_cfg, scope="group", images=images, log=self.log)
            else:
                await self.send_group_msg(int(group_id), send_text, images=images)
            # 【空间】心情爆表时，用一次专用调用让它自己去发条说说（内容不进群）
            try:
                import qzone_post as _QZP
                await _QZP.maybe_post(
                    self, chat_type="group", chat_id=group_id,
                    system_prompt=self._system_prompt(group_id),
                    call_llm=self._call_api)
            except Exception as _e:
                await self.log("WARN", f"[空间] 触发异常：{_e}")
        if _memes_a and _sep:
            await self.send_group_msg(int(group_id), "", images=_memes_a)
            await self.log("INFO", f"[群聊] 再发 {len(_memes_a)} 张表情包（正文之后）")
        # 上下文里落「带标签的原文」，让它记得自己画过图，下次才会继续主动画
        if _db2 is not None:
            _db2.mark_replied(_sid2)
        self._append(group_id, "assistant", text, self.bot_name())
        self.stats["replied"] += 1
        if _drew:
            self.stats["drew"] += _drew
        self._sent.insert(0, {"group_id": str(group_id), "text": send_text,
                              "images": len(images),
                              "time": time.strftime("%Y-%m-%d %H:%M:%S")})
        del self._sent[40:]
        await self.log("SUCCESS", f"[群聊] 已发送到群 {group_id}：{send_text[:70]}"
                                   + (f"（含 {_drew} 张图）" if _drew else "")
                                   + (f"（含 {len(_memes)} 张表情包）" if _memes else ""))
        # 【记忆】这轮回复落库了，顺手看要不要自动总结（后台跑，不阻塞）
        if self.auto_summary:
            try:
                self.auto_summary(int(group_id), self.bot_name())
            except Exception as _e:
                print(f"[claude] 自动总结排队失败：{_e}")

    async def _handle_pics(self, group_id: Any, text: str):
        """正文里带 <pic prompt="..." /> 就真出图。返回 (要发的文字, 本地图片路径列表)。"""
        if not self._cfg("pic_enabled", True):
            return text, []
        try:
            from comfyui import extract_pic_jobs, format_size
        except Exception as e:
            print(f"[claude] 生图模块不可用：{e}")
            return text, []
        try:
            clean, jobs = extract_pic_jobs(text, max_count=int(self._cfg("pic_max", 1) or 1))
        except Exception as e:
            await self.log("WARN", f"[群聊] <pic> 标签解析失败，按纯文本发：{e}")
            return text, []
        if not jobs:
            return text, []
        comfy = self.comfy_getter() if self.comfy_getter else None
        if comfy is None:
            await self.log("WARN", "[群聊] 生图客户端不可用，本轮只发文字")
            return (clean.strip() or "（想画张图，可是画不出来…）"), []
        if not self.config.get("comfy_enabled", True):
            await self.log("INFO", "[群聊] 生图总开关已关，跳过出图")
            return (clean.strip() or "（想画张图）"), []
        cd = int(self.config.get("comfy_cooldown", 20) or 0)
        now = time.time()
        if cd > 0 and (now - self._last_draw_at) < cd:
            await self.log("WARN", f"[群聊] 生图冷却中（{cd}s），本轮跳过出图")
            return (clean.strip() or "（刚画过，等会儿再画）"), []
        self._last_draw_at = now
        images: List[str] = []
        for job in jobs:
            prompt = str(job.get("prompt") or "")
            w, h = job.get("size") or (0, 0)
            await self.log("INFO", f"[群聊] 自主出图：{prompt[:80]}｜尺寸 {format_size(w, h) or '工作流默认'}")
            try:
                res = await comfy.generate(prompt, width=w, height=h,
                                           loras=job.get("loras") or None)
            except Exception as e:
                await self.log("ERROR", f"[群聊] 生图调用异常：{e}")
                continue
            if res.get("ok"):
                got = res.get("images") or []
                images.extend(got)
                await self.log("SUCCESS", f"[群聊] 出图成功（{res.get('elapsed')}s，{len(got)} 张）")
            else:
                await self.log("ERROR", f"[群聊] 生图失败：{res.get('message')}")
        if not clean.strip() and images:
            clean = "（画好了，给你们看看～）"
        return clean, images

    def persona_id(self) -> str:
        pid = str(self._cfg("persona_id") or "").strip()
        return pid or DEFAULT_PERSONA_ID

    def personas(self) -> List[Dict[str, Any]]:
        """内置 + 自定义人设，给面板用。"""
        out: List[Dict[str, Any]] = []
        for k, v in PERSONAS.items():
            out.append({"id": k, "name": v["name"], "builtin": True,
                        "inject": self.persona_inject(k),
                        "text": str(v["text"]).replace("{name}", self.bot_name())})
        for p in (self._cfg("personas") or []):
            if isinstance(p, dict) and str(p.get("id") or ""):
                out.append({"id": str(p["id"]), "name": str(p.get("name") or "未命名人设"),
                            "inject": self.persona_inject(str(p["id"])),
                            "text": str(p.get("text") or ""), "builtin": False})
        return out

    def persona_inject(self, pid: str = "") -> Dict[str, bool]:
        """这套人设记下的四个注入开关（没记过就用默认，纯净档默认全关）。"""
        pid = pid or self.persona_id()
        base = dict(PERSONA_INJECT_DEFAULT.get(pid, _DEFAULT_INJECT))
        saved = (self._cfg("persona_inject") or {}).get(pid) or {}
        if isinstance(saved, dict):
            for k in INJECT_KEYS:
                if k in saved:
                    base[k] = bool(saved[k])
        return base

    def apply_persona_inject(self, pid: str = "") -> Dict[str, bool]:
        """把这套人设记下的开关写回那四个全局开关（面板勾选框看的就是它们）。"""
        inj = self.persona_inject(pid)
        for k in INJECT_KEYS:
            self.config["claude_%s_enabled" % k] = bool(inj[k])
        return inj

    def _named(self, txt: str) -> str:
        """名字留空 → 一句都不提名字，让模型自己认。"""
        name = self.bot_name()
        out = str(txt or "").replace("{name}", name)
        if name and "你的昵称" not in out:
            out = "你的昵称是「%s」。\n" % name + out
        return out.strip()

    def _persona_text(self) -> str:
        """当前生效的人设文本。面板上没保存的编辑用 _persona_override 临时生效（预览用）。"""
        override = str(getattr(self, "_persona_override", "") or "").strip()
        if override:
            return self._named(override)
        pid = self.persona_id()
        for p in self.personas():
            if p["id"] == pid:
                return self._named(str(p["text"]))
        legacy = str(self._cfg("system_prompt") or "").strip()
        if legacy:
            return self._named(legacy)
        return self._named(str(PERSONAS[DEFAULT_PERSONA_ID]["text"]))

    def _note_rule(self) -> str:
        if not self._cfg("notebook_enabled", True) or self.notebook is None:
            return ""
        return NOTE_RULE

    def fixed_rules(self) -> Dict[str, str]:
        """系统写死、面板只展示不给改的规则。"""
        marker = str(self._cfg("silence_marker") or "<silence />")
        return {
            "pic": (PIC_RULE.replace("{marker}", marker) if self._cfg("pic_enabled", True) else ""),
            "meme": self._meme_guide(),
            "notebook": (NOTE_RULE if self._cfg("notebook_enabled", True) else ""),
        }

    def _group_rules(self) -> str:
        """面板上「群聊规则」框的内容；留空用内置精简版。"""
        marker = str(self._cfg("silence_marker") or "<silence />")
        custom = str(self._cfg("group_rules") or "").strip()
        return (custom or GROUP_RULES).replace("{marker}", marker)

    def _meme_guide(self) -> str:
        """发表情包说明：开关关了就返回空串（由 sticker_steal 出文案，库里没货也会说明）。"""
        st = self.meme_getter() if self.meme_getter else None
        if st is None:
            return ""
        try:
            return str(st.guide_text() or "")
        except Exception:
            return ""

    async def _handle_memes(self, group_id: Any, text: str):
        """正文里带 <meme ... /> 就从库里挑一张。返回 (抠掉标记的文本, 本地文件路径列表)。"""
        try:
            import sticker_steal as _SS
        except Exception:
            return text, [], []
        try:
            clean, jobs = _SS.extract_meme_jobs(
                text, max_count=int(self._cfg("sticker_send_max", 1) or 1))
        except Exception:
            return text, [], []
        if not jobs:
            return clean, [], []
        st = self.meme_getter() if self.meme_getter else None
        if st is None:
            return clean, [], []
        # 位置：模型显式写了 pos 的听模型的；只靠标签落点推断的，按精力 + 情绪极性掷随机
        _energy = 60
        try:
            if bool(self._cfg("sticker_pos_random", True)):
                import mood as _MO
                _energy = int((_MO.current(self.config) or {}).get("energy") or 60)
                for _j in jobs:
                    if not _j.get("pos_explicit"):
                        _j["pos"] = _SS.decide_pos(
                            _j.get("emo") or _j.get("emotion") or "",
                            _energy, self.config or {})
                await self.log("INFO", "[群聊] 表情包时机：精力 %d → 前 %d / 后 %d"
                               % (_energy,
                                  sum(1 for _j in jobs if _j.get("pos") == "before"),
                                  sum(1 for _j in jobs if _j.get("pos") != "before")))
        except Exception as _e:
            await self.log("WARN", f"[群聊] 表情包时机随机失败，按默认位置：{_e}")
        _jb = [j for j in jobs if str(j.get("pos") or "") == "before"]
        _ja = [j for j in jobs if str(j.get("pos") or "") != "before"]
        _b: List[str] = []
        _a: List[str] = []
        try:
            if _jb:
                _b = st.pick_for_send(_jb, chat_type="group", chat_id=group_id) or []
            if _ja:
                _a = st.pick_for_send(_ja, chat_type="group", chat_id=group_id) or []
        except Exception as e:
            await self.log("WARN", f"[群聊] 挑表情包失败：{e}")
            return clean, [], []
        if _b or _a:
            await self.log("INFO", f"[群聊] 发表情包 {len(_b) + len(_a)} 张（前 {len(_b)} / 后 {len(_a)}）")
        else:
            await self.log("INFO", "[群聊] 写了发表情包标记，但库里没有对得上的，已跳过")
        return clean, _b, _a

    def _pic_guide(self) -> str:
        """生图说明：留空用内置短版，不再把整份 ComfyUI 说明书塞进去（那玩意最压性格）。"""
        if not self._cfg("pic_enabled", True):
            return ""
        marker = str(self._cfg("silence_marker") or "<silence />")
        guide = str(self._cfg("pic_guide") or "").strip() or PIC_RULE
        return guide.replace("{marker}", marker)

    def _preset_extras(self) -> str:
        """群聊模式自己那套破限 + 文风；主 Bot 的预设不会注入到这里。"""
        cfg = self.config or {}
        seg: List[str] = []
        # 破限：只认 claude_llm_preset_id，没选就什么都不注入
        try:
            from bot import _preset_now
            pid = str(cfg.get("claude_llm_preset_id") or "").strip()
            if pid and bool(self._cfg("jailbreak_enabled", True)):
                scoped = dict(cfg)
                for k in ("llm_preset_id", "llm_presets"):
                    scoped[k] = cfg.get("claude_" + k)
                # 破限主体（tfe + body）改由 _preset_head() 钉在 system 最上方，
                # bypass 由 _preset_bypass() 单独成一条 system 消息钉在请求最底部 ——
                # 这里不再拼破限，避免同一段内容出现在两个位置。
                pass
        except Exception as e:
            print(f"[claude] 破限预设读取失败：{e}")
        # 文风：按启用顺序拼
        try:
            en = cfg.get("claude_style_enabled") or []
            plist = cfg.get("claude_style_presets") or []
            idx = {str(p.get("id")): p for p in plist if isinstance(p, dict)}
            for sid in en:
                p = idx.get(str(sid))
                if not p:
                    continue
                txt = str(p.get("text") or "").strip()
                if txt:
                    seg.append("<writing_style>【%s】\n%s\n</writing_style>"
                               % (p.get("name") or sid, txt))
        except Exception as e:
            print(f"[claude] 文风预设读取失败：{e}")
        return "\n\n".join(seg)

    def _preset_head(self) -> str:
        """破限主体：模型交替说明(tfe) + 人设卡/世界书(body)。
        和主 Bot 链路一样钉在 system 的最上方（主 Bot 走 assemble_darkside_system_prompt，
        也是把 preset_body 放在第一个 part）。"""
        if not self._preset_on():
            return ""
        try:
            from bot import _preset_now
            p = _preset_now(self._preset_scoped_cfg()) or {}
        except Exception:
            return ""
        out = []
        for k in ("tfe", "body"):
            v = str(p.get(k) or "").strip()
            if v:
                out.append(v)
        return "\n\n".join(out)

    # ---- 预设（整包）取用：群聊这一档的键映射成 bot._preset_* 认的键 ----
    def _preset_scoped_cfg(self) -> Dict[str, Any]:
        cfg = self.config or {}
        scoped = dict(cfg)
        for k in ("llm_preset_id", "llm_presets"):
            scoped[k] = cfg.get("claude_" + k)
        return scoped

    def _preset_on(self) -> bool:
        """群聊这一档选了破限预设、且破限总开关开着。"""
        cfg = self.config or {}
        return (bool(str(cfg.get("claude_llm_preset_id") or "").strip())
                and bool(self._cfg("jailbreak_enabled", True)))

    def _preset_part(self, key: str) -> str:
        if not self._preset_on():
            return ""
        try:
            from bot import _preset_now
            return str((_preset_now(self._preset_scoped_cfg()) or {}).get(key) or "").strip()
        except Exception:
            return ""

    def _preset_recency(self) -> str:
        return self._preset_part("recency")

    def _preset_bypass(self) -> str:
        return self._preset_part("bypass")

    def _preset_tool_def(self):
        """预设要求的正文工具定义（工具名随预设走）。"""
        if not self._preset_on():
            return None
        try:
            from bot import _preset_tool
            return _preset_tool(self._preset_scoped_cfg())
        except Exception:
            return None

    def _system_prompt(self, group_id: Any) -> str:
        """组装这次要发出去的固定提示词。整段都能在面板上看到并改。"""
        marker = str(self._cfg("silence_marker") or "<silence />")

        recent = self._recent(group_id)
        query = "\n".join(f"{m.get('sender_name') or ''}：{m.get('content') or ''}"
                          for m in recent[-6:]).strip()
        # 纯净档：默认只发群聊上下文；那四个开关要是勾上了，对应内容照样注入
        # 破限主体钉在 system 最上方（勾了破限预设才有）
        _head = self._preset_head()
        if self.persona_id() == "pure" and not str(getattr(self, "_persona_override", "") or "").strip():
            seg = [PURE_SYSTEM.replace("{marker}", marker)]
            for blk in (self._world_book_block(query), self._memory_block(query, group_id),
                        self._notebook_block(query, group_id), self._pic_guide(),
                        self._meme_guide(), self._note_rule()):
                if blk:
                    seg.append(str(blk))
            _ex = self._preset_extras()
            if _ex:
                seg.append(_ex)
            out = re.sub(r"[ \t]+\n", "\n", "\n\n".join(seg))
            out = re.sub(r"\n{3,}", "\n\n", out).strip()
            return (_head + "\n\n" + out).strip() if _head else out
        blocks = {
            "{persona}": self._persona_text(),
            "{note_rule}": self._note_rule(),
            "{worldbook}": self._world_book_block(query),
            "{memory}": self._memory_block(query, group_id),
            "{notebook}": self._notebook_block(query, group_id),
            "{pic_guide}": self._pic_guide(),
            "{meme_guide}": self._meme_guide(),
            "{rules}": self._group_rules(),
            "{name}": self.bot_name(),
        }
        out = str(self._cfg("prompt_template") or "").strip() or DEFAULT_TEMPLATE
        for k, v in blocks.items():
            out = out.replace(k, v)
        # 自定义模板里没写 {meme_guide} 的，补在末尾，保证发表情包规则一定进提示词
        _mg = self._meme_guide()
        if _mg and "{meme_guide}" not in str(self._cfg("prompt_template") or ""):
            out = out + "\n\n" + _mg
        _ex = self._preset_extras()
        if _ex:
            out = out + "\n\n" + _ex
        out = re.sub(r"[ \t]+\n", "\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        out = out.strip()
        return (_head + "\n\n" + out).strip() if _head else out

    def preview(self, group_id: Any = None) -> Dict[str, Any]:
        """给面板看：实际会发出去的原文（固定提示词 + 群聊记录）。"""
        gid = group_id
        if gid in (None, "", 0, "0"):
            gs = self.groups()
            gid = gs[0].get("group_id") if gs else "__preview__"
        try:
            gid = int(gid)
        except Exception:
            gid = "__preview__"
        try:
            msgs = self._build_messages(gid)
        except Exception as e:
            return {"system": f"组装失败：{e}", "messages": [], "chars": 0, "group_id": gid}
        sys_txt = str(msgs[0].get("content") or "")
        return {
            "group_id": gid,
            "chars": len(sys_txt),
            "system": sys_txt,
            "messages": [{"role": m.get("role"), "content": self._flat_content(m.get("content"))[:4000]}
                         for m in msgs[1:]],
        }

    @staticmethod
    def decide(reply: str, marker: str = "<silence />") -> Optional[str]:
        """返回该发出去的文本；None = 保持沉默（一个字都不发）。"""
        raw = str(reply or "")
        if not raw.strip():
            return None
        if marker and marker in raw:
            return None
        # 兜底：把可能漏出来的沉默标签/思考残留清掉，清完是空的也不发
        text = re.sub(r"<silence\s*/?>", "", raw)
        text = re.sub(r"^\s*finishReason:[^\n]*\nfinishMessage:[^\n]*\n?", "", text)
        text = text.strip()
        return text or None

    # ---------------- 上下文组装 ----------------
    def _append(self, group_id: Any, role: str, content: str, sender_name: str = "",
                sender_id: Any = None):
        try:
            self.history.append(int(group_id), role, content, sender_name=sender_name,
                                sender_id=sender_id)
        except Exception as e:
            print(f"[claude] 写群聊记录失败：{e}")

    def _recent(self, group_id: Any) -> List[Dict[str, Any]]:
        # 0/留空 = 不限（未总结的全带上；群聊记录同样会被记忆库自动总结裁剪）
        limit = int(self._cfg("context_limit", 0) or 0)
        try:
            msgs = [m for m in self.history.get_messages(int(group_id))
                    if not m.get("hidden") and str(m.get("content") or "").strip()]
        except Exception:
            return []
        if limit <= 0:
            return msgs[-400:]      # 兜底，防 Prompt 无限膨胀
        return msgs[-max(2, limit):]

    def _world_book_block(self, query: str) -> str:
        if not self._cfg("worldbook_enabled", True) or not self.world_book:
            return ""
        try:
            from bot import match_world_book, format_world_book_block
            return format_world_book_block(match_world_book(query, self.world_book))
        except Exception as e:
            print(f"[claude] 世界书命中失败：{e}")
            return ""

    def _memory_block(self, query: str, group_id: Any = None) -> str:
        if not self._cfg("memory_enabled", True) or self.memory is None:
            return ""
        # 记忆库按会话分组（群聊里就是群号），recall 的第一个参数是 user_id。
        # 这里以前传的是 query（聊天原文），所以永远查不到条目、注入恒为空。
        uid = "" if group_id is None else str(group_id)
        try:
            # 跨会话召回：开关打开时，把其他群/会话的记忆也一起召回
            extra_ids = None
            # 群聊档的「注入其他群聊的记忆」：和跨会话总开关是或的关系，
            # 任一打开就把同一数据源里的其他群会话一起召回。
            _cross_on = bool(self._cfg("memory_cross_inject", False))
            if not _cross_on and bool(self._cfg("memory_cross_groups", False)):
                _cross_on = True
            if _cross_on:
                extra_ids = self.memory.other_user_ids(
                    uid, self._cfg("memory_cross_allow", []) or [])
            hits = self.memory.recall(uid, query,
                                      int(self._cfg("memory_top_k", 5) or 5),
                                      float(self._cfg("memory_min_score", 0.25) or 0.25),
                                      extra_ids)
        except Exception as e:
            print(f"[claude] 记忆召回失败：{e}")
            return ""
        if not hits:
            return ""
        lines = []
        for h in hits:
            title = str(h.get("title") or "").strip()
            text = str(h.get("text") or "").strip()
            if h.get("is_other"):
                text = f"（来自会话 {h.get('from_user_id')}）" + text
            lines.append(f"- {title}：{text}" if title else f"- {text}")
        return "【你记得的事】\n" + "\n".join(lines)

    def _notebook_block(self, query: str, group_id: Any = None) -> str:
        if not self._cfg("notebook_enabled", True) or self.notebook is None:
            return ""
        # 自动召回：开关 / 条数 / 阈值 / 预览字数，各数据源独立（群聊走 claude_ 前缀）
        if not self._cfg("notebook_inject", True):
            return ""
        try:
            _tk = max(1, int(self._cfg("notebook_top_k", 3) or 3))
        except Exception:
            _tk = 3
        try:
            _pv = max(50, int(self._cfg("notebook_inject_chars", 300) or 300))
        except Exception:
            _pv = 300
        try:
            _thr = float(self._cfg("notebook_inject_threshold", 0) or 0)
        except Exception:
            _thr = 0.0
        _parts = []
        try:
            _b1 = self.notebook.inject_block(query, top_k=_tk, preview=_pv, min_score=_thr)
            if _b1:
                _parts.append(str(_b1).strip())
        except Exception as e:
            print(f"[claude] 笔记召回失败：{e}")
        # 跨对话注入：开了开关、且（白名单为空 或 本群在白名单里）时，
        # 把主 Bot（私聊）那一侧的笔记本也一起带上。
        try:
            _c2 = getattr(self.bot, "config", None) or {}
            if _c2.get("claude_notebook_cross_inject"):
                _allow = [str(x) for x in (_c2.get("claude_notebook_cross_allow") or [])]
                if not _allow or (group_id is not None and str(group_id) in _allow):
                    _mst = getattr(self.bot, "notebook_store", None)
                    if _mst is not None:
                        _b2 = _mst.inject_block(query, top_k=_tk, preview=_pv, min_score=_thr)
                        if _b2:
                            _parts.append("【另一侧（主 Bot 私聊）的笔记本】\n" + str(_b2).strip())
        except Exception as e:
            print(f"[claude] 跨对话笔记本召回失败：{e}")
        blk = "\n\n".join(_parts)
        # 本模式没接笔记本工具，把「调用 angel_note_read 展开」那行去掉，
        # 不然等于让模型去调一个它没有的工具。
        blk = "\n".join(ln for ln in str(blk or "").split("\n") if "angel_note_read" not in ln)
        return blk.strip()

    def _build_messages(self, group_id: Any) -> List[Dict[str, Any]]:
        recent = self._recent(group_id)
        messages: List[Dict[str, Any]] = [{"role": "system", "content": self._system_prompt(group_id)}]
        # 【时间感知】群聊也生效：勾了 time_inject_group 就注入，不看群聊用的是哪一档人设
        if self.config.get("time_inject_group", True) and self.config.get("time_inject_enabled", True):
            try:
                from bot import build_time_block
                _tb = build_time_block(self.config)
                if _tb:
                    messages[0]["content"] += "\n\n" + _tb
            except Exception:
                pass

        # 【戳一戳】群里这段时间被戳的记录，捎给主对话（报一次就清空）
        try:
            import active_msg as _AM
            _am_p = _AM.prompt_block(getattr(self, "config", None))
            if _am_p:
                messages.append({"role": "system", "content": _am_p})
        except Exception:
            pass
        try:
            import poke as _POKE
            _pk = _POKE.take(getattr(self, "config", None), "group", str(group_id))
            if _pk:
                messages.append({"role": "system", "content": _pk})
        except Exception:
            pass

        # 群聊记录：连续的群友发言合并成一段「昵称：内容」记录，AI 自己的发言作为 assistant
        buf: List[str] = []

        def flush():
            if buf:
                messages.append({"role": "user", "content": "\n".join(buf)})
                buf.clear()

        for m in recent:
            if m.get("role") == "assistant":
                flush()
                messages.append({"role": "assistant", "content": str(m.get("content") or "")})
            else:
                buf.append(f"{m.get('sender_name') or '群友'}：{m.get('content') or ''}")
        flush()
        if len(messages) == 1:
            messages.append({"role": "user", "content": "（群里暂时还没人说话）"})
        # 最后一条直接 @ 了它：明确要求必须回一句（光靠它自己判断容易漏掉）
        _marker = str(self._cfg("silence_marker") or "<silence />")
        if messages[-1].get("role") == "user" and "@你" in str(messages[-1].get("content") or ""):
            messages[0]["content"] += ("\n\n【这条 @ 了你】上面最后一条消息直接 @ 了你，"
                                       "你必须回一句，不能输出 " + _marker + "。")
        # 群里发过的图：挂到最后那条 user 消息上，让它真能看到
        try:
            if messages and messages[-1].get("role") == "user":
                _its = self._imgs_for(group_id, str(messages[-1].get("content") or ""))
                if _its:
                    self._attach_imgs(messages, _its)
        except Exception:
            pass
        # 【破限】硬提醒贴到最后一条消息尾部（离模型生成位置最近，实测这样才拉得回来），
        # 防截断块单独成一条 system 消息、钉在请求最底部 —— 与主 Bot 链路同序。
        _rc = self._preset_recency()
        if _rc and messages:
            _last = dict(messages[-1])
            if not isinstance(_last.get("content"), list):
                _last["content"] = str(_last.get("content") or "") + "\n\n" + _rc
                messages[-1] = _last
        _byp = self._preset_bypass()
        if _byp:
            messages.append({"role": "system", "content": _byp})
        return messages

    # ---------------- API 调用 ----------------
    def endpoint(self) -> str:
        base = str(self._cfg("base_url") or "").strip() or str(self.config.get("llm_base_url") or "").strip()
        base = base.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def model_name(self) -> str:
        return str(self._cfg("model") or "").strip() or str(self.config.get("llm_model") or "").strip()

    def _api_key(self) -> str:
        return (str(self._cfg("api_key") or "").strip()
                or str(self.config.get("llm_api_key") or "").strip())

    async def _call_api(self, messages: List[Dict[str, Any]]) -> str:
        try:
            import mood as _MO4
            _mb4 = _MO4.prompt_block(getattr(self, 'config', None))
            try:
                import coyote as _CY4
                _cb4 = _CY4.prompt_block()
                if _cb4:
                    _mb4 = (_mb4 + "\n\n" + _cb4) if _mb4 else _cb4
            except Exception:
                pass
            if _mb4:
                messages = list(messages)
                if messages and messages[0].get('role') == 'system':
                    messages[0] = dict(messages[0])
                    messages[0]['content'] = str(messages[0].get('content') or '') + '\n\n' + _mb4
                else:
                    messages.insert(0, {'role': 'system', 'content': _mb4})
        except Exception:
            pass
        payload = {
            "model": self.model_name(),
            "messages": messages,
            "temperature": float(self._cfg("temperature", 0.8) or 0.8),
        }
        # 预设要求的正文工具要真的注册进去：<censorship_bypass> 要求「所有输出都走工具」，
        # 工具不存在的话模型只能退回纯文本，破限就是空的。
        _tool = self._preset_tool_def()
        if _tool:
            payload["tools"] = [_tool]
            payload["tool_choice"] = "auto"
        # 流式传输：长连接持续有数据，绕开「长时间无响应」被中转/CF 掐断
        if bool(self._cfg("stream", False)):
            payload["stream"] = True
        timeout = int(self._cfg("timeout", 90) or 90)
        headers = {"Authorization": f"Bearer {self._api_key()}", "Content-Type": "application/json"}
        # 让顶部「查看实际发送 Prompt」面板也能看到群聊链路的真实请求
        try:
            import bot as _BOT
            _BOT.last_debug_info.update({
                "request_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model": payload.get("model"),
                "endpoint": self.endpoint(),
                "messages": messages,
                "temperature": payload.get("temperature"),
                "tools": payload.get("tools"),
                "tool_choice": payload.get("tool_choice"),
                "raw_response": None,
                "source": "群聊",
                "jailbreak_mode": ("群聊+预设破限" if self._preset_on() else "群聊（不破限）"),
            })
        except Exception:
            pass
        # 【调试模式】把群聊链路真正发出去的报文打到控制台（不改上下文行为）
        if bool(getattr(self, "config", {}).get("debug_mode", False)):
            try:
                import bot as _BOT5
                await self.log("TEST", "[调试模式] 群聊发往 API 的完整请求报文：\n"
                               + json.dumps(_BOT5.redact_vision(payload), ensure_ascii=False, indent=2))
            except Exception as _e:
                await self.log("WARN", f"[调试模式] 群聊请求报文打印失败: {_e}")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(self.endpoint(), headers=headers, json=payload) as r:
                if r.status != 200:
                    body = await r.text()
                    raise RuntimeError(f"HTTP {r.status}: {body[:300]}")
                if payload.get("stream"):
                    data = await _collect_stream(r)
                else:
                    body = await r.text()
                    data = json.loads(body)
        if bool(getattr(self, "config", {}).get("debug_mode", False)):
            try:
                await self.log("TEST", "[调试模式] 群聊 API 原始响应体（全量）：\n"
                               + json.dumps(data, ensure_ascii=False, indent=2))
            except Exception as _e:
                await self.log("WARN", f"[调试模式] 群聊响应打印失败: {_e}")
        try:
            _rm = str((data.get("model") or "")) if isinstance(data, dict) else ""
            if _rm:
                _rq = payload.get("model")
                _msg = "[模型核对] 请求 %s → 响应回报 %s" % (_rq, _rm)
                _msg += "" if str(_rq) == _rm else "  ⚠ 不一致！可能被中转换了模型"
                try:
                    await self.log("WARN" if str(_rq) != _rm else "INFO", _msg)
                except Exception:
                    print(_msg)
        except Exception:
            pass
        ch = (data.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        text = str(msg.get("content") or "")
        # 破限生效时正文落在工具调用的 content 参数里，把它取出来当正文
        _tool_used = ""
        for _tc in (msg.get("tool_calls") or []):
            try:
                _args = json.loads((_tc.get("function") or {}).get("arguments") or "{}")
            except Exception:
                _args = {}
            _c = str((_args or {}).get("content") or "").strip()
            if _c:
                text = (_c if not text.strip() else text + "\n\n" + _c)
                _tool_used = str((_tc.get("function") or {}).get("name") or "").strip() or "工具"
                break
        # 【思维链正则块】勾了就剥掉再发（默认勾选）。主 Bot 走 sanitize_reply，群聊这里对齐。
        if bool(self._cfg("strip_think_enabled", True)):
            try:
                from bot import sanitize_reply
                text = sanitize_reply(text)
            except Exception as _e:
                await self.log("WARN", f"[群聊] 思维链剥离失败（原样发送）: {_e}")
        # 勾了破限预设时，按主 Bot 的规矩报一下正文来自哪（工具 ✅ / 纯文本 ⚠️）
        if self._preset_on():
            try:
                _src_txt = (f"{_tool_used} 工具 ✅ 破限生效" if _tool_used
                            else "纯文本 content ⚠️ 破限未生效（模型没走工具）")
                await self.log("INFO", f"[群聊模型] API 响应成功！正文来自：{_src_txt}")
            except Exception:
                pass
        try:
            import bot as _BOT
            _BOT.last_debug_info["raw_response"] = json.dumps(data, ensure_ascii=False)[:6000]
        except Exception:
            pass
        if not text.strip() and str(msg.get("reasoning_content") or "").strip():
            # 只出思维链、正文为空：宁可沉默，也不把思考过程发进群
            await self.log("WARN", "[群聊] 模型只返回了 reasoning_content，正文为空 → 按沉默处理")
            return ""
        return text

    # ---------------- 给面板 / 接口用的辅助 ----------------
    def groups(self) -> List[Dict[str, Any]]:
        out = []
        try:
            for c in self.history.list_conversations():
                uid = c.get("user_id")
                msgs = self.history.get_messages(uid) or []
                out.append({"group_id": str(uid),
                            "name": c.get("sender_name") or "",
                            "count": len(msgs),
                            "last": str(msgs[-1].get("content") or "")[:60] if msgs else "",
                            "last_time": str(msgs[-1].get("time") or msgs[-1].get("ts") or "") if msgs else ""})
        except Exception as e:
            print(f"[claude] 群列表读取失败：{e}")
        return out

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "bot_name": self.bot_name() or "克劳德",
            "model": self.model_name(),
            "base_url": str(self._cfg("base_url") or self.config.get("llm_base_url") or ""),
            "has_own_key": bool(str(self._cfg("api_key") or "").strip()),
            "groups_filter": str(self._cfg("groups") or ""),
            "context_limit": int(self._cfg("context_limit", 40) or 40),
            "silence_marker": str(self._cfg("silence_marker") or "<silence />"),
            "data_dir": str(self.data_dir.resolve()),
            "pending": self.pending_count(),
            "busy": self.busy_groups(),
            "stats": dict(self.stats),
            "sent": self._sent[:10],
            "groups": self.groups(),
        }
