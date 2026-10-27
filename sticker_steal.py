# -*- coding: utf-8 -*-
"""表情包小偷（按 QQX_AIBot 架构实现，不依赖重型库）

链路：
    群 / 私聊里的图  →  按模式决定收不收（概率 / 冷却 + 黑白名单 + 容量）
                     →  sha256 去重、黑名单过滤
                     →  待审核池（只存原图 + 极简元数据，绝不存 base64）
                     →  人工通过（WebUI）→ 后台识图队列
                     →  独立视觉模型分类（分类 / 情绪 / 描述 / 标签 / 场景 / 图上文字）
                     →  表情包库（浏览 / 搜索 / 排序 / 改分类 / 收藏 / 作用域 / 删除 / 自动淘汰）

数据目录：sticker_data/
    pool/<sid>.<ext>      待审核原图
    library/<sid>.<ext>   入库原图
    pool.json             待审核元数据
    library.json          图库元数据
    meta.json             分类表 / 黑名单 / 运行统计

设计约束（与项目一致）：无重型依赖（Pillow 可选，装了才有动图九帧分镜与近似去重）、
原子写盘 + 线程锁、中文注释、不碰用户其他数据。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io as _io
import json
import zlib
import os
import random
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------- 常量 ----------------
STICKER_DIR = Path("sticker_data")
POOL_DIR = STICKER_DIR / "pool"
LIB_DIR = STICKER_DIR / "library"
POOL_JSON = STICKER_DIR / "pool.json"
LIB_JSON = STICKER_DIR / "library.json"
META_JSON = STICKER_DIR / "meta.json"

DEFAULT_CATEGORIES = ["开心", "可爱", "无语", "生气", "震惊", "难过", "害羞", "嘲讽", "疑问", "其他"]

# 收录分档（勾选式）：marker=QQ 表情标记 / animated=动图 / small=小方图 / all=所有图片
KIND_LABELS = {
    "marker": "QQ 表情标记",
    "animated": "动图",
    "small": "小方图",
    "all": "所有图片",
}
DEFAULT_KINDS = ["marker", "animated"]


def _legacy_kinds(scope: Any) -> List[str]:
    """老配置的 scope 单选 -> 新的 kinds 勾选。"""
    s = str(scope or "").strip()
    if s == "all_images":
        return ["all"]
    if s == "gif_only":
        return ["animated"]
    return list(DEFAULT_KINDS)


def seg_tags(seg: Dict[str, Any]) -> set:
    """按消息段的平台元信息给这张图打标签。"""
    d = seg.get("data") or {}
    tags = set()
    stype = str(seg.get("type") or "")
    name = str(d.get("file") or d.get("url") or "").lower()
    if stype in ("mface", "marketface"):
        tags.add("marker")
    if str(d.get("sub_type") or d.get("subType") or "0") == "1":
        tags.add("marker")
    if d.get("emoji_id") or d.get("emoji_package_id"):
        tags.add("marker")
    sm = str(d.get("summary") or "").lower()
    if sm and ("表情" in sm or "emoji" in sm or "sticker" in sm):
        tags.add("marker")
    if "vip.qq.com/club/item/parcel" in name or "gxh.vip.qq.com" in name:
        tags.add("marker")
    if name.endswith((".gif", ".webp", ".apng", ".mface")):
        tags.add("animated")
    if not tags:
        tags.add("plain")
    return tags


# 图片魔数 → 扩展名（不依赖 Pillow 也能认出格式）
_MAGIC = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
]
_STICKER_EXT = (".gif", ".webp", ".apng", ".mface")

# 可选：装了 Pillow 才有动图九帧分镜 / 近似去重
try:  # pragma: no cover - 取决于运行环境
    from PIL import Image  # type: ignore

    HAS_PIL = True
except Exception:  # pragma: no cover
    Image = None  # type: ignore
    HAS_PIL = False


# ---------------- 【AI 主动发表情包】标记解析 ----------------
# 跟 <pic prompt="..." /> 一个思路：AI 在回复正文里写 <meme ... />，
# 程序把标记抠掉、从「已入库」的表情包里挑一张，跟正文一起发出去。
MEME_RULE = """【想发表情包的时候】
如果你觉得这会儿发张表情包正合适（比如聊到某个情绪、有人让你来个表情包），就在回复里带上一个 <meme emo="开心" /> 标签，系统会从表情包库里挑一张一起发出去。
也可以按关键词找：<meme q="猫 无语" />；两个一起写也行：<meme emo="开心" q="猫" />；什么都不写 <meme /> 就是随便来一张。
库里现有的情绪：{emotions}
库里现有的分类：{cats}
发在正文前面还是后面，默认由系统按当前精力和这张表情包的情绪随机决定（精力越高越偏后面，生气震惊这类负向情绪更偏前面）。你想强制位置就直接写 <meme pos="before" /> 或 <meme pos="after" />。
用不用完全由你自己决定 —— 想发就写，不想发就别写，别为了发表情包而发表情包。
不要解释这个标签，也不要把它当成话题说出来。"""

_RE_MEME = re.compile(r"<\s*meme\b([^>]*?)(?:/\s*>|>([\s\S]*?)<\s*/\s*meme\s*>|>)", re.I)


def _parse_attrs(raw: str) -> Dict[str, Any]:
    """把 emo="开心" q='猫' n=2 这类属性抠成字典。"""
    out: Dict[str, Any] = {}
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+)", str(raw or "")):
        out[m.group(1).lower()] = m.group(2).strip("\"'").strip()
    return out


def extract_meme_jobs(text: str, max_count: int = 1) -> Tuple[str, List[Dict[str, Any]]]:
    """从回复正文里抠出 <meme ... /> 标记。
    返回 (抠掉标记的干净文本, 条件列表)。不管条件认不认得出来，标记都会被抠掉 —— 不能漏到聊天里。"""
    s = str(text or "")
    try:
        cap = max(0, int(max_count if max_count is not None else 1))
    except Exception:
        cap = 1
    jobs: List[Dict[str, Any]] = []
    keep: List[str] = []
    last = 0
    for m in _RE_MEME.finditer(s):
        keep.append(s[last:m.start()])
        last = m.end()
        if len(jobs) >= cap:
            continue
        attrs = _parse_attrs(m.group(1) or "")
        inner = str(m.group(2) or "").strip()
        if inner and not attrs.get("q"):
            attrs["q"] = inner
        # 位置：AI 显式写 pos=before/after 就听它的；否则看标签落在正文的什么位置
        _p = str(attrs.get("pos") or "").strip().lower()
        if _p in ("before", "front", "pre", "top", "前", "前面"):
            attrs["pos"] = "before"
            attrs["pos_explicit"] = True
        elif _p in ("after", "back", "post", "tail", "后", "后面"):
            attrs["pos"] = "after"
            attrs["pos_explicit"] = True
        else:
            # 没显式写 pos：先按标签落点给个默认，真正的时机交给 decide_pos 掷
            attrs["pos"] = "before" if not s[:m.start()].strip() else "after"
            attrs["pos_explicit"] = False
        jobs.append(attrs)
    if not keep:
        return s.strip(), jobs
    keep.append(s[last:])
    clean = "".join(keep)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip(), jobs


# ---------------- 【发表情包的时机】精力 + 情绪极性 -> 权重随机 ----------------
# 默认正向情绪（后置概率往上加）与负向情绪（后置概率往下压，也就是更爱发在正文前）。
DEFAULT_POS_EMOS = ("开心,高兴,快乐,可爱,害羞,喜欢,爱心,亲亲,比心,抱抱,笑,得意,期待,甜,惊喜,卖萌,撒娇")
DEFAULT_NEG_EMOS = ("生气,愤怒,震惊,无语,难过,哭,嘲讽,鄙视,嫌弃,害怕,惊恐,崩溃,抓狂,委屈,冷漠,翻白眼,尴尬")


def _emo_words(raw: Any, default: str) -> List[str]:
    src = str(raw or "").strip() or default
    return [w for w in re.split(r"[\s,，、/|]+", src.lower()) if w]


def polarity_of(emotion: Any, cfg: Optional[Dict[str, Any]] = None) -> int:
    """情绪词 -> 极性：+1 正向 / -1 负向 / 0 中性（两边都命中算中性）。"""
    e = str(emotion or "").strip().lower()
    if not e:
        return 0
    c = cfg or {}
    pos = any(w in e for w in _emo_words(c.get("sticker_pos_pos_emos"), DEFAULT_POS_EMOS))
    neg = any(w in e for w in _emo_words(c.get("sticker_pos_neg_emos"), DEFAULT_NEG_EMOS))
    if pos and not neg:
        return 1
    if neg and not pos:
        return -1
    return 0


def pos_prob_after(emotion: Any, energy: Any = 60,
                   cfg: Optional[Dict[str, Any]] = None) -> float:
    """发在正文【后面】的概率（0~1）。

    精力越高越偏后置（精力 100 加满、0 减满）；正向情绪往上加、负向往下压，
    所以负向表情包更倾向发在正文前。
    """
    c = cfg or {}

    def _f(k: str, d: float) -> float:
        try:
            return float(c.get(k, d))
        except Exception:
            return float(d)

    base = _f("sticker_pos_base", 50) / 100.0
    eg = _f("sticker_pos_energy", 35) / 100.0
    pg = _f("sticker_pos_polar", 30) / 100.0
    try:
        en = max(0.0, min(100.0, float(energy)))
    except Exception:
        en = 60.0
    p = base + eg * (en - 50.0) / 50.0 + pg * polarity_of(emotion, c)
    return max(0.02, min(0.98, p))


def decide_pos(emotion: Any, energy: Any = 60,
               cfg: Optional[Dict[str, Any]] = None) -> str:
    """按权重掷一次，返回 "before" / "after"。"""
    return "after" if random.random() < pos_prob_after(emotion, energy, cfg) else "before"


def sniff_ext(data: bytes) -> str:
    """按魔数判断图片格式，认不出来给 bin。"""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    head = data[:16]
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "bin"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_sid() -> str:
    """形如 20261016_123001_a1b2c3，天然按时间排序。"""
    return time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)


def dhash_of(data: bytes) -> str:
    """近似指纹（dHash 64bit）；没装 Pillow 就返回空串。"""
    if not HAS_PIL:
        return ""
    try:
        im = Image.open(_io.BytesIO(data)).convert("L").resize((9, 8))
        px = list(im.getdata())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
        return "%016x" % bits
    except Exception:
        return ""


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 99
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# ---------------- 纯 Python 图片工具（不依赖 Pillow） ----------------
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (len(data).to_bytes(4, "big") + tag + data
            + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big"))


def _png_from_rgb(w: int, h: int, rgb: bytes) -> bytes:
    """RGB 原始像素 -> PNG 字节（纯 zlib）。"""
    raw = bytearray()
    stride = w * 3
    for y in range(h):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0)))
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _png_chunk(b"IEND", b""))


def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """从文件头读宽高（PNG/JPEG/GIF/BMP/WEBP），读不出来返回 None。"""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return (int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"))
        if data[:2] == b"BM":
            return (abs(int.from_bytes(data[18:22], "little", signed=True)),
                    abs(int.from_bytes(data[22:26], "little", signed=True)))
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fmt = data[12:16]
            if fmt == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            if fmt == b"VP8 ":
                k = data.find(b"\x9d\x01\x2a")
                if k > 0:
                    return (int.from_bytes(data[k + 3:k + 5], "little") & 0x3FFF,
                            int.from_bytes(data[k + 5:k + 7], "little") & 0x3FFF)
            if fmt == b"VP8L" and len(data) > 25:
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        if data[:2] == b"\xff\xd8":
            k, n = 2, len(data)
            while k + 9 < n:
                if data[k] != 0xFF:
                    k += 1
                    continue
                mk = data[k + 1]
                if mk in (0xD8, 0x01) or 0xD0 <= mk <= 0xD7:
                    k += 2
                    continue
                seg = int.from_bytes(data[k + 2:k + 4], "big")
                if mk in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return (int.from_bytes(data[k + 7:k + 9], "big"),
                            int.from_bytes(data[k + 5:k + 7], "big"))
                if seg <= 0:
                    break
                k += 2 + seg
    except Exception:
        pass
    return None


def _gif_lzw(min_code: int, buf: bytes, need: int) -> bytearray:
    """GIF 的 LZW 解码（纯 Python）。"""
    clear = 1 << min_code
    end = clear + 1
    size = min_code + 1
    table = [bytes((i,)) for i in range(clear)] + [b"", b""]
    out = bytearray()
    prev = None
    bitbuf = 0
    bitcnt = 0
    for byte in buf:
        bitbuf |= byte << bitcnt
        bitcnt += 8
        while bitcnt >= size:
            code = bitbuf & ((1 << size) - 1)
            bitbuf >>= size
            bitcnt -= size
            if code == clear:
                table = [bytes((i,)) for i in range(clear)] + [b"", b""]
                size = min_code + 1
                prev = None
                continue
            if code == end:
                return out
            if prev is None:
                if code >= len(table):
                    return out
                entry = table[code]
            elif code < len(table):
                entry = table[code]
                table.append(prev + entry[:1])
            else:
                entry = prev + prev[:1]
                table.append(entry)
            out += entry
            prev = entry
            if len(table) >= (1 << size) and size < 12:
                size += 1
            if len(out) >= need:
                return out
    return out


def gif_frames(data: bytes, limit: int = 9) -> List[Tuple[int, int, bytes]]:
    """纯 Python 解 GIF：返回 [(宽, 高, RGB 字节)]，最多 limit 帧，已处理透明/交错/disposal。"""
    if len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return []
    sw = int.from_bytes(data[6:8], "little")
    sh = int.from_bytes(data[8:10], "little")
    if not (0 < sw <= 4096 and 0 < sh <= 4096):
        return []
    flags = data[10]
    pos = 13
    gct = b""
    if flags & 0x80:
        n = 2 << (flags & 7)
        gct = data[pos:pos + n * 3]
        pos += n * 3
    bgc = (0, 0, 0)
    if gct and (data[11] * 3 + 3) <= len(gct):
        b0 = data[11] * 3
        bgc = (gct[b0], gct[b0 + 1], gct[b0 + 2])
    canvas = bytearray(bytes(bgc) * (sw * sh))
    out: List[Tuple[int, int, bytes]] = []
    trans = -1
    disposal = 0
    pending = None
    while pos < len(data) and len(out) < limit:
        b = data[pos]
        if b == 0x3B:
            break
        if b == 0x21:
            if pos + 2 >= len(data):
                break
            label = data[pos + 1]
            if label == 0xF9 and pos + 7 < len(data):
                packed = data[pos + 3]
                disposal = (packed >> 2) & 7
                trans = data[pos + 6] if packed & 1 else -1
                pos += 8
                continue
            p = pos + 2
            while p < len(data) and data[p]:
                p += 1 + data[p]
            pos = p + 1
            continue
        if b != 0x2C:
            pos += 1
            continue
        if pos + 10 > len(data):
            break
        ix = int.from_bytes(data[pos + 1:pos + 3], "little")
        iy = int.from_bytes(data[pos + 3:pos + 5], "little")
        iw = int.from_bytes(data[pos + 5:pos + 7], "little")
        ih = int.from_bytes(data[pos + 7:pos + 9], "little")
        ipk = data[pos + 9]
        p = pos + 10
        pal = gct
        if ipk & 0x80:
            n = 2 << (ipk & 7)
            pal = data[p:p + n * 3]
            p += n * 3
        interlaced = bool(ipk & 0x40)
        if p >= len(data):
            break
        min_code = data[p]
        p += 1
        chunks = []
        while p < len(data) and data[p]:
            n = data[p]
            chunks.append(data[p + 1:p + 1 + n])
            p += 1 + n
        pos = p + 1
        if iw <= 0 or ih <= 0 or not pal:
            continue
        idx = _gif_lzw(min_code, b"".join(chunks), iw * ih)
        if len(idx) < iw * ih:
            idx = bytes(idx) + bytes(iw * ih - len(idx))
        if pending:
            d_prev, rect, saved = pending
            if d_prev == 2:
                x0, y0, w0, h0 = rect
                for y in range(y0, min(y0 + h0, sh)):
                    base = (y * sw + x0) * 3
                    canvas[base:base + w0 * 3] = bytes(bgc) * w0
            elif d_prev == 3 and saved is not None:
                canvas = bytearray(saved)
            pending = None
        saved_before = bytes(canvas) if disposal == 3 else None
        rows = list(range(ih))
        if interlaced:
            rows = [r for st, sp in ((0, 8), (4, 8), (2, 4), (1, 2)) for r in range(st, ih, sp)]
        for ry in rows:
            ty = iy + ry
            if ty >= sh:
                continue
            rb = ry * iw
            tb = ty * sw
            for xx in range(iw):
                tx = ix + xx
                if tx >= sw:
                    continue
                ci = idx[rb + xx]
                if ci == trans:
                    continue
                o = ci * 3
                if o + 3 > len(pal):
                    continue
                d = (tb + tx) * 3
                canvas[d] = pal[o]
                canvas[d + 1] = pal[o + 1]
                canvas[d + 2] = pal[o + 2]
        out.append((sw, sh, bytes(canvas)))
        pending = (disposal, (ix, iy, iw, ih), saved_before)
    return out


def _fit_rgb(w: int, h: int, rgb: bytes, box: int) -> Tuple[int, int, bytes]:
    """最近邻缩放，最长边不超过 box。"""
    if w <= box and h <= box:
        return w, h, rgb
    k = box / float(max(w, h))
    nw = max(1, int(w * k))
    nh = max(1, int(h * k))
    out = bytearray(nw * nh * 3)
    for y in range(nh):
        sy = min(h - 1, int(y / k))
        base = sy * w * 3
        for x in range(nw):
            sx = min(w - 1, int(x / k))
            i = base + sx * 3
            j = (y * nw + x) * 3
            out[j] = rgb[i]
            out[j + 1] = rgb[i + 1]
            out[j + 2] = rgb[i + 2]
    return nw, nh, bytes(out)


def gif_frames_png(data: bytes, frames: int = 9, cell: int = 200) -> Optional[bytes]:
    """GIF -> PNG：多帧拼 3×3 分镜，单帧就直接输出那一帧。纯 Python，不需要 Pillow。"""
    try:
        fr = gif_frames(data, frames)
        if not fr:
            return None
        if len(fr) == 1:
            w, h, rgb = _fit_rgb(fr[0][0], fr[0][1], fr[0][2], 512)
            return _png_from_rgb(w, h, rgb)
        side = cell * 3
        canvas = bytearray(bytes((18, 18, 18)) * (side * side))
        for i, (w, h, rgb) in enumerate(fr[:9]):
            nw, nh, small = _fit_rgb(w, h, rgb, cell)
            ox = (i % 3) * cell + (cell - nw) // 2
            oy = (i // 3) * cell + (cell - nh) // 2
            for y in range(nh):
                dst = ((oy + y) * side + ox) * 3
                canvas[dst:dst + nw * 3] = small[y * nw * 3:(y + 1) * nw * 3]
        return _png_from_rgb(side, side, bytes(canvas))
    except Exception:
        return None


def prep_for_model(data: bytes, ext: str) -> Tuple[bytes, str, str]:
    """把图转成识图模型能吃的格式。返回 (数据, mime, 说明)。"""
    ext = (ext or "").lower()
    if ext == "gif":
        png = gif_frames_png(data)
        if png:
            return png, "image/png", "gif->png"
        return data, "image/gif", ""
    if ext in ("webp", "apng", "bmp"):
        if HAS_PIL:
            try:
                im = Image.open(_io.BytesIO(data)).convert("RGB")
                o = _io.BytesIO()
                im.save(o, format="PNG")
                return o.getvalue(), "image/png", "pil->png"
            except Exception:
                pass
        if ext == "bmp":
            return data, "image/bmp", ""
        return data, "image/" + ext, ""
    if ext in ("jpg", "jpeg"):
        return data, "image/jpeg", ""
    if ext == "png":
        return data, "image/png", ""
    return data, "image/" + (ext or "bin"), ""


def _is_small_square(raw: bytes, max_side: int = 512, tol: float = 0.25,
                     max_bytes: int = 1536 * 1024) -> bool:
    """小方图启发式：最长边 <=512 且接近正方形、体积 <=1.5MB。"""
    if not raw or len(raw) > max_bytes:
        return False
    wh = image_size(raw)
    if not wh:
        return False
    w, h = wh
    if w <= 0 or h <= 0 or max(w, h) > max_side:
        return False
    return abs(w - h) <= max(16, tol * max(w, h))


def gif_montage(data: bytes, frames: int = 9) -> Optional[bytes]:
    """动图等距抽 9 帧拼 3×3 分镜（PNG）。装了 Pillow 才有。"""
    if not HAS_PIL:
        return None
    try:
        im = Image.open(_io.BytesIO(data))
        n = int(getattr(im, "n_frames", 1) or 1)
        if n <= 1:
            return None
        picks = [round(i * (n - 1) / (frames - 1)) for i in range(frames)] if n > 1 else [0]
        shots = []
        for idx in picks:
            im.seek(min(idx, n - 1))
            shots.append(im.convert("RGB").copy())
        w, h = shots[0].size
        cell = 240
        canvas = Image.new("RGB", (cell * 3, cell * 3), (20, 20, 20))
        for i, sh in enumerate(shots[:9]):
            sh.thumbnail((cell, cell))
            x = (i % 3) * cell + (cell - sh.size[0]) // 2
            y = (i // 3) * cell + (cell - sh.size[1]) // 2
            canvas.paste(sh, (x, y))
        out = _io.BytesIO()
        canvas.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return None


# ---------------- 存储 ----------------
class _Store:
    """pool / library / meta 三份 JSON 的读写，原子写盘 + 线程锁。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        STICKER_DIR.mkdir(parents=True, exist_ok=True)
        POOL_DIR.mkdir(parents=True, exist_ok=True)
        LIB_DIR.mkdir(parents=True, exist_ok=True)
        self.pool: Dict[str, Dict[str, Any]] = self._load(POOL_JSON, {})
        self.lib: Dict[str, Dict[str, Any]] = self._load(LIB_JSON, {})
        self.meta: Dict[str, Any] = self._load(META_JSON, {})
        if not self.meta.get("categories"):
            self.meta["categories"] = list(DEFAULT_CATEGORIES)
        if not isinstance(self.meta.get("blacklist"), list):
            self.meta["blacklist"] = []
        if not isinstance(self.meta.get("stat"), dict):
            self.meta["stat"] = {}
        self._save(META_JSON, self.meta)

    @staticmethod
    def _load(p: Path, default: Any) -> Any:
        try:
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8") or "null") or default
        except Exception:
            pass
        return default

    @staticmethod
    def _save(p: Path, obj: Any) -> None:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def flush_pool(self) -> None:
        with self.lock:
            self._save(POOL_JSON, self.pool)

    def flush_lib(self) -> None:
        with self.lock:
            self._save(LIB_JSON, self.lib)

    def flush_meta(self) -> None:
        with self.lock:
            self._save(META_JSON, self.meta)

    def categories(self) -> List[str]:
        with self.lock:
            cats = [str(c) for c in (self.meta.get("categories") or []) if str(c).strip()]
        return cats or list(DEFAULT_CATEGORIES)

    def known_hashes(self) -> set:
        with self.lock:
            out = {str(v.get("sha256")) for v in self.pool.values() if v.get("sha256")}
            out |= {str(v.get("sha256")) for v in self.lib.values() if v.get("sha256")}
        return out

    def blacklist(self) -> set:
        with self.lock:
            return {str(x) for x in (self.meta.get("blacklist") or [])}


