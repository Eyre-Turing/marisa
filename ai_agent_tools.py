#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
兴趣使然的 AI Agent —— 工具定义与实现（从 ai_agent_prompt.py 拆分而来）

本模块负责「工具」这一层，包含：
  - 工具的 JSON schema 列表（tools）
  - 各工具函数的实现：run_bash / read_image / 文件读写 / 后台任务 / skills / MCP ...
  - 工具名 -> 函数 的路由表（tool_func_map）
  - MCP 工具的动态注册与清理

与主程序 ai_agent_prompt.py 的协作方式：
  少数工具函数需要访问主程序里的运行时状态或能力，例如：
    全局 messages、中断标志 interrupted、大内容计数器 large_content_counter、
    子 Agent 调用 call_api()、上下文大小 get_context_size()、
    多模态标记 _main_model_no_multimodal、配置 load_config()/DEFAULT_CONFIG 等。

  这些依赖统一通过「运行时句柄」_RT 在运行时解析：主程序启动时调用
  bind_runtime(ai_agent_prompt 模块对象)，之后工具函数用 _RT.<name> 读写。

  ⚠️ 本模块不得在顶层 import ai_agent_prompt（会造成循环导入）。
     所有对主程序的依赖都走 _RT，请在工具函数内部访问。
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
import queue
import time
import atexit
import tempfile

# MCP (Model Context Protocol) 支持 —— 连接外部 MCP 服务器
from mcp_manager import get_mcp_manager


# ============================================================
#  运行时句柄 —— 由主程序在启动时注入
# ============================================================
_RT = None


def bind_runtime(module):
    """由 ai_agent_prompt 在启动时调用，注入其模块对象。

    工具函数通过 _RT 访问主程序中共享的运行时状态/能力，例如：
      _RT.messages / _RT.interrupted / _RT.large_content_counter /
      _RT._main_model_no_multimodal / _RT.LARGE_CONTENT_THRESHOLD /
      _RT.DEFAULT_CONFIG / _RT.load_config() / _RT.call_api() /
      _RT.get_context_size() / _RT._parse_size_bytes() / _RT.UserInterrupt
    """
    global _RT
    _RT = module


# 工具输出硬截断阈值：单次工具返回内容超过该大小则截断，避免撑爆上下文
TOOL_HARD_CUTOFF = 200 * 1024  # 200KB


# ============================================================
#  2. 工具定义 & 执行
# ============================================================

tools = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "在本地执行一条命令并返回结果。正常情况结果为一个json，有stdout、stderr、code、shell四个字段，stdout为标准输出（字符串），stderr为标准错误（字符串），code为返回值（数字），shell为实际使用的执行外壳（'bash'/'sh'/'cmd'）。如果异常，结果为空字符串。注意 shell 字段：bash/sh 支持单引号；cmd（仅当 Windows 且无 bash 时）不支持单引号——在 cmd 下 python -c 'print(1)' 会静默无输出且 code=0，这是引号问题不是命令错误，请改用双引号 python -c \"print(1)\"",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令行"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间，单位为秒，如果不传默认为10"
                    },
                    "force_use_bash": {
                        "type": "boolean",
                        "description": "选填，默认为false。为true时强制以 bash 执行（等价于 bash -c '命令'，不经过 cmd 解析，单引号可用）；若系统没有 bash 可执行文件，会返回找不到 bash 的报错信息，届时请自行改用 cmd 语法、去掉 force_use_bash，或将命令写入脚本文件再执行"
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
    {
        "type": "function",
        "function": {
            "name": "mcp_list_servers",
            "description": "列出所有已配置的 MCP 服务器及其运行状态。返回每个服务器的名称、传输层、是否启用、是否自动连接、当前是否在运行、工具数量等信息。当你需要了解当前有哪些 MCP 服务可用、或者某个 MCP 进程是否存活时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_connect_server",
            "description": "连接/启动指定的 MCP 服务器。如果服务器已经在运行，会先断开再重新连接。连接成功后自动注册该服务器的工具到工具列表中。适用于：手动启动 auto_connect=false 的服务器、重新连接已崩溃的 MCP 进程、或者你想临时启用一个未自动连接的 MCP 服务。参数 name 为 mcp_config.json 中配置的服务器名称。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要连接的 MCP 服务器名称（与 mcp_config.json 中配置的 name 完全一致）"
                    }
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_disconnect_server",
            "description": "断开指定的 MCP 服务器，停止其进程、释放资源，并从工具列表中移除该服务器提供的工具。适用于你想手动停止某个 MCP 服务以释放系统资源。参数 name 为要断开的服务器名称。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要断开的 MCP 服务器名称"
                    }
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_restart_server",
            "description": "重启指定的 MCP 服务器（先断开再重新连接）。重启成功后自动重新注册该服务器提供的工具。主要适用于 MCP 进程崩溃后重新启动。参数 name 为要重启的服务器名称。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要重启的 MCP 服务器名称"
                    }
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_image",
            "description": "读取图片。支持两种来源（二选一）：①本地图片文件（filepath）；②网络图片 URL（url）。当用户提供本地图片路径时传 filepath；当用户给出图片网址时传 url。读取成功后图片会自动以多模态格式（role:user）注入对话，你可以直接看到图片内容：本地源转 base64 内联，url 源直接把链接交给多模态 API 由其服务端获取（无需下载）。如果配置了多模态辅助模型（mul_api_key 等），可以同时传入 description 参数描述你想从图片读到什么，工具会调用辅助多模态模型识别并直接返回文本描述，无需主模型支持多模态。支持格式: png, jpg, jpeg, gif, bmp, webp 等。本地图片最大 5MB。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "（与 url 二选一）本地图片文件的完整路径（支持绝对路径和相对路径）"
                    },
                    "url": {
                        "type": "string",
                        "description": "（与 filepath 二选一）网络图片的 http(s) 链接。默认不下载，直接把 URL 交给多模态 API 由其服务端获取"
                    },
                    "description": {
                        "type": "string",
                        "description": "（可选）描述你想从这个图片读到什么。如果配置了多模态辅助模型（mul_api_key/mul_base_url/mul_model/mul_protocol），将调用辅助多模态模型识别图片并返回文本描述；不传则按原方式注入多模态图片消息"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_main_model_multimodal",
            "description": "强制设置当前主模型是否支持读图（多模态）的状态。这是一个手动开关：当主模型被误判为不支持读图、但其实能看图时，传 supported=True 强制复位为支持读图，之后图片会直接注入给主模型看；若用户明确知道当前大模型不支持读图，可传 supported=False 主动标记为不支持，之后图片会交给辅助模型识别或跳过。",
            "parameters": {
                "type": "object",
                "properties": {
                    "supported": {
                        "type": "boolean",
                        "description": "true=主模型支持读图（强制复位为支持读图）；false=主模型不支持读图（标记为不支持读图）"
                    }
                },
                "required": ["supported"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "start_bg_task",
            "description": "在后台启动一条长耗时命令并立即返回（不阻塞当前会话，适合安装工具、编译大型项目、长时间下载/测试等）。返回 JSON：success、task_id、pid、status(running)、shell、stdout_path、stderr_path。拿到 task_id 后可用 bg_task_status 实时查询状态/输出、bg_task_kill 终止；任务结束后 agent 会自动收到通知并汇报。输出实时写入临时文件（避免管道缓冲导致延迟），bg_task_status 可随时看到最新输出；但注意部分程序（如 python 不带 -u）自身会缓冲输出，如需实时请给命令加 -u 或重定向到文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要在后台执行的完整命令行"
                    },
                    "force_use_bash": {
                        "type": "boolean",
                        "description": "选填，默认 false。为 true 时强制以 bash 执行（等价于 bash -c '命令'，不经 cmd 解析，单引号可用）；若系统没有 bash 会返回报错，请改回 false 或把命令写入脚本文件再执行"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bg_task_status",
            "description": "查询后台任务状态和已收集的输出（非阻塞）。返回 JSON：task_id、status(running/done/failed/killed/error)、returncode、running_seconds、stdout、stderr、command。任务未完成时输出为已收集的部分；任务完成后输出完整（超 200KB 自动截断）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "后台任务 id（由 start_bg_task 返回）"
                    }
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bg_task_kill",
            "description": "终止一个正在运行的后台任务（杀死整个进程树）。任务已结束或不存在会返回错误。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": "后台任务 id"
                    }
                },
                "required": ["task_id"]
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
    """调用 MCP 工具（由 tool_func_map 路由）
    
    MCP 工具返回的结果现在是结构化 dict: {"text": "...", "images": [...]}
    如果包含图片数据，会在返回 JSON 中加入 _mcp_images 字段，
    供主循环检测并注入多模态 user 消息（类似 read_image 的处理方式）。
    """
    try:
        mcp = get_mcp_manager()
        result = mcp.call_tool(tool_name, arguments)
        # result 现在是 dict: {"text": "...", "images": [{"base64": "...", "mimeType": "..."}]}
        if isinstance(result, dict):
            text = result.get("text", "")
            images = result.get("images", [])
            response = {"success": 1, "result": text}
            if images:
                response["_mcp_images"] = images
                print(f"   📸 MCP 工具 {tool_name} 返回了 {len(images)} 张图片", flush=True)
            return json.dumps(response)
        else:
            # 兼容旧格式（纯字符串）
            return json.dumps({"success": 1, "result": str(result)})
    except Exception as e:
        err_msg = f"MCP 工具 {tool_name} 调用失败: {e}"
        print(f"   ⚠️ {err_msg}", flush=True)
        return json.dumps({"success": 0, "err": err_msg})
