#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地看板服务管理：重启 8765 端口的正式服务（真实数据）。

用法：
    python scripts/start_server.py            # 杀掉旧进程并启动，等待健康检查
    python scripts/start_server.py --port 8765

说明：服务以独立进程后台运行，日志写入 output/uvicorn.log，
仅绑定 127.0.0.1，不对外网开放。
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


def listening_pids(port: int) -> list[int]:
    """返回监听指定端口的进程号（Windows netstat 解析）。"""
    try:
        out = subprocess.run(
            f'netstat -ano | findstr ":{port} "',
            shell=True, capture_output=True, text=True, errors="replace",
        ).stdout
    except OSError:
        return []
    pids: list[int] = []
    for line in out.splitlines():
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


def wait_health(port: int, timeout: float = 30.0) -> bool:
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
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
    log = open(LOG_PATH, "a", encoding="utf-8")
    log.write(f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动服务 =====\n")
    log.flush()

    creation = 0
    if os.name == "nt":
        creation = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "info"],
        cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        creationflags=creation, close_fds=True,
    )

    if wait_health(args.port):
        print(f"服务已启动: http://127.0.0.1:{args.port}   日志: {LOG_PATH}")
        return 0
    print(f"服务启动后健康检查未通过，请查看日志: {LOG_PATH}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
