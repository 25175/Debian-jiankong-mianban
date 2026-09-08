#!/usr/bin/env python3
"""Isolated MonkeyCode login browser for jiankong.

Runs Chromium inside Xvfb with a persistent profile. Its noVNC frontend is
published by the platform port preview; session cookies are only read locally
through Chromium's loopback DevTools endpoint.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket

BASE = Path(__file__).resolve().parent
DATA = BASE / "data" / "monkeycode-login-browser"
PROFILE = DATA / "profile"
RUN = DATA / "run"
LOG = DATA / "browser.log"
PORT = 6080
VNC = 5900
CDP = 9223
TARGET = "https://monkeycode-ai.com/console/tasks"


def pid_path(name: str) -> Path:
    return RUN / f"{name}.pid"


def alive(path: Path) -> bool:
    try:
        return os.kill(int(path.read_text().strip()), 0) is None
    except (OSError, ValueError):
        return False


def spawn(name: str, args: list[str], env: dict[str, str] | None = None) -> None:
    if alive(pid_path(name)):
        return
    with LOG.open("ab") as log:
        proc = subprocess.Popen(args, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    pid_path(name).write_text(str(proc.pid))


def vnc_auth_file() -> Path:
    token_file = BASE / "control-token"
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("控制令牌为空，无法保护登录浏览器")
    auth = RUN / "vnc.pass"
    if not auth.exists():
        subprocess.run(["x11vnc", "-storepasswd", token, str(auth)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(auth, 0o600)
    return auth


def start() -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    RUN.mkdir(parents=True, exist_ok=True)
    spawn("xvfb", ["Xvfb", ":99", "-screen", "0", "1440x900x24", "-nolisten", "tcp"])
    env = os.environ.copy()
    env["DISPLAY"] = ":99"
    if not alive(pid_path("fluxbox")):
        with LOG.open("ab") as log:
            proc = subprocess.Popen(["fluxbox"], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        pid_path("fluxbox").write_text(str(proc.pid))
    if not alive(pid_path("vnc")):
        with LOG.open("ab") as log:
            proc = subprocess.Popen(["x11vnc", "-display", ":99", "-forever", "-shared", "-rfbauth", str(vnc_auth_file()), "-localhost", "-rfbport", str(VNC)], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        pid_path("vnc").write_text(str(proc.pid))
    spawn("novnc", ["websockify", "--web", "/usr/share/novnc", str(PORT), f"localhost:{VNC}"])
    if not alive(pid_path("chromium")):
        spawn("chromium", ["chromium", "--no-sandbox", "--disable-dev-shm-usage", "--user-data-dir=" + str(PROFILE), "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(CDP), TARGET], env)
    time.sleep(2)
    return status()


def status() -> dict:
    return {"running": alive(pid_path("chromium")), "vnc": alive(pid_path("vnc")), "web": alive(pid_path("novnc")), "port": PORT, "cdp": CDP}


def cookie() -> dict:
    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=5))
    page = next((p for p in pages if p.get("type") == "page" and "monkeycode-ai.com" in p.get("url", "")), None)
    if not page:
        raise RuntimeError("登录浏览器尚未打开 MonkeyCode 页面")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True, timeout=8)
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        while True:
            result = json.loads(ws.recv())
            if result.get("id") == 1:
                break
    finally:
        ws.close()
    found = next((c for c in result.get("result", {}).get("cookies", []) if c.get("name") == "monkeycode_ai_session" and "monkeycode-ai.com" in c.get("domain", "") and c.get("value")), None)
    if not found:
        raise RuntimeError("尚未登录 MonkeyCode；请在登录浏览器中完成登录")
    return {"cookie": "monkeycode_ai_session=" + found["value"], "expiresAt": int(found.get("expires", -1) * 1000) if found.get("expires", -1) > 0 else None}


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "start":
        print(json.dumps(start(), ensure_ascii=False))
    elif action == "status":
        print(json.dumps(status(), ensure_ascii=False))
    elif action == "cookie":
        # Never print cookie outside an authenticated server-side caller.
        print(json.dumps(cookie(), ensure_ascii=False))
    else:
        raise SystemExit("usage: monkeycode_browser.py [start|status|cookie]")
