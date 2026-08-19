#!/usr/bin/env python3
"""D12 LAN status monitor and explicitly authorized service controls."""
from __future__ import annotations
import html, json, os, socket, subprocess, time, threading, glob, re, shutil, collections, secrets, select, http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
HOST, PORT = os.getenv("JIANKONG_HOST", "0.0.0.0"), int(os.getenv("JIANKONG_PORT", "8888"))
PROD_PORT, STARTED = int(os.getenv("JIANKONG_PRODUCTION_PORT", "3001")), time.time()
D12_RUNTIME_ID = os.getenv("JIANKONG_D12_RUNTIME_ID", "63bbf346-20d0-4482-8892-0a17b19523c4")
TOKEN = (BASE / "control-token").read_text().strip()
TASK_CACHE: dict = {"at": 0.0, "data": {"available": False, "running": 0, "recent": [], "error": "正在读取任务"}}
TASK_CACHE_TTL = 15
ISSUE_TITLE_CACHE: dict[str, str] = {}
TASK_LOCK = threading.Lock()
RESOURCE_HISTORY = collections.deque(maxlen=1200)  # 3-second samples, retained in memory for one hour.
RESOURCE_LOCK = threading.Lock()
RESOURCE_LAST: dict = {"at": 0.0, "cpu": None, "net": None}
# Short-lived browser tickets become HttpOnly gateway sessions. Neither is written to disk.
BROWSER_TICKETS: dict[str, float] = {}
BROWSER_SESSIONS: dict[str, float] = {}
BROWSER_LOCK = threading.Lock()
BROWSER_TTL = 300

def _prune_browser_tokens() -> None:
    now=time.time()
    with BROWSER_LOCK:
        for store in (BROWSER_TICKETS, BROWSER_SESSIONS):
            for key, expires in list(store.items()):
                if expires <= now: store.pop(key, None)

def browser_ticket() -> str:
    _prune_browser_tokens(); ticket=secrets.token_urlsafe(32)
    with BROWSER_LOCK: BROWSER_TICKETS[ticket]=time.time()+60
    return ticket

def consume_browser_ticket(ticket: str) -> bool:
    _prune_browser_tokens()
    with BROWSER_LOCK:
        return bool(BROWSER_TICKETS.pop(ticket, None))

def browser_session(cookie: str) -> bool:
    _prune_browser_tokens()
    token=next((part.strip()[7:] for part in cookie.split(";") if part.strip().startswith("jk_vnc=")), "")
    with BROWSER_LOCK: return BROWSER_SESSIONS.get(token, 0)>time.time()

def new_browser_session() -> str:
    session=secrets.token_urlsafe(32)
    with BROWSER_LOCK: BROWSER_SESSIONS[session]=time.time()+BROWSER_TTL
    return session

def _read_cpu() -> tuple[int, int]:
    parts = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:]))
    total=sum(parts); idle=parts[3]+(parts[4] if len(parts)>4 else 0)
    return total,idle

def _active_iface() -> str:
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            fields=line.split()
            if len(fields)>2 and fields[1]=="00000000": return fields[0]
    except OSError: pass
    return "eth0"

def _net_bytes(iface: str) -> tuple[int,int]:
    line=next((x for x in Path("/proc/net/dev").read_text().splitlines() if x.strip().startswith(iface+":")), "")
    vals=line.split(":",1)[1].split() if ":" in line else []
    return (int(vals[0]),int(vals[8])) if len(vals)>8 else (0,0)

