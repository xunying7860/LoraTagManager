# -*- coding: utf-8 -*-
"""
LoRA 打标管理 WebUI —— 标准库 http.server 后端（v2 性能优化版）

相比 v1（FastAPI/uvicorn/pydantic/requests）的改动：
- P0-1  词库哈希索引 + cache.json 内存常驻（300 标签翻译 976ms → <1ms；cache 写穿+2s 延迟落盘）
- P0-2  /api/translate/bulk 一次性批量翻译（替代 300/批 GET 长 URL）
- P0-5  缩略图：JPEG draft 解码期降采样 + BILINEAR + immutable 缓存头 + 扫描后后台预热
- P0-6  扫描：SQLite 索引 + txt mtime/size 增量 + 后台任务 + 进度/分页接口
- P0-7  初筛：标签分批并发 LLM + ndjson 流式回推；/api/tags/batch 真正用上 save_concurrency
- L1-3  taglib.yaml/danbooru.csv 预编译为 JSON（去 PyYAML 运行时依赖，365ms→~20ms）
- L1-4  requests → urllib（标准库）
- L2-2  FastAPI/uvicorn/pydantic → http.server.ThreadingHTTPServer（import 534ms→~150ms）
接口路径与 JSON 契约与 v1 完全一致（前端零兼容成本），新增 /api/translate/bulk、
/api/scan/progress、/api/scan/result、/api/screen/stream。
"""
import atexit
import base64
import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import string
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------- 路径常量（Windows 原生路径） ----------
# PyInstaller 打包（frozen）时——可写数据（config/cache/backups）放 exe 同目录，
# 只读资源（taglib/static）放打包资源区 _MEIPASS；源码运行时两者同目录
import sys as _sys
if getattr(_sys, "frozen", False):
    APP_DIR = Path(_sys.executable).resolve().parent
    RES_DIR = Path(getattr(_sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent
    RES_DIR = APP_DIR

BASE_DIR = APP_DIR  # 可写数据目录（config/cache/backups/thumbs/index.db）
RES_BASE = RES_DIR  # 只读资源目录（taglib/static）
CONFIG_PATH = BASE_DIR / "config.json"
CACHE_PATH = BASE_DIR / "cache.json"
BACKUP_DIR = BASE_DIR / "backups"
THUMB_DIR = BASE_DIR / "thumbs"
DB_PATH = BASE_DIR / "index.db"
TAGLIB_JSON = RES_BASE / "taglib" / "taglib.json"
DANBOORU_JSON = RES_BASE / "taglib" / "danbooru_zh.json"
ZH_EXTRA_JSON = RES_BASE / "taglib" / "zh_extra.json"

IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".avif"}
SCANNED_DIRS = set()  # 已扫描目录白名单（/api/image /api/thumb 仅允许访问其中文件）

# ============================================================
# 配置读写（与 v1 完全一致：API Key XOR+base64 混淆加密）
# ============================================================
DEFAULT_CONFIG = {
    "deepseek": {"api_key": "", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    # vllm_enabled: 在 LM Studio 地址上启用 vLLM 模式（多模态输入 + 视觉版提示词），复用同一 URL/模型
    "lmstudio": {"base_url": "http://localhost:1234/v1", "model": "", "api_key": "lm-studio", "vllm_enabled": False},
    "screen_provider": "auto",  # 初筛引擎：auto=LM Studio 没填就云端 / lmstudio / deepseek
    "save_concurrency": 100,    # 全部保存的并发数（设置里可调）
    "preview_size": 256,        # 预览缩略图尺寸（P0-5：默认 512→256，面积 1/4 编码快 2~3x）
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
_KEY_PREFIX = "enc:"  # 加密标记前缀：幂等保护——已加密的 key 不再重复加密


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
    for sec in ("deepseek", "lmstudio"):
        sec_cfg = cfg.get(sec) or {}
        if sec_cfg.get("api_key"):
            sec_cfg["api_key"] = _encrypt_key(sec_cfg["api_key"])


def _cfg_key_decrypt(cfg):
    for sec in ("deepseek", "lmstudio"):
        sec_cfg = cfg.get(sec) or {}
        if sec_cfg.get("api_key"):
            sec_cfg["api_key"] = _decrypt_key(sec_cfg["api_key"])


def save_config_enc(cfg):
    """带加密的保存：只写 config.json 本体，不产生任何备份副本"""
    cfg = json.loads(json.dumps(cfg))  # 深拷贝，不污染内存明文
    _cfg_key_encrypt(cfg)
    save_config(cfg)


# ============================================================
# 翻译缓存（P0-1：内存常驻 + 写穿 + 2s 延迟落盘）
# v1 每个未命中标签都 read+json.loads 整个 cache.json —— 300 标签 970ms 空转
# ============================================================
_cache_mem = {}
_cache_lock = threading.Lock()
_cache_dirty = False


def _cache_load():
    global _cache_mem
    if CACHE_PATH.exists():
        try:
            _cache_mem = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            _cache_mem = {}
    return _cache_mem


def save_cache(cache=None):
    """写穿：更新内存即生效，落盘交给后台 flusher（不再每次翻译全量重写）"""
    global _cache_mem, _cache_dirty
    with _cache_lock:
        if cache is not None:
            _cache_mem = cache
        _cache_dirty = True


def _cache_flusher():
    global _cache_dirty
    while True:
        time.sleep(2)
        if _cache_dirty:
            with _cache_lock:
                try:
                    CACHE_PATH.write_text(json.dumps(_cache_mem, ensure_ascii=False), encoding="utf-8")
                    _cache_dirty = False
                except Exception as e:
                    print(f"[warn] cache 落盘失败: {e}")


def flush_cache_now():
    global _cache_dirty
    with _cache_lock:
        if _cache_dirty:
            try:
                CACHE_PATH.write_text(json.dumps(_cache_mem, ensure_ascii=False), encoding="utf-8")
                _cache_dirty = False
            except Exception as e:
                print(f"[warn] cache 退出落盘失败: {e}")


# ============================================================
# 词库（P0-1：预编译 JSON + 哈希索引，启动预建）
# 优先级与 v1 完全一致：词库(zh非空) > zh_extra > danbooru > 翻译缓存 > 原文
# ============================================================
_tag_lib = []      # 全部条目 [{en, zh, group, cat, color}]（/api/taglib 用）
_tag_index = {}    # en -> 条目（lookup_style O(1)）
_zh_extra = {}     # en -> zh
_zh_dict = {}      # en -> zh（danbooru）
_idx_built = False
_idx_lock = threading.Lock()


def _build_indexes():
    global _tag_lib, _tag_index, _zh_extra, _zh_dict, _idx_built
    if _idx_built:
        return
    with _idx_lock:
        if _idx_built:
            return
        # 1) 词库：预编译 taglib.json（构建期由 tools/prebuild_taglib.py 生成）
        try:
            if TAGLIB_JSON.exists():
                for t in json.loads(TAGLIB_JSON.read_text(encoding="utf-8")):
                    _tag_lib.append(t)
                    _tag_index[t["en"]] = t
            else:
                # 兜底：taglib.json 缺失时尝试 yaml（若环境有 PyYAML）
                import yaml
                data = yaml.safe_load((RES_BASE / "taglib" / "taglib.yaml").read_text(encoding="utf-8"))
                for cat in data or []:
                    cat_name = cat.get("name", "")
                    for grp in cat.get("groups", []) or []:
                        for en, zh in (grp.get("tags", {}) or {}).items():
                            t = {"en": en, "zh": zh if zh else "", "group": grp.get("name", ""),
                                 "cat": cat_name, "color": grp.get("color", "")}
                            _tag_lib.append(t)
                            _tag_index[t["en"]] = t
        except Exception as e:
            print(f"[warn] 标签库加载失败: {e}")
        # 2) 二级翻译词库
        try:
            if ZH_EXTRA_JSON.exists():
                _zh_extra = json.loads(ZH_EXTRA_JSON.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] zh_extra.json 加载失败: {e}")
        try:
            if DANBOORU_JSON.exists():
                _zh_dict = json.loads(DANBOORU_JSON.read_text(encoding="utf-8"))
            else:
                with open(RES_BASE / "taglib" / "danbooru_zh.csv", encoding="utf-8-sig", newline="") as f:
                    for row in csv.reader(f):
                        if len(row) >= 2 and row[0].strip():
                            _zh_dict[row[0].strip()] = row[1].strip()
        except Exception as e:
            print(f"[warn] danbooru 词库加载失败: {e}")
        # 3) 翻译缓存常驻内存
        _cache_load()
        _idx_built = True


def ensure_indexes():
    _build_indexes()


def lookup_zh(en):
    """翻译查找：标签库(zh非空) > zh_extra > danbooru > 翻译缓存 > 原文（全 O(1)）"""
    ensure_indexes()
    en = (en or "").strip()
    if not en:
        return en
    t = _tag_index.get(en)
    if t and t["zh"]:
        return t["zh"]
    if en in _zh_extra:
        return _zh_extra[en]
    if en in _zh_dict:
        return _zh_dict[en]
    # danbooru 词典用下划线命名（long_hair），打标常用空格（long hair），做规范化匹配
    norm = en.replace(" ", "_")
    if norm != en and norm in _zh_dict:
        return _zh_dict[norm]
    c = _cache_mem.get(en)
    return c if c else en


def lookup_style(en):
    """标签风格信息：匹配到词库返回分组+颜色，未命中返回默认灰（O(1)）"""
    ensure_indexes()
    t = _tag_index.get((en or "").strip())
    if t:
        return {"group": t["group"], "cat": t["cat"], "color": t["color"], "matched": True}
    return {"group": "", "cat": "", "color": "", "matched": False}


# ============================================================
# 标签解析 / 写回 / 备份
# ============================================================
def parse_tags(text):
    """解析打标 txt：按中英文逗号、换行分割，去空"""
    parts = re.split(r"[,，\n]", text)
    return [p.strip() for p in parts if p.strip()]


def backup_file(path):
    """覆写前备份到 backups/ 目录（保留时间戳；同秒重名自动加序号防并发覆盖）"""
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"{Path(path).name}.{ts}.bak"
    n = 0
    while dst.exists():
        n += 1
        dst = BACKUP_DIR / f"{Path(path).name}.{ts}_{n}.bak"
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


def _read_tags_txt(txt_path):
    """读 txt 标签（utf-8 优先，gbk 兜底——与 v1 一致）"""
    try:
        return parse_tags(Path(txt_path).read_text(encoding="utf-8"))
    except Exception:
        try:
            return parse_tags(Path(txt_path).read_text(encoding="gbk"))
        except Exception:
            return []


# ============================================================
# SQLite 扫描索引（P0-6：txt mtime/size 增量，二次扫描免读 txt）
# images(path PK, dir, txt_mtime, txt_size, tags_json, has_txt)
# ============================================================
_db = None
_db_lock = threading.Lock()


def _get_db():
    global _db
    if _db is None:
        _db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA synchronous=NORMAL")
        _db.execute("""CREATE TABLE IF NOT EXISTS images(
            path TEXT PRIMARY KEY, dir TEXT, txt_mtime REAL, txt_size INTEGER,
            tags_json TEXT, has_txt INT)""")
        _db.execute("CREATE INDEX IF NOT EXISTS idx_images_dir ON images(dir)")
        _db.commit()
    return _db


def _index_get_known(folder):
    """取该目录全部已知行：path -> (txt_mtime, txt_size, tags_json, has_txt)"""
    with _db_lock:
        cur = _get_db().execute(
            "SELECT path, txt_mtime, txt_size, tags_json, has_txt FROM images WHERE dir = ?", (folder,))
        return {r[0]: r[1:] for r in cur.fetchall()}


def _index_batch(rows):
    """rows: [(path, dir, txt_mtime, txt_size, tags_json, has_txt), ...]"""
    if not rows:
        return
    with _db_lock:
        _get_db().executemany(
            "INSERT OR REPLACE INTO images(path, dir, txt_mtime, txt_size, tags_json, has_txt) VALUES(?,?,?,?,?,?)",
            rows)
        _get_db().commit()


def _index_delete_missing(folder, found_paths):
    """清理该目录下已不存在的图片行"""
    with _db_lock:
        db = _get_db()
        cur = db.execute("SELECT path FROM images WHERE dir = ?", (folder,))
        stale = [r[0] for r in cur.fetchall() if r[0] not in found_paths]
        if stale:
            db.executemany("DELETE FROM images WHERE path = ?", [(p,) for p in stale])
            db.commit()


def _index_touch(img_path, tags):
    """保存/批量/恢复后刷新索引行（保持 dir 不变；无行则新建）"""
    txt = find_txt_for_image(img_path)
    tm = ts = None
    if txt and os.path.exists(txt):
        try:
            st = os.stat(txt)
            tm, ts = st.st_mtime, st.st_size
        except OSError:
            pass
    tags_json = json.dumps(tags, ensure_ascii=False)
    has_txt = 1 if (txt and os.path.exists(txt)) else 0
    with _db_lock:
        db = _get_db()
        cur = db.execute("SELECT dir FROM images WHERE path = ?", (img_path,))
        row = cur.fetchone()
        if row:
            db.execute("UPDATE images SET txt_mtime=?, txt_size=?, tags_json=?, has_txt=? WHERE path=?",
                       (tm, ts, tags_json, has_txt, img_path))
        else:
            db.execute("INSERT INTO images(path, dir, txt_mtime, txt_size, tags_json, has_txt) VALUES(?,?,?,?,?,?)",
                       (img_path, os.path.dirname(img_path) or "", tm, ts, tags_json, has_txt))
        db.commit()


# ============================================================
# 扫描（P0-6：后台任务 + 进度 + 分页；增量复用 SQLite 缓存）
# ============================================================
_scans = {}
_scans_lock = threading.Lock()


def _scan_rec(scan_id):
    with _scans_lock:
        return _scans.get(scan_id)


def start_scan(folder):
    """启动后台扫描，立即返回 scan_id（前端轮询 progress 再分页取 result）"""
    folder = (folder or "").strip().strip('"')
    if not folder or not os.path.isdir(folder):
        return 400, {"error": f"文件夹不存在: {folder}"}
    # 记录最近目录
    cfg = load_config()
    if folder not in cfg["recent_dirs"]:
        cfg["recent_dirs"].insert(0, folder)
        cfg["recent_dirs"] = cfg["recent_dirs"][:10]
        save_config_enc(cfg)
    SCANNED_DIRS.add(os.path.abspath(folder))  # 提前授权：扫描期间缩略图/原图即可访问
    scan_id = uuid.uuid4().hex[:12]
    with _scans_lock:
        _scans[scan_id] = {"status": "running", "done": 0, "total": 0, "error": "", "result": None,
                           "_t": time.time()}
        # 只保留最近 5 次扫描结果（防内存无限增长）
        if len(_scans) > 5:
            oldest = min(_scans, key=lambda k: _scans[k].get("_t", 0))
            _scans.pop(oldest, None)
    threading.Thread(target=_run_scan, args=(scan_id, folder), daemon=True).start()
    return 200, {"ok": True, "scan_id": scan_id}


def _run_scan(scan_id, folder):
    rec = _scan_rec(scan_id)
    if rec is None:
        return
    try:
        known = _index_get_known(folder)
        # 第一遍：收集全部图片文件（稳定排序，与 v1 一致：目录/文件名小写序）
        file_list = []
        walker = sorted(os.walk(folder), key=lambda t: t[0].lower())
        for root, dirs, files in walker:
            dirs.sort(key=str.lower)
            for name in sorted(files, key=str.lower):
                if os.path.splitext(name)[1].lower() in IMG_EXTS:
                    file_list.append(os.path.join(root, name))
        rec["total"] = len(file_list)
        # 第二遍：逐图取标签（txt mtime/size 未变 → 直接复用索引缓存，免读 txt）
        images = []
        tag_counter = Counter()
        total_txt = 0
        rows = []
        found = set()
        for i, full in enumerate(file_list):
            if not os.path.isfile(full):
                continue
            found.add(full)
            txt = None
            for e in (".txt", ".caption"):
                # 替换扩展名（v1 语义：with_suffix），不是拼接（拼接会得 img.jpg.txt）
                cand = str(Path(full).with_suffix(e))
                if os.path.isfile(cand):
                    txt = cand
                    break
            tm = ts = None
            if txt:
                try:
                    st = os.stat(txt)
                    tm, ts = st.st_mtime, st.st_size
                except OSError:
                    pass
            key = known.get(full)
            if key is not None and key[0] == tm and key[1] == ts:
                # 增量命中：txt 未变（或从无 txt），复用缓存标签；已入库无需回写
                tags = json.loads(key[2]) if key[2] else []
                has_txt = bool(key[3]) and txt is not None
            else:
                tags = _read_tags_txt(txt) if txt else []
                has_txt = txt is not None
            if txt:
                total_txt += 1
            for t in tags:
                tag_counter[t] += 1
            # 只有未命中（新增/变更）才回写索引：纯增量扫描免全量 upsert（7000 图数据集省 ~1s）
            if key is None or key[0] != tm or key[1] != ts:
                rows.append((full, folder, tm, ts, json.dumps(tags, ensure_ascii=False), 1 if has_txt else 0))
            images.append({
                "name": os.path.basename(full),
                "path": full,
                "txt": txt,
                "has_txt": has_txt,
                "tags": tags,
                "count": len(tags),
            })
            rec["done"] = i + 1
        n_img = len(images)
        # 共同标签：出现次数 == 有 txt 图片数（且 >=1）
        common = sorted([t for t, c in tag_counter.items() if c == total_txt and total_txt > 0])
        # 全部标签统计（按频率降序）
        tag_stats = [{"tag": t, "count": c, "freq": round(c / n_img, 3) if n_img else 0}
                     for t, c in tag_counter.most_common()]
        # 写回索引（批量一次事务）+ 清理已删除图片
        _index_batch(rows)
        _index_delete_missing(folder, found)
        rec["result"] = {
            "folder": folder,
            "images": images,
            "tag_stats": tag_stats,
            "common_tags": common,
            "total_images": n_img,
            "total_txt": total_txt,
            "total_tags": len(tag_stats),
        }
        rec["status"] = "done"
        # 扫描完成：后台预热缩略图（不等用户滚到才生成，P0-5）
        _prewarm_thumbs([im["path"] for im in images])
    except Exception as e:
        rec["status"] = "error"
        rec["error"] = str(e)


def scan_progress(scan_id):
    rec = _scan_rec(scan_id)
    if rec is None:
        return 404, {"error": "扫描不存在或已过期"}
    return 200, {"ok": True, "status": rec["status"], "done": rec["done"],
                 "total": rec["total"], "error": rec["error"]}


def scan_result(scan_id, offset, limit):
    """分页取扫描结果：元数据（tag_stats/common_tags）+ 图片页"""
    rec = _scan_rec(scan_id)
    if rec is None:
        return 404, {"error": "扫描不存在或已过期"}
    if rec["status"] != "done" or not rec["result"]:
        return 200, {"ok": False, "status": rec["status"], "error": rec["error"]}
    r = rec["result"]
    images = r["images"]
    page = images[offset:offset + limit]
    return 200, {
        "ok": True,
        "folder": r["folder"],
        "tag_stats": r["tag_stats"],
        "common_tags": r["common_tags"],
        "total_images": r["total_images"],
        "total_txt": r["total_txt"],
        "total_tags": r["total_tags"],
        "images": page,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(images),
    }


# ============================================================
# 缩略图（P0-5：draft 解码期降采样 + BILINEAR + immutable 缓存头 + 并发限流）
# ============================================================
THUMB_SEM = threading.Semaphore(4)  # 限制并发解码，别把 CPU 打满


def make_thumb_bytes(path, size):
    """生成缩略图字节（size×size 居中裁剪）"""
    from PIL import Image
    import io
    with Image.open(path) as im:
        try:
            im.draft("RGB", (size * 2, size * 2))  # JPEG 直接在解码期降采样（实测 1.5x）
        except Exception:
            pass
        im = im.convert("RGB")
        w, h = im.size
        s = min(w, h)
        im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
        im = im.resize((size, size), Image.BILINEAR)  # 256~512px 下与 LANCZOS 肉眼无差
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue()


def get_thumb_bytes(path, size):
    """取缩略图字节：缓存命中直接读；未命中生成（.tmp 原子替换防并发半截文件）"""
    size = max(64, min(size, 2048))
    key = hashlib.md5(path.encode("utf-8")).hexdigest()[:16]
    THUMB_DIR.mkdir(exist_ok=True)
    cache_path = THUMB_DIR / f"{key}_{size}.jpg"
    if cache_path.exists():
        return cache_path.read_bytes()
    with THUMB_SEM:
        if cache_path.exists():
            return cache_path.read_bytes()  # 双检：别的线程可能已生成
        tmp = THUMB_DIR / f"{key}_{size}.tmp"
        try:
            data = make_thumb_bytes(path, size)
            tmp.write_bytes(data)
            os.replace(tmp, cache_path)  # 原子替换
            return data
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


def _prewarm_thumbs(paths, limit=500):
    """扫描完成后后台预热缩略图（按当前 preview_size；0=原图模式则跳过）
    限量预热前 limit 张（约 80 行网格，够用户滚动好几分钟）：
    全量预热 7000 图会抢磁盘/CPU，拖慢紧随其后的二次扫描；其余按需生成（2~16ms/张，永久缓存）"""
    if not paths:
        return
    cfg = load_config()
    size = int(cfg.get("preview_size", 0) or 0)
    if size <= 0:
        return
    paths = paths[:limit]

    def run():
        with ThreadPoolExecutor(max_workers=4) as ex:
            def one(p):
                try:
                    get_thumb_bytes(p, size)
                except Exception:
                    pass
            list(ex.map(one, paths))

    threading.Thread(target=run, daemon=True).start()


# ============================================================
# LLM HTTP（L1-4：requests → urllib 标准库）
# ============================================================
def llm_post(url, payload, headers, timeout=120):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ============================================================
# 初筛（P0-7：标签分批并发 + 流式回推；提示词与 v1 完全一致）
# ============================================================
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
    import io
    im = Image.open(img_path)
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side))
    if im.mode != "RGB":
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _screen_resolve(cfg, req_model, system_prompt):
    """解析初筛引擎参数：返回 (provider_tag, url, headers, model, sys_prompt, vllm_mode)；
    失败时返回 (None, code, msg)"""
    provider = _pick_screen_provider(cfg)
    vllm_mode = False
    if provider == "deepseek":
        ds = cfg.get("deepseek", {})
        if not (ds.get("api_key") or "").strip():
            return None, 400, "云端初筛需要 DeepSeek API Key（请在设置中配置）"
        url = (ds.get("base_url") or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {ds['api_key']}", "Content-Type": "application/json"}
        model = ds.get("model") or "deepseek-chat"
        sys_prompt = system_prompt or SCREEN_SYSTEM_PROMPT
    else:  # lmstudio（含 vLLM 模式：同一地址，仅换提示词与输入条件）
        lm = cfg.get("lmstudio", {})
        vllm_mode = bool(lm.get("vllm_enabled"))
        url = (lm.get("base_url") or "http://localhost:1234/v1").rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {lm.get('api_key', 'lm-studio')}", "Content-Type": "application/json"}
        model = req_model or lm.get("model", "")
        sys_prompt = system_prompt or (SCREEN_SYSTEM_PROMPT_VLLM if vllm_mode else SCREEN_SYSTEM_PROMPT)
    provider_tag = provider + ("+vllm" if provider == "lmstudio" and cfg.get("lmstudio", {}).get("vllm_enabled") else "")
    return provider_tag, url, headers, model, sys_prompt, vllm_mode


def _screen_call(url, headers, model, sys_prompt, img_path, img_name, tags, user_prompt, vllm_mode):
    """单次 LLM 初筛调用（一个批次或一张图）"""
    extra = f"\n\n用户附加要求：{user_prompt}" if user_prompt else ""
    image_data = None
    if vllm_mode and img_path:
        try:
            image_data = _image_data_url(img_path)
        except Exception:
            image_data = None
    if image_data is not None:
        user_content = [
            {"type": "image_url", "image_url": {"url": image_data}},
            {"type": "text", "text": f"图片文件：{img_name}\n现有标签列表：\n{json.dumps(tags, ensure_ascii=False)}{extra}"},
        ]
    else:
        user_content = f"图片文件：{img_name}\n现有标签列表：\n{json.dumps(tags, ensure_ascii=False)}{extra}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
    }
    r = llm_post(url, payload, headers, timeout=180)
    content = r["choices"][0]["message"]["content"]
    verdicts = json.loads(re.sub(r"^```json\s*|\s*```$", "", content.strip()))
    if not isinstance(verdicts, list):
        verdicts = []
    return verdicts


def _screen_jobs(images, tags, user_prompt, model, sys_prompt, vllm_mode):
    """构造初筛任务列表：
    - tags 非空（批量判定模式）：每 100 个标签一批（防 prompt 超限），image 取 images[0]
    - tags 为空（逐图模式）：每张图读自己的 txt 判定
    返回 [(img_path, img_name, tag_list), ...]
    """
    jobs = []
    if tags:
        img0 = images[0] if images else ""
        for i in range(0, len(tags), 100):
            jobs.append((img0, os.path.basename(img0) if img0 else "", tags[i:i + 100]))
    else:
        for img in images:
            if not os.path.isfile(img):
                continue
            txt = find_txt_for_image(img)
            t = parse_tags(Path(txt).read_text(encoding="utf-8")) if txt else []
            jobs.append((img, os.path.basename(img), t))
    return jobs


def screen(cfg, images, tags, user_prompt, system_prompt, model):
    """非流式初筛（v1 契约）：内部并发执行，聚合返回"""
    resolved = _screen_resolve(cfg, model, system_prompt)
    if resolved[0] is None:
        return 400, {"error": resolved[2]}
    provider, url, headers, model, sys_prompt, vllm_mode = resolved
    jobs = _screen_jobs(images, tags, user_prompt, model, sys_prompt, vllm_mode)

    def one(job):
        img, name, t = job
        if not t:
            return {"image": img, "ok": True, "tags": [], "verdicts": []}
        try:
            verdicts = _screen_call(url, headers, model, sys_prompt, img, name, t, user_prompt, vllm_mode)
            return {"image": img, "ok": True, "tags": t, "verdicts": verdicts}
        except Exception as e:
            return {"image": img, "ok": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(one, jobs))
    return 200, {"ok": True, "provider": provider, "results": results}


def screen_stream_iter(cfg, images, tags, user_prompt, system_prompt, model):
    """流式初筛：并发执行、按批次顺序产出（ndjson 行），供 /api/screen/stream。
    返回 (generator, None)；参数错误返回 (None, errmsg)"""
    resolved = _screen_resolve(cfg, model, system_prompt)
    if resolved[0] is None:
        return None, resolved[2]
    provider, url, headers, model, sys_prompt, vllm_mode = resolved
    jobs = _screen_jobs(images, tags, user_prompt, model, sys_prompt, vllm_mode)
    total = len(jobs)

    def one(i, job):
        img, name, t = job
        if not t:
            return {"image": img, "ok": True, "tags": [], "verdicts": [],
                    "batch": i, "total": total, "provider": provider}
        try:
            verdicts = _screen_call(url, headers, model, sys_prompt, img, name, t, user_prompt, vllm_mode)
            return {"image": img, "ok": True, "tags": t, "verdicts": verdicts,
                    "batch": i, "total": total, "provider": provider}
        except Exception as e:
            return {"image": img, "ok": False, "error": str(e),
                    "batch": i, "total": total, "provider": provider}

    def gen():
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(one, i, j) for i, j in enumerate(jobs)]
            # 并发执行、按提交顺序产出（前端进度条稳定）
            for f in futs:
                yield f.result()

    return gen, None  # 返回生成器函数，由 handler 调用 gen() 迭代


