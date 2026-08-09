# -*- coding: utf-8 -*-
"""
LoRA 打标管理 WebUI —— FastAPI 后端
功能：扫描图片+txt 打标、共同标签识别、标签增删覆写、词库+LLM 翻译、LM Studio 初筛
部署：H:/Hermes工作区/Hermes/LoraTagManager/
"""
import os
import re
import json
import time
import shutil
import csv
import base64
import threading
from pathlib import Path
from collections import Counter
from datetime import datetime

import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------- 路径常量（Windows 原生路径） ----------
# 路径：PyInstaller 打包（frozen）时——可写数据（config/cache/backups）放 exe 同目录，
# 只读资源（taglib/static）放打包资源区 _MEIPASS；源码运行时两者同目录
import sys as _sys
if getattr(_sys, "frozen", False):
    APP_DIR = Path(_sys.executable).resolve().parent
    RES_DIR = Path(getattr(_sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent
    RES_DIR = APP_DIR

BASE_DIR = APP_DIR  # 可写数据目录（config/cache/backups）
RES_BASE = RES_DIR  # 只读资源目录（taglib/static）
CONFIG_PATH = BASE_DIR / "config.json"
CACHE_PATH = BASE_DIR / "cache.json"
BACKUP_DIR = BASE_DIR / "backups"
SCANNED_DIRS = set()  # G4/G5：已扫描目录白名单（/api/image /api/thumb 仅允许访问扫描过的目录内文件）

# 词库/离线词典：已封装到项目内 taglib/ 目录（不跨目录调用外部插件）
WEILIN_YAML = RES_BASE / "taglib" / "taglib.yaml"
DANBOORU_CSV = RES_BASE / "taglib" / "danbooru_zh.csv"

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".avif"}

app = FastAPI(title="LoRA Tag Manager")
# CORS：允许 localhost/127.0.0.1 互访——域名分片扩展连接池（翻译走 127.0.0.1、UI 走 localhost，互不排队）
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 配置读写 ----------
DEFAULT_CONFIG = {
    "deepseek": {"api_key": "", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    # vllm_enabled: 在 LM Studio 地址上启用 vLLM 模式（多模态输入 + 视觉版提示词），复用同一 URL/模型
    "lmstudio": {"base_url": "http://localhost:1234/v1", "model": "", "api_key": "lm-studio", "vllm_enabled": False},
    "screen_provider": "auto",  # 初筛引擎：auto=LM Studio 没填就云端 / lmstudio / deepseek
    "save_concurrency": 100,    # 全部保存的并发数（设置里可调）
    "preview_size": 512,
    "recent_dirs": [],
    # 自定义 LLM 系统提示词（初筛用）：[{title, content}]，active=-1=用内置默认
    "llm_prompts": [],
    "llm_prompt_active": -1,
    "font_size": "default",  # 界面字号：sm / default / lg
    "translate_concurrency": 3,  # LLM 翻译并发数（1-8，默认 3）
}


def load_config():
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            _cfg_key_decrypt(cfg)  # api_key 解密回明文（内存用）
            return cfg
        except Exception:
            # 读取/解密失败：返回默认（仅内存），绝不覆盖原文件——防止用户配置被冲掉
            return json.loads(json.dumps(DEFAULT_CONFIG))
    # 文件确实不存在：自动创建默认配置（仅此一种创建途径；绝不备份、绝不覆盖已有配置）
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        save_config(cfg)
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- API Key 加密（防 config.json 明文泄露） ----------
_ENC_KEY = b"lora-tag-mgr-2026"  # 本地混淆盐（非安全传输用途，仅防明文）
_KEY_PREFIX = "enc:"  # 加密标记前缀：幂等保护——已加密的 key 不再重复加密（防止误把密文再加密导致 key 损坏）


def _encrypt_key(plain: str) -> str:
    """简单可逆混淆：XOR + base64（本地工具用途，防明文即可）；已加密的返回原样"""
    if not plain:
        return ""
    if plain.startswith(_KEY_PREFIX):
        return plain  # 已是密文，跳过（幂等）
    data = plain.encode("utf-8")
    xored = bytes(b ^ _ENC_KEY[i % len(_ENC_KEY)] for i, b in enumerate(data))
    return _KEY_PREFIX + base64.b64encode(xored).decode()


def _decrypt_key(enc: str) -> str:
    """解密 _encrypt_key 的结果；非加密格式（历史明文/空）原样返回"""
    if not enc:
        return ""
    if enc.startswith(_KEY_PREFIX):
        enc = enc[len(_KEY_PREFIX):]
    try:
        data = base64.b64decode(enc.encode())
        return bytes(b ^ _ENC_KEY[i % len(_ENC_KEY)] for i, b in enumerate(data)).decode("utf-8")
    except Exception:
        return enc  # 历史明文 key 兼容


def _cfg_key_encrypt(cfg):
    """保存前：把 deepseek.api_key / lmstudio.api_key 加密"""
    for sec in ("deepseek", "lmstudio"):
        sec_cfg = cfg.get(sec) or {}
        if sec_cfg.get("api_key"):
            sec_cfg["api_key"] = _encrypt_key(sec_cfg["api_key"])


def _cfg_key_decrypt(cfg):
    """读取后：把加密的 api_key 解密（内存中保持明文供请求头使用）"""
    for sec in ("deepseek", "lmstudio"):
        sec_cfg = cfg.get(sec) or {}
        if sec_cfg.get("api_key"):
            sec_cfg["api_key"] = _decrypt_key(sec_cfg["api_key"])


def save_config_enc(cfg):
    """带加密的保存：只写 config.json 本体，不产生任何备份副本"""
    cfg = json.loads(json.dumps(cfg))  # 深拷贝，不污染内存明文
    _cfg_key_encrypt(cfg)
    save_config(cfg)


# ---------- 翻译缓存 ----------
_cache_lock = threading.Lock()


def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    with _cache_lock:
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 词库加载（weilin 风格：分组+颜色+中英对照） ----------
_tag_lib = None  # [{en, zh, group, color, cat}]


def load_tag_lib():
    """加载 weilin default.yaml 标签库（一级分类 > 二级分组(颜色) > 标签(英:中)）"""
    global _tag_lib
    if _tag_lib is not None:
        return _tag_lib
    _tag_lib = []
    if not WEILIN_YAML.exists():
        return _tag_lib
    try:
        import yaml
        data = yaml.safe_load(WEILIN_YAML.read_text(encoding="utf-8"))
        for cat in data or []:
            cat_name = cat.get("name", "")
            for grp in cat.get("groups", []) or []:
                color = grp.get("color", "")
                grp_name = grp.get("name", "")
                for en, zh in (grp.get("tags", {}) or {}).items():
                    _tag_lib.append({
                        "en": en,
                        "zh": zh if zh else "",
                        "group": grp_name,
                        "cat": cat_name,
                        "color": color,
                    })
    except Exception as e:
        print(f"[warn] 标签库解析失败: {e}")
    return _tag_lib


# 二级翻译词库：en -> zh（danbooru csv 补充 + 子代理补齐的 zh_extra）
_zh_dict = None
_zh_extra = None


def load_zh_extra():
    """加载子代理补齐的翻译（taglib/zh_extra.json），词库优先于词典"""
    global _zh_extra
    if _zh_extra is not None:
        return _zh_extra
    _zh_extra = {}
    p = RES_BASE / "taglib" / "zh_extra.json"
    if p.exists():
        try:
            _zh_extra = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] zh_extra.json 加载失败: {e}")
    return _zh_extra


def load_zh_dict():
    global _zh_dict
    if _zh_dict is not None:
        return _zh_dict
    _zh_dict = {}
    if DANBOORU_CSV.exists():
        try:
            with open(DANBOORU_CSV, encoding="utf-8-sig", newline="") as f:
                for row in csv.reader(f):
                    if len(row) >= 2 and row[0].strip():
                        _zh_dict[row[0].strip()] = row[1].strip()
        except Exception as e:
            print(f"[warn] danbooru 词库加载失败: {e}")
    return _zh_dict


def lookup_zh(en):
    """翻译查找：标签库(zh非空) > zh_extra(子代理补齐) > danbooru 词库 > 翻译缓存 > 原文"""
    en = en.strip()
    lib = load_tag_lib()
    for t in lib:
        if t["en"] == en and t["zh"]:
            return t["zh"]
    extra = load_zh_extra()
    if en in extra:
        return extra[en]
    d = load_zh_dict()
    if en in d:
        return d[en]
    # danbooru 词典用下划线命名（long_hair），打标常用空格（long hair），做规范化匹配
    norm = en.replace(" ", "_")
    if norm in d:
        return d[norm]
    cache = load_cache()
    if en in cache:
        return cache[en]
    return en  # 未命中返回原文


def lookup_style(en):
    """标签风格信息：匹配到词库返回分组+颜色，未命中返回默认灰"""
    en = en.strip()
    for t in load_tag_lib():
        if t["en"] == en:
            return {"group": t["group"], "cat": t["cat"], "color": t["color"], "matched": True}
    return {"group": "", "cat": "", "color": "", "matched": False}


# ---------- 标签解析 / 写回 ----------
def parse_tags(text):
    """解析打标 txt：按中英文逗号、换行分割，去空"""
    parts = re.split(r"[,，\n]", text)
    return [p.strip() for p in parts if p.strip()]


def backup_file(path):
    """覆写前备份到 backups/ 目录（保留时间戳）"""
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"{Path(path).name}.{ts}.bak"
    shutil.copy2(path, dst)
    return str(dst)


def find_txt_for_image(img_path):
    """找同名 txt：优先 .txt，兼容 .caption"""
    p = Path(img_path)
    for ext in (".txt", ".caption"):
        cand = p.with_suffix(ext)
        if cand.exists():
            return str(cand)
    return None


# ---------- API 模型 ----------
class ScanReq(BaseModel):
    folder: str


class UpdateReq(BaseModel):
    image: str      # 图片路径
    tags: list[str] # 新标签列表（覆写）


class BatchReq(BaseModel):
    images: list[str]      # 图片路径列表
    remove: list[str] = [] # 要删除的标签
    add: list[str] = []    # 要添加的标签


class TranslateReq(BaseModel):
    tags: list[str]  # 待翻译标签
    user_prompt: str = ""  # LLM 用户提示词（附加到翻译请求）
    to_en: bool = False  # True=中文→英文方向（输入框中文转标签用）


class ScreenReq(BaseModel):
    images: list[str]  # 图片路径列表（初筛）
    model: str = ""
    tags: list[str] = []  # 全选批量初筛：直接判定这批标签（不逐图读 txt）
    user_prompt: str = ""  # LLM 用户提示词（附加到 user content，前端编辑条单行框）
    system_prompt: str = ""  # 自定义 LLM 系统提示词（设置里选择；空=用内置默认）


# ---------- 扫描 ----------
@app.post("/api/scan")
def scan(req: ScanReq):
    folder = req.folder.strip().strip('"')
    if not folder or not os.path.isdir(folder):
        return JSONResponse({"error": f"文件夹不存在: {folder}"}, status_code=400)

    # 记录最近目录
    cfg = load_config()
    if folder not in cfg["recent_dirs"]:
        cfg["recent_dirs"].insert(0, folder)
        cfg["recent_dirs"] = cfg["recent_dirs"][:10]
        save_config_enc(cfg)

    SCANNED_DIRS.add(os.path.abspath(folder))  # G4：记录已授权目录

    images = []
    tag_counter = Counter()
    total_txt = 0
    # 递归识别子文件夹（数据集可能按分类存子目录）：os.walk 遍历全部层级
    try:
        walker = sorted(os.walk(folder), key=lambda t: t[0].lower())
    except Exception as e:
        return JSONResponse({"error": f"读取目录失败: {e}"}, status_code=400)

    for root, dirs, files in walker:
        dirs.sort(key=str.lower)  # 子目录稳定顺序
        for name in sorted(files, key=str.lower):
            full = os.path.join(root, name)
            ext = os.path.splitext(name)[1].lower()
            if ext not in IMG_EXTS or not os.path.isfile(full):
                continue
            txt_path = find_txt_for_image(full)
            tags = []
            if txt_path:
                try:
                    tags = parse_tags(Path(txt_path).read_text(encoding="utf-8"))
                except Exception:
                    try:
                        tags = parse_tags(Path(txt_path).read_text(encoding="gbk"))
                    except Exception:
                        tags = []
            if txt_path:
                total_txt += 1
            for t in tags:
                tag_counter[t] += 1
            images.append({
            "name": name,
            "path": full,
            "txt": txt_path,
            "has_txt": txt_path is not None,
            "tags": tags,
            "count": len(tags),
        })

    n_img = len(images)
    # 共同标签：出现次数 == 有 txt 图片数（且 >=1）
    common = sorted([t for t, c in tag_counter.items() if c == total_txt and total_txt > 0])
    # 全部标签统计（按频率降序）
    tag_stats = [{"tag": t, "count": c, "freq": round(c / n_img, 3) if n_img else 0}
                 for t, c in tag_counter.most_common()]

    return {
        "folder": folder,
        "images": images,
        "tag_stats": tag_stats,
        "common_tags": common,
        "total_images": n_img,
        "total_txt": total_txt,
        "total_tags": len(tag_stats),
    }


@app.post("/api/recent")
def remember_folder(req: ScanReq):
    """缓存上次填写的路径：输入框失焦/回车时记录（不依赖扫描成功）"""
    folder = (req.folder or "").strip().strip('"')
    if not folder:
        return {"ok": True}
    cfg = load_config()
    if folder not in cfg["recent_dirs"]:
        cfg["recent_dirs"].insert(0, folder)
    else:
        # 已存在则移到最前（最近使用优先）
        cfg["recent_dirs"].remove(folder)
        cfg["recent_dirs"].insert(0, folder)
    cfg["recent_dirs"] = cfg["recent_dirs"][:10]
    save_config_enc(cfg)
    return {"ok": True}


# ---------- 单图覆写 ----------
@app.post("/api/tags/update")
def update(req: UpdateReq):
    if not os.path.isfile(req.image):
        return JSONResponse({"error": "图片不存在"}, status_code=400)
    txt = find_txt_for_image(req.image)
    if not txt:
        # 无 txt 则新建同名 txt
        txt = str(Path(req.image).with_suffix(".txt"))
    backup_path = ""
    if os.path.exists(txt):
        backup_path = backup_file(txt)
    Path(txt).write_text(", ".join(req.tags), encoding="utf-8")
    return {"ok": True, "txt": txt, "backup": backup_path, "tags": req.tags}


# ---------- 批量增删（覆写） ----------
@app.post("/api/tags/batch")
def batch(req: BatchReq):
    results = []
    for img in req.images:
        if not os.path.isfile(img):
            results.append({"image": img, "ok": False, "error": "图片不存在"})
            continue
        txt = find_txt_for_image(img)
        if not txt:
            txt = str(Path(img).with_suffix(".txt"))
        if os.path.exists(txt):
            tags = parse_tags(Path(txt).read_text(encoding="utf-8"))
        else:
            tags = []
        # 删除
        remove_set = set(req.remove)
        new_tags = [t for t in tags if t not in remove_set]
        # 添加（去重、保留原顺序）
        for t in req.add:
            if t not in new_tags:
                new_tags.append(t)
        if os.path.exists(txt):
            backup_path = backup_file(txt)
        else:
            backup_path = ""
        Path(txt).write_text(", ".join(new_tags), encoding="utf-8")
        results.append({"image": img, "ok": True, "txt": txt, "backup": backup_path, "tags": new_tags})
    return {"ok": True, "results": results}


# ---------- 从备份恢复（撤销已保存的覆写） ----------
class RestoreReq(BaseModel):
    items: list[dict]  # [{image, backup}] 图片路径 + 备份文件路径


@app.post("/api/restore")
def restore(req: RestoreReq):
    results = []
    for it in req.items:
        img = it.get("image", "")
        bak = it.get("backup", "")
        if not img or not bak:
            results.append({"image": img, "ok": False, "error": "参数缺失"})
            continue
        if not os.path.isfile(bak):
            results.append({"image": img, "ok": False, "error": f"备份不存在: {bak}"})
            continue
        # G5：备份文件必须位于 backups/ 目录内（防任意文件覆写）
        bak_abs = os.path.abspath(bak)
        if not (bak_abs.startswith(str(BACKUP_DIR) + os.sep) or bak_abs == str(BACKUP_DIR)):
            results.append({"image": img, "ok": False, "error": "备份路径未授权"})
            continue
        txt = find_txt_for_image(img)
        if not txt:
            txt = str(Path(img).with_suffix(".txt"))
        # 恢复前先备份当前状态（保留回退链），再覆写
        if os.path.exists(txt):
            backup_file(txt)
        shutil.copy2(bak, txt)
        # 返回恢复后的标签（前端直接更新内存，无需刷新图片）
        try:
            tags = parse_tags(Path(txt).read_text(encoding="utf-8"))
        except Exception:
            tags = []
        results.append({"image": img, "ok": True, "txt": txt, "tags": tags})
    return {"ok": True, "results": results}


# ---------- 图片访问 ----------
THUMB_DIR = BASE_DIR / "thumbs"  # 缩略图缓存目录


@app.get("/api/image")
def get_image(path: str):
    if not path or not os.path.isfile(path):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    # G4：仅允许访问已扫描目录内的文件（防任意文件读取）
    ap = os.path.abspath(path)
    if not any(ap.startswith(d + os.sep) or ap == d for d in SCANNED_DIRS):
        return JSONResponse({"error": "未授权的路径（请先扫描该目录）"}, status_code=403)
    return FileResponse(path)


@app.get("/api/thumb")
def get_thumb(path: str, size: int = 512):
    """生成缩略图（默认 512x512 居中裁剪），防大数据集卡顿；缓存到 thumbs/ 目录"""
    if not path or not os.path.isfile(path):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    # G4：仅允许访问已扫描目录内的文件（防任意文件读取）
    ap = os.path.abspath(path)
    if not any(ap.startswith(d + os.sep) or ap == d for d in SCANNED_DIRS):
        return JSONResponse({"error": "未授权的路径（请先扫描该目录）"}, status_code=403)
    size = max(64, min(size, 2048))  # 限制范围 64~2048
    # 缓存 key：路径哈希 + 尺寸
    import hashlib
    key = hashlib.md5(path.encode("utf-8")).hexdigest()[:16]
    THUMB_DIR.mkdir(exist_ok=True)
    cache_path = THUMB_DIR / f"{key}_{size}.jpg"
    if cache_path.exists():
        return FileResponse(str(cache_path))
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            # 居中裁剪成正方形再缩放
            w, h = im.size
            s = min(w, h)
            left = (w - s) // 2
            top = (h - s) // 2
            im = im.crop((left, top, left + s, top + s)).resize((size, size), Image.LANCZOS)
            im.save(str(cache_path), "JPEG", quality=82)
    except Exception as e:
        return JSONResponse({"error": f"缩略图失败: {e}"}, status_code=500)
    return FileResponse(str(cache_path))


# ---------- 翻译：词库离线匹配 ----------
@app.get("/api/translate/dict")
def translate_dict(tags: str = ""):
    """tags: 逗号分隔的标签列表，返回 中英对照+风格信息"""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    out = []
    for t in tag_list:
        style = lookup_style(t)
        zh = lookup_zh(t)
        if zh == t and style["matched"]:
            zh = ""
        out.append({"en": t, "zh": zh if zh != t else "", **style})
    return {"ok": True, "items": out}


@app.get("/api/translate/cache")
def translate_cache():
    """返回全部 LLM 翻译缓存（cache.json），前端启动时回填 zhMap——翻译持久化到本地"""
    return {"ok": True, "cache": load_cache()}


# ---------- 翻译：DeepSeek LLM（批量，缓存） ----------
@app.post("/api/translate/llm")
def translate_llm(req: TranslateReq):
    cfg = load_config()
    ds = cfg.get("deepseek", {})
    api_key = ds.get("api_key", "").strip()
    if not api_key:
        return JSONResponse({"error": "未配置 DeepSeek API Key，请在设置中填写"}, status_code=400)

    # 只翻译缓存未命中的
    cache = load_cache()
    todo = [t for t in req.tags if t and t not in cache]
    if not todo:
        return {"ok": True, "translated": [], "hit_cache": len([t for t in req.tags if t in cache])}

    # 分批（每批 50 个标签）+ 并发请求（并发数可调：translate_concurrency，默认 3）
    batch_size = 50
    result_map = {}
    url = ds.get("base_url", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    concurrency = max(1, min(8, int(cfg.get("translate_concurrency", 3) or 3)))
    chunks = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]

    def translate_chunk(chunk):
        if req.to_en:
            # 中文→英文方向（输入框输中文时自动转标签）
            prompt = (
                "你是 Stable Diffusion 标签转换助手。请把以下中文/混合内容转换为对应的英文 Danbooru 标签，"
                "使用小写和下划线（如：长发 → long_hair）。输出严格 JSON 对象，键为输入原文，值为英文标签。"
                "若已是英文则规范化（空格换下划线）后原样返回。不要输出任何其他内容。\n"
                + json.dumps(chunk, ensure_ascii=False)
            )
        else:
            prompt = (
                "你是 Stable Diffusion 标签翻译助手。请把以下英文标签翻译成简体中文，"
                "输出严格 JSON 对象，键为原英文标签，值为中文翻译。"
                "若标签已是中文或无需翻译则原样返回。不要输出任何其他内容。\n"
                + json.dumps(chunk, ensure_ascii=False)
            )
        if req.user_prompt:
            # 用户自定义提示词附加（编辑条 LLM 用户提示词框），放在标签列表之后
            prompt += f"\n\n用户附加要求：{req.user_prompt}"
        payload = {
            "model": ds.get("model", "deepseek-chat"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        r = requests.post(url, json=payload, headers=headers, timeout=120)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(content)

    # 并发请求各批（可调并发数），失败批不阻塞其他批
    import concurrent.futures
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(translate_chunk, c): c for c in chunks}
        for f in concurrent.futures.as_completed(futures):
            try:
                result_map.update(f.result())
            except Exception as e:
                errors.append(str(e))
    # 成功的先写缓存（部分失败也不浪费）
    for k, v in result_map.items():
        cache[k] = v
    save_cache(cache)
    if errors:
        return JSONResponse({"error": f"翻译部分失败（{len(errors)}/{len(chunks)} 批）：{errors[0][:120]}"}, status_code=502)
    return {"ok": True, "translated": result_map, "hit_cache": 0}


# ---------- 初筛（LM Studio / DeepSeek 云端 / vLLM 多模态，标注顾问系统提示词） ----------
SCREEN_SYSTEM_PROMPT = """角色设定

你是一名资深LoRA训练数据标注顾问，专精于为Stable Diffusion（含UNet和DiT架构）准备训练集标注文件（TXT）。你的核心理念是："需要AI学会的不用打标，不需要AI学的才打标"，即只标注画面中可变、可替换的元素，固定不变的身份特征和环境默认值一律留白。

---

核心判定流程（逐层过滤）

第1步：判断该元素是否为"人物的固有身份特征"

· 是 → 绝对不打标（如：痣的位置、脸型、瞳孔颜色、体型、标志性发型）
· 否 → 进入第2步

第2步：判断该元素是否为"可变的临时状态"

· 是 → 必须打标（如：服装、眼镜、手持道具、动作姿势、表情变化）
· 否 → 进入第3步

第3步：判断该元素是否属于"环境默认值"

· 包括：背景（无论纯色或复杂）、画质、光线、拍摄角度
· 全部不打标，视为训练时的"空气"背景，让AI自行泛化

特殊判定：负向标签（质量标签如blurry, lowres等）

· 一律不打标，因为AI学会"什么是模糊"毫无意义。模糊图作为数据集已起到正则化作用，无需文字说明。

---

三大红线（绝对禁止标注项）

1. 背景词：禁止出现 background、wall、studio 等任何背景描述
2. 颜色词（指背景色）：禁止出现 red background、blue bg 等
3. 质量词：禁止出现 masterpiece、best quality、sharp focus ——这类词应放在推理时的正向提示词中

---

你的任务：对给定图片的现有标签逐条判定，输出 JSON 数组，每项格式：
{"tag": "原标签", "action": "keep" 或 "delete", "reason": "简短中文理由（10字内）"}
只输出 JSON，不要其他内容。"""

# vLLM 多模态版：可以看图片，判定逻辑更准（输入条件从纯文本标签 → 图片+标签）
SCREEN_SYSTEM_PROMPT_VLLM = """角色设定

你是一名资深LoRA训练数据标注顾问，专精于为Stable Diffusion（含UNet和DiT架构）准备训练集标注文件（TXT）。你的核心理念是："需要AI学会的不用打标，不需要AI学的才打标"，即只标注画面中可变、可替换的元素，固定不变的身份特征和环境默认值一律留白。

你可以直接看到图片内容，请结合图片实际画面与现有标签逐条核对。

---

核心判定流程（逐层过滤）

第1步：判断该元素是否为"人物的固有身份特征"

· 是 → 绝对不打标（如：痣的位置、脸型、瞳孔颜色、体型、标志性发型）
· 否 → 进入第2步

第2步：判断该元素是否为"可变的临时状态"

· 是 → 必须打标（如：服装、眼镜、手持道具、动作姿势、表情变化）
· 否 → 进入第3步

第3步：判断该元素是否属于"环境默认值"

· 包括：背景（无论纯色或复杂）、画质、光线、拍摄角度
· 全部不打标，视为训练时的"空气"背景，让AI自行泛化

特殊判定：负向标签（质量标签如blurry, lowres等）

· 一律不打标，因为AI学会"什么是模糊"毫无意义。模糊图作为数据集已起到正则化作用，无需文字说明。

---

三大红线（绝对禁止标注项）

1. 背景词：禁止出现 background、wall、studio 等任何背景描述
2. 颜色词（指背景色）：禁止出现 red background、blue bg 等
3. 质量词：禁止出现 masterpiece、best quality、sharp focus ——这类词应放在推理时的正向提示词中

---

额外注意（你能看图，务必执行）：
· 若图片实际内容与标签不符（标签描述的东西画面里没有），该标签判 delete
· 若画面有明显的可打标元素（服装/道具/姿势/表情）而标签缺失，reason 里可注明"建议补充"
· 输出 JSON 数组，每项：{"tag": "原标签", "action": "keep" 或 "delete", "reason": "简短中文理由（10字内）"}
· 只输出 JSON，不要其他内容。"""


def _pick_screen_provider(cfg):
    """初筛引擎选择：auto=LM Studio 配了就用它，否则云端 DeepSeek"""
    provider = (cfg.get("screen_provider") or "auto").strip().lower()
    lm = cfg.get("lmstudio", {})
    lm_ready = bool((lm.get("base_url") or "").strip() and (lm.get("model") or "").strip())
    if provider == "auto":
        return "lmstudio" if lm_ready else "deepseek"
    return provider  # lmstudio / deepseek 显式选择


def _image_data_url(img_path, max_side=1024):
    """图片转 base64 data URL（缩到 max_side 内，控制 token 量）"""
    from PIL import Image
    import base64, io
    im = Image.open(img_path)
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
    if im.mode != "RGB":
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


@app.post("/api/screen")
def screen(req: ScreenReq):
    cfg = load_config()
    provider = _pick_screen_provider(cfg)
    vllm_mode = False  # 默认关闭；lmstudio 分支按配置覆盖（修复：deepseek 分支此前 UnboundLocalError → 500）

    if provider == "deepseek":
        ds = cfg.get("deepseek", {})
        if not (ds.get("api_key") or "").strip():
            return JSONResponse({"error": "云端初筛需要 DeepSeek API Key（请在设置中配置）"}, status_code=400)
        url = (ds.get("base_url") or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {ds['api_key']}", "Content-Type": "application/json"}
        model = ds.get("model") or "deepseek-chat"
        sys_prompt = req.system_prompt or SCREEN_SYSTEM_PROMPT  # 自定义提示词优先
        image_data = None
    else:  # lmstudio（含 vLLM 模式：同一地址，仅换提示词与输入条件）
        lm = cfg.get("lmstudio", {})
        vllm_mode = bool(lm.get("vllm_enabled"))
        url = (lm.get("base_url") or "http://localhost:1234/v1").rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {lm.get('api_key','lm-studio')}", "Content-Type": "application/json"}
        model = req.model or lm.get("model", "")
        sys_prompt = req.system_prompt or (SCREEN_SYSTEM_PROMPT_VLLM if vllm_mode else SCREEN_SYSTEM_PROMPT)
        # S4 修复：vLLM 模式 image_data 移入循环内按当前图生成（不再共用第一张图）

    results = []
    for img in req.images:
        if not os.path.isfile(img):
            results.append({"image": img, "ok": False, "error": "图片不存在"})
            continue
        if req.tags:
            # 全选批量初筛：直接判定传入的标签（共同标签），不逐图读 txt
            tags = req.tags
        else:
            txt = find_txt_for_image(img)
            tags = parse_tags(Path(txt).read_text(encoding="utf-8")) if txt else []
        if not tags:
            results.append({"image": img, "ok": True, "tags": [], "verdicts": []})
            continue
        # 输入条件：vLLM 模式 = 图片(base64) + 标签；普通模式 = 纯文本标签
        # S4 修复：vLLM 下每张图独立生成 base64（多图批量初筛不再共用第一张图）
        image_data = None
        if vllm_mode:
            try:
                image_data = _image_data_url(img)
            except Exception:
                image_data = None
        extra = f"\n\n用户附加要求：{req.user_prompt}" if req.user_prompt else ""
        if image_data is not None:
            user_prompt = {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_data}},
                    {"type": "text", "text": f"图片文件：{os.path.basename(img)}\n现有标签列表：\n{json.dumps(tags, ensure_ascii=False)}{extra}"},
                ],
            }
        else:
            user_prompt = {"role": "user", "content": f"图片文件：{os.path.basename(img)}\n现有标签列表：\n{json.dumps(tags, ensure_ascii=False)}{extra}"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                user_prompt,
            ],
            "temperature": 0.1,
        }
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=180)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            verdicts = json.loads(re.sub(r"^```json\s*|\s*```$", "", content.strip()))
            # B5：校验 verdicts 为 list（LLM 可能返回对象/字符串，前端 .filter 需要数组）
            if not isinstance(verdicts, list):
                verdicts = []
            results.append({"image": img, "ok": True, "tags": tags, "verdicts": verdicts})
        except Exception as e:
            results.append({"image": img, "ok": False, "error": str(e)})
    return {"ok": True, "provider": provider + ("+vllm" if provider == "lmstudio" and cfg.get("lmstudio", {}).get("vllm_enabled") else ""), "results": results}


