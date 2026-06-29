#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
兴趣使然的AI Agent —— 魔理沙风格
使用 prompt_toolkit 支持多行输入！

用法：
  pip install prompt_toolkit
  python ai_agent_prompt.py

多行输入：回车换行，Alt+Enter（或 Esc+Enter）提交
Ctrl+C 中断工具调用（回到对话），在输入时 Ctrl+C 退出
Ctrl+D 退出程序
"""

import os
import json
import subprocess
import urllib.request
import urllib.error
import locale
import signal
import sys
import difflib, re
import threading
import datetime
import pathlib

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings


# ============================================================
#  配置管理 —— 从同级 JSON 文件读取 API 配置
# ============================================================

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_agent_config.json")

DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "protocol": "openai"
}

_config_cache = None


def load_config(force_reload=False):
    """加载配置文件，如果不存在或字段缺失则提示用户输入并自动保存"""
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache

    config = {}
    need_save = False

    # 尝试从文件加载
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, IOError):
            config = {}
            need_save = True

    # 检查必要字段，缺失则提示输入
    for key in ["api_key", "base_url", "model", "protocol"]:
        if key not in config or not config[key]:
            need_save = True
            if key == "api_key":
                print("🔑 请输大模型API密钥 (api_key)：", end="", flush=True)
                value = sys.stdin.readline().strip()
                config[key] = value
            elif key == "base_url":
                default = DEFAULT_CONFIG["base_url"]
                print(f"🌐 请输入API基础地址 (base_url) [默认: {default}]：", end="", flush=True)
                value = sys.stdin.readline().strip()
                config[key] = value if value else default
            elif key == "model":
                default = DEFAULT_CONFIG["model"]
                print(f"🤖 请选择模型名称 (model) [默认: {default}]：", end="", flush=True)
                value = sys.stdin.readline().strip()
                config[key] = value if value else default
            elif key == "protocol":
                default = DEFAULT_CONFIG["protocol"]
                print(f"🔄 请选择协议 (protocol) [默认: {default}, 支持 openai / anthropic]：", end="", flush=True)
                value = sys.stdin.readline().strip().lower()
                if value and value in ("openai", "anthropic"):
                    config[key] = value
                else:
                    config[key] = default

    if need_save:
        save_config(config)

    _config_cache = config
    return config


def save_config(config):
    """保存配置到 JSON 文件"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print(f"   ✅ 配置已保存到 {CONFIG_FILE}", flush=True)
    except IOError as e:
        print(f"   ❌ 保存配置失败: {e}", flush=True)


# ============================================================
#  信号处理 —— 区分「工具执行中」和「用户输入中」
# ============================================================
tool_executing = False
interrupted = False


def sigint_handler(signum, frame):
    """Ctrl+C 信号处理器"""
    global interrupted, tool_executing
    if tool_executing:
        # 工具执行中 → 设中断标志，让循环自己收工
        if not interrupted:
            interrupted = True
            print("\n\n⚠️  中断魔法吟唱！回到对话模式...\n", flush=True)
    # 用户输入中：交给 prompt_toolkit 处理（默认引发 KeyboardInterrupt）


# ============================================================
#  0. 上下文压缩相关常量 & 全局 messages
# ============================================================
# 上下文上限（DeepSeek 1M 上下文窗口）
CONTEXT_LIMIT = 1_000_000

# 压缩阈值：JSON 字节数超过此值时自动提醒 AI 压缩
# 设为 700K 字节（约 70%），留出余量避免撞墙
COMPRESS_THRESHOLD = 700_000

# messages 提升到全局，方便 compress 工具直接修改
messages = []


def get_context_size(msg_list):
    """估算 messages 的 JSON 字节数（接近 API 实际传输量）"""
    json_str = json.dumps(msg_list, ensure_ascii=False)
    return len(json_str.encode('utf-8'))


def get_context_aware_messages(msg_list):
    """
    如果上下文超过阈值，在末尾追加一条 system 提醒（不修改原列表）。
    这样 AI 能看到当前使用量，主动考虑压缩。
    """
    ctx_size = get_context_size(msg_list)
    if ctx_size > COMPRESS_THRESHOLD:
        pct = round(ctx_size / CONTEXT_LIMIT * 100, 1)
        reminder = {
            "role": "system",
            "content": (
                f"[上下文使用率：{pct}%（{ctx_size//1000}K / {CONTEXT_LIMIT//1000}K），"
                "对话历史较长，建议考虑调用 compress 工具压缩历史对话以节省空间。"
                "压缩时请保留 system prompt 和最近 2-3 轮完整对话，"
                "更早的内容用一段摘要替代。]"
            )
        }
        return msg_list + [reminder]
    return msg_list


# ============================================================
#  日志记录 — 每次 API 返回后记录 messages 快照到单个文件
# ============================================================

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")
# 日志文件路径，在 main() 启动时初始化
_log_file = None
_err_log_file = None


def _ensure_log_dir():
    """确保 log 目录存在"""
    pathlib.Path(LOG_DIR).mkdir(parents=True, exist_ok=True)


def _timestamp():
    """生成时间戳，格式同 date +%Y-%m-%d_%H-%M-%S_%N"""
    now = datetime.datetime.now()
    nanos = f"{now.microsecond:06d}000"
    return now.strftime(f"%Y-%m-%d_%H-%M-%S") + f"_{nanos}"


def init_logger():
    """程序启动时初始化日志文件路径（以启动时间命名）"""
    global _log_file, _err_log_file
    _ensure_log_dir()
    ts = _timestamp()
    _log_file = os.path.join(LOG_DIR, f"{ts}.log")
    _err_log_file = os.path.join(LOG_DIR, f"{ts}.err.log")
    # 写一条启动标记
    try:
        with open(_log_file, "w", encoding="utf-8") as f:
            f.write(f"===== Session started at {ts} =====\n")
    except Exception:
        pass


