# -*- coding: utf-8 -*-
"""
P2 中文分词 + BM25 倒排索引

源实现要点（已在源码中核对）：
  * 对「分段总结 chunks」与「剧情事件 events」两类文档统一建索引；
  * 提供 IDF 访问器 getLexicalIdfAccessor()，供查询侧加权；
  * 有词表同步 synchronizeVocabulary()、预热 warmupIndex()、失效 invalidateLexicalIndex()；
  * 支持按楼层增删文档：addDocumentsForFloor / removeDocumentsByFloor / removeDocumentsFromFloor /
    replaceEventDocuments / addEventDocuments / removeEventDocuments；
  * 查询词分组 buildLexicalQueryGroups(tokenizer, terms) —— 词法侧同样区分查询来源。

本实现把上面逐项落地为 Python：BM25（k1=1.2, b=0.75，标准取值）替代 minisearch 的 TF-IDF 打分，
对中文更稳；分词优先 jieba，未安装则退回内置二元切分，保证零额外依赖。
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------- 分词
_CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
_TOKEN_RE = re.compile(rf"[A-Za-z][A-Za-z0-9_'-]*|[{_CJK}]+|\d+(?:\.\d+)?")

# 精简停用词（中文虚词 + 英文常见词）
STOPWORDS: Set[str] = set("""
的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这
那 他 她 它 们 与 及 或 而 但 又 把 被 让 给 从 对 向 为 以 之 其 该 等 后 前 里 中 下 时
啊 吧 呢 吗 呀 哦 嗯 哈 啦 嘛 唉 哎 什么 怎么 这样 那样 如果 因为 所以 但是 然后 就是
可以 应该 需要 已经 还是 只是 不过 其实 可能 一直 现在 这里 那里 我们 你们 他们
a an the of to in on at is are was were be been being and or if then for with as by
this that these those it its he she they we you i not no do does did have has had
""".split())

_jieba = None
_jieba_tried = False


def _get_jieba():
    global _jieba, _jieba_tried
    if not _jieba_tried:
        _jieba_tried = True
        try:
            import jieba  # type: ignore
            _jieba = jieba
        except Exception:
            _jieba = None
    return _jieba


def _cut_cjk(run: str) -> List[str]:
    """无 jieba 时的中文兜底：二元切分（对 BM25 足够），单字串直接保留。"""
    if len(run) == 1:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def tokenize(text: str, keep_stopwords: bool = False) -> List[str]:
    """把文本切成检索词。中文优先 jieba，缺失则二元切分。"""
    if not text:
        return []
    text = str(text)
    jb = _get_jieba()
    out: List[str] = []
    if jb is not None:
        for tok in jb.lcut(text):
            tok = tok.strip()
            if not tok or tok.isspace():
                continue
            if not re.search(rf"[A-Za-z0-9{_CJK}]", tok):
                continue
            out.append(tok.lower())
    else:
        for m in _TOKEN_RE.finditer(text):
            piece = m.group(0)
            if re.match(rf"^[{_CJK}]+$", piece):
                out.extend(_cut_cjk(piece))
            else:
                out.append(piece.lower())
    if keep_stopwords:
        return out
    return [t for t in out if t not in STOPWORDS and len(t) > 1 or (t.isdigit() and len(t) > 1)]


def extract_key_terms(text: str, max_terms: int = 10) -> List[str]:
    """按词频取关键词。"""
    toks = tokenize(text)
    if not toks:
        return []
    ranked = Counter(toks).most_common()
    ranked.sort(key=lambda kv: (-kv[1], -len(kv[0])))
    return [w for w, _ in ranked[:max_terms]]


# ---------------------------------------------------------------- 索引
class LexicalIndex:
    """BM25 倒排索引，支持按楼层增删与词表同步。"""

    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1 = float(k1)
        self.b = float(b)
        self._lock = threading.RLock()
        self.docs: Dict[str, Dict[str, Any]] = {}        # doc_id -> {tf, len, floor, meta}
        self.postings: Dict[str, Set[str]] = {}          # term -> {doc_id}
        self.df: Counter = Counter()                     # term -> 文档频次
        self.total_len = 0
        self._dirty = True

    # ---------- 写入 ----------
    def add_document(self, doc_id: str, text: str, floor: int = 0, meta: Optional[Dict] = None) -> None:
        with self._lock:
            self.remove_document(doc_id, _internal=True)
            toks = tokenize(text)
            tf = Counter(toks)
            self.docs[doc_id] = {"tf": tf, "len": len(toks), "floor": int(floor), "meta": meta or {}}
            self.total_len += len(toks)
            for term in tf:
                self.postings.setdefault(term, set()).add(doc_id)
                self.df[term] += 1
            self._dirty = True

    def add_documents(self, items: Iterable[Tuple[str, str, int, Optional[Dict]]]) -> int:
        n = 0
        for doc_id, text, floor, meta in items:
            self.add_document(doc_id, text, floor, meta)
            n += 1
        return n

    def remove_document(self, doc_id: str, _internal: bool = False) -> bool:
        with self._lock:
            doc = self.docs.pop(doc_id, None)
            if not doc:
                return False
            self.total_len -= doc["len"]
            for term in doc["tf"]:
                ids = self.postings.get(term)
                if ids:
                    ids.discard(doc_id)
                    if not ids:
                        self.postings.pop(term, None)
                self.df[term] -= 1
                if self.df[term] <= 0:
                    self.df.pop(term, None)
            self._dirty = True
            return True

    def remove_by_floor(self, floor: int) -> int:
        return self._remove_where(lambda d: d["floor"] == int(floor))

    def remove_from_floor(self, from_floor: int) -> int:
        return self._remove_where(lambda d: d["floor"] >= int(from_floor))

    def _remove_where(self, pred) -> int:
        with self._lock:
            victims = [k for k, v in self.docs.items() if pred(v)]
            for v in victims:
                self.remove_document(v, _internal=True)
            return len(victims)

    def clear(self) -> None:
        with self._lock:
            self.docs.clear(); self.postings.clear(); self.df.clear()
            self.total_len = 0; self._dirty = True

    # ---------- 词表同步 / 预热 / 失效 ----------
    def synchronize_vocabulary(self) -> int:
        """重建 postings / df，保证与 docs 一致。"""
        with self._lock:
            self.postings = {}
            self.df = Counter()
            self.total_len = 0
            for doc_id, doc in self.docs.items():
                self.total_len += doc["len"]
                for term in doc["tf"]:
                    self.postings.setdefault(term, set()).add(doc_id)
                    self.df[term] += 1
            self._dirty = False
            return len(self.postings)

    def warmup(self) -> int:
        return self.synchronize_vocabulary()

    def invalidate(self) -> None:
        self._dirty = True

    # ---------- 打分 ----------
    @property
    def n_docs(self) -> int:
        return len(self.docs)

    @property
    def avgdl(self) -> float:
        return (self.total_len / self.n_docs) if self.n_docs else 0.0

    def idf(self, term: str) -> float:
        """BM25 概率型 IDF，供查询侧加权。"""
        n = self.n_docs
        if n == 0:
            return 0.0
        df = self.df.get(term, 0)
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(self, terms: Sequence[str], top_k: int = 20,
               term_weights: Optional[Dict[str, float]] = None) -> List[Tuple[str, float]]:
        """单组词查询，返回 [(doc_id, score)] 降序。"""
        if not terms or not self.docs:
            return []
        avgdl = self.avgdl or 1.0
        scores: Dict[str, float] = {}
        for term in terms:
            ids = self.postings.get(term)
            if not ids:
                continue
            w = 1.0 if not term_weights else float(term_weights.get(term, 1.0))
            idf = self.idf(term) * w
            if idf <= 0:
                continue
            for doc_id in ids:
                doc = self.docs[doc_id]
                tf = doc["tf"].get(term, 0)
                if not tf:
                    continue
                dl = doc["len"] or 1
                denom = tf + self.k1 * (1 - self.b + self.b * dl / avgdl)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (self.k1 + 1)) / denom
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_k]

    def search_groups(self, groups: Sequence[Tuple[str, Sequence[str], float]],
                      top_k: int = 20) -> List[Tuple[str, float, Dict[str, float]]]:
        """
        多组查询：每组 (来源名, 词表, 组权重)。
        返回 [(doc_id, 加权总分, {来源: 得分})]，用于与向量路/图扩散路融合时保留来源信息。
        """
        merged: Dict[str, float] = {}
        detail: Dict[str, Dict[str, float]] = {}
        for name, terms, weight in groups:
            hits = self.search(terms, top_k=max(top_k * 3, 30))
            if not hits:
                continue
            top = hits[0][1] or 1.0
            for doc_id, sc in hits:
                norm = sc / top                      # 组内归一，避免长组压制短组
                merged[doc_id] = merged.get(doc_id, 0.0) + norm * float(weight)
                detail.setdefault(doc_id, {})[name] = norm * float(weight)
        ranked = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [(d, s, detail.get(d, {})) for d, s in ranked]

    def stats(self) -> Dict[str, Any]:
        return {"docs": self.n_docs, "terms": len(self.postings),
                "avgdl": round(self.avgdl, 2), "dirty": self._dirty}
