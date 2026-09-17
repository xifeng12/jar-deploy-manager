import sqlite3
import json
import os
import threading

from runtime_paths import get_runtime_paths
from jar_names import normalize_jar_name

DB_PATH = str(get_runtime_paths().database)
_lock = threading.Lock()


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_conn = None


def get_conn():
    global _conn
    if _conn is None:
        _conn = _connect()
        _init_tables()
    return _conn


def _init_tables():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS servers (
            ip TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            jars TEXT NOT NULL DEFAULT '[]',
            status TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS deploy_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            servers TEXT NOT NULL,
            jars TEXT NOT NULL,
            success_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            skip_count INTEGER DEFAULT 0,
            duration_seconds INTEGER,
            status TEXT DEFAULT 'done',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS log_offsets (
            server_ip TEXT NOT NULL,
            jar_name TEXT NOT NULL,
            filename TEXT NOT NULL,
            offset INTEGER DEFAULT 0,
            inode INTEGER,
            size INTEGER,
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (server_ip, jar_name, filename)
        );
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(servers)").fetchall()}
    if "name" not in columns:
        conn.execute("ALTER TABLE servers ADD COLUMN name TEXT NOT NULL DEFAULT ''")
    conn.commit()


# ========== Servers CRUD ==========

def get_all_servers():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM servers ORDER BY ip").fetchall()
    result = []
    for row in rows:
        result.append({
            "ip": row["ip"],
            "name": row["name"],
            "jars": json.loads(row["jars"]),
            "status": row["status"],
        })
    return result


def get_server(ip):
    conn = get_conn()
    row = conn.execute("SELECT * FROM servers WHERE ip=?", (ip,)).fetchone()
    if not row:
        return None
    return {
        "ip": row["ip"],
        "name": row["name"],
        "jars": json.loads(row["jars"]),
        "status": row["status"],
    }


def _check_jar_ownership(conn, jars, excluded_ips):
    names = {normalize_jar_name(jar["name"] if isinstance(jar, dict) else jar) for jar in jars or []}
    for row in conn.execute("SELECT ip, jars FROM servers"):
        if row["ip"] in excluded_ips:
            continue
        for jar in json.loads(row["jars"]):
            jar_name = normalize_jar_name(jar["name"] if isinstance(jar, dict) else jar)
            if jar_name in names:
                raise ValueError(f"{jar_name} 已归属服务器 {row['ip']}，一个JAR包只能归属一台服务器")


def add_server(ip, jars=None, name=""):
    conn = get_conn()
    jars_json = json.dumps(jars or [], ensure_ascii=False)
    with _lock, conn:
        if conn.execute("SELECT 1 FROM servers WHERE ip=?", (ip,)).fetchone():
            raise ValueError(f"服务器 {ip} 已存在")
        _check_jar_ownership(conn, jars, set())
        conn.execute(
            "INSERT INTO servers (ip, name, jars, updated_at) VALUES (?, ?, ?, datetime('now','localtime'))",
            (ip, name or "", jars_json)
        )


def update_server(old_ip, ip, jars=None, name=""):
    conn = get_conn()
    jars_json = json.dumps(jars or [], ensure_ascii=False)
    with _lock, conn:
        if not conn.execute("SELECT 1 FROM servers WHERE ip=?", (old_ip,)).fetchone():
            raise ValueError(f"服务器 {old_ip} 不存在")
        if old_ip != ip and conn.execute("SELECT 1 FROM servers WHERE ip=?", (ip,)).fetchone():
            raise ValueError(f"服务器 {ip} 已存在")
        _check_jar_ownership(conn, jars, {old_ip})
        conn.execute(
            "UPDATE servers SET ip=?, name=?, jars=?, updated_at=datetime('now','localtime') WHERE ip=?",
            (ip, name or "", jars_json, old_ip)
        )


def delete_server(ip):
    conn = get_conn()
    with _lock:
        conn.execute("DELETE FROM servers WHERE ip=?", (ip,))
        conn.commit()


def update_server_status(ip, status):
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE servers SET status=?, updated_at=datetime('now','localtime') WHERE ip=?",
            (status, ip)
        )
        conn.commit()


# ========== Settings CRUD ==========

def get_all_settings():
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row:
        return row["value"]
    return default


def set_setting(key, value):
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now','localtime'))",
            (key, str(value))
        )
        conn.commit()


def set_settings(data):
    conn = get_conn()
    with _lock:
        for key, value in data.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now','localtime'))",
                (key, str(value))
            )
        conn.commit()


# ========== Deploy History CRUD ==========

def add_deploy_history(servers, jars, success_count, fail_count, skip_count=0, duration=None, status="done"):
    conn = get_conn()
    with _lock:
        conn.execute(
            "INSERT INTO deploy_history (servers, jars, success_count, fail_count, skip_count, duration_seconds, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (json.dumps(servers, ensure_ascii=False),
             json.dumps(jars, ensure_ascii=False),
             success_count, fail_count, skip_count, duration, status)
        )
        conn.commit()


def get_deploy_history(limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM deploy_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    result = []
    for row in rows:
        result.append({
            "id": row["id"],
            "servers": json.loads(row["servers"]),
            "jars": json.loads(row["jars"]),
            "success_count": row["success_count"],
            "fail_count": row["fail_count"],
            "skip_count": row["skip_count"],
            "duration_seconds": row["duration_seconds"],
            "status": row["status"],
            "created_at": row["created_at"],
        })
    return result


# ========== Log Offsets CRUD ==========

def get_offset(server_ip, jar_name, filename):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM log_offsets WHERE server_ip=? AND jar_name=? AND filename=?",
        (server_ip, jar_name, filename)
    ).fetchone()
    if row:
        return {"offset": row["offset"], "inode": row["inode"], "size": row["size"]}
    return {"offset": 0, "inode": None, "size": 0}


def set_offset(server_ip, jar_name, filename, offset, inode, size):
    conn = get_conn()
    with _lock:
        conn.execute(
            """INSERT OR REPLACE INTO log_offsets
               (server_ip, jar_name, filename, offset, inode, size, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
            (server_ip, jar_name, filename, offset, inode, size)
        )
        conn.commit()


def reset_offset(server_ip, jar_name, filename):
    conn = get_conn()
    with _lock:
        conn.execute(
            "DELETE FROM log_offsets WHERE server_ip=? AND jar_name=? AND filename=?",
            (server_ip, jar_name, filename)
        )
        conn.commit()


def db_exists():
    return os.path.exists(DB_PATH)
