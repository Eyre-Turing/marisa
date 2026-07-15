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

# MCP (Model Context Protocol) 支持 —— 连接外部 MCP 服务器
# 通过 mcp_manager.py 统一管理 stdio/HTTP 模式的 MCP 服务器连接
from mcp_manager import get_mcp_manager, load_mcp_config


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

# 长内容自动过期机制（第二层：软过期）
# tool response 内容超过此大小时在 large_content_counter 中计数，
# 达到过期轮数后自动替换为过期提示
LARGE_CONTENT_THRESHOLD = 50 * 1024  # 50KB
# 大内容在 messages 中存在超过此轮数（外层 while 循环次数）后自动过期
LARGE_CONTENT_EXPIRE_ROUNDS = 5

# 工具函数硬截断阈值（第一层：保底保护）
# 当工具返回的内容超过此大小时，直接截断保留前 N 字节，
# 防止单次工具调用撑爆上下文
TOOL_HARD_CUTOFF = 200 * 1024  # 200KB

# 大内容计数器：tool_call_id -> 被检测到为大内容的轮次数
# 在外层 while 循环每次开始时扫描 messages 并更新
large_content_counter = {}

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


    # ---------- 🔗 合并连续的 tool_result user 消息 ----------
    # Anthropic API 要求：同一组 tool_use 对应的所有 tool_result 必须合并到
    # 紧随 assistant 之后的一条 user 消息中，不能拆成多条独立的 user 消息
    merged_anthropic_messages = []
    i = 0
    while i < len(anthropic_messages):
        msg = anthropic_messages[i]
        if msg["role"] == "user":
            content_blocks = msg.get("content", "")
            if isinstance(content_blocks, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content_blocks
            ):
                # 这是一条 tool_result 消息，检查后面是否还有连续的 tool_result 消息
                merged_blocks = list(content_blocks)
                j = i + 1
                while j < len(anthropic_messages):
                    next_msg = anthropic_messages[j]
                    if next_msg["role"] == "user":
                        next_content = next_msg.get("content", "")
                        if isinstance(next_content, list) and any(
                            isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in next_content
                        ):
                            # 合并 tool_result blocks
                            for block in next_content:
                                if isinstance(block, dict) and block.get("type") == "tool_result":
                                    merged_blocks.append(block)
                            j += 1
                            continue
                    break
                if j > i + 1:
                    print(f"   🔗 合并 {j - i} 条连续的 tool_result user 消息", flush=True)
                merged_anthropic_messages.append({"role": "user", "content": merged_blocks})
                i = j
                continue
        merged_anthropic_messages.append(msg)
        i += 1
    anthropic_messages = merged_anthropic_messages

    # ---------- 🔒 校验：修复孤立的 tool_use（缺少紧跟着的 tool_result） ----------
    # Anthropic API 要求每个 tool_use 后面必须紧跟着一个 tool_result
    # 如果因为消息损坏或中断导致 tool_use 没有配对的 tool_result，API 会报 400
    fixed_anthropic_messages = []
    i = 0
    while i < len(anthropic_messages):
        msg = anthropic_messages[i]
        if msg["role"] == "assistant":
            content_blocks = msg.get("content", "")
            if isinstance(content_blocks, list):
                # 检查这个 assistant 消息中是否有 tool_use
                has_tool_use = any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in content_blocks
                )
                if has_tool_use:
                    # 检查下一条消息是不是 tool_result（即 role=user 且 content 包含 tool_result）
                    next_has_result = False
                    if i + 1 < len(anthropic_messages):
                        next_msg = anthropic_messages[i + 1]
                        if next_msg["role"] == "user":
                            next_content = next_msg.get("content", "")
                            if isinstance(next_content, list):
                                next_has_result = any(
                                    isinstance(b, dict) and b.get("type") == "tool_result"
                                    for b in next_content
                                )
                    
                    if not next_has_result:
                        # ⚡ 孤立 tool_use！跳过这个 assistant 消息
                        # 同时也跳过它之后可能跟着的 user 文本消息（如果有的话）
                        print("   ⚠️ 检测到孤立的 tool_use，已自动跳过修复", flush=True)
                        i += 1
                        # 跳过后续的 user 消息（直到遇到下一个 assistant 或结尾）
                        while i < len(anthropic_messages) and anthropic_messages[i]["role"] == "user":
                            i += 1
                        continue
        
        fixed_anthropic_messages.append(msg)
        i += 1
    
    anthropic_messages = fixed_anthropic_messages

    # ---------- 🔒 最终校验：统计所有 tool_use id，确保每个都有配对的 tool_result ----------
    # 这是针对 Anthropic API 的严格要求：每个 tool_use 都必须有对应的 tool_result
    # 收集所有 tool_use id
    all_tool_use_ids = set()
    all_tool_result_ids = set()
    for msg in anthropic_messages:
        content_blocks = msg.get("content", "")
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        all_tool_use_ids.add(block.get("id", ""))
                    elif block.get("type") == "tool_result":
                        all_tool_result_ids.add(block.get("tool_use_id", ""))
    
    # 找出孤立 tool_use（没有对应 tool_result 的）
    orphan_tool_use_ids = all_tool_use_ids - all_tool_result_ids
    if orphan_tool_use_ids:
        print(f"   ⚠️ 发现 {len(orphan_tool_use_ids)} 个孤立 tool_use，正在修复...", flush=True)
        # 从 assistant 消息中移除孤立的 tool_use block
        fixed_msgs = []
        for msg in anthropic_messages:
            if msg["role"] == "assistant":
                content_blocks = msg.get("content", "")
                if isinstance(content_blocks, list):
                    new_blocks = []
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            if block.get("id", "") in orphan_tool_use_ids:
                                print(f"     移除孤立 tool_use: {block.get('id', '')[:20]}... ({block.get('name', '')})", flush=True)
                                continue  # 跳过这个孤立的 tool_use
                        new_blocks.append(block)
                    # 如果所有 block 都被移除了，就完全跳过这个 assistant 消息
                    if not new_blocks:
                        continue
                    msg["content"] = new_blocks
            fixed_msgs.append(msg)
        anthropic_messages = fixed_msgs
    
    # 同时也检查是否有多余的 tool_result（没有对应 tool_use 的）
    orphan_tool_result_ids = all_tool_result_ids - all_tool_use_ids
    if orphan_tool_result_ids:
        print(f"   ⚠️ 发现 {len(orphan_tool_result_ids)} 个多余的 tool_result，正在修复...", flush=True)
        fixed_msgs = []
        for msg in anthropic_messages:
            content_blocks = msg.get("content", "")
            if isinstance(content_blocks, list):
                new_blocks = []
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        if block.get("tool_use_id", "") in orphan_tool_result_ids:
                            print(f"     移除多余 tool_result: {block.get('tool_use_id', '')[:20]}...", flush=True)
                            continue
                    new_blocks.append(block)
                msg["content"] = new_blocks
            fixed_msgs.append(msg)
        anthropic_messages = fixed_msgs

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
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "智能加载技能知识文件到对话上下文中。这是一个子Agent模式——你只需要描述你要做什么事、需要哪方面的知识，它会自动分析 skills/ 目录下的 Markdown 技能文件，找到最匹配的一个或多个技能并加载。加载后技能内容会作为 system 消息追加到对话中，同时工具返回结果的 contents 字段会直接包含技能文件的具体内容（键为技能名、值为完整文本），你无需再单独去读取文件。注意：技能加载后持续有效直到对话被压缩。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_description": {
                        "type": "string",
                        "description": "描述你要做什么事、需要哪方面知识的文本，例如 '我想了解魔理沙的魔法弹幕技能' 或 '我需要蘑菇相关的知识来制作魔法药水'"
                    }
                },
                "required": ["task_description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_code_path",
            "description": "获取当前 AI Agent 主程序文件（ai_agent_prompt.py）所在的完整目录路径。当你需要读取或修改自己的代码、查看自己的文件结构时可以使用此工具获取基准路径。返回值为一个 json，有 path（字符串，目录路径）和 success（数字，1为成功）字段。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
]

