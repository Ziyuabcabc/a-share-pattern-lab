#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地看板服务的「被拉起」入口（供 start_server.py 使用，也可单独运行）。

与直接执行 `python -m uvicorn` 的区别：
- 自身把 uvicorn 日志重定向到文件，因此拉起方无需处理标准输出重定向；
- 配合 `Invoke-CimMethod Win32_Process Create` 使用时，进程完全脱离调用方的
  作业对象（Job Object），调用方退出后服务继续监听。

仅绑定 127.0.0.1，不对外网开放。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="启动本地看板服务（独立进程入口）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log", default=str(ROOT / "output" / "uvicorn.log"))
    args = parser.parse_args()

    os.chdir(ROOT)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    log = open(log_path, "a", encoding="utf-8", buffering=1)
    log.write(
        f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"启动服务 pid={os.getpid()} port={args.port} =====\n"
    )
    sys.stdout = log
    sys.stderr = log

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port,
                log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
