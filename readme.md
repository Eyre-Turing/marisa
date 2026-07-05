
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
      "timeout": 180
    }
  ]
}
```

### 使用

启动 Marisa 后，MCP 服务会自动连接。你只需要告诉 Marisa 要在 Blender 里做什么，它就会自动调用 Blender MCP 的工具帮你建模！

---

## 🧩 自定义技能

Marisa 支持加载自定义技能（Skill）文件，让 Agent 获得特定领域的专业知识。技能文件位于 `skills/` 目录下，在对话中会根据需要自动加载。

---

## 📁 项目结构

```
marisa/
├── marisa                  # 启动脚本（Linux / Bash）
├── marisa.bat              # 启动脚本（Windows CMD）
├── main.py                 # 主程序入口
├── ai_agent_prompt.py      # AI Agent 核心逻辑
├── ai_agent_config.json    # 大模型配置（自动生成）
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