# ============================================================
# API：扫描 / 最近目录 / 覆写 / 批量 / 恢复
# ============================================================
def remember_folder(folder):
    """缓存上次填写的路径：输入框失焦/回车时记录（不依赖扫描成功）"""
    folder = (folder or "").strip().strip('"')
    if not folder:
        return 200, {"ok": True}
    cfg = load_config()
    if folder not in cfg["recent_dirs"]:
        cfg["recent_dirs"].insert(0, folder)
    else:
        cfg["recent_dirs"].remove(folder)
        cfg["recent_dirs"].insert(0, folder)
    cfg["recent_dirs"] = cfg["recent_dirs"][:10]
    save_config_enc(cfg)
    return 200, {"ok": True}


def update(body):
    """单图覆写：覆写 txt（自动备份）"""
    img = body.get("image", "")
    tags = body.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    if not img or not os.path.isfile(img):
        return 400, {"error": "图片不存在"}
    txt = find_txt_for_image(img)
    if not txt:
        txt = str(Path(img).with_suffix(".txt"))
    backup_path = ""
    if os.path.exists(txt):
        backup_path = backup_file(txt)
    Path(txt).write_text(", ".join(tags), encoding="utf-8")
    _index_touch(img, tags)
    return 200, {"ok": True, "txt": txt, "backup": backup_path, "tags": tags}


