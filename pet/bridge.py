#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宠物机（主控端）桥接器：拉取本机状态，转写成 dsh-pet 的工作状态。

只做本机环回出站请求，不监听任何对外端口。无第三方依赖，兼容 Python 3.9+。

常用：
    python bridge.py --check        # 自检：两端通不通、端口对不对，不启动
    python bridge.py --discover     # 只找 DSH 端口（会枚举本机所有监听端口）
    python bridge.py --dump         # 列出本机监听端口 + 所属进程，人工排查用
    python bridge.py --reset        # 清空 dsh-pet 的档位
    python bridge.py -v             # 正式运行
    python bridge.py --dry-run -v   # 只看会发什么，不真的调用 dsh-pet
"""

import argparse
import concurrent.futures
import http.client
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

DEFAULT_CANDIDATES = (3080, 3000, 8080, 8000, 5000, 4000, 3001, 5173)
LOCK_PORT = 19998
WORK_PATH = "/dsh-pet-7340/work-status"
STATE_PATH = "/dsh-pet-7340/state"
ALLOWED_STATES = ("thinking", "working", "result", "waiting", "success", "error")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# 只把这些进程名占用的端口当作「疑似 DSH」。任何 HTTP 服务对未知路径都会回 404，
# 不加名字过滤就会把 tomcat / 路由器后台之类误报成 DSH。
DSH_PROC = re.compile(r"dsh|deepseek|electron", re.I)
DISCOVER_BUDGET = 12.0

log = logging.getLogger("bridge")


def setup_logging(verbose):
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers = []

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.log")
    try:
        fh = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as exc:
        log.warning("日志文件不可写，仅输出到控制台：%s", exc)


# ---------------------------------------------------------------- HTTP 基础

def http_json(host, port, path, method="GET", body=None, timeout=2.0, token=None):
    """返回 (status, json_or_none, raw_text)。异常原样抛出。"""
    payload = None
    headers = {}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(payload))
    if token:
        headers["x-pet-token"] = token

    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        text = raw.decode("utf-8", "replace")
        data = None
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        return resp.status, data, text
    finally:
        conn.close()


# ---------------------------------------------------------------- 端口发现

def _run(cmd):
    try:
        return subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW
        ).decode("utf-8", "replace")
    except Exception:
        return ""


def listening_ports():
    """本机所有 TCP 监听端口 -> [(host, port, pid)]。用于枚举式发现。"""
    rows = []
    for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        host, _, port_s = parts[1].rpartition(":")
        if not port_s.isdigit():
            continue
        port = int(port_s)
        if port <= 0:
            continue
        rows.append((host, port, parts[4] if len(parts) > 4 else ""))
    return rows


def pid_names():
    """PID -> 进程名，给 --dump 用。"""
    names = {}
    for line in _run(["tasklist", "/fo", "csv", "/nh"]).splitlines():
        parts = line.split('","')
        if len(parts) >= 2:
            names[parts[1].strip('"')] = parts[0].strip('"')
    return names


def probe(port, host="127.0.0.1", timeout=1.2):
    """探测 dsh-pet 的 /state。返回 ok / auth / no-endpoint / http-N / None。"""
    try:
        status, _, _ = http_json(host, port, STATE_PATH, timeout=timeout)
    except Exception:
        return None
    if status == 200:
        return "ok"
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "no-endpoint"
    return "http-%d" % status


def probe_any_host(port, timeout=1.2):
    """先 IPv4 回环再 IPv6 回环 —— 有的服务只绑 ::1，只试 127.0.0.1 会误判。"""
    for host in ("127.0.0.1", "::1"):
        kind = probe(port, host, timeout)
        if kind:
            return kind, host
    return None, None


def discover_port(explicit, scan_range):
    """返回 (port, host, 说明)。找不到返回 (0, None, 原因)。

    只探测「确实在监听」的端口：盲试不存在的端口每次都要等超时，8 个候选就能拖掉 10 秒。
    """
    if explicit:
        kind, host = probe_any_host(explicit)
        if kind == "ok":
            return explicit, host, "命令行指定"
        if kind:
            return explicit, host, "命令行指定（%s）" % kind
        log.warning("指定端口 %d 无响应，继续探测", explicit)

    rows = listening_ports()
    names = pid_names()
    bound = {}
    for host, port, pid in rows:
        bound.setdefault(port, (host, pid, names.get(pid, "")))

    # 探测顺序：常见候选优先，再按端口号
    order = [p for p in DEFAULT_CANDIDATES if p in bound]
    order += sorted(p for p in bound if p >= 1024 and p != LOCK_PORT and p not in order)

    def _try(port):
        host, _pid, _name = bound[port]
        # 绑定在 0.0.0.0 / :: 的走回环；绑在具体地址的就用那个地址
        if host in ("0.0.0.0", "0.0.0.0:0"):
            cands = ["127.0.0.1"]
        elif host in ("::", "[::]"):
            cands = ["127.0.0.1", "::1"]
        elif host in ("::1", "[::1]"):
            cands = ["::1", "127.0.0.1"]
        else:
            cands = [host, "127.0.0.1"]
        for h in cands:
            kind = probe(port, h, 0.45)
            if kind:
                return port, h, kind
        return port, None, None

    if order:
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
            results = list(pool.map(_try, order))
        for port, host, kind in results:
            if kind == "ok":
                return port, host, "枚举监听端口命中"

        # 端口在、但端点不对：只认「进程名像 DSH」的，否则任何 HTTP 服务都会误报
        for port, host, kind in results:
            name = bound[port][2]
            if kind and DSH_PROC.search(name):
                return port, host, "命中但%s" % (
                    "需要鉴权" if kind == "auth" else "没有 dsh-pet 端点")

    # 用户指定扫描段（会真的去试未监听的端口，所以要给预算）
    if scan_range:
        try:
            lo_s, hi_s = scan_range.split("-")
            lo, hi = int(lo_s), int(hi_s)
            started = time.time()
            for port in range(lo, min(hi, lo + 200) + 1):
                if port in bound or time.time() - started > DISCOVER_BUDGET:
                    continue
                kind, host = probe_any_host(port, timeout=0.4)
                if kind == "ok":
                    return port, host, "扫描段命中"
        except ValueError:
            log.warning("--scan 格式应为 3000-4000")

    return 0, None, "未发现"


# ---------------------------------------------------------------- 桥接器

class Bridge(object):
    def __init__(self, opts):
        self.opts = opts
        u = urlparse(opts.state_url)
        self.s_host = u.hostname or "127.0.0.1"
        self.s_port = u.port or 80
        self.s_path = u.path or "/state"
        self.token = opts.token
        self.dsh_port = 0
        self.dsh_host = "127.0.0.1"
        self.applied_seq = -1
        self.since = 0
        self.last_ok = time.time()
        self.fails = 0
        self.next_clear = 0.0
        self.lock_sock = None

    # ---------- 基础设施 ----------
    def acquire_lock(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", LOCK_PORT))
            s.listen(1)
        except OSError:
            s.close()
            return False
        self.lock_sock = s
        return True

    def ensure_port(self):
        if self.dsh_port and probe(self.dsh_port, self.dsh_host) == "ok":
            return self.dsh_port
        port, host, how = discover_port(self.opts.dsh or 0, self.opts.scan)
        if port:
            if port != self.dsh_port:
                log.info("DSH 端口 = %d @ %s（%s）", port, host, how)
            self.dsh_port, self.dsh_host = port, host
            if host == "127.0.0.1":
                try:
                    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "dsh-port.txt"), "w", encoding="utf-8") as f:
                        f.write(str(port))
                except Exception:
                    pass
        else:
            self.dsh_port = 0
        return self.dsh_port

    # ---------- 动作 ----------
    def post_work(self, state, task):
        if self.opts.dry_run:
            log.info("[dry-run] POST %s %s", state, task or "")
            return True
        if not self.ensure_port():
            log.warning("DSH 不可达，跳过 state=%s", state)
            return False
        body = {"state": state}
        if task:
            body["task"] = task
        try:
            status, data, text = http_json(self.dsh_host, self.dsh_port, WORK_PATH,
                                           "POST", body, timeout=3.0)
            if status != 200 or (isinstance(data, dict) and data.get("ok") is False):
                log.error("work-status 被拒: %s %s", status, text[:160])
                return False
            return True
        except Exception as exc:
            log.error("work-status 失败: %s", exc)
            self.dsh_port = 0
            return False

    def clear_work(self, reason):
        self.next_clear = 0.0
        if self.opts.dry_run:
            log.info("[dry-run] DELETE work-status（%s）", reason)
            return True
        if not self.ensure_port():
            return False
        try:
            http_json(self.dsh_host, self.dsh_port, WORK_PATH, "DELETE", timeout=3.0)
            log.info("work-status 已清空（%s）", reason)
            return True
        except Exception as exc:
            log.warning("清空失败: %s", exc)
            self.dsh_port = 0
            return False

    # ---------- 主循环 ----------
    def poll(self):
        # 长轮询会挂住主循环，所以把挂起时长夹到「距离下次自清还有多久」，
        # 否则 success 的 TTL 会被顶住，实际延后整整一个轮询周期。
        timeout_ms = self.opts.poll_ms
        if self.next_clear:
            remain = int((self.next_clear - time.time()) * 1000)
            timeout_ms = max(200, min(timeout_ms, remain))
        path = "%s?since=%d&timeout=%d" % (self.s_path, self.since, timeout_ms)
        status, data, text = http_json(
            self.s_host, self.s_port, path,
            timeout=timeout_ms / 1000.0 + 4.0,
            token=self.token,
        )
        if status != 200:
            raise RuntimeError("state http %s" % status)
        if not isinstance(data, dict):
            raise RuntimeError("state 返回非 JSON: %s" % text[:120])
        return data

    def apply(self, snap):
        seq = int(snap.get("seq") or 0)
        if seq < self.applied_seq:
            log.warning("状态服务已重启（seq %d -> %d），重置判重", self.applied_seq, seq)
            self.applied_seq = -1
        self.since = seq
        if seq == self.applied_seq:
            return
        self.applied_seq = seq

        state = snap.get("state")
        task = snap.get("task")
        if state is None:
            self.clear_work("本机空闲")
            return

        if self.post_work(state, task):
            log.info("state -> %s / %s (seq %d)", state, task or "-", seq)
            # 每个档位都要重置自清计划：留着上一个 success 的截止时间会误伤新状态。
            # TTL 传 0 表示关闭该档位的自动清空。
            if state in ("success", "error") and self.opts.success_ttl > 0:
                self.next_clear = time.time() + self.opts.success_ttl / 1000.0
            elif state == "waiting" and self.opts.waiting_ttl > 0:
                self.next_clear = time.time() + self.opts.waiting_ttl / 1000.0
            else:
                self.next_clear = 0.0

    def run(self):
        if not self.acquire_lock():
            log.error("已有实例在运行（锁端口 %d 被占用），退出", LOCK_PORT)
            return 1

        log.info("启动 state=%s dsh=%s dry_run=%s",
                 self.opts.state_url, self.opts.dsh or "auto", self.opts.dry_run)
        self.ensure_port()

        backoff = 0.5
        while True:
            try:
                snap = self.poll()
                self.last_ok = time.time()
                self.fails = 0
                backoff = 0.5
                self.apply(snap)
            except KeyboardInterrupt:
                log.info("收到中断，退出")
                return 0
            except Exception as exc:
                self.fails += 1
                waited = time.time() - self.last_ok
                log.warning("拉取失败(%d次，上次成功 %.0fs 前): %s", self.fails, waited, exc)

                if waited > self.opts.stale:
                    self.clear_work("链路疑似中断")
                    self.last_ok = time.time()

                if self.fails >= self.opts.max_fail:
                    log.info("连续失败 %d 次，退出（交由 gate 或人工重启）", self.fails)
                    try:
                        self.clear_work("隧道断开")
                    except Exception:
                        pass
                    return 0

                time.sleep(backoff)
                backoff = min(backoff * 2, 6.0)
                continue

            if self.next_clear and time.time() >= self.next_clear:
                self.clear_work("档位自清")

    # ---------- 诊断 ----------
    def check(self):
        """两端 + 端口 + 锁，一次全查。排查时先跑这个。"""
        print("桥接器自检")
        print("  python          : %s" % sys.version.split()[0])
        print("  状态服务 URL    : %s" % self.opts.state_url)

        ok_state = False
        try:
            status, data, text = http_json(self.s_host, self.s_port, "/health",
                                           timeout=2.0, token=self.token)
            if status == 200 and isinstance(data, dict):
                ok_state = True
                print("  状态服务        : 可达  快照=%s" % json.dumps(
                    {k: data.get(k) for k in ("state", "task", "seq")}, ensure_ascii=False))
            else:
                print("  状态服务        : 异常  HTTP %s %s" % (status, text[:80]))
        except Exception as exc:
            print("  状态服务        : 不可达  %s" % exc)
            print("                    -> 检查 UU 端口映射面板是否为「已连接」")

        n_ports = len(listening_ports())
        port, host, how = discover_port(self.opts.dsh or 0, self.opts.scan)
        usable = how in ("命令行指定", "常见端口命中", "枚举监听端口命中", "扫描段命中")
        if port and usable:
            print("  DSH 端口        : %d @ %s（%s）" % (port, host, how))
            print("  dsh-pet 接口    : 可达")
        elif port:
            print("  DSH 端口        : %d @ %s（%s）" % (port, host, how))
            print("  dsh-pet 接口    : 不可用 —— 端口找到了，但这个端点上没有 dsh-pet 路由")
            print("                    -> 打开 DSH「插件管理」确认 dsh-pet 已安装，装完要重开 DSH")
        else:
            print("  DSH 端口        : 未发现（已枚举本机 %d 个监听端口）" % n_ports)
            print("                    -> 1) 确认 DSH 桌面版窗口还开着，不是缩到托盘后退出了")
            print("                    -> 2) 跑 python bridge.py --dump 看它到底在听哪个端口")
            print("                    -> 3) 知道端口后：python bridge.py --dsh <端口> --check")

        s = socket.socket()
        try:
            s.bind(("127.0.0.1", LOCK_PORT))
            print("  单实例锁        : 空闲（当前没有桥在跑）")
        except OSError:
            print("  单实例锁        : 被占用（已有桥在跑）")
        finally:
            s.close()

        print("  结论            : %s" % ("可以启动" if (ok_state and usable) else "先解决上面提示的那项"))
        return 0 if (ok_state and usable) else 1

    def dump(self):
        """把所有监听端口拉出来，附带进程名，人工排查用。"""
        names = pid_names()
        rows = sorted(listening_ports(), key=lambda r: r[1])
        print("本机 TCP 监听端口（%d 条）" % len(rows))
        print("  %-22s %-6s %-8s %s" % ("地址", "端口", "PID", "进程"))
        for host, port, pid in rows:
            print("  %-22s %-6d %-8s %s" % (host, port, pid, names.get(pid, "?")))
        print()
        print("挑 PID 属于 dsh / electron / node 的那几个端口，用")
        print("  python bridge.py --dsh <端口> --check")
        print("逐个验证。")


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy -> dsh-pet 桥接器")
    ap.add_argument("--state-url", default=os.environ.get("PET_STATE_URL",
                                                          "http://127.0.0.1:18787/state"))
    ap.add_argument("--token", default=os.environ.get("PET_TOKEN", ""))
    ap.add_argument("--dsh", type=int, default=int(os.environ.get("DSH_PORT", "0")),
                    help="DSH 端口；不填则自动探测")
    ap.add_argument("--scan", default="", help="探测失败时扫描端口段，如 3000-4000")
    ap.add_argument("--poll-ms", type=int, default=8000, help="长轮询挂起时长")
    ap.add_argument("--success-ttl", type=int, default=12000,
                    help="success/error 停留多久后自动回空闲（毫秒）")
    ap.add_argument("--waiting-ttl", type=int, default=120000,
                    help="waiting 最长停留多久后回空闲（毫秒），防止卡在等待动作")
    ap.add_argument("--stale", type=float, default=45.0, help="多久无成功拉取判定链路中断")
    ap.add_argument("--max-fail", type=int, default=2,
                    help="连续失败多少次后退出（默认 2，约 1 秒内响应隧道断开）")
    ap.add_argument("--check", action="store_true", help="自检后退出")
    ap.add_argument("--discover", action="store_true", help="只探测 DSH 端口")
    ap.add_argument("--dump", action="store_true", help="列出本机监听端口与所属进程")
    ap.add_argument("--reset", action="store_true", help="清空 dsh-pet 档位后退出")
    ap.add_argument("--poke", metavar="STATE", help="写入一个测试档位后退出（默认 working）")
    ap.add_argument("--dry-run", action="store_true", help="不真的调用 dsh-pet")
    ap.add_argument("-v", "--verbose", action="store_true")
    opts = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    setup_logging(opts.verbose)
    bridge = Bridge(opts)

    if opts.dump:
        bridge.dump()
        return 0

    if opts.discover:
        port, host, how = discover_port(opts.dsh, opts.scan)
        if port:
            print("DSH 端口: %d @ %s（%s）" % (port, host, how))
            return 0
        print("未发现 DSH。下一步：python bridge.py --dump 看它实际监听哪个端口")
        return 1

    if opts.check:
        return bridge.check()

    if opts.reset:
        print("已清空" if bridge.clear_work("手动 reset") else "清空失败（DSH 不可达？先跑 --check）")
        return 0

    if opts.poke is not None:
        st = opts.poke if opts.poke in ALLOWED_STATES else "working"
        if bridge.post_work(st, "连通性测试"):
            print("已写入 state=%s，看桌宠是否切换动作；看完用 --reset 收回" % st)
            return 0
        print("写入失败（DSH 不可达？先跑 --check）")
        return 1

    return bridge.run()


if __name__ == "__main__":
    sys.exit(main())
