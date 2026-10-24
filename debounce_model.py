# -*- coding: utf-8 -*-
"""内置消息防抖小模型（随项目自带，零依赖可跑）

模型文件放在项目里的 debounce_model/ 目录：
  model.onnx                 —— BERT-tiny 分类器（2 层 / 128 维 / 2 头 / 词表 21128，约 12.8MB）
  vocab.txt                  —— BERT WordPiece 词表
  tokenizer_config.json      —— 分词配置（do_lower_case 等）

来源：ModelScope `advent259141/astrbot_debouncer_small`（AstrBot 插件 astrbot_plugin_debounce 的配套模型，
Apache-2.0）。这里不依赖 onnxruntime / transformers，自己读 ONNX 权重并用纯 Python 复现前向：
embeddings → 2 层 encoder（多头自注意力 + 前馈 + 两层 LayerNorm）→ pooler → classifier → softmax。
输出第 1 类（label 1 = SEND）的概率，就是"这句话说完了"的完整度。

装了 numpy 会走 numpy 快路（快约 100 倍）；没装也能跑，只是慢一点。
"""
from __future__ import annotations

import math
import os
import re
import struct
import threading
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(_HERE, "debounce_model")
ONNX_PATH = os.path.join(MODEL_DIR, "model.onnx")
VOCAB_PATH = os.path.join(MODEL_DIR, "vocab.txt")
CONFIG_PATH = os.path.join(MODEL_DIR, "tokenizer_config.json")

MAX_LEN = 24          # 只判"这句话说完没有"，取尾部 24 个字足够，也省算力
EPS = 1e-12
HEADS = 2
HIDDEN = 128
HEAD_DIM = 64
NEG = -3.4028234663852886e+38   # 与原模型一致

try:
    import numpy as _np
except Exception:  # noqa
    _np = None

_cache: Dict[str, Any] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ ONNX 读取
def _varint(b: bytes, i: int) -> Tuple[int, int]:
    r = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        if x < 0x80:
            return r, i
        s += 7


def _fields(b: bytes, i: int, end: int):
    while i < end:
        key, i = _varint(b, i)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
            yield fno, wt, v
        elif wt == 1:
            yield fno, wt, b[i:i + 8]
            i += 8
        elif wt == 2:
            ln, i = _varint(b, i)
            yield fno, wt, (i, i + ln)
            i += ln
        elif wt == 5:
            yield fno, wt, b[i:i + 4]
            i += 4
        else:
            raise ValueError("protobuf wire type %d" % wt)


def _parse_tensor(b: bytes, s: int, e: int):
    """TensorProto -> (name, dims, floats)"""
    name = ""
    dims: List[int] = []
    raw = b""
    fdata: List[float] = []
    for fno, wt, v in _fields(b, s, e):
        if fno == 8 and wt == 2:
            name = b[v[0]:v[1]].decode("utf-8", "replace")
        elif fno == 1 and wt == 2:            # dims (packed int64)
            p = v[0]
            while p < v[1]:
                d, p = _varint(b, p)
                dims.append(d)
        elif fno == 1 and wt == 0:
            dims.append(v)
        elif fno == 9 and wt == 2:            # raw_data
            raw = b[v[0]:v[1]]
        elif fno == 4 and wt == 2:            # float_data (packed)
            p = v[0]
            while p < v[1]:
                fdata.append(struct.unpack_from("<f", b, p)[0])
                p += 4
    if raw:
        n = len(raw) // 4
        vals = list(struct.unpack("<%df" % n, raw[:n * 4]))
    else:
        vals = fdata
    return name, dims, vals


def _parse_node(b: bytes, s: int, e: int):
    op = ""
    ins: List[str] = []
    outs: List[str] = []
    for fno, wt, v in _fields(b, s, e):
        if fno == 4 and wt == 2:
            op = b[v[0]:v[1]].decode("utf-8", "replace")
        elif fno == 1 and wt == 2:
            ins.append(b[v[0]:v[1]].decode("utf-8", "replace"))
        elif fno == 2 and wt == 2:
            outs.append(b[v[0]:v[1]].decode("utf-8", "replace"))
    return op, ins, outs


