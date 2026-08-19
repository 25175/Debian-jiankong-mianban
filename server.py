#!/usr/bin/env python3
"""Portable Linux service monitor with optional safe service controls."""
from __future__ import annotations

import collections
import glob
import html
import http.client
import json
import os
import re
import secrets
import select
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "jiankong.json"
TOKEN_PATH = BASE / "control-token"
STARTED = time.time()
CONFIG_DEFAULT = {
    "name": "Debian 服务监控面板",
    "listen": {"host": "0.0.0.0", "port": 8888},
    "services": [],
    "browser": {"enabled": True, "port": None},
    "multica": {"enabled": True, "executable": None, "working_directory": None, "runtime_id": None},
    "codex": {"enabled": True, "sessions_dir": None},
}


def merge_config(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    try:
        return merge_config(CONFIG_DEFAULT, json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return dict(CONFIG_DEFAULT)


CONFIG = load_config()
HOST = str(CONFIG["listen"].get("host") or "0.0.0.0")
PORT = int(CONFIG["listen"].get("port") or 8888)
TOKEN = TOKEN_PATH.read_text(encoding="utf-8").strip() if TOKEN_PATH.exists() else ""
TASK_CACHE: dict = {"at": 0.0, "data": {"available": False, "running": 0, "recent": [], "error": "正在读取任务"}}
TASK_CACHE_TTL = 15
ISSUE_TITLE_CACHE: dict[str, str] = {}
TASK_LOCK = threading.Lock()
RESOURCE_HISTORY = collections.deque(maxlen=1200)
RESOURCE_LOCK = threading.Lock()
RESOURCE_LAST: dict = {"at": 0.0, "cpu": None, "net": None}
BROWSER_TICKETS: dict[str, float] = {}
BROWSER_SESSIONS: dict[str, float] = {}
BROWSER_LOCK = threading.Lock()
BROWSER_TTL = 300
SERVICE_TARGETS: dict[str, dict] = {}
SERVICE_LOCK = threading.Lock()


def command(*args: str, timeout: int = 8, cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _prune_browser_tokens() -> None:
    now = time.time()
    with BROWSER_LOCK:
        for store in (BROWSER_TICKETS, BROWSER_SESSIONS):
            for key, expires in list(store.items()):
                if expires <= now:
                    store.pop(key, None)


def browser_ticket() -> str:
    _prune_browser_tokens()
    ticket = secrets.token_urlsafe(32)
    with BROWSER_LOCK:
        BROWSER_TICKETS[ticket] = time.time() + 60
    return ticket


def consume_browser_ticket(ticket: str) -> bool:
    _prune_browser_tokens()
    with BROWSER_LOCK:
        return bool(BROWSER_TICKETS.pop(ticket, None))


def browser_session(cookie: str) -> bool:
    _prune_browser_tokens()
    token = next((part.strip()[7:] for part in cookie.split(";") if part.strip().startswith("jk_vnc=")), "")
    with BROWSER_LOCK:
        return BROWSER_SESSIONS.get(token, 0) > time.time()


def new_browser_session() -> str:
    session = secrets.token_urlsafe(32)
    with BROWSER_LOCK:
        BROWSER_SESSIONS[session] = time.time() + BROWSER_TTL
    return session


def _read_cpu() -> tuple[int, int]:
    try:
        parts = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:]))
        return sum(parts), parts[3] + (parts[4] if len(parts) > 4 else 0)
    except (OSError, IndexError, ValueError):
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
        count = os.cpu_count() or 1
        return int(load * 1000), int(max(0, count * 1000 - load * 1000))


def _active_iface() -> str:
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000":
                return fields[0]
    except OSError:
        pass
    code, output = command("route", "-n", "get", "default")
    if code == 0:
        match = re.search(r"interface:\s*(\S+)", output)
        if match:
            return match.group(1)
    return ""


def _net_bytes(iface: str) -> tuple[int, int]:
    if not iface:
        return 0, 0
    try:
        proc_dev = Path("/proc/net/dev").read_text()
    except OSError:
        return 0, 0
    line = next((x for x in proc_dev.splitlines() if x.strip().startswith(iface + ":")), "")
    values = line.split(":", 1)[1].split() if ":" in line else []
    return (int(values[0]), int(values[8])) if len(values) > 8 else (0, 0)


