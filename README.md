# Debian 服务监控面板（jiankong）

可迁移的 Linux/Debian 局域网监控与受控运维页面。启动时扫描当前服务器的 TCP 监听端口，并按端口进程关联本用户的 systemd service；默认监听 `0.0.0.0:8888`。

## 访问

- 局域网：`http://<服务器地址>:8888`
- 本机：`http://127.0.0.1:8888`

## 页面能力

- 每 5 秒刷新当前服务器已发现的监听端口、systemd user service、CPU、内存、磁盘和网络状态。
- 监听端口按实际请求主机地址生成访问链接；服务名称可在 `jiankong.json` 中自定义。
- 仅对已关联或明确配置的 user service 提供一键启动、一键重启与最近 60 行日志查看。
- Multica、Codex、浏览器网关均为可选能力，路径、运行目录和 runtime ID 可自动发现或在 `jiankong.json` 中覆盖。

## 控制令牌

因为该页面对局域网开放，所有启动、重启和日志接口均需控制令牌。令牌存放在同目录的 `control-token`（权限 `600`），不会被网页、日志或 API 回显。首次安装时 `install.sh` 会在本机生成令牌。

## 安全边界

程序只操作白名单内的 `systemctl --user` 服务或 `multica daemon restart`，不会接受任意 shell 命令；状态读取不会改动受监控服务。

## 文件

- `server.py`：监控与控制 HTTP 服务
- `jiankong.service`：常驻 user service 定义
- `control-token`：本机控制令牌
- `jiankong.json`：面板名称、监听端口和可选服务匹配规则
- `install.sh`：首次安装生成本机令牌并显示；提供查看、重新生成、安装、重启、状态菜单

端口、服务匹配、名称和可选组件配置见 `jiankong.json`。程序不会假设某个固定业务端口或固定服务器 IP。

## 安装与令牌管理

### 1. 获取项目

仓库为私有仓库，需要已登录并有权限的 GitHub 账号：

```bash
git clone https://github.com/25175/Debian-jiankong-mianban.git
cd Debian-jiankong-mianban
```

### 2. 首次安装

```bash
chmod +x install.sh
./install.sh
```

第一次在某台服务器执行时，即使仓库中带有 `control-token`，安装器也会把它视为仓库初始令牌，生成该服务器自己的新令牌并立即显示。请复制并保存终端输出的令牌，再访问：

```text
http://<服务器地址>:8888
```

安装器会自动：

- 生成本机专用 `control-token`
- 设置令牌权限为 `600`
- 根据当前项目目录和 Python 路径生成 systemd user service
- 执行 `systemctl --user daemon-reload`
- 设置 `jiankong.service` 开机启用并立即启动

安装要求当前服务器具备 Debian/Linux、Python 3.9+、systemd user services 和 `systemctl`。

### 3. 再次安装或更新

后续再次执行安装不会自动重新生成令牌：

```bash
./install.sh --install
```

该命令会保留当前服务器已有的 `control-token`，只更新并启动服务。如果服务器已有本机令牌但还没有安装器状态记录，脚本会保留该令牌，不会盲目覆盖。

### 4. 菜单操作

直接执行以下命令进入管理菜单：

```bash
./install.sh
```

菜单包括：

```text
1) 安装/更新并启动服务（不改变现有令牌）
2) 查看当前令牌
3) 重新生成令牌（旧令牌立即失效）
4) 重启服务
5) 查看服务状态
0) 退出
```

### 5. 非交互命令

保留当前令牌，安装或更新服务：

```bash
./install.sh --install
```

查看当前令牌：

```bash
./install.sh --show-token
```

确认后重新生成令牌：

```bash
./install.sh --regenerate
```

非交互重新生成令牌：

```bash
./install.sh --regenerate --yes
```

重新生成会覆盖旧令牌、显示新令牌，并在服务已安装时自动重启 `jiankong.service`。旧令牌立即失效。

重启服务：

```bash
./install.sh --restart
```

查看服务状态：

```bash
./install.sh --status
```

查看帮助：

```bash
./install.sh --help
```

### 6. 令牌使用规则

- `control-token` 是服务器本机控制令牌，不应在服务器之间复用。
- 仓库中保留的令牌仅用于满足当前私有仓库的完整文件保存要求；其他服务器首次安装时会自动生成自己的新令牌。
- 普通的 `--install` 不会改变现有令牌。
- 只有菜单中的“重新生成令牌”、`--regenerate` 或 `--regenerate --yes` 会改变令牌。
- 令牌用于页面的启动、重启、日志和远程浏览器入口授权。
- 令牌不会由网页、状态接口或日志接口回显。
- 令牌文件权限必须保持为 `600`。

### 7. 服务和监控配置

编辑 `jiankong.json` 可配置：

- 面板名称
- 监控服务监听地址和端口
- 服务匹配规则
- 服务显示名称、描述和访问 URL
- 浏览器网关端口
- Multica 可执行文件、工作目录和 runtime ID
- Codex 会话目录

未配置时，程序会自动扫描当前服务器的 TCP 监听端口，并尝试关联当前用户的 systemd service。不会依赖固定服务器 IP、用户目录或业务端口。

修改 `jiankong.json` 后执行：

```bash
./install.sh --install
```

### 8. 常用排查命令

```bash
systemctl --user status jiankong.service --no-pager
journalctl --user -u jiankong.service -n 100 --no-pager
ss -ltnp
stat -c '%a %n' control-token
```

本机访问测试：

```bash
curl http://127.0.0.1:8888/
curl http://127.0.0.1:8888/api/status
curl http://127.0.0.1:8888/api/resources
```
