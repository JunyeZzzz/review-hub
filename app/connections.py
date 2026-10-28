"""GitHub 凭据注册表：项目显式绑定；token 存权限受限的独立文件。"""
import os
import secrets
import subprocess
from pathlib import Path

from . import config, db

SECRET_DIR = config.DATA_DIR / "secrets"


def _secret_path(ref: str) -> Path:
    if not ref.startswith("file:") or "/" in ref[5:] or ".." in ref:
        raise ValueError("无效凭据引用")
    return SECRET_DIR / ref[5:]


def _write_secret(token: str) -> str:
    SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(SECRET_DIR, 0o700)
    name = secrets.token_hex(20)
    fd = os.open(SECRET_DIR / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token.strip())
    return f"file:{name}"


def create(label: str, token: str) -> int:
    if not label.strip() or not token.strip():
        raise ValueError("名称和 token 不能为空")
    ref = _write_secret(token)
    try:
        return db.execute("INSERT INTO github_connections(label,token_ref,created_at,updated_at) VALUES(?,?,?,?)",
                          (label.strip(), ref, db.now(), db.now()))
    except Exception:
        _secret_path(ref).unlink(missing_ok=True)
        raise


def rotate(cid: int, token: str) -> None:
    row = db.get("SELECT token_ref FROM github_connections WHERE id=?", (cid,))
    if not row or not token.strip():
        raise ValueError("连接不存在或 token 为空")
    ref = _write_secret(token)
    db.execute("UPDATE github_connections SET token_ref=?,updated_at=?,account_login=NULL,last_checked_at=NULL,last_error=NULL WHERE id=?",
               (ref, db.now(), cid))
    if row["token_ref"].startswith("file:"):
        _secret_path(row["token_ref"]).unlink(missing_ok=True)


def token_for_id(cid: int | None) -> str | None:
    if cid is None:
        return None
    row = db.get("SELECT token_ref,disabled FROM github_connections WHERE id=?", (cid,))
    if not row or row["disabled"]:
        raise ValueError(f"GitHub 连接 #{cid} 不可用")
    ref = row["token_ref"]
    if ref == "env:GITHUB_TOKEN":
        token = config.GITHUB_TOKEN
    else:
        token = _secret_path(ref).read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"GitHub 连接 #{cid} 没有可用 token")
    return token


def token_for_project(project) -> str | None:
    return token_for_id(project["github_connection_id"])


def validate_id(cid: int | None) -> None:
    if cid is not None:
        token_for_id(cid)


def list_public() -> list[dict]:
    rows = db.query("SELECT c.id,c.label,c.disabled,c.account_login,c.last_checked_at,c.last_error,c.created_at,c.updated_at,"
                    "(SELECT COUNT(*) FROM projects p WHERE p.github_connection_id=c.id) AS project_count "
                    "FROM github_connections c ORDER BY c.id")
    return [dict(r) for r in rows]


def migrate_legacy() -> None:
    """旧单 token 升级一次：保留旧项目的权限行为，新项目默认匿名。"""
    if db.get("SELECT id FROM github_connections LIMIT 1"):
        return
    old = db.get_setting("github_token")
    if old:
        cid = create("旧版默认连接", old)
        db.set_setting("github_token", "")
    elif config.GITHUB_TOKEN:
        cid = db.execute("INSERT INTO github_connections(label,token_ref,created_at,updated_at) VALUES(?,?,?,?)",
                         ("环境变量 GITHUB_TOKEN", "env:GITHUB_TOKEN", db.now(), db.now()))
    else:
        return
    db.execute("UPDATE projects SET github_connection_id=? WHERE kind='github' AND github_connection_id IS NULL", (cid,))


def scrub_clone_remotes() -> None:
    """启动时清理旧版本在 .git/config 中留下的带 token origin。"""
    for project in db.query("SELECT id,full_name FROM projects WHERE kind='github'"):
        repo = config.CLONES_DIR / str(project["id"])
        if not (repo / ".git").exists():
            continue
        subprocess.run(["git", "remote", "set-url", "origin",
                        f"https://github.com/{project['full_name']}.git"],
                       cwd=repo, capture_output=True, text=True)
