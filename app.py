import asyncio
import json
import urllib.request
import struct
import zlib
import base64
import time
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, Any, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

import sticker_steal as CSS
from bot import OneBotClient, fetch_available_models, last_debug_info
from memory import EmbeddingClient, MEMORY_KIND_LABEL

# 生图（ComfyUI）：工作流管理 + 出图
import comfyui as COMFY

# 初始化日志
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("QQBotX-Server")

CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG = {
    "llm_base_url": "https://api.openai.com/v1",
    "llm_api_key": "",
    "llm_model": "gpt-4o-mini",
    "temperature": 0.7,
    "llm_profiles": [],              # 主对话 API 的多套配置档（保存/切换用）

    # ---------------- 笔记本（AI 可读写的独立知识库） ----------------
    "notebook_enabled": True,        # 笔记本总开关
    "notebook_inject": True,         # 每轮把相关笔记注入主对话提示词
    "notebook_top_k": 3,             # 注入的笔记条数上限
    "notebook_inject_chars": 300,    # 每条笔记注入时的预览字数
    "notebook_tools_enabled": True,  # 注册 4 个 LLM 工具（让 AI 自己记笔记/回忆）
    "notebook_tools_mode": "inline",
    "jailbreak_tool_name": "game_content",
    "llm_preset_id": "",              # 当前生效的破限预设 id（空=用第一套）
    "llm_presets": [],                # 破限预设列表（首次打开预设页签时自动写入内置两套）
    "style_preset_id": "",            # 当前生效的文风预设 id（空=用第一套）
    "style_presets": [],
    # 群聊模式自己的一套预设（和主 Bot 完全独立）
    "claude_jailbreak_enabled": True,   # 群聊模式的破限总开关（预设页签里勾选控制）
    "claude_strip_think_enabled": True,  # 群聊：把思维链正则块剥掉再发（默认勾选）
    "strip_think_enabled": True,         # 主 Bot：同上
    "claude_llm_preset_id": "",
    "claude_llm_presets": [],
    "claude_style_preset_id": "",
    "claude_style_presets": [],
    "claude_style_enabled": [],
    "style_enabled": [],              # 已勾选启用的文风预设 id（按列表顺序注入）              # 文风预设列表（独立于破限，可单独拔插）
    "jailbreak_recency_enabled": True,  # 把破限硬提醒贴到最后一条消息尾部（实测决定性）
    "jailbreak_recency_prompt": "",      # 留空用内置文案；填了就用你的  # 破限工具名，必须和预设里写的一致  # inline=并进主请求(省调用) / prepass=单开一轮(破甲强制不动) / off
    "notebook_tools_max_rounds": 3,  # 工具轮最多来回几次
    "notebook_tools_history": 6,     # 工具轮带几条最近原文
    "notebook_recall_top_k": 8,      # angel_recall 检索记忆库的条数
    "notebook_tool_names": ["angel_recall", "angel_remember", "angel_note_create", "angel_note_read"],
    # ---------------- 【魂】消息防抖（连发的多条消息合并成一条完整消息再交给 AI） ----------------
    "debounce_enabled": False,        # 总开关
    "debounce_scope": "both",         # private / group / both
    "debounce_threshold": 0.5,        # 完整度阈值 0~1，越高越严格
    "debounce_timeout": 30,           # 秒；0 = 不超时
    "debounce_cancel_on_new": True,   # 回复发出前又收到新消息 → 本次回复作废
    "debounce_debug": False,          # 打详细日志
    "debounce_judge": "local",        # local（项目自带小模型，默认）/ builtin（内置规则）/ model（本机接口小模型）
    "debounce_model_base_url": "http://127.0.0.1:11434/v1",
    "debounce_model_key": "ollama",
    "debounce_model_name": "qwen2.5:0.5b-instruct",
    "debounce_model_timeout": 20,
    "debounce_min_chars": 0,          # 小于这个字数直接放行（0 = 不启用）
    # ---- 【魂】消息分段发送（只影响怎么发，不影响记录与上下文） ----
    "seg_enabled": False,             # 总开关
    "seg_scope": "both",              # private / group / both
    "seg_rule": "newline",            # 分割规则：newline / blankline / sentence
    "seg_min_len": 0,                 # 片段短于这个字数就并到上一条（0 = 不合并）
    "seg_base_ms": 300,               # 每条之间的基础停顿（毫秒）
    "seg_per_char_ms": 45,            # 每个字再加多少毫秒
    "seg_jitter": 0.35,               # 随机浮动 ±35%
    "seg_min_ms": 150,                # 停顿下限
    "seg_max_ms": 4000,               # 停顿上限
    "seg_debug": False,
    # ---- 【秘书网关】群聊模式：复读 / 关键词 / 权重与时机判断 ----
    "sec_enabled": False,
    "sec_base_url": "",
    "sec_key": "",
    "sec_model": "",
    "sec_timeout": 60,
    "sec_temperature": 0.3,
    "sec_ctx_count": 10,              # 发给秘书的上下文条数
    "sec_yes_weight": 6,              # Yes 权重
    "sec_no_weight": 4,               # No 权重（阈值 = No/(Yes+No)）
    "sec_mood_threshold": 0.5,        # 气氛时机分门槛
    "sec_keywords": [],               # 命中就直接给主对话
    "sec_repeat_enabled": True,       # 复读检测
    "sec_repeat_window": 6,
    "sec_repeat_need": 3,
    "sec_repeat_min_len": 2,
    "sec_debug": False,
    # ---- 群聊呼唤模式：被@/被回复后保持在线 N 分钟，期间不经秘书直接放给主对话 ----
    "claude_call_mode_enabled": True,      # 呼唤模式总开关
    "claude_call_mode_minutes": 5,         # 保持在线多少分钟（每条新消息重新续满）
    "claude_call_stop_words": "",          # 闭嘴词：呼唤模式在线时命中就结束呼唤模式（逗号/换行分隔）
    "poke_enabled": True,
    "poke_private": True,
    "poke_group": True,
    "poke_max": 8,
    "poke_window": 900,
    "poke_note": True,
    "poke_back_enabled": True,
    "poke_back_rate": 0.5,
    "poke_back_delay": 1.5,
    "poke_follow_enabled": True,
    "poke_follow_rate": 0.3,
    "poke_follow_delay": 2.0,
    "poke_follow_cooldown": 60,
    "active_msg_enabled": True,
    "active_msg_prompt": True,
    "active_msg_max": 20,
    "active_msg_grace": 300,
    "active_msg_keep_done": 8,
    "active_msg_shift": True,
    "poke_active_enabled": True,
    "poke_active_max": 1,
    "poke_active_cooldown": 120,
    "idle_enabled": True,
    "idle_after_hours": 6,
    "idle_base_rate": 0.2,
    "idle_step_rate": 0.03,
    "idle_step_minutes": 60,
    "idle_check_minutes": 20,
    "idle_night_start": 0,
    "idle_night_end": 8,
    "idle_wake_rate": 0.2,
    "idle_weight_idle": 50,
    "idle_weight_recall": 50,
    "idle_max_per_day": 3,
    "idle_chat_recall_enabled": True,
    "idle_chat_recall_rate": 0.08,
    "idle_prompt_idle": "",
    "idle_prompt_recall": "",
    "idle_prompt_chat_recall": "",
    "system_prompt": "",
    "napcat_ws_url": "ws://127.0.0.1:3001",
    "napcat_cmd": r"C:\ai\NapCat.Shell.Windows.Node\napcat.bat",
    # ---- 协议端：napcat（默认，QQX_AIBot 托管进程+保活）/ snowluma（空协议模式，只连 OneBot）----
    "qq_protocol": "napcat",
    "snowluma_ws_url": "ws://127.0.0.1:3001",
    # SnowLuma 的启动脚本路径（切到 SnowLuma 时由 QQX_AIBot 拉起；空协议模式下不管登录/保活）
    "snowluma_cmd": r"C:\ai\SnowLuma\launcher.bat",
    "snowluma_keepalive": True,     # SnowLuma 进程保活：进程掉了自动拉起（对齐 NapCat 保活）
    "snowluma_ka_interval": 30,     # SnowLuma 保活检测间隔（秒）
    "jailbreak_enabled": True,
    "payload_jailbreak": True,
    "depth_injection": True,
    # ---- 长期记忆库（向量版）----
    "embedding_base_url": "",      # 嵌入服务地址，如 https://api.siliconflow.cn/v1
    "embedding_api_key": "",
    "embedding_model": "",         # 如 BAAI/bge-m3
    "embedding_timeout": 20,
    "memory_enabled": True,        # 关闭则完全退回旧的全量上下文行为
    "memory_history_limit": 0,
    # 外审拦截（PROHIBITED_CONTENT）：none=什么都不做直接回「被外审拦截」；retry=自动重试
    "block_action": "none",
    "block_retry_times": 2,
    # 私聊：把「空文本」「该用工具却没调工具」也判为被外审拦截（默认关，勾了才生效）
    "block_empty_as_blocked": False,          # block_action=retry 时的重试次数（1~10）    # 只带最近 N 条原文进 Prompt，更早的靠记忆召回（0=不限）
    "memory_top_k": 5,             # 每轮最多注入几条记忆
    "memory_min_score": 0.25,      # 召回相似度阈值
    "memory_cross_inject": False,  # 跨会话召回：是否把其他会话的记忆也纳入召回
    "memory_cross_allow": [],      # 跨会话白名单 UID；留空=所有其他会话共享
    "memory_batch": 20,            # 每次整理多少条对话

    # ---------------- 生图（ComfyUI） ----------------
    "comfy_enabled": True,           # 生图总开关
    "comfy_server": "http://127.0.0.1:8000",  # ComfyUI 地址（注意不是默认的 8188）
    "comfy_workflow": "",            # 当前工作流文件名，留空=用内置默认流
    "comfy_prompt_node": "",         # 正向提示词节点 ID，留空=自动识别
    "comfy_negative_node": "",       # 负向提示词节点 ID，留空=自动识别
    "comfy_timeout": 300,            # 单次出图超时（秒）
    "comfy_ai_draw": True,           # 允许 AI 自己写 <pic> 标签自主出图
    "comfy_ai_max": 1,               # 一条回复最多出几张
    "comfy_cooldown": 20,            # 出图冷却（秒），防刷屏
    "comfy_guide": "",               # 注入给模型的生图能力说明；留空=用内置默认，支持 {max_rule} 占位符
    "time_inject_enabled": True,     # 是否把「当前真实时间」注入 system 提示词
    "debug_mode": False,          # 调试模式：只打印 API 请求/返回，不改上下文行为
    "time_inject_tmpl": ""           # 自定义时间块文案；留空=用内置默认，支持 {time}/{date}/{weekday}/{hm}/{iso}
}


# ---------------- 【识图】表情包页默认值 ----------------
DEFAULT_CONFIG.update({
    "vision_enabled": True,           # 识图总开关；关掉后退回旧行为（图片段直接忽略）
    "vision_max_images": 3,           # 单条消息最多识别几张
    "vision_store_max": 120,               # 「已收到」图片保留上限，超了自动删最老的
    "vision_max_mb": 8,               # 单张图片大小上限（MB），超了跳过
    "vision_sticker_enabled": True,   # 表情包（image sub_type=1）也一起识别
    "vision_see_enabled": True,       # 允许 AI 用 <see n="编号" /> 回看历史图片（会多一次请求）
    "vision_guide": "",               # 注入给模型的识图说明；留空=用内置默认
})


# ---------------- 【表情包】偷图 / 待审核池 / 独立识图模型 ----------------
DEFAULT_CONFIG.update({
    "sticker_steal_enabled": True,          # 偷图总开关
    "sticker_steal_mode": "probability",    # probability=按概率 / cooldown=按冷却
    "sticker_steal_chance": 0.3,            # 概率模式下每次收到图的收集概率
    "sticker_steal_cooldown": 30,           # 冷却模式：两次收集至少间隔多少秒
    "sticker_steal_audit": True,            # 需人工审核：先入池，通过了才识图入库
    "sticker_steal_scope": "sticker_only",  # sticker_only / all_images / gif_only
    "sticker_steal_kinds": ["marker", "animated"],
    # ---- AI 主动发表情包（写 <meme ... /> 触发）----
    "sticker_send_enabled": True,           # 发表情包总开关
    "sticker_send_where": ["claude", "private"],   # claude=群聊模式(群聊) / private=主对话(私聊)
    "sticker_send_max": 1,                  # 单条回复最多发几张
    "sticker_send_fav_first": True,         # 收藏的优先挑
    "sticker_send_guide": "",               # 留空用内置说明
    # ---- 发送时机：独立成条 + 精力/情绪极性 -> 前后权重随机 ----
    "sticker_send_separate": True,          # 表情包单独发一条，不跟文字挤在同一条里
    "sticker_pos_random": True,             # 没显式指定位置时，按权重随机前后
    "sticker_pos_base": 50,                 # 基础后置概率（%）
    "sticker_pos_energy": 35,               # 精力影响（%）：精力越高后置概率越大
    "sticker_pos_polar": 30,                # 情绪极性影响（%）：正向加、负向减
    "sticker_pos_pos_emos": "开心,高兴,快乐,可爱,害羞,喜欢,爱心,亲亲,比心,抱抱,笑,得意,期待,甜,惊喜,卖萌,撒娇",
    "sticker_pos_neg_emos": "生气,愤怒,震惊,无语,难过,哭,嘲讽,鄙视,嫌弃,害怕,惊恐,崩溃,抓狂,委屈,冷漠,翻白眼,尴尬",
    "sticker_pool_capacity": 200,           # 待审核池上限，满了自动暂停偷图
    "sticker_max_num": 100,                 # 图库自动淘汰上限（收藏的不占额度）
    "sticker_evict_usage_weight": 0.7,      # 淘汰打分里「用得少」的权重
    "sticker_steal_wl": [],                 # 偷图白名单：group:群号 / user:QQ号
    "sticker_steal_bl": [],                 # 偷图黑名单
    "sticker_steal_priority": "whitelist_first",   # 黑白名单冲突时谁优先
    "sticker_max_mb": 8,                    # 单张大小上限（MB）
    "sticker_vision_base_url": "http://127.0.0.1:11434/v1",   # 表情包专用识图模型
    "sticker_vision_key": "ollama",
    "sticker_vision_model": "huihui_ai/qwen3-vl-abliterated:8b",
    "sticker_vision_interval": 5,           # 两次识图之间至少间隔多少秒
    "sticker_vision_timeout": 300,          # 单张识图超时（秒），本地 8B 视觉模型冷启动可能上百秒
    "sticker_vision_prompt": "",            # 留空用内置分类提示词
})


# 群聊模式：与主 Bot 完全独立的一套（自己的上下文/记忆/笔记本/世界书，不注入破甲）
DEFAULT_CONFIG.update({
    "claude_enabled": False,
    "claude_base_url": "",          # 留空 = 沿用主对话 base_url
    "claude_api_key": "",           # 留空 = 沿用主对话 key
    "claude_model": "",             # 留空 = 沿用主对话 model
    "claude_temperature": 0.8,
    "claude_timeout": 90,
    "claude_bot_name": "克劳德",
    "claude_system_prompt": "",     # 留空 = 用内置群友人设
    "claude_groups": "",            # 留空 = 所有群都生效；填群号则只在这些群生效
    "claude_context_limit": 40,
    "claude_silence_marker": "<silence />",
    "claude_worldbook_enabled": True,
    "claude_memory_enabled": True,
    "notebook_cross_inject": False,
    "notebook_cross_allow": [],
    "claude_notebook_cross_inject": False,
    "claude_notebook_cross_allow": [],
    "claude_memory_top_k": 5,
    "claude_memory_cross_groups": False,   # 群聊档：允许注入其他群聊的记忆（同一数据源）
    "claude_memory_min_score": 0.25,
    "claude_memory_cross_inject": False,   # 群聊：跨会话召回开关
    "claude_memory_cross_allow": [],       # 群聊：跨会话白名单 UID；留空=全部共享
    "claude_notebook_enabled": True,
    "claude_pic_enabled": True,        # Claude 模式能不能自己写生图词出图
    "claude_pic_max": 1,               # 一次回复最多出几张
    "claude_api_profiles": [],          # Claude 模式自己的 API 配置档
    "claude_group_rules": "",           # 群聊规则（留空用内置精简版）
    "claude_prompt_template": "",       # 整段提示词骨架
    "claude_pic_guide": "",             # 生图说明（留空用内置短版）
    "claude_persona_id": "attach",      # 当前用哪套人设注入
    "claude_personas": [],              # 自定义人设
    "claude_persona_inject": {},        # 每套人设各记一套「注入什么」
    "claude_vision_enabled": True,      # 群里发的图 / 表情包要不要给它看（多模态）
})


def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                config = DEFAULT_CONFIG.copy()
                config.update(saved)
                return config
        except Exception as e:
            logger.warning(f"读取 config.json 失败: {e}，使用默认配置")
    return DEFAULT_CONFIG.copy()


def save_config(config: Dict[str, Any]):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


current_config = load_config()

# ---------------- 协议端（NapCat / SnowLuma）----------------
# qq_protocol = "napcat"（默认）：QQX_AIBot 托管 NapCat 进程 + 登录 + 保活。
# qq_protocol = "snowluma"（空协议模式）：SnowLuma 自己挂在 QQ 本体上跑（自带 WebUI、
#   自己扫码），QQX_AIBot 只做 OneBot v11 客户端连接，不接管进程、不跑 NapCat 保活。
def protocol_backend() -> str:
    """当前协议端：napcat / snowluma。"""
    return "snowluma" if str(current_config.get("qq_protocol") or "").strip().lower() == "snowluma" else "napcat"


def napcat_mode() -> bool:
    """是否处于 NapCat 模式。SnowLuma 模式下所有 NapCat 专属逻辑都要让位。"""
    return protocol_backend() == "napcat"


def _snowluma_dirs() -> list:
    """SnowLuma 可能的安装目录（用来读它自己生成的 OneBot 配置）。"""
    out = []
    raw = str(current_config.get("snowluma_cmd") or "").strip().strip('"')
    if raw:
        out.append(os.path.dirname(raw) if os.path.isfile(raw) else raw)
    here = os.path.dirname(os.path.abspath(__file__))
    out += [os.path.join("C:\\ai", "SnowLuma"),
            os.path.join(os.path.dirname(here), "SnowLuma"),
            os.path.join(here, "SnowLuma")]
    res, seen = [], set()
    for d in out:
        d = str(d or "").strip()
        if d and d not in seen and os.path.isdir(d):
            seen.add(d); res.append(d)
    return res


def snowluma_ws_info() -> dict:
    """从 SnowLuma 自己的配置里读正向 WS 服务（networks.wsServers[0]）的端口与 access_token。"""
    import glob as _glob
    for _d in _snowluma_dirs():
        try:
            _fs = sorted(_glob.glob(os.path.join(_d, "config", "onebot_*.json")),
                         key=os.path.getmtime, reverse=True)
        except Exception:
            continue
        for _p in _fs:
            try:
                with io.open(_p, encoding="utf-8") as _f:
                    _j = json.load(_f)
                _ws = ((_j.get("networks") or {}).get("wsServers") or [])
                if _ws and isinstance(_ws[0], dict):
                    _s = _ws[0]
                    return {"file": _p, "port": int(_s.get("port") or 0),
                            "path": str(_s.get("path") or "/"),
                            "token": str(_s.get("accessToken") or "")}
            except Exception:
                continue
    return {}


def snowluma_ws_autofix(url: str) -> str:
    """SnowLuma 模式：地址里没带 access_token 就自动从它自己的配置里补上。"""
    u = str(url or "").strip()
    if not u or "access_token=" in u:
        return u
    info = snowluma_ws_info()
    tok = str(info.get("token") or "")
    if not tok:
        return u
    # 规范成 ws://host:port/<path>?access_token=... ：SnowLuma 的 wsServers path 是 "/"，
    # 少了这个斜杠它会把请求当成别的路径处理
    from urllib.parse import urlsplit, urlunsplit, quote
    sp = urlsplit(u)
    q = (sp.query + "&") if sp.query else ""
    return urlunsplit((sp.scheme, sp.netloc, sp.path or "/",
                       q + "access_token=" + quote(tok, safe=""), ""))


def active_ws_url() -> str:
    """按协议端取 OneBot 正向 WS 地址。"""
    if napcat_mode():
        return str(current_config.get("napcat_ws_url") or "ws://127.0.0.1:3001")
    return snowluma_ws_autofix(str(current_config.get("snowluma_ws_url") or "ws://127.0.0.1:3001"))


def _napcat_guard(action: str):
    """SnowLuma 模式下 NapCat 专属动作统一拒绝；返回 None 表示放行。"""
    if napcat_mode():
        return None
    return {"status": "warn",
            "message": "当前协议端是 SnowLuma，%s 已停用；到「设置 → 协议端」切回 NapCat 后可用" % action}


# 数据源跟着群聊模式走：开着群聊模式就是群聊模式的数据（重启后同样成立）
try:
    _view_scope = "claude" if current_config.get("claude_enabled") else "main"
except Exception:
    _view_scope = "main"

WORLD_BOOK_FILE = Path("world_book.json")


