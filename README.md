# LoRA 打标管理器

本地 WebUI：LoRA 数据集标签编辑工具（扫描统计 / 标签编辑 / 中文词库 / 翻译 / AI 初筛 / 自动备份）。

## 运行

**桌面版（推荐）**：双击 `LoraTagManager.exe` 或 `启动.bat`，零依赖，自动打开无边框窗口。

**源码模式**：仅需 Pillow（服务为标准库 http.server，无 FastAPI/requests/yaml 运行时依赖）：

```bash
pip install pillow
python server.py [端口，默认 8765]
```

访问 http://localhost:8765。

## 功能

- 扫描数据集 → 标签统计、共同标签提示（后台扫描 + 进度轮询 + 分页取数据，UI 不阻塞）
- 标签增删、拖拽排序、单击改名+自动翻译
- 内置 5749 条 Danbooru 中文词库（点击添加，自动配色）
- 输入中英文自动匹配词库，中文未命中自动转英文
- 离线词典 + DeepSeek 翻译（批量接口），结果本地缓存
- AI 初筛（DeepSeek / LM Studio / vLLM），标签分批并发 + 流式进度，丢弃标签可放回
- 保存自动备份到 `backups/`，支持撤销/重做
- 虚拟滚动图片网格（7000+ 图不卡）、缩略图预生成缓存

## v2 性能与体积

| 项目 | v1 | v2 |
|---|---|---|
| 启动 | onefile 解压 3~30s | onedir 免解压，~0.2s |
| 包体积 | ~80MB 单文件 | ~41MB onedir |
| 字体 | 4×TTF 24.6MB | 2×woff2 子集 476KB |
| 300 标签翻译 | 976ms（300 次 GET） | ~15ms（1 次批量） |
| 7000 图扫描 | 全量读 txt ~45-60s | 首扫 ~1.5s / 增量 ~1s |
| 前端渲染 | 全量重建 1500 DOM | 虚拟滚动只画可视窗口 |
| 初筛 | 阻塞单响应 | 分批并发 + ndjson 流式进度 |

接口路径与 JSON 契约与 v1 完全一致（前端零兼容成本）；新增 `/api/translate/bulk`、`/api/scan/progress`、`/api/scan/result`、`/api/screen/stream`。

## 构建

```bash
pyinstaller LoraTagManager.spec --noconfirm
```

产物在 `dist/LoraTagManager/`（onedir）。词库改动后重跑 `tools/prebuild_taglib.py`（yaml→json 预编译）；字体改动后重跑 `tools/gen_font_chars.py` + pyftsubset。

## 本地数据

`config.json`（含 API Key 密文）、`cache.json`、`backups/`、`thumbs/`、`index.db` 为本地数据，不入库。
