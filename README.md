# JAR Deploy Manager

用于通过 SSH 部署、重启和回滚 Java JAR 的本地 Windows 工具。启动后自动打开浏览器，只监听 `127.0.0.1`。

## Windows 便携版

下载 `jar-deploy-manager-windows-x64-v0.1.0.zip`，解压后双击 `jar-deploy-manager.exe`。首次启动会在 EXE 同级目录创建 `data/`、`jars/`、`logs/`、`scheduled_tasks.json` 与 `known_hosts`；可用 `DEPLOY_HOME` 指定另一运行目录。

发布包不需要 Python。关闭控制台窗口会停止本地服务。

## 配置

在“系统设置”中选择直连或单级跳板机模式。目标服务器端口可单独配置，两个端点都默认使用账号密码，也可分别启用本地私钥路径和可选口令。

主机密钥策略默认“首次接受并记录”：首次连接写入应用 `known_hosts`，后续变化会被拒绝；“仅已知主机”只接受系统或应用 `known_hosts` 中已有的主机密钥。

Windows 密码和私钥口令用 DPAPI 加密。其他系统需要提供 Fernet 格式的 `DEPLOY_CONFIG_KEY`；缺少该密钥时应用拒绝保存秘密配置。

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
