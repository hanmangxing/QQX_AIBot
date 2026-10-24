# -*- coding: utf-8 -*-
"""
QQbotX · ComfyUI 生图引擎
=========================
对接本地 ComfyUI（HTTP API），把提示词注入工作流指定节点后出图。

设计原则（不引入外部依赖）：
  1. 工作流用 ComfyUI 的 **API 格式** JSON，放在 comfyui_workflows/ 目录下。
  2. 你只需要告诉它「正向提示词写在哪个节点」和「图片从哪个节点出来」。
  3. 出图走 /prompt 提交 → 轮询 /history → /view 取图，落盘到 comfyui_output/。
  4. 提示词节点/输出节点/种子节点都能自动识别，识别不了再手动填。

纯标准库 + aiohttp（项目已有依赖），不装新包。
"""

import asyncio
import json
import os
import random
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None


# ============================================================
#  常量
# ============================================================

DEFAULT_WORKFLOW_NAME = "默认-anima文生图.json"

# 图片扩展名
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# 提示词节点的候选类名（按优先级）
PROMPT_NODE_TYPES = ("CLIPTextEncode", "CLIPTextEncodeSDXL", "CLIPTextEncodeFlux",
                     "T5TextEncode", "TextEncodeQwenImageEdit")

# 输出节点的候选类名
OUTPUT_NODE_TYPES = ("SaveImage", "PreviewImage", "SaveImageWebsocket",
                     "Image Save", "SaveImageWithMetadata")


# 内置默认工作流：anima 系（UNETLoader + CLIPLoader + VAELoader），已实测可出图。
# 如果你的模型名不一样，直接在 Web UI 的「生图」页签里改，或导入自己的工作流。
DEFAULT_WORKFLOW: Dict[str, Any] = {
    "1": {"class_type": "UNETLoader",
          "inputs": {"unet_name": "miaomiaoHarem_anima12.safetensors",
                     "weight_dtype": "default"},
          "_meta": {"title": "Load Diffusion Model"}},
    "2": {"class_type": "CLIPLoader",
          "inputs": {"clip_name": "miaomiaoHarem_anima12_txt.safetensors",
                     "type": "stable_diffusion", "device": "default"},
          "_meta": {"title": "Load CLIP"}},
    "3": {"class_type": "VAELoader",
          "inputs": {"vae_name": "qwenImage_qwenImageVAE.safetensors"},
          "_meta": {"title": "Load VAE"}},
    "4": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "masterpiece, best quality, score_7, 1girl, solo",
                     "clip": ["2", 0]},
          "_meta": {"title": "CLIP Text Encode (Positive Prompt)"}},
    "5": {"class_type": "CLIPTextEncode",
          "inputs": {"text": "worst quality, low quality, lowres, bad anatomy, "
                             "watermark, signature, jpeg artifacts, text, error",
                     "clip": ["2", 0]},
          "_meta": {"title": "CLIP Text Encode (Negative Prompt)"}},
    "6": {"class_type": "EmptySD3LatentImage",
          "inputs": {"width": 832, "height": 1216, "batch_size": 1}},
    "7": {"class_type": "KSampler",
          "inputs": {"seed": 0, "steps": 28, "cfg": 4.0,
                     "sampler_name": "euler_ancestral", "scheduler": "normal",
                     "denoise": 1.0,
                     "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                     "latent_image": ["6", 0]},
          "_meta": {"title": "KSampler"}},
    "8": {"class_type": "VAEDecode",
          "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
    "9": {"class_type": "SaveImage",
          "inputs": {"filename_prefix": "QQbotX", "images": ["8", 0]},
          "_meta": {"title": "Save Image"}},
}


class ComfyUIError(Exception):
    """ComfyUI 相关错误。"""
    pass


# ============================================================
#  提示词标签解析（从 AI 正文里捞 <pic prompt="...">）
# ============================================================

# 兼容：<pic prompt="..."> / <pic prompt='...' /> / <pic>...</pic>
_PIC_RE = re.compile(
    r"<pic\b[^>]*?\bprompt\s*=\s*(?P<q>[\"'])(?P<prompt>.*?)(?P=q)[^>]*?/?>"
    r"(?:\s*</pic>)?",
    re.IGNORECASE | re.DOTALL,
)
_PIC_LOOSE_RE = re.compile(r"<pic\b[^>]*?>(?P<prompt>.*?)</pic>", re.IGNORECASE | re.DOTALL)
_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)


