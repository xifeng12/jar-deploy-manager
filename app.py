import os
import re
import threading
import logging
import unicodedata
import time
import urllib.request
import webbrowser
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
from deploy_service import DeployService, ScheduleTaskManager
from crypto_utils import SecretEncryptionError, encrypt, is_encrypted
from db import get_all_servers, add_server, update_server, delete_server, get_all_settings, set_settings
from runtime_paths import ensure_runtime_directories, get_runtime_paths, resource_dir

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.config["TEMPLATES_AUTO_RELOAD"] = True
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
ensure_runtime_directories()

SECRET_FIELDS = ("password", "jump_password", "server_key_passphrase", "jump_key_passphrase")
SECRET_MASK = "********"

deploy_service = DeployService(socketio)
task_manager = ScheduleTaskManager(deploy_service, socketio)
task_manager.recover()

logging.basicConfig(level=logging.INFO)

BASE_DIR = str(get_runtime_paths().home)


def normalize_jar_name(filename):
    filename = unicodedata.normalize("NFKC", os.path.basename(str(filename or "")))
    return re.sub(r"(?:\s*\(\d+\))+(?=\.jar$)", "", filename, flags=re.IGNORECASE)


def resolve_jar_name(filename):
    target_core = deploy_service._extract_core(normalize_jar_name(filename))
    matches = {
        jar["name"]
        for server in get_all_servers()
        for jar in server.get("jars", [])
        if isinstance(jar, dict)
        and jar.get("name")
        and deploy_service._extract_core(jar["name"]) == target_core
    }
    return next(iter(matches)) if len(matches) == 1 else None


def normalize_jar_names(names):
    result = []
    seen = set()
    for name in names or []:
        clean_name = normalize_jar_name(name)
        if clean_name and clean_name not in seen:
            result.append(clean_name)
            seen.add(clean_name)
    return result


@app.route("/")
def index():
    return send_from_directory(str(resource_dir() / "static" / "react"), "index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/config", methods=["GET", "POST"])
def config():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        old_data = get_all_settings()
        saved_data = old_data.copy()
        saved_data.update(data)
        for key in SECRET_FIELDS:
            value = str(data.get(key, ""))
            if (not value or value.startswith("********")) and old_data.get(key):
                saved_data[key] = old_data[key]
            elif value and not is_encrypted(value):
                try:
                    saved_data[key] = encrypt(value)
                except SecretEncryptionError:
                    return jsonify({"error": "无法安全保存秘密配置"}), 400
        set_settings(saved_data)
        deploy_service.update_config(get_all_settings())
        return jsonify({"success": True})

    config_data = get_all_settings()
    config_data.setdefault("connection_mode", "jump" if config_data.get("jump_host") else "direct")
    config_data.setdefault("server_port", "22")
    config_data.setdefault("host_key_policy", "accept-new")
    for key in SECRET_FIELDS:
        if config_data.get(key):
            config_data[key] = SECRET_MASK
    return jsonify(config_data)


@app.route("/api/servers", methods=["GET", "POST", "PUT", "DELETE"])
def servers():
    if request.method == "GET":
        return jsonify(get_all_servers())
    elif request.method == "POST":
        data = request.json
        add_server(data["ip"], data.get("jars", []), data.get("name", "").strip())
        return jsonify({"success": True})
    elif request.method == "PUT":
        data = request.json
        update_server(data["old_ip"], data["ip"], data.get("jars", []), data.get("name", "").strip())
        return jsonify({"success": True})
    elif request.method == "DELETE":
        data = request.get_json(silent=True) or {}
        ip = data.get("ip")
        if not ip:
            return jsonify({"success": False, "error": "未指定服务器IP"}), 400
        if not any(server["ip"] == ip for server in get_all_servers()):
            return jsonify({"success": False, "error": f"服务器不存在: {ip}"}), 404
        delete_server(ip)
        return jsonify({"success": True})


def _delete_jar_file(filename):
    jars_dir = os.path.join(BASE_DIR, "jars")
    os.makedirs(jars_dir, exist_ok=True)
    if not filename:
        return jsonify({"success": False, "error": "未指定文件名"}), 400

    filename = os.path.basename(filename)
    if not filename.lower().endswith(".jar"):
        return jsonify({"success": False, "error": "只能删除JAR文件"}), 400

    filepath = os.path.join(jars_dir, filename)
    if not os.path.abspath(filepath).startswith(os.path.abspath(jars_dir)):
        return jsonify({"success": False, "error": "非法路径"}), 400
    if not os.path.exists(filepath):
        return jsonify({"success": False, "error": f"文件不存在: {filename}"}), 404
    try:
        os.remove(filepath)
        return jsonify({"success": True, "deleted": filename})
    except Exception as e:
        return jsonify({"success": False, "error": f"删除失败: {str(e)}"}), 500


