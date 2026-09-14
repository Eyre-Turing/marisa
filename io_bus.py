#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多路复用输入总线（纯标准库）

设计目标
========
1. **零第三方依赖**：本模块只用标准库，保证「只装了 python3」也能完整工作。
   输入的多路复用本身（threading + queue + socket）根本不需要任何第三方库。
2. **输入源可插拔**：键盘 / socket / 管道(stdin) / 后台任务 / 系统 —— 各自独立线程，
   谁先有输入谁就唤醒 agent 主循环（队列语义天然就是「任意一个就绪即触发」）。
3. **降级即另一种实现**：没装 prompt_toolkit 时，键盘源换成 input() 实现，
   多路复用能力一点不少，只是少了「输入框常驻底部」的分区渲染。

事件流
======
    [键盘源(主线程 UI)]  ─┐
    [SocketSource 线程]  ─┤
    [StdinPipeSource]    ─┼──►  InputBus(queue)  ──►  agent 工作线程 bus.get()
    [后台任务完成钩子]    ─┘

约定：任何源投递的 `InputEvent.text` 只要 `is STOP`，主循环就收工退出。
"""

import socket
import sys
import threading
import queue


# ============================================================
#  事件源标识
# ============================================================
SRC_KEYBOARD = "keyboard"
SRC_SOCKET = "socket"
SRC_PIPE = "pipe"
SRC_BACKGROUND = "background"
SRC_SYSTEM = "system"

# 哨兵：投递它表示「请主循环收工退出」。
# 用带名字的 object() 而不是 None，方便在日志里一眼看出是停止信号。
STOP = object()


class InputEvent:
    """一条输入事件：来自哪个源、内容是什么。"""

    __slots__ = ("source", "text", "extra")

    def __init__(self, source, text, extra=None):
        self.source = source
        self.text = text
        self.extra = extra if extra is not None else {}

    def __repr__(self):
        preview = self.text if isinstance(self.text, str) else repr(self.text)
        if len(preview) > 40:
            preview = preview[:40] + "..."
        return f"<InputEvent [{self.source}] {preview!r}>"


# ============================================================
#  输入总线
# ============================================================
class InputBus:
    """线程安全的多源输入事件总线。

    - `put()` 可由任意线程调用（键盘 UI 线程、socket 客户端线程、后台任务钩子……）
    - `get()` 由 agent 工作线程阻塞调用，收到任意源的事件就返回
    """

    def __init__(self, name="marisa-input"):
        self.name = name
        self._q = queue.Queue()
        self._sources = []
        self._started = False
        self._lock = threading.Lock()

    # ---------- 投递 ----------
    def put(self, text, source=SRC_SYSTEM, extra=None):
        """投递一条输入事件（线程安全）。"""
        self._q.put(InputEvent(source, text, extra))

    def put_stop(self):
        """投递「收工」哨兵（线程安全，重复投递无害）。"""
        self._q.put(InputEvent(SRC_SYSTEM, STOP))

    # ---------- 消费 ----------
    def get(self, timeout=None):
        """阻塞取一条事件（timeout=None 一直等）。"""
        return self._q.get(timeout=timeout)

    def get_nowait(self):
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    def empty(self):
        return self._q.empty()

    # ---------- 源管理 ----------
    def add_source(self, source):
        with self._lock:
            if self._started:
                # 已经启动过了：补启动这个迟到的源，避免静默失效
                try:
                    source.start(self)
                except Exception as e:
                    print(f"   ⚠️ 输入源 [{source.name}] 启动失败: {e}", flush=True)
                return
            self._sources.append(source)

    def start(self):
        """启动所有已注册的输入源（每个源自己起线程，失败不影响其他源）。"""
        with self._lock:
            if self._started:
                return
            self._started = True
            sources = list(self._sources)
        for s in sources:
            try:
                s.start(self)
            except Exception as e:
                print(f"   ⚠️ 输入源 [{getattr(s, 'name', '?')}] 启动失败: {e}", flush=True)

    def stop(self):
        """停止所有输入源（仅用于进程退出清理）。"""
        with self._lock:
            sources = list(self._sources)
        for s in sources:
            try:
                s.stop()
            except Exception:
                pass


# ============================================================
#  输入源基类
# ============================================================
class InputSource:
    """输入源基类。子类实现 start(bus) / stop()，自己负责起线程。"""

    name = "base"

    def start(self, bus):
        raise NotImplementedError

    def stop(self):
        pass


# ============================================================
#  管道 / 重定向输入源（stdin 非 TTY 时使用）
# ============================================================
class StdinPipeSource(InputSource):
    """流式读取 stdin，按 SMTP '.' 协议切分成一条条消息。

    规则与交互式 input() 模式完全一致：
      - 单独一行 '.'     → 当前消息结束，投递出去，开始下一条
      - 以 '..' 开头的行 → 还原为以 '.' 开头的一行
      - 其余行原样拼入当前消息
      - 读到 EOF         → 投递残留消息，然后投递 STOP 请求主循环退出

    不复用主程序里的 read_multiline_input，是为了让它能在独立线程里跑而不阻塞主线程。
    """

    name = "pipe"

    def __init__(self, stream=None):
        self._stream = stream
        self._bus = None
        self._stop = threading.Event()
        self._thread = None

    def start(self, bus):
        self._bus = bus
        self._thread = threading.Thread(
            target=self._loop, name="marisa-stdin-pipe", daemon=True
        )
        self._thread.start()

    def _loop(self):
        stream = self._stream if self._stream is not None else sys.stdin
        buf = []
        try:
            while not self._stop.is_set():
                line = stream.readline()
                if line == "":            # EOF
                    if buf:
                        self._bus.put("\n".join(buf), SRC_PIPE)
                        buf = []
                    self._bus.put_stop()
                    return
                line = line.rstrip("\r\n")
                if line == ".":
                    self._bus.put("\n".join(buf), SRC_PIPE)
                    buf = []
                elif line.startswith(".."):
                    buf.append(line[1:])
                else:
                    buf.append(line)
        except Exception:
            # stdin 炸了也要让主循环收工，别把整个 agent 卡死
            try:
                self._bus.put_stop()
            except Exception:
                pass

    def stop(self):
        self._stop.set()


# ============================================================
#  Socket 输入源
# ============================================================
class SocketSource(InputSource):
    """TCP 输入源：任意客户端连上来后按行发文本，每行就是一条输入。

    协议（刻意做得极简，方便 `nc` / `telnet` / 脚本直接对接）：
      - 每行文本 = 一条消息（空行忽略，行尾 \\r\\n 与 \\n 都兼容）
      - 以 '!' 开头的行为控制指令：!exit / !quit / !stop → 通知主循环退出
      - 若配置了 auth_token：连接后的第一行必须等于该 token，否则直接断开

    用法示例：
        echo "帮我看看磁盘占用" | nc 127.0.0.1 8765
        nc 127.0.0.1 8765        # 之后逐行输入，Ctrl+C 断开
    """

    name = "socket"

    def __init__(self, host="127.0.0.1", port=8765, auth_token="", encoding="utf-8"):
        self.host = host
        self.port = port
        self.auth_token = auth_token or ""
        self.encoding = encoding
        self._bus = None
        self._server = None
        self._stop = threading.Event()
        self._clients = []
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self, bus):
        self._bus = bus
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(8)
            # 设超时是为了 accept 能周期性回来看 _stop，从而优雅退出
            srv.settimeout(0.5)
        except Exception:
            try:
                srv.close()
            except Exception:
                pass
            raise
        self._server = srv
        threading.Thread(
            target=self._accept_loop, name="marisa-socket-accept", daemon=True
        ).start()
        print(f"   🌐 输入源 [socket] 已监听 {self.host}:{self.port}", flush=True)

    def stop(self):
        self._stop.set()
        srv = self._server
        if srv is not None:
            try:
                srv.close()
            except Exception:
                pass
        with self._lock:
            conns = list(self._clients)
            self._clients = []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

    # ---------- 内部 ----------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break          # server 被 close 掉了
            except Exception:
                continue
            with self._lock:
                self._clients.append(conn)
            threading.Thread(
                target=self._client_loop, args=(conn, addr),
                name="marisa-socket-client", daemon=True
            ).start()

    def _client_loop(self, conn, addr):
        # 注意：这里刻意【不】给 conn 设 timeout ——
        # 带超时的 socket 交给 makefile().readline() 会在超时时把缓冲区搞坏，
        # 退出清理统一靠 stop() 里 close(conn) 打断阻塞读。
        fileobj = None
        try:
            fileobj = conn.makefile("r", encoding=self.encoding,
                                    errors="replace", newline="\n")
            authed = not self.auth_token
            while not self._stop.is_set():
                try:
                    line = fileobj.readline()
                except (OSError, ValueError):
                    break
                if line == "":       # 对端关闭
                    break
                line = line.rstrip("\r\n")
                if not authed:
                    # 首行必须是 token
                    if line.strip() != self.auth_token:
                        try:
                            conn.sendall("auth failed\n".encode(self.encoding))
                        except Exception:
                            pass
                        break
                    authed = True
                    continue
                if line == "":
                    continue
                if line.startswith("!"):
                    cmd = line[1:].strip().lower()
                    if cmd in ("exit", "quit", "stop"):
                        print("\n🌐 socket 客户端请求退出\n", flush=True)
                        self._bus.put_stop()
                        break
                    continue
                self._bus.put(line, SRC_SOCKET)
        except Exception:
            pass
        finally:
            if fileobj is not None:
                try:
                    fileobj.close()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                try:
                    self._clients.remove(conn)
                except ValueError:
                    pass
