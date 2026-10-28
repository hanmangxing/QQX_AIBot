# -*- coding: utf-8 -*-
"""NovelAI 生图后端。

对外接口对齐 comfyui.ComfyUIClient（generate / list_images / image_path /
delete_image / clear_images），图片落进同一个目录，上层按「生图端口」切换即可。

接口：POST https://image.novelai.net/ai/generate-image
鉴权：Authorization: Bearer <持久化 token>（官网 Account → Get Persistent API Token）
返回：ZIP 二进制，内含 1~n 张 PNG
多角色：characterPrompts + v4_prompt.caption.char_captions（角色分区）
"""

import io
import os
import json
import time
import random
import zipfile
import asyncio
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except Exception:  # 没装 aiohttp 时给出明确提示，不影响其它后端
    aiohttp = None


class NAIError(Exception):
    """NovelAI 返回的业务错误（带人话解释）。"""


BASE_URL = "https://image.novelai.net"
GENERATE_PATH = "/ai/generate-image"
USER_DATA_PATH = "/user/data"

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# ---------------- 模型 / 采样器 / UC ----------------

MODELS = [
    ("nai-diffusion-5-full", "NAI Diffusion V5 完整版"),
    ("nai-diffusion-5-curated", "NAI Diffusion V5 精选版"),
    ("nai-diffusion-4-5-full", "NAI Diffusion V4.5 完整版"),
    ("nai-diffusion-4-5-curated", "NAI Diffusion V4.5 精选版"),
    ("nai-diffusion-4-full", "NAI Diffusion V4 完整版"),
    ("nai-diffusion-3", "NAI Diffusion V3"),
]
DEFAULT_MODEL = "nai-diffusion-5-full"

SAMPLERS = ["k_euler_ancestral", "k_euler", "k_dpmpp_2m", "k_dpmpp_2s_ancestral",
            "k_dpmpp_sde", "ddim", "k_dpmpp_2m_sde", "k_heun"]

# 0=Heavy 1=Light 2=Human Focus 3=None
UC_PRESETS = [(0, "Heavy"), (1, "Light"), (2, "Human Focus"), (3, "None")]

# NAI 只吃 64 的倍数，且总像素有上限（约 1024x1024x3）
SIZE_STEP = 64
MAX_PIXELS = 3145728
DEFAULT_W, DEFAULT_H = 832, 1216

# Opus 档常见尺寸（不扣 Anlas）
FREE_SIZES = [(832, 1216), (1216, 832), (1024, 1024)]


def fit_size(width: int, height: int) -> Tuple[int, int]:
    """把任意宽高对齐到 NAI 能吃的规格：64 的倍数 + 总像素不超上限。"""
    w = int(width or 0)
    h = int(height or 0)
    if w <= 0 or h <= 0:
        return DEFAULT_W, DEFAULT_H
    w = max(SIZE_STEP, int(round(w / float(SIZE_STEP))) * SIZE_STEP)
    h = max(SIZE_STEP, int(round(h / float(SIZE_STEP))) * SIZE_STEP)
    if w * h > MAX_PIXELS:
        k = (MAX_PIXELS / float(w * h)) ** 0.5
        w = max(SIZE_STEP, int(w * k / SIZE_STEP) * SIZE_STEP)
        h = max(SIZE_STEP, int(h * k / SIZE_STEP) * SIZE_STEP)
    return w, h


def format_size(width: int, height: int) -> str:
    w = int(width or 0)
    h = int(height or 0)
    return "%dx%d" % (w, h) if w > 0 and h > 0 else ""


def split_characters(text: str) -> Tuple[str, List[str]]:
    """把 '2girls, outdoors || 角色A的tag || 角色B的tag' 拆成 (base, [角色A, 角色B])。

    没有 '||' 就是单段，返回 (原文, [])，行为与以前完全一致。
    """
    s = str(text or "").strip()
    if "||" not in s:
        return s, []
    parts = [p.strip() for p in s.split("||")]
    base = parts[0]
    chars = [p for p in parts[1:] if p]
    return base, chars