def resources() -> dict:
    now = time.time()
    iface = _active_iface()
    total, idle = _read_cpu()
    rx, tx = _net_bytes(iface)
    try:
        memory = {key: int(value.strip().split()[0]) * 1024 for key, value in (x.split(":", 1) for x in Path("/proc/meminfo").read_text().splitlines() if ":" in x)}
        mem_total = memory.get("MemTotal", 1)
        mem_used = mem_total - memory.get("MemAvailable", 0)
    except (OSError, ValueError):
        try:
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            total_pages = int(os.sysconf("SC_PHYS_PAGES"))
            available_pages = int(os.sysconf("SC_AVPHYS_PAGES")) if "SC_AVPHYS_PAGES" in os.sysconf_names else 0
            mem_total = max(1, total_pages * page_size)
            mem_used = max(0, mem_total - available_pages * page_size)
        except (OSError, ValueError, TypeError):
            mem_total, mem_used = 1, 0
    disk = shutil.disk_usage("/")
    with RESOURCE_LOCK:
        previous = RESOURCE_LAST.copy()
        dt = now - previous["at"] if previous["at"] else 0
        cpu = 0.0 if previous["cpu"] is None else max(0.0, min(100.0, 100 * (1 - (idle - previous["cpu"][1]) / max(1, total - previous["cpu"][0]))))
        down = 0.0 if not previous["net"] or dt <= 0 else max(0, (rx - previous["net"][0]) / dt)
        up = 0.0 if not previous["net"] or dt <= 0 else max(0, (tx - previous["net"][1]) / dt)
        RESOURCE_LAST.update({"at": now, "cpu": (total, idle), "net": (rx, tx)})
        sample = {"t": int(now), "cpu": round(cpu, 1), "memory": round(mem_used / mem_total * 100, 1), "disk": round(disk.used / disk.total * 100, 1), "down": round(down, 1), "up": round(up, 1)}
        if not RESOURCE_HISTORY or now - RESOURCE_HISTORY[-1]["t"] >= 2:
            RESOURCE_HISTORY.append(sample)
        recent = [x for x in RESOURCE_HISTORY if now - x["t"] <= 60]
        hour = list(RESOURCE_HISTORY)

    def state(pct: float) -> str:
        return "danger" if pct >= 90 else ("warning" if pct >= 75 else "ok")

    return {"sample": sample, "memory": {"used": mem_used, "total": mem_total, "state": state(sample["memory"])}, "disk": {"used": disk.used, "total": disk.total, "state": state(sample["disk"])}, "cpu": {"state": state(sample["cpu"])}, "network": {"iface": iface, "down": down, "up": up, "state": "ok" if iface else "danger"}, "recent": recent, "hour": hour}


def systemd_units() -> dict[str, dict]:
    code, output = command("systemctl", "--user", "list-units", "--type=service", "--all", "--no-legend", "--no-pager")
    if code != 0:
        return {}
    units = {}
    for line in output.splitlines():
        fields = line.split(None, 4)
        if not fields or not fields[0].endswith(".service"):
            continue
        unit = fields[0]
        show_code, show = command("systemctl", "--user", "show", unit, "--property=MainPID,Description,ActiveState,SubState")
        values = dict(item.split("=", 1) for item in show.splitlines() if "=" in item) if show_code == 0 else {}
        units[unit] = {"unit": unit, "pid": int(values.get("MainPID", "0") or 0), "name": values.get("Description") or fields[-1], "active": values.get("ActiveState") == "active", "state": values.get("SubState") or fields[2]}
    return units


def listening_ports() -> list[dict]:
    code, output = command("ss", "-ltnpH")
    ports = []
    if code == 0:
        for line in output.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            try:
                port = int(fields[3].rsplit(":", 1)[-1].rstrip("]"))
            except ValueError:
                continue
            match = re.search(r'users:\(\("([^"]+)",pid=(\d+)', " ".join(fields[4:]))
            pid = int(match.group(2)) if match else 0
            process_name = match.group(1) if match else ""
            ports.append({"port": port, "pid": pid, "process": process_name, "address": fields[3]})
    return sorted({(x["port"], x["pid"], x["process"]): x for x in ports}.values(), key=lambda x: x["port"])


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
            return True
    except OSError:
        return False


def config_match(item: dict, rule: dict) -> bool:
    match = rule.get("match", rule)
    return bool((match.get("unit") and match["unit"] == item.get("unit")) or (match.get("port") is not None and int(match["port"]) == item.get("port")) or (match.get("process") and match["process"].lower() in item.get("process", "").lower()))


def safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "service"