def resources() -> dict:
    now=time.time(); iface=_active_iface(); total,idle=_read_cpu(); rx,tx=_net_bytes(iface)
    mem={k:int(v.strip().split()[0])*1024 for k,v in (x.split(":",1) for x in Path("/proc/meminfo").read_text().splitlines() if ":" in x)}
    mem_total=mem.get("MemTotal",1); mem_available=mem.get("MemAvailable",0); mem_used=mem_total-mem_available
    disk=shutil.disk_usage("/")
    with RESOURCE_LOCK:
        previous=RESOURCE_LAST.copy(); dt=now-previous["at"] if previous["at"] else 0
        cpu=0.0 if previous["cpu"] is None else max(0.0,min(100.0,100*(1-(idle-previous["cpu"][1])/max(1,total-previous["cpu"][0]))))
        down=0.0 if not previous["net"] or dt<=0 else max(0,(rx-previous["net"][0])/dt)
        up=0.0 if not previous["net"] or dt<=0 else max(0,(tx-previous["net"][1])/dt)
        RESOURCE_LAST.update({"at":now,"cpu":(total,idle),"net":(rx,tx)})
        sample={"t":int(now),"cpu":round(cpu,1),"memory":round(mem_used/mem_total*100,1),"disk":round(disk.used/disk.total*100,1),"down":round(down,1),"up":round(up,1)}
        if not RESOURCE_HISTORY or now-RESOURCE_HISTORY[-1]["t"]>=2: RESOURCE_HISTORY.append(sample)
        recent=[x for x in RESOURCE_HISTORY if now-x["t"]<=60]
        hour=list(RESOURCE_HISTORY)
    def state(pct:float)->str: return "danger" if pct>=90 else ("warning" if pct>=75 else "ok")
    return {"sample":sample,"memory":{"used":mem_used,"total":mem_total,"state":state(sample["memory"])},"disk":{"used":disk.used,"total":disk.total,"state":state(sample["disk"])},"cpu":{"state":state(sample["cpu"])},"network":{"iface":iface,"down":down,"up":up,"state":"ok" if iface else "danger"},"recent":recent,"hour":hour}

SERVICES = {
    "development": ("xiaode-saas.service", "开发服务", "端口 3000 · Next.js 开发模式", 3000),
    "xvfb": ("d12-browser-xvfb.service", "虚拟显示器", "Xvfb :99", None),
    "vnc": ("d12-browser-vnc.service", "VNC", "127.0.0.1:5901 · 仅 SSH 隧道可访问", 5901),
    "novnc": ("d12-browser-novnc.service", "noVNC", "127.0.0.1:6080 · 仅 SSH 隧道可访问", 6080),
    "chromium": ("d12-browser-chromium.service", "Chromium", "可视浏览器 · CDP 127.0.0.1:9222", 9222),
}

def command(*args: str, timeout: int = 8, cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e: return 127, str(e)

def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1): return True
    except OSError: return False

def svc_state(unit: str) -> str:
    code, out = command("systemctl", "--user", "is-active", unit)
    return "active" if code == 0 and out == "active" else (out or "unknown")

def process(pattern: str) -> bool: return command("pgrep", "-f", pattern)[0] == 0

def codex_cli() -> dict:
    """Read native Codex CLI sessions only; this is separate from Multica orchestration tasks."""
    pattern = "/home/seek/.codex/sessions/**/*.jsonl"
    sessions=[]
    for filename in glob.glob(pattern, recursive=True):
        try:
            with open(filename, encoding="utf-8") as f:
                first=json.loads(f.readline())
                meta=first.get("payload", {}) if first.get("type") == "session_meta" else {}
                # Extract only the first user message as a session label; do not retain dialogue/output.
                summary="常规 CLI 会话"
                for line in f:
                    event=json.loads(line)
                    payload=event.get("payload", {})
                    if event.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                        content=payload.get("content", [])
                        text=next((c.get("text", "") for c in content if c.get("type") in ("input_text", "text")), "")
                        # Codex writes an automatic environment wrapper before the real user request.
                        if "<environment_context>" in text or "<cwd>" in text:
                            continue
                        summary=safe_task_summary(text)
                        break
            sessions.append({"timestamp":meta.get("timestamp") or first.get("timestamp"), "cwd":meta.get("cwd", ""), "source":meta.get("source", "unknown"), "originator":meta.get("originator", "unknown"), "cli_version":meta.get("cli_version", ""), "summary":summary})
        except (OSError, json.JSONDecodeError): continue
    sessions.sort(key=lambda x:x.get("timestamp") or "", reverse=True)
    code, raw=command("pgrep", "-af", "codex")
    lines=[x for x in raw.splitlines() if "codex" in x and "pgrep" not in x]
    managed=any("app-server" in x for x in lines)
    direct=any("app-server" not in x for x in lines)
    return {"running":len(lines), "mode":"multica_managed" if managed else ("direct_cli" if direct else "idle"), "recent":sessions[:5]}

