# LoRA 打标管理器

本地 WebUI：LoRA 数据集标签编辑工具（扫描统计 / 标签编辑 / 中文词库 / 翻译 / AI 初筛 / 自动备份）。

## 运行

```bash
pip install fastapi uvicorn pillow
python server.py
```

访问 http://localhost:8765；Windows 可双击 `启动.bat`。

## 功能

- 扫描数据集 → 标签统计、共同标签提示
- 标签增删、拖拽排序、单击改名+自动翻译
- 内置 5749 条 Danbooru 中文词库（点击添加，自动配色）
- 输入中英文自动匹配词库，中文未命中自动转英文
- 离线词典 + DeepSeek 翻译，结果本地缓存
- AI 初筛（DeepSeek / LM Studio / vLLM），丢弃标签可放回
- 保存自动备份到 `backups/`，支持撤销/重做

`config.json`、`cache.json`、`backups/`、`thumbs/` 为本地数据，不入库。
