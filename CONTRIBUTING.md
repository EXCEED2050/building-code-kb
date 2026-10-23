# 开发与提交规范

本项目采用 [Conventional Commits 1.0.0（约定式提交）](https://www.conventionalcommits.org/zh-hans/v1.0.0/)。Python 代码遵循 [PEP 8](https://peps.python.org/pep-0008/) 的基本布局与导入约定，具体格式由 [Ruff](https://docs.astral.sh/ruff/formatter/) 统一；本项目约定每行目标长度为 100 字符。

## 首次配置

在项目根目录创建并激活 Python 3.12 虚拟环境后执行：

```sh
python -m pip install -e .
python -m pip install -r requirements-dev.txt
python scripts/setup_dev.py
```

配置脚本只设置当前仓库：安装 `pre-commit` 和 `commit-msg` 两个 Git 钩子，启用 `.gitmessage` 提交模板。首次运行钩子时，会为固定版本的 Ruff 和 Commitizen 建立独立环境；之后编辑器或终端提交均可检查。每位协作者克隆后都要执行一次配置，Git 不会自动复制另一台电脑的钩子。

## 提交格式

```text
type(scope): 简短说明

可选正文：为什么修改、行为有什么变化、如何验证。

可选脚注：BREAKING CHANGE: 不兼容的变化和迁移方法
```

| 类型 | 用途 | 示例 |
| --- | --- | --- |
| `feat` | 新功能 | `feat(web): 支持导出当前问答` |
| `fix` | 修复错误 | `fix(ingest): 修复 OCR 命令启动失败` |
| `docs` | 文档 | `docs: 更新启动说明` |
| `style` | 格式、空格与导入整理 | `style(python): 统一代码格式` |
| `refactor` | 保持功能的结构调整 | `refactor(search): 提取规范号解析` |
| `perf` | 性能改进 | `perf(search): 缓存书目索引` |
| `test` | 测试 | `test(web): 覆盖过期会话` |
| `build`、`ci`、`chore` | 构建、自动检查、维护工具 | `ci: 检查新增提交的信息格式` |
| `revert` | 撤销变更 | `revert: 撤回检索参数调整` |

`scope` 可省略，建议使用 `web`、`search`、`ingest`、`qa`、`data`、`dev` 等模块名称。描述允许中文或英文。类型、英文冒号和其后的空格由工具校验；类型按项目约定使用小写。破坏兼容性的变更用 `!` 或 `BREAKING CHANGE:` 标记，并写明使用者需要如何迁移。

每个提交围绕一个目的组织，格式整理与功能修复分别提交。发布过的主分支提交不重写、不强制推送；历史旧提交保留，从本规范启用后的提交开始检查。PR 中写清实际变化、验证结果和限制。

## 代码约定

- 使用 UTF-8、LF、4 个空格缩进，保留中文注释；使用清楚的变量名。
- 标准库、第三方库和项目导入分组，由 Ruff 排序；删除无用导入。需要导入副作用时明确注明并保留理由。
- 函数、变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。不要用局部变量覆盖 `paths` 等配置模块。
- 新增或修改接口时补齐有意义的类型提示；改变行为时增加针对实际失败场景的回归检查。
- 路径默认值集中在 `src/building_kb/paths.py`。业务模块不依赖终端当前目录，不硬编码本机路径。
- 格式化只作用于当前 Python 代码、测试、维护脚本；原始条文、PDF/OCR、历史文档与归档不做批量格式化。
- `.env`、真实机器配置、缓存和模型仍按 `.gitignore` 排除。仓库和 Release 当前公开，新增资料前核实其共享范围。

## 修改与验证

先同步主分支，再在功能分支修改。自动修正格式和导入后运行完整检查：

```sh
python -m ruff check src tests scripts run_dev.py --fix
python -m ruff format src tests scripts run_dev.py
python scripts/check.py
```

`scripts/check.py` 检查 Python 静态问题与格式、回归、数据快照、公开子集、30 题检索预检和页面引用。GitHub 执行同样检查，并用 [Commitizen](https://commitizen-tools.github.io/commitizen/commands/check/) 检查新增提交信息。CI 的起点为规范启用前的 `3adec2502bc729a3433f7740cb9722c8847b3097`，不会要求改写既有历史。

```sh
git add 明确需要提交的文件
git commit -m "fix(search): 修复指定版本的条号匹配"
# 或使用交互式提交向导
cz commit
```

钩子失败时按提示修改、重新暂存后再提交。需要人工核验的 PDF、版本效力或真实模型效果，应在 PR 中注明；关键词预检通过不等于人工验收通过。
