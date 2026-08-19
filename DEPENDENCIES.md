# Debian 服务监控面板依赖

## 已包含

- `server.py`: 监控 HTTP 服务、资源采集、服务控制、受保护 noVNC 网关
- `index.html`: 运行控制台页面
- `jiankong.service`: systemd user service 模板
- `icon.svg` / `apple-touch-icon.png`: 页面图标
- `README.md`: 功能与安全说明
- `control-token`: 本机控制令牌
- `jiankong.json`: 当前服务器的名称和可选覆盖配置
- `install.sh`: 本机令牌生命周期和 systemd user service 菜单管理

## Python 依赖

无第三方 Python 包。仅使用 Python 3 标准库。

## 服务器依赖

监控服务自身：

- Linux
- Python 3.9+
- systemd user services

自动发现使用的命令与接口：

- `systemctl --user`
- `ss -ltnp`：扫描当前服务器 TCP 监听端口
- `systemctl --user`：读取当前用户 service 并关联主进程
- `/proc`：读取 CPU、内存和网卡统计
- `multica`：如果在 PATH 中存在则自动读取 agent/task
- Codex 会话目录：默认使用当前用户的 `~/.codex/sessions`

## 本地部署注意事项

在项目目录执行 `./install.sh`。首次执行会把仓库自带令牌替换为本机专用令牌并显示；后续执行保留现有令牌。脚本会动态写入当前目录和当前 Python 解释器，不需要修改 service 模板。

每台服务器应使用自己的 `control-token`。本仓库按用户明确要求保留当前目录中的令牌文件，但其他服务器首次执行安装器时会自动替换为本机新令牌并显示。只有选择“重新生成令牌”或执行 `./install.sh --regenerate --yes` 才会再次改变令牌。

远程浏览器网关只有在服务器实际监听 noVNC/websockify 端口时才显示；也可以在 `jiankong.json` 的 `browser.port` 中明确指定。