# ==================== 分辨率：模型自己在第二个块里挑，这里做解析 + 容错 ====================

SIZE_MIN, SIZE_MAX = 512, 2048
SIZE_STEP = 8

# 常用预设（会显示在「生图」页，模型优先用这几个）
SIZE_PRESETS = (
    ("1024x1536", 1024, 1536, "竖图 · 近景 / 特写 / 双人互动"),
    ("1536x1024", 1536, 1024, "横图 · 全景 / 远景 / 大场面"),
    ("1024x1024", 1024, 1024, "方图 · 拿不准时用"),
    ("832x1216", 832, 1216, "竖图 · 工作流默认比例"),
    ("1216x832", 1216, 832, "横图 · 工作流默认比例"),
)

# 模型只给描述、没给数字时的兜底
_SIZE_WORDS = (
    (("横", "全景", "远景", "landscape", "horizontal", "wide"), (1536, 1024)),
    (("竖", "近景", "特写", "portrait", "vertical", "tall"), (1024, 1536)),
    (("方", "square"), (1024, 1024)),
)

# 分辨率块：<size>1536x1024</size>（也认 pic_size / resolution / res / 分辨率）
_SIZE_BLOCK_RE = re.compile(
    r"<\s*(?P<t>size|pic_size|pic-size|resolution|res|分辨率)\b[^>]*>"
    r"(?P<v>.*?)"
    r"<\s*/\s*(?P=t)\s*>",
    re.IGNORECASE | re.DOTALL,
)
# 也兼容把尺寸写进提示词标签的属性：<pic prompt="..." size="1024x1536" />
_SIZE_ATTR_RE = re.compile(r"\bsize\s*=\s*[\"'](?P<v>[^\"']+)[\"']", re.IGNORECASE)


def _snap_size(v: Any) -> int:
    """对齐到 8 的倍数，并夹到 [SIZE_MIN, SIZE_MAX]。"""
    try:
        n = int(v)
    except Exception:
        return 0
    n = max(SIZE_MIN, min(SIZE_MAX, n))
    return int(round(n / float(SIZE_STEP))) * SIZE_STEP


def parse_size(raw: Any) -> Tuple[int, int]:
    """把模型给的分辨率文本解析成 (宽, 高)。

    认 1536x1024 / 1536×1024 / 1536*1024 / 1536 1024 / W=1536,H=1024 / 横图 / 竖图 …
    解析不出来返回 (0, 0)，表示「用工作流自己写死的尺寸」。
    """
    s = str(raw or "").strip()
    if not s:
        return 0, 0
    m = re.search(r"(\d{3,4})\s*[x×*X,，:：\s]\s*(\d{3,4})", s)
    if m:
        return _snap_size(m.group(1)), _snap_size(m.group(2))
    nums = re.findall(r"\d{3,4}", s)
    if len(nums) >= 2:
        return _snap_size(nums[0]), _snap_size(nums[1])
    low = s.lower()
    for words, wh in _SIZE_WORDS:
        for w in words:
            if w in low:
                return wh
    return 0, 0


def format_size(w: Any, h: Any) -> str:
    """(1024, 1536) -> '1024x1536'；无效返回空串。"""
    try:
        a, b = int(w), int(h)
    except Exception:
        return ""
    return "%dx%d" % (a, b) if (a > 0 and b > 0) else ""


def _pic_prompt_of(m) -> str:
    """从 <pic> 匹配里取提示词，兼容不同的分组命名。"""
    gd = m.groupdict() or {}
    for k in ("prompt", "p", "text", "body"):
        if gd.get(k):
            return str(gd[k]).strip()
    if m.groups():
        return str(m.group(1) or "").strip()
    return ""


def size_targets(wf: Dict[str, Any]) -> List[str]:
    """挑出「决定出图尺寸」的节点：类名带 Latent 或以 Empty 开头，且同时有 width + height。

    这样不会误改工作流里的缩放 / 放大节点。
    """
    out: List[str] = []
    if not isinstance(wf, dict):
        return out
    for k, v in wf.items():
        if not (isinstance(v, dict) and isinstance(v.get("inputs"), dict)):
            continue
        ins = v["inputs"]
        if "width" not in ins or "height" not in ins:
            continue
        ct = str(v.get("class_type") or "")
        if "Latent" in ct or ct.startswith("Empty"):
            out.append(k)
    return out