# ============================================================
# MCP 进程控制工具 —— 让大模型可以查看和控制 MCP 服务状态
# ============================================================

def mcp_list_servers():
    """列出所有已配置的 MCP 服务器及其运行状态
    
    返回每个服务器的名称、传输层、是否启用、是否自动连接、
    当前是否在运行、工具数量等信息。
    """
    try:
        mcp = get_mcp_manager()
        servers = mcp.list_servers_config()
        return json.dumps({"success": 1, "servers": servers})
    except Exception as e:
        return json.dumps({"success": 0, "err": str(e)})


def mcp_connect_server(name):
    """连接/启动指定的 MCP 服务器
    
    如果服务器已经在运行，会先断开再重新连接。
    连接成功后会自动注册该服务器提供的工具到工具列表中。
    
    参数:
        name: 要连接的 MCP 服务器名称（与 mcp_config.json 中配置的 name 一致）
    """
    global tools
    try:
        mcp = get_mcp_manager()
        result = mcp.connect_server(name)
        
        if result.get("success"):
            # 连接成功后，刷新 MCP 工具注册
            _refresh_mcp_tools()
        
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": 0, "err": str(e)})


def mcp_disconnect_server(name):
    """断开指定的 MCP 服务器
    
    停止其进程、释放资源，并从工具列表中移除该服务器提供的工具。
    
    参数:
        name: 要断开的 MCP 服务器名称
    """
    global tools
    try:
        mcp = get_mcp_manager()
        result = mcp.disconnect_server(name)
        
        if result.get("success"):
            # 断开后刷新 MCP 工具注册（移除对应的工具）
            _refresh_mcp_tools()
        
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": 0, "err": str(e)})


def mcp_restart_server(name):
    """重启指定的 MCP 服务器
    
    先断开再重新连接。适用于 MCP 进程崩溃后重新启动。
    重启成功后会自动注册该服务器提供的工具。
    
    参数:
        name: 要重启的 MCP 服务器名称
    """
    global tools
    try:
        mcp = get_mcp_manager()
        result = mcp.restart_server(name)
        
        if result.get("success"):
            _refresh_mcp_tools()
        
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"success": 0, "err": str(e)})


def _refresh_mcp_tools():
    """刷新 MCP 工具的路由注册和 tools 列表
    
    在 MCP 服务器连接/断开/重启后调用，同步 tool_func_map 和 tools 列表。
    """
    global tools, tool_func_map
    
    mcp = get_mcp_manager()
    
    # 1. 清除旧的 MCP 工具路由（保留非 MCP 工具）
    # 找出所有当前 MCP 服务器的工具名
    old_mcp_tool_names = set()
    for server in mcp.servers.values():
        if server.connected:
            for tool in server.tools:
                old_mcp_tool_names.add(tool["function"]["name"])
    
    # 从 tool_func_map 中移除旧的 MCP 工具（注意保留 mcp_ 开头的控制工具本身）
    keys_to_remove = []
    for key in tool_func_map:
        if key in old_mcp_tool_names:
            keys_to_remove.append(key)
    for key in keys_to_remove:
        del tool_func_map[key]
    
    # 2. 重新注册当前已连接服务器的工具
    mcp_tools = mcp.get_all_tools()
    for t in mcp_tools:
        tool_name = t["function"]["name"]
        if tool_name not in tool_func_map:
            tool_func_map[tool_name] = lambda name=tool_name, **kw: mcp_call_tool(name, **kw)
    
    # 3. 同步 tools 列表：重建为 [静态工具] + [当前MCP工具]
    # 先找出 tools 中哪些是静态工具（非MCP工具）
    static_tool_names = set()
    for t in tools:
        # 静态工具的特征：不是通过 mcp_call_tool 路由的
        # 用白名单方式：不在 old_mcp_tool_names 和 新 mcp_tools 中的就是静态工具
        pass
    
    # 更简单的方式：保留 tools 中所有非MCP工具，再追加当前MCP工具
    # 获取当前所有MCP工具名
    current_mcp_names = set()
    for t in mcp_tools:
        current_mcp_names.add(t["function"]["name"])
    current_mcp_names.update(old_mcp_tool_names)  # 也包含旧的可能还在tools里的
    
    # 保留非MCP工具
    static_tools = [t for t in tools if t["function"]["name"] not in current_mcp_names]
    
    # 重建 tools
    tools = static_tools + mcp_tools
    
    print(f"   🔄 MCP 工具列表已刷新: {len(static_tools)} 个静态工具 + {len(mcp_tools)} 个 MCP 工具", flush=True)


# 终止型工具集合：执行这些工具后会直接结束本轮工具调用循环
# 因为这类工具（如 compress）会修改全局 messages 状态，
# 后续的 tool response 追加和 API 调用会基于错误的上下文继续执行
TERMINAL_TOOLS = {"compress"}

# 图片工具集合：执行这些工具后不会追加 tool response，
# 而是改为注入一条 role:user 的多模态消息（包含图片 base64）
IMAGE_TOOLS = {"read_image"}


# read_image 单张图片大小上限（字节）。默认 5MB，可由 ai_agent_config.json 的
# max_image_size_byte 覆盖（见 _refresh_tool_limits）。注意：这里读的是模块全局，
# 函数内部每次都重新取值，所以运行时刷新立即生效。
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 图片最大 5MB = 5242880 字节


def _refresh_tool_limits():
    """从 ai_agent_config.json 读取工具相关阈值，刷新本模块的全局常量。

    对应配置项：
      max_image_size_byte  read_image 单张图片大小上限（单位字节；
                           不带单位即按字节算，也支持 k/kb、m/mb 后缀）

    由主程序在启动时（load_config 之后）调用；读取失败或值非法时保持默认值。
    """
    global MAX_IMAGE_SIZE
    try:
        cfg = _RT.load_config()
    except Exception:
        return
    MAX_IMAGE_SIZE = _RT._parse_size_bytes(cfg.get("max_image_size_byte"), MAX_IMAGE_SIZE)


def set_main_model_multimodal(supported):
    """强制设置当前主模型是否支持读图（多模态）的状态。

    这是一个"手动开关"，供主模型或用户在读图相关逻辑误判/需要时，主动强制覆盖
    _main_model_no_multimodal 标志，而不必等代码自动判断。

    参数 supported：
      - True  → 主模型【支持】读图（_main_model_no_multimodal=False）：后续图片直接注入给主模型看；
      - False → 主模型【不支持】读图（_main_model_no_multimodal=True）：后续图片交给辅助模型/跳过。

    典型用途：
      - 主模型被误判为"不支持读图"后，若它其实能看图，可调用本工具传 supported=True 强制复位；
      - 若用户明确知道当前大模型不支持读图，可让本模型调用本工具传 supported=False 主动标记。
    """

    # 兼容可能以字符串传入的布尔值
    if isinstance(supported, str):
        supported = supported.strip().lower() in ("true", "1", "yes", "y", "是", "支持")
    supported = bool(supported)

    _RT._main_model_no_multimodal = not supported
    status = "支持读图" if supported else "不支持读图"
    print(
        f"   🔧 [set_main_model_multimodal] 主模型读图状态已强制设为：{status} "
        f"(_RT._main_model_no_multimodal={_RT._main_model_no_multimodal})",
        flush=True
    )
    return json.dumps({
        "success": 1,
        "supported": supported,
        "multimodal_supported": supported,
        "message": f"主模型读图状态已强制设置为：{status}。后续对图片的处理将按此状态执行。",
    }, ensure_ascii=False)