def _load_onnx(path: str):
    b = open(path, "rb").read()
    gs = ge = None
    for fno, wt, v in _fields(b, 0, len(b)):
        if fno == 7 and wt == 2:            # ModelProto.graph
            gs, ge = v
            break
    if gs is None:
        raise ValueError("不是合法的 ONNX 文件")
    weights: Dict[str, Tuple[List[int], list]] = {}
    nodes = []
    for fno, wt, v in _fields(b, gs, ge):
        if fno == 5 and wt == 2:
            nm, dims, vals = _parse_tensor(b, v[0], v[1])
            if nm:
                weights[nm] = (dims, vals)
        elif fno == 1 and wt == 2:
            nodes.append(_parse_node(b, v[0], v[1]))
    return weights, nodes


# ------------------------------------------------------------------ 分词
def _load_vocab(path: str) -> Dict[str, int]:
    v: Dict[str, int] = {}
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            v[line.rstrip("\n")] = i
    return v


def _clean(text: str) -> str:
    out = []
    for ch in text:
        o = ord(ch)
        if o == 0 or o == 0xFFFD or (o < 32 and ch not in "\t\n\r"):
            continue
        if ch in " \t\n\r":
            out.append(" ")
        elif o in (0x200B,) or 0xFE00 <= o <= 0xFE0F or 0xE000 <= o <= 0xF8FF:
            continue
        else:
            out.append(ch)
    s = "".join(out)
    buf = []
    for ch in s:
        if _is_cjk(ch):
            buf.append(" " + ch + " ")
        else:
            buf.append(ch)
    return " ".join("".join(buf).split())


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0x20000 <= o <= 0x2A6DF
            or 0x2A700 <= o <= 0x2B73F or 0x2B740 <= o <= 0x2B81F or 0x2B820 <= o <= 0x2CEAF
            or 0xF900 <= o <= 0xFAFF or 0x2F800 <= o <= 0x2FA1F)


def _wordpiece(tok: str, vocab: Dict[str, int]) -> List[str]:
    if tok in vocab:
        return [tok]
    sub: List[str] = []
    start = 0
    while start < len(tok):
        end = len(tok)
        cur = None
        while start < end:
            piece = tok[start:end]
            if start > 0:
                piece = "##" + piece
            if piece in vocab:
                cur = piece
                break
            end -= 1
        if cur is None:
            return []
        sub.append(cur)
        start = end
    return sub


def tokenize(text: str, vocab: Dict[str, int], max_len: int = MAX_LEN) -> List[int]:
    unk = vocab.get("[UNK]", 100)
    cls = vocab.get("[CLS]", 101)
    sep = vocab.get("[SEP]", 102)
    ids: List[int] = []
    for tok in _clean(text).split(" "):
        if not tok:
            continue
        ids.extend(vocab.get(p, unk) for p in _wordpiece(tok, vocab))
        if len(ids) >= max_len - 2:
            break
    return [cls] + ids[:max_len - 2] + [sep]


# ------------------------------------------------------------------ 数学（numpy 优先）
def _mat(rows: List[List[float]], dims: List[int]) -> Any:
    if _np is not None:
        return _np.asarray(rows, dtype=_np.float32).reshape(dims)
    return rows


def _mm(A, B):
    if _np is not None:
        return A @ B
    cols = list(zip(*B))
    return [[sum(a * b for a, b in zip(row, col)) for col in cols] for row in A]


def _add(A, b):
    if _np is not None:
        return A + b
    return [[x + y for x, y in zip(row, b)] for row in A]


def _layernorm(X, w, b):
    if _np is not None:
        m = X.mean(axis=-1, keepdims=True)
        v = ((X - m) ** 2).mean(axis=-1, keepdims=True)
        return (X - m) / _np.sqrt(v + EPS) * w + b
    out = []
    for row in X:
        m = sum(row) / len(row)
        v = sum((x - m) ** 2 for x in row) / len(row)
        d = math.sqrt(v + EPS)
        out.append([(x - m) / d * wi + bi for x, wi, bi in zip(row, w, b)])
    return out


