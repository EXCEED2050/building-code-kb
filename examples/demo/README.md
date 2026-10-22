# 演示数据

`chunks/DEMO.jsonl` 是人工编写的虚构数据，共三条。编号 `GB 00000-2099` 不代表真实规范，所有内容只用于验证程序，不可作为设计依据。

演示不包含规范原文、PDF、模型权重或密钥。支持登录、条号直达、关键词检索和条文详情；无页码、无原件预览，默认无 AI 作答。

从项目根目录运行：

```sh
KB_CHUNKS=examples/demo/chunks python run_dev.py
```

登录口令见启动日志。试问 `GB 00000 第1.0.1条` 或 `演示数据如何校对`。

命令行检索：

```sh
python -m building_kb.search.retrieve --chunks examples/demo/chunks --no-rerank "GB 00000 第1.0.1条"
```
