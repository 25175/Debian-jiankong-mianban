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
VNC_PASSWORD = "1"
REQUIRED_COMMANDS = ("Xvfb", "fluxbox", "x11vnc", "websockify", "chromium")


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
        spawn("chromium", ["chromium", "--no-sandbox", "--disable-dev-shm-usage", "--lang=zh-CN", "--accept-lang=zh-CN,zh", "--user-data-dir=" + str(PROFILE), "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=" + str(CDP), TARGET], env)
    time.sleep(2)
    return status()


def status() -> dict:
    missing = missing_dependencies()
    return {"installed": not missing, "missing": missing, "running": alive(pid_path("chromium")), "vnc": alive(pid_path("vnc")), "web": alive(pid_path("novnc")), "port": PORT, "cdp": CDP, "vnc_password_hint": "1"}


def pages() -> list[dict]:
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=5))


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
        raise RuntimeError("登录浏览器尚未打开 MonkeyCode 页面")
    return page


def browser_page() -> dict:
    """Return a VM Chromium page even when it is still on about:blank."""
    page = next((p for p in pages() if p.get("type") == "page" and p.get("webSocketDebuggerUrl")), None)
    if not page:
        raise RuntimeError("登录浏览器未创建可用页面；请稍后重试")
    return page


def github_login_url() -> dict:
    """Drive the already-isolated VM browser to MonkeyCode's GitHub OAuth URL.

    The returned URL is intentionally short-lived OAuth state, not a Cookie.
    GitHub authorization happens on the user's phone; the VM browser remains
    the session whose Cookie is later polled and synchronized by jiankong.
    """
    start()
    # A newly created Chromium profile can initially expose only about:blank;
    # navigate that real page before requiring a MonkeyCode URL to exist.
    page = browser_page()
    cdp(page, "Page.navigate", {"url": TARGET})
    script = """(() => {
      const text = e => (e.innerText || e.textContent || '').trim();
      const clickText = needle => {
        const el = [...document.querySelectorAll('button,a,[role=button],div,span')]
          .find(x => text(x).includes(needle));
        if (el) { el.click(); return true; }
        return false;
      };
      clickText('百智云');
      const checkbox = [...document.querySelectorAll('input[type=checkbox]')].find(x => !x.checked);
      if (checkbox) checkbox.click();
      const github = [...document.querySelectorAll('a,button,[role=button]')]
        .find(x => /github/i.test((x.href || '') + ' ' + text(x) + ' ' + (x.getAttribute('aria-label') || '')));
      if (github) { github.click(); return 'clicked-github'; }
      return 'waiting-github';
    })()"""
    deadline = time.time() + 15
    last_url = ""
    while time.time() < deadline:
        try:
            page = monkeycode_page()
            cdp(page, "Runtime.evaluate", {"expression": script, "awaitPromise": True})
        except RuntimeError:
            pass
        time.sleep(1)
        for candidate in pages():
            url = str(candidate.get("url") or "")
            if "github.com/login" in url and "client_id=" in url:
                return {"url": url, "expiresAt": int((time.time() + 300) * 1000)}
            last_url = url or last_url
    raise RuntimeError("未能从 MonkeyCode 登录页取得 GitHub 授权链接；请检查百智云登录页是否已加载（当前页：%s）" % (last_url[:200] or "未知"))


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


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "install":
        print(json.dumps(install_dependencies(), ensure_ascii=False))
    elif action == "start":
        print(json.dumps(start(), ensure_ascii=False))
    elif action == "restart":
        print(json.dumps(restart(), ensure_ascii=False))
    elif action == "status":
        print(json.dumps(status(), ensure_ascii=False))
    elif action == "github-url":
        print(json.dumps(github_login_url(), ensure_ascii=False))
    elif action == "cookie":
        # Never print cookie outside an authenticated server-side caller.
        print(json.dumps(cookie(), ensure_ascii=False))
    else:
        raise SystemExit("usage: monkeycode_browser.py [install|start|restart|status|github-url|cookie]")