def read_image(filepath=None, url=None, description=""):
    """读取图片（本地文件或网络 URL 二选一），供主循环注入多模态消息或以子 Agent 识别。

    参数（filepath 与 url 二选一，必须且只能提供一个）：
      - filepath: 本地图片文件路径（绝对/相对均可）
      - url:      网络图片的 http(s) 链接。默认不下载，直接把 URL 交给多模态 API 由其服务端获取；
                  仅当需要 base64（如辅助模型读图且直读失败）时才本地回退下载。
      - description: 可选。描述你想从图片读到什么。若配置了多模态辅助模型（mul_api_key 等），
                     将调用辅助模型识别并直接返回文本描述。

    工具本身不修改全局 messages，调用结果由主循环特殊处理：
      - 本地源：转 base64，注入 role:user 的 data URL 多模态消息；
      - url 源：直接把 http 链接作为多模态消息的 image_url.url（免下载）。
    """
    try:
        import base64
        import mimetypes
    except ImportError:
        return json.dumps({"success": 0, "err": "缺少 base64 或 mimetypes 模块"})

    # —— 参数二选一校验 ——
    filepath = filepath.strip() if isinstance(filepath, str) else filepath
    url = url.strip() if isinstance(url, str) else url
    has_file = bool(filepath)
    has_url = bool(url)
    if has_file and has_url:
        return json.dumps({"success": 0, "err": "filepath 与 url 只能二选一，请只传其中一个"})
    if not has_file and not has_url:
        return json.dumps({"success": 0, "err": "必须提供 filepath（本地图片）或 url（网络图片）之一"})

    # —— URL 源：默认免下载，交由多模态 API 服务端获取 ——
    if has_url:
        return _read_image_from_url(url, description)

    filepath = _normalize_path(filepath)
    
    if not os.path.exists(filepath):
        return json.dumps({"success": 0, "err": f"文件不存在: {filepath}"})
    
    # 检查文件大小
    file_size = os.path.getsize(filepath)
    if file_size > MAX_IMAGE_SIZE:
        return json.dumps({
            "success": 0, 
            "err": f"图片过大: {file_size // 1024}KB，最大支持 {MAX_IMAGE_SIZE / 1024 / 1024:g}MB"
        })
    
    # 猜测 MIME 类型
    mime_type, _ = mimetypes.guess_type(filepath)
    if not mime_type or not mime_type.startswith("image/"):
        return json.dumps({"success": 0, "err": f"不支持的文件类型或不是图片: {filepath} (mime: {mime_type})"})
    
    try:
        with open(filepath, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        return json.dumps({"success": 0, "err": f"读取文件失败: {e}"})
    
    b64_size_kb = len(b64) * 3 // 4 // 1024  # base64 解码后的实际大小
    
    # ---- 🔮 多模态辅助模型（子 Agent 读图）模式 ----
    # 若配置了 mul_* 字段且传入了 description，则调用辅助多模态模型识别图片，
    # 直接返回文本描述（不再注入多模态消息，兼容不支持多模态的主模型）
    mul_config = _build_mul_config()
    if mul_config is not None and isinstance(description, str) and description.strip():
        return _read_image_via_subagent(
            filepath, mime_type, b64, b64_size_kb, description, mul_config
        )
    
    print(
        f"\n📷 图片读取成功!\n"
        f"   文件: {filepath}\n"
        f"   类型: {mime_type}\n"
        f"   大小: {b64_size_kb}KB\n"
        f"   base64: {len(b64)} 字符\n",
        flush=True
    )
    
    return json.dumps({
        "success": 1,
        "source": "local",
        "filepath": filepath,
        "mime": mime_type,
        "base64": b64,
        "size_kb": b64_size_kb,
        "message": f"图片已读取，大小约 {b64_size_kb}KB"
    })


def _read_image_from_url(url, description=""):
    """按 URL 读取网络图片，默认不下载。

    - 主模型支持多模态：返回 url 源，由主循环把 http 链接作为 image_url.url，
      交给多模态 API 在服务端获取（无需本地落盘）。
    - 配置了多模态辅助模型且传了 description：调用辅助模型识别；优先把 URL 直接
      交给辅助模型，若其无法获取该 URL，则本地下载后回退为 base64 再识别。
    """
    import mimetypes

    low = url.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return json.dumps({"success": 0, "err": f"url 必须是 http(s) 链接: {url}"})

    # 尽力从扩展名猜 mime（服务端会按实际内容解码，猜不准也不影响）
    mime_type, _ = mimetypes.guess_type(url.split("?")[0].split("#")[0])
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/png"

    # ---- 🔮 辅助多模态模型读图模式 ----
    mul_config = _build_mul_config()
    if mul_config is not None and isinstance(description, str) and description.strip():
        res = _read_image_via_subagent(
            url, mime_type, None, 0, description, mul_config, url_ref=url
        )
        # 辅助模型抓不到该 URL 时，回退：本地下载 → base64 → 再识别一次
        try:
            ok = bool(json.loads(res).get("success"))
        except (json.JSONDecodeError, TypeError):
            ok = False
        if not ok:
            print(f"   ↩️ 辅助模型无法直接获取该 URL，回退本地下载: {url}", flush=True)
            dl = _download_image_to_temp(url)
            if dl.get("success"):
                return _read_image_via_subagent(
                    url, dl["mime"], dl["base64"], dl["size_kb"],
                    description, mul_config
                )
        return res

    print(
        f"\n🌐 网络图片已按 URL 读取（免下载，交由多模态 API 服务端获取）\n"
        f"   URL: {url}\n",
        flush=True
    )
    return json.dumps({
        "success": 1,
        "source": "url",
        "url": url,
        "filepath": url,
        "mime": mime_type,
        "size_kb": 0,
        "message": f"已按 URL 读取网络图片（免下载）: {url}",
    })


def _download_image_to_temp(url, max_size=None):
    """把网络图片下载后转为 base64（URL 直读失败时的回退手段）。

    返回 {success, base64, mime, size_kb}；失败返回 {success: 0, err}。
    """
    import base64
    import mimetypes
    import urllib.request

    if max_size is None:
        max_size = MAX_IMAGE_SIZE

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            mime_type = resp.headers.get_content_type() or ""
            data = resp.read(max_size + 1)
            if len(data) > max_size:
                return {"success": 0, "err": f"图片过大（超过 {max_size / 1024 / 1024:g}MB）"}
            if not mime_type or not mime_type.startswith("image/"):
                guess, _ = mimetypes.guess_type(url.split("?")[0])
                mime_type = guess or "image/png"
    except Exception as e:
        return {"success": 0, "err": f"下载失败: {e}"}

    return {
        "success": 1,
        "base64": base64.b64encode(data).decode("utf-8"),
        "mime": mime_type,
        "size_kb": len(data) // 1024,
    }


def _build_mul_config():
    """从主配置中提取 mul_ 前缀字段，组装成标准结构的辅助模型配置 dict。

    返回标准结构：{"api_key", "base_url", "model", "protocol"}，
    可直接传给 call_api 的 config_override。
    若未配置多模态辅助模型（mul_api_key 或 mul_model 缺失/为空），返回 None。
    """
    config = _RT.load_config()
    mul_api_key = (config.get("mul_api_key") or "").strip()
    mul_model = (config.get("mul_model") or "").strip()
    if not mul_api_key or not mul_model:
        return None

    mul_base_url = (config.get("mul_base_url") or "").strip()
    if not mul_base_url:
        mul_base_url = (config.get("base_url") or "").strip() or _RT.DEFAULT_CONFIG["base_url"]

    mul_protocol = (config.get("mul_protocol") or "").strip().lower()
    if not mul_protocol:
        mul_protocol = (config.get("protocol") or "openai").strip().lower() or "openai"

    return {
        "api_key": mul_api_key,
        "base_url": mul_base_url,
        "model": mul_model,
        "protocol": mul_protocol,
        # 辅助模型也有独立的附加参数透传出口（思考等级等），走 mul_extra_* 配置；
        # 同样剔除 messages / tools 这类由程序管理的结构性字段
        "extra_headers": _RT._sanitize_extra_fields(
            config.get("mul_extra_headers"), _RT._EXTRA_HEADER_RESERVED
        ),
        "extra_body": _RT._sanitize_extra_fields(
            config.get("mul_extra_body"), _RT._EXTRA_BODY_RESERVED
        ),
    }


def _read_image_via_subagent(filepath, mime_type, b64, b64_size_kb, description, mul_config, url_ref=None):
    """通过多模态辅助模型（子 Agent）识别图片内容，返回文本描述。

    类似 load_skill 的子 Agent 模式，但更简单：
    不带工具、单轮 API 调用即可拿到结果。
    返回的 JSON 含 mode="subagent"，主循环据此按普通工具结果处理（不注入多模态消息）。
    """
    if _RT.interrupted:
        return json.dumps({"success": 0, "err": "用户中断了魔法吟唱"})

    mul_model = mul_config.get("model", "?")
    print(
        f"\n🔮 多模态辅助模型读图子Agent启动...\n"
        f"   模型: {mul_model}\n"
        f"   图片: {filepath} ({b64_size_kb}KB)\n"
        f"   问题: {description[:200]}{'...' if len(description) > 200 else ''}\n",
        flush=True
    )

    sub_system_prompt = (
        "你是一个图片识别助手。用户会给你一张图片和一个具体的问题描述。\n"
        "请仔细观察图片内容，并根据问题描述给出准确、详细的回答。\n"
        "要求：\n"
        "1. 仔细观察图片中的每一个细节，不要凭空猜测或编造\n"
        "2. 如果图片中有文字，请准确读出\n"
        "3. 回答要直接、简洁但完整，不要客套\n"
        "4. 如果图片内容无法回答该问题，请如实说明"
    )

    # 图片来源：url_ref（http 链接，免下载，交给 API 服务端获取）优先，
    # 否则用本地 base64 拼成 data URL
    _img_url_str = url_ref if url_ref else f"data:{mime_type};base64,{b64}"
    sub_messages = [
        {"role": "system", "content": sub_system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"问题：{description}\n\n请根据图片内容回答。"},
                {
                    "type": "image_url",
                    "image_url": {"url": _img_url_str}
                }
            ]
        }
    ]

    try:
        sub_msg, _, _sub_usage = _RT.call_api(sub_messages, tools=None, config_override=mul_config, guard_multimodal=False)
    except _RT.UserInterrupt:
        print("   ⏹️ 用户中断了读图辅助模型调用\n", flush=True)
        return json.dumps({"success": 0, "err": "用户中断"})
    except Exception as e:
        err_msg = f"多模态辅助模型调用失败: {e}"
        print(f"   ⚠️ {err_msg}\n", flush=True)
        return json.dumps({"success": 0, "err": err_msg})

    result_text = (sub_msg.get("content") or "").strip()
    if not result_text:
        return json.dumps({"success": 0, "err": "多模态辅助模型返回了空结果"})

    print(
        f"   ✅ 子Agent读图完成: {result_text[:100]}{'...' if len(result_text) > 100 else ''}\n",
        flush=True
    )

    return json.dumps({
        "success": 1,
        "mode": "subagent",
        "result": result_text,
        "source_model": mul_model,
        "filepath": filepath,
        "mime": mime_type,
        "size_kb": b64_size_kb,
        "message": f"多模态辅助模型 ({mul_model}) 已识别图片，结果见 result 字段"
    })


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