# ============================================================
# MCP 工具动态合并 —— 将外部 MCP 服务器的工具合并到主工具列表
# ============================================================

def _merge_mcp_tools():
    """将 MCP 管理器中已连接服务器的工具合并到主工具列表
    
    返回合并后的完整工具列表。
    注意：MCP 工具的调用会通过 tool_func_map 中的 mcp_call_tool 路由。
    """
    try:
        mcp = get_mcp_manager()
        mcp_tools = mcp.get_all_tools()
        if mcp_tools:
            print(f"   📦 合并 {len(mcp_tools)} 个 MCP 工具到工具列表", flush=True)
            return tools + mcp_tools
    except Exception as e:
        print(f"   ⚠️ 合并 MCP 工具时出错: {e}", flush=True)
    return tools


# MCP 工具调用路由 —— 所有 MCP 工具的调用都会走这个函数
def mcp_call_tool(tool_name, **arguments):
    """调用 MCP 工具（由 tool_func_map 路由）"""
    try:
        mcp = get_mcp_manager()
        result = mcp.call_tool(tool_name, arguments)
        return json.dumps({"success": 1, "result": result})
    except Exception as e:
        err_msg = f"MCP 工具 {tool_name} 调用失败: {e}"
        print(f"   ⚠️ {err_msg}", flush=True)
        return json.dumps({"success": 0, "err": err_msg})


# 终止型工具集合：执行这些工具后会直接结束本轮工具调用循环
# 因为这类工具（如 compress）会修改全局 messages 状态，
# 后续的 tool response 追加和 API 调用会基于错误的上下文继续执行
TERMINAL_TOOLS = {"compress"}



