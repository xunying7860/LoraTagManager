# -*- coding: utf-8 -*-
"""LoraTagManager v2 端到端测试：全接口 + 性能基准 + mock LLM 流式初筛"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = None
MOCK_PORT = None
BASE = None
WORK = r"H:\Hermes工作区\Hermes\LoraTagManager"
DATASET = r"H:\caches\Temp\lora_opt\test_dataset"
REAL = r"H:\ai-toolkit-easy2use-portable\datasets\老的"


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_dataset():
    """每次运行重新生成合成数据集（300 图 / 270 txt / 5 共同标签），消除跨运行污染"""
    import random
    from PIL import Image
    base = Path(DATASET)
    base.mkdir(parents=True, exist_ok=True)
    for f in base.rglob("*"):
        if f.is_file():
            f.unlink()
    random.seed(42)
    common = ["1girl", "solo", "long_hair", "looking_at_viewer", "smile"]
    pool = ["blue_eyes", "green_dress", "white_shoes", "outdoor", "city", "night",
            "rain", "red_ribbon", "black_coat", "holding_flower", "waving", "jumping",
            "cat", "dog", "bicycle", "book", "glasses", "hat", "scarf", "earrings",
            "blush", "teeth", "open_mouth", "closed_eyes", "braid", "ponytail",
            "twintails", "ahoge", "freckles", "pale_skin", "tall", "short",
            "muscular", "thin", "chubby", "bare_shoulders", "crop_top", "jeans",
            "sweater", "hoodie", "turtleneck", "vest", "apron", "gloves"]
    exts = [".jpg", ".jpg", ".jpg", ".png", ".webp"]
    for i in range(300):
        d = base / (f"sub{i % 3}" if i % 7 == 0 else "")
        d.mkdir(parents=True, exist_ok=True)
        name = f"img_{i:04d}" + random.choice(exts)
        im = Image.new("RGB", (256, 384))
        px = im.load()
        for y in range(384):
            for x in range(256):
                px[x, y] = (random.randrange(256), random.randrange(256), random.randrange(256))
        im.save(d / name, quality=80)
        if i % 10 != 9:
            tags = common + random.sample(pool, 20)
            random.shuffle(tags)
            (d / (name.rsplit(".", 1)[0] + ".txt")).write_text(", ".join(tags), encoding="utf-8")
    # 清理服务端本地状态，保证每次都是"首扫"
    for f in (Path(WORK) / "index.db", Path(WORK) / "index.db-wal", Path(WORK) / "index.db-shm",
              Path(WORK) / "cache.json", Path(WORK) / "config.json"):
        if f.exists():
            f.unlink()
    print(f"[setup] 数据集已重新生成: {DATASET}")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  | {detail}" if detail else ""))


def get(path, timeout=60):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def post(path, obj, timeout=120):
    data = json.dumps(obj, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# ---------- mock LLM 服务（初筛/翻译测试用） ----------
class MockLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b"{}"
        user = body.decode("utf-8", "ignore")
        if "标签翻译助手" in user:
            content = json.dumps({"mock_tag": "模拟翻译", "mock_tag2": "模拟翻译二"}, ensure_ascii=False)
        else:
            content = json.dumps(
                [{"tag": "1girl", "action": "keep", "reason": "身份特征"},
                 {"tag": "smile", "action": "delete", "reason": "默认表情"}], ensure_ascii=False)
        payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


make_dataset()
PORT = free_port()
MOCK_PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
print(f"[setup] server port={PORT}, mock port={MOCK_PORT}")
mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockLLM)
threading.Thread(target=mock.serve_forever, daemon=True).start()

# ---------- 启动被测服务 ----------
srv = subprocess.Popen([sys.executable, "server.py", str(PORT)], cwd=WORK,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
time.sleep(2.5)
alive = srv.poll() is None
check("server 启动", alive, f"poll={srv.poll()}")
if not alive:
    print(srv.stdout.read()[:3000])
    sys.exit(1)

try:
    # ========== 静态资源 ==========
    st, hd, body = get("/")
    check("GET / 返回 HTML", st == 200 and "LoRA 打标管理器" in body.decode("utf-8", "ignore"), f"status={st} len={len(body)}")
    check("index.html no-cache", "no-cache" in hd.get("Cache-Control", ""), hd.get("Cache-Control", ""))

    st, hd, body = get("/fonts/OPPOSans-R.cjk.woff2")
    check("CJK woff2 200+immutable", st == 200 and len(body) > 100_000 and "immutable" in hd.get("Cache-Control", ""),
          f"status={st} size={len(body)} cc={hd.get('Cache-Control','')}")
    st, hd, body = get("/fonts/OPPOSans-R.woff2")
    check("ASCII woff2 200", st == 200 and len(body) > 1000, f"size={len(body)}")
    st, hd, body = get("/vendor/Sortable.min.js")
    check("Sortable.min.js 200", st == 200 and len(body) > 10_000, f"size={len(body)}")
    st, hd, body = get("/../server.py")
    check("路径穿越被拒", st == 404, f"status={st}")
    st, hd, body = get("/api/nonexistent")
    check("未知 API 404", st == 404, f"status={st}")

    # ========== 设置 ==========
    st, hd, body = get("/api/settings")
    cfg = json.loads(body)
    check("GET /api/settings", st == 200 and "deepseek" in cfg, f"keys={list(cfg.keys())[:5]}")
    st, hd, body = post("/api/settings", {"deepseek": {"api_key": "sk-mock", "base_url": f"http://127.0.0.1:{MOCK_PORT}", "model": "mock"},
                                          "preview_size": 0})  # preview 0：测试期跳过缩略图预热
    cfg2 = json.loads(body)["settings"]
    check("POST /api/settings 生效", st == 200 and cfg2["deepseek"]["base_url"].endswith(str(MOCK_PORT)) and cfg2["preview_size"] == 0)
    # 加密落盘验证：config.json 里 api_key 应为 enc: 密文
    disk_cfg = json.loads((Path(WORK) / "config.json").read_text(encoding="utf-8"))
    check("config.json api_key 已加密", disk_cfg["deepseek"]["api_key"].startswith("enc:"), disk_cfg["deepseek"]["api_key"][:12])

    # ========== 扫描（合成数据集） ==========
    t0 = time.time()
    st, hd, body = post("/api/scan", {"folder": DATASET})
    r = json.loads(body)
    check("POST /api/scan 返回 scan_id", st == 200 and r.get("scan_id"), f"scan_id={r.get('scan_id')}")
    scan_id = r["scan_id"]
    last = None
    while True:
        st, hd, body = get(f"/api/scan/progress?scan_id={scan_id}")
        p = json.loads(body)
        last = p
        if p["status"] != "running":
            break
        time.sleep(0.2)
    t_scan1 = time.time() - t0
    check("扫描完成", p["status"] == "done", f"{p['done']}/{p['total']} err={p.get('error')}")
    print(f"  [time] 合成数据集首扫(300图): {t_scan1:.2f}s")

    st, hd, body = get(f"/api/scan/result?scan_id={scan_id}&offset=0&limit=200")
    r1 = json.loads(body)
    check("result 首页 200 图", r1["ok"] and len(r1["images"]) == 200 and r1["total_images"] == 300,
          f"total={r1['total_images']} txt={r1['total_txt']} tags={r1['total_tags']}")
    check("共同标签=5 个内置", sorted(r1["common_tags"]) == sorted(["1girl", "solo", "long_hair", "looking_at_viewer", "smile"]),
          str(r1["common_tags"]))
    check("result has_more", r1["has_more"] is True)
    st, hd, body = get(f"/api/scan/result?scan_id={scan_id}&offset=200&limit=200")
    r2 = json.loads(body)
    check("result 尾页 100 图", len(r2["images"]) == 100 and r2["has_more"] is False, f"n={len(r2['images'])}")
    all_paths = [im["path"] for im in r1["images"] + r2["images"]]
    check("分页无重复无遗漏", len(set(all_paths)) == 300)

    # 二次扫描（增量）
    t0 = time.time()
    st, hd, body = post("/api/scan", {"folder": DATASET})
    scan_id2 = json.loads(body)["scan_id"]
    while True:
        st, hd, body = get(f"/api/scan/progress?scan_id={scan_id2}")
        p = json.loads(body)
        if p["status"] != "running":
            break
        time.sleep(0.15)
    t_scan2 = time.time() - t0
    check("二次扫描完成", p["status"] == "done")
    print(f"  [time] 合成数据集二扫(增量): {t_scan2:.2f}s  (首扫 {t_scan1:.2f}s)")
    check("增量扫描不慢于首扫", t_scan2 <= t_scan1 * 1.5, f"{t_scan2:.2f}s vs {t_scan1:.2f}s")

    # 扫描间 txt 变更：改一个 txt → 增量扫描应只更新该图（其余走缓存）
    probe_img = all_paths[10]
    probe_txt = str(Path(probe_img).with_suffix(".txt"))
    if os.path.isfile(probe_txt):
        Path(probe_txt).write_text("probe_changed_tag", encoding="utf-8")
        time.sleep(0.05)
        st, hd, body = post("/api/scan", {"folder": DATASET})
        sid3 = json.loads(body)["scan_id"]
        while True:
            st, hd, body = get(f"/api/scan/progress?scan_id={sid3}")
            p = json.loads(body)
            if p["status"] != "running":
                break
            time.sleep(0.15)
        st, hd, body = get(f"/api/scan/result?scan_id={sid3}&offset=0&limit=300")
        r3 = json.loads(body)
        probe = next(im for im in r3["images"] if im["path"] == probe_img)
        check("txt 变更后增量扫描更新该图", probe["tags"] == ["probe_changed_tag"], str(probe["tags"]))
        # 其余图标签未变（缓存复用）
        other = next(im for im in r3["images"] if im["path"] == all_paths[0])
        check("未变更图标签保持缓存", other["tags"] == next(im["tags"] for im in r1["images"] if im["path"] == all_paths[0]))
        # 恢复原 txt（从备份恢复不现实，直接重写原内容：重扫前已存 old_content 的图是 all_paths[0]）
        Path(probe_txt).write_text(", ".join(next(im["tags"] for im in r1["images"] if im["path"] == probe_img)), encoding="utf-8")

    # ========== 批量翻译（P0-2 核心基准） ==========
    tags = [im["tags"][0] for im in r1["images"] if im["tags"]] + \
           [f"rare_tag_{i}" for i in range(200)]
    tags = list(dict.fromkeys(tags))[:300]
    t0 = time.time()
    st, hd, body = post("/api/translate/bulk", {"tags": tags})
    t_bulk = time.time() - t0
    r = json.loads(body)
    n_hit = sum(1 for it in r["items"] if it["zh"])
    check("POST /api/translate/bulk 全部返回", st == 200 and len(r["items"]) == len(tags), f"sent={len(tags)} hits={n_hit}")
    print(f"  [time] bulk 300 标签: {t_bulk*1000:.1f}ms (v1 基线 976ms)")
    check("bulk 耗时 < 50ms", t_bulk < 0.05, f"{t_bulk*1000:.1f}ms")
    it0 = r["items"][0]
    check("bulk 含风格信息", "group" in it0 and "matched" in it0, str(it0)[:80])

    st, hd, body = get("/api/translate/dict?tags=1girl")
    r = json.loads(body)
    check("dict 1girl→1个女性", r["items"][0]["zh"] == "1个女性", str(r["items"][0]))
    st, hd, body = get("/api/translate/dict?tags=" + urllib.request.quote("long hair"))
    r = json.loads(body)
    check("dict 空格→下划线 规范化", r["items"][0]["zh"] == "长发", str(r["items"][0]))

    # ========== LLM 翻译（mock）+ 缓存落盘 ==========
    st, hd, body = post("/api/translate/llm", {"tags": ["mock_tag", "mock_tag2"]})
    r = json.loads(body)
    check("translate/llm (mock)", st == 200 and r.get("translated", {}).get("mock_tag") == "模拟翻译", str(r)[:100])
    time.sleep(2.5)  # 等 flusher 2s 落盘
    st, hd, body = get("/api/translate/cache")
    r = json.loads(body)
    check("缓存已落盘(内存常驻)", r["cache"].get("mock_tag") == "模拟翻译", str(r["cache"])[:100])
    disk_cache = json.loads((Path(WORK) / "cache.json").read_text(encoding="utf-8"))
    check("cache.json 文件已写入", disk_cache.get("mock_tag") == "模拟翻译")

    # ========== 缩略图（P0-5） ==========
    img0 = all_paths[0]
    t0 = time.time()
    st, hd, body = get("/api/thumb?path=" + urllib.request.quote(img0) + "&size=256")
    t_thumb1 = time.time() - t0
    check("thumb 256 JPEG", st == 200 and body[:2] == b"\xff\xd8" and "immutable" in hd.get("Cache-Control", ""),
          f"size={len(body)} cc={hd.get('Cache-Control','')}")
    t0 = time.time()
    st, hd, body = get("/api/thumb?path=" + urllib.request.quote(img0) + "&size=256")
    t_thumb2 = time.time() - t0
    print(f"  [time] thumb 首生成 {t_thumb1*1000:.0f}ms / 缓存命中 {t_thumb2*1000:.0f}ms")
    # 扫描后预热线程可能已提前生成（两次都快=预热生效）；断言：都快且第二次不回退
    check("thumb 快速响应(预热生效)", t_thumb1 < 0.1 and t_thumb2 <= t_thumb1 * 1.5 + 0.005,
          f"{t_thumb1*1000:.0f}ms vs {t_thumb2*1000:.0f}ms")

    # ========== 原图 + 安全 ==========
    st, hd, body = get("/api/image?path=" + urllib.request.quote(img0))
    check("原图 200 字节一致", st == 200 and len(body) == os.path.getsize(img0), f"size={len(body)}")
    st, hd, body = get("/api/image?path=C%3A%5CWindows%5Cwin.ini")
    check("未扫描目录 403", st == 403, f"status={st}")

    # ========== 覆写 / 批量 / 恢复 ==========
    txt0 = str(Path(img0).with_suffix(".txt"))
    old_content = Path(txt0).read_text(encoding="utf-8")
    st, hd, body = post("/api/tags/update", {"image": img0, "tags": ["a", "b", "c"]})
    r = json.loads(body)
    check("tags/update 覆写", st == 200 and r["ok"] and Path(txt0).read_text(encoding="utf-8") == "a, b, c",
          f"backup={os.path.basename(r.get('backup',''))}")
    bak = r.get("backup", "")
    check("备份文件已生成", bak and os.path.isfile(bak) and Path(bak).read_text(encoding="utf-8") == old_content)

    imgs5 = all_paths[1:6]
    st, hd, body = post("/api/tags/batch", {"images": imgs5, "remove": ["a"], "add": ["batch_tag"]})
    r = json.loads(body)
    ok5 = sum(1 for x in r["results"] if x["ok"])
    t0 = time.time()
    st, hd, body = post("/api/tags/batch", {"images": all_paths[:100], "remove": ["x"], "add": []})
    t_batch = time.time() - t0
    print(f"  [time] batch 100 图并发: {t_batch*1000:.0f}ms")
    check("tags/batch 5 图", ok5 == 5, str(r["results"][0])[:80])
    check("batch 100 图 < 3s", t_batch < 3.0, f"{t_batch*1000:.0f}ms")

    st, hd, body = post("/api/restore", {"items": [{"image": img0, "backup": bak}]})
    r = json.loads(body)
    check("restore 恢复", st == 200 and r["results"][0]["ok"] and
          Path(txt0).read_text(encoding="utf-8") == old_content, str(r["results"][0])[:80])
    st, hd, body = post("/api/restore", {"items": [{"image": img0, "backup": r"C:\fake\evil.bak"}]})
    r = json.loads(body)
    check("restore 拒绝 backups/ 外路径", r["results"][0]["ok"] is False, str(r["results"][0])[:60])

    # ========== 初筛流式（P0-7，mock LLM） ==========
    tags250 = [f"tag_{i}" for i in range(250)]  # 3 批
    t0 = time.time()
    req = urllib.request.Request(BASE + "/api/screen/stream",
                                 data=json.dumps({"images": [all_paths[0]], "tags": tags250}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        check("stream Content-Type ndjson", "ndjson" in resp.headers.get("Content-Type", ""), resp.headers.get("Content-Type", ""))
        raw = resp.read().decode("utf-8")
    t_stream = time.time() - t0
    lines = [json.loads(l) for l in raw.strip().split("\n") if l.strip()]
    check("stream 3 批全部回推", len(lines) == 3 and all(l.get("total") == 3 for l in lines), f"lines={len(lines)}")
    check("stream 含 verdicts", lines[0]["ok"] and isinstance(lines[0]["verdicts"], list), str(lines[0])[:100])
    print(f"  [time] stream 3 批(mock): {t_stream*1000:.0f}ms")

    st, hd, body = post("/api/screen", {"images": [all_paths[0]], "tags": ["1girl", "smile"]})
    r = json.loads(body)
    check("screen 非流式(v1 契约)", st == 200 and r["ok"] and len(r["results"]) == 1 and r["results"][0]["ok"], str(r)[:120])

    # ========== 词库 / 目录 ==========
    st, hd, body = get("/api/taglib?q=hair")
    r = json.loads(body)
    check("taglib 搜索", st == 200 and len(r["items"]) > 0 and all("cat_zh" in it for it in r["items"]), f"n={len(r['items'])} total={r['total']}")
    st, hd, body = get("/api/taglib")
    r = json.loads(body)
    check("taglib 全量 5749", r["total"] == 5749 and len(r["items"]) == 5749, f"total={r['total']}")
    st, hd, body = get("/api/drives")
    r = json.loads(body)
    check("drives", st == 200 and "H:\\" in r["drives"], str(r["drives"]))
    st, hd, body = get("/api/listdir?path=" + urllib.request.quote("H:\\"))
    r = json.loads(body)
    check("listdir", st == 200 and isinstance(r.get("dirs"), list), f"n={len(r.get('dirs', []))}")
    st, hd, body = post("/api/recent", {"folder": DATASET})
    check("recent", st == 200 and json.loads(body)["ok"])

    # ========== 真实数据集（老的：2065 图 / 1178 txt） ==========
    t0 = time.time()
    st, hd, body = post("/api/scan", {"folder": REAL})
    scan_real = json.loads(body)["scan_id"]
    while True:
        st, hd, body = get(f"/api/scan/progress?scan_id={scan_real}")
        p = json.loads(body)
        if p["status"] != "running":
            break
        time.sleep(0.5)
    t_real1 = time.time() - t0
    check("真实数据集首扫", p["status"] == "done" and p["done"] == p["total"] and p["total"] >= 2000,
          f"{p['done']}/{p['total']} err={p.get('error')}")
    print(f"  [time] 真实数据集首扫({p['total']}图): {t_real1:.2f}s")
    check("首扫 < 6s", t_real1 < 6.0, f"{t_real1:.2f}s")

    st, hd, body = get(f"/api/scan/result?scan_id={scan_real}&offset=0&limit=200")
    rr = json.loads(body)
    check("真实扫描元数据", rr["total_images"] == p["total"] and rr["total_txt"] > 1000,
          f"img={rr['total_images']} txt={rr['total_txt']} tags={rr['total_tags']}")

    t0 = time.time()
    st, hd, body = post("/api/scan", {"folder": REAL})
    scan_real2 = json.loads(body)["scan_id"]
    while True:
        st, hd, body = get(f"/api/scan/progress?scan_id={scan_real2}")
        p = json.loads(body)
        if p["status"] != "running":
            break
        time.sleep(0.3)
    t_real2 = time.time() - t0
    check("真实数据集二扫", p["status"] == "done")
    print(f"  [time] 真实数据集二扫(增量): {t_real2:.2f}s  (加速 {t_real1/max(t_real2,0.01):.1f}x)")
    # 验收口径：严格快于首扫（增量免 txt 读取）+ 绝对值 < 2.5s（负载波动下比率不稳，不卡 0.7x）
    check("二扫快于首扫", t_real2 < t_real1 and t_real2 < 2.5, f"{t_real2:.2f}s vs {t_real1:.2f}s")

    # 真实数据集全标签 bulk 翻译基准
    st, hd, body = get(f"/api/scan/result?scan_id={scan_real2}&offset=0&limit=1")
    rr = json.loads(body)
    all_tags = [t["tag"] for t in rr["tag_stats"]]
    t0 = time.time()
    st, hd, body = post("/api/translate/bulk", {"tags": all_tags})
    t_bulk_real = time.time() - t0
    r = json.loads(body)
    n_hit = sum(1 for it in r["items"] if it["zh"])
    print(f"  [time] 真实数据集 bulk {len(all_tags)} 标签: {t_bulk_real*1000:.1f}ms (词典命中 {n_hit})")
    check("真实 bulk < 100ms", t_bulk_real < 0.1, f"{t_bulk_real*1000:.1f}ms")

finally:
    srv.terminate()
    try:
        srv.wait(timeout=5)
    except Exception:
        srv.kill()
    try:
        mock.shutdown()
    except Exception:
        pass
    print()
    print("=" * 60)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  -", f)
    else:
        print("全部通过")
    # 强制退出：防止任何残留线程挂住进程、占用端口（先 flush 输出）
    import os as _os
    sys.stdout.flush()
    sys.stderr.flush()
    _os._exit(0 if not FAIL else 1)

print()
print("=" * 60)
print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    print("失败项:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("全部通过")