def _detect_bash_path():
    """探测可用的 bash 解释器路径（Windows 专用）。找到返回路径字符串，找不到返回 None。

    排除 WSL 的 System32\\bash.exe —— 那是 Linux 子系统启动器，-c 会进 Linux 环境，
    用它执行 Windows 命令会得到完全不同的语义。
    结果做模块级缓存，只探测一次。
    """
    global _bash_path_probed, _bash_path_cache
    if _bash_path_probed:
        return _bash_path_cache or None
    _bash_path_probed = True

    if sys.platform != "win32":
        # 非 Windows：系统自带 /bin/sh，无需探测，run_bash 直接 shell=True 即可
        _bash_path_cache = ""
        return None

    # 1) 先查已知的 Git/MSYS2/Cygwin 安装路径（绕开 PATH 顺序坑）
    candidates = [
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        r"C:\msys64\usr\bin\bash.exe",
        r"C:\cygwin64\bin\bash.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            _bash_path_cache = c
            return c

    # 2) 兜底：PATH 里找 bash / sh，但排除 System32 下的（那是 WSL/系统内置启动器，语义不同）
    import shutil
    for name in ("bash", "sh"):
        found = shutil.which(name)
        if not found:
            continue
        low = found.lower().replace("\\", "/")
        if "/system32/" in low:
            continue
        _bash_path_cache = found
        return found

    _bash_path_cache = ""
    return None


_bash_path_probed = False
_bash_path_cache = None


def run_bash(command, timeout=10, force_use_bash=False):
    """执行一条命令，打印人类可读结果，返回 json.dumps 后的字符串

    参数:
        command: 要执行的命令字符串
        timeout: 超时秒数
        force_use_bash: 选填，默认为 False。为 True 时强制以 bash 执行
            （等价于 ["bash", "-c", command]，不经过 cmd 解析，单引号可用）；
            若系统没有 bash 可执行文件，会把找不到 bash 的报错原样返回，
            由调用方（大模型）自行决定改用 cmd 语法或写脚本文件执行。
            为 False 时自动模式：Windows 下若探测到 bash 则用 bash，否则降级 cmd；
            非 Windows 使用系统默认 shell（/bin/sh）。

    使用 Popen + 进程组 + watchdog 线程实现真正的超时机制，
    即使用户跑了交互命令（top、python、read 等）也能强制结束。
    """
    # 🛡️ 类型护盾！防止 DeepSeek 模型乱传参数
    if not isinstance(command, str) or not command.strip():
        result_dict = {"stdout": "", "stderr": f"无效的command参数: {repr(command)}", "code": -2, "shell": ""}
        print(f"\n{result_dict['stderr']}\n", flush=True)
        return json.dumps(result_dict)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 10
    # force_use_bash 规范化：防止模型传 "true"/"false"/1/0 等
    if isinstance(force_use_bash, str):
        force_use_bash = force_use_bash.strip().lower() in ("1", "true", "yes", "on")
    else:
        force_use_bash = bool(force_use_bash)

    # 确定实际使用的 shell（用于返回给大模型 + 防"闹鬼"提示）
    used_shell = "sh"      # 非 Windows 默认
    bash_path = None       # 自动模式下探测到的 bash 路径
    if force_use_bash:
        used_shell = "bash"
    elif sys.platform == "win32":
        bash_path = _detect_bash_path()
        used_shell = "bash" if bash_path else "cmd"

    # 如果已经被中断，直接跳过
    if _RT.interrupted:
        result_dict = {"stdout": "", "stderr": "用户中断了魔法吟唱", "code": -1, "shell": used_shell}
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
        if force_use_bash or (sys.platform == "win32" and bash_path):
            # 用 bash 执行：不经过 cmd 解析，单引号/管道/重定向都由 bash 处理
            # force_use_bash=True 时用 "bash" 名字走 PATH 解析；找不到会抛 FileNotFoundError，
            # 由下方的 except 分支把报错原样返回给大模型。
            bash_cmd = ["bash", "-c", command] if force_use_bash else [bash_path, "-c", command]
            proc = subprocess.Popen(
                bash_cmd,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **extra_kwargs
            )
        else:
            # 非 Windows（/bin/sh）或 Windows 无 bash（降级 cmd）：走系统 shell
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
            "user_interrupted": False,
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
        except KeyboardInterrupt:
            # 🛡️ 用户按了 Ctrl+C 中断魔法吟唱！绝不能让它把整个 agent 干崩。
            # Ctrl+C 时 signal handler 已把 interrupted 置 True，这里捕获后做善后：
            # 杀掉子进程、读取残留输出，然后返回"用户中断"结果，程序继续存活。
            result_container["timed_out"] = False
            # 杀掉整个进程组（跟 watchdog 一样的杀法）
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=5
                    )
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            # 读取残留输出（短暂等待，失败就算了）
            try:
                stdout_data, stderr_data = proc.communicate(timeout=3)
                result_container["stdout"] = stdout_data
                result_container["stderr"] = stderr_data
            except (Exception, KeyboardInterrupt):
                pass
            # 中断结果留给下方统一处理（stderr 里说明是用户中断）
            result_container["user_interrupted"] = True
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
            except (Exception, KeyboardInterrupt):
                pass

        # 等待看门狗线程结束（最多等 2 秒）
        try:
            watchdog_thread.join(timeout=2)
        except KeyboardInterrupt:
            # 部分平台（尤其 Linux/CentOS）上阻塞等待 join 时收到 SIGINT
            # 会直接抛 KeyboardInterrupt。转成一致的“用户中断”，走下方
            # user_interrupted 分支优雅返回，而不是崩掉整个程序。
            result_container["user_interrupted"] = True
            try:
                # 若子进程还在，尝试结束它
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=5
                    )
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if result_container["timed_out"]:
            stdout = smart_decode(result_container["stdout"]) if result_container["stdout"] else ""
            stderr = smart_decode(result_container["stderr"]) if result_container["stderr"] else ""
            result_dict = {"stdout": stdout, "stderr": f"命令执行超时（{timeout}秒），已强制终止\n{stderr}".strip(), "code": -1, "shell": used_shell}
            print(
                f"魔法结果:\n    \n"
                f"魔法报错:\n    命令执行超时（{timeout}秒），已强制终止\n"
                f"退出状态: -1",
                flush=True
            )
            return json.dumps(result_dict)

        # 🛡️ 用户中断处理：Ctrl+C 中断了魔法吟唱，优雅返回而不是崩掉整个程序
        if result_container["user_interrupted"]:
            stdout = smart_decode(result_container["stdout"]) if result_container["stdout"] else ""
            stderr = smart_decode(result_container["stderr"]) if result_container["stderr"] else ""
            # stderr 里带上用户中断的说明，方便后续对话上下文理解
            interrupt_msg = "用户按 Ctrl+C 中断了命令执行"
            stderr = (interrupt_msg + ("\n" + stderr if stderr else "")) if stderr else interrupt_msg
            result_dict = {"stdout": stdout, "stderr": stderr, "code": -1, "shell": used_shell}
            print(
                "魔法结果:\n    \n"
                f"魔法报错:\n    {interrupt_msg}",
                flush=True
            )
            return json.dumps(result_dict)

        # 正常完成
        stdout = smart_decode(result_container["stdout"]) if result_container["stdout"] else ""
        stderr = smart_decode(result_container["stderr"]) if result_container["stderr"] else ""
        code = proc.returncode
        # 防"闹鬼"：cmd 模式下命令含单引号 + 空输出 + code0 → 主动提示引号问题，
        # 避免大模型以为是 -c 内容写错而反复试错
        if used_shell == "cmd" and code == 0 and not stdout.strip() and "'" in command:
            stderr = (
                (stderr + "\n" if stderr else "")
                + "[提示] 当前执行 shell 是 cmd，命令中含单引号 ' 但 cmd 不把单引号当引号；本命令无任何输出(code=0)，多半是引号未生效。"
                + " 例如 python -c 'print(1)' 应写成 python -c \"print(1)\"；若必须用 bash 语法，可调用 run_bash 时加 force_use_bash=true（需系统装有 bash）。"
            )
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
            "code": code,
            "shell": used_shell
        }
        print(
            "魔法结果:\n"
            f"    {stdout.replace(chr(10), chr(10)+'    ')}\n"
            f"    {'[输出较大，已截断至' + str(TOOL_HARD_CUTOFF//1024) + 'KB]' if stdout_orig_len > TOOL_HARD_CUTOFF else ''}"
            "\n魔法报错:\n"
            f"    {stderr.replace(chr(10), chr(10)+'    ')}\n"
            f"退出状态: {code}",
            flush=True
        )
        return json.dumps(result_dict)

    except FileNotFoundError as e:
        # 典型场景：force_use_bash=true 但系统没有 bash 可执行文件 → 把报错原样返回，
        # 让大模型自己决定改用 cmd 语法或写脚本文件执行
        detail = str(e)
        msg = f"命令执行异常（找不到可执行文件）: {detail}"
        if force_use_bash:
            msg += (
                "\n[提示] 你设置了 force_use_bash=true，但系统中没有可用的 bash 可执行文件，命令无法启动。可选做法："
                "① 去掉 force_use_bash（自动模式会降级到 cmd，注意 cmd 不支持单引号，请改用双引号）；"
                "② 把命令改写为 cmd 语法；③ 用 write_full_file 把脚本写成 .bat/.py 文件再执行。"
            )
            used_shell = "bash(缺失)"
        result_dict = {"stdout": "", "stderr": msg, "code": -2, "shell": used_shell}
        print(
            "魔法结果:\n    \n"
            "魔法报错:\n"
            f"    {msg}\n"
            "退出状态: -2",
            flush=True
        )
        return json.dumps(result_dict)
    except Exception as e:
        result_dict = {"stdout": "", "stderr": f"命令执行异常: {str(e)}", "code": -2, "shell": used_shell}
        print(
            "魔法结果:\n    \n"
            "魔法报错:\n"
            f"    命令执行异常: {str(e)}\n"
            "退出状态: -2",
            flush=True
        )
        return json.dumps(result_dict)


