# -*- coding: utf-8 -*-
"""
P10 聚合层：把记忆条目聚合成可视化视图的数据（不依赖存储与网络，纯计算）。

可视化界面用的数据结构：
  * 人物关系  —— 由 interactions 互动对共现 + 重要度聚成 7 档强度
                 破裂 / 厌恶 / 反感 / 陌生 / 投缘 / 亲密 / 交融
  * 世界状态  —— 由 anchors 的 predicate / value / trend 聚成事实图谱
  * 人物档案  —— 某个角色相关的事件 / 事实 / 角色弧光
  * 剧情时间线 —— 事件按楼层排序（已有 lwb 时间线接口，这里只补排序与分页）

说明：7 档里的负面三档（破裂/厌恶/反感）无法只靠共现次数推出来，
      这里用条目正文里的冲突词做极性判断；极性若要精确，应由提示词直接输出。
"""
from __future__ import annotations

import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence

RELATION_TIERS = ["破裂", "厌恶", "反感", "陌生", "投缘", "亲密", "交融"]
NEGATIVE_TIERS = {"破裂", "厌恶", "反感"}

# 冲突词：只用于把关系判成负面三档，命中比例越高越负面
CONFLICT_WORDS = (
    "争吵", "冲突", "决裂", "背叛", "敌意", "威胁", "攻击", "反目", "憎恨", "厌恶",
    "反感", "怀疑", "算计", "欺骗", "欺骗", "伤害", "报复", "对立", "翻脸", "拒绝",
)

DEFAULT_TOP_N = 12


def _txt(it: Dict[str, Any]) -> str:
    return " ".join(str(it.get(k) or "") for k in ("title", "summary", "content"))


def _imp(it: Dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(it.get("importance") or 0)))
    except (TypeError, ValueError):
        return 0.0


def _names(it: Dict[str, Any]) -> List[str]:
    out, seen = [], set()
    for x in (it.get("entities") or []):
        s = str(x).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# 明显是地点 / 机构的后缀——以这些字结尾的专有名词不当人物
_PLACE_TAIL = set(
    "铺店馆楼城村镇山河湖海岛谷林园街路巷桥院寺观堂宫殿府庄寨营洞窟塔亭阁轩斋苑堡港湾洲"
    "峰岭关驿站校局署厅部委社坊号会派宗帮军队司厂场站门家党界圈"
)

# entities 是模型抽的「任意专有名词」，工具、作品、物品、称谓都会混进来。
# 下面两张表把它们挡在人物之外，免得「表情包」「生图工具」也长出一份人物档案。
_NON_PERSON_WORDS = set("""
主人 客人 大家 各位 对方 本人 自己 用户 角色 人物 群友 网友 观众 听众 读者 玩家
客服 管理员 系统 官方 老大 老板 亲们 某人 有人 谁 所有人 每个人 其他人
表情包 贴纸 图片 照片 视频 音频 音乐 歌曲 歌词 旋律 专辑 唱片 声音 语气 表情 动作
程序 软件 工具 插件 脚本 代码 模型 算法 数据 数据库 服务器 接口 账号 密码 频道 群聊
画板 画布 菜单 按钮 功能 界面 窗口 页面 网页 网站 网盘 云盘 硬盘 手机 电脑 设备
机器 相机 耳机 衣服 服装 演出服 睡裙 睡衣 道具 装备 礼物 红包 食物 包子 零食 饮料
项目 活动 会议 通知 公告 规则 计划 方案 报告 日志 表格 文档 文件 消息 记录 清单 名单
游戏 动漫 电影 小说 漫画 剧本 台词 剧情 设定 世界观 地图 攻略 关卡 技能
""".split())

# 以这些词结尾的多半是工具 / 作品 / 平台 / 物件，不是人名
_NON_PERSON_TAIL = (
    "工具", "软件", "程序", "系统", "平台", "插件", "脚本", "模型", "算法", "数据库",
    "服务器", "接口", "账号", "频道", "群聊", "画板", "画布", "菜单", "按钮", "功能",
    "界面", "窗口", "页面", "网页", "网站", "网盘", "云盘", "硬盘", "手机", "电脑",
    "设备", "机器", "耳机", "相机", "衣服", "服装", "演出服", "睡裙", "睡衣", "道具",
    "装备", "礼物", "红包", "零食", "饮料", "表情包", "贴纸", "图片", "照片", "视频",
    "音频", "音乐", "歌曲", "歌词", "旋律", "专辑", "唱片", "声音", "语气",
    "项目", "活动", "会议", "通知", "公告", "规则", "计划", "方案", "报告", "日志",
    "表格", "文档", "文件", "消息", "记录", "清单", "名单", "游戏", "动漫", "电影",
    "小说", "漫画", "剧本", "台词", "剧情", "设定", "世界观", "地图", "攻略", "关卡",
    "技能", "世界", "食谱", "助手",
    "具", "板", "序", "服", "颂", "谱", "册", "页", "篇", "稿", "剧",
)