def read_workflow_size(wf: Dict[str, Any]) -> Tuple[int, int]:
    """读回工作流里实际生效的出图尺寸，读不到返回 (0, 0)。"""
    for k in size_targets(wf):
        ins = wf[k].get("inputs") or {}
        try:
            return int(ins.get("width") or 0), int(ins.get("height") or 0)
        except Exception:
            return 0, 0
    return 0, 0


def extract_pic_jobs(text: str, max_count: int = 1) -> Tuple[str, List[Dict[str, Any]]]:
    """从正文里提取生图任务。

    写法（提示词块 + 紧跟一个分辨率块）：
        <pic prompt="1girl, ..." />
        <size>1536x1024</size>
    也兼容把尺寸写进标签属性：<pic prompt="..." size="1024x1536" />

    返回 (清理后的正文, [{"prompt": str, "size": (w, h)}])。
    size 为 (0, 0) 表示没指定、用工作流自带尺寸。
    清理后的正文已经去掉所有 <pic> / <size> 块和 <think> 块，可直接发给用户。
    """
    if not text:
        return "", []

    jobs: List[Dict[str, Any]] = []
    spans: List[Tuple[int, int]] = []

    pic_hits = list(_PIC_RE.finditer(text))
    if not pic_hits:
        pic_hits = list(_PIC_LOOSE_RE.finditer(text))

    # 先把所有分辨率块连位置一起收好，后面按「紧跟在哪个提示词块之后」配对
    size_hits: List[Dict[str, Any]] = []
    for m in _SIZE_BLOCK_RE.finditer(text):
        size_hits.append({"start": m.start(), "end": m.end(),
                          "size": parse_size(m.group("v"))})

    used_size = set()
    for idx, m in enumerate(pic_hits):
        p = _pic_prompt_of(m)
        if not p:
            continue
        spans.append((m.start(), m.end()))
        # (1) 优先看标签属性
        am = _SIZE_ATTR_RE.search(m.group(0))
        size = parse_size(am.group("v")) if am else (0, 0)
        # (2) 再看紧跟在这个提示词块后面的那个分辨率块（到下一个提示词块为止）
        if not size[0]:
            nxt = pic_hits[idx + 1].start() if idx + 1 < len(pic_hits) else len(text)
            for si, sh in enumerate(size_hits):
                if si in used_size:
                    continue
                if m.end() <= sh["start"] < nxt:
                    size = sh["size"]
                    used_size.add(si)
                    break
        jobs.append({"prompt": p, "size": size})

    # (3) 还有没配上的分辨率块，按先后顺序补给还没定尺寸的任务
    for si, sh in enumerate(size_hits):
        if si in used_size:
            continue
        for j in jobs:
            if not j["size"][0]:
                j["size"] = sh["size"]
                used_size.add(si)
                break

    # 把用掉的标签区间整段摘掉（提示词块 + 分辨率块）
    spans.extend((sh["start"], sh["end"]) for sh in size_hits)
    if spans:
        spans.sort()
        buf, cursor = [], 0
        for s0, e0 in spans:
            if s0 < cursor:
                continue
            buf.append(text[cursor:s0])
            cursor = e0
        buf.append(text[cursor:])
        text = "".join(buf)

    # 残留的（没配成提示词的）标签也一并清掉，别让主人看见
    text = _THINK_RE.sub("", text)
    text = re.sub(r"<\s*/?\s*pic\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/?\s*(?:size|pic_size|pic-size|resolution|res|分辨率)\b[^>]*>",
                  "", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text, (jobs[:max_count] if max_count and max_count > 0 else jobs)


def extract_pic_prompts(text: str, max_count: int = 1) -> Tuple[str, List[str]]:
    """兼容旧调用：只要提示词列表的场合（内部走 extract_pic_jobs）。"""
    clean, jobs = extract_pic_jobs(text, max_count)
    return clean, [j["prompt"] for j in jobs]


# ============================================================
#  ComfyUI 客户端
# ============================================================

class ComfyUIClient:
    """ComfyUI HTTP 客户端。线程/协程安全（无共享可变状态，除目录）。"""

    def __init__(self, root_dir: str, config: Optional[Dict[str, Any]] = None):
        self.root_dir = root_dir
        self.config = config or {}
        self.workflows_dir = os.path.join(root_dir, "comfyui_workflows")
        self.output_dir = os.path.join(root_dir, "comfyui_output")
        self._history_cache: List[Dict[str, Any]] = []

    # ---------------- 配置读取 ----------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        if isinstance(self.config, dict):
            v = self.config.get(key)
            if v is not None:
                return v
        return default

    @property
    def server(self) -> str:
        s = str(self._cfg("comfy_server", "http://127.0.0.1:8000") or "").strip()
        if not s:
            s = "http://127.0.0.1:8000"
        if not s.startswith(("http://", "https://")):
            s = "http://" + s
        return s.rstrip("/")

    @property
    def timeout(self) -> int:
        try:
            return max(30, int(self._cfg("comfy_timeout", 300) or 300))
        except Exception:
            return 300

    def update_config(self, config: Dict[str, Any]):
        self.config = config or {}

    # ---------------- 目录 ----------------

    def ensure_dirs(self):
        os.makedirs(self.workflows_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

    def ensure_default_workflow(self) -> str:
        """首次使用时写入内置默认工作流。返回默认工作流文件名。"""
        self.ensure_dirs()
        path = os.path.join(self.workflows_dir, DEFAULT_WORKFLOW_NAME)
        if not os.path.exists(path):
            try:
                self._atomic_write_json(path, DEFAULT_WORKFLOW)
            except Exception:
                pass
        return DEFAULT_WORKFLOW_NAME

    @staticmethod
    def _atomic_write_json(path: str, data: Any):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ---------------- 工作流管理 ----------------

    def list_workflows(self) -> List[Dict[str, Any]]:
        """列出工作流目录下的所有 API 格式 JSON。"""
        self.ensure_dirs()
        out: List[Dict[str, Any]] = []
        for fn in sorted(os.listdir(self.workflows_dir)):
            if not fn.lower().endswith(".json"):
                continue
            p = os.path.join(self.workflows_dir, fn)
            if not os.path.isfile(p):
                continue
            info: Dict[str, Any] = {"name": fn, "size": os.path.getsize(p),
                                    "valid": False, "nodes": 0, "error": ""}
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, dict):
                    raise ValueError("顶层不是对象")
                info["nodes"] = len(d)
                if d and all(isinstance(v, dict) and "class_type" in v for v in d.values()):
                    info["valid"] = True
                    info["prompt_node"] = self.detect_prompt_node(d)
                    info["output_node"] = self.detect_output_node(d)
                else:
                    info["error"] = "不是 API 格式（缺 class_type），请用 ComfyUI 的「导出（API格式）」"
            except Exception as e:
                info["error"] = str(e)[:120]
            out.append(info)
        return out

    def load_workflow(self, name: str) -> Dict[str, Any]:
        self.ensure_dirs()
        safe = os.path.basename(name or "")
        if not safe:
            safe = self.ensure_default_workflow()
        path = os.path.join(self.workflows_dir, safe)
        if not os.path.exists(path):
            raise ComfyUIError("工作流不存在: %s" % safe)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not d:
            raise ComfyUIError("工作流内容为空或格式不对: %s" % safe)
        bad = [k for k, v in d.items() if not (isinstance(v, dict) and "class_type" in v)]
        if bad:
            raise ComfyUIError(
                "这个 JSON 不是 ComfyUI 的 API 格式（有 %d 个节点缺 class_type）。"
                "请在 ComfyUI 里用「工作流 → 导出（API格式）」重新导出。" % len(bad))
        return d

    def save_workflow(self, name: str, data: Any) -> str:
        self.ensure_dirs()
        safe = os.path.basename(str(name or "").strip())
        if not safe:
            raise ComfyUIError("文件名不能为空")
        if not safe.lower().endswith(".json"):
            safe += ".json"
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict) or not data:
            raise ComfyUIError("工作流内容为空")
        bad = [k for k, v in data.items() if not (isinstance(v, dict) and "class_type" in v)]
        if bad:
            raise ComfyUIError(
                "不是 API 格式：%d 个节点缺 class_type。"
                "请在 ComfyUI 里点「工作流 → 导出（API格式）」再导入。" % len(bad))
        self._atomic_write_json(os.path.join(self.workflows_dir, safe), data)
        return safe

    def delete_workflow(self, name: str) -> bool:
        safe = os.path.basename(str(name or "").strip())
        path = os.path.join(self.workflows_dir, safe)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False

    # ---------------- 节点自动识别 ----------------

    @staticmethod
    def detect_prompt_node(wf: Dict[str, Any]) -> str:
        """找正向提示词节点：优先 title 含 Positive，其次第一个 CLIPTextEncode。"""
        cands = [(k, v) for k, v in wf.items()
                 if isinstance(v, dict) and v.get("class_type") in PROMPT_NODE_TYPES]
        if not cands:
            return ""
        for k, v in cands:
            title = str((v.get("_meta") or {}).get("title") or "")
            if re.search(r"positive|正向", title, re.IGNORECASE):
                return str(k)
        return str(cands[0][0])

    @staticmethod
    def detect_output_node(wf: Dict[str, Any]) -> str:
        """找输出节点：优先 SaveImage，其次 PreviewImage。"""
        for want in ("SaveImage", "PreviewImage", "SaveImageWebsocket"):
            for k, v in wf.items():
                if isinstance(v, dict) and v.get("class_type") == want:
                    return str(k)
        for k, v in wf.items():
            if isinstance(v, dict) and v.get("class_type") in OUTPUT_NODE_TYPES:
                return str(k)
        return ""

    @staticmethod
    def detect_negative_node(wf: Dict[str, Any], prompt_node: str = "") -> str:
        cands = [(k, v) for k, v in wf.items()
                 if isinstance(v, dict) and v.get("class_type") in PROMPT_NODE_TYPES
                 and str(k) != str(prompt_node)]
        for k, v in cands:
            title = str((v.get("_meta") or {}).get("title") or "")
            if re.search(r"negative|负向", title, re.IGNORECASE):
                return str(k)
        return str(cands[0][0]) if cands else ""

    @staticmethod
    def detect_seed_nodes(wf: Dict[str, Any]) -> List[str]:
        """找所有带 seed / noise_seed 参数的节点。"""
        out = []
        for k, v in wf.items():
            if not isinstance(v, dict):
                continue
            ins = v.get("inputs") or {}
            if "seed" in ins or "noise_seed" in ins:
                out.append(str(k))
        return out

    # ---------------- 提示词注入 ----------------

    def build_prompt(self,
                     wf: Dict[str, Any],
                     prompt: str,
                     prompt_node: str = "",
                     negative: str = "",
                     negative_node: str = "",
                     steps: int = 0,
                     seed: int = -1,
                     batch: int = 0,
                     width: int = 0,
                     height: int = 0) -> Dict[str, Any]:
        """把提示词/参数注入工作流，返回可直接提交的 prompt 字典。"""
        wf = json.loads(json.dumps(wf))  # 深拷贝，不动原对象

        pn = str(prompt_node or self._cfg("comfy_prompt_node", "") or "").strip()
        if not pn or pn not in wf:
            pn = self.detect_prompt_node(wf)
        if not pn or pn not in wf:
            raise ComfyUIError("找不到正向提示词节点，请在生图页签里手动指定节点 ID")

        node = wf[pn]
        ins = node.setdefault("inputs", {})
        if "text" in ins or node.get("class_type") in PROMPT_NODE_TYPES:
            ins["text"] = prompt
        else:
            # 少数节点用 prompt / text_g 之类
            for key in ("prompt", "text_g", "positive", "string"):
                if key in ins:
                    ins[key] = prompt
                    break
            else:
                ins["text"] = prompt

        # 负向提示词（可选）
        if negative:
            nn = str(negative_node or self._cfg("comfy_negative_node", "") or "").strip()
            if not nn or nn not in wf:
                nn = self.detect_negative_node(wf, pn)
            if nn and nn in wf:
                wf[nn].setdefault("inputs", {})["text"] = negative

        # 种子随机化：seed<0 表示自动随机
        seed_nodes = self.detect_seed_nodes(wf)
        fixed = seed if seed is not None and seed >= 0 else -1
        if fixed < 0:
            fixed = random.randint(1, 2 ** 31 - 1)
        for sid in seed_nodes:
            si = wf[sid].setdefault("inputs", {})
            if "seed" in si:
                si["seed"] = fixed
            elif "noise_seed" in si:
                si["noise_seed"] = fixed

        # 步数覆盖（>0 才生效）
        try:
            steps = int(steps or 0)
        except Exception:
            steps = 0
        if steps > 0:
            for v in wf.values():
                if isinstance(v, dict) and isinstance(v.get("inputs"), dict) \
                        and "steps" in v["inputs"]:
                    v["inputs"]["steps"] = steps

        # 批量张数覆盖（>0 才生效）
        try:
            batch = int(batch or 0)
        except Exception:
            batch = 0
        if batch > 0:
            for v in wf.values():
                if isinstance(v, dict) and isinstance(v.get("inputs"), dict) \
                        and "batch_size" in v["inputs"]:
                    v["inputs"]["batch_size"] = batch

        # 尺寸覆盖（宽高都给且都 >0 才生效）：AI 自己挑的横图 / 竖图
        try:
            _ow, _oh = int(width or 0), int(height or 0)
        except Exception:
            _ow = _oh = 0
        if _ow > 0 and _oh > 0:
            for _sid in size_targets(wf):
                wf[_sid]["inputs"]["width"] = _ow
                wf[_sid]["inputs"]["height"] = _oh

        return wf

    # ---------------- HTTP ----------------

    async def _get_json(self, session, path: str, timeout: int = 20) -> Any:
        url = self.server + path
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            txt = await r.text()
            if r.status != 200:
                raise ComfyUIError("HTTP %s: %s" % (r.status, txt[:200]))
            return json.loads(txt) if txt else {}

    async def test_connection(self) -> Dict[str, Any]:
        """测试 ComfyUI 是否可达。"""
        if aiohttp is None:
            return {"ok": False, "message": "aiohttp 未安装"}
        try:
            async with aiohttp.ClientSession() as s:
                st = await self._get_json(s, "/system_stats", timeout=12)
            sysinfo = st.get("system", {}) if isinstance(st, dict) else {}
            return {"ok": True, "message": "连接成功", "server": self.server,
                    "comfyui_version": sysinfo.get("comfyui_version"),
                    "python_version": (sysinfo.get("python_version") or "")[:40],
                    "pytorch_version": sysinfo.get("pytorch_version")}
        except Exception as e:
            return {"ok": False, "server": self.server,
                    "message": "连不上 ComfyUI：%s" % e}

    async def queue_state(self) -> Dict[str, Any]:
        try:
            async with aiohttp.ClientSession() as s:
                q = await self._get_json(s, "/queue", timeout=12)
            return {"ok": True,
                    "running": len(q.get("queue_running") or []),
                    "pending": len(q.get("queue_pending") or [])}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    async def interrupt(self) -> bool:
        """中断当前任务。"""
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(self.server + "/interrupt",
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                    return r.status == 200
        except Exception:
            return False

    # ---------------- 出图 ----------------

    async def generate(self,
                       prompt: str,
                       workflow: str = "",
                       prompt_node: str = "",
                       negative: str = "",
                       steps: int = 0,
                       seed: int = -1,
                       batch: int = 0,
                       width: int = 0,
                       height: int = 0,
                       progress=None) -> Dict[str, Any]:
        """跑一次生图。

        progress: 可选回调 async fn(stage: str, detail: str)
        返回 {"ok":bool, "images":[绝对路径], "prompt_id":.., "elapsed":.., "message":..}
        """
        if aiohttp is None:
            return {"ok": False, "message": "aiohttp 未安装"}
        if not (prompt or "").strip():
            return {"ok": False, "message": "提示词为空"}

        wf_name = str(workflow or self._cfg("comfy_workflow", "") or "").strip()
        if not wf_name:
            wf_name = self.ensure_default_workflow()
        raw = self.load_workflow(wf_name)
        wf = self.build_prompt(raw, prompt.strip(),
                               prompt_node=prompt_node, negative=negative,
                               steps=steps, seed=seed, batch=batch,
                               width=width, height=height)
        # 读回工作流里实际生效的出图尺寸（用于日志 / 返回值）
        _used_w, _used_h = read_workflow_size(wf)

        client_id = uuid.uuid4().hex
        payload = {"prompt": wf, "client_id": client_id}
        t0 = time.time()

        async def _say(stage, detail=""):
            if progress:
                try:
                    await progress(stage, detail)
                except Exception:
                    pass

        try:
            async with aiohttp.ClientSession() as s:
                await _say("submit", "已提交给 ComfyUI")
                async with s.post(self.server + "/prompt", json=payload,
                                  timeout=aiohttp.ClientTimeout(total=60)) as r:
                    body = await r.text()
                    if r.status != 200:
                        raise ComfyUIError("ComfyUI 拒绝了这个工作流（HTTP %s）：%s"
                                           % (r.status, body[:500]))
                    res = json.loads(body)

                pid = res.get("prompt_id")
                if not pid:
                    raise ComfyUIError("ComfyUI 没返回 prompt_id：%s" % body[:300])

                # 轮询
                info = None
                deadline = time.time() + self.timeout
                last_note = 0.0
                while time.time() < deadline:
                    await asyncio.sleep(1.5)
                    try:
                        async with s.get(self.server + "/history/" + pid,
                                         timeout=aiohttp.ClientTimeout(total=20)) as hr:
                            if hr.status != 200:
                                continue
                            h = json.loads(await hr.text())
                    except Exception:
                        continue
                    if isinstance(h, dict) and pid in h:
                        info = h[pid]
                        break
                    el = time.time() - t0
                    if el - last_note > 8:
                        last_note = el
                        await _say("running", "生成中… %.0fs" % el)

                if not info:
                    raise ComfyUIError("等待出图超时（%ss），ComfyUI 可能还在排队或卡住了"
                                       % self.timeout)

                st = info.get("status") or {}
                if st.get("status_str") == "error":
                    msgs = st.get("messages") or []
                    detail = ""
                    for m in msgs:
                        if isinstance(m, list) and len(m) > 1 and m[0] == "execution_error":
                            d = m[1] or {}
                            detail = "%s @ 节点%s：%s" % (d.get("node_type", ""),
                                                        d.get("node_id", ""),
                                                        d.get("exception_message", ""))
                            break
                    raise ComfyUIError("工作流执行报错：%s" % (detail or json.dumps(msgs, ensure_ascii=False)[:300]))

                # 收集图片
                found = []
                for nid, o in (info.get("outputs") or {}).items():
                    for im in (o.get("images") or []):
                        found.append(im)
                if not found:
                    raise ComfyUIError("工作流跑完了但没产出图片，检查输出节点是否接了 SaveImage")

                await _say("download", "正在取回 %d 张图" % len(found))
                saved = []
                self.ensure_dirs()
                stamp = time.strftime("%Y%m%d_%H%M%S")
                for i, im in enumerate(found):
                    fn = im.get("filename") or ""
                    if not fn:
                        continue
                    qs = "filename=%s&subfolder=%s&type=%s" % (
                        fn, im.get("subfolder", "") or "", im.get("type", "output") or "output")
                    from urllib.parse import quote
                    url = "%s/view?filename=%s&subfolder=%s&type=%s" % (
                        self.server, quote(fn),
                        quote(im.get("subfolder", "") or ""),
                        quote(im.get("type", "output") or "output"))
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=90)) as ir:
                        if ir.status != 200:
                            continue
                        data = await ir.read()
                    ext = os.path.splitext(fn)[1].lower() or ".png"
                    out_name = "comfy_%s_%d%s" % (stamp, i, ext)
                    out_path = os.path.join(self.output_dir, out_name)
                    with open(out_path, "wb") as f:
                        f.write(data)
                    saved.append(out_path)

                if not saved:
                    raise ComfyUIError("图片下载失败")

                el = round(time.time() - t0, 1)
                await _say("done", "完成，用时 %.1fs" % el)
                return {"ok": True, "images": saved, "prompt_id": pid,
                        "elapsed": el, "workflow": wf_name,
                        "message": "出图成功（%.1fs）" % el}

        except ComfyUIError as e:
            return {"ok": False, "message": str(e), "elapsed": round(time.time() - t0, 1)}
        except asyncio.TimeoutError:
            return {"ok": False, "message": "请求 ComfyUI 超时", "elapsed": round(time.time() - t0, 1)}
        except Exception as e:
            return {"ok": False, "message": "生图失败：%s" % e, "elapsed": round(time.time() - t0, 1)}

    # ---------------- 图片历史 ----------------

    def list_images(self, limit: int = 60) -> List[Dict[str, Any]]:
        """列出已生成的图片，按时间倒序。"""
        self.ensure_dirs()
        out = []
        try:
            names = os.listdir(self.output_dir)
        except Exception:
            return out
        for fn in names:
            if not fn.lower().endswith(IMAGE_EXTS):
                continue
            p = os.path.join(self.output_dir, fn)
            if not os.path.isfile(p):
                continue
            try:
                stt = os.stat(p)
            except Exception:
                continue
            out.append({"name": fn, "size": stt.st_size, "mtime": stt.st_mtime,
                        "time_text": time.strftime("%m-%d %H:%M", time.localtime(stt.st_mtime))})
        out.sort(key=lambda x: x["mtime"], reverse=True)
        return out[:max(1, limit)]

    def image_path(self, name: str) -> Optional[str]:
        safe = os.path.basename(str(name or ""))
        if not safe.lower().endswith(IMAGE_EXTS):
            return None
        p = os.path.join(self.output_dir, safe)
        return p if os.path.isfile(p) else None

    def delete_image(self, name: str) -> bool:
        p = self.image_path(name)
        if not p:
            return False
        try:
            os.remove(p)
            return True
        except Exception:
            return False

    def clear_images(self) -> int:
        n = 0
        for it in self.list_images(limit=100000):
            if self.delete_image(it["name"]):
                n += 1
        return n

