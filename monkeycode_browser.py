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
    """Read the VM Chromium target list without letting a stalled renderer abort login.

    Chromium's DevTools HTTP endpoint can briefly stop answering while a page is
    navigating or opening a new target. Retry a few times and let callers decide
    whether an empty snapshot is recoverable.
    """
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=2) as response:
                value = json.load(response)
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
        # existing VM target rather than forcing the mobile controller to use
        # VNC just to open the first page.
        page = browser_page()
        cdp(page, "Page.navigate", {"url": TARGET})
        time.sleep(1)
        page = next((p for p in pages() if p.get("type") == "page" and "monkeycode-ai.com" in p.get("url", "")), page)
    return page


def browser_page() -> dict:
    """Return a VM Chromium page even when it is still on about:blank."""
    candidates = [p for p in pages() if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
    # Prefer the actual MonkeyCode document; Chromium also exposes internal
    # omnibox pages which are not controllable login targets.
    page = next((p for p in candidates if "monkeycode-ai.com" in p.get("url", "")), None)
    page = page or next((p for p in candidates if p.get("url", "").startswith(("http://", "https://"))), None)
    if not page:
        raise RuntimeError("登录浏览器未创建可用页面；请稍后重试")
    return page


def github_login_url() -> dict:
    """Capture the real GitHub OAuth navigation through the VM DevTools protocol.

    Navigation and URL discovery are CDP Network events, not VNC pixels or a
    guessed OAuth endpoint. Runtime.evaluate is limited to choosing the real
    provider controls rendered by MonkeyCode's current login page.
    """
    start()
    page = browser_page()
    script = """(() => {
      const text = e => (e.innerText || e.textContent || '').trim();
      // Click only real interactive controls. Clicking a wrapping div can
      // invoke a parent handler twice or do nothing, which is dangerous for
      // OAuth because each provider click replaces the server-side state.
      const controls = [...document.querySelectorAll('button,a,[role="button"]')];
      const clickText = needle => {
        const el = controls
          .filter(x => text(x).includes(needle))
          .sort((a, b) => text(a).length - text(b).length)[0];
        if (el) { el.click(); return true; }
        return false;
      };
      // Each transition is clicked at most once. Repeated OAuth clicks replace
      // the server-side state and make the previously returned phone link fail.
      if (!window.__jk_baizhi_clicked && clickText('百智云')) {
        window.__jk_baizhi_clicked = true;
        return 'clicked-baizhi';
      }
      const checkbox = [...document.querySelectorAll('input[type=checkbox]')].find(x => !x.checked);
      if (checkbox && !window.__jk_terms_accepted) {
        checkbox.click(); window.__jk_terms_accepted = true;
        return 'accepted-terms';
      }
      const github = controls.find(x => /github/i.test((x.href || '') + ' ' + text(x) + ' ' + (x.getAttribute('aria-label') || '')));
      // Baizhi.Cloud exposes the provider via an aria-label, with no visible
      // text. Match both forms so the phone-login flow reaches GitHub.
      if (github && !window.__jk_github_clicked) {
        window.__jk_github_clicked = true;
        github.click();
        return 'clicked-github-once';
      }
      return 'waiting-github';
    })()"""
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True, timeout=8)
    try:
        next_id = 0

        def send(method: str, params: dict | None = None) -> int:
            nonlocal next_id
            next_id += 1
            ws.send(json.dumps({"id": next_id, "method": method, "params": params or {}}))
            return next_id

        def github_url(event: dict) -> str:
            request = event.get("params", {}).get("request", {})
            url = str(request.get("url") or "")
            return url if "github.com/login" in url and "client_id=" in url else ""

        send("Network.enable")
        send("Page.enable")
        # Reuse the selected MonkeyCode target and reset it to the login page.
        # This avoids attaching to Chromium's internal omnibox target and then
        # reporting a misleading OAuth timeout.
        send("Page.navigate", {"url": TARGET})
        deadline = time.time() + 25
        # Give the login SPA time to mount before inspecting controls. On a
        # repeat, the previous OAuth page may still be transitioning.
        next_click = time.time() + 2
        ready_at = time.time() + 1.5
        last_url = TARGET
        while time.time() < deadline:
            if time.time() >= next_click and time.time() >= ready_at:
                send("Runtime.evaluate", {"expression": script, "awaitPromise": True})
                next_click = time.time() + 1
            try:
                event = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if event.get("method") == "Network.requestWillBeSent":
                url = github_url(event)
                if url:
                    return {"url": url, "expiresAt": int((time.time() + 300) * 1000), "source": "vm-cdp-network"}
            if event.get("method") == "Page.frameNavigated":
                last_url = str(event.get("params", {}).get("frame", {}).get("url") or last_url)
            # GitHub may be opened in a new tab. The target-list fallback is
            # best-effort: a temporarily stalled /json/list must not discard a
            # valid Network event or turn a transient navigation delay into a
            # traceback shown to the phone user.
            try:
                candidates = pages()
            except RuntimeError:
                candidates = []
            for candidate in candidates:
                url = str(candidate.get("url") or "")
                if "github.com/login" in url and "client_id=" in url:
                    return {"url": url, "expiresAt": int((time.time() + 300) * 1000), "source": "vm-cdp-page"}
                # Ignore Chromium internal targets (for example omnibox
                # pages); they are not useful diagnostics for this flow.
                if url.startswith(("http://", "https://")):
                    last_url = url or last_url
    finally:
        ws.close()
    raise RuntimeError("未从 VM Chromium 的真实 Network 导航中捕获 GitHub OAuth 链接（当前页：%s）" % (last_url[:200] or "未知"))