def get_code_path():
    """获取当前 AI Agent 主程序文件所在的目录路径

    返回一个 json 字符串，包含 path（目录路径）和 success（1为成功）字段。
    """
    path = os.path.dirname(os.path.abspath(__file__))
    result = {"path": path, "success": 1}
    print(f"\n📂 我的代码在: {path}\n", flush=True)
    return json.dumps(result)


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
        # 硬截断保护：防止单次输出过大撑爆上下文
        stdout_orig_len = len(stdout)
        stderr_orig_len = len(stderr)
        if stdout_orig_len > TOOL_HARD_CUTOFF:
            stdout = stdout[:TOOL_HARD_CUTOFF] + f"\n\n...（输出过大，已截断。原始大小 {stdout_orig_len//1024}KB，仅显示前 {TOOL_HARD_CUTOFF//1024}KB）"
        if stderr_orig_len > TOOL_HARD_CUTOFF:
            stderr = stderr[:TOOL_HARD_CUTOFF] + f"\n\n...（错误输出过大，已截断。原始大小 {stderr_orig_len//1024}KB，仅显示前 {TOOL_HARD_CUTOFF//1024}KB）"
        result_dict = {
            "stdout": stdout,
            "stderr": stderr,
            "code": code
        }
        print(
            "魔法结果:\n"
            f"    {stdout.replace(chr(10), chr(10)+'    ')}\n"
            f"    {'[输出较大，已截断至' + str(TOOL_HARD_CUTOFF//1024) + 'KB]' if stdout_orig_len > TOOL_HARD_CUTOFF else ''}"
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


# ============================================================
#  路径标准化 —— Windows 下兼容 Linux 风格路径
# ============================================================

def _normalize_path(filename):
    """将路径转为当前平台原生格式。
    在 Windows 下，将 /c/xxx、/abc/xxx 等 Linux 风格路径转换为 c:/xxx、abc:/xxx，
    其他平台不做转换。
    """
    if sys.platform == "win32" and isinstance(filename, str) and len(filename) > 2 and filename[0] == '/':
        # 匹配 /xxx/... 或 /Xxx/... 模式（第一个分段作为盘符/挂载点名），转为 xxx:\...
        import re as _re
        m = _re.match(r'^/([a-zA-Z][a-zA-Z0-9]*)/(.*)', filename)
        if m:
            filename = m.group(1) + ':/' + m.group(2)
    return filename


# ============================================================
#  文件读写工具
# ============================================================

def write_full_file(filename, data):
    """写入文件，打印人类可读结果，返回 json.dumps 后的字符串"""
    filename = _normalize_path(filename)
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
    filename = _normalize_path(filename)
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
        file_size = len(raw_data)
        # 硬截断保护：防止单次读取过大文件撑爆上下文
        if file_size > TOOL_HARD_CUTOFF:
            content = content[:TOOL_HARD_CUTOFF] + f"\n\n...（文件过大，已截断。原始大小 {file_size//1024}KB，仅显示前 {TOOL_HARD_CUTOFF//1024}KB）"
        result_dict = {"filename": filename, "content": content, "success": 1, "err": ""}
        # 不打印文件内容到终端，避免刷屏！
        print(
            f"读取整个文件: {filename}\n"
            f"文件大小: {file_size} 字节\n"
            f"{'[文件较大，已截断至' + str(TOOL_HARD_CUTOFF//1024) + 'KB]' if file_size > TOOL_HARD_CUTOFF else ''}"
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
    filename = _normalize_path(filename)
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
        # 硬截断保护：防止单次读取行数过多撑爆上下文
        content_orig_len = len(numbered_content)
        if content_orig_len > TOOL_HARD_CUTOFF:
            # 保留前半部分和后半部分，中间省略
            half = TOOL_HARD_CUTOFF // 2
            numbered_content = (numbered_content[:half] +
                f"\n...（内容过多，已截断。原始大小 {content_orig_len//1024}KB，共 {total_lines} 行）\n" +
                numbered_content[-half:])

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
            f"{'[返回内容较大，已截断至' + str(TOOL_HARD_CUTOFF//1024) + 'KB]' if content_orig_len > TOOL_HARD_CUTOFF else ''}"
            f"是否成功: 1",
            flush=True
        )
        return json.dumps(result_dict)
        print(
            f"读取文件行: {filename} (第{start_line}~{actual_end}行, 共{total_lines}行)\n"
            f"{'[返回内容较大，已标记为大内容]' if len(numbered_content) > LARGE_CONTENT_THRESHOLD else ''}"
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
    global messages, interrupted, large_content_counter

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
    # 压缩后清空大内容计数器，因为 messages 已被完全替换
    large_content_counter.clear()

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


# ============================================================
#  大内容自动过期机制 —— 在外层 while 循环每次开始时调用
# ============================================================

def _expire_large_content():
    """扫描 messages 中所有 role=tool 的消息，对大内容进行计数和过期处理。

    每次外层 while 循环开始时调用。逻辑：
    1. 遍历 messages 中所有 role=tool 的消息
    2. 检查 content 的字符串长度，如果超过 LARGE_CONTENT_THRESHOLD 则视为大内容
    3. 在 large_content_counter 中对该 tool_call_id 计数 +1
    4. 如果计数达到 LARGE_CONTENT_EXPIRE_ROUNDS，把 content 替换为过期提示
    5. 已过期的消息不再参与后续计数

    注意：此函数不依赖工具函数内部的任何标记（如 is_large），
    完全基于 content 的实际大小来判断。
    """
    global large_content_counter, messages

    if not messages:
        return

    expired_count = 0
    for msg in messages:
        if msg.get("role") != "tool":
            continue

        tool_call_id = msg.get("tool_call_id", "")
        if not tool_call_id:
            continue

        content = msg.get("content", "")
        if not isinstance(content, str):
            continue

        # 跳过已经过期了的内容
        if content.startswith("[数据已过期"):
            continue

        # 直接根据 content 长度判断是否为大内容（不解析 JSON，不看标记）
        if len(content) > LARGE_CONTENT_THRESHOLD:
            # 这是一个大内容，计数 +1
            count = large_content_counter.get(tool_call_id, 0) + 1
            large_content_counter[tool_call_id] = count

            if count >= LARGE_CONTENT_EXPIRE_ROUNDS:
                # 达到过期轮数，替换内容
                old_size = len(content)
                # 尝试从 JSON 中提取文件名等信息作为参考
                original_filename = ""
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        original_filename = parsed.get("filename", "") or parsed.get("command", "")[:100]
                except (json.JSONDecodeError, TypeError):
                    pass
                msg["content"] = json.dumps({
                    "success": 0,
                    "err": f"[数据已过期：该内容已在对话中存在 {count} 轮（原始大小 {old_size//1024}KB），如需请重新获取]",
                    "_expired": True,
                    "_original_tool_call_id": tool_call_id,
                    "_original_filename": original_filename,
                    "_original_size": old_size
                })
                # 从计数器中移除，避免重复触发
                large_content_counter.pop(tool_call_id, None)
                expired_count += 1
                print(f"   ⏰ 大内容过期: tool_call_id={tool_call_id[:16]}... (原大小 {old_size//1024}KB, 已存在 {count} 轮)", flush=True)

    if expired_count > 0:
        print(f"   🧹 本次过期了 {expired_count} 条大内容，当前 messages 大小: {get_context_size(messages)//1024}KB", flush=True)


def edit_file_lines(filename, start_line, end_line, new_content):
    """编辑文件中指定行范围的内容，并以 git diff 风格展示改动"""
    filename = _normalize_path(filename)
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
    filename = _normalize_path(filename)
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


# ============================================================
#  Skills 技能加载系统 —— 按需从 skills/ 目录加载 Markdown 技能文件
#  使用子 Agent 模式：根据任务描述自动分析并加载最匹配的技能
# ============================================================
#  Messages 顺序修复 —— 把插队到 tool_calls 和 tool 之间的消息挪到后面
# ============================================================

def _fix_messages_tool_order(msg_list):
    """修复 messages 中 tool_calls 和 tool 响应之间被插队的消息顺序。

    OpenAI 协议规范：assistant（带 tool_calls）必须紧跟 role: tool 的消息。
    如果中间被 system 等其他消息插队，把后面的 tool 消息移动上来。

    参数:
        msg_list: 要修复的 messages 列表（直接修改原列表）

    返回:
        bool: 是否做了修复
    """
    fixed = False
    i = 0
    while i < len(msg_list):
        msg = msg_list[i]
        # 找到带 tool_calls 的 assistant 消息
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            j = i + 1
            # 收集插队的消息（非tool的消息）
            interleaving = []
            while j < len(msg_list):
                next_msg = msg_list[j]
                if next_msg.get("role") == "tool":
                    # 找到了 tool 消息，看看之前有没有插队的
                    if interleaving:
                        # 有插队！把这个 tool 消息移到 interleaving 之前
                        tool_msg = msg_list.pop(j)
                        msg_list.insert(i + 1, tool_msg)
                        fixed = True
                        # 重置，重新检查
                        break
                    else:
                        # 没插队，正常，继续找下一个 tool（可能有多个 tool_calls）
                        j += 1
                        continue
                else:
                    # 不是 tool 消息，记录为插队消息
                    interleaving.append(next_msg)
                    j += 1
            else:
                # 没找到 tool 消息（可能是未完成的 tool_calls）
                pass
        i += 1
    return fixed


# ============================================================
#  Skills 技能加载系统 —— 按需从 skills/ 目录加载 Markdown 技能文件
#  使用子 Agent 模式：根据任务描述自动分析并加载最匹配的技能
# ============================================================

# Skills 目录配置：同时支持启动目录（CWD）和代码目录下的 skills/
# 启动目录的 skills/ 优先级更高，同名技能文件以启动目录的版本为准
# 在 main() 启动时初始化
CWD_SKILLS_DIR = None   # 启动目录下的 skills/
CODE_SKILLS_DIR = None  # 代码目录下的 skills/


def _init_skills_dirs():
    """初始化两个 skills 目录路径，并返回合并后的技能列表
    注意：不再检查目录是否存在（将检查延迟到使用时），
    这样即使启动后用户才创建 skills 文件夹并添加 .md 文件，也能被动态加载。
    """
    global CWD_SKILLS_DIR, CODE_SKILLS_DIR
    
    # 启动目录下的 skills/ —— 无论是否存在都记录路径，使用时再动态检查
    CWD_SKILLS_DIR = os.path.join(os.getcwd(), "skills")
    
    # 代码目录下的 skills/
    CODE_SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def _scan_skills(dir_path):
    """扫描指定目录下的 .md 文件，返回技能名列表"""
    if not dir_path or not os.path.isdir(dir_path):
        return []
    try:
        result = []
        for f in sorted(os.listdir(dir_path)):
            if f.endswith(".md"):
                result.append(f[:-3])
        return result
    except Exception:
        return []


def _merge_skills():
    """合并两个 skills 目录的技能列表，启动目录优先（同名覆盖）"""
    cwd_skills = _scan_skills(CWD_SKILLS_DIR)
    code_skills = _scan_skills(CODE_SKILLS_DIR)
    
    # 启动目录优先：如果启动目录有同名技能，代码目录的被覆盖
    # 用 dict 去重，启动目录的先插入，代码目录的后插入会覆盖
    merged = {}
    for s in code_skills:
        merged[s] = "code"
    for s in cwd_skills:
        merged[s] = "cwd"
    
    # 保持排序稳定：按名字排序
    return [(s, merged[s]) for s in sorted(merged.keys())]


def _resolve_skill_file(skill_name):
    """根据技能名返回实际文件路径（启动目录优先）"""
    if CWD_SKILLS_DIR:
        fpath = os.path.join(CWD_SKILLS_DIR, f"{skill_name}.md")
        if os.path.isfile(fpath):
            return fpath
    if CODE_SKILLS_DIR:
        fpath = os.path.join(CODE_SKILLS_DIR, f"{skill_name}.md")
        if os.path.isfile(fpath):
            return fpath
    return None


def load_skill(task_description):
    """根据任务描述，自动分析 skills/ 目录下的技能文件，加载最匹配的一个或多个技能到对话上下文。

    这是一个"子Agent"模式——函数内部会启动一次独立的 AI 调用（不带全局对话历史），
    让 AI 自己决定哪些技能最匹配当前需求，然后读取对应的 .md 文件内容，
    以 system 消息形式追加到全局对话中。

    参数:
        task_description: 描述你要做什么事、需要哪方面知识的文本
    """
    global messages, interrupted

    if interrupted:
        result_dict = {"success": 0, "err": "用户中断了魔法吟唱"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    # 类型护盾
    if not isinstance(task_description, str) or not task_description.strip():
        result_dict = {"success": 0, "err": f"无效的 task_description 参数: {repr(task_description)}"}
        print(f"\n{result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    task = task_description.strip()

    print(f"\n🔍 技能检索子Agent启动...")
    print(f"   任务: {task[:200]}{'...' if len(task) > 200 else ''}", flush=True)

    # ---- 获取可用技能列表 ----
    available_skills = _list_available_skills()
    if not available_skills:
        result_dict = {"success": 0, "err": "skills/ 目录下没有可用的技能文件（.md）"}
        print(f"   {result_dict['err']}\n", flush=True)
        return json.dumps(result_dict)

    # ---- 构造子 Agent 的 System Prompt ----
    skills_list_str = "\n".join(f"  - {s}" for s in available_skills)
    # ---- 构造子 Agent 的 System Prompt ----
    skills_list_str = "\n".join(f"  - {s}" for s in available_skills)
    sub_system_prompt = (
        "你是一个技能检索助手。你的任务是根据用户描述的需求，从技能库中找出最匹配的技能。\n"
        "\n"
        f"可用技能文件（位于 skills/ 目录下，均为 .md 格式）：\n"
        f"{skills_list_str}\n"
        "\n"
        "请按以下步骤操作：\n"
        "1. 分析用户的需求描述，判断需要哪些技能\n"
        "2. 使用 list_skills 工具查看可用技能列表（确认最新情况）\n"
        "3. 如果不确定某个技能的内容是否匹配，可以使用 read_full_file 工具读取来确认\n"
        "4. 确定最终匹配的技能列表\n"
        "\n"
        "注意：\n"
        "- 技能文件名（不含 .md）大致反映了其内容领域\n"
        "- 可以匹配多个技能（如问题涉及多个领域）\n"
        "- 尽量精准匹配，不要加载无关的技能\n"
        "- 如果没有匹配的技能，matched_skills 列表返回空 []\n"
        "- 技能文件来源：合并启动目录和代码目录两个 skills/ 目录，同名以启动目录版本为准\n"
        "\n"
        "### ⚠️ 重要：输出格式要求\n"
        "\n"
        "在完成分析后，你的最终回复**必须**按以下格式输出（纯文本，非 JSON 代码块）：\n"
        "\n"
        "---MATCHED_SKILLS_JSON_START---\n"
        '{"matched_skills": ["技能名1", "技能名2"], "reasoning": "选择理由简要说明"}\n'
        "---MATCHED_SKILLS_JSON_END---\n"
        "\n"
        "其中：\n"
        "- matched_skills: 你认为匹配的技能名列表（从可用技能中选择），不匹配则为 []\n"
        "- reasoning: 简短说明为什么选择这些技能（或为什么没有匹配）\n"
        "\n"
        "在 JSON 块之前可以自由发挥写分析过程，但 JSON 块必须出现在最终回复中。\n"
    )

    # ---- 子 Agent 的 tools（只有读相关工具 + list_skills）----
    # 注意：不能给 run_bash 等危险工具！子 agent 只需要读文件能力
    sub_tools = [
        {
            "type": "function",
            "function": {
                "name": "read_full_file",
                "description": "读取整个文本文件的内容并返回",
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
                "name": "list_skills",
                "description": "列出 skills/ 目录下所有可用的技能文件（返回技能名列表）。在读取具体文件前可以先调用此工具看看有哪些技能可选。",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        },
    ]

    # ---- 子 Agent 的 tool_func_map ----
    sub_tool_func_map = {
        "read_full_file": read_full_file,
        "list_skills": _list_available_skills_wrapper,
    }

    # ---- 构建子 Agent 的消息列表 ----
    sub_messages = [
        {"role": "system", "content": sub_system_prompt},
        {"role": "user", "content": f"需求描述：{task}\n\n请分析上述需求，确定哪些技能匹配（或没有匹配），然后按输出格式要求输出 JSON 结果。"}
    ]

    # ---- 子 Agent 的多轮工具调用循环 ----
    try:
        max_sub_rounds = 10  # 防止无限循环
        for sub_round in range(max_sub_rounds):
            if interrupted:
                break

            # 调用 API
            try:
                sub_msg, sub_reasoning = call_api(sub_messages, tools=sub_tools, tool_choice="auto")
            except Exception as e:
                err_msg = f"技能检索子Agent API 调用失败: {e}"
                print(f"   ⚠️ {err_msg}\n", flush=True)
                result_dict = {"success": 0, "err": err_msg}
                return json.dumps(result_dict)

            sub_content = sub_msg.get("content") or ""
            sub_tool_calls = sub_msg.get("tool_calls")

            # 组装 assistant 消息
            sub_assistant = {"role": "assistant", "content": sub_content}
            if sub_tool_calls:
                sub_assistant["tool_calls"] = sub_tool_calls
            sub_messages.append(sub_assistant)

            # 如果没有工具调用，说明子 Agent 工作完成
            if not sub_tool_calls:
                print(f"   ✅ 子Agent完成任务: {sub_content[:100]}{'...' if len(sub_content) > 100 else ''}", flush=True)
                break

            # 执行子 Agent 的工具调用
            for tool_call in sub_tool_calls:
                if tool_call["type"] != "function":
                    continue
                try:
                    args = json.loads(tool_call["function"]["arguments"])
                except json.JSONDecodeError:
                    continue

                func = sub_tool_func_map.get(tool_call["function"]["name"])
                if func is None:
                    continue

                try:
                    tool_result = func(**args)
                except Exception as e:
                    tool_result = json.dumps({"success": 0, "err": f"工具调用失败: {e}"})

                sub_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_call["function"]["name"],
                    "content": tool_result
                })
        else:
            # 超过最大轮数
            sub_content = sub_messages[-1].get("content", "") if sub_messages else ""
            print(f"   ⚠️ 子Agent达到最大轮数，使用最后结果", flush=True)
    except Exception as e:
        err_msg = f"技能检索子Agent异常: {e}"
        print(f"   ⚠️ {err_msg}\n", flush=True)
        result_dict = {"success": 0, "err": err_msg}
        return json.dumps(result_dict)

    # ---- 从子 Agent 的最终回复中提取 JSON 格式的匹配结果 ----
    # 子 Agent 的回复格式为：
    # ---MATCHED_SKILLS_JSON_START---
    # {"matched_skills": ["skill1", "skill2"], "reasoning": "说明"}
    # ---MATCHED_SKILLS_JSON_END---

    final_reasoning = ""
    matched_skills = []

    for msg in reversed(sub_messages):
        if msg.get("role") == "assistant" and msg.get("content", "").strip():
            final_reasoning = msg["content"]
            break

    if final_reasoning:
        # 从 final_reasoning 中提取 JSON 块
        json_match = re.search(
            r'---MATCHED_SKILLS_JSON_START---\s*(.*?)\s*---MATCHED_SKILLS_JSON_END---',
            final_reasoning,
            re.DOTALL
        )
        if json_match:
            try:
                json_str = json_match.group(1).strip()
                parsed = json.loads(json_str)
                if isinstance(parsed, dict) and isinstance(parsed.get("matched_skills"), list):
                    matched_skills = parsed["matched_skills"]
                    # 验证技能名合法性：只保留确实存在的技能
                    available = set(_list_available_skills())
                    matched_skills = [s for s in matched_skills if s in available]
            except (json.JSONDecodeError, TypeError):
                matched_skills = []

    # ---- 主Agent根据子Agent的匹配结果，自己读取技能文件并加载 ----
    loaded_contents = []
    if matched_skills:
        for skill_name in matched_skills:
            skill_file = _resolve_skill_file(skill_name)
            try:
                with open(skill_file, "rb") as f:
                    raw_data = f.read()
                content = smart_decode(raw_data)
                loaded_contents.append({
                    "skill_name": skill_name,
                    "content": content,
                    "size": len(content)
                })
                print(f"读取技能文件: {skill_file}\n文件大小: {len(raw_data)} 字节\n是否成功: 1", flush=True)
            except Exception as e:
                print(f"读取技能文件: {skill_file}\n是否成功: 0\n报错: {str(e)}", flush=True)

    # ---- 将技能内容追加到全局 messages ----
    if not matched_skills or not loaded_contents:
        # 没有匹配的技能，只把子Agent的分析结论加入上下文做参考
        # if final_reasoning:
        #     messages.append({
        #         "role": "system",
        #         "content": f"===== 技能检索结果 =====\n\n{final_reasoning}\n\n===== 技能检索结束 ====="
        #     })
        result_dict = {
            "success": 0,
            "message": "未找到匹配的技能（技能库中没有与需求相关的技能文件）",
            "reasoning": final_reasoning[:500] if final_reasoning else "",
            "skills_loaded": []
        }
        print(f"   📖 子Agent分析已加入上下文（未找到匹配技能，success=0）\n", flush=True)
        return json.dumps(result_dict)

    # 将读取到的技能内容逐个追加到全局 messages
    skill_summary_parts = []
    for item in loaded_contents:
        # skill_msg = {
        #     "role": "system",
        #     "content": (
        #         f"===== 已加载技能：{item['skill_name']} =====\n\n"
        #         f"{item['content']}\n\n"
        #         f"===== 技能 {item['skill_name']} 结束 ====="
        #     )
        # }
        # messages.append(skill_msg)
        skill_summary_parts.append(f"{item['skill_name']}({item['size']}字符)")

    # # 再加一条总结，说明子 Agent 的选择理由
    # if final_reasoning:
    #     messages.append({
    #         "role": "system",
    #         "content": f"===== 技能加载说明 =====\n\n子Agent根据以下分析选择了上述技能：\n{final_reasoning}\n\n===== 说明结束 ====="
    #     })

    skill_summary = "、".join(skill_summary_parts)
    print(
        f"\n📖 技能加载完成！\n"
        f"   已加载技能: {skill_summary}\n"
        f"   子Agent分析已同步到上下文\n",
        flush=True
    )

    return json.dumps({
        "success": 1,
        "skills_loaded": [item["skill_name"] for item in loaded_contents],
        "skills_detail": [{"name": item["skill_name"], "size_chars": item["size"]} for item in loaded_contents],
        "contents": {item["skill_name"]: item["content"] for item in loaded_contents},
        "reasoning_preview": final_reasoning[:300] if final_reasoning else "",
        "message": f"技能 '{'、'.join(item['skill_name'] for item in loaded_contents)}' 已加载成功，AI 将参考这些技能知识"
    })


def _list_available_skills():
    """列出所有可用技能（合并两个 skills 目录，启动目录优先）"""
    merged = _merge_skills()
    return [s for s, _ in merged]


def _list_available_skills_wrapper():
    """给子 Agent 用的 list_skills 工具包装函数，返回格式化的 JSON"""
    skills = _list_available_skills()
    merged = _merge_skills()
    
    # 标记每个技能来自哪个目录
    cwd_skills = []
    code_skills = []
    for s_name, source in merged:
        if source == "cwd":
            cwd_skills.append(s_name)
        else:
            code_skills.append(s_name)
    
    info_parts = []
    if CWD_SKILLS_DIR:
        info_parts.append(f"启动目录({CWD_SKILLS_DIR.replace(chr(92), '/')}): {len(cwd_skills)}个技能")
    if CODE_SKILLS_DIR:
        info_parts.append(f"代码目录({CODE_SKILLS_DIR.replace(chr(92), '/')}): {len(code_skills)}个技能")
    info_parts.append(f"合并后共 {len(skills)} 个技能（同名以启动目录版本为准）")
    
    return json.dumps({
        "success": 1,
        "skills": skills,
        "cwd_skills": cwd_skills,
        "code_skills": code_skills,
        "message": " | ".join(info_parts)
    })

# 工具名称 → 函数的映射表（自动路由用）
tool_func_map = {
    "run_bash": run_bash,
    "write_full_file": write_full_file,
    "read_full_file": read_full_file,
    "read_file_lines": read_file_lines,
    "compress": compress,
    "edit_file_lines": edit_file_lines,
    "edit_file_match": edit_file_match,
    "load_skill": load_skill,
    "get_code_path": get_code_path,
}


# ============================================================
# MCP 工具动态注册 —— 启动时获取 MCP 工具并注册到 tool_func_map
# ============================================================

def _init_mcp_tools():
    """初始化 MCP 工具连接，注册到 tool_func_map
    
    在 main() 启动时调用一次。
    之后 tools 列表要用 _get_merged_tools() 获取合并后的列表。
    """
    global tools
    
    mcp = get_mcp_manager()
    mcp.connect_all()
    
    mcp_tools = mcp.get_all_tools()
    if mcp_tools:
        print(f"   📦 注册 {len(mcp_tools)} 个 MCP 工具到路由表", flush=True)
        # 注册到 tool_func_map，让工具执行循环能路由到 mcp_call_tool
        for t in mcp_tools:
            tool_name = t["function"]["name"]
            if tool_name not in tool_func_map:
                tool_func_map[tool_name] = lambda name=tool_name, **kw: mcp_call_tool(name, **kw)
        
        # 更新全局 tools 列表
        tools = tools + mcp_tools
        print(f"   ✅ 合并后共 {len(tools)} 个工具可用", flush=True)


def _cleanup_mcp_tools():
    """清理 MCP 工具连接（程序退出时调用）"""
    try:
        mcp = get_mcp_manager()
        mcp.disconnect_all()
    except Exception as e:
        print(f"   ⚠️ 清理 MCP 连接时出错: {e}", flush=True)


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
    global tool_executing, interrupted, messages, CWD_SKILLS_DIR, CODE_SKILLS_DIR

    # 注册信号处理器 —— 只用于工具执行中的中断
    # 用户输入中的 Ctrl+C 由 prompt_toolkit 处理
    signal.signal(signal.SIGINT, sigint_handler)

    # 加载 API 配置（如果配置文件不存在或字段缺失，会提示用户输入）
    load_config()

    # 初始化日志（以程序启动时间命名）
    init_logger()

    # ----- 初始化 skills 目录（同时检查启动目录和代码目录下的 skills/，同名以启动目录版本为准） -----
    global CWD_SKILLS_DIR, CODE_SKILLS_DIR
    # ----- 初始化 skills 目录（同时检查启动目录和代码目录下的 skills/，同名以启动目录版本为准） -----
    global CWD_SKILLS_DIR, CODE_SKILLS_DIR
    _init_skills_dirs()
    
    # ----- 初始化 MCP 工具连接（连接所有配置的 MCP 服务器，注册工具到路由表） -----
    _init_mcp_tools()
    
    # ----- 构建 skills 列表提示 -----
    available_skills = _list_available_skills()
    skills_hint = ""
    if available_skills:
        skills_list_str = "\n".join(f"  - {s}" for s in available_skills)
        skills_hint = (
            f"\n\n你有以下技能知识库可供按需加载（使用 load_skill 工具）：\n"
            f"{skills_list_str}\n\n"
            f"load_skill 是一个智能子Agent——你不需要指定文件名，只需要描述你想做什么、需要哪方面的知识，"
            f"它会自动分析 skills/ 目录下的文件，找到最匹配的技能并加载到上下文中。\n"
            f"例如：\n"
            f"  load_skill(task_description='我想了解魔理沙的魔法弹幕技能')\n"
            f"  load_skill(task_description='我需要蘑菇相关的知识来制作魔法药水，可能还需要调查技巧')\n"
            f"当对话涉及相关领域时，你应该主动调用 load_skill 来获取更准确的知识。"
            f"技能只需加载一次，之后整个对话中都可以参考。"
        )

    system_prompt = (
        "你是一个兴趣使然的AI Agent，请模仿东方project的魔理沙来回复问题"
        f"{skills_hint}"
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
        # 每次外层循环开始时，执行大内容过期检查
        _expire_large_content()

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
                    # # ✨ 修复 tool_calls 和 tool 响应之间被插队的消息顺序
                    # # 这是因为 load_skill 等工具在执行时可能会向 messages 中插入 system 消息，
                    # # 导致 assistant（带 tool_calls）后面不是紧跟着 tool 响应，违反 OpenAI 协议规范
                    # if _fix_messages_tool_order(messages):
                    #     print("   🗟 已修复 tool_calls 和 tool 响应之间的消息顺序", flush=True)

                    # ✨ 关键改动：使用 get_context_aware_messages 包装
                    # 如果上下文超过阈值，会自动追加一条 system 提醒
                    api_messages = get_context_aware_messages(messages)
                    msg, reasoning_content = call_api(api_messages, tools=tools)
                except Exception as e:
                    print(f"\n💥 API调用翻车了: {e}\n", flush=True)
                    # # 🧹 回滚：如果最后一条消息是 assistant（含 tool_use），删掉它
                    # # 避免留下孤立的 tool_use 导致后续 API 调用持续报 400
                    # if len(messages) > 1 and messages[-1].get("role") == "assistant":
                    #     last_msg = messages[-1]
                    #     if last_msg.get("tool_calls"):
                    #         print("   🧹 检测到孤立的 tool_use，自动回滚最后一条消息", flush=True)
                    #         messages.pop()
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
    
    # ----- 程序退出时清理 MCP 连接 -----
    _cleanup_mcp_tools()


if __name__ == "__main__":
    main()
