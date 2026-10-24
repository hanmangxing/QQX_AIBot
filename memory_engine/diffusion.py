# -*- coding: utf-8 -*-
"""
P4 实体/互动共现图 + 个性化 PageRank 扩散召回

源实现思想：向量相似度只能捞「说得像」的记忆，捞不到「同一批角色、同一地点、时间接近」
但措辞完全不同的旧剧情。图扩散用 PPR 沿共现边游走，把结构上强关联的节点捞回来。

源参数（已从 diffusion.js 的 CONFIG 原样核对，照搬）：
    ALPHA = 0.15              // 重启(传送)概率
    EPSILON = 1e-5            // L1 收敛阈值
    MAX_ITER = 50             // 硬迭代上限（通常 15~25 收敛）
    GAMMA = {what:0.40, rSem:0.40, who:0.10, where:0.05, time:0.05}
    R_SEM_MIN_SIM = 0.62, R_SEM_TOPK = 8
    TIME_WINDOW_MAX = 80, TIME_DECAY_DIVISOR = 12
    WHERE_MAX_GROUP_SIZE = 16, WHERE_FREQ_DAMP_PIVOT = 6, WHERE_FREQ_DAMP_MIN = 0.20
    COSINE_GATE = 0.46        // 语义门：低于此余弦直接剔除
    SCORE_FLOOR = 0.10        // finalScore = PPR归一 × 余弦 的下限
    DIFFUSION_CAP = 100       // 最多扩散出的非种子节点数

迭代对齐 NetworkX pagerank()：damping = 1 - ALPHA，传送项 = ALPHA。
纯 Python 稀疏实现，不依赖 numpy。
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

ALPHA = 0.15
EPSILON = 1e-5
MAX_ITER = 50
GAMMA = {"what": 0.40, "rSem": 0.40, "who": 0.10, "where": 0.05, "time": 0.05}
R_SEM_MIN_SIM = 0.62
R_SEM_TOPK = 8
TIME_WINDOW_MAX = 80
TIME_DECAY_DIVISOR = 12
WHERE_MAX_GROUP_SIZE = 16
WHERE_FREQ_DAMP_PIVOT = 6
WHERE_FREQ_DAMP_MIN = 0.20
COSINE_GATE = 0.46
SCORE_FLOOR = 0.10
DIFFUSION_CAP = 100


# ---------------------------------------------------------------- 基础度量
def cosine(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a or ()), set(b or ())
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def overlap_coefficient(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a or ()), set(b or ())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def get_floor_distance(a: Any, b: Any) -> int:
    try:
        return abs(int(a) - int(b))
    except (TypeError, ValueError):
        return 0


def get_time_score(distance: int) -> float:
    """时间/楼层距离衰减。超出窗口为 0，否则指数衰减。"""
    d = max(0, int(distance or 0))
    if d > TIME_WINDOW_MAX:
        return 0.0
    return math.exp(-d / float(TIME_DECAY_DIVISOR))


# ---------------------------------------------------------------- 特征抽取
def extract_entities(item: Dict[str, Any], exclude: Iterable[str] = ()) -> Set[str]:
    ex = set(exclude or ())
    ents: Set[str] = set()
    for key in ("entities", "keywords", "characters"):
        v = item.get(key)
        if isinstance(v, str):
            v = [v]
        for x in (v or ()):
            x = str(x).strip()
            if x and x not in ex:
                ents.add(x)
    return ents


def extract_interaction_pairs(item: Dict[str, Any], exclude: Iterable[str] = ()) -> Set[Tuple[str, str]]:
    """互动对（WHAT 通道）。显式 interactions 优先；否则用条目内实体两两共现。"""
    ex = set(exclude or ())
    pairs: Set[Tuple[str, str]] = set()
    raw = item.get("interactions")
    if isinstance(raw, list):
        for p in raw:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                a, b = str(p[0]).strip(), str(p[1]).strip()
                if a and b and a != b and a not in ex and b not in ex:
                    pairs.add(tuple(sorted((a, b))))
    if not pairs:
        ents = sorted(extract_entities(item, ex))
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                pairs.add((ents[i], ents[j]))
    return pairs


def extract_location(item: Dict[str, Any]) -> str:
    v = item.get("locations")
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)) and v:
        return str(v[0]).strip()
    return str(item.get("location") or "").strip()


def extract_all_features(items: Sequence[Dict[str, Any]], exclude: Iterable[str] = ()) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for it in items:
        iid = str(it.get("id"))
        out[iid] = {
            "entities": extract_entities(it, exclude),
            "pairs": extract_interaction_pairs(it, exclude),
            "location": extract_location(it),
            "floor": int(it.get("floor_start") or it.get("floor") or 0),
        }
    return out


def build_inverted_indices(features: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Set[str]]]:
    ent_idx: Dict[str, Set[str]] = defaultdict(set)
    loc_idx: Dict[str, Set[str]] = defaultdict(set)
    pair_idx: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    for iid, f in features.items():
        for e in f["entities"]:
            ent_idx[e].add(iid)
        if f["location"]:
            loc_idx[f["location"]].add(iid)
        for p in f["pairs"]:
            pair_idx[p].add(iid)
    return {"entity": dict(ent_idx), "location": dict(loc_idx), "pair": dict(pair_idx)}


def collect_pairs_from_index(indices: Dict[str, Dict[str, Set[str]]],
                             features: Dict[str, Dict[str, Any]]) -> Set[Tuple[str, str]]:
    """候选边生成：只在「共享实体 / 共享互动对 / 同地点」的节点之间建边。"""
    candidates: Set[Tuple[str, str]] = set()
    for idx in indices.values():
        for ids in idx.values():
            ids = sorted(ids)
            if len(ids) < 2:
                continue
            if len(ids) > WHERE_MAX_GROUP_SIZE and ids is not None:
                # 过热的公共分组（如「城镇」）不做全展开，只连相邻
                for i in range(len(ids) - 1):
                    candidates.add((ids[i], ids[i + 1]))
                continue
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    candidates.add((ids[i], ids[j]))
    return candidates


# ---------------------------------------------------------------- 建图
def _where_damp(group_size: int) -> float:
    """地点过常见时衰减其 WHERE 贡献。"""
    if group_size <= WHERE_FREQ_DAMP_PIVOT:
        return 1.0
    return max(WHERE_FREQ_DAMP_MIN, WHERE_FREQ_DAMP_PIVOT / float(group_size))


def build_graph(items: Sequence[Dict[str, Any]],
                vectors: Optional[Dict[str, Sequence[float]]] = None,
                exclude_entities: Iterable[str] = ()) -> Dict[str, Dict[str, float]]:
    """构造无向加权图：邻接表 {id: {nbr: weight}}。"""
    features = extract_all_features(items, exclude_entities)
    indices = build_inverted_indices(features)
    pairs = collect_pairs_from_index(indices, features)
    vectors = vectors or {}

    loc_size = {loc: len(ids) for loc, ids in indices["location"].items()}
    adj: Dict[str, Dict[str, float]] = defaultdict(dict)
    for a, b in pairs:
        fa, fb = features.get(a), features.get(b)
        if not fa or not fb:
            continue
        w = 0.0
        w += GAMMA["what"] * overlap_coefficient(fa["pairs"], fb["pairs"])
        w += GAMMA["rSem"] * cosine(vectors.get(a), vectors.get(b))
        w += GAMMA["who"] * jaccard(fa["entities"], fb["entities"])
        if fa["location"] and fa["location"] == fb["location"]:
            w += GAMMA["where"] * _where_damp(loc_size.get(fa["location"], 1))
        w += GAMMA["time"] * get_time_score(get_floor_distance(fa["floor"], fb["floor"]))
        if w <= 0:
            continue
        adj[a][b] = adj[a].get(b, 0.0) + w
        adj[b][a] = adj[b].get(a, 0.0) + w
    return dict(adj)


# ---------------------------------------------------------------- PPR
def build_seed_vector(seeds: Sequence[str], ids: Sequence[str]) -> List[float]:
    s = [0.0] * len(ids)
    pos = {nid: i for i, nid in enumerate(ids)}
    hit = [nid for nid in seeds if nid in pos]
    if not hit:
        return s
    w = 1.0 / len(hit)
    for nid in hit:
        s[pos[nid]] = w
    return s


def _column_normalize(adj: Dict[str, Dict[str, float]], ids: Sequence[str]) -> Tuple[List[Dict[int, float]], float]:
    """按列归一化，并返回悬挂节点（无出边）的总质量。"""
    pos = {nid: i for i, nid in enumerate(ids)}
    cols: List[Dict[int, float]] = [dict() for _ in ids]
    dangling = 0.0
    for nid in ids:
        nbrs = adj.get(nid) or {}
        total = sum(nbrs.values())
        if total <= 0:
            dangling += 1.0
            continue
        col = {}
        for nb, w in nbrs.items():
            j = pos.get(nb)
            if j is not None:
                col[j] = w / total
        cols[pos[nid]] = col
    return cols, dangling


def power_iteration(adj: Dict[str, Dict[str, float]], seeds: Sequence[str],
                    ids: Sequence[str], alpha: float = ALPHA,
                    epsilon: float = EPSILON, max_iter: int = MAX_ITER
                    ) -> Tuple[Dict[str, float], int, float]:
    """对齐 NetworkX pagerank 的幂迭代。返回 (分数, 迭代次数, 最终误差)。"""
    n = len(ids)
    if n == 0:
        return {}, 0, 0.0
    cols, dangling = _column_normalize(adj, ids)
    s = build_seed_vector(seeds, ids)
    d = 1.0 - alpha
    pi = list(s)
    iterations, err = 0, 0.0
    for iterations in range(1, max_iter + 1):
        dangling_sum = sum(pi[i] for i in range(n) if not cols[i])
        new = [0.0] * n
        for i in range(n):
            col = cols[i]
            if not col:
                continue
            pv = pi[i]
            if pv:
                for j, w in col.items():
                    new[j] += pv * w
        for j in range(n):
            new[j] = alpha * s[j] + d * (new[j] + dangling_sum * s[j])
        err = sum(abs(new[i] - pi[i]) for i in range(n))
        pi = new
        if err < epsilon:
            break
    return {ids[i]: pi[i] for i in range(n)}, iterations, err


def post_verify(pi: Dict[str, float], seeds: Sequence[str],
                vectors: Dict[str, Sequence[float]],
                query_vector: Optional[Sequence[float]] = None,
                cosine_gate: float = COSINE_GATE,
                score_floor: float = SCORE_FLOOR,
                cap: int = DIFFUSION_CAP) -> List[Dict[str, Any]]:
    """语义门 + 分数下限 + 数量上限。"""
    seed_set = set(seeds)
    max_ppr = 0.0
    for nid, v in pi.items():
        if nid not in seed_set and v > max_ppr:
            max_ppr = v
    if max_ppr <= 0:
        return []
    out: List[Dict[str, Any]] = []
    for nid, v in pi.items():
        if nid in seed_set or v <= 0:
            continue
        cos = cosine(query_vector, vectors.get(nid)) if query_vector else 1.0
        if cos < cosine_gate:
            continue
        ppr_norm = v / max_ppr
        final = ppr_norm * cos
        if final < score_floor:
            continue
        out.append({"id": nid, "ppr": v, "ppr_normalized": ppr_norm,
                    "cosine": cos, "score": final})
    out.sort(key=lambda r: -r["score"])
    return out[:cap]


def diffuse_from_seeds(seeds: Sequence[str],
                       items: Sequence[Dict[str, Any]],
                       vectors: Optional[Dict[str, Sequence[float]]] = None,
                       query_vector: Optional[Sequence[float]] = None,
                       exclude_entities: Iterable[str] = (),
                       cap: int = DIFFUSION_CAP) -> List[Dict[str, Any]]:
    """完整扩散流程：建图 → 种子向量 → 幂迭代 → 后验过滤。"""
    items = list(items)
    ids = [str(it.get("id")) for it in items]
    by_id = {str(it.get("id")): it for it in items}
    if not ids or not seeds:
        return []
    adj = build_graph(items, vectors, exclude_entities)
    pi, iterations, err = power_iteration(adj, seeds, ids)
    hits = post_verify(pi, seeds, vectors or {}, query_vector, cap=cap)
    for h in hits:
        h["item"] = by_id.get(h["id"])
        h["iterations"] = iterations
        h["error"] = err
    return hits
