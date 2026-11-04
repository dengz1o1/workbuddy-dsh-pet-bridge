#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机（被控端）状态服务。

职责：接收 WorkBuddy hook 事件，映射成 dsh-pet 的 6 档状态，供宠物机经 UU 隧道拉取。
约束：只绑 127.0.0.1；无第三方依赖；兼容 Python 3.9+（本机 3.13、宠物机 3.9 均可跑）。

用法：
    python state_server.py
    python state_server.py --port 8787 --debug
    python state_server.py --check          # 只看配置与端口是否就绪，不启动
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse, parse_qs

ALLOWED = ("thinking", "working", "result", "waiting", "success", "error")
MAX_TASK = 40
MAX_BODY = 8192
MAX_POLL_MS = 15000

# 权限确认时"保守呈现"：只暴露工具类别，不带命令内容、文件路径、参数。
# 宁可少说，也不让命令行里的路径/密钥被屏幕边上的人看见。
TOOL_ALIAS = {
    "Bash": "要执行命令",
    "Write": "要写文件",
    "Edit": "要改文件",
    "MultiEdit": "要改文件",
    "NotebookEdit": "要改文件",
    "Read": "要读文件",
    "WebFetch": "要联网抓取",
    "WebSearch": "要联网搜索",
}

log = logging.getLogger("state")


def permission_task(tool):
    """把工具名变成一句人话。取不到工具名就退回笼统说法。"""
    if not tool:
        return "等你确认"
    return (TOOL_ALIAS.get(tool) or ("要用 " + tool)) + "，等你点"


def tool_from_message(message):
    """从 'CodeBuddy needs your permission to use Bash' 里取工具名。"""
    m = re.search(r"use\s+([A-Za-z_][\w]*)\s*\.?\s*$", message or "")
    return m.group(1) if m else ""


# WorkBuddy 事件 -> dsh-pet 档位。改文案只改这里，注意 task 上限 40 个字符。
def map_event(payload):
    ev = payload.get("hook_event_name") or ""
    tool = payload.get("tool_name") or ""

    if ev == "UserPromptSubmit":
        return {"state": "thinking", "task": "正在理解需求"}
    if ev == "PreToolUse":
        if tool in ("Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "NotebookRead"):
            return {"state": "working", "task": "正在读资料"}
        if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "ApplyPatch"):
            return {"state": "working", "task": "正在改文件"}
        if tool == "Bash":
            return {"state": "working", "task": "正在执行命令"}
        if tool in ("Task", "Agent"):
            return {"state": "working", "task": "正在派活给子代理"}
        return {"state": "working", "task": "正在调用工具"}
    if ev == "PermissionRequest":
        return {"state": "waiting", "task": permission_task(payload.get("tool_name") or "")}
    if ev == "Notification":
        ntype = payload.get("notification_type")
        if ntype == "permission_prompt":
            tool = payload.get("tool_name") or tool_from_message(payload.get("message"))
            return {"state": "waiting", "task": permission_task(tool)}
        if ntype == "idle_prompt":
            # 「AI 干完了，等你输入」语义是空闲，不是等待。
            # 映射成 waiting 会让桌宠永久停在等待动作（waiting 优先级最高且不会自清）。
            return {"state": None}
        # auth_success 等其余通知：不动状态。之前一律当 waiting，导致登录成功也摆等待动作。
        return None
    if ev == "Stop":
        return {"state": "success", "task": "这轮做完了"}
    if ev == "SubagentStop":
        return {"state": "working", "task": "子任务结束"}
    if ev == "SessionEnd":
        return {"state": None}
    return None


class Store(object):
    """内存里的唯一一份状态。seq 只在状态真的变化时自增，桥接器据此判重。"""

    def __init__(self):
        self._cond = threading.Condition()
        self.state = None
        self.task = None
        self.seq = 0
        self.updated_at = time.time()

    def _snap_locked(self):
        return {
            "state": self.state,
            "task": self.task,
            "seq": self.seq,
            "updatedAt": int(self.updated_at * 1000),
            "ageMs": int((time.time() - self.updated_at) * 1000),
        }

    def snapshot(self):
        with self._cond:
            return self._snap_locked()

    def apply(self, state, task, keep_task=False):
        with self._cond:
            new_task = self.task if keep_task else task
            if state is None:
                new_task = None
            if new_task is not None:
                new_task = new_task[:MAX_TASK]
            if state != self.state or new_task != self.task:
                self.state = state
                self.task = new_task
                self.seq += 1
                self.updated_at = time.time()
                self._cond.notify_all()
                return True
            return False

    def wait_change(self, since, timeout_sec):
        with self._cond:
            if self.seq <= since:
                self._cond.wait(timeout_sec)
            return self._snap_locked()