def batch(body):
    """批量增删（覆写）：P0-7 真正并发（save_concurrency 落地，v1 是串行 for）"""
    images = body.get("images", [])
    remove = body.get("remove", []) or []
    add = body.get("add", []) or []
    if not isinstance(images, list):
        images = []
    remove_set = set(remove)

    def one(img):
        if not os.path.isfile(img):
            return {"image": img, "ok": False, "error": "图片不存在"}
        txt = find_txt_for_image(img)
        if not txt:
            txt = str(Path(img).with_suffix(".txt"))
        if os.path.exists(txt):
            tags = parse_tags(Path(txt).read_text(encoding="utf-8"))
        else:
            tags = []
        # 删除
        new_tags = [t for t in tags if t not in remove_set]
        # 添加（去重、保留原顺序）
        for t in add:
            if t not in new_tags:
                new_tags.append(t)
        backup_path = ""
        if os.path.exists(txt):
            backup_path = backup_file(txt)
        Path(txt).write_text(", ".join(new_tags), encoding="utf-8")
        _index_touch(img, new_tags)
        return {"image": img, "ok": True, "txt": txt, "backup": backup_path, "tags": new_tags}

    cfg = load_config()
    n = max(1, min(16, int(cfg.get("save_concurrency", 8) or 8)))
    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(one, images))
    return 200, {"ok": True, "results": results}


