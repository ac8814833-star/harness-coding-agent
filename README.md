# Harness Coding Agent

面向本地代码仓库的命令行编码助手，支持读取项目上下文、修改文件、执行命令，以及保存和恢复会话。本仓库用于个人学习、开发与实验。

## 功能

- 仓库上下文：读取工作目录、项目说明和代码，为任务提供上下文。
- 工具执行：通过工具读取文件、应用修改和运行 shell 命令。
- 交互与单次任务：支持连续对话，也可以直接传入任务后退出。
- 会话与记忆：保存会话状态、工作记忆和运行工件，支持恢复会话。
- 多模型后端：支持 DeepSeek、OpenAI 兼容 Responses 接口、Anthropic 兼容 Messages 接口，以及本地 Ollama。
- 执行控制：支持审批策略、最大执行步数和敏感环境变量脱敏配置。

## 环境要求

- Python 3.10 或以上。
- 使用远程模型时，需要相应服务的接口地址、模型名称和 API Key。
- 使用本地模型时，需要先安装并启动 Ollama，准备好对应模型。

模型是否可用取决于实际接入的服务。`.env.example` 中的地址和模型名是当前项目的配置示例，请按所用服务调整。

## 安装与配置

以下命令在项目根目录的 PowerShell 中执行。

### 1. 安装到独立环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

后续示例直接使用虚拟环境中的程序，无需激活环境。

### 2. 创建本地配置

如果还没有 `.env`，复制示例配置：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
notepad .env
```

默认后端是 `deepseek`。打开 `.env`，根据注释填写对应后端的 API Key，并确认接口地址和模型名称与所用服务一致。只需配置实际使用的后端。

真实密钥保存在本地 `.env`，该文件已由 `.gitignore` 排除。变量名以 `.env.example` 为准；解析器不展开 `$变量名`，请直接填写字面值。

| 后端 | `--provider` 参数 | 配置要求 |
| --- | --- | --- |
| DeepSeek | `deepseek` | 对应接口地址、模型名称和 API Key |
| OpenAI 兼容接口 | `openai` | 兼容 Responses 协议的接口地址、模型名称和 API Key |
| Anthropic 兼容接口 | `anthropic` | 兼容 Messages 协议的接口地址、模型名称和 API Key |
| Ollama | `ollama` | 已启动的本地服务和已准备好的模型 |

远程兼容接口的示例配置包含第三方网关地址。切换服务时，需要同步调整地址、模型和密钥，避免向错误的服务发送请求。

## 启动

安装后，命令入口由 `pyproject.toml` 的 `[project.scripts]` 定义。下面的 PowerShell 命令读取当前配置中的入口名称，并启动程序：

```powershell
$entry = Select-String -Path .\pyproject.toml -Pattern '^([A-Za-z0-9_-]+)\s*=\s*"[^"]+:main"\s*$'
$commandName = $entry.Matches[0].Groups[1].Value
$agent = Join-Path '.\.venv\Scripts' ($commandName + '.exe')
& $agent
```

在同一个 PowerShell 窗口中，后续可复用 `$agent` 执行下列命令。

执行一次任务：

```powershell
& $agent "阅读项目结构，说明程序入口和主要模块"
```

指定另一个工作目录：

```powershell
& $agent --cwd 'D:\path\to\project'
```

选择已配置好的模型后端：

```powershell
& $agent --provider openai
& $agent --provider anthropic
```

恢复最近一次会话：

```powershell
& $agent --resume latest
```

查看全部参数：

```powershell
& $agent --help
```

常用参数包括 `--model`、`--base-url`、`--approval` 和 `--max-steps`。默认审批策略为 `ask`；执行修改文件或 shell 命令等操作时，按程序提示处理审批。

## 交互命令

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看交互帮助 |
| `/memory` | 查看工作记忆 |
| `/session` | 查看当前会话文件路径 |
| `/reset` | 重置当前会话状态 |
| `/exit`、`/quit` | 退出程序 |

## 代码组织

- 命令行入口：负责参数解析、配置加载和交互循环。
- 模型适配：封装不同模型服务的请求与响应处理。
- 执行工具：提供文件读取、内容修改和命令执行能力。
- 上下文与记忆：管理工作区信息、会话历史和持久化记忆。
- 评估模块：包含基准任务执行和指标统计工具。
- 辅助目录：`tests/` 保存测试，`benchmarks/` 保存评估数据，`scripts/` 保存辅助脚本，`examples/` 保存示例。

会话、记忆和运行工件保存在所选工作区的本地隐藏运行目录中。每次运行可生成 `task_state.json`、`trace.jsonl` 和 `report.json`；这些本地记录不随本仓库提交。

## 开发与验证

在上述虚拟环境中安装开发工具：

```powershell
.\.venv\Scripts\python.exe -m pip install "pytest>=8.3.5" "ruff>=0.4.4"
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check .
```

本仓库的 Markdown 上传规则仅保留根目录 `README.md`。其他 Markdown 文档仍可保留在本地，但不会上传。部分测试和基准任务依赖这些文档或 Markdown 测试数据，因此重新克隆后，如需完整运行相应检查，需要补齐本地文件。
