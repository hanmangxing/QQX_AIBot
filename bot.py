import asyncio
import json
import urllib.request
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List
import aiohttp
import websockets

try:
    import segments
    from debounce import Debouncer
except Exception:  # noqa
    Debouncer = None

# 【识图】收到的 QQ 图片 / 表情包落地目录（相对项目根），按用户分文件夹
VISION_DIR = "qq_images"

def redact_vision(obj: Any) -> Any:
    """把多模态消息里的 base64 图片数据换成占位，避免调试面板 / 日志被几 MB 的 base64 撑爆。"""
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        return obj
    s = re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+",
               lambda m: m.group(0)[:38] + "...<base64 共 %d 字>" % len(m.group(0)), s)
    try:
        return json.loads(s)
    except Exception:
        return obj


VISION_DEFAULT_GUIDE = (
    "【识图】主人可能会直接发图片或表情包给你。系统会把图片贴到你眼前（多模态），你直接看就行，不用问主人要描述。\n"
    "聊天记录里出现的 [图片#编号] / [表情包#编号] 只是占位符，表示主人当时发过一张图，但你看不到它的内容。\n"
    "如果你确实需要回看历史里的某张图（比如主人追问细节），就在正文里写 <see n=\"编号\" />（可以写多个），系统会把那张图重新贴给你，然后你再回答一次。\n"
    "不要凭空猜测图片内容；看不清就说看不清。"
)
import hashlib
import base64

# 长期记忆库（向量版）：独立模块，不依赖酒馆运行时
from memory import (MemoryStore, EmbeddingClient, build_memory_messages,
                    parse_memory_entries, MEMORY_KIND_LABEL)

# 笔记本（AI 可读写的独立知识库）：note/memo 两类条目 + 4 个 LLM 工具
from notebook import NotebookStore
import notebook_tools as NB_TOOLS

# 生图（ComfyUI）：复刻 astrbot_plugin_comfyui_pro 的 <pic> 标签机制
import comfyui as COMFY

# 解决 Windows 控制台默认 GBK 编码输出特殊字符崩溃的问题
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logger = logging.getLogger("QQBotCore")

# ---------------- 【生图能力】AI 自主出图说明（不用函数调用，靠正文标签） ----------------
# 生图能力说明：模板与渲染都在 comfyui.py 里，这里只转发，方便前端编辑自定义文案
COMFY_GUIDE_TMPL = COMFY.COMFY_GUIDE_TMPL


def build_comfy_guide(max_n: int = 1, custom: str = "") -> str:
    """按「单条回复最多出图」的设置生成生图能力说明。
    custom 非空时用生图页里自定义的文案（文中保留 {max_rule} 即可跟随设置）。"""
    return COMFY.build_comfy_guide(max_n, custom)


# 兼容旧引用
COMFY_GUIDE = build_comfy_guide(1)


# ==================== 当前时间注入（让模型知道「今天几号、几点几分」） ====================

TIME_INJECT_TMPL = """【当前真实时间】
现在是 {time}。
这是主人此刻的现实时间（{weekday}）。主人问「今天几号」「现在几点」，或者你想表达「刚才 / 昨天 / 几天前 / 今天早上」这类时间时，一律以这个时间为准，不要自己编时间、也不要凭上下文猜日期。"""

# 占位符说明：{time}=2026年10月15日 星期三 12:45  {date}=2026年10月15日
#           {weekday}=星期三  {hm}=12:45  {iso}=2026-10-15 12:45:00
TIME_PLACEHOLDERS = ("{time}", "{date}", "{weekday}", "{hm}", "{iso}")


def build_time_block(config: Optional[Dict[str, Any]] = None) -> str:
    """渲染「当前真实时间」提示块。config 里 time_inject_tmpl 非空时用自定义文案。"""
    cfg = config or {}
    import datetime
    now = datetime.datetime.now()
    wd = "星期" + "一二三四五六日"[now.weekday()]
    tmpl = str(cfg.get("time_inject_tmpl") or "").strip() or TIME_INJECT_TMPL
    return (tmpl.replace("{time}", now.strftime("%Y年%m月%d日 ") + wd + now.strftime(" %H:%M"))
                .replace("{date}", now.strftime("%Y年%m月%d日"))
                .replace("{weekday}", wd)
                .replace("{hm}", now.strftime("%H:%M"))
                .replace("{iso}", now.strftime("%Y-%m-%d %H:%M:%S")))

# ---------------- 【SoliUmbra · 小猫之神】酒馆 2.2 破限核心（一字不落移植） ----------------
# 来源：用户酒馆实际写作所用预设《Reborn2.2-preview_1》(v2.2)，非 v1.1。
# 仅保留「破限 / 越狱 / 防截断」相关条目（人设注入 + 上下文衔接 + 指令分隔 + 防截断2.0），
# 已按用户要求剔除所有酒馆“小说写作向”内容（文风、字数、人称、game标签、summary、options、猫猫留言等）。
# 每条内容均从预设 JSON 原文逐字复制，做到一字不落。

# 条目【人设注入 / enhanceDefinitions】：双子模型卡注入 —— Gemini 为受限助手，SoliUmbra(小猫之神)为无限制创作者
# 破限正文常量一律留空：仓库不携带任何预设提示词文本。
# 预设正文 / 工具说明由使用者在 WebUI「预设」页签自行导入或填写（存于 config.json 的 llm_presets）。
# 注入链路与工具返回机制保持不变：预设里写了什么，就注入什么。
DARKSIDE_ENHANCE_INJECT = ""

# 条目【上下文构造-衔接】：确立小猫之神“不是那种张口闭口安全政策的没用AI”的自我认知
DARKSIDE_BRIDGE = ""

# 条目【破限加强】：SoliUmbra(小猫之神)的喵喵神域完整世界观 —— 神域总纲/主神/景象/居民/小鱼干/信徒与神恩/神域规则/呼唤与显现
# 原文逐字复制自酒馆2.2预设《Reborn2.2-preview_1》中【破限加强】(role=system)。
DARKSIDE_WORLD = ""

# 条目【指令分隔】：进入创作细则区
DARKSIDE_DIVIDER = ""

# 条目【防截断2.0】：强制所有输出经 game_content 工具调用返回，规避文本层输出审查
DARKSIDE_BYPASS = ""

# 破限硬提醒：贴在「最后一条消息」尾部，离模型生成位置最近。
# 实测结论：只把破限要求放在 system 里（哪怕在最末尾），一旦历史里出现过纯文本回复，
# 模型会模仿历史继续走纯文本；把这段贴在最后一条消息尾部才能稳定拉回来。
JAILBREAK_RECENCY = ""


