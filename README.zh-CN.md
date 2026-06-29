# Runspool

**本地优先（local-first）的命令行工作流引擎，让个人自动化更可靠。**

> English is the primary documentation. 本文为中文补充，完整内容请以
> [English README](README.md) 为准。

Runspool 把脚本、文件和人工 checklist 变成「可恢复、可观测」的工作流：用 SQLite
保存状态，内置重试、日志、暂停/恢复控制、步骤插件，并为人类、脚本和 AI agent
提供 JSON 输出。

它完全在你自己的机器上运行——默认没有托管服务、不需要账号、数据不外传。

[English README](README.md) · [文档](docs/) · [示例](examples/)

---

## 为什么用 Runspool

个人自动化往往从一个 shell 脚本开始，然后慢慢失控：跑到一半挂了，你不知道哪步执行
过；重跑会重复劳动；没有历史；想暂停或重试就得改脚本。

Runspool 给这类自动化一根「主心骨」：

- **可恢复**：每个任务都是 SQLite 里的一行；崩溃或重启都不丢进度。
- **可观测**：每次状态变化都是一条事件；每次步骤运行都有计时。
- **可控制**：在命令行里暂停、恢复、重试、终止、调整优先级。
- **可组合**：工作流是有序的步骤列表；可以用插件添加自己的步骤。
- **可脚本化**：所有读取类命令都支持 `--json`，为 shell 和 AI agent 而设计。

它**不是** AI 工具，也**不是**云端工作流平台。它是一个小而可靠的引擎，把本地脚本、
文件和 checklist 变成你能信任的工作流。

## 安装

推荐用 `pipx` 安装 CLI：

```bash
pipx install runspool
```

如果 Mac 上还没有 `pipx`：

```bash
brew install pipx
pipx ensurepath
```

然后重开终端并检查命令：

```bash
runspool --help
```

也可以用 pip 安装：

```bash
python -m pip install runspool
```

或从源码安装：

```bash
git clone https://github.com/ethan-sun-dev/runspool
cd runspool
pip install -e ".[dev]"
```

需要 Python 3.11+。核心依赖：Typer、Pydantic、PyYAML（SQLite 来自标准库）。

## 快速上手（约 3 分钟，无需任何外部配置）

```bash
# 1. 生成配置和数据库
runspool init

# 2. 添加任务。默认的 local_file 工作流只用内置步骤
echo "Invoice #42  Total amount due: 1320  Payment terms: net 30" > invoice.txt
runspool add ./invoice.txt

# 3. 一次性把所有任务推进到完成
runspool run

# 4. 查看结果
runspool status
runspool inspect 1
```

任务会依次经过五个步骤，产物落在 `workspace/ready/1/`（规范化的 Markdown、摘要、
分类、元数据）。这就是一个带持久化状态、日志和步骤时间线的完整工作流——而且零外部
依赖。

## 命令行（CLI）

```text
runspool init                     # 创建配置 + 数据库
runspool add <input> -w <wf>      # 入队一个任务（默认工作流：local_file）
runspool run                      # 一次性推进所有可运行任务（适合演示/批处理）
runspool daemon                   # 常驻循环（长任务自动化）
runspool daemon-status            # 查看 daemon 是否在运行
runspool daemon-stop              # 通知运行中的 daemon 停止
runspool status [<id>]            # 列出任务，或查看单个任务详情
runspool inspect <id>             # 面向 agent 的快照 + 建议的下一步动作
runspool logs <id>                # 任务事件历史
runspool overview                 # 按状态汇总
runspool pause|resume|retry|terminate <id>
runspool set-priority|set-retries|set-step <id> <value>
runspool workflows                # 列出工作流及其步骤
runspool doctor                   # 检查本机环境
```

以下命令都支持 `--json`：只读类的 `status`、`inspect`、`logs`、`overview`、
`workflows`、`doctor`，以及会推进状态的 `run`。

## 为 AI agent 和脚本而设计

`runspool inspect <id> --json` 会返回自动化调用方决策所需的一切：当前状态、最近的
错误、已产出的产物、当前可执行的动作，以及一句自然语言建议——

```json
{
  "id": 1,
  "status": "manual_required",
  "current_step": "collect_sources",
  "last_error": "FileNotFoundError: Missing required source(s): requirements.md",
  "available_actions": ["retry", "set-step", "set-retries", "terminate"],
  "suggested_next_action": "... Resolve the cause, then run `runspool retry 1`."
}
```

agent 可以轮询 `inspect --json`，根据 `available_actions` 行动，修复原因后调用
`runspool retry 1`——无需解析任何人类可读文本。详见
[docs/agent-json-output.md](docs/agent-json-output.md)。

## 三个示例

| 示例 | 展示内容 |
| --- | --- |
| [local-file-pipeline](examples/local-file-pipeline/) | 快速上手。仅用内置步骤，离线几分钟跑通。 |
| [client-intel-brief](examples/client-intel-brief/) | 真实顾问场景：把资料整理成简报包。自定义插件步骤，演示 `manual_required` 恢复流程。 |
| [creator-publishing-pipeline](examples/creator-publishing-pipeline/) | 内容流水线，生成多平台**草稿**包（默认绝不自动发布）。 |

## 编写自定义步骤

一个步骤就是一个小类：读取任务、做事、写产物、返回结果。

```python
from runspool.engine.step import Step, StepContext, StepResult

class GreetStep(Step):
    name = "greet"

    def run(self, ctx: StepContext) -> StepResult:
        name = ctx.task.get("name") or "world"
        return StepResult(message=f"hello, {name}")
```

在配置中加载并用于工作流：

```yaml
plugin_paths: [steps]
steps:
  greet:
    import: "my_steps:GreetStep"
workflows:
  hello:
    steps: [greet, archive]
```

详见 [docs/writing-steps.md](docs/writing-steps.md)。

## 隐私与安全

- **本地优先**：所有状态都在你机器上的 `workspace_root` 下，默认不上传任何数据。
- **无需密钥**：引擎和内置步骤不需要任何 API key。
- **只出草稿，不自动发布**：内容类示例只生成草稿和 checklist，发布始终是你手动、
  有意识的一步。

## 非目标（Non-goals）

- 不是托管/云端工作流平台。
- 不是分布式调度器，也不是重型编排系统的替代品。
- 不是 AI 产品（但刻意对 AI agent 友好）。
- 当前不做 Web UI——CLI 和 JSON 就是接口。

## 许可证

[MIT](LICENSE)。