@app.route("/api/jars", methods=["GET", "POST", "DELETE"])
def jars():
    jars_dir = os.path.join(BASE_DIR, "jars")
    os.makedirs(jars_dir, exist_ok=True)

    if request.method == "GET":
        jars_list = []
        for f in os.listdir(jars_dir):
            if f.endswith(".jar"):
                path = os.path.join(jars_dir, f)
                jars_list.append(
                    {
                        "name": f,
                        "size": os.path.getsize(path),
                        "modified": os.path.getmtime(path),
                    }
                )
        return jsonify(jars_list)
    elif request.method == "POST":
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400
        if file.filename.endswith(".jar"):
            clean_name = resolve_jar_name(file.filename)
            if not clean_name:
                return jsonify({"error": "JAR名称未在服务器配置中唯一匹配"}), 400
            file.save(os.path.join(jars_dir, clean_name))
            return jsonify({"success": True, "name": clean_name})
        return jsonify({"error": "Only .jar files allowed"}), 400
    elif request.method == "DELETE":
        data = request.get_json(silent=True) or {}
        return _delete_jar_file(request.args.get("filename") or data.get("filename"))


@app.route("/api/jars/<path:filename>", methods=["GET", "DELETE"])
def download_jar(filename):
    if request.method == "DELETE":
        return _delete_jar_file(filename)
    return send_from_directory(
        os.path.join(BASE_DIR, "jars"), filename, as_attachment=True
    )


@app.route("/api/discover-jars", methods=["POST"])
def discover_jars():
    data = request.json
    server_ip = data.get("ip")
    if not server_ip:
        return jsonify({"success": False, "error": "请提供服务器IP"}), 400
    result = deploy_service.discover_jars(server_ip)
    return jsonify(result)


@app.route("/api/deploy", methods=["POST"])
def deploy():
    data = request.json
    selected_servers = data.get("servers", [])
    selected_jars = normalize_jar_names(data.get("jars", []))

    # 部署前校验
    jars_dir = os.path.join(BASE_DIR, "jars")
    missing = [j for j in selected_jars if not os.path.exists(os.path.join(jars_dir, j))]
    if missing:
        return jsonify({"success": False, "error": f"以下JAR包未上传: {', '.join(missing)}"}), 400

    all_servers = get_all_servers()
    unconfigured = []
    for ip in selected_servers:
        srv = next((s for s in all_servers if s["ip"] == ip), None)
        if not srv or not srv.get("jars"):
            unconfigured.append(ip)
    if unconfigured:
        return jsonify({"success": False, "error": f"以下服务器未配置JAR包: {', '.join(unconfigured)}"}), 400


    def run_deploy():
        deploy_service.deploy(selected_servers, selected_jars)
        deploy_result = {
            "servers": selected_servers,
            "jars": selected_jars,
            "success": deploy_service.last_deploy_success,
            "fail": deploy_service.last_deploy_fail,
        }
        socketio.emit("deploy_done", deploy_result)

    thread = threading.Thread(target=run_deploy)
    thread.start()

    return jsonify({"success": True, "task_id": thread.ident})


@app.route("/api/deploy/cancel", methods=["POST"])
def cancel_deploy():
    deploy_service.cancel_deploy()
    socketio.emit("deploy_done", {
        "servers": [], "jars": [],
        "success": deploy_service.last_deploy_success or 0,
        "fail": 0, "cancelled": True,
    })
    return jsonify({"success": True})


@app.route("/api/logs", methods=["GET"])
def logs():
    logs_dir = os.path.join(BASE_DIR, "logs")
    if not os.path.exists(logs_dir):
        return jsonify([])

    log_files = []
    for f in sorted(
        os.listdir(logs_dir),
        key=lambda x: os.path.getmtime(os.path.join(logs_dir, x)),
        reverse=True,
    ):
        if f.endswith(".log"):
            log_files.append(f)
    return jsonify(log_files)


@app.route("/api/logs/<path:filename>", methods=["GET"])
def view_log(filename):
    logs_dir = os.path.join(BASE_DIR, "logs")
    filename = os.path.basename(filename)
    filepath = os.path.join(logs_dir, filename)
    if not os.path.abspath(filepath).startswith(os.path.abspath(logs_dir)):
        return jsonify({"error": "非法路径"}), 400
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return jsonify({"content": f.read()})
    return jsonify({"error": "Log file not found"}), 404


@app.route("/api/test-connection", methods=["POST"])
def test_connection():
    data = request.json
    server_ip = data.get("ip")

    def run_test():
        result = deploy_service.test_connection(server_ip)
        socketio.emit("test_result", {"ip": server_ip, "result": result})

    thread = threading.Thread(target=run_test)
    thread.start()
    return jsonify({"success": True, "message": "测试中..."})


