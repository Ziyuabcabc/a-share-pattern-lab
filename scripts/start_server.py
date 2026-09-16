#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地看板服务管理：重启 8765 端口的正式服务（真实数据）。

用法：
    python scripts/start_server.py            # 杀掉旧进程并启动，等待健康检查
    python scripts/start_server.py --port 8765

说明：服务以独立进程后台运行，日志写入 output/uvicorn.log，
仅绑定 127.0.0.1，不对外网开放。

启动策略（Windows）：优先用 WMI `Win32_Process.Create` 创建进程，
使 uvicorn 完全脱离调用方的作业对象——否则调用方（终端 / 编辑器宿主）
退出时会连带回收子进程，表现为「刚起来就掉线」。
WMI 不可用时回退到 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "output" / "uvicorn.log"


def _run_ps(script: str, timeout: int = 60) -> str:
    """执行一段 PowerShell 并合并返回 stdout + stderr（失败返回空串）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "") + (r.stderr or "")


def listening_pids(port: int) -> list[int]:
    """返回监听指定端口的进程号（优先 PowerShell，回退 netstat 解析）。"""
    out = _run_ps(
        f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
        f"-ErrorAction SilentlyContinue).OwningProcess -join ','"
    )
    pids = [int(x) for x in out.strip().split(",") if x.strip().isdigit()]
    if pids:
        return sorted(set(pids))

    try:
        raw = subprocess.run(
            f'netstat -ano | findstr ":{port} "',
            shell=True, capture_output=True, text=True, errors="replace",
        ).stdout
    except OSError:
        return []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            try:
                pids.append(int(parts[4]))
            except ValueError:
                continue
    return sorted(set(pids))


def kill_pids(pids: list[int]) -> None:
    for pid in pids:
        subprocess.run(f"taskkill /PID {pid} /F", shell=True,
                       capture_output=True, text=True, errors="replace")


def spawn_via_wmi(argv: list[str], cwd: str) -> int:
    """用 WMI 创建进程，返回 pid（0 表示失败）。

    WMI 创建出的进程不属于调用方的作业对象，因此调用方退出后仍然存活。
    """
    cmdline = subprocess.list2cmdline(argv).replace("'", "''")
    ps = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine = '" + cmdline + "'; CurrentDirectory = '"
        + cwd.replace("'", "''") + "'}; "
        "if ($r.ReturnValue -eq 0) { $r.ProcessId } else { 'ERR:' + $r.ReturnValue }"
    )
    out = _run_ps(ps).strip()
    return int(out) if out.isdigit() else 0


def spawn_detached_flags(argv: list[str], cwd: str, log) -> int:
    """回退方案：DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP。"""
    creation = 0
    if os.name == "nt":
        creation = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        argv, cwd=cwd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        creationflags=creation, close_fds=True,
    )
    return proc.pid


def wait_health(port: int, timeout: float = 30.0) -> bool:
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # 本机注入了 HTTP(S)_PROXY，访问 127.0.0.1 必须显式绕过代理
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.6)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="重启本地看板服务")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    pids = listening_pids(args.port)
    if pids:
        print(f"停止端口 {args.port} 上的旧进程: {pids}")
        kill_pids(pids)
        time.sleep(1.2)
    else:
        print(f"端口 {args.port} 当前无监听进程")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        str(ROOT / "scripts" / "serve_local.py"),
        "--port", str(args.port),
        "--log", str(LOG_PATH),
    ]

    pid = spawn_via_wmi(argv, str(ROOT))
    if pid:
        print(f"已通过 WMI 启动独立进程 pid={pid}")
    else:
        print("WMI 启动失败，回退到 DETACHED_PROCESS 方式")
        log = open(LOG_PATH, "a", encoding="utf-8")
        pid = spawn_detached_flags(argv, str(ROOT), log)

    if wait_health(args.port):
        print(f"服务已启动: http://127.0.0.1:{args.port}   pid={pid}   日志: {LOG_PATH}")
        return 0
    print(f"服务启动后健康检查未通过，请查看日志: {LOG_PATH}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