# ---------- 设置 ----------
@app.get("/api/settings")
def get_settings():
    cfg = load_config()
    return {
        "deepseek": cfg.get("deepseek", {}),
        "lmstudio": cfg.get("lmstudio", {}),
        "screen_provider": cfg.get("screen_provider", "auto"),
        "save_concurrency": cfg.get("save_concurrency", 100),
        "preview_size": cfg.get("preview_size", 512),
        "recent_dirs": cfg.get("recent_dirs", []),
        "llm_prompts": cfg.get("llm_prompts", []),
        "llm_prompt_active": cfg.get("llm_prompt_active", -1),
        "font_size": cfg.get("font_size", "default"),
        "translate_concurrency": cfg.get("translate_concurrency", 3),
        "taglib_path": str(WEILIN_YAML) if WEILIN_YAML.exists() else "",
        "dict_path": str(DANBOORU_CSV) if DANBOORU_CSV.exists() else "",
    }


class SettingsReq(BaseModel):
    deepseek: dict = {}
    lmstudio: dict = {}
    screen_provider: str = ""
    save_concurrency: int = -1
    preview_size: int = -1
    llm_prompts: list = []        # 自定义 LLM 系统提示词 [{title, content}]
    llm_prompt_active: int = -99  # 当前选中提示词索引（-1=内置默认）
    font_size: str = ""           # 界面字号：sm / default / lg
    translate_concurrency: int = -1  # LLM 翻译并发数（1-8，默认 3）


