#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
兴趣使然的AI Agent —— 魔理沙风格

【输入架构：多路复用 + 分区渲染】
  · 主线程 = UI 线程：常驻键盘输入循环。用 prompt_toolkit 的 patch_stdout() 做分区渲染，
    输入框常驻底部，模型输出画在输入框上方 —— 输出时照样能打字，输入排队追加到后续。
  · 输入框下方有一条状态栏（bottom_toolbar）：实时显示
      💤 等待输入 / ✨ 思考中… / 🔧 执行工具：<工具名> / 📦 压缩上下文…
    并在右侧展示上下文 token 用量 —— 一眼就能看出模型现在是在等你，还是在干活。
  · 后台线程 = agent 工作线程：从「输入总线」取事件，逐条驱动一轮对话。
  · 输入总线（io_bus.py，纯标准库）把 键盘 / socket / 管道(stdin) / 后台任务完成
    统一成一个事件队列：任意来源有输入，都会唤醒 agent（谁先来谁触发）。

【零依赖也能跑】
  一个第三方库都不装，只要 python3 即可运行：
    · 多路复用能力完整保留（socket / 管道照常工作）
    · 键盘输入自动退化为 input()（不再有常驻输入框，输出可能与输入行交织）
    · Markdown 渲染自动关闭，退化为纯文本
  装上 prompt_toolkit 可得常驻输入框 + 分区渲染，装上 rich 可得 Markdown 渲染。

用法：
  python ai_agent_prompt.py                       # 直接跑（零依赖也可）
  pip install prompt_toolkit rich                 # 可选增强
  python ai_agent_prompt.py --no-prompt-toolkit   # 强制退化输入
  python ai_agent_prompt.py -c ctx.log            # 指定上下文日志文件（续写）

多行输入：回车换行，Alt+Enter（或 Esc+Enter）提交；退化模式用单独一行 '.' 结束
Ctrl+C 中断工具调用（回到对话）；空闲时连按两次 Ctrl+C 退出
Ctrl+D 退出程序

socket 输入源（可选）：在 ai_agent_config.json 的 input_sources.socket 里开启，
  之后 `echo "帮我看看磁盘" | nc 127.0.0.1 8765` 就能从外部喂消息给 agent。
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
import argparse
import queue
import time
import atexit
import tempfile

# prompt_toolkit 为可选依赖 —— 有则用其增强输入（多行、历史、快捷键），无则退化为 input()
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Dimension
    from prompt_toolkit.application import get_app
    # patch_stdout：把「后台线程的输出」重定向成「输入框上方的输出」，
    # 让输入框常驻底部、模型输出时照样能打字（B 档分区渲染的核心）
    from prompt_toolkit.patch_stdout import patch_stdout
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    PromptSession = None
    InMemoryHistory = None
    KeyBindings = None
    Dimension = None
    get_app = None
    patch_stdout = None
    HAS_PROMPT_TOOLKIT = False

# rich 为可选依赖 —— 有则把「大模型自己说的话」渲染成终端 markdown 样式（表格/加粗/代码块），
# 无则退化为纯文本缩进输出。注意：只用于大模型的自然语言回复，
# 工具函数/命令的一切输出仍走普通 print，不参与渲染。
try:
    from rich.console import Console as _RichConsole
    from rich.console import Group as _RichGroup
    from rich.markdown import Markdown as _RichMarkdown
    from rich.padding import Padding as _RichPadding
    from rich.table import Table as _RichTable
    from rich import box as _RichBox
    HAS_RICH = True
except ImportError:
    _RichConsole = None
    _RichGroup = None
    _RichMarkdown = None
    _RichPadding = None
    _RichTable = None
    _RichBox = None
    HAS_RICH = False

# 是否启用 markdown 渲染。默认关闭，由 main() 根据「rich 可用 + 交互模式 + 使用 prompt_toolkit」最终裁定。
USE_MARKDOWN = False

# rich Console 实例（仅 HAS_RICH 时创建；非 TTY 环境下 rich 会自动剥离样式）。
# Windows 上强制 legacy_windows=True，让 rich 走 Win32 控制台 API 而非吐 ANSI 转义码——
# 否则在 mintty/winpty、老式 conhost 等不解析 ANSI 的终端里，颜色码会被原样显示成 ?[1;36m 乱码。
_rich_kwargs = {"legacy_windows": True} if os.name == "nt" else {}
_rich_console = _RichConsole(**_rich_kwargs) if HAS_RICH else None


def _rich_markdown_supports_tables():
    """探测当前 rich 的 Markdown 是否渲染 GFM 表格。

    rich < 13.4 的 Markdown 基于 commonmark，不支持表格（Python 3.6 能装到的最高版本
    rich 12.6.0 正属此列）。这里渲染一个极小的表格，看输出里是否还残留原始竖线 '|'。
    """
    if not HAS_RICH or _rich_console is None:
        return False
    try:
        with _rich_console.capture() as cap:
            _rich_console.print(_RichMarkdown("| a | b |\n| - | - |\n| 1 | 2 |"))
        return "|" not in cap.get()   # 渲染成表格则不再出现原始竖线
    except Exception:
        return False


# 当前 rich 能否自己渲染 markdown 表格；不能则由下面的轻量表格渲染器补齐
RICH_MD_TABLES = _rich_markdown_supports_tables()


# ============ 轻量 GFM 表格渲染（补齐旧版 rich 不支持的 markdown 表格）============
_MD_DELIM_CELL = re.compile(r":?-{1,}:?")


def _md_split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _md_is_delim_row(line):
    """判断是否为表格的 '| --- | --- |' 分隔行。"""
    if "-" not in line:
        return False
    cells = [c for c in _md_split_row(line) if c]
    return bool(cells) and all(_MD_DELIM_CELL.fullmatch(c) for c in cells)


def _md_justify(delim_cell):
    left = delim_cell.startswith(":")
    right = delim_cell.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    return "left"


def _md_build_table(header, delims, rows):
    table = _RichTable(box=_RichBox.SIMPLE, show_header=True,
                       header_style="bold", pad_edge=False)
    for idx, col in enumerate(header):
        cell = delims[idx] if idx < len(delims) else ""
        table.add_column(col, justify=_md_justify(cell))
    for row in rows:
        table.add_row(*row)
    return table


def _md_render_blocks(content):
    """把 markdown 拆成「表格块」与「其余块」：表格用 rich.Table 渲染，其余交给 rich.Markdown。

    仅用于当前 rich 自身不支持 markdown 表格时（如 Python 3.6 上的 rich 12.x）。
    代码围栏（``` / ~~~）内的一律不当表格处理。
    """
    renderables = []
    buf = []
    lines = content.split("\n")
    in_fence = False
    fence = None
    i = 0

    def flush_md():
        if buf:
            text = "\n".join(buf).strip("\n")
            if text.strip():
                renderables.append(_RichMarkdown(text, code_theme="monokai"))
            del buf[:]

    while i < len(lines):
        line = lines[i]
        s = line.strip()

        # 代码围栏跟踪（围栏内不解析表格）
        if s.startswith("```") or s.startswith("~~~"):
            mark = s[:3]
            if not in_fence:
                in_fence, fence = True, mark
            elif mark == fence:
                in_fence, fence = False, None
            buf.append(line)
            i += 1
            continue

        # 表格起始：当前行含 '|' 且下一行是分隔行
        if (not in_fence) and ("|" in line) and (i + 1 < len(lines)) and _md_is_delim_row(lines[i + 1]):
            flush_md()
            header = _md_split_row(line)
            delims = _md_split_row(lines[i + 1])
            ncol = len(header)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip() and "|" in lines[i] and not _md_is_delim_row(lines[i]):
                cells = _md_split_row(lines[i])
                cells = (cells + [""] * ncol)[:ncol]
                rows.append(cells)
                i += 1
            renderables.append(_md_build_table(header, delims, rows))
            continue

        buf.append(line)
        i += 1

    flush_md()
    if not renderables:
        return _RichMarkdown(content, code_theme="monokai")
    if len(renderables) == 1:
        return renderables[0]
    return _RichGroup(*renderables)


# ============================================================
#  常驻输入模式（patch_stdout）下的 rich Console
# ============================================================
# 这里有个大坑，务必看清：
#   patch_stdout() 在 raw=False（默认）时，底层 Output.write() 会把 ESC(\x1b)
#   直接替换成 '?'，于是 rich 的颜色码就成了用户看到的 ?[1;36m 乱码；而 Win32Output
#   这类后端本身就完全不解析 ANSI，写进去同样变乱码。
#   所以：rich 能不能输出颜色，完全取决于 prompt_toolkit 当前用的是哪种输出后端。
#     · Vt100 / Windows10 / ConEmu 后端 → ANSI 安全，开颜色（patch_stdout(raw=True)）
#     · Win32Output 等             → 必须关颜色，只输出结构（表格/标题/列表）不带转义
# 用一个「不带颜色的 Console」渲染时，rich 依然会画出表格框线、缩进和层级结构，
# 只是没有颜色和加粗 —— 观感损失很小，但绝不会再出现乱码。
_rich_console_ui = None          # 无颜色版（Win32 后端）
_rich_console_ui_ansi = None     # 带 ANSI 版（Vt100 系后端）


def _ui_console():
    """懒创建「常驻输入模式」专用 rich Console（不可用返回 None）。

    按 UI_ANSI_OK 选择带色 / 不带色两种实现，避免把 ANSI 塞给吃不下它的后端。
    """
    global _rich_console_ui, _rich_console_ui_ansi
    if not HAS_RICH:
        return None
    if UI_ANSI_OK:
        if _rich_console_ui_ansi is None:
            try:
                _rich_console_ui_ansi = _RichConsole(
                    force_terminal=True, legacy_windows=False
                )
            except Exception:
                _rich_console_ui_ansi = None
        return _rich_console_ui_ansi
    if _rich_console_ui is None:
        try:
            # color_system=None：一个转义序列都不吐，最安全
            _rich_console_ui = _RichConsole(color_system=None, legacy_windows=False)
        except Exception:
            _rich_console_ui = None
    return _rich_console_ui


# 匹配 ANSI 转义序列（CSI / 两字符序列），用作最后一道保险丝
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def _strip_ansi(text):
    """剥掉字符串里的 ANSI 转义序列（保险丝，防止任何漏网的转义码污染界面）。"""
    return _ANSI_ESCAPE_RE.sub("", text)


def print_assistant(content, name="魔理沙"):
    """打印大模型回复：满足条件时走 markdown 渲染，否则退回纯文本缩进（与旧行为一致）。

    仅供「大模型自己说的话」使用；工具函数的输出请勿调用本函数。
    """
    if not content:
        return
    if USE_MARKDOWN and _rich_console is not None:
        try:
            if RICH_MD_TABLES:
                body = _RichMarkdown(content, code_theme="monokai")
            else:
                # 旧版 rich（如 Python 3.6 上的 12.x）不认 markdown 表格，用自带渲染器补齐
                body = _md_render_blocks(content)

            if UI_PROMPT_ACTIVE:
                # 常驻输入模式：先整体渲染成字符串，再一次性写出。
                # 这样 patch_stdout 收到的是一整块文本，输入框不会被 rich 的多次写操作撕成几截。
                console = _ui_console()
                if console is None:
                    raise RuntimeError("常驻输入模式下 rich Console 不可用")
                with console.capture() as cap:
                    console.print(f"[bold cyan]{name}:[/bold cyan]")
                    console.print(_RichPadding(body, (0, 0, 0, 4)))
                text = cap.get()
                if text:
                    if not UI_ANSI_OK:
                        # 保险丝：当前后端吃不下 ANSI（Win32Output）或 patch_stdout
                        # 会把 ESC 换成 '?'，这里再剥一层，确保界面上绝不出现 ?[1;36m
                        text = _strip_ansi(text)
                    if not text.endswith("\n"):
                        text += "\n"
                    sys.stdout.write(text)
                    sys.stdout.flush()
                return

            # 直接交给 rich 输出，绝不要 capture 成字符串再 print——
            # capture 会把样式强制序列化成 ANSI 转义码，从而绕过 rich 的 Win32 控制台渲染路径，
            # 在 legacy Windows 终端（mintty/winpty、老式 conhost）下就会显示成 ?[1;36m 之类的乱码。
            _rich_console.print(f"[bold cyan]{name}:[/bold cyan]")
            _rich_console.print(_RichPadding(body, (0, 0, 0, 4)))
            sys.stdout.flush()
            return
        except Exception:
            pass  # 渲染翻车就退回纯文本，绝不拖累主流程
    print(f"{name}:\n    {content.replace(chr(10), chr(10) + '    ')}", flush=True)


# MCP (Model Context Protocol) 支持 —— 连接外部 MCP 服务器
# 通过 mcp_manager.py 统一管理 stdio/HTTP 模式的 MCP 服务器连接
from mcp_manager import get_mcp_manager, load_mcp_config

# 多路复用输入总线（纯标准库，零第三方依赖）——键盘 / socket / 管道 / 后台任务统一入口
import io_bus

# ============================================================
#  工具模块 —— 工具定义、工具函数、MCP/Skills 已拆分到 ai_agent_tools.py
# ============================================================
# 工具 schema 列表（tools）会被 MCP 动态刷新，故经由模块属性访问；
# tool_func_map 始终原地增删、从不整体重新绑定，可直接 import 其对象。
import ai_agent_tools
from ai_agent_tools import tool_func_map, smart_decode, TERMINAL_TOOLS, IMAGE_TOOLS
# 把本模块对象注入工具模块，供工具函数访问共享运行时状态（messages/interrupted/...）
ai_agent_tools.bind_runtime(sys.modules[__name__])


# ============================================================
#  配置管理 —— 从同级 JSON 文件读取 API 配置
# ============================================================

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_agent_config.json")

DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "protocol": "openai",
    # 多模态辅助模型配置（可选）——字段与主配置一一对应，加 mul_ 前缀：
    # mul_api_key / mul_base_url / mul_model / mul_protocol
    # 配置后 read_image 可传 description 参数，由辅助多模态模型识别图片并返回文本描述
    "mul_api_key": "",
    "mul_base_url": "",
    "mul_model": "",
    "mul_protocol": "openai",
    # ---- 上下文 / 压缩阈值（均以 token 计，取自 API 返回的真实用量）----
    # 三者都支持 k / m 单位，大小写与空格不限，如 1000000 / "1m" / "1 M" / "700k" / " 1.5 M "
    # 当前模型最大上下文 token 数：用于计算上下文使用率百分比
    "context_limit_tokens": 1000000,
    # 达到此 token 数触发「无人值守压缩」：程序在工具循环里自动压缩中间的工具调用（轻量、不调用大模型）
    "auto_compress_tokens": 700000,
    # 达到此 token 数触发「大模型压缩」：暂缓用户输入，先让大模型调用 compress 工具重写上下文（重量、耗一次 API）
    "llm_compress_tokens": 700000,
    # ---- 工具阈值 ----
    # read_image 单张图片大小上限，单位「字节」，不带单位即按字节算；
    # 也支持 k / kb（1024）、m / mb（1024×1024）后缀，如 "5mb" / "512kb"
    "max_image_size_byte": 5 * 1024 * 1024,
    # ---- 请求直通：provider 相关的高级参数（思考等级 / 扩展思考 / beta 开关等）----
    # 各家供应商的「思考等级」「推理预算」等参数名各不相同，且基本都在请求体（body）
    # 而非请求头里。这里提供两个「原样透传」的出口，程序不需要认识每一个参数名：
    #   extra_headers — 合并进 HTTP 请求头（如 anthropic-beta、额外鉴权头）
    #   extra_body    — 合并进请求体 JSON（如 reasoning_effort、thinking、enable_thinking）
    # 例：{"extra_body": {"reasoning_effort": "high"}}（OpenAI o 系列 / GPT-5）
    #     {"extra_body": {"thinking": {"type": "enabled", "budget_tokens": 16000},
    #                     "max_tokens": 32000},
    #      "extra_headers": {"anthropic-beta": "interleaved-thinking-2025-05-14"}}
    # 注意：messages / tools / tool_choice / stream 由程序自己构造，不允许覆盖。
    "extra_headers": {},
    "extra_body": {},
    # 多模态辅助模型也有对应的透传出口（同样只在配置了 mul_* 后生效）
    "mul_extra_headers": {},
    "mul_extra_body": {},
    # ---- 多路复用输入源配置 ----
    # socket 输入源：开启后可用 nc / telnet / 任意 TCP 客户端把消息喂给 agent，
    # 与键盘输入平级 —— 谁先来谁触发主循环。
    # 协议：每行文本 = 一条消息；以 '!' 开头的行是控制指令（!exit 退出）；
    #       auth_token 非空时，连接后第一行必须等于该 token。
    "input_sources": {
        "socket": {
            "enabled": False,
            "host": "127.0.0.1",
            "port": 8765,
            "auth_token": ""
        }
    },
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

    # 补齐 DEFAULT_CONFIG 中有实际默认值、但配置文件里缺失的字段（如上下文 / 压缩阈值），
    # 让它们出现在 ai_agent_config.json 中，方便用户直接查看和修改。
    # mul_* 是多模态辅助模型的可选字段，全部跳过，避免给没用上的用户塞一堆无用配置。
    for key, default_value in DEFAULT_CONFIG.items():
        if key in config or key.startswith("mul_"):
            continue
        config[key] = default_value
        need_save = True

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