def safe_task_summary(value: str) -> str:
    """Human-readable execution label; suppress potentially sensitive task text on LAN UI."""
    value = " ".join((value or "").split())
    sensitive = re.compile(r"(?i)(password|passwd|token|secret|cookie|authorization|api[_ -]?key|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})")
    if not value: return "常规任务执行"
    if sensitive.search(value): return "敏感任务内容已隐藏"
    # Remove transport/implementation noise before a LAN dashboard presents the task to humans.
    value = re.sub(r"\[@[^]]+\]\([^)]*\)", "", value)
    value = re.sub(r"`[^`]*`", "", value)
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"\b[0-9a-f]{7,40}\b", "", value, flags=re.I)
    value = re.sub(r"\bagent/[\w./-]+\b", "功能分支", value)
    value = re.sub(r"^[-•\d. ]+", "", value).strip()
    # Keep the first complete clause instead of a raw multi-line agent report.
    first = re.split(r"[。！？\n]|\s+-\s+", value, maxsplit=1)[0].strip()
    return (first or "常规任务执行")[:72] + ("…" if len(first) > 72 else "")

def issue_title(issue_id: str) -> str:
    """Fetch only the issue title, never description/comments, for safe human-readable task labels."""
    if not issue_id: return "未关联任务"
    if issue_id in ISSUE_TITLE_CACHE: return ISSUE_TITLE_CACHE[issue_id]
    code, raw = command("/usr/local/bin/multica", "issue", "get", issue_id, "--output", "json", timeout=15, cwd="/home/seek")
    try: title = json.loads(raw).get("title", "未命名任务") if code == 0 else "未命名任务"
    except json.JSONDecodeError: title = "未命名任务"
    ISSUE_TITLE_CACHE[issue_id] = title
    return title

def _refresh_tasks_in_background() -> None:
    """Multica may be slow; never make the dashboard status endpoint wait for it."""
    try:
        _d12_tasks_refresh()
    finally:
        TASK_LOCK.release()

def d12_tasks() -> dict:
    """Return cached safe task data immediately and refresh stale data off the request path."""
    if time.time() - TASK_CACHE["at"] >= TASK_CACHE_TTL and TASK_LOCK.acquire(blocking=False):
        threading.Thread(target=_refresh_tasks_in_background, daemon=True).start()
    data = TASK_CACHE["data"].copy()
    if not TASK_CACHE["at"]: data["refreshing"] = True
    return data

def _d12_tasks_refresh() -> dict:
    code, raw = command("/usr/local/bin/multica", "agent", "list", "--output", "json", timeout=25, cwd="/home/seek")
    if code != 0:
        previous = TASK_CACHE["data"]
        if previous.get("available"): return {**previous, "stale": True}
        print(f"jiankong: multica agent list failed: {raw[:500]}", flush=True)
        return {"available": False, "running": 0, "recent": [], "error": "无法读取 D12 Agent 任务"}
    try: agents = json.loads(raw)
    except json.JSONDecodeError: return {"available": False, "running": 0, "recent": [], "error": "任务数据格式无效"}
    agents = [a for a in agents if a.get("runtime_id") == D12_RUNTIME_ID]
    all_tasks=[]
    for agent in agents:
        code, raw = command("/usr/local/bin/multica", "agent", "tasks", str(agent.get("id")), "--output", "json", timeout=20, cwd="/home/seek")
        if code != 0: continue
        try: tasks=json.loads(raw)
        except json.JSONDecodeError: continue
        for task in tasks:
            if task.get("runtime_id") != D12_RUNTIME_ID: continue
            issue_id = task.get("issue_id", "")
            all_tasks.append({"id": task.get("id", ""), "issue_id": issue_id, "parent_title": issue_title(issue_id), "summary": safe_task_summary(task.get("trigger_summary", "")), "agent": agent.get("name", "D12 Agent"), "status": task.get("status", "unknown"), "created_at": task.get("created_at"), "started_at": task.get("started_at"), "completed_at": task.get("completed_at"), "error": bool(task.get("error"))})
    all_tasks.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    running={"running", "starting", "queued", "pending"}
    result = {"available": True, "runtime_id": D12_RUNTIME_ID, "agent_count": len(agents), "running": sum(x["status"] in running for x in all_tasks), "recent": all_tasks[:5], "stale": False}
    TASK_CACHE.update({"at": time.time(), "data": result})
    return result