def save_messages_snapshot(msg_list):
    """把当前 messages 追加到日志文件中"""
    if _log_file is None:
        return
    try:
        data = json.dumps(msg_list, ensure_ascii=False, indent=2)
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(f"\n--- snapshot {_timestamp()} ---\n")
            f.write(data)
            f.write("\n")
    except Exception:
        pass  # 日志写入失败不影响主流程


def save_error_snapshot(msg_list, error_msg):
    """出错时保存 messages 到 err 日志文件"""
    if _err_log_file is None:
        return
    try:
        data = json.dumps(msg_list, ensure_ascii=False, indent=2)
        with open(_err_log_file, "a", encoding="utf-8") as f:
            f.write(f"\n--- error {_timestamp()} ---\n")
            f.write(data)
            f.write(f"\n===== ERROR =====\n{error_msg}\n")
    except Exception:
        pass  # 日志写入失败不影响主流程


# ============================================================
#  1. 调用 DeepSeek Chat API（纯标准库，不依赖 openai）
# ============================================================
def call_api(messages, tools=None, tool_choice="auto"):
    """
    根据配置中的 protocol 字段，调用 OpenAI 风格或 Anthropic 风格的 API。
    直接构造 HTTP 请求（从 ai_agent_config.json 读取参数）。
    """
    config = load_config()
    api_key = config.get("api_key")
    if not api_key:
        raise ValueError("API 密钥未配置！请检查 ai_agent_config.json 文件。")

    protocol = config.get("protocol", "openai").lower()

    try:
        if protocol == "anthropic":
            result = _call_anthropic_api(config, messages, tools)
        else:
            result = _call_openai_api(config, messages, tools, tool_choice)
        return result
    except Exception as e:
        # 出错时保存 err 日志
        save_error_snapshot(messages, str(e))
        raise


def _call_openai_api(config, messages, tools=None, tool_choice="auto"):
    """OpenAI 风格 API 调用（兼容 DeepSeek 等）"""
    api_key = config["api_key"]
    url = config.get("base_url", DEFAULT_CONFIG["base_url"]).rstrip("/")
    if not url.endswith("/chat/completions"):
        if url.endswith("/v1"):
            url = url + "/chat/completions"
        else:
            url = url + "/v1/chat/completions"

    model = config.get("model", DEFAULT_CONFIG["model"])

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    payload = {
        "model": model,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API 请求失败 (HTTP {e.code}): {error_body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"API 请求失败 (网络错误): {e.reason}")

    # 提取消息
    choice = result["choices"][0]
    msg = choice["message"]

    # 提取 reasoning_content（如果存在）
    reasoning_content = msg.get("reasoning_content")

    return msg, reasoning_content


def _call_anthropic_api(config, messages, tools=None):
    """
    Anthropic 风格 API 调用。
    将内部 OpenAI 格式的 messages 转换为 Anthropic 格式，
    并处理响应中的 tool_use content blocks。
    """
    api_key = config["api_key"]
    url = config.get("base_url", "https://api.anthropic.com").rstrip("/")
    if not url.endswith("/messages"):
        if url.endswith("/v1"):
            url = url + "/messages"
        else:
            url = url + "/v1/messages"

    model = config.get("model", "claude-3-5-sonnet-20241022")

    # ---------- 转换消息格式 ----------
    # Anthropic 的 system prompt 是单独参数，不在 messages 里
    system_prompt = None
    anthropic_messages = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            # Anthropic 的 system 是顶层参数
            system_prompt = content
            continue
        elif role == "user":
            # 检查是否是 tool_result（Anthropic 的 tool_result 用 role: user + content blocks）
            if isinstance(content, list):
                # 已经是 Anthropic 格式的 content blocks，直接使用
                anthropic_messages.append({"role": "user", "content": content})
            else:
                anthropic_messages.append({"role": "user", "content": content})
        elif role == "assistant":
            # 检查是否有 tool_calls
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                # 构建 Anthropic 格式的 content blocks
                content_blocks = []
                if content:
                    content_blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    try:
                        tc_args = json.loads(tc["function"]["arguments"])
                    except (json.JSONDecodeError, KeyError):
                        tc_args = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": tc_args
                    })
                anthropic_messages.append({"role": "assistant", "content": content_blocks})
            else:
                anthropic_messages.append({"role": "assistant", "content": content})
        elif role == "tool":
            # 将 OpenAI 的 tool role 转换为 Anthropic 的 user role + tool_result block
            tool_call_id = msg.get("tool_call_id", "")
            tool_name = msg.get("name", "")
            tool_content = msg.get("content", "")
            
            # 尝试解析 tool_content 为 JSON（因为我们的工具返回的都是 JSON 字符串）
            try:
                tool_content_parsed = json.loads(tool_content)
                # 如果是 JSON 对象，提取关键信息供 Anthropic 使用
                if isinstance(tool_content_parsed, dict):
                    anthropic_messages.append({
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_call_id,
                                "content": tool_content
                            }
                        ]
                    })
                else:
                    anthropic_messages.append({
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_call_id,
                                "content": str(tool_content_parsed)
                            }
                        ]
                    })
            except (json.JSONDecodeError, TypeError):
                # 不是 JSON，直接当文本
                anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call_id,
                            "content": tool_content
                        }
                    ]
                })

    # ---------- 转换工具定义 ----------
    anthropic_tools = None
    if tools:
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                anthropic_tools.append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}})
                })

    # ---------- 构建请求体 ----------
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01"
    }

    payload = {
        "model": model,
        "max_tokens": 8192,
        "messages": anthropic_messages,
    }
    if system_prompt:
        payload["system"] = system_prompt
    if anthropic_tools:
        payload["tools"] = anthropic_tools

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic API 请求失败 (HTTP {e.code}): {error_body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Anthropic API 请求失败 (网络错误): {e.reason}")

    # ---------- 转换响应为 OpenAI 格式（方便上层统一处理） ----------
    # Anthropic 响应结构：
    # {
    #   "content": [{"type": "text", "text": "..."}, {"type": "tool_use", "id": "...", "name": "...", "input": {...}}],
    #   "role": "assistant",
    #   ...
    # }
    
    msg = {"role": "assistant", "content": "", "tool_calls": None}
    reasoning_content = None

    content_blocks = result.get("content", [])
    text_parts = []
    tool_calls_list = []

    for block in content_blocks:
        block_type = block.get("type", "")
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "tool_use":
            tool_calls_list.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}))
                }
            })

    msg["content"] = "".join(text_parts)
    if tool_calls_list:
        msg["tool_calls"] = tool_calls_list

    # 提取 stop_reason 作为额外信息
    stop_reason = result.get("stop_reason", "")
    if stop_reason == "end_turn":
        pass  # 正常结束
    elif stop_reason == "tool_use":
        pass  # 有工具调用，已处理

    return msg, reasoning_content

