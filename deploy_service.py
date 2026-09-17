import os
import json
import re
import socket
import shlex
from ipaddress import ip_address
import paramiko
import time
import threading
import uuid
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock, Event
from crypto_utils import SecretEncryptionError, decrypt, encrypt, is_encrypted
from db import get_all_servers, get_all_settings, set_settings, add_deploy_history
from runtime_paths import get_runtime_paths
from ssh_connection import AuthConfig, ConnectionConfig, ConnectionConfigurationError, SshConnectionFactory
from jar_names import normalize_jar_name, normalize_jar_names

deploy_lock = Lock()
deploy_cancel_flag = Event()
current_task = None
task_status = {"operation_id": None, "operation": None, "status": "idle", "success": 0, "fail": 0, "skip": 0}
operation_state_lock = Lock()


def build_jar_plan(servers, selected_servers, selected_jars):
    plan = {ip: [] for ip in selected_servers}
    for jar_name in normalize_jar_names(selected_jars):
        owners = {
            server["ip"] for server in servers
            if jar_name in {
                normalize_jar_name(jar["name"] if isinstance(jar, dict) else jar)
                for jar in (server.get("jars") or [])
            }
        }
        if len(owners) != 1:
            raise ValueError(f"{jar_name} 必须且只能归属一台服务器，请检查服务器配置")
        owner = next(iter(owners))
        if owner not in plan:
            raise ValueError(f"{jar_name} 的所属服务器 {owner} 未选中")
        plan[owner].append(jar_name)
    return {ip: names for ip, names in plan.items() if names}


def validate_restart_items(servers, items):
    plan = build_jar_plan(servers, [item["server"] for item in items], [item["jar"] for item in items])
    for item in items:
        if normalize_jar_name(item["jar"]) not in plan.get(item["server"], []):
            raise ValueError(f"{item['jar']} 不属于服务器 {item['server']}")