def build_payload(prompt: str, negative: str, model: str, width: int, height: int,
                  steps: int, scale: float, sampler: str, seed: int, n_samples: int,
                  uc_preset: int, quality: bool,
                  characters: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """按 V4/V4.5/V5 的骨架拼请求体（V4 及以上必须带 v4_prompt，缺了会 500）。"""
    base = str(prompt or "")
    uc = str(negative or "")
    chars = [c for c in (characters or []) if str((c or {}).get("prompt") or "").strip()]

    params: Dict[str, Any] = {
        "width": int(width),
        "height": int(height),
        "scale": float(scale),
        "sampler": str(sampler),
        "steps": int(steps),
        "n_samples": int(n_samples),
        "seed": int(seed),
        "ucPreset": int(uc_preset),
        "qualityToggle": bool(quality),
        "dynamic_thresholding": False,
        "controlnet_strength": 1,
        "legacy": False,
        "add_original_image": False,
        "cfg_rescale": 0,
        "noise_schedule": "karras",
        "legacy_v3_extend": False,
        "skip_cfg_above_sigma": None,
        "negative_prompt": uc,
        "extra_noise_seed": int(seed),
    }

    is_v3 = "diffusion-3" in str(model)
    if not is_v3:
        params["params_version"] = 3
        params["use_coords"] = bool(chars)
        char_captions = []
        char_prompts = []
        for c in chars:
            p = str(c.get("prompt") or "").strip()
            x = float(c.get("x", 0.5) or 0.5)
            y = float(c.get("y", 0.5) or 0.5)
            char_captions.append({"char_caption": p, "centers": [{"x": x, "y": y}]})
            char_prompts.append({"prompt": p, "uc": str(c.get("uc") or ""),
                                 "center": {"x": x, "y": y}})
        params["v4_prompt"] = {
            "caption": {"base_caption": base, "char_captions": char_captions},
            "use_coords": bool(chars),
            "use_order": bool(chars),
        }
        params["v4_negative_prompt"] = {
            "caption": {"base_caption": uc, "char_captions": []},
            "use_coords": False,
            "use_order": False,
        }
        params["characterPrompts"] = char_prompts

    return {"input": base, "model": str(model), "action": "generate", "parameters": params}


# ==================== 生图能力说明（注入给 LLM 的提示词，可在「生图」页编辑） ====================

NAI_GUIDE_TMPL = """【生图能力｜你可以自己画图】
你手边连着一台 NovelAI 绘图后端。当你觉得此刻画面感很强、值得给主人看一眼的时候，可以在回复正文里自己写生图标签，系统会自动把图画出来、直接发给主人——不需要任何人下指令，全凭你自己的意愿。
格式（英文逗号分隔的 danbooru 风格标签，越具体越好看）：
<pic prompt="1girl, silver hair, blue eyes, school uniform, cherry blossom, looking at viewer" />
<size>832x1216</size>
规则：
1. 标签写在正文的任意位置都行，系统会自动把它摘掉，主人看不到这行字。
2. {max_rule}
3. 只在你真的想画的时候写；不要每句都画，也不要为了画而画。
4. 标签必须写在 game_content 的 content 参数里、跟正文放在一起。
5. NovelAI 是纯标签流：只写英文逗号分隔的 tag，不要写自然语言长句；画质词（masterpiece、best quality 这类）系统会自动带上，不用自己写；也不要写 lowres、bad anatomy 这类负向词，负向内容由系统的 UC 预设负责。
{size_rule}{multi_rule}"""

GUIDE_MAX_PLACEHOLDER = "{max_rule}"
GUIDE_SIZE_PLACEHOLDER = "{size_rule}"
GUIDE_MULTI_PLACEHOLDER = "{multi_rule}"

SIZE_RULE = (
    "尺寸（可选）：不写就用默认竖版 832x1216。想指定就在提示词块后面另起一行写 <size>宽x高</size>：\n"
    "· 竖构图 / 特写 → <size>832x1216</size>\n"
    "· 横构图 / 全景 / 多人 → <size>1216x832</size>\n"
    "· 方形 → <size>1024x1024</size>\n"
    "宽高会自动对齐到 64 的倍数，总像素不能超过 3145728。这个块跟提示词块一样会被系统摘掉，主人看不到。"
)

MULTI_RULE = (
    "多人 / 合照（重要）：用 || 把角色分开写 —— || 前面是画面公共部分，之后每个 || 段是一个角色的专属 tag：\n"
    "<pic prompt=\"2girls, outdoors, standing together || luo tianyi, long grey hair, green eyes, hanfu || luluka, pink short hair, red eyes, white dress\" />\n"
    "· 有几个角色就写几段，一个都不能漏，也不要用自己编的 tag 顶替角色设定里已经给过的标准提示词；只有确实没给标准提示词的角色，才自己补特征 tag。\n"
    "· 人数标签写在第一段：2girls / multiple girls（男角色用 2boys / multiple boys）。\n"
    "· 每个角色的 tag 尽量写全（发色、瞳色、发型、服装），角色之间别互相穿插。\n"
    "· 单个角色时不要写 ||，照常写一段就行。"
)


def build_nai_guide(max_n: int = 1, custom: str = "") -> str:
    """渲染注入给 LLM 的生图说明。custom 非空则用它，并把缺失的规则补在末尾。"""
    text = str(custom or "").strip() or NAI_GUIDE_TMPL
    if GUIDE_MAX_PLACEHOLDER in text:
        text = text.replace(GUIDE_MAX_PLACEHOLDER, _max_rule(max_n))
    elif "张" not in text:
        text = text.rstrip() + "\n" + _max_rule(max_n)
    extras: List[str] = []
    if GUIDE_SIZE_PLACEHOLDER in text:
        text = text.replace(GUIDE_SIZE_PLACEHOLDER, SIZE_RULE)
    elif "<size>" not in text:
        extras.append(SIZE_RULE)
    if GUIDE_MULTI_PLACEHOLDER in text:
        text = text.replace(GUIDE_MULTI_PLACEHOLDER, MULTI_RULE)
    elif "||" not in text:
        extras.append(MULTI_RULE)
    if extras:
        text = text.rstrip() + "\n" + "\n".join(extras)
    return text


def _max_rule(max_n: int) -> str:
    n = max(1, int(max_n or 1))
    if n <= 1:
        return "一条回复里最多画 1 张，多了会被系统丢掉。"
    return "一条回复里最多画 %d 张，多了会被系统丢掉。" % n


# ==================== 客户端 ====================

class NAIClient:
    """NovelAI 生图客户端（接口与 ComfyUIClient 保持一致，便于上层切换）。"""

    def __init__(self, config_dir: str, config: Optional[Dict[str, Any]] = None,
                 output_dir: Optional[str] = None):
        self.config_dir = config_dir
        # 保留同一个 dict 引用（不复制）：面板改了 token / 模型 / 步数要即时生效
        self._config = config if isinstance(config, dict) else {}
        self.output_dir = output_dir or os.path.join(config_dir, "comfyui_output")

    # ---- 基础 ----

    @property
    def server(self) -> str:
        return str(self._config.get("nai_base_url") or BASE_URL).rstrip("/")

    @property
    def token(self) -> str:
        return str(self._config.get("nai_token") or "").strip()

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def update_config(self, config: Optional[Dict[str, Any]] = None) -> None:
        """面板保存配置后同步进来（与 ComfyUIClient.update_config 对齐）。"""
        if isinstance(config, dict):
            self._config = config

    @property
    def proxy(self) -> str:
        """出海代理，例如 http://127.0.0.1:7890；留空则交给系统代理环境变量。"""
        return str(self._config.get("nai_proxy") or "").strip()

    def _proxy_kw(self) -> Dict[str, Any]:
        return {"proxy": self.proxy} if self.proxy else {}

    def ensure_dirs(self) -> None:
        try:
            os.makedirs(self.output_dir, exist_ok=True)
        except Exception:
            pass

    @staticmethod
    def _explain(status: int, data: bytes) -> str:
        txt = ""
        try:
            j = json.loads(data.decode("utf-8", "ignore"))
            txt = str(j.get("message") or j.get("error") or j.get("detail") or "")
        except Exception:
            txt = data.decode("utf-8", "ignore")[:200]
        if status == 401:
            return "NovelAI 鉴权失败：token 不对或已过期。%s" % txt
        if status == 402:
            return "Anlas 点数不够了（当前档位出这个尺寸 / 步数要扣点数）。%s" % txt
        if status == 429:
            return "NovelAI 并发到上限了，等一会儿再试。%s" % txt
        if status == 400:
            return "NovelAI 拒绝了这次请求（400）：%s" % txt
        return "NovelAI 返回 %d：%s" % (status, txt)

    async def account(self) -> Dict[str, Any]:
        """读账户信息（档位 + 剩余 Anlas），给设置页显示用。"""
        if aiohttp is None:
            return {"ok": False, "message": "缺少 aiohttp"}
        if not self.token:
            return {"ok": False, "message": "还没填 API token"}
        try:
            async with aiohttp.ClientSession(trust_env=True) as s:
                async with s.get(self.server + USER_DATA_PATH,
                                 headers={"Authorization": "Bearer %s" % self.token},
                                 timeout=aiohttp.ClientTimeout(total=20),
                                 **self._proxy_kw()) as r:
                    data = await r.read()
                    if r.status != 200:
                        return {"ok": False, "message": self._explain(r.status, data)}
            j = json.loads(data.decode("utf-8", "ignore"))
        except asyncio.TimeoutError:
            return {"ok": False, "message": "连接 NovelAI 超时（20 秒无响应）：本机到 %s 不通%s"
                    % (self.server, ("，代理 %s 可能没生效" % self.proxy) if self.proxy
                       else "，需要填一条能出海的代理地址")}
        except Exception as e:
            return {"ok": False, "message": "读取账户信息失败：%s" % (e or type(e).__name__)}
        sub = j.get("subscription") or {}
        ts = sub.get("trainingStepsLeft")
        anlas = 0
        if isinstance(ts, dict):
            anlas = int(ts.get("fixedTrainingStepsLeft", 0) or 0) + \
                    int(ts.get("purchasedTrainingSteps", 0) or 0)
        elif isinstance(ts, (int, float)):
            anlas = int(ts)
        return {"ok": True, "tier": sub.get("tier", 0), "anlas": anlas,
                "active": bool(sub.get("active", True))}

    # ---- 出图 ----

    async def generate(self, prompt: str, workflow: Any = None, prompt_node: Any = None,
                       negative: Optional[str] = None, steps: int = 0, seed: int = 0,
                       batch: int = 0, width: int = 0, height: int = 0,
                       loras: Any = None, characters: Any = None,
                       progress: Any = None) -> Dict[str, Any]:
        """出图。签名与 ComfyUIClient.generate 一致（workflow / prompt_node / loras 忽略）。"""
        async def _say(stage: str, msg: str) -> None:
            if progress is None:
                return
            try:
                r = progress(stage, msg)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass

        t0 = time.time()
        if aiohttp is None:
            return {"ok": False, "message": "缺少 aiohttp，先装一下（pip install aiohttp）",
                    "elapsed": 0}
        if not self.token:
            return {"ok": False, "message": "还没填 NovelAI 的 API token（设置 → 生图 → NovelAI）",
                    "elapsed": 0}

        base, auto_chars = split_characters(prompt)
        chars = list(characters or [])
        if not chars and auto_chars:
            chars = [{"prompt": c} for c in auto_chars]

        model = str(self._cfg("nai_model") or DEFAULT_MODEL)
        w, h = fit_size(int(width or 0) or int(self._cfg("nai_width", 0) or 0),
                        int(height or 0) or int(self._cfg("nai_height", 0) or 0))
        _steps = int(steps or 0) or int(self._cfg("nai_steps", 28) or 28)
        _scale = float(self._cfg("nai_scale", 5.0) or 5.0)
        _sampler = str(self._cfg("nai_sampler") or SAMPLERS[0])
        _n = int(batch or 0) or int(self._cfg("nai_batch", 1) or 1)
        _n = max(1, min(4, _n))
        _seed = int(seed or 0)
        if _seed <= 0:
            _seed = random.randint(1, 2 ** 31 - 1)
        _uc_preset = int(self._cfg("nai_uc_preset", 0) or 0)
        _quality = bool(self._cfg("nai_quality", True))
        _neg = str(negative if negative is not None else (self._cfg("nai_negative") or ""))

        payload = build_payload(base, _neg, model, w, h, _steps, _scale, _sampler,
                                _seed, _n, _uc_preset, _quality, chars)
        url = self.server + GENERATE_PATH
        headers = {"Authorization": "Bearer %s" % self.token,
                   "Content-Type": "application/json",
                   "Accept": "*/*",
                   "User-Agent": "QQX_AIBot"}

        await _say("submit", "提交 NovelAI：%s / %d 步%s" % (
            format_size(w, h), _steps, ("｜%d 个角色分区" % len(chars)) if chars else ""))

        try:
            async with aiohttp.ClientSession(trust_env=True) as s:
                async with s.post(url, json=payload, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=240),
                                  **self._proxy_kw()) as r:
                    data = await r.read()
                    if r.status != 200:
                        raise NAIError(self._explain(r.status, data))
        except NAIError as e:
            return {"ok": False, "message": str(e), "elapsed": round(time.time() - t0, 1)}
        except asyncio.TimeoutError:
            return {"ok": False, "message": "请求 NovelAI 超时（240 秒无响应）：本机到 %s 不通，检查代理设置"
                    % self.server, "elapsed": round(time.time() - t0, 1)}
        except Exception as e:
            return {"ok": False, "message": "生图失败：%s" % e, "elapsed": round(time.time() - t0, 1)}

        saved = self._save_images(data)
        if not saved:
            return {"ok": False, "message": "NovelAI 没返回图片（可能被内容审核拦了）",
                    "elapsed": round(time.time() - t0, 1)}
        el = round(time.time() - t0, 1)
        await _say("done", "完成，用时 %.1fs" % el)
        return {"ok": True, "images": saved, "seed": _seed, "elapsed": el,
                "model": model, "size": format_size(w, h),
                "message": "出图成功（%.1fs）" % el}

    def _save_images(self, data: bytes) -> List[str]:
        """返回体是 ZIP（内含若干 PNG）；也兼容直接返回单张 PNG。"""
        self.ensure_dirs()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        saved: List[str] = []
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception:
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                p = os.path.join(self.output_dir, "nai_%s_0.png" % stamp)
                with open(p, "wb") as f:
                    f.write(data)
                return [p]
            return []
        for i, nm in enumerate(zf.namelist()):
            ext = os.path.splitext(nm)[1].lower()
            if ext not in IMAGE_EXTS:
                continue
            p = os.path.join(self.output_dir, "nai_%s_%d%s" % (stamp, i, ext or ".png"))
            try:
                with open(p, "wb") as f:
                    f.write(zf.read(nm))
            except Exception:
                continue
            saved.append(p)
        return saved

    # ---------------- 图片历史（与 comfyui 同一套） ----------------

    def list_images(self, limit: int = 60) -> List[Dict[str, Any]]:
        self.ensure_dirs()
        out: List[Dict[str, Any]] = []
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
                        "time_text": time.strftime("%m-%d %H:%M",
                                                   time.localtime(stt.st_mtime))})
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