def public_host(host_header: str) -> str:
    host = (host_header or "").split(",", 1)[0].strip()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def service_status(host_header: str = "") -> list[dict]:
    units = systemd_units()
    discovered = listening_ports()
    rules = CONFIG.get("services", []) if isinstance(CONFIG.get("services"), list) else []
    items = []
    used_ids = set()
    for entry in discovered:
        unit_info = next((x for x in units.values() if x["pid"] and x["pid"] == entry["pid"]), None)
        item = {**entry, "unit": unit_info["unit"] if unit_info else "", "unit_name": unit_info["name"] if unit_info else "", "state": "active", "category": "service"}
        rule = next((x for x in rules if config_match(item, x)), {})
        item_id = safe_id(rule.get("id") or item["unit"] or f"port-{item['port']}-{item['process']}")
        while item_id in used_ids:
            item_id += "-2"
        used_ids.add(item_id)
        title = rule.get("name") or item["unit_name"] or item["process"] or f"端口 {item['port']}"
        actions = rule.get("actions", ["restart"] if item["unit"] else [])
        item.update({"key": item_id, "title": title, "detail": rule.get("description") or f"{item['address']} · {item['process'] or '监听进程'}", "actions": actions, "url": rule.get("url"), "ok": True})
        if item["url"]:
            item["url"] = str(item["url"]).format(host=public_host(host_header), port=item["port"], unit=item["unit"], process=item["process"])
        items.append(item)

    for rule in rules:
        if any(config_match(item, rule) for item in items):
            continue
        unit = rule.get("match", rule).get("unit", "")
        unit_info = units.get(unit, {})
        if not unit:
            continue
        item_id = safe_id(rule.get("id") or unit)
        items.append({"key": item_id, "title": rule.get("name") or unit_info.get("name") or unit, "detail": rule.get("description") or unit, "state": unit_info.get("state", "inactive"), "ok": False, "port": rule.get("port"), "process": "", "unit": unit, "actions": rule.get("actions", ["start", "restart"]), "url": rule.get("url"), "category": "service"})

    browser_port = CONFIG.get("browser", {}).get("port")
    if browser_port is None and CONFIG.get("browser", {}).get("enabled", True):
        browser_port = next((x["port"] for x in discovered if any(word in x["process"].lower() for word in ("novnc", "websockify"))), None)
    if browser_port:
        browser_ok = port_open(browser_port)
        items.append({"key": "browser_session", "title": "远程浏览器会话", "detail": f"自动发现的浏览器网关 · 127.0.0.1:{browser_port}", "ok": browser_ok, "state": "active" if browser_ok else "inactive", "port": browser_port, "actions": [], "url": None, "category": "component"})

    with SERVICE_LOCK:
        SERVICE_TARGETS.clear()
        SERVICE_TARGETS.update({x["key"]: x for x in items})
    return items


def process_running(name: str) -> bool:
    return any(x["process"] == name for x in listening_ports())


def codex_cli() -> dict:
    if not CONFIG.get("codex", {}).get("enabled", True):
        return {"running": 0, "mode": "disabled", "recent": []}
    session_dir = Path(CONFIG.get("codex", {}).get("sessions_dir") or (Path.home() / ".codex" / "sessions"))
    sessions = []
    for filename in glob.glob(str(session_dir / "**" / "*.jsonl"), recursive=True):
        try:
            with open(filename, encoding="utf-8") as file:
                first = json.loads(file.readline())
                meta = first.get("payload", {}) if first.get("type") == "session_meta" else {}
                summary = "常规 CLI 会话"
                for line in file:
                    event = json.loads(line)
                    payload = event.get("payload", {})
                    if event.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                        text = next((c.get("text", "") for c in payload.get("content", []) if c.get("type") in ("input_text", "text")), "")
                        if "<environment_context>" not in text and "<cwd>" not in text:
                            summary = safe_task_summary(text)
                            break
            sessions.append({"timestamp": meta.get("timestamp") or first.get("timestamp"), "cwd": meta.get("cwd", ""), "source": meta.get("source", "unknown"), "summary": summary})
        except (OSError, json.JSONDecodeError):
            continue
    sessions.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    return {"running": 0, "mode": "available" if sessions else "idle", "recent": sessions[:5]}


