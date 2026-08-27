
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

```bash
pip3 install prompt_toolkit
```

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
├── ai_agent_prompt.py      # AI Agent 核心逻辑
├── ai_agent_config.json    # 大模型配置（自动生成）
├── mcp_manager.py          # MCP 服务管理
├── mcp_config.json         # MCP 服务配置
├── skills/                 # 技能知识库
└── README.md               # 就是本文件啦 ✨
```

---

## 🧪 技术特性

- 🔄 **跨平台**：Windows & Linux 开箱即用
- 🧠 **多协议支持**：兼容 OpenAI 和 Anthropic 协议的大模型
- 🔌 **MCP 扩展**：通过 MCP 协议连接外部工具（Blender 等）
- 📚 **技能系统**：可按需加载领域知识，让 Agent 更聪明
- ⚡ **轻量简洁**：依赖少，启动快，即装即用

---

## 📜 许可证

[MIT](LICENSE)

---

<p align="center">
  <i>兴趣使然，自娱自乐 🌟</i>
</p>