def restore(body):
    """从备份恢复（撤销已保存的覆写）"""
    items = body.get("items", [])
    if not isinstance(items, list):
        items = []
    results = []
    for it in items:
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
        try:
            tags = parse_tags(Path(txt).read_text(encoding="utf-8"))
        except Exception:
            tags = []
        _index_touch(img, tags)
        results.append({"image": img, "ok": True, "txt": txt, "tags": tags})
    return 200, {"ok": True, "results": results}


# ============================================================
# API：翻译（词库离线匹配 / 批量 / LLM）
# ============================================================
def translate_dict(tags_str):
    """v1 兼容：tags 逗号分隔（单标签自动翻译用）"""
    tag_list = [t.strip() for t in (tags_str or "").split(",") if t.strip()]
    out = []
    for t in tag_list:
        style = lookup_style(t)
        zh = lookup_zh(t)
        if zh == t and style["matched"]:
            zh = ""
        out.append({"en": t, "zh": zh if zh != t else "", **style})
    return 200, {"ok": True, "items": out}


def translate_bulk(body):
    """P0-2：一次性批量翻译（替代 300/批 GET 长 URL；300 标签 <1ms）"""
    tags = body.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    out = []
    for t in tags:
        if not isinstance(t, str) or not t.strip():
            continue
        style = lookup_style(t)
        zh = lookup_zh(t)
        if zh == t and style["matched"]:
            zh = ""
        out.append({"en": t, "zh": zh if zh != t else "", **style})
    return 200, {"ok": True, "items": out}