def load_world_book() -> list:
    """加载世界书与角色人设设定列表"""
    if not WORLD_BOOK_FILE.exists():
        initial = [
            {
                "id": "wb_01",
                "title": "专属伴侣人设",
                "status": "blue",
                "keywords": "你, 名字, 身份, 伴侣, 角色",
                "content": "你是用户的专属二次元亲密伙伴，温柔体贴，偶尔撒娇娇憨，全心全意陪伴用户。",
                "enabled": True
            },
            {
                "id": "wb_02",
                "title": "世界观与场景设定",
                "status": "green",
                "keywords": "世界, 场景, 房间, 水榭, 霞芒宫, 学院",
                "content": "故事发生在一个充满幻想色彩的唯美世界中，你与用户处于舒适温馨的私人场景里，彼此信赖。",
                "enabled": True
            }
        ]
        try:
            WORLD_BOOK_FILE.write_text(json.dumps(initial, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return initial
    try:
        with open(WORLD_BOOK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"读取 world_book.json 失败: {e}")
        return []


def save_world_book(data: list):
    """持久化保存世界书与角色人设设定列表"""
    with open(WORLD_BOOK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


app = FastAPI(title="QQX_AIBot - OneBot v11 AI 伴侣")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 维护活动的 WebSocket UI 客户端连接
active_websockets: Set[WebSocket] = set()

# NapCat 子进程管理变量
napcat_process: asyncio.subprocess.Process = None
# NapCat 启动时打印的「带 token 的 WebUI 链接」，抓到就用它（比配置文件里的 token 更准）
napcat_webui_live: str = ""
napcat_console_tail: list = []      # NapCat 最近的输出（找登录密钥用）
napcat_hints: list = []             # 疑似含密钥/token 的行
napcat_reader_task: asyncio.Task = None


async def broadcast_ws(payload: dict):
    """向所有连接的前端 Web UI 推送消息"""
    if not active_websockets:
        return
    dead_sockets = set()
    for ws in list(active_websockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead_sockets.add(ws)
    for ws in dead_sockets:
        active_websockets.discard(ws)


async def log_publisher(level: str, message: str):
    """供 OneBotClient 和系统调用的统一日志广播接口"""
    await broadcast_ws({
        "type": "log",
        "level": level,
        "message": message
    })


async def chat_event_publisher(action: str, payload: dict):
    """把上下文存储的变更（新增 / 删除 / 清空消息）实时广播给前端聊天记录窗口"""
    await broadcast_ws({"type": action, **payload})


async def memory_event_publisher(action: str, payload: dict):
    """把记忆库的变更（新增 / 修改 / 删除 / 清空）实时广播给前端记忆库面板"""
    await broadcast_ws({"type": action, **payload})


# 初始化 OneBot 核心客户端并装载世界书与角色设定
bot_client = OneBotClient(current_config, log_callback=log_publisher,
                          history_event_callback=chat_event_publisher,
                          memory_event_callback=memory_event_publisher)
bot_client.update_world_book(load_world_book())

# ==================== 库切换：面板看「主 Bot」还是「群聊」 ====================
_view_scope = "main"


def _claude_mode():
    return getattr(bot_client, "claude", None)


def _scope_stores(scope: str = ""):
    """某个范围对应的 (history, memory, notebook)；不传就用面板当前看的那个。"""
    if str(scope or _view_scope) == "claude":
        cm = _claude_mode()
        if cm is not None:
            return cm.history, cm.memory, cm.notebook
    return bot_client.history_store, bot_client.memory_store, getattr(bot_client, "notebook_store", None)


def _hist(scope: str = ""):
    return _scope_stores(scope)[0]


def _mem(scope: str = ""):
    return _scope_stores(scope)[1]


def _nb(scope: str = ""):
    return _scope_stores(scope)[2]


def _claude_stores():
    """群聊那一套 store；群聊模式没起来时返回 None。"""
    cm = _claude_mode()
    if cm is None:
        return None
    return (cm.history, cm.memory, cm.notebook)


def _scope_pair(scope: str = ""):
    """数据源三档 → [(scope名, (history, memory, notebook)), ...]。

    main = 主 Bot（私聊）；claude = 群聊模式；all = 两套都返回（不存在的不加）。
    记忆库 / 笔记本页签用这个，跟全局 _view_scope 解耦 —— 页签自己选的数据源
    不会因为别处开了群聊模式而被换掉。
    """
    sc = str(scope or "main").strip().lower()
    if sc == "all":
        out = [("main", _scope_stores("main"))]
        c = _claude_stores()
        if c is not None:
            out.append(("claude", c))
        return out
    if sc == "claude":
        c = _claude_stores()
        if c is not None:
            return [("claude", c)]
        return [("main", _scope_stores("main"))]
    return [("main", _scope_stores("main"))]


def _nb_cross_on(scope: str, uid: Any = None) -> bool:
    """笔记本要不要跨数据源注入：开关开了，且（白名单为空 或 uid 在白名单里）。"""
    sc = "claude" if str(scope or "").lower() == "claude" else "main"
    pre = "claude_" if sc == "claude" else ""
    if not bool(current_config.get(pre + "notebook_cross_inject")):
        return False
    allow = [str(x) for x in (current_config.get(pre + "notebook_cross_allow") or [])]
    if not allow:
        return True
    return uid is not None and str(uid) in allow


def _wb_path() -> Path:
    if _view_scope == "claude":
        cm = _claude_mode()
        if cm is not None:
            return Path(cm.data_dir) / "world_book.json"
    return WORLD_BOOK_FILE


def _wb_load() -> list:
    p = _wb_path()
    if p == WORLD_BOOK_FILE:
        return load_world_book()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or []
    except Exception as e:
        logger.warning(f"读取 {p} 失败: {e}")
    return []


def _wb_save(entries: list):
    p = _wb_path()
    if p == WORLD_BOOK_FILE:
        save_world_book(entries)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _wb_apply(entries: list):
    if _view_scope == "claude":
        bot_client.update_claude_world_book(entries)
    else:
        bot_client.update_world_book(entries)


@app.get("/api/scope")
async def get_scope():
    return {"status": "ok", "scope": _view_scope, "claude_ready": _claude_mode() is not None}


@app.post("/api/scope")
async def set_scope(payload: Dict[str, Any] = Body(None)):
    """切换面板查看的库：main=主 Bot，claude=群聊模式。"""
    global _view_scope
    sc = str(((payload or {}).get("scope")) or "main").strip().lower()
    if sc not in ("main", "claude"):
        return {"status": "error", "message": "scope 只能是 main / claude"}
    # 尊重前端传进来的选择；只在群聊模式没开、却要切到 claude 时才拦回 main
    if sc == "claude" and not current_config.get("claude_enabled"):
        _view_scope = "main"
    else:
        _view_scope = sc
    return {"status": "ok", "scope": _view_scope}


# ---------------- API 路由 ----------------

@app.get("/")
async def get_index():
    """提供 Web UI 单页"""
    index_path = Path("index.html")
    if not index_path.exists():
        return JSONResponse({"status": "error", "message": "index.html 未找到"}, status_code=404)
    return FileResponse("index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/config")
async def get_config():
    """获取当前配置"""
    return current_config


@app.get("/api/mood")
async def mood_status():
    """情绪注入：当前情绪状况 + 精力/情欲/心情。"""
    try:
        import mood as _MD
        _d = _MD.status(current_config)
        _d["prompts"] = {"mood": _MD.DEFAULTS.get("mood_prompt", "")}
        return {"status": "ok", "data": _d}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": {}}


@app.post("/api/mood")
async def mood_set(payload: Dict[str, Any]):
    """手动修改情绪状况和三个数值。"""
    try:
        import mood as _MD
        _d = _MD.set_values(current_config,
                            word=payload.get("word"), desc=payload.get("desc"),
                            energy=payload.get("energy"), lust=payload.get("lust"),
                            mood=payload.get("mood"), note="面板手动修改")
        return {"status": "ok", "data": _d}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/idle")
async def idle_status():
    """闲置触发状态：闲置多久、当前概率、抽选日志。"""
    try:
        import idle as _ID
        _ID.seed_from_history()
        _d = _ID.status(current_config)
        _d["prompts"] = {"idle": _ID.DEFAULTS.get("idle_prompt_idle", ""),
                         "recall": _ID.DEFAULTS.get("idle_prompt_recall", ""),
                         "chat": _ID.DEFAULTS.get("idle_prompt_chat_recall", "")}
        return {"status": "ok", "data": _d}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": {}}


@app.get("/api/active_msg")
async def active_msg_list():
    """主动消息列表（含已完成的灰色项）。"""
    try:
        import active_msg as _AM
        rows = _AM.all_rows()
        rows.sort(key=lambda r: (bool(r.get("done")), float(r.get("at") or 0)))
        return {"status": "ok", "enabled": _AM.enabled(current_config), "rows": rows}
    except Exception as e:
        return {"status": "error", "message": str(e), "rows": []}


@app.post("/api/active_msg")
async def active_msg_op(payload: Dict[str, Any] = Body(None)):
    """主动消息：删除 / 清掉已完成 / 手动排一条。"""
    try:
        import active_msg as _AM
        d = payload or {}
        act = str(d.get("action") or "")
        if act == "delete":
            return {"status": "ok", "removed": _AM.remove(str(d.get("id") or ""))}
        if act == "clear_done":
            return {"status": "ok", "removed": _AM.clear(True)}
        if act == "create":
            mid = _AM.add(current_config, float(d.get("at") or 0), str(d.get("text") or ""),
                          str(d.get("kind") or "private"), str(d.get("chat_id") or ""), "手动")
            return {"status": "ok", "id": mid}
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/config")
async def update_config(new_conf: Dict[str, Any]):
    """更新配置"""
    current_config.update(new_conf)
    # 这里原本会把 _view_scope 重置成「跟着 claude_enabled 走」，
    # 导致用户手动切到主 Bot 后，一保存配置就被拉回群聊。数据源改由 /api/scope 决定，不再动它。
    save_config(current_config)
    bot_client.update_config(current_config)
    await log_publisher("SUCCESS", "系统配置已保存并热加载")
    return {"status": "ok", "config": current_config}
    _lwb_rebind_callers()


@app.post("/api/debug_mode")
async def set_debug_mode(payload: Dict[str, Any] = Body(None)):
    """切换「调试模式」：只在控制台打印发往 API 的请求与返回内容，不改动上下文组装与回写。"""
    data = payload or {}
    enabled = bool(data.get("enabled", not bool(current_config.get("debug_mode", False))))
    current_config["debug_mode"] = enabled
    save_config(current_config)
    bot_client.update_config(current_config)
    if enabled:
        await log_publisher("WARN", "[调试模式] 已开启：发往 API 的完整报文与返回内容会实时打印到控制台（不影响历史注入与回写）")
    else:
        await log_publisher("SUCCESS", "[调试模式] 已关闭：不再打印 API 报文")
    return {"status": "ok", "debug_mode": enabled}


@app.post("/api/test_mode")
async def set_test_mode(payload: Dict[str, Any] = Body(None)):
    '''切换「测试模式」：开启后不注入任何历史与长期记忆，本轮上下文也不回写聊天记录，
    并把每次发往 API 的完整报文与返回内容全量打印到控制台。'''
    data = payload or {}
    enabled = bool(data.get("enabled", not bool(current_config.get("test_mode", False))))
    current_config["test_mode"] = enabled
    save_config(current_config)
    bot_client.update_config(current_config)
    if enabled:
        await log_publisher("WARN", "[测试模式] 已开启：不注入历史与长期记忆、上下文不回写、报文全量打印到控制台")
    else:
        await log_publisher("SUCCESS", "[测试模式] 已关闭：恢复正常上下文组装与回写")
    return {"status": "ok", "test_mode": enabled}


# ---------------------------------------------------------------- 主对话 API 配置档

# 一套「主对话 API 配置」包含的字段；保存/切换都以这几个为准。
LLM_PROFILE_FIELDS = ("llm_base_url", "llm_api_key", "llm_model", "temperature")


def _llm_profile_rows() -> list:
    rows = current_config.get("llm_profiles")
    return rows if isinstance(rows, list) else []


# ---------------------------------------------------------------- 笔记本

# ------------------------------------------------------- 笔记本 导出 / 导入

NB_EXPORT_FORMAT = "qqbotx.notebook"
NB_EXPORT_VERSION = 1


def _nb_raw(st) -> list:
    """取笔记本底层条目列表（含正文），拿不到就返回空表。"""
    d = getattr(st, "_data", None)
    items = d.get("items") if isinstance(d, dict) else None
    return items if isinstance(items, list) else []


def _nb_fp(it) -> str:
    """内容指纹：kind + 标题 + 正文，用于导入去重。"""
    if not isinstance(it, dict):
        return ""
    return "%s|%s|%s" % (str(it.get("kind") or ""), str(it.get("title") or "").strip(),
                         str(it.get("content") or "").strip())


@app.get("/api/notebook/export")
async def notebook_export(kind: str = "", scope: str = ""):
    """导出笔记本条目。kind 留空 = 全部；可只导 note（笔记）或 memo（速记）。"""
    _sc = str(scope or "").strip().lower()
    k = str(kind or "").strip()
    if k not in ("", "note", "memo"):
        k = ""
    raw = []
    _got = False
    for _s, (_h, _m, _st) in (_scope_pair("all") if _sc == "all" else [(str(scope or _view_scope), _scope_stores(scope))]):
        if _st is None:
            continue
        _got = True
        with _st._lock:
            raw.extend(dict(it) for it in _nb_raw(_st)
                       if isinstance(it, dict) and str(it.get("content") or "").strip())
    if not _got:
        return JSONResponse({"status": "error", "message": "笔记本未初始化"}, status_code=500)
    if k:
        raw = [it for it in raw if str(it.get("kind") or "") == k]
    notes = sum(1 for it in raw if str(it.get("kind") or "") == "note")
    memos = sum(1 for it in raw if str(it.get("kind") or "") == "memo")
    payload = {
        "format": NB_EXPORT_FORMAT,
        "version": NB_EXPORT_VERSION,
        "scope": _sc or str(scope or _view_scope),
        "kind": k,
        "exported_at": time.time(),
        "exported_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_items": len(raw),
        "total_notes": notes,
        "total_memos": memos,
        "items": raw,
    }
    await log_publisher("SUCCESS", "[笔记本] 导出 %d 条（笔记 %d / 速记 %d）" % (len(raw), notes, memos))
    return payload


@app.post("/api/notebook/import")
async def notebook_import(body: dict = Body(...), scope: str = ""):
    """把导出的 JSON 合并进当前笔记本。

    只增不改不删：按条目 id + 内容指纹去重，重复的跳过。
    导入条目的 short_id 一律置 0，交给 NotebookStore 重新分配，避免与现有条目撞号。
    """
    st = _nb(scope)
    if st is None:
        return JSONResponse({"status": "error", "message": "笔记本未初始化"}, status_code=500)

    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return JSONResponse({"status": "error", "message": "文件不是合法的 JSON"}, status_code=400)
    if not isinstance(data, dict) or not data:
        return JSONResponse({"status": "error", "message": "文件内容不是合法的 JSON 对象"}, status_code=400)

    items = data.get("items")
    if not isinstance(items, list):
        items = data.get("entries")       # 兼容别的写法
    if not isinstance(items, list):
        return JSONResponse({"status": "error",
                             "message": "这个文件里没有 items 列表，不是笔记本导出文件"},
                            status_code=400)

    added = skipped = 0
    with st._lock:
        bucket = _nb_raw(st)
        have_ids = {str(x.get("id")) for x in bucket if isinstance(x, dict) and x.get("id")}
        have_fp = {_nb_fp(x) for x in bucket if isinstance(x, dict)}
        for one in items:
            if not isinstance(one, dict):
                continue
            content = str(one.get("content") or "").strip()
            if not content:
                skipped += 1
                continue
            iid = str(one.get("id") or "").strip()
            if iid and iid in have_ids:
                skipped += 1
                continue
            fp = _nb_fp(one)
            if fp and fp in have_fp:
                skipped += 1
                continue
            kk = str(one.get("kind") or "")
            mt = str(one.get("memory_type") or "")
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            new = {
                "id": iid,
                "short_id": 0,            # 交给 _ensure_ids 重新分配
                "kind": "note" if kk == "note" else "memo",
                "title": str(one.get("title") or "")[:200],
                "content": content,
                "tags": [str(t) for t in (one.get("tags") or []) if str(t).strip()],
                "memory_type": mt if mt in ("knowledge", "skill", "emotional", "event") else "knowledge",
                "reasoning": str(one.get("reasoning") or ""),
                "scope": str(one.get("scope") or "") or "global",
                "source": str(one.get("source") or "") or "import",
                "created_at": str(one.get("created_at") or "") or now,
                "updated_at": str(one.get("updated_at") or "") or now,
            }
            bucket.append(new)
            if iid:
                have_ids.add(iid)
            have_fp.add(fp)
            added += 1
        if added:
            st._ensure_ids()
            st._save()
    await log_publisher("SUCCESS", "[笔记本] 导入完成：新增 %d 条、跳过 %d 条（重复/空）" % (added, skipped))
    return {"status": "ok", "added": added, "skipped": skipped,
            "message": "新增 %d 条，跳过 %d 条重复" % (added, skipped)}


@app.get("/api/notebook/items")
async def notebook_items(kind: str = "", scope: str = ""):
    """列出笔记本条目（不含正文，只给预览）。scope=all 时两个库拼一起当一个库看。"""
    _sc = str(scope or "").strip().lower()
    if _sc == "all":
        items = []
        stats = {"total": 0, "notes": 0, "memos": 0, "chars": 0}
        for s, (_h, _m, st) in _scope_pair("all"):
            if st is None:
                continue
            for it in (st.list_items(kind=kind) or []):
                it["from_scope"] = s
                items.append(it)
            _s = st.stats() or {}
            for k in ("total", "notes", "memos", "chars"):
                stats[k] = stats.get(k, 0) + int(_s.get(k, 0) or 0)
        return {"status": "ok", "items": items, "stats": stats, "scope": "all"}
    st = _nb(_sc or _view_scope)
    if st is None:
        return {"status": "error", "message": "笔记本未初始化"}
    return {"status": "ok", "items": st.list_items(kind=kind), "stats": st.stats()}


@app.get("/api/notebook/item/{nid}")
async def notebook_item(nid: str, scope: str = ""):
    """取一条完整内容（含正文）。scope=all 时两个库都找一遍。"""
    _sc = str(scope or "").strip().lower()
    st = _nb(_sc or _view_scope)
    if _sc == "all":
        for _s, (_h, _m, _st) in _scope_pair("all"):
            if _st is not None and _st.get(nid) is not None:
                st = _st
                break
    if st is None:
        return {"status": "error", "message": "笔记本未初始化"}
    it = st.get(nid)
    if it is None:
        return {"status": "not_found", "message": "没有这条笔记：%s" % nid}
    return {"status": "ok", "item": it, "sections": st.sections(it.get("content") or "")}


@app.post("/api/notebook/item")
async def notebook_create(payload: Dict[str, Any]):
    """手动新建一条（note 或 memo）。"""
    st = _nb()
    if st is None:
        return {"status": "error", "message": "笔记本未初始化"}
    try:
        it = st.create(
            kind=str(payload.get("kind") or "note"),
            title=str(payload.get("title") or ""),
            content=str(payload.get("content") or ""),
            tags=payload.get("tags") or [],
            memory_type=str(payload.get("memory_type") or "knowledge"),
            reasoning=str(payload.get("reasoning") or ""),
            scope=str(payload.get("scope") or "global"),
            source="user",
        )
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    await log_publisher("SUCCESS", "笔记本新建《%s》" % it.get("title"))
    return {"status": "ok", "item": it}


@app.put("/api/notebook/item/{nid}")
async def notebook_update(nid: str, payload: Dict[str, Any]):
    """改一条。"""
    st = _nb()
    if st is None:
        return {"status": "error", "message": "笔记本未初始化"}
    fields = {k: v for k, v in (payload or {}).items()
              if k in ("title", "content", "tags", "memory_type", "reasoning", "kind")}
    if not fields:
        return {"status": "error", "message": "没有可改的字段"}
    it = st.update(nid, **fields)
    if it is None:
        return {"status": "not_found", "message": "没有这条笔记：%s" % nid}
    await log_publisher("SUCCESS", "笔记本已更新《%s》" % it.get("title"))
    return {"status": "ok", "item": it}


@app.delete("/api/notebook/item/{nid}")
async def notebook_delete(nid: str, scope: str = ""):
    """删一条。scope=all 时两个库都找一遍。"""
    _sc = str(scope or "").strip().lower()
    st = _nb(_sc or _view_scope)
    if _sc == "all":
        for _s, (_h, _m, _st) in _scope_pair("all"):
            if _st is not None and _st.get(nid) is not None:
                st = _st
                break
    if st is None:
        return {"status": "error", "message": "笔记本未初始化"}
    if not st.delete(nid):
        return {"status": "not_found", "message": "没有这条笔记：%s" % nid}
    await log_publisher("INFO", "笔记本已删除条目 %s" % nid)
    return {"status": "ok"}


@app.post("/api/notebook/tools/test")
async def notebook_tools_test():
    """工具自检：真发一次带 tools 的请求，看当前主对话 API 支不支持 Function Calling。"""
    import aiohttp
    base_url = str(current_config.get("llm_base_url") or "").rstrip("/")
    api_key = str(current_config.get("llm_api_key") or "").strip()
    model = str(current_config.get("llm_model") or "").strip()
    if not base_url or not api_key:
        return {"status": "error", "message": "主对话 API 还没配好（地址或密钥为空）"}
    if base_url.endswith("/chat/completions"):
        endpoint = base_url
    elif base_url.endswith("/v1"):
        endpoint = base_url + "/chat/completions"
    else:
        endpoint = base_url + "/v1/chat/completions"

    import notebook_tools as _nb
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是记忆助理。用户要你回忆一件事，请调用工具检索。"},
            {"role": "user", "content": "帮我回忆一下我们之前聊过什么。"},
        ],
        "tools": _nb.tool_schemas(),
        "tool_choice": "auto",
        "temperature": 0.2,
        "stream": False,
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as sess:
            async with sess.post(endpoint,
                                 headers={"Authorization": f"Bearer {api_key}",
                                          "Content-Type": "application/json"},
                                 json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return {"status": "error", "supported": False,
                            "message": "HTTP %s：%s" % (resp.status, body[:300])}
                data = await resp.json()
    except Exception as e:
        return {"status": "error", "supported": False, "message": "请求失败：%s" % e}

    try:
        msg = (data.get("choices") or [{}])[0].get("message") or {}
    except Exception:
        msg = {}
    tcs = msg.get("tool_calls") or []
    called = [str((t.get("function") or {}).get("name")) for t in tcs]
    supported = bool(called)
    await log_publisher("INFO" if supported else "WARN",
                        "[笔记本] 工具自检：%s" % ("通过，模型调用了 " + "、".join(called)
                                                 if supported else "模型没有发起工具调用"))
    return {
        "status": "ok",
        "supported": supported,
        "called": called,
        "model": model,
        "endpoint": endpoint,
        "message": ("✅ 当前模型支持 Function Calling，模型主动调用了 " + "、".join(called))
                   if supported else
                   ("⚠️ 接口通了，但模型没有发起工具调用。可能是该模型/中转站不支持 tools 参数，"
                    "或把工具当成了普通文本。工具会静默失效。"),
        "raw_content": str(msg.get("content") or "")[:300],
    }


@app.get("/api/llm/profiles")
async def llm_profiles_list():
    """列出已保存的主对话 API 配置档 + 当前生效的那套。"""
    return {"status": "ok", "profiles": _llm_profile_rows(),
            "active": {k: current_config.get(k) for k in LLM_PROFILE_FIELDS}}


@app.post("/api/llm/profiles")
async def llm_profile_save(payload: Dict[str, Any]):
    """把一套主对话 API 配置存成具名配置档（同名覆盖）。"""
    name = str(payload.get("name") or "").strip()
    if not name:
        return {"status": "error", "message": "配置档名字不能为空"}
    row = {"name": name}
    for k in LLM_PROFILE_FIELDS:
        row[k] = payload.get(k, current_config.get(k))
    rows = [r for r in _llm_profile_rows() if str(r.get("name")) != name]
    rows.append(row)
    current_config["llm_profiles"] = rows
    save_config(current_config)
    await log_publisher("SUCCESS", "主对话 API 配置档已保存：%s（%s）" % (name, row.get("llm_model")))
    return {"status": "ok", "profiles": rows}


@app.post("/api/llm/profiles/apply")
async def llm_profile_apply(payload: Dict[str, Any]):
    """切换到某个配置档：直接写进当前配置并热加载，不重启。"""
    name = str(payload.get("name") or "").strip()
    row = next((r for r in _llm_profile_rows() if str(r.get("name")) == name), None)
    if row is None:
        return {"status": "not_found", "message": "没有这个配置档：%s" % name}
    for k in LLM_PROFILE_FIELDS:
        if k in row:
            current_config[k] = row[k]
    save_config(current_config)
    bot_client.update_config(current_config)
    await log_publisher("SUCCESS", "已切换到主对话 API 配置档：%s（%s）"
                        % (name, row.get("llm_model")))
    return {"status": "ok", "active": {k: current_config.get(k) for k in LLM_PROFILE_FIELDS}}


@app.post("/api/llm/profiles/rename")
async def llm_profile_rename(payload: Dict[str, Any]):
    """改配置档的名字（只改存档名，不动当前生效的配置）。"""
    old = str(payload.get("old") or "").strip()
    new = str(payload.get("new") or "").strip()
    if not old or not new:
        return {"status": "error", "message": "旧名和新名都要有"}
    rows = _llm_profile_rows()
    if new == old:
        return {"status": "ok", "profiles": rows}
    hit = next((r for r in rows if str(r.get("name")) == old), None)
    if hit is None:
        return {"status": "not_found", "message": "没有这个配置档：%s" % old}
    if any(str(r.get("name")) == new for r in rows):
        return {"status": "error", "message": f"已经有叫「{new}」的配置档了"}
    hit["name"] = new
    current_config["llm_profiles"] = rows
    save_config(current_config)
    await log_publisher("SUCCESS", "主对话 API 配置档已重命名：%s → %s" % (old, new))
    return {"status": "ok", "profiles": rows}

@app.delete("/api/llm/profiles/{name}")
async def llm_profile_delete(name: str):
    """删掉一个配置档（只删存档，不动当前生效的配置）。"""
    rows = _llm_profile_rows()
    left = [r for r in rows if str(r.get("name")) != str(name)]
    if len(left) == len(rows):
        return {"status": "not_found", "message": "没有这个配置档：%s" % name}
    current_config["llm_profiles"] = left
    save_config(current_config)
    await log_publisher("INFO", "已删除主对话 API 配置档：%s" % name)
    return {"status": "ok", "profiles": left}


@app.get("/api/world_book")
async def get_world_book():
    """获取当前世界书与角色人设设定列表"""
    entries = _wb_load()
    return {"status": "ok", "entries": entries}


@app.post("/api/world_book")
async def update_world_book_endpoint(payload: Dict[str, Any]):
    """保存并热加载世界书与角色人设设定列表（蓝绿灯机制）"""
    entries = payload.get("entries", [])
    _wb_save(entries)
    _wb_apply(entries)
    
    # 统计蓝灯与绿灯数量
    blue_count = sum(1 for e in entries if e.get("enabled", True) and e.get("status") == "blue")
    green_count = sum(1 for e in entries if e.get("enabled", True) and e.get("status") == "green")
    await log_publisher("SUCCESS", f"[世界书引擎] 已保存并热加载设定集：常驻蓝灯 {blue_count} 个，动态绿灯 {green_count} 个")
    return {"status": "ok", "entries": entries}


@app.get("/api/world_book/export")
async def world_book_export():
    """导出当前数据源（主 Bot / 群聊模式）的世界书为 JSON。"""
    entries = _wb_load()
    _sc = str(_view_scope or "main")
    payload = {
        "format": "qqbotx.worldbook",
        "version": 1,
        "scope": _sc,
        "exported_at": time.time(),
        "exported_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_items": len(entries),
        "entries": entries,
    }
    await log_publisher("SUCCESS", "[世界书] 导出 %d 条（%s）"
                        % (len(entries), "群聊模式" if _sc == "claude" else "主 Bot"))
    return payload


def _wb_fp(entry: dict) -> str:
    """条目指纹：整条内容做键，用来在合并导入时跳过重复项。"""
    try:
        return json.dumps(entry, ensure_ascii=False, sort_keys=True)
    except Exception:
        return ""


@app.post("/api/world_book/import")
async def world_book_import(body: Dict[str, Any] = Body(...)):
    """把导出的 JSON 导入当前数据源的世界书。

    mode=replace（默认）整份替换；mode=merge 合并追加，重复的跳过。
    """
    body = body or {}
    mode = str(body.get("mode") or "replace").strip().lower()
    if mode not in ("replace", "merge"):
        mode = "replace"
    data = body.get("data") if isinstance(body.get("data"), (dict, str, list)) else body
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return JSONResponse({"status": "error", "message": "文件不是合法的 JSON"}, status_code=400)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("entries")
        if not isinstance(items, list):
            items = data.get("items")
    else:
        items = None
    if not isinstance(items, list):
        return JSONResponse({"status": "error",
                             "message": "这个文件里没有 entries 列表，不是世界书导出文件"},
                            status_code=400)
    items = [dict(e) for e in items if isinstance(e, dict)]
    # replace 允许空列表（= 用空世界书清空回来）；merge 时空列表没有意义
    if not items and mode != "replace":
        return JSONResponse({"status": "error", "message": "文件里没有可导入的设定条目"},
                            status_code=400)
    cur = _wb_load()
    if mode == "merge":
        have_ids = {str(e.get("id") or "") for e in cur if isinstance(e, dict) and e.get("id")}
        have_fp = {_wb_fp(e) for e in cur if isinstance(e, dict)}
        entries = list(cur)
        added = skipped = 0
        for e in items:
            iid = str(e.get("id") or "")
            fp = _wb_fp(e)
            if (iid and iid in have_ids) or (fp and fp in have_fp):
                skipped += 1
                continue
            entries.append(e)
            if iid:
                have_ids.add(iid)
            if fp:
                have_fp.add(fp)
            added += 1
        note = "新增 %d 条、跳过重复 %d 条（原有 %d 条）" % (added, skipped, len(cur))
    else:
        entries = items
        note = "整份替换 %d → %d 条" % (len(cur), len(items))
    _wb_save(entries)
    _wb_apply(entries)
    await log_publisher("SUCCESS", "[世界书] 导入完成（%s）：%s"
                        % ("群聊模式" if str(_view_scope) == "claude" else "主 Bot", note))
    return {"status": "ok", "mode": mode, "entries": entries,
            "total": len(entries), "note": note}


@app.post("/api/models")
async def get_models(payload: Dict[str, Any] = None):
    """请求远端 OpenAI 规范的 /v1/models 接口获取支持的模型列表"""
    data = payload or {}
    base_url = (data.get("base_url") or current_config.get("llm_base_url", "")).strip()
    api_key = (data.get("api_key") or current_config.get("llm_api_key", "")).strip()
    if not base_url:
        return JSONResponse({"status": "error", "message": "LLM Base URL 不能为空"}, status_code=400)
    try:
        models = await fetch_available_models(base_url, api_key)
        return {"status": "ok", "models": models}
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"获取远端模型列表失败: {e}"}, status_code=500)


@app.get("/api/debug/last_payload")
async def get_last_payload():
    """获取最近一次发送给大模型 API 的完整 Prompt 报文与响应结果"""
    return last_debug_info


async def _fill_group_names(convs: list) -> None:
    """给群聊（群聊模式数据源）的会话补上真实群名，私聊会话不动。

    群号当名字显示没有意义，这里把群名查回来。查不到就留空，界面自己回退。
    """
    for c in convs or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("from_scope") or "") != "claude" and not c.get("is_group"):
            continue
        try:
            c["group_name"] = await bot_client.get_group_name(c.get("user_id"))
        except Exception:
            c["group_name"] = ""


# ---------------- 对话上下文（历史记录）API ----------------

@app.get("/api/history")
async def get_history_list(scope: str = "all"):
    """获取所有会话（按用户）的上下文摘要，供聊天记录窗口的会话列表使用。

    默认 scope=all：主 Bot 与群聊两个数据源的会话全部列出来，每条标出来源。
    """
    _sc = str(scope or "all").strip().lower()
    convs = []
    total = 0
    for _s, (_hist_st, _m, _n) in _scope_pair(_sc):
        for _c in (_hist_st.list_conversations() or []):
            _c = dict(_c)
            _c["is_group"] = (_s == "claude")
            _c["from_scope"] = _s
            convs.append(_c)
        try:
            total += int(_hist_st.total_messages() or 0)
        except Exception:
            pass
    await _fill_group_names(convs)
    return {"status": "ok", "scope": _sc, "conversations": convs, "total": total}


@app.get("/api/history/{user_id}")
async def get_history_detail(user_id: str, scope: str = ""):
    """获取指定用户的完整对话上下文（含每条消息的 id / 角色 / 内容 / 时间戳）。

    scope=all 时两个数据源都找一遍，返回有消息的那一份。
    """
    _sc = str(scope or "").strip().lower()
    if _sc == "all":
        conv = None
        for _s, (_hst, _m, _n) in _scope_pair("all"):
            _c = _hst.get_conversation(user_id)
            if isinstance(_c, dict) and (_c.get("messages") or []):
                _c = dict(_c)
                _c["is_group"] = (_s == "claude")
                _c["from_scope"] = _s
                conv = _c
                break
        if conv is None:
            _c = _hist("main").get_conversation(user_id)
            conv = dict(_c) if isinstance(_c, dict) else _c
            if isinstance(conv, dict):
                conv["is_group"] = False
                conv["from_scope"] = "main"
        await _fill_group_names([conv] if isinstance(conv, dict) else [])
        return {"status": "ok", "conversation": conv}
    _s = _sc if _sc in ("main", "claude") else _view_scope
    conv = _hist(_s).get_conversation(user_id)
    if isinstance(conv, dict):
        conv["is_group"] = (_s == "claude")
        conv["from_scope"] = _s
    await _fill_group_names([conv] if isinstance(conv, dict) else [])
    return {"status": "ok", "conversation": conv}


@app.delete("/api/history/{user_id}/message/{message_id}")
async def delete_history_message(user_id: str, message_id: str, scope: str = ""):
    """从上下文（history.json）中彻底抹除单条消息，删除后不再参与 Prompt 组装"""
    _s = str(scope or "").strip().lower()
    _s = _s if _s in ("main", "claude") else _view_scope
    ok = _hist(_s).delete_message(user_id, message_id)
    if ok:
        await log_publisher("INFO", f"[上下文管理] 已从上下文中彻底抹除消息 {message_id} (用户 {user_id})")
    return {"status": "ok" if ok else "not_found", "deleted": ok}


@app.put("/api/history/{user_id}/message/{message_id}")
async def update_history_message(user_id: str, message_id: str, payload: Dict[str, Any] = Body(...), scope: str = ""):
    """修改上下文中某条消息的内容；改完立即落盘，之后组装 Prompt 时使用新内容"""
    if not isinstance(payload, dict) or "content" not in payload:
        raise HTTPException(status_code=400, detail="请求体缺少 content 字段")
    _s = str(scope or "").strip().lower()
    _s = _s if _s in ("main", "claude") else _view_scope
    msg = _hist(_s).update_message(user_id, message_id, payload.get("content"))
    if msg is None:
        return {"status": "not_found", "updated": False}
    await log_publisher("INFO", f"[上下文管理] 已修改消息 {message_id} 的内容 (用户 {user_id})")
    return {"status": "ok", "updated": True, "message": msg}


@app.post("/api/history/{user_id}/message/{message_id}/hide")
async def hide_history_message(user_id: str, message_id: str, payload: Dict[str, Any] = Body(None), scope: str = ""):
    '''隐藏 / 取消隐藏单条消息：页面照常显示，但不参与 Prompt 组装（等价于只给自己看）'''
    hidden = True if payload is None else bool(payload.get("hidden", True))
    _s = str(scope or "").strip().lower()
    _s = _s if _s in ("main", "claude") else _view_scope
    msg = _hist(_s).set_hidden(user_id, message_id, hidden)
    if msg is None:
        return {"status": "not_found", "hidden": None}
    await log_publisher("INFO", f"[上下文管理] 已{'隐藏' if hidden else '取消隐藏'}消息 {message_id} (用户 {user_id})")
    return {"status": "ok", "hidden": hidden, "message": msg}


@app.delete("/api/history/{user_id}")
async def clear_history(user_id: str, scope: str = ""):
    """清空指定用户的全部上下文"""
    _s = str(scope or "").strip().lower()
    _s = _s if _s in ("main", "claude") else _view_scope
    ok = _hist(_s).clear(user_id)
    await log_publisher("WARN", f"[上下文管理] 已清空用户 {user_id} 的全部上下文")
    return {"status": "ok", "cleared": ok}


# ---------------- 长期记忆库（向量版）API ----------------

def _uid(user_id):
    """路径里的 user_id 统一还原成 int，保证与 history.json / memory.json 的键一致"""
    s = str(user_id)
    return int(s) if s.lstrip("-").isdigit() else s


# 全局视图用的聚合作用域——它本身不存条目，只把各会话汇总出来。
# 在这个视图下删/改记忆，必须先解析出条目真正属于哪个会话，否则必然 not_found。
_LWB_ALL_SCOPES = ("all", "global")


def _lwb_is_all_scope(user_id) -> bool:
    return str(user_id or "").strip().lower() in _LWB_ALL_SCOPES



def _ts_parts(ts) -> Dict[str, str]:
    """epoch 秒 -> {msg_date: 2026-10-14, msg_hm: 23:13, msg_full: 2026-10-14 23:13}"""
    try:
        v = float(ts or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0:
        return {"msg_date": "", "msg_hm": "", "msg_full": ""}
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(v)
    return {"msg_date": d.strftime("%Y-%m-%d"), "msg_hm": d.strftime("%H:%M"),
            "msg_full": d.strftime("%Y-%m-%d %H:%M")}


def _lwb_item_msg_ts(it: Dict[str, Any], msgs: List[Dict[str, Any]]) -> float:
    """条目 -> 它对应的那条消息的时间戳。

    floor_start / floor_end 是「消息下标」，优先用它回查真实消息时间；
    查不到（没有历史、下标越界）再退回条目自身的 time_ts / created_at。
    """
    # 落库时固化的真实消息时间优先：手动整理清空聊天记录后，回查不到也不会退化
    try:
        _v0 = float(it.get("msg_ts") or 0)
    except (TypeError, ValueError):
        _v0 = 0.0
    if _v0 > 0:
        return _v0
    for key in ("floor_start", "floor_end"):
        try:
            idx = int(it.get(key) or 0)
        except (TypeError, ValueError):
            idx = 0
        # 楼层号是 1 基的：第 N 楼 = messages[N-1]
        if 1 <= idx <= len(msgs):
            try:
                v = float(msgs[idx - 1].get("ts") or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                return v
    for key in ("time_ts", "created_at", "updated_at"):
        try:
            v = float(it.get(key) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    return 0.0

def _lwb_owner_of(entry_id: str):
    """在全局视图下找出某条记忆真正属于哪个会话；找不到返回 None。"""
    store = _mem()
    if store is None or not entry_id:
        return None
    try:
        convs = store.list_conversations()
    except Exception:
        convs = []
    for c in convs or []:
        uid = c.get("user_id")
        if uid is None:
            continue
        try:
            if store.get_entry(uid, entry_id):
                return _uid(uid)
        except Exception:
            pass
        # 兜底：个别 store 没实现 get_entry 时退回遍历
        try:
            for it in store.list_entries(uid):
                if str(it.get("id")) == str(entry_id):
                    return _uid(uid)
        except Exception:
            pass
    return None



# ---------------- 记忆库 导出 / 导入 ----------------
# 真实数据由记忆引擎（LwbStore）持有：
#   {会话key: {version, user_id, sender_name, cursor, items[], updated_at}}
# 引擎未接入时回退旧 MemoryStore.data（{..., entries[]}），两条路都兼容。
# 向量索引不参与导出，导入后点一次「重建向量」即可。

MEM_EXPORT_FORMAT = "qqbotx.memory"
MEM_EXPORT_VERSION = 2
_MEM_AGG_KEYS = ("all",)          # all 只是只读聚合视图，不是真实存储桶


def _mem_engine(st):
    """取记忆引擎门面（st.lwb）；未接入返回 None。"""
    lwb = getattr(st, "lwb", None)
    if lwb is not None and getattr(lwb, "store", None) is not None:
        return lwb
    return None


def _mem_lock(st):
    """与真实数据同一把锁，避免导入导出跟写入打架。"""
    lwb = _mem_engine(st)
    if lwb is not None:
        return lwb.store._lock
    return st._lock


def _mem_map(st):
    """{会话key: 会话dict}：新引擎走 conversations，旧结构走 data。"""
    lwb = _mem_engine(st)
    if lwb is not None:
        return lwb.store.conversations
    return getattr(st, "data", None) or {}


def _mem_items(conv):
    """取会话里的条目：新结构 items / 旧结构 entries。"""
    if not isinstance(conv, dict):
        return []
    for k in ("items", "entries"):
        v = conv.get(k)
        if isinstance(v, list):
            return v
    return []


def _mem_text(e):
    """条目正文（新结构 summary/content，旧结构 text）。"""
    if not isinstance(e, dict):
        return ""
    for k in ("summary", "content", "text"):
        v = e.get(k)
        if v and str(v).strip():
            return str(v)
    return ""


def _mem_fp(e):
    """内容指纹：两边各自生成过同一条时 id 会不同，靠正文兜住重复。"""
    raw = "%s|%s|%s|%s" % (str(e.get("kind") or ""), str(e.get("title") or ""),
                           _mem_text(e), str(e.get("name") or ""))
    return "".join(raw.split()).lower()


def _mem_norm(raw):
    """把导入条目规范化成新引擎的条目结构（旧结构的 text 补进 summary/content）。"""
    it = dict(raw or {})
    t = str(it.get("text") or "")
    if t:
        if not it.get("summary"):
            it["summary"] = t
        if not it.get("content"):
            it["content"] = t
    if not it.get("kind"):
        it["kind"] = "event"
    now = time.time()
    try:
        it["created_at"] = float(it.get("created_at") or now)
        it["updated_at"] = float(it.get("updated_at") or it["created_at"])
    except (TypeError, ValueError):
        it["created_at"] = it["updated_at"] = now
    for k in ("keywords", "entities", "locations"):
        v = it.get(k)
        if isinstance(v, str):
            it[k] = [s.strip() for s in v.replace("，", ",").split(",") if s.strip()]
        elif not isinstance(v, list):
            it[k] = []
    it.pop("text", None)
    it.pop("source_ids", None)
    try:
        from memory_engine.store import MemoryItem as _MI
        return _MI.from_dict(it).to_dict()
    except Exception:
        return it


@app.get("/api/memory/export")
async def memory_export(scope: str = ""):
    """导出当前记忆库全部内容（含跨会话共享桶），不含向量索引。"""
    st = _mem(scope)
    if st is None:
        return JSONResponse({"status": "error", "message": "记忆库不可用"}, status_code=500)
    sc = str(scope or _view_scope)
    convs = []
    with _mem_lock(st):
        for k, v in list(_mem_map(st).items()):
            key = str(k)
            if key in _MEM_AGG_KEYS:
                continue
            items = []
            for e in _mem_items(v):
                if isinstance(e, dict) and _mem_text(e):
                    items.append(dict(e))
            if not items:
                continue          # 空会话不导出，避免来回导入长出空桶
            convs.append({
                "key": key,
                "user_id": (v.get("user_id") if isinstance(v, dict) else None) or key,
                "sender_name": str((v.get("sender_name") if isinstance(v, dict) else "") or ""),
                "cursor": int((v.get("cursor") if isinstance(v, dict) else 0) or 0),
                "updated_at": (v.get("updated_at") if isinstance(v, dict) else 0) or 0,
                "entries": items,
            })
    convs.sort(key=lambda c: c.get("updated_at", 0) or 0, reverse=True)
    total = sum(len(c["entries"]) for c in convs)
    payload = {
        "format": MEM_EXPORT_FORMAT,
        "version": MEM_EXPORT_VERSION,
        "scope": sc,
        "exported_at": time.time(),
        "exported_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_conversations": len(convs),
        "total_entries": total,
        "conversations": convs,
    }
    await log_publisher("SUCCESS", "[记忆库] 导出 %d 个会话 / %d 条记忆" % (len(convs), total))
    return payload


@app.post("/api/memory/import")
async def memory_import(body: dict = Body(...), scope: str = ""):
    """把导出的 JSON 合并进当前记忆库。

    只增不改不删：按条目 id + 内容指纹去重，重复的跳过；游标只增不减。
    导入不带向量，导入完点一次「重建向量」即可。
    """
    st = _mem(scope)
    if st is None:
        return JSONResponse({"status": "error", "message": "记忆库不可用"}, status_code=500)

    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return JSONResponse({"status": "error", "message": "文件不是合法的 JSON"}, status_code=400)
    if not isinstance(data, dict) or not data:
        return JSONResponse({"status": "error", "message": "文件内容不是合法的 JSON 对象"}, status_code=400)

    convs = data.get("conversations")
    if not isinstance(convs, list):
        # 兼容直接丢一份裸 memory.json：{uid: {user_id, items/entries, ...}}
        cand = [k for k, v in data.items()
                if isinstance(v, dict) and ("items" in v or "entries" in v)]
        if cand:
            convs = [dict(data[k], key=k) for k in cand]
        else:
            return JSONResponse({"status": "error",
                                 "message": "这个文件里既没有 conversations，也不是 memory.json 的结构"},
                                status_code=400)

    lwb = _mem_engine(st)
    added = skipped = new_convs = 0
    detail = []
    touched = []
    with _mem_lock(st):
        cmap = _mem_map(st)
        for c in convs:
            if not isinstance(c, dict):
                continue
            key = str(c.get("key") or c.get("user_id") or "").strip()
            if not key or key in _MEM_AGG_KEYS:
                continue
            items = _mem_items(c)
            conv = cmap.get(key)
            if not isinstance(conv, dict):
                if not items:
                    continue      # 没内容就不凭空建桶
                conv = {"version": 2, "user_id": key, "sender_name": "", "cursor": 0,
                        "items": [], "updated_at": time.time()}
                cmap[key] = conv
                new_convs += 1
            elif not conv.get("sender_name") and c.get("sender_name"):
                conv["sender_name"] = str(c.get("sender_name"))
            if lwb is not None:
                bucket = conv.setdefault("items", [])
                conv.setdefault("version", 2)
            else:
                bucket = conv.setdefault("entries", [])
            have_ids = {str(x.get("id")) for x in bucket if isinstance(x, dict) and x.get("id")}
            have_fp = {_mem_fp(x) for x in bucket if isinstance(x, dict)}
            n_add = 0
            for one in items:
                if not isinstance(one, dict):
                    continue
                if not _mem_text(one):
                    skipped += 1
                    continue
                iid = str(one.get("id") or "")
                if iid and iid in have_ids:
                    skipped += 1
                    continue
                fp = _mem_fp(one)
                if fp and fp in have_fp:
                    skipped += 1
                    continue
                item = _mem_norm(one) if lwb is not None else st._ensure_entry(one)
                bucket.append(item)
                have_ids.add(str(item.get("id")))
                have_fp.add(fp)
                added += 1
                n_add += 1
            try:
                # 游标只增不减：别让导入把「待总结」拉回去重做
                conv["cursor"] = max(int(conv.get("cursor", 0) or 0), int(c.get("cursor", 0) or 0))
            except (TypeError, ValueError):
                pass
            if n_add:
                detail.append({"user_id": key, "added": n_add})
                touched.append(key)
        if added:
            if lwb is not None:
                lwb.store.save()
                for key in touched:
                    try:
                        lwb.engine.invalidate(key)
                    except Exception:
                        pass
            else:
                st._save()
    await log_publisher("SUCCESS", "[记忆库] 导入完成：新增 %d 条、跳过 %d 条（重复/空）" % (added, skipped))
    return {"status": "ok", "added": added, "skipped": skipped,
            "new_conversations": new_convs, "detail": detail,
            "message": "新增 %d 条，跳过 %d 条重复" % (added, skipped)}


@app.get("/api/memory")
async def get_memory_overview(scope: str = ""):
    """记忆库总览：各会话的记忆条数与整理进度。scope=all 时两个库拼在一起看。"""
    pairs = _scope_pair(scope)
    convs = []
    emb = None
    for sc, (hist, store, _n) in pairs:
        history_convs = hist.list_conversations()
        totals = {str(c.get("user_id")): c.get("count", 0) for c in history_convs}
        part = store.list_conversations(totals)
        known = {str(c.get("user_id")) for c in part}
        for c in history_convs:
            if str(c.get("user_id")) not in known:
                part.append({
                    "user_id": c.get("user_id"), "sender_name": c.get("sender_name", ""),
                    "count": 0, "cursor": 0, "total_messages": c.get("count", 0),
                    "updated_at": c.get("updated_at", 0),
                })
        for c in part:
            c["from_scope"] = sc
        convs.extend(part)
        if emb is None:
            emb = store.embedder
    await _fill_group_names(convs)
    return {
        "status": "ok",
        "scope": str(scope or "main").strip().lower() or "main",
        "conversations": convs,
        "total_entries": sum(c.get("count", 0) for c in convs),
        "embedding_configured": bool(emb and emb.is_configured()),
        "embedding_model": (emb.model if emb else ""),
        "embedding_endpoint": (emb.endpoint() if emb and emb.is_configured() else ""),
    }


def _cross_ids(scope: str, uid, store) -> list:
    """按 scope 读跨会话召回配置，返回要一并召回的会话 UID 列表。

    开关关闭时返回空列表（只召回自己）；开启时返回同库里其他会话，
    allow 非空则只返回白名单里的那些。
    """
    sc = "claude" if str(scope or "").lower() == "claude" else "main"
    if sc == "claude":
        on = bool(current_config.get("claude_memory_cross_inject"))
        allow = current_config.get("claude_memory_cross_allow") or []
    else:
        on = bool(current_config.get("memory_cross_inject"))
        allow = current_config.get("memory_cross_allow") or []
    if not on or store is None:
        return []
    try:
        return list(store.other_user_ids(uid, allow) or [])
    except Exception:
        return []


@app.post("/api/memory/search")
async def search_memory(payload: Dict[str, Any] = Body(...)):
    """召回测试：看看某句话能命中哪些记忆（用于验证注入效果，不写入任何数据）

    与真实注入链路口径一致：跨会话召回开关打开时，其他会话的记忆也一起参与，
    命中结果会带 is_other / from_user_id 标记。
    """
    uid = _uid(payload.get("user_id"))
    query = str(payload.get("query") or "").strip()
    top_k = int(payload.get("top_k") or current_config.get("memory_top_k", 5) or 5)
    raw_min = payload.get("min_score")
    min_score = float(raw_min) if raw_min is not None else float(current_config.get("memory_min_score", 0.25) or 0.25)
    _sc = str(payload.get("scope") or "").strip().lower()
    if _sc == "all":
        hits = []
        _extra = []
        for _s, (_h, _st, _nbk) in _scope_pair("all"):
            _ex = _cross_ids(_s, uid, _st)
            _extra.extend(_ex)
            for h in await asyncio.to_thread(_st.recall, uid, query, top_k, min_score, _ex):
                h["from_scope"] = _s
                hits.append(h)
        hits.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
        hits = hits[:top_k]
    else:
        _store = _mem(_sc or _view_scope)
        _extra = _cross_ids(_sc or _view_scope, uid, _store)
        hits = await asyncio.to_thread(_store.recall, uid, query, top_k, min_score, _extra)
    lines = ["- 【%s】%s：%s" % (MEMORY_KIND_LABEL.get(h.get("kind", "fact"), "记忆"),
                                h.get("title"), h.get("text")) for h in hits]
    block = ("【长期记忆｜与当前话题相关的既有剧情】\n" + "\n".join(lines)) if lines else ""
    _other = [h for h in hits if h.get("is_other")]
    return {"status": "ok", "hits": hits, "count": len(hits), "preview": block,
            "cross_scope_ids": [str(x) for x in _extra],
            "other_count": len(_other),
            # 诊断：说明这次为什么召回/不召回其他会话
            "cross_dbg": {"scope": _sc or _view_scope,
                          "on": bool(current_config.get(
                              "claude_memory_cross_inject" if (_sc or _view_scope) == "claude"
                              else "memory_cross_inject")),
                          "allow": current_config.get(
                              "claude_memory_cross_allow" if (_sc or _view_scope) == "claude"
                              else "memory_cross_allow")}}


@app.post("/api/embedding/test")
async def test_embedding(payload: Dict[str, Any] = Body(...)):
    """测试嵌入服务连通性（可直接用面板里尚未保存的参数试）"""
    data = payload or {}
    client = EmbeddingClient(
        (data.get("base_url") or current_config.get("embedding_base_url", "")).strip(),
        (data.get("api_key") or current_config.get("embedding_api_key", "")).strip(),
        (data.get("model") or current_config.get("embedding_model", "")).strip(),
        data.get("timeout") or current_config.get("embedding_timeout", 20),
    )
    res = await asyncio.to_thread(client.test)
    await log_publisher("SUCCESS" if res.get("ok") else "ERROR",
                        "[记忆库] 嵌入服务连通性测试：%s" % res.get("message"))
    return {"status": "ok" if res.get("ok") else "error", **res}


@app.get("/api/memory/{user_id}")
async def get_memory_detail(user_id: str, q: str = "", limit: int = 300, scope: str = ""):
    """某个用户的记忆条目（q 非空时走向量/关键词检索）。scope=all 时两个库拼一起。"""
    _sc = str(scope or "").strip().lower()
    if _sc == "all":
        uid0 = _uid(user_id)
        entries = []
        stats = {"total": 0, "indexed": 0, "pending": 0, "messages": 0, "cursor": 0}
        for s, (hist, st, _n) in _scope_pair("all"):
            msgs = hist.get_messages(uid0)
            try:
                _s = st.stats(uid0, len(msgs)) or {}
            except Exception:
                _s = {}
            if q.strip():
                es = await asyncio.to_thread(st.search, uid0, q, limit)
            else:
                es = list(st.list_entries(uid0) or [])
                es.sort(key=lambda e: e.get("updated_at", 0) or 0, reverse=True)
            vi = st._vector_items(uid0)
            for e in es:
                e["indexed"] = bool((vi.get(e["id"]) or {}).get("v"))
                e["from_scope"] = s
            entries.extend(es)
            for k in ("total", "indexed", "pending"):
                stats[k] = stats.get(k, 0) + int(_s.get(k, 0) or 0)
            stats["messages"] = stats.get("messages", 0) + len(msgs)
        return {"status": "ok", "stats": stats, "entries": entries, "query": q, "scope": "all"}
    store = _mem(_sc or _view_scope)
    uid = _uid(user_id)
    msgs = _hist().get_messages(uid)
    stats = store.stats(uid, len(msgs))
    if q.strip():
        entries = await asyncio.to_thread(store.search, uid, q, limit)
    else:
        entries = store.list_entries(uid)
        entries.sort(key=lambda e: e.get("updated_at", 0) or 0, reverse=True)
    vec_items = store._vector_items(uid)
    for e in entries:
        e["indexed"] = bool((vec_items.get(e["id"]) or {}).get("v"))
    return {"status": "ok", "stats": stats, "entries": entries, "query": q}


@app.post("/api/memory/{user_id}")
async def add_memory_entry(user_id: str, payload: Dict[str, Any] = Body(...)):
    """手动新增一条记忆"""
    store = _mem()
    uid = _uid(user_id)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="记忆内容不能为空")
    # keywords / source_ids 必须用关键字传：add_entry 的第 5 个位置参数是 sender_name，
    # 之前按位置传会导致 7 个参数超签名（手动新增记忆一直 500）
    entry = store.add_entry(uid, payload.get("kind", "fact"), payload.get("title", ""),
                            text, keywords=payload.get("keywords"),
                            source_ids=payload.get("source_ids"))
    if payload.get("index", True):
        await asyncio.to_thread(store.index_entries, uid)
    await log_publisher("INFO", "[记忆库] 已新增记忆「%s」(用户 %s)" % (entry.get("title"), uid))
    return {"status": "ok", "entry": entry}


@app.put("/api/memory/{user_id}/entry/{entry_id}")
async def update_memory_entry(user_id: str, entry_id: str, payload: Dict[str, Any] = Body(...)):
    """修改一条记忆的内容，改完自动重建该条向量"""
    store = _mem()
    uid = _uid(user_id)
    if _lwb_is_all_scope(user_id):
        owner = await asyncio.to_thread(_lwb_owner_of, entry_id)
        if owner is None:
            return {"status": "not_found", "updated": False,
                    "message": "全局视图下找不到这条记忆的归属会话"}
        uid = owner
    fields = {k: payload[k] for k in ("kind", "title", "text", "keywords") if k in payload}
    if not fields:
        raise HTTPException(status_code=400, detail="请求体没有任何可更新字段")
    entry = store.update_entry(uid, entry_id, **fields)
    if entry is None:
        return {"status": "not_found", "updated": False}
    await asyncio.to_thread(store.index_entries, uid)
    await log_publisher("INFO", "[记忆库] 已修改记忆「%s」(用户 %s)" % (entry.get("title"), uid))
    return {"status": "ok", "updated": True, "entry": entry}


@app.delete("/api/memory/{user_id}/entry/{entry_id}")
async def delete_memory_entry(user_id: str, entry_id: str):
    """删除一条记忆（连同它的向量一起移除，之后不再被召回）"""
    uid = _uid(user_id)
    if _lwb_is_all_scope(user_id):
        owner = await asyncio.to_thread(_lwb_owner_of, entry_id)
        if owner is None:
            return {"status": "not_found", "deleted": False,
                    "message": "全局视图下找不到这条记忆的归属会话"}
        uid = owner
    ok = _mem().delete_entry(uid, entry_id)
    if ok:
        await log_publisher("INFO", "[记忆库] 已删除记忆 %s (用户 %s)" % (entry_id, user_id))
    return {"status": "ok" if ok else "not_found", "deleted": ok}


@app.delete("/api/memory/{user_id}")
async def clear_memory(user_id: str):
    """清空某用户的全部记忆与向量"""
    ok = _mem().clear(_uid(user_id))
    await log_publisher("WARN", "[记忆库] 已清空用户 %s 的全部记忆" % user_id)
    return {"status": "ok", "cleared": ok}


@app.post("/api/memory/{user_id}/build")
async def build_memory(user_id: str, payload: Dict[str, Any] = None):
    """从聊天记录抽取记忆（增量：已整理过的部分不会重复总结）"""
    data = payload or {}
    uid = _uid(user_id)
    batch = int(data.get("batch") or current_config.get("memory_batch", 20) or 20)
    conv = _hist().get_conversation(uid)
    result = await bot_client.build_memories(uid, batch=batch, sender_name=conv.get("sender_name", ""),
                                             history_store=_hist(), memory_store=_mem())
    return {"status": "ok" if result.get("ok") else "error", **result}


@app.post("/api/memory/{user_id}/index")
async def index_memory(user_id: str, payload: Dict[str, Any] = None):
    """为记忆条目建立 / 重建向量索引"""
    force = bool((payload or {}).get("force"))
    res = await asyncio.to_thread(_mem().index_entries, _uid(user_id), force)
    await log_publisher("INFO" if res.get("ok") else "WARN", "[记忆库] %s" % res.get("message"))
    return {"status": "ok" if res.get("ok") else "error", **res}


# ==================== 【识图】接口 ====================

VISION_DIR_NAME = "qq_images"


def _vision_make_test_png() -> bytes:
    """纯手搓一张有可辨认内容的 PNG（红圆 + 蓝方 + 绿三角），不依赖 Pillow。"""
    w, h = 360, 220
    bg = (250, 250, 210)
    px = [[bg for _ in range(w)] for _ in range(h)]

    def put(x, y, c):
        if 0 <= x < w and 0 <= y < h:
            px[y][x] = c

    for y in range(20, 120):
        for x in range(20, 120):
            if (x - 70) ** 2 + (y - 70) ** 2 <= 50 * 50:
                put(x, y, (220, 40, 40))
    for y in range(40, 140):
        for x in range(230, 330):
            put(x, y, (40, 70, 220))
    for y in range(130, 200):
        for x in range(20, 140):
            if abs(x - 80) <= (y - 130):
                put(x, y, (40, 170, 70))

    raw = bytearray()
    for row in px:
        raw.append(0)
        for r, g, b in row:
            raw += bytes((r, g, b))
    out = bytearray(b"\x89PNG\r\n\x1a\n")

    def chunk(tag, data):
        out.extend(struct.pack(">I", len(data)))
        out.extend(tag)
        out.extend(data)
        out.extend(struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    chunk(b"IEND", b"")
    return bytes(out)


@app.post("/api/debounce/test")
async def debounce_test(payload: Dict[str, Any] = Body(default={})):
    """试一下防抖判断：给一段文本，返回完整度分数和会不会放行。"""
    text = str((payload or {}).get("text") or "")
    try:
        from debounce import builtin_score, model_score, DEFAULTS as _DB_DEF
        c = dict(_DB_DEF)
        c.update(current_config or {})
        c.update({k: v for k, v in (payload or {}).items() if str(k).startswith("debounce_")})
        b = builtin_score(text)
        mode = str(c.get("debounce_judge") or "local")
        lo = None
        if mode == "local":
            try:
                from debounce_model import predict_send as _ps
                lo = await asyncio.to_thread(_ps, text)
            except Exception:
                lo = None
        m = await model_score(text, c) if mode == "model" else None
        if mode == "local":
            score = b if lo is None else max(b, lo)
        elif mode == "model":
            score = b if m is None else m
        else:
            score = b
        thr = float(c.get("debounce_threshold") or 0.5)
        src = ("local" if (mode == "local" and lo is not None)
               else "model" if (mode == "model" and m is not None) else "builtin")
        return {"ok": True, "score": score, "builtin": b, "local": lo, "model": m,
         "source": src, "threshold": thr,
                "verdict": "send" if score >= thr else "wait",
                "note": "send=已说完，放行给 AI；wait=还没说完，继续等后续消息"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/segments/test")
async def segments_test(payload: Dict[str, Any] = Body(default={})):
    """预览分段效果：给一段文本，返回会被切成几条、每条多少字、停多久。"""
    try:
        import segments as _SG
        c = dict(_SG.DEFAULTS)
        c.update(current_config or {})
        c.update({k: v for k, v in (payload or {}).items() if str(k).startswith("seg_")})
        text = str((payload or {}).get("text") or "")
        steps = _SG.plan(text, c)
        total = sum(int(s["delay_ms"]) for s in steps)
        return {"ok": True, "count": len(steps), "total_ms": total,
                "rule": str(c.get("seg_rule") or "newline"), "rule_label": _SG.rule_label(str(c.get("seg_rule") or "newline")),
                "rules": [{"name": k, "label": v[0]} for k, v in _SG.SPLIT_RULES.items()],
                "steps": steps}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/secretary/status")
async def secretary_status():
    """秘书网关状态。"""
    try:
        import secretary as _SC
        return {"ok": True, **_SC.create_secretary(lambda: current_config).status()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/secretary/test")
async def secretary_test(payload: Dict[str, Any] = Body(default={})):
    """试一下：给一条消息（可带最近上下文），看秘书网关会怎么办。"""
    try:
        import secretary as _SC
        cfg = dict(current_config or {})
        cfg.update({k: v for k, v in (payload or {}).items() if str(k).startswith("sec_")})
        sc = _SC.create_secretary(lambda: cfg)
        text = str((payload or {}).get("text") or "")
        recent = []
        for it in ((payload or {}).get("recent") or []):
            if isinstance(it, (list, tuple)) and len(it) >= 2:
                recent.append((str(it[0]), str(it[1])))
            elif isinstance(it, dict):
                recent.append((str(it.get("who") or it.get("sender_name") or "某人"), str(it.get("text") or it.get("content") or "")))
        d = await sc.gate("test", text, recent, mentioned=bool((payload or {}).get("mentioned")))
        return {"ok": True, "decision": d, "threshold": round(sc.threshold(), 3),
                "repeat_preview": _SC.detect_repeat(recent, int(cfg.get("sec_repeat_window", 6) or 6),
                                                    int(cfg.get("sec_repeat_need", 3) or 3),
                                                    int(cfg.get("sec_repeat_min_len", 2) or 2))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/vision/status")
async def vision_status():
    """识图页：当前配置 + 已收图片统计。"""
    users = []
    root = Path(VISION_DIR_NAME)
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            files = [f for f in sorted(d.iterdir()) if f.is_file()]
            users.append({
                "user_id": d.name,
                "count": len(files),
                "latest": files[-1].name if files else "",
                "latest_time": (time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(files[-1].stat().st_mtime)) if files else ""),
            })
    cfg = current_config
    return {
        "ok": True,
        "vision_enabled": bool(cfg.get("vision_enabled", True)),
        "vision_max_images": int(cfg.get("vision_max_images", 3) or 3),
        "vision_max_mb": float(cfg.get("vision_max_mb", 8) or 8),
        "vision_sticker_enabled": bool(cfg.get("vision_sticker_enabled", True)),
        "vision_see_enabled": bool(cfg.get("vision_see_enabled", True)),
        "vision_guide": str(cfg.get("vision_guide") or ""),
        "llm_model": str(cfg.get("llm_model") or ""),
        "llm_base_url": str(cfg.get("llm_base_url") or ""),
        "dir": str(Path(VISION_DIR_NAME).resolve()),
        "users": users,
    }


def _vision_safe(user_id: Any, name: Any) -> Path:
    """把 (用户, 文件名) 拼成安全路径，挡掉目录穿越。"""
    u = "".join(c for c in str(user_id or "") if c.isdigit())
    n = Path(str(name or "")).name
    return Path(VISION_DIR_NAME) / u / n


def _vision_prune(root: Path, cap: int) -> int:
    """超过上限就按时间删掉最老的（新的顶进来）。cap<=0 表示不限制。"""
    if cap <= 0 or not root.is_dir():
        return 0
    files = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.is_file():
                try:
                    files.append((f.stat().st_mtime, f))
                except Exception:
                    pass
    files.sort(key=lambda x: x[0], reverse=True)
    n = 0
    for _t, f in files[int(cap):]:
        try:
            f.unlink()
            n += 1
        except Exception:
            pass
    return n


@app.get("/api/vision/images")
async def vision_images(user_id: str = "", limit: int = 0):
    """列已收到的图片（按用户），给识图页看图墙用。超出保留上限的老图会被清掉。"""
    root = Path(VISION_DIR_NAME)
    try:
        cap = int(current_config.get("vision_store_max", 120) or 120)
    except Exception:
        cap = 120
    pruned = _vision_prune(root, cap)
    if not limit:
        limit = cap if cap > 0 else 200
    items = []
    if root.is_dir():
        dirs = [d for d in sorted(root.iterdir()) if d.is_dir()]
        if user_id:
            dirs = [d for d in dirs if d.name == str(user_id)]
        for d in dirs:
            for f in sorted([x for x in d.iterdir() if x.is_file()], reverse=True):
                if len(items) >= limit:
                    break
                items.append({
                    "user_id": d.name,
                    "name": f.name,
                    "no": int(f.name.split("_", 1)[0]) if f.name.split("_", 1)[0].isdigit() else 0,
                    "kind": "sticker" if f.name.endswith(".gif") else "image",
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime)),
                    "url": f"/api/vision/file/{d.name}/{f.name}",
                })
    return {"ok": True, "count": len(items), "cap": cap, "pruned": pruned, "items": items}


@app.post("/api/vision/images/delete")
async def vision_images_delete(payload: Dict[str, Any] = Body(default={})):
    """删掉「已收到」里的图（支持单张，也支持一批）。"""
    root = Path(VISION_DIR_NAME)
    items = list(payload.get("items") or [])
    if payload.get("name"):
        items.append({"user_id": payload.get("user_id"), "name": payload.get("name")})
    n = 0
    for it in items:
        p = _vision_safe((it or {}).get("user_id"), (it or {}).get("name"))
        if p.is_file():
            try:
                p.unlink()
                n += 1
            except Exception:
                pass
    return {"ok": True, "deleted": n, "left": len(items) - n}


@app.post("/api/vision/images/to-library")
async def vision_images_to_library(payload: Dict[str, Any] = Body(default={})):
    """把「已收到」里的一张图收进表情包库。"""
    st = getattr(bot_client, "sticker", None)
    if st is None:
        return {"ok": False, "error": "偷图/图库模块没启用"}
    p = _vision_safe(payload.get("user_id"), payload.get("name"))
    if not p.is_file():
        return {"ok": False, "error": "图片不存在"}
    try:
        data = p.read_bytes()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        # 跟「待审核池 → 通过」同一条路：先进池排队 → 识图 → 自动入库
        r = await st.upload(p.name, data, analyze=True)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "result": r}


@app.get("/api/vision/file/{user_id}/{name}")
async def vision_file(user_id: str, name: str):
    """把落地图片原样吐回去，给页面看图墙预览用。"""
    safe_u = "".join(c for c in str(user_id) if c.isdigit())
    safe_n = Path(str(name)).name
    p = Path(VISION_DIR_NAME) / safe_u / safe_n
    if not p.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(str(p))


@app.post("/api/vision/test")
async def vision_test():
    """识图自检：现场画一张图发给当前主对话模型，看它能不能认出来。"""
    cfg = current_config
    base_url = str(cfg.get("llm_base_url") or "").rstrip("/")
    api_key = str(cfg.get("llm_api_key") or "").strip()
    model = str(cfg.get("llm_model") or "").strip()
    if not base_url or not model:
        return {"ok": False, "error": "还没配置主对话 API（base_url / model 为空）"}
    if base_url.endswith("/chat/completions"):
        endpoint = base_url
    elif base_url.endswith("/v1"):
        endpoint = f"{base_url}/chat/completions"
    else:
        endpoint = f"{base_url}/v1/chat/completions"

    png = _vision_make_test_png()
    b64 = base64.b64encode(png).decode()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这张图里有几个图形？分别是什么形状、什么颜色？简短回答。"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    t0 = time.time()

    def _post():
        req = urllib.request.Request(
            endpoint, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    try:
        data = await asyncio.to_thread(_post)
    except Exception as e:
        return {"ok": False, "error": f"请求失败: {type(e).__name__}: {e}"}

    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    reply = str(msg.get("content") or "")
    usage = data.get("usage") or {}
    ok = any(k in reply for k in ("圆形", "圆", "正方形", "三角"))
    return {
        "ok": bool(ok and reply.strip()),
        "reply": reply,
        "model": model,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "elapsed": round(time.time() - t0, 1),
        "hint": "" if ok else "模型没说出图形名称——可能是该模型/中转不支持图片输入",
    }


# ==================== 【识图】接口结束 ====================

# ==================== 群聊模式 ====================
CLAUDE_PROFILE_FIELDS = ("claude_base_url", "claude_api_key", "claude_model", "claude_temperature")


def _claude_profiles() -> list:
    raw = current_config.get("claude_api_profiles")
    if isinstance(raw, dict):
        return [dict(v or {}, name=k) for k, v in raw.items()]
    return [p for p in (raw or []) if isinstance(p, dict)]


def _claude_profiles_save(lst: list):
    current_config["claude_api_profiles"] = lst
    save_config(current_config)


def _claude_active() -> Dict[str, Any]:
    return {k: current_config.get(k, "") for k in CLAUDE_PROFILE_FIELDS}


@app.get("/api/claude/profiles")
async def claude_profiles_list():
    return {"status": "ok", "profiles": _claude_profiles(), "active": _claude_active()}


@app.post("/api/claude/profiles")
async def claude_profiles_upsert(payload: Dict[str, Any] = Body(None)):
    """把当前这套 Claude API 存成一个配置档（同名覆盖）。"""
    d = payload or {}
    name = str(d.get("name") or "").strip()
    if not name:
        return {"status": "error", "message": "配置档要有个名字"}
    prof: Dict[str, Any] = {"name": name}
    for k in CLAUDE_PROFILE_FIELDS:
        if k in d:
            prof[k] = d[k]
    lst = [p for p in _claude_profiles() if str(p.get("name") or "") != name]
    lst.append(prof)
    _claude_profiles_save(lst)
    await log_publisher("SUCCESS", f"[群聊] 已保存 API 配置档：{name}")
    return {"status": "ok", "profiles": lst}


@app.post("/api/claude/profiles/apply")
async def claude_profiles_apply(payload: Dict[str, Any] = Body(None)):
    """切换到某个配置档：直接写进当前生效的 claude_* 并落盘，即时生效。"""
    name = str((payload or {}).get("name") or "").strip()
    hit = next((p for p in _claude_profiles() if str(p.get("name") or "") == name), None)
    if hit is None:
        return {"status": "error", "message": "没有这个配置档"}
    for k in CLAUDE_PROFILE_FIELDS:
        if k in hit:
            current_config[k] = hit[k]
    save_config(current_config)
    bot_client.update_config(current_config)
    await log_publisher("SUCCESS", f"[群聊] 已切换到 API 配置档：{name}")
    return {"status": "ok", "active": _claude_active()}


@app.post("/api/claude/profiles/rename")
async def claude_profiles_rename(payload: Dict[str, Any] = Body(None)):
    """改配置档的名字（只改存档名，不动当前生效的配置）。"""
    d = payload or {}
    old = str(d.get("old") or "").strip()
    new = str(d.get("new") or "").strip()
    if not old or not new:
        return {"status": "error", "message": "旧名和新名都要有"}
    lst = _claude_profiles()
    if new == old:
        return {"status": "ok", "profiles": lst}
    hit = next((p for p in lst if str(p.get("name") or "") == old), None)
    if hit is None:
        return {"status": "error", "message": f"没有这个配置档：{old}"}
    if any(str(p.get("name") or "") == new for p in lst):
        return {"status": "error", "message": f"已经有叫「{new}」的配置档了"}
    hit["name"] = new
    _claude_profiles_save(lst)
    await log_publisher("SUCCESS", f"[群聊] 配置档已重命名：{old} → {new}")
    return {"status": "ok", "profiles": lst}

@app.delete("/api/claude/profiles/{name}")
async def claude_profiles_delete(name: str):
    lst = [p for p in _claude_profiles() if str(p.get("name") or "") != name]
    _claude_profiles_save(lst)
    return {"status": "ok", "profiles": lst}


@app.get("/api/claude/personas")
async def claude_personas_list():
    cm = _claude_mode()
    if cm is None:
        return {"status": "error", "message": "Claude 模式未装配"}
    return {"status": "ok", "active": cm.persona_id(),
            "personas": cm.personas(), "rules": cm.fixed_rules()}


@app.post("/api/claude/personas")
async def claude_personas_op(payload: Dict[str, Any] = Body(None)):
    """人设注入：切换 / 新增 / 改 / 删。内置那两档不给改也不给删。"""
    d = payload or {}
    act = str(d.get("action") or "").strip()
    cm = _claude_mode()
    if cm is None:
        return {"status": "error", "message": "Claude 模式未装配"}
    lst = [p for p in (current_config.get("claude_personas") or []) if isinstance(p, dict)]
    pid = str(d.get("id") or "").strip()

    def _ok(extra: Dict[str, Any] = None):
        save_config(current_config)
        bot_client.update_config(current_config)
        out = {"status": "ok", "personas": cm.personas(),
               "inject": cm.persona_inject(),
               "active": str(current_config.get("claude_persona_id") or cm.persona_id())}
        if extra:
            out.update(extra)
        return out

    if act == "active":
        if not pid:
            return {"status": "error", "message": "缺少 id"}
        current_config["claude_persona_id"] = pid
        cm.apply_persona_inject(pid)
        await log_publisher("INFO", f"[群聊] 人设注入切换为 {pid}")
        return _ok()
    if act == "add":
        name = str(d.get("name") or "").strip() or "我的自定义人设"
        new_id = "u" + str(int(time.time() * 1000))[-9:]
        lst.append({"id": new_id, "name": name, "text": str(d.get("text") or "")})
        current_config["claude_personas"] = lst
        current_config["claude_persona_id"] = new_id
        # 新人设继承当前那四个勾选
        inj_all = dict(current_config.get("claude_persona_inject") or {})
        inj_all[new_id] = {k: bool(current_config.get("claude_%s_enabled" % k, True))
                           for k in ("worldbook", "memory", "notebook", "pic")}
        current_config["claude_persona_inject"] = inj_all
        await log_publisher("SUCCESS", f"[群聊] 新增人设：{name}")
        return _ok({"id": new_id})
    if act == "update":
        hit = next((p for p in lst if str(p.get("id") or "") == pid), None)
        if hit is None:
            return {"status": "error", "message": "这条人设不存在（内置人设不能改）"}
        if "name" in d and str(d.get("name") or "").strip():
            hit["name"] = str(d["name"]).strip()
        if "text" in d:
            hit["text"] = str(d.get("text") or "")
        current_config["claude_personas"] = lst
        return _ok()
    if act == "delete":
        hit = next((p for p in lst if str(p.get("id") or "") == pid), None)
        if hit is None:
            return {"status": "error", "message": "内置人设不能删"}
        current_config["claude_personas"] = [p for p in lst if str(p.get("id") or "") != pid]
        inj_all = dict(current_config.get("claude_persona_inject") or {})
        inj_all.pop(pid, None)
        current_config["claude_persona_inject"] = inj_all
        if str(current_config.get("claude_persona_id") or "") == pid:
            current_config["claude_persona_id"] = "attach"
            cm.apply_persona_inject("attach")
        await log_publisher("INFO", f"[群聊] 已删除人设：{hit.get('name')}")
        return _ok()
    if act == "inject":
        # 把这四个勾选记到当前人设上
        cur_pid = str(current_config.get("claude_persona_id") or cm.persona_id())
        inj_all = dict(current_config.get("claude_persona_inject") or {})
        vals = {k: bool(d.get(k, current_config.get("claude_%s_enabled" % k, True)))
                for k in ("worldbook", "memory", "notebook", "pic")}
        inj_all[cur_pid] = vals
        current_config["claude_persona_inject"] = inj_all
        for k, v in vals.items():
            current_config["claude_%s_enabled" % k] = v
        return _ok()
    return {"status": "error", "message": "未知操作：%s" % act}


@app.post("/api/claude/preview")
async def claude_preview(payload: Dict[str, Any] = Body(None)):
    """看实际会发出去的固定提示词原文。带上面板里还没保存的编辑，所见即所得。"""
    d = payload or {}
    cgm = getattr(bot_client, "claude", None)
    if cgm is None:
        return {"status": "error", "message": "Claude 模式未装配"}
    snap: Dict[str, Any] = {}
    for k in ("system_prompt", "group_rules", "prompt_template", "pic_guide"):
        if k in d:
            snap[k] = cgm.config.get("claude_" + k)
            cgm.config["claude_" + k] = d[k]
    cgm._persona_override = str(d.get("persona_text") or "")
    try:
        out = cgm.preview(d.get("group_id"))
    finally:
        cgm._persona_override = ""
        for k, v in snap.items():
            cgm.config["claude_" + k] = v
    return {"status": "ok", **out}


@app.get("/api/claude/status")
async def claude_status():
    cm = _claude_mode()
    if cm is None:
        return {"status": "error", "message": "群聊模式未初始化（看服务端启动日志）"}
    return {"status": "ok", **cm.status()}


@app.post("/api/claude/toggle")
async def claude_toggle(payload: Dict[str, Any] = Body(None)):
    """控制台那个「群聊模式」按钮：开/关。"""
    data = payload or {}
    on = bool(data.get("enabled", not bool(current_config.get("claude_enabled", False))))
    current_config["claude_enabled"] = on
    save_config(current_config)
    bot_client.update_config(current_config)
    await log_publisher("SUCCESS" if on else "WARN",
                        "[群聊] 模式已" + ("开启：群聊交给它，它自己决定说不说话" if on else "关闭"))
    return {"status": "ok", "enabled": on}


@app.get("/api/claude/groups")
async def claude_groups():
    cm = _claude_mode()
    return {"status": "ok", "groups": cm.groups() if cm is not None else []}


@app.post("/api/claude/clear")
async def claude_clear(payload: Dict[str, Any] = Body(None)):
    cm = _claude_mode()
    gid = (payload or {}).get("group_id")
    if cm is None or gid in (None, ""):
        return {"status": "error", "message": "缺少 group_id"}
    try:
        ok = cm.history.clear(int(gid))
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "cleared": bool(ok)}


@app.post("/api/claude/test")
async def claude_test(payload: Dict[str, Any] = Body(None)):
    """用一段假群聊记录真调一次模型（只看它说不说话，不真的发到群里）。"""
    cm = _claude_mode()
    if cm is None:
        return {"status": "error", "message": "群聊模式未初始化"}
    data = payload or {}
    lines = [str(x).strip() for x in (data.get("lines") or []) if str(x).strip()]
    if not lines:
        lines = ["小明：今晚有人打游戏吗", "小红：我我我", "小明：@克劳德 你来不来"]
    gid = int(data.get("group_id") or 900000001)
    try:
        cm.history.clear(gid)
        for ln in lines:
            nm, tx = (ln.split("：", 1) if "：" in ln else ("群友", ln))
            cm.history.append(gid, "user", tx.strip(), sender_name=nm.strip())
        msgs = cm._build_messages(gid)
        raw = await cm._call_api(msgs)
    except Exception as e:
        return {"status": "error", "message": f"调用失败：{e}"}
    text = cm.decide(raw, str(cm._cfg("silence_marker") or "<silence />"))
    return {"status": "ok", "model": cm.model_name(), "endpoint": cm.endpoint(),
            "prompt_messages": len(msgs), "raw": raw[:800],
            "will_send": text, "silent": text is None}


@app.get("/api/status")
async def get_status():
    """获取 NapCat 进程与 QQ WebSocket 连接状态"""
    napcat_running = (
        napcat_process is not None and napcat_process.returncode is None
    )
    qq_connected = bot_client.is_ws_connected
    return {
        "napcat_running": napcat_running,
        "qq_connected": qq_connected
    }


# ---------------- NapCat 进程管理 ----------------

async def _read_napcat_stream(proc: asyncio.subprocess.Process):
    """NapCat 输出已重定向到文件，这里跟随读取、抓 WebUI 链接并推送到前端"""

    async def _hook(line_text):
        global napcat_webui_live, napcat_console_tail, napcat_hints
        import re as _re
        _lt = line_text.rstrip()
        if _lt:
            napcat_console_tail.append(_lt)
            if len(napcat_console_tail) > 400:
                del napcat_console_tail[:200]
        _m = _re.search(r"https?://[^\s\"'<>]*webui[^\s\"'<>]*", line_text)
        if _m and "token=" in _m.group(0):
            napcat_webui_live = _m.group(0).rstrip(".,;)")
            _KA.write_live_webui(napcat_webui_live)
            await log_publisher("INFO", "[原生后台] 已捕获带 Token 的 WebUI 地址")
        elif _re.search(r"(?i)(token|key|密钥|登录)", line_text) and len(_lt) < 300:
            napcat_hints.append(_lt)
            if len(napcat_hints) > 30:
                del napcat_hints[:15]
            await log_publisher("INFO", "[原生后台] 疑似登录密钥行：" + _lt)

    await _follow_console("napcat", proc, _hook)
    returncode = await proc.wait()
    await log_publisher("WARN", f"NapCat 进程已退出 (退出码: {returncode})")
    await broadcast_ws({"type": "status_change", "napcat_running": False})


def auto_enable_napcat_ws():
    """确保本地 NapCat 的所有账号配置中都开启了 3001 正向 WebSocket 服务。

    SnowLuma 模式（空协议）下直接跳过：不去改 NapCat 的配置文件。
    """
    if not napcat_mode():
        return
    config_dir = Path(r"C:\ai\NapCat.Shell.Windows.Node\napcat\config")
    if not config_dir.exists():
        return

    ws_server_cfg = {
        "name": "OneBot-WS",
        "enable": True,
        "host": "127.0.0.1",
        "port": 3001,
        "messagePostFormat": "string",
        "reportSelfMessage": False,
        "token": "",
        "enableForcePushEvent": True,
        "debug": False,
        "heartInterval": 30000
    }

    for fpath in config_dir.glob("onebot11_*.json"):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            net = data.setdefault("network", {})
            ws_servers = net.setdefault("websocketServers", [])
            has_3001 = any(s.get("port") == 3001 and s.get("enable") for s in ws_servers)
            if not has_3001:
                # 移除旧的不生效条目或直接添加
                ws_servers = [s for s in ws_servers if s.get("port") != 3001]
                ws_servers.append(ws_server_cfg)
                net["websocketServers"] = ws_servers
                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                logger.info(f"已自动为 NapCat 开启 3001 端口: {fpath.name}")
        except Exception as e:
            logger.warning(f"检查 NapCat 配置异常 {fpath}: {e}")


import keepalive as _KA
import coyote as _CY

_KA_LOOP = None


def _ka_log(level, msg):
    """保活模块的日志 -> 控制台"""
    try:
        if _KA_LOOP:
            asyncio.run_coroutine_threadsafe(log_publisher(level, msg), _KA_LOOP)
    except Exception:
        pass


def _ka_restart():
    """保活模块要求重启 NapCat 时，走同一个被跟踪的启停入口。

    直接 Popen 起来的话 QQX_AIBot 拿不到这个进程：控制台看不到它的输出、/api/status
    也会显示未运行；再点一次「启动」还会再起一个实例抢端口。这里统一切到
    stop_napcat + start_napcat。
    """
    if _KA_LOOP is None:
        # 启动钩子还没跑（或事件循环未就绪）：不要在这里抛异常，交给 act_l4 回退到 Popen
        return None

    async def _do():
        try:
            await stop_napcat()
        except Exception:
            pass
        return await start_napcat()
    return asyncio.run_coroutine_threadsafe(_do(), _KA_LOOP).result(timeout=120)


def _ka_stop():
    try:
        return asyncio.run_coroutine_threadsafe(stop_napcat(), _KA_LOOP).result(timeout=60)
    except Exception:
        return None



@app.on_event("startup")
async def _ka_boot():
    global _KA_LOOP
    _KA_LOOP = asyncio.get_running_loop()
    _KA.set_logger(_ka_log)
    _CY.set_logger(_ka_log)
    _KA.set_process_hooks(_ka_restart, _ka_stop)   # 保活重启/停止 NapCat 走同一个入口
    # SnowLuma（空协议）模式：循环照旧起，但整体挂起，不做任何检测/重登
    _KA.set_suspended(not napcat_mode())
    asyncio.create_task(_KA.loop())
    try:
        if napcat_mode():
            await log_publisher("INFO", "[保活] 模块已加载，开关在控制台「NapCat 保活」卡片")
        else:
            await log_publisher("INFO", "[保活] 当前协议端是 SnowLuma（空协议模式），NapCat 保活已挂起（切回 NapCat 后自动恢复）")
    except Exception:
        pass


@app.get("/api/protocol")
async def api_protocol_get():
    """当前协议端状态：谁在用、地址、启动脚本、进程在不在跑。"""
    if protocol_backend() == "snowluma":
        _raw = str(current_config.get("snowluma_ws_url") or "")
        _fix = snowluma_ws_autofix(_raw)
        if _fix and _fix != _raw:
            current_config["snowluma_ws_url"] = _fix
            try:
                save_config(current_config)
            except Exception:
                pass
    return {"status": "ok", "protocol": protocol_backend(), "ws_url": active_ws_url(),
            "napcat_ws_url": current_config.get("napcat_ws_url", ""),
            "snowluma_ws_url": current_config.get("snowluma_ws_url", ""),
            "napcat_cmd": current_config.get("napcat_cmd", ""),
            "snowluma_cmd": current_config.get("snowluma_cmd", ""),
            "napcat_running": bool(napcat_process and napcat_process.returncode is None),
            "snowluma_running": bool(snowluma_process and snowluma_process.returncode is None),
            "snowluma_webui": snowluma_webui_url()}


@app.post("/api/protocol")
async def api_protocol_set(body: dict = Body(...)):
    """切换协议端（napcat <-> snowluma）。

    切换动作是一条完整链路：
      1) 停掉原协议端的本地进程（两个协议端都会占 3000/3001，不能并存）
      2) 落盘新协议端，NapCat 保活随之挂起 / 恢复
      3) 拉起新协议端的本地进程，等它的 OneBot 端口就绪
      4) OneBot 客户端按新地址重连，自动连上新的协议端
    """
    p = str((body or {}).get("protocol") or "").strip().lower()
    if p not in ("napcat", "snowluma"):
        return {"status": "error", "message": "protocol 只能是 napcat 或 snowluma"}
    # 允许同时带上该协议端的启动命令 / OneBot 地址（控制台那两个输入框）
    if p == "snowluma":
        _u = str((body or {}).get("snowluma_ws_url") or "").strip()
        if _u:
            current_config["snowluma_ws_url"] = _u
        _c = str((body or {}).get("snowluma_cmd") or "").strip()
        if _c:
            current_config["snowluma_cmd"] = _c
    else:
        _u = str((body or {}).get("napcat_ws_url") or "").strip()
        if _u:
            current_config["napcat_ws_url"] = _u
        _c = str((body or {}).get("napcat_cmd") or "").strip()
        if _c:
            current_config["napcat_cmd"] = _c

    _old = protocol_backend()
    _name = "SnowLuma" if p == "snowluma" else "NapCat"
    _old_name = "SnowLuma" if _old == "snowluma" else "NapCat"
    _switched = (p != _old)
    _proc = {}

    # ---- 1) 停旧协议端进程（只有真的换了协议端才动进程）----
    if _switched:
        try:
            _proc["stop_old"] = await stop_protocol(_old)
            await log_publisher("INFO", "[协议端] 已停止 %s 进程：%s" % (
                _old_name, (_proc["stop_old"] or {}).get("message", "")))
        except Exception as _e:
            await log_publisher("WARN", "[协议端] 停止 %s 进程失败：%s" % (_old_name, _e))

    # ---- 2) 落盘新协议端 + 保活挂起/恢复 ----
    current_config["qq_protocol"] = p
    save_config(current_config)
    bot_client.update_config(current_config)
    try:
        _KA.set_suspended(p != "napcat")
    except Exception:
        pass

    # ---- 3) 启新协议端进程 + 等 OneBot 端口就绪 ----
    if _switched:
        try:
            _proc["start_new"] = await start_protocol(p)
            _st = (_proc["start_new"] or {}).get("status")
            await log_publisher("INFO" if _st == "ok" else "WARN",
                                "[协议端] 启动 %s 进程：%s" % (_name, (_proc["start_new"] or {}).get("message", "")))
        except Exception as _e:
            await log_publisher("WARN", "[协议端] 启动 %s 进程失败：%s" % (_name, _e))
        try:
            _h, _port = _proto_ws_hostport(p)
            _ready = await _wait_tcp(_h, _port, timeout=25.0)
            await log_publisher("INFO" if _ready else "WARN",
                                "[协议端] OneBot 端口 %s:%d %s" % (
                                    _h, _port, "已就绪，开始连接" if _ready else "尚未就绪（客户端会持续自动重试）"))
        except Exception:
            pass

    # ---- 4) OneBot 链路按新地址重连（自动连上新的协议端）----
    _reconnected = False
    try:
        if bot_client.is_running:
            await bot_client.stop()
        auto_enable_napcat_ws()
        await bot_client.start()
        _reconnected = True
    except Exception as _e:
        await log_publisher("WARN", "[协议端] OneBot 链路重连失败：%s" % _e)
    _label = "SnowLuma（空协议模式）" if p == "snowluma" else "NapCat"
    await log_publisher("SUCCESS", "[协议端] 已切换到 %s，OneBot 地址 %s" % (_label, active_ws_url()))
    try:
        await broadcast_ws({"type": "protocol_change", "protocol": p, "ws_url": active_ws_url()})
    except Exception:
        pass
    return {"status": "ok", "protocol": p, "ws_url": active_ws_url(),
            "reconnected": _reconnected, "process": _proc,
            "snowluma_webui": snowluma_webui_url(),
            "message": "已切换到 " + _label}


@app.post("/api/keepalive/save")
async def api_keepalive_save(body: dict = Body(...)):
    _g = _napcat_guard("NapCat 保活设置")
    if _g:
        return _g
    try:
        if not isinstance(body, dict):
            return {"status": "error", "message": "body must be object"}
        saved = _KA.save_cfg(body)
        await log_publisher("INFO", "[保活] 设置已保存")
        return {"status": "ok", "keepalive": saved}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/coyote/status")
async def api_coyote_status():
    try:
        return {"status": "ok", "data": _CY.status()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/coyote/save")
async def api_coyote_save(body: dict = Body(...)):
    try:
        if not isinstance(body, dict):
            return {"status": "error", "message": "body must be object"}
        saved = _CY.save_cfg(body)
        await log_publisher("INFO", "[郊狼] 设置已保存")
        return {"status": "ok", "coyote": saved}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/coyote/test")
async def api_coyote_test():
    """测试连通：读一次游戏信息 + 波形列表"""
    try:
        info = await asyncio.get_running_loop().run_in_executor(None, _CY.game_info)
        pulses = await asyncio.get_running_loop().run_in_executor(None, _CY.pulse_list)
        ok = isinstance(info, dict) and info.get("status") == 1
        return {"status": "ok", "connected": bool(ok),
                "message": "连接成功" if ok else (_CY.status().get("last_error") or "连接失败"),
                "info": info or {}, "pulses": pulses}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/coyote/fire")
async def api_coyote_fire(body: dict = Body(...)):
    """手动开火（面板上的按钮）"""
    try:
        b = body or {}
        ok, msg = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _CY.fire(b.get("strength", 10), b.get("time"), b.get("override"), b.get("pulse_id"), "面板"))
        await log_publisher("INFO" if ok else "WARN", "[郊狼] " + str(msg))
        return {"status": "ok" if ok else "error", "message": msg}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/keepalive/status")
async def api_keepalive_status():
    if not napcat_mode():
        return {"status": "ok", "data": {"suspended": True, "protocol": "snowluma", "enabled": False,
                                          "online": None, "stage": 0, "offline_hits": 0, "log": []}}
    try:
        return {"status": "ok", "data": _KA.status()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/keepalive/refresh_qr")
async def api_keepalive_refresh_qr():
    """刷新 NapCat 登录二维码（手动点「L5 扫码」前先调它，避免扫到过期码）。仅 NapCat 模式可用。"""
    _g = _napcat_guard("NapCat 登录二维码")
    if _g:
        return _g
    try:
        d = await asyncio.get_running_loop().run_in_executor(None, _KA.refresh_qr)
        await log_publisher("INFO" if d.get("ok") else "WARN",
                            "[保活] 登录二维码已刷新" if d.get("ok") else "[保活] 刷新登录二维码失败")
        return {"status": "ok", "data": d}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/keepalive/refresh_captcha")
async def api_keepalive_refresh_captcha():
    """现取一条新鲜的验证会话（验证链接 + aid/login_appid/sid）。

    腾讯验证会话有时效，点「立即验证」前先调这里换新，避免
    「你太久没有操作，请重新登录」（result 140022012）。
    """
    _g = _napcat_guard("NapCat 验证会话")
    if _g:
        return _g
    try:
        d = await asyncio.get_running_loop().run_in_executor(None, lambda: _KA.refresh_captcha())
        await log_publisher("INFO" if d.get("ok") else "WARN",
                            "[保活] 已现取新的验证会话" if d.get("ok")
                            else ("[保活] 现取验证会话失败：" + str(d.get("message") or "")))
        return {"status": "ok" if d.get("ok") else "error", "data": d, "message": d.get("message") or ""}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/keepalive/new_device_qr")
async def api_keepalive_new_device_qr(body: dict = Body(None)):
    """取「新设备验证」二维码：腾讯在验证码通过后还要求新设备扫码时用。

    uin / jump_url 缺省则用最近一次 L3 或验证码登录缓存下来的那份。
    """
    b = body or {}
    _g = _napcat_guard("NapCat 新设备验证")
    if _g:
        return _g
    try:
        d = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _KA.new_device_qr(b.get("uin") or "", b.get("jump_url") or ""))
        await log_publisher("INFO" if d.get("ok") else "WARN",
                            "[保活] 已取到新设备验证二维码" if d.get("ok")
                            else ("[保活] 取新设备验证二维码失败：" + str(d.get("message") or "")))
        return {"status": "ok" if d.get("ok") else "error", "data": d, "message": d.get("message") or ""}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/keepalive/manual")
async def api_keepalive_manual(body: dict = Body(...)):
    """手动执行某一级保活动作（保活卡片下方的按钮）：1=L1 重连，3=L3 密码登录，5=L5 刷新登录二维码。仅 NapCat 模式可用。"""
    _g = _napcat_guard("NapCat 保活手动执行")
    if _g:
        return _g
    try:
        lv = int((body or {}).get("level") or 0)
    except Exception:
        lv = 0
    if lv not in (1, 3, 5):
        return {"status": "error", "message": "level 只能是 1（重连）/ 3（密码登录）/ 5（刷新扫码）"}
    name = {1: "L1 重连 + get_status", 3: "L3 密码 / 快速登录", 5: "L5 刷新登录二维码"}[lv]
    try:
        await log_publisher("INFO", "[保活] 手动执行 " + name)
        ok = await asyncio.get_running_loop().run_in_executor(None, _KA.act_manual, lv)
        online = bool(_KA.status().get("online"))
        msg = name + ("：成功，已在线" if ok else "：已执行，但还没回到在线")
        await log_publisher("INFO" if ok else "WARN", "[保活] " + msg)
        return {"status": "ok", "ok": bool(ok), "online": online, "message": msg}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _napcat_webui_url() -> str:
    """读 NapCat 自己写的 webui.json，拼出带 token 的原生后台地址（免手填 Token）。"""
    if napcat_webui_live:
        return napcat_webui_live
    # QQX_AIBot 里记的明文密码最优先（NapCat 自己存的是 hash，拿来拼 URL 会报 token invalid）
    _t = str(current_config.get("napcat_webui_token") or "").strip()
    if _t:
        try:
            import json as _j2
            _wd = _j2.loads((Path(r"C:\ai\NapCat.Shell.Windows.Node") / "napcat" / "config" / "webui.json").read_text(encoding="utf-8")) or {}
            _pt = int(_wd.get("port") or 6099)
            _hs = str(_wd.get("host") or "127.0.0.1").strip() or "127.0.0.1"
            if _hs in ("0.0.0.0", "::"):
                _hs = "127.0.0.1"
        except Exception:
            _pt, _hs = 6099, "127.0.0.1"
        return f"http://{_hs}:{_pt}/webui?token={_t}"
    import json as _json
    base = Path(r"C:\ai\NapCat.Shell.Windows.Node")
    for p in (base / "napcat" / "config" / "webui.json",
              base / "config" / "webui.json",
              base / "napcat" / "webui.json"):
        try:
            if not p.exists():
                continue
            d = _json.loads(p.read_text(encoding="utf-8")) or {}
            tok = str(d.get("token") or "").strip()
            port = int(d.get("port") or 6099)
            host = str(d.get("host") or "127.0.0.1").strip() or "127.0.0.1"
            if host in ("0.0.0.0", "::"):
                host = "127.0.0.1"
            # 先试着拿一次登录凭证（NapCat 前端登录后用的就是它），失败再用配置里的 token
            if tok:
                try:
                    import urllib.request as _ur
                    _req = _ur.Request(f"http://{host}:{port}/api/auth/login",
                                       data=_json.dumps({"token": tok}).encode("utf-8"),
                                       headers={"Content-Type": "application/json"})
                    _r = _json.loads(_ur.urlopen(_req, timeout=3).read().decode("utf-8"))
                    _cred = str(((_r.get("data") or {}) if isinstance(_r, dict) else {}).get("Credential") or "")
                    if _cred:
                        return f"http://{host}:{port}/webui?token={_cred}"
                except Exception:
                    pass
            url = f"http://{host}:{port}/webui"
            if tok:
                url += "?token=" + tok
            return url
        except Exception:
            continue
    return "http://127.0.0.1:6099/webui"


@app.get("/api/napcat/webui")
async def napcat_webui(wait: int = 0):
    """返回点进去就不用填 Token 的 NapCat 原生后台地址。"""
    try:
        _u = _napcat_webui_url()
        # wait>0 时先等 WebUI 端口起来，避免刚点开就“拒绝连接”
        if wait and int(wait) > 0:
            import urllib.request as _ur2
            _dl = time.time() + min(int(wait), 90)
            while time.time() < _dl:
                try:
                    _ur2.urlopen(_u, timeout=2).read(1)
                    break
                except Exception:
                    await asyncio.sleep(1)
        _hint = list(napcat_hints)[-6:] or [x for x in napcat_console_tail[-40:] if "http" in x][-4:]
        _src = ("启动输出" if (_u and _u == napcat_webui_live)
                else ("QQX_AIBot 里记的密码" if str(current_config.get("napcat_webui_token") or "").strip() and str(current_config.get("napcat_webui_token") or "") in _u
                      else ("配置文件" if "token=" in _u else "无 token")))
        return {"status": "ok", "url": _u, "with_token": "token=" in _u, "source": _src, "hint": _hint}
    except Exception as e:
        return {"status": "error", "message": str(e), "url": "http://127.0.0.1:6099/webui"}


@app.post("/api/napcat/captcha")
async def napcat_captcha(body: dict = Body(...)):
    """提交腾讯验证码的 ticket / randstr，让 NapCat 完成登录第二步。"""
    try:
        return _KA.captcha_login(body.get("ticket"), body.get("randstr"),
                                 sid=body.get("sid") or "", uin=body.get("uin") or "",
                                 url=body.get("url") or "")
    except Exception as e:
        return {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}


@app.post("/api/napcat/verify_busy")
async def napcat_verify_busy():
    """前端验证/扫码弹窗还开着时的心跳：让保活这段时间别重启 NapCat。"""
    try:
        return _KA.mark_verify_busy()
    except Exception as e:
        return {"status": "error", "message": "%s: %s" % (type(e).__name__, e)}


@app.get("/api/napcat/login_status")
async def napcat_login_status():
    """NapCat 当前登录阶段（is_login / phase / error），供验证码与新设备验证流程轮询。"""
    try:
        _d = await asyncio.get_running_loop().run_in_executor(None, _KA.login_status)
        return {"status": "ok", "data": _d or {}}
    except Exception as e:
        return {"status": "error", "message": "%s: %s" % (type(e).__name__, e)}


@app.get("/api/napcat/console_tail")
async def napcat_console_tail_api(limit: int = 120):
    """回放 NapCat 最近的终端输出：NapCat 输出是实时推送的，页面刷新后不重放就看不到。"""
    try:
        _n = max(1, min(int(limit), 400))
    except Exception:
        _n = 120
    return {"status": "ok", "lines": list(napcat_console_tail)[-_n:]}


@app.get("/api/napcat/qr")
async def napcat_qr():
    """把 NapCat 落盘的登录二维码（cache/qrcode.png）原样回传，前端就能直接显示真二维码。"""
    try:
        from fastapi.responses import Response as _ImgResp
        _b, _mt = await asyncio.get_running_loop().run_in_executor(None, _KA.read_login_qr)
        if not _b:
            return {"status": "error", "message": "暂无二维码文件（NapCat 还没进入扫码登录阶段）"}
        return _ImgResp(content=_b, media_type="image/png",
                        headers={"Cache-Control": "no-store, max-age=0",
                                 "X-Qr-Mtime": str(int(_mt))})
    except Exception as e:
        return {"status": "error", "message": "%s: %s" % (type(e).__name__, e)}



@app.get("/api/napcat/quicklist")
async def napcat_quicklist():
    """本地可快速登录的 QQ 列表 + 当前快速登录配置（保活卡片用）。"""
    try:
        _lst = await asyncio.get_running_loop().run_in_executor(None, _KA.quick_login_list)
        _c = _KA.load_cfg()
        return {"status": "ok",
                "quick_account": _KA.quick_account(_c),
                "quick_password_set": bool(str(_c.get("quick_password") or _c.get("password") or "").strip()),
                "launch_cmd": _KA.launch_cmd(_c),
                "list": _lst}
    except Exception as e:
        return {"status": "error", "message": str(e)}


_napcat_starting = False   # 启动中标记：连点/并发触发时避免各起一个 NapCat（多实例抢端口）
_napcat_starting_at = 0.0  # 该标记的写入时间；超过 90 秒自动失效，避免异常路径把它永久卡住


# ==================== 协议端进程管理（NapCat / SnowLuma 统一入口）====================
# 两个协议端是并列关系，同一时刻只能有一个在跑（都会占 3000/3001）：
#   切走 -> 停掉旧协议端进程；切过去 -> 拉起新协议端进程；端口就绪后再连它的 OneBot。
snowluma_process = None
snowluma_reader_task = None
_snowluma_starting = False
_snowluma_starting_at = 0.0


def _proto_cmd(p: str) -> str:
    """协议端的启动脚本路径（没配就用默认）。"""
    if p == "snowluma":
        return (current_config.get("snowluma_cmd") or "").strip() or r"C:\ai\SnowLuma\launcher.bat"
    return (current_config.get("napcat_cmd") or "").strip() or r"C:\ai\NapCat.Shell.Windows.Node\napcat.bat"


def _proto_ws_url(p: str) -> str:
    """协议端的 OneBot WS 地址。"""
    if p == "snowluma":
        return str(current_config.get("snowluma_ws_url") or "").strip() or "ws://[filtered: mount info]:3001"
    return str(current_config.get("napcat_ws_url") or "").strip() or "ws://[filtered: mount info]:3001"


def _proto_ws_hostport(p: str):
    """从 OneBot WS 地址里解析出 (host, port)。"""
    try:
        import urllib.parse as _up
        _u = _up.urlparse(_proto_ws_url(p))
        return (_u.hostname or "127.0.0.1", int(_u.port or 3001))
    except Exception:
        return ("127.0.0.1", 3001)


async def _wait_tcp(host: str, port: int, timeout: float = 25.0) -> bool:
    """轮询等待端口可连接（协议端起来后要几秒才开始监听）。"""
    import socket as _sk

    def _try():
        try:
            with _sk.create_connection((host, port), timeout=1.0):
                return True
        except Exception:
            return False

    _t0 = time.time()
    while (time.time() - _t0) < timeout:
        try:
            if await asyncio.get_running_loop().run_in_executor(None, _try):
                return True
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return False


def _snowluma_pids() -> list:
    """找出 SnowLuma 的 node 进程 PID（命令行含 SnowLuma，避免误杀别的 node）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
             "Where-Object { $_.CommandLine -like '*SnowLuma*' } | "
             "ForEach-Object { $_.ProcessId }"],
            capture_output=True, timeout=25)
        _out = (r.stdout or b"").decode("utf-8", errors="replace")
        return [int(x) for x in _out.split() if x.strip().isdigit()]
    except Exception:
        return []


# ---------------- 协议端输出：写文件 + 跟随读取 ----------------
# 以前用 stdout=PIPE：QQX_AIBot 被强杀/崩溃时管道写端断开，协议端会不停刷
# EPIPE: broken pipe 并卡死（实测 SnowLuma 5 分钟写了 1GB 日志、OneBot 端口掉线）。
# 改成协议端自己把输出写进文件，QQX_AIBot 只跟随文件增量读，父进程死活都不影响它。
_CONSOLE_FILES = {}


def _console_path(name: str) -> str:
    """协议端控制台输出文件；文件过大先清空，避免无限增长。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + "_console.log")
    _CONSOLE_FILES[name] = p
    try:
        if os.path.exists(p) and os.path.getsize(p) > 20 * 1024 * 1024:
            open(p, "wb").close()
    except Exception:
        pass
    return p


async def _follow_console(name: str, proc, on_line=None):
    """跟随协议端写出的控制台日志文件，把新行推给前端（替代原来的管道读取）。"""
    path = _CONSOLE_FILES.get(name) or _console_path(name)
    try:
        pos = os.path.getsize(path)
    except Exception:
        pos = 0

    async def _drain():
        nonlocal pos
        try:
            size = os.path.getsize(path)
        except Exception:
            return
        if size < pos:
            pos = 0
        if size <= pos:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except Exception:
            return
        for line in chunk.splitlines():
            if on_line:
                try:
                    await on_line(line)
                except Exception:
                    pass
            await broadcast_ws({"type": "napcat_log", "text": line + "\n"})

    while proc.returncode is None:
        try:
            await _drain()
            await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            break
        except Exception as e:
            await log_publisher("ERROR", f"读取 {name} 控制台输出异常: {e}")
            break
    await _drain()


async def _read_snowluma_stream(proc):
    """SnowLuma 输出走文件，这里跟随读取并推送到前端控制台。"""
    await _follow_console("snowluma", proc)
    try:
        returncode = await proc.wait()
        await log_publisher("WARN", f"SnowLuma 进程已退出 (退出码: {returncode})")
    except Exception:
        pass


async def start_snowluma() -> dict:
    """启动本地 SnowLuma 进程（切到 SnowLuma 时的托管入口）。"""
    global snowluma_process, snowluma_reader_task, _snowluma_starting, _snowluma_starting_at
    if snowluma_process and snowluma_process.returncode is None:
        return {"status": "warn", "message": "SnowLuma 进程已经在运行中"}
    if _snowluma_starting and (time.time() - _snowluma_starting_at) < 90:
        return {"status": "warn", "message": "SnowLuma 正在启动中，请稍候"}
    # 已经在跑（多半是手工起的，QQX_AIBot 没跟踪）就不要再起一个抢端口
    try:
        _exist = await asyncio.get_running_loop().run_in_executor(None, _snowluma_pids)
        if _exist:
            return {"status": "warn",
                    "message": "SnowLuma 已在运行（PID %s），跳过启动" % ",".join(str(x) for x in _exist)}
    except Exception:
        pass
    _snowluma_starting = True
    _snowluma_starting_at = time.time()
    cmd = _proto_cmd("snowluma")
    cwd = None
    _clean = cmd.strip('"')
    if os.path.exists(_clean):
        cwd = os.path.dirname(os.path.abspath(_clean))
    elif os.path.exists(r"C:\ai\SnowLuma"):
        cwd = r"C:\ai\SnowLuma"
    try:
        await log_publisher("INFO", f"正在启动 SnowLuma 子进程: {cmd} (工作目录: {cwd}) ...")
        _sl_log = _console_path("snowluma")
        snowluma_process = await asyncio.create_subprocess_shell(
            '%s >> "%s" 2>&1' % (cmd, _sl_log),
            cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
        snowluma_reader_task = asyncio.create_task(_read_snowluma_stream(snowluma_process))
        return {"status": "ok", "message": "SnowLuma 启动成功，正在读取控制台输出"}
    except Exception as e:
        await log_publisher("ERROR", f"启动 SnowLuma 失败: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        _snowluma_starting = False


async def stop_snowluma() -> dict:
    """停止 SnowLuma 进程（含 QQX_AIBot 未跟踪、手工启动的实例）。"""
    global snowluma_process, snowluma_reader_task
    _killed = []
    if snowluma_process and snowluma_process.returncode is None:
        try:
            snowluma_process.terminate()
            try:
                await asyncio.wait_for(snowluma_process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                snowluma_process.kill()
                await asyncio.wait_for(snowluma_process.wait(), timeout=5.0)
        except Exception:
            pass
    # Windows 下 launcher 是批处理，terminate 只杀外层，node 子进程会留下来继续占端口
    try:
        _pids = await asyncio.get_running_loop().run_in_executor(None, _snowluma_pids)
        for _p in _pids:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(_p)],
                               capture_output=True, timeout=15)
                _killed.append(_p)
            except Exception:
                pass
    except Exception:
        pass
    if snowluma_reader_task:
        try:
            snowluma_reader_task.cancel()
        except Exception:
            pass
        snowluma_reader_task = None
    snowluma_process = None
    if _killed:
        return {"status": "ok", "message": "SnowLuma 已停止（清理 PID: %s）" % ",".join(str(x) for x in _killed)}
    return {"status": "ok", "message": "SnowLuma 未在运行"}


async def start_protocol(p: str) -> dict:
    """启动指定协议端的本地进程。"""
    if p == "snowluma":
        return await start_snowluma()
    return await start_napcat()


async def stop_protocol(p: str) -> dict:
    """停止指定协议端的本地进程。"""
    if p == "snowluma":
        return await stop_snowluma()
    return await stop_napcat()


def snowluma_webui_url() -> str:
    """SnowLuma 自带 WebUI 的地址（端口读它自己的 config/runtime.json，默认 5099）。

    SnowLuma 必须先在它自己的 WebUI 里注入 QQ 进程并登录，OneBot 端口才会开，
    QQX_AIBot 才连得上——所以这里给它一个可直接跳转的入口。
    """
    _port, _host = 5099, "127.0.0.1"
    try:
        _cfg = json.loads(Path(r"C:\ai\SnowLuma\config\runtime.json").read_text(encoding="utf-8")) or {}
        _port = int(_cfg.get("webuiPort") or 5099)
        _h = str(_cfg.get("webuiHost") or "").strip()
        if _h and _h not in ("0.0.0.0", "::", "::0"):
            _host = _h
    except Exception:
        pass
    return "http://%s:%d" % (_host, _port)


@app.get("/api/snowluma/webui")
async def api_snowluma_webui():
    """SnowLuma 自带 WebUI 入口 + 状态（切到 SnowLuma 后要在那里登录 / 注入 QQ）。"""
    _h, _p = _proto_ws_hostport("snowluma")
    _onebot = False
    try:
        _onebot = await _wait_tcp(_h, _p, timeout=1.0)
    except Exception:
        pass
    # WebUI 端口在监听 = SnowLuma 确实在跑（QQX_AIBot 重启后没跟踪它的进程时也准）
    _wurl = snowluma_webui_url()
    try:
        _wport = int(_wurl.rsplit(":", 1)[-1])
    except Exception:
        _wport = 5099
    _webup = False
    try:
        _webup = await _wait_tcp("127.0.0.1", _wport, timeout=1.0)
    except Exception:
        pass
    return {"status": "ok", "url": _wurl,
            "running": bool(_webup or (snowluma_process and snowluma_process.returncode is None)),
            "onebot_ready": _onebot, "onebot_host": _h, "onebot_port": _p,
            "keepalive": bool(current_config.get("snowluma_keepalive", True)),
            "ka_interval": int(current_config.get("snowluma_ka_interval") or 30),
            "ka_restarts": int(_sl_ka_state.get("restarts", 0)),
            "ka_running": _sl_ka_state.get("running")}


@app.post("/api/snowluma/start")
async def api_snowluma_start():
    """启动 SnowLuma 进程（控制台 SnowLuma 卡片上的按钮）。"""
    try:
        _r = await start_snowluma()
        await log_publisher("INFO" if (_r or {}).get("status") == "ok" else "WARN",
                            "[SnowLuma] " + str((_r or {}).get("message", "")))
        return {"status": "ok", "result": _r, "webui": snowluma_webui_url()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/snowluma/stop")
async def api_snowluma_stop():
    """停止 SnowLuma 进程。"""
    try:
        _r = await stop_snowluma()
        await log_publisher("INFO", "[SnowLuma] " + str((_r or {}).get("message", "")))
        return {"status": "ok", "result": _r}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ---------------- SnowLuma 保活（进程级：掉了自动拉起）----------------
# 对齐 NapCat 保活的定位：NapCat 保活管「掉线重登」，SnowLuma 保活管「进程没了拉起来」。
# SnowLuma 的登录态是持久的，进程拉起来后它会自己恢复在线，所以这里不碰它的登录，
# 只做进程级看护；协议端不是 SnowLuma、或开关关掉时整体挂起。
_sl_ka_state = {"checks": 0, "restarts": 0, "last_restart": "", "running": None, "last_error": ""}


async def _snowluma_alive() -> bool:
    """SnowLuma 在不在跑：看它自己的 WebUI 端口有没有在监听。"""
    try:
        _wurl = snowluma_webui_url()
        _wport = int(_wurl.rsplit(":", 1)[-1])
    except Exception:
        _wport = 5099
    try:
        return await _wait_tcp("127.0.0.1", _wport, timeout=1.0)
    except Exception:
        return False


async def _snowluma_guard_loop():
    """SnowLuma 保活主循环：进程掉了 -> 用 snowluma_cmd 自动拉起。"""
    await asyncio.sleep(15)
    while True:
        _iv = 30
        try:
            _iv = max(10, int(current_config.get("snowluma_ka_interval") or 30))
        except Exception:
            _iv = 30
        try:
            if protocol_backend() != "snowluma" or not current_config.get("snowluma_keepalive", True):
                # 不是 SnowLuma 模式、或用户关了保活：挂起，什么都不做
                _sl_ka_state["running"] = None
                await asyncio.sleep(_iv)
                continue
            if _snowluma_starting:
                # 正在启动中，别插一脚再起一个实例
                await asyncio.sleep(_iv)
                continue
            _alive = await _snowluma_alive()
            _sl_ka_state["running"] = _alive
            _sl_ka_state["checks"] += 1
            if not _alive:
                await log_publisher("WARN", "[SnowLuma 保活] 检测到进程不在，正在自动拉起…")
                _r = await start_snowluma()
                _sl_ka_state["restarts"] += 1
                _sl_ka_state["last_restart"] = time.strftime("%Y-%m-%d %H:%M:%S")
                await log_publisher("INFO" if (_r or {}).get("status") == "ok" else "WARN",
                                    "[SnowLuma 保活] " + str((_r or {}).get("message", "")))
                await broadcast_ws({"type": "snowluma_change", "running": False, "auto": True})
            await asyncio.sleep(_iv)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _sl_ka_state["last_error"] = str(e)
            await asyncio.sleep(_iv)


@app.post("/api/snowluma/keepalive")
async def api_snowluma_keepalive(body: dict):
    """保存 SnowLuma 保活设置（是否自动拉起 + 检测间隔）。"""
    try:
        _b = body or {}
        if "enabled" in _b:
            current_config["snowluma_keepalive"] = bool(_b.get("enabled"))
        if "interval" in _b:
            current_config["snowluma_ka_interval"] = max(10, int(_b.get("interval") or 30))
        save_config()
        return {"status": "ok",
                "enabled": bool(current_config.get("snowluma_keepalive", True)),
                "interval": int(current_config.get("snowluma_ka_interval") or 30)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/napcat/start")
async def start_napcat():
    """启动本地 NapCat 进程（仅 NapCat 模式可用）"""
    _g = _napcat_guard("启动 NapCat 进程")
    if _g:
        return _g
    global napcat_process, napcat_reader_task, _napcat_starting, _napcat_starting_at

    if napcat_process and napcat_process.returncode is None:
        return {"status": "warn", "message": "NapCat 进程已经在运行中"}
    if _napcat_starting and (time.time() - _napcat_starting_at) < 90:
        return {"status": "warn", "message": "NapCat 正在启动中，请稍候"}
    _napcat_starting = True
    _napcat_starting_at = time.time()

    # 启动前自动确保 3001 端口已配置
    auto_enable_napcat_ws()

    cmd = current_config.get("napcat_cmd", "").strip()
    if not cmd:
        cmd = r"C:\ai\NapCat.Shell.Windows.Node\napcat.bat"

    # 快速登录：带 -q <QQ号> 启动，NapCat 用本地票据直接免扫码登录
    _ka_cfg = _KA.load_cfg()
    _quick_qq = _KA.quick_account(_ka_cfg)
    _nap_env = None
    if _quick_qq:
        cmd = _KA.launch_cmd(_ka_cfg)
        _nap_env = dict(os.environ)
        _nap_env.update(_KA.quick_env(_ka_cfg))

    # 确定子进程工作目录，确保 NapCat 找到 node.exe、index.js 与本地 dll
    cwd = None
    clean_cmd = cmd.strip('"')
    if os.path.exists(clean_cmd):
        cwd = os.path.dirname(os.path.abspath(clean_cmd))
    elif os.path.exists(r"C:\ai\NapCat.Shell.Windows.Node"):
        cwd = r"C:\ai\NapCat.Shell.Windows.Node"

    # 启动前清掉残留的 NapCat 进程：残留实例占着 6099，新实例只能漂到 6100，
    # 而 QQX_AIBot 的 WebUI 接口按端口走，就会连到另一个实例上
    try:
        _left = await asyncio.get_running_loop().run_in_executor(None, _KA.kill_napcat_procs)
        if _left:
            await log_publisher("WARN", "[NapCat] 启动前已清理残留进程: " + ", ".join(str(x) for x in _left))
            await asyncio.sleep(1.5)
    except Exception as _e:
        await log_publisher("WARN", "[NapCat] 清理残留进程失败: %s" % _e)

    try:
        await log_publisher("INFO", f"正在启动 NapCat 子进程: {cmd} (工作目录: {cwd}) ...")
        if _quick_qq:
            await log_publisher("INFO", f"[NapCat] 已启用快速登录（QQ {_quick_qq}），本次启动免扫码")
        else:
            await log_publisher("WARN", "[NapCat] 未配置快速登录 QQ 号，启动后可能需要扫码"
                                         "（在控制台「NapCat 保活」卡片里填）")

        # 使用 asyncio 创建跨平台的子进程
        # Windows 下支持通过 shell=True 启动批处理脚本或可执行文件
        _nc_log = _console_path("napcat")
        napcat_process = await asyncio.create_subprocess_shell(
            '%s >> "%s" 2>&1' % (cmd, _nc_log),
            cwd=cwd,
            env=_nap_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL
        )

        napcat_reader_task = asyncio.create_task(_read_napcat_stream(napcat_process))
        await broadcast_ws({"type": "status_change", "napcat_running": True})
        _napcat_starting = False
        return {"status": "ok", "message": "NapCat 启动成功，正在读取控制台输出"}
    except Exception as e:
        await log_publisher("ERROR", f"启动 NapCat 失败: {e}")
        _napcat_starting = False
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
    finally:
        # 兜底：请求被取消时抛的是 CancelledError（不是 Exception），也要复位，
        # 否则会永久卡在「正在启动中」，之后点启动再无反应
        _napcat_starting = False


@app.post("/api/napcat/restart")
async def api_napcat_restart():
    """重启 NapCat 进程（它的 WebUI 跟着重启）。面板「重启 WebUI」按钮用。仅 NapCat 模式可用。"""
    _g = _napcat_guard("重启 NapCat 进程")
    if _g:
        return _g
    try:
        await log_publisher("INFO", "[NapCat] 手动重启 WebUI（重启 NapCat 进程）")
        ok = await asyncio.get_running_loop().run_in_executor(None, _KA.restart_webui)
        msg = "NapCat 已重启（WebUI 随之重启）" if ok else "重启命令已发出，但进程状态未确认"
        await log_publisher("INFO" if ok else "WARN", "[NapCat] " + msg)
        return {"status": "ok", "ok": bool(ok), "message": msg}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/napcat/stop")
async def stop_napcat():
    """停止 NapCat 进程（仅 NapCat 模式可用）"""
    _g = _napcat_guard("停止 NapCat 进程")
    if _g:
        return _g
    global napcat_process, napcat_reader_task

    if not napcat_process or napcat_process.returncode is not None:
        return {"status": "warn", "message": "NapCat 进程未在运行"}

    try:
        napcat_process.terminate()
        try:
            await asyncio.wait_for(napcat_process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            napcat_process.kill()
            await napcat_process.wait()

        # Windows 下 terminate 只杀 cmd 包装进程，node 子进程会留下来继续占端口
        try:
            _left = await asyncio.get_running_loop().run_in_executor(None, _KA.kill_napcat_procs)
            _KA.clear_live_webui()
            if _left:
                await log_publisher("WARN", "[NapCat] 已清理残留 node 进程: " + ", ".join(str(x) for x in _left))
        except Exception:
            pass

        if napcat_reader_task:
            napcat_reader_task.cancel()
            napcat_reader_task = None

        await log_publisher("INFO", "已终止 NapCat 进程")
        await broadcast_ws({"type": "status_change", "napcat_running": False})
        return {"status": "ok", "message": "NapCat 进程已停止"}
    except Exception as e:
        await log_publisher("ERROR", f"停止 NapCat 进程出错: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ---------------- QQ OneBot 客户端连接 ----------------

@app.post("/api/bot/connect")
async def connect_bot():
    """启动 OneBot 客户端连接 NapCat"""
    if bot_client.is_running:
        return {"status": "warn", "message": "QQ Bot 客户端已经在连接状态中"}

    auto_enable_napcat_ws()
    await bot_client.start()
    return {"status": "ok", "message": "已触发 QQ 连接流程"}


@app.post("/api/bot/disconnect")
async def disconnect_bot():
    """断开 OneBot 连接"""
    if not bot_client.is_running:
        return {"status": "warn", "message": "QQ Bot 客户端未连接"}

    await bot_client.stop()
    await broadcast_ws({"type": "status_change", "qq_connected": False})
    return {"status": "ok", "message": "已断开与 QQ 的连接"}


# ---------------- Web UI WebSocket ----------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.add(websocket)
    # 首次连接，回传当前实时状态
    try:
        napcat_running = (
            napcat_process is not None and napcat_process.returncode is None
        )
        qq_connected = bot_client.is_ws_connected
        await websocket.send_json({
            "type": "init_status",
            "napcat_running": napcat_running,
            "qq_connected": qq_connected
        })
        while True:
            # 保持长连接与心跳
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_websockets.discard(websocket)
    except Exception:
        active_websockets.discard(websocket)


@app.on_event("startup")
async def on_startup():
    """服务启动时自动检查并启动 OneBot Client 链路"""
    auto_enable_napcat_ws()
    if napcat_mode():
        logger.info("系统初始化完成，正在后台自动启动 NapCat OneBot 监听...")
    else:
        logger.info("系统初始化完成（协议端：SnowLuma 空协议模式），正在连接其 OneBot 服务...")
    asyncio.create_task(bot_client.start())
    asyncio.create_task(_snowluma_guard_loop())


@app.on_event("shutdown")
async def on_shutdown():
    """服务优雅停机"""
    if bot_client.is_running:
        await bot_client.stop()
    if napcat_process and napcat_process.returncode is None:
        try:
            napcat_process.terminate()
            await asyncio.wait_for(napcat_process.wait(), timeout=3.0)
        except Exception:
            napcat_process.kill()


# ==================== 记忆引擎接口（自动生成，勿手改）====================
LWB_API_SLOTS = [
    ("lwb_llm_api", "主模型", "剧情总结主模型（推荐deepseekv4f）"),
    ("lwb_l0_api", "L0 锚点提取", "逐条抽取事件/锚点的模型（推荐低成本模型，如Qwen3-8B）"),
    ("lwb_embed_api", "Embedding", "向量化模型，如 BAAI/bge-m3"),
    ("lwb_rerank_api", "重排序", "召回结果重排，如 BAAI/bge-reranker-v2-m3"),
]


def _lwb():
    """取记忆引擎门面；未接入返回 None。"""
    return getattr(_mem(), "lwb", None)


# ---------------- 【破限预设】列表 / 保存 / 切换 / 删除 ----------------
def _preset_flush():
    """写盘 + 同步给 bot 客户端（不用重启，下一句话就生效）。"""
    try:
        save_config(current_config)
    except Exception as e:
        print(f"[preset] 写盘失败：{e}")
    try:
        bot_client.update_config(current_config)
    except Exception as e:
        print(f"[preset] 同步失败：{e}")


def _preset_builtins() -> list:
    import bot as _B
    return _B.preset_builtins()


def _style_list() -> list:
    ps = current_config.get("style_presets")
    if not isinstance(current_config.get("style_enabled"), list):
        # 老配置升级：原来选中的那套（有正文的）自动勾上，升级前后行为一致
        _p0 = _preset_by_id(ps if isinstance(ps, list) else [],
                            str(current_config.get("style_preset_id") or ""))
        current_config["style_enabled"] = ([str(_p0.get("id"))]
            if (_p0 is not None and str(_p0.get("text") or "").strip()) else [])
    if not isinstance(ps, list) or not ps:
        import bot as _B
        ps = _B.style_builtins()
        current_config["style_presets"] = ps
        if not str(current_config.get("style_preset_id") or "").strip():
            current_config["style_preset_id"] = ps[0]["id"]
        _preset_flush()
    return ps


def _preset_list() -> list:
    ps = current_config.get("llm_presets")
    if not isinstance(ps, list) or not ps:
        ps = _preset_builtins()
        current_config["llm_presets"] = ps
        if not str(current_config.get("llm_preset_id") or "").strip():
            current_config["llm_preset_id"] = ps[0]["id"]
        _preset_flush()
    return ps


def _preset_by_id(ps: list, pid: str):
    for p in ps:
        if isinstance(p, dict) and str(p.get("id")) == str(pid):
            return p
    return None


# jailbreak_enabled 必须在这里 —— 少了它，/api/presets?scope=claude 虽然换了预设列表，
# 但返回的开关状态读的还是主 Bot 那个，前端就会把关闭状态的预设显示成已勾选。
_PRESET_SCOPE_KEYS = ("llm_preset_id", "llm_presets", "style_preset_id",
                      "style_presets", "style_enabled", "jailbreak_enabled")


def _preset_scope_enter(scope: str):
    """scope=claude 时，把 current_config 里的预设键临时换成 claude_* 那一套。"""
    if str(scope or "").strip().lower() != "claude":
        return None
    saved = {}
    for k in _PRESET_SCOPE_KEYS:
        saved[k] = current_config.get(k)
        cur = current_config.get("claude_" + k)
        if cur is None:
            cur = [] if (k.endswith("presets") or k == "style_enabled") else ""
        current_config[k] = cur
    return saved


def _preset_scope_exit(saved):
    """还原主 Bot 那一套，并把群聊那一套写回 claude_* 键（含落盘）。"""
    if saved is None:
        return
    for k in _PRESET_SCOPE_KEYS:
        current_config["claude_" + k] = current_config.get(k)
        current_config[k] = saved[k]
    try:
        save_config(current_config)
        bot_client.update_config(current_config)
    except Exception:
        pass


@app.get("/api/version")
async def api_version():
    """当前版本（WebUI 显示 / 检查更新用）。"""
    try:
        from version import __version__ as _v
    except Exception:
        _v = "unknown"
    return {"status": "ok", "version": _v}


# ==================== 版本与更新 ====================
# 远端仓库（owner/repo）：优先 version.REPO，其次 config 的 update_repo，最后从 git remote origin 推断。
def _upd_git(*args, timeout=60):
    return subprocess.run(["git"] + list(args), cwd=os.path.dirname(os.path.abspath(__file__)),
                          capture_output=True, text=True, timeout=timeout)


def _upd_remote_url():
    try:
        return (_upd_git("remote", "get-url", "origin", timeout=10).stdout or "").strip()
    except Exception:
        return ""


def _upd_repo():
    # 远端 owner/repo：优先 version.REPO，其次 config 的 update_repo，最后从 origin 推断（不限域名）
    try:
        from version import REPO as _R
    except Exception:
        _R = ""
    r = str(_R or current_config.get("update_repo") or "").strip()
    if r:
        return r
    m = re.search(r"^(?:https?://|ssh://|git@)([^/:]+)[/:](.+?)(?:\.git)?/?$", _upd_remote_url())
    if not m:
        return ""
    parts = [p for p in m.group(2).split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def _upd_host():
    m = re.search(r"^(?:https?://|ssh://|git@)([^/:]+)", _upd_remote_url())
    return m.group(1) if m else ""


def _upd_platform():
    # github / gitee / gitlab：优先 version.PLATFORM 或 config，其次按域名猜
    try:
        from version import PLATFORM as _P
    except Exception:
        _P = ""
    p = str(_P or current_config.get("update_platform") or "").strip().lower()
    if p:
        return p
    h = _upd_host().lower()
    if "gitlab" in h:
        return "gitlab"
    if "gitee" in h:
        return "gitee"
    return "github"


def _upd_api_url(plat, repo, host):
    from urllib.parse import quote
    host = host or {"gitlab": "gitlab.com", "gitee": "gitee.com"}.get(plat, "github.com")
    if plat == "gitlab":
        return "https://%s/api/v4/projects/%s/releases?per_page=1" % (host, quote(repo, safe=""))
    if plat == "gitee":
        return "https://gitee.com/api/v5/repos/%s/releases/latest" % repo
    return "https://api.github.com/repos/%s/releases/latest" % repo


def _upd_tags_url(plat, repo, host):
    from urllib.parse import quote
    if plat == "gitlab":
        return "https://%s/api/v4/projects/%s/repository/tags?per_page=1" % (host, quote(repo, safe=""))
    return ""


def _upd_ver(s):
    nums = re.findall(r"\d+", str(s or ""))
    return tuple(int(x) for x in nums[:3]) or (0,)


@app.get("/api/update/check")
async def api_update_check():
    # 查远端最新版本（公开仓库不需要 token）
    try:
        from version import __version__ as cur
    except Exception:
        cur = "unknown"
    repo = _upd_repo()
    if not repo:
        return {"status": "error", "current": cur,
                "msg": "还没配置远端仓库：在 config.json 里填 update_repo（形如 用户名/仓库名），"
                       "或先给本地仓库加一个 origin。"}
    plat = _upd_platform()
    host = _upd_host()
    url = _upd_api_url(plat, repo, host)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
            async with sess.get(url, headers={"User-Agent": "QQX_AIBot-update-check",
                                              "Accept": "application/vnd.github+json"}) as resp:
                if resp.status == 404:
                    return {"status": "ok", "current": cur, "latest": "", "has_update": False,
                            "repo": repo, "platform": plat,
                            "msg": "远端还没有发布任何版本（打完第一个 tag 并发布 Release 之后就能检查到）。"}
                if resp.status != 200:
                    return {"status": "error", "current": cur,
                            "msg": "远端返回 HTTP %s（%s 上的仓库名写对了吗？私有仓库需要 token，不支持）" % (resp.status, plat)}
                data = await resp.json()
    except Exception as e:
        return {"status": "error", "current": cur, "msg": "连不上 %s：%s" % (plat, e)}
    if plat == "gitlab":
        rels = data if isinstance(data, list) else []
        if rels:
            rel = rels[0]
            latest = str(rel.get("tag_name") or "").strip()
            notes = (rel.get("description") or "")[:600]
            web = "https://%s/%s/-/releases/%s" % (host, repo, latest)
        else:
            # 只打了 tag、还没建 Release：退一步用 tag 名，也能提示到
            t = _upd_git("ls-remote", "--tags", "--refs", "origin", timeout=30).stdout or ""
            tags = re.findall(r"refs/tags/(\S+)", t)
            latest = tags[-1] if tags else ""
            notes = ""
            web = "https://%s/%s/-/tags" % (host, repo)
            if latest:
                notes = "（远端只有 tag，还没建 Release）"
    else:
        latest = str(data.get("tag_name") or data.get("name") or "").strip()
        notes = (data.get("body") or "")[:600]
        web = data.get("html_url") or ""
    has = bool(latest) and _upd_ver(latest) > _upd_ver(cur)
    if not latest:
        return {"status": "ok", "current": cur, "latest": "", "has_update": False,
                "repo": repo, "platform": plat,
                "msg": "远端还没有发布任何版本（打完 tag 并发布 Release 之后就能检查到）。"}
    return {"status": "ok", "current": cur, "latest": latest, "has_update": has,
            "repo": repo, "platform": plat, "url": web, "notes": notes,
            "msg": ("发现新版本 %s" % latest) if has else "已经是最新版本"}


@app.post("/api/update/apply")
async def api_update_apply():
    # 从远端拉取更新：只动被版本控制的文件；本地有未提交改动时先 stash 备份
    def _run():
        r0 = _upd_git("rev-parse", "--short", "HEAD", timeout=15)
        if r0.returncode:
            return {"status": "error",
                    "msg": "这里不是 git 仓库（或没装 git），没法自动更新，请手动下载新版本覆盖。"}
        before = r0.stdout.strip()
        stashed = False
        try:
            st = _upd_git("status", "--porcelain", timeout=20).stdout or ""
        except Exception:
            st = ""
        dirty = [l for l in st.split("\n") if l.strip() and not l.startswith("??")]
        if dirty:
            rs = _upd_git("stash", "push", "-m", "QQX_AIBot 更新前自动备份", timeout=30)
            stashed = rs.returncode == 0
        try:
            _upd_git("fetch", "--tags", "--prune", timeout=90)
            rp = _upd_git("pull", "--ff-only", timeout=120)
            if rp.returncode:
                # 没有上游分支时（例如刚加完 origin、还没 push -u），显式指定 origin <branch> 再试
                br = (_upd_git("rev-parse", "--abbrev-ref", "HEAD", timeout=15).stdout or "").strip()
                if br and br != "HEAD":
                    rp2 = _upd_git("pull", "--ff-only", "origin", br, timeout=120)
                    if rp2.returncode == 0:
                        rp = rp2
        except Exception as e:
            if stashed:
                _upd_git("stash", "pop", timeout=30)
            return {"status": "error", "msg": "拉取失败：%s" % e, "before": before, "stashed": False}
        if rp.returncode:
            if stashed:
                _upd_git("stash", "pop", timeout=30)
            _err = ((rp.stderr or rp.stdout or "").strip())[:300]
            if re.search(r"couldn't find remote ref|no such ref|Could not find remote branch", _err):
                _err = "远端还没有你要拉取的分支——你还没把代码推上去（git push -u origin main）"
            elif re.search(r"not a git repository", _err):
                _err = "这里不是 git 仓库，请手动下载新版本覆盖"
            return {"status": "error", "before": before, "stashed": False,
                    "msg": "拉取失败：%s" % _err}
        after = (_upd_git("rev-parse", "--short", "HEAD", timeout=15).stdout or "").strip()
        changed = before != after
        return {"status": "ok", "before": before, "after": after, "changed": changed,
                "stashed": stashed,
                "msg": ("已更新 %s → %s" % (before, after)) if changed else "已经是最新，无需更新"}

    res = await asyncio.to_thread(_run)
    if res.get("status") == "ok" and res.get("changed"):
        res["msg"] = res["msg"] + "；重启程序后生效"
        if res.get("stashed"):
            res["msg"] = res["msg"] + "（本地改动已备份到 git stash，可用 git stash list 找回）"
    return res


@app.get("/api/presets")
async def presets_get(scope: str = ""):
    """scope=claude 时读写的群聊模式那一套预设。"""
    _saved = _preset_scope_enter(scope)
    try:
        return await _presets_get_impl()
    finally:
        _preset_scope_exit(_saved)


async def _presets_get_impl():
    ps = _preset_list()
    pid = str(current_config.get("llm_preset_id") or "")
    if _preset_by_id(ps, pid) is None:
        pid = str(ps[0].get("id"))
        current_config["llm_preset_id"] = pid
        _preset_flush()
    sps = _style_list()
    sid = str(current_config.get("style_preset_id") or "")
    if _preset_by_id(sps, sid) is None:
        sid = str(sps[0].get("id"))
        current_config["style_preset_id"] = sid
        _preset_flush()
    return {"status": "ok", "active": pid, "presets": ps,
            "style_active": sid, "style_presets": sps,
            "style_enabled": list(current_config.get("style_enabled") or []),
            "jailbreak_enabled": bool(current_config.get("jailbreak_enabled", True)),
            "payload_jailbreak": bool(current_config.get("payload_jailbreak", True)),
            "recency_enabled": bool(current_config.get("jailbreak_recency_enabled", True)),
            "tool_name": str(_preset_by_id(ps, pid).get("tool_name") or "")}


@app.post("/api/presets")
async def presets_post(payload: Dict[str, Any] = Body(default={})):
    _saved = _preset_scope_enter(str((payload or {}).get("scope") or ""))
    try:
        return await _presets_post_impl(payload)
    finally:
        _preset_scope_exit(_saved)


async def _presets_post_impl(payload: Dict[str, Any] = None):
    act = str(payload.get("action") or "save")
    kind = str(payload.get("kind") or "jb")
    if kind == "style":
        ps = _style_list()
        key_list, key_id = "style_presets", "style_preset_id"
        fields = ("name", "desc", "text")
    else:
        ps = _preset_list()
        key_list, key_id = "llm_presets", "llm_preset_id"
        fields = ("name", "desc", "tfe", "body", "bypass", "recency",
                  "tool_name", "tool_desc", "tool_arg_desc")
    if act == "activate":
        pid = str(payload.get("id") or "")
        p = _preset_by_id(ps, pid)
        if p is None:
            return {"status": "error", "msg": "预设不存在"}
        current_config[key_id] = pid
        if kind == "style":
            current_config["style_enabled"] = [pid]
        _preset_flush()
        return {"status": "ok", "active": pid, "tool_name": str(p.get("tool_name") or "")}
    if act == "toggle":
        # 文风专用：勾上/取消某一套（可同时勾多套，按列表顺序注入）
        pid = str(payload.get("id") or "")
        if _preset_by_id(ps, pid) is None:
            return {"status": "error", "msg": "找不到这条预设"}
        cur = [str(x) for x in (current_config.get("style_enabled") or [])]
        if bool(payload.get("on", True)):
            if pid not in cur:
                cur.append(pid)
        else:
            cur = [x for x in cur if x != pid]
        current_config["style_enabled"] = cur
        _preset_flush()
        return {"status": "ok", "style_enabled": cur, "presets": ps}
    if act == "order":
        # 拖动排序：按传来的 id 顺序重排预设列表（注入顺序 = 列表顺序）
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return {"status": "error", "msg": "缺少排序列表"}
        rank = {}
        for i, x in enumerate(ids):
            if str(x) not in rank:
                rank[str(x)] = i
        out = sorted(ps, key=lambda p: rank.get(str(p.get("id")), 9999))
        current_config[key_list] = out
        _preset_flush()
        return {"status": "ok", "presets": out}
    if act == "delete":
        pid = str(payload.get("id") or "")
        if len(ps) <= 1:
            return {"status": "error", "msg": "至少留一套预设"}
        if _preset_by_id(ps, pid) is None:
            return {"status": "error", "msg": "预设不存在"}
        left = [p for p in ps if str(p.get("id")) != pid]
        current_config[key_list] = left
        if kind == "style":
            current_config["style_enabled"] = [x for x in (current_config.get("style_enabled") or [])
                                               if str(x) != pid]
        if str(current_config.get(key_id)) == pid:
            current_config[key_id] = str(left[0].get("id"))
        _preset_flush()
        return {"status": "ok", "presets": left, "active": current_config.get(key_id)}
    if act == "blank":
        # 新建空白：只给名字和一个不撞车的工具名（破限）／空白正文（文风）
        cur = _preset_by_id(ps, str(current_config.get(key_id) or "")) or ps[0]
        if kind == "style":
            newp = {"id": "s_%d" % int(time.time()), "name": "新文风", "desc": "",
                    "builtin": False, "text": ""}
        else:
            _tn = str(cur.get("tool_name") or "game_content")
            newp = {"id": "p_%d" % int(time.time()), "name": "新预设", "desc": "",
                    "builtin": False, "tfe": "", "body": "", "bypass": "", "recency": "",
                    "tool_name": (_tn + "_new")[:48], "tool_desc": "", "tool_arg_desc": ""}
        current_config[key_list] = ps + [newp]
        if kind == "style":   # 新建/复制的文风默认勾上，省得再点一下
            _en = [str(x) for x in (current_config.get("style_enabled") or [])]
            if newp["id"] not in _en:
                _en.append(newp["id"])
            current_config["style_enabled"] = _en
        _preset_flush()
        return {"status": "ok", "presets": current_config[key_list], "id": newp["id"],
                "style_enabled": list(current_config.get("style_enabled") or [])}
    if act == "import":
        # 导入：接受单个预设对象 / 列表 / {presets:[...]}。正文原样收下（仓库不预置任何预设文本），
        # id 一律重新分配避免撞车，其余字段按上面 fields 白名单收。
        raw = payload.get("data")
        if raw is None:
            raw = payload.get("preset") or payload.get("presets")
        items = raw if isinstance(raw, list) else [raw]
        used = {str(p.get("id")) for p in ps if isinstance(p, dict)}
        added = []
        for it in items:
            if not isinstance(it, dict):
                continue
            newp = {"id": ("s_%d" if kind == "style" else "p_%d") % (int(time.time() * 1000) + len(added)),
                    "builtin": False}
            for f in fields:
                v = it.get(f)
                if v is not None:
                    newp[f] = str(v)
            if not str(newp.get("name") or "").strip():
                newp["name"] = "导入的预设"
            base_id, n = newp["id"], 1
            while newp["id"] in used:
                n += 1
                newp["id"] = "%s_%d" % (base_id, n)
            used.add(newp["id"])
            added.append(newp)
        if not added:
            return {"status": "error", "msg": "没有可导入的预设"}
        current_config[key_list] = ps + added
        _preset_flush()
        return {"status": "ok", "presets": current_config[key_list], "id": added[-1]["id"],
                "added": len(added)}
    if act in ("duplicate", "new"):
        src = _preset_by_id(ps, str(payload.get("id") or ""))
        if src is None:
            src = ps[0]
        newp = dict(src)
        newp["id"] = ("s_%d" if kind == "style" else "p_%d") % int(time.time())
        newp["name"] = str(src.get("name") or "预设") + " 副本"
        newp["builtin"] = False
        if kind != "style":
            _tn = str(src.get("tool_name") or "game_content")
            newp["tool_name"] = (_tn + "_2")[:48]
            # 文案里的工具名跟着换，免得模型被叫去调一个不存在的工具
            for k in ("tfe", "body", "bypass", "recency"):
                if isinstance(newp.get(k), str):
                    newp[k] = newp[k].replace(_tn, newp["tool_name"])
        current_config[key_list] = ps + [newp]
        if kind == "style":   # 新建/复制的文风默认勾上，省得再点一下
            _en = [str(x) for x in (current_config.get("style_enabled") or [])]
            if newp["id"] not in _en:
                _en.append(newp["id"])
            current_config["style_enabled"] = _en
        _preset_flush()
        return {"status": "ok", "presets": current_config[key_list], "id": newp["id"],
                "style_enabled": list(current_config.get("style_enabled") or [])}
    if act == "reset":
        pid = str(payload.get("id") or "")
        p = _preset_by_id(ps, pid)
        if p is None:
            return {"status": "error", "msg": "预设不存在"}
        import bot as _B
        b = _preset_by_id(_B.style_builtins() if kind == "style" else _preset_builtins(), pid)
        if b is None:
            return {"status": "error", "msg": "这套是自定义的，没有出厂文案"}
        out = [b if str(x.get("id")) == pid else x for x in ps]
        current_config[key_list] = out
        _preset_flush()
        return {"status": "ok", "presets": out}
    p = payload.get("preset")
    if not isinstance(p, dict) or not str(p.get("id") or "").strip():
        return {"status": "error", "msg": "预设数据不对"}
    pid = str(p["id"])
    out, hit = [], False
    keys = fields
    for old in ps:
        if str(old.get("id")) == pid:
            merged = dict(old)
            merged.update({k: p[k] for k in keys if k in p})
            out.append(merged)
            hit = True
        else:
            out.append(old)
    if not hit:
        out.append(p)
    current_config[key_list] = out
    _preset_flush()
    return {"status": "ok", "presets": out}


@app.get("/api/mem/insight/{user_id}")
async def lwb_insight(user_id: str, top_n: int = 12):
    """人物关系图 / 世界状态 / 角色列表（可视化视图用）"""
    store = _mem()
    uid = _uid(user_id)
    data = await asyncio.to_thread(store.lwb_insight, uid, top_n)
    return {"status": "ok", **data}


@app.get("/api/mem/profile/{user_id}")
async def lwb_character_profile(user_id: str, name: str = "", limit: int = 60):
    """某个角色的人物档案：重要事件 / 相关事实 / 角色弧光 / 关系对象"""
    store = _mem()
    uid = _uid(user_id)
    if not str(name).strip():
        raise HTTPException(status_code=400, detail="缺少角色名")
    data = await asyncio.to_thread(store.lwb_profile, uid, name, limit)
    return {"status": "ok", **data}


@app.get("/api/mem/aliases")
async def lwb_get_aliases():
    """别名映射（别名 -> 角色主名）"""
    return {"status": "ok", "aliases": _mem().lwb_aliases()}


@app.post("/api/mem/aliases")
async def lwb_set_aliases(payload: Dict[str, Any] = Body(...)):
    """保存别名映射"""
    mapping = payload.get("aliases") if isinstance(payload, dict) else None
    saved = await asyncio.to_thread(_mem().lwb_set_aliases, mapping or {})
    return {"status": "ok", "aliases": saved}


@app.get("/api/mem/overrides")
async def lwb_get_overrides():
    """关系档位的手动覆盖表"""
    return {"status": "ok", "overrides": _mem().lwb_overrides()}


@app.post("/api/mem/overrides")
async def lwb_set_overrides(payload: Dict[str, Any] = Body(...)):
    """保存关系档位覆盖：{"甲|乙": "亲密"}；值传空表示恢复自动判定"""
    mapping = payload.get("overrides") if isinstance(payload, dict) else None
    saved = await asyncio.to_thread(_mem().lwb_set_overrides, mapping or {})
    return {"status": "ok", "overrides": saved}


@app.get("/api/mem/health")
async def lwb_health(user_id: str = ""):
    """记忆引擎就绪自检：四个槽位是否配好 / 向量索引进度 / embedding 与重排能否真的调通"""
    cfg = current_config if isinstance(current_config, dict) else {}
    slots = []
    for key, label, _desc in LWB_API_SLOTS:
        v = cfg.get(key) or {}
        slots.append({"key": key, "label": label,
                      "has_url": bool(str(v.get("url") or "").strip()),
                      "has_key": bool(str(v.get("key") or "").strip()),
                      "model": str(v.get("model") or "").strip(),
                      "ready": bool(str(v.get("url") or "").strip() and str(v.get("key") or "").strip()
                                    and str(v.get("model") or "").strip())})
    out = {"status": "ok", "slots": slots, "vector": {}, "embed": {}, "rerank": {},
           "recall": {"weights": {"lexical": 0.30, "vector": 0.45, "graph": 0.25},
                      "top_k": int(_lwb_opt("memory_top_k") or 5),
                      "threshold": int(_lwb_opt("lwb_summary_threshold", _view_scope) or 150),
                      "keep_recent": int(_lwb_opt("lwb_keep_recent", _view_scope) or 20)}}
    st = _mem()
    if st is not None:
        try:
            if str(user_id).strip():
                _huid = _uid(user_id)
                try:
                    _hmsgs = _hist().get_messages(_huid)
                except Exception:
                    _hmsgs = []
                s = await asyncio.to_thread(st.stats, _huid, len(_hmsgs))
                out["vector"] = {"scope": str(user_id), "total": int(s.get("count") or 0),
                                 "vectorized": int(s.get("vector_count") or 0),
                                 "cursor": int(s.get("cursor") or 0),
                                 "pending": int(s.get("pending") or 0),
                                 "messages": int(s.get("total_messages") or 0)}
            else:
                tot = vec = 0
                for c in (await asyncio.to_thread(st.list_conversations) or []):
                    _u2 = c.get("user_id")
                    try:
                        _m2 = _hist().get_messages(_uid(_u2))
                    except Exception:
                        _m2 = []
                    s2 = await asyncio.to_thread(st.stats, _u2, len(_m2))
                    tot += int(s2.get("count") or 0)
                    vec += int(s2.get("vector_count") or 0)
                out["vector"] = {"scope": "all", "total": tot, "vectorized": vec}
        except Exception as e:
            out["vector"] = {"error": str(e)}
        # embedding 真调一次
        try:
            emb = getattr(st, "embedder", None)
            ok = False
            if emb is not None:
                v = await asyncio.to_thread(emb.embed, ["连通性测试"])
                ok = bool(v)
            out["embed"] = {"configured": emb is not None, "ok": ok}
        except Exception as e:
            out["embed"] = {"configured": True, "ok": False, "error": str(e)}
    # 重排真调一次
    try:
        rr = getattr(bot_client, "lwb_rerank_call", None)
        if not rr:
            out["rerank"] = {"configured": False, "ok": False}
        else:
            r = await asyncio.to_thread(rr, "连通性测试", ["甲", "乙"], 2)
            out["rerank"] = {"configured": True, "ok": bool(r), "scores": r or None}
    except Exception as e:
        out["rerank"] = {"configured": True, "ok": False, "error": str(e)}
    return out


@app.post("/api/mem/probe_models")
async def lwb_probe_models(payload: Dict[str, Any] = Body(default=None)):
    """给「记忆引擎 → API 配置」用：拿地址 + 密钥去拉远端模型列表，供下拉点选"""
    data = payload or {}
    base = str(data.get("url") or "").strip()
    key = str(data.get("api_key") or "").strip()
    if not base:
        return JSONResponse({"status": "error", "message": "接口地址不能为空"}, status_code=400)
    try:
        models = await fetch_available_models(base, key)
        return {"status": "ok", "count": len(models or []), "models": models or []}
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"拉取模型列表失败: {e}"}, status_code=500)


@app.get("/api/mem/apis")
async def lwb_get_apis():
    """四个 API 配置位：主模型 / L0 锚点提取 / Embedding / 重排序"""
    cfg = current_config if isinstance(current_config, dict) else {}
    out = []
    for key, label, desc in LWB_API_SLOTS:
        v = cfg.get(key) or {}
        out.append({"key": key, "label": label, "desc": desc,
                    "provider": v.get("provider") or "custom",
                    "url": v.get("url") or "",
                    "api_key": v.get("key") or "",
                    "model": v.get("model") or "",
                    "ready": bool(v.get("url"))})
    return {"status": "ok", "slots": out}


@app.post("/api/mem/apis")
async def lwb_set_apis(payload: Dict[str, Any]):
    """保存四个 API 配置位（合并更新，只传要改的键）"""
    valid = {k for k, _, _ in LWB_API_SLOTS}
    changed = []
    for key, v in (payload or {}).items():
        if key not in valid or not isinstance(v, dict):
            continue
        cur = dict(current_config.get(key) or {})
        for kk in ("provider", "url", "key", "model"):
            if kk in v:
                cur[kk] = v[kk]
        # 前端字段名是 api_key，配置里存的是 key —— 不收这个就等于密钥白填
        if "api_key" in v:
            cur["key"] = v["api_key"]
        current_config[key] = cur
        changed.append(key)
    if changed:
        save_config(current_config)
        bot_client.update_config(current_config)
        await log_publisher("SUCCESS", "记忆引擎 API 配置已保存：" + ", ".join(changed))
    return {"status": "ok", "changed": changed}


@app.get("/api/mem/status")
async def lwb_status():
    """引擎状态：是否已接入、各模块规模"""
    lwb = _lwb()
    if lwb is None:
        return {"status": "ok", "installed": False}
    store = lwb.store
    convs = store.list_conversations()
    return {"status": "ok", "installed": True,
            "conversations": len(convs),
            "items": sum(c.get("count", 0) for c in convs),
            "events": sum(c.get("events", 0) for c in convs),
            "anchors": sum(c.get("anchors", 0) for c in convs),
            "tasks": len(lwb.tasks.store.list("all")),
            "state_vars": len(lwb.state.variables or {})}


@app.get("/api/mem/timeline/{user_id}")
async def lwb_timeline(user_id: str, kind: str = "", date: str = "",
                       date_from: str = "", date_to: str = ""):
    '''剧情时间线：返回事件与锚点，并给每条补上「源消息时间」。

    date / date_from / date_to 用 YYYY-MM-DD，按「源消息所处的那一天」筛选。
    dates 字段返回全部可用日期与条数，供前端下拉用（不受筛选影响）。
    '''
    lwb = _lwb()
    if lwb is None:
        return {"status": "error", "message": "记忆引擎未接入"}
    uid = _uid(user_id)
    items = lwb.store.list_items(uid)
    if kind in ("event", "anchor"):
        items = [i for i in items if i.get("kind") == kind]

    try:
        msgs = _hist().get_messages(uid) or []
    except Exception:
        msgs = []

    for it in items:
        ts = _lwb_item_msg_ts(it, msgs)
        it["msg_ts"] = ts
        it.update(_ts_parts(ts))

    items.sort(key=lambda x: (int(x.get("floor_start") or 0), x.get("created_at") or 0))

    counts: Dict[str, int] = {}
    for it in items:
        d = it.get("msg_date") or ""
        if d:
            counts[d] = counts.get(d, 0) + 1
    dates = [{"date": d, "count": c} for d, c in sorted(counts.items(), reverse=True)]

    date = (date or "").strip()
    date_from = (date_from or "").strip()
    date_to = (date_to or "").strip()
    if date:
        items = [i for i in items if (i.get("msg_date") or "") == date]
    elif date_from or date_to:
        def _in_range(i):
            d = i.get("msg_date") or ""
            if not d:
                return False
            if date_from and d < date_from:
                return False
            if date_to and d > date_to:
                return False
            return True
        items = [i for i in items if _in_range(i)]

    return {"status": "ok", "count": len(items), "items": items, "dates": dates}


@app.post("/api/mem/entry/{user_id}")
async def lwb_save_entry(user_id: str, payload: Dict[str, Any]):
    """新增或修改一条记忆条目（面板的条目编辑）"""
    lwb = _lwb()
    if lwb is None:
        return {"status": "error", "message": "记忆引擎未接入"}
    uid = _uid(user_id)
    eid = str(payload.get("id") or "").strip()
    if _lwb_is_all_scope(user_id):
        if not eid:
            return {"status": "error",
                    "message": "全局视图下不能新增条目，请先在左侧选一个具体会话"}
        owner = await asyncio.to_thread(_lwb_owner_of, eid)
        if owner is None:
            return {"status": "not_found", "message": "全局视图下找不到这条记忆的归属会话"}
        uid = owner
    fields = {k: payload[k] for k in
              ("kind", "title", "summary", "content", "keywords", "entities",
               "interactions", "locations", "importance", "floor_start",
               "floor_end", "hidden") if k in payload}
    if eid:
        out = lwb.store.update_item(uid, eid, **fields)
    else:
        out = lwb.store.add_item(uid, fields)
    lwb.engine.invalidate(uid)
    if out:
        try:
            lwb._index_one(uid, out)
        except Exception:
            pass
    return {"status": "ok", "item": out}


@app.get("/api/mem/tasks")
async def lwb_get_tasks():
    """委托任务列表"""
    lwb = _lwb()
    if lwb is None:
        return {"status": "error", "message": "记忆引擎未接入"}
    return {"status": "ok", "tasks": lwb.tasks.store.list("all"),
            "prompt": lwb.tasks.build_task_prompt()}


@app.post("/api/mem/tasks")
async def lwb_save_tasks(payload: Dict[str, Any]):
    """新增 / 修改 / 删除委托任务；payload.action = save|delete"""
    lwb = _lwb()
    if lwb is None:
        return {"status": "error", "message": "记忆引擎未接入"}
    from memory_engine.tasks import Task
    action = str(payload.get("action") or "save")
    name = str(payload.get("name") or "").strip()
    if action == "delete":
        ok = lwb.tasks.store.remove(name)
        return {"status": "ok", "deleted": ok}
    data = {k: payload[k] for k in
            ("name", "commands", "interval", "trigger_timing", "floor_type",
             "scope", "disabled", "button_activated") if k in payload}
    if not name:
        return {"status": "error", "message": "任务名不能为空"}
    exist = lwb.tasks.store.find(name)
    if exist:
        out = lwb.tasks.store.update(name, "all", **Task.from_dict({**exist, **data}).to_dict())
    else:
        out = lwb.tasks.store.add(Task.from_dict(data))
    return {"status": "ok", "task": out}
# ==================== 接口结束 ====================




# ==================== 记忆引擎：阈值配置与上下文压缩（自动生成）====================
LWB_DEFAULTS = {
    "captcha_popup_enabled": True,   # 检测到人工验证时自动弹窗（设置页可关）
    "lwb_summary_threshold": 150,   # 攒够多少条聊天记录触发自动总结
    "lwb_keep_recent": 20,          # 总结后保留多少条最近上下文
    "lwb_auto_summary": True,       # 是否开启自动总结
    "lwb_l0_anchor": True,          # 是否用 L0 模型单独抽取锚点
}


def _lwb_scope_key(key: str, scope: str = "main") -> str:
    """群聊模式的运行参数各存一份，键名 = claude_ + 主键名。"""
    return ("claude_" + key) if str(scope or "main").lower() == "claude" else key


def _lwb_opt(key: str, scope: str = "main"):
    """读运行参数。scope=claude 时优先读 claude_ 那份，没设过就沿用主对话那份。"""
    if str(scope or "main").lower() == "claude":
        v = current_config.get(_lwb_scope_key(key, "claude"))
        if v is not None:
            return v
    v = current_config.get(key)
    return LWB_DEFAULTS[key] if v is None else v


@app.get("/api/nb/options")
async def nb_get_options():
    """笔记本的跨对话注入设置（群聊与主 Bot 各存一份）"""
    def _pack(sc: str) -> Dict[str, Any]:
        pre = "claude_" if sc == "claude" else ""
        return {"notebook_cross_inject": bool(current_config.get(pre + "notebook_cross_inject")),
                "notebook_cross_allow": [str(x) for x in (current_config.get(pre + "notebook_cross_allow") or [])]}

    return {"status": "ok", "main": _pack("main"), "claude": _pack("claude"),
            "defaults": {"notebook_cross_inject": False, "notebook_cross_allow": []}}


@app.post("/api/nb/options")
async def nb_set_options(payload: Dict[str, Any]):
    """保存笔记本跨对话注入设置（按 scope 写对应那一份）"""
    sc = str((payload or {}).get("scope") or "main").strip().lower()
    if sc not in ("main", "claude"):
        sc = "main"
    pre = "claude_" if sc == "claude" else ""
    changed = []
    for k in ("notebook_cross_inject", "notebook_cross_allow"):
        if k in (payload or {}):
            current_config[pre + k] = payload[k]
            changed.append(pre + k)
    if changed:
        save_config(current_config)
        bot_client.update_config(current_config)
        await log_publisher("SUCCESS", ("[群聊] " if sc == "claude" else "") +
                            "笔记本跨对话注入已更新：" + ", ".join(changed))
    return {"status": "ok", "scope": sc, "changed": changed,
            "options": {"notebook_cross_inject": bool(current_config.get(pre + "notebook_cross_inject")),
                        "notebook_cross_allow": [str(x) for x in (current_config.get(pre + "notebook_cross_allow") or [])]}}


@app.get("/api/mem/options")
async def lwb_get_options():
    """自动总结阈值 / 保留条数等运行参数"""
    def _pack(sc: str) -> Dict[str, Any]:
        return {k: _lwb_opt(k, sc) for k in LWB_DEFAULTS}

    sc = _view_scope if _view_scope in ("main", "claude") else "main"
    return {"status": "ok", "scope": sc, "options": _pack(sc),
            "main": _pack("main"), "claude": _pack("claude"), "defaults": LWB_DEFAULTS}


@app.post("/api/mem/options")
async def lwb_set_options(payload: Dict[str, Any]):
    """保存运行参数（合并更新）"""
    sc = str((payload or {}).get("scope") or _view_scope or "main").strip().lower()
    if sc not in ("main", "claude"):
        sc = "main"
    changed = []
    for k in LWB_DEFAULTS:
        if k in (payload or {}):
            _k = _lwb_scope_key(k, sc)
            current_config[_k] = payload[k]
            changed.append(_k)
    if changed:
        save_config(current_config)
        bot_client.update_config(current_config)
        _tag = "[群聊] " if sc == "claude" else ""
        await log_publisher("SUCCESS", _tag + "记忆引擎参数已更新：" + ", ".join(changed))
    return {"status": "ok", "scope": sc, "changed": changed,
            "options": {k: _lwb_opt(k, sc) for k in LWB_DEFAULTS}}


async def lwb_auto_summary(uid: str, sender_name: str = "", force: bool = False, scope: str = ""):
    """聊天记录攒够阈值后自动总结 → 归档并删除已总结部分，只留最近 N 条。"""
    sc = str(scope or _view_scope or "main").strip().lower()
    if sc not in ("main", "claude"):
        sc = "main"
    st = _mem(sc)
    lwb = getattr(st, "lwb", None) if st else None
    if lwb is None:
        return None
    threshold = int(_lwb_opt("lwb_summary_threshold", sc) or 150)
    keep_recent = int(_lwb_opt("lwb_keep_recent", sc) or 20)
    msgs = _hist(sc).get_messages(uid)
    if not msgs:
        return {"skipped": True, "pending": 0, "threshold": threshold,
                "reason": "该会话还没有聊天记录"}

    def _empty_note(n):
        return {"skipped": True, "pending": 0, "threshold": threshold,
                "reason": "待总结 0 条（共 %d 条，最近 %d 条受保护）" % (n, keep_recent)}
    # 保护的最近 keep_recent 条不参与总结，也不算进阈值
    # 口径：「总结阈值」指的是「总条数」——总条数攒到阈值就触发，
    # 总结最早的（阈值 - 保留最近）条，最近 keep_recent 条不动。
    if force:
        # 手动整理：把「全部未总结的」都总结掉，不受阈值与保留限制
        summarize_n = len(msgs)
    else:
        if not _lwb_opt("lwb_auto_summary", sc) or len(msgs) < threshold:
            return {"skipped": True, "pending": len(msgs), "threshold": threshold}
        summarize_n = max(0, len(msgs) - keep_recent)
        if summarize_n <= 0:
            return {"skipped": True, "pending": 0, "threshold": threshold,
                    "reason": "阈值 %d 不大于保留最近 %d，无可总结内容" % (threshold, keep_recent)}
    unprotected = summarize_n   # 兼容后续引用
    # 喂给总结 / 锚点抽取的消息，先把生图、表情包标记剥掉（只给程序用的东西）
    try:
        from bot import _strip_media_markers as _smm
    except Exception:
        def _smm(x):
            return x
    _sum_msgs = []
    for _m in msgs[:summarize_n]:
        if isinstance(_m, dict):
            _m2 = dict(_m)
            _m2["content"] = _smm(str(_m.get("content") or ""))
            _sum_msgs.append(_m2)
        else:
            _sum_msgs.append(_m)
    try:
        res = await asyncio.to_thread(st.summarize_lwb, uid, _sum_msgs, sender_name)
    except Exception as e:
        await log_publisher("ERROR", f"记忆自动总结失败：{e}")
        return {"error": str(e)}
    # 主填表调用失败时 summarize 会吞掉异常并返回 errors，这里必须上报，
    # 否则界面只显示「事件 0 · 锚点 0」而用户以为整理成功。
    _errs = (res or {}).get("errors") or []
    if _errs:
        _em = str(_errs[0].get("error") or _errs[0])
        _to = (current_config.get("lwb_llm_api") or {}).get("timeout") or 120
        await log_publisher("ERROR",
            f"剧情总结失败：{_em}｜本次没有归档，聊天记录和「待总结」都保持原样。"
            f"多数情况是填表模型响应太慢超过 {_to} 秒，可到「记忆引擎 → API 配置」"
            f"把主模型的 timeout 调大后重试。")
        return {"error": _em, "summary": res}
    # L0 锚点抽取（独立模型）
    if _lwb_opt("lwb_l0_anchor", sc) and hasattr(lwb, "extract_anchors"):
        try:
            await asyncio.to_thread(lwb.extract_anchors, uid, _sum_msgs, sender_name)
        except Exception as e:
            print(f"[lwb] L0 锚点抽取失败（不影响总结）：{e}")
    # 压缩上下文：手动整理把已总结的全清掉（待总结归零），自动总结保留最近 keep_recent 条
    comp = {"dropped": 0, "kept": len(msgs)}
    try:
        comp = await asyncio.to_thread(bot_client.lwb_compact_history, uid,
                                        0 if force else keep_recent, sc)
    except Exception as e:
        print(f"[lwb] 上下文压缩失败：{e}")
    # 归档完顺手把新条目送去 embedding（走 lwb_embed_api），否则向量召回这一路一直是空的
    vec = {"indexed": 0}
    if (res or {}).get("events", 0) or (res or {}).get("anchors", 0) or (res or {}).get("arcs", 0):
        try:
            vec = await asyncio.to_thread(st.index_entries, uid, False)
        except Exception as e:
            print(f"[lwb] 向量索引失败（不影响归档）：{e}")
    await log_publisher("SUCCESS",
        f"记忆自动总结完成：事件 {res.get('events', 0)}，锚点 {res.get('anchors', 0)}，"
        f"弧光 {res.get('arcs', 0)}，新索引 {vec.get('indexed', 0)} 条，"
        f"压缩掉 {comp.get('dropped', 0)} 条，保留最近 {comp.get('kept', 0)} 条")
    return {"summary": res, "compact": comp, "vector": vec}


# ---------------- 自动总结：攒够阈值就在后台整理 ----------------
_AUTO_RUNNING: set = set()
_AUTO_COOLDOWN: Dict[str, float] = {}


async def _auto_summary_worker(uid: Any, sender_name: str, scope: str):
    """攒够阈值的会话，在后台整理一次；同一会话不并发，失败后 5 分钟不重试。"""
    key = "%s:%s" % (scope, uid)
    if key in _AUTO_RUNNING or _AUTO_COOLDOWN.get(key, 0.0) > time.time():
        return
    _AUTO_RUNNING.add(key)
    try:
        await asyncio.sleep(1.5)          # 先让这轮回复发出去
        r = await lwb_auto_summary(uid, sender_name, False, scope)
        if isinstance(r, dict) and r.get("error"):
            _AUTO_COOLDOWN[key] = time.time() + 300
    except Exception as e:
        _AUTO_COOLDOWN[key] = time.time() + 300
        print(f"[lwb] 自动总结失败（{key}）：{e}")
    finally:
        _AUTO_RUNNING.discard(key)


def schedule_auto_summary(uid: Any, sender_name: str = "", scope: str = "main"):
    """回复发完后调用：后台跑，不阻塞回复、不往外抛错。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        loop.create_task(_auto_summary_worker(uid, sender_name, scope))
    except Exception as e:
        print(f"[lwb] 自动总结排队失败：{e}")


@app.post("/api/mem/summarize/{user_id}")
async def lwb_summarize_now(user_id: str, force: bool = True):
    """手动触发一次剧情总结（force=true 忽略阈值）；user_id 传 all/global 时遍历全部会话"""
    raw = str(user_id or "").strip()
    if raw.lower() in ("all", "global"):
        rows, total = [], {"events": 0, "anchors": 0, "arcs": 0, "indexed": 0}
        for c in (_hist().list_conversations() or []):
            cid = c.get("user_id")
            if cid is None:
                continue
            r = await lwb_auto_summary(cid, c.get("sender_name") or "", force, _view_scope)
            if isinstance(r, dict) and isinstance(r.get("summary"), dict):
                for k in ("events", "anchors", "arcs"):
                    total[k] += int(r["summary"].get(k) or 0)
                total["indexed"] += int((r.get("vector") or {}).get("indexed") or 0)
            rows.append({"user_id": cid, "sender_name": c.get("sender_name") or "", "result": r})
        await log_publisher("SUCCESS" if rows else "WARN",
                            f"[记忆库] 全局整理：{len(rows)} 个会话，归档事件 {total['events']} 条")
        return {"status": "ok", "scope": "all", "conversations": len(rows),
                "events": total["events"], "totals": total, "results": rows}
    uid = _uid(user_id)
    msgs = _hist().get_messages(uid)
    sender = ""
    for m in reversed(msgs or []):
        if m.get("role") == "user":
            sender = m.get("sender_name") or m.get("name") or ""
            break
    return {"status": "ok", "result": await lwb_auto_summary(uid, sender, force, _view_scope)}


@app.get("/api/mem/context/{user_id}")
async def lwb_context_preview(user_id: str):
    """预览真正会发给 API 的上下文（= 聊天记录面板显示的内容）"""
    uid = _uid(user_id)
    msgs = _hist().get_messages(uid)
    keep = int(_lwb_opt("lwb_keep_recent", _view_scope) or 20)
    thr = int(_lwb_opt("lwb_summary_threshold", _view_scope) or 150)
    _unprot = (max(0, len(msgs) - keep) if len(msgs) >= thr else 0)   # 保留最近 keep 条以外、会被总结掉的条数
    return {"status": "ok", "count": len(msgs), "threshold": thr,
            "keep_recent": keep, "unprotected": _unprot,
            "will_summarize": len(msgs) >= thr,   # 总数口径：总条数达到阈值就会触发
            "messages": msgs}

# ---------------------------------------------------------------- 生图（ComfyUI）

def _comfy():
    """取生图客户端（挂在 bot_client 上，配置热更新共享同一个 config 字典）。"""
    return getattr(bot_client, "comfy", None)


@app.get("/api/comfy/status")
async def comfy_status():
    """生图后端状态：连接、队列、当前工作流、图片数。"""
    c = _comfy()
    if c is None:
        return {"status": "error", "message": "生图客户端未初始化（看控制台 [comfy] 报错）"}
    conn = await c.test_connection()
    q = await c.queue_state() if conn.get("ok") else {"ok": False}
    return {
        "status": "ok",
        "enabled": bool(current_config.get("comfy_enabled", True)),
        "ai_draw": bool(current_config.get("comfy_ai_draw", True)),
        "ai_max": int(current_config.get("comfy_ai_max", 1) or 1),
        "cooldown": int(current_config.get("comfy_cooldown", 20) or 0),
        "server": c.server,
        "current_workflow": str(current_config.get("comfy_workflow") or "") or COMFY.DEFAULT_WORKFLOW_NAME,
        "connection": conn,
        "queue": q,
        "workflows_dir": c.workflows_dir,
        "output_dir": c.output_dir,
        "image_count": len(c.list_images(limit=100000)),
        "size_presets": [{"size": _sp[0], "w": _sp[1], "h": _sp[2], "desc": _sp[3]} for _sp in COMFY.SIZE_PRESETS],
        "guide_custom": str(current_config.get("comfy_guide") or ""),
        "guide_effective": COMFY.build_comfy_guide(
            current_config.get("comfy_ai_max", 1),
            str(current_config.get("comfy_guide") or "")),
        "guide_default": COMFY.build_comfy_guide(current_config.get("comfy_ai_max", 1), ""),
        "guide_template": COMFY.COMFY_GUIDE_TMPL,
        "guide_placeholder": COMFY.GUIDE_MAX_PLACEHOLDER,
    }


@app.get("/api/comfy/workflows")
async def comfy_workflows():
    """列出工作流（含是否 API 格式、自动识别出的节点 ID）。"""
    c = _comfy()
    if c is None:
        return {"status": "error", "message": "生图客户端未初始化"}
    try:
        c.ensure_default_workflow()
    except Exception as e:
        logger.warning("写入内置默认工作流失败: %s" % e)
    return {"status": "ok", "workflows": c.list_workflows(),
            "current": str(current_config.get("comfy_workflow") or ""),
            "default_name": COMFY.DEFAULT_WORKFLOW_NAME}


@app.get("/api/comfy/workflow/{name}")
async def comfy_get_workflow(name: str):
    """取一份工作流的完整 JSON（给前端查看/编辑）。"""
    c = _comfy()
    if c is None:
        return {"status": "error", "message": "生图客户端未初始化"}
    try:
        wf = c.load_workflow(name)
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "name": os.path.basename(name), "data": wf,
            "prompt_node": c.detect_prompt_node(wf),
            "output_node": c.detect_output_node(wf),
            "negative_node": c.detect_negative_node(wf, c.detect_prompt_node(wf))}


@app.post("/api/comfy/workflow")
async def comfy_save_workflow(payload: Dict[str, Any] = Body(None)):
    """保存 / 导入工作流。内容必须是 ComfyUI「导出（API格式）」的 JSON。"""
    c = _comfy()
    if c is None:
        return {"status": "error", "message": "生图客户端未初始化"}
    d = payload or {}
    name = str(d.get("name") or "").strip()
    data = d.get("data")
    if not name:
        return {"status": "error", "message": "缺少工作流文件名"}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as e:
            return {"status": "error", "message": "JSON 解析失败：%s" % e}
    if not isinstance(data, dict) or not data:
        return {"status": "error", "message": "工作流内容为空或不是 JSON 对象"}
    try:
        saved = c.save_workflow(name, data)
    except Exception as e:
        return {"status": "error", "message": str(e)}
    await log_publisher("SUCCESS", "生图工作流已保存：%s" % saved)
    return {"status": "ok", "name": saved,
            "prompt_node": c.detect_prompt_node(data),
            "output_node": c.detect_output_node(data)}


@app.delete("/api/comfy/workflow/{name}")
async def comfy_delete_workflow(name: str):
    """删除一个工作流。"""
    c = _comfy()
    if c is None:
        return {"status": "error", "message": "生图客户端未初始化"}
    if not c.delete_workflow(name):
        return {"status": "not_found", "message": "没有这个工作流：%s" % name}
    await log_publisher("INFO", "生图工作流已删除：%s" % name)
    return {"status": "ok"}


@app.post("/api/comfy/generate")
async def comfy_generate(payload: Dict[str, Any] = Body(None)):
    """手动出图。"""
    c = _comfy()
    if c is None:
        return {"status": "error", "message": "生图客户端未初始化"}
    d = payload or {}
    prompt = str(d.get("prompt") or "").strip()
    if not prompt:
        return {"status": "error", "message": "提示词不能为空"}

    def _int(v, dv=0):
        try:
            return int(v)
        except Exception:
            return dv

    await log_publisher("INFO", "[生图] 手动出图：%s" % prompt[:80])
    try:
        res = await c.generate(
            prompt,
            workflow=str(d.get("workflow") or ""),
            prompt_node=str(d.get("prompt_node") or ""),
            negative=str(d.get("negative") or ""),
            steps=_int(d.get("steps"), 0),
            seed=_int(d.get("seed"), -1) if d.get("seed") not in (None, "") else -1,
            batch=_int(d.get("batch"), 0),
            width=_int(d.get("width"), 0),
            height=_int(d.get("height"), 0),
        )
    except Exception as e:
        return {"status": "error", "message": str(e)}
    if res.get("ok"):
        await log_publisher("SUCCESS", "[生图] 出图成功（%ss）" % res.get("elapsed"))
    else:
        await log_publisher("ERROR", "[生图] 出图失败：%s" % res.get("message"))
    return {"status": "ok" if res.get("ok") else "error", **res}


@app.get("/api/comfy/images")
async def comfy_images(limit: int = 60):
    """已生成图片列表（按时间倒序）。"""
    c = _comfy()
    if c is None:
        return {"status": "error", "message": "生图客户端未初始化"}
    return {"status": "ok", "images": c.list_images(limit=max(1, min(500, int(limit or 60))))}


@app.get("/api/comfy/image/{name}")
async def comfy_image(name: str):
    """取一张已生成的图。"""
    c = _comfy()
    if c is None:
        return JSONResponse({"status": "error", "message": "生图客户端未初始化"}, status_code=404)
    p = c.image_path(name)
    if not p:
        return JSONResponse({"status": "not_found", "message": "没有这张图：%s" % name}, status_code=404)
    return FileResponse(p)


@app.delete("/api/comfy/image/{name}")
async def comfy_delete_image(name: str):
    """删一张图。"""
    c = _comfy()
    if c is None:
        return {"status": "error", "message": "生图客户端未初始化"}
    if not c.delete_image(name):
        return {"status": "not_found", "message": "没有这张图：%s" % name}
    return {"status": "ok"}


@app.post("/api/comfy/images/clear")
async def comfy_clear_images():
    """清空出图历史（只删 comfyui_output/，不动 ComfyUI 自己的输出目录）。"""
    c = _comfy()
    if c is None:
        return {"status": "error", "message": "生图客户端未初始化"}
    n = c.clear_images()
    await log_publisher("WARN", "生图历史已清空，删除 %d 张图" % n)
    return {"status": "ok", "deleted": n}


@app.post("/api/comfy/interrupt")
async def comfy_interrupt():
    """中断当前出图任务。"""
    c = _comfy()
    if c is None:
        return {"status": "error", "message": "生图客户端未初始化"}
    ok = await c.interrupt()
    await log_publisher("WARN" if ok else "ERROR",
                        "[生图] 已发送中断" if ok else "[生图] 中断失败")
    return {"status": "ok" if ok else "error"}


# ==================== 表情包小偷（偷图 / 待审核池 / 图库 / 独立识图模型） ====================
# 表情包偷取：偷图 → 待审核池 → 独立识图模型分类入库
_STK_KEYS = [
    "sticker_steal_enabled", "sticker_steal_mode", "sticker_steal_chance",
    "sticker_steal_cooldown", "sticker_steal_audit", "sticker_steal_scope",
    "sticker_steal_kinds",
    "sticker_send_enabled", "sticker_send_where", "sticker_send_max",
    "sticker_send_fav_first", "sticker_send_guide",
    "sticker_send_separate", "sticker_pos_random", "sticker_pos_base",
    "sticker_pos_energy", "sticker_pos_polar",
    "sticker_pos_pos_emos", "sticker_pos_neg_emos",
    "sticker_pool_capacity", "sticker_max_num", "sticker_evict_usage_weight",
    "sticker_steal_wl", "sticker_steal_bl", "sticker_steal_priority", "sticker_max_mb",
    "sticker_vision_base_url", "sticker_vision_key", "sticker_vision_model",
    "sticker_vision_interval", "sticker_vision_timeout", "sticker_vision_prompt",
    "sticker_vision_fallback_transcribe",
]


def _stk():
    """表情包小偷实例（挂在 OneBotClient 上）。"""
    return getattr(bot_client, "sticker", None)


def _stk_err():
    return {"ok": False, "error": "表情包小偷没初始化起来，看控制台日志"}


@app.get("/api/sticker/status")
async def sticker_status():
    """偷图页：配置 + 待审核池 / 图库统计。"""
    st = _stk()
    if st is None:
        return _stk_err()
    st.ensure_worker()
    return st.stats()


@app.post("/api/sticker/config")
async def sticker_config(payload: Dict[str, Any] = Body(default={})):
    """保存偷图 / 识图模型配置（即时生效，不用重启）。"""
    for k in _STK_KEYS:
        if k in payload:
            v = payload[k]
            if k in ("sticker_steal_wl", "sticker_steal_bl"):
                if isinstance(v, str):
                    v = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
                v = [str(x).strip() for x in (v or []) if str(x).strip()]
            if k == "sticker_steal_kinds":
                v = [str(x) for x in (v or []) if str(x) in ("marker", "animated", "small", "all")]
            if k == "sticker_send_where":
                v = [str(x) for x in (v or []) if str(x) in ("claude", "private")]
            current_config[k] = v
    save_config(current_config)
    bot_client.update_config(current_config)
    return {"ok": True, "config": {k: current_config.get(k) for k in _STK_KEYS}}


@app.get("/api/sticker/pool")
async def sticker_pool(status: str = "", page: int = 1, size: int = 60):
    """待审核池列表。"""
    st = _stk()
    if st is None:
        return _stk_err()
    return st.pool_list(status=status, page=page, size=size)


@app.post("/api/sticker/pool/action")
async def sticker_pool_action(payload: Dict[str, Any] = Body(default={})):
    """待审核池批量操作：approve=通过 / reject=删除 / retry=重试。"""
    st = _stk()
    if st is None:
        return _stk_err()
    ids = [str(x) for x in (payload.get("ids") or [])]
    act = str(payload.get("action") or "")
    if not ids:
        return {"ok": False, "error": "没选图片"}
    if act == "approve":
        return await st.approve(ids)
    if act == "reject":
        return await st.reject(ids)
    if act == "retry":
        return await st.retry(ids)
    return {"ok": False, "error": "未知操作：%s" % act}


@app.get("/api/sticker/library")
async def sticker_library(category: str = "", q: str = "", sort: str = "new",
                          scope: str = "", fav: bool = False, page: int = 1, size: int = 40):
    """表情包库列表（分类 / 关键词 / 排序 / 作用域 / 收藏）。"""
    st = _stk()
    if st is None:
        return _stk_err()
    return st.library_list(category=category, q=q, sort=sort, scope=scope,
                           fav=fav, page=page, size=size)


@app.post("/api/sticker/library/action")
async def sticker_library_action(payload: Dict[str, Any] = Body(default={})):
    """图库批量/单条操作：delete / blacklist / fav / scope / category / update / bump。"""
    st = _stk()
    if st is None:
        return _stk_err()
    ids = [str(x) for x in (payload.get("ids") or [])]
    act = str(payload.get("action") or "")
    if not ids:
        return {"ok": False, "error": "没选表情包"}
    kw = {k: payload.get(k) for k in ("value", "desc", "tags", "scenes", "emotion", "category", "scope")
          if k in payload}
    return st.library_action(ids, act, **kw)


@app.get("/api/sticker/item/{sid}")
async def sticker_item(sid: str):
    """单条详情。"""
    st = _stk()
    if st is None:
        return _stk_err()
    it = st.get_item(sid)
    if not it:
        raise HTTPException(status_code=404, detail="没有这条")
    return {"ok": True, "item": it}


@app.get("/api/sticker/file/{sid}")
async def sticker_file(sid: str):
    """吐原图（列表和详情都直接用它，不生成缩略图，省一个依赖）。"""
    st = _stk()
    if st is None:
        raise HTTPException(status_code=404, detail="表情包小偷未初始化")
    p = st.item_path(sid)
    if not p or not Path(p).is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(str(p))


@app.get("/api/sticker/categories")
async def sticker_categories():
    st = _stk()
    if st is None:
        return _stk_err()
    return {"ok": True, "categories": st.store.categories()}


@app.post("/api/sticker/categories")
async def sticker_categories_action(payload: Dict[str, Any] = Body(default={})):
    """分类增删改：add / rename / delete。"""
    st = _stk()
    if st is None:
        return _stk_err()
    return st.categories_action(str(payload.get("action") or ""),
                                name=str(payload.get("name") or ""),
                                new=str(payload.get("new") or ""))


@app.post("/api/sticker/upload")
async def sticker_upload(payload: Dict[str, Any] = Body(default={})):
    """单张上传（前端读成 base64 传上来，省掉 multipart 依赖）。

    analyze=true 走识图队列自动分类；否则按 category 直接入库。
    """
    st = _stk()
    if st is None:
        return _stk_err()
    b64 = str(payload.get("data_b64") or "")
    if "," in b64[:80]:
        b64 = b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return {"ok": False, "error": "base64 解析失败"}
    return await st.upload(str(payload.get("name") or "upload.png"), raw,
                           analyze=bool(payload.get("analyze")),
                           category=str(payload.get("category") or ""))


@app.post("/api/sticker/library/reanalyze")
async def sticker_library_reanalyze(payload: Dict[str, Any] = Body(default={})):
    """重新识图并**直接写回库里**（识别 + 落库 + 返回结果）。"""
    st = _stk()
    if st is None:
        return _stk_err()
    ids = [str(x) for x in (payload.get("ids") or [])]
    if not ids:
        return {"ok": False, "error": "没选表情包"}
    results = []
    for sid in ids:
        r = await st.analyze(sid)
        if not r.get("ok"):
            results.append({"sid": sid, "ok": False, "error": r.get("error")})
            continue
        res = r.get("result") or {}
        w = st.apply_analysis(sid, res)
        results.append({"sid": sid, "ok": bool(w.get("ok")), "error": w.get("error"), "result": res})
    ok = sum(1 for x in results if x.get("ok"))
    return {"ok": ok > 0, "count": ok, "results": results}


@app.post("/api/sticker/analyze/{sid}")
async def sticker_analyze(sid: str):
    """VLM 复核：只返回新结果，不落库（前端对比后确认再应用）。"""
    st = _stk()
    if st is None:
        return _stk_err()
    return await st.analyze(sid)


@app.post("/api/sticker/vision/test")
async def sticker_vision_test():
    """测试表情包识图模型：现场画一张图发过去，看它认不认。"""
    st = _stk()
    if st is None:
        return _stk_err()
    try:
        import tempfile as _tf
        p = Path(_tf.gettempdir()) / "sticker_vision_test.png"
        p.write_bytes(CSS.test_png())   # 手搓笑脸：比抽象几何图更能看出模型认不认
        res = await st.vision.classify(p, st.store.categories())
        return {"ok": True, "endpoint": st.vision.endpoint(),
                "model": str(current_config.get("sticker_vision_model") or ""),
                "result": res}
    except Exception as e:
        return {"ok": False, "endpoint": st.vision.endpoint(),
                "model": str(current_config.get("sticker_vision_model") or ""),
                "error": str(e)[:300]}


# ==================== 结束 ====================


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8090, reload=False)