def status() -> dict:
    items=[]
    codex = codex_cli()
    items.append({"key":"codex_cli","title":"Codex CLI","detail":"D12 原生 Codex CLI · 直接 SSH 会话与 Multica 托管执行共用同一 CLI 安装","ok":True,"state":codex["mode"],"port":None,"actions":[],"url":None,"codex":codex})
    for key,(unit,title,detail,port) in SERVICES.items():
        checks=[svc_state(unit)=="active"] + ([port_open(port)] if port else [])
        if key=="chromium": checks.append(process("/usr/lib/chromium/chromium"))
        url = "http://10.10.10.90:3000" if key == "development" else None
        items.append({"key":key,"title":title,"detail":detail,"ok":all(checks),"state":svc_state(unit),"port":port,"actions":["restart"] if svc_state(unit) == "active" else ["start"],"url":url})
    browser_parts = [x for x in items if x["key"] in {"xvfb", "vnc", "novnc", "chromium"}]
    browser_ok = all(x["ok"] for x in browser_parts)
    items.append({"key":"browser_session","title":"远程浏览器会话","detail":"Xvfb → VNC → noVNC → Chromium · 通过 SSH 隧道在本机操作 D12 浏览器","ok":browser_ok,"state":"active" if browser_ok else "degraded","port":None,"actions":[],"url":"http://127.0.0.1:16080/vnc.html","link_label":"打开远程浏览器"})
    prod=port_open(PROD_PORT)
    items.insert(1,{"key":"production","title":"生产服务","detail":f"端口 {PROD_PORT} · 当前未配置生产启动单元","ok":prod,"state":"active" if prod else "inactive","port":PROD_PORT,"actions":[],"url":f"http://10.10.10.90:{PROD_PORT}"})
    multica_ok=process("multica daemon start") and port_open(19514)
    agent_tasks = d12_tasks()
    items.append({"key":"agent_tasks","title":"Multica 编排任务","detail":"由 Multica 分派给 D12 Agent 的任务记录（不等同于原生 Codex CLI 历史）","ok":True,"state":"running" if agent_tasks.get("running",0) else "idle","port":None,"actions":[],"url":None,"tasks":agent_tasks})
    items.append({"key":"multica","title":"Multica","detail":"本地 agent runtime · 127.0.0.1:19514","ok":multica_ok,"state":"active" if multica_ok else "inactive","port":19514,"actions":["restart"] if multica_ok else [],"url":None})
    return {"generated_at":time.strftime("%Y-%m-%d %H:%M:%S %Z"),"host":socket.gethostname(),"resources":resources(),"monitor_port":PORT,"production_port":PROD_PORT,"all_ok":all(x["ok"] for x in items),"items":items,"uptime_seconds":int(time.time()-STARTED)}

def allowed_action(target: str, action: str) -> tuple[bool,str]:
    if target in SERVICES and action in ("start","restart"):
        unit=SERVICES[target][0]; code,out=command("systemctl","--user",action,unit,timeout=20)
        return code==0, out or f"{unit} {action} 请求已执行"
    if target=="multica" and action=="restart":
        code,out=command("/usr/local/bin/multica","daemon","restart",timeout=25,cwd="/home/seek"); return code==0,out or "Multica daemon restart 请求已执行"
    return False,"不允许的操作：只允许已列出的服务执行启动或重启。"

def logs(target: str) -> tuple[bool,str]:
    if target in SERVICES: return True,command("journalctl","--user","-u",SERVICES[target][0],"-n","60","--no-pager")[1]
    if target=="multica": return True,command("/usr/local/bin/multica","daemon","logs",cwd="/home/seek")[1][-12000:]
    return False,"未知服务"