# ---------------- 视觉模型 ----------------
class StickerVision:
    """独立的表情包识图模型（OpenAI 兼容 /chat/completions，走 base64 多模态）。"""

    BUILTIN_PROMPT = (
        "你在给一个 QQ 表情包图库做自动分类。仔细看这张图"
        "（如果是九宫格分镜，那是动图按时间顺序抽的九帧，从左到右、从上到下读），"
        "只输出一个 JSON 对象，不要解释、不要 markdown 代码块。\n"
        "字段：\n"
        '  "category"：从给定分类里选最贴切的一个，必须原样照抄分类名\n'
        '  "emotion"：这张图传达的情绪，2~6 个字\n'
        '  "desc"：一句话描述画面（谁在做什么、什么表情）\n'
        '  "tags"：3~6 个关键词，数组\n'
        '  "scenes"：2~4 条适合发这张图的场景，数组\n'
        '  "ocr"：图上出现的文字，没有就给空字符串\n'
        "分类只能从这个列表里选：{categories}\n"
        '示例：{"category":"开心","emotion":"开心","desc":"一只柴犬在笑",'
        '"tags":["柴犬","笑","开心"],"scenes":["被夸的时候","心情好"],"ocr":""}'
    )

    def __init__(self, cfg: Callable[[], Dict[str, Any]], log: Optional[Callable] = None) -> None:
        self._cfg = cfg
        self._log = log

    def endpoint(self) -> str:
        base = str(self._cfg().get("sticker_vision_base_url") or "").strip().rstrip("/")
        if not base:
            return ""
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def prompt(self, categories: Optional[List[str]] = None) -> str:
        cats = [str(c) for c in (categories or []) if str(c).strip()] or list(DEFAULT_CATEGORIES)
        tpl = str(self._cfg().get("sticker_vision_prompt") or "").strip() or self.BUILTIN_PROMPT
        if "{categories}" in tpl:
            return tpl.replace("{categories}", " / ".join(cats))
        return tpl + "\n分类只能从这个列表里选：" + " / ".join(cats)

    @staticmethod
    def parse_json(txt: str) -> Optional[Dict[str, Any]]:
        """从模型回复里抠出 JSON（容忍代码块、前后废话、单引号）。"""
        raw = str(txt or "").strip()
        if not raw:
            return None
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
        for cand in (raw,):
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            frag = m.group(0)
            for cand in (frag, frag.replace("'", '"')):
                try:
                    obj = json.loads(cand)
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    continue
        return None

    def normalize(self, obj: Dict[str, Any], raw_reply: str = "",
                  categories: Optional[List[str]] = None) -> Dict[str, Any]:
        cats = [str(c) for c in (categories or []) if str(c).strip()] or list(DEFAULT_CATEGORIES)

        def _list(v, n):
            if isinstance(v, str):
                v = [x for x in re.split(r"[，,、;；\s]+", v) if x]
            if not isinstance(v, list):
                return []
            return [str(x).strip() for x in v if str(x).strip()][:n]

        cat = str(obj.get("category") or "").strip()
        if cat not in cats:
            for c in cats:
                if c and c in cat:
                    cat = c
                    break
            else:
                cat = "其他" if "其他" in cats else cats[0]
        return {
            "category": cat,
            "emotion": str(obj.get("emotion") or "").strip()[:20],
            "desc": str(obj.get("desc") or obj.get("description") or "").strip()[:300],
            "tags": _list(obj.get("tags"), 8),
            "scenes": _list(obj.get("scenes"), 6),
            "ocr": str(obj.get("ocr") or obj.get("text") or "").strip()[:300],
            "raw_reply": str(raw_reply or "")[:600],
            "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    async def classify(self, path: Path,
                       categories: Optional[List[str]] = None) -> Dict[str, Any]:
        """把一张图交给识图模型，返回归一化后的分类结果。失败抛异常。"""
        import aiohttp

        cfg = self._cfg()
        ep = self.endpoint()
        if not ep:
            raise RuntimeError("还没配置表情包识图模型的 base_url（在「API 配置」页签里填）")
        model = str(cfg.get("sticker_vision_model") or "").strip()
        if not model:
            raise RuntimeError("还没配置表情包识图模型的 model 名")
        key = str(cfg.get("sticker_vision_key") or "").strip()
        timeout = float(cfg.get("sticker_vision_timeout", 600) or 600)
        # 思维链额度：qwen3-vl 这类会先输出一大段思考，给少了正文就空了
        try:
            max_tokens = int(cfg.get("sticker_vision_max_tokens", 8192) or 8192)
        except (TypeError, ValueError):
            max_tokens = 8192
        max_tokens = max(512, min(65536, max_tokens))

        data = path.read_bytes()
        ext = path.suffix.lstrip(".").lower() or sniff_ext(data)
        if ext in ("", "bin"):
            raise RuntimeError("这张图认不出格式（不是 PNG/JPEG/GIF/WebP），识图模型看不了")
        payload, mime, _note = prep_for_model(data, ext)
        if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            raise RuntimeError(
                "这张图是 %s 格式，识图模型不支持；装上 Pillow（py -3 -m pip install pillow）"
                "后重启就能自动转 PNG" % ext)
        b64 = base64.b64encode(payload).decode()

        parts: List[Dict[str, Any]] = [{"type": "text", "text": self.prompt(categories)}]
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        body = {
            "model": model,
            "messages": [{"role": "user", "content": parts}],
            "temperature": 0.2,
            "max_tokens": max_tokens,   # 可在 WebUI 调（默认 8192）：给思维链留的额度
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                async with s.post(ep, json=body, headers=headers) as r:
                    txt = await r.text()
                    if r.status != 200:
                        extra = ""
                        if "invalid image input" in txt:
                            extra = "（模型不认这张图的格式，已自动转过一次；把这张图发我看看）"
                        raise RuntimeError(f"识图接口 HTTP {r.status}: {txt[:200]}{extra}")
        except (asyncio.TimeoutError, TimeoutError):
            raise RuntimeError(
                f"识图超时（等了 {int(timeout)} 秒）：本机模型可能正忙，或刚被卸载正在重新加载。"
                f"把「API 配置 → 表情包识图模型 → 单张超时」调大一点，再点重试就行。")
        try:
            _msg = json.loads(txt)["choices"][0]["message"]
        except Exception:
            raise RuntimeError(f"识图接口返回结构不对: {txt[:200]}")
        reply = _msg.get("content")
        if isinstance(reply, list):  # 有些实现把 content 拆成数组
            reply = "".join(str(p.get("text") or "") for p in reply if isinstance(p, dict))
        reply = str(reply or "")
        obj = self.parse_json(reply)
        if obj is None:
            # 思维链模型（qwen3 系）的 JSON 可能落在 reasoning / reasoning_content 里
            alt = str(_msg.get("reasoning") or _msg.get("reasoning_content") or "")
            if alt:
                obj = self.parse_json(alt)
        if obj is None:
            if not reply.strip():
                raise RuntimeError("识图模型没吐内容（思维链把 token 吃光了？换个识图模型或把超时调大）")
            return self.normalize({}, raw_reply=reply, categories=categories)
        return self.normalize(obj, raw_reply=reply, categories=categories)


# ---------------- 主逻辑 ----------------
class StickerStealer:
    """偷图 → 待审核池 → 识图入库 → 图库管理。"""

    def __init__(self, cfg: Callable[[], Dict[str, Any]], log: Optional[Callable] = None,
                 fetch_bytes: Optional[Callable] = None) -> None:
        self._cfg = cfg
        self._log = log
        self._fetch = fetch_bytes
        self.store = _Store()
        self.vision = StickerVision(cfg, log)
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._busy: Optional[str] = None
        self._last_steal = 0.0
        self._pool_full_warned = False
        self._auto_paused = False
        self._recent_sent: List[str] = []   # 最近发过的（sid，新的在前），避免老重复

    # ---------- 小工具 ----------
    def _c(self) -> Dict[str, Any]:
        try:
            return self._cfg() or {}
        except Exception:
            return {}

    async def log(self, level: str, msg: str) -> None:
        if self._log:
            try:
                await self._log(level, msg)
            except Exception:
                pass

    def _path_of(self, item: Dict[str, Any], lib: bool = False) -> Path:
        d = LIB_DIR if lib else POOL_DIR
        return d / f"{item.get('sid')}.{item.get('ext') or 'bin'}"

    def ensure_worker(self) -> None:
        """保证后台识图队列在跑（应用启动 / 首次审核时调用）。"""
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_event_loop().create_task(self.worker_loop())
            except RuntimeError:
                self._task = None
        self._wake.set()

    # ---------- 收录判断 ----------
    @staticmethod
    def is_sticker(seg: Dict[str, Any]) -> bool:
        """有表情标记或本身是动图，就算表情包（保留旧接口）。"""
        return bool(seg_tags(seg) & {"marker", "animated"})


    @staticmethod
    def _allowed(cfg: Dict[str, Any], chat_type: str, chat_id: Any, user_id: Any) -> bool:
        wl = [str(x).strip() for x in (cfg.get("sticker_steal_wl") or []) if str(x).strip()]
        bl = [str(x).strip() for x in (cfg.get("sticker_steal_bl") or []) if str(x).strip()]
        if not wl and not bl:
            return True
        keys = [f"group:{chat_id}", f"user:{user_id}"] if chat_type == "group" else [f"user:{user_id}"]
        in_wl = any(k in wl for k in keys)
        in_bl = any(k in bl for k in keys)
        first = str(cfg.get("sticker_steal_priority") or "whitelist_first")
        if first == "blacklist_first":
            return (not in_bl) and (in_wl or not wl)
        return (in_wl or not wl) and (not in_bl)

    def _kinds(self, cfg: Dict[str, Any]) -> List[str]:
        """当前勾选的分档；空列表 = 什么都不收。"""
        k = cfg.get("sticker_steal_kinds")
        if isinstance(k, str):
            k = [k]
        if isinstance(k, list) and k:
            return [str(x) for x in k if str(x) in KIND_LABELS]
        if isinstance(k, list):
            return []
        return _legacy_kinds(cfg.get("sticker_steal_scope"))

    # ---------------- 【AI 主动发表情包】挑一张 ----------------
    def guide_text(self) -> str:
        """给模型的「发表情包」说明；开关关了就给空串。"""
        cfg = self._c()
        if not cfg.get("sticker_send_enabled", True):
            return ""
        with self.store.lock:
            items = [dict(v) for v in self.store.lib.values()]
            cats = list(self.store.categories())
        emos: List[str] = []
        for v in items:
            e = str(v.get("emotion") or "").strip()
            if e and e not in emos:
                emos.append(e)
        g = str(cfg.get("sticker_send_guide") or "").strip() or MEME_RULE
        return (g.replace("{emotions}", "、".join(emos[:20]) or "（库里还没表情包）")
                 .replace("{cats}", "、".join(cats[:20]) or "（无）"))

    def energy_now(self, cfg: Optional[Dict[str, Any]] = None) -> int:
        """当前精力值（mood 里那个，读不到就按默认 60）。"""
        try:
            import mood as _MO
            return int((_MO.current(cfg) or {}).get("energy") or 60)
        except Exception:
            return 60

    def _send_where(self) -> List[str]:
        cfg = self._c()
        w = cfg.get("sticker_send_where")
        if not isinstance(w, list):
            return ["claude", "private"]
        return [str(x) for x in w if str(x) in ("claude", "private")]

    def pick_for_send(self, jobs: List[Dict[str, Any]], chat_type: str = "",
                      chat_id: Any = "", user_id: Any = "") -> List[str]:
        """按 <meme ... /> 的条件挑表情包，返回本地文件路径列表（顺手记一次使用）。"""
        cfg = self._c()
        if not cfg.get("sticker_send_enabled", True):
            return []
        chan = "claude" if str(chat_type) == "group" else "private"
        if chan not in self._send_where():
            return []
        try:
            cap = max(0, min(5, int(cfg.get("sticker_send_max", 1) or 0)))
        except Exception:
            cap = 1
        if cap <= 0:
            return []
        out: List[str] = []
        for job in list(jobs or [])[:cap]:
            it = self._pick_one(job)
            if not it:
                continue
            try:
                p = self._path_of(it, lib=True)
            except Exception:
                continue
            if p.is_file():
                out.append(str(p))
        return out

    def _pick_one(self, job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """给定条件挑一张：先过滤再打分，分高的赢（带点随机）。"""
        import random
        cfg = self._c()
        job = job or {}
        terms = [t for t in re.split(r"[\s,，、/|]+", str(job.get("q") or "").strip().lower()) if t]
        emo = str(job.get("emo") or job.get("emotion") or "").strip().lower()
        cat = str(job.get("cat") or job.get("category") or "").strip()
        fav_first = bool(cfg.get("sticker_send_fav_first", True))
        with self.store.lock:
            blk = {str(x) for x in (self.store.meta.get("blacklist") or [])}
            items = [dict(v) for v in self.store.lib.values()]
        if not items:
            return None
        recent = list(self._recent_sent)
        scored: List[Tuple[float, str, Dict[str, Any]]] = []
        for v in items:
            if str(v.get("status") or "") != "done":
                continue
            if str(v.get("sha256") or "") in blk:
                continue
            blob = " ".join([str(v.get("desc") or ""), str(v.get("ocr") or ""),
                             str(v.get("emotion") or ""), str(v.get("category") or ""),
                             " ".join(str(x) for x in (v.get("tags") or [])),
                             " ".join(str(x) for x in (v.get("scenes") or []))]).lower()
            s = 0.0
            if terms:
                hit = sum(1 for t in terms if t in blob)
                if hit <= 0:
                    continue
                s += 2.0 * hit
            if emo:
                if emo == str(v.get("emotion") or "").strip().lower():
                    s += 3.0
                elif emo in blob:
                    s += 1.5
                else:
                    continue
            if cat and cat == str(v.get("category") or ""):
                s += 2.0
            if fav_first and v.get("fav"):
                s += 1.5
            try:
                s -= 0.35 * min(int(v.get("use_count") or 0), 6)
            except Exception:
                pass
            s += random.random() * 1.2
            scored.append((s, str(v.get("sid")), v))
        if not scored:
            return None
        # 最近发过的先排除（只要还有别的可选），避免连着发同一张
        _fresh = [x for x in scored if x[1] not in recent]
        _pool = _fresh or scored
        _pool.sort(key=lambda x: -x[0])
        best = _pool[0][2]
        sid = str(best.get("sid"))
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.store.lock:
            cur = self.store.lib.get(sid)
            if cur is not None:
                cur["use_count"] = int(cur.get("use_count") or 0) + 1
                cur["last_used_at"] = now
                best = cur
            st = self.store.meta.setdefault("stat", {})
            st["sent"] = int(st.get("sent") or 0) + 1
            st["sent_at"] = now
        self.store.flush_lib()
        self.store.flush_meta()
        self._recent_sent.insert(0, sid)
        del self._recent_sent[12:]
        return best

    def _capacity_left(self) -> int:
        cap = int(self._c().get("sticker_pool_capacity", 200) or 200)
        with self.store.lock:
            n = len(self.store.pool)
        return max(0, cap - n)

    # ---------- 收录 ----------
    async def offer(self, segs: List[Dict[str, Any]], *, chat_type: str = "group",
                    chat_id: Any = 0, user_id: Any = 0, sender_name: str = "") -> int:
        """把消息里的图/表情包丢进待审核池。返回收了几张。"""
        cfg = self._c()
        if not cfg.get("sticker_steal_enabled"):
            return 0
        if not segs or not self._fetch:
            return 0
        if not self._allowed(cfg, chat_type, chat_id, user_id):
            return 0
        left = self._capacity_left()
        if left <= 0:
            if not self._pool_full_warned:
                self._pool_full_warned = True
                await self.log("WARN", f"[偷图] 待审核池已满（上限 {cfg.get('sticker_pool_capacity')}），"
                                       f"先去「表情包」页签处理掉一些")
            return 0
        self._pool_full_warned = False

        # 概率 / 冷却门控（对整条消息生效一次）
        mode = str(cfg.get("sticker_steal_mode") or "probability")
        if mode == "cooldown":
            cd = float(cfg.get("sticker_steal_cooldown", 30) or 0)
            if cd > 0 and (time.time() - self._last_steal) < cd:
                return 0
        else:
            chance = float(cfg.get("sticker_steal_chance", 0.3) or 0)
            if chance <= 0 or random.random() > chance:
                return 0

        kinds = self._kinds(cfg)
        kset = set(kinds)
        max_mb = float(cfg.get("sticker_max_mb", 8) or 8)
        known = self.store.known_hashes()
        blk = self.store.blacklist()
        audit = bool(cfg.get("sticker_steal_audit", True))
        got = 0
        for seg in segs:
            if got >= left:
                break
            d = seg.get("data") or {}
            tags = seg_tags(seg)
            kind = "sticker" if (tags & {"marker", "animated"}) else "image"
            need_size_check = False
            if "all" not in kset:
                if tags & kset:
                    pass
                elif "small" in kset and tags == {"plain"}:
                    need_size_check = True
                else:
                    continue
            try:
                raw = await self._fetch(d)
            except Exception as e:
                await self.log("WARN", f"[偷图] 取图失败: {e}")
                continue
            if not raw:
                continue
            if need_size_check and not _is_small_square(raw):
                continue
            if len(raw) > max_mb * 1024 * 1024:
                await self.log("WARN", f"[偷图] 跳过一张 {len(raw) / 1048576:.1f}MB 的图（上限 {max_mb}MB）")
                continue
            sha = sha256_of(raw)
            if sha in blk:
                continue
            if sha in known:
                continue
            # 近似去重（需要 Pillow）
            dh = dhash_of(raw)
            if dh:
                with self.store.lock:
                    for v in list(self.store.pool.values()) + list(self.store.lib.values()):
                        if v.get("dhash") and hamming(dh, str(v["dhash"])) <= 4:
                            sha = None
                            break
                if sha is None:
                    continue
            ext = sniff_ext(raw)
            sid = new_sid()
            item = {
                "sid": sid,
                "sha256": sha,
                "dhash": dh,
                "ext": ext,
                "size": len(raw),
                "kind": kind,
                "kinds": sorted(tags),
                "chat_type": chat_type,
                "chat_id": str(chat_id),
                "user_id": str(user_id),
                "sender_name": str(sender_name or "")[:40],
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "pending",
                "source": "chat",
            }
            try:
                self._path_of(item).write_bytes(raw)
            except Exception as e:
                await self.log("WARN", f"[偷图] 原图落地失败: {e}")
                continue
            with self.store.lock:
                self.store.pool[sid] = item
            known.add(sha)
            got += 1
            self._last_steal = time.time()
        if got:
            self.store.flush_pool()
            await self.log("INFO", f"[偷图] 收到 {got} 张，已进待审核池（{chat_type} {chat_id}）")
            if not audit:
                await self.approve([i for i, v in self.store.pool.items()
                                    if v.get("status") == "pending" and v.get("source") == "chat"])
        return got

    # ---------- 审核 ----------
    async def approve(self, sids: List[str]) -> Dict[str, Any]:
        n = 0
        with self.store.lock:
            for sid in sids:
                it = self.store.pool.get(str(sid))
                if not it:
                    continue
                it["status"] = "queued"
                it.pop("error", None)
                n += 1
        self.store.flush_pool()
        if n:
            self.ensure_worker()
            await self.log("INFO", f"[偷图] {n} 张通过审核，进入识图队列")
        return {"ok": True, "count": n}

    async def reject(self, sids: List[str]) -> Dict[str, Any]:
        n = 0
        with self.store.lock:
            for sid in sids:
                it = self.store.pool.pop(str(sid), None)
                if not it:
                    continue
                try:
                    self._path_of(it).unlink(missing_ok=True)
                except Exception:
                    pass
                n += 1
        self.store.flush_pool()
        if n:
            await self.log("INFO", f"[偷图] 删掉 {n} 张待审核图片")
        return {"ok": True, "count": n}

    async def retry(self, sids: List[str]) -> Dict[str, Any]:
        n = 0
        with self.store.lock:
            for sid in sids:
                it = self.store.pool.get(str(sid))
                if it and it.get("status") == "failed":
                    it["status"] = "queued"
                    it.pop("error", None)
                    n += 1
        self.store.flush_pool()
        if n:
            self.ensure_worker()
        return {"ok": True, "count": n}

    # ---------- 识图队列 ----------
    async def worker_loop(self) -> None:
        await self.log("INFO", "[偷图] 识图队列已启动")
        while True:
            try:
                item = None
                with self.store.lock:
                    for v in sorted(self.store.pool.values(), key=lambda x: str(x.get("created_at") or "")):
                        if v.get("status") == "queued":
                            item = v
                            break
                if item is None:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                sid = str(item.get("sid"))
                self._busy = sid
                with self.store.lock:
                    item["status"] = "analyzing"
                self.store.flush_pool()
                p = self._path_of(item)
                try:
                    res = await self.vision.classify(p, self.store.categories())
                    self._ingest(item, res)
                    await self.log("SUCCESS", f"[偷图] 识图入库：{res.get('category')} · "
                                              f"{(res.get('desc') or '')[:24]}")
                except Exception as e:
                    with self.store.lock:
                        cur = self.store.pool.get(sid)
                        if cur is not None:
                            cur["status"] = "failed"
                            cur["error"] = str(e)[:300]
                    self.store.flush_pool()
                    await self.log("WARN", f"[偷图] 识图失败（原图保留，可点通过重试）：{str(e)[:160]}")
                finally:
                    self._busy = None
                itv = float(self._c().get("sticker_vision_interval", 5) or 0)
                if itv > 0:
                    await asyncio.sleep(itv)
            except asyncio.CancelledError:
                await self.log("INFO", "[偷图] 识图队列已停止")
                return
            except Exception as e:  # 兜底：队列不能因为单条异常整体挂掉
                await self.log("ERROR", f"[偷图] 队列异常: {e}")
                await asyncio.sleep(3)

    def _ingest(self, item: Dict[str, Any], res: Dict[str, Any]) -> None:
        """识图成功：原图从 pool 挪到 library，写元数据。"""
        sid = str(item.get("sid"))
        src = self._path_of(item)
        dst = self._path_of(item, lib=True)
        try:
            if src.is_file():
                if dst.exists():
                    dst.unlink()
                os.replace(src, dst)
        except Exception:
            pass
        rec = dict(item)
        rec.update(res)
        rec["status"] = "done"
        rec["scope"] = "public"
        rec["fav"] = False
        rec["use_count"] = 0
        rec["created_at"] = item.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S")
        rec["ingested_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.store.lock:
            self.store.pool.pop(sid, None)
            self.store.lib[sid] = rec
        self.store.flush_pool()
        self.store.flush_lib()
        self._enforce_capacity()

    def _enforce_capacity(self) -> None:
        """通用库超上限时按「用得少 + 入库早」淘汰（收藏的不动）。"""
        try:
            limit = int(self._c().get("sticker_max_num", 100) or 0)
        except Exception:
            limit = 0
        if limit <= 0:
            return
        with self.store.lock:
            cands = [v for v in self.store.lib.values()
                     if not v.get("fav") and not v.get("pinned") and v.get("source") == "chat"]
            if len(cands) <= limit:
                return
            w = float(self._c().get("sticker_evict_usage_weight", 0.7) or 0.7)
            uses = [int(v.get("use_count") or 0) for v in cands]
            order = sorted(str(v.get("ingested_at") or v.get("created_at") or "") for v in cands)
            umin, umax = min(uses), max(uses)

            def _age01(t: str) -> float:
                """0 = 最新，1 = 最旧（时间串同为 YYYY-mm-dd HH:MM:SS，可直接比大小）"""
                if len(order) <= 1 or order[-1] == order[0]:
                    return 0.0
                rank = sum(1 for x in order if x <= t) - 1
                return rank / max(1, len(order) - 1)

            scored = []
            for v in cands:
                u = int(v.get("use_count") or 0)
                t = str(v.get("ingested_at") or v.get("created_at") or "")
                su = 0.0 if umax == umin else 1.0 - (u - umin) / (umax - umin)  # 用得越少越接近 1
                scored.append((w * su + (1 - w) * _age01(t), v))
            scored.sort(key=lambda x: (-x[0], int(x[1].get("use_count") or 0),
                                       str(x[1].get("ingested_at") or "")))
            over = len(cands) - limit
            for _, v in scored[:over]:
                self._remove_lib_locked(str(v.get("sid")))
        self.store.flush_lib()

    def _remove_lib_locked(self, sid: str) -> None:
        it = self.store.lib.pop(sid, None)
        if not it:
            return
        try:
            self._path_of(it, lib=True).unlink(missing_ok=True)
        except Exception:
            pass

    # ---------- 图库管理 ----------
    def library_list(self, *, category: str = "", q: str = "", sort: str = "new",
                     scope: str = "", fav: bool = False, page: int = 1, size: int = 40) -> Dict[str, Any]:
        with self.store.lock:
            items = list(self.store.lib.values())
        if category:
            items = [v for v in items if str(v.get("category")) == category]
        if scope:
            items = [v for v in items if str(v.get("scope") or "public") == scope]
        if fav:
            items = [v for v in items if v.get("fav")]
        if q:
            kw = str(q).lower()
            def hit(v):
                blob = " ".join([str(v.get("desc") or ""), str(v.get("ocr") or ""),
                                 str(v.get("emotion") or ""), " ".join(v.get("tags") or []),
                                 " ".join(v.get("scenes") or []), str(v.get("category") or "")]).lower()
                return kw in blob
            items = [v for v in items if hit(v)]
        if sort == "old":
            items.sort(key=lambda v: str(v.get("ingested_at") or ""))
        elif sort == "use":
            items.sort(key=lambda v: int(v.get("use_count") or 0))
        elif sort == "use_desc":
            items.sort(key=lambda v: -int(v.get("use_count") or 0))
        else:
            items.sort(key=lambda v: str(v.get("ingested_at") or ""), reverse=True)
        total = len(items)
        page = max(1, int(page or 1))
        size = max(1, min(200, int(size or 40)))
        items = items[(page - 1) * size: page * size]
        return {"ok": True, "total": total, "page": page, "size": size, "items": items}

    def get_item(self, sid: str) -> Optional[Dict[str, Any]]:
        with self.store.lock:
            return self.store.lib.get(str(sid)) or self.store.pool.get(str(sid))

    def item_path(self, sid: str) -> Optional[Path]:
        with self.store.lock:
            if str(sid) in self.store.lib:
                return self._path_of(self.store.lib[str(sid)], lib=True)
            if str(sid) in self.store.pool:
                return self._path_of(self.store.pool[str(sid)])
        return None

    def library_action(self, sids: List[str], action: str, **kw) -> Dict[str, Any]:
        n = 0
        with self.store.lock:
            for sid in sids:
                it = self.store.lib.get(str(sid))
                if not it:
                    continue
                if action == "delete":
                    self._remove_lib_locked(str(sid))
                elif action == "blacklist":
                    if it.get("sha256"):
                        bl = self.store.meta.setdefault("blacklist", [])
                        if it["sha256"] not in bl:
                            bl.append(it["sha256"])
                    self._remove_lib_locked(str(sid))
                elif action == "fav":
                    it["fav"] = bool(kw.get("value", True))
                elif action == "scope":
                    it["scope"] = "local" if str(kw.get("value")) == "local" else "public"
                elif action == "category":
                    it["category"] = str(kw.get("value") or "其他")
                elif action == "update":
                    for k in ("category", "emotion", "desc", "scope"):
                        if k in kw and kw[k] is not None:
                            it[k] = str(kw[k])[:300]
                    for k in ("tags", "scenes"):
                        if isinstance(kw.get(k), list):
                            it[k] = [str(x)[:40] for x in kw[k]][:8]
                elif action == "bump":
                    it["use_count"] = int(it.get("use_count") or 0) + 1
                n += 1
        self.store.flush_lib()
        self.store.flush_meta()
        return {"ok": True, "count": n}

    def apply_analysis(self, sid: str, res: Dict[str, Any]) -> Dict[str, Any]:
        """把重新识图的结果直接写回库里那一条（点「重新识图」用，免得叉掉就丢）。"""
        if not isinstance(res, dict) or not res:
            return {"ok": False, "error": "识别结果为空"}
        with self.store.lock:
            it = self.store.lib.get(str(sid))
            if not it:
                return {"ok": False, "error": "图库里没有这张"}
            for k in ("category", "emotion", "desc"):
                if res.get(k) is not None:
                    it[k] = str(res.get(k))[:300]
            for k in ("tags", "scenes"):
                if isinstance(res.get(k), list):
                    it[k] = [str(x)[:40] for x in res[k]][:8]
            if res.get("ocr") is not None:
                it["ocr"] = str(res.get("ocr"))[:600]
            it["reanalyzed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.store.flush_lib()
        return {"ok": True}

    async def analyze(self, sid: str) -> Dict[str, Any]:
        """VLM 复核：只返回新结果，不落库（前端对比后确认再应用）。"""
        p = self.item_path(sid)
        if not p or not p.is_file():
            return {"ok": False, "error": "图片不存在"}
        try:
            res = await self.vision.classify(p, self.store.categories())
            return {"ok": True, "result": res}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300]}

    # ---------- 上传 ----------
    async def upload(self, name: str, data: bytes, *, analyze: bool = False,
                     category: str = "") -> Dict[str, Any]:
        if not data:
            return {"ok": False, "error": "空文件"}
        ext = sniff_ext(data)
        if ext == "bin":
            ext = (Path(str(name or "")).suffix.lstrip(".") or "bin").lower()
        sha = sha256_of(data)
        with self.store.lock:
            for v in list(self.store.lib.values()):
                if v.get("sha256") == sha:
                    return {"ok": False, "error": "图库里已经有这张图了"}
        sid = new_sid()
        item = {"sid": sid, "sha256": sha, "dhash": dhash_of(data), "ext": ext, "size": len(data),
                "kind": "sticker" if ext in ("gif", "webp") else "image", "chat_type": "upload",
                "chat_id": "", "user_id": "", "sender_name": "", "source": "upload",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if analyze and not category:
            item.update({"status": "pending"})
            self._path_of(item).write_bytes(data)
            with self.store.lock:
                self.store.pool[sid] = item
            self.store.flush_pool()
            await self.approve([sid])
            return {"ok": True, "sid": sid, "queued": True}
        item.update({"status": "done", "scope": "public", "fav": False, "use_count": 0,
                     "category": category or "其他", "emotion": "", "desc": "", "tags": [],
                     "scenes": [], "ocr": "", "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        self._path_of(item, lib=True).write_bytes(data)
        with self.store.lock:
            self.store.lib[sid] = item
        self.store.flush_lib()
        return {"ok": True, "sid": sid}

    # ---------- 分类 ----------
    def categories_action(self, action: str, name: str = "", new: str = "") -> Dict[str, Any]:
        name = str(name or "").strip()
        new = str(new or "").strip()
        with self.store.lock:
            cats = self.store.categories()
            if action == "add":
                if not name:
                    return {"ok": False, "error": "分类名不能为空"}
                if name in cats:
                    return {"ok": False, "error": "分类已存在"}
                cats.append(name)
            elif action == "rename":
                if name not in cats:
                    return {"ok": False, "error": "分类不存在"}
                if not new:
                    return {"ok": False, "error": "新名字不能为空"}
                cats[cats.index(name)] = new
                for v in self.store.lib.values():
                    if str(v.get("category")) == name:
                        v["category"] = new
            elif action == "delete":
                if name not in cats:
                    return {"ok": False, "error": "分类不存在"}
                cats.remove(name)
                for sid in [k for k, v in self.store.lib.items() if str(v.get("category")) == name]:
                    self._remove_lib_locked(sid)
            else:
                return {"ok": False, "error": "未知操作"}
            self.store.meta["categories"] = cats
        self.store.flush_lib()
        self.store.flush_meta()
        return {"ok": True, "categories": cats}

    def stats(self) -> Dict[str, Any]:
        cfg = self._c()
        with self.store.lock:
            pool = list(self.store.pool.values())
            lib = list(self.store.lib.values())
        by_status: Dict[str, int] = {}
        for v in pool:
            s = str(v.get("status") or "pending")
            by_status[s] = by_status.get(s, 0) + 1
        by_cat: Dict[str, int] = {}
        for v in lib:
            c = str(v.get("category") or "其他")
            by_cat[c] = by_cat.get(c, 0) + 1
        return {
            "ok": True,
            "enabled": bool(cfg.get("sticker_steal_enabled")),
            "mode": str(cfg.get("sticker_steal_mode") or "probability"),
            "chance": float(cfg.get("sticker_steal_chance", 0.3) or 0),
            "cooldown": float(cfg.get("sticker_steal_cooldown", 30) or 0),
            "scope": str(cfg.get("sticker_steal_scope") or "sticker_only"),
            "kinds": self._kinds(self._c()),
            "audit": bool(cfg.get("sticker_steal_audit", True)),
            "capacity": int(cfg.get("sticker_pool_capacity", 200) or 200),
            "max_num": int(cfg.get("sticker_max_num", 100) or 100),
            "wl": list(cfg.get("sticker_steal_wl") or []),
            "bl": list(cfg.get("sticker_steal_bl") or []),
            "priority": str(cfg.get("sticker_steal_priority") or "whitelist_first"),
            "max_mb": float(cfg.get("sticker_max_mb", 8) or 8),
            "evict_weight": float(cfg.get("sticker_evict_usage_weight", 0.7) or 0),
            "vision_base": str(cfg.get("sticker_vision_base_url") or ""),
            "vision_key": str(cfg.get("sticker_vision_key") or ""),
            "vision_model": str(cfg.get("sticker_vision_model") or ""),
            "vision_interval": float(cfg.get("sticker_vision_interval", 5) or 0),
            "vision_timeout": float(cfg.get("sticker_vision_timeout", 180) or 180),
        "vision_max_tokens": int(cfg.get("sticker_vision_max_tokens", 8192) or 8192),
            "vision_prompt": str(cfg.get("sticker_vision_prompt") or ""),
            "vision_fallback_transcribe": bool(
                cfg.get("sticker_vision_fallback_transcribe", False)),
            # ---- AI 主动发表情包 ----
            "send_enabled": bool(cfg.get("sticker_send_enabled", True)),
            "send_where": self._send_where(),
            "send_max": int(cfg.get("sticker_send_max", 1) or 1),
            "send_fav_first": bool(cfg.get("sticker_send_fav_first", True)),
            "send_guide": str(cfg.get("sticker_send_guide") or ""),
            "send_guide_eff": self.guide_text(),
            "sent_total": int(((self.store.meta.get("stat") or {}).get("sent")) or 0),
            # ---- 发送时机：独立成条 + 精力/情绪极性权重随机 ----
            "send_separate": bool(cfg.get("sticker_send_separate", True)),
            "pos_random": bool(cfg.get("sticker_pos_random", True)),
            "pos_base": float(cfg.get("sticker_pos_base", 50) or 0),
            "pos_energy": float(cfg.get("sticker_pos_energy", 35) or 0),
            "pos_polar": float(cfg.get("sticker_pos_polar", 30) or 0),
            "pos_pos_emos": str(cfg.get("sticker_pos_pos_emos") or "") or DEFAULT_POS_EMOS,
            "pos_neg_emos": str(cfg.get("sticker_pos_neg_emos") or "") or DEFAULT_NEG_EMOS,
            "energy_now": self.energy_now(cfg),
            "pool_total": len(pool),
            "pool_status": by_status,
            "lib_total": len(lib),
            "lib_by_cat": by_cat,
            "fav_total": sum(1 for v in lib if v.get("fav")),
            "blacklist_total": len(self.store.blacklist()),
            "categories": self.store.categories(),
            "busy": self._busy or "",
            "has_pil": HAS_PIL,
            "dir": str(STICKER_DIR.resolve()),
        }

    def pool_list(self, *, status: str = "", page: int = 1, size: int = 60) -> Dict[str, Any]:
        with self.store.lock:
            items = list(self.store.pool.values())
        if status:
            items = [v for v in items if str(v.get("status")) == status]
        items.sort(key=lambda v: str(v.get("created_at") or ""), reverse=True)
        total = len(items)
        page = max(1, int(page or 1))
        size = max(1, min(200, int(size or 60)))
        return {"ok": True, "total": total, "page": page, "size": size,
                "items": items[(page - 1) * size: page * size]}


def test_png(size: int = 128) -> bytes:
    """手搓一张笑脸 PNG（黄脸 + 两只眼睛 + U 形嘴），给「测试识图」按钮用。

    不依赖 Pillow：zlib + PNG 分块自己拼。识图模型对这张图通常能给到「开心」。
    """
    w = h = size
    c = size / 2.0
    r = size * 0.46
    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            dx, dy = x - c, y - c
            d = (dx * dx + dy * dy) ** 0.5
            if d > r:
                row.append((245, 245, 245))
            elif d > r * 0.93:
                row.append((90, 90, 90))
            else:
                row.append((255, 214, 79))
            # 两只眼睛
            for ex in (c - r * 0.34, c + r * 0.34):
                if (x - ex) ** 2 + (y - (c - r * 0.18)) ** 2 <= (r * 0.11) ** 2:
                    row[-1] = (40, 40, 40)
            # U 形嘴 = 微笑（倒 U 会变哭脸）
            if abs(dx) <= r * 0.52:
                my = c + r * 0.46 - (dx * dx) / (1.2 * r)
                if abs(y - my) <= max(1.0, r * 0.035):
                    row[-1] = (40, 40, 40)
        rows.append(row)

    raw = bytearray()
    for row in rows:
        raw.append(0)
        for rr, gg, bb in row:
            raw += bytes((rr, gg, bb))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big"))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", w.to_bytes(4, "big") + h.to_bytes(4, "big")
                    + bytes((8, 2, 0, 0, 0)))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def create_stealer(cfg: Callable[[], Dict[str, Any]], log: Optional[Callable] = None,
                   fetch_bytes: Optional[Callable] = None) -> StickerStealer:
    return StickerStealer(cfg, log, fetch_bytes)