def cookie() -> dict:
    result = cdp(monkeycode_page(), "Network.getAllCookies")
    found = next((c for c in result.get("result", {}).get("cookies", []) if c.get("name") == "monkeycode_ai_session" and "monkeycode-ai.com" in c.get("domain", "") and c.get("value")), None)
    if not found:
        raise RuntimeError("尚未登录 MonkeyCode；请在登录浏览器中完成登录")
    return {"cookie": "monkeycode_ai_session=" + found["value"], "expiresAt": int(found.get("expires", -1) * 1000) if found.get("expires", -1) > 0 else None}


def snapshot() -> dict:
    # During OAuth the active VM page is GitHub/Baizhi rather than MonkeyCode.
    # Snapshot the current web page so the phone UI never appears frozen.
    try:
        page = monkeycode_page()
    except RuntimeError:
        page = browser_page()
    shot = cdp(page, "Page.captureScreenshot", {"format": "jpeg", "quality": 62})
    text = cdp(page, "Runtime.evaluate", {"expression": "document.body ? document.body.innerText.slice(0, 12000) : ''", "returnByValue": True})
    value = text.get("result", {}).get("result", {}).get("value", "")
    return {"url": page.get("url", ""), "title": page.get("title", ""), "image": shot.get("result", {}).get("data", ""), "text": value}