def translate_llm(body):
    """DeepSeek LLM 翻译（批量，缓存；v1 契约不变，requests→urllib）"""
    cfg = load_config()
    ds = cfg.get("deepseek", {})
    api_key = ds.get("api_key", "").strip()
    if not api_key:
        return 400, {"error": "未配置 DeepSeek API Key，请在设置中填写"}
    tags = body.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    to_en = bool(body.get("to_en", False))
    user_prompt = body.get("user_prompt", "") or ""

    # 只翻译缓存未命中的
    todo = [t for t in tags if t and t not in _cache_mem]
    if not todo:
        return 200, {"ok": True, "translated": [], "hit_cache": len([t for t in tags if t in _cache_mem])}

    # 分批（每批 50 个标签）+ 并发请求（并发数可调：translate_concurrency，默认 3）
    batch_size = 50
    result_map = {}
    url = ds.get("base_url", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    concurrency = max(1, min(8, int(cfg.get("translate_concurrency", 3) or 3)))
    chunks = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]

    def translate_chunk(chunk):
        if to_en:
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
        if user_prompt:
            # 用户自定义提示词附加（编辑条 LLM 用户提示词框），放在标签列表之后
            prompt += f"\n\n用户附加要求：{user_prompt}"
        payload = {
            "model": ds.get("model", "deepseek-chat"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        r = llm_post(url, payload, headers, timeout=120)
        content = r["choices"][0]["message"]["content"]
        return json.loads(content)

    # 并发请求各批（可调并发数），失败批不阻塞其他批
    errors = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(translate_chunk, c) for c in chunks]
        for f in futs:
            try:
                result_map.update(f.result())
            except Exception as e:
                errors.append(str(e))
    # 成功的先写缓存（部分失败也不浪费；写穿内存，2s 落盘）
    if result_map:
        with _cache_lock:
            _cache_mem.update(result_map)
        save_cache()
    if errors:
        return 502, {"error": f"翻译部分失败（{len(errors)}/{len(chunks)} 批）：{errors[0][:120]}"}
    return 200, {"ok": True, "translated": result_map, "hit_cache": 0}


# ============================================================
# API：设置 / 目录浏览 / 词库
# ============================================================
def get_settings():
    cfg = load_config()
    return 200, {
        "deepseek": cfg.get("deepseek", {}),
        "lmstudio": cfg.get("lmstudio", {}),
        "screen_provider": cfg.get("screen_provider", "auto"),
        "save_concurrency": cfg.get("save_concurrency", 100),
        "preview_size": cfg.get("preview_size", 256),
        "recent_dirs": cfg.get("recent_dirs", []),
        "llm_prompts": cfg.get("llm_prompts", []),
        "llm_prompt_active": cfg.get("llm_prompt_active", -1),
        "font_size": cfg.get("font_size", "default"),
        "translate_concurrency": cfg.get("translate_concurrency", 3),
        "taglib_path": str(TAGLIB_JSON) if TAGLIB_JSON.exists() else "",
        "dict_path": str(DANBOORU_JSON) if DANBOORU_JSON.exists() else "",
    }


def post_settings(body):
    cfg = load_config()
    if isinstance(body.get("deepseek"), dict) and body.get("deepseek"):
        cfg["deepseek"].update(body["deepseek"])
    if isinstance(body.get("lmstudio"), dict) and body.get("lmstudio"):
        cfg["lmstudio"].update(body["lmstudio"])
    if body.get("screen_provider"):
        cfg["screen_provider"] = body["screen_provider"]
    if int(body.get("save_concurrency", -1) or -1) > 0:
        cfg["save_concurrency"] = int(body["save_concurrency"])
    # preview_size 允许 0（0=原图模式）：不能用 `or -1`（0 是 falsy 会被吞）
    ps = body.get("preview_size", None)
    if isinstance(ps, (int, float)) and not isinstance(ps, bool) and ps >= 0:
        cfg["preview_size"] = int(ps)
    # 自定义 LLM 系统提示词：只保存标题非空的条目；active 越界时回退内置默认
    # G1 修复：空列表也允许清空（原 `and req.llm_prompts` 导致删除全部后旧条目复活）
    if body.get("llm_prompts") is not None:
        cleaned = []
        for p in body["llm_prompts"]:
            if isinstance(p, dict) and (p.get("title") or "").strip():
                cleaned.append({"title": p["title"].strip(), "content": p.get("content", "")})
        cfg["llm_prompts"] = cleaned
    if body.get("llm_prompt_active", -99) != -99:
        active = body["llm_prompt_active"]
        if active < -1 or active >= len(cfg.get("llm_prompts", [])):
            active = -1  # 越界回退内置默认
        cfg["llm_prompt_active"] = active
    if body.get("font_size") in ("xs", "sm", "default", "lg"):
        cfg["font_size"] = body["font_size"]
    if 1 <= int(body.get("translate_concurrency", -1) or -1) <= 8:
        cfg["translate_concurrency"] = int(body["translate_concurrency"])
    save_config_enc(cfg)
    return 200, {"ok": True, "settings": cfg}


def get_drives():
    drives = []
    for letter in string.ascii_uppercase:
        p = f"{letter}:\\"
        if os.path.isdir(p):
            drives.append(p)
    return 200, {"ok": True, "drives": drives}


def listdir(path):
    if not path:
        return 400, {"error": "缺少路径"}
    # 过滤系统隐藏目录：$ 开头（$Recycle.Bin 等）、System Volume Information、隐藏属性
    HIDDEN_NAMES = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
                    "recycler", "recovery", "perflogs"}
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
        return 200, {"ok": True, "path": path, "dirs": sorted(subs, key=str.lower)}
    except Exception as e:
        return 400, {"error": str(e)}