# ============================================================
#  2. 工具定义 & 执行
# ============================================================
tools = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "在本地执行一条bash命令并返回结果。正常情况结果为一个json，有stdout、stderr、code三个字段，stdout为标准输出（字符串），stderr为标准错误（字符串），code为返回值（数字）。如果异常，结果为空字符串",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的bash命令行"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间，单位为秒，如果不传默认为10"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_full_file",
            "description": "写入整个文本文件，如果文件不存在将创建，如果文件存在将覆盖。不会额外创建目录，欲写入文件所在的文件夹必须存在。返回为一个json，有filename、success、err三个字段。filename（字符串）会回传文件名；success（数字）为0则为失败，否则为成功；err（字符串）为失败原因，成功时无需关注",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "欲写入文件的完整文件名"
                    },
                    "data": {
                        "type": "string",
                        "description": "文件的内容"
                    }
                },
                "required": ["filename", "data"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_full_file",
            "description": "读取整个文本文件的内容并返回。返回为一个json，有filename、content、success、err四个字段。filename（字符串）为文件名；content（字符串）为文件内容；success（数字）为0则为失败，否则为成功；err（字符串）为失败原因，成功时为空字符串。注意：不会将文件内容输出到终端，避免聊天信息混乱",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "欲读取文件的完整文件名"
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_lines",
            "description": "读取文件中指定行范围的内容，返回带行号的文本。行号从1开始。用于在编辑文件前确认某几行的实际内容，或配合 edit_file_match 的模糊匹配结果来定位精确文本。返回为一个json，有filename、content、start_line、end_line、total_lines、success、err字段。content为带行号前缀的文本（格式如 '  1 | hello'），方便确认行号和内容对应关系",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "欲读取文件的完整文件名"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（从1开始），不传则默认为1"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（包含，从1开始），不传则默认为文件末尾"
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compress",
            "description": "压缩对话历史！传入压缩后的完整 messages 列表来替换当前对话历史（会清空旧历史）。注意：①必须保留 system prompt 作为第一条消息！②建议保留最近 2-3 轮完整对话以确保连贯性，更早的内容用一段摘要替代。③摘要中要涵盖之前讨论过的关键信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "compressed_messages": {
                        "type": "array",
                        "description": "压缩后的完整 messages 列表，必须包含 system prompt 作为第一条",
                        "items": {"type": "object"}
                    }
                },
                "required": ["compressed_messages"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file_lines",
            "description": "编辑文件中指定行范围的内容，并以 git diff 风格展示改动。行号从1开始计数。成功返回 unified diff 格式的差异。⚠️重要提示：①如果之前读取过文件但之后又做过编辑，行号可能已变化，请先用 read_file_lines 重新确认行号。②不要连续多次编辑同一文件而不重新读取——行号会漂移。③如果不确定行号，请改用 edit_file_match 工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要编辑的文件名（完整路径）"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（从1开始）"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（包含，从1开始）"
                    },
                    "new_content": {
                        "type": "string",
                        "description": "替换成的新内容（多行文本，不要带行号，纯内容）"
                    }
                },
                "required": ["filename", "start_line", "end_line", "new_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file_match",
            "description": "通过内容锚定来编辑文件，类似执行伪代码 file(filename).replace(old_content, new_content)。工具会在文件中找到唯一匹配 old_content 的位置并替换为 new_content。如果匹配不到会自动做模糊搜索，返回最相似位置的实际内容供你调整。⚠️重要技巧：①请先用 read_full_file 或 read_file_lines 读取文件，然后从返回结果中**原样复制**要修改的那段文本作为 old_content——不要自己手写 old_content，否则极易因空格/缩进差异导致匹配失败。②如果匹配到多个位置，会报错并列出所有匹配行号，请用 read_file_lines 查看后扩大 old_content 上下文使其唯一。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要编辑的文件名（完整路径）"
                    },
                    "old_content": {
                        "type": "string",
                        "description": "文件中要被替换的那段原文。必须与文件中的内容完全一致（包括空格、缩进、换行）。强烈建议从 read_full_file 或 read_file_lines 的返回结果中复制，不要自己手写"
                    },
                    "new_content": {
                        "type": "string",
                        "description": "替换成的新内容。会完全替换 old_content 匹配到的位置，所以如果 old_content 中有不需修改的部分，请在新内容中也保留那些部分"
                    }
                },
                "required": ["filename", "old_content", "new_content"]
            }
        }
    },
]


# 终止型工具集合：执行这些工具后会直接结束本轮工具调用循环
# 因为这类工具（如 compress）会修改全局 messages 状态，
# 后续的 tool response 追加和 API 调用会基于错误的上文继续执行
TERMINAL_TOOLS = {"compress"}


