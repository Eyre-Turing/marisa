
<p align="center">
  <h1 align="center">✨ Marisa — 兴趣使然的 AI Agent</h1>
  <p align="center">
    一个简洁、跨平台的 AI Agent 工具，支持 MCP 协议扩展
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8+-blue?logo=python&logoColor=white" alt="Python Version"/>
  <img src="https://img.shields.io/badge/Platform-Windows%20|%20Linux-green" alt="Platform"/>
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License"/>
  <img src="https://img.shields.io/badge/Protocol-OpenAI%20|%20Anthropic-orange" alt="Supported Protocols"/>
</p>

---

## 📖 简介

**Marisa** 是一个轻量级的 AI Agent 工具，支持 **Windows** 和 **Linux** 双平台。它通过命令行与大模型交互，并可通过 **MCP（Model Context Protocol）** 协议连接外部服务，扩展 Agent 的能力边界。

> 🎯 名字取自东方 Project 的魔法使——雾雨魔理沙。像她一样，Marisa 也是一个爱收集各种"能力道具"（MCP 服务）的兴趣使然的 Agent。

---

## 🚀 快速开始

### 环境依赖

- Python 3.8+
- [prompt-toolkit](https://pypi.org/project/prompt-toolkit/)
- [rich](https://pypi.org/project/rich/)（**可选**，用于把大模型回复渲染成 markdown 样式）

```bash
pip3 install prompt_toolkit
# 可选：安装 rich 后，交互模式下大模型的回复会自动渲染表格 / 加粗 / 代码块
pip3 install rich
```

> `rich` 为**可选依赖**：未安装、非交互模式（管道/重定向）、或未使用 prompt_toolkit 时，一律退回原来的纯文本输出。
> 渲染仅作用于**大模型自己说的话**，工具函数/命令的输出始终保持原样。可用 `--no-markdown` 强制关闭渲染。
> - Python 3.6 上 rich 最高只能装到 12.6.0（其 Markdown 不支持表格），程序内置了轻量 GFM 表格渲染器自动补齐。
> - Windows（含 Git Bash/mintty、winpty、老式 conhost）上强制走 Win32 控制台 API，避免 ANSI 转义码被显示成 `?[1;36m` 乱码。

### 配置大模型

首次运行时会提示你输入 API Key 等信息，输入后自动保存到 `ai_agent_config.json`。你也可以手动创建该文件：

**支持的协议：** `openai`（兼容 DeepSeek、OpenAI 等） / `anthropic`

以 **DeepSeek** 为例（使用 OpenAI 协议）：

```json
{
  "api_key": "sk-xxxx",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-chat",
  "protocol": "openai"
}
```

当然，众所周知 **DeepSeek** 也支持 Anthropic 协议，因此你可以将 `protocol` 设置为 `anthropic` ，如下：

```json
{
  "api_key": "sk-xxxx",
  "base_url": "https://api.deepseek.com/anthropic",
  "model": "deepseek-chat",
  "protocol": "anthropic"
}
```

另外，你可以使用支持多模态的大模型来辅助你的大模型读图，比如 deepseek 本身不支持读图，但是可以使用 qwen3.8-max 来辅助读图，那就可以这样写：

```json
  "api_key": "sk-xxxx",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-v4-flash",
  "protocol": "openai",
  "mul_api_key": "sk-ws-xxxx",
  "mul_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "mul_model": "qwen3.8-max",
  "mul_protocol": "openai",
```

### 上下文、压缩与工具阈值

`ai_agent_config.json` 中还有四个阈值，首次运行会自动写入默认值，可自行修改：

```json
{
  "context_limit_tokens": "1m",
  "auto_compress_tokens": "700k",
  "llm_compress_tokens": 700000,
  "max_image_size_byte": "5mb"
}
```

| 配置项 | 含义 |
|---|---|
| `context_limit_tokens` | 当前模型的最大上下文 token 数，用于计算使用率百分比 |
| `auto_compress_tokens` | 达到该 token 数触发 **无人值守压缩**：程序在工具循环里自动压缩中间的工具调用（轻量，不调用大模型） |
| `llm_compress_tokens` | 达到该 token 数触发 **大模型压缩**：暂缓用户输入，先让大模型调用 `compress` 工具重写上下文（重量，消耗一次 API） |
| `max_image_size_byte` | `read_image` 单张图片大小上限，单位 **字节**（不带单位即按字节算，`"5mb"` = 5242880） |

> 💡 **前三个是 token 数**，支持 `k` / `m` 单位（`k = 1000`，`m = 1000000`），大小写与空格不限，也认千分位逗号：
> `1000000`、`"1m"`、`"1 M"`、`"700k"`、`"1500K"`、`"1.5m"`、`"1,000,000"` 都能识别
> （`"1500K"` 和 `"1.5m"` 等价，都是 1500000；不带单位则按绝对数值算）。
>
> 💡 **`max_image_size_byte` 是字节数**，不带单位按**字节**算，也可以带 `k` / `kb`（×1024）、`m` / `mb`（×1024×1024）
> 后缀——采用 1024 进位，与文件管理器的显示口径一致：
> `5242880`、`"5mb"`、`"5 M"`、`"512kb"`、`"8k"`、`"2.5mb"` 均可。默认 `5mb`（5242880 字节）。
>
> ⚠️ 注意两类单位含义不同：token 的 `k` / `m` 是 1000 进位，字节的 `k` / `m` 是 1024 进位。
>
> 缺失、非数字、`0` 或负数一律回退到默认值（`1m` / `700k` / `700k` / `5mb`）；启动时会打印当前生效的阈值。

大模型返回的 `usage` 会被自动解析，**每次回复末尾追加一行用量摘要**，方便一眼对照 token 与字节数：

```
📊 上下文 3,685 tok / 1,000,000（0.4%） ｜ 本次输出 163 tok（合计 3,848），缓存命中 3,456 ｜ 请求体 15,463 B（15.1 KB，消息 0.9 KB + 工具 14.2 KB） ｜ 约 4.20 B/tok
```

- **token 数取自 API 返回的真实 `usage`**（上下文按上次请求的 token / 请求体字节比例换算，每次 API 返回都会自我校正），不再是拍脑袋的字节估算；
- 字节数按 **`messages` + `tools` 的完整请求体**统计——这一点很关键：`prompt_tokens` 是**整个请求**的 token 数，包含工具 schema，而内置工具 + MCP 工具的 schema 动辄十几 KB，往往是请求体的绝对大头。若只统计 `messages`，短对话会得出「字节数 ≪ token 数」的假象（例如消息 0.9 KB 却对应 3,685 tok）；
- 同一行里给出换算比 `约 x B/tok`，正常落在 **3~4 字节/token** 附近，两者对照一目了然；
- 若接口未返回 `usage`，则退化为按字节粗略估算（约 4 字节 / token），并在数值前标 `≈`。

### 🎛️ 思考等级 / 供应商专有参数（请求直通）

各家 API 的「思考等级」「推理预算」这类参数**名字互不相同，而且都放在请求体（JSON body）里，不是请求头**。请求头只承担三件事：认证、协议版本、以及极少数 beta 功能开关。所以程序提供两个「原样透传」的出口，不需要认识每一个参数名：

```jsonc
{
  // 合并进请求体 JSON —— 思考等级、推理预算、输出上限等都写这里
  "extra_body": { "reasoning_effort": "high" },
  // 合并进 HTTP 请求头 —— beta 开关、额外鉴权头等写这里
  "extra_headers": { "anthropic-beta": "interleaved-thinking-2025-05-14" }
}
```

**常见供应商的对应参数**（名称与可用值以各家官方文档为准，不同版本会变）：

| 供应商 | 参数 | 位置 | 备注 |
|---|---|---|---|
| OpenAI（o 系列 / GPT-5） | `reasoning_effort`：`minimal` / `low` / `medium` / `high` | body | GPT-5 另有 `verbosity` |
| OpenAI Responses API | `reasoning: { effort, summary }` | body | 本程序走 chat.completions，一般用不上 |
| Anthropic（Claude） | `thinking: { type: "enabled", budget_tokens: N }` | body | 需同时把 `max_tokens` 调到**大于** `budget_tokens`；且不能同时改 `temperature` / `top_p` |
| Anthropic | `anthropic-beta: interleaved-thinking-2025-05-14` | **header** | 少数「开关在头里」的例子 |
| DeepSeek | 用**模型名**切换：`deepseek-reasoner`（思考）/ `deepseek-chat`（非思考） | body | 推理内容从 `reasoning_content` 自动解析 |
| 通义千问（DashScope） | `enable_thinking: true`、`thinking_budget: N` | body | OpenAI 兼容模式下走这里 |
| 智谱 GLM | `thinking: { type: "enabled" }` | body | |

**Anthropic 开扩展思考的完整写法**（`max_tokens` 必须一起调大，否则 400）：

```jsonc
{
  "extra_body": {
    "thinking": { "type": "enabled", "budget_tokens": 16000 },
    "max_tokens": 32000
  }
}
```

几点说明：

- **结构性字段受保护**：`messages` / `tools` / `tool_choice` / `stream` / `model` 由程序自己构造，写在 `extra_body` 里会被忽略并给出警告——否则请求结构会被破坏。`max_tokens` **不在**保护名单里，正是为了让你调扩展思考的输出上限。
- **额外请求头**：`Content-Length` / `Content-Type` 由程序管理，写在 `extra_headers` 里会被忽略。
- **辅助模型**：多模态辅助模型有独立的 `mul_extra_body` / `mul_extra_headers`（同样只在配置了 `mul_*` 后生效）。
- **启动可见**：配了才会打印，方便确认有没有吃上：
  ```
  ⚙️ 附加请求参数: 请求体 {"reasoning_effort": "high"} ｜ 请求头 {"X-Custom": "1"}
  ```
- 透传的值原样进 JSON，可以是对象 / 数组 / 数字 / 布尔等任意结构。未配置（`{}` 或字段缺失）时，请求体与改造前**完全一致**，没有任何副作用。

### 启动

**Linux：**
```bash
./marisa
```

**Windows — CMD 终端：**
```bash
marisa
```

**Windows — Bash 终端（Git Bash 等）：**
```bash
./marisa
```

### 🎛️ 命令行参数

**`-r` / `--resume`**：从日志文件恢复会话（只读，不写回该文件）
```bash
./marisa -r /path/to/log/file.log
```

**`-c` / `--context-log`**：指定上下文日志文件并持续写入（增强版 `-r`）
- 若文件**不存在**：自动创建（含父目录），作为本次会话的上下文日志
- 若文件**已存在**：先加载其中的快照恢复会话，后续快照继续追加写入该文件

```bash
# 指定一个全新路径 → 创建文件并作为会话日志
./marisa -c /tmp/context.log

# 复用之前的上下文日志 → 自动加载上次会话，并继续写入
./marisa -c /tmp/context.log
```

> ⚠️ `-r` 与 `-c` 互斥，**不能同时指定**，否则会报错退出。

---

## 🔌 连接 MCP 服务

Marisa 支持通过 MCP 协议连接外部工具服务。以 **Blender MCP** 为例：

### 准备工作

1. 确保电脑已安装 [Blender](https://www.blender.org/) 并启动 MCP 服务
2. 安装 [uvx](https://docs.astral.sh/uv/) 工具（uv 包管理器）

> 💡 如果没有安装 uvx，可参考 [这篇文章](https://zhuanlan.zhihu.com/p/1974065640361977322) 进行安装

### 配置 MCP

在项目目录下编辑 `mcp_config.json` 文件（将 `C:\\Users\\User\\.local\\bin\\uvx.exe` 替换为本地实际路径）：

```json
{
  "mcp_servers": [
    {
      "name": "blender",
      "transport": "stdio",
      "enabled": true,
      "command": "C:\\Users\\User\\.local\\bin\\uvx.exe",
      "args": ["blender-mcp"],
      "env": {},
      "auto_connect": true,
      "tool_prefix": "blender_",
      "timeout": 180,
      "debug": false
    }
  ]
}
```

考虑到最近 blender-mcp 搞事情，把 api 版本更新了，导致不兼容低版本，可以在 args 里指定使用低版本，如强制使用 1.29.0 版本：

```json
{
  "mcp_servers": [
    {
      "name": "blender",
      "transport": "stdio",
      "enabled": true,
      "command": "C:\\Users\\User\\.local\\bin\\uvx.exe",
      "args": ["--with", "mcp==1.29.0", "blender-mcp"],
      "env": {},
      "auto_connect": true,
      "tool_prefix": "blender_",
      "timeout": 180,
      "debug": false
    }
  ]
}
```

### 使用

启动 Marisa 后，MCP 服务会自动连接。你只需要告诉 Marisa 要在 Blender 里做什么，它就会自动调用 Blender MCP 的工具帮你建模！

### 使用 HTTP 传输协议

除了通过 `stdio` 启动本地进程外，MCP 服务也可以通过 **Streamable HTTP** 传输协议连接。此时服务端是一个暴露在 `http://` 或 `https://` 上的 HTTP 端点，Marisa 通过 `JSON-RPC 2.0 over HTTP POST` 与其通信（响应支持纯 JSON 与 `text/event-stream`（SSE）两种格式）。

配置方式与 `stdio` 类似，只需把 `transport` 设为 `"http"` 并指定 `url` 即可。以连接某个远程 MCP 服务为例：

```json
{
  "mcp_servers": [
    {
      "name": "remote_mcp",
      "transport": "http",
      "url": "https://example.com/mcp",
      "enabled": true,
      "auto_connect": true,
      "tool_prefix": "remote_",
      "timeout": 60,
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      },
      "debug": false
    }
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | :-: | --- |
| `name` | 是 | 服务名称（唯一标识） |
| `transport` | 是 | 固定为 `"http"` |
| `url` | 是 | MCP 服务的 HTTP 端点地址 |
| `enabled` | 否 | 是否启用，默认 `true` |
| `auto_connect` | 否 | 启动 Marisa 时是否自动连接，默认 `true` |
| `tool_prefix` | 否 | 给工具名加前缀，避免与其他 MCP 服务冲突 |
| `timeout` | 否 | 请求超时时间（秒），默认 `60` |
| `headers` | 否 | 附加的 HTTP 请求头，常用于鉴权（如 `Authorization`） |
| `debug` | 否 | 是否打印 HTTP 请求/响应日志，默认 `false` |

> 💡 需要鉴权的服务，把令牌放到 `headers` 里即可，例如：`"headers": {"Authorization": "Bearer sk-xxxx"}`。

---

## 🧩 自定义技能

Marisa 支持加载自定义技能（Skill）文件，让 Agent 获得特定领域的专业知识。技能文件位于 `skills/` 目录下，在对话中会根据需要自动加载。

---

## 📁 项目结构

```
marisa/
├── marisa                  # 启动脚本（Linux / Bash）
├── marisa.bat              # 启动脚本（Windows CMD）
├── ai_agent_prompt.py      # 主程序：配置 / API 调用 / 输入处理 / 对话主循环
├── ai_agent_tools.py       # 工具层：工具定义与实现、MCP 注册、技能加载
├── ai_agent_config.json    # 大模型配置（自动生成）
├── mcp_manager.py          # MCP 服务管理
├── mcp_config.json         # MCP 服务配置
├── skills/                 # 技能知识库
└── README.md               # 就是本文件啦 ✨
```

### 🧩 两个核心模块

- **`ai_agent_prompt.py`（主程序）**：负责配置读取、大模型 API 调用（OpenAI / Anthropic 协议）、终端输入输出、信号处理，以及最关键的工具调用主循环。
- **`ai_agent_tools.py`（工具层）**：负责工具的 JSON schema 定义、各工具函数的具体实现（命令执行、文件读写、图片读取、后台任务、技能加载等）、工具名到函数的映射表 `tool_func_map`，以及 MCP 工具的动态注册。

> 💡 工具函数有时需要访问主程序里的运行时状态（如全局对话历史 `messages`、中断标志 `interrupted`、子 Agent 调用 `call_api()` 等）。为此主程序启动时会把自己的模块对象通过 `ai_agent_tools.bind_runtime(...)` 注入，工具层再以 `_RT.<name>` 的形式访问这些共享状态——既避免了两个模块之间的循环导入，也保证了状态始终一致。

---

## 🧪 技术特性

- 🔄 **跨平台**：Windows & Linux 开箱即用
- 🧠 **多协议支持**：兼容 OpenAI 和 Anthropic 协议的大模型
- 🔌 **MCP 扩展**：通过 MCP 协议连接外部工具（Blender 等）
- 📚 **技能系统**：可按需加载领域知识，让 Agent 更聪明
- 📊 **真实 token 统计**：解析 API 返回的 `usage`，每次回复后展示 token 与字节数对照，压缩 / 图片大小等阈值均可配置（支持 `k` / `m` 单位）
- 🎛️ **请求直通**：`extra_body` / `extra_headers` 原样透传供应商专有参数（思考等级、扩展思考、beta 开关等），无需程序逐个适配
- ⚡ **轻量简洁**：依赖少，启动快，即装即用

---

## 📜 许可证

[MIT](LICENSE)

---

<p align="center">
  <i>兴趣使然，自娱自乐 🌟</i>
</p>
