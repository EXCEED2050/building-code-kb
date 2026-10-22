> 附件上传状态（2026-10-22）：完整原件、MinerU 与 OCR 资料已上传到私有 Release，共 7 个独立 ZIP，约 4.10 GB、4,220 个文件。完整条文、台账、代码和文档在主分支；恢复方法见下文。

# 建筑法规知识库

按规范号、条文号或关键词检索建筑规范，以 H5 页面展示条文及出处；配置模型后可流式生成带引用的回答。

本仓库为两人协作的私有项目，包含应用代码、架构图、开发文档、完整台账和 159 本规范的 43,813 个条文块。过滤后检索池为 28,440 块，默认读取 `kb/data/chunks/`。

规范 PDF/Word 原件和 OCR/MinerU 资料作为同一私有仓库的下载附件共享，见 [私有资料恢复](docs/私有资料恢复.md)。真实密钥、模型、索引及页图缓存不共享。请保持仓库及完整资料为私有。

两人共同开发的接手步骤和资料分工见 [协作开发](docs/协作开发.md)。大文件附件约 4.10 GB，以 7 个完整、独立 ZIP 提供；单独下载不会增加每次克隆代码的体积，无需传输片段或合并。详见资料恢复说明。

## 快速试运行

需要 Python 3.10 或以上，已验证的演示环境为 Python 3.12。前端为单文件页面，无需 Node.js 或前端构建。