# 书名号里的基本是作品名；括号多半是给名字加的补充说明，判断词性时先去掉
_TITLE_MARK = "《》〈〉"
_BRACKETS = ("（", "(", "【", "[", "「")


def _person_like(nm: str) -> bool:
    """这个名字本身像不像人名（只看名字，不看它出现在哪条里）。

    关系图的边来自 interactions 互动对，那边同样会混进称谓和物品名
    （「主人」「用户」「表情包」），所以判定要抽出来给两边共用。
    """
    if not nm or not (1 <= len(nm) <= 15):
        return False
    if nm[-1] in _PLACE_TAIL:
        return False
    if any(ch in nm for ch in _TITLE_MARK):
        return False
    base = nm
    for sep in _BRACKETS:
        if sep in base:
            base = base.split(sep, 1)[0].strip() or nm
            break
    if base in _NON_PERSON_WORDS or base.endswith(_NON_PERSON_TAIL):
        return False
    if "的" in nm and len(nm) > 4:
        return False
    return True


def _person_names(it: Dict[str, Any]) -> List[str]:
    """只把「像人」的实体当角色。

    entities 是模型抽的任意专有名词，地点、组织、铺子、工具、作品、称谓都会混进来，
    直接拿去当人名会让「街边早点铺」「表情包」也长出一份人物档案。
    先按 _person_like 挡掉不像人名的，再补两条和「本条」相关的：
    出现在 locations 里的、或是某个地点名一部分的。
    """
    places = {str(x).strip() for x in (it.get("locations") or []) if str(x).strip()}
    out = []
    for nm in _names(it):
        if not _person_like(nm):
            continue
        if nm in places:
            continue
        if any(nm in p and nm != p for p in places):
            continue
        out.append(nm)
    return out


def _pairs(it: Dict[str, Any]) -> List[List[str]]:
    out = []
    for p in (it.get("interactions") or []):
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            a, b = str(p[0]).strip(), str(p[1]).strip()
            if a and b and a != b:
                out.append([a, b])
    return out


