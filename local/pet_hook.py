#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy hook 转发器：把 stdin 的事件 JSON 原样转给 state_server 的 /event。

性能设计（hook 是同步阻塞的，每毫秒都算在对话延迟里）：
  * 用裸 socket 手写最小 HTTP 请求，不 import http.client（实测能省 ~170ms）
  * 只 import os / socket / sys / threading
  * 配合 `python -S` 启动（跳过 site 初始化，再省 ~200ms）
  实测总耗时 ~150ms，其中解释器自身启动占大头。

铁律：无论发生什么，都以 exit 0 在 ~300ms 内退出。
"""

import os
import socket
import sys
import threading

ENDPOINT = os.environ.get("PET_EVENT_URL", "http://127.0.0.1:8787/event")
TOKEN = os.environ.get("PET_TOKEN", "")
TIMEOUT = float(os.environ.get("PET_HOOK_TIMEOUT", "0.3"))


def bail(code=0):
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def parse_url(url):
    """极简解析 http://host:port/path，避免 import urllib。"""
    rest = url.split("://", 1)[-1]
    if "/" in rest:
        authority, path = rest.split("/", 1)
        path = "/" + path
    else:
        authority, path = rest, "/"
    host, _, port = authority.partition(":")
    return host or "127.0.0.1", int(port or 80), path


def main():
    watchdog = threading.Timer(max(TIMEOUT * 2.0, 1.0), bail)
    watchdog.daemon = True
    watchdog.start()

    stdin = sys.stdin
    if stdin is None or stdin.isatty():
        return bail(0)

    try:
        raw = stdin.buffer.read()
    except Exception:
        return bail(0)

    try:
        host, port, path = parse_url(ENDPOINT)
    except Exception:
        return bail(0)

    if not raw:
        raw = b"{}"

    head = "POST %s HTTP/1.1\r\nHost: %s:%d\r\nContent-Type: application/json; charset=utf-8\r\n" \
           "Content-Length: %d\r\nConnection: close\r\n" % (path, host, port, len(raw))
    if TOKEN:
        head += "x-pet-token: %s\r\n" % TOKEN
    head += "\r\n"

    try:
        sock = socket.create_connection((host, port), timeout=TIMEOUT)
        try:
            sock.sendall(head.encode("ascii") + raw)
            sock.recv(256)
        finally:
            sock.close()
    except Exception:
        pass
    return bail(0)


if __name__ == "__main__":
    main()