# Ctrl+C 连续两次确认退出（仿 Claude 风格）：记录上次 Ctrl+C 的时间戳
_ctrl_c_state = {"ts": 0.0}
# 两次 Ctrl+C 之间的时间窗口（秒）
EXIT_CONFIRM_SECONDS = 2.0

# API 超时/网络错误自动重试（仿运维保活）：调用 API 时若遇到网络错误或超时，
# 等待 API_TIMEOUT_RETRY_WAIT 秒后重试，每条用户输入最多重试 API_TIMEOUT_RETRY_MAX 次；
# 每次收到新的用户输入会刷新剩余额度，用尽后停止自动重试，回到等待用户输入。
API_TIMEOUT_RETRY_WAIT = 10      # 每次重试前等待（秒）
API_TIMEOUT_RETRY_MAX = 10       # 每条用户输入最多自动重试次数
_api_timeout_retry_remaining = API_TIMEOUT_RETRY_MAX
# 是否为 input() 退化模式（prompt_toolkit 不可用或被 --no-prompt-toolkit 强制禁用时置 True）。
# 用于 sigint_handler 区分 Ctrl+C 的处理方式。
USE_INPUT_MODE = False

# ---- 多路复用输入相关全局 ----
# 是否处于「常驻输入模式」：主线程在 patch_stdout 下跑 prompt_toolkit 输入循环，
# agent 跑在后台线程。此标志为 True 时：
#   ① 输出统一走被 patch_stdout 接管的 sys.stdout，画到输入框上方；
#   ② 转圈动画（写 stderr）停用，避免撕坏输入框。
UI_PROMPT_ACTIVE = False
# 常驻输入模式下，prompt_toolkit 的输出后端能否安全承载原生 ANSI 转义。
#   True （Vt100 / Windows10 / ConEmu）→ rich 可以上色，patch_stdout(raw=True)
#   False（Win32Output / PlainTextOutput 等）→ 必须关颜色，否则界面出现 ?[1;36m 乱码
# 由 _run_ui_loop() 在启动时用 _pt_can_use_ansi() 探测后写入。
UI_ANSI_OK = False
# 当前的输入总线实例（由 main() 创建，供后台任务完成钩子投递事件）
_input_bus = None

# ---- 常驻输入模式下的「输入框下方状态栏」数据 ----
# agent 工作线程写，主线程渲染时读。
#   state  : idle(等待输入) / thinking(思考中) / tool(执行工具) / compress(压缩上下文)
#   detail : 附加信息，例如正在执行的工具名
#   usage  : 精简用量行（懒计算后缓存，状态栏只读不算，避免每次重绘都去序列化上下文）
_status_lock = threading.Lock()
_agent_status = {"state": "idle", "detail": "", "usage": ""}

_STATUS_LABEL = {
    "idle": "💤 等待输入",
    "thinking": "✨ 思考中…",
    "tool": "🔧 执行工具",
    "compress": "📦 压缩上下文…",
}


def _set_agent_status(state, detail=""):
    """更新 agent 运行状态（工作线程调用）。"""
    with _status_lock:
        _agent_status["state"] = state
        _agent_status["detail"] = detail or ""


def _set_usage(text):
    """更新状态栏里的用量行（工作线程调用）。"""
    with _status_lock:
        _agent_status["usage"] = text or ""


def _bottom_toolbar():
    """prompt_toolkit 的 bottom_toolbar 回调：渲染「输入框下方」的状态栏。

    这个回调运行在主线程的渲染循环里，会被频繁调用，
    所以这里只做字符串拼接，绝不重复计算 token（用量走缓存）。
    """
    with _status_lock:
        state = _agent_status["state"]
        detail = _agent_status["detail"]
        usage = _agent_status["usage"]

    label = _STATUS_LABEL.get(state, state)
    if detail:
        label = f"{label}：{detail}"
    text = f" {label}"
    if usage:
        text += f"   ｜   📊 {usage}"
    text += " "
    return [("class:bottom-toolbar", text)]


def sigint_handler(signum, frame):
    """Ctrl+C 信号处理器"""
    global interrupted, tool_executing
    if tool_executing:
        # 工具执行中 → 设中断标志，让循环自己收工
        if not interrupted:
            interrupted = True
            print("\n\n⚠️  中断魔法吟唱！回到对话模式...\n", flush=True)
        return
    # 非工具执行中：
    #   - prompt_toolkit 模式：由 prompt_toolkit 接管输入中的 Ctrl+C，这里不需要动作
    #   - input() 模式：此 handler 是 Python 对 SIGINT 的唯一响应者，若什么都不做，
    #     input() 就不会收到 KeyboardInterrupt，导致按 Ctrl+C 无法退出。
    #     因此主动抛 KeyboardInterrupt，让主循环统一捕获并退出。
    if USE_INPUT_MODE:
        raise KeyboardInterrupt


class UserInterrupt(Exception):
    """用户按 Ctrl+C 中断了 API 请求，需要立即停止等待。

    与普通 Exception 区分开，方便调用上方捕获后做「优雅中断」而非「错误」处理。
    """
    pass


def _should_retry_timeout():
    """检查并消费一次「API 超时/网络错误」的自动重试名额。

    返回 True 表示应重试（已消耗一次额度）；返回 False 表示额度已用尽，应停止重试。
    额度为每条用户输入一份，由主循环在收到新输入时重置为 API_TIMEOUT_RETRY_MAX。
    """
    global _api_timeout_retry_remaining
    if _api_timeout_retry_remaining > 0:
        _api_timeout_retry_remaining -= 1
        return True
    return False


def _interruptible_sleep(seconds):
    """可被 Ctrl+C 中断的 sleep：在等待期间轮询全局 interrupted 标志。

    用于 API 超时/网络错误后的重试等待。用户按 Ctrl+C（工具执行中 interrupted=True）
    时能在 ~0.1s 内检测到并抛出 UserInterrupt，立即结束等待，而不会干等整段 sleep。
    """
    global interrupted
    deadline = time.time() + seconds
    try:
        while not interrupted and time.time() < deadline:
            time.sleep(0.1)
    except KeyboardInterrupt:
        # 部分平台（尤其 Linux/CentOS）上 sleep 被 Ctrl+C 打断时可能直接抛
        # KeyboardInterrupt。转成统一的优雅中断，交由调用方捕获。
        interrupted = True
        raise UserInterrupt()
    if interrupted:
        raise UserInterrupt()


def _urlopen_interruptible(req, timeout=180, poll_interval=0.2):
    """
    在后台线程中执行 urllib.request.urlopen，主线程以 poll_interval 粒度轮询
    全局 interrupted 标志，实现「按 Ctrl+C 立即结束网络等待」。

    背景：
      原有 urllib.request.urlopen 是阻塞调用，按 Ctrl+C 时 sigint_handler 仅设置
      interrupted=True，并不会打断这个阻塞等待，导致用户白白等待 network/API 卡顿。
      本函数把请求放到 daemon 子线程，主线程循环 join(timeout) 并检查 interrupted，
      一旦中断就抛出 UserInterrupt，立即结束等待（最多 poll_interval 秒延迟）。
      即使后台线程仍在跑（daemon），也不阻塞进程退出，用户可立刻回到对话。

    返回：
      成功   -> {"ok": True, "result": <解析后的 JSON dict>}
      HTTP 错 -> {"ok": False, "kind": "http", "code": <int>, "body": <str>}
      网络错  -> {"ok": False, "kind": "url", "reason": <str>}

    抛出：
      UserInterrupt：用户中断。
    """
    global interrupted
    container = {
        "ok": False,
        "result": None,
        "kind": None,
        "code": None,
        "body": None,
        "reason": None,
        "done": False,
    }

    def worker():
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                container["result"] = json.loads(resp.read().decode("utf-8"))
            container["ok"] = True
        except urllib.error.HTTPError as e:
            # 在线程内读取错误 body，避免跨线程读取网络对象
            container["kind"] = "http"
            container["code"] = e.code
            try:
                container["body"] = e.read().decode("utf-8", errors="replace")
            except Exception:
                container["body"] = ""
        except urllib.error.URLError as e:
            container["kind"] = "url"
            container["reason"] = str(e.reason)
        except Exception as e:
            container["kind"] = "url"
            container["reason"] = str(e)
        finally:
            container["done"] = True

    # daemon=True：用户中断后，即使后台请求仍在跑，也不会阻止进程退出
    t = threading.Thread(target=worker, daemon=True)
    t.start()

    # 主线程轮询：请求一旦完成立即返回；否则每 poll_interval 秒检查一次中断
    while not container["done"]:
        if interrupted:
            raise UserInterrupt()
        try:
            t.join(timeout=poll_interval)
        except KeyboardInterrupt:
            # 部分平台（尤其 Linux/CentOS + Python3.6）上，阻塞等待线程 join 时
            # 收到 SIGINT 会直接抛 KeyboardInterrupt，而不是先让 sigint_handler
            # 设置 interrupted。这里转成统一的优雅中断（UserInterrupt）。
            interrupted = True
            raise UserInterrupt()

    if container["kind"] == "http":
        return {"ok": False, "kind": "http", "code": container["code"], "body": container["body"]}
    if container["kind"] == "url":
        return {"ok": False, "kind": "url", "reason": container["reason"]}
    return {"ok": True, "result": container["result"]}

# ============================================================
#  0. 上下文压缩相关常量 & 全局 messages
# ============================================================
# 以下三个阈值均可通过 ai_agent_config.json 配置，这里只作为默认值；
# main() 启动时会调用 _refresh_context_limits() 用配置覆盖。
#
# 当前模型最大上下文 token 数（用于计算使用率百分比）
CONTEXT_LIMIT = 1_000_000
# 「无人值守压缩」阈值（token）：工具循环中上下文超过此值时，程序自动压缩中间的工具调用（轻量、不调大模型）
AUTO_COMPRESS_THRESHOLD = 700_000
# 「大模型压缩」阈值（token）：新的一轮对话开始前上下文超过此值时，先让大模型调用 compress 工具重写上下文
LLM_COMPRESS_THRESHOLD = 700_000

# 长内容自动过期机制（第二层：软过期）
# tool response 内容超过此大小时在 large_content_counter 中计数，
# 达到过期轮数后自动替换为过期提示
LARGE_CONTENT_THRESHOLD = 50 * 1024  # 50KB
# 大内容在 messages 中存在超过此轮数（外层 while 循环次数）后自动过期
LARGE_CONTENT_EXPIRE_ROUNDS = 50

# 工具函数硬截断阈值（第一层：保底保护）
# 当工具返回的内容超过此大小时，直接截断保留前 N 字节，
# 防止单次工具调用撑爆上下文

# 大内容计数器：tool_call_id -> 被检测到为大内容的轮次数
# 在外层 while 循环每次开始时扫描 messages 并更新
large_content_counter = {}

# 主模型是否已确认不支持多模态（读图）：
# 一旦检测到模型对多模态内容报 HTTP 400 或返回空 content，置为 True；
# 之后注入图片前先检查此标记，避免反复触发"空返回"毒化对话。
_main_model_no_multimodal = False

# messages 提升到全局，方便 compress 工具直接修改
messages = []


def _json_bytes(obj):
    """任意对象序列化成 JSON 后的 UTF-8 字节数。"""
    json_str = json.dumps(obj, ensure_ascii=False)
    # 用户粘贴/外部输入可能带入 UTF-8 surrogate（\ud800-\udfff），直接 encode 会抛
    # UnicodeEncodeError。这里用 replace 兜底，保证上下文估算永不因编码崩溃。
    return len(json_str.encode('utf-8', errors='replace'))


def get_context_size(msg_list):
    """messages 的 JSON 字节数（只算对话本身，不含工具 schema）。"""
    return _json_bytes(msg_list)


def get_tools_size(tools=None):
    """工具 schema 在请求体里占的 JSON 字节数（tools 为空则返回 0）。

    这是请求体里最容易被忽略的大头：内置工具 + MCP 工具的 schema 加起来
    通常有十几 KB，却会实实在在消耗 prompt_tokens。tools 随 MCP 连接变化，
    所以每次实时算，不做缓存（十几 KB 的序列化开销可以忽略）。
    """
    if tools is None:
        tools = ai_agent_tools.tools
    if not tools:
        return 0
    return _json_bytes({"tools": tools, "tool_choice": "auto"})


def get_request_size(msg_list=None, tools=None):
    """估算「实际发出去的请求体」的 JSON 字节数（messages + tools）。

    必须按这个口径统计，才能和 API 返回的 prompt_tokens 对照——因为
    prompt_tokens 是「整个请求」的 token 数，包含 tools schema。
    只统计 messages 的话，短对话会出现「字节数 ≪ token 数」的假象
    （工具 schema 一二十 KB，而消息本身可能只有几百字节）。

    注：请求体里还有 model 等固定字段（几十字节），这里忽略，误差可忽略。
    """
    if msg_list is None:
        msg_list = messages
    if tools is None:
        tools = ai_agent_tools.tools
    payload = {"messages": msg_list}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return _json_bytes(payload)


# ---- 真实 token 用量（取自 API 响应的 usage 字段）----
# 最近一次「主模型」调用的用量与请求体字节数，用于：
#   ① 展示准确的上下文 token 数（不再靠字节数瞎猜）；② 让压缩阈值按 token 判断。
_last_usage = None           # 归一化 usage：{"prompt_tokens","completion_tokens","total_tokens","cached_tokens"}
_last_usage_ctx_bytes = 0    # 该次请求发送时「完整请求体」的 JSON 字节数（含 tools，口径同 prompt_tokens）
_last_usage_sent_id = 0      # 该次请求发送时 messages 列表的对象 id（判「回复是否已写回」用）
_last_usage_msg_count = 0    # 该次请求发送时 messages 的条数（判「回复是否已写回」用）


def _parse_token_count(raw, fallback):
    """把配置里的 token 数量解析成正整数，支持 k / m 单位（大小写不限、空格与逗号忽略）。

    可接受的写法：
        1000000     "1000000"     "1m"      "1 M"      "1.5m"
        700000      "700k"        " 700 K "  "1500K"   "1,000,000"
    空白（含全角空格）、下划线、千分位逗号一律忽略；k = 1000，m = 1000000，
    因此 "1500K" 与 "1.5m" 等价，都是 1500000。不带单位则视为绝对数值。
    其余情况（空值、文本、0 / 负数、inf / nan 等）一律返回 fallback。
    """
    if raw is None or isinstance(raw, bool):
        return fallback

    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = str(raw).strip().lower()
        # 去掉所有空白字符（含全角空格）、下划线和千分位逗号：" 1.5 M " -> "1.5m"
        text = "".join(text.split()).replace("_", "").replace(",", "")
        if not text:
            return fallback
        multiplier = 1.0
        if text.endswith("k"):
            multiplier, text = 1_000.0, text[:-1]
        elif text.endswith("m"):
            multiplier, text = 1_000_000.0, text[:-1]
        try:
            value = float(text) * multiplier
        except ValueError:
            return fallback

    try:
        value = int(value)  # inf / nan / 超范围会在这里抛异常
    except (ValueError, OverflowError):
        return fallback
    return value if value > 0 else fallback