class DeployService:
    def __init__(self, socketio):
        self.socketio = socketio
        self.config = {}
        self.last_deploy_success = 0
        self.last_deploy_fail = 0
        self.current_log_file = None
        self.log_file_lock = Lock()
        self.load_config()
        self._migrate_servers()

    def load_config(self):
        self.config = get_all_settings()
        self._migrate_passwords()

    def update_config(self, config):
        self.config = config

    def _start_log_file(self):
        logs_dir = str(get_runtime_paths().logs)
        os.makedirs(logs_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filepath = os.path.join(logs_dir, f"deploy_{stamp}.log")
        with self.log_file_lock:
            self.current_log_file = filepath
        return filepath

    def _stop_log_file(self):
        with self.log_file_lock:
            self.current_log_file = None

    def _write_log_file(self, message, level):
        with self.log_file_lock:
            filepath = self.current_log_file
        if not filepath:
            return
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] [{level.upper()}] {message}\n")
        except Exception:
            pass

    def _migrate_servers(self):
        servers_path = str(get_runtime_paths().home / "servers.json")
        if not os.path.exists(servers_path):
            return
        with open(servers_path, "r", encoding="utf-8") as f:
            servers = json.load(f)
        changed = False
        migrated = 0
        for s in servers:
            j = s.get("jars", [])
            if j and isinstance(j[0], str):
                s["jars"] = [{"name": name, "script": self.get_script_name(name)} for name in j]
                changed = True
                migrated += 1
        if changed:
            with open(servers_path, "w", encoding="utf-8") as f:
                json.dump(servers, f, ensure_ascii=False, indent=2)
            self.log(f"[系统] 已迁移 {migrated} 台服务器的 JAR 配置到显式脚本名", "info")

    def _find_jar_script(self, jar_name):
        target_name = normalize_jar_name(jar_name)
        for server in get_all_servers():
            for jar in server.get("jars", []):
                if isinstance(jar, dict) and normalize_jar_name(jar.get("name")) == target_name:
                    script = jar.get("script")
                    if script:
                        return script
                    return self.get_script_name(target_name)
        return self.get_script_name(target_name)

    def _find_jar_config(self, jar_name, server_ip=None):
        target_name = normalize_jar_name(jar_name)
        for server in get_all_servers():
            if server_ip and server.get("ip") != server_ip:
                continue
            for jar in server.get("jars", []):
                if isinstance(jar, dict) and normalize_jar_name(jar.get("name")) == target_name:
                    return jar
        return None

    def _resolve_log_path(self, log_dir):
        log_dir = str(log_dir or "").strip()
        if not log_dir:
            return ""
        if log_dir.startswith("/"):
            return log_dir.rstrip("/")
        log_root = self.config.get("log_dir", "/home/app/applog/")
        return f"{log_root.rstrip('/')}/{log_dir.strip('/')}"

    def _safe_relpath(self, relpath):
        relpath = str(relpath or "").replace("\\", "/").strip("/")
        if not relpath or ".." in relpath.split("/"):
            return ""
        return relpath

    def _log_identity_tokens(self, jar_name):
        return {part for part in self._extract_core(jar_name) if len(part) > 1}

    def _path_identity_tokens(self, value):
        value = re.sub(r"\s*\(\d+\)(?=\.jar$)", "", str(value or "")).lower()
        return {part for part in re.split(r"[^a-z0-9]+", value) if len(part) > 1}

    def _log_file_belongs_to_jar(self, jar_name, log_subdir, relpath):
        safe_relpath = self._safe_relpath(relpath)
        if not safe_relpath:
            return False
        jar_tokens = self._log_identity_tokens(jar_name)
        if not jar_tokens:
            return False

        rel_tokens = self._path_identity_tokens(safe_relpath)
        if jar_tokens & rel_tokens:
            return True

        log_dir_parts = str(log_subdir or "").replace("\\", "/").strip("/").split("/")
        log_dir_tail = log_dir_parts[-1] if log_dir_parts else ""
        return bool(jar_tokens & self._path_identity_tokens(log_dir_tail))

    def _socket_yield(self):
        try:
            self.socketio.sleep(0)
        except Exception:
            pass

    def _extract_core(self, filename):
        jar_prefix = self.config.get("jar_prefix", "land-")
        jar_suffix = self.config.get("jar_suffix", "-biz.jar")
        script_prefix = self.config.get("script_prefix", "shell-")
        script_suffix = self.config.get("script_suffix", ".sh")

        # Strip known prefix
        name = normalize_jar_name(filename)
        if name.startswith(jar_prefix):
            name = name[len(jar_prefix):]
        elif name.startswith(script_prefix):
            name = name[len(script_prefix):]

        # Strip known suffix
        if name.endswith(jar_suffix):
            name = name[:-len(jar_suffix)]
        elif name.endswith(script_suffix):
            name = name[:-len(script_suffix)]
        elif name.endswith(".jar"):
            name = name[:-4]

        # Normalize: lowercase, split on - and .
        parts = [p.lower() for p in name.replace(".", "-").split("-") if p]
        return parts

    def _match_jars_scripts(self, jar_names, script_names):
        jar_cores = [(j, self._extract_core(j)) for j in jar_names]
        script_cores = [(s, self._extract_core(s)) for s in script_names]
        matched = {}
        result = []

        # Pass 1: exact match (intersection of all parts)
        for jar_name, jcore in jar_cores:
            jset = frozenset(jcore)
            candidates = [(sn, score) for sn, score in script_cores
                          if frozenset(score) == jset]
            if len(candidates) == 1:
                matched[jar_name] = candidates[0][0]
                script_cores = [(sn, sc) for sn, sc in script_cores if sn != candidates[0][0]]

        # Pass 2: partial match (at least one word overlap)
        for jar_name, jcore in jar_cores:
            if jar_name in matched:
                continue
            jset = set(jcore)
            candidates = [(sn, score) for sn, score in script_cores
                          if jset & set(score)]
            if len(candidates) == 1:
                matched[jar_name] = candidates[0][0]
                script_cores = [(sn, sc) for sn, sc in script_cores if sn != candidates[0][0]]

        # Build result
        used_scripts = set(matched.values())
        for jar_name, jcore in jar_cores:
            if jar_name in matched:
                result.append({"name": jar_name, "script": matched[jar_name], "match": "exact"})
            else:
                remaining = [sn for sn, _ in script_cores]
                result.append({"name": jar_name, "script": None, "match": "none",
                               "script_options": remaining})

        unmatched_scripts = [sn for sn, _ in script_cores]
        return result, unmatched_scripts

    def log(self, message, level="info"):
        self._write_log_file(message, level)
        try:
            self.socketio.emit("log", {"message": message, "level": level})
        except Exception:
            self._write_log_file("实时日志推送失败，请查看本地日志", "warning")

    def _emit_operation_event(self, event, result):
        try:
            self.socketio.emit(event, result)
        except Exception:
            self.log("操作状态推送失败，请刷新页面查询最终状态", "warning")

    def _migrate_passwords(self):
        changed = False
        for key in ("password", "jump_password", "server_key_passphrase", "jump_key_passphrase"):
            value = self.config.get(key, "")
            if value and not is_encrypted(value):
                try:
                    self.config[key] = encrypt(value)
                except SecretEncryptionError:
                    return
                changed = True
        if changed:
            set_settings(self.config)

    def _secret_value(self, key):
        value = self.config.get(key, "")
        if not value or str(value).startswith("********"):
            return ""
        if not is_encrypted(value):
            raise SecretEncryptionError("秘密配置未加密")
        return decrypt(value)

    def _registered_server_ip(self, server_ip):
        try:
            normalized = str(ip_address(str(server_ip).strip()))
        except ValueError as error:
            raise ConnectionConfigurationError("目标服务器地址必须是合法IP") from error
        for server in get_all_servers():
            try:
                if str(ip_address(str(server.get("ip", "")).strip())) == normalized:
                    return normalized
            except ValueError:
                continue
        raise ConnectionConfigurationError("目标服务器未登记")

    def _connection_config(self, server_ip, fallback_password=""):
        server_ip = self._registered_server_ip(server_ip)
        jump_host = str(self.config.get("jump_host", "")).strip()
        configured_mode = self.config.get("connection_mode", "")
        mode = configured_mode or ("jump" if jump_host else "direct")
        target_private_key = str(self.config.get("server_use_private_key", "")).lower() in ("1", "true", "yes", "on")
        target_auth = AuthConfig(
            username=str(self.config.get("server_user", "")).strip(),
            password=self._secret_value("password") or fallback_password,
            use_private_key=target_private_key,
            key_path=str(self.config.get("server_key_path", "")).strip(),
            key_passphrase=self._secret_value("server_key_passphrase"),
        )
        jump_auth = None
        if mode == "jump":
            jump_private_key = str(self.config.get("jump_use_private_key", "")).lower() in ("1", "true", "yes", "on")
            jump_auth = AuthConfig(
                username=str(self.config.get("jump_user", "")).strip(),
                password=self._secret_value("jump_password"),
                use_private_key=jump_private_key,
                key_path=str(self.config.get("jump_key_path", "")).strip(),
                key_passphrase=self._secret_value("jump_key_passphrase"),
            )
        return ConnectionConfig(
            mode=mode,
            target_host=server_ip,
            target_port=int(self.config.get("server_port", 22)),
            target_auth=target_auth,
            jump_host=jump_host,
            jump_port=int(self.config.get("jump_port", 22)),
            jump_auth=jump_auth,
            host_key_policy=str(self.config.get("host_key_policy", "accept-new")),
        )

    def open_ssh_connection(self, server_ip, fallback_password=""):
        return SshConnectionFactory(self._connection_config(server_ip, fallback_password)).connect()

    def test_connection(self, server_ip):
        result = {"success": False, "message": "", "steps": []}
        try:
            config = self._connection_config(server_ip)
            mode_name = "连接跳板机并打开转发" if config.mode == "jump" else "直连服务器"
            result["steps"].append({"name": mode_name, "status": "testing"})
            with self.open_ssh_connection(server_ip) as connection:
                result["steps"][-1]["status"] = "ok"
                result["steps"].append({"name": "执行 jps 命令", "status": "testing"})
                _, stdout, _ = connection.client.exec_command("jps -l", timeout=10)
                jps_output = stdout.read().decode().strip()
                result["steps"][-1]["status"] = "ok"
                result["steps"][-1]["detail"] = jps_output[:300] if jps_output else "(无Java进程)"
            result["success"] = True
            result["message"] = "连接成功"
        except SecretEncryptionError:
            result["message"] = "凭据无法解密，请在当前 Windows 用户下重新保存密码或私钥口令"
        except (ConnectionConfigurationError, ValueError):
            result["message"] = "连接配置无效，请检查认证方式、端口和密钥路径"
        except (socket.timeout, OSError):
            result["message"] = "连接超时，请检查IP、端口或网络"
        except paramiko.AuthenticationException:
            result["message"] = "SSH认证失败，请检查账号、密码或私钥口令"
        except paramiko.SSHException as error:
            result["message"] = f"SSH错误: {error}"
        for step in result["steps"]:
            if step["status"] == "testing":
                step["status"] = "fail"
        return result

    def get_script_name(self, jar_name):
        jar_prefix = self.config.get("jar_prefix", "land-")
        jar_suffix = self.config.get("jar_suffix", "-biz.jar")
        script_prefix = self.config.get("script_prefix", "shell-")
        script_suffix = self.config.get("script_suffix", ".sh")

        clean_name = normalize_jar_name(jar_name)
        name_part = clean_name
        if clean_name.startswith(jar_prefix):
            name_part = clean_name[len(jar_prefix):]
        elif clean_name.startswith(script_prefix):
            name_part = clean_name[len(script_prefix):]

        suffix_pos = name_part.rfind(jar_suffix)
        if suffix_pos > 0:
            core_name = name_part[:suffix_pos]
        else:
            dot_pos = name_part.rfind(".")
            without_ext = name_part[:dot_pos] if dot_pos > 0 else name_part
            dash_pos = without_ext.rfind("-")
            core_name = without_ext[:dash_pos] if dash_pos > 0 else without_ext
        return f"{script_prefix}{core_name}{script_suffix}"

    def reserve_operation(self, operation):
        global current_task, task_status
        with operation_state_lock:
            if not deploy_lock.acquire(blocking=False):
                return None
            operation_id = uuid.uuid4().hex
            deploy_cancel_flag.clear()
            current_task = operation_id
            task_status = {"operation_id": operation_id, "operation": operation, "status": "running",
                           "success": 0, "fail": 0, "skip": 0}
            return operation_id

    def release_reservation(self, operation_id):
        global current_task, task_status
        with operation_state_lock:
            if current_task == operation_id:
                current_task = None
                task_status = {**task_status, "status": "failed"}
                try:
                    self._emit_operation_event("operation_done", task_status)
                finally:
                    deploy_lock.release()

    def get_operation_status(self):
        with operation_state_lock:
            return dict(task_status)

    def _run_operation(self, operation, servers, jars, action, operation_id=None):
        global current_task, task_status
        operation_id = operation_id or self.reserve_operation(operation)
        if not operation_id:
            return {"success": 0, "fail": 0, "skip": 0, "status": "busy"}
        with operation_state_lock:
            if current_task != operation_id or task_status.get("operation") != operation:
                raise ValueError("操作预留已失效")
        started = time.monotonic()
        result = {"success": 0, "fail": len(jars), "skip": 0, "status": "failed"}
        try:
            self._start_log_file()
            self.log(f"[操作] {operation_id} {operation}: {servers} / {jars}")
            self._emit_operation_event("operation_started", self.get_operation_status())
            result = action()
            if deploy_cancel_flag.is_set():
                result["status"] = "cancelled"
            elif result.get("fail") or result.get("skip"):
                result["status"] = "failed"
        except Exception as error:
            self.log(f"[错误] {operation} 未完成: {error}", "error")
            result["error"] = str(error)
        finally:
            result.update(operation_id=operation_id, operation=operation)
            try:
                add_deploy_history(servers, jars, result["success"], result["fail"], result["skip"],
                                   round(time.monotonic() - started), f"{operation}:{result['status']}")
            except Exception as error:
                result["audit_error"] = "历史记录保存失败，请保留日志"
                self.log(f"[审计错误] 历史记录未保存: {error}", "error")
            self.log(f"[系统] 日志已保存: {self.current_log_file}")
            self._stop_log_file()
            with operation_state_lock:
                task_status = dict(result)
                current_task = None
                # Emit before releasing so an older completion cannot follow a new start.
                try:
                    self._emit_operation_event("operation_done", result)
                finally:
                    deploy_lock.release()
        return result

    def deploy(self, selected_servers, selected_jars, operation_id=None):
        return self._run_operation("deploy", selected_servers, selected_jars,
                                   lambda: self._deploy(selected_servers, selected_jars), operation_id)

    def restart(self, items, operation_id=None):
        return self._run_operation("restart", list(dict.fromkeys(i["server"] for i in items)),
                                   [i["jar"] for i in items], lambda: self._restart(items), operation_id)

    def rollback(self, server_ip, jar_name, operation_id=None):
        return self._run_operation("rollback", [server_ip], [jar_name],
                                   lambda: self._rollback(server_ip, jar_name), operation_id)

    def _checked_command(self, ssh, command):
        _, stdout, stderr = ssh.exec_command(command, timeout=120)
        output = stdout.read().decode("utf-8", errors="replace").strip()
        error = stderr.read().decode("utf-8", errors="replace").strip()
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            raise RuntimeError(f"远程命令失败（退出码 {exit_code}）: {error[:500]}")
        return output

    def _jar_pids(self, ssh, jar_name):
        output = self._checked_command(ssh, "source /etc/profile && jps -l")
        return {parts[0] for line in output.splitlines() if len(parts := line.split()) >= 2
                and parts[0].isdigit() and os.path.basename(parts[1]) == jar_name}

    def _restart_verified(self, ssh, jar_name, remote_script, start_wait):
        old_pids = self._jar_pids(ssh, jar_name)
        self._checked_command(ssh, f"source /etc/profile && {shlex.quote(remote_script)} restart")
        time.sleep(start_wait)
        for attempt in range(3):
            new_pids = self._jar_pids(ssh, jar_name)
            if new_pids and not (new_pids & old_pids):
                for pid in new_pids:
                    self._checked_command(ssh, f"kill -0 {pid}")
                return
            if attempt < 2:
                time.sleep(5)
        raise RuntimeError("重启后未检测到新的存活 JAR 进程，不能确认启动成功")

    def _restore_backup(self, ssh, backup, remote_jar):
        temporary = f"{remote_jar}.restore-{uuid.uuid4().hex}"
        try:
            self._checked_command(ssh, f"cp -p -- {shlex.quote(backup)} {shlex.quote(temporary)} && "
                                  f"cmp -s -- {shlex.quote(backup)} {shlex.quote(temporary)} && "
                                  f"mv -f -- {shlex.quote(temporary)} {shlex.quote(remote_jar)}")
        finally:
            self._checked_command(ssh, f"rm -f -- {shlex.quote(temporary)}")

    def _deploy_one(self, ssh, task, start_wait):
        remote = task["remote_jar"]
        temporary = f"{remote}.upload-{uuid.uuid4().hex}"
        replaced = False
        sftp = ssh.open_sftp()
        try:
            if not task["script_exists"]:
                raise RuntimeError("启动脚本不存在，未上传或替换 JAR")
            if task["jar_exists"] and not task["backup_file"]:
                raise RuntimeError("备份未确认，禁止替换 JAR")
            sftp.put(task["local_jar"], temporary)
            if sftp.stat(temporary).st_size != os.path.getsize(task["local_jar"]):
                raise RuntimeError("临时 JAR 上传大小不符，未替换旧文件")
            if task["jar_exists"]:
                sftp.chmod(temporary, sftp.stat(remote).st_mode & 0o777)
            replaced = True
            sftp.posix_rename(temporary, remote)
            self._restart_verified(ssh, task["jar_name"], task["remote_script"], start_wait)
        except Exception:
            if replaced and task["backup_file"]:
                try:
                    self._restore_backup(ssh, task["backup_file"], remote)
                    self._restart_verified(ssh, task["jar_name"], task["remote_script"], start_wait)
                    self.log(f"[恢复] {task['jar_name']} 已恢复旧文件并验证启动；备份保留", "warning")
                except Exception as recovery_error:
                    self.log(f"[恢复失败] {task['jar_name']}: {recovery_error}；请手工检查，备份: {task['backup_file']}", "error")
            elif replaced:
                self.log(f"[恢复] {task['jar_name']} 无旧版本备份，新文件仍在远端，请手动检查", "error")
            raise
        finally:
            try:
                sftp.remove(temporary)
            except OSError:
                pass
            sftp.close()

    def _deploy(self, selected_servers, selected_jars):
        global current_task, task_status, deploy_cancel_flag

        selected_jars = normalize_jar_names(selected_jars)
        log_file = self.current_log_file
        task_status.update({
            "progress": 0,
            "success_count": 0,
            "fail_count": 0,
            "skip_count": 0,
        })

        completed = 0
        success_count = 0
        fail_count = 0
        skip_count = 0
        total_tasks = max(len(selected_jars), 1)
        status_lock = Lock()

        def update_totals(result):
            nonlocal completed, success_count, fail_count, skip_count
            with status_lock:
                completed += result.get("completed", 0)
                success_count += result.get("success", 0)
                fail_count += result.get("fail", 0)
                skip_count += result.get("skip", 0)
                task_status["progress"] = int(min(completed, total_tasks) / total_tasks * 100)
                task_status["success_count"] = success_count
                task_status["fail_count"] = fail_count
                task_status["skip_count"] = skip_count

        def deploy_server(server_ip, server_jars, jar_dir, script_dir, backup_dir,
                          deploy_interval, start_wait, jars_dir):
            result = {"success": 0, "fail": 0, "skip": 0, "completed": 0}
            prefix = f"[{server_ip}]"
            ssh = None
            connection = None
            jar_tasks = []

            if deploy_cancel_flag.is_set():
                result["skip"] = len(server_jars)
                result["completed"] = len(server_jars)
                return result

            try:
                self.log(f"\n{'═' * 50}")
                self.log(f"{prefix} 🖥️ 服务器队列开始，共 {len(server_jars)} 个JAR")
                connection = self.open_ssh_connection(server_ip)
                ssh = connection.client

                for jar_name in server_jars:
                    local_jar = os.path.join(jars_dir, jar_name)
                    if not os.path.exists(local_jar):
                        self.log(f"{prefix} ❌ 本地JAR包不存在: {jar_name}", "error")
                        result["fail"] += 1
                        result["completed"] += 1
                        continue
                    script_name = self._find_jar_script(jar_name)
                    remote_jar = f"{jar_dir}/{jar_name}"
                    remote_script = f"{script_dir.rstrip('/')}/{script_name}"
                    jar_tasks.append({
                        "jar_name": jar_name,
                        "local_jar": local_jar,
                        "remote_jar": remote_jar,
                        "remote_script": remote_script,
                        "script_name": script_name,
                        "backup_file": None,
                    })

                if not jar_tasks:
                    return result

                self.log(f"{prefix} [预检] 检查远程文件状态...")
                dir_missing = False
                for task in jar_tasks:
                    q_script = shlex.quote(task["remote_script"])
                    q_jar = shlex.quote(task["remote_jar"])
                    q_dir = shlex.quote(os.path.dirname(task["remote_jar"]))
                    stdin, stdout, stderr = ssh.exec_command(
                        f"printf 'script:'; test -f {q_script} && echo 'exists' || echo 'missing';"
                        f"printf 'jar:'; test -f {q_jar} && echo 'exists' || echo 'missing';"
                        f"printf 'dir:'; test -d {q_dir} && echo 'exists' || echo 'missing'"
                    )
                    out = stdout.read().decode().strip()
                    task["script_exists"] = "script:exists" in out
                    task["jar_exists"] = "jar:exists" in out
                    task["dir_exists"] = "dir:exists" in out
                    if not task["script_exists"]:
                        self.log(f"{prefix}   ⚠️ {task['script_name']} 不存在", "warning")
                    if not task["jar_exists"]:
                        self.log(f"{prefix}   ⚠️ {task['jar_name']} 远程不存在", "warning")
                    if not task["dir_exists"]:
                        self.log(f"{prefix}   ❌ 目录不存在: {os.path.dirname(task['remote_jar'])}", "error")
                        dir_missing = True

                if dir_missing:
                    self.log(f"{prefix}   ❌ 部署中止：请检查 jar_dir 和 script_dir 配置", "error")
                    result["fail"] += len(jar_tasks)
                    result["completed"] += len(jar_tasks)
                    return result

                self.log(f"{prefix} [备份] 批量备份旧JAR包...")
                for task in jar_tasks:
                    if deploy_cancel_flag.is_set():
                        break
                    if task["jar_exists"]:
                        jar_name = task["jar_name"]
                        remote_jar = task["remote_jar"]
                        q_remote = shlex.quote(remote_jar)
                        backup_file = f"{backup_dir}/{jar_name}.原{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex}"
                        self._checked_command(ssh,
                            f"mkdir -p -- {shlex.quote(backup_dir)} && "
                            f"cp -p -- {q_remote} {shlex.quote(backup_file)} && "
                            f"cmp -s -- {q_remote} {shlex.quote(backup_file)}")
                        task["backup_file"] = backup_file
                        self.log(f"{prefix}   ✓ {jar_name} → {task['backup_file']}")
                    else:
                        self.log(f"{prefix}   ⊘ {task['jar_name']} 远程不存在，跳过备份", "warning")

                if deploy_cancel_flag.is_set():
                    result["skip"] += len(jar_tasks)
                    result["completed"] += len(jar_tasks)
                    return result

                for index, task in enumerate(jar_tasks):
                    if deploy_cancel_flag.is_set():
                        result["skip"] += len(jar_tasks) - index
                        result["completed"] += len(jar_tasks) - index
                        break

                    jar_name = task["jar_name"]
                    self.log(f"{prefix} [{jar_name}] [上传] 正在上传JAR包...")
                    try:
                        self._deploy_one(ssh, task, start_wait)
                        self.log(f"{prefix} [{jar_name}] ✅ 新版本启动成功")
                        result["success"] += 1
                    except Exception as error:
                        self.log(f"{prefix} [{jar_name}] ❌ 部署失败: {error}", "error")
                        result["fail"] += 1

                    result["completed"] += 1
                    if index < len(jar_tasks) - 1 and deploy_interval > 0:
                        self.log(f"{prefix} [等待] {deploy_interval}秒后继续下一个JAR...")
                        time.sleep(deploy_interval)

                return result

            except Exception as e:
                self.log(f"{prefix} [❌ 错误] {str(e)}", "error")
                remaining = max(len(server_jars) - result["completed"], 0)
                result["fail"] += remaining
                result["completed"] += remaining
                return result
            finally:
                if connection:
                    connection.close()

        try:
            self.load_config()
            all_servers = get_all_servers()
            plan = build_jar_plan(all_servers, selected_servers, selected_jars)
            if not plan:
                raise ValueError("请选择已关联服务器的JAR包")
            selected_servers = list(plan)

            jars_dir = str(get_runtime_paths().jars)
            missing_jars = [
                jar_name for jar_name in selected_jars
                if not os.path.exists(os.path.join(jars_dir, jar_name))
            ]
            if missing_jars:
                self.log(f"❌ 以下JAR包未上传，部署中止: {', '.join(missing_jars)}", "error")
                return {"success": 0, "fail": len(missing_jars), "skip": 0, "status": "failed"}

            jar_dir = self.config.get("jar_dir", "/opt/app/jars")
            script_dir = self.config.get("script_dir", "/opt/app/scripts")
            backup_dir = self.config.get("backup_dir", "/opt/app/backup")
            deploy_interval = int(self.config.get("deploy_interval", 30))
            start_wait = int(self.config.get("start_wait", 10))

            worker_count = max(len(selected_servers), 1)
            self.log("=" * 50)
            self.log("🚀 开始并发部署")
            self.log("=" * 50)
            self.log(f"[系统] 日志文件: {log_file}")
            self.log(f"连接方式: {self.config.get('connection_mode') or ('jump' if self.config.get('jump_host') else 'direct')}")
            self.log(f"部署服务器: {len(selected_servers)} 台")
            self.log(f"部署JAR包: {len(selected_jars)} 个")
            self.log(f"并发服务器: {worker_count} 台")
            self.log("策略: 多服务器并发，同一服务器内JAR按顺序部署")

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(
                        deploy_server,
                        server_ip, plan[server_ip], jar_dir,
                        script_dir, backup_dir, deploy_interval, start_wait, jars_dir,
                    ): server_ip
                    for server_ip in selected_servers
                }
                for future in as_completed(futures):
                    result = future.result()
                    update_totals(result)

            status = "cancelled" if deploy_cancel_flag.is_set() else "done"
            self.log("\n" + "=" * 50)
            self.log("📊 部署完成")
            self.log(f"✅ 成功: {success_count} 个")
            self.log(f"❌ 失败: {fail_count} 个")
            if skip_count > 0:
                self.log(f"⚠️ 跳过: {skip_count} 个")
            self.log("=" * 50)

            self.last_deploy_success = success_count
            self.last_deploy_fail = fail_count
            return {
                "success": success_count,
                "fail": fail_count,
                "skip": skip_count,
                "status": status,
            }

        except Exception as e:
            self.log(f"[❌ 系统错误] {str(e)}", "error")
            return {"success": success_count, "fail": fail_count + 1, "skip": skip_count, "status": "failed"}


    def cancel_deploy(self):
        with operation_state_lock:
            if current_task:
                deploy_cancel_flag.set()
        self.log("⏹️ 用户请求停止；当前单项将完成或恢复后退出", "warning")

    def is_deploying(self):
        return deploy_lock.locked()

    def _restart(self, servers_jars):
        validate_restart_items(get_all_servers(), servers_jars)
        result = {"success": 0, "fail": 0, "skip": 0, "status": "done"}
        script_dir = self.config.get("script_dir", "/opt/app/scripts")
        start_wait = int(self.config.get("start_wait", 10))
        deploy_interval = int(self.config.get("deploy_interval", 30))
        for index, item in enumerate(servers_jars):
            if deploy_cancel_flag.is_set():
                result["skip"] += len(servers_jars) - index
                break
            jar_name = normalize_jar_name(item["jar"])
            connection = None
            try:
                connection = self.open_ssh_connection(item["server"])
                script = f"{script_dir.rstrip('/')}/{self._find_jar_script(jar_name)}"
                self._restart_verified(connection.client, jar_name, script, start_wait)
                self.log(f"[✅ 成功] {jar_name} 重启完成", "success")
                result["success"] += 1
            except Exception as error:
                self.log(f"[❌ 错误] {item['server']}:{jar_name}: {error}", "error")
                result["fail"] += 1
            finally:
                if connection:
                    connection.close()
            if index < len(servers_jars) - 1 and deploy_interval > 0:
                deploy_cancel_flag.wait(deploy_interval)
        return result

    def discover_jars(self, server_ip):
        result = {"success": False, "jars": [], "error": ""}
        jar_dir = self.config.get("jar_dir", "/opt/app/jars")

        connection = None

        try:
            connection = self.open_ssh_connection(server_ip)
            ssh = connection.client

            script_dir = self.config.get("script_dir", "/opt/app/scripts")
            script_prefix = self.config.get("script_prefix", "shell-")
            script_suffix = self.config.get("script_suffix", ".sh")
            log_dir = self.config.get("log_dir", "/home/app/applog/")
            cmd = (
                f"echo '---JARS---'; for f in {shlex.quote(jar_dir)}/*.jar; do [ -f \"$f\" ] && basename \"$f\"; done; "
                f"echo '---SCRIPTS---'; for f in {shlex.quote(script_dir)}/{shlex.quote(script_prefix)}*{shlex.quote(script_suffix)}; do [ -f \"$f\" ] && basename \"$f\"; done; "
                f"echo '---LOGDIRS---'; for d in {shlex.quote(log_dir)}/*/; do [ -d \"$d\" ] && basename \"$d\"; done; true"
            )
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
            out = stdout.read().decode().strip()

            jar_names = []
            script_names = []
            logdir_names = []
            section = None
            for line in out.split("\n"):
                line = line.strip()
                if line == "---JARS---":
                    section = "jars"
                elif line == "---SCRIPTS---":
                    section = "scripts"
                elif line == "---LOGDIRS---":
                    section = "logdirs"
                elif line and section == "jars":
                    jar_names.append(os.path.basename(line))
                elif line and section == "scripts":
                    script_names.append(os.path.basename(line))
                elif line and section == "logdirs":
                    logdir_names.append(line)

            if not jar_names:
                result["error"] = f"服务器 {server_ip} 的 {jar_dir} 目录下未找到 .jar 文件"
                return result

            result["success"] = True
            matched, unmatched_scripts = self._match_jars_scripts(sorted(jar_names), sorted(script_names))
            log_matched, _ = self._match_jars_scripts(sorted(jar_names), sorted(logdir_names))
            log_map = {m["name"]: m["script"] for m in log_matched if m["script"]}
            for m in matched:
                m["log_dir"] = log_map.get(m["name"], None)
            result["jars"] = matched
            result["unmatched_scripts"] = unmatched_scripts

        except paramiko.AuthenticationException:
            result["error"] = f"服务器 {server_ip} 认证失败，请检查用户名和密码"
        except Exception as e:
            result["error"] = f"获取失败: {str(e)}"
        finally:
            if connection:
                connection.close()

        return result

    def _rollback(self, server_ip, jar_name):
        jar_name = normalize_jar_name(jar_name)
        validate_restart_items(get_all_servers(), [{"server": server_ip, "jar": jar_name}])
        self.log(f"手动回滚: {server_ip}:{jar_name}")
        connection = self.open_ssh_connection(server_ip)
        try:
            ssh = connection.client
            jar_dir = self.config.get("jar_dir", "/opt/app/jars")
            script_dir = self.config.get("script_dir", "/opt/app/scripts")
            backup_dir = self.config.get("backup_dir", "/opt/app/backup")
            remote_jar = f"{jar_dir}/{jar_name}"
            remote_script = f"{script_dir.rstrip('/')}/{self._find_jar_script(jar_name)}"
            candidates = self._checked_command(ssh,
                f"find {shlex.quote(backup_dir)} -maxdepth 1 -type f "
                f"-name {shlex.quote(jar_name + '.*')} -printf '%T@ %p\\n' | sort -nr")
            backups = [line.split(" ", 1)[1] for line in candidates.splitlines() if " " in line]
            if not backups:
                raise RuntimeError("未找到备份文件，未修改远端 JAR")
            backup = backups[0]
            if os.path.dirname(backup) != backup_dir.rstrip("/") or not os.path.basename(backup).startswith(jar_name + "."):
                raise RuntimeError("备份路径与目标 JAR 不匹配")
            self._restore_backup(ssh, backup, remote_jar)
            self._restart_verified(ssh, jar_name, remote_script, int(self.config.get("start_wait", 10)))
            self.log("[✅] 回滚成功；备份文件保留", "success")
            return {"success": 1, "fail": 0, "skip": 0, "status": "done"}
        finally:
            connection.close()

    def list_log_files(self, server_ip, jar_name):
        result = {"success": False, "files": [], "error": ""}
        jar_config = self._find_jar_config(jar_name, server_ip)
        log_subdir = jar_config.get("log_dir", "") if jar_config else ""
        if not log_subdir:
            result["error"] = "该JAR未配置日志目录，请先使用自动获取功能发现"
            return result
        log_path = self._resolve_log_path(log_subdir)
        today = datetime.now().strftime("%Y-%m-%d")
        connection = None
        try:
            connection = self.open_ssh_connection(server_ip)
            ssh = connection.client
            cmd = (f"find {shlex.quote(log_path)} -type f \\( -name '*.log' -o -name '*.log.gz' \\) -newermt '{today} 00:00:00' ! -newermt '{today} 23:59:59' -exec stat -c '%Y|%s|%n' {{}} \\; 2>/dev/null | sort -t'|' -k3")
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
            out = stdout.read().decode().strip()
            result["success"] = True
            result["files"] = []
            if out:
                for line in out.split("\n"):
                    parts = line.strip().split("|", 2)
                    if len(parts) == 3:
                        mtime_ts, size, fullpath = int(parts[0]), int(parts[1]), parts[2]
                        relpath = fullpath[len(log_path):].lstrip("/")
                        if self._log_file_belongs_to_jar(jar_name, log_subdir, relpath):
                            result["files"].append({"filename": os.path.basename(fullpath), "relpath": relpath, "size": size, "mtime": datetime.fromtimestamp(mtime_ts).strftime("%H:%M:%S")})
        except Exception as e:
            result["error"] = f"获取日志列表失败: {str(e)}"
        finally:
            if connection: connection.close()
        return result

    def download_logs(self, server_ip, jar_name, filenames, save_dir, mode="overwrite"):
        jar_config = self._find_jar_config(jar_name, server_ip)
        log_subdir = jar_config.get("log_dir", "") if jar_config else ""
        if not log_subdir:
            self.log("[日志] 该JAR未配置日志目录", "error")
            return 0, 0
        log_path = self._resolve_log_path(log_subdir)
        os.makedirs(save_dir, exist_ok=True)
        connection = None
        success = 0; fail = 0; total = len(filenames)
        try:
            connection = self.open_ssh_connection(server_ip)
            ssh = connection.client
            sftp = ssh.open_sftp()
            self.log(f"[日志] 开始下载 {server_ip}:{jar_name} 的 {total} 个文件到 {save_dir}", "info")
            self._socket_yield()
            for i, relpath in enumerate(filenames, 1):
                safe_relpath = self._safe_relpath(relpath)
                if not safe_relpath:
                    self.log(f"[日志 {i}/{total}] 非法路径，已跳过", "error")
                    fail += 1
                    self._socket_yield()
                    continue
                if not self._log_file_belongs_to_jar(jar_name, log_subdir, safe_relpath):
                    self.log(f"[日志 {i}/{total}] 非当前JAR日志，已跳过: {safe_relpath}", "warning")
                    fail += 1
                    self._socket_yield()
                    continue
                remote_path = f"{log_path}/{safe_relpath}"
                local_name = os.path.basename(safe_relpath)
                local_path = os.path.join(save_dir, local_name)
                try:
                    if mode == "skip" and os.path.exists(local_path):
                        self.log(f"[日志 {i}/{total}] ⊘ {local_name} (已存在，跳过)", "warning")
                        success += 1
                        self._socket_yield()
                        continue
                    if mode == "rename" and os.path.exists(local_path):
                        base, ext = os.path.splitext(local_name)
                        n = 1
                        while os.path.exists(local_path):
                            local_path = os.path.join(save_dir, f"{base}({n}){ext}")
                            n += 1
                        local_name = os.path.basename(local_path)
                    self.log(f"[日志 {i}/{total}] 正在下载 {safe_relpath}", "info")
                    self._socket_yield()
                    sftp.get(remote_path, local_path)
                    self.log(f"[日志 {i}/{total}] ✓ {local_name} ({self._fmt(os.path.getsize(local_path))})", "success")
                    success += 1
                except Exception as e:
                    self.log(f"[日志 {i}/{total}] ✗ {os.path.basename(safe_relpath)}: {e}", "error")
                    fail += 1
                self._socket_yield()
            sftp.close()
        except Exception as e:
            self.log(f"[日志] 下载失败: {e}", "error")
            fail = total
        finally:
            if connection: connection.close()
        self.log(f"[日志] 下载完成: 成功 {success}, 失败 {fail}", "info")
        return success, fail

    def _fmt(self, size):
        if size < 1024: return f"{size}B"
        if size < 1024*1024: return f"{size/1024:.1f}KB"
        return f"{size/(1024*1024):.1f}MB"


