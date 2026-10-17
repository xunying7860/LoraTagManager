# -*- coding: utf-8 -*-
"""
构建期预编译词库（L1-3：去 PyYAML 运行时依赖）
- taglib/taglib.yaml  -> taglib/taglib.json   （扁平 [{en, zh, group, cat, color}]，与旧 load_tag_lib 输出一致）
- taglib/danbooru_zh.csv -> taglib/danbooru_zh.json （{en: zh}，utf-8-sig BOM 兼容）
产物提交进仓库；server.py 运行时只读 JSON（json.loads ~20ms，yaml.safe_load 365ms）。
yaml 源文件保留作单一事实来源，改词库后重跑本脚本即可。
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAGLIB_DIR = ROOT / "taglib"


def build_taglib():
    import yaml  # 仅构建期需要
    data = yaml.safe_load((TAGLIB_DIR / "taglib.yaml").read_text(encoding="utf-8"))
    flat = []
    for cat in data or []:
        cat_name = cat.get("name", "")
        for grp in cat.get("groups", []) or []:
            color = grp.get("color", "") or ""
            grp_name = grp.get("name", "")
            for en, zh in (grp.get("tags", {}) or {}).items():
                flat.append({
                    "en": en,
                    "zh": zh if zh else "",
                    "group": grp_name,
                    "cat": cat_name,
                    "color": color,
                })
    out = TAGLIB_DIR / "taglib.json"
    out.write_text(json.dumps(flat, ensure_ascii=False), encoding="utf-8")
    print(f"taglib.json: {len(flat)} 条 -> {out}")
    return flat


def build_danbooru():
    d = {}
    with open(TAGLIB_DIR / "danbooru_zh.csv", encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0].strip():
                d[row[0].strip()] = row[1].strip()
    out = TAGLIB_DIR / "danbooru_zh.json"
    out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    print(f"danbooru_zh.json: {len(d)} 条 -> {out}")
    return d


if __name__ == "__main__":
    build_taglib()
    build_danbooru()
    sys.exit(0)