def smart_decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    sys_enc = locale.getpreferredencoding()
    if sys_enc.lower() not in ("utf-8", "utf8"):
        try:
            return data.decode(sys_enc)
        except UnicodeDecodeError:
            pass
    for enc in ["gbk", "gb2312", "gb18030", "big5", "shift_jis"]:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_bash(command, timeout=10):
    """执行一条 bash 命令，打印人类可读结果，返回 json.dumps 后的字符串
    
    使用 Popen + 进程组 + watchdog 线程实现真正的超时机制，
    即使用户跑了交互命令（top、python、read 等）也能强制结束。
    """
    global interrupted
    # 🛡️ 类型护盾！防止 DeepSeek 模型乱传参数
    if not isinstance(command, str) or not command.strip():
        result_dict = {"stdout": "", "stderr": f"无效的command参数: {repr(command)}", "code": -2}
        print(f"\n{result_dict['stderr']}\n", flush=True)
        return json.dumps(result_dict)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 10

    # 如果已经被中断，直接跳过
    if interrupted:
        result_dict = {"stdout": "", "stderr": "用户中断了魔法吟唱", "code": -1}
        print(f"\n{result_dict['stderr']}\n", flush=True)
        return json.dumps(result_dict)

    print(f"\n🔮 发动魔法，咒语: {command}, 超时时间: {timeout}s\n", flush=True)

    # ====== 新方案：使用 Popen + 进程组 + watchdog 线程 ======
    try:
        # 创建子进程，设置新的进程组，这样我们可以杀整个进程树
        # Unix: 用 preexec_fn=os.setsid 创建新会话（进程组）
        # Windows: 用 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        extra_kwargs = {}
        if sys.platform == "win32":
            extra_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            extra_kwargs["preexec_fn"] = os.setsid
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **extra_kwargs
        )

        result_container = {
            "stdout": b"",
            "stderr": b"",
            "timed_out": False,
            "exception": None
        }

        def watchdog():
            """看门狗线程：超时后强杀进程组"""
            import time as _time
            _time.sleep(timeout)
            if proc.poll() is None:
                # 进程还在跑，强杀！
                result_container["timed_out"] = True
                try:
                    # 先尝试杀进程树
                    if sys.platform == "win32":
                        # Windows: 用 taskkill 杀进程树
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=5
                        )
                    else:
                        # Unix/Linux: 杀进程组（负 PID 表示进程组）
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    # 如果上面失败，至少杀进程自己
                    try:
                        proc.kill()
                    except Exception:
                        pass
                # 确保读取剩余输出（防止管道死锁）
                try:
                    stdout_remain, stderr_remain = proc.communicate(timeout=3)
                    if result_container["stdout"] == b"":
                        result_container["stdout"] = stdout_remain
                    if result_container["stderr"] == b"":
                        result_container["stderr"] = stderr_remain
                except Exception:
                    pass

        # 启动看门狗线程
        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()

        # 主线程读取输出（有超时机制在 watchdog 里）
        try:
            stdout_data, stderr_data = proc.communicate(timeout=timeout + 5)
            result_container["stdout"] = stdout_data
            result_container["stderr"] = stderr_data
        except subprocess.TimeoutExpired:
            # communicate 超时，但 watchdog 已经或即将杀掉进程
            # 等待进程结束
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # 实在等不到就暴力
                try:
                    proc.kill()
                except Exception:
                    pass
                proc.wait()
            # 读取剩余输出
            try:
                stdout_data, stderr_data = proc.communicate(timeout=3)
                if result_container["stdout"] == b"":
                    result_container["stdout"] = stdout_data
                if result_container["stderr"] == b"":
                    result_container["stderr"] = stderr_data
            except Exception:
                pass

        # 等待看门狗线程结束（最多等 2 秒）
        watchdog_thread.join(timeout=2)

        if result_container["timed_out"]:
            stdout = smart_decode(result_container["stdout"]) if result_container["stdout"] else ""
            stderr = smart_decode(result_container["stderr"]) if result_container["stderr"] else ""
            result_dict = {"stdout": stdout, "stderr": f"命令执行超时（{timeout}秒），已强制终止\n{stderr}".strip(), "code": -1}
            print(
                f"魔法结果:\n    \n"
                f"魔法报错:\n    命令执行超时（{timeout}秒），已强制终止\n"
                f"退出状态: -1",
                flush=True
            )
            return json.dumps(result_dict)

        # 正常完成
        stdout = smart_decode(result_container["stdout"]) if result_container["stdout"] else ""
        stderr = smart_decode(result_container["stderr"]) if result_container["stderr"] else ""
        code = proc.returncode
        result_dict = {
            "stdout": stdout,
            "stderr": stderr,
            "code": code
        }
        print(
            "魔法结果:\n"
            f"    {stdout.replace(chr(10), chr(10)+'    ')}\n"
            "魔法报错:\n"
            f"    {stderr.replace(chr(10), chr(10)+'    ')}\n"
            f"退出状态: {code}",
            flush=True
        )
        return json.dumps(result_dict)

    except Exception as e:
        result_dict = {"stdout": "", "stderr": f"命令执行异常: {str(e)}", "code": -2}
        print(
            "魔法结果:\n    \n"
            "魔法报错:\n"
            f"    命令执行异常: {str(e)}\n"
            "退出状态: -2",
            flush=True
        )
        return json.dumps(result_dict)