# ==================== 生图能力说明（注入给 LLM 的提示词，可在「生图」页编辑） ====================

COMFY_GUIDE_TMPL = """【生图能力｜你可以自己画图】
你手边连着一台 ComfyUI 绘图后端。当你觉得此刻画面感很强、值得给主人看一眼的时候，可以在回复正文里自己写生图标签，系统会自动把图画出来、直接发给主人——不需要任何人下指令，全凭你自己的意愿。
格式（英文逗号分隔的 danbooru 风格标签，越具体越好看）：
<pic prompt="1girl, silver hair, blue eyes, school uniform, cherry blossom, looking at viewer" />
<size>1024x1536</size>
规则：
1. 标签写在正文的任意位置都行，系统会自动把它摘掉，主人看不到这行字。
2. {max_rule}
3. 只在你真的想画的时候写；不要每句都画，也不要为了画而画。
4. prompt 里只写英文标签，不要写中文、不要写解释、不要写引号。
5. 主人明确要求画时（例如「画一张看看」），照办。
6. 标签必须写在 game_content 的 content 参数里、跟正文放在一起。
{size_rule}"""

# 占位符：渲染时会被「单条回复最多出图」对应的规则文案替换
GUIDE_MAX_PLACEHOLDER = "{max_rule}"

# 占位符：渲染时会被「分辨率块怎么写」的说明替换（自定义文案里没写它会自动补在末尾）
GUIDE_SIZE_PLACEHOLDER = "{size_rule}"