def _clean(items: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """入口统一过滤：只留 dict 条目，坏数据不炸。"""
    return [i for i in (items or []) if isinstance(i, dict)]


def _norm_name(name: Any) -> str:
    """名字归一化：NFKC（花体/全角 -> 普通字符）+ 去空白 + 小写化西文。"""
    s = unicodedata.normalize("NFKC", str(name or ""))
    s = "".join(ch for ch in s if not ch.isspace())
    return s


def canonical(name: str, aliases: Optional[Dict[str, str]] = None) -> str:
    """按别名表把名字归一到主名。

    先做 NFKC 归一化：模型写昵称时经常混用花体/全角字符，同一个人的
    「寒芒𝓋𝒾𝓸𝓁𝑒𝓉」和「寒芒𝓋iolet」会被当成两个人各建一个节点。
    NFKC 会把 𝓋𝒾𝓸𝓁𝑒𝓉 这类数学字母还原成普通 ASCII，两者自然合并。
    """
    s = _norm_name(name)
    if not s or not aliases:
        return s
    return _norm_name(aliases.get(s) or s)


def relation_tier(count: int, max_count: int, importance: float, conflict_ratio: float) -> str:
    """共现次数 + 重要度 → 7 档；冲突词占比高时走负面三档。"""
    ratio = (count / max_count) if max_count > 0 else 0.0
    score = 0.6 * ratio + 0.4 * max(0.0, min(1.0, importance))
    if conflict_ratio >= 0.5:
        if score >= 0.65:
            return "破裂"
        return "厌恶" if score >= 0.35 else "反感"
    if score >= 0.8:
        return "交融"
    if score >= 0.6:
        return "亲密"
    if score >= 0.35:
        return "投缘"
    return "陌生"


# ------------------------------------------------------------------ 人物关系图
def build_graph(items: Sequence[Dict[str, Any]], aliases: Optional[Dict[str, str]] = None,
                min_count: int = 1, top_n: int = DEFAULT_TOP_N,
                overrides: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """把条目聚成 {nodes, edges, max_count}。"""
    items = _clean(items)
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[Any, Dict[str, Any]] = {}

    for it in items:
        imp = _imp(it)
        text = _txt(it)
        conflict = any(w in text for w in CONFLICT_WORDS)
        for raw in _person_names(it):
            nm = canonical(raw, aliases)
            if not nm:
                continue
            nd = nodes.setdefault(nm, {"name": nm, "count": 0, "importance": 0.0, "kinds": set()})
            nd["count"] += 1
            nd["importance"] = max(nd["importance"], imp)
            nd["kinds"].add(str(it.get("kind") or "event"))
        for p in _pairs(it):
            a, b = canonical(p[0], aliases), canonical(p[1], aliases)
            if not a or not b or a == b:
                continue
            # 互动对里也会混进称谓 / 工具 / 作品名（「主人」「表情包」），一并挡掉
            if not (_person_like(a) and _person_like(b)):
                continue
            key = (a, b) if a <= b else (b, a)
            ed = edges.setdefault(key, {"source": key[0], "target": key[1], "count": 0,
                                        "importance": 0.0, "conflict": 0, "events": []})
            ed["count"] += 1
            ed["importance"] = max(ed["importance"], imp)
            if conflict:
                ed["conflict"] += 1
            if len(ed["events"]) < 5:
                ed["events"].append({"id": it.get("id"), "title": it.get("title") or "",
                                     "summary": it.get("summary") or "", "importance": imp})

    max_count = max([e["count"] for e in edges.values()] or [1])
    out_edges = []
    for ed in edges.values():
        if ed["count"] < min_count:
            continue
        cr = ed["conflict"] / max(1, ed["count"])
        key_str = ed["source"] + "|" + ed["target"]
        forced = str((overrides or {}).get(key_str) or "").strip()
        out_edges.append({
            "source": ed["source"], "target": ed["target"],
            "count": ed["count"], "importance": round(ed["importance"], 3),
            "conflict_ratio": round(cr, 3),
            "tier": forced if forced in RELATION_TIERS else relation_tier(
                ed["count"], max_count, ed["importance"], cr),
            "tier_auto": relation_tier(ed["count"], max_count, ed["importance"], cr),
            "tier_forced": bool(forced in RELATION_TIERS),
            "events": ed["events"],
        })
    out_edges.sort(key=lambda e: (e["count"], e["importance"]), reverse=True)

    for e in out_edges:
        for nm in (e["source"], e["target"]):
            nodes.setdefault(nm, {"name": nm, "count": 0, "importance": 0.0, "kinds": set()})

    out_nodes = [{"name": n["name"], "count": n["count"],
                  "importance": round(n["importance"], 3),
                  "degree": sum(1 for e in out_edges if n["name"] in (e["source"], e["target"])),
                  "kinds": sorted(n["kinds"])}
                 for n in nodes.values()]
    out_nodes.sort(key=lambda n: (n["degree"], n["count"]), reverse=True)

    # 关系图只画「真的和别人有互动」的人：只被提及、没有任何互动对的实体
    # （称谓、工具、作品名这类残留基本都落在这一堆）不上图，单独返回给界面交代。
    related = {nm for e in out_edges for nm in (e["source"], e["target"])}
    mentioned = [n for n in out_nodes if n["name"] not in related]
    out_nodes = [n for n in out_nodes if n["name"] in related]
    return {"nodes": out_nodes[:max(top_n * 3, 30)], "edges": out_edges[:top_n * 3],
            "mentioned": [{"name": n["name"], "count": n["count"],
                           "importance": n["importance"]} for n in mentioned[:80]],
            "max_count": max_count, "tiers": RELATION_TIERS}


# ------------------------------------------------------------------ 世界状态
def build_facts(items: Sequence[Dict[str, Any]], aliases: Optional[Dict[str, str]] = None,
                limit: int = 200) -> List[Dict[str, Any]]:
    """anchors 的 predicate / value / trend → 事实图谱 主体|谓词|值|趋势。"""
    items = _clean(items)
    out = []
    for it in items:
        if str(it.get("kind") or "") != "anchor":
            continue
        pred = str(it.get("predicate") or "").strip()
        val = str(it.get("value") or "").strip()
        if not pred and not val:
            continue                       # 早期数据没有这两个字段，跳过
        ents = _names(it)
        subject = canonical(ents[0], aliases) if ents else str(it.get("title") or "").strip()
        out.append({
            "id": it.get("id"), "subject": subject,
            "predicate": pred or "—", "value": val or "—",
            "trend": str(it.get("trend") or "未变").strip() or "未变",
            "title": it.get("title") or "", "summary": it.get("summary") or "",
            "importance": round(_imp(it), 3), "floor_start": it.get("floor_start") or 0,
        })
    out.sort(key=lambda f: (f["importance"], f["floor_start"]), reverse=True)
    return out[:limit]


# ------------------------------------------------------------------ 人物档案
def list_characters(items: Sequence[Dict[str, Any]], aliases: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """所有出场角色，带相关条目数 / 重要度 / 是否有弧光。"""
    items = _clean(items)
    acc: Dict[str, Dict[str, Any]] = {}
    for it in items:
        kind = str(it.get("kind") or "event")
        names = [canonical(x, aliases) for x in _person_names(it)]
        for p in _pairs(it):
            names.extend([x for x in (canonical(p[0], aliases), canonical(p[1], aliases))
                          if _person_like(x)])
        if kind == "arc":
            nm = canonical(it.get("name") or "", aliases)
            if nm:
                names.append(nm)
        for nm in {x for x in names if x}:
            d = acc.setdefault(nm, {"name": nm, "count": 0, "events": 0, "anchors": 0,
                                    "arcs": 0, "importance": 0.0, "stage": ""})
            d["count"] += 1
            d["importance"] = max(d["importance"], _imp(it))
            if kind == "arc":
                d["arcs"] += 1
                if not d["stage"] and it.get("stage"):
                    d["stage"] = str(it.get("stage"))
            elif kind == "anchor":
                d["anchors"] += 1
            else:
                d["events"] += 1
    rows = list(acc.values())
    rows.sort(key=lambda r: (r["count"], r["importance"]), reverse=True)
    for r in rows:
        r["importance"] = round(r["importance"], 3)
    return rows


def build_profile(items: Sequence[Dict[str, Any]], name: str,
                  aliases: Optional[Dict[str, str]] = None, limit: int = 60) -> Dict[str, Any]:
    """某个角色的人物档案：重要事件（按重要度排序）+ 相关事实 + 角色弧光。"""
    items = _clean(items)
    target = canonical(name, aliases)
    events, anchors, arcs, relations = [], [], [], []

    for it in items:
        kind = str(it.get("kind") or "event")
        ents = [canonical(x, aliases) for x in _person_names(it)]
        pairs = [[canonical(p[0], aliases), canonical(p[1], aliases)] for p in _pairs(it)
                 if _person_like(canonical(p[0], aliases)) and _person_like(canonical(p[1], aliases))]
        arc_name = canonical(it.get("name") or "", aliases) if kind == "arc" else ""
        hit = target in ents or any(target in p for p in pairs) or arc_name == target
        if not hit:
            continue
        row = {"id": it.get("id"), "title": it.get("title") or "",
               "summary": it.get("summary") or "", "importance": round(_imp(it), 3),
               "floor_start": it.get("floor_start") or 0, "entities": ents,
               "locations": [str(x) for x in (it.get("locations") or [])],
               # 补全字段：档案里直接点编辑时，缺这些会把原文和关键词洗掉
               "kind": kind, "content": it.get("content") or "",
               "keywords": [str(x) for x in (it.get("keywords") or [])],
               "floor_end": it.get("floor_end") or 0,
               "hidden": bool(it.get("hidden"))}
        if kind == "arc":
            arcs.append({**row, "name": arc_name or target,
                         "stage": str(it.get("stage") or ""),
                         "trajectory": str(it.get("trajectory") or it.get("summary") or "")})
        elif kind == "anchor":
            anchors.append({**row, "predicate": str(it.get("predicate") or ""),
                            "value": str(it.get("value") or ""),
                            "trend": str(it.get("trend") or "未变")})
        else:
            events.append(row)
        for p in pairs:
            other = p[1] if p[0] == target else (p[0] if p[1] == target else None)
            if other and other != target:
                relations.append(other)

    events.sort(key=lambda e: (e["importance"], e["floor_start"]), reverse=True)
    anchors.sort(key=lambda a: (a["importance"], a["floor_start"]), reverse=True)
    arcs.sort(key=lambda a: (a["importance"], a["floor_start"]), reverse=True)

    tally: Dict[str, int] = {}
    for o in relations:
        tally[o] = tally.get(o, 0) + 1
    top_relations = [{"name": k, "count": v}
                     for k, v in sorted(tally.items(), key=lambda kv: kv[1], reverse=True)[:8]]

    latest_arc = arcs[0] if arcs else None
    return {"name": target, "events": events[:limit], "anchors": anchors[:limit],
            "arcs": arcs[:limit], "relations": top_relations,
            "stage": (latest_arc or {}).get("stage", ""),
            "trajectory": (latest_arc or {}).get("trajectory", ""),
            "counts": {"events": len(events), "anchors": len(anchors), "arcs": len(arcs)}}


# ------------------------------------------------------------------ 总入口
def build_overview(items: Sequence[Dict[str, Any]], aliases: Optional[Dict[str, Any]] = None,
                   top_n: int = DEFAULT_TOP_N,
                   overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """一次算好三个视图要的数据。"""
    items = _clean(items)
    al = {str(k): str(v) for k, v in (aliases or {}).items() if str(k).strip() and str(v).strip()}
    graph = build_graph(items, al, top_n=top_n, overrides=overrides)
    facts = build_facts(items, al)
    chars = list_characters(items, al)
    return {
        "graph": graph,
        "facts": facts,
        "characters": chars,
        "counts": {"items": len(list(items)), "nodes": len(graph["nodes"]),
                   "edges": len(graph["edges"]), "facts": len(facts),
                   "characters": len(chars),
                   "mentioned": len(graph.get("mentioned") or [])},
    }