STORE = Store()
CONF = {"token": ""}


class Handler(BaseHTTPRequestHandler):
    server_version = "wbpet-state/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- 基础工具 ----
    def _send(self, code, body, extra_headers=None):
        payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _auth_ok(self):
        if not CONF["token"]:
            return True
        return self.headers.get("x-pet-token") == CONF["token"]

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("payload-too-large")
        return self.rfile.read(length) if length else b""

    # ---- 路由 ----
    def do_GET(self):
        parsed = urlparse(self.path)
        if not self._auth_ok():
            return self._send(401, {"ok": False, "reason": "unauthorized"})

        if parsed.path == "/health":
            snap = STORE.snapshot()
            snap["ok"] = True
            return self._send(200, snap)

        if parsed.path == "/state":
            qs = parse_qs(parsed.query)
            if "since" not in qs or "timeout" not in qs:
                return self._send(200, STORE.snapshot())
            try:
                since = int(qs["since"][0])
                timeout_ms = min(max(int(qs["timeout"][0]), 0), MAX_POLL_MS)
            except (ValueError, IndexError):
                return self._send(200, STORE.snapshot())
            snap = STORE.wait_change(since, timeout_ms / 1000.0)
            try:
                return self._send(200, snap)
            except (BrokenPipeError, ConnectionResetError):
                return

        return self._send(404, {"ok": False, "reason": "not-found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._auth_ok():
            return self._send(401, {"ok": False, "reason": "unauthorized"})

        try:
            raw = self._read_body()
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError as exc:
            return self._send(400, {"ok": False, "reason": str(exc)})
        except Exception as exc:
            return self._send(400, {"ok": False, "reason": "invalid-json:%s" % exc})

        if parsed.path == "/event":
            mapped = map_event(data)
            if mapped is None:
                snap = STORE.snapshot()
                snap.update({"ok": True, "ignored": True})
                return self._send(200, snap)
            changed = STORE.apply(mapped.get("state"), mapped.get("task"))
            log.info("event %s %s%s -> %s / %s", data.get("hook_event_name"),
                     data.get("tool_name") or "-",
                     ("(" + data["notification_type"] + ")") if data.get("notification_type") else "",
                     STORE.state, STORE.task)
            snap = STORE.snapshot()
            snap.update({"ok": True, "changed": changed})
            return self._send(200, snap)

        if parsed.path == "/state":
            state = data.get("state")
            if state == "idle" or state == "":
                state = None
            if state is not None and state not in ALLOWED:
                return self._send(400, {"ok": False, "reason": "bad-state"})
            keep = "task" not in data
            changed = STORE.apply(state, data.get("task"), keep_task=keep)
            log.info("manual set -> %s / %s", STORE.state, STORE.task)
            snap = STORE.snapshot()
            snap.update({"ok": True, "changed": changed})
            return self._send(200, snap)

        return self._send(404, {"ok": False, "reason": "not-found"})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if not self._auth_ok():
            return self._send(401, {"ok": False, "reason": "unauthorized"})
        if parsed.path != "/state":
            return self._send(404, {"ok": False, "reason": "not-found"})
        STORE.apply(None, None)
        log.info("cleared -> idle")
        snap = STORE.snapshot()
        snap["ok"] = True
        return self._send(200, snap)


def setup_logging(debug):
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    log.handlers = []

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    path = os.environ.get("PET_LOG") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "state-server.log")
    try:
        fh = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as exc:
        log.warning("日志文件不可写，仅输出到控制台：%s", exc)


def preflight(port):
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True, "端口 %d 可用" % port
    except OSError as exc:
        return False, "端口 %d 不可用（%s）" % (port, exc)
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy -> dsh-pet 状态服务")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PET_STATE_PORT", "8787")))
    ap.add_argument("--token", default=os.environ.get("PET_TOKEN", ""))
    ap.add_argument("--debug", action="store_true", help="打印每个请求")
    ap.add_argument("--check", action="store_true", help="自检后退出，不启动服务")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    setup_logging(args.debug)
    CONF["token"] = args.token

    ok, msg = preflight(args.port)
    if args.check:
        print("状态服务自检")
        print("  python      : %s" % sys.version.split()[0])
        print("  监听端口    : %d  %s" % (args.port, msg))
        print("  鉴权 token  : %s" % ("已启用" if args.token else "关闭"))
        print("  当前状态    : %s" % json.dumps(STORE.snapshot(), ensure_ascii=False))
        return 0 if ok else 1
    if not ok:
        log.error(msg)
        return 1

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    log.info("listening http://127.0.0.1:%d  (token=%s)", args.port, "on" if args.token else "off")
    log.info("hook 写入: POST /event   桥接拉取: GET /state?since=&timeout=")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