def browser_action(payload: dict) -> dict:
    page = monkeycode_page()
    action, value = str(payload.get("action") or ""), str(payload.get("value") or "")
    if action == "auto_login":
        mode = str(payload.get("mode") or "password")
        account = str(payload.get("account") or "")
        password = str(payload.get("password") or "")
        if mode not in {"password", "baizhi", "github"}:
            raise RuntimeError("不支持的登录方式")
        if mode == "password" and (not account or not password):
            raise RuntimeError("账号密码登录需要账号和密码")
        # Navigate to the real MonkeyCode login page, select the requested
        # provider, then fill only visible form fields. No credential is saved.
        cdp(page, "Page.navigate", {"url": "https://monkeycode-ai.com/login"})
        time.sleep(1.2)
        if mode == "password":
            expr = f"""(()=>{{const a={json.dumps(account,ensure_ascii=False)},p={json.dumps(password,ensure_ascii=False)}; const es=[...document.querySelectorAll('input')]; const email=es.find(e=>e.type==='email'||/email|账号|account/i.test(e.placeholder||e.name||'')); const pw=es.find(e=>e.type==='password'); if(!email||!pw) throw new Error('未找到账号密码输入框'); const set=(e,v)=>{{const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;s.call(e,v);e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));}};set(email,a);set(pw,p);const b=[...document.querySelectorAll('button')].find(e=>/登录|sign in/i.test(e.innerText||''));if(!b)throw new Error('未找到登录按钮');b.click();return 'submitted';}})()"""
            result = cdp(page, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
        else:
            def evaluate(expression: str, target: dict | None = None) -> dict:
                target = target or browser_page()
                result = cdp(target, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
                if result.get("result", {}).get("exceptionDetails"):
                    detail = result["result"]["exceptionDetails"].get("exception", {}).get("description") or "页面操作失败"
                    raise RuntimeError(detail[:500])
                return result

            def accept_terms() -> None:
                evaluate("(()=>{const x=[...document.querySelectorAll('input[type=checkbox]')].find(e=>!e.checked);if(x)x.click();return 'ok'})()")

            def click_text(text: str, aria: bool = False) -> None:
                needle = json.dumps(text, ensure_ascii=False)
                field = "(e.getAttribute('aria-label')||'')" if aria else "(e.innerText||e.textContent||'')"
                deadline = time.time() + 15
                while time.time() < deadline:
                    expr = f"""(()=>{{const needle={needle};const xs=[...document.querySelectorAll('button,a,[role=button]')];const x=xs.filter(e=>{field}.includes(needle)).sort((a,b)=>(a.innerText||'').length-(b.innerText||'').length)[0];if(!x)return false;x.click();return true}})()"""
                    if evaluate(expr).get("result", {}).get("result", {}).get("value"):
                        return
                    time.sleep(.5)
                raise RuntimeError("未找到控件：" + text)

            # CN flow: MonkeyCode -> Baizhi -> consent -> provider.
            accept_terms()
            current_url = str(browser_page().get("url") or "")
            if "baizhi.cloud" not in current_url:
                click_text("百智云登录")
                time.sleep(2)
            accept_terms()
            if mode == "github":
                click_text("GitHub 登录", aria=True)
                # The real OAuth navigation opens GitHub in the same VM page.
                # Fill and submit GitHub credentials there; the callback then
                # returns to Baizhi/MonkeyCode in this same browser profile.
                deadline = time.time() + 20
                while time.time() < deadline:
                    target = browser_page()
                    if "github.com" in str(target.get("url") or ""):
                        def fill(selector: str, value: str) -> None:
                            expr = f"""(()=>{{const e=document.querySelector({json.dumps(selector)});if(!e)throw new Error('未找到 GitHub 输入框');const s=Object.getOwnPropertyDescriptor(e.constructor.prototype,'value')?.set||Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;s.call(e,{json.dumps(value,ensure_ascii=False)});e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));return true}})()"""
                            evaluate(expr, target)
                        fill('input[name="login"]', account)
                        fill('input[name="password"]', password)
                        evaluate("(()=>{const e=document.querySelector('input[type=submit],button[type=submit]');if(!e)throw new Error('未找到 GitHub 登录提交按钮');e.click();return true})()", target)
                        time.sleep(5)
                        break
                    time.sleep(.5)
        time.sleep(1)
        return snapshot()
    if action == "navigate":
        if not value.startswith("https://"):
            raise RuntimeError("只允许导航到 HTTPS 地址")
        result = cdp(page, "Page.navigate", {"url": value})
    elif action == "back":
        result = cdp(page, "Runtime.evaluate", {"expression": "history.back(); 'ok'", "returnByValue": True})
    elif action == "refresh":
        result = cdp(page, "Page.reload", {"ignoreCache": True})
    elif action == "home":
        result = cdp(page, "Page.navigate", {"url": TARGET})
    elif action == "click_text":
        needle = json.dumps(value, ensure_ascii=False)
        expr = f"""(()=>{{const needle={needle}; const xs=[...document.querySelectorAll('button,a,[role=button],input[type=submit]')]; const x=xs.filter(e=>(e.innerText||e.textContent||e.value||e.getAttribute('aria-label')||'').trim().includes(needle)).sort((a,b)=>(a.innerText||a.value||'').length-(b.innerText||b.value||'').length)[0]; if(!x) throw new Error('未找到控件：'+needle); x.click(); return x.innerText||x.value||x.getAttribute('aria-label')||'ok';}})()"""
        result = cdp(page, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
        if result.get("result", {}).get("exceptionDetails"):
            raise RuntimeError("未找到可点击控件：" + value)
    elif action == "type":
        result = cdp(page, "Input.insertText", {"text": value})
    elif action == "key":
        if value not in {"Enter", "Tab", "Escape", "ArrowLeft", "ArrowRight", "Backspace"}:
            raise RuntimeError("不支持的按键")
        result = cdp(page, "Input.dispatchKeyEvent", {"type": "keyDown", "key": value, "code": value})
        cdp(page, "Input.dispatchKeyEvent", {"type": "keyUp", "key": value, "code": value})
    else:
        raise RuntimeError("不支持的浏览器操作")
    time.sleep(0.4)
    return snapshot()


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
    elif action == "snapshot":
        print(json.dumps(snapshot(), ensure_ascii=False))
    elif action == "action":
        payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        print(json.dumps(browser_action(payload), ensure_ascii=False))
    else:
        raise SystemExit("usage: monkeycode_browser.py [install|start|restart|status|github-url|cookie|snapshot|action JSON]")
