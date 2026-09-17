import os
import threading
import logging
import time
import urllib.request
import webbrowser
from ipaddress import ip_address
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
from deploy_service import DeployService, ScheduleTaskManager, build_jar_plan, validate_restart_items
from crypto_utils import SecretEncryptionError, encrypt, is_encrypted
from db import get_all_servers, add_server, update_server, delete_server, get_all_settings, set_settings, get_deploy_history
from jar_names import normalize_jar_name, normalize_jar_names
from runtime_paths import ensure_runtime_directories, get_runtime_paths, resource_dir

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
app.config["TEMPLATES_AUTO_RELOAD"] = True
socketio = SocketIO(
    app,
    cors_allowed_origins=(
        "http://127.0.0.1:5000",
        "http://localhost:5000",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ),
    async_mode="threading",
)
ensure_runtime_directories()

SECRET_FIELDS = ("password", "jump_password", "server_key_passphrase", "jump_key_passphrase")
SECRET_MASK = "********"

deploy_service = DeployService(socketio)
task_manager = ScheduleTaskManager(deploy_service, socketio)
task_manager.recover()

logging.basicConfig(level=logging.INFO)

BASE_DIR = str(get_runtime_paths().home)


def is_safe_jar_name(filename):
    name = str(filename or "")
    return bool(
        name
        and name == os.path.basename(name)
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and name.lower().endswith(".jar")
    )


def normalize_server_ip(value):
    try:
        return str(ip_address(str(value).strip()))
    except ValueError:
        return ""


def resolve_jar_name(filename):
    target_core = deploy_service._extract_core(normalize_jar_name(filename))
    matches = {
        name
        for server in get_all_servers()
        for jar in server.get("jars", [])
        for name in [jar.get("name") if isinstance(jar, dict) else jar]
        if name
        and is_safe_jar_name(name)
        and deploy_service._extract_core(name) == target_core
    }
    return next(iter(matches)) if len(matches) == 1 else None


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
        data = request.get_json(silent=True) or {}
        server_ip = normalize_server_ip(data.get("ip"))
        if not server_ip:
            return jsonify({"success": False, "error": "服务器地址必须是合法IP"}), 400
        try:
            add_server(server_ip, data.get("jars", []), data.get("name", "").strip())
        except ValueError as error:
            return jsonify({"success": False, "error": str(error)}), 400
        return jsonify({"success": True})
    elif request.method == "PUT":
        data = request.get_json(silent=True) or {}
        server_ip = normalize_server_ip(data.get("ip"))
        if not data.get("old_ip") or not server_ip:
            return jsonify({"success": False, "error": "服务器地址必须是合法IP"}), 400
        try:
            update_server(data["old_ip"], server_ip, data.get("jars", []), data.get("name", "").strip())
        except ValueError as error:
            return jsonify({"success": False, "error": str(error)}), 400
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
            if f.lower().endswith(".jar"):
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
        if file.filename.lower().endswith(".jar"):
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

    try:
        selected_servers = list(build_jar_plan(all_servers, selected_servers, selected_jars))
    except ValueError as error:
        return jsonify({"success": False, "error": str(error)}), 400
    if not selected_servers:
        return jsonify({"success": False, "error": "请选择已关联服务器的JAR包"}), 400

    return _start_operation("deploy", deploy_service.deploy, selected_servers, selected_jars)


def _start_operation(operation, action, *args):
    operation_id = deploy_service.reserve_operation(operation)
    if operation_id is None:
        return jsonify({
            "success": False,
            "error": "已有部署、重启或回滚任务正在执行",
            "operation_status": deploy_service.get_operation_status(),
        }), 409
    try:
        thread = threading.Thread(target=lambda: action(*args, operation_id=operation_id))
        thread.start()
    except Exception:
        deploy_service.release_reservation(operation_id)
        app.logger.exception("无法启动%s任务", operation)
        return jsonify({"success": False, "error": "无法启动任务，请稍后重试"}), 500
    return jsonify({"success": True, "operation_id": operation_id, "task_id": thread.ident})


@app.route("/api/deploy/status", methods=["GET"])
def deploy_status():
    return jsonify(deploy_service.get_operation_status())


@app.route("/api/deploy/history", methods=["GET"])
def deploy_history():
    return jsonify(get_deploy_history())


@app.route("/api/deploy/cancel", methods=["POST"])
def cancel_deploy():
    deploy_service.cancel_deploy()
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
    data = request.get_json(silent=True) or {}
    server_ip = normalize_server_ip(data.get("ip"))
    if not server_ip:
        return jsonify({"success": False, "error": "服务器地址必须是合法IP"}), 400
    if not any(normalize_server_ip(server.get("ip")) == server_ip for server in get_all_servers()):
        return jsonify({"success": False, "error": "服务器未登记"}), 404
    request_id = data.get("request_id")

    def run_test():
        result = deploy_service.test_connection(server_ip)
        socketio.emit("test_result", {"ip": server_ip, "request_id": request_id, "result": result})

    thread = threading.Thread(target=run_test)
    thread.start()
    return jsonify({"success": True, "message": "测试中..."})


@app.route("/api/rollback", methods=["POST"])
def rollback():
    data = request.get_json(silent=True) or {}
    server_ip = normalize_server_ip(data.get("server_ip"))
    jar_name = normalize_jar_name(data.get("jar_name"))
    if not server_ip or not is_safe_jar_name(jar_name):
        return jsonify({"success": False, "error": "请提供有效服务器IP和JAR名称"}), 400
    try:
        validate_restart_items(get_all_servers(), [{"server": server_ip, "jar": jar_name}])
    except ValueError as error:
        return jsonify({"success": False, "error": str(error)}), 400
    return _start_operation("rollback", deploy_service.rollback, server_ip, jar_name)


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

    try:
        validate_restart_items(get_all_servers(), valid_items)
    except ValueError as error:
        return jsonify({"success": False, "error": str(error)}), 400

    return _start_operation("restart", deploy_service.restart, valid_items)


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