def _parse_size_bytes(raw, fallback_bytes):
    """把配置里的「字节数」解析成正整数，支持 k / kb、m / mb 单位（大小写与空格不限）。

    可接受的写法：
        5242880     "5242880"     "5m"      "5 MB"     "5120KB"    "1,048,576"
    不带单位时按「字节」算；带单位时 k / kb = 1024，m / mb = 1024×1024
    （采用二进制的 1024 进位，与文件管理器里的显示口径一致）。
    其余情况（空值、文本、0 / 负数、inf / nan 等）一律返回 fallback_bytes。
    """
    if raw is None or isinstance(raw, bool):
        return fallback_bytes

    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        text = "".join(str(raw).strip().lower().split()).replace("_", "").replace(",", "")
        if not text:
            return fallback_bytes
        # 先匹配两字母后缀再匹配单字母，避免 "kb" 被当成 "k" + 尾巴 "b"
        multiplier = 1.0
        if text.endswith("kb"):
            multiplier, text = 1024.0, text[:-2]
        elif text.endswith("mb"):
            multiplier, text = 1024.0 * 1024, text[:-2]
        elif text.endswith("k"):
            multiplier, text = 1024.0, text[:-1]
        elif text.endswith("m"):
            multiplier, text = 1024.0 * 1024, text[:-1]
        try:
            value = float(text) * multiplier
        except ValueError:
            return fallback_bytes

    try:
        value = int(value)  # inf / nan / 超范围会在这里抛异常
    except (ValueError, OverflowError):
        return fallback_bytes
    return value if value > 0 else fallback_bytes


def _refresh_context_limits():
    """从 ai_agent_config.json 读取上下文 / 压缩阈值（token），刷新全局变量。

    对应配置项（缺失或非法时保持当前默认值）：
      context_limit_tokens  当前模型最大上下文 token 数
      auto_compress_tokens  达到多少 token 触发「无人值守压缩」
      llm_compress_tokens   达到多少 token 触发「大模型压缩」

    三者都支持 k / m 单位，例如 1000000 / "1m" / "1 M" / "700k" 均可。
    """
    global CONTEXT_LIMIT, AUTO_COMPRESS_THRESHOLD, LLM_COMPRESS_THRESHOLD
    try:
        cfg = load_config()
    except Exception:
        return

    def _pick(key, current):
        return _parse_token_count(cfg.get(key, current), current)

    CONTEXT_LIMIT = _pick("context_limit_tokens", CONTEXT_LIMIT)
    AUTO_COMPRESS_THRESHOLD = _pick("auto_compress_tokens", AUTO_COMPRESS_THRESHOLD)
    LLM_COMPRESS_THRESHOLD = _pick("llm_compress_tokens", LLM_COMPRESS_THRESHOLD)


def _record_main_usage(usage, sent_messages, sent_tools=None):
    """记录主模型本次调用的真实用量（子 Agent / 辅助模型调用不记录）。

    字节锚点取「完整请求体」（messages + tools），与 prompt_tokens 同口径；
    否则工具的 token 会被按比例摊到消息字节上，导致后续估算严重偏高。
    """
    global _last_usage, _last_usage_ctx_bytes, _last_usage_sent_id, _last_usage_msg_count
    if not usage:
        return
    _last_usage = usage
    _last_usage_ctx_bytes = get_request_size(sent_messages, sent_tools)
    # 记下发送时 messages 的对象 id 与条数，供 _pending_completion_tokens 判断
    # 「这次回复是否已写回 messages」（写回后列表变长 / 被整体替换，就视为已计入）
    _last_usage_sent_id = id(sent_messages)
    _last_usage_msg_count = len(sent_messages)


def _tokens_from_bytes(cur_bytes):
    """把「请求体字节数」换算成 token 数。

    - 有 API usage 时：以最近一次请求的 prompt_tokens / 请求体字节数为比例换算
      （每次 API 返回都会自我校正）；
    - 没有 usage 时：退回粗略估算（约 4 字节 / token）。
    """
    if _last_usage and _last_usage_ctx_bytes > 0:
        pt = _last_usage.get("prompt_tokens") or 0
        if pt > 0:
            return int(cur_bytes * pt / _last_usage_ctx_bytes)
    return cur_bytes // 4


def _pending_completion_tokens():
    """最近一次主模型调用的 completion_tokens —— 仅当这次回复「尚未写回 messages」时才算数。

    判据：当前 messages 仍是发送请求时那个列表（对象 id 相同）且长度未变，
    说明本轮回复还没 append 进来 → 它的输出需要暂时补进「上下文总占用」。
    一旦回复写回（同一个列表变长），或被 compress 工具整体替换（变成另一个列表对象），
    输出就已经作为 prompt 的一部分存在于 messages 中 → 返回 0，绝不重复计入。

    这样「上下文总占用」在任意时刻都自洽：压缩阈值与状态栏显示走同一口径。
    """
    if not _last_usage:
        return 0
    if id(messages) != _last_usage_sent_id or len(messages) != _last_usage_msg_count:
        return 0
    return _last_usage.get("completion_tokens") or 0


def get_context_tokens(msg_list=None, tools=None):
    """当前上下文的「总占用」token 数（输入 + 输出，口径同 API 的 total_tokens）。

    锚点与换算都基于「完整请求体」（含 tools schema）：
      · 输入侧 = 当前请求体折算出的 prompt_tokens；
      · 输出侧 = 最近一次调用的 completion_tokens，且仅当回复尚未写回 messages 时才补
        （见 _pending_completion_tokens）——因此无论何时调用都不会重复计数。

    压缩判定在「上一轮回复已写回 messages」之后执行，此时返回值 ≈ 下一轮请求的
    prompt_tokens（过去所有输出都已沉淀为 prompt），与窗口口径 total_tokens 等价。
    """
    return _tokens_from_bytes(get_request_size(msg_list, tools)) + _pending_completion_tokens()


def format_usage_line():
    """一行式用量摘要：真实 token 数与请求体 JSON 字节数对照。

    上下文口径与 API 对齐：窗口占用 = 输入（prompt_tokens）+ 输出（completion_tokens），
    也就是 API 返回的 total_tokens。

    本函数（经 print_usage）在「每次 API 返回后、assistant 回复尚未写回 messages 之前」
    被调用，此时 messages 恰好等于刚发出去的那份 prompt，所以：
        prompt_tok（输入估算）≈ 刚发出的 prompt_tokens
        prompt_tok + completion_tokens = 刚那次调用的 total_tokens
    因为回复还没写回 messages，加输出不会把同一段内容重复计入。
    """
    msg_bytes = get_context_size(messages)
    tools_bytes = get_tools_size()
    cur_bytes = get_request_size(messages)
    # 复用同一个 cur_bytes 换算 token，保证展示出的 tok 与字节严格对得上
    prompt_tok = _tokens_from_bytes(cur_bytes)                        # 输入侧
    comp_tok = _pending_completion_tokens()                           # 输出侧（回复未写回时才补）
    total_tok = prompt_tok + comp_tok                                 # 总占用（≈ total_tokens）
    limit = CONTEXT_LIMIT or 0
    pct = round(total_tok / limit * 100, 1) if limit else 0.0
    bpt = (cur_bytes / prompt_tok) if prompt_tok else 0.0
    approx = "" if _last_usage else "≈"
    segs = [f"上下文 {approx}{total_tok:,} tok / {limit:,}（{pct}%）"]
    if comp_tok:
        seg = f"输入 {prompt_tok:,} + 输出 {comp_tok:,}"
        cached = _last_usage.get("cached_tokens") or 0
        if cached:
            seg += f"（缓存命中 {cached:,}）"
        segs.append(seg)
    # 字节数按「完整请求体」给，才和上面的 token 数同口径；并拆出工具 schema 的占比，
    # 免得再被误读成「字节数怎么比 token 还少」
    if tools_bytes:
        breakdown = f"，消息 {msg_bytes/1024:.1f} KB + 工具 {tools_bytes/1024:.1f} KB"
    else:
        breakdown = ""
    segs.append(f"请求体 {cur_bytes:,} B（{cur_bytes/1024:.1f} KB{breakdown}）")
    segs.append(f"约 {bpt:.2f} B/tok")
    return "📊 " + " ｜ ".join(segs)

def _fmt_tok(n):
    """把 token 数压成短格式（1_000_000 → 1M，700_000 → 700k），状态栏用。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:g}M"
    if n >= 1_000:
        return f"{n / 1_000:g}k"
    return str(n)


def format_usage_short():
    """状态栏用的精简用量：上下文总占用（输入+输出）tok / 上限（百分比）｜输入 / 输出。

    口径与 API 对齐：窗口占用 = prompt_tokens + completion_tokens（= total_tokens）。

    调用时机同 format_usage_line()：本函数在「每次 API 返回后、assistant 回复尚未写回
    messages 之前」执行，此时 messages 正好等于刚发出的 prompt，因此
        prompt_tok（输入估算）≈ 刚发出的 prompt_tokens
        prompt_tok + completion_tokens = 刚那次调用的 total_tokens
    回复还没写回 messages，所以加输出不会重复计入。

    跟 format_usage_line() 一样会做一次请求体序列化来估算 token，代价不低，
    所以只在每次 API 返回后算一次并缓存进 _agent_status，状态栏渲染时直接读缓存。
    """
    msg_bytes = get_context_size(messages)
    tools_bytes = get_tools_size()
    cur_bytes = get_request_size(messages)
    prompt_tok = _tokens_from_bytes(cur_bytes)                        # 输入侧
    comp_tok = _pending_completion_tokens()                           # 输出侧（回复未写回时才补）
    total_tok = prompt_tok + comp_tok                                 # 总占用（≈ total_tokens）
    limit = CONTEXT_LIMIT or 0
    pct = round(total_tok / limit * 100, 1) if limit else 0.0
    approx = "" if _last_usage else "≈"
    segs = [f"上下文 {approx}{total_tok:,} / {_fmt_tok(limit)} tok ({pct}%)"]
    if comp_tok:
        segs.append(f"输入 {prompt_tok:,} + 输出 {comp_tok:,}")
    if tools_bytes:
        segs.append(f"请求体 {(msg_bytes + tools_bytes) / 1024:.1f} KB")
    return " ｜ ".join(segs)


def print_usage():
    """展示 token / 字节用量。

    · 普通模式：像以前一样，在大模型回复后追加打印一行完整用量
    · 常驻输入模式（TUI）：不再往输出区刷屏，改为更新输入框下方状态栏的一行精简用量
    """
    try:
        if UI_PROMPT_ACTIVE:
            _set_usage(format_usage_short())
            return
        print("   " + format_usage_line(), flush=True)
    except Exception:
        pass  # 用量展示失败绝不拖累主流程


# def get_context_aware_messages(msg_list):
#     """
#     如果上下文超过阈值，在末尾追加一条 user 提醒（不修改原列表）。
#     用 user 角色让 AI 更倾向执行压缩，而非 system 那样容易被忽略。
#     """
#     ctx_size = get_context_size(msg_list)
#     if ctx_size > COMPRESS_THRESHOLD:
#         pct = round(ctx_size / CONTEXT_LIMIT * 100, 1)
#         reminder = {
#             "role": "user",
#             "content": (
#                 f"[上下文使用率：{pct}%（{ctx_size//1000}K / {CONTEXT_LIMIT//1000}K），"
#                 "对话历史较长，建议考虑调用 compress 工具压缩历史对话以节省空间。"
#                 "压缩时请保留 system prompt 和最近 2-3 轮完整对话，"
#                 "更早的内容用一段摘要代替。]"
#             )
#         }
#         return msg_list + [reminder]
#     return msg_list


# ============================================================
#  日志记录 — 每次 API 返回后记录 messages 快照到单个文件
# ============================================================

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")
# 日志文件路径，在 main() 启动时初始化
_log_file = None
_err_log_file = None
# 用户通过 -c/--context-log 指定的上下文日志文件；若设置则优先写入该文件
_context_log_file = None


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
    """把当前 messages 追加到日志文件中。若设置了 -c/--context-log 则优先写入该文件"""
    target = _context_log_file if _context_log_file else _log_file
    if target is None:
        return
    try:
        data = json.dumps(msg_list, ensure_ascii=False, indent=2)
        with open(target, "a", encoding="utf-8") as f:
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
#  Loading 转圈动画 —— 仅在交互式终端下启用
#  非交互 / 管道 / 重定向模式（stdin 非 TTY）下退化为空操作，
#  避免污染管道输出与重定向结果。
# ============================================================

def _disp_width(text):
    """计算文本在终端中的近似显示列宽（全角/宽字符算 2 列）。"""
    try:
        import unicodedata
        width = 0
        for ch in text:
            width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        return width
    except Exception:
        return len(text)


class _Spinner:
    """一个轻量、线程安全的命令行转圈动画上下文管理器。

    用法::

        with spinner("✨ 魔理沙思考中"):
            resp = call_api(...)

    ``__enter__`` 时启动一个守护线程刷新转圈帧；
    ``__exit__`` 时停止动画并清除所在行。
    仅当 ``sys.stdin`` 与 ``sys.stderr`` 均为 TTY（交互式终端）时启用。
    """
    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, message="✨ 魔理沙思考中"):
        self.message = message
        self._thread = None
        self._stop = threading.Event()
        self._stream = sys.stderr

    def __enter__(self):
        # 常驻输入模式（patch_stdout）下禁用转圈动画：
        # 转圈写的是 stderr 上的 '\r'，会把 prompt_toolkit 画好的输入框撕掉
        if UI_PROMPT_ACTIVE:
            return self
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            self._stream.write(f"\r{self.message} {frame}")
            self._stream.flush()
            i += 1
            self._stop.wait(0.08)

    def __exit__(self, exc_type, exc, tb):
        if self._thread is not None:
            self._stop.set()
            try:
                self._thread.join(timeout=0.5)
            except KeyboardInterrupt:
                # 转圈结束瞬间被 Ctrl+C 打断也无妨——忽略即可，继续清行收尾。
                pass
            # 用空格覆盖转圈所在行，再回到行首。不使用 ANSI 转义序列，
            # 避免在不支持转义的终端上把 \x1b 显示成乱码（如 ?[2K）。
            width = _disp_width(self.message) + 2
            self._stream.write("\r" + " " * width + "\r")
            self._stream.flush()
            self._thread = None
        return False


def spinner(message="✨ 魔理沙思考中"):
    """返回一个转圈上下文管理器（见 :class:`_Spinner`）。"""
    return _Spinner(message)


# ============================================================
#  1. 调用 DeepSeek Chat API（纯标准库，不依赖 openai）
# ============================================================
def call_api(messages, tools=None, tool_choice="auto", config_override=None, guard_multimodal=True):
    """
    根据配置中的 protocol 字段，调用 OpenAI 风格或 Anthropic 风格的 API。
    直接构造 HTTP 请求（从 ai_agent_config.json 读取参数）。

    config_override: 可选。传入一个标准结构的配置 dict
        （含 api_key / base_url / model / protocol 键），
        用于调用独立配置的辅助模型（如多模态读图子 Agent）。
        为 None 时使用主配置（ai_agent_config.json）。

    guard_multimodal: 可选，默认 True。是否启用"多模态防护"：
        True  → 主模型调用：HTTP 400 且上下文含多模态时自动剥离并重试，并记录
                _main_model_no_multimodal 标记；
        False → 子 Agent（辅助模型）调用：HTTP 400 直接抛异常交给上层处理，
                不做剥离、不触碰全局标记，避免辅助模型的失败污染主模型判定。

    返回值：(msg, reasoning_content, usage)
        usage 为归一化的 token 用量 dict：
        {"prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"}；
        接口未返回 usage 时为 None。
    """
    if config_override is not None:
        config = config_override
    else:
        config = load_config()
    api_key = config.get("api_key")
    if not api_key:
        raise ValueError("API 密钥未配置！请检查 ai_agent_config.json 文件。")

    protocol = config.get("protocol", "openai").lower()

    try:
        if protocol == "anthropic":
            msg, reasoning_content, usage = _call_anthropic_api(config, messages, tools, allow_retry=True, guard_multimodal=guard_multimodal)
        else:
            msg, reasoning_content, usage = _call_openai_api(config, messages, tools, tool_choice, guard_multimodal=guard_multimodal)
        # 仅主模型调用（config_override 为空）才记录用量，供上下文统计与阈值判断使用；
        # 子 Agent / 辅助模型调用不会污染主上下文的口径。
        if config_override is None:
            _record_main_usage(usage, messages, tools)
        return msg, reasoning_content, usage
    except UserInterrupt:
        # 用户按 Ctrl+C 中断，不保存错误快照，直接向上传播（主循环检测 interrupted）
        raise
    except Exception as e:
        # 出错时保存 err 日志
        save_error_snapshot(messages, str(e))
        raise


def _has_multimodal_content(msgs):
    """检查 messages 列表中是否包含多模态内容（content 为 list 且含 image_url 等非 text 类型的 block）。
    
    用于判断 API 400 报错是否可能是因为模型不支持多模态导致的。
    不依赖 API 返回的错误信息格式，直接检查我们发送的数据结构。
    """
    for msg in msgs:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") != "text":
                    return True
    return False