# ============================================================
#  后台任务管理器 —— 长耗时命令异步执行
#  用法：
#    start_bg_task(command)      → 立即返回 task_id，不阻塞当前会话
#    bg_task_status(task_id)     → 查询状态/已收集输出（非阻塞）
#    bg_task_kill(task_id)       → 终止任务（杀整个进程树）
#  任务结束后自动进入完成队列，主循环在空闲点注入通知消息并驱动模型汇报，
#  用户无需等待、agent 也能"主动开口"汇报结果。
# ============================================================

_bg_tasks = {}                 # task_id -> task_info dict
_bg_task_seq = 0               # 递增任务 id
_bg_completed = queue.Queue()  # 已完成/失败/被杀 的 task_id 通知队列
_bg_lock = threading.Lock()
# 本 agent 专属的日志目录（懒创建）。用 tempfile.TemporaryDirectory（mkdtemp）：
# ① 目录名带随机后缀，OS 原子创建 → 多 agent 各自独立目录，task_id 从 1 计数也不会撞文件；
# ② agent 退出时 cleanup() 自动删除整个目录 → 不留垃圾日志。
_bg_tmpdir = None
# 完成通知注入对话时最多携带的输出尾部字节数
_BG_NOTIFY_TAIL = 4 * 1024              # 4KB


def _bg_ensure_tmpdir():
    """懒创建本 agent 专属日志目录（线程安全）。返回 TemporaryDirectory 对象。"""
    global _bg_tmpdir
    if _bg_tmpdir is None:
        with _bg_lock:
            if _bg_tmpdir is None:
                _bg_tmpdir = tempfile.TemporaryDirectory(prefix="marisa_bg_")
    return _bg_tmpdir


def _bg_next_task_id():
    """分配递增的任务 id（线程安全）"""
    global _bg_task_seq
    with _bg_lock:
        _bg_task_seq += 1
        return _bg_task_seq


def _bg_notify(task_id):
    """把 task_id 放入完成通知队列（去重：wait 线程和 kill 可能同时触发）"""
    with _bg_lock:
        info = _bg_tasks.get(task_id)
        if info is None or info.get("notified"):
            return
        info["notified"] = True
        _bg_completed.put(task_id)


def _bg_log_path(task_id, kind):
    """后台任务输出文件路径（本 agent 专属临时目录内，文件名带 task_id）。

    关键设计：子进程 stdout/stderr 不走管道而是直接写文件——
    管道会让子进程 stdio 走全缓冲（输出攒到缓冲区满或进程退出才可见），
    而写文件是即时系统调用，bash 的 echo / 脚本逐行输出实时落盘，
    bg_task_status 随时可读到最新输出。

    多 agent 隔离：目录由 mkdtemp 唯一创建（marisa_bg_xxxxxx），
    每个 agent 进程写自己的目录，task_id 重复也不会冲突。
    """
    d = _bg_ensure_tmpdir().name
    return os.path.join(d, f"task_{task_id}_{kind}.log")


def _bg_read_file(path, max_bytes):
    """读取后台任务输出文件。文件超限时只取末尾 max_bytes（最新输出）。"""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= max_bytes:
                f.seek(0)
                data = f.read()
                return smart_decode(data)
            f.seek(size - max_bytes)
            data = f.read()
            text = smart_decode(data)
            return f"…（输出过大，已截断，仅显示尾部 {max_bytes//1024}KB）\n{text}"
    except Exception:
        return ""


def _bg_wait_process(task_id, proc):
    """后台 wait 线程：等待进程结束 → 更新状态 → 入完成队列"""
    info = _bg_tasks.get(task_id)
    if info is None:
        return
    try:
        proc.wait()
        info["returncode"] = proc.returncode
        info["finish_time"] = time.time()
        # 若已被 bg_task_kill 标记为 killed，保持 killed，避免被进程返回码覆盖
        if info.get("status") != "killed":
            info["status"] = "done" if proc.returncode == 0 else "failed"
    except Exception:
        if info.get("status") != "killed":
            info["status"] = "error"
        info["finish_time"] = time.time()
    _bg_notify(task_id)
    print(f"\n🔔 后台任务 #{task_id} 已结束（exit={info['returncode']}）\n", flush=True)