PAGE = (BASE / 'index.html').read_text(encoding='utf-8')

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*a:object)->None: pass
    def send_json(self,code:int,payload:dict)->None:
        data=json.dumps(payload,ensure_ascii=False).encode();self.send_response(code);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    def authorized(self)->bool: return self.headers.get("X-Jiankong-Token","")==TOKEN
    def browser_authorized(self)->bool: return browser_session(self.headers.get("Cookie", ""))
    def send_browser_redirect(self, location: str, cookie: str | None = None)->None:
        self.send_response(302); self.send_header("Location", location)
        if cookie: self.send_header("Set-Cookie", f"jk_vnc={cookie}; Path=/browser; HttpOnly; SameSite=Strict; Max-Age={BROWSER_TTL}")
        self.end_headers()
    def browser_proxy(self)->None:
        # All noVNC assets and its WebSocket are proxied; 6080 remains loopback-only.
        upstream_path=self.path[len("/browser"): ] or "/"
        if upstream_path=="/":
            self.send_browser_redirect("/browser/vnc.html?path=browser/websockify&autoconnect=true")
            return
        if upstream_path.startswith("/websockify"):
            return self.browser_websocket(upstream_path.replace("/websockify", "/websockify", 1))
        try:
            conn=http.client.HTTPConnection("127.0.0.1",6080,timeout=8)
            headers={k:v for k,v in self.headers.items() if k.lower() not in {"host","cookie","connection","upgrade"}}
            conn.request("GET",upstream_path,headers=headers); response=conn.getresponse(); body=response.read()
            self.send_response(response.status)
            for k,v in response.getheaders():
                if k.lower() not in {"connection","transfer-encoding","content-length"}: self.send_header(k,v)
            self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body);conn.close()
        except OSError: self.send_json(502,{"error":"远程浏览器网关暂不可用"})
    def browser_websocket(self, upstream_path: str)->None:
        if self.headers.get("Upgrade","").lower()!="websocket": self.send_json(400,{"error":"需要 WebSocket 连接"});return
        try:
            backend=socket.create_connection(("127.0.0.1",6080),timeout=8)
            request=f"GET {upstream_path} HTTP/1.1\r\nHost: 127.0.0.1:6080\r\n"
            for k,v in self.headers.items():
                if k.lower() not in {"host","cookie"}: request+=f"{k}: {v}\r\n"
            backend.sendall((request+"\r\n").encode())
            # Forward handshake then relay opaque WebSocket frames bidirectionally.
            response=b""
            while b"\r\n\r\n" not in response:
                chunk=backend.recv(4096)
                if not chunk: raise OSError("noVNC handshake failed")
                response+=chunk
            self.connection.sendall(response)
            backend.setblocking(False);self.connection.setblocking(False)
            sockets=[self.connection,backend]
            while True:
                readable,_,_=select.select(sockets,[],[],60)
                if not readable: continue
                for source in readable:
                    data=source.recv(65536)
                    if not data: return
                    (backend if source is self.connection else self.connection).sendall(data)
        except OSError: return
        finally:
            try: backend.close()
            except Exception: pass
    def do_GET(self)->None:
        if self.path.startswith("/browser/launch?"):
            from urllib.parse import parse_qs, urlsplit
            ticket=parse_qs(urlsplit(self.path).query).get("ticket",[""])[0]
            if not consume_browser_ticket(ticket): self.send_json(403,{"error":"浏览器访问票据无效或已过期"});return
            self.send_browser_redirect("/browser/",new_browser_session());return
        if self.path=="/browser" or self.path.startswith("/browser/"):
            if not self.browser_authorized(): self.send_json(401,{"error":"需要从运行控制台打开远程浏览器"});return
            self.browser_proxy();return
        asset_path=self.path.split("?",1)[0]
        if asset_path in ("/icon.svg", "/apple-touch-icon.png"):
            asset=BASE / asset_path.lstrip("/")
            if not asset.is_file(): self.send_json(404,{"error":"Not found"});return
            body=asset.read_bytes();self.send_response(200);self.send_header("Content-Type", "image/svg+xml" if asset.suffix==".svg" else "image/png");self.send_header("Cache-Control","public, max-age=86400");self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body);return
        if self.path in ("/","/index.html"):
            data=PAGE.encode();self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data);return
        if self.path=="/api/status": self.send_json(200,status());return
        if self.path=="/api/resources": self.send_json(200,resources());return
        if self.path.startswith("/api/logs"):
            if not self.authorized(): self.send_json(401,{"error":"需要控制令牌"});return
            target=self.path.split("target=",1)[-1].split("&",1)[0]; target=target.replace("%2D","-")
            ok,out=logs(target);self.send_json(200 if ok else 404,{"title":target,"log":out});return
        self.send_json(404,{"error":"Not found"})
    def do_POST(self)->None:
        if self.path=="/api/browser-session":
            if not self.authorized():self.send_json(401,{"error":"控制令牌无效"});return
            self.send_json(200,{"url":"/browser/launch?ticket="+browser_ticket()});return
        if self.path!="/api/action":self.send_json(404,{"error":"Not found"});return
        if not self.authorized():self.send_json(401,{"error":"控制令牌无效"});return
        try: body=json.loads(self.rfile.read(int(self.headers.get("Content-Length","0"))))
        except Exception:self.send_json(400,{"error":"无效请求"});return
        ok,out=allowed_action(str(body.get("target","")),str(body.get("action","")));self.send_json(200 if ok else 400,{"ok":ok,"message":out})

if __name__=="__main__": ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
