#!/usr/bin/env python3
"""External watchdog for the jiankong monitor.

Runs as a separate process and re-launches server.py whenever it disappears.
The watchdog itself is kept alive by a cron entry (see install below); it only
spawns one child at a time and never stacks instances.

Usage:
  python3 watchdog.py            # foreground supervisor loop
  python3 watchdog.py install    # write the crontab entry that keeps this alive
"""
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
SERVER = BASE / "server.py"
LOG = BASE / "jiankong.log"
PID_FILE = BASE / "data" / "watchdog.pid"
SINGLE_LOCK = BASE / "data" / "watchdog.lock"


def already_running() -> bool:
    """True if another watchdog process owns the lock file and is alive."""
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not SINGLE_LOCK.exists():
            return False
        pid = int(SINGLE_LOCK.read_text().strip())
        os.kill(pid, 0)
        return pid != os.getpid()
    except (OSError, ValueError):
        return False


def write_lock() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    SINGLE_LOCK.write_text(str(os.getpid()))


def server_pid() -> int | None:
    """PID of the current server.py child, from the port listener."""
    try:
        out = subprocess.run(
            ["ss", "-ltnp"], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            if f":{PORT}" in line and "users:" in line:
                start = line.find("pid=") + 4
                end = line.find(",", start)
                return int(line[start:end])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


PORT = 8888


def health() -> bool:
    """Quick local probe - a live listener answering on 127.0.0.1 means up."""
    import urllib.request

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=8)
        return True
    except Exception:
        return False


def start_server() -> subprocess.Popen:
    write_lock()
    log = open(LOG, "ab", buffering=0)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONOPTIMIZE"] = "1"
    return subprocess.Popen(
        [sys.executable, str(SERVER)],
        cwd=str(BASE),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )


def main() -> None:
    if already_running():
        print("watchdog 已在运行，退出本实例", flush=True)
        return
    write_lock()
    print(f"[watchdog {os.getpid()}] 启动，看护 server.py（端口 {PORT}）", flush=True)
    child: subprocess.Popen | None = None
    consecutive_failures = 0
    while True:
        try:
            pid = server_pid()
            if pid is None or not health():
                if child is not None and child.poll() is None:
                    # Server process alive but not answering (hung): force kill.
                    print(f"[watchdog] server 无响应（pid={pid}），强制结束", flush=True)
                    try:
                        child.kill()
                        child.wait(timeout=10)
                    except subprocess.SubprocessError:
                        pass
                    child = None
                    time.sleep(2)
                    continue
                consecutive_failures += 1
                if consecutive_failures > 8:
                    print("[watchdog] 连续 8 次无法启动，放弃本周期，60 秒后重试", flush=True)
                    time.sleep(60)
                    consecutive_failures = 0
                    continue
                print(f"[watchdog] server 未运行，第 {consecutive_failures} 次启动", flush=True)
                child = start_server()
                time.sleep(6)
                continue
            consecutive_failures = 0
            if child is not None and child.poll() is not None:
                # Listener exists but our child exited (started by someone else
                # or restarted externally): re-attach by not spawning a new one.
                child = None
        except Exception as exc:  # noqa: BLE001 - watchdog must never die
            print(f"[watchdog] 循环异常：{exc}", flush=True)
        time.sleep(10)


def install() -> None:
    """Keep this watchdog alive without cron/systemd.

    Neither crontab nor a running systemd bus exists on this VM, so the watchdog
    cannot be scheduled externally. Instead jiankong itself runs a lightweight
    thread that respawns watchdog.py when it disappears (see server.py), and the
    watchdog does the same for server.py - a two-parent arrangement where the
    only way both die at once is an outright VM restart.
    """
    print("此环境无 crontab/systemd；watchdog 由 jiankong 内部线程互拉保活", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        install()
    else:
        main()
