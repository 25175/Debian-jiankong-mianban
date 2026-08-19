# jiankong 监控包

## 已包含

- `server.py`: 监控 HTTP 服务、资源采集、服务控制、受保护 noVNC 网关
- `index.html`: 运行控制台页面
- `jiankong.service`: systemd user service 模板
- `icon.svg` / `apple-touch-icon.png`: 页面图标
- `README.md`: 功能与安全说明
- `control-token`: 本机控制令牌，仅在部署机器本地创建，不提交 Git

## Python 依赖

无第三方 Python 包。仅使用 Python 3 标准库。

## D12 系统依赖

监控服务自身：

- Linux
- Python 3.9+
- systemd user services

当前 D12 被监控的外部服务与命令：

- `systemctl --user`
- `multica` CLI，当前路径 `/usr/local/bin/multica`
- `multica daemon`，当前监听 `127.0.0.1:19514`
- `xiaode-saas.service`，开发服务端口 `3000`
- Chromium，CDP 端口 `9222`
- Xvfb、x11vnc、noVNC/websockify
- 生产端口监控默认检查 `3001`

## 本地部署注意事项

`jiankong.service` 中的 `WorkingDirectory` 和 `ExecStart` 使用的是 D12 的绝对路径；部署到其他机器前需要改成实际目录。

`control-token` 已包含当前 D12 的令牌，不要提交 Git、上传公共网盘或放入前端代码。若迁移到新机器，应重新生成令牌并同步服务端配置。

远程浏览器网关需要 D12 的 noVNC/websockify 服务运行在 `127.0.0.1:6080`，并需要监控服务能够访问该端口。