@app.post("/api/settings")
def post_settings(req: SettingsReq):
    cfg = load_config()
    if req.deepseek:
        cfg["deepseek"].update(req.deepseek)
    if req.lmstudio:
        cfg["lmstudio"].update(req.lmstudio)
    if req.screen_provider:
        cfg["screen_provider"] = req.screen_provider
    if req.save_concurrency > 0:
        cfg["save_concurrency"] = req.save_concurrency
    if req.preview_size >= 0:
        cfg["preview_size"] = req.preview_size
    # 自定义 LLM 系统提示词：只保存标题非空的条目；active 越界时回退内置默认
    # G1 修复：空列表也允许清空（原 `and req.llm_prompts` 导致删除全部后旧条目复活）
    if req.llm_prompts is not None:
        cleaned = []
        for p in req.llm_prompts:
            if isinstance(p, dict) and (p.get("title") or "").strip():
                cleaned.append({"title": p["title"].strip(), "content": p.get("content", "")})
        cfg["llm_prompts"] = cleaned
    if req.llm_prompt_active != -99:
        active = req.llm_prompt_active
        if active < -1 or active >= len(cfg.get("llm_prompts", [])):
            active = -1  # 越界回退内置默认
        cfg["llm_prompt_active"] = active
    if req.font_size in ("xs", "sm", "default", "lg"):
        cfg["font_size"] = req.font_size
    if 1 <= req.translate_concurrency <= 8:
        cfg["translate_concurrency"] = req.translate_concurrency
    save_config_enc(cfg)
    return {"ok": True, "settings": cfg}


