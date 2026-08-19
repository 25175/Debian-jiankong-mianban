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
- `install.sh`：按当前目录和当前 Python 路径安装 systemd user service

端口、服务匹配、名称和可选组件配置见 `jiankong.json`。程序不会假设某个固定业务端口或固定服务器 IP。
