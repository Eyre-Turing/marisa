#!/usr/bin/env python3
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
    "model": "deepseek-chat"
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
    for key in ["api_key", "base_url", "model"]:
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
#  1. 调用 DeepSeek Chat API（纯标准库，不依赖 openai）
# ============================================================
def call_deepseek_api(messages, tools=None, tool_choice="auto"):
    """直接构造 HTTP 请求调用 API（从 ai_agent_config.json 读取参数）"""
    config = load_config()
    api_key = config.get("api_key")
    if not api_key:
        raise ValueError("API 密钥未配置！请检查 ai_agent_config.json 文件。")

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
            "description": "编辑文件中指定行范围的内容，并以 git diff 风格展示改动。行号从1开始计数。修改前会备份原始行内容，成功返回 unified diff 格式的差异。注意：只有当你完全确认原文件待改动的开始行号和结束行号时才可以使用此工具。",
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
            "description": "通过内容锚定来编辑文件，可以理解为类似执行伪代码 file(filename).replace(old_content, new_content) ，把文件里的旧内容完全替换为新内容。传入要匹配的旧内容（作为锚定）和新内容（替换后的内容），工具会自动在文件中找到唯一匹配的位置进行替换。如果匹配不到或匹配到多个位置会报错，并返回详细信息供AI调整。替代 edit_file_lines 在不确定行号时使用。注意：只有当你能确定文件中有且仅有一处匹配时才使用此工具！",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要编辑的文件名（完整路径）"
                    },
                    "old_content": {
                        "type": "string",
                        "description": "用于锚定位置的旧内容，必须精确匹配文件中的一段连续文本，且只能匹配到唯一位置。这部分内容会完全替换为new_content的内容"
                    },
                    "new_content": {
                        "type": "string",
                        "description": "替换成的新内容，将替换 old_content 匹配到的位置。会完全替换old_content的内容，所以如果old_content如果仅做为锚定而不想修改，请在这个参数里把old_content的内容也带上"
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
            # 没找到！报错并给出文件的部分内容供AI参考
            # 显示文件的前后各一部分，帮助AI调试
            preview = ""
            if len(original_text) > 500:
                preview = original_text[:250] + "\n......\n" + original_text[-250:]
            else:
                preview = original_text
            err_msg = (
                f"在文件 {filename} 中未找到匹配的内容！\n"
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
                f"请提供更精确的 old_content（增加更多上下文内容以确保唯一匹配），"
                f"或者使用 edit_file_lines 工具（如果知道精确行号）进行修改。"
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
                    msg, reasoning_content = call_deepseek_api(api_messages, tools=tools)
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

                for tool in tool_calls:
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

                    tool_result = func(**args)

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