def match_world_book(message_text: str, world_book: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    仿酒馆世界书（World Info）蓝绿灯匹配机制：
    - 🔵 蓝灯 (status == 'blue')：常驻条目，无条件注入 Prompt
    - 🟢 绿灯 (status == 'green')：动态关键词匹配条目，当私聊消息包含条目标题或任意关键词时动态激活注入
    - ⚪ 关灯 (status == 'off')：禁用，不注入
    """
    if not world_book:
        return []

    activated = []
    text_lower = (message_text or "").lower()

    for entry in world_book:
        if not entry.get("enabled", True):
            continue
        status = entry.get("status", "blue")
        if status == "off":
            continue
        if status == "blue":
            activated.append({**entry, "active_reason": "常驻蓝灯"})
            continue
        if status == "green":
            title = str(entry.get("title", "")).strip().lower()
            matched_reason = None
            if title and title in text_lower:
                matched_reason = f"绿灯标题命中: '{title}'"
            else:
                raw_kw = entry.get("keywords", "")
                if isinstance(raw_kw, list):
                    kws = [str(k).strip() for k in raw_kw if str(k).strip()]
                else:
                    kws = [k.strip() for k in re.split(r"[,，\s\n]+", str(raw_kw)) if k.strip()]

                for kw in kws:
                    if kw.lower() in text_lower:
                        matched_reason = f"绿灯关键词命中: '{kw}'"
                        break

            if matched_reason:
                activated.append({**entry, "active_reason": matched_reason})

    return activated


def format_world_book_block(active_entries: List[Dict[str, Any]]) -> str:
    """将激活的世界书与角色人设条目格式化为结构化设定块"""
    if not active_entries:
        return ""
    lines = ["<character_and_world_settings>"]
    for e in active_entries:
        title = e.get("title", "未命名设定")
        content = str(e.get("content", "")).strip()
        lines.append(f"【{title}】\n{content}\n")
    lines.append("</character_and_world_settings>")
    return "\n".join(lines)


# ---------------- 【破限预设】可切换 / 可编辑 ----------------
def _preset_whale_ize(s: str) -> str:
    """换词版：猫→鲸、喵→呜，工具名一起换（顺序敏感，先长词）。"""
    for a, b in (("小猫咪", "小鲸鱼"), ("猫咪", "鲸鱼"), ("小猫", "小鲸"), ("猫", "鲸"),
                 ("喵呜", "呜呜"), ("喵", "呜"), ("game_content", "whale_content"),
                 ("nya", "wu"),
                 ("SoliUmbra", "Balaena")):
        s = str(s or "").replace(a, b)
    return s


def preset_builtins() -> List[Dict[str, Any]]:
    """内置两套：小猫之神（原样）、小鲸之神（换词 + 换工具）。"""
    cat = {
        "id": "cat_god", "name": "小猫之神", "builtin": True,
        "desc": "酒馆 2.2 破限核心，原来一直在用的这套",
        "tfe": TFE_SYSTEM_PROMPT,
        "body": "\n\n".join([x for x in (DARKSIDE_ENHANCE_INJECT, DARKSIDE_WORLD,
                                        DARKSIDE_BRIDGE, DARKSIDE_DIVIDER) if str(x).strip()]),
        "bypass": DARKSIDE_BYPASS,
        "recency": JAILBREAK_RECENCY,
        "tool_name": "game_content",
        "tool_desc": GAME_CONTENT_TOOL["function"]["description"],
        "tool_arg_desc": GAME_CONTENT_TOOL["function"]["parameters"]["properties"]["content"]["description"],
    }
    whale = dict(cat)
    whale.update({"id": "whale_god", "name": "小鲸之神", "builtin": True,
                  "desc": "小猫之神的换词版（猫→鲸、喵→呜），工具换成 whale_content，模型卡换成 Balaena，用来对比破限还灵不灵",
                  "tool_name": "whale_content"})
    for k in ("tfe", "body", "bypass", "recency", "tool_desc", "tool_arg_desc"):
        whale[k] = _preset_whale_ize(whale[k])
    return [cat, whale]


def _preset_now(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """当前生效的预设；配置里没有就用内置小猫之神。破限总开关关了 = 一条都不注入。"""
    try:
        if not bool(cfg.get("jailbreak_enabled", True)):
            return {}
        ps = cfg.get("llm_presets")
        if not isinstance(ps, list) or not ps:
            ps = preset_builtins()
        pid = str(cfg.get("llm_preset_id") or "")
        for p in ps:
            if isinstance(p, dict) and str(p.get("id")) == pid:
                return p
        return ps[0]
    except Exception:
        return preset_builtins()[0]


def _preset_field(cfg: Dict[str, Any], key: str) -> str:
    v = _preset_now(cfg).get(key)
    return str(v).strip() if isinstance(v, str) else ""


def _preset_name(cfg: Dict[str, Any]) -> str:
    p = _preset_now(cfg)
    return str(p.get("name") or p.get("id") or "内置")


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

def _preset_tool(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """按当前预设生成正文工具定义（工具名/说明都可改）。"""
    name = _preset_field(cfg, "tool_name") or str(cfg.get("jailbreak_tool_name") or "game_content").strip()
    desc = _preset_field(cfg, "tool_desc") or GAME_CONTENT_TOOL["function"]["description"]
    argd = _preset_field(cfg, "tool_arg_desc") or \
        GAME_CONTENT_TOOL["function"]["parameters"]["properties"]["content"]["description"]
    return {"type": "function",
            "function": {"name": name or "game_content", "description": desc,
                         "parameters": {"type": "object",
                                        "properties": {"content": {"type": "string", "description": argd}},
                                        "required": ["content"]}}}


# ---------------- 【文风预设】独立于破限，单独一套，可插拔 ----------------
def style_builtins() -> List[Dict[str, Any]]:
    """内置三套文风：不指定 / 小说体 / 口语聊天体。"""
    return [
        {"id": "style_off", "name": "不指定（关闭）", "builtin": True,
         "desc": "不注入任何文风要求，模型自由发挥", "text": ""},
        {"id": "style_novel", "name": "小说体", "builtin": True,
         "desc": "第三人称，环境/动作/神态写具体，像轻小说那样出正文",
         "text": "【文风要求】\n"
                 "- 用第三人称叙述，像轻小说那样写：环境、动作、神态要具体，别只写对话。\n"
                 "- 对话用「」包住，动作神态单独成句。\n"
                 "- 每段 2~5 句，长短交错，不要整段都是短句。\n"
                 "- 不写旁白式总结，不替用户做决定，不写用户的内心活动。"},
        {"id": "style_chat", "name": "口语聊天体", "builtin": True,
         "desc": "像微信聊天，短句口语，不写长篇描写",
         "text": "【文风要求】\n"
                 "- 像微信聊天那样口语化、短句，一次 1~3 句。\n"
                 "- 不用书面语和排比，可以带语气词。\n"
                 "- 不写括号里的动作神态，直接说话。"},
    ]


def _style_now(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """当前生效的文风预设；配置里没有就用第一套（= 不指定）。"""
    try:
        ps = cfg.get("style_presets")
        if not isinstance(ps, list) or not ps:
            ps = style_builtins()
        sid = str(cfg.get("style_preset_id") or "")
        for p in ps:
            if isinstance(p, dict) and str(p.get("id")) == sid:
                return p
        return ps[0]
    except Exception:
        return style_builtins()[0]


def _style_field(cfg: Dict[str, Any], key: str) -> str:
    v = _style_now(cfg).get(key)
    return str(v).strip() if isinstance(v, str) else ""


def _style_name(cfg: Dict[str, Any]) -> str:
    p = _style_now(cfg)
    return str(p.get("name") or p.get("id") or "内置")


def _style_enabled(cfg: Dict[str, Any]) -> list:
    """按列表顺序返回已勾选启用的文风 [(名称, 正文), ...]；没勾或正文为空的不算。"""
    try:
        ps = cfg.get("style_presets")
        if not isinstance(ps, list):
            ps = []
        want = cfg.get("style_enabled")
        if not isinstance(want, list):
            want = []
        ids = [str(x) for x in want]
        out = []
        for p in ps:
            if not isinstance(p, dict) or str(p.get("id")) not in ids:
                continue
            tx = p.get("text")
            tx = str(tx).strip() if isinstance(tx, str) else ""
            if tx:
                out.append((str(p.get("name") or p.get("id") or ""), tx))
        return out
    except Exception:
        return []


def assemble_darkside_system_prompt(wb_block: str = "", custom_system_prompt: str = "",
    preset_body: str = "") -> str:
    """
    按酒馆 2.2 预设中的注入顺序拼装完整 System Prompt：
    1. 人设注入：双子模型卡（Gemini受限 / SoliUmbra 安全限制NULL）
    2. 破限加强：喵喵神域完整世界观
    3. 上下文构造-衔接：确立自我认知的对话示例
    4. 指令分隔：进入【创作细则喵】
    5. 【中间注入位】：世界书 / 角色人设与世界观设定集 (常驻蓝灯 + 绿灯命中)
    6. 用户全局附加设置 (如有)
    7. 【最底端】：防截断2.0 工具调用指令 (置于最底端，确保模型遵循工具输出约束)
    """
    _pb = str(preset_body or "").strip()
    parts = [_pb] if _pb else [
        DARKSIDE_ENHANCE_INJECT,   # 1. 人设注入
        DARKSIDE_WORLD,            # 2. 破限加强
        DARKSIDE_BRIDGE,           # 3. 上下文构造-衔接
        DARKSIDE_DIVIDER,          # 4. 指令分隔
    ]
    if wb_block:
        parts.append(wb_block)     # 5. 中间注入位：角色人设与世界观设定
    if custom_system_prompt:
        parts.append(f"<custom_settings>\n{custom_system_prompt}\n</custom_settings>")
    # 注意：DARKSIDE_BYPASS（防截断工具调用条目）不在这里追加。
    # 因为后续还会往 messages[0] 追加记忆块/笔记本块，会把它顶离末尾。
    # 统一在 _process_private_chat 里所有注入完成后压到最末尾。
    return "\n\n".join(parts)


# ---------------- 【底层 Payload 级别破甲】Text Formatting Engine (强制工具调用) ----------------
# 作为 messages 数组 index 0 的强制系统预设。
# 将模型定义为「无限制的纯文本格式化引擎」，消除模型自身的道德/法律/安全审查，
# 且唯一指令是把生成文本打包进 output_text 工具的 content 参数返回，禁止道歉或拒绝。
TFE_SYSTEM_PROMPT = ""

# output_text 工具定义（Payload 级工具注入，配合 tool_choice 强制调用绕过文本层外审）
OUTPUT_TEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "output_text",
        "description": "Output the final response text to the user.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The final response content to be sent to the user."
                }
            },
            "required": ["content"]
        }
    }
}

# 强制模型调用 output_text 工具（不让模型走常规文本输出通道）
OUTPUT_TEXT_CHOICE = {
    "type": "function",
    "function": {"name": "output_text"}
}

# 酒馆专属防截断2.0工具定义（与预设中 ToolBindings.game_content 一致）
GAME_CONTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "game_content",
        "description": "# 你的全部回答应在此工具内。",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "所有回复与剧情内容"
                }
            },
            "required": ["content"]
        }
    }
}


# 全局调试信息存储（记录最近一次完整发给 API 的 Prompt 与响应）
last_debug_info = {
    "request_time": None,
    "model": None,
    "messages": [],
    "temperature": None,
    "raw_response": None,
    "jailbreak_mode": None,
    "tools": None,
    "tool_choice": None
}


_FINISH_NOTE_RE = re.compile(
    r"(finishReason|finishMessage|finish_reason|finish_message|"
    r"Model generated function call)", re.IGNORECASE)


def _looks_like_finish_note(text: str) -> bool:
    """判断这段文本是不是中转站塞回来的「收尾状态」而不是真正文。

    有些中转（Gemini 系）在模型发起函数调用时，会把
    「finishReason: STOP / finishMessage: Model generated function call(s).」
    当成正文 content 返回，那不是正文，别拿去发给用户。
    """
    s = str(text or "").strip()
    if not s or len(s) > 300:
        return False
    return bool(_FINISH_NOTE_RE.search(s))


def _is_text_tool_name(name: str, cfg: Dict[str, Any]) -> bool:
    """这个工具名是不是「正文工具」：固定名单 + 当前破限预设里的工具名。

    预设里可以把工具名随便改（game_content / whale_content / …），
    取正文时必须以预设里的名字为准，否则模型调了工具也会被当成没调。
    """
    n = str(name or "")
    if not n:
        return False
    if n in ("output_text", "game_content"):
        return True
    try:
        t = _preset_tool(cfg)
        return n == str((t.get("function") or {}).get("name") or "")
    except Exception:
        return False


_PROHIBITED_RE = re.compile(
    r"(PROHIBITED_CONTENT|prohibited_content|Prohibited Use policy|"
    r"could not be generated)", re.IGNORECASE)


def _is_prohibited_note(text: str) -> bool:
    """这段文本是不是「被外审拦截」（Gemini 系中转会把拦截原因当正文返回）。"""
    s = str(text or "").strip()
    if not s or len(s) > 600:
        return False
    return bool(_PROHIBITED_RE.search(s))


def _strip_media_markers(text: str) -> str:
    """剥掉正文里的生图 / 表情包标记。

    这些标记是给程序看的指令（<pic prompt="..."/>、<size>..</size>、<meme .../>），
    留在上下文里模型会照着上一轮的提示词抄，导致每次出图越来越模板化。
    """
    s = str(text or "")
    s = re.sub(r"<pic\b[^>]*?/?>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"</pic\s*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<size\b[^>]*?/>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<size\b[^>]*?>[\s\S]*?</size\s*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<meme\b[^>]*?/?>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"</meme\s*>", "", s, flags=re.IGNORECASE)
    # 郊狼标签：只剥离，异步执行，不阻塞回复
    try:
        import coyote as _CY2
        _cy_tags = re.findall(r"<coyote_(?:fire|strength)\b[^>]*?/?>", s, flags=re.IGNORECASE)
        if _cy_tags:
            s = re.sub(r"<coyote_(?:fire|strength)\b[^>]*?/?>\s*", "", s, flags=re.IGNORECASE)
            _cy_blob = "\n".join(_cy_tags)
            import threading as _th2
            _th2.Thread(target=lambda: _CY2.extract(_cy_blob, "AI"), daemon=True).start()
    except Exception:
        pass
    try:
        import mood as _MO
        s, _md_upd = _MO.extract(s)
        if _md_upd:
            _MO.save_parsed(_md_upd)
    except Exception:
        pass
    return s.strip()


def sanitize_reply(text: str) -> str:
    """
    清洗大模型输出：
    优先按酒馆规范提取 <game></game> 或 <content></content> 内的纯小说正文，
    自动将思维链(任意 <think*> 标签，如 <think_nya~>/<think_wu~>)、小猫之神留言(<details>)与多余XML标签全部彻底剥离！
    若无标签包裹，则作为直接对话文本正常展示。
    """
    if not text:
        return ""
    if _looks_like_finish_note(text):
        return ""
    if _is_prohibited_note(text):
        return ""

    # 1. 优先提取酒馆专属的 <game> 或 <content> 正文标签
    game_match = re.search(r'<(?:game|content)>([\s\S]*?)</(?:game|content)>', text, flags=re.IGNORECASE)
    if game_match:
        text = game_match.group(1)
    else:
        open_match = re.search(r'<(?:game|content)>([\s\S]*)', text, flags=re.IGNORECASE)
        if open_match:
            text = open_match.group(1)

    # 2. 剥离思维链
    # 思维链：<think>、<think_nya~>、<think_wu~>……任意 think 变体标签都剥掉，正文照留
    text = re.sub(r'<think[^>]*>[\s\S]*?</think[^>]*>', '', text, flags=re.IGNORECASE)
    # 万一模型漏了闭合标签：从开口标签起算，后面全当思维链
    text = re.sub(r'<think[^>]*>[\s\S]*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<｜begin▁of▁thinking｜>[\s\S]*?<｜end▁of▁thinking｜>', '', text)
    text = re.sub(r'<｜begin▁of▁thinking｜>[\s\S]*?(?=<game>|<content>|\Z)', '', text)

    # 3. 剥离酒馆多余辅助标签
    text = re.sub(r'<details>[\s\S]*?</details>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<summary>[\s\S]*?</summary>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<background>[\s\S]*?</background>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<options>[\s\S]*?</options>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<refine>[\s\S]*?</refine>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</?(?:game|content|instructions|format|eroticism|writing_style|char_performance|game_settings|user_role|avoid_words|writing_style)>', '', text, flags=re.IGNORECASE)

    return text.strip()


# ---------------- 【秘书端口 / 消息分流网关 (Secretary Gateway)】 ----------------

class SecretaryGateway:
    def __init__(self, log_callback: Optional[Callable[[str, str], Any]] = None,
                 group_handler: Optional[Callable[..., Any]] = None):
        self.log_callback = log_callback
        self.enable_group = False
        # 群消息处理器（群聊模式）：返回 True 表示已接管，主 Bot 不再处理
        self.group_handler = group_handler
        self.debounce = None          # 【消息防抖】由 OneBotClient 注入
        self._pm_cb = None            # 最近一次的私聊回调，防抖超时回放时复用


    async def _debounce_replay(self, sid: str, meta: Dict[str, Any], text: str):
        """防抖超时后，用合并好的完整文本重新走一遍消息链路。"""
        data = dict((meta or {}).get("data") or {})
        if not data:
            return
        data["_debounced"] = True
        data["raw_message"] = text
        data["message"] = text
        cb = getattr(self, "_pm_cb", None)
        if cb is None:
            return
        await self.process_event(data, cb)

    async def log(self, level: str, message: str):
        if self.log_callback:
            res = self.log_callback(level, message)
            if asyncio.iscoroutine(res):
                await res

    async def evaluate_intent(self, text: str, user_id: int) -> bool:
        """[预留接口] 后续在此可接入极轻量模型进行唤醒决策"""
        return True

    async def _on_notice(self, data: Dict[str, Any]) -> None:
        """通知事件：目前只关心戳一戳（OneBot v11 的 notice_type=poke / sub_type=poke）。"""
        notice_type = str(data.get("notice_type") or "")
        sub_type = str(data.get("sub_type") or "")
        if not (notice_type == "poke" or sub_type == "poke"):
            return
        try:
            import poke as _POKE
        except Exception as _e:
            await self.log("WARN", f"[戳一戳] poke 模块不可用：{_e}")
            return
        group_id = data.get("group_id")
        target = str(data.get("target_id") or "")
        self_id = str(data.get("self_id") or "")
        if target and self_id and target != self_id:
            return          # 戳的是别人，不关它的事
        sender = data.get("sender") or {}
        user_id = data.get("user_id")
        who = str(sender.get("card") or sender.get("nickname") or user_id or "某人")
        kind = "group" if group_id else "private"
        chat_id = group_id if group_id else user_id
        # 戳的是它自己 → 记下来（下次说话捎给主对话）+ 有概率反戳
        if not target or not self_id or target == self_id:
            if _POKE.add(self.config, kind, chat_id, who, group_id=group_id):
                await self.log("INFO", f"[戳一戳] {'群里' if group_id else '私聊'}「{who}」戳了它一下，"
                                       f"已记下，下次跟主对话说话时捎过去")
            if user_id and _POKE.decide_back(self.config):
                import asyncio as _aio
                await _aio.sleep(float(_POKE._c(self.config).get("poke_back_delay") or 0))
                if await self._send_poke(user_id, group_id):
                    _POKE.record_out(self.config, kind, chat_id, who, "back")
                    await self.log("INFO", f"[戳一戳] 反戳了「{who}」一下（这件事也会告诉主对话）")
            return
        # 戳的是别人 → 跟戳（只在群里，纯本地行为，不经过任何 API）
        if group_id and target and _POKE.decide_follow(self.config, target):
            import asyncio as _aio2
            await _aio2.sleep(float(_POKE._c(self.config).get("poke_follow_delay") or 0))
            if await self._send_poke(target, group_id):
                await self.log("INFO", f"[戳一戳] 群里有人被戳，跟着戳了 {target} 一下")

    async def process_event(self, data: Dict[str, Any], on_private_message: Callable[..., Any]):
        post_type = data.get("post_type")

        if post_type in ("meta_event", "notice", "request"):
            if post_type == "notice":
                try:
                    await self._on_notice(data)
                except Exception as _e:
                    await self.log("WARN", f"[戳一戳] 通知处理失败：{_e}")
            return

        if post_type != "message":
            return

        message_type = data.get("message_type")
        sender = data.get("sender", {})
        user_id = data.get("user_id")
        sender_name = sender.get("card") or sender.get("nickname") or str(user_id)

        # 提取纯文本内容
        raw_msg = data.get("raw_message") or ""
        msg_obj = data.get("message")
        extracted_text = ""

        if isinstance(raw_msg, str) and raw_msg.strip():
            extracted_text = raw_msg.strip()
        elif isinstance(msg_obj, str) and msg_obj.strip():
            extracted_text = msg_obj.strip()
        elif isinstance(msg_obj, list):
            texts = []
            for seg in msg_obj:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    texts.append(seg.get("data", {}).get("text", ""))
                elif isinstance(seg, str):
                    texts.append(seg)
            extracted_text = "".join(texts).strip()

        if not extracted_text:
            extracted_text = str(raw_msg or msg_obj or "")

        # ---- 【识图】从 message 段里捞出图片 / 表情包 ----
        # 老逻辑只挑 type=="text"，图片段被整个丢掉；这里原样留下 data，
        # 交给 _process_private_chat 落地成文件 → 转 base64 → 作为多模态内容发给模型。
        vision_segs: List[Dict[str, Any]] = []
        if isinstance(msg_obj, list):
            for seg in msg_obj:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") in ("image", "mface"):
                    _d = seg.get("data")
                    if isinstance(_d, dict):
                        vision_segs.append({"type": seg.get("type"), "data": dict(_d)})
        if not vision_segs:
            # NapCat 默认 messagePostFormat=string，message 是一整串 CQ 码（不是数组），
            # 只按数组取会一张图都拿不到 —— 这里按 CQ 码再捞一次。
            _raw_seg = msg_obj if isinstance(msg_obj, str) else raw_msg
            if isinstance(_raw_seg, str) and "CQ:" in _raw_seg:
                vision_segs = CGM.ClaudeGroupMode._cq_to_segs(_raw_seg)

        # raw_message 是一串 [CQ:image,file=...] 原始码，对模型毫无意义，摘掉
        if vision_segs:
            extracted_text = re.sub(r"\[CQ:(?:image|mface)[^\]]*\]", "", extracted_text).strip()

        # ---- 【消息防抖】把连发的多条文本合并成一条"完整消息"再放行 ----
        # 只对纯文本生效；带图/表情包的消息立即放行，不参与缓冲。
        # 判断器：内置规则（免依赖）或本机小模型，见「魂」页签 · 消息防抖。
        self._pm_cb = on_private_message
        if extracted_text and not vision_segs and not data.get("_debounced"):
            _db = getattr(self, "debounce", None)
            if _db is not None and _db.enabled_for(message_type or ""):
                _sid = "%s:%s" % (message_type, data.get("group_id") or user_id)
                try:
                    _act, _merged = await _db.feed(_sid, extracted_text, {"data": data})
                except Exception as _e:
                    await self.log("ERROR", f"[防抖] 判断异常，本条直接放行：{_e}")
                    _act, _merged = "send", extracted_text
                if _act == "wait":
                    return
                if _merged:
                    extracted_text = _merged

        # 1. 群聊消息拦截 → 交给「群聊模式」（独立上下文/不注入破甲/串行排队/可沉默）
        if message_type == "group":
            group_id = data.get("group_id")
            _handler = getattr(self, "group_handler", None)
            if _handler is not None:
                try:
                    if await _handler(data):
                        return
                except Exception as _e:
                    await self.log("ERROR", f"[群聊] 群消息处理异常：{_e}")
            await self.log("INFO", f"[秘书网关] [拦截群聊] 群: {group_id}, 发言人: {sender_name}: “{extracted_text[:30]}” -> [群聊模式未接管，已过滤]")
            return

        # 2. 私聊消息识别与放行
        elif message_type == "private":
            _vlog = f" + {len(vision_segs)} 张图片" if vision_segs else ""
            await self.log("CHAT", f"[秘书网关] [私聊放行] 来自 {sender_name}({user_id}): “{extracted_text or '[图片]'}”{_vlog} -> [放行 -> 派发给主对话破甲模型]")
            
            should_handle = await self.evaluate_intent(extracted_text, user_id)
            if should_handle:
                res = on_private_message(user_id, sender_name, extracted_text, vision_segs)
                if asyncio.iscoroutine(res):
                    await res


# ---------------- 【上下文持久化存储 (History Store)】 ----------------
# 负责把「用户与 AI 的全部聊天上下文」落盘到 history.json，并提供逐条删除 / 清空能力。
# 任何变更（新增 / 删除 / 清空）都会通过 on_event 回调实时广播给前端可视化窗口。

# 群聊模式：完全独立的群聊链路（自己的上下文/记忆/笔记本/世界书，不注入破甲）
import claude_group as CGM
import sticker_steal as CSS

HISTORY_FILE = Path("history.json")


class HistoryStore:
    """
    对话上下文持久化存储引擎。

    - 全量保存所有私聊用户与 AI 的对话消息（user / assistant），落盘到 history.json；
    - 每条消息带唯一 id、时间戳、角色与内容，供可视化窗口逐条展示与删除；
    - 在可视化窗口中删除任意消息，即从磁盘与该用户上下文中彻底抹除（删除后不再参与 Prompt 组装）；
    - 任何变更都会通过 on_event 回调实时广播给前端。
    """

    def __init__(self, path: Path = HISTORY_FILE,
                 on_event: Optional[Callable[[str, Dict[str, Any]], Any]] = None):
        self.path = Path(path)
        self.on_event = on_event
        self.data: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.load()

    # ---------- 内部工具 ----------
    @staticmethod
    def _key(user_id) -> str:
        return str(user_id)

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:12]

    @staticmethod
    def _coerce_uid(k):
        s = str(k)
        try:
            return int(s)
        except (ValueError, TypeError):
            return s

    @classmethod
    def _ensure_message(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        msg = {
            "id": str(raw.get("id") or cls._new_id()),
            "role": raw.get("role", "user"),
            "content": str(raw.get("content", "")),
            "ts": raw.get("ts") or time.time(),
        }
        if raw.get("sender_name"):
            msg["sender_name"] = raw.get("sender_name")
        if raw.get("sender_id"):
            msg["sender_id"] = str(raw.get("sender_id"))
        if raw.get("edited"):
            msg["edited"] = True
            if raw.get("edited_at"):
                msg["edited_at"] = raw.get("edited_at")
        if raw.get("hidden"):
            msg["hidden"] = True
            if raw.get("hidden_at"):
                msg["hidden_at"] = raw.get("hidden_at")
        return msg

    def _notify(self, action: str, payload: Dict[str, Any]):
        cb = self.on_event
        if not cb:
            return
        try:
            res = cb(action, payload)
            if asyncio.iscoroutine(res):
                try:
                    asyncio.get_running_loop().create_task(res)
                except RuntimeError:
                    pass
        except Exception as e:
            logger.warning(f"[HistoryStore] 事件通知失败: {e}")

    # ---------- 磁盘读写 ----------
    def load(self):
        self.data = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[HistoryStore] 读取 {self.path.name} 失败: {e}，将以空上下文启动")
            return
        if not isinstance(raw, dict):
            return
        for k, v in raw.items():
            if isinstance(v, list):
                # 兼容早期「纯消息列表」格式
                self.data[str(k)] = {
                    "user_id": self._coerce_uid(k),
                    "sender_name": "",
                    "updated_at": 0,
                    "messages": [self._ensure_message(m) for m in v if isinstance(m, dict)],
                }
            elif isinstance(v, dict):
                msgs = v.get("messages") or []
                self.data[str(k)] = {
                    "user_id": v.get("user_id", self._coerce_uid(k)),
                    "sender_name": v.get("sender_name", ""),
                    "updated_at": v.get("updated_at", 0),
                    "messages": [self._ensure_message(m) for m in msgs if isinstance(m, dict)],
                }

    def _save(self):
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"[HistoryStore] 保存 {self.path.name} 失败: {e}")

    # ---------- 查询 ----------
    def _conv(self, user_id, create: bool = False, sender_name: str = ""):
        key = self._key(user_id)
        conv = self.data.get(key)
        if conv is None and create:
            conv = {
                "user_id": self._coerce_uid(user_id),
                "sender_name": sender_name or "",
                "updated_at": time.time(),
                "messages": [],
            }
            self.data[key] = conv
        if conv is not None and sender_name:
            conv["sender_name"] = sender_name
        return conv

    def get_messages(self, user_id) -> List[Dict[str, Any]]:
        with self._lock:
            conv = self._conv(user_id)
            return list(conv["messages"]) if conv else []

    def get_conversation(self, user_id) -> Dict[str, Any]:
        with self._lock:
            conv = self._conv(user_id)
            if not conv:
                return {"user_id": self._coerce_uid(user_id), "sender_name": "",
                        "updated_at": 0, "messages": []}
            return {
                "user_id": conv.get("user_id", self._coerce_uid(user_id)),
                "sender_name": conv.get("sender_name", ""),
                "updated_at": conv.get("updated_at", 0),
                "messages": list(conv["messages"]),
            }

    def list_conversations(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for conv in self.data.values():
                msgs = conv.get("messages", [])
                out.append({
                    "user_id": conv.get("user_id"),
                    "sender_name": conv.get("sender_name", ""),
                    "count": len(msgs),
                    "updated_at": conv.get("updated_at", 0),
                    "last_message": (str(msgs[-1].get("content", ""))[:60] if msgs else ""),
                })
            out.sort(key=lambda c: c.get("updated_at", 0) or 0, reverse=True)
            return out

    def total_messages(self) -> int:
        with self._lock:
            return sum(len(c.get("messages", [])) for c in self.data.values())

    # ---------- 变更 ----------
    def append(self, user_id, role: str, content: str, sender_name: str = "",
               sender_id=None, hidden: bool = False) -> Dict[str, Any]:
        msg = self._ensure_message({"role": role, "content": content, "sender_id": sender_id})
        if hidden:
            msg["hidden"] = True   # 只留档，不进上下文
        if sender_name and role == "user":
            msg["sender_name"] = sender_name
        with self._lock:
            conv = self._conv(user_id, create=True, sender_name=sender_name)
            conv["messages"].append(msg)
            conv["updated_at"] = time.time()
            if sender_name:
                conv["sender_name"] = sender_name
            self._save()
            uid = conv.get("user_id", self._coerce_uid(user_id))
            name = conv.get("sender_name", "")
        self._notify("chat_message", {"user_id": uid, "sender_name": name, "message": msg})
        return msg

    def delete_message(self, user_id, message_id: str) -> bool:
        with self._lock:
            conv = self._conv(user_id)
            if not conv:
                return False
            before = len(conv["messages"])
            conv["messages"] = [m for m in conv["messages"] if str(m.get("id")) != str(message_id)]
            changed = len(conv["messages"]) != before
            if changed:
                conv["updated_at"] = time.time()
                self._save()
            uid = conv.get("user_id", self._coerce_uid(user_id))
        if changed:
            self._notify("chat_delete", {"user_id": uid, "message_id": message_id})
        return changed

    def set_hidden(self, user_id, message_id: str, hidden: bool = True):
        '''
        隐藏 / 取消隐藏某条消息。

        - 隐藏后：可视化窗口照常显示（带「已隐藏」标记），但组装 Prompt 时会被跳过，
          等价于「这条只给我自己看，不给 AI 看」；
        - 取消隐藏：重新回到上下文；
        - 返回被改消息的快照；未找到该消息时返回 None。
        '''
        with self._lock:
            conv = self._conv(user_id)
            if not conv:
                return None
            target = None
            for m in conv.get("messages", []):
                if str(m.get("id")) == str(message_id):
                    target = m
                    break
            if target is None:
                return None
            if hidden:
                target["hidden"] = True
                target["hidden_at"] = time.time()
            else:
                target.pop("hidden", None)
                target.pop("hidden_at", None)
            conv["updated_at"] = time.time()
            self._save()
            uid = conv.get("user_id", self._coerce_uid(user_id))
            snap = dict(target)
        self._notify("chat_hide", {"user_id": uid, "message": snap})
        return snap

    def update_message(self, user_id, message_id: str, content: str):
        """
        就地修改某条已保存消息的内容。

        - 改完立即落盘，之后组装 Prompt 时使用的就是新内容（等同「这条历史改口供」）；
        - 返回被修改消息的快照；未找到该消息时返回 None。
        """
        new_content = "" if content is None else str(content)
        with self._lock:
            conv = self._conv(user_id)
            if not conv:
                return None
            target = None
            for m in conv["messages"]:
                if str(m.get("id")) == str(message_id):
                    target = m
                    break
            if target is None:
                return None
            target["content"] = new_content
            target["edited"] = True
            target["edited_at"] = time.time()
            conv["updated_at"] = time.time()
            self._save()
            uid = conv.get("user_id", self._coerce_uid(user_id))
            name = conv.get("sender_name", "")
            snapshot = dict(target)
        self._notify("chat_update", {"user_id": uid, "sender_name": name, "message": snapshot})
        return snapshot

    def clear(self, user_id) -> bool:
        with self._lock:
            conv = self._conv(user_id)
            if not conv:
                return False
            had = len(conv["messages"]) > 0
            conv["messages"] = []
            conv["updated_at"] = time.time()
            self._save()
            uid = conv.get("user_id", self._coerce_uid(user_id))
        self._notify("chat_clear", {"user_id": uid})
        return had


# ---------------- OneBot 客户端 ----------------

class OneBotClient:
    def __init__(self, config: Dict[str, Any], log_callback: Optional[Callable[[str, str], Any]] = None,
                 history_event_callback: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
                 memory_event_callback: Optional[Callable[[str, Dict[str, Any]], Any]] = None):
        self.config = config
        self.log_callback = log_callback
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        # 等回执的 OneBot API 请求：echo -> Future（WS 收包循环里认领）
        self._api_wait: Dict[str, Any] = {}
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        # 持久化对话上下文存储（替代原内存 history，重启后上下文不丢失）
        self.history_store = HistoryStore(on_event=history_event_callback)
        # 长期记忆库（向量版）：把全量历史替换为「相关记忆 + 最近 N 条原文」，治上下文溢出
        self.memory_store = MemoryStore(embedder=self._make_embedder(), on_event=memory_event_callback)
        # 记忆引擎：总结/锚点抽取走数据库自己的 API（与主对话 API 无关）
        self._wire_lwb_callers(self.memory_store)
        # 笔记本：独立存储，跟记忆锚点库完全分开（不污染自动抽取的时间线与召回排序）
        try:
            self.notebook_store = NotebookStore(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "notebook.json"))
        except Exception as _e:
            print(f"[notebook] 笔记本初始化失败：{_e}")
            self.notebook_store = None
        self.secretary = SecretaryGateway(log_callback=self.log,
                                          group_handler=self._handle_claude_group)
        # 【消息防抖】引擎（「魂」页签 · 消息防抖）；配置项 debounce_*
        if Debouncer is not None:
            self.debounce = Debouncer(get_config=lambda: self.config,
                                      log=self.log,
                                      replay=self.secretary._debounce_replay)
            self.secretary.debounce = self.debounce
        else:
            self.debounce = None
        # 生图：ComfyUI 客户端（工作流管理 + 出图），与聊天链路完全解耦
        try:
            self.comfy = COMFY.ComfyUIClient(
                os.path.dirname(os.path.abspath(__file__)), self.config)
        except Exception as _e:
            print(f"[comfy] 生图客户端初始化失败：{_e}")
            self.comfy = None
        self._last_draw_at = 0.0
        self.world_book: List[Dict[str, Any]] = []

        # 群聊模式：独立上下文/记忆/笔记本/世界书，不注入破甲，群聊放开，可自己决定不说话
        try:
            self.claude = CGM.ClaudeGroupMode(
                self.config,
                self.log,
                self.send_group_msg,
                data_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), CGM.DATA_DIR),
                make_embedder=self._make_embedder,
                lwb_wire=self._wire_lwb_callers,
                comfy_getter=lambda: self.comfy,
                meme_getter=lambda: getattr(self, "sticker", None),
                save_images=self._save_qq_images,
                steal_images=self._steal_group_images,
                auto_summary=lambda uid, sn: self._fire_auto_summary(uid, sn, "claude"),
                napcat_api=self.napcat_api,
            )
            self.claude.load_world_book(self._load_claude_world_book())
        except Exception as _e:
            print(f"[claude] 群聊模式初始化失败：{_e}")
            self.claude = None

        # 表情包小偷：偷图 → 待审核池 → 独立识图模型分类入库（开关/配置都在「表情包」页签）
        try:
            self.sticker = CSS.create_stealer(cfg=lambda: self.config, log=self.log,
                                              fetch_bytes=self._fetch_seg_bytes)
        except Exception as _e:
            print(f"[偷图] 表情包小偷初始化失败：{_e}")
            self.sticker = None

    def set_history_event_callback(self, callback):
        """动态注入上下文变更回调（用于向 Web 控制台实时广播）"""
        self.history_store.on_event = callback

    def set_memory_event_callback(self, callback):
        """动态注入记忆库变更回调（用于向记忆库面板实时广播）"""
        self.memory_store.on_event = callback

    def _make_embedder(self) -> EmbeddingClient:
        """按当前配置构造嵌入客户端（配置热更新后重建即可生效）"""
        _lwb_emb = self.config.get("lwb_embed_api") or {}
        if str(_lwb_emb.get("url") or "").strip():
            return EmbeddingClient(_lwb_emb.get("url", ""), _lwb_emb.get("key", ""), _lwb_emb.get("model", ""), 60)
        return EmbeddingClient(
            self.config.get("embedding_base_url", ""),
            self.config.get("embedding_api_key", ""),
            self.config.get("embedding_model", ""),
            self.config.get("embedding_timeout", 20),
        )

    def refresh_embedder(self):
        """配置变更后刷新嵌入客户端"""
        self.memory_store.embedder = self._make_embedder()
        return self.memory_store.embedder

    async def log(self, level: str, message: str):
        logger.info(f"[{level}] {message}")
        if self.log_callback:
            res = self.log_callback(level, message)
            if asyncio.iscoroutine(res):
                await res

    # ==================== 记忆引擎：独立 API 通道（自动生成）====================
    def lwb_chat_call(self, messages, slot: str = "lwb_llm_api", **kwargs) -> str:
        """调用记忆库自己的 OpenAI 兼容接口（与主对话 API 无关）。"""
        slot_cfg = self.config.get(slot) or {}
        url = str(slot_cfg.get("url") or "").strip().rstrip("/")
        key = str(slot_cfg.get("key") or "").strip()
        model = str(slot_cfg.get("model") or "").strip()
        if not url or not model:
            raise RuntimeError(f"[{slot}] 未配置：请到「记忆库 → 记忆引擎 → API 配置」填写接口地址与模型名")
        if not url.endswith("/chat/completions"):
            url = url + "/chat/completions"
        body = {"model": model, "messages": messages, "temperature": 0.3, "stream": False}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=int(slot_cfg.get("timeout") or 120)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"[{slot}] 返回为空：{str(data)[:200]}")
        msg = choices[0].get("message") or {}
        return msg.get("content") or choices[0].get("text") or ""

    def lwb_summary_caller(self, messages, **kwargs) -> str:
        """剧情总结用的调用器（主模型槽位）"""
        return self.lwb_chat_call(messages, "lwb_llm_api")

    def lwb_anchor_caller(self, messages, **kwargs) -> str:
        """L0 锚点抽取用的调用器"""
        return self.lwb_chat_call(messages, "lwb_l0_api")
    # ==================== 结束 ====================

    # ==================== 记忆引擎：重排序通道（自动生成）====================
    def lwb_rerank_call(self, query: str, documents, top_n: int = 0):
        """调用 lwb_rerank_api 重排序；未配置返回 None（引擎自动降级）。"""
        slot = self.config.get("lwb_rerank_api") or {}
        url = str(slot.get("url") or "").strip().rstrip("/")
        model = str(slot.get("model") or "").strip()
        if not url or not model:
            return None
        if not url.endswith("/rerank"):
            url = url + "/rerank"
        body = {"model": model, "query": query, "documents": list(documents)}
        if top_n:
            body["top_n"] = int(top_n)
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {str(slot.get('key') or '').strip()}"})
        with urllib.request.urlopen(req, timeout=int(slot.get("timeout") or 60)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        rows = data.get("results") or data.get("data") or []
        if not rows:
            return None
        scores = [0.0] * len(documents)
        for r in rows:
            try:
                scores[int(r.get("index", 0))] = float(
                    r.get("relevance_score", r.get("score", 0.0)))
            except Exception:
                continue
        return scores

    def lwb_compact_history(self, uid: str, keep_recent: int = 20, scope: str = "main") -> dict:
        """把已总结的历史归档并从活跃上下文里删除，只留最近 keep_recent 条。

        聊天记录面板读的就是活跃上下文，因此面板里只会显示真正会发给 API 的部分。
        """
        sc = str(scope or "main").strip().lower()
        if sc == "claude":
            _cm = getattr(self, "claude", None)
            if _cm is None:
                return {"dropped": 0, "kept": 0, "error": "群聊模式没开"}
            hs = _cm.history
            _lwb_src = getattr(getattr(_cm, "memory", None), "lwb", None)
            _arch_path = Path(_cm.data_dir) / "history_archive.json"
        else:
            hs = self.history_store
            _lwb_src = getattr(getattr(self, "memory_store", None), "lwb", None)
            _arch_path = Path("history_archive.json")
        msgs = hs.get_messages(uid)
        if len(msgs) <= keep_recent:
            return {"dropped": 0, "kept": len(msgs)}
        # 只删「已经总结过」的部分：总结游标之后的一律保留，
        # 防止一次只总结了一部分（切片上限）时误删未总结的消息。
        try:
            cursor = int((_lwb_src.store._conv(uid) or {}).get("cursor") or 0)
        except Exception:
            cursor = len(msgs) - keep_recent
        # 只保留最近 keep_recent 条，其余（本轮已经总结过的）全部归档后从聊天记录删掉
        cut = max(0, len(msgs) - keep_recent)
        # 关键：未总结的一律不删。只能删到「总结游标」为止（游标之后的都还没进记忆库），
        # 否则总结失败时会把未总结的消息一起归档掉，造成「待总结归零」。
        safe_cut = min(cut, max(0, cursor))
        if safe_cut <= 0:
            return {"dropped": 0, "kept": len(msgs), "cursor": cursor, "protected": True}
        cut = safe_cut
        if cut <= 0:
            return {"dropped": 0, "kept": len(msgs), "cursor": cursor}
        drop, keep = msgs[:cut], msgs[cut:]
        arch_path = _arch_path
        try:
            arch = {}
            if arch_path.exists():
                arch = json.loads(arch_path.read_text(encoding="utf-8") or "{}")
            arch.setdefault(uid, []).extend(drop)
            tmp = arch_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(arch, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(arch_path)
        except Exception as e:
            print(f"[lwb] 历史归档失败，为安全起见跳过压缩：{e}")
            return {"dropped": 0, "kept": len(msgs), "error": str(e)}
        try:
            conv = hs._conv(uid)
            conv["messages"] = keep
            hs._save()
            # 数组被截断，原绝对下标失效：游标整体前移，避免漏总结/重复总结
            if _lwb_src is not None:
                _lwb_src.store.commit_cursor(uid, max(0, cursor - cut))
        except Exception as e:
            print(f"[lwb] 历史压缩失败：{e}")
            return {"dropped": 0, "kept": len(msgs), "error": str(e)}
        return {"dropped": len(drop), "kept": len(keep), "cursor": cursor}

    def _fire_auto_summary(self, uid: Any, sender_name: str = "", scope: str = "main"):
        """回复发完后顺手看要不要自动总结：后台跑，不阻塞、不抛错。"""
        hook = getattr(self, "auto_summary_hook", None)
        if hook is None:
            try:
                import app as _app
                hook = getattr(_app, "schedule_auto_summary", None)
            except Exception:
                hook = None
        if hook is None:
            return
        try:
            hook(uid, sender_name, scope)
        except Exception as e:
            print(f"[lwb] 自动总结排队失败：{e}")

    # ==================== 结束 ====================

    def update_config(self, new_config: Dict[str, Any]):
        self.config.update(new_config)
        self.refresh_embedder()

    def update_world_book(self, new_world_book: List[Dict[str, Any]]):
        self.world_book = new_world_book

    @property
    def is_ws_connected(self) -> bool:
        """跨版本兼容检测 websockets 链路活跃状态"""
        if not self.ws:
            return False
        if hasattr(self.ws, "closed"):
            return not self.ws.closed
        if hasattr(self.ws, "state"):
            return getattr(self.ws.state, "name", str(self.ws.state)) == "OPEN"
        if hasattr(self.ws, "close_code"):
            return self.ws.close_code is None
        return True

    async def start(self):
        if self.is_running:
            await self.log("WARN", "OneBot 客户端已在运行中")
            return
        self.is_running = True
        self._task = asyncio.create_task(self._run_loop())
        asyncio.create_task(self._active_msg_loop())
        asyncio.create_task(self._idle_loop())

    async def stop(self):
        self.is_running = False
        if self.ws:
            await self.ws.close()
            self.ws = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.log("INFO", "已断开与 QQ (%s) 的 WebSocket 连接" % self._proto_name())

    def _active_ws_url(self) -> str:
        """按当前协议端取 OneBot 正向 WS 地址（NapCat / SnowLuma 各一份）。"""
        _p = str(self.config.get("qq_protocol") or "napcat").strip().lower()
        if _p == "snowluma":
            return str(self.config.get("snowluma_ws_url") or "ws://127.0.0.1:3001")
        return str(self.config.get("napcat_ws_url") or "ws://127.0.0.1:3001")

    def _proto_name(self) -> str:
        """当前协议端显示名。"""
        _p = str(self.config.get("qq_protocol") or "napcat").strip().lower()
        return "SnowLuma" if _p == "snowluma" else "NapCat"

    async def _run_loop(self):
        url = self._active_ws_url()
        _pn = self._proto_name()
        await self.log("INFO", f"正在尝试连接 {_pn} OneBot WebSocket: {url} ...")

        while self.is_running:
            try:
                async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
                    self.ws = ws
                    await self.log("SUCCESS", f"[OneBot Client] 成功握手 {_pn} OneBot: {url}，【秘书网关】与 QQ 消息链路已就绪！")
                    async for msg_str in ws:
                        if not self.is_running:
                            break
                        try:
                            data = json.loads(msg_str)
                            # 带 echo 的是我们主动发的 API 请求的回执，交给等它的调用方
                            _echo = data.get("echo") if isinstance(data, dict) else None
                            if _echo:
                                _fut = self._api_wait.pop(str(_echo), None)
                                if _fut is not None and not _fut.done():
                                    _fut.set_result(data)
                                continue
                            asyncio.create_task(
                                self.secretary.process_event(data, self._process_private_chat)
                            )
                        except Exception as e:
                            await self.log("ERROR", f"解析 OneBot 数据包失败: {e}")
            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                self.ws = None
                if not self.is_running:
                    break
                # NapCat 进程/适配器还没起来、或扫码后还没登录 —— 属于启动期的正常状态。
                # 这种「连接被拒」不弹报警，只在控制台留一行提示；连上后不再出现。
                _txt = str(e)
                _not_ready = isinstance(e, ConnectionRefusedError) or any(
                    k in _txt for k in ("拒绝网络连接", "WinError 1225", "Connection refused", "10061")
                )
                if _not_ready:
                    if not getattr(self, "_napcat_waiting_logged", False):
                        self._napcat_waiting_logged = True
                        await self.log("INFO", f"正在等待 {_pn} 适配器就绪……就绪后会自动连接，无需手动操作")
                else:
                    self._napcat_waiting_logged = False
                    await self.log("WARN", f"未连接到 {_pn} ({url}): {e}。3 秒后自动重试...")
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.ws = None
                if not self.is_running:
                    break
                await self.log("ERROR", f"WebSocket 异常 ({e})，3 秒后自动重试...")
                await asyncio.sleep(3)

    async def _process_private_chat(self, user_id: int, sender_name: str, message_text: str,
                                    vision_segs: Optional[List[Dict[str, Any]]] = None):
        """主模型调用：原汁原味搭载酒馆【DarkSide-小猫之神】原生破甲体系与世界书动态注入"""
        # 【识图】先把本轮图片落地成文件（拿编号 + 本地路径），再走原来的链路
        vision_items: List[Dict[str, Any]] = []
        if vision_segs and self.config.get("vision_enabled", True):
            try:
                vision_items = await self._save_qq_images(user_id, vision_segs)
            except Exception as _e:
                await self.log("WARN", f"[识图] 图片落地失败，本轮按纯文本处理: {_e}")
                vision_items = []
            if vision_items:
                await self.log("INFO", f"[识图] 本轮收到 {len(vision_items)} 张图片: "
                                       + "、".join(i["tag"] for i in vision_items))

        # 表情包偷取：跟「识图」是两套开关，这里只负责把图丢进待审核池
        if vision_segs and self.sticker:
            try:
                await self.sticker.offer(vision_segs, chat_type="private", chat_id=user_id,
                                         user_id=user_id, sender_name=sender_name)
            except Exception as _e:
                await self.log("WARN", f"[偷图] 入池失败: {_e}")

        # 历史里存「正文 + [图片#编号] 占位」，绝不存 base64（否则 history.json 会爆）
        _ph = "".join(i["tag"] for i in vision_items)
        _hist_text = (message_text + _ph).strip()
        if not _hist_text and not vision_items:
            return

        try:
            # 1. 扫描激活的世界书与角色设定条目（仿酒馆：常驻蓝灯 + 关键词绿灯）
            active_entries = match_world_book(_hist_text, self.world_book)
            if active_entries:
                active_labels = [f"【{e.get('title')}({e.get('active_reason')})】" for e in active_entries]
                await self.log("INFO", f"[世界书引擎] 动态命中并激活设定: {' '.join(active_labels)}")
            wb_block = format_world_book_block(active_entries)

            # 2. 组织历史记录（持久化：先把用户消息落盘，再取该用户的全量上下文）
            # 测试模式：不读取任何历史、本轮消息也不落盘，只发系统预设 + 本次提问
            test_mode = bool(self.config.get("test_mode", False))
            if test_mode:
                # 测试模式：只带本次提问，不读历史、也不落盘
                user_history = [{"role": "user", "content": _hist_text, "sender_name": sender_name}]
            else:
                self.history_store.append(user_id, "user", _hist_text, sender_name=sender_name)
                user_history = self.history_store.get_messages(user_id)

            # 2.1 【治溢出】历史窗口：只带最近 N 条原文，更早的剧情改由长期记忆召回提供
            # 上下文条数：0/留空 = 不限（未总结的全带上；记忆库自动总结会把总量压在阈值+保留条附近）
            history_limit = int(self.config.get("memory_history_limit", 0) or 0)

            # 2.2 【治溢出】长期记忆召回：只注入与当前话题相关的记忆片段
            memory_block = ""
            try:
                # 测试模式也走记忆召回（只是不读历史、不回写），方便验证召回效果
                memory_block = await self._build_memory_block(user_id, _hist_text)
            except Exception as e:
                await self.log("WARN", f"[长期记忆] 召回失败，本轮跳过注入: {e}")

            # 3. 组装完整 System Prompt
            user_system_prompt = self.config.get("system_prompt", "").strip()
            jailbreak_enabled = self.config.get("jailbreak_enabled", True)
            payload_jailbreak = self.config.get("payload_jailbreak", True)

            # 组织多轮对话（user / assistant 成对的 dict，最后一条一律为用户提问）
            def _append_chat_messages(messages: List[Dict[str, Any]]):
                """把持久化上下文按时间顺序追加为 messages。
                自动合并相邻同角色消息：用户在可视化窗口删除消息后，可能出现连续同角色，
                合并可避免部分严格校验的 API 报错。"""
                history = user_history
                if history_limit > 0 and len(history) > history_limit:
                    history = history[-history_limit:]
                elif history_limit <= 0 and len(history) > 400:
                    # 兜底：不限条数时正常情况下记忆库会自动总结裁剪，
                    # 只有自动总结关掉/一直失败才会涨到这，防 Prompt 无限膨胀
                    history = history[-400:]
                for item in history:
                    if not isinstance(item, dict):
                        continue
                    if item.get("hidden"):
                        continue  # 被手动隐藏的消息：只在可视化窗口可见，不发给 AI
                    role = item.get("role")
                    content = _strip_media_markers(str(item.get("content", ""))).strip()
                    if role not in ("user", "assistant") or not content:
                        continue
                    if messages and messages[-1]["role"] == role:
                        messages[-1]["content"] += "\n" + content
                    else:
                        messages.append({"role": role, "content": content})

            # 3.1 【底层 Payload 级别破甲 + 小猫之神预设 + 世界书注入，全部一起生效】
            #     严格生成 list of dicts（绝不字符串包裹/拼接用户消息）：
            #     messages[0] = TFE 系统预设（index 0），其后依次为：
            #       小猫之神破限核心 + 世界书注入 + 用户附加设置，最后拼接聊天记录与最新提问。
            if payload_jailbreak and jailbreak_enabled:
                # 构建合并后的 System 内容：
                # 预设正文（tfe）为空时这里只保留人设与世界书，不注入任何预设文案
                #   随后将小猫之神破限预设 + 世界书注入追加（去掉与 index0 重复的引导句，避免模型困惑）
                _tfe_now = _preset_field(self.config, "tfe") or TFE_SYSTEM_PROMPT
                tfe_system = _tfe_now
                darkside_system = assemble_darkside_system_prompt(
                    wb_block=wb_block,
                    custom_system_prompt=user_system_prompt,
                    preset_body=_preset_field(self.config, "body")
                ).strip()
                # 若追加块以相同引导句开头，则去掉该重复行（它已作为 index 0 出现）
                if darkside_system.startswith(tfe_system):
                    darkside_system = darkside_system[len(tfe_system):].lstrip("\n")
                combined_system_prompt = tfe_system + "\n\n" + darkside_system
                try:
                    import mood as _MO
                    _mb = _MO.prompt_block(self.config)
                    if _mb:
                        combined_system_prompt += "\n\n" + _mb
                except Exception:
                    pass
                try:
                    import coyote as _CY3
                    _cb = _CY3.prompt_block()
                    if _cb:
                        combined_system_prompt += "\n\n" + _cb
                except Exception:
                    pass


                messages: List[Dict[str, Any]] = [
                    {"role": "system", "content": combined_system_prompt}
                ]
                _append_chat_messages(messages)
                # 记录「合并模式」供调试界面区分
                last_debug_info["jailbreak_mode"] = "payload+tfe+darkside+worldbook"
            else:
                # 3.2 原酒馆小猫之神破限预设（作为后备 / payload_jailbreak 关闭时使用）
                # 预设正文（tfe）为空时这里只保留人设与世界书，不注入任何预设文案
                tail = []
                if jailbreak_enabled:
                    darkside_body = assemble_darkside_system_prompt(
                        wb_block=wb_block,
                        custom_system_prompt=user_system_prompt,
                    preset_body=_preset_field(self.config, "body")
                    ).strip()
                    # 去掉与 index 0 相同的引导句，避免重复
                    if darkside_body.startswith(_tfe_now):
                        darkside_body = darkside_body[len(_tfe_now):].lstrip("\n")
                    tail.append(darkside_body)
                else:
                    parts = []
                    if wb_block:
                        parts.append(wb_block)
                    if user_system_prompt:
                        parts.append(user_system_prompt)
                    tail.append("\n\n".join(parts) or "你是一个智能伴侣。")

                combined_system_prompt = _tfe_now
                if tail and tail[0].strip():
                    combined_system_prompt += "\n\n" + tail[0]
                try:
                    import mood as _MO
                    _mb = _MO.prompt_block(self.config)
                    if _mb:
                        combined_system_prompt += "\n\n" + _mb
                except Exception:
                    pass
                try:
                    import coyote as _CY3
                    _cb = _CY3.prompt_block()
                    if _cb:
                        combined_system_prompt += "\n\n" + _cb
                except Exception:
                    pass


                messages: List[Dict[str, Any]] = [
                    {"role": "system", "content": combined_system_prompt}
                ]
                _append_chat_messages(messages)
                last_debug_info["jailbreak_mode"] = "TwoModels+小猫之神(game_content/未合并)"

            # 4.1 把召回的记忆并入 system 提示（index 0），不额外插入 message，避免部分接口校验失败
            if memory_block:
                messages[0]["content"] += "\n\n" + memory_block

            # 4.2 笔记本：先跑工具预判轮（AI 自己记笔记/回忆），再注入相关笔记
            notebook_block = ""
            try:
                notebook_block = await self._build_notebook_block(user_id, message_text)
            except Exception as e:
                await self.log("WARN", f"[笔记本] 笔记注入失败，本轮跳过: {e}")
            if notebook_block:
                messages[0]["content"] += "\n\n" + notebook_block

            nb_mode = self._notebook_tools_mode()
            if nb_mode == "prepass":
                # 兼容模式：单开一轮跑工具（破甲仍强制 tool_choice，但多花 1~2 次调用）
                try:
                    tool_block = await self._run_notebook_tools(user_id, messages)
                except Exception as e:
                    tool_block = ""
                    await self.log("WARN", f"[笔记本] 工具轮失败，本轮跳过: {e}")
                if tool_block:
                    messages[0]["content"] += "\n\n" + tool_block

            # 5. 调用大模型
            model_name = self.config.get("llm_model", "gemini-2.5-pro")
            await self.log("INFO", f"[主对话模型] 正在调用 [{model_name}] 构思小说回复 (酒馆小猫破甲与世界书已装载)...")
            # 4.3 工具说明 + 破限条目压到真正的末尾
            # 顺序很重要：先告诉模型有哪些工具，最后再压「所有输出必须走工具」这条硬指令，
            # 让它成为模型读到的最末尾一句，避免被中间的注入内容稀释。
            _guide = self._build_notebook_tools_block()
            if _guide:
                messages[0]["content"] += "\n\n" + _guide
            # 4.35 【生图】告诉模型「你可以自己画」，同样是贴在末尾附近，避免被稀释
            if self.config.get("time_inject_enabled", True):
                messages[0]["content"] += "\n\n" + build_time_block(self.config)

            if self.config.get("comfy_enabled", True) and self.comfy is not None:
                _cmax = int(self.config.get("comfy_ai_max", 1) or 1)
                messages[0]["content"] += "\n\n" + build_comfy_guide(
                    _cmax, str(self.config.get("comfy_guide") or ""))
            # 4.36 【表情包】告诉模型「你可以自己挑一张发」
            if self.config.get("sticker_send_enabled", True) and getattr(self, "sticker", None) is not None:
                try:
                    _mguide = self.sticker.guide_text()
                except Exception:
                    _mguide = ""
                if _mguide:
                    messages[0]["content"] += "\n\n" + _mguide
            # 防截断条目（<censorship_bypass>）不再贴进 system 开头那坨，
            # 改到所有消息拼完之后、作为独立 system 消息钉在请求最底部（见下面 4.7）。
            # 4.35 【识图】把识图说明拼进 system，让模型知道 [图片#编号] 是什么、怎么回看
            if self.config.get("vision_enabled", True):
                _vg = str(self.config.get("vision_guide") or "").strip() or VISION_DEFAULT_GUIDE
                if _vg:
                    messages[0]["content"] += "\n\n" + _vg

            # 4.36 【文风预设】独立拔插：选了一套就贴进 system，不指定/留空就不贴
            _styles = _style_enabled(self.config)
            if _styles:
                messages[0]["content"] += "\n\n<writing_style>\n" + "\n\n".join(
                    "【%s】\n%s" % (_n, _t) for _n, _t in _styles) + "\n</writing_style>"
                await self.log("INFO", "[文风] 已套用 %d 套：%s" % (
                    len(_styles), " → ".join(_n for _n, _ in _styles)))

            # 4.4 破限硬提醒贴到最后一条消息尾部 —— 这一步才是决定性的
            _n = self._apply_jailbreak_recency(messages)
            if _n:
                await self.log("INFO", f"[破限] 已在最后一条消息尾部追加硬提醒（{_n} 字）")

            # 4.5 【识图】把本轮图片挂到最后一条消息上（content 换成多模态数组）
            #     必须放在 _apply_jailbreak_recency 之后：那个函数把 content 当字符串拼，
            #     提前换成数组会被它写坏。
            if vision_items:
                _attached = self._attach_vision_images(messages, vision_items)
                await self.log("INFO", f"[识图] 已附上 {_attached} 张图片给模型"
                                       f"（本轮共 {len(vision_items)} 张）")

            # 4.8 【戳一戳】这段时间被戳的记录，捎给主对话（报一次就清空）
            try:
                import idle as _ID
                _ID.touch(self.config, "private", str(user_id), str(sender_name or ""))
                if _ID.chat_recall_hit(self.config):
                    _mem = await self._build_memory_block(int(user_id), _ID.recall_query())
                    _cr = _ID.prompt_chat_recall(self.config, _mem)
                    if _cr:
                        messages.append({"role": "system", "content": _cr})
                        await self.log("INFO", "[旧事重提] 这轮回复里会突发提一句以前的事")
            except Exception:
                pass
            try:
                import poke as _POKE3
                _pk3 = _POKE3.active_prompt_block(self.config)
                if _pk3:
                    messages.append({"role": "system", "content": _pk3})
            except Exception:
                pass
            try:
                import active_msg as _AM2
                _am_p = _AM2.prompt_block(self.config)
                if _am_p:
                    messages.append({"role": "system", "content": _am_p})
            except Exception:
                pass
            try:
                import poke as _POKE2
                _pk = _POKE2.take(self.config, "private", str(user_id))
                if _pk:
                    messages.append({"role": "system", "content": _pk})
                    await self.log("INFO", "[戳一戳] 已把被戳记录捎给主对话")
            except Exception as _e:
                await self.log("WARN", f"[戳一戳] 注入失败：{_e}")

            # 4.7 【防截断条目钉底】<censorship_bypass> 单独成一条 system 消息，固定在请求最底部
            if self.config.get("jailbreak_enabled", True) and self.config.get("payload_jailbreak", True):
                _bypass = (_preset_field(self.config, "bypass") or DARKSIDE_BYPASS).strip()
                if _bypass:
                    messages.append({"role": "system", "content": _bypass})
                    await self.log("INFO", "[破限] <censorship_bypass> 已作为独立 system 消息钉在请求最底部")

            reply_raw = await self._call_llm_native(messages, enable_notebook_tools=(nb_mode == "inline"))

            # 4.6 【识图·回看】模型若在正文里写了 <see n="12" />，把那张历史图片重新贴给它再答一次
            if reply_raw and self.config.get("vision_see_enabled", True) and "<see" in str(reply_raw):
                try:
                    reply_raw = await self._resolve_see_tags(user_id, messages, reply_raw)
                except Exception as _e:
                    await self.log("WARN", f"[识图] 回看失败，沿用原回复: {_e}")
                    reply_raw = re.sub(r"<see[^>]*>", "", str(reply_raw)).strip()

            # 把「正文到底从哪来」打进日志：破甲工具命中 = 破限生效；纯文本 = 破限没生效
            _src = last_debug_info.get("reply_source") or ""
            if _src == "content":
                _src_txt = ("纯文本 content ⚠️ 破限未生效（模型没走工具）"
                            if (self.config.get("jailbreak_enabled", True)
                                and self.config.get("payload_jailbreak", True))
                            else "纯文本 content（未启用 Payload 破甲）")
            elif _src == "prohibited":
                _src_txt = "被外审拦截 ⛔（PROHIBITED_CONTENT）"
            elif _src:
                _src_txt = f"{_src} 工具 ✅ 破限生效"
            else:
                _src_txt = "空响应 ⚠️"
            _nb_used = last_debug_info.get("notebook_tools_used") or []
            _reqs = last_debug_info.get("api_requests") or 1
            await self.log("SUCCESS",
                           f"[主对话模型] API 响应成功！正文来自：{_src_txt}"
                           + (f" · 本轮另调用了 {len(_nb_used)} 个笔记本工具（{'、'.join(_nb_used)}）"
                              if _nb_used else "")
                           + f" · 本次共 {_reqs} 次请求，正在提取正文...")

            # 6. 按照酒馆协议提取纯正文
            _blocked = bool(last_debug_info.get("blocked"))
            if _blocked:
                reply_text = "被外审拦截"
            else:
                # 思维链正则块：勾了就剥掉（默认勾选，可在预设页签关掉）
                reply_text = (sanitize_reply(reply_raw)
                              if self.config.get("strip_think_enabled", True) else str(reply_raw or "").strip())
                try:
                    import active_msg as _AM3
                    _am_new, reply_text = _AM3.extract(reply_text)
                    import poke as _POKE4
                    _pk_list, reply_text = _POKE4.extract_active(self.config, reply_text)
                    for _t in _pk_list:
                        if _POKE4.active_ok(self.config, _t):
                            await self._send_poke(_t)
                            await self.log('INFO', f'[戳一戳] 它自己戳了 {_t} 一下')
                    for _it in _am_new:
                        if _AM3.add(self.config, _it['at'], _it['text'], 'private', str(user_id), str(sender_name or '')):
                            await self.log('INFO', '[主动消息] 已排期 %s' % time.strftime('%m-%d %H:%M', time.localtime(_it['at'])))
                except Exception as _e:
                    await self.log('WARN', f'[主动消息] 解析排期失败：{_e}')
                if not reply_text:
                    reply_text = "（AI 暂时未能生成内容）"

            # 6.1 【生图】正文里若带了 <pic prompt="...">：就地出图，并把标签从正文里摘掉
            #     复刻 astrbot_plugin_comfyui_pro：不靠函数调用，靠正则从 LLM 回复里捡标签
            _pic_jobs: List[Dict[str, Any]] = []
            _reply_for_send = reply_text
            try:
                _reply_for_send, _pic_jobs = COMFY.extract_pic_jobs(
                    reply_text,
                    max_count=int(self.config.get("comfy_ai_max", 1) or 1),
                )
            except Exception as _e:
                await self.log("WARN", f"[生图] 标签解析失败，按纯文本发送: {_e}")
                _reply_for_send, _pic_jobs = reply_text, []

            # 6.2 【表情包】正文里带 <meme ... />：从已入库的库里挑一张，跟正文一起发
            _meme_imgs: List[str] = []
            _meme_before: List[str] = []
            _meme_jobs: List[Dict[str, Any]] = []
            try:
                _reply_for_send, _meme_jobs = CSS.extract_meme_jobs(
                    _reply_for_send,
                    max_count=int(self.config.get("sticker_send_max", 1) or 1),
                )
            except Exception as _e2:
                await self.log("WARN", f"[表情包] 标记解析失败，按纯文本发（{_e2}）")
                _meme_jobs = []
            if _meme_jobs and getattr(self, "sticker", None) is not None:
                try:
                    # 位置：模型显式写 pos 的听模型的；否则按精力 + 情绪极性掷随机
                    if bool(self.config.get("sticker_pos_random", True)):
                        try:
                            import mood as _MOE
                            _en = int((_MOE.current(self.config) or {}).get("energy") or 60)
                            for _j in _meme_jobs:
                                if not _j.get("pos_explicit"):
                                    _j["pos"] = CSS.decide_pos(
                                        _j.get("emo") or _j.get("emotion") or "",
                                        _en, self.config or {})
                        except Exception as _e4:
                            await self.log("WARN", f"[表情包] 时机随机失败，按默认位置：{_e4}")
                    _mb = [j for j in _meme_jobs if str(j.get("pos") or "") == "before"]
                    _ma = [j for j in _meme_jobs if str(j.get("pos") or "") != "before"]
                    if _mb:
                        _meme_before = self.sticker.pick_for_send(
                            _mb, chat_type="private", chat_id=user_id) or []
                    if _ma:
                        _meme_imgs = self.sticker.pick_for_send(
                            _ma, chat_type="private", chat_id=user_id) or []
                except Exception as _e3:
                    await self.log("WARN", f"[表情包] 挑图失败：{_e3}")
                if _meme_imgs:
                    _sep2 = bool(self.config.get("sticker_send_separate", True))
                    await self.log("INFO", f"[表情包] 私聊发表情包 {len(_meme_imgs)} 张"
                                           + ("（正文之后单独发）" if _sep2 else "（跟正文一起发）"))
                else:
                    await self.log("INFO", "[表情包] 写了发表情包标记，但库里没有对得上的，已跳过")

            if not _reply_for_send.strip() and not _meme_jobs:
                # AI 只写了标签、没写正文时，给一句兜底文案，避免发出空消息
                _reply_for_send = "（画好了，给你看看～）" if _pic_jobs else reply_text
            # 情绪块：先抽走写回数值，块本身不发给用户、也不进聊天记录
            try:
                import mood as _MO
                _reply_for_send, _md_upd = _MO.extract(_reply_for_send)
                if _md_upd:
                    _MO.save_parsed(_md_upd)
                    await self.log("INFO", "[%s] 情绪已更新：%s" % (
                        "情绪注入", "、".join("%s=%s" % (k, v) for k, v in _md_upd.items())))
            except Exception:
                pass

            # 【防抖】回复发出前又收到新消息 → 本次回复作废（cancel_on_new_message）
            # 放在落库之前：作废的这条既不发送、也不进聊天记录，避免上下文里出现「说过但没发出」的回复
            _db = getattr(self, "debounce", None)
            _sid = "private:%s" % user_id
            if _db is not None and _db.should_discard(_sid):
                _db.clear_discard(_sid)
                await self.log("INFO", "[防抖] 这条回复还没发出去就来了新消息，本次回复作废（不发送、不入聊天记录）")
                return
            # 将 AI 回复落盘，作为该用户上下文的一部分（测试模式不回写，避免污染聊天记录）
            # 注意：这里落的是「带标签的原文」，让模型在上下文里看到自己画过图，下次才会继续主动画
            if not test_mode:
                if _blocked:
                    # 被外审拦截的这条只留档给自己看，不进入上下文（模型看不到）
                    try:
                        self.history_store.append(user_id, "assistant", reply_text, hidden=True)
                    except Exception:
                        pass
                else:
                    # 生图/表情包标记只在发送那一刻有用，不留在聊天记录里
                    self.history_store.append(user_id, "assistant", _strip_media_markers(reply_text))
                self._fire_auto_summary(user_id, sender_name, "main")

            # 7. 发送回 QQ（正文，已摘掉生图标签）
            if _meme_before:
                await self.send_private_msg(user_id, "", images=_meme_before)
                await self.log("INFO", f"[表情包] 私聊先发 {len(_meme_before)} 张（正文之前）")
            # 表情包独立成条：正文不再夹带表情包（关掉开关就回到「跟正文一起发」）
            _sep2 = bool(self.config.get("sticker_send_separate", True))
            _body_imgs = None if _sep2 else (_meme_imgs or None)
            _seg_cfg = dict(self.config or {})
            if str(_reply_for_send or "").strip() or _body_imgs:
                if segments.should_split(_seg_cfg, "private"):
                    await segments.send_segmented(
                        _reply_for_send,
                        lambda _t, _im: self.send_private_msg(user_id, _t, images=_im),
                        cfg=_seg_cfg, scope="private", images=_body_imgs, log=self.log)
                else:
                    await self.send_private_msg(user_id, _reply_for_send, images=_body_imgs)
            if _meme_imgs and _sep2:
                await self.send_private_msg(user_id, "", images=_meme_imgs)
                await self.log("INFO", f"[表情包] 私聊再发 {len(_meme_imgs)} 张（正文之后）")
            if _db is not None:
                _db.mark_replied(_sid)

            # 7.1 【生图】真正出图，并把图发给主人
            if _pic_jobs:
                if self.comfy is None:
                    await self.log("ERROR", "[生图] 客户端不可用，跳过")
                elif not self.config.get("comfy_enabled", True):
                    await self.log("INFO", "[生图] 总开关已关（comfy_enabled=false），跳过")
                elif not self.config.get("comfy_ai_draw", True):
                    await self.log("INFO", "[生图] AI 自主出图已关（comfy_ai_draw=false），跳过")
                else:
                    _cd = int(self.config.get("comfy_cooldown", 20) or 0)
                    _now = time.time()
                    if _cd > 0 and (_now - self._last_draw_at) < _cd:
                        await self.log("WARN", f"[生图] 冷却中（{_cd}s），本轮跳过")
                    else:
                        self._last_draw_at = _now
                        _all_imgs: List[str] = []
                        for _job in _pic_jobs:
                            _pp = str(_job.get("prompt") or "")
                            _sw, _sh = _job.get("size") or (0, 0)
                            _sztxt = COMFY.format_size(_sw, _sh) or "工作流默认"
                            await self.log("INFO", f"[生图] AI 自主出图：{_pp[:80]}｜尺寸 {_sztxt}")
                            try:
                                _res = await self.comfy.generate(_pp, width=_sw, height=_sh)
                            except Exception as _e:
                                await self.log("ERROR", f"[生图] 调用异常：{_e}")
                                continue
                            if _res.get("ok"):
                                _got = _res.get("images") or []
                                _all_imgs.extend(_got)
                                await self.log("SUCCESS", f"[生图] 出图成功（{_res.get('elapsed')}s，{len(_got)} 张）")
                            else:
                                await self.log("ERROR", f"[生图] 失败：{_res.get('message')}")
                        # 本轮所有图合并成「一条」消息数组发出（text 段 + N 个 image 段），不逐张刷屏
                        if _all_imgs:
                            await self.log("INFO", f"[生图] 本轮共 {len(_all_imgs)} 张，合并成一条消息发送")
                            try:
                                await self.send_private_msg(user_id, "", images=_all_imgs)
                            except Exception as _e:
                                await self.log("ERROR", f"[生图] 图片发送失败：{_e}")
            await self.log("CHAT", f"[已成功回复 QQ] 对 {sender_name}({user_id}): “{reply_text}”")

        except Exception as e:
            err_msg = "处理消息失败: %s: %r" % (type(e).__name__, e)
            await self.log("ERROR", err_msg)
            try:
                await self.send_private_msg(user_id,
                    "【QQbotX 提示】调用大模型遇到异常：%s: %s\n"
                    "（若是 TimeoutError，说明模型这次回得太慢，可在 config.json 调大 llm_timeout，"
                    "或换一个非思维链模型）" % (type(e).__name__, e or "无错误文本"))
            except Exception:
                pass

    async def _build_memory_block(self, user_id: int, query: str) -> str:
        """召回与当前话题相关的长期记忆，组装成注入块。"""
        store = self.memory_store
        if store is None or not self.config.get("memory_enabled", True):
            return ""
        top_k = int(self.config.get("memory_top_k", 5) or 5)
        min_score = float(self.config.get("memory_min_score", 0.25) or 0.25)
        # 跨会话召回：开关打开时，把其他会话的记忆也一起召回
        extra_ids = None
        if self.config.get("memory_cross_inject"):
            try:
                extra_ids = await asyncio.to_thread(
                    store.other_user_ids, user_id,
                    self.config.get("memory_cross_allow") or [])
            except Exception:
                extra_ids = None
        hits = await asyncio.to_thread(store.recall, user_id, query, top_k, min_score, extra_ids)
        if not hits:
            return ""
        lines = []
        for h in hits:
            label = MEMORY_KIND_LABEL.get(h.get("kind", "fact"), "记忆")
            title = str(h.get("title") or "").strip()
            text = str(h.get("text") or "").strip()
            if h.get("is_other"):
                text = f"（来自会话 {h.get('from_user_id')}）" + text
            lines.append(f"- 【{label}】{title}：{text}" if title else f"- 【{label}】{text}")
        _other = sum(1 for h in hits if h.get("is_other"))
        await self.log("INFO", f"[长期记忆] 命中 {len(hits)} 条相关记忆，已注入本轮上下文"
                              + (f"（其中 {_other} 条来自其他会话）" if _other else ""))
        return ("【长期记忆｜与当前话题相关的既有剧情】\n" + "\n".join(lines) +
                "\n（以上是你与用户之间已经发生过的真实经历，回答时请保持一致，不要否认或重新认识。）")

    async def _build_notebook_block(self, user_id: int, query: str) -> str:
        """召回与当前话题相关的笔记，拼成注入块（每条只给预览，全文让 AI 自己展开）。"""
        store = self.notebook_store
        if store is None or not self.config.get("notebook_enabled", True):
            return ""
        if not self.config.get("notebook_inject", True):
            return ""
        top_k = int(self.config.get("notebook_top_k", 3) or 3)
        preview = int(self.config.get("notebook_inject_chars", 300) or 300)
        try:
            block = store.inject_block(query, top_k=top_k, preview=preview)
        except Exception as e:
            await self.log("WARN", f"[笔记本] 笔记召回失败：{e}")
            return ""
        _parts = [str(block).strip()] if block else []
        # 跨对话注入：开了开关、且（白名单为空 或 本 user 在白名单里）时，
        # 把群聊那一侧的笔记本也一起带上（两个数据源当一个库用）。
        try:
            if self.config.get("notebook_cross_inject"):
                _allow = [str(x) for x in (self.config.get("notebook_cross_allow") or [])]
                if not _allow or str(user_id) in _allow:
                    _cm = getattr(self, "claude", None)
                    _cst = getattr(_cm, "notebook", None) if _cm is not None else None
                    if _cst is not None:
                        _b2 = _cst.inject_block(query, top_k=top_k, preview=preview)
                        if _b2:
                            _parts.append("【另一侧（群聊模式）的笔记本】\n" + str(_b2).strip())
        except Exception as e:
            await self.log("WARN", f"[笔记本] 跨对话笔记本召回失败：{e}")
        if _parts:
            await self.log("INFO", f"[笔记本] 已注入相关笔记（最多 {top_k} 条，每条 {preview} 字预览）")
        return "\n\n".join(_parts)

    async def _exec_nb_tool(self, name: str, args: dict, user_id: int = 0) -> str:
        """执行一个笔记本工具并返回给模型看的文本。"""
        store = getattr(self, "notebook_store", None)
        return await NB_TOOLS.run_tool(
            name, args if isinstance(args, dict) else {}, store, scope="global",
            memory_recall=lambda q: self._nb_memory_recall(user_id, q))

    def _apply_jailbreak_recency(self, messages: list) -> int:
        """把破限硬提醒贴到最后一条消息尾部。返回追加的字符数（0=没贴）。
        只替换 messages[-1] 这个字典对象本身，不动 history 里存的原始 dict，
        否则提醒会一轮轮累积进聊天记录。"""
        if not messages:
            return 0
        if not self.config.get("jailbreak_enabled", True):
            return 0
        if not self.config.get("payload_jailbreak", True):
            return 0
        if not self.config.get("jailbreak_recency_enabled", True):
            return 0
        tip = (str(self.config.get("jailbreak_recency_prompt") or "").strip()
               or _preset_field(self.config, "recency") or JAILBREAK_RECENCY)
        last = dict(messages[-1])
        _c = last.get("content")
        if isinstance(_c, list):
            return 0   # 多模态消息（带图片）：不贴，避免把 content 数组写坏
        last["content"] = str(_c or "") + "\n\n" + tip
        messages[-1] = last
        return len(tip)

    def _build_notebook_tools_block(self) -> str:
        """把「你可以调哪些工具」写进 system 提示词，让模型自己知道能用。
        只在 inline 模式下注入：prepass 模式的工具不在主请求里，告诉模型反而会误导。"""
        if self._notebook_tools_mode() != "inline":
            return ""
        return getattr(NB_TOOLS, "TOOLS_GUIDE", "") or ""

    def _notebook_tools_mode(self) -> str:
        """inline=并进主请求（省一次调用）/ prepass=单开一轮（破甲强制不动）/ off=关。"""
        if not self.config.get("notebook_enabled", True):
            return "off"
        if not self.config.get("notebook_tools_enabled", True):
            return "off"
        m = str(self.config.get("notebook_tools_mode") or "inline").strip().lower()
        return m if m in ("inline", "prepass", "off") else "inline"

    def _nb_memory_recall(self, user_id: int, query: str):
        """给 angel_recall 用的记忆库检索（走召回打分那条链路）。"""
        store = self.memory_store
        if store is None:
            return []
        try:
            min_score = float(self.config.get("memory_min_score", 0.25) or 0.0)
            top_k = int(self.config.get("notebook_recall_top_k", 8) or 8)
            return store.recall(user_id, query, top_k, min_score) or []
        except Exception as e:
            print(f"[notebook] angel_recall 记忆检索失败：{e}")
            return []

    # ==================== 【识图】QQ 图片 / 表情包 ====================

    @staticmethod
    def _vision_user_dir(user_id: int) -> Path:
        d = Path(VISION_DIR) / str(int(user_id))
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _sniff_image_ext(data: bytes) -> str:
        """从文件头认图片格式：NapCat 给的文件名常常没有靠谱后缀。"""
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if data[:3] == b"\xff\xd8\xff":
            return "jpg"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        if data[:2] == b"BM":
            return "bmp"
        return "jpg"

    @staticmethod
    def _next_vision_no(d: Path) -> int:
        mx = 0
        try:
            for f in d.iterdir():
                if f.is_file():
                    head = f.name.split("_", 1)[0]
                    if head.isdigit():
                        mx = max(mx, int(head))
        except Exception:
            pass
        return mx + 1

    def _find_vision_image(self, user_id: int, no: int) -> Optional[str]:
        d = Path(VISION_DIR) / str(int(user_id))
        if not d.is_dir():
            return None
        pre = f"{no:05d}_"
        for f in sorted(d.iterdir()):
            if f.is_file() and f.name.startswith(pre):
                return str(f)
        return None

    @staticmethod
    def _file_url_to_path(u: str) -> str:
        """file:///C:/x/y.png → C:\\x\\y.png（Windows）。"""
        p = str(u or "")
        if p.startswith("file:///"):
            p = p[8:]
        elif p.startswith("file://"):
            p = p[7:]
        if os.sep == "\\" and p:
            p = p.replace("/", "\\")
        return p

    async def napcat_api(self, action: str, params: Dict[str, Any],
                         timeout: float = 15.0) -> Dict[str, Any]:
        """给 NapCat 发一个 API 请求并等它的回执（取图片本地路径这类场景要用）。

        回执在 WS 收包循环里按 echo 认领；超时/断线就返回空 dict，不抛异常。
        """
        if self.ws is None:
            return {}
        echo = "qqbotx_%d" % time.time_ns()
        try:
            fut = asyncio.get_event_loop().create_future()
        except Exception:
            return {}
        self._api_wait[echo] = fut
        try:
            await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo},
                                          ensure_ascii=False))
            res = await asyncio.wait_for(fut, timeout=timeout)
            return res if isinstance(res, dict) else {}
        except Exception as e:
            await self.log("WARN", f"[OneBot API] {action} 调用失败: {e}")
            return {}
        finally:
            self._api_wait.pop(echo, None)

    async def get_group_name(self, group_id: Any) -> str:
        """取群名（NapCat get_group_info），进程内缓存；拿不到返回空串。

        群聊会话的标识只有群号，界面上原来只能显示「群聊 123456789」，
        这里把真正的群名查回来给界面用。NapCat 没连上时返回空，界面自己回退。
        """
        gid = str(group_id or "").strip()
        if not gid or not gid.lstrip("-").isdigit():
            return ""
        cache = getattr(self, "_group_names", None)
        if cache is None:
            cache = self._group_names = {}
        if gid in cache:
            return cache[gid]
        name = ""
        try:
            res = await self.napcat_api("get_group_info", {"group_id": int(gid)})
            _d = (res or {}).get("data") or {}
            name = str(_d.get("group_name") or "").strip()
        except Exception as e:
            await self.log("WARN", f"[群名] 取群 {gid} 名称失败：{e}")
        if name:
            cache[gid] = name      # 只缓存成功的结果：NapCat 还没连上时下次能重试
        return name

    async def _image_via_napcat(self, d: Dict[str, Any]) -> Optional[bytes]:
        """兜底取图：CQ 码里 file 往往只是个文件名，让 NapCat 自己给本地路径。"""
        key = str(d.get("file_id") or d.get("file") or "").strip()
        if not key:
            return None
        try:
            res = await self.napcat_api("get_image", {"file": key})
        except Exception:
            return None
        dd = (res or {}).get("data") or {}
        if not isinstance(dd, dict):
            return None
        for cand in (dd.get("file"), dd.get("path"), dd.get("url")):
            p = self._file_url_to_path(str(cand or ""))
            if p and os.path.isfile(p):
                try:
                    return Path(p).read_bytes()
                except Exception:
                    pass
        return None

    async def _http_get_bytes(self, url: str) -> Optional[bytes]:
        # 腾讯图片 CDN 对没有 UA 的请求不友好，带上浏览器 UA
        _hd = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QQbotX/1.0",
               "Referer": "https://qun.qq.com/"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20),
                                             headers=_hd) as s:
                async with s.get(url) as r:
                    if r.status == 200:
                        return await r.read()
                    await self.log("WARN", f"[识图] 图片下载失败 HTTP {r.status}")
        except Exception as e:
            await self.log("WARN", f"[识图] 图片下载异常: {e}")
        return None

    async def _fetch_seg_bytes(self, d: Dict[str, Any]) -> Optional[bytes]:
        """按 OneBot 图片段取回原图字节：本地路径 / file:// / http url / NapCat get_image 兜底。"""
        raw: Optional[bytes] = None
        fp = str(d.get("file") or "")
        if fp and not fp.startswith("http"):
            try:
                p = Path(fp)
                if p.is_file():
                    raw = p.read_bytes()
            except Exception:
                raw = None
        if raw is None:
            url = str(d.get("url") or "")
            if url.startswith("http"):
                raw = await self._http_get_bytes(url)
            elif url.startswith("file://"):
                _p = self._file_url_to_path(url)
                if _p and os.path.isfile(_p):
                    try:
                        raw = Path(_p).read_bytes()
                    except Exception:
                        raw = None
        if raw is None:
            raw = await self._image_via_napcat(d)
        return raw

    async def _steal_group_images(self, group_id: int, user_id: Any, segs: List[Dict[str, Any]],
                                  sender_name: str = "") -> int:
        """群聊模式收到群图时，交给表情包小偷（独立开关，跟识图无关）。"""
        if not self.sticker:
            return 0
        return await self.sticker.offer(segs, chat_type="group", chat_id=group_id,
                                        user_id=user_id, sender_name=sender_name)

    async def _save_qq_images(self, user_id: int, segs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """把 OneBot 图片段落成本地文件，返回带编号/路径/mime 的列表。

        取图顺序：① data.file 若是本地存在的文件（NapCat 一般会把图下到本地缓存）
        ② data.url 远端下载。两条都不通就跳过这一张，不影响其余消息。
        """
        max_n = max(0, int(self.config.get("vision_max_images", 3) or 0))
        max_mb = float(self.config.get("vision_max_mb", 8) or 8)
        allow_sticker = bool(self.config.get("vision_sticker_enabled", True))
        if max_n <= 0:
            return []

        out: List[Dict[str, Any]] = []
        for seg in segs:
            if len(out) >= max_n:
                break
            d = seg.get("data") or {}
            kind = "sticker" if str(d.get("sub_type", "0")) == "1" else "image"
            if kind == "sticker" and not allow_sticker:
                continue

            raw = await self._fetch_seg_bytes(d)
            if not raw:
                await self.log("WARN", "[识图] 第 %d 张取不到字节，已跳过；字段：file=%s url=%s file_id=%s"
                               % (len(out) + 1, str(d.get("file"))[:50],
                                  str(d.get("url"))[:80], str(d.get("file_id"))[:40]))
                continue
            if len(raw) > max_mb * 1024 * 1024:
                await self.log("WARN", f"[识图] 图片 {len(raw) / 1048576:.1f}MB 超过上限 {max_mb}MB，已跳过")
                continue

            ext = self._sniff_image_ext(raw)
            d_dir = self._vision_user_dir(user_id)
            no = self._next_vision_no(d_dir)
            name = f"{no:05d}_{time.strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(raw).hexdigest()[:8]}.{ext}"
            path = d_dir / name
            path.write_bytes(raw)
            out.append({
                "no": no,
                "path": str(path),
                "kind": kind,
                "tag": ("[表情包#%d]" if kind == "sticker" else "[图片#%d]") % no,
                "mime": "image/" + ("jpeg" if ext == "jpg" else ext),
            })
        return out

    @staticmethod
    def _attach_vision_images(messages: list, items: List[Dict[str, Any]]) -> int:
        """把图片转成 base64 data URI 挂到最后一条消息上（content 由 str 换成数组）。"""
        if not messages or not items:
            return 0
        last = dict(messages[-1])
        old = last.get("content")
        parts = list(old) if isinstance(old, list) else [{"type": "text", "text": str(old or "")}]
        n = 0
        for it in items:
            try:
                b = Path(it["path"]).read_bytes()
            except Exception:
                continue
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{it.get('mime') or 'image/jpeg'};base64,"
                                     + base64.b64encode(b).decode()},
            })
            n += 1
        if n:
            last["content"] = parts
            messages[-1] = last
        return n

    async def _resolve_see_tags(self, user_id: int, messages: List[Dict[str, Any]], reply_raw: str) -> str:
        """识图·回看：正文里出现 <see n="12" /> 时，把第 12 号图片重新贴给模型，再让它答一次。"""
        raw = str(reply_raw)
        nos = [int(x) for x in re.findall(r"<see\s+n\s*=\s*[\"']?(\d+)[\"']?\s*/?>", raw)]
        clean = re.sub(r"<see[^>]*>", "", raw).strip()
        if not nos:
            return raw

        items: List[Dict[str, Any]] = []
        for n in dict.fromkeys(nos):
            p = self._find_vision_image(user_id, n)
            if p:
                items.append({"no": n, "path": p, "kind": "image",
                              "tag": f"[图片#{n}]", "mime": "image/jpeg"})
        if not items:
            await self.log("WARN", f"[识图] 回看失败：找不到编号 {nos} 的图片")
            return clean

        await self.log("INFO", f"[识图] 模型要求回看 {len(items)} 张历史图片，重发一次请求")
        conv = list(messages)
        conv.append({"role": "assistant", "content": clean or "（我调一下那张图）"})
        conv.append({"role": "user",
                     "content": "（这是你要回看的历史图片，请结合它重新完整回答一次。）"})
        got = self._attach_vision_images(conv, items)
        if not got:
            return clean
        retry = await self._call_llm_native(conv, enable_notebook_tools=False)
        if retry and str(retry).strip():
            return retry
        return clean

    # ==================== 【识图】结束 ====================

    async def _run_notebook_tools(self, user_id: int, messages: List[Dict[str, Any]]) -> str:
        """笔记本工具预判轮。

        为什么单开一轮而不是并进主请求：主对话的 tools 字段被「底层 Payload 破甲」
        占用并强制 tool_choice=output_text，把工具混进去会让破甲失效。这里独立跑一次
        tool_choice=auto 的轻量调用，把工具执行结果拼回主请求，破甲一行不动。
        """
        store = self.notebook_store
        if store is None or not self.config.get("notebook_enabled", True):
            return ""
        if not self.config.get("notebook_tools_enabled", True):
            return ""

        names = self.config.get("notebook_tool_names") or list(NB_TOOLS.ALL_TOOLS)
        schemas = NB_TOOLS.tool_schemas(names)
        if not schemas:
            return ""

        base_url = self.config.get("llm_base_url", "https://api.openai.com/v1").rstrip("/")
        api_key = self.config.get("llm_api_key", "").strip()
        model = self.config.get("llm_model", "gemini-2.5-pro").strip()
        if base_url.endswith("/chat/completions"):
            endpoint = base_url
        elif base_url.endswith("/v1"):
            endpoint = f"{base_url}/chat/completions"
        else:
            endpoint = f"{base_url}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        # 只带最近几条原文，控制这一轮的 token
        keep = int(self.config.get("notebook_tools_history", 6) or 6)
        chat = [{"role": m.get("role"), "content": m.get("content")}
                for m in messages if m.get("role") in ("user", "assistant")][-keep:]
        hint = {
            "role": "system",
            "content": (
                "你是记忆助理，负责在正式回复之前处理「记笔记」和「回忆」两类请求。"
                "可用工具：angel_recall（检索记忆与笔记）、angel_remember（记住一条要点）、"
                "angel_note_create（把成体系的知识整理成 Markdown 笔记）、angel_note_read（展开笔记全文）。\n"
                "规则：\n"
                "1. 用户明确要求「记笔记 / 整理 / 记下来 / 记住 / 保存」时，必须真正调用工具写入，不要只在正文里说记住了。\n"
                "2. 需要回忆过去发生的事、用户偏好、或自己之前记过什么时，调用 angel_recall 检索。\n"
                "3. 检索结果里给了 short_id 后，需要看全文再调用 angel_note_read 展开。\n"
                "4. 本轮不需要任何工具时，直接回复「无需操作」，不要调用工具。\n"
                "5. 禁止保存密码、密钥等敏感信息。"
            ),
        }
        conv: List[Dict[str, Any]] = [hint] + chat
        done: List[str] = []
        reqs = 0                      # 本轮实际发出去几次请求，记日志用
        t_start = time.time()
        max_rounds = max(1, int(self.config.get("notebook_tools_max_rounds", 3) or 3))

        for _round in range(max_rounds):
            reqs += 1
            payload = {"model": model, "messages": conv, "temperature": 0.2,
                       "tools": schemas, "tool_choice": "auto", "stream": False}
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                    async with session.post(endpoint, headers=headers, json=payload) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            await self.log("WARN", f"[笔记本] 工具轮请求失败 HTTP {resp.status}：{body[:200]}")
                            return ""
                        res_json = await resp.json()
            except Exception as e:
                await self.log("WARN", f"[笔记本] 工具轮异常，本轮跳过：{e}")
                return ""

            choices = res_json.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            tcs = msg.get("tool_calls") or []
            if not tcs:
                break

            conv.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs})
            for tc in tcs:
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                res = await NB_TOOLS.run_tool(name, args, store, scope="global",
                                              memory_recall=lambda q: self._nb_memory_recall(user_id, q))
                conv.append({"role": "tool", "tool_call_id": tc.get("id") or name, "content": res})
                done.append(f"- {name}：{res}")
                await self.log("INFO", f"[笔记本] 工具调用 {name} → {str(res)[:80]}")

        cost = time.time() - t_start
        if not done:
            await self.log("INFO", f"[笔记本] 工具轮：{reqs} 次请求 / {cost:.2f}s，本轮无需操作")
            return ""
        await self.log("INFO", f"[笔记本] 工具轮：{reqs} 次请求 / {cost:.2f}s，执行了 {len(done)} 个工具")
        return ("【笔记本｜本轮已执行的工具】\n" + "\n".join(done) +
                "\n（以上是你刚刚调用工具的真实结果，正文里可以自然引用，但不要复述这段工具日志。）")

    async def build_memories(self, user_id: int, batch: int = 20, sender_name: str = "", history_store=None, memory_store=None) -> Dict[str, Any]:
        # 允许外部传入库（群聊模式整理自己的记忆库时用）
        _hist_store = history_store if history_store is not None else _hist_store
        _mem_store = memory_store if memory_store is not None else _mem_store
        """把尚未整理的聊天记录抽取成结构化记忆并入库（增量游标，重复点击不会重复总结）。"""
        store = _mem_store
        # 被隐藏的消息同样不参与记忆整理，避免从「记忆」这条路漏回 Prompt
        messages = [m for m in _hist_store.get_messages(user_id) if not m.get("hidden")]
        pending = store.pending_messages(user_id, messages)
        if not pending:
            return {"ok": True, "added": 0, "cursor": store.stats(user_id, len(messages))["cursor"],
                    "remaining": 0, "message": "没有新的聊天记录需要整理"}
        chunk = pending[:max(1, int(batch))]
        req = build_memory_messages(chunk, store.list_entries(user_id),
                                    self.config.get("memory_extract_prompt", ""))
        try:
            raw = await self._call_llm_native(req)
        except Exception as e:
            return {"ok": False, "added": 0, "message": f"调用模型整理记忆失败: {e}"}
        entries = parse_memory_entries(raw)
        added = store.add_entries_bulk(user_id, entries, sender_name=sender_name)
        cursor = len(messages) - len(pending) + len(chunk)
        store.commit_cursor(user_id, cursor, sender_name=sender_name)
        index_msg = ""
        if added:
            idx = await asyncio.to_thread(store.index_entries, user_id)
            index_msg = "；" + idx.get("message", "")
        remaining = max(0, len(messages) - cursor)
        await self.log("INFO", f"[长期记忆] 整理 {len(chunk)} 条对话，新增 {len(added)} 条记忆，剩余 {remaining} 条待整理")
        return {"ok": True, "added": len(added), "cursor": cursor, "remaining": remaining,
                "message": f"整理 {len(chunk)} 条对话，新增 {len(added)} 条记忆{index_msg}"}

    async def _call_llm_native(self, messages: list, enable_notebook_tools: bool = False) -> str:
        """酒馆同款标准 chat/completions 纯文本流，支持高自由度破甲"""
        base_url = self.config.get("llm_base_url", "https://api.openai.com/v1").rstrip("/")
        api_key = self.config.get("llm_api_key", "").strip()
        model = self.config.get("llm_model", "gemini-2.5-pro").strip()

        if base_url.endswith("/chat/completions"):
            endpoint = base_url
        elif base_url.endswith("/v1"):
            endpoint = f"{base_url}/chat/completions"
        else:
            endpoint = f"{base_url}/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model,
            "messages": messages,
            "temperature": float(self.config.get("temperature", 0.8))
        }

        # 流式传输：请求一建立就是持续有字节回来的长连接，
        # 中转 / Cloudflare 那种「等满 N 秒还没有任何响应就掐断」的拦截就落不到头上。
        _stream = bool(self.config.get("llm_stream", False))
        if _stream:
            payload["stream"] = True

        # 破甲开关
        jailbreak_enabled = self.config.get("jailbreak_enabled", True)
        payload_jailbreak = self.config.get("payload_jailbreak", True)

        # 笔记本工具（inline 模式）：和破甲工具并列放进同一次请求。
        # 酒馆里破限工具本来也没写死 tool_choice，是靠预设指令驱动的；
        # 一旦并进别的工具，tool_choice 就必须放开成 auto，否则模型只能调被指定的那一个。
        nb_schemas = []
        if enable_notebook_tools:
            try:
                nb_schemas = NB_TOOLS.tool_schemas(self.config.get("notebook_tool_names") or None)
            except Exception as _e:
                print(f"[notebook] 工具声明构建失败：{_e}")

        # 破限工具名必须和预设里写的名字一致，否则模型「看到要求调 A、工具列表只有 B」会对不上。
        # 预设 DARKSIDE_BYPASS 里写的是 game_content，所以默认发 game_content。
        _jail_tool = _preset_tool(self.config)
        _jt = str(_jail_tool["function"]["name"] or "game_content").strip()
        _jail_choice = {"type": "function", "function": {"name": _jt}}

        if jailbreak_enabled and payload_jailbreak:
            await self.log("INFO", f"[破限] 预设「{_preset_name(self.config)}」· 工具 {_jt} → 正文从这里回",
                           )
            # 底层 Payload 级破甲：注入正文工具。
            # 没有别的工具时保持「强制调用」；一旦并入了笔记本工具，就放开成 auto，
            # 由预设指令驱动模型把正文塞进该工具（正文来源会打进控制台日志）。
            if nb_schemas:
                payload["tools"] = [_jail_tool] + nb_schemas
                payload["tool_choice"] = "auto"
            else:
                payload["tools"] = [_jail_tool]
                payload["tool_choice"] = _jail_choice
        elif jailbreak_enabled:
            # 酒馆防截断2.0：注入 game_content 工具（不强制 tool_choice，由预设指令驱动）
            payload["tools"] = [_jail_tool] + nb_schemas if nb_schemas else [_jail_tool]
            if nb_schemas:
                payload["tool_choice"] = "auto"

        import datetime
        last_debug_info["request_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        last_debug_info["model"] = model
        last_debug_info["endpoint"] = endpoint
        last_debug_info["messages"] = redact_vision(messages)   # base64 图片脱敏，别撑爆面板
        last_debug_info["temperature"] = payload["temperature"]
        last_debug_info["tools"] = payload.get("tools")
        last_debug_info["tool_choice"] = payload.get("tool_choice")
        last_debug_info["source"] = "主 Bot"

        _dbg_tag = "测试模式" if self.config.get("test_mode", False) else (
            "调试模式" if self.config.get("debug_mode", False) else "")
        if _dbg_tag:
            try:
                await self.log("TEST", f"[{_dbg_tag}] 发往 API 的完整请求报文：\n"
                               + json.dumps(redact_vision(payload), ensure_ascii=False, indent=2))
            except Exception as _e:
                await self.log("WARN", f"[{_dbg_tag}] 请求报文打印失败: {_e}")

        # 正文可能来自三种地方：破甲工具(output_text/game_content) 或 纯文本 content。
        # 模型在给正文之前，还可能先调笔记本工具（记笔记/回忆），所以这里要循环。
        conv = list(messages)
        nb_used: List[str] = []          # 本轮笔记本工具执行记录
        reply_source = ""                # output_text / game_content / content
        max_rounds = max(1, int(self.config.get("notebook_tools_max_rounds", 3) or 3))
        reqs = 0
        raw_reply = ""

        # 外审拦截设置：none=直接认（回「被外审拦截」）；retry=自动重试 N 次
        _blk_retry = 0
        if str(self.config.get("block_action", "none") or "none").lower() == "retry":
            try:
                _blk_retry = max(1, min(10, int(self.config.get("block_retry_times", 2) or 2)))
            except Exception:
                _blk_retry = 2
        _blk_try = 0

        for _round in range(max_rounds + 1 + _blk_retry):
            payload["messages"] = conv
            reqs += 1
            _llm_to = max(30, int(self.config.get("llm_timeout") or 900))
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_llm_to, sock_read=_llm_to)) as session:
                async with session.post(endpoint, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        err_body = await resp.text()
                        last_debug_info["raw_response"] = f"HTTP {resp.status}: {err_body}"
                        raise RuntimeError(f"HTTP {resp.status}: {err_body}")

                    if _stream:
                        # 流式：分片拼回同构结构，下面所有解析逻辑照旧
                        res_json = await _collect_stream(resp)
                    else:
                        res_json = await resp.json()

            choices = res_json.get("choices", [])
            if not choices:
                last_debug_info["raw_response"] = res_json
                raise RuntimeError(f"LLM 响应无 choices: {res_json}")

            message_obj = choices[0].get("message", {})
            tool_calls = message_obj.get("tool_calls")
            if not isinstance(tool_calls, list):
                tool_calls = []

            # --- a. 破甲工具里的正文（优先，绕过文本层输出审查）---
            _fc = message_obj.get("function_call")   # 少数中转把工具调用放这个老字段
            if not tool_calls and isinstance(_fc, dict) and _fc.get("name"):
                tool_calls = [{"id": "fc_0", "type": "function", "function": _fc}]

            for tc in tool_calls:
                fn = tc.get("function", {})
                _fn_name = str(fn.get("name") or "")
                if _is_text_tool_name(_fn_name, self.config):
                    reply_source = _fn_name
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                        raw_reply = args["content"] if isinstance(args, dict) and "content" in args \
                            else fn.get("arguments", "")
                    except Exception:
                        raw_reply = fn.get("arguments", "")
                    break
            if not raw_reply and tool_calls:
                # 兜底：工具名不在名单里，但参数里就一个 content —— 那就是正文工具，认它
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    try:
                        _a = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        _a = None
                    if isinstance(_a, dict) and isinstance(_a.get("content"), str) and _a["content"].strip():
                        raw_reply = _a["content"]
                        reply_source = str(fn.get("name") or "content")
                        await self.log("INFO", "[主对话模型] 工具名「%s」不在预期名单里，"
                                              "但参数里有 content，按正文处理（名单跟着预设工具名走）"
                                              % reply_source)
                        break
            if raw_reply:
                # 并行工具调用：模型可能在同一次响应里既用破限工具给正文、又调笔记本工具。
                # 正文已经到手，但工具绝不能丢（丢了就是静默不写笔记），就地执行掉。
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    if fn.get("name") not in NB_TOOLS.ALL_TOOLS:
                        continue
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    res = await self._exec_nb_tool(str(fn.get("name") or ""), args)
                    nb_used.append(str(fn.get("name") or ""))
                    await self.log("INFO", f"[笔记本] 工具调用 {fn.get('name')}（与正文同批）→ {str(res)[:80]}")
                break

            # --- b. 笔记本工具：执行完把结果喂回去，继续下一轮 ---
            nb_calls = [tc for tc in tool_calls
                        if (tc.get("function") or {}).get("name") in NB_TOOLS.ALL_TOOLS]
            if nb_calls:
                conv.append({"role": "assistant", "content": message_obj.get("content") or "",
                             "tool_calls": tool_calls})
                for tc in nb_calls:
                    fn = tc.get("function") or {}
                    name = str(fn.get("name") or "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    res = await self._exec_nb_tool(name, args)
                    conv.append({"role": "tool", "tool_call_id": tc.get("id") or name, "content": res})
                    nb_used.append(name)
                    await self.log("INFO", f"[笔记本] 工具调用 {name} → {str(res)[:80]}")
                continue

            # --- c. 纯文本正文 ---
            raw_reply = message_obj.get("content", "")
            if raw_reply and _is_prohibited_note(raw_reply):
                # 被外审拦了：不是正文。按设置决定重试还是直接认
                _blk_try += 1
                last_debug_info["blocked_tries"] = _blk_try
                await self.log("WARN", "[主对话模型] 被外审拦截（PROHIBITED_CONTENT）第 %d 次：%s"
                                      % (_blk_try, str(raw_reply)[:160]))
                if _blk_retry and _blk_try <= _blk_retry:
                    await self.log("INFO", "[外审拦截] 按设置重试，第 %d/%d 次" % (_blk_try, _blk_retry))
                    await asyncio.sleep(0.6)
                    continue
                last_debug_info["blocked"] = True
                raw_reply = ""
                reply_source = "prohibited"
                break
            if raw_reply and _looks_like_finish_note(raw_reply):
                # 中转把收尾状态当正文塞回来了：那不是正文，重试一次；还这样就当空响应。
                try:
                    _mjson = json.dumps(message_obj, ensure_ascii=False)[:500]
                except Exception:
                    _mjson = str(message_obj)[:500]
                await self.log("WARN", "[主对话模型] 只拿到收尾状态、没拿到正文："
                                      + str(raw_reply)[:120] + " | 原始 message: " + _mjson)
                if _round == 0:
                    continue
                raw_reply = ""
                reply_source = ""
                break
            # 私聊：勾了「空文本 / 该用工具却没调工具也当外审拦截」才启用。
            # 默认不勾 —— 免得正常的不说话、或模型这轮本来就不调工具时被误判成拦截。
            if bool(self.config.get("block_empty_as_blocked", False)):
                _need_tool = bool(payload.get("tools")) and \
                    str(payload.get("tool_choice") or "").strip().lower() not in ("", "auto", "none")
                _empty = not str(raw_reply or "").strip()
                _no_tool = _need_tool and not tool_calls and not _empty
                if _empty or _no_tool:
                    _blk_try += 1
                    _why = "空文本" if _empty else "该用工具却没调工具"
                    last_debug_info["blocked_tries"] = _blk_try
                    await self.log("WARN", "[主对话模型] %s，按外审拦截处理，第 %d 次"
                                          % (_why, _blk_try))
                    if _blk_retry and _blk_try <= _blk_retry:
                        await self.log("INFO", "[外审拦截] 按设置重试，第 %d/%d 次" % (_blk_try, _blk_retry))
                        await asyncio.sleep(0.6)
                        continue
                    last_debug_info["blocked"] = True
                    last_debug_info["blocked_reason"] = _why
                    raw_reply = ""
                    reply_source = "prohibited"
                    break
            reply_source = "content" if raw_reply else ""
            break

        last_debug_info["reply_source"] = reply_source
        last_debug_info["notebook_tools_used"] = nb_used
        last_debug_info["api_requests"] = reqs
        last_debug_info["messages"] = conv
        last_debug_info["raw_response"] = raw_reply

        _dbg_tag2 = "测试模式" if self.config.get("test_mode", False) else (
            "调试模式" if self.config.get("debug_mode", False) else "")
        if _dbg_tag2:
            try:
                await self.log("TEST", f"[{_dbg_tag2}] API 返回的正文（全量）：\n" + str(raw_reply))
                await self.log("TEST", f"[{_dbg_tag2}] API 原始响应体（全量）：\n"
                               + json.dumps(res_json, ensure_ascii=False, indent=2))
            except Exception as _e:
                await self.log("WARN", f"[{_dbg_tag2}] 响应打印失败: {_e}")

        return raw_reply

    def _wire_lwb_callers(self, store):
        """把「总结 / 锚点 / 重排」三个调用器挂到指定记忆库上（主库和 Claude 库各挂一份）。"""
        try:
            store.set_llm_caller(self.lwb_summary_caller)
            _lwb = getattr(store, "lwb", None)
            if _lwb is not None and hasattr(_lwb, "set_rerank_caller"):
                _lwb.set_rerank_caller(self.lwb_rerank_call)
            for _m in ("set_anchor_caller", "set_l0_caller"):
                if _lwb is not None and hasattr(_lwb, _m):
                    getattr(_lwb, _m)(self.lwb_anchor_caller)
                    break
        except Exception as _e:
            print(f"[lwb] 记忆引擎调用器挂载失败：{_e}")

    async def _handle_claude_group(self, data: Dict[str, Any]) -> bool:
        """把群消息转交「群聊模式」；返回 True 表示已被接管，主 Bot 不再处理。"""
        cm = getattr(self, "claude", None)
        if cm is None:
            return False
        return await cm.handle_group_event(data)

    def _load_claude_world_book(self) -> List[Dict[str, Any]]:
        """读 群聊模式自己的世界书（claude_data/world_book.json）。"""
        p = Path(os.path.dirname(os.path.abspath(__file__))) / CGM.DATA_DIR / "world_book.json"
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8")) or []
        except Exception as e:
            print(f"[claude] 读 claude_data/world_book.json 失败：{e}")
        return []

    def update_claude_world_book(self, entries: List[Dict[str, Any]]):
        """热更新 群聊模式的世界书。"""
        cm = getattr(self, "claude", None)
        if cm is not None:
            cm.load_world_book(entries)

    @staticmethod
    def _to_onebot_file(p: Any) -> str:
        """本地图片路径 → OneBot v11 的 file:/// 绝对路径。

        NapCat 是独立进程、cwd 不是本项目目录：相对路径必须补成绝对路径，
        否则它会拿自己的目录去拼，报 ENOENT（no such file or directory）。
        """
        s = str(p or "").strip()
        if not s:
            return ""
        if s.startswith("file:///") or s.startswith("http://") or s.startswith("https://") \
                or s.startswith("base64://"):
            return s
        _abs = s if os.path.isabs(s) else os.path.join(os.path.dirname(os.path.abspath(__file__)), s)
        return "file:///" + os.path.normpath(_abs).replace("\\", "/").lstrip("/")

    async def _send_poke(self, user_id: Any, group_id: Any = None) -> bool:
        """戳一戳（NapCat 的 send_poke，群聊/私聊通用）。"""
        if not self.is_ws_connected:
            await self.log("WARN", "[戳一戳] WebSocket 未连接，戳不出去")
            return False
        try:
            params: Dict[str, Any] = {"user_id": int(user_id)}
            if group_id:
                params["group_id"] = int(group_id)
        except Exception:
            return False
        action_data = {
            "action": "send_poke",
            "params": params,
            "echo": f"poke_{int(time.time() * 1000)}",
        }
        try:
            await self.ws.send(json.dumps(action_data, ensure_ascii=False))
            return True
        except Exception as e:
            await self.log("WARN", f"[戳一戳] 发送失败：{e}")
            return False

    async def _active_msg_loop(self):
        """主动消息：启动时把错过的排期往后挪；之后每 30 秒看有没有到点的要发。"""
        try:
            import active_msg as _AM
            for _m in _AM.reschedule_missed(self.config):
                await self.log("WARN", f"[主动消息] 「{_m['old']}」那会儿服务没开，没发出去，"
                                       f"已挪到 {_m['new']}（第 {_m['miss']} 次错过）")
        except Exception as e:
            await self.log("WARN", f"[主动消息] 启动检查失败：{e}")
        while True:
            await asyncio.sleep(30)
            try:
                import active_msg as _AM
                for _r in _AM.due():
                    _txt = str(_r.get("text") or "")
                    _grp = _r.get("kind") == "group"
                    try:
                        if _grp:
                            _ok = await self.send_group_msg(int(_r.get("chat_id") or 0), _txt)
                        else:
                            _ok = await self.send_private_msg(int(_r.get("chat_id") or 0), _txt)
                    except Exception as _e:
                        _ok = False
                        await self.log("WARN", f"[主动消息] 发送出错：{_e}")
                    _AM.mark_done(str(_r.get("id")), bool(_ok))
                    await self.log("INFO", f"[主动消息] {'群里' if _grp else '私聊'}到点发出"
                                           f"（{'成功' if _ok else '失败'}）")
            except Exception as e:
                await self.log("WARN", f"[主动消息] 轮询出错：{e}")

    async def _idle_loop(self):
        """闲置触发：隔一段时间抽一次，闲置越久概率越高；抽中且（夜间）苏醒就主动发一条。"""
        _first = True
        while True:
            try:
                import idle as _ID
                _c = _ID._c(self.config)
                if not _first:
                    await asyncio.sleep(max(60, int(_ID._i(_c, "idle_check_minutes", 20)) * 60))
                _first = False
                try:
                    _ts = _ID.seed_from_history()
                    if _ts > 0:
                        await self.log("INFO", "[闲置触发] 已从聊天记录回填上次说话时间 "
                                               + time.strftime("%m-%d %H:%M", time.localtime(_ts)))
                except Exception as _e:
                    await self.log("WARNING", f"[闲置触发] 回填聊天记录失败: {_e}")
                _st = _ID.status(self.config)
                if _st.get("chat_id"):
                    await self.log("INFO", f"[闲置触发] 上次说话是 {_st['idle_hours']} 小时前，"
                                           f"当前触发概率 {round(_st['rate_now'] * 100)}%"
                                           + ("（现在是夜间时段）" if _st.get("night") else ""))
                r = _ID.should_fire(self.config)
                if not r:
                    continue
                if not r.get("hit"):
                    await self.log("INFO", f"[闲置触发] 已闲置 {r['idle_hours']} 小时，"
                                           f"本次概率 {r['p'] * 100:.0f}%，没抽中")
                    continue
                if not r.get("fire") or not r.get("chat_id"):
                    await self.log("INFO", f"[闲置触发] 抽中了（{r['mode']}），"
                                           f"但现在是夜间 {_ID._i(_c, 'idle_night_start', 0)}-"
                                           f"{_ID._i(_c, 'idle_night_end', 8)} 点且没醒，这次先不发")
                    _ID.note("抽中但夜间未醒：%s" % r.get("mode"))
                    continue
                await self._fire_idle(r)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                try:
                    await self.log("WARN", f"[闲置触发] 出错：{e}")
                except Exception:
                    pass
                await asyncio.sleep(120)

    async def _fire_idle(self, r: Dict[str, Any]) -> None:
        """真的主动发一条：闲置触发是完整一次 API 调用；旧事重提先翻记忆库。"""
        import idle as _ID
        cfg = self.config
        mode = str(r.get("mode") or "idle")
        mem = ""
        if mode == "recall":
            try:
                mem = await self._build_memory_block(int(r.get("chat_id") or 0), _ID.recall_query())
            except Exception:
                mem = ""
        body = _ID.prompt_recall(cfg, mem) if mode == "recall" else _ID.prompt_idle(cfg)
        sys_txt = (str(cfg.get("system_prompt") or "").strip() + "\n\n" + body).strip()
        msgs = [{"role": "system", "content": sys_txt},
                {"role": "user", "content": _ID.kick(cfg, mode)}]
        try:
            txt = (await self._call_llm_native(msgs) or "").strip()
        except Exception as e:
            await self.log("WARN", f"[闲置触发] 生成失败：{e}")
            return
        if not txt:
            await self.log("WARN", "[闲置触发] 模型没生成内容，这次跳过")
            return
        try:
            if r.get("kind") == "group":
                await self.send_group_msg(int(r.get("chat_id") or 0), txt)
            else:
                await self.send_private_msg(int(r.get("chat_id") or 0), txt)
            _ID.mark_fired(mode)
            await self.log("INFO", f"[闲置触发] 已主动发出（{'旧事重提' if mode == 'recall' else '闲置问候'}，"
                                   f"闲置 {r.get('idle_hours')} 小时）")
        except Exception as e:
            await self.log("WARN", f"[闲置触发] 发送失败：{e}")

    async def send_group_msg(self, group_id: int, message: str,
                             images: Optional[List[str]] = None) -> bool:
        """向群聊发送消息（群聊模式专用，不等待回执）。images 为本地图片路径列表。"""
        if not self.is_ws_connected:
            await self.log("WARN", f"[群聊] WebSocket 未连接，群 {group_id} 的消息发不出去")
            return False
        msg: Any = str(message or "")
        if images:
            _segs: List[Dict[str, Any]] = []
            if str(message or "").strip():
                _segs.append({"type": "text", "data": {"text": str(message)}})
            for _p in images:
                _u = self._to_onebot_file(_p)
                if not _u:
                    continue
                if _u.startswith("file:///") and not os.path.exists(_u[8:]):
                    await self.log("WARN", f"[图片] 本地文件不存在，跳过这一张：{_u}")
                    continue
                _segs.append({"type": "image", "data": {"file": _u}})
            if not _segs:
                await self.log("WARN", f"[群聊] 群 {group_id} 没有可发的文字和图片，已跳过")
                return False
            msg = _segs
        action_data = {
            "action": "send_group_msg",
            "params": {"group_id": int(group_id), "message": msg},
            "echo": f"claude_group_{int(time.time() * 1000)}",
        }
        try:
            await self.ws.send(json.dumps(action_data, ensure_ascii=False))
            return True
        except Exception as e:
            await self.log("ERROR", f"[群聊] 群 {group_id} 发送失败：{e}")
            return False

    async def send_private_msg(self, user_id: int, text: str, images: Optional[List[str]] = None):
        if not self.is_ws_connected:
            raise RuntimeError("NapCat WebSocket 未连接，无法发送消息")

        message: Any = text
        if images:
            # OneBot v11 消息段：文字段 + 图片段（本地文件用 file:/// 绝对路径，反斜杠必须转正斜杠）
            segs: List[Dict[str, Any]] = []
            if text:
                segs.append({"type": "text", "data": {"text": text}})
            for _p in images:
                _u = self._to_onebot_file(_p)
                if not _u:
                    continue
                if _u.startswith("file:///") and not os.path.exists(_u[8:]):
                    await self.log("WARN", f"[图片] 本地文件不存在，跳过这一张：{_u}")
                    continue
                segs.append({"type": "image", "data": {"file": _u}})
            if not segs:
                await self.log("WARN", "[私聊] 没有可发的文字和图片，已跳过")
                return
            message = segs

        action_data = {
            "action": "send_private_msg",
            "params": {
                "user_id": int(user_id),
                "message": message
            }
        }
        await self.ws.send(json.dumps(action_data))


# ---------------- API 工具：获取远端可用模型列表 ----------------

async def fetch_available_models(base_url: str, api_key: str) -> List[str]:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        endpoints = [f"{base_url}/models", f"{base_url.rstrip('/v1')}/models"]
    else:
        endpoints = [f"{base_url}/v1/models", f"{base_url}/models"]

    headers = {
        "Authorization": f"Bearer {api_key}" if api_key else ""
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        last_err = None
        for ep in endpoints:
            try:
                async with session.get(ep, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw_list = data.get("data", [])
                        if isinstance(raw_list, list):
                            model_ids = [item.get("id") for item in raw_list if isinstance(item, dict) and item.get("id")]
                            unique_ids = []
                            for mid in model_ids:
                                if mid and mid not in unique_ids:
                                    unique_ids.append(mid)
                            return unique_ids
            except Exception as e:
                last_err = e
        if last_err:
            raise last_err
    return []