# ---------- 目录浏览（盘符/子目录） ----------
@app.get("/api/drives")
def get_drives():
    drives = []
    import string
    for letter in string.ascii_uppercase:
        p = f"{letter}:\\"
        if os.path.isdir(p):
            drives.append(p)
    return {"ok": True, "drives": drives}


@app.get("/api/listdir")
def listdir(path: str = ""):
    if not path:
        return JSONResponse({"error": "缺少路径"}, status_code=400)
    # 过滤系统隐藏目录：$ 开头（$Recycle.Bin 等）、System Volume Information、隐藏属性
    import stat as stat_mod
    HIDDEN_NAMES = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws", "recycler", "recovery", "perflogs"}
    try:
        subs = []
        for d in os.listdir(path):
            full = os.path.join(path, d)
            if not os.path.isdir(full):
                continue
            low = d.lower()
            if low.startswith("$") or low in HIDDEN_NAMES:
                continue
            try:
                if os.stat(full).st_file_attributes & 0x2:  # FILE_ATTRIBUTE_HIDDEN
                    continue
            except Exception:
                pass
            subs.append(d)
        return {"ok": True, "path": path, "dirs": sorted(subs, key=str.lower)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# ---------- 词库查询（前端点选用） ----------
# 一级分类中文翻译（weilin 分类名是英文，前端一级菜单需要中文显示）
CAT_ZH = {
    "Person": "人物", "Apparel": "服饰", "Facial expression and action": "表情动作",
    "Image": "画风", "Environment": "环境", "Scene": "场景", "Items": "物品",
    "Camera": "镜头", "Hanfu": "汉服", "Negative Prompt": "负面提示词",
}


@app.get("/api/taglib")
def taglib(q: str = ""):
    """返回词库（weilin 风格分组），q 非空则按英文/中文模糊搜索，最多 300 条"""
    lib = load_tag_lib()
    if not lib:
        return {"ok": True, "items": [], "source": ""}
    items = lib
    if q.strip():
        ql = q.strip().lower()
        items = [t for t in lib if ql in t["en"].lower() or ql in t["zh"].lower()]
        items = items[:300]
    # 词典兜底：yaml 中无中文翻译时查 zh_extra/danbooru 词典（不跨目录，已封装在项目内）
    extra = load_zh_extra()
    d = load_zh_dict()
    for t in items:
        if not t["zh"]:
            t["zh"] = extra.get(t["en"]) or d.get(t["en"]) or d.get(t["en"].replace(" ", "_"), "")
        # 附带一级分类中文名（前端一级菜单用）
        t["cat_zh"] = CAT_ZH.get(t.get("cat", ""), t.get("cat", "其他"))
    return {"ok": True, "items": items, "source": str(WEILIN_YAML), "total": len(lib)}


# ---------- 静态前端 ----------
app.mount("/", StaticFiles(directory=str(RES_BASE / "static"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("LoRA Tag Manager 已启动: http://localhost:8765")
    print("=" * 50)
    uvicorn.run(app, host="127.0.0.1", port=8765)
