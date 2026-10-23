# 建筑法规知识库

在手机、微信或电脑浏览器里按规范号、条文号及关键词查建筑规范，查看条文与 PDF 原页；配置模型后可进行带出处的 AI 解读和追问。

这是用于共同开发的公开项目。主分支包含 **159 本规范、43,813 个原始条文块**，其中 **28,440 块进入检索池**；这些是开发快照，版本效力、转录与工程适用性仍需核对。原件和解析资料保存在本仓库的 Release，真实密钥与运行缓存保存在各自电脑上。

[逐份 PDF 下载](docs/data/PDF目录.md) · [完整资料恢复](docs/data/私有资料恢复.md) · [项目架构](docs/architecture/项目结构.md) · [协作开发](docs/development/协作开发.md)

## 启动

使用 Python 3.12，在项目根目录执行，无需切换到子目录：

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python run_dev.py
```

Windows 激活命令为 `.venv\Scripts\activate`。打开 <http://127.0.0.1:8000>，输入启动日志里的随机口令，试查 `GB 50016 第5.5.17条`。条号与关键词检索无需模型或 API 密钥；PDF 预览需先恢复资料。

AI 解读后可继续追问，每轮重新检索并显示本轮依据。当前一组问答保存在本浏览器，可导出；新查询会替换当前记录，不跨设备同步。

## 目录

```text
src/building_kb/       应用代码，按职责分包
  web/                FastAPI、登录认证、H5 页面
  ingest/             扫描台账、Word/OCR 解析、切条与页码定位
  search/             条号、BM25、向量检索与索引
  qa/                 问答上下文与引用
  commands/           资料恢复、核验、OCR 队列和校对等命令
  providers.py        模型适配
  paths.py            统一配置和路径
  source_pdf.py       原件与完整阅读本定位
  verification.py     带内容指纹的人工校订

data/
  catalog/            台账、人工规则、校订记录、PDF 索引和验收题
  chunks/             159 本原始条文 JSONL
  manifests/          条文快照与资料附件的校验清单
  sources/            PDF/Word 原件，恢复后生成，本地保留
  processed/          MinerU、OCR 结果和完整阅读本，本地保留

docs/                 architecture、development、data、acceptance、history
examples/             demo 虚构样例、public 公开开发子集
config/               OCR 配置模板
scripts/              项目维护与共同检查入口
tests/                检索、追问、校订、PDF、恢复与界面回归
var/                  模型、索引、缓存、日志、队列、生成的校对页；不提交
archive/              本机旧工程和迁移备份；不提交
.github/workflows/    自动运行共同检查
```

本地与 GitHub 使用同一套代码和资料路径。克隆后通过恢复工具补齐 `data/sources/` 和 `data/processed/`；大文件不随每次代码克隆重复下载。旧架构图和实施记录放在 `docs/history/`，当前架构以 [项目结构](docs/architecture/项目结构.md) 为准。

## 获取资料

- [PDF 目录](docs/data/PDF目录.md)：375 个独立 PDF，点击即可下载，无需解压。
- [完整资料 Release](https://github.com/EXCEED2050/building-code-kb/releases/tag/data-2026-10-20)：7 个独立资料卷，约 4.10 GB，包含 4,220 个原件与解析文件。恢复工具兼容旧压缩包里的路径，并放入新目录。
- [数据格式](docs/data/数据格式.md)：条号、页码、效力状态、强条和校订记录的含义。

```sh
building-kb check-snapshot
building-kb restore /下载附件的文件夹
building-kb build-pdfs
building-kb check-reviews
```

完整阅读本由登记分卷重建；例如 GB 55037 为 136 页。JSONL 的页码仍指向原定位副本，通过映射打开完整阅读本。仅想演示页面可运行 `KB_CHUNKS=examples/demo/chunks python run_dev.py`；16 本公开子集的使用方法见 [公开开发数据](examples/public/README.md)。

## 开发与配置

```sh
python -m pip install -r requirements-dev.txt
python scripts/setup_dev.py
python scripts/check.py
```

开发与提交须遵循 [贡献规范](CONTRIBUTING.md)：采用 Conventional Commits 提交格式，Ruff 统一 Python 格式和静态检查；配置脚本会启用本仓库的提交钩子。

共同检查覆盖 Python 回归、完整快照、公开子集、30 题检索预检和页面引用/表格处理；GitHub 每次推送和 PR 自动执行。人工验收题与核对结论见 [试用验收](docs/acceptance/广州试用验收题-20261022.md)。检索预检不替代真实模型与工程人员验收。

复制 `.env.example` 为根目录 `.env`，填写需要的模型密钥。程序会先读取根目录 `.env`，已有环境变量优先。默认本机向量与重排、DeepSeek 作答；模型未配置时可只检索。

正式服务在 `.env` 设置 `KB_PASSCODE`、`KB_SECRET`，然后运行：

```sh
python -m uvicorn building_kb.web.main:app --host 127.0.0.1 --port 8000
```

HTTPS 反向代理部署时设置 `KB_HTTPS=1`；HTTP 本机测试留空。认证是共用口令与 12 小时签名 Cookie，详见 [认证与请求流程](docs/architecture/认证与请求流程.md)。密钥只放本机，不提交、不交给前端。

[运行与维护工具](docs/development/运行与工具.md)包含模型下载、索引、OCR、资料处理和二维码命令。[迁移说明](docs/development/目录迁移.md)列出旧路径与新路径。