@app.route("/api/rollback", methods=["POST"])
def rollback():
    data = request.json
    server_ip = data.get("server_ip")
    jar_name = data.get("jar_name")

    def run_rollback():
        deploy_service.rollback(server_ip, jar_name)
        socketio.emit("deploy_done", {
            "servers": [server_ip], "jars": [jar_name],
            "success": 1, "fail": 0,
        })
    thread = threading.Thread(target=run_rollback)
    thread.start()
    return jsonify({"success": True})


@app.route("/api/restart", methods=["POST"])
def restart():
    data = request.json
    servers_jars = data.get("items", [])

    valid_items = []
    errors = []
    for item in servers_jars:
        server_ip = item.get("server")
        jar_name = item.get("jar")
        if not server_ip or not jar_name:
            errors.append(f"缺少服务器或JAR包参数")
            continue
        valid_items.append({"server": server_ip, "jar": jar_name})

    if errors:
        return jsonify({"success": False, "error": "; ".join(errors)}), 400

    if not valid_items:
        return jsonify({"success": False, "error": "未指定重启项"}), 400


    def run_restart():
        success, fail = deploy_service.restart(valid_items)
        socketio.emit("restart_done", {"success": success, "fail": fail})

    thread = threading.Thread(target=run_restart)
    thread.start()

    return jsonify({"success": True, "task_id": thread.ident})


@app.route("/api/browse-dir", methods=["POST"])
def browse_dir():
    import subprocess, tempfile
    script = '''
import tkinter as tk; from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
print(filedialog.askdirectory(title="选择日志保存目录") or "")
root.destroy()
'''
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
        f.write(script)
        tmpfile = f.name
    try:
        result = subprocess.run(["python", tmpfile], capture_output=True, text=True, timeout=60)
        folder = result.stdout.strip()
    finally:
        try: os.unlink(tmpfile)
        except: pass
    return jsonify({"folder": folder})


@app.route("/api/list-log-files", methods=["POST"])
def list_log_files():
    data = request.json
    server_ip = data.get("server_ip")
    jar_name = data.get("jar_name")
    if not server_ip or not jar_name:
        return jsonify({"success": False, "error": "请提供服务器IP和JAR名"}), 400
    result = deploy_service.list_log_files(server_ip, jar_name)
    return jsonify(result)


@app.route("/api/download-logs", methods=["POST"])
def download_logs():
    data = request.json or {}
    server_ip = data.get("server_ip")
    jar_name = data.get("jar_name")
    filenames = data.get("filenames", [])
    save_dir = data.get("save_dir")
    check_only = data.get("check_only", False)
    mode = data.get("mode", "rename")

    if not server_ip or not jar_name or not filenames or not save_dir:
        return jsonify({"success": False, "error": "请提供服务器、JAR、日志文件和保存目录"}), 400
    if mode not in ("overwrite", "rename", "skip"):
        return jsonify({"success": False, "error": "无效的下载模式"}), 400

    if check_only:
        conflicts = []
        if save_dir and os.path.exists(save_dir):
            for f in filenames:
                local_name = os.path.basename(str(f).replace("\\", "/"))
                if os.path.exists(os.path.join(save_dir, local_name)):
                    conflicts.append(local_name)
        return jsonify({"success": True, "conflicts": conflicts})


    def run_download():
        success, fail = deploy_service.download_logs(server_ip, jar_name, filenames, save_dir, mode)
        socketio.emit("download_done", {"success": success, "fail": fail})

    socketio.start_background_task(run_download)
    return jsonify({"success": True})


@app.route("/api/scheduled-tasks", methods=["GET", "POST"])
def scheduled_tasks():
    if request.method == "GET":
        return jsonify(task_manager.get_all())
    else:
        data = request.json
        result = task_manager.create_task(
            servers=data.get("servers", []),
            jars=normalize_jar_names(data.get("jars", [])),
            scheduled_at=data.get("scheduled_at", ""),
            name=data.get("name", ""),
            task_type=data.get("type", "deploy"),
        )
        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 400


@app.route("/api/scheduled-tasks/<task_id>", methods=["DELETE"])
def cancel_scheduled_task(task_id):
    result = task_manager.cancel_task(task_id)
    if result.get("success"):
        return jsonify(result)
    result = task_manager.delete_task(task_id)
    if result.get("success"):
        return jsonify(result)
    return jsonify(result), 404


@app.after_request
def add_no_cache(response):
    if request.path.endswith('.js'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


def _open_browser_when_ready():
    for _ in range(50):
        try:
            with urllib.request.urlopen("http://127.0.0.1:5000/api/health", timeout=1):
                webbrowser.open("http://127.0.0.1:5000")
                return
        except OSError:
            time.sleep(0.1)


if __name__ == "__main__":
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    socketio.run(app, host="127.0.0.1", port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
