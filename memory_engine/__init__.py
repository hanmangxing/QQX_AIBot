# -*- coding: utf-8 -*-
"""
memory_engine —— 长期记忆引擎（独立实现）。

模块划分：
    store.py       数据模型与存储（events / anchors 双层 + 旧格式迁移）
    lexical.py     中文分词 + BM25 倒排索引
    query.py       多源查询包构造
    diffusion.py   实体/互动共现图 + 个性化 PageRank
    recall.py      三路混合召回 + 按楼层融合
    generate.py    分段总结 / 事件 / 锚点生成
    insight.py     人物关系 / 档案 / 世界状态的可视化数据
    state2.py      状态变量引擎：解析→执行→守卫
    tasks.py       委托任务
    bridge.py      接入层：把本引擎挂到 QQbotX 的 MemoryStore 上

本包不 import app.py / bot.py，可独立测试。
"""
from .store import (
    SCHEMA_VERSION,
    ANCHOR_KINDS,
    EVENT_KINDS,
    MemoryItem,
    LwbStore,
    migrate_legacy_conversation,
)

__all__ = [
    "SCHEMA_VERSION", "ANCHOR_KINDS", "EVENT_KINDS",
    "MemoryItem", "LwbStore", "migrate_legacy_conversation",
]