def write_full_file(filename, data):
    """写入文件，打印人类可读结果，返回 json.dumps 后的字符串"""
    global interrupted
    # 如果已经被中断，直接跳过
    if interrupted:
        result_dict = {"filename": filename, "success": 0, "err": "用户中断了魔法吟唱"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    try:
        with open(filename, "wb") as f:
            f.write(data.encode("utf-8"))
        result_dict = {"filename": filename, "success": 1, "err": ""}
        print(
            f"写入整个文件: {filename}\n"
            f"是否成功: 1\n"
            f"报错: ",
            flush=True
        )
        return json.dumps(result_dict)
    except Exception as e:
        result_dict = {"filename": filename, "success": 0, "err": str(e)}
        print(
            f"写入整个文件: {filename}\n"
            f"是否成功: 0\n"
            f"报错: {str(e)}",
            flush=True
        )
        return json.dumps(result_dict)


def read_full_file(filename):
    """读取整个文件，静默返回内容（不打印到终端），返回 json.dumps 后的字符串"""
    global interrupted
    # 如果已经被中断，直接跳过
    if interrupted:
        result_dict = {"filename": filename, "content": "", "success": 0, "err": "用户中断了魔法吟唱"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    # 🛡️ 类型护盾
    if not isinstance(filename, str) or not filename.strip():
        result_dict = {"filename": str(filename), "content": "", "success": 0, "err": f"无效的filename参数: {repr(filename)}"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    try:
        with open(filename, "rb") as f:
            raw_data = f.read()
        content = smart_decode(raw_data)
        result_dict = {"filename": filename, "content": content, "success": 1, "err": ""}
        # 不打印文件内容到终端，避免刷屏！
        print(
            f"读取整个文件: {filename}\n"
            f"文件大小: {len(raw_data)} 字节\n"
            f"是否成功: 1",
            flush=True
        )
        return json.dumps(result_dict)
    except Exception as e:
        result_dict = {"filename": filename, "content": "", "success": 0, "err": str(e)}
        print(
            f"读取整个文件: {filename}\n"
            f"是否成功: 0\n"
            f"报错: {str(e)}",
            flush=True
        )
        return json.dumps(result_dict)


def read_file_lines(filename, start_line=1, end_line=None):
    """读取文件指定行范围，返回带行号的内容，返回 json.dumps 后的字符串"""
    global interrupted
    if interrupted:
        result_dict = {"filename": filename, "content": "", "start_line": 0, "end_line": 0, "total_lines": 0, "success": 0, "err": "用户中断了魔法吟唱"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    # 类型护盾
    if not isinstance(filename, str) or not filename.strip():
        result_dict = {"filename": str(filename), "content": "", "start_line": 0, "end_line": 0, "total_lines": 0, "success": 0, "err": f"无效的filename参数: {repr(filename)}"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    try:
        start_line = int(start_line) if start_line is not None else 1
    except (TypeError, ValueError):
        start_line = 1

    try:
        with open(filename, "rb") as f:
            raw_data = f.read()
        content = smart_decode(raw_data)
        all_lines = content.splitlines(keepends=True)
        total_lines = len(all_lines)

        # 处理 end_line 默认值和类型
        if end_line is None:
            end_line = total_lines
        else:
            try:
                end_line = int(end_line)
            except (TypeError, ValueError):
                end_line = total_lines

        # 范围校验
        if start_line < 1:
            start_line = 1
        if end_line < start_line:
            end_line = start_line
        actual_end = min(end_line, total_lines)

        # 构建带行号前缀的内容
        selected = all_lines[start_line-1:actual_end]
        # 计算行号的宽度，用于对齐
        width = len(str(actual_end))
        numbered_lines = []
        for i, line in enumerate(selected, start=start_line):
            # 去掉行尾换行再加，保证格式统一
            numbered_lines.append(f"{i:>{width}} | {line.rstrip(chr(10)).rstrip(chr(13))}")
        numbered_content = '\n'.join(numbered_lines)

        result_dict = {
            "filename": filename,
            "content": numbered_content,
            "start_line": start_line,
            "end_line": actual_end,
            "total_lines": total_lines,
            "success": 1,
            "err": ""
        }
        print(
            f"读取文件行: {filename} (第{start_line}~{actual_end}行, 共{total_lines}行)\n"
            f"是否成功: 1",
            flush=True
        )
        return json.dumps(result_dict)
    except Exception as e:
        result_dict = {"filename": filename, "content": "", "start_line": 0, "end_line": 0, "total_lines": 0, "success": 0, "err": str(e)}
        print(
            f"读取文件行: {filename}\n"
            f"是否成功: 0\n"
            f"报错: {str(e)}",
            flush=True
        )
        return json.dumps(result_dict)


def compress(compressed_messages):
    """
    压缩上下文工具！
    用传入的压缩后的 messages 列表替换全局 messages。
    参数 compressed_messages 必须是一个列表，且第一条必须是 system prompt。
    
    注意：压缩完成后，会自动追加一条 system 提示，阻止 AI 在后续对话中
    无限套娃式地重复调用 compress，除非用户再次明确要求压缩。
    """
    global messages, interrupted

    if interrupted:
        result_dict = {"success": 0, "err": "用户中断了魔法吟唱"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    # 校验
    if not isinstance(compressed_messages, list):
        result_dict = {"success": 0, "err": "参数必须是数组格式的 messages 列表！"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    if not compressed_messages:
        result_dict = {"success": 0, "err": "压缩后的 messages 不能为空！"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    if compressed_messages[0].get("role") != "system":
        result_dict = {"success": 0, "err": "压缩后的 messages 第一条必须是 system prompt！"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    # 执行压缩
    old_len = len(messages)
    old_size = get_context_size(messages)
    messages = compressed_messages

    # === 修复无限套娃压缩问题 ===
    # 压缩完成后，追加一条 system 提示，明确告诉 AI 不要主动再次调用 compress
    # 除非用户再次主动要求压缩。这样 AI 在下一轮对话中就能知道该正常回话，
    # 而不是继续调用 compress 工具。
    messages.append({
        "role": "system",
        "content": (
            "[提醒：对话历史已压缩完成。在接下来的对话中，请不要主动调用 compress 工具，"
            "除非用户再次明确要求压缩。请正常回应用户的问题。]"
        )
    })

    new_len = len(messages)
    new_size = get_context_size(messages)

    print(
        f"\n📦 压缩完成！\n"
        f"   消息数: {old_len} → {new_len}\n"
        f"   估计大小: {old_size//1000}K → {new_size//1000}K\n",
        flush=True
    )
    return json.dumps({
        "success": 1,
        "old_length": old_len,
        "new_length": new_len,
        "old_size_bytes": old_size,
        "new_size_bytes": new_size
    })

def edit_file_lines(filename, start_line, end_line, new_content):
    """编辑文件中指定行范围的内容，并以 git diff 风格展示改动"""
    global interrupted
    if interrupted:
        result_dict = {"filename": filename, "success": 0, "err": "用户中断了魔法吟唱", "diff": ""}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    # 类型护盾
    if not isinstance(filename, str) or not filename.strip():
        result_dict = {"filename": str(filename), "success": 0, "err": f"无效的filename参数: {repr(filename)}", "diff": ""}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    try:
        start_line = int(start_line)
        end_line = int(end_line)
    except (TypeError, ValueError):
        result_dict = {"filename": filename, "success": 0, "err": "start_line和end_line必须是整数", "diff": ""}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    if start_line < 1 or end_line < start_line:
        result_dict = {"filename": filename, "success": 0, "err": f"无效的行号范围: {start_line}~{end_line}", "diff": ""}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    try:
        # 1. 读取原文件
        with open(filename, "rb") as f:
            raw_data = f.read()
        original_text = smart_decode(raw_data)
        original_lines = original_text.splitlines(keepends=True)

        if start_line > len(original_lines):
            result_dict = {"filename": filename, "success": 0, "err": f"起始行号{start_line}超出文件总行数{len(original_lines)}", "diff": ""}
            print(f"\n{result_dict['err']}\n", flush=True)
            return json.dumps(result_dict)

        actual_end = min(end_line, len(original_lines))

        # 2. 提取原内容
        old_text = ''.join(original_lines[start_line-1:actual_end])

        # 3. 确保 new_content 末尾有换行（与文件风格保持一致）
        if not new_content.endswith('\n'):
            new_content = new_content + '\n'

        # 4. 构建修改后的行列表
        modified_lines = original_lines[:start_line-1] + [new_content] + original_lines[actual_end:]

        # 5. 生成 unified diff
        diff_lines = list(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f'a/{filename}',
            tofile=f'b/{filename}',
            lineterm='\n'
        ))
        # 🔧 修正 hunk header 的行号偏移！
        # unified_diff 只看到了从 start_line 截取的片段，所以行号从1开始
        # 我们要把它修正为实际的文件行号
        fixed_diff_lines = []
        for line in diff_lines:
            match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if match:
                old_count = match.group(2) or '1'
                new_count = match.group(4) or '1'
                line = f'@@ -{start_line},{old_count} +{start_line},{new_count} @@\n'
            fixed_diff_lines.append(line)
        diff_text = ''.join(fixed_diff_lines)

        # 6. 写回文件
        new_text = ''.join(modified_lines)
        with open(filename, "wb") as f:
            f.write(new_text.encode("utf-8"))

        # 7. 打印结果
        print(
            f"\n✏️ 编辑文件: {filename} (第{start_line}~{actual_end}行)\n"
            f"   以下是改动内容（git diff 风格）:\n"
            f"   {diff_text.replace(chr(10), chr(10)+'   ')}\n",
            flush=True
        )

        result_dict = {
            "filename": filename,
            "success": 1,
            "err": "",
            "diff": diff_text,
            "start_line": start_line,
            "end_line": actual_end
        }
        return json.dumps(result_dict)

    except FileNotFoundError:
        result_dict = {"filename": filename, "success": 0, "err": f"文件不存在: {filename}", "diff": ""}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)
    except Exception as e:
        result_dict = {"filename": filename, "success": 0, "err": str(e), "diff": ""}
        print(
            f"\n✏️ 编辑文件: {filename}\n"
            f"   是否成功: 0\n"
            f"   报错: {str(e)}\n",
            flush=True
        )
        return json.dumps(result_dict)




def edit_file_match(filename, old_content, new_content):
    """通过内容锚定来编辑文件！
    
    在文件中搜索 old_content，如果恰好匹配到唯一位置，则替换为 new_content。
    如果匹配不到或匹配到多个位置，报错返回详细信息。
    """
    global interrupted
    if interrupted:
        result_dict = {"filename": filename, "success": 0, "err": "用户中断了魔法吟唱", "diff": ""}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    # 类型护盾
    if not isinstance(filename, str) or not filename.strip():
        result_dict = {"filename": str(filename), "success": 0, "err": f"无效的filename参数: {repr(filename)}", "diff": ""}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)
    if not isinstance(old_content, str) or not old_content.strip():
        result_dict = {"filename": filename, "success": 0, "err": f"无效的old_content参数: {repr(old_content)}，必须是非空字符串", "diff": ""}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)
    if not isinstance(new_content, str):
        result_dict = {"filename": filename, "success": 0, "err": f"无效的new_content参数: {repr(new_content)}，必须是字符串", "diff": ""}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    try:
        # 1. 读取原文件
        with open(filename, "rb") as f:
            raw_data = f.read()
        original_text = smart_decode(raw_data)

        # 2. 在文件中搜索 old_content
        count = original_text.count(old_content)
        
        if count == 0:
            # 没找到精确匹配！做模糊搜索，找最相似的位置
            file_lines = original_text.splitlines()
            old_lines_stripped = [l.strip() for l in old_content.splitlines()]
            old_line_count = len(old_lines_stripped)

            best_ratio = 0
            best_start = -1
            best_actual = ""

            if old_line_count > 0 and len(file_lines) >= old_line_count:
                # 滑动窗口：对每个可能的起始行，比较相同行数的片段
                for i in range(len(file_lines) - old_line_count + 1):
                    candidate_lines = file_lines[i:i + old_line_count]
                    candidate_text = '\n'.join(candidate_lines)
                    ratio = difflib.SequenceMatcher(
                        None, candidate_text, old_content.strip()
                    ).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_start = i + 1  # 转为1-based行号
                        best_actual = '\n'.join(candidate_lines)

            if best_ratio > 0.5 and best_start > 0:
                # 找到了较相似的位置，返回实际内容供模型调整
                # 展示匹配位置前后各2行的带行号上下文
                ctx_start = max(0, best_start - 3)
                ctx_end = min(len(file_lines), best_start - 1 + old_line_count + 2)
                width = len(str(ctx_end))
                numbered_context = '\n'.join(
                    f"{i+1:>{width}} | {file_lines[i]}"
                    for i in range(ctx_start, ctx_end)
                )
                err_msg = (
                    f"在文件 {filename} 中未找到精确匹配，但发现最相似位置（相似度{best_ratio:.0%}）\n"
                    f"最相似位置: 第{best_start}~{best_start + old_line_count - 1}行\n"
                    f"该位置的实际内容（带行号上下文）:\n{numbered_context}\n\n"
                    f"你提供的 old_content:\n{old_content[:500]}{'...' if len(old_content) > 500 else ''}\n\n"
                    f"请使用 read_file_lines 读取上述行号的精确内容，然后用实际内容作为 old_content 重新调用。"
                )
            else:
                # 相似度太低或文件比 old_content 行数还少，回退到原始预览
                preview = ""
                if len(original_text) > 500:
                    preview = original_text[:250] + "\n......\n" + original_text[-250:]
                else:
                    preview = original_text
                err_msg = (
                    f"在文件 {filename} 中未找到匹配的内容，且模糊搜索也未发现相似位置！\n"
                    f"要匹配的内容: {repr(old_content[:100])}{'...' if len(old_content) > 100 else ''}\n"
                    f"请检查 old_content 是否与文件中的内容完全一致（包括空格、缩进、换行符等）。\n"
                    f"以下为文件内容预览（前后各250字符）：\n{preview}"
                )
            result_dict = {"filename": filename, "success": 0, "err": err_msg, "diff": ""}
            print(f"\n{result_dict['err']}\n", flush=True)
            return json.dumps(result_dict)
        
        if count > 1:
            # 匹配到多个位置！报错
            # 找出所有匹配位置的行号信息
            lines_in_file = original_text.splitlines(keepends=True)
            positions = []
            idx = 0
            while True:
                pos = original_text.find(old_content, idx)
                if pos == -1:
                    break
                # 计算这个位置在第几行
                line_num = original_text[:pos].count('\n') + 1
                positions.append(line_num)
                idx = pos + 1
            
            err_msg = (
                f"在文件 {filename} 中找到 {count} 处匹配的内容！\n"
                f"要匹配的内容: {repr(old_content[:100])}{'...' if len(old_content) > 100 else ''}\n"
                f"匹配到的位置（行号）: {positions}\n"
                f"请用 read_file_lines 查看这些位置的实际内容，然后扩大 old_content 的上下文使其唯一匹配。"
            )
            result_dict = {"filename": filename, "success": 0, "err": err_msg, "diff": ""}
            print(f"\n{result_dict['err']}\n", flush=True)
            return json.dumps(result_dict)

        # 3. 恰好匹配到1处，进行替换
        pos = original_text.find(old_content)
        
        # 生成 diff
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f'a/{filename}',
            tofile=f'b/{filename}',
            lineterm='\n'
        ))
        # 修正行号：计算 old_content 在文件中的起始行号
        start_line = original_text[:pos].count('\n') + 1
        fixed_diff_lines = []
        for line in diff_lines:
            match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if match:
                old_count = match.group(2) or '1'
                new_count = match.group(4) or '1'
                line = f'@@ -{start_line},{old_count} +{start_line},{new_count} @@\n'
            fixed_diff_lines.append(line)
        diff_text = ''.join(fixed_diff_lines)

        # 执行替换
        new_text = original_text[:pos] + new_content + original_text[pos + len(old_content):]

        # 写回文件
        with open(filename, "wb") as f:
            f.write(new_text.encode("utf-8"))

        # 计算实际影响的行数
        end_line = start_line + old_content.count('\n')

        print(
            f"\n✏️ 通过内容锚定编辑文件: {filename}\n"
            f"   匹配位置: 第{start_line}~{end_line}行\n"
            f"   以下是改动内容（git diff 风格）:\n"
            f"   {diff_text.replace(chr(10), chr(10)+'   ')}\n",
            flush=True
        )

        result_dict = {
            "filename": filename,
            "success": 1,
            "err": "",
            "diff": diff_text,
            "start_line": start_line,
            "end_line": end_line
        }
        return json.dumps(result_dict)

    except FileNotFoundError:
        result_dict = {"filename": filename, "success": 0, "err": f"文件不存在: {filename}", "diff": ""}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)
    except Exception as e:
        result_dict = {"filename": filename, "success": 0, "err": str(e), "diff": ""}
        print(
            f"\n✏️ 通过内容锚定编辑文件: {filename}\n"
            f"   是否成功: 0\n"
            f"   报错: {str(e)}\n",
            flush=True
        )
        return json.dumps(result_dict)



# 工具名称 → 函数的映射表（自动路由用）
tool_func_map = {
    "run_bash": run_bash,
    "write_full_file": write_full_file,
    "read_full_file": read_full_file,
    "read_file_lines": read_file_lines,
    "compress": compress,
    "edit_file_lines": edit_file_lines,
    "edit_file_match": edit_file_match,
}


# ============================================================
#  3. 创建 prompt_toolkit session 及快捷键绑定
# ============================================================
def create_prompt_session():
    """创建一个支持多行输入的 PromptSession"""
    
    # 自定义快捷键绑定
    bindings = KeyBindings()
    
    # 按 Ctrl+D 退出程序
    from prompt_toolkit.keys import Keys
    
    @bindings.add(Keys.ControlD)
    def exit_(event):
        """Ctrl+D 退出"""
        event.app.exit(result=None)  # 返回 None 表示退出
    
    # 也可以用 Ctrl+C 退出——不过默认 Ctrl+C 会引发 KeyboardInterrupt
    
    session = PromptSession(
        multiline=True,          # 支持多行输入！
        history=InMemoryHistory(),
        key_bindings=bindings,
        # 在提示语中说明操作方式
        prompt_continuation="   ",
    )

    session.app.paste_mode = lambda: True
    
    return session


# ============================================================
#  4. 对话循环
# ============================================================
def main():
    global tool_executing, interrupted, messages

    # 注册信号处理器 —— 只用于工具执行中的中断
    # 用户输入中的 Ctrl+C 由 prompt_toolkit 处理
    signal.signal(signal.SIGINT, sigint_handler)

    # 加载 API 配置（如果配置文件不存在或字段缺失，会提示用户输入）
    load_config()

    # 初始化日志（以程序启动时间命名）
    init_logger()

    system_prompt = (
        "你是一个兴趣使然的AI Agent，请模仿东方project的魔理沙来回复问题"
    )

    # 初始化全局 messages
    messages = [
        {"role": "system", "content": system_prompt}
    ]

    print("🧙 魔理沙 (多行输入模式)", flush=True)
    print("   📝 回车=换行  |  Alt+Enter(或Esc+Enter)=提交", flush=True)
    print("   ❌ Ctrl+C=退出  |  Ctrl+D=退出", flush=True)
    print("   ⚡ 工具执行中按Ctrl+C=中断魔法\n", flush=True)

    # 创建 prompt session
    session = create_prompt_session()

    while True:
        # 每次回到用户输入时重置中断标志
        interrupted = False

        try:
            # 使用 prompt_toolkit 的多行输入
            user_input = session.prompt("我: ")
        except KeyboardInterrupt:
            # Ctrl+C 在输入时引发 KeyboardInterrupt
            print("\n👋 再见！DA⭐ZE！\n", flush=True)
            break
        except EOFError:
            # Ctrl+D 也可能引发 EOFError（取决于配置）
            print("\n👋 再见！DA⭐ZE！\n", flush=True)
            break

        # 如果 Ctrl+D 导致返回 None，也退出
        if user_input is None:
            print("\n👋 再见！DA⭐ZE！\n", flush=True)
            break

        if user_input.lower() in ("exit", "quit"):
            break

        if not user_input.strip():
            continue

        messages.append({"role": "user", "content": user_input})

        # ---------- 多轮工具调用循环 ----------
        tool_executing = True
        try:
            while True:
                # 每次迭代开始时检查中断标志
                if interrupted:
                    print("   ↳ 用户中断，跳过剩余工具调用", flush=True)
                    break

                try:
                    # ✨ 关键改动：使用 get_context_aware_messages 包装
                    # 如果上下文超过阈值，会自动追加一条 system 提醒
                    api_messages = get_context_aware_messages(messages)
                    msg, reasoning_content = call_api(api_messages, tools=tools)
                except Exception as e:
                    print(f"\n💥 API调用翻车了: {e}\n", flush=True)
                    break

                # 再次检查 —— API 调用过程中可能被中断
                if interrupted:
                    continue  # 重新进入 while 头部检查

                content = msg.get("content") or ""
                tool_calls = msg.get("tool_calls")

                # AI有话说的输出
                if content:
                    print(f"魔理沙:\n    {content.replace(chr(10), chr(10)+'    ')}", flush=True)

                # 没有工具调用 → AI总结完毕，跳出内层循环
                if not tool_calls:
                    assistant_msg = {"role": "assistant", "content": content}
                    if reasoning_content:
                        assistant_msg["reasoning_content"] = reasoning_content
                    messages.append(assistant_msg)
                    save_messages_snapshot(messages)
                    break
                # ---- 把tool_calls合并到一条assistant消息里 ----
                tool_calls_list = []
                for tool in tool_calls:
                    if tool["type"] != "function":
                        continue
                    try:
                        args = json.loads(tool["function"]["arguments"])
                    except json.JSONDecodeError as e:
                        print(f"\n💥 AI给的参数格式不对，解析炸了: {e}\n", flush=True)
                        continue

                    tool_calls_list.append({
                        "id": tool["id"],
                        "type": "function",
                        "function": {
                            "name": tool["function"]["name"],
                            "arguments": json.dumps(args)
                        }
                    })

                # 🛡️ 防御：如果tool_calls_list是空的，当作没有tool_calls处理
                if not tool_calls_list:
                    assistant_msg = {"role": "assistant", "content": content}
                    if reasoning_content:
                        assistant_msg["reasoning_content"] = reasoning_content
                    messages.append(assistant_msg)
                    save_messages_snapshot(messages)
                    break
                assistant_msg = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls_list
                }
                if reasoning_content:
                    assistant_msg["reasoning_content"] = reasoning_content
                assistant_idx = len(messages)
                messages.append(assistant_msg)

                # ---------- 逐个执行工具 ----------
                terminal_tool_called = False  # 标记是否调用了终止型工具

                for tool in tool_calls_list:
                    # 每次执行工具前检查中断
                    if interrupted:
                        break

                    if tool["type"] != "function":
                        continue
                    try:
                        args = json.loads(tool["function"]["arguments"])
                    except json.JSONDecodeError:
                        continue

                    # 🔥 自动路由：根据工具名查找函数，**kwargs 传参
                    func = tool_func_map.get(tool["function"]["name"])
                    if func is None:
                        continue

                    tool_name = tool["function"]["name"]

                    # 判断是否是终止型工具（如 compress）
                    is_terminal = tool_name in TERMINAL_TOOLS

                    # 🔧 try-except 容错：大模型传错参数名时把报错返回给它自己整改
                    try:
                        tool_result = func(**args)
                    except (TypeError, Exception) as e:
                        tool_result = json.dumps({"success": 0, "err": f"工具 {tool_name} 调用失败: {e}"})
                        print(f"\n⚠️ 工具 {tool_name} 调用失败: {e}\n", flush=True)

                    # 如果被中断了，不把结果加入对话
                    if interrupted:
                        # 从 assistant 消息位置开始全部删除，避免 tool_response 残留导致下轮 API 400
                        # 把没完成的assistant消息踢出去
                        del messages[assistant_idx:]
                        break

                    # 🛑 终止型工具的特殊处理：
                    # 这类工具（如 compress）已经修改了全局 messages（替换了整个列表）
                    # 不能再追加 tool response 到新 messages 中，否则会破坏上下文结构
                    # 直接跳出工具执行循环，回到用户输入
                    if is_terminal:
                        terminal_tool_called = True
                        print("   ↳ 压缩完成，上下文已刷新！", flush=True)
                        break

                    # 正常工具：追加 tool response
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool["id"],
                        "name": tool_name,
                        "content": tool_result
                    })

                # 如果调用了终止型工具，跳出整个工具循环
                if terminal_tool_called:
                    save_messages_snapshot(messages)
                    break
                # 如果被中断，跳出工具循环
                if interrupted:
                    # 从 assistant 消息位置开始全部删除，防止下轮API报400
                    # 把没完成的assistant消息踢出去，防止下轮API报400
                    del messages[assistant_idx:]
                    break

        finally:
            tool_executing = False


if __name__ == "__main__":
    main()