在项目根目录打开终端：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r kb/requirements.txt
cd kb
python run_dev.py
```

如果系统 `python3` 版本低于 3.10，请将第一行替换为已安装的新版 Python，例如 `python3.12 -m venv .venv`。

要复现已验证的完整轻量依赖，请使用 Python 3.12，将安装命令替换为 `python -m pip install -r kb/requirements-tested-py312.txt`。该文件包含间接依赖的实测版本；常规 `requirements.txt` 固定直接依赖，允许安装器选择兼容的间接依赖。

打开 <http://127.0.0.1:8000>，输入启动日志中的随机访问口令。查询 `GB 50016 第5.5.17条` 或 `安全疏散`。可直接开发和验证条号、关键词、书目和引用展示；未配置模型时只检索；PDF 原件预览需要先恢复下载附件。当前条文是 OCR/切条开发快照，未逐条验收，也不表示所收版本已完成最新有效性核验。

只想使用三条虚构数据演示时，可改为 `KB_CHUNKS=examples/chunks python run_dev.py`。公开数据的筛选范围、来源和限制见 [公开数据说明](kb/public-data/README.md)。

核验完整数据请从 `kb/` 运行 `python -m tools.check_snapshot`。原先的 16 本公开子集仍保留在 `public-data/`，需要切换时可显式设置 `KB_CHUNKS=public-data/chunks`。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `kb/app/` | FastAPI 后端、口令认证与 H5 页面 |
| `kb/src/` | 台账、解析、切条、检索、模型适配和原件定位 |
| `kb/tools/` | OCR 队列、云端 OCR、模型下载、校对等辅助工具 |
| `kb/data/chunks/`、`catalog.csv` | 完整私有条文快照及台账 |
| `kb/data/*.tsv` | 人工订正、优先级和引用提醒规则 |
| `kb/data/*-manifest.json` | 完整条文和大文件附件的 SHA256 清单 |
| `kb/examples/` | 不含真实规范的演示条文 |
| `kb/public-data/` | 16 本规范的公开正文开发快照、来源、台账和 SHA256 清单 |
| `kb/conf/magic-pdf.example.json` | 本地 OCR 配置模板 |
| `架构图/` | 架构 HTML、数据流程 SVG、模块清单 |
| `docs/认证与请求流程.md` | 代码组件、请求流程、凭据与 Cookie 处理 |
| `docs/开发计划.md` | 两人分工建议、待办和验收标准 |
| `docs/数据格式.md` | 条文数据字段、页码和状态含义 |

架构 HTML 下载后用浏览器打开，SVG 可直接查看。图中数量、进度和依赖缺口为历史快照；模型默认值和运行方式以当前代码及本说明为准。

## 数据处理

完整数据流程：规范 PDF/Word → 台账 → 文本解析或 OCR → 条文 JSONL → 检索 → 可选问答 → 原件页码核对。

下面命令均从 `kb/` 执行，先把原件放在项目根目录 `建筑规范/`，Word 放在其 `word/` 子目录，MinerU 解析结果放在项目根目录 `MinerU/`：

```sh
python -m src.catalog --roots ../建筑规范 --out data/catalog.csv
# 核对台账状态、被代替关系，再切条
python -m src.batch --catalog data/catalog.csv \
  --word-dir ../建筑规范/word --mineru-dir ../MinerU --out data/chunks
python -m src.retrieve "规范编号或关键词"
python -m src.source_pdf
```

自动扫描不能确认规范是否现行；台账状态、修订关系、强条与页码必须经过核对。`overrides.tsv` 为台账订正，`tiers.tsv` 为 OCR 优先级，`clause_notes.tsv` 为特定条文的引用提醒；这些表也需要随资料更新。

条文 JSONL 每行一个对象，主要字段包括 `chunk_id`、`std_code`、`std_name`、`version`、`clause_no`、`chapter_path`、`section_type`、`text`、`tables`、`page_idx`、`is_mandatory`、`mandatory_basis`、`superseded`、`status`、`caveat`。`page_idx` 从 0 开始，没有可靠页码时为 `null`，完整样例见 `kb/examples/chunks/DEMO.jsonl`。

数据源路径可通过 `KB_CATALOG`、`KB_WORD_DIR`、`KB_MINERU_DIR`、`KB_OCR_DIR` 改写；`KB_CHUNKS` 控制 Web 应用条文目录。台账中的相对 `file_path` 按项目根目录解析。

## 可选模型与 OCR

轻量安装支持条号直达和 BM25，无需模型密钥。以下重型功能不属于演示验证范围，需结合硬件与资料单独验证。

本机向量与重排：

```sh
python -m pip install -r requirements-models.txt
python -m tools.get_models
python -m src.index
```

模型为 `bge-m3` 和 `bge-reranker-v2-m3`，下载到 `kb/models/`。`src.index` 按内容哈希增量建库；`--rebuild` 会清空并重建当前集合。

作答配置：复制 `.env.example` 为 `.env`，在本机填入自己的密钥。默认作答使用 DeepSeek；未配置时仍可检索。提供方由 `KB_EMBED_PROVIDER`、`KB_RERANK_PROVIDER`、`KB_LLM_PROVIDER` 分别指定，也可用 `KB_PROVIDER` 统一指定。`local` 代表内网/本地 API；`native` 代表直接运行本机 embedding/rerank 模型。

本地 OCR 使用旧版 `magic-pdf 1.3.12` 的内部接口，需建立独立环境安装 `requirements-ocr.txt`，下载对应模型，再复制 `conf/magic-pdf.example.json` 为 `conf/magic-pdf.json`，填写实际模型绝对路径与设备。支持 `MINERU_TOOLS_CONFIG_JSON` 指定其他配置文件。不要直接换装 MinerU 2.x。

云端 OCR 工具为 `python -m tools.ocr_cloud --list data/ocr_queue.txt`，需 `MINERU_TOKEN`，会向 MinerU 上传 PDF。二维码工具另外需要 `python -m pip install 'qrcode[pil]'`。

## 服务器启动与凭据

从 `kb/` 执行：

```sh
cp .env.example .env
# 在本机编辑 .env，设置 KB_PASSCODE、KB_SECRET 和需要的模型配置
python -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000
```

正式服务建议放在 HTTPS 反向代理后，并设置 `KB_HTTPS=1`。本机 HTTP 测试时留空，否则浏览器不会正常发送 Secure Cookie。多进程部署必须共享同一 `KB_SECRET`；无固定密钥时重启会使旧会话失效。

`--env-file` 确保认证配置在应用导入前加载；仅把口令写进 `.env` 后直接运行普通 uvicorn 不会自动加载它。认证采用共用口令加 12 小时 HMAC 签名 Cookie，详见 [认证与请求流程](docs/认证与请求流程.md)。

`.gitignore` 排除凭据、原件、模型、缓存和本地 ZIP。完整条文与台账在私有主分支中追踪。16 本公开子集单独保存在 `kb/public-data/`，仍可运行 `python -m tools.export_public --check` 核验；它的筛选规则不代表完整私有资料可以公开。