# 一级分类中文翻译（weilin 分类名是英文，前端一级菜单需要中文显示）
CAT_ZH = {
    "Person": "人物", "Apparel": "服饰", "Facial expression and action": "表情动作",
    "Image": "画风", "Environment": "环境", "Scene": "场景", "Items": "物品",
    "Camera": "镜头", "Hanfu": "汉服", "Negative Prompt": "负面提示词",
}


def taglib(q=""):
    """返回词库（weilin 风格分组），q 非空则按英文/中文模糊搜索，最多 300 条"""
    ensure_indexes()
    lib = _tag_lib
    if not lib:
        return 200, {"ok": True, "items": [], "source": "", "total": 0}
    items = lib
    if q.strip():
        ql = q.strip().lower()
        items = [t for t in lib if ql in t["en"].lower() or ql in t["zh"].lower()]
        items = items[:300]
    # 词典兜底：yaml 中无中文翻译时查 zh_extra/danbooru 词典（不跨目录，已封装在项目内）
    out = []
    for t in items:
        tt = dict(t)  # 拷贝，不污染缓存条目
        if not tt["zh"]:
            tt["zh"] = _zh_extra.get(t["en"]) or _zh_dict.get(t["en"]) \
                or _zh_dict.get(t["en"].replace(" ", "_"), "")
        # 附带一级分类中文名（前端一级菜单用）
        tt["cat_zh"] = CAT_ZH.get(t.get("cat", ""), t.get("cat", "其他"))
        out.append(tt)
    return 200, {"ok": True, "items": out, "source": str(TAGLIB_JSON), "total": len(lib)}