def _bg_kill_process_tree(pid):
    """杀死整个进程树（Windows: taskkill /F /T；Unix: killpg）"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5
        )
    else:
        os.killpg(os.getpgid(pid), signal.SIGKILL)


def start_bg_task(command, force_use_bash=False):
    """在后台启动一条命令，立即返回 task_id，不阻塞当前会话。

    返回 json：success、task_id、pid、status("running")、shell。
    后续用 bg_task_status(task_id) 查询，任务完成时 agent 会自动收到通知。
    """
    if not isinstance(command, str) or not command.strip():
        return json.dumps({"success": 0, "task_id": None, "err": f"无效的command参数: {repr(command)}"})
    # force_use_bash 规范化（防止模型传 "true"/"false"/1/0 等）
    if isinstance(force_use_bash, str):
        force_use_bash = force_use_bash.strip().lower() in ("1", "true", "yes", "on")
    else:
        force_use_bash = bool(force_use_bash)

    # 确定实际使用的 shell（与 run_bash 同一套逻辑）
    used_shell = "sh"
    bash_path = None
    if force_use_bash:
        used_shell = "bash"
    elif sys.platform == "win32":
        bash_path = _detect_bash_path()
        used_shell = "bash" if bash_path else "cmd"

    # 分配 task_id 并创建输出文件：
    # stdout/stderr 直接写文件（不走管道）——避免管道全缓冲导致输出延迟到任务结束，
    # 文件写入是即时系统调用，bash echo / 脚本输出实时落盘，随时可查。
    task_id = _bg_next_task_id()
    out_path = _bg_log_path(task_id, "out")
    err_path = _bg_log_path(task_id, "err")

    # 后台任务要“免疫用户 Ctrl+C”：用户按 Ctrl+C 只应打断/停止前端对话，
    # 而不是借机把后台任务一起干掉。因此这里做三件事：
    #   - Unix：start_new_session=True，子进程进入新会话，不再随前台进程组收到终端 SIGINT
    #   - Windows bash：给命令前缀 trap '' INT，让 bash 及其后代忽略 SIGINT/控制台中断
    #   - Windows cmd：依赖 CREATE_NEW_PROCESS_GROUP 放入独立进程组
    # 另外统一把 stdin 置为 DEVNULL，后台任务不应占用控制台输入。
    extra_kwargs = {}
    if sys.platform == "win32":
        extra_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        extra_kwargs["start_new_session"] = True
    extra_kwargs["stdin"] = subprocess.DEVNULL

    # 记录给模型的原始 command（不带 signal 包装），保持报告干净
    orig_command = command
    spawn_command = command
    is_bash_spawn = False
    if force_use_bash or (sys.platform == "win32" and bash_path):
        is_bash_spawn = True
        # 让 bash 子树忽略 Ctrl+C（SIGINT 被 SIG_IGN 会被子进程继承）
        spawn_command = f"trap '' INT; {command}"

    try:
        out_f = open(out_path, "wb")
        err_f = open(err_path, "wb")
        if is_bash_spawn:
            bash_cmd = ["bash", "-c", spawn_command] if force_use_bash else [bash_path, "-c", spawn_command]
            proc = subprocess.Popen(
                bash_cmd, shell=False,
                stdout=out_f, stderr=err_f,
                **extra_kwargs
            )
        else:
            proc = subprocess.Popen(
                spawn_command, shell=True,
                stdout=out_f, stderr=err_f,
                **extra_kwargs
            )
        # 父进程关闭文件对象（子进程持有 fd 继续写入，不受影响）
        out_f.close()
        err_f.close()
    except Exception as e:
        return json.dumps({"success": 0, "task_id": task_id, "err": f"启动后台任务失败: {e}"})

    info = {
        "task_id": task_id,
        "command": orig_command,
        "pid": proc.pid,
        "proc": proc,            # 退出清理时需等待进程真正结束（Windows 句柄释放）
        "shell": used_shell,
        "status": "running",
        "returncode": None,
        "stdout_path": out_path,
        "stderr_path": err_path,
        "start_time": time.time(),
        "finish_time": None,
        "notified": False,
    }
    _bg_tasks[task_id] = info

    # wait 线程：等待进程结束 → 更新状态 → 入完成队列
    threading.Thread(target=_bg_wait_process, args=(task_id, proc), daemon=True).start()

    # 🔊 让用户在界面上能看到启动了哪个后台任务
    print(f"\n⏳ 后台任务 #{task_id} 已启动（PID {proc.pid}）\n    ⚙️ 命令: {orig_command}\n    📄 输出: {out_path}", flush=True)

    return json.dumps({
        "success": 1,
        "task_id": task_id,
        "pid": proc.pid,
        "status": "running",
        "shell": used_shell,
        "stdout_path": out_path,
        "stderr_path": err_path,
        "message": (
            f"后台任务 #{task_id} 已启动，输出实时写入文件（{out_path}）。"
            f"可用 bg_task_status(task_id={task_id}) 随时查看最新输出；任务完成后我会收到通知。"
        )
    })


def bg_task_status(task_id):
    """查询后台任务状态和已收集的输出（非阻塞）"""
    try:
        task_id = int(task_id)
    except (TypeError, ValueError):
        return json.dumps({"success": 0, "err": f"无效的task_id: {repr(task_id)}"})
    info = _bg_tasks.get(task_id)
    if info is None:
        active = [t for t, i in _bg_tasks.items() if i.get("status") == "running"]
        return json.dumps({
            "success": 0,
            "err": f"找不到后台任务 #{task_id}。当前活跃任务: {active if active else '无'}"
        })

    # 从输出文件读取（输出实时落盘；文件超限时只取尾部最新内容）
    stdout = _bg_read_file(info.get("stdout_path"), TOOL_HARD_CUTOFF)
    stderr = _bg_read_file(info.get("stderr_path"), TOOL_HARD_CUTOFF)

    dur = None
    if info["finish_time"] is not None:
        dur = round(info["finish_time"] - info["start_time"], 1)
    elif info["start_time"]:
        dur = round(time.time() - info["start_time"], 1)

    # 运行中但无任何输出时，提示可能是程序自身缓冲或刚开始执行
    hint = ""
    if info["status"] == "running" and not stdout and not stderr:
        hint = "（任务运行中暂无输出，可能是程序自身缓冲或刚开始执行）"
    # 🔊 让用户能感知查询结果
    print(f"   🔍 后台任务 #{task_id} 状态: {info['status']}（命令: {info['command']}）", flush=True)

    return json.dumps({
        "success": 1,
        "task_id": task_id,
        "status": info["status"],
        "pid": info["pid"],
        "command": info["command"],
        "shell": info["shell"],
        "returncode": info["returncode"],
        "running_seconds": dur,
        "stdout": stdout,
        "stderr": stderr,
        "message": f"后台任务 #{task_id} 状态: {info['status']}{hint}"
    })


def bg_task_kill(task_id):
    """终止一个正在运行的后台任务（杀死整个进程树）"""
    try:
        task_id = int(task_id)
    except (TypeError, ValueError):
        return json.dumps({"success": 0, "err": f"无效的task_id: {repr(task_id)}"})
    info = _bg_tasks.get(task_id)
    if info is None:
        return json.dumps({"success": 0, "err": f"找不到后台任务 #{task_id}"})
    if info["status"] != "running":
        return json.dumps({"success": 0, "err": f"任务 #{task_id} 已不在运行（状态: {info['status']}）"})
    try:
        _bg_kill_process_tree(info["pid"])
        info["status"] = "killed"
        info["finish_time"] = time.time()
        _bg_notify(task_id)
        print(f"\n✂️  后台任务 #{task_id}（PID {info['pid']}）已终止\n", flush=True)
        return json.dumps({"success": 1, "task_id": task_id, "status": "killed",
                           "message": f"后台任务 #{task_id} 已被终止"})
    except Exception as e:
        return json.dumps({"success": 0, "err": f"终止任务 #{task_id} 失败: {e}"})


def _bg_build_event_message():
    """从完成队列取一个 task_id，构造 user 通知消息文本；队列空返回 None"""
    try:
        task_id = _bg_completed.get_nowait()
    except queue.Empty:
        return None
    info = _bg_tasks.get(task_id)
    if info is None:
        return None

    status = info["status"]
    code = info["returncode"]
    dur = None
    if info["finish_time"] is not None:
        dur = round(info["finish_time"] - info["start_time"], 1)
    elif info["start_time"]:
        dur = round(time.time() - info["start_time"], 1)

    stdout = _bg_read_file(info.get("stdout_path"), _BG_NOTIFY_TAIL)
    stderr = _bg_read_file(info.get("stderr_path"), _BG_NOTIFY_TAIL)
    stdout_tail = stdout[-_BG_NOTIFY_TAIL:] if stdout else ""
    stderr_tail = stderr[-_BG_NOTIFY_TAIL:] if stderr else ""
    tail_note = ""
    if len(stdout) > _BG_NOTIFY_TAIL or len(stderr) > _BG_NOTIFY_TAIL:
        tail_note = "（输出较长，仅展示尾部，可用 bg_task_status 查询完整输出）"

    text = (
        f"[后台任务通知] 后台任务 #{task_id} 已结束：\n"
        f"  命令: {info['command']}\n"
        f"  状态: {status}（exit code: {code if code is not None else 'N/A'}）\n"
        f"  运行时长: {dur} 秒\n"
        f"{tail_note}\n"
    )
    if stdout_tail:
        text += f"  --- 标准输出尾部 ---\n{stdout_tail}\n"
    if stderr_tail:
        text += f"  --- 标准错误尾部 ---\n{stderr_tail}\n"
    text += "请查看结果并继续后续工作（如需可调用 bg_task_status 获取完整输出，或发起新的后台任务）。"
    return text

def _bg_shutdown():
    """程序退出时终止仍在运行的后台任务，并清理本 agent 的日志目录（防止孤儿进程/垃圾日志）"""
    running = [tid for tid, i in _bg_tasks.items() if i.get("status") == "running"]
    if running:
        print(f"\n🔮 程序退出，正在终止 {len(running)} 个后台任务...", flush=True)
        for tid in running:
            try:
                info = _bg_tasks[tid]
                _bg_kill_process_tree(info["pid"])
                info["status"] = "killed"
                print(f"   ✂️  后台任务 #{tid}（PID {info['pid']}）已终止", flush=True)
            except Exception:
                pass
        # 等待子进程真正退出：Windows 上进程终止后句柄才释放，
        # 否则紧接着删除日志文件会因占用而失败（WinError 32）。
        for tid in running:
            proc = (_bg_tasks.get(tid) or {}).get("proc")
            if proc is not None:
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
    # 清理本 agent 的专属日志目录（TemporaryDirectory.cleanup → 删除整个目录）。
    # 句柄释放可能有延迟，重试几次兜底；仍失败则交给系统临时目录清理。
    global _bg_tmpdir
    if _bg_tmpdir is not None:
        for _ in range(3):
            try:
                _bg_tmpdir.cleanup()
                break
            except Exception:
                time.sleep(0.3)
        _bg_tmpdir = None


# 模块级注册退出清理：import 即生效。即使不调用 main()（测试/嵌入式场景），
# 后台任务与日志目录也能在进程退出时被正确清理（否则只剩 tempfile 的 finalizer，
# 它不会 kill 任务/等待句柄释放，日志目录会因文件占用删不掉）。
atexit.register(_bg_shutdown)

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
    # 如果已经被中断，直接跳过
    if _RT.interrupted:
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
    # 如果已经被中断，直接跳过
    if _RT.interrupted:
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
    if _RT.interrupted:
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
            f"{'[返回内容较大，已标记为大内容]' if len(numbered_content) > _RT.LARGE_CONTENT_THRESHOLD else ''}"
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

    if _RT.interrupted:
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
    old_len = len(_RT.messages)
    old_size = _RT.get_context_size(_RT.messages)
    _RT.messages = compressed_messages
    # 压缩后清空大内容计数器，因为 messages 已被完全替换
    _RT.large_content_counter.clear()

    # === 修复无限套娃压缩问题 ===
    # 压缩完成后，追加一条 assistant 回复作为确认，
    # 这样 messages 以 assistant 结尾，下次用户输入追加 user 消息时符合交替规则。
    # 同时也告诉 AI 不要主动再次调用 compress，除非用户再次明确要求压缩。
    _RT.messages.append({
        "role": "assistant",
        "content": (
            "📦 压缩完成！对话历史已压缩，请继续正常聊天。"
        )
    })

    new_len = len(_RT.messages)
    new_size = _RT.get_context_size(_RT.messages)

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
    filename = _normalize_path(filename)
    if _RT.interrupted:
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
        # unified_diff 只看到了从 start_line 截取的片段，所以 hunk 头里的
        # 相对行号是从 1 开始的。真实文件行号 = start_line - 1 + 相对行号。
        # 关键：多个 hunk 时每个 hunk 的相对行号不同（如 @@ -1,5 @@ 和 @@ -300,5 @@），
        # 必须分别加上 start_line-1 偏移，而不是全部硬编码成 start_line！
        fixed_diff_lines = []
        for line in diff_lines:
            match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if match:
                old_rel = int(match.group(1))      # 片段内旧文件相对起始行号（从1开始）
                old_count = match.group(2) or '1'
                new_rel = int(match.group(3))      # 片段内新文件相对起始行号（从1开始）
                new_count = match.group(4) or '1'
                # 整个 old_text 片段被 new_content 替换，start_line 是片段起始处，
                # 所以每个 hunk 的真实行号 = start_line - 1 + 相对行号
                old_abs = start_line - 1 + old_rel
                new_abs = start_line - 1 + new_rel
                line = f'@@ -{old_abs},{old_count} +{new_abs},{new_count} @@\n'
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
    if _RT.interrupted:
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
        # 🔧 修正行号：计算 old_content 在文件中的起始行号
        start_line = original_text[:pos].count('\n') + 1
        # 多个 hunk 时每个 hunk 的相对行号不同，必须分别加上 start_line-1 偏移，
        # 而不是全部硬编码成 start_line ！否则第二个 @@ 的行号会错。
        fixed_diff_lines = []
        for line in diff_lines:
            match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if match:
                old_rel = int(match.group(1))      # 片段内旧文件相对起始行号（从1开始）
                old_count = match.group(2) or '1'
                new_rel = int(match.group(3))      # 片段内新文件相对起始行号（从1开始）
                new_count = match.group(4) or '1'
                # old_content 被 new_content 替换，start_line 是替换起始处，
                # 每个 hunk 的真实行号 = start_line - 1 + 相对行号
                old_abs = start_line - 1 + old_rel
                new_abs = start_line - 1 + new_rel
                line = f'@@ -{old_abs},{old_count} +{new_abs},{new_count} @@\n'
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
    """扫描指定技能根目录，返回技能清单，兼容两种主流形态：

    1. 扁平型（向后兼容）：<dir>/<技能名>.md           -> 技能名=文件名
    2. 目录型（主流 Claude Code / OpenHands / ppt-master）：
       <dir>/<技能名>/SKILL.md                          -> 技能名=目录名

    返回列表，每个元素为 dict: {name, kind, root}
      - kind: "file"  扁平型，root 为该 .md 文件所在目录
      - kind: "dir"   目录型，root 为该技能目录
    """
    if not dir_path or not os.path.isdir(dir_path):
        return []
    result = []
    try:
        for f in sorted(os.listdir(dir_path)):
            full = os.path.join(dir_path, f)
            # 扁平型：直接 .md 文件
            if os.path.isfile(full) and f.endswith(".md"):
                result.append({"name": f[:-3], "kind": "file", "root": dir_path})
            # 目录型：子目录且含 SKILL.md（大小写不敏感，兼容 Windows/macOS）
            elif os.path.isdir(full):
                skill_md = _find_skill_md(full)
                if skill_md:
                    result.append({"name": f, "kind": "dir", "root": full})
        return result
    except Exception:
        return []


def _find_skill_md(skill_root):
    """在技能目录中查找 SKILL.md，大小写不敏感地匹配主流的 SKILL.md 命名。"""
    if not os.path.isdir(skill_root):
        return None
    try:
        for entry in os.listdir(skill_root):
            if entry.lower() == "skill.md":
                p = os.path.join(skill_root, entry)
                if os.path.isfile(p):
                    return p
    except Exception:
        pass
    return None


def _merge_skills():
    """合并两个技能根目录的技能清单，启动目录优先（同名覆盖）。

    返回列表，每个元素为 dict: {name, kind, root}
    - kind: "file"  扁平型 .md 技能
    - kind: "dir"   目录型 SKILL.md 技能
    """
    cwd_skills = _scan_skills(CWD_SKILLS_DIR)
    code_skills = _scan_skills(CODE_SKILLS_DIR)

    # 启动目录优先：同名技能，启动目录版本覆盖代码目录版本
    # 用 dict 按技能名去重，code 先插入，cwd 后插入覆盖
    merged = {}
    for s in code_skills:
        merged[s["name"]] = s
    for s in cwd_skills:
        merged[s["name"]] = s

    # 按名字排序，保持稳定
    return [merged[k] for k in sorted(merged.keys())]


def _resolve_skill_file(skill_info):
    """根据技能信息返回实际技能入口文件路径（支持 dict 或字符串两种入参）。

    - skill_info 为 dict（来自 _merge_skills）：按 kind 解析
      * kind="file" -> <root>/<name>.md
      * kind="dir"  -> <root>/SKILL.md（大小写不敏感）
    - skill_info 为字符串（老代码传技能名）：向后兼容，自动探测两种形态
    """
    if isinstance(skill_info, dict):
        name = skill_info.get("name")
        kind = skill_info.get("kind")
        root = skill_info.get("root")
        if not name or not root:
            return None
        if kind == "dir":
            return _find_skill_md(root) or None
        # file 形态
        if CWD_SKILLS_DIR and os.path.normpath(root) == os.path.normpath(CWD_SKILLS_DIR):
            fpath = os.path.join(root, f"{name}.md")
            if os.path.isfile(fpath):
                return fpath
        fpath = os.path.join(root, f"{name}.md")
        return fpath if os.path.isfile(fpath) else None

    # 字符串兼容分支：直接在两个 skills 根目录下探测两种形态
    skill_name = skill_info
    for base in (CWD_SKILLS_DIR, CODE_SKILLS_DIR):
        if not base:
            continue
        # 目录型
        sub = os.path.join(base, skill_name)
        skill_md = _find_skill_md(sub)
        if skill_md:
            return skill_md
        # 扁平型
        fpath = os.path.join(base, f"{skill_name}.md")
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
    # global messages

    if _RT.interrupted:
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
    merged_info = _merge_skills()
    available_with_desc = [_skill_desc(s) for s in merged_info]
    skills_list_str = "\n".join(f"  - {s}" for s in available_with_desc)
    sub_system_prompt = (
        "你是一个技能检索助手。你的任务是根据用户描述的需求，从技能库中找出最匹配的技能。\n"
        "\n"
        "可用技能（位于 skills/ 目录下，兼容两种形态：扁平 .md 文件，或含 SKILL.md 的目录型技能）"
        "，括号中为可用的技能描述/用途：\n"
        f"{skills_list_str}\n"
        "\n"
        "请按以下步骤操作：\n"
        "1. 分析用户的需求描述，判断需要哪些技能\n"
        "2. 使用 list_skills 工具查看可用技能列表（确认最新情况）\n"
        "3. 如果不确定某个技能的内容是否匹配，可以使用 read_full_file 工具读取其入口文件（扁平 .md 或 SKILL.md）来确认\n"
        "4. 确定最终匹配的技能列表\n"
        "\n"
        "注意：\n"
        "- 技能名（不含 .md / SKILL.md）大致反映了其内容领域\n"
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
            if _RT.interrupted:
                break

            # 调用 API
            try:
                sub_msg, sub_reasoning, _sub_usage = _RT.call_api(sub_messages, tools=sub_tools, tool_choice="auto", guard_multimodal=False)
            except _RT.UserInterrupt:
                print("   ⏹️ 用户中断了技能检索子Agent调用\n", flush=True)
                result_dict = {"success": 0, "err": "用户中断"}
                return json.dumps(result_dict)
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
    # 事先构建「技能名 -> 技能信息(dict)」映射，便于按形态精确解析入口与技能根目录
    merged_index = {s["name"]: s for s in _merge_skills()}
    loaded_contents = []
    if matched_skills:
        for skill_name in matched_skills:
            skill_info = merged_index.get(skill_name)
            skill_file = _resolve_skill_file(skill_info) if skill_info else _resolve_skill_file(skill_name)
            try:
                with open(skill_file, "rb") as f:
                    raw_data = f.read()
                content = smart_decode(raw_data)
                # 目录型技能附带技能根目录，便于后续读取 references/ 等辅助资源
                skill_root = ""
                if skill_info and skill_info.get("kind") == "dir":
                    skill_root = skill_info.get("root", "")
                loaded_contents.append({
                    "skill_name": skill_name,
                    "content": content,
                    "size": len(content),
                    "skill_root": skill_root,
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
        "skills_detail": [{
            "name": item["skill_name"],
            "size_chars": item["size"],
            "skill_root": item.get("skill_root", ""),
        } for item in loaded_contents],
        "contents": {item["skill_name"]: item["content"] for item in loaded_contents},
        "reasoning_preview": final_reasoning[:300] if final_reasoning else "",
        "message": f"技能 '{'、'.join(item['skill_name'] for item in loaded_contents)}' 已加载成功，AI 将参考这些技能知识"
    })


def _parse_frontmatter(text):
    """极简解析 Markdown 顶部的 YAML frontmatter（--- 到 --- 之间的键值对）。

    不依赖第三方 yaml 库，只处理扁平键值（兼容主流 SKILL.md 的 name/description）。
    返回 dict。解析失败或没有 frontmatter 时返回空 dict。
    """
    result = {}
    if not text:
        return result
    lines = text.split("\n")
    start = -1
    for i, ln in enumerate(lines[:60]):
        if ln.strip() == "---":
            start = i
            break
    if start < 0:
        return result
    end = -1
    for i in range(start + 1, min(len(lines), start + 60)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return result
    pending_key = None
    pending_val = []
    # YAML 块标量指示符：> | >- |- 等，后续缩进行都属于该 key 的值
    BLOCK_SCALAR = (">", "|", ">-", ">>-", "|-", "||-", ">&", ">>&")
    for ln in lines[start + 1:end]:
        if not ln.strip():
            continue
        if not ln[0].isspace() and ":" in ln:
            key, _, val = ln.partition(":")
            key = key.strip()
            val = val.strip()
            if pending_key and pending_val:
                result[pending_key] = "\n".join(pending_val).strip()
            if val and val in BLOCK_SCALAR:
                # 块标量：后续缩进行收集到该 key
                pending_key, pending_val = key, []
            elif val:
                pending_key, pending_val = None, []
                result[key] = val.strip().strip('"').strip("'")
            else:
                pending_key, pending_val = key, []
        elif pending_key is not None:
            pending_val.append(ln.strip())
    if pending_key and pending_val:
        result[pending_key] = "\n".join(pending_val).strip()
    return result


def _skill_desc(skill_info, text=None):
    """为技能生成一个给子 Agent 看的描述字符串。

    目录型技能优先用 frontmatter 里的 name/description；扁平型用其技能名。
    """
    name = skill_info.get("name", "")
    kind = skill_info.get("kind", "file")
    if kind == "dir":
        if text is None:
            skill_md = _find_skill_md(skill_info.get("root", ""))
            if skill_md:
                try:
                    with open(skill_md, "rb") as f:
                        text = smart_decode(f.read())
                except Exception:
                    text = None
        if text:
            fm = _parse_frontmatter(text)
            desc = fm.get("description") or fm.get("desc") or ""
            fm_name = fm.get("name") or ""
            parts = [f"[目录型技能] {name}"]
            if fm_name and fm_name != name:
                parts[0] += f" (frontmatter name={fm_name})"
            if desc:
                parts.append(desc[:200])
            return " | ".join(parts)
    return f"[扁平技能] {name}"


def _list_available_skills():
    """列出所有可用技能（合并两个 skills 目录，启动目录优先）。

    返回技能名列表（与老接口一致，供主 Agent 简单引用）。
    """
    merged = _merge_skills()
    return [s["name"] for s in merged]


def _list_available_skills_wrapper():
    """给子 Agent 用的 list_skills 工具包装函数，返回格式化的 JSON。

    相比老版本，额外附带每个技能的形态与描述，帮助子 Agent 更精准匹配。
    """
    merged = _merge_skills()

    cwd_skills = []
    code_skills = []
    for s in merged:
        if CWD_SKILLS_DIR and os.path.normpath(s.get("root", "")) == os.path.normpath(CWD_SKILLS_DIR):
            s["_source"] = "cwd"
            cwd_skills.append(s["name"])
        else:
            s["_source"] = "code"
            code_skills.append(s["name"])

    skills_names = [s["name"] for s in merged]
    skills_with_info = [_skill_desc(s) for s in merged]

    info_parts = []
    if CWD_SKILLS_DIR:
        info_parts.append(f"启动目录({CWD_SKILLS_DIR.replace(chr(92), '/')}): {len(cwd_skills)}个技能")
    if CODE_SKILLS_DIR:
        info_parts.append(f"代码目录({CODE_SKILLS_DIR.replace(chr(92), '/')}): {len(code_skills)}个技能")
    info_parts.append(f"合并后共 {len(skills_names)} 个技能（同名以启动目录版本为准）")

    return json.dumps({
        "success": 1,
        "skills": skills_names,
        "cwd_skills": cwd_skills,
        "code_skills": code_skills,
        "skills_with_info": skills_with_info,
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
    "mcp_list_servers": mcp_list_servers,
    "mcp_connect_server": mcp_connect_server,
    "mcp_disconnect_server": mcp_disconnect_server,
    "mcp_restart_server": mcp_restart_server,
    "read_image": read_image,
    "set_main_model_multimodal": set_main_model_multimodal,
    "start_bg_task": start_bg_task,
    "bg_task_status": bg_task_status,
    "bg_task_kill": bg_task_kill,
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