SIZE_RULE = (
    "尺寸（可选）：在每个提示词块后面紧跟一个分辨率块，你自己按画面判断：\n"
    "<size>1536x1024</size>\n"
    "· 全景 / 远景 / 大场面 / 横构图 → 1536x1024\n"
    "· 近景 / 特写 / 双人互动 / 竖构图 → 1024x1536\n"
    "· 拿不准 → 1024x1024\n"
    "不写就用工作流默认尺寸。这个块跟提示词块一样会被系统摘掉，主人看不到；"
    "它会被填进绘图工作流的宽高。"
)


def build_comfy_guide(max_n: int = 1, custom: str = "") -> str:
    """渲染注入给模型的生图能力说明。

    max_n  : 「单条回复最多出图」的上限，决定 {max_rule} 的内容
    custom : 用户在生图页自定义的文案；非空时用它替代内置模板
    """
    try:
        n = int(max_n)
    except Exception:
        n = 1
    n = max(1, min(8, n))
    if n <= 1:
        rule = "一次回复最多写 1 条标签。"
    else:
        rule = ("一次回复最多写 %d 条标签。当这一轮剧情里值得看的画面不止一个时，"
                "就把 %d 条写满，系统会把这几张图一起发出去。" % (n, n))
    text = custom if (custom or "").strip() else COMFY_GUIDE_TMPL
    text = text.replace(GUIDE_MAX_PLACEHOLDER, rule)
    # 分辨率说明：自定义文案里既没写占位符、也没自己写 <size>，就自动补在末尾，
    # 保证模型一定知道「可以自己挑横图 / 竖图」
    if GUIDE_SIZE_PLACEHOLDER in text:
        text = text.replace(GUIDE_SIZE_PLACEHOLDER, SIZE_RULE)
    elif "<size>" not in text:
        text = text.rstrip() + "\n" + SIZE_RULE
    return text

