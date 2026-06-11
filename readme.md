# 说明

这是一个非常简单的 AI Agent 工具，可以在 Windows 和 Linux 之间通用。

# 环境依赖

依赖 python3 ，需要安装 prompt-toolkit 库

```bash
pip3 install prompt_toolkit
```

# 使用方法

第一次调用如果没有提前在 ai_agent_config.json 文件配置大模型 api key 等信息，会提示让你输入，输入后将自动保存大模型配置。

ai_agent_config.json 文件示例如下：

```json
{
  "api_key": "sk-xxxx",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-chat"
}
```

## Linux 下

```bash
./marisa
```

## Windows 下

### cmd 终端

```bash
marisa
```

### bash 终端（git bash 等）

```bash
./marisa
```
