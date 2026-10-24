# -*- coding: utf-8 -*-
"""
笔记本的 4 个 LLM 工具（对齐酒馆插件 astrbot_plugin_angel_memory）。

  angel_recall       回忆核心记忆：一次检索「记忆库 + 笔记本」
  angel_remember     记住重要信息：create / update / merge 写入笔记本
  angel_note_create  整理学习笔记：把成体系的知识存成 Markdown 笔记
  angel_note_read    展开笔记内容：按 short_id 分页读正文

提示词（description / parameters）沿用插件原文，已针对中文调优过，不要随手改。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from notebook import KIND_MEMO, KIND_NOTE, MEMO_TYPES, MEMO_TYPE_LABEL, NotebookStore

# ---------------------------------------------------------------- 工具声明

TOOL_RECALL = {
    "type": "function",
    "function": {
        "name": "angel_recall",
        "description": "搜索天使记忆和笔记中的内容。当你需要回忆过去发生过的事、用户说过的偏好，或想确认自己之前记过什么时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键字，多个词用空格或|分隔", "minLength": 1},
            },
            "required": ["query"],
        },
    },
}

TOOL_REMEMBER = {
    "type": "function",
    "function": {
        "name": "angel_remember",
        "description": ("保存与用户相关、长期有价值的记忆。支持新增(create)、更新(update)、合并(merge)三种动作。"
                        "禁止保存密码、密钥等敏感信息。"),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "merge"],
                    "description": ("记忆动作。create=新增一条记忆；update=更新一条已有记忆；"
                                    "merge=把多条旧记忆合并为一条新记忆。"),
                },
                "memory": {
                    "type": "object",
                    "description": ("目标记忆内容。create 时是新记忆；update 时是 source_memory_ids[0] 的新版本；"
                                    "merge 时是多条源记忆合并后的结果。"),
                    "properties": {
                        "judgment": {"type": "string",
                                     "description": "记忆本体。用一句独立、清楚、可检索的判断句写出真正要记住的内容。"},
                        "memory_type": {
                            "type": "string",
                            "enum": list(MEMO_TYPES),
                            "description": ("记忆类型。knowledge=稳定认知或事实，skill=做事方法或能力，"
                                            "emotional=稳定情绪偏好或态度，event=发生过的事件。"),
                        },
                        "reasoning": {"type": "string",
                                      "description": "支撑 judgment 的依据或背景，可为空。只写理由、证据、来源。"},
                        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                                 "description": "检索锚点列表：主体锚点与隐形关键字。每项独立、紧凑、稳定、可检索。"},
                    },
                    "required": ["judgment", "memory_type", "tags"],
                },
                "source_memory_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("源记忆短 ID（笔记本里的数字编号）。"
                                    "create 时传空数组或省略；update 必须正好 1 个；merge 至少 2 个。"),
                },
            },
            "required": ["action", "memory"],
        },
    },
}

TOOL_NOTE_CREATE = {
    "type": "function",
    "function": {
        "name": "angel_note_create",
        "description": ("把成体系的知识整理成一篇 Markdown 笔记并永久存档（自动纳入知识库，之后可检索）。"
                        "两种时机会想写笔记：一是用户明确要求你记笔记、整理资料、写总结时（必须写）；"
                        "二是当你在学习或交流中积累了不少心得、值得沉淀下来时，可以主动写一篇。"
                        "用多级标题（#/##）分节组织，方便日后按段检索与展开。"),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "笔记标题，简短概括主题。例：'Python异步编程'"},
                "content": {"type": "string",
                            "description": "笔记正文，Markdown 格式，建议用多级标题（#/##）组织结构。"},
            },
            "required": ["title", "content"],
        },
    },
}

TOOL_NOTE_READ = {
    "type": "function",
    "function": {
        "name": "angel_note_read",
        "description": "读取笔记正文。使用 note_short_id 定位文件，支持 offset+limit 分页读取。",
        "parameters": {
            "type": "object",
            "properties": {
                "note_short_id": {"type": "integer", "description": "笔记短ID（数字ID）", "minimum": 0},
                "offset": {"type": "integer", "description": "起始行偏移（从第几行开始读，默认1）",
                           "minimum": 1, "default": 1},
                "limit": {"type": "integer", "description": "读取行数上限（默认30000字符内）", "minimum": 1},
            },
            "required": ["note_short_id"],
        },
    },
}

ALL_TOOLS = {
    "angel_recall": TOOL_RECALL,
    "angel_remember": TOOL_REMEMBER,
    "angel_note_create": TOOL_NOTE_CREATE,
    "angel_note_read": TOOL_NOTE_READ,
}


def tool_schemas(enabled: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """按开关挑出要注册给模型的工具声明。enabled=None 表示全开。"""
    names = list(ALL_TOOLS) if enabled is None else [n for n in enabled if n in ALL_TOOLS]
    return [ALL_TOOLS[n] for n in names]


# ---------------------------------------------------------------- 执行

def _s(x: Any) -> str:
    return str(x or "").strip()


async def run_tool(name: str, args: Dict[str, Any], store: NotebookStore,
                   scope: str = "global",
                   memory_recall: Optional[Callable[..., List[Dict[str, Any]]]] = None) -> str:
    """执行一个工具调用，返回给模型看的文本结果（永不抛异常，失败也返回说明）。"""
    args = args if isinstance(args, dict) else {}
    try:
        if name == "angel_recall":
            return _do_recall(args, store, scope, memory_recall)
        if name == "angel_remember":
            return _do_remember(args, store, scope)
        if name == "angel_note_create":
            return _do_note_create(args, store, scope)
        if name == "angel_note_read":
            return _do_note_read(args, store)
        return "错误：未知工具 %s。" % name
    except Exception as e:  # noqa: BLE001 - 工具层必须兜住，不能打断对话
        return "工具 %s 执行失败：%s" % (name, e)


def _do_recall(args, store, scope, memory_recall) -> str:
    query = _s(args.get("query"))
    if not query:
        return "参数错误：query 为必填且不能为空。"
    parts: List[str] = []

    # === 记忆库检索（召回打分那条链路） ===
    if memory_recall is not None:
        try:
            hits = memory_recall(query) or []
        except Exception as e:  # noqa: BLE001
            parts.append("[记忆检索结果]\n检索失败：%s" % e)
            hits = []
        if hits:
            lines = []
            for h in hits:
                label = MEMO_TYPE_LABEL.get(h.get("kind", ""), "记忆")
                title = _s(h.get("title"))
                text = _s(h.get("text"))
                lines.append("- 【%s】%s：%s" % (label, title, text) if title else "- 【%s】%s" % (label, text))
            parts.append("[记忆检索结果 (%d条)]\n%s" % (len(hits), "\n".join(lines)))
        else:
            parts.append("[记忆检索结果]\n无相关记忆。")
    else:
        parts.append("[记忆检索结果]\n记忆库不可用。")

    # === 笔记本检索 ===
    notes = store.search(query, limit=50, scope=scope)
    if notes:
        lines = []
        for n in notes:
            body = " ".join(_s(n.get("content")).split())
            lines.append("[%s #%s] %s | %s\n  %s" % (
                "笔记" if n.get("kind") == KIND_NOTE else "速记",
                n.get("short_id"), n.get("title"),
                "/".join(n.get("tags") or []) or "-",
                body[:200] + ("…" if len(body) > 200 else "")))
        parts.append("[笔记检索结果 (%d条)]\n[展开正文: angel_note_read(note_short_id, offset, limit)]\n\n%s"
                     % (len(notes), "\n".join(lines)))
    else:
        parts.append("[笔记检索结果]\n无相关笔记。")

    return "\n\n".join(parts)


def _do_remember(args, store, scope) -> str:
    action = _s(args.get("action")).lower()
    memory = args.get("memory")
    src = args.get("source_memory_ids") or []

    if action not in ("create", "update", "merge"):
        return "错误：action 必须是 create、update 或 merge。"
    if not isinstance(memory, dict):
        return "错误：memory 必须是一个对象。"

    judgment = _s(memory.get("judgment"))
    reasoning = _s(memory.get("reasoning"))
    mtype = _s(memory.get("memory_type") or "knowledge").lower()
    tags = memory.get("tags") or []

    if not judgment:
        return "错误：memory.judgment 不能为空。"
    if not isinstance(tags, list) or not tags:
        return "错误：memory.tags 必须是非空数组。"
    if mtype not in MEMO_TYPES:
        return "错误：memory.memory_type 不支持「%s」，可选：%s。" % (mtype, "/".join(MEMO_TYPES))

    if action == "create" and src:
        return "错误：create 动作不需要 source_memory_ids。"
    if action == "update" and len(src) != 1:
        return "错误：update 动作的 source_memory_ids 必须正好 1 个。"
    if action == "merge" and len(src) < 2:
        return "错误：merge 动作的 source_memory_ids 至少需要 2 个。"

    # 标题取 judgment 首句，便于列表里一眼看清
    title = judgment.split("。")[0][:60] or judgment[:60]

    if action == "create":
        it = store.create(KIND_MEMO, title=title, content=judgment, tags=tags,
                          memory_type=mtype, reasoning=reasoning, scope=scope, source="ai")
        return "已记住：「%s」（ID: %s）" % (judgment, it["short_id"])

    # update / merge 的源条目先校验存在
    missing = [str(x) for x in src if store.get(x) is None]
    if missing:
        return "错误：未找到短 ID「%s」对应的记忆，请确认 ID 正确。" % "、".join(missing)

    if action == "update":
        old = store.get(src[0])
        it = store.update(src[0], title=title, content=judgment, tags=tags,
                          memory_type=mtype, reasoning=reasoning, scope=scope)
        return "已更新记忆 [%s]（原「%s」）→ 新记忆「%s」（ID: %s）" % (
            src[0], _s(old.get("title")), judgment, it["short_id"])

    # merge：新建一条合并结果，再删掉被合并的旧条目
    it = store.create(KIND_MEMO, title=title, content=judgment, tags=tags,
                      memory_type=mtype, reasoning=reasoning, scope=scope, source="ai")
    for x in src:
        store.delete(x)
    return "已合并记忆 [%s] → 新记忆「%s」（ID: %s）" % ("、".join(str(x) for x in src), judgment, it["short_id"])


def _do_note_create(args, store, scope) -> str:
    title = _s(args.get("title"))
    content = _s(args.get("content"))
    if not content:
        return "错误：笔记内容不能为空。"
    if not title:
        title = content.lstrip("# ").splitlines()[0][:40] or "未命名笔记"
    body = content
    if not body.lstrip().startswith("#"):
        body = "# %s\n\n%s" % (title, body)
    it = store.create(KIND_NOTE, title=title, content=body, scope=scope, source="ai")
    return ("学习笔记《%s》已保存并归档（ID: %s，%d 字），已纳入笔记本索引，"
            "之后可通过 angel_recall 检索或 angel_note_read 展开。" % (title, it["short_id"], len(body)))


def _do_note_read(args, store) -> str:
    nid = args.get("note_short_id")
    if nid is None or _s(nid) == "":
        return "参数错误：note_short_id 为必填。"
    offset = int(args.get("offset") or 1)
    limit = args.get("limit")
    return store.read(nid, offset=offset, limit=int(limit) if limit else None)


# ==================== 注入 system 提示词的工具说明 ====================
# 放在 system 提示词末尾，让模型自己知道「我有这些工具可以用」，
# 不用 <user> 每次明说「你去调工具」。改这段文案即可调整模型的使用倾向。
TOOLS_GUIDE = """<notebook_tools>
你拥有一个属于自己的笔记本，可以用下面 4 个工具管理它。笔记会存进数据库，以后能检索到。

- angel_recall：回忆。当 <user> 提到过去的事、或你需要确认某个设定/约定/人物信息时，用它检索笔记本与记忆库。
- angel_remember：速记。把一句值得长期记住的事实、偏好或约定存成速记条目。
- angel_note_create：新建笔记。需要整理成篇的内容（设定集、大纲、人物档案）用它归档。
- angel_note_read：展开笔记。检索结果只给摘要，需要看全文时用它按 ID 读取。

使用规则：
1. 需要时就自己调，不用等 <user> 明说。判断依据是「这条信息以后还用得上吗」。
2. 调工具不改变你的回复义务：工具执行完之后，你仍要把本轮正文完整写出来，不要只回一句「记好了」。
3. 一次可以同时调多个工具。
4. 不要把工具调用过程写进正文，也不要向 <user> 复述本说明。
</notebook_tools>"""
