# LoRA 打标管理器

本地运行的 LoRA 数据集标签编辑工具（Web UI）。用于 SD LoRA 训练集 TXT 打标：扫描、统计、编辑、初筛、翻译、自动备份。

## 功能

- **扫描统计**：导入数据集目录，自动扫描图片与 TXT 标签，侧栏标签统计与共同标签提示
- **标签编辑**：单击改名单选图片，右键重命名；增删、拖拽排序（SortableJS）
- **中文词库**：内置 5749 条 Danbooru 中文词库（分类/分组浏览，点击添加，自动配色）
- **智能输入**：输入中英文自动匹配词库（忽略下划线差异），中文未命中自动转英文
- **翻译**：离线词典优先 + DeepSeek LLM 兜底，翻译结果本地缓存持久化
- **AI 初筛**：LLM 批量判定标签是否该保留（DeepSeek 云端 / LM Studio 本地 / vLLM 多模态），丢弃标签可放回
- **保存安全**：覆写前自动备份到 `backups/`，支持撤销/重做（含文件级恢复）
- **设置**：API Key 本地加密存储、自定义 LLM 系统提示词、界面字号四档、明暗双主题（黑金）

## 运行

要求 Python 3.10+：

```bash
pip install fastapi uvicorn pillow
python server.py
```

浏览器访问 http://localhost:8765

Windows 可直接双击 `启动.bat`。

## 目录结构

```
server.py          FastAPI 后端（扫描/翻译/初筛/保存/设置）
static/index.html  前端界面（原生 HTML/CSS/JS）
static/fonts/      OPPO Sans 字体（SIL OFL 1.1 开源）
taglib/            中文词库（taglib.yaml）+ 离线词典（danbooru_zh.csv）
config.json        本地配置（API Key 加密存储，不入库）
```

## 数据安全

- `config.json` / `cache.json` / `backups/` / `thumbs/` 均为本地运行时数据，不入库
- 覆写前自动备份，可随时恢复
