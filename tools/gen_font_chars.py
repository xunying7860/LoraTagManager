# -*- coding: utf-8 -*-
"""
字体子集化字符收集（L1-1：24.6MB TTF -> ~2MB woff2）
从 UI 文案 + 全部词库中文（taglib/zh_extra/danbooru）收集实际用到的字符，
拆成两份：
- ui_chars_ascii.txt  拉丁/符号（ASCII + 标点 + 常用符号）
- ui_chars_cjk.txt    CJK（汉字 + 全角标点 + 全角形式）
随后 pyftsubset 各子集化一份 woff2（构建期跑一次，产物提交仓库）。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAGLIB_DIR = ROOT / "taglib"


def collect_chars():
    chars = set()
    # 1. 前端 UI 全部文案（含 JS 字符串里的中文）
    chars |= set((ROOT / "static" / "index.html").read_text(encoding="utf-8"))
    # 2. 词库中文（chip zh-line 显示用）
    lib = json.loads((TAGLIB_DIR / "taglib.json").read_text(encoding="utf-8"))
    for t in lib:
        chars |= set(t.get("zh", ""))
    # 3. 子代理补齐翻译
    extra = json.loads((TAGLIB_DIR / "zh_extra.json").read_text(encoding="utf-8"))
    chars |= set("".join(extra.values()))
    # 4. danbooru 词典中文（chip/标签面板翻译兜底来源）
    db = json.loads((TAGLIB_DIR / "danbooru_zh.json").read_text(encoding="utf-8"))
    chars |= set("".join(db.values()))
    # 5. 后端面向用户的提示文案
    for py in ("server.py", "desktop.py"):
        chars |= set((ROOT / py).read_text(encoding="utf-8"))
    # 去掉空白与不可见字符
    chars = {c for c in chars if c.strip()}
    return chars


def main():
    chars = collect_chars()
    cjk = set()
    ascii_ = set()
    for c in chars:
        cp = ord(c)
        if (0x3000 <= cp <= 0x303F) or (0x3400 <= cp <= 0x4DBF) or (0x4E00 <= cp <= 0x9FFF) \
           or (0xF900 <= cp <= 0xFAFF) or (0xFF00 <= cp <= 0xFFEF):
            cjk.add(c)
        elif cp < 0x3000:
            ascii_.add(c)
        # 兼容区等其余 CJK 扩展归入 cjk（保险）
        else:
            cjk.add(c)
    (ROOT / "ui_chars_ascii.txt").write_text("".join(sorted(ascii_)), encoding="utf-8")
    (ROOT / "ui_chars_cjk.txt").write_text("".join(sorted(cjk)), encoding="utf-8")
    print(f"总字符 {len(chars)} | ascii {len(ascii_)} | cjk {len(cjk)}")


if __name__ == "__main__":
    main()
    sys.exit(0)
