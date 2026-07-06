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
            # 启动子进程
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            proc_env = os.environ.copy()
            proc_env.update(self.env)
            
            self.proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=proc_env,
                startupinfo=startupinfo,
                bufsize=0  # 无缓冲，确保即时通信
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
        for part in content_parts:
            part_type = part.get("type", "")
            if part_type == "text":
                text_parts.append(part.get("text", ""))
            elif part_type == "image":
                # 图片数据以 base64 形式返回，这里简单标记
                text_parts.append(f"[Image: {part.get('mimeType', 'unknown')}]")
            elif part_type == "resource":
                text_parts.append(f"[Resource: {part.get('uri', 'unknown')}]")
            else:
                text_parts.append(str(part))
        
        return "\n".join(text_parts)
    
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