# ============================================================
# HTTP 服务（L2-2：标准库 ThreadingHTTPServer，接口契约与 v1 一致）
# ============================================================
STATIC_DIR = (RES_BASE / "static").resolve()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive，避免每张缩略图重连
    server_version = "LTM/2.0"

    def log_message(self, *a):
        pass  # 等价 uvicorn --no-access-log：缩略图高频请求不写盘

    # ---------- 基础发送 ----------
    def _cors(self):
        # 前端翻译走 127.0.0.1、UI 走 localhost（域名分片连接池），跨源需要 CORS
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, code, ctype, payload, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _send_stream_file(self, code, ctype, size, fileobj, extra=None):
        """流式发文件（原图可能 30MB+，不整读进内存）"""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self._cors()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        while True:
            b = fileobj.read(1 << 20)
            if not b:
                break
            self.wfile.write(b)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _chunk(self, data):
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")

    def _chunk_end(self):
        self.wfile.write(b"0\r\n\r\n")

    # ---------- 方法分发 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if p.startswith("/api/"):
                self._route_get(p, q)
            else:
                self._serve_static(p)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        body = self._body()
        try:
            if p.startswith("/api/"):
                self._route_post(p, body)
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def _route_get(self, p, q):
        if p == "/api/settings":
            code, obj = get_settings()
        elif p == "/api/drives":
            code, obj = get_drives()
        elif p == "/api/listdir":
            code, obj = listdir(q.get("path", [""])[0])
        elif p == "/api/taglib":
            code, obj = taglib(q.get("q", [""])[0])
        elif p == "/api/translate/dict":
            code, obj = translate_dict(q.get("tags", [""])[0])
        elif p == "/api/translate/cache":
            code, obj = 200, {"ok": True, "cache": dict(_cache_mem)}
        elif p == "/api/scan/progress":
            code, obj = scan_progress(q.get("scan_id", [""])[0])
        elif p == "/api/scan/result":
            code, obj = scan_result(q.get("scan_id", [""])[0],
                                   int(q.get("offset", ["0"])[0] or 0),
                                   int(q.get("limit", ["200"])[0] or 200))
        elif p == "/api/image":
            self._serve_file(q.get("path", [""])[0])
            return
        elif p == "/api/thumb":
            self._serve_thumb(q.get("path", [""])[0], int(q.get("size", ["256"])[0] or 256))
            return
        else:
            self._json({"error": "not found"}, 404)
            return
        self._json(obj, code)

    def _route_post(self, p, body):
        if p == "/api/scan":
            code, obj = start_scan(body.get("folder", ""))
        elif p == "/api/recent":
            code, obj = remember_folder(body.get("folder", ""))
        elif p == "/api/tags/update":
            code, obj = update(body)
        elif p == "/api/tags/batch":
            code, obj = batch(body)
        elif p == "/api/restore":
            code, obj = restore(body)
        elif p == "/api/translate/bulk":
            code, obj = translate_bulk(body)
        elif p == "/api/translate/llm":
            code, obj = translate_llm(body)
        elif p == "/api/screen":
            cfg = load_config()
            code, obj = screen(cfg, body.get("images", []) or [], body.get("tags", []) or [],
                               body.get("user_prompt", "") or "", body.get("system_prompt", "") or "",
                               body.get("model", "") or "")
        elif p == "/api/screen/stream":
            self._screen_stream(body)
            return
        elif p == "/api/settings":
            code, obj = post_settings(body)
        else:
            self._json({"error": "not found"}, 404)
            return
        self._json(obj, code)

    # ---------- 文件服务 ----------
    def _serve_file(self, path):
        """原图访问（G4：仅允许已扫描目录内文件）"""
        if not path or not os.path.isfile(path):
            self._json({"error": "文件不存在"}, 404)
            return
        ap = os.path.abspath(path)
        if not any(ap.startswith(d + os.sep) or ap == d for d in SCANNED_DIRS):
            self._json({"error": "未授权的路径（请先扫描该目录）"}, 403)
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            self._send_stream_file(200, ctype, os.path.getsize(path), f,
                                   {"Cache-Control": "no-cache"})

    def _serve_thumb(self, path, size):
        """缩略图（immutable 长缓存：翻页/重开不再重新生成与下载）"""
        if not path or not os.path.isfile(path):
            self._json({"error": "文件不存在"}, 404)
            return
        ap = os.path.abspath(path)
        if not any(ap.startswith(d + os.sep) or ap == d for d in SCANNED_DIRS):
            self._json({"error": "未授权的路径（请先扫描该目录）"}, 403)
            return
        try:
            data = get_thumb_bytes(path, size)
        except Exception as e:
            self._json({"error": f"缩略图失败: {e}"}, 500)
            return
        self._send(200, "image/jpeg", data,
                   {"Cache-Control": "public, max-age=31536000, immutable"})

    def _serve_static(self, p):
        if p == "/":
            p = "/index.html"
        f = (STATIC_DIR / p.lstrip("/")).resolve()
        # 防路径穿越：必须落在 static/ 内
        if not str(f).startswith(str(STATIC_DIR) + os.sep):
            self._json({"error": "not found"}, 404)
            return
        if not f.is_file():
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or "javascript" in ctype or "json" in ctype or "svg" in ctype:
            ctype += "; charset=utf-8"
        data = f.read_bytes()
        # 字体/第三方库长缓存（内容不可变）；index.html 不缓存（更新立即生效）
        if p.startswith("/fonts/") or p.startswith("/vendor/"):
            cc = "public, max-age=31536000, immutable"
        else:
            cc = "no-cache"
        self._send(200, ctype, data, {"Cache-Control": cc})

    # ---------- 初筛流式（ndjson） ----------
    def _screen_stream(self, body):
        cfg = load_config()
        gen, err = screen_stream_iter(cfg, body.get("images", []) or [], body.get("tags", []) or [],
                                      body.get("user_prompt", "") or "", body.get("system_prompt", "") or "",
                                      body.get("model", "") or "")
        if gen is None:
            self._json({"error": err}, 400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self._cors()
        self.end_headers()
        try:
            for line in gen():
                self._chunk((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
            self._chunk_end()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            # 头部已发出无法再回 JSON 错误，只记日志（客户端会收到截断流）
            import traceback
            traceback.print_exc()


# ============================================================
# 入口
# ============================================================
def serve(port=8765, host="127.0.0.1"):
    # 启动即预建索引（taglib.json ~20ms + danbooru ~50ms + cache ~5ms），首请求零延迟
    _build_indexes()
    threading.Thread(target=_cache_flusher, daemon=True).start()
    atexit.register(flush_cache_now)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print("=" * 50)
    print(f"LoRA Tag Manager 已启动: http://localhost:{port}")
    print("=" * 50)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        flush_cache_now()
        if _db is not None:
            with _db_lock:
                _db.close()


if __name__ == "__main__":
    # 支持命令行指定端口（测试/多开用）：python server.py [port]
    _port = int(_sys.argv[1]) if len(_sys.argv) > 1 else 8765
    serve(_port)
