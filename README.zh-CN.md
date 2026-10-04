# Runspool

**本地优先（local-first）的命令行工作流引擎，让个人自动化更可靠。**

> English is the primary documentation. 本文为中文补充，完整内容请以
> [English README](README.md) 为准。

Runspool 把脚本、文件和人工 checklist 变成「可恢复、可观测」的工作流：用 SQLite
保存状态，内置重试、日志、暂停/恢复控制，有对外副作用的步骤须经你审批，并为人类、
脚本和 AI agent 提供 JSON 输出。

它由一个很小的内核加一组插件构成：存储、步骤登记、任务生命周期，乃至命令行的扩展
命令都是插件；你自己的步骤、工作流和命令也用同一套方式接进来。

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
- **稳妥**：标明有对外副作用的步骤（发布、上传、发送）会等你批准后才执行，绝不会
  未经批准就运行。
- **可组合**：工作流是有序的步骤列表；步骤、工作流和命令都可以通过插件添加。
- **可脚本化**：所有读取类命令都支持 `--json`，为 shell 和 AI agent 而设计。

它**不是** AI 工具，也**不是**云端工作流平台。它是一个小而可靠的引擎，把本地脚本、
文件和 checklist 变成你能信任的工作流。

## 安装

推荐用 [uv](https://docs.astral.sh/uv/) 安装 CLI：

```bash
uv tool install runspool
```

如果还没有 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

然后检查命令：

```bash
runspool --help
```

也可以用 pip 安装：

```bash
pip install runspool
```

或从源码安装（用于开发）：

```bash
git clone https://github.com/ethan-sun-dev/runspool
cd runspool
uv sync
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

## 看结果（20 秒，无需安装）

不想跑？快速上手的真实产物已提交在 [`sample-output/`](sample-output/)。上面那份
invoice 完成后，`runspool inspect 1 --json` 返回的内容如下——一次调用就把脚本或 AI
agent 需要的全貌交到手里：

```jsonc
{
  "id": 1,
  "name": "invoice",
  "status": "completed",
  "current_step": "archive",
  "step_runs": [
    { "step": "ingest_file",        "status": "ok", "duration_ms": 1 },
    { "step": "classify_text",      "status": "ok", "duration_ms": 0 },
    { "step": "normalize_markdown", "status": "ok", "duration_ms": 0 },
    { "step": "summarize_text",     "status": "ok", "duration_ms": 0 },
    { "step": "archive",            "status": "ok", "duration_ms": 0 }
  ],
  "artifacts": [
    "ready/1/classification.json", "ready/1/metadata.json",
    "ready/1/normalized.md",       "ready/1/summary.md", "..."
  ],
  "available_actions": [],
  "suggested_next_action": "Task is complete; no action needed."
}
```

工作流把原始的 `invoice.txt` 变成了结构化产物，例如
[`ready/1/classification.json`](sample-output/ready/1/classification.json)：

```json
{ "category": "invoice", "confidence": 1.0,
  "matched_keywords": ["invoice", "amount due", "subtotal", "total", "payment terms"] }
```

完整快照、任务列表和每个产物都在 [`sample-output/`](sample-output/)。注意
`step_runs`（每步独立计时——失败可归因到具体步骤，而非整个任务）和
`available_actions` / `suggested_next_action`（引擎直接告诉 agent 当前**能**做什么、
**该**做什么）。为什么这样设计，见
[docs/design-decisions.md](docs/design-decisions.md)。

## 命令行（CLI）

```text
runspool init                     # 创建配置 + 数据库
runspool add <input> -w <wf>      # 入队一个任务（默认工作流：local_file）
                                  #   另有 --meta KEY=VALUE、--parent <id>、--name、--force
runspool run                      # 一次性推进所有可运行任务（适合演示/批处理）
runspool daemon                   # 常驻循环（长任务自动化）
runspool daemon-status            # 查看 daemon 是否在运行
runspool daemon-stop              # 通知运行中的 daemon 停止
runspool status [<id>]            # 列出任务，或查看单个任务详情
runspool inspect <id>             # 面向 agent 的快照 + 建议的下一步动作
runspool logs <id>                # 任务事件历史
runspool overview                 # 按状态汇总
runspool pause|resume|retry|terminate <id>
runspool approve <id>             # 放行一个等待审批的副作用步骤（仅限这一次尝试）
runspool reject <id> --reason ... # 拒绝它；任务转为需要人工处理
runspool wake <id>                # 让推迟中的任务立即可运行，不再等延时
runspool set-priority|set-retries|set-step <id> <value>
runspool workflows                # 列出工作流及其步骤
runspool doctor                   # 检查本机环境、插件和凭证
```

插件可以添加自己的命令，用 `runspool -c <profile> --help` 查看——例如官方公众号插件
提供 `runspool wechat preview` 和 `runspool wechat token`。

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
| [client-intel-brief](examples/client-intel-brief/) | 真实顾问场景：把资料整理成简报包。从配置加载自定义步骤，演示 `manual_required` 恢复流程。 |
| [creator-publishing-pipeline](examples/creator-publishing-pipeline/) | 内容流水线，生成多平台**草稿**包（默认绝不自动发布）。步骤来自一个插件包。 |

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

步骤还可以抛出 `StepDeferred` 等待前置条件（可指定延时，不计失败次数），在只完成了
部分工作时返回 `degraded=True`，或抛出任意异常表示失败并重试。会对外产生副作用的步骤
设置 `side_effect = True`，每次执行前都要经你批准。详见
[docs/writing-steps.md](docs/writing-steps.md)。

如果想让步骤自带配置、默认工作流、命令和 doctor 检查，并能用 `pip` 安装，就把它们
打包成插件，见 [docs/plugins.md](docs/plugins.md)。官方插件
[runspool-wechat](plugins/runspool-wechat/)（把 Markdown 排版成公众号格式，经审批后
存为草稿）就是一个完整的例子。

## 隐私与安全

- **本地优先**：所有状态都在你机器上的 `workspace_root` 下，默认不上传任何数据。
- **无需密钥**：引擎和内置步骤不需要任何 API key。需要密钥的插件（如
  runspool-wechat）只按名字引用凭证；值来自环境变量或仅本人可读的凭证文件，绝不会
  出现在配置、日志或错误信息里。
- **副作用先审批**：发布、上传、发送类步骤只有在你批准这一次尝试后才执行；没有可用
  的审批策略时，它会被拒绝，而不是被执行。
- **只出草稿，不自动发布**：内容类示例和公众号插件只生成草稿，发布始终是你手动、
  有意识的一步。

## 非目标（Non-goals）

- 不是托管/云端工作流平台。
- 不是分布式调度器，也不是重型编排系统的替代品。
- 不是 AI 产品（但刻意对 AI agent 友好）。
- 当前不做 Web UI——CLI 和 JSON 就是接口。

## 许可证

[MIT](LICENSE)。