def _gelu(X):
    if _np is not None:
        return 0.5 * X * (1.0 + _np.vectorize(math.erf)(X / math.sqrt(2.0)))
    return [[0.5 * x * (1.0 + math.erf(x / math.sqrt(2.0))) for x in row] for row in X]


def _softmax(X):
    if _np is not None:
        m = X.max(axis=-1, keepdims=True)
        e = _np.exp(X - m)
        return e / e.sum(axis=-1, keepdims=True)
    out = []
    for row in X:
        m = max(row)
        e = [math.exp(x - m) for x in row]
        s = sum(e)
        out.append([x / s for x in e])
    return out


def _split_heads(X):
    if _np is not None:
        S = X.shape[0]
        return X.reshape(S, HEADS, HEAD_DIM).transpose(1, 0, 2)
    return [[row[i * HEAD_DIM:(i + 1) * HEAD_DIM] for row in X] for i in range(HEADS)]


def _merge_heads(H):
    if _np is not None:
        h, S, d = H.shape
        return H.transpose(1, 0, 2).reshape(S, h * d)
    S = len(H[0])
    return [[H[i][s][d] for i in range(HEADS) for d in range(HEAD_DIM)] for s in range(S)]


# ------------------------------------------------------------------ 前向
class _Net:
    """按节点名把权重映射成"角色"，再手写 BERT 前向。"""

    ROLE_PATTERNS = (
        ("attention/self/query/MatMul", "query"),
        ("attention/self/key/MatMul", "key"),
        ("attention/self/value/MatMul", "value"),
        ("attention/output/dense/MatMul", "attn_out"),
        ("intermediate/dense/MatMul", "inter"),
        ("output/dense/MatMul", "out"),
        ("pooler/dense/Gemm", "pooler"),
        ("/classifier/Gemm", "classifier"),
    )

    def __init__(self, weights, nodes):
        self.w = weights
        self.role: Dict[str, str] = {}
        for op, ins, outs in nodes:
            if op not in ("MatMul", "Gemm") or len(ins) < 2 or not outs:
                continue
            wname = ins[1]
            if wname not in weights:
                continue
            out = outs[0]
            if wname == "classifier.weight":
                self.role["classifier"] = wname
                continue
            if wname == "bert.pooler.dense.weight":
                self.role["pooler"] = wname
                continue
            for pat, role in self.ROLE_PATTERNS:
                if pat in out:
                    layer = ""
                    m = re.search(r"encoder/layer\.(\d+)/", out)
                    if m:
                        layer = m.group(1) + "."
                    self.role[layer + role] = wname
                    break
        self.layers = sorted({k.split(".")[3] for k in weights
                              if k.startswith("bert.encoder.layer.") and k.endswith("attention.self.query.bias")},
                             key=int)

    def W(self, role: str):
        key = self.role[role]
        dims, vals = self.w[key]
        n = dims[-1]
        rows = [vals[i:i + n] for i in range(0, len(vals), n)]
        return _mat(rows, dims)

    def WT(self, role: str):
        """Gemm 的权重是 [out, in]，用的时候要转置。"""
        W = self.W(role)
        if _np is not None:
            return W.T
        return [list(col) for col in zip(*W)]

    def vec(self, key: str):
        v = self.w[key][1]
        return _np.asarray(v, dtype=_np.float32) if _np is not None else v

    def forward(self, ids: List[int], mask: Optional[List[int]] = None) -> List[float]:
        S = len(ids)
        H = HIDDEN
        emb_w = self.w["bert.embeddings.word_embeddings.weight"][1]
        pos_w = self.w["bert.embeddings.position_embeddings.weight"][1]
        typ_w = self.w["bert.embeddings.token_type_embeddings.weight"][1]
        rows = []
        for i, tid in enumerate(ids):
            a = emb_w[tid * H:(tid + 1) * H]
            b = pos_w[i * H:(i + 1) * H]
            c = typ_w[:H]
            rows.append([x + y + z for x, y, z in zip(a, b, c)])
        X = _mat(rows, [S, H])
        X = _layernorm(X, self.vec("bert.embeddings.LayerNorm.weight"), self.vec("bert.embeddings.LayerNorm.bias"))
        for li in self.layers:
            p = "bert.encoder.layer.%s." % li
            q = _add(_mm(X, self.W(li + ".query")), self.vec(p + "attention.self.query.bias"))
            k = _add(_mm(X, self.W(li + ".key")), self.vec(p + "attention.self.key.bias"))
            v = _add(_mm(X, self.W(li + ".value")), self.vec(p + "attention.self.value.bias"))
            qh, kh, vh = _split_heads(q), _split_heads(k), _split_heads(v)
            if _np is not None:
                sc = (qh @ kh.transpose(0, 2, 1)) / math.sqrt(HEAD_DIM)
                if mask is not None:
                    m = _np.asarray(mask, dtype=_np.float32).reshape(1, 1, S)
                    sc = sc + (1.0 - m) * NEG
                ctx = _softmax(sc) @ vh
            else:
                ctx = []
                for hi in range(HEADS):
                    sc = _mm(qh[hi], list(zip(*kh[hi])))
                    sc = [[x / math.sqrt(HEAD_DIM) for x in row] for row in sc]
                    if mask is not None:
                        sc = [[x + (0.0 if mask[j] else NEG) for j, x in enumerate(row)] for row in sc]
                    ctx.append(_mm(_softmax(sc), vh[hi]))
            att = _add(_mm(_merge_heads(ctx), self.W(li + ".attn_out")),
                       self.vec(p + "attention.output.dense.bias"))
            X = _layernorm(_add(X, att), self.vec(p + "attention.output.LayerNorm.weight"),
                           self.vec(p + "attention.output.LayerNorm.bias"))
            inter = _gelu(_add(_mm(X, self.W(li + ".inter")), self.vec(p + "intermediate.dense.bias")))
            out = _add(_mm(inter, self.W(li + ".out")), self.vec(p + "output.dense.bias"))
            X = _layernorm(_add(X, out), self.vec(p + "output.LayerNorm.weight"), self.vec(p + "output.LayerNorm.bias"))
        if _np is not None:
            pooled = _add(X[0].reshape(1, -1), self.vec("bert.pooler.dense.bias"))
            pooled = _np.tanh(_add(_mm(pooled, self.WT("pooler")), self.vec("bert.pooler.dense.bias")))
        else:
            pooled = _add([X[0]], self.vec("bert.pooler.dense.bias"))
            pooled = [[math.tanh(x) for x in row] for row in _add(_mm(pooled, self.WT("pooler")),
                                                                 self.vec("bert.pooler.dense.bias"))]
        logits = _add(_mm(pooled, self.WT("classifier")), self.vec("classifier.bias"))
        probs = _softmax(logits)
        return [float(x) for x in probs[0]]


def _net() -> _Net:
    with _lock:
        if "net" not in _cache:
            w, n = _load_onnx(ONNX_PATH)
            _cache["net"] = _Net(w, n)
            _cache["vocab"] = _load_vocab(VOCAB_PATH)
        return _cache["net"]


def available() -> bool:
    return os.path.exists(ONNX_PATH) and os.path.exists(VOCAB_PATH)


def predict_send(text: str, max_len: int = MAX_LEN) -> Optional[float]:
    """返回"这句话说完了"的概率（0~1）；不可用/出错返回 None。"""
    try:
        net = _net()
        vocab = _cache["vocab"]
        ids = tokenize(text or "", vocab, max_len)
        if len(ids) < 2:
            return None
        probs = net.forward(ids)
        return max(0.0, min(1.0, float(probs[1])))
    except Exception:
        return None