def _strip_multimodal(msgs):
    """
    直接原地修改 messages 列表，删除多模态结构的数据。
    
    如果消息的 role 为 "user" 且 content 为数组格式（多模态），
    则将该消息整条删除。
    这是因为在不支持多模态的模型场景下，留着这些 user 消息
    会破坏 assistant(tool_calls) 跟 tool 响应的配对关系，
    导致报错 "An assistant message with 'tool_calls' must be followed by tool messages..."。
    
    其他角色（assistant、tool、system）的消息不动，
    大模型自己会处理残留的 tool_calls 和 tool response。
    
    返回是否做了修改（True=有改动）。
    """
    modified = False
    # 从后往前遍历，这样删除不会影响前面的索引
    i = len(msgs) - 1
    while i >= 0:
        msg = msgs[i]
        # 只删除 role 为 "user" 且 content 为 list（多模态格式）的消息
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            msgs.pop(i)
            modified = True
        i -= 1
    return modified


def _as_int(v):
    """尽量把 usage 字段转成 int，异常值一律当 0。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _extract_openai_usage(u):
    """把 OpenAI / DeepSeek 风格的 usage 归一化为统一结构（无则返回 None）。

    OpenAI:  {"prompt_tokens", "completion_tokens", "total_tokens",
              "prompt_tokens_details": {"cached_tokens"}}
    DeepSeek:{"prompt_tokens", "completion_tokens", "total_tokens",
              "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
    """
    if not isinstance(u, dict):
        return None
    prompt = u.get("prompt_tokens")
    completion = u.get("completion_tokens")
    total = u.get("total_tokens")
    if prompt is None and completion is None and total is None:
        return None
    prompt = _as_int(prompt)
    completion = _as_int(completion)
    total = _as_int(total) if total is not None else prompt + completion
    cached = u.get("prompt_cache_hit_tokens")
    if cached is None:
        details = u.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = details.get("cached_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": _as_int(cached),
    }


def _extract_anthropic_usage(u):
    """把 Anthropic 风格的 usage 归一化为统一结构（无则返回 None）。

    Anthropic: {"input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens"}
    """
    if not isinstance(u, dict):
        return None
    prompt = u.get("input_tokens")
    completion = u.get("output_tokens")
    if prompt is None and completion is None:
        return None
    prompt = _as_int(prompt)
    completion = _as_int(completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cached_tokens": _as_int(u.get("cache_read_input_tokens")),
    }


# ============================================================
#  请求直通：把 provider 相关的高级参数原样透传给 API
#  各家「思考等级」「推理预算」等参数名不统一，且都在请求体里，
#  这里只负责「合并 + 保护结构性字段」，不解释具体参数含义。
# ============================================================

# 请求体里由程序自己构造的字段，不允许被 extra_body 覆盖（覆盖会破坏请求结构）
_EXTRA_BODY_RESERVED = {"messages", "tools", "tool_choice", "stream", "model"}
# 请求头里由 urllib / 程序管理的字段，配置了也不生效
_EXTRA_HEADER_RESERVED = {"content-length", "content-type"}
# 同一类警告只提示一次，避免工具循环里每轮都刷屏
_warned_extra_keys = set()


def _sanitize_extra_fields(raw, reserved):
    """把配置里的 extra_body / extra_headers 归一化成干净的 {str: 值} 字典。

    - 非 dict（None / 字符串 / 列表 / 数字）一律当作「未配置」，返回 {}
    - 键统一转成字符串（JSON 里键本来就是字符串，这里防手改出的意外）
    - 剔除 reserved 里由程序管理的字段，避免用户配置破坏请求结构
    """
    if not isinstance(raw, dict):
        return {}
    clean = {}
    dropped = []
    for key, value in raw.items():
        name = str(key)
        if name.lower() in reserved:
            dropped.append(name)
            continue
        clean[name] = value
    if dropped:
        signature = (frozenset(reserved), tuple(sorted(dropped)))
        if signature not in _warned_extra_keys:
            _warned_extra_keys.add(signature)
            print(f"   ⚠️ 以下字段由程序管理，已忽略: {', '.join(sorted(dropped))}", flush=True)
    return clean


def _build_extra_headers(config):
    """取出生效的附加请求头（值统一转成字符串，避免 urllib 报类型错）。"""
    extra = _sanitize_extra_fields(config.get("extra_headers"), _EXTRA_HEADER_RESERVED)
    return {k: str(v) for k, v in extra.items()}


def _build_extra_body(config):
    """取出生效的附加请求体字段。"""
    return _sanitize_extra_fields(config.get("extra_body"), _EXTRA_BODY_RESERVED)


def _report_api_extras():
    """启动时打印当前生效的附加参数，方便一眼确认「思考等级」这类设置有没有吃上。"""
    try:
        cfg = load_config()
    except Exception:
        return
    rows = []
    body = _build_extra_body(cfg)
    if body:
        rows.append("请求体 " + json.dumps(body, ensure_ascii=False))
    headers = _build_extra_headers(cfg)
    if headers:
        rows.append("请求头 " + json.dumps(headers, ensure_ascii=False))
    mul_body = _sanitize_extra_fields(cfg.get("mul_extra_body"), _EXTRA_BODY_RESERVED)
    mul_headers = _sanitize_extra_fields(cfg.get("mul_extra_headers"), _EXTRA_HEADER_RESERVED)
    if mul_body or mul_headers:
        rows.append(
            "辅助模型 "
            + json.dumps({"body": mul_body, "headers": mul_headers}, ensure_ascii=False)
        )
    if rows:
        print("   ⚙️ 附加请求参数: " + " ｜ ".join(rows), flush=True)


def _call_openai_api(config, messages, tools=None, tool_choice="auto", guard_multimodal=True):
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
    # 附加请求头（配置里 extra_headers 原样透传，用于 beta 开关 / 额外鉴权头等）
    extra_headers = _build_extra_headers(config)
    if extra_headers:
        headers.update(extra_headers)
    # 附加请求体字段（配置里 extra_body 原样透传，用于 reasoning_effort / thinking 等）
    extra_body = _build_extra_body(config)

    return _do_openai_request(
        url, headers, model, messages, tools, tool_choice,
        guard_multimodal=guard_multimodal, extra_body=extra_body,
    )


def _do_openai_request(url, headers, model, messages, tools=None, tool_choice="auto", allow_retry=True, guard_multimodal=True, extra_body=None):
    """
    实际执行 OpenAI 风格 API 请求。
    
    如果 HTTP 400 报错且 messages 中包含多模态内容（模型可能不支持多模态），
    自动将全局 messages 中的多模态数据删除，然后重试一次，并记录
    _main_model_no_multimodal 标记（仅 guard_multimodal=True 时，即主模型调用）。

    guard_multimodal=False 时（子 Agent / 辅助模型调用）：HTTP 400 不做剥离重试，
    直接抛异常交给上层处理，避免辅助模型的失败污染主模型的多模态判定标记。
    空返回的检测与处理由主循环负责，不在本函数内。
    """
    global _main_model_no_multimodal
    payload = {
        "model": model,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    # 附加请求体字段最后合并（结构性字段已在 _build_extra_body 里被剔除）
    if extra_body:
        payload.update(extra_body)

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    r = _urlopen_interruptible(req, timeout=180)
    if not r["ok"]:
        if r["kind"] == "http":
            error_body = r["body"]
            code = r["code"]
            # 🗑️ 检测到 400 错误且 messages 中包含多模态内容，删除并重试
            # 不依赖错误信息中的关键字，直接检查我们发送的数据结构
            # 仅主模型调用（guard_multimodal=True）执行此防护，子 Agent 直接抛异常
            if allow_retry and guard_multimodal and code == 400 and _has_multimodal_content(messages):
                print(
                    "   🗑️ HTTP 400 且上下文含多模态数据，自动清理后重试...",
                    flush=True
                )
                _strip_multimodal(messages)  # 直接修改全局 messages，一劳永逸
                _main_model_no_multimodal = True  # 记住：主模型不支持多模态
                return _do_openai_request(
                    url, headers, model, messages, tools, tool_choice,
                    allow_retry=False,  # 防止无限递归
                    guard_multimodal=guard_multimodal,
                    extra_body=extra_body,
                )
            raise RuntimeError(f"API 请求失败 (HTTP {code}): {error_body}")
        elif r["kind"] == "url":
            if _should_retry_timeout():
                retry_left = _api_timeout_retry_remaining
                print(
                    f"   ⏳ API 超时/网络错误，等待 {API_TIMEOUT_RETRY_WAIT}s 后自动重试"
                    f"（剩余 {retry_left} 次）...",
                    flush=True,
                )
                _interruptible_sleep(API_TIMEOUT_RETRY_WAIT)
                return _do_openai_request(
                    url, headers, model, messages, tools, tool_choice,
                    allow_retry=allow_retry,
                    guard_multimodal=guard_multimodal,
                    extra_body=extra_body,
                )
            raise RuntimeError(f"API 请求失败 (网络错误): {r['reason']}")
        else:
            raise RuntimeError(f"API 请求失败 (网络错误): {r['reason']}")
    result = r["result"]

    # 提取消息
    choice = result["choices"][0]
    msg = choice["message"]

    # 提取 reasoning_content（如果存在）
    reasoning_content = msg.get("reasoning_content")

    # 提取 token 用量（OpenAI / DeepSeek 等）
    usage = _extract_openai_usage(result.get("usage"))

    return msg, reasoning_content, usage


def _call_anthropic_api(config, messages, tools=None, allow_retry=True, guard_multimodal=True):
    """
    Anthropic 风格 API 调用。
    将内部 OpenAI 格式的 messages 转换为 Anthropic 格式，
    并处理响应中的 tool_use content blocks。

    如果 HTTP 400 报错且原始 messages 中包含多模态内容，
    会自动清理全局 messages 中的多模态 user 消息并重试一次，并记录
    _main_model_no_multimodal 标记（仅 guard_multimodal=True 时，即主模型调用）。

    guard_multimodal=False 时（子 Agent / 辅助模型调用）：HTTP 400 不做剥离重试，
    直接抛异常交给上层处理，避免辅助模型的失败污染主模型的多模态判定标记。
    空返回的检测与处理由主循环负责，不在本函数内。
    """
    global _main_model_no_multimodal
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
                # 将 OpenAI 多模态格式（image_url）转换为 Anthropic 格式（image）
                converted_blocks = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        # 从 data URL 中提取 base64 和 mime 类型
                        image_url = block.get("image_url", {}).get("url", "")
                        if image_url.startswith("data:"):
                            # 格式: data:image/png;base64,xxxx
                            try:
                                header, b64_data = image_url.split(",", 1)
                                media_type = header.split(":")[1].split(";")[0]
                                converted_blocks.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": b64_data
                                    }
                                })
                            except (ValueError, IndexError):
                                # 转换失败，保留原样
                                converted_blocks.append(block)
                        elif image_url.startswith(("http://", "https://")):
                            # http(s) URL：转换为 Anthropic 的 url 源，由其服务端获取（免下载）
                            converted_blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "url",
                                    "url": image_url
                                }
                            })
                        else:
                            # 其他未知形式，保留原样
                            converted_blocks.append(block)
                    else:
                        converted_blocks.append(block)
                anthropic_messages.append({"role": "user", "content": converted_blocks})
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
                # if j > i + 1:
                #     print(f"   🔗 合并 {j - i} 条连续的 tool_result user 消息", flush=True)
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
                        # 🔍 特殊检查：IMAGE_TOOLS（如 read_image）的 tool_use 后面
                        # 跟着的是包含 image 类型 block 的 user 消息，而不是 tool_result
                        # 这是正常的流程，不应视为孤立 tool_use
                        is_image_tool_pair = False
                        tool_use_names_in_msg = [
                            b.get("name", "") for b in content_blocks
                            if isinstance(b, dict) and b.get("type") == "tool_use"
                        ]
                        if tool_use_names_in_msg and all(n in IMAGE_TOOLS for n in tool_use_names_in_msg):
                            if i + 1 < len(anthropic_messages):
                                next_msg = anthropic_messages[i + 1]
                                if next_msg["role"] == "user":
                                    next_content = next_msg.get("content", "")
                                    if isinstance(next_content, list):
                                        has_image_block = any(
                                            isinstance(b, dict) and b.get("type") == "image"
                                            for b in next_content
                                        )
                                        if has_image_block:
                                            is_image_tool_pair = True
                        
                        if not is_image_tool_pair:
                            # ⚡ 真正的孤立 tool_use！跳过这个 assistant 消息
                            # 同时也跳过它之后可能跟着的 user 文本消息（如果有的话）
                            # print("   ⚠️ 检测到孤立的 tool_use，已自动跳过修复", flush=True)
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
        # 🔍 从孤立集合中排除 IMAGE_TOOLS（如 read_image）
        # 这些工具的 tool_use 后面跟的是包含 image 的 user 消息，而非 tool_result
        # 收集所有 IMAGE_TOOLS 的 tool_use id
        image_tool_ids = set()
        for msg in anthropic_messages:
            if msg["role"] == "assistant":
                content_blocks = msg.get("content", "")
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            if block.get("id", "") in orphan_tool_use_ids and block.get("name", "") in IMAGE_TOOLS:
                                image_tool_ids.add(block.get("id", ""))
        
        real_orphan_ids = orphan_tool_use_ids - image_tool_ids
        if image_tool_ids:
            print(f"   ℹ️ 排除 {len(image_tool_ids)} 个 IMAGE_TOOLS 的 tool_use（它们后面跟着图片消息）", flush=True)
        
        if real_orphan_ids:
            print(f"   ⚠️ 发现 {len(real_orphan_ids)} 个孤立 tool_use，正在修复...", flush=True)
            # 从 assistant 消息中移除孤立的 tool_use block
            fixed_msgs = []
            for msg in anthropic_messages:
                if msg["role"] == "assistant":
                    content_blocks = msg.get("content", "")
                    if isinstance(content_blocks, list):
                        new_blocks = []
                        for block in content_blocks:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                if block.get("id", "") in real_orphan_ids:
                                    print(f"     移除孤立 tool_use: {block.get('id', '')[:20]}... ({block.get('name', '')})", flush=True)
                                    continue  # 跳过这个孤立的 tool_use
                            new_blocks.append(block)
                        # 如果所有 block 都被移除了，就完全跳过这个 assistant 消息
                        if not new_blocks:
                            continue
                        msg["content"] = new_blocks
                fixed_msgs.append(msg)
            anthropic_messages = fixed_msgs
        else:
            print(f"   ℹ️ 所有 {len(orphan_tool_use_ids)} 个疑似孤立的 tool_use 均属于 IMAGE_TOOLS，已保留", flush=True)
    
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
    # 附加请求头：Anthropic 的 beta 功能开关走这里（如 interleaved-thinking）
    extra_headers = _build_extra_headers(config)
    if extra_headers:
        headers.update(extra_headers)

    payload = {
        "model": model,
        "max_tokens": 8192,
        "messages": anthropic_messages,
    }
    if system_prompt:
        payload["system"] = system_prompt
    if anthropic_tools:
        payload["tools"] = anthropic_tools
    # 附加请求体字段最后合并：扩展思考（thinking）、max_tokens 上限等都在这里给。
    # 注意 Anthropic 要求 max_tokens > thinking.budget_tokens，开了思考要一并调大。
    extra_body = _build_extra_body(config)
    if extra_body:
        payload.update(extra_body)

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    r = _urlopen_interruptible(req, timeout=180)
    if not r["ok"]:
        if r["kind"] == "http":
            error_body = r["body"]
            code = r["code"]
            # 🗑️ 检测到 400 错误且原始 messages 中包含多模态内容，清理并重试
            if allow_retry and guard_multimodal and code == 400 and _has_multimodal_content(messages):
                print(
                    "   🗑️ Anthropic HTTP 400 且上下文含多模态数据，自动清理后重试...",
                    flush=True
                )
                _strip_multimodal(messages)  # 直接修改全局 messages，一劳永逸
                _main_model_no_multimodal = True  # 记住：主模型不支持多模态
                return _call_anthropic_api(
                    config, messages, tools,
                    allow_retry=False,  # 防止无限递归
                    guard_multimodal=guard_multimodal
                )
            raise RuntimeError(f"Anthropic API 请求失败 (HTTP {code}): {error_body}")
        elif r["kind"] == "url":
            if _should_retry_timeout():
                retry_left = _api_timeout_retry_remaining
                print(
                    f"   ⏳ Anthropic API 超时/网络错误，等待 {API_TIMEOUT_RETRY_WAIT}s 后自动重试"
                    f"（剩余 {retry_left} 次）...",
                    flush=True,
                )
                _interruptible_sleep(API_TIMEOUT_RETRY_WAIT)
                return _call_anthropic_api(
                    config, messages, tools,
                    allow_retry=allow_retry,
                    guard_multimodal=guard_multimodal,
                )
            raise RuntimeError(f"Anthropic API 请求失败 (网络错误): {r['reason']}")
        else:
            raise RuntimeError(f"Anthropic API 请求失败 (网络错误): {r['reason']}")
    result = r["result"]

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

    # 提取 token 用量（Anthropic：input_tokens / output_tokens）
    usage = _extract_anthropic_usage(result.get("usage"))

    return msg, reasoning_content, usage

# ============================================================
#  2. 工具定义 & 执行
#  已拆分到 ai_agent_tools.py：
#    - 工具 schema 列表 tools
#    - 各工具函数实现（run_bash / read_image / 文件读写 / 后台任务 / skills / MCP ...）
#    - 工具路由表 tool_func_map 与 MCP 工具动态注册
# ============================================================

# ============================================================
#  从日志文件恢复会话 —— 支持 -r / --resume 参数
# ============================================================

def _resume_from_log(log_path):
    """Restore the last messages snapshot from a log file."""
    if not os.path.isfile(log_path):
        return None, "file not found: " + log_path

    try:
        with open(log_path, "rb") as f:
            raw_data = f.read()
        content = smart_decode(raw_data)
    except Exception as e:
        return None, "read failed: " + str(e)

    # match the last "--- snapshot ... ---" marker
    # use \r? to handle both Windows (\r\n) and Unix (\n) line endings
    matches = list(re.finditer(r'^--- snapshot .* ---\r?$', content, re.MULTILINE))
    if not matches:
        return None, "no snapshot marker found in: " + log_path

    last_match = matches[-1]
    json_text = content[last_match.end():].strip()

    if not json_text:
        return None, "empty content after last snapshot"

    try:
        messages = json.loads(json_text)
    except json.JSONDecodeError as e:
        return None, "JSON parse failed: " + str(e)

    if not isinstance(messages, list):
        return None, "messages is not a list: " + str(type(messages))

    if len(messages) == 0 or messages[0].get("role") != "system":
        return None, "first message is not system prompt, cannot resume"

    return messages, None


# ============================================================
#  已加载上下文的终端回放 —— 供 -r / -c 恢复会话后回顾
# ============================================================

# 回放时单条消息最多显示的字符数（超出截断，避免一加载长会话就刷屏）
CONTEXT_REPLAY_MAX_CHARS = 1200
# 回放时单条工具结果最多显示的字符数（工具输出通常很长，截得更狠）
CONTEXT_REPLAY_TOOL_MAX_CHARS = 200


def _content_to_text(content):
    """把消息 content 归一成可显示的文本。

    - str            → 原样返回
    - list（多模态）→ 拼接各 text 段，并统计图片数量
    - None / 其他    → 空串 / 字符串
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        n_img = 0
        for item in content:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "text":
                texts.append(item.get("text", ""))
            elif itype in ("image_url", "image", "input_image"):
                n_img += 1
        out = "\n".join(t for t in texts if t)
        if n_img:
            out += (f"\n[{n_img} 张图片]" if out else f"[{n_img} 张图片]")
        return out
    if content is None:
        return ""
    return str(content)


def _clip_text(text, limit):
    """按字符数截断过长文本，并在末尾附上省略提示。"""
    text = (text or "").rstrip()
    if limit and len(text) > limit:
        return text[:limit] + f"\n…（已省略 {len(text) - limit} 字，共 {len(text)} 字）"
    return text


def _indent_text(text, prefix="    "):
    """给多行文本统一加缩进前缀。"""
    return prefix + (text or "").replace("\n", "\n" + prefix)


def _replay_loaded_context(msg_list, source=""):
    """把 -r / -c 恢复出来的历史上下文回放到终端。

    只回放对话部分（跳过 system 提示词）；单条内容过长时截断，
    避免加载一个长会话时把屏幕刷爆。
    """
    try:
        if not msg_list:
            return
        body = [
            m for m in msg_list
            if isinstance(m, dict) and m.get("role") != "system"
        ]
        n_sys = len(msg_list) - len(body)

        title = "已加载的会话上下文"
        if source:
            title += f" · {source}"
        print(f"\n{'━' * 3} {title} {'━' * 3}", flush=True)
        tail = f"（已跳过 {n_sys} 条 system 提示词）" if n_sys else ""
        print(f"   共 {len(body)} 条对话消息{tail}", flush=True)

        if not body:
            print("   （没有可回放的对话内容）", flush=True)
            print(f"{'━' * 3} 回放结束 {'━' * 3}\n", flush=True)
            return

        for m in body:
            role = m.get("role")
            if role == "user":
                text = _content_to_text(m.get("content"))
                print("\n👤 我:", flush=True)
                print(_indent_text(_clip_text(text, CONTEXT_REPLAY_MAX_CHARS)), flush=True)
            elif role == "assistant":
                text = _content_to_text(m.get("content"))
                if text.strip():
                    print(flush=True)
                    print_assistant(_clip_text(text, CONTEXT_REPLAY_MAX_CHARS))
                for tc in (m.get("tool_calls") or []):
                    fn = ""
                    if isinstance(tc, dict):
                        fn = (
                            (tc.get("function") or {}).get("name")
                            or tc.get("name")
                            or ""
                        )
                    print(f"   🔧 调用工具：{fn or '?'}", flush=True)
            elif role == "tool":
                name = m.get("name") or "tool"
                text = _clip_text(
                    _content_to_text(m.get("content")),
                    CONTEXT_REPLAY_TOOL_MAX_CHARS,
                )
                if not text:
                    print(f"   ↳ 🔧 {name}：（空结果）", flush=True)
                    continue
                first_line, *rest = text.split("\n")
                print(f"   ↳ 🔧 {name}：{first_line}", flush=True)
                for ln in rest:
                    print(f"        {ln}", flush=True)

        print(f"\n{'━' * 3} 回放结束 {'━' * 3}\n", flush=True)
    except Exception:
        # 回放只是辅助展示，任何异常都不该影响正常启动
        pass


# ============================================================
#  大内容自动过期机制 —— 在外层 while 循环每次开始时调用
# ============================================================

def _expire_large_content():
    """扫描 messages 中所有 role=tool 的大内容以及 role=user 的多模态消息，进行计数和过期处理。

    每次外层 while 循环开始以及内层工具循环每次迭代时调用。逻辑：
    1. 遍历 messages 中所有 role=tool 的消息，检查 content 长度，超过阈值则计数，达到轮数则过期
    2. 遍历 messages 中所有 role=user 且 content 为 list（多模态）的消息，同样计数和过期
    3. 已过期的消息不参与后续计数

    注意：此函数不依赖工具函数内部的任何标记（如 is_large），
    完全基于 content 的实际大小来判断。
    """
    global large_content_counter, messages

    if not messages:
        return

    import hashlib
    expired_count = 0
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        # --- 判断是否为"大内容"，提取 key 和 content_size ---
        key = None
        content_size = 0
        tool_call_id = ""

        if role == "tool":
            if not isinstance(content, str):
                continue
            tool_call_id = msg.get("tool_call_id", "")
            if not tool_call_id:
                continue
            if content.startswith("[数据已过期"):
                continue
            if len(content) <= LARGE_CONTENT_THRESHOLD:
                continue
            key = f"tool:{tool_call_id}"
            content_size = len(content)

        elif role == "user" and isinstance(content, list):
            # 多模态 user 消息：content 是 list 结构
            content_str = json.dumps(content, ensure_ascii=False, sort_keys=True)
            if len(content_str) <= LARGE_CONTENT_THRESHOLD:
                continue
            content_hash = hashlib.md5(content_str.encode()).hexdigest()[:16]
            key = f"user_mm:{content_hash}"
            content_size = len(content_str)

        if key is None:
            continue

        # --- 计数 ---
        count = large_content_counter.get(key, 0) + 1
        large_content_counter[key] = count

        # --- 达到过期轮数则替换 ---
        if count >= LARGE_CONTENT_EXPIRE_ROUNDS:
            old_size = content_size

            if role == "tool":
                # 工具消息：尝试提取文件名做参考
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
                display_id = tool_call_id[:20]
                tag = "tool_call"
            else:
                # 多模态 user 消息：替换为简短过期文本
                msg["content"] = f"[多模态数据已过期：原始大小 {old_size//1024}KB，已在对话中存在 {count} 轮]"
                display_id = content_hash
                tag = "多模态"

            large_content_counter.pop(key, None)
            expired_count += 1
            print(f"   ⏰ 大内容过期: {tag}_id={display_id}... (原大小 {old_size//1024}KB, 已存在 {count} 轮)", flush=True)

    if expired_count > 0:
        print(f"   🧹 本次过期了 {expired_count} 条大内容，当前 messages 大小: {get_context_size(messages)//1024}KB", flush=True)


# ============================================================
#  无人值守自动压缩 —— 在内层工具循环中压缩历史工具调用
# ============================================================

def _squash_tool_calls_automatically(msg_list):
    """在无人值守时，自动压缩 messages 中中间的工具调用轮次。

    规则：
    - 保留 system prompt（第0条）
    - 保留所有非多模态的 user 消息（纯文本 user 保留，多模态 user 也删掉）
    - 保留最近 KEEP_RECENT_ROUNDS 轮完整的 assistant(tool_calls) + tool 配对
    - 删除中间轮次的 assistant(有tool_calls) 及其后面紧跟着的 tool 消息
    - 其他消息（如无 tool_calls 的 assistant 总结、system 等）不动

    返回值: bool — 是否真的做了压缩
    """
    KEEP_RECENT_ROUNDS = 20  # 保留最近几轮完整的工具调用

    if len(msg_list) < 3:
        return False

    # 找出所有要删除的索引
    # 收集所有 assistant(有tool_calls) 的索引
    assistant_tool_idxs = []
    for i, msg in enumerate(msg_list):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant_tool_idxs.append(i)

    if len(assistant_tool_idxs) <= KEEP_RECENT_ROUNDS:
        return False  # 不够删的

    # 决定保留最近 KEEP_RECENT_ROUNDS 轮
    keep_indices = set()

    # 保留 system prompt（第0条）
    keep_indices.add(0)

    # 保留所有非多模态的 user 消息（纯文本 user 消息保留，多模态的也删掉）
    for i, msg in enumerate(msg_list):
        if msg.get("role") == "user":
            content = msg.get("content")
            # 多模态消息的 content 是 list（如图片+文本），纯文本消息的 content 是 str
            if isinstance(content, str):
                keep_indices.add(i)

    # 保留最近 KEEP_RECENT_ROUNDS 轮的 assistant(tool_calls) + 对应的 tool 消息
    recent_assistant_idxs = assistant_tool_idxs[-KEEP_RECENT_ROUNDS:]
    for idx in recent_assistant_idxs:
        keep_indices.add(idx)
        # 从 idx 往后找 tool 消息，直到遇到下一条非 tool 消息
        j = idx + 1
        while j < len(msg_list):
            if msg_list[j].get("role") == "tool":
                keep_indices.add(j)
                j += 1
            else:
                break

    # 所有不在 keep_indices 中的、且满足删除条件的条目：
    # assistant(有tool_calls) 和其紧跟着的 tool 消息
    to_delete = set()
    for idx in assistant_tool_idxs:
        if idx in keep_indices:
            continue  # 保留轮次，不删
        # 要删除这个 assistant
        to_delete.add(idx)
        # 以及后面紧跟着的 tool 消息（直到遇到非 tool）
        j = idx + 1
        while j < len(msg_list):
            if msg_list[j].get("role") == "tool":
                to_delete.add(j)
                j += 1
            else:
                break

    # 🐛 修复：删除不在 keep_indices 中的多模态 user 消息（图片 base64 数据）
    # 多模态 user 消息的 content 为 list（如图片+文本），纯文本 user 的 content 为 str。
    # 之前只把 str 类型的纯文本 user 加入了 keep_indices，而多模态 user 既不在
    # keep_indices 中，也不在 assistant/tool 的删除逻辑里，导致它们无限累积占用上下文。
    for i, msg in enumerate(msg_list):
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            if i not in keep_indices:
                to_delete.add(i)

    if not to_delete:
        return False

    # 从后往前删除，避免索引变化
    old_size = get_context_size(msg_list)
    old_len = len(msg_list)
    for idx in sorted(to_delete, reverse=True):
        del msg_list[idx]

    new_size = get_context_size(msg_list)
    new_len = len(msg_list)
    print(
        f"\n📦 无人值守自动压缩: {old_len}条→{new_len}条, "
        f"{old_size//1024}KB→{new_size//1024}KB "
        f"(保留最近 {KEEP_RECENT_ROUNDS} 轮工具调用)\n",
        flush=True
    )
    return True
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
#  兜底清理：孤立 tool_calls 的 assistant 消息
# ============================================================

def _prune_incomplete_tool_calls(msg_list):
    """兜底防御：修复 API 报 400 的根因
    ("An assistant message with 'tool_calls' must be followed by tool messages
     responding to each 'tool_call_id'")。

    场景：多轮工具循环中，如果某条 assistant 消息包含 tool_calls，但其部分/全部
    tool_call_id 在后面没有对应 role=tool 的消息去回应（例如工具名未知导致跳过、
    执行中断、异常分支遗漏等），那么直接把这条坏消息序列发给 API 就会报 400。

    策略：
      1. 扫描 msg_list，找到所有带 tool_calls 的 assistant 消息及其 tool_call_ids。
      2. 收集这些 id 在后面出现过的 role=tool 消息的 tool_call_id 集合。
      3. 若某 assistant 的 tool_call_id 中有未被回应的，为每个缺失的 id 在其
         assistant 消息之后补一条 tool 错误响应，保持配对完整（相比删除更保守，
         保留 AI 的意图）。

    参数:
        msg_list: 要修复的 messages 列表（原地修改）。

    返回:
        int: 补了几条 tool 响应。
    """
    repaired = 0
    # 收集所有已出现的 tool_call_id（用于判断某 id 是否已被回应过）
    # 由于 tool 消息总是紧跟在其 assistant 之后，从前往后扫描时，只要
    # 尚未遇到某 assistant 的 tool 回应，且之后出现了非 tool 消息，即视为缺失。
    i = 0
    n = len(msg_list)
    while i < n:
        msg = msg_list[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tcs = msg.get("tool_calls") or []
            if not isinstance(tcs, list) or not tcs:
                i += 1
                continue
            needed_ids = [tc.get("id") for tc in tcs if tc.get("id")]
            replied_ids = set()
            j = i + 1
            # 向后扫描直到遇到下一条 assistant，收集这期间的所有 tool 消息 id
            while j < n:
                nxt = msg_list[j]
                if nxt.get("role") == "assistant":
                    break
                if nxt.get("role") == "tool" and nxt.get("tool_call_id"):
                    replied_ids.add(nxt.get("tool_call_id"))
                j += 1
            # 找出未回应的 id，在 assistant 之后补 tool 错误响应
            missing = [tid for tid in needed_ids if tid not in replied_ids]
            if missing:
                # 从后往前插入，保持顺序（直接顺序插入即可，因为插在 assistant 之后）
                known = ", ".join(sorted(tool_func_map.keys()))
                insert_pos = i + 1
                for tid in missing:
                    msg_list.insert(insert_pos, {
                        "role": "tool",
                        "tool_call_id": tid,
                        "name": "unknown_tool",
                        "content": json.dumps({
                            "success": 0,
                            "err": (f"[兜底修复] 工具调用尚未收到响应即需要继续，"
                                    f"视为未执行。若需该功能请改用可用工具：{known}。")
                        }),
                    })
                    insert_pos += 1
                    repaired += 1
                n += len(missing)  # 列表变长，同步更新长度
                i += len(missing)
        i += 1
    if repaired:
        print(f"\n🛠 兜底修复：补齐 {repaired} 条缺失的 tool 响应，避免 API 报 400\n", flush=True)
    return repaired


# ============================================================
#  3. 输入处理 —— prompt_toolkit 会话 / 退化 input 多行读取
# ============================================================
if HAS_PROMPT_TOOLKIT:

    class _BoundedInputSession(PromptSession):
        """限制多行输入可视化高度的 PromptSession 子类。

        默认 prompt_toolkit 的多行输入高度会随内容无限增长：当用户在终端里
        粘贴一大段文本时，输入区会向上扩展，把上方已有的历史输出（如 AI 回复
        的最后几行）覆盖掉。此子类把输入区高度限制在终端高度的一定比例内，
        超出内容在输入区内部滚动显示，从而不侵占上方屏幕。
        """

        def _get_default_buffer_control_height(self) -> "Dimension":
            # 先取父类高度（含补全菜单所需的预留空间）。
            base = super()._get_default_buffer_control_height()
            # 计算合理上限：终端行数的一部分，且保持在 [6, 16] 之间。
            try:
                rows = get_app().output.get_size().rows
                max_h = max(6, min(16, int(rows * 0.4)))
            except Exception:
                max_h = 10
            # 在父类基础上施加高度上限。
            if base.max is None or base.max > max_h:
                base = Dimension(min=base.min or 0, max=max_h)
            return base


def create_prompt_session():
    """创建一个支持多行输入的 PromptSession（需 prompt_toolkit 可用时调用）"""
    if not HAS_PROMPT_TOOLKIT:
        # 组件缺失时不应被调用，返回 None 让调用方走 input 分支
        return None

    # 自定义快捷键绑定
    bindings = KeyBindings()

    # 按 Ctrl+D 退出程序
    try:
        from prompt_toolkit.keys import Keys

        @bindings.add(Keys.ControlD)
        def exit_(event):
            """Ctrl+D 退出"""
            event.app.exit(result=None)  # 返回 None 表示退出

        # 也可以用 Ctrl+C 退出——不过默认 Ctrl+C 会引发 KeyboardInterrupt
    except ImportError:
        pass  # 快捷键增强（Keys）不可用时跳过，不影响基本多行输入

    session = _BoundedInputSession(
        multiline=True,          # 支持多行输入！
        history=InMemoryHistory(),
        key_bindings=bindings,
        prompt_continuation="    ",  # 续行缩进 4 空格，与首行 "我: "（显示宽度 4 列）对齐
    )

    session.app.paste_mode = lambda: True

    return session


def read_multiline_input(prompt):
    """退化版多行输入：纯 input() 逐行读取。

    多行消息终止规则（参考 SMTP 的 '.'）：
      - 用户输入的某一行为单个 '.' 时，视为整句话结束标记
      - 用户如需输入一行以 '.' 开头的内容，须以 '..' 开头（此处还原为单个 '.'）
      - 其余任意行原样拼入，不做转义

    Ctrl+C 会向上抛出 KeyboardInterrupt（由主循环统一处理），
    Ctrl+D / EOF 抛出 EOFError（同上）。
    """
    lines = []
    prompt_display = prompt
    while True:
        try:
            line = input(prompt_display)
        except EOFError:
            # 无内容时 EOF 视为取消/退出；已有内容时结束本段输入
            if not lines:
                raise
            break
        # 首行之后续行不再显示 "我: "，改显示空提示或续行符号
        prompt_display = ""
        if line == ".":
            # 单行 '.' = 结束标记
            break
        if line.startswith(".."):
            # '..' 开头还原为 '.'，其余保留
            line = line[1:]
        lines.append(line)
    return "\n".join(lines)


def _display_width(s):
    """计算字符串在终端里的显示宽度：东亚宽/全角字符算 2 列，组合字符算 0。"""
    import unicodedata

    width = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _input_may_scroll(text):
    """粗略判断这段输入在 prompt_toolkit 输入框里是否可能发生滚动。

    输入框高度上限见 _BoundedInputSession：终端行数的 40%，并 clamp 到 [6, 16]。
    只要行数超过该上限、或有单行发生软换行（显示宽度超过终端列宽），
    提交后就会丢掉上方被滚走的部分 —— 仅这种情况才需要补打完整内容。
    """
    import shutil

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    try:
        size = shutil.get_terminal_size(fallback=(80, 24))
        cols, rows = size.columns or 80, size.lines or 24
    except Exception:
        cols, rows = 80, 24
    max_h = max(6, min(16, int(rows * 0.4)))
    if len(lines) > max_h:
        return True
    if any(_display_width(ln) + 5 > cols for ln in lines):  # +5 ≈ "我: " 前缀占位
        return True
    return False


def _echo_user_input(text):
    """把用户刚提交的完整提示词补打到终端滚动历史。

    背景：prompt_toolkit 的多行输入框高度有上限（见 _BoundedInputSession），
    当提示词很长、在输入框里滚动过之后，按回车提交只会把「最后可视的那一屏」
    留在终端上，上方被滚掉的部分不会进入滚动历史，后续也就回看不到了。

    这里在提交之后（输入框已 accept 结束、patch_stdout 仍接管输出）重新把
    完整内容打印一遍，作为普通历史输出保留，方便回看/复制。仅在确实可能
    发生滚动时才补打，避免短输入被重复显示。
    """
    try:
        if text is None:
            return
        body = text.replace("\r\n", "\n").replace("\r", "\n")
        if not body.strip():
            # 纯空白提交会被 _submit 丢弃，不必回显
            return
        if not _input_may_scroll(body):
            return
        # 续行缩进 = 首行前缀的显示宽度，保证多行时每行左缘对齐
        plain_head = "👤 我: "
        body = body.replace("\n", "\n" + " " * _display_width(plain_head))
        if UI_ANSI_OK:
            head = "\x1b[1;36m👤 我:\x1b[0m "
        else:
            head = plain_head
        print(f"\n{head}{body}", flush=True)
    except Exception:
        # 补打只是辅助显示，任何异常都不该影响主输入循环
        pass


def split_pipe_messages(text):
    """将管道/重定向输入的内容按 SMTP '.' 协议切分成多条独立消息。

    规则（与交互式 input 模式一致）：
      - 某一行内容恰好为 '.'（去掉行尾换行符后）→ 当前消息至此结束，开启下一条
      - '..' 开头的行 → 还原为 '.' 开头的一个字符（用于在消息中转义单点开头）
      - 其余行原样加入当前消息
    输入兼容 '\\r\\n' 和 '\\n' 两种换行。

    例如输入："你好\\n.\\n今天天气怎么样\\n.\\n" → ['你好', '今天天气怎么样']
    """
    # 统一换行符为 '\n'
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    messages = []
    current = []
    for raw in lines:
        line = raw
        if line == '.':
            # 当前消息结束；结束标记本身不进入内容
            messages.append("\n".join(current))
            current = []
        elif line.startswith('..'):
            # 转义：'..' 开头还原为单个 '.'
            current.append(line[1:])
        else:
            current.append(line)
    # 若内容未以 '.' 结束（EOF 前没有结束标记），把残留也作为一条消息
    if current:
        messages.append("\n".join(current))
    # 去除完全空白的分段（结束标记之间无内容、或首尾多余空行等）
    return [m for m in messages if m.strip() != ""]


# ============================================================
#  4. 对话循环
# ============================================================
def main():
    global tool_executing, interrupted, messages
    global _main_model_no_multimodal, _api_timeout_retry_remaining

    # 注册信号处理器——只用于工具执行中的中断
    # 用户输入中的 Ctrl+C 由 prompt_toolkit 处理
    signal.signal(signal.SIGINT, sigint_handler)
    # 后台任务清理已在模块级注册 atexit，无需在此重复注册

    # ---- 命令行参数解析 ----
    parser = argparse.ArgumentParser(description='魔理沙 AI Agent - 兴趣使然的对话助手')
    # -r 与 -c 互斥：只能选其一
    ctx_group = parser.add_mutually_exclusive_group()
    ctx_group.add_argument('-r', '--resume', type=str, default=None,
                           help='从日志文件恢复会话（只读，不写回该文件），用法：-r /path/to/log/file.log')
    ctx_group.add_argument('-c', '--context-log', type=str, default=None,
                           help='指定上下文日志文件并持续写入：若文件不存在则创建并作为会话日志；'
                                '若文件已存在则先加载其中快照恢复会话，后续快照继续追加写入该文件。'
                                '用法：-c /path/to/context.log')
    parser.add_argument('--no-prompt-toolkit', action='store_true',
                        help='强制退化使用 input() 输入（即使环境装有 prompt_toolkit 也不用）')
    parser.add_argument('--no-markdown', action='store_true',
                        help='关闭大模型回复的 markdown 渲染，改为纯文本输出（仅在装有 rich 的交互模式下有意义）')
    args = parser.parse_args()

    # 加载 API 配置（如果配置文件不存在或字段缺失，会提示用户输入）
    load_config()

    # 用配置里的上下文 / 压缩阈值（token）覆盖默认值
    _refresh_context_limits()
    # 工具阈值（图片大小上限等）也一并从配置刷新
    ai_agent_tools._refresh_tool_limits()
    print(
        f"   📊 上下文窗口 {CONTEXT_LIMIT:,} tok ｜ 无人值守压缩 {AUTO_COMPRESS_THRESHOLD:,} tok "
        f"｜ 大模型压缩 {LLM_COMPRESS_THRESHOLD:,} tok ｜ "
        f"图片上限 {ai_agent_tools.MAX_IMAGE_SIZE:,} 字节（{ai_agent_tools.MAX_IMAGE_SIZE / (1024 * 1024):g} MB）",
        flush=True,
    )
    # 打印当前生效的附加请求参数（思考等级 / beta 开关等），配了才显示
    _report_api_extras()

    # 初始化日志（以程序启动时间命名）
    init_logger()

    # ----- 初始化 skills 目录（同时检查启动目录和代码目录下的 skills/，同名以启动目录版本为准） -----
    # ----- 初始化 skills 目录（同时检查启动目录和代码目录下的 skills/，同名以启动目录版本为准） -----
    ai_agent_tools._init_skills_dirs()
    
    # ----- 初始化 MCP 工具连接（连接所有配置的 MCP 服务器，注册工具到路由表） -----
    ai_agent_tools._init_mcp_tools()
    
    # ----- 构建 skills 列表提示 -----
    available_skills = ai_agent_tools._list_available_skills()
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
    # 记录本次是否从 -r / -c 恢复了历史上下文（启动后回放到终端用）
    loaded_context_source = None
    # ---- 处理 -c / --context-log：指定上下文日志文件并持续写入 ----
    if args.context_log:
        ctx_log_path = args.context_log
        global _context_log_file
        # 记录文件原本是否存在：存在则先加载，不存在则直接创建并作为新会话语料
        ctx_existed = False
        try:
            # 确保文件可写（不存在则创建，含父目录）
            ctx_dir = os.path.dirname(os.path.abspath(ctx_log_path))
            if ctx_dir:
                os.makedirs(ctx_dir, exist_ok=True)
            ctx_existed = os.path.isfile(ctx_log_path)
            if not ctx_existed:
                with open(ctx_log_path, "w", encoding="utf-8") as _f:
                    _f.write(f"===== Session started at {_timestamp()} =====\n")
        except Exception as e:
            print(f"   ❌ 无法创建/访问上下文日志文件: {e}", flush=True)
            print(f"   将回退到普通会话（不写入该文件）。", flush=True)
            ctx_log_path = None

        if ctx_log_path is not None:
            _context_log_file = ctx_log_path
            if ctx_existed:
                print(f"📝 上下文日志文件(已存在): {ctx_log_path}", flush=True)
            else:
                print(f"📝 已创建上下文日志文件: {ctx_log_path}", flush=True)

        if ctx_log_path is not None and ctx_existed:
            # 文件已存在：先尝试加载其中的快照恢复会话
            print(f"📥 正在从上下文日志加载会话: {ctx_log_path}", flush=True)
            restored_messages, err = _resume_from_log(ctx_log_path)
            if not err:
                msg_count = len(restored_messages)
                last_user_msg = ""
                for m in reversed(restored_messages):
                    if m.get("role") == "user" and isinstance(m.get("content"), str):
                        last_user_msg = m["content"][:100]
                        break
                print(f"   ✅ 加载成功！共 {msg_count} 条消息", flush=True)
                if last_user_msg:
                    print(f"   📝 最后一条用户输入: {last_user_msg}{'...' if len(last_user_msg) >= 100 else ''}", flush=True)
                messages = restored_messages
                loaded_context_source = f"上下文日志 {ctx_log_path}"
            else:
                print(f"   ⚠️ 未从文件恢复（{err}），以新会话启动，后续快照仍写入该文件。", flush=True)
                messages = [
                    {"role": "system", "content": system_prompt}
                ]
        else:
            # 文件原本不存在（刚创建）或创建失败：以新会话启动
            messages = [
                {"role": "system", "content": system_prompt}
            ]
        # 注：若 ctx_log_path 为 None（创建失败）则 _context_log_file 未设置，
        #     快照仍写入默认 log 目录。

    # ---- 处理 -r / --resume 恢复会话 ----
    if args.resume:
        # -r 优先级高于 -c：它会覆盖 messages，这里同步重置回放来源（成功后重设）
        loaded_context_source = None
        log_path = args.resume
        print(f"📥 正在从日志恢复会话: {log_path}", flush=True)
        restored_messages, err = _resume_from_log(log_path)
        if err:
            print(f"   ❌ 恢复失败: {err}", flush=True)
            print("   将启动新的会话。", flush=True)
            messages = [
                {"role": "system", "content": system_prompt}
            ]
        else:
            msg_count = len(restored_messages)
            # 取最后一条用户输入作为提示
            last_user_msg = ""
            for m in reversed(restored_messages):
                if m.get("role") == "user" and isinstance(m.get("content"), str):
                    last_user_msg = m["content"][:100]
                    break
            print(f"   ✅ 恢复成功！共 {msg_count} 条消息", flush=True)
            if last_user_msg:
                print(f"   📝 最后一条用户输入: {last_user_msg}{'...' if len(last_user_msg) >= 100 else ''}", flush=True)
            messages = restored_messages
            loaded_context_source = f"日志 {log_path}"
    elif not args.context_log:
        # 初始化全局 messages（未指定 -r 或 -c 时的新会话）
        messages = [
            {"role": "system", "content": system_prompt}
        ]

    # 判断是否管道/重定向输入（stdin 非 TTY）。此时无法用 prompt_toolkit（其要求 TTY），
    # 且无需交互循环——读取 stdin 全部内容作为单条输入，处理完即自动退出。
    pipe_mode = not sys.stdin.isatty()

    # 决定输入模式：有依赖且未强制禁用，且 stdin 是 TTY → 用 prompt_toolkit；否则退化 input()
    use_prompt_toolkit = HAS_PROMPT_TOOLKIT and not args.no_prompt_toolkit and not pipe_mode
    global USE_INPUT_MODE
    USE_INPUT_MODE = not use_prompt_toolkit

    # markdown 渲染：仅在「装了 rich + 交互模式 + 使用 prompt_toolkit」时启用；
    # 非交互（管道/重定向）或未使用 prompt_toolkit 时，一律保持纯文本 print。
    global USE_MARKDOWN
    USE_MARKDOWN = bool(HAS_RICH and use_prompt_toolkit and not args.no_markdown)

    if use_prompt_toolkit:
        print("🧙 魔理沙 (常驻输入框 · 分区渲染 prompt_toolkit)", flush=True)
        print("   📝 回车=换行  |  Alt+Enter(或Esc+Enter)=提交", flush=True)
        print("   ⚡ 模型输出时照样能打字 —— 输入会排队追加到后续，不会被输出干扰", flush=True)
        print("   📊 输入框下方状态栏：💤等待输入 / ✨思考中 / 🔧执行工具 ＋ 上下文 token 用量", flush=True)
        print("   ❌ Ctrl+C 连按两次=退出  |  Ctrl+D=退出", flush=True)
        if not HAS_RICH:
            print("   🖋️ Markdown 渲染：关闭（未安装 rich）", flush=True)
        elif args.no_markdown:
            print("   🖋️ Markdown 渲染：关闭（--no-markdown）", flush=True)
        else:
            print("   🖋️ Markdown 渲染：开启（仅美化大模型回复，工具输出保持原样）", flush=True)
        print("   ⚡ 工具执行中按Ctrl+C=中断魔法\n", flush=True)
    else:
        if pipe_mode:
            reason = "stdin 非 TTY（管道/重定向输入）"
        elif not HAS_PROMPT_TOOLKIT:
            reason = "环境未安装 prompt_toolkit"
        else:
            reason = "已按 --no-prompt-toolkit 强制退化"
        print("🧙 魔理沙 (多行输入模式 input)", flush=True)
        print(f"   ⚠️  检测到：{reason}", flush=True)
        if not pipe_mode:
            print("   ℹ️  未启用分区渲染：输出可能与输入行交织（装上 prompt_toolkit 即可获得常驻输入框）", flush=True)
        if pipe_mode:
            print("   📤 流式读取标准输入：每收到一条以 '.' 行结尾（或管道关闭时）的消息就回答一次，直到 EOF 自动退出\n", flush=True)
        else:
            print("   📝 每行输入一段，单行 '.' 结束整句话；以 '..' 开头的行会还原为 '.'", flush=True)
            print("   ❌ 按 Ctrl+C 退出（Windows 下 Ctrl+D 不标准，请用 Ctrl+C 或输入 exit）  |  ⚡ 工具执行中按Ctrl+C=中断魔法\n", flush=True)

    # ---- 若从 -r / -c 恢复了历史上下文，先把这段对话回放到终端（开工前先回顾）----
    if loaded_context_source:
        _replay_loaded_context(messages, loaded_context_source)
    # 创建输入会话（prompt_toolkit 模式返回 session；input 模式返回 None）
    session = create_prompt_session() if use_prompt_toolkit else None

    # ══════════════════════════════════════════════════════════════
    #  多路复用输入总线 —— 键盘 / socket / 管道 / 后台任务 统一入口
    #    · 主线程 = UI 线程：常驻键盘输入循环（prompt_toolkit + patch_stdout 分区渲染）
    #    · 后台线程 = agent 工作线程：从总线取事件，逐条驱动一轮对话
    #    · 任意来源有输入都会唤醒 agent 工作线程（谁先来谁触发）
    #    · 没有 prompt_toolkit 时退化为 input()：多路复用能力不减，只是没有分区渲染
    # ══════════════════════════════════════════════════════════════
    global _input_bus
    _input_bus = io_bus.InputBus()
    _register_input_sources(_input_bus, pipe_mode)
    _input_bus.start()
    # 后台任务完成 → 投递到输入总线（真正异步唤醒 agent，而不是等用户下次输入）
    ai_agent_tools.set_bg_event_hook(_bg_event_hook)

    worker = threading.Thread(
        target=_agent_worker, args=(_input_bus,), name="marisa-agent", daemon=True
    )
    worker.start()

    if pipe_mode:
        # 管道 / 重定向：stdin 已由 StdinPipeSource 线程流式读取，读到 EOF 时投递 STOP
        worker.join()
    else:
        # 交互模式：主线程跑常驻键盘输入循环，直到用户要求退出
        _run_ui_loop(_input_bus, session, use_prompt_toolkit)
        _input_bus.put_stop()
        worker.join(timeout=30)

    # ----- 程序退出时清理 MCP 连接 -----
    ai_agent_tools._cleanup_mcp_tools()


def _bg_event_hook(task_id):
    """后台任务完成钩子：把「任务已结束」转成一条输入事件投递到输入总线。

    这样后台任务完成可以真正「异步唤醒」agent，而不是像以前那样只在
    用户下次敲回车时才被顺带发现。
    """
    bus = _input_bus
    if bus is None:
        return
    try:
        text = ai_agent_tools._bg_build_event_message()
    except Exception:
        return
    if text is None:
        return
    bus.put(text, io_bus.SRC_BACKGROUND)


def _register_input_sources(bus, pipe_mode):
    """按配置把「后台输入源」挂到总线上（键盘源由主线程 UI 循环负责）。"""
    # ① 管道 / 重定向：stdin 流式逐条读取
    if pipe_mode:
        bus.add_source(io_bus.StdinPipeSource())

    # ② socket 输入源（可选；在 ai_agent_config.json 的 input_sources.socket 里开启）
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    sock_cfg = (cfg.get("input_sources") or {}).get("socket") or {}
    if sock_cfg.get("enabled"):
        host = sock_cfg.get("host") or "127.0.0.1"
        token = sock_cfg.get("auth_token") or ""
        try:
            port = int(sock_cfg.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if port > 0:
            bus.add_source(io_bus.SocketSource(host=host, port=port, auth_token=token))
        else:
            print("   ⚠️ input_sources.socket 已启用但 port 无效，已跳过", flush=True)



def _pt_can_use_ansi():
    """prompt_toolkit 当前输出后端能否安全承载原生 ANSI 转义序列。

    为什么需要判断：
      · patch_stdout() 默认 raw=False，底层 Vt100_Output.write() 会把 ESC(\x1b)
        直接替换成 '?'  →  rich 的颜色码就变成 ?[1;36m 乱码（用户实际看到的）
      · Win32Output 走 Win32 控制台 API，根本不解析 ANSI → 同样变乱码

    判定方式（比 isinstance 白名单更耐版本变化）：
      · Vt100_Output                → ANSI 安全
      · Windows10_Output / ConEmuOutput → 内部持有 vt100_output，flush 时开 VT → ANSI 安全
      · Win32Output / PlainTextOutput / DummyOutput → 不安全
    """
    try:
        from prompt_toolkit.application import get_app_session
        from prompt_toolkit.output.vt100 import Vt100_Output

        out = get_app_session().output
        if isinstance(out, Vt100_Output):
            return True
        # 用 __dict__ 取值，避免触发包装类 __getattr__ 的无限递归
        inner = getattr(out, "__dict__", {}).get("vt100_output")
        return isinstance(inner, Vt100_Output)
    except Exception:
        return False


def _pt_output_name():
    """当前 prompt_toolkit 输出后端的类名（仅用于启动时诊断打印）。"""
    try:
        from prompt_toolkit.application import get_app_session

        return type(get_app_session().output).__name__
    except Exception:
        return "未知"


def _handle_prompt_ctrl_c():
    """在输入处按下 Ctrl+C 的统一处理。返回 True 继续，False 退出。

    与改造前语义保持一致：
      · agent 正在后台跑 → 只中断它，不退出程序
      · 空闲时 → 2 秒内连按两次才退出（仿 Claude）
    """
    global interrupted
    if tool_executing:
        interrupted = True
        print("\n⚠️  中断魔法吟唱！回到对话模式...\n", flush=True)
        return True
    now = time.time()
    if now - _ctrl_c_state["ts"] < EXIT_CONFIRM_SECONDS:
        return False
    _ctrl_c_state["ts"] = now
    print("\n⚠️ 再按一次 Ctrl+C 退出（2 秒内）\n", flush=True)
    return True


def _run_ui_loop(bus, session, use_prompt_toolkit):
    """主线程 UI：常驻键盘输入循环。函数返回即代表用户要求退出。

    · prompt_toolkit 模式：用 patch_stdout() 把 agent 的输出「画到输入框上方」，
      输入框常驻底部；模型输出时照样能打字，打的内容会排队追加到后续输入。
    · 退化模式：input() 逐行读取，功能完全一致，只是输出可能与输入行交织。
    """
    global UI_PROMPT_ACTIVE

    def _submit(text):
        """投递一条键盘输入。返回 True 继续循环，False 表示该退出了。"""
        if text is None:
            return False
        if not text.strip():
            return True
        if text.strip().lower() in ("exit", "quit"):
            return False
        bus.put(text, io_bus.SRC_KEYBOARD)
        return True

    def _degraded_loop():
        """退化模式：纯 input() 逐行读取（没有分区渲染，但输入输出依然互不阻塞）。"""
        while True:
            try:
                text = read_multiline_input("我: ")
            except KeyboardInterrupt:
                if not _handle_prompt_ctrl_c():
                    break
                continue
            except EOFError:
                print("\n👋 再见！DA☆ZE！\n", flush=True)
                break
            if not _submit(text):
                print("\n👋 再见！DA☆ZE！\n", flush=True)
                break

    if use_prompt_toolkit and session is not None and patch_stdout is not None:
        # 先探测输出后端能不能吃下 ANSI —— 这决定了 rich 是否上色、patch_stdout 的 raw 开关。
        global UI_ANSI_OK
        UI_ANSI_OK = _pt_can_use_ansi()
        print(
            f"   🖥️ 终端渲染后端：{_pt_output_name()}"
            f"（ANSI {'支持' if UI_ANSI_OK else '不支持'}）",
            flush=True,
        )
        if not UI_ANSI_OK:
            print(
                "   ℹ️ 该后端无法解析 ANSI，Markdown 将以无颜色方式渲染"
                "（表格 / 标题 / 列表结构保留）",
                flush=True,
            )

        # patch_stdout() 会在构造时就探测终端输出能力（某些终端如未走 winpty 的
        # mintty 会直接抛 NoConsoleScreenBufferError）。这里必须容错：
        # 分区渲染不可用就退回普通输入，绝不因此让整个程序崩掉。
        ctx = None
        try:
            # raw=True：让 ANSI 原样穿过，交给 Vt100 系后端渲染颜色；
            # raw=False（默认）会把 ESC 替换成 '?'，正是 ?[1;36m 乱码的来源。
            ctx = patch_stdout(raw=UI_ANSI_OK)
            ctx.__enter__()
        except Exception as e:
            print(f"   ⚠️ 分区渲染不可用，退化为普通输入模式：{e}", flush=True)
            try:
                if ctx is not None:
                    ctx.__exit__(None, None, None)
            except Exception:
                pass
            ctx = None

        if ctx is not None:
            UI_PROMPT_ACTIVE = True
            # 先把上下文用量算一次填进状态栏（之后每次 API 返回会自动刷新）
            try:
                _set_usage(format_usage_short())
            except Exception:
                pass
            try:
                while True:
                    try:
                        # bottom_toolbar：在输入框下方画状态栏（当前状态 + 上下文用量）
                        # refresh_interval：周期性自动重绘，让后台线程改的状态能及时显出来
                        text = session.prompt(
                            "我: ",
                            bottom_toolbar=_bottom_toolbar,
                            refresh_interval=0.3,
                        )
                        # 提交后补打完整提示词——长提示词在输入框里滚动过之后，
                        # 终端上只留下最后一屏，这里补一份完整记录到滚动历史
                        _echo_user_input(text)
                    except KeyboardInterrupt:
                        if not _handle_prompt_ctrl_c():
                            break
                        continue
                    except EOFError:
                        print("\n👋 再见！DA☆ZE！\n", flush=True)
                        break
                    if not _submit(text):
                        print("\n👋 再见！DA☆ZE！\n", flush=True)
                        break
            except Exception as e:
                # 输入会话本身崩了（终端能力不支持等）→ 退回退化模式，保住可用性
                print(f"\n⚠️ 常驻输入框异常，退化为普通输入模式：{e}\n", flush=True)
            finally:
                UI_PROMPT_ACTIVE = False
                try:
                    ctx.__exit__(None, None, None)
                except Exception:
                    pass
            return

    # ---- 退化模式 ----
    _degraded_loop()

def _agent_worker(bus):
    """Agent 工作线程：从输入总线取事件，驱动一轮又一轮对话。

    任一输入源（键盘 / socket / 管道 / 后台任务通知）有输入都会唤醒本循环。
    输出统一走 print / rich，由主线程的 patch_stdout 渲染到输入框上方。
    """
    global tool_executing, interrupted, messages
    global _main_model_no_multimodal, _api_timeout_retry_remaining

    while True:
        # 每次外层循环开始时，执行大内容过期检查
        _expire_large_content()

        # 每次回到用户输入时重置中断标志
        interrupted = False

        # 💤 即将阻塞在 bus.get() 上 —— 状态栏显示「等待输入」
        _set_agent_status("idle")

        # 📥 从输入总线阻塞取一条事件（多路复用：谁先来谁触发）
        try:
            event = bus.get()
        except (KeyboardInterrupt, EOFError):
            break
        except (KeyboardInterrupt, EOFError):
            break
        if event is None or event.text is io_bus.STOP:
            break
        user_input = event.text
        if user_input is None:
            break

        if event.source != io_bus.SRC_KEYBOARD:
            print(f"   📨 收到来自 [{event.source}] 的输入\n", flush=True)

        if not user_input.strip():
            # 空输入：跳过，继续等下一条（与改造前一致）
            continue

        # 每次收到新的用户输入，刷新本轮的超时自动重试额度（最多 API_TIMEOUT_RETRY_MAX 次）
        _api_timeout_retry_remaining = API_TIMEOUT_RETRY_MAX

        # ✨ 开始干活 —— 状态栏切到「思考中」
        _set_agent_status("thinking")

        # ════════════════════════════════════════════════════════════
        # 智能预压缩：上下文超阈值时，先搁置用户输入，让 AI 压缩完再处理
        # ════════════════════════════════════════════════════════════
        # 先检查当前上下文（不含用户新输入）的 token 数是否超「大模型压缩」阈值
        cur_tokens = get_context_tokens(messages)
        need_pre_compress = cur_tokens > LLM_COMPRESS_THRESHOLD

        if need_pre_compress:
            pct = round(cur_tokens / CONTEXT_LIMIT * 100, 1) if CONTEXT_LIMIT else 0.0
            print(f"   📊 上下文 {cur_tokens:,} tok / {CONTEXT_LIMIT:,}（{pct}%），先让 AI 压缩再处理你的问题...", flush=True)
            # 追加压缩请求（不追加用户真实输入）
            messages.append({
                "role": "user",
                "content": (
                    f"[上下文使用率：{pct}%（{cur_tokens:,} tok / {CONTEXT_LIMIT:,} tok），"
                    "对话历史较长，请先调用 compress 工具压缩历史对话以节省空间。"
                    "压缩时请保留 system prompt 和最近 2-3 轮完整对话，"
                    "更早的内容用一段摘要代替。"
                    "压缩完成后我会告诉你用户真正的问题。]"
                )
            })
            # 单次 API 调用：让 AI 调用 compress
            tool_executing = True
            _set_agent_status("compress")
            try:
                with spinner("✨ 正在预压缩上下文..."):
                    msg, reasoning_content, _usage = call_api(messages, tools=ai_agent_tools.tools)
            except UserInterrupt:
                # 用户按 Ctrl+C 中断，不当作 API 翻车；interrupted 已置 True，外层会清理并 continue
                msg = None
            except Exception as e:
                print(f"\n💥 API调用翻车了: {e}\n", flush=True)
                msg = None
            finally:
                tool_executing = False

            if msg is None or interrupted:
                # API 失败或被中断，清理残留
                if msg is None:
                    messages.pop()  # 移除压缩请求
                continue

            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls")

            if content:
                print_assistant(content)  # 大模型回复走 markdown 渲染（未启用时自动纯文本）

            # 📊 预压缩这次调用的用量
            print_usage()

            if tool_calls:
                # 处理工具调用（预期是 compress）
                for tool in tool_calls:
                    if tool["type"] != "function":
                        continue
                    try:
                        args = json.loads(tool["function"]["arguments"])
                    except json.JSONDecodeError:
                        continue
                    func = tool_func_map.get(tool["function"]["name"])
                    tool_name = tool["function"]["name"]
                    if func is None:
                        # 同主循环：未知工具名追加 tool 错误响应，保持配对完整
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool["id"],
                            "name": tool_name,
                            "content": json.dumps({
                                "success": 0,
                                "err": (f"未知工具名 '{tool_name}'，该工具不存在。"
                                        f"请使用可用的工具：{', '.join(sorted(tool_func_map.keys()))}。")
                            })
                        })
                        continue
                    is_terminal = tool_name in TERMINAL_TOOLS
                    _set_agent_status("tool", tool_name)
                    try:
                        tool_result = func(**args)
                    except (TypeError, Exception) as e:
                        tool_result = json.dumps({"success": 0, "err": f"工具 {tool_name} 调用失败: {e}"})
                    # 终端工具（compress）内部已修改全局 messages 并追加了 assistant 确认
                    if is_terminal:
                        print("   ↳ 压缩完成，上下文已刷新！", flush=True)
                        # 🖥️ 同上：压缩已替换 messages，立即刷新状态栏用量
                        print_usage()
                    else:
                        # 非预期工具，追加 tool response
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool["id"],
                            "name": tool_name,
                            "content": tool_result
                        })
            # 无论 AI 是否调用了 compress，压缩请求 user 消息都保留在 messages 中
            # 但 compress 函数内部已追加了 assistant 确认，所以 messages 以 assistant 结尾
            # 现在追加用户真实输入，进入正常处理流程
            if not interrupted:
                messages.append({"role": "user", "content": user_input})
                # 若预压缩阶段大模型调用了 compress 等工具成功，进入正式工具循环前也「续命」一次
                if tool_calls:
                    _api_timeout_retry_remaining = API_TIMEOUT_RETRY_MAX
        else:
            # 上下文没超阈值，正常追加用户输入
            messages.append({"role": "user", "content": user_input})

        # ---------- 多轮工具调用循环 ----------
        tool_executing = True
        try:
            while True:
                # 每次迭代开始时检查中断标志
                if interrupted:
                    print("   ↳ 用户中断，跳过剩余工具调用", flush=True)
                    break


                # ⏰ 大内容过期：工具循环中也定期过期 tool 返回和 user 多模态消息
                if not interrupted:
                    _expire_large_content()

                # 📦 无人值守自动压缩：工具循环中上下文 token 超阈值时自动压缩中间的工具调用
                if not interrupted and get_context_tokens(messages) > AUTO_COMPRESS_THRESHOLD:
                    _squash_tool_calls_automatically(messages)
                    # 压缩后重新检查上下文，如果还是超阈值就继续压缩（最多再试一次）
                    if get_context_tokens(messages) > AUTO_COMPRESS_THRESHOLD:
                        _squash_tool_calls_automatically(messages)

                # 🛡 兜底防御：发送 API 前，修复可能残留的"孤立 tool_calls"
                # （assistant 带 tool_calls 但后面没有对应 tool 响应，会导致 400）
                _prune_incomplete_tool_calls(messages)

                try:
                    # # ✨ 修复 tool_calls 和 tool 响应之间被插队的消息顺序
                    # # 这是因为 load_skill 等工具在执行时可能会向 messages 中插入 system 消息，
                    # # 导致 assistant（带 tool_calls）后面不是紧跟着 tool 响应，违反 OpenAI 协议规范
                    # if _fix_messages_tool_order(messages):
                    #     print("   🗟 已修复 tool_calls 和 tool 响应之间的消息顺序", flush=True)

                    # # ✨ 关键改动：使用 get_context_aware_messages 包装
                    # # 如果上下文超过阈值，会自动追加一条 system 提醒
                    # api_messages = get_context_aware_messages(messages)
                    # msg, reasoning_content = call_api(api_messages, tools=tools)
                    _set_agent_status("thinking")
                    with spinner("✨ 魔理沙思考中..."):
                        msg, reasoning_content, _usage = call_api(messages, tools=ai_agent_tools.tools)
                except UserInterrupt:
                    # 用户按 Ctrl+C 中断，立即退出工具循环（interrupted 已置 True）
                    break
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
                    print_assistant(content)  # 大模型回复走 markdown 渲染（未启用时自动纯文本）

                # 📊 每次大模型回复后追加一行 token / 字节用量（一眼看出两者对照）
                print_usage()

                # 没有工具调用 → AI总结完毕，跳出内层循环
                if not tool_calls:
                    # 🛡️ 空返回防御：模型啥也没说。
                    # 若上下文含多模态 → 判定主模型不支持多模态：置标记、剥离毒消息、重试；
                    # 否则 → 不写入空消息污染历史，提示用户后结束本轮。
                    if not content:
                        if not reasoning_content and _has_multimodal_content(messages):
                            _main_model_no_multimodal = True  # 记住：主模型不支持多模态
                            print(
                                "   🗑️ 模型返回空内容且上下文含多模态数据，疑似不支持多模态，自动清理后重试...",
                                flush=True
                            )
                            _strip_multimodal(messages)
                            continue  # 重试（毒消息已清除，不会再触发本分支）
                        print(
                            "\n⚠️ 模型返回了空内容（可能是模型不支持读图或临时抽风）。"
                            "本轮未写入对话历史，请重试或换个问法。\n",
                            flush=True
                        )
                        break
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
                image_tool_called = False     # 标记是否调用了图片工具
                pending_image_msgs = []       # 收集待注入的图片 user 消息，循环结束后统一注入

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

                    tool_name = tool["function"]["name"]

                    # 🔥 自动路由：根据工具名查找函数，**kwargs 传参
                    func = tool_func_map.get(tool_name)
                    if func is None:
                        # ⚠️ 修复：未知工具名（如 AI 传了 "bash" 而非 "run_bash"）
                        # 之前这里直接 continue 跳过了，导致 assistant(tool_calls) 后面
                        # 没有对应的 tool 消息去回应它的 tool_call_id，违反 OpenAI 协议，
                        # 下一轮 API 会报 "insufficient tool messages following tool_calls message"(400)。
                        # 现在改为追加一条 tool 错误响应，保持 assistant↔tool 配对完整，
                        # 同时把错误信息返回给大模型，让它改用正确的工具名。
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool["id"],
                            "name": tool_name,
                            "content": json.dumps({
                                "success": 0,
                                "err": (f"未知工具名 '{tool_name}'，该工具不存在。"
                                        f"请使用可用的工具：{', '.join(sorted(tool_func_map.keys()))}。")
                            })
                        })
                        print(f"\n⚠️ 未知工具名，已把错误返回给大模型: {tool_name}\n", flush=True)
                        continue

                    # 判断工具类型
                    is_terminal = tool_name in TERMINAL_TOOLS  # 终止型（如 compress）
                    is_image = tool_name in IMAGE_TOOLS       # 图片型（如 read_image）

                    # 🔧 状态栏显示正在执行哪个工具
                    _set_agent_status("tool", tool_name)

                    # 🔧 try-except 容错：大模型传错参数名时把报错返回给它自己整改
                    try:
                        tool_result = func(**args)
                    except (TypeError, Exception) as e:
                        tool_result = json.dumps({"success": 0, "err": f"工具 {tool_name} 调用失败: {e}"})
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
                        # 🖥️ 压缩已整体替换 messages，立即刷新状态栏用量，
                        # 否则输入框下方会一直显示压缩前的旧值，直到下次对话才更新。
                        print_usage()
                        break

                    # 🖼️ 图片工具的特殊处理：
                    # 不追加 tool response，而是注入一条 role:user 的多模态消息
                    if is_image:
                        try:
                            result_data = json.loads(tool_result)
                        except (json.JSONDecodeError, TypeError):
                            result_data = {"success": 0}

                        _is_url_src = result_data.get("source") == "url"
                        if result_data.get("success") and ("base64" in result_data or _is_url_src):
                            # 原多模态注入逻辑（未配置多模态辅助模型，或未传 description）
                            img_base64 = result_data.get("base64", "")
                            img_mime = result_data.get("mime", "image/png")
                            img_path = result_data.get("filepath", result_data.get("url", ""))
                            # 图片在 API 侧的表示：url 源直接用 http 链接（免下载，由服务端获取），
                            # 本地源用 data URL（base64 内联）
                            if _is_url_src:
                                img_url_ref = result_data.get("url", "")
                            else:
                                img_url_ref = f"data:{img_mime};base64,{img_base64}"

                            # 🛡️ 已知主模型不支持多模态：不再注入图片消息，避免毒化对话
                            if _main_model_no_multimodal:
                                mul_config = ai_agent_tools._build_mul_config()
                                if mul_config is not None:
                                    # 改用辅助多模态模型读图，把文本描述作为 tool 响应
                                    read_prompt = (
                                        "这张图片是主模型调用 read_image 工具后返回的结果。"
                                        "请详细描述图片内容，包括所有可见的文字、物体、布局、颜色等细节。"
                                    )
                                    tool_content = ai_agent_tools._read_image_via_subagent(
                                        img_path, img_mime, img_base64,
                                        result_data.get("size_kb", 0),
                                        read_prompt,
                                        mul_config,
                                        url_ref=(img_url_ref if _is_url_src else None)
                                    )
                                    print(f"   🔮 主模型不支持多模态，已改用辅助多模态模型读图: {img_path}", flush=True)
                                else:
                                    # 没有辅助模型：只给文本元信息 + 明确提示
                                    tool_content = json.dumps({
                                        "success": 1,
                                        "filepath": img_path,
                                        "mime": img_mime,
                                        "size_kb": result_data.get("size_kb", 0),
                                        "message": ("图片已读取，但当前主模型不支持多模态读图，无法查看图片内容。"
                                                    "如需识别图片，请配置 mul_* 多模态辅助模型后使用带 description 的读图方式。")
                                    })
                                    print(f"   ⚠️ 主模型不支持多模态，未注入图片内容: {img_path}", flush=True)
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool["id"],
                                    "name": tool_name,
                                    "content": tool_content
                                })
                                continue  # 不注入多模态消息，进入下一轮工具循环

                            # 第一步：先追加 tool 响应，满足 API 协议要求
                            # assistant(tool_calls) 后面必须紧跟着 tool 消息
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool["id"],
                                "name": tool_name,
                                "content": json.dumps({
                                    "success": 1,
                                    "filepath": img_path,
                                    "mime": img_mime,
                                    "size_kb": result_data.get("size_kb", 0),
                                    "message": f"图片已读取，大小约 {result_data.get('size_kb', 0)}KB"
                                })
                            })
                            # 第二步：再注入 role:user 的多模态消息，让 AI 看到图片内容
                            pending_image_msgs.append({
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": f"[这是你刚才请求读取的图片：{img_path}]"
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": img_url_ref
                                        }
                                    }
                                ]
                            })
                            print(f"   🖼️ 图片已注入对话上下文: {img_path}", flush=True)
                            image_tool_called = True  # 在循环外定义
                        else:
                            # 多模态辅助模型读图模式（mode=subagent，结果已是文本描述）
                            # 或读取失败：统一按普通工具追加 tool response，
                            # 不再注入多模态消息（主模型可能不支持多模态）
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool["id"],
                                "name": tool_name,
                                "content": tool_result
                            })
                            if result_data.get("success"):
                                print(f"   🔮 多模态辅助模型读图完成，结果已返回: {result_data.get('filepath', '')}", flush=True)
                            else:
                                print(f"   ⚠️ 图片读取失败: {result_data.get('err', '未知错误')}", flush=True)
                        continue  # 继续处理其他工具

                    # 📸 MCP 图片检测：任何工具（包括 MCP 工具）返回的结果中
                    # 如果包含 _mcp_images 字段，像 IMAGE_TOOLS 一样注入多模态消息
                    mcp_images = None
                    try:
                        _tr_parsed = json.loads(tool_result)
                        if isinstance(_tr_parsed, dict) and "_mcp_images" in _tr_parsed:
                            mcp_images = _tr_parsed["_mcp_images"]
                    except (json.JSONDecodeError, TypeError):
                        pass

                    if mcp_images and isinstance(mcp_images, list) and len(mcp_images) > 0:
                        # 从 tool_result 中去掉 _mcp_images 字段，得到纯文本 tool 响应
                        try:
                            _clean_result = json.loads(tool_result)
                            if isinstance(_clean_result, dict):
                                _clean_result.pop("_mcp_images", None)
                                clean_tool_content = json.dumps(_clean_result)
                            else:
                                clean_tool_content = tool_result
                        except (json.JSONDecodeError, TypeError):
                            clean_tool_content = tool_result

                        mul_config = ai_agent_tools._build_mul_config()
                        if mul_config:
                            # 🔮 多模态辅助模型读图模式：MCP 图片交给辅助模型识别成文本描述，
                            # 并入 tool response 文本返回（主模型可能不支持多模态，不再注入图片消息）
                            # 🔮 读图 prompt 携带主模型调用 MCP 工具的原始 arguments（原文），
                            # 直接把调用 MCP 函数时的完整参数原文交给辅助模型，
                            # 让辅助模型自行判断重点观察哪里；无参数时回退通用描述
                            if isinstance(args, dict) and args:
                                read_prompt = (
                                    "这张图片是调用 MCP 工具 " + tool_name + " 后返回的结果。\n"
                                    f"调用该工具时的参数：{json.dumps(args, ensure_ascii=False)}\n"
                                    "请重点观察与上述调用信息相关的细节并准确回答，同时简要说明图片的整体内容。"
                                )
                            else:
                                read_prompt = "请详细描述这张图片的内容，包括所有可见的文字、物体、布局、颜色等细节。"

                            img_descriptions = []
                            for img_idx, img in enumerate(mcp_images):
                                if not isinstance(img, dict):
                                    continue
                                img_b64 = img.get("base64", "")
                                img_mime = img.get("mimeType", "image/png")
                                if not img_b64:
                                    continue
                                img_size_kb = len(img_b64) * 3 // 4 // 1024
                                img_label = f"[MCP 工具 {tool_name} 返回的图片"
                                if len(mcp_images) > 1:
                                    img_label += f" {img_idx + 1}/{len(mcp_images)}"
                                img_label += f" ({img_size_kb}KB)]"
                                sub_result = ai_agent_tools._read_image_via_subagent(
                                    img_label, img_mime, img_b64, img_size_kb,
                                    read_prompt,
                                    mul_config
                                )
                                try:
                                    sub_data = json.loads(sub_result)
                                except (json.JSONDecodeError, TypeError):
                                    sub_data = {"success": 0, "err": f"读图结果解析失败: {str(sub_result)[:100]}"}
                                if sub_data.get("success"):
                                    desc_text = sub_data.get("result", "")
                                else:
                                    desc_text = f"读取失败: {sub_data.get('err', '未知错误')}"
                                img_descriptions.append(f"图片 {img_idx + 1}/{len(mcp_images)} 内容：{desc_text}")
                            # 将描述并入 tool response 文本
                            _full_content = clean_tool_content
                            if img_descriptions:
                                try:
                                    _full_obj = json.loads(clean_tool_content)
                                    if isinstance(_full_obj, dict):
                                        _full_obj["image_descriptions"] = img_descriptions
                                        _full_content = json.dumps(_full_obj, ensure_ascii=False)
                                    else:
                                        _full_content = clean_tool_content + "\n" + "\n".join(img_descriptions)
                                except (json.JSONDecodeError, TypeError):
                                    _full_content = clean_tool_content + "\n" + "\n".join(img_descriptions)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool["id"],
                                "name": tool_name,
                                "content": _full_content
                            })
                            print(f"   🔮 MCP 图片已由多模态辅助模型识别为文本描述（{len(img_descriptions)} 张），已并入 tool response", flush=True)
                            continue  # 跳过正常 tool response 追加（已追加）

                        # 原逻辑：注入 role:user 多模态消息（主模型支持多模态时使用）
                        if _main_model_no_multimodal:
                            # 🛡️ 主模型不支持多模态：只给文本提示，不注入图片
                            _note = (
                                "（当前主模型不支持多模态读图，图片内容已跳过。"
                                "如需识别图片，请配置 mul_* 多模态辅助模型。）"
                            )
                            try:
                                _full_obj = json.loads(clean_tool_content)
                                if isinstance(_full_obj, dict):
                                    _full_obj["image_note"] = _note
                                    _full_content = json.dumps(_full_obj, ensure_ascii=False)
                                else:
                                    _full_content = clean_tool_content + "\n" + _note
                            except (json.JSONDecodeError, TypeError):
                                _full_content = clean_tool_content + "\n" + _note
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool["id"],
                                "name": tool_name,
                                "content": _full_content
                            })
                            print(f"   ⚠️ 主模型不支持多模态，跳过 {len(mcp_images)} 张 MCP 图片注入", flush=True)
                            continue  # 跳过正常 tool response 追加（已追加）

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool["id"],
                            "name": tool_name,
                            "content": clean_tool_content
                        })
                        # 为每张图片注入 role:user 的多模态消息
                        for img_idx, img in enumerate(mcp_images):
                            if not isinstance(img, dict):
                                continue
                            img_b64 = img.get("base64", "")
                            img_mime = img.get("mimeType", "image/png")
                            if not img_b64:
                                continue
                            img_size_kb = len(img_b64) * 3 // 4 // 1024
                            img_label = f"[MCP 工具 {tool_name} 返回的图片"
                            if len(mcp_images) > 1:
                                img_label += f" {img_idx + 1}/{len(mcp_images)}"
                            img_label += f" ({img_size_kb}KB)]"
                            pending_image_msgs.append({
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": img_label
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:{img_mime};base64,{img_b64}"
                                        }
                                    }
                                ]
                            })
                            print(f"   🖼️ MCP 图片已注入对话上下文：{img_label}", flush=True)
                        image_tool_called = True
                        continue  # 跳过正常 tool response 追加

                    # 正常工具：追加 tool response
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool["id"],
                        "name": tool_name,
                        "content": tool_result
                    })
                # 本轮工具调用已成功执行完毕 → 触发下一次获取大模型响应前，刷新超时重试额度
                #（无人值守长跑场景：每完成一轮工具调用都「续命」一次 10 次超时重试）
                _api_timeout_retry_remaining = API_TIMEOUT_RETRY_MAX

                # 循环结束后，把收集到的图片 user 消息统一注入，确保不打断 tool_calls↔tool 配对
                if pending_image_msgs:
                    messages.extend(pending_image_msgs)

                # 如果调用了终止型工具，跳出整个工具循环
                if terminal_tool_called:
                    save_messages_snapshot(messages)
                    break
                # 如果调用了图片工具，继续下一轮 API 调用让大模型看到图片
                if image_tool_called:
                    save_messages_snapshot(messages)
                    print("   ↳ 图片已注入，继续识别图片内容...", flush=True)
                    continue
                # 如果被中断，跳出工具循环
                if interrupted:
                    # 从 assistant 消息位置开始全部删除，防止下轮API报400
                    # 把没完成的assistant消息踢出去，防止下轮API报400
                    del messages[assistant_idx:]
                    break

        finally:
            tool_executing = False

    # 走到这里说明 worker 收到了 STOP 哨兵 —— 退出与资源清理统一由主线程负责


if __name__ == "__main__":
    main()
