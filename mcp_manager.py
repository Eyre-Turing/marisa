#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP (Model Context Protocol) 管理器
===================================
通用的 MCP 服务器连接管理，支持 stdio 和 Streamable HTTP 两种传输层。

当前支持：
  - stdio 模式：通过子进程 stdin/stdout 通信（覆盖 95% 以上的 MCP server）
  - 自动初始化握手（initialize → tools/list）
  - MCP tool schema → OpenAI tool schema 自动转换
  - 工具调用路由：根据工具名前缀自动分发到对应的 MCP server
  - 进程生命周期管理（启动/保活/优雅关闭）

使用方法：
  1. 在 mcp_config.json 中配置 MCP 服务器
  2. 启动时调用 mcp_manager.connect_all() 自动连接所有配置的服务器
  3. 通过 mcp_manager.get_all_tools() 获取合并后的工具列表
  4. 通过 mcp_manager.call_tool(name, args) 调用工具
"""

import os
import json
import subprocess
import threading
import signal
import sys
import uuid
import shutil
import time

# ============================================================
# 配置管理
# ============================================================

MCP_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_config.json")

DEFAULT_MCP_CONFIG = {
    "mcp_servers": [
        # {
        #     "name": "blender",
        #     "transport": "stdio",
        #     "enabled": True,
        #     "command": "uv",
        #     "args": ["run", "--directory", "C:/blender_mcp/blender-mcp", "blender-mcp"],
        #     "env": {},
        #     "auto_connect": True,
        #     "tool_prefix": "blender_",  # 可选：给工具名加前缀避免冲突
        #     "timeout": 180,
        #     "debug": false              # 可选：true 则打印 stderr 日志（调试用），默认 false
        # },
        # {
        #     "name": "playwright",
        #     "transport": "stdio",
        #     "enabled": False,
        #     "command": "npx",
        #     "args": ["@anthropic-ai/mcp-playwright"],
        #     "env": {},
        #     "auto_connect": False,
        #     "tool_prefix": "pw_",
        #     "timeout": 60,
        #     "debug": false
        # },
        # {
        #     "name": "github",
        #     "transport": "stdio",
        #     "enabled": False,
        #     "command": "docker",
        #     "args": ["run", "-i", "--rm", "ghcr.io/github/github-mcp-server"],
        #     "env": {"GITHUB_TOKEN": "xxx"},
        #     "auto_connect": False,
        #     "tool_prefix": "gh_",
        #     "timeout": 60,
        #     "debug": false
        # }
    ]
}


def load_mcp_config():
    """加载 MCP 配置文件，如果不存在则创建默认配置"""
    config = {}
    if os.path.exists(MCP_CONFIG_FILE):
        try:
            with open(MCP_CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
        except (json.JSONDecodeError, IOError):
            config = {}
    
    # 确保有 mcp_servers 字段
    if "mcp_servers" not in config:
        config["mcp_servers"] = []
        # 首次创建时写入默认配置
        save_mcp_config(config)
    
    return config


def save_mcp_config(config):
    """保存 MCP 配置文件"""
    try:
        with open(MCP_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print(f"   ✅ MCP 配置已保存到 {MCP_CONFIG_FILE}", flush=True)
    except IOError as e:
        print(f"   ❌ 保存 MCP 配置失败: {e}", flush=True)


# ============================================================
# MCP 协议常量
# ============================================================

LATEST_PROTOCOL_VERSION = "2025-11-05"
JSONRPC_VERSION = "2.0"

# ============================================================
# MCP stdio 服务器客户端
# ============================================================

class MCPStdioServer:
    """
    通过 stdio 子进程连接的 MCP 服务器。
    
    通信协议：JSON-RPC 2.0 over stdin/stdout
    生命周期：initialize → tools/list → tools/call → shutdown
    """
    
    def __init__(self, name, command, args=None, env=None, tool_prefix="", timeout=60, debug=False):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.tool_prefix = tool_prefix
        self.timeout = timeout
        self.debug = debug
        
        self.proc = None
        self.tools = []          # OpenAI 格式的工具列表
        self.mcp_tools = []      # 原始 MCP 格式的工具列表
        self.connected = False
        self.server_info = {}
        self._lock = threading.Lock()
        self._next_id = 1
        self._stderr_thread = None
    
    def _get_id(self):
        """获取递增的请求 ID"""
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            return rid
    
    def connect(self):
        """启动子进程并完成 MCP 初始化握手"""
        if self.connected:
            return True
        
        print(f"   🔌 连接 MCP 服务器 [{self.name}]...", flush=True)
        
        try:
            # Windows 下 .cmd/.bat 不能被 subprocess 直接启动，需补全扩展名（如 npx -> npx.cmd）
            command = self.command
            if sys.platform == "win32" and os.path.splitext(command)[1].lower() not in (".cmd", ".bat", ".exe", ".com"):
                resolved = shutil.which(command)
                # shutil.which 在 Windows 上会优先解析到扩展名，但 Popen 列表模式仍可能报 WinError 2，
                # 这里附带查找 .cmd/.bat 版本并转换为绝对路径
                if resolved and os.path.splitext(resolved)[1].lower() in (".cmd", ".bat"):
                    command = resolved
                else:
                    cmd_candidate = shutil.which(command + ".cmd")
                    bat_candidate = shutil.which(command + ".bat")
                    if cmd_candidate:
                        command = cmd_candidate
                    elif bat_candidate:
                        command = bat_candidate

            # 启动子进程
            startupinfo = None
            popen_kwargs = {}
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

                # 关键修复：让 MCP 子进程进入独立的进程组，
                # 避免它在用户按 Ctrl+C 时被控制台广播的 CTRL_C_EVENT 连坐杀掉。
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                # POSIX：脱离控制终端成为新会话/进程组组长，同样不会接收前台 Ctrl+C。
                popen_kwargs["start_new_session"] = True

            proc_env = os.environ.copy()
            proc_env.update(self.env)

            self.proc = subprocess.Popen(
                [command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=proc_env,
                startupinfo=startupinfo,
                bufsize=0,  # 无缓冲，确保即时通信
                **popen_kwargs
            )
            
            # 无论 debug 是否开启，都要启动 stderr 读取线程，否则管道缓冲区满了 uvx 会卡死
            # debug=True 时打印到终端，debug=False 时只读取不显示
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                daemon=True
            )
            self._stderr_thread.start()
            
            # 发送 initialize 请求
            init_result = self._send_request("initialize", {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "marisa-ai-agent",
                    "version": "1.0.0"
                }
            })
            
            self.server_info = init_result
            self.connected = True
            
            # 获取工具列表
            self._fetch_tools()
            
            print(f"   ✅ MCP 服务器 [{self.name}] 连接成功！"
                  f" 工具数: {len(self.tools)}"
                  f" 服务器: {self.server_info.get('serverInfo', {}).get('name', 'unknown')}",
                  flush=True)
            
            return True
            
        except Exception as e:
            print(f"   ❌ MCP 服务器 [{self.name}] 连接失败: {e}", flush=True)
            self._cleanup()
            return False
    
    def _read_stderr(self):
        """在后台线程中读取 stderr，避免管道阻塞
        无论 debug 是否开启都会读取，但只在 debug=True 时打印到终端
        """
        try:
            if self.proc and self.proc.stderr:
                for line in iter(self.proc.stderr.readline, b''):
                    if line:
                        if self.debug:
                            line_str = line.decode('utf-8', errors='replace').rstrip()
                            if line_str:
                                print(f"   [MCP:{self.name} stderr] {line_str}", flush=True)
        except Exception:
            pass
    
    def _send_request(self, method, params=None):
        """发送 JSON-RPC 请求并等待响应"""
        if not self.proc or not self.proc.stdin:
            raise ConnectionError(f"MCP 服务器 [{self.name}] 未连接")
        
        req_id = self._get_id()
        request = {
            "jsonrpc": JSONRPC_VERSION,
            "id": req_id,
            "method": method,
            "params": params or {}
        }
        
        request_bytes = (json.dumps(request) + "\n").encode("utf-8")
        
        with self._lock:
            self.proc.stdin.write(request_bytes)
            self.proc.stdin.flush()
        
        # 读取响应（逐行读取 JSON）
        response = self._read_response(req_id)
        return response
    
    def _read_response(self, expected_id=None):
        """从 stdout 中读取 JSON-RPC 响应"""
        if not self.proc or not self.proc.stdout:
            raise ConnectionError(f"MCP 服务器 [{self.name}] 已断开")
        
        buffer = b""
        deadline = time.time() + self.timeout
        
        while time.time() < deadline:
            # 检查进程是否还活着
            if self.proc.poll() is not None:
                raise ConnectionError(
                    f"MCP 服务器 [{self.name}] 已退出，返回码: {self.proc.returncode}"
                )
            
            # 读取一行
            try:
                line = self.proc.stdout.readline()
            except Exception as e:
                raise ConnectionError(f"读取 MCP 响应失败: {e}")
            
            if not line:
                # 空行 = 连接关闭？
                if buffer:
                    # 尝试解析已有的 buffer
                    try:
                        resp = json.loads(buffer.decode("utf-8"))
                        if expected_id is None or resp.get("id") == expected_id:
                            if "error" in resp:
                                raise RuntimeError(
                                    f"MCP 请求失败: {resp['error'].get('message', 'unknown error')}"
                                )
                            return resp.get("result", {})
                    except json.JSONDecodeError:
                        pass
                raise ConnectionError(f"MCP 服务器 [{self.name}] 连接断开")
            
            buffer += line
            
            # 尝试解析完整的 JSON 对象
            try:
                resp = json.loads(buffer.decode("utf-8"))
                
                # 检查是否有错误
                if "error" in resp:
                    raise RuntimeError(
                        f"MCP 请求失败: {resp['error'].get('message', 'unknown error')}"
                    )
                
                # 如果指定了 expected_id，检查是否匹配
                if expected_id is None or resp.get("id") == expected_id:
                    return resp.get("result", {})
                
                # 不匹配的响应（可能是之前的响应），继续读取
                buffer = b""
                continue
                
            except json.JSONDecodeError:
                # 不完整的 JSON，继续读取
                continue
        
        raise TimeoutError(f"MCP 服务器 [{self.name}] 响应超时")
    
    def _fetch_tools(self):
        """获取 MCP 服务器的工具列表并转换为 OpenAI 格式"""
        result = self._send_request("tools/list", {})
        
        raw_tools = result.get("tools", [])
        self.mcp_tools = raw_tools
        
        # 转换为 OpenAI function calling 格式
        converted = []
        for tool in raw_tools:
            mcp_name = tool.get("name", "")
            name = f"{self.tool_prefix}{mcp_name}" if self.tool_prefix else mcp_name
            
            converted.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {
                        "type": "object",
                        "properties": {}
                    })
                },
                "_mcp_server": self.name,       # 标记来自哪个 MCP 服务器
                "_mcp_original_name": mcp_name   # 保存原始名称
            })
        
        self.tools = converted
    
    def call_tool(self, name, arguments):
        """调用 MCP 工具"""
        if not self.connected:
            raise ConnectionError(f"MCP 服务器 [{self.name}] 未连接")
        
        # 去除前缀获取原始工具名
        original_name = name
        if self.tool_prefix and name.startswith(self.tool_prefix):
            original_name = name[len(self.tool_prefix):]
        
        result = self._send_request("tools/call", {
            "name": original_name,
            "arguments": arguments
        })
        
        # 处理 MCP 返回结果格式
        # MCP 结果格式：{"content": [{"type": "text", "text": "..."}, {"type": "image", ...}]}
        content_parts = result.get("content", [])
        
        text_parts = []
        image_parts = []  # 📸 收集图片数据，不再丢弃！
        for part in content_parts:
            part_type = part.get("type", "")
            if part_type == "text":
                text_parts.append(part.get("text", ""))
            elif part_type == "image":
                # 📸 保留完整的 base64 图片数据，供上层注入多模态消息
                mime = part.get("mimeType", "image/png")
                data = part.get("data", "")
                if data:
                    image_parts.append({"base64": data, "mimeType": mime})
                    text_parts.append(f"[Image: {mime}, {len(data)//1024}KB base64]")
                else:
                    text_parts.append(f"[Image: {mime}, no data]")
            elif part_type == "resource":
                text_parts.append(f"[Resource: {part.get('uri', 'unknown')}]")
            else:
                text_parts.append(str(part))
        
        # 返回结构化 dict：text 给普通工具响应，images 给主循环注入多模态
        return {"text": "\n".join(text_parts), "images": image_parts}
    
    def disconnect(self):
        """断开 MCP 服务器连接"""
        print(f"   🔌 断开 MCP 服务器 [{self.name}]...", flush=True)
        self._cleanup()
    
    def _cleanup(self):
        """清理资源"""
        self.connected = False
        self.tools = []
        self.mcp_tools = []
        
        if self.proc:
            try:
                # 尝试优雅关闭
                if self.proc.stdin:
                    try:
                        shutdown_req = json.dumps({
                            "jsonrpc": JSONRPC_VERSION,
                            "id": self._get_id(),
                            "method": "shutdown",
                            "params": {}
                        }) + "\n"
                        self.proc.stdin.write(shutdown_req.encode("utf-8"))
                        self.proc.stdin.flush()
                    except Exception:
                        pass
                
                # 关闭 stdin，让子进程自行退出
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
                
                # 等待进程结束
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # 强制终止
                    if sys.platform == "win32":
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=5
                        )
                    else:
                        try:
                            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                        except Exception:
                            self.proc.kill()
            except Exception:
                pass
            finally:
                self.proc = None
        
        print(f"   ✅ MCP 服务器 [{self.name}] 已断开", flush=True)
    
    def is_alive(self):
        """检查服务器是否还活着"""
        if not self.connected or not self.proc:
            return False
        if self.proc.poll() is not None:
            self.connected = False
            return False
        return True
    
    def __repr__(self):
        return f"<MCPStdioServer {self.name} tools={len(self.tools)} connected={self.connected}>"


# ============================================================
# MCP Streamable HTTP 服务器客户端
# ============================================================

class MCPHttpServer:
    """
    通过 Streamable HTTP 连接的 MCP 服务器。

    通信协议：JSON-RPC 2.0 over HTTP POST (Streamable HTTP transport)
    生命周期：initialize → tools/list → tools/call
    """

    def __init__(self, name, url, tool_prefix="", timeout=60, headers=None, debug=False):
        self.name = name
        self.url = url
        self.tool_prefix = tool_prefix
        self.timeout = timeout
        self.headers = headers or {}
        self.debug = debug

        self.tools = []          # OpenAI 格式的工具列表
        self.mcp_tools = []      # 原始 MCP 格式的工具列表
        self.connected = False
        self.server_info = {}
        self._next_id = 1
        # 会话 ID（服务器在 initialize 响应中可能通过 Mcp-Session-Id 头返回）
        self._session_id = None

    def _get_id(self):
        rid = self._next_id
        self._next_id += 1
        return rid

    def _send_request(self, method, params=None):
        """
        发送 JSON-RPC 请求并等待响应（同步 HTTP POST）。
        """
        import urllib.request
        import urllib.error

        req_id = self._get_id()
        request = {
            "jsonrpc": JSONRPC_VERSION,
            "id": req_id,
            "method": method,
            "params": params or {}
        }

        data = json.dumps(request).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        req_headers.update(self.headers)
        if self._session_id:
            req_headers["Mcp-Session-Id"] = self._session_id

        if self.debug:
            print(f"   [MCP:{self.name} http] -> {method} (id={req_id})", flush=True)

        try:
            req = urllib.request.Request(self.url, data=data, headers=req_headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                # 捕获会话 ID
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid

                body = resp.read().decode("utf-8")
                content_type = resp.headers.get("Content-Type", "")

                if self.debug:
                    print(f"   [MCP:{self.name} http] <- {content_type}: {body[:500]}", flush=True)

                result = self._parse_http_response(body, content_type, req_id)
                return result

        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise ConnectionError(
                f"MCP HTTP 服务器 [{self.name}] 返回 HTTP {e.code}: {body[:500]}"
            )
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"MCP HTTP 服务器 [{self.name}] 连接失败: {e.reason}"
            )

    def _parse_http_response(self, body, content_type, expected_id):
        """
        解析 HTTP 响应体，支持纯 JSON 和 SSE (text/event-stream) 两种格式。
        """
        # 处理 SSE 格式：逐行解析 data: {...}
        if "text/event-stream" in content_type:
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    json_str = line[5:].strip()
                    if not json_str:
                        continue
                    try:
                        resp = json.loads(json_str)
                    except json.JSONDecodeError:
                        continue
                    if resp.get("id") == expected_id:
                        if "error" in resp:
                            raise RuntimeError(
                                f"MCP 请求失败: {resp['error'].get('message', 'unknown error')}"
                            )
                        return resp.get("result", {})
            raise RuntimeError(
                f"MCP HTTP 服务器 [{self.name}] 在 SSE 响应中未找到 id={expected_id} 的结果"
            )

        # 纯 JSON 格式
        resp = json.loads(body)
        if "error" in resp:
            raise RuntimeError(
                f"MCP 请求失败: {resp['error'].get('message', 'unknown error')}"
            )
        if resp.get("id") != expected_id:
            raise RuntimeError(
                f"MCP HTTP 响应 id 不匹配: 期望 {expected_id}, 得到 {resp.get('id')}"
            )
        return resp.get("result", {})

    def connect(self):
        """
        连接到 HTTP MCP 服务器并完成初始化握手。
        """
        if self.connected:
            return True

        print(f"   📬 连接 MCP HTTP 服务器 [{self.name}] -> {self.url}...", flush=True)

        try:
            init_result = self._send_request("initialize", {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "marisa-ai-agent",
                    "version": "1.0.0"
                }
            })

            self.server_info = init_result
            self.connected = True

            # 发送 initialized 通知（部分服务器需要）
            try:
                self._send_notification("notifications/initialized", {})
            except Exception:
                pass  # 通知失败不影响连接

            # 获取工具列表
            self._fetch_tools()

            print(f"   ✅ MCP HTTP 服务器 [{self.name}] 连接成功！"
                  f" 工具数: {len(self.tools)}"
                  f" 服务器: {self.server_info.get('serverInfo', {}).get('name', 'unknown')}",
                  flush=True)

            return True

        except Exception as e:
            print(f"   ❌ MCP HTTP 服务器 [{self.name}] 连接失败: {e}", flush=True)
            self.connected = False
            return False

    def _send_notification(self, method, params=None):
        """
        发送 JSON-RPC 通知（无 id，不等待响应）。
        """
        import urllib.request

        notification = {
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
            "params": params or {}
        }

        data = json.dumps(notification).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        req_headers.update(self.headers)
        if self._session_id:
            req_headers["Mcp-Session-Id"] = self._session_id

        try:
            req = urllib.request.Request(self.url, data=data, headers=req_headers, method="POST")
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception:
            pass  # 通知不需要处理响应

    def _fetch_tools(self):
        """
        获取 MCP 服务器的工具列表并转换为 OpenAI 格式。
        """
        result = self._send_request("tools/list", {})

        raw_tools = result.get("tools", [])
        self.mcp_tools = raw_tools

        converted = []
        for tool in raw_tools:
            mcp_name = tool.get("name", "")
            name = f"{self.tool_prefix}{mcp_name}" if self.tool_prefix else mcp_name

            converted.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {
                        "type": "object",
                        "properties": {}
                    })
                },
                "_mcp_server": self.name,
                "_mcp_original_name": mcp_name
            })

        self.tools = converted

    def call_tool(self, name, arguments):
        """
        调用 MCP 工具。
        """
        if not self.connected:
            raise ConnectionError(f"MCP HTTP 服务器 [{self.name}] 未连接")

        original_name = name
        if self.tool_prefix and name.startswith(self.tool_prefix):
            original_name = name[len(self.tool_prefix):]

        result = self._send_request("tools/call", {
            "name": original_name,
            "arguments": arguments
        })

        # 处理 MCP 返回结果格式（与 stdio 版本逻辑一致）
        content_parts = result.get("content", [])

        text_parts = []
        image_parts = []
        for part in content_parts:
            part_type = part.get("type", "")
            if part_type == "text":
                text_parts.append(part.get("text", ""))
            elif part_type == "image":
                mime = part.get("mimeType", "image/png")
                data = part.get("data", "")
                if data:
                    image_parts.append({"base64": data, "mimeType": mime})
                    text_parts.append(f"[Image: {mime}, {len(data)//1024}KB base64]")
                else:
                    text_parts.append(f"[Image: {mime}, no data]")
            elif part_type == "resource":
                text_parts.append(f"[Resource: {part.get('uri', 'unknown')}]")
            else:
                text_parts.append(str(part))

        return {"text": "\n".join(text_parts), "images": image_parts}

    def disconnect(self):
        """
        断开 HTTP MCP 服务器连接。
        """
        print(f"   📬 断开 MCP HTTP 服务器 [{self.name}]...", flush=True)
        self.connected = False
        self.tools = []
        self.mcp_tools = []
        self._session_id = None
        print(f"   ✅ MCP HTTP 服务器 [{self.name}] 已断开", flush=True)

    def is_alive(self):
        """
        检查服务器是否还活着（HTTP 模式下只要 connected 就算活着）。
        """
        return self.connected

    def __repr__(self):
        return f"<MCPHttpServer {self.name} url={self.url} tools={len(self.tools)} connected={self.connected}>"


# ============================================================
# MCP 老式 SSE transport 服务器客户端
# ============================================================

class MCPHttpSseServer:
    """
    通过老式 SSE (Server-Sent Events) transport 连接的 MCP 服务器。

    通信协议：JSON-RPC 2.0 over HTTP + SSE
    生命周期：
      1. GET <sse_url> 建立 SSE 长连接，收到 `event: endpoint` 事件得到消息端点
      2. POST JSON-RPC 请求到该消息端点（返回 HTTP 202 + 空 body）
      3. 响应通过 SSE 长连接异步推回（同一 SSE 流）
    """

    def __init__(self, name, url, tool_prefix="", timeout=60, headers=None, debug=False):
        self.name = name
        self.sse_url = url          # SSE 入口 URL，如 http://host/sse
        self.url = url              # 连接后会被 SSE 流更新为 messages 端点
        self.tool_prefix = tool_prefix
        self.timeout = timeout
        self.headers = headers or {}
        self.debug = debug

        self.tools = []          # OpenAI 格式工具列表
        self.mcp_tools = []      # 原始 MCP 格式工具列表
        self.connected = False
        self.server_info = {}
        self._next_id = 1
        self._session_id = None

        # SSE 状态
        self._endpoint_ready = threading.Event()
        self._endpoint_url = None
        self._pending = {}       # req_id -> {"event": Event, "result": None, "error": None}
        self._pending_lock = threading.Lock()
        self._sse_thread = None
        self._sse_response = None
        self._stopped = threading.Event()

    def _get_id(self):
        rid = self._next_id
        self._next_id += 1
        return rid
    def _sse_listener(self):
        """后台线程：持续读取原始 SSE 字节流，解析 endpoint 事件和 JSON-RPC 异步响应。

        使用 select 轮询底层 socket。若沿用 setttimeout(1.0) 做空闲轮询，socket 一旦
        读超时一次就会进入 Python 的 timeout_occurred 坏状态，此后每次 read 都抛
        OSError('cannot read from timed out object')，导致监听线程崩溃、后续响应无人接收。
        改为非阻塞 + select 后，空闲时不会破坏 socket，且能感知 _stopped 优雅退出，
        在 socket 关闭时也正常中止，不会在 Windows 上与其他线程 close 死锁。
        """
        import select
        import urllib.request
        import socket as _socket_mod
        req = urllib.request.Request(self.sse_url, headers={"Accept": "text/event-stream"})
        try:
            resp = urllib.request.urlopen(req, timeout=None)
        except Exception as e:
            if self.debug:
                print(f"   [MCP:{self.name} sse] 连接 SSE 失败: {e}", flush=True)
            self._fail_all_pending(f"SSE 连接失败: {e}")
            return
        self._sse_response = resp
        # 底层 socket 设为非阻塞，配合 select 轮询，避免超时后进入破状态
        sock = resp.fp.raw._sock
        try:
            sock.setblocking(False)
        except Exception:
            pass
        current_event = None
        data_buf = []
        buf = b""
        try:
            while not self._stopped.is_set():
                try:
                    readable, _, _ = select.select([sock], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if not readable:
                    continue
                try:
                    chunk = sock.recv(65536)
                except BlockingIOError as e:
                    # 非阻塞下偶发无数据，继续等待
                    continue
                except (OSError, ValueError) as e:
                    if self.debug:
                        print(f"   [MCP:{self.name} sse] 读取异常: {e}", flush=True)
                    break
                if not chunk:
                    break  # 对端关闭连接
                buf += chunk
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
                    if line.startswith("event:"):
                        if data_buf:
                            payload = "\n".join(data_buf)
                            data_buf = []
                            self._process_sse_data(current_event, payload)
                        current_event = line[len("event:"):].strip()
                        continue
                    if line.startswith("data:"):
                        data_buf.append(line[len("data:"):].strip())
                        continue
                    if line == "":
                        if data_buf:
                            payload = "\n".join(data_buf)
                            data_buf = []
                            self._process_sse_data(current_event, payload)
                        current_event = None
                        continue
        except Exception:
            pass
        finally:
            # SSE 断开时，通知仍在等待的请求
            self._fail_all_pending("SSE 连接已断开")
            try:
                resp.close()
            except Exception:
                pass

    def _process_sse_data(self, event, payload):
        """处理一个完整 SSE 事件的 data payload（可能由多行 data: 拼接而成）。"""
        if event == "endpoint":
            self._endpoint_url = payload
            self.url = payload
            self._endpoint_ready.set()
            if self.debug:
                print(f"   [MCP:{self.name} sse] endpoint -> {payload}", flush=True)
            return
        self._dispatch_response(payload)

    def _dispatch_response(self, data):
        """尝试将 SSE 数据解析为 JSON-RPC 响应，并唤醒对应的等待请求。"""
        try:
            msg = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            if self.debug:
                print(f"   [MCP:{self.name} sse] 非 JSON 数据: {data[:200]}", flush=True)
            return
        rid = msg.get("id")
        if rid is None:
            return
        with self._pending_lock:
            entry = self._pending.get(rid)
        if entry:
            if "error" in msg:
                entry["error"] = msg["error"]
            else:
                entry["result"] = msg.get("result", {})
            entry["event"].set()

    def _fail_all_pending(self, reason):
        with self._pending_lock:
            for entry in self._pending.values():
                entry["error"] = {"message": reason}
                entry["event"].set()
            self._pending.clear()

    def _wait_for_response(self, rid):
        with self._pending_lock:
            entry = self._pending.get(rid)
        if not entry:
            raise RuntimeError(f"MCP SSE 服务器 [{self.name}] 无挂起的请求 id={rid}")
        if not entry["event"].wait(self.timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"MCP SSE 服务器 [{self.name}] 请求 id={rid} 等待响应超时")
        with self._pending_lock:
            self._pending.pop(rid, None)
        if entry["error"]:
            raise RuntimeError(f"MCP 请求失败: {entry['error'].get('message', entry['error'])}")
        return entry["result"]

    def _send_request(self, method, params=None):
        """发送 JSON-RPC 请求并等待 SSE 流异步返回响应。"""
        import urllib.request
        if not self._endpoint_ready.is_set():
            raise ConnectionError(f"MCP SSE 服务器 [{self.name}] 尚未建立 SSE endpoint")
        req_id = self._get_id()
        request = {"jsonrpc": JSONRPC_VERSION, "id": req_id, "method": method, "params": params or {}}
        data = json.dumps(request).encode("utf-8")
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        req_headers.update(self.headers)
        if self._session_id:
            req_headers["Mcp-Session-Id"] = self._session_id

        if self.debug:
            print(f"   [MCP:{self.name} sse] -> {method} (id={req_id})", flush=True)

        entry = {"event": threading.Event(), "result": None, "error": None}
        with self._pending_lock:
            self._pending[req_id] = entry

        try:
            rq = urllib.request.Request(self.url, data=data, headers=req_headers, method="POST")
            with urllib.request.urlopen(rq, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
            # 老式 SSE：POST 返回 202 + 空 body，结果经 SSE 流推送
            return self._wait_for_response(req_id)
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise ConnectionError(f"MCP SSE 服务器 [{self.name}] 请求 {method} 失败: {e}")

    def _send_notification(self, method, params=None):
        import urllib.request
        notification = {"jsonrpc": JSONRPC_VERSION, "method": method, "params": params or {}}
        data = json.dumps(notification).encode("utf-8")
        req_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        req_headers.update(self.headers)
        if self._session_id:
            req_headers["Mcp-Session-Id"] = self._session_id
        try:
            rq = urllib.request.Request(self.url, data=data, headers=req_headers, method="POST")
            urllib.request.urlopen(rq, timeout=self.timeout).read()
        except Exception:
            pass

    def connect(self):
        """建立 SSE 连接，拿到消息端点，完成 initialize 握手。"""
        if self.connected:
            return True
        self.url = self.sse_url
        print(f"   📬 连接 MCP SSE 服务器 [{self.name}] -> {self.sse_url}...", flush=True)
        try:
            # 若已有旧 SSE 线程在运行，先停止并等它退出，避免与新连接混淆
            if self._sse_thread and self._sse_thread.is_alive():
                self._stopped.set()
                self._sse_thread.join(timeout=2.0)
                self._sse_thread = None
            # 启动 SSE 监听线程，等待 endpoint 事件
            self._stopped.clear()
            self._sse_thread = threading.Thread(target=self._sse_listener, daemon=True)
            self._sse_thread.start()
            if not self._endpoint_ready.wait(self.timeout):
                raise TimeoutError("等待 SSE endpoint 事件超时")

            init_result = self._send_request("initialize", {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "marisa-ai-agent", "version": "1.0.0"}
            })

            self.server_info = init_result
            self.connected = True

            try:
                self._send_notification("notifications/initialized", {})
            except Exception:
                pass

            self._fetch_tools()

            print(f"   ✅ MCP SSE 服务器 [{self.name}] 连接成功！"
                  f" 工具数: {len(self.tools)}"
                  f" 服务器: {self.server_info.get('serverInfo', {}).get('name', 'unknown')}", flush=True)
            return True

        except Exception as e:
            print(f"   ❌ MCP SSE 服务器 [{self.name}] 连接失败: {e}", flush=True)
            self.connected = False
            return False

    def _fetch_tools(self):
        result = self._send_request("tools/list", {})
        raw_tools = result.get("tools", [])
        self.mcp_tools = raw_tools
        converted = []
        for tool in raw_tools:
            mcp_name = tool.get("name", "")
            name = f"{self.tool_prefix}{mcp_name}" if self.tool_prefix else mcp_name
            converted.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("inputSchema", {"type": "object", "properties": {}})
                },
                "_mcp_server": self.name,
                "_mcp_original_name": mcp_name
            })
        self.tools = converted

    def call_tool(self, name, arguments):
        if not self.connected:
            raise ConnectionError(f"MCP SSE 服务器 [{self.name}] 未连接")
        original_name = name
        if self.tool_prefix and name.startswith(self.tool_prefix):
            original_name = name[len(self.tool_prefix):]
        result = self._send_request("tools/call", {"name": original_name, "arguments": arguments})
        content_parts = result.get("content", [])
        text_parts = []
        image_parts = []
        for part in content_parts:
            part_type = part.get("type", "")
            if part_type == "text":
                text_parts.append(part.get("text", ""))
            elif part_type == "image":
                mime = part.get("mimeType", "image/png")
                data = part.get("data", "")
                if data:
                    image_parts.append({"base64": data, "mimeType": mime})
                    text_parts.append(f"[Image: {mime}, {len(data)//1024}KB base64]")
                else:
                    text_parts.append(f"[Image: {mime}, no data]")
            elif part_type == "resource":
                text_parts.append(f"[Resource: {part.get('uri', 'unknown')}]")
            else:
                text_parts.append(str(part))
        return {"text": "\n".join(text_parts), "images": image_parts}

    def disconnect(self):
        print(f"   📬 断开 MCP SSE 服务器 [{self.name}]...", flush=True)
        self.connected = False
        self.tools = []
        self.mcp_tools = []
        self._session_id = None
        # 通知 SSE 线程停止。不在此处调用 _sse_response.close()/sock.close()：
        # 阻塞读取的 socket 在另一个线程 close() 会与读线程死锁（Windows 上尤为明显）。
        # SSE 线程会在读超时轮询中感知 _stopped 并自行优雅退出。
        self._stopped.set()
        self._fail_all_pending("连接已断开")
        self._endpoint_ready.clear()
        print(f"   ✅ MCP SSE 服务器 [{self.name}] 已断开", flush=True)

    def is_alive(self):
        return self.connected

    def __repr__(self):
        return f"<MCPHttpSseServer {self.name} url={self.url} tools={len(self.tools)} connected={self.connected}>"


# ============================================================
# MCP 管理器（主入口）
# ============================================================
class MCPManager:
    """
    统一的 MCP 服务器管理器。
    
    用法：
        manager = MCPManager()
        manager.connect_all()          # 连接所有配置的服务器
        tools = manager.get_all_tools()  # 获取合并后的工具列表
        result = manager.call_tool("blender_execute_code", {...})  # 调用工具
        manager.disconnect_all()        # 断开所有连接
    """
    
    def __init__(self, config_path=None):
        self.config_path = config_path or MCP_CONFIG_FILE
        self.servers = {}  # name -> MCPStdioServer
        self._name_to_server = {}  # 工具名(含前缀) -> MCPStdioServer
    
    def connect_all(self):
        """连接所有配置中启用的 MCP 服务器"""
        config = load_mcp_config()
        server_configs = config.get("mcp_servers", [])
        
        if not server_configs:
            print("   ℹ️  MCP 配置为空，跳过连接", flush=True)
            return
        
        connected_count = 0
        for cfg in server_configs:
            if not cfg.get("enabled", True):
                print(f"   ⏭️  MCP 服务器 [{cfg.get('name', 'unknown')}] 已禁用，跳过", flush=True)
                continue
            
            name = cfg.get("name", f"mcp_{len(self.servers)}")
            transport = cfg.get("transport", "stdio")
            
            if transport == "stdio":
                server = MCPStdioServer(
                    name=name,
                    command=cfg["command"],
                    args=cfg.get("args", []),
                    env=cfg.get("env", {}),
                    tool_prefix=cfg.get("tool_prefix", ""),
                    timeout=cfg.get("timeout", 60),
                    debug=cfg.get("debug", False)
                )
                
                if cfg.get("auto_connect", True):
                    if server.connect():
                        self.servers[name] = server
                        # 注册工具路由
                        for tool in server.tools:
                            tool_name = tool["function"]["name"]
                            self._name_to_server[tool_name] = name
                        connected_count += 1
                else:
                    self.servers[name] = server
            elif transport == "http":
                server = MCPHttpServer(
                    name=name,
                    url=cfg["url"],
                    tool_prefix=cfg.get("tool_prefix", ""),
                    timeout=cfg.get("timeout", 60),
                    headers=cfg.get("headers", {}),
                    debug=cfg.get("debug", False)
                )
                
                if cfg.get("auto_connect", True):
                    if server.connect():
                        self.servers[name] = server
                        for tool in server.tools:
                            tool_name = tool["function"]["name"]
                            self._name_to_server[tool_name] = name
                        connected_count += 1
                else:
                    self.servers[name] = server
            elif transport == "sse":
                server = MCPHttpSseServer(
                    name=name,
                    url=cfg["url"],
                    tool_prefix=cfg.get("tool_prefix", ""),
                    timeout=cfg.get("timeout", 60),
                    headers=cfg.get("headers", {}),
                    debug=cfg.get("debug", False)
                )
                
                if cfg.get("auto_connect", True):
                    if server.connect():
                        self.servers[name] = server
                        for tool in server.tools:
                            tool_name = tool["function"]["name"]
                            self._name_to_server[tool_name] = name
                        connected_count += 1
                else:
                    self.servers[name] = server
            else:
                print(f"   ⚠️  MCP 服务器 [{name}] 不支持的传输层: {transport}，跳过", flush=True)
        
        if connected_count > 0:
            print(f"   📡 MCP 管理器：已连接 {connected_count} 个服务器，"
                  f"共 {len(self._name_to_server)} 个 MCP 工具", flush=True)
    
    def get_server(self, name):
        """获取指定名称的 MCP 服务器实例"""
        return self.servers.get(name)
    
    def get_all_tools(self):
        """获取所有已连接 MCP 服务器的工具列表（合并为一个列表）"""
        all_tools = []
        for server in self.servers.values():
            if server.connected:
                all_tools.extend(server.tools)
        return all_tools
    
    def get_tools_for_server(self, name):
        """获取指定 MCP 服务器的工具列表"""
        server = self.servers.get(name)
        if server and server.connected:
            return server.tools
        return []
    
    def call_tool(self, name, arguments):
        """
        调用 MCP 工具。
        
        根据工具名自动路由到对应的 MCP 服务器。
        """
        server_name = self._name_to_server.get(name)
        if not server_name:
            raise ValueError(f"未知的 MCP 工具: {name}")
        
        server = self.servers.get(server_name)
        if not server or not server.connected:
            raise ConnectionError(f"MCP 服务器 [{server_name}] 未连接")
        
        return server.call_tool(name, arguments)
    
    def get_server_for_tool(self, tool_name):
        """获取某个工具所属的 MCP 服务器名称"""
        return self._name_to_server.get(tool_name)
    
    def is_mcp_tool(self, tool_name):
        """判断是否是 MCP 工具"""
        return tool_name in self._name_to_server
    
    def disconnect_all(self):
        """断开所有 MCP 服务器连接"""
        for name in list(self.servers.keys()):
            server = self.servers.pop(name)
            try:
                server.disconnect()
            except Exception as e:
                print(f"   ⚠️ 断开 MCP 服务器 [{name}] 时出错: {e}", flush=True)
        
        self._name_to_server.clear()
        print("   📡 MCP 管理器：所有服务器已断开", flush=True)
    
    def reconnect_all(self):
        """重新连接所有 MCP 服务器"""
        self.disconnect_all()
        self.connect_all()
    
    def list_servers_config(self):
        """返回所有已配置 MCP 服务器的详细状态列表
        
        从 mcp_config.json 读取配置，结合当前运行状态返回完整信息。
        """
        config = load_mcp_config()
        server_configs = config.get("mcp_servers", [])
        result = []
        for cfg in server_configs:
            name = cfg.get("name", "unknown")
            enabled = cfg.get("enabled", True)
            auto_connect = cfg.get("auto_connect", True)
            transport = cfg.get("transport", "stdio")
            
            # 检查运行状态
            server = self.servers.get(name)
            is_running = False
            tool_count = 0
            if server is not None:
                is_running = server.is_alive()
                tool_count = len(server.tools) if server.connected else 0
            
            result.append({
                "name": name,
                "transport": transport,
                "enabled": enabled,
                "auto_connect": auto_connect,
                "running": is_running,
                "tool_count": tool_count,
                "command": cfg.get("command", ""),
                "tool_prefix": cfg.get("tool_prefix", ""),
            })
        return result

    def connect_server(self, name):
        """连接指定的 MCP 服务器
        
        根据配置创建新的 MCPStdioServer 实例并连接。
        如果已有同名服务器在运行，会先断开旧连接。
        
        参数:
            name: 服务器名称（与 mcp_config.json 中的 name 一致）
        
        返回:
            dict: {"success": True/False, "name": name, "tool_count": N, "err": "错误信息"}
        """
        config = load_mcp_config()
        server_configs = config.get("mcp_servers", [])
        
        # 查找配置
        cfg = None
        for c in server_configs:
            if c.get("name") == name:
                cfg = c
                break
        
        if not cfg:
            return {"success": False, "name": name, "err": f"未找到 MCP 服务器配置: {name}"}
        
        if not cfg.get("enabled", True):
            return {"success": False, "name": name, "err": f"MCP 服务器 [{name}] 已禁用（enabled=false），请先修改 mcp_config.json 启用它"}
        
        # 如果已有服务器实例且正在运行，先断开
        existing = self.servers.get(name)
        if existing:
            if existing.is_alive():
                print(f"   🔄 MCP 服务器 [{name}] 已在运行，先断开旧连接...", flush=True)
                existing.disconnect()
            # 从路由表中移除旧的工具
            self._name_to_server = {k: v for k, v in self._name_to_server.items() if v != name}
        
        # 创建新实例并连接
        transport = cfg.get("transport", "stdio")
        if transport == "stdio":
            server = MCPStdioServer(
                name=name,
                command=cfg["command"],
                args=cfg.get("args", []),
                env=cfg.get("env", {}),
                tool_prefix=cfg.get("tool_prefix", ""),
                timeout=cfg.get("timeout", 60),
                debug=cfg.get("debug", False)
            )
            
            if server.connect():
                self.servers[name] = server
                for tool in server.tools:
                    tool_name = tool["function"]["name"]
                    self._name_to_server[tool_name] = name
                return {"success": True, "name": name, "tool_count": len(server.tools), "err": ""}
            else:
                return {"success": False, "name": name, "tool_count": 0, "err": f"MCP 服务器 [{name}] 连接失败，请检查配置和日志"}
        elif transport == "http":
            server = MCPHttpServer(
                name=name,
                url=cfg["url"],
                tool_prefix=cfg.get("tool_prefix", ""),
                timeout=cfg.get("timeout", 60),
                headers=cfg.get("headers", {}),
                debug=cfg.get("debug", False)
            )
            
            if server.connect():
                self.servers[name] = server
                for tool in server.tools:
                    tool_name = tool["function"]["name"]
                    self._name_to_server[tool_name] = name
                return {"success": True, "name": name, "tool_count": len(server.tools), "err": ""}
            else:
                return {"success": False, "name": name, "tool_count": 0, "err": f"MCP HTTP 服务器 [{name}] 连接失败，请检查 URL 和网络"}
        elif transport == "sse":
            server = MCPHttpSseServer(
                name=name,
                url=cfg["url"],
                tool_prefix=cfg.get("tool_prefix", ""),
                timeout=cfg.get("timeout", 60),
                headers=cfg.get("headers", {}),
                debug=cfg.get("debug", False)
            )
            
            if server.connect():
                self.servers[name] = server
                for tool in server.tools:
                    tool_name = tool["function"]["name"]
                    self._name_to_server[tool_name] = name
                return {"success": True, "name": name, "tool_count": len(server.tools), "err": ""}
            else:
                return {"success": False, "name": name, "tool_count": 0, "err": f"MCP SSE 服务器 [{name}] 连接失败，请检查 URL 和网络"}
        else:
            return {"success": False, "name": name, "err": f"不支持的传输层: {transport}"}

    def disconnect_server(self, name):
        """断开指定的 MCP 服务器
        
        停止进程并清理路由表中的工具注册。
        
        参数:
            name: 服务器名称
        """
        server = self.servers.get(name)
        if not server:
            return {"success": False, "name": name, "err": f"MCP 服务器 [{name}] 不存在或未连接"}
        
        # 从路由表中移除工具
        self._name_to_server = {k: v for k, v in self._name_to_server.items() if v != name}
        
        server.disconnect()
        del self.servers[name]
        return {"success": True, "name": name, "err": ""}

    def restart_server(self, name):
        """重启指定的 MCP 服务器（先断开再重新连接）"""
        disc_result = self.disconnect_server(name)
        if not disc_result["success"] and "不存在" not in disc_result.get("err", ""):
            return disc_result
        return self.connect_server(name)

    def __repr__(self):
        return f"<MCPManager servers={len(self.servers)} tools={len(self._name_to_server)}>"


# ============================================================
# 全局单例
# ============================================================

_mcp_manager = None


def get_mcp_manager():
    """获取全局 MCP 管理器单例"""
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
    return _mcp_manager
