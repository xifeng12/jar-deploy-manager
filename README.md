# JAR Deploy Manager

用于通过 SSH 部署、重启和回滚 Java JAR 的本地 Windows 工具。启动后自动打开浏览器，只监听 `127.0.0.1`。

## Windows 便携版

下载 `jar-deploy-manager-windows-x64-v0.1.0.zip`，解压后双击 `jar-deploy-manager.exe`。首次启动会在 EXE 同级目录创建 `data/`、`jars/`、`logs/`、`scheduled_tasks.json` 与 `known_hosts`；可用 `DEPLOY_HOME` 指定另一运行目录。

发布包不需要 Python。关闭控制台窗口会停止本地服务。

## 配置

在“系统设置”中选择直连或单级跳板机模式。目标服务器端口可单独配置，两个端点都默认使用账号密码，也可分别启用本地私钥路径和可选口令。

主机密钥策略默认“首次接受并记录”：首次连接写入应用 `known_hosts`，后续变化会被拒绝；“仅已知主机”只接受系统或应用 `known_hosts` 中已有的主机密钥。

Windows 密码和私钥口令用 DPAPI 加密。其他系统需要提供 Fernet 格式的 `DEPLOY_CONFIG_KEY`；缺少该密钥时应用拒绝保存秘密配置。

DPAPI 凭据不能直接迁移到其他 Windows 用户或计算机；提示无法解密时，请在当前用户下重新保存密码/私钥口令，不要将此错误当作网络超时。

## 部署安全边界

每个 JAR 只能归属一台服务器。部署、重启和回滚互斥执行；停止请求会等当前单项完成或恢复后结束，不代表已立即停止远端命令。

已有 JAR 必须备份并校验成功后才允许替换。上传先写同目录临时文件，再通过 SFTP 原子重命名替换，服务器必须支持 OpenSSH 的 `posix-rename` 扩展。重启要求脚本返回零且检测到新的存活 JAR 进程；这不是应用接口健康检查。

启动失败会尝试从保留的备份恢复并验证重启；断网、磁盘或权限错误可能阻止自动恢复，应根据日志手动检查。首次部署没有旧版本可恢复。进程中断后的运行中定时任务标记为“已中断”，不会自动重放。

## 从源码运行

```powershell
python -m pip install -r requirements.txt
npm ci
npm run build
python app.py
```

## 开发验证

```powershell
python -m pytest -q
npm run build
```

本仓库不包含运行数据、JAR、日志、私钥、密码或容器配置。

测试使用临时 `DEPLOY_HOME` 和模拟 SSH，不会访问实际部署服务器。`scripts/check_jar_selection.py`、`scripts/check_health_ui.py` 可在已安装 Python Playwright/Chromium 的开发环境中检查构建后的界面；它们拦截所有请求，不启动真实部署。