class ScheduleTaskManager:
    def __init__(self, deploy_service, socketio):
        self.deploy_service = deploy_service
        self.socketio = socketio
        self.tasks_file = str(get_runtime_paths().scheduled_tasks)
        self.timers = {}
        self._lock = Lock()

    def _load_tasks(self):
        if os.path.exists(self.tasks_file):
            with open(self.tasks_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    def _save_tasks(self, tasks):
        directory = os.path.dirname(os.path.abspath(self.tasks_file))
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".scheduled-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                json.dump(tasks, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.tasks_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get_all(self):
        with self._lock:
            return self._load_tasks()

    def create_task(self, servers, jars, scheduled_at, name="", task_type="deploy"):
        if task_type not in ("deploy", "restart"):
            return {"success": False, "error": "无效的任务类型"}
        jars = normalize_jar_names(jars)
        if not servers:
            return {"success": False, "error": "请选择目标服务器"}
        if not jars:
            return {"success": False, "error": "请选择JAR包"}
        try:
            servers = list(build_jar_plan(get_all_servers(), servers, jars))
        except ValueError as error:
            return {"success": False, "error": str(error)}

        if len(scheduled_at) == 16:
            scheduled_at += ":00"
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at)
        except ValueError:
            return {"success": False, "error": "时间格式无效"}

        now = datetime.now()
        if scheduled_dt <= now:
            return {"success": False, "error": "预定时间必须晚于当前时间"}

        task_id = uuid.uuid4().hex
        task = {
            "id": task_id,
            "name": name or f"任务-{task_id[-6:]}",
            "type": task_type,
            "servers": servers,
            "jars": jars,
            "scheduled_at": scheduled_dt.isoformat(),
            "status": "pending",
            "created_at": now.isoformat(),
            "executed_at": None,
            "result": None,
            "error": None,
        }

        with self._lock:
            tasks = self._load_tasks()
            tasks.append(task)
            self._save_tasks(tasks)

        delay = (scheduled_dt - now).total_seconds()
        self._schedule_timer(task_id, delay)
        self.socketio.emit("scheduled_task_created", task)
        return {"success": True, "task": task}

    def cancel_task(self, task_id):
        with self._lock:
            tasks = self._load_tasks()
            for t in tasks:
                if t["id"] == task_id and t["status"] == "pending":
                    t["status"] = "cancelled"
                    self._save_tasks(tasks)
                    if task_id in self.timers:
                        self.timers[task_id].cancel()
                        del self.timers[task_id]
                    self.socketio.emit("scheduled_task_cancelled", {"id": task_id})
                    return {"success": True}
            return {"success": False, "error": "任务不存在或状态不允许取消"}

    def delete_task(self, task_id):
        with self._lock:
            tasks = self._load_tasks()
            for i, t in enumerate(tasks):
                if t["id"] == task_id:
                    if t["status"] == "running":
                        return {"success": False, "error": "运行中任务不能删除，请先等待结束"}
                    if t["status"] == "pending" and task_id in self.timers:
                        self.timers[task_id].cancel()
                        del self.timers[task_id]
                    del tasks[i]
                    self._save_tasks(tasks)
                    return {"success": True}
            return {"success": False, "error": "任务不存在"}

    def _schedule_timer(self, task_id, delay):
        if delay <= 0:
            return
        timer = threading.Timer(delay, self._execute_task, args=[task_id])
        timer.daemon = True
        self.timers[task_id] = timer
        timer.start()

    def _execute_task(self, task_id):
        with self._lock:
            tasks = self._load_tasks()
            task = next((t for t in tasks if t["id"] == task_id), None)
            if not task or task["status"] != "pending":
                return
            task["status"] = "running"
            task["executed_at"] = datetime.now().isoformat()
            self._save_tasks(tasks)

        self.socketio.emit("scheduled_task_started", {
            "id": task_id, "name": task["name"]
        })

        try:
            result = {"status": "busy"}
            deadline = time.monotonic() + 300
            while result.get("status") == "busy":
                if task["type"] == "deploy":
                    result = self.deploy_service.deploy(task["servers"], task["jars"])
                else:
                    plan = build_jar_plan(get_all_servers(), task["servers"], task["jars"])
                    items = [{"server": ip, "jar": jar} for ip, jars in plan.items() for jar in jars]
                    result = self.deploy_service.restart(items)
                if result.get("status") != "busy":
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("等待操作锁超时（5分钟）")
                time.sleep(10)
            if result.get("status") == "cancelled":
                status, error = "cancelled", "任务已取消；请查看已完成和跳过的数量"
            elif result.get("status") == "done" and result.get("success", 0) > 0 and not result.get("fail") and not result.get("skip"):
                status, error = "completed", None
            else:
                status, error = "failed", result.get("error") or "任务未全部完成，请查看日志"

        except Exception as e:
            status = "failed"
            result = None
            error = str(e)

        with self._lock:
            tasks = self._load_tasks()
            for t in tasks:
                if t["id"] == task_id:
                    t["status"] = status
                    t["result"] = result
                    t["error"] = error
            self._save_tasks(tasks)

        if task_id in self.timers:
            del self.timers[task_id]

        self.socketio.emit("scheduled_task_done", {
            "id": task_id, "name": task["name"],
            "status": status, "result": result, "error": error,
        })

    def recover(self):
        with self._lock:
            return self._recover()

    def _recover(self):
        tasks = self._load_tasks()
        now = datetime.now()
        recovered = 0
        for task in tasks:
            if task["status"] == "running":
                task["status"] = "interrupted"
                task["error"] = "进程曾中断，远端状态未知；请人工检查后新建任务，不自动重放"
                continue
            if task["status"] != "pending":
                continue
            try:
                scheduled_dt = datetime.fromisoformat(task["scheduled_at"])
            except ValueError:
                task["status"] = "expired"
                task["error"] = "无效的时间格式"
                continue
            if scheduled_dt <= now:
                task["status"] = "expired"
                task["error"] = "预定时间已过，服务曾离线"
            else:
                delay = (scheduled_dt - now).total_seconds()
                self._schedule_timer(task["id"], delay)
                recovered += 1
        self._save_tasks(tasks)
        return recovered