def safe_task_summary(value: str) -> str:
    value = " ".join((value or "").split())
    if not value or re.search(r"(?i)(password|passwd|token|secret|cookie|authorization|api[_ -]?key|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", value):
        return "敏感任务内容已隐藏" if value else "常规任务执行"
    value = re.sub(r"https?://\S+|`[^`]*`|\b[0-9a-f]{7,40}\b", "", value, flags=re.I)
    first = re.split(r"[。！？\n]|\s+-\s+", value, maxsplit=1)[0].strip()
    return (first or "常规任务执行")[:72] + ("…" if len(first) > 72 else "")


def multica_info() -> dict:
    cfg = CONFIG.get("multica", {})
    executable = cfg.get("executable") or shutil.which("multica")
    if not cfg.get("enabled", True) or not executable:
        return {"available": False, "running": 0, "recent": [], "error": "未发现 Multica CLI"}
    cwd = cfg.get("working_directory") or str(Path.home())
    runtime_id = cfg.get("runtime_id") or os.getenv("JIANKONG_RUNTIME_ID")
    code, raw = command(executable, "agent", "list", "--output", "json", timeout=25, cwd=cwd)
    if code != 0:
        return {"available": False, "running": 0, "recent": [], "error": "无法读取 Multica 任务"}
    try:
        agents = json.loads(raw)
    except json.JSONDecodeError:
        return {"available": False, "running": 0, "recent": [], "error": "Multica 返回数据格式无效"}
    if runtime_id:
        agents = [agent for agent in agents if agent.get("runtime_id") == runtime_id]
    all_tasks = []
    running_states = {"running", "starting", "queued", "pending"}
    for agent in agents:
        agent_id = agent.get("id")
        if agent_id is None:
            continue
        task_code, task_raw = command(executable, "agent", "tasks", str(agent_id), "--output", "json", timeout=20, cwd=cwd)
        if task_code != 0:
            continue
        try:
            tasks = json.loads(task_raw)
        except json.JSONDecodeError:
            continue
        for task in tasks:
            if runtime_id and task.get("runtime_id") != runtime_id:
                continue
            all_tasks.append({"id": task.get("id", ""), "summary": safe_task_summary(task.get("trigger_summary", "")), "agent": agent.get("name", "Agent"), "status": task.get("status", "unknown"), "created_at": task.get("created_at"), "started_at": task.get("started_at"), "completed_at": task.get("completed_at"), "error": bool(task.get("error"))})
    all_tasks.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {"available": True, "agent_count": len(agents), "running": sum(task["status"] in running_states for task in all_tasks), "recent": all_tasks[:5], "stale": False}


