#!/usr/bin/env python3
"""Isolated MonkeyCode login browser for jiankong.

Runs Chromium inside Xvfb with a persistent profile. Its noVNC frontend is
published by the platform port preview; session cookies are only read locally
through Chromium's loopback DevTools endpoint.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import typing
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
# Chromium 152 listens on [::1] for --remote-debugging-address=127.0.0.1;
# pages() picks the working stack and caches it here for the whole run.
CDP_HOST: list[str] = ["127.0.0.1"]
TARGET = "https://monkeycode-ai.com/console/tasks"
VNC_PASSWORD = "1"
REQUIRED_COMMANDS = ("Xvfb", "fluxbox", "x11vnc", "websockify", "chromium")


def pid_path(name: str) -> Path:
    return RUN / f"{name}.pid"


def alive(path: Path) -> bool:
    try:
        return os.kill(int(path.read_text().strip()), 0) is None
    except (OSError, ValueError):
        return False


def spawn(name: str, args: list[str], env: dict | None = None) -> None:
    if alive(pid_path(name)):
        return
    # Chromium in this VM repeatedly dies with EMFILE ("Too many open files")
    # because the inherited 1024 soft fd limit is far below what a running
    # browser plus its restarted network service needs. Raise it for the whole
    # browser process tree; the hard limit (4096) is untouched.
    preexec: typing.Callable[[], None] | None = None
    if name == "chromium":
        def preexec() -> None:
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    with LOG.open("ab") as log:
        proc = subprocess.Popen(args, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True, preexec_fn=preexec)
    pid_path(name).write_text(str(proc.pid))


def missing_dependencies() -> list[str]:
    return [name for name in REQUIRED_COMMANDS if not shutil.which(name)]


def install_dependencies() -> dict:
    missing = missing_dependencies()
    if not missing:
        return {"installed": True, "changed": False, "message": "VM 登录浏览器运行环境已安装"}
    apt = shutil.which("apt-get")
    if not apt:
        raise RuntimeError("当前系统缺少 apt-get，无法自动安装 VM 登录浏览器组件：" + ", ".join(missing))
    # Debian packages give a compatible Chromium/Xvfb/noVNC stack. No external
    # download mirror is used, so it works through the VM's normal domestic or
    # international apt mirror configuration.
    packages = ["chromium", "xvfb", "fluxbox", "x11vnc", "novnc", "websockify", "fonts-noto-cjk"]
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    proc = subprocess.run([apt, "update"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
    if proc.returncode:
        raise RuntimeError("刷新系统软件源失败：" + proc.stdout[-800:])
    proc = subprocess.run([apt, "install", "-y", "--no-install-recommends", *packages], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
    if proc.returncode:
        raise RuntimeError("安装 VM 登录浏览器组件失败：" + proc.stdout[-1200:])
    missing = missing_dependencies()
    if missing:
        raise RuntimeError("安装完成但仍缺少组件：" + ", ".join(missing))
    return {"installed": True, "changed": True, "message": "已安装 Chromium、中文字体、Xvfb、x11vnc 与 noVNC"}


def vnc_auth_file() -> tuple[Path, bool]:
    auth = RUN / "vnc.pass"
    desired = RUN / "vnc.pass.version"
    # VNC password is intentionally independent of the jiankong control token.
    # This stable default is part of the plugin contract: password = 1.
    needs_write = not auth.exists() or not desired.exists() or desired.read_text(encoding="utf-8").strip() != VNC_PASSWORD
    if needs_write:
        auth.unlink(missing_ok=True)
        subprocess.run(["x11vnc", "-storepasswd", VNC_PASSWORD, str(auth)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        desired.write_text(VNC_PASSWORD, encoding="utf-8")
        os.chmod(auth, 0o600)
    return auth, needs_write


def stop_process(name: str) -> None:
    path = pid_path(name)
    if alive(path):
        try:
            os.kill(int(path.read_text().strip()), 15)
        except (OSError, ValueError):
            pass
        for _ in range(20):
            if not alive(path):
                break
            time.sleep(0.1)
    path.unlink(missing_ok=True)


def start() -> dict:
    missing = missing_dependencies()
    if missing:
        raise RuntimeError("VM 登录浏览器插件尚未安装：" + ", ".join(missing))
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
    auth, password_changed = vnc_auth_file()
    if password_changed:
        # x11vnc reads its password file only at startup; restart it so a
        # migration from an older control-token password takes effect now.
        stop_process("vnc")
    if not alive(pid_path("vnc")):
        with LOG.open("ab") as log:
            proc = subprocess.Popen(["x11vnc", "-display", ":99", "-forever", "-shared", "-rfbauth", str(auth), "-localhost", "-rfbport", str(VNC)], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        pid_path("vnc").write_text(str(proc.pid))
    spawn("novnc", ["websockify", "--web", "/usr/share/novnc", str(PORT), f"localhost:{VNC}"])
    if not alive(pid_path("chromium")):
        # This VM runs with ~8GB RAM and no swap; a full Chromium with default
        # process counts OOMs within minutes. Cap renderer/GPU processes and
        # disable the memory-hungry background services the login browser does
        # not need.
        spawn("chromium", ["chromium", "--no-sandbox", "--disable-dev-shm-usage", "--lang=zh-CN", "--accept-lang=zh-CN,zh", "--user-data-dir=" + str(PROFILE), "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(CDP), "--renderer-process-limit=2", "--gpu-process-limit=1", "--disable-background-networking", "--disable-component-update", "--disable-default-apps", "--disable-extensions", "--disable-gpu", "--disable-sync", "--no-first-run", "--disable-features=GCM", TARGET], env)
    time.sleep(2)
    return status()


def status() -> dict:
    missing = missing_dependencies()
    return {"installed": not missing, "missing": missing, "running": alive(pid_path("chromium")), "vnc": alive(pid_path("vnc")), "web": alive(pid_path("novnc")), "port": PORT, "cdp": CDP}


def pages() -> list[dict]:
    """Read the VM Chromium target list without letting a stalled renderer abort login.

    Chromium's DevTools HTTP endpoint can briefly stop answering while a page is
    navigating or opening a new target. Retry a few times and let callers decide
    whether an empty snapshot is recoverable.
    """
    # Chromium 152 binds DevTools to [::1] even when launched with
    # --remote-debugging-address=127.0.0.1, so the IPv4 URL gets refused.
    # Probe both stacks; the address that accepts /json/list wins.
    last_error: Exception | None = None
    for attempt in range(3):
        for host in ("127.0.0.1", "[::1]"):
            try:
                with urllib.request.urlopen(f"http://{host}:{CDP}/json/list", timeout=2) as response:
                    value = json.load(response)
                CDP_HOST[0] = host
                return value if isinstance(value, list) else []
            except (OSError, TimeoutError, ValueError) as exc:
                last_error = exc
        if attempt < 2:
            time.sleep(0.15 * (attempt + 1))
    raise RuntimeError(f"VM Chromium DevTools {CDP} 暂时无响应：{last_error}")


def cdp(page: dict, method: str, params: dict | None = None) -> dict:
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True, timeout=8)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            result = json.loads(ws.recv())
            if result.get("id") == 1:
                return result
    finally:
        ws.close()


def monkeycode_page() -> dict:
    page = next((p for p in pages() if p.get("type") == "page" and "monkeycode-ai.com" in p.get("url", "")), None)
    if not page:
        # A fresh login browser can still be on about:blank. Navigate the
        # existing VM target rather than requiring a manual navigation just
        # to open the first page.
        page = browser_page()
        cdp(page, "Page.navigate", {"url": TARGET})
        time.sleep(1)
        page = next((p for p in pages() if p.get("type") == "page" and "monkeycode-ai.com" in p.get("url", "")), page)
    return page


def browser_page() -> dict:
    """Return a real web page, waiting through Chromium target replacement."""
    deadline = time.time() + 15
    last = None
    while time.time() < deadline:
        try:
            candidates = [p for p in pages() if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
            page = next((p for p in candidates if p.get("url", "").startswith(("http://", "https://"))), None)
            if page:
                return page
            last = "没有可用的 page target"
        except RuntimeError as exc:
            last = str(exc)
        time.sleep(.4)
    raise RuntimeError(f"登录浏览器未创建可用页面：{last or '未知错误'}")


def cookie() -> dict:
    result = cdp(monkeycode_page(), "Network.getAllCookies")
    found = next((c for c in result.get("result", {}).get("cookies", []) if c.get("name") == "monkeycode_ai_session" and "monkeycode-ai.com" in c.get("domain", "") and c.get("value")), None)
    if not found:
        raise RuntimeError("尚未登录 MonkeyCode；请在登录浏览器中完成登录")
    return {"cookie": "monkeycode_ai_session=" + found["value"], "expiresAt": int(found.get("expires", -1) * 1000) if found.get("expires", -1) > 0 else None}


def restart() -> dict:
    path = pid_path("chromium")
    if alive(path):
        try:
            os.kill(int(path.read_text().strip()), 15)
        except (OSError, ValueError):
            pass
        time.sleep(1)
    path.unlink(missing_ok=True)
    return start()


def stop() -> dict:
    """Stop every browser component but keep the profile and login data intact."""
    stop_process("chromium")
    stop_process("vnc")
    stop_process("novnc")
    stop_process("xvfb")
    return status()


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "install":
        print(json.dumps(install_dependencies(), ensure_ascii=False))
    elif action == "start":
        print(json.dumps(start(), ensure_ascii=False))
    elif action == "restart":
        print(json.dumps(restart(), ensure_ascii=False))
    elif action == "stop":
        print(json.dumps(stop(), ensure_ascii=False))
    elif action == "status":
        print(json.dumps(status(), ensure_ascii=False))
    elif action == "github-url":
        print(json.dumps(github_login_url(), ensure_ascii=False))
    elif action == "cookie":
        # Chromium can be dead (EMFILE crash, OOM, manual kill) while its pid
        # file lingers. A cookie read then fails with a confusing "DevTools
        # 暂时无响应" instead of restarting the browser. Bring it back first;
        # start() is a no-op for everything already alive.
        start()
        print(json.dumps(cookie(), ensure_ascii=False))
    else:
        raise SystemExit("usage: monkeycode_browser.py [install|start|stop|restart|status|github-url|cookie]")