def status(host_header: str = "") -> dict:
    items = service_status(host_header)
    codex = codex_cli()
    items.insert(0, {"key": "codex_cli", "title": "Codex CLI", "detail": "当前用户的本地 Codex CLI 会话", "ok": True, "state": codex["mode"], "port": None, "actions": [], "url": None, "codex": codex, "category": "meta"})
    tasks = multica_info()
    items.insert(1, {"key": "agent_tasks", "title": "Multica 编排任务", "detail": "自动发现的 Multica 任务来源", "ok": tasks.get("available", False), "state": "available" if tasks.get("available") else "inactive", "port": None, "actions": [], "url": None, "tasks": tasks, "category": "meta"})
    return {"dashboard_name": CONFIG.get("name", "Debian 服务监控面板"), "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "host": socket.gethostname(), "monitor_port": PORT, "all_ok": all(x["ok"] for x in items), "items": items, "uptime_seconds": int(time.time() - STARTED), "resources": resources()}


def allowed_action(target: str, action: str) -> tuple[bool, str]:
    with SERVICE_LOCK:
        item = dict(SERVICE_TARGETS.get(target, {}))
    if not item or action not in ("start", "restart") or action not in item.get("actions", []):
        return False, "不允许的操作：目标必须是自动发现或配置的 user service。"
    unit = item.get("unit")
    if not unit:
        return False, "该端口没有关联的 systemd user service。"
    code, output = command("systemctl", "--user", action, unit, timeout=20)
    return code == 0, output or f"{unit} {action} 请求已执行"


def logs(target: str) -> tuple[bool, str]:
    with SERVICE_LOCK:
        item = dict(SERVICE_TARGETS.get(target, {}))
    if not item or not item.get("unit"):
        return False, "该目标没有关联的 systemd user service"
    return True, command("journalctl", "--user", "-u", item["unit"], "-n", "60", "--no-pager")[1]


PAGE = (BASE / "index.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def send_json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self) -> bool:
        return bool(TOKEN) and self.headers.get("X-Jiankong-Token", "") == TOKEN

    def browser_authorized(self) -> bool:
        return browser_session(self.headers.get("Cookie", ""))

    def send_browser_redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", f"jk_vnc={cookie}; Path=/browser; HttpOnly; SameSite=Strict; Max-Age={BROWSER_TTL}")
        self.end_headers()

    def browser_port(self) -> int | None:
        if not CONFIG.get("browser", {}).get("enabled", True):
            return None
        configured = CONFIG.get("browser", {}).get("port")
        if configured:
            return int(configured)
        return next((x["port"] for x in listening_ports() if any(word in x["process"].lower() for word in ("novnc", "websockify"))), None)

    def browser_proxy(self) -> None:
        port = self.browser_port()
        if not port:
            self.send_json(503, {"error": "未发现浏览器网关端口"})
            return
        upstream_path = self.path[len("/browser"):] or "/"
        if upstream_path == "/":
            self.send_browser_redirect("/browser/vnc.html?path=browser/websockify&autoconnect=true")
            return
        if upstream_path.startswith("/websockify"):
            self.browser_websocket(upstream_path, port)
            return
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
            headers = {key: value for key, value in self.headers.items() if key.lower() not in {"host", "cookie", "connection", "upgrade"}}
            conn.request("GET", upstream_path, headers=headers)
            response = conn.getresponse()
            body = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in {"connection", "transfer-encoding", "content-length"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            conn.close()
        except OSError:
            self.send_json(502, {"error": "浏览器网关暂不可用"})

    def browser_websocket(self, upstream_path: str, port: int) -> None:
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self.send_json(400, {"error": "需要 WebSocket 连接"})
            return
        backend = None
        try:
            backend = socket.create_connection(("127.0.0.1", port), timeout=8)
            request = f"GET {upstream_path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            for key, value in self.headers.items():
                if key.lower() not in {"host", "cookie"}:
                    request += f"{key}: {value}\r\n"
            backend.sendall((request + "\r\n").encode())
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = backend.recv(4096)
                if not chunk:
                    raise OSError("browser gateway handshake failed")
                response += chunk
            self.connection.sendall(response)
            backend.setblocking(False)
            self.connection.setblocking(False)
            sockets = [self.connection, backend]
            while True:
                readable, _, _ = select.select(sockets, [], [], 60)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (backend if source is self.connection else self.connection).sendall(data)
        except OSError:
            return
        finally:
            if backend:
                backend.close()

    def do_GET(self) -> None:
        if self.path.startswith("/browser/launch?"):
            ticket = parse_qs(urlsplit(self.path).query).get("ticket", [""])[0]
            if not consume_browser_ticket(ticket):
                self.send_json(403, {"error": "浏览器访问票据无效或已过期"})
                return
            self.send_browser_redirect("/browser/", new_browser_session())
            return
        if self.path == "/browser" or self.path.startswith("/browser/"):
            if not self.browser_authorized():
                self.send_json(401, {"error": "需要从运行控制台打开远程浏览器"})
                return
            self.browser_proxy()
            return
        asset_path = self.path.split("?", 1)[0]
        if asset_path in ("/icon.svg", "/apple-touch-icon.png"):
            asset = BASE / asset_path.lstrip("/")
            if not asset.is_file():
                self.send_json(404, {"error": "Not found"})
                return
            body = asset.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml" if asset.suffix == ".svg" else "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if asset_path in ("/", "/index.html"):
            data = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if asset_path == "/api/status":
            self.send_json(200, status(self.headers.get("Host", "")))
            return
        if asset_path == "/api/resources":
            self.send_json(200, resources())
            return
        if asset_path.startswith("/api/logs"):
            if not self.authorized():
                self.send_json(401, {"error": "需要控制令牌"})
                return
            target = parse_qs(urlsplit(self.path).query).get("target", [""])[0]
            ok, output = logs(target)
            self.send_json(200 if ok else 404, {"title": target, "log": output})
            return
        self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if self.path == "/api/browser-session":
            if not self.authorized():
                self.send_json(401, {"error": "控制令牌无效"})
                return
            self.send_json(200, {"url": "/browser/launch?ticket=" + browser_ticket()})
            return
        if self.path != "/api/action":
            self.send_json(404, {"error": "Not found"})
            return
        if not self.authorized():
            self.send_json(401, {"error": "控制令牌无效"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except Exception:
            self.send_json(400, {"error": "无效请求"})
            return
        ok, output = allowed_action(str(body.get("target", "")), str(body.get("action", "")))
        self.send_json(200 if ok else 400, {"ok": ok, "message": output})


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()