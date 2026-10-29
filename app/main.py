"""Review Hub 控制枢纽 — FastAPI 主应用。"""
import hmac
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import config, connections, db, evaluator, github, worker
from .engine import api_nodes, gates, prompts, reviewers

app = FastAPI(title="Review Hub", docs_url=None, redoc_url=None)


@app.middleware("http")
async def protect_server(request: Request, call_next):
    """服务器模式最小访问控制：浏览器原生 Basic，Webhook 使用签名独立认证。"""
    if request.url.path == "/api/webhook/github":
        return await call_next(request)
    password = os.environ.get("HUB_ADMIN_PASSWORD", "")
    if not password:
        if config.PUBLIC_BASE_URL:
            return JSONResponse({"detail": "服务器模式必须配置 HUB_ADMIN_PASSWORD"}, status_code=503)
        return await call_next(request)
    import base64
    try:
        scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
        user, supplied = base64.b64decode(encoded).decode().split(":", 1) if scheme.lower() == "basic" else ("", "")
    except (ValueError, UnicodeError):
        user, supplied = "", ""
    expected_user = os.environ.get("HUB_ADMIN_USER", "admin")
    if not (hmac.compare_digest(user, expected_user) and hmac.compare_digest(supplied, password)):
        return JSONResponse({"detail": "需要管理员登录"}, status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Review Hub"'})
    return await call_next(request)


@app.on_event("startup")
def startup() -> None:
    if config.PUBLIC_BASE_URL and not config.PUBLIC_BASE_URL.startswith("https://"):
        raise RuntimeError("公网 PUBLIC_BASE_URL 必须使用 HTTPS")
    db.init()
    evaluator.recover_interrupted()
    connections.scrub_clone_remotes()
    worker.start_background()


# ---------------- 静态前端 ----------------

@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/static/{name}")
def static_file(name: str):
    p = Path(__file__).parent.parent / "static" / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ---------------- 设置 / 状态 ----------------

@app.get("/api/settings")
def settings():
    return {
        "credentials": config.credential_status(),
        "claude_cli": reviewers.claude_available(),
        "codex_cli": reviewers.codex_available(),
        "semgrep_cli": gates.semgrep_available(),
        "poll_interval": config.POLL_INTERVAL,
        "webhook_url": (config.PUBLIC_BASE_URL.rstrip("/") + "/api/webhook/github")
        if config.PUBLIC_BASE_URL else "",
    }


# ---------------- 配置中心：凭证 / 模型 / 提示词 / 流水线 ----------------

SECRET_KEYS = {"kimi_api_key", "claude_token"}
EDITABLE_KEYS = {"kimi_api_key", "claude_token",
                 "claude_model", "kimi_model", "kimi_base_url"}


def _mask(v: str) -> str:
    return f"已配置（…{v[-4:]}）" if v else ""


@app.get("/api/config")
def get_config():
    out = {}
    for k in EDITABLE_KEYS:
        v = db.get_setting(k)
        if v is None:   # 未在网站里改过 -> 显示 env 兜底的状态
            v = {"kimi_api_key": config.KIMI_API_KEY,
                 "claude_token": config.CLAUDE_CODE_OAUTH_TOKEN or config.ANTHROPIC_API_KEY,
                 "kimi_model": config.KIMI_MODEL,
                 "kimi_base_url": config.KIMI_BASE_URL,
                 "claude_model": ""}.get(k, "")
        out[k] = _mask(v) if (k in SECRET_KEYS and v) else (v or "")
        out[f"{k}_set"] = bool(v)
    return {"values": out}


class ConfigIn(BaseModel):
    values: dict


@app.put("/api/config")
def put_config(body: ConfigIn):
    changed = []
    for k, v in body.values.items():
        if k not in EDITABLE_KEYS:
            continue
        v = (v or "").strip()
        if k in SECRET_KEYS and (v.startswith("已配置") or v == "••••"):
            continue   # 前端把掩码原样传回来了 -> 不改动
        db.set_setting(k, v)
        changed.append(k)
    return {"ok": True, "changed": changed}


# GitHub 连接：API 只返回元数据，token 永不回显。
class ConnectionIn(BaseModel):
    label: str
    token: str


@app.get("/api/github-connections")
def list_connections():
    return connections.list_public()


@app.post("/api/github-connections")
def create_connection(body: ConnectionIn):
    try:
        cid = connections.create(body.label, body.token)
    except (ValueError, sqlite3.IntegrityError) as e:
        raise HTTPException(400, str(e))
    return {"id": cid}


@app.put("/api/github-connections/{cid}/token")
def rotate_connection(cid: int, body: dict):
    try:
        connections.rotate(cid, str(body.get("token") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.patch("/api/github-connections/{cid}")
def update_connection(cid: int, body: dict):
    if not db.get("SELECT id FROM github_connections WHERE id=?", (cid,)):
        raise HTTPException(404, "连接不存在")
    if "disabled" in body:
        db.execute("UPDATE github_connections SET disabled=?,updated_at=? WHERE id=?",
                   (int(bool(body["disabled"])), db.now(), cid))
    if "label" in body and str(body["label"]).strip():
        try:
            db.execute("UPDATE github_connections SET label=?,updated_at=? WHERE id=?",
                       (str(body["label"]).strip(), db.now(), cid))
        except sqlite3.IntegrityError as e:
            raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/github-connections/{cid}")
def delete_connection(cid: int):
    row = db.get("SELECT token_ref FROM github_connections WHERE id=?", (cid,))
    if not row:
        raise HTTPException(404, "连接不存在")
    if db.get("SELECT 1 FROM projects WHERE github_connection_id=? LIMIT 1", (cid,)):
        raise HTTPException(409, "请先解绑使用此连接的项目")
    db.execute("DELETE FROM github_connections WHERE id=?", (cid,))
    if row["token_ref"].startswith("file:"):
        connections._secret_path(row["token_ref"]).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/github-connections/{cid}/test")
def test_connection(cid: int):
    try:
        token = connections.token_for_id(cid)
        info = github.gh("GET", "/user", token=token).json()
    except (ValueError, RuntimeError) as e:
        if db.get("SELECT id FROM github_connections WHERE id=?", (cid,)):
            db.execute("UPDATE github_connections SET last_checked_at=?,last_error=? WHERE id=?",
                       (db.now(), str(e)[-180:], cid))
        raise HTTPException(400, str(e))
    db.execute("UPDATE github_connections SET account_login=?,last_checked_at=?,last_error=NULL WHERE id=?",
               (info.get("login", ""), db.now(), cid))
    return {"ok": True, "login": info.get("login", ""), "scopes": "仅确认 token 有效；仓库权限需按项目验证"}


@app.get("/api/pipeline")
def get_pipeline():
    return {"steps": worker.get_pipeline()}


@app.get("/api/model-providers")
def model_providers():
    return [{"id": key, "label": label, "base_url": url, "protocol": protocol}
            for key, (label, url, protocol) in api_nodes.PROVIDERS.items()]


@app.get("/api/model-credentials")
def model_credentials():
    return api_nodes.list_public()


@app.post("/api/model-credentials")
def create_model_credential(body: dict):
    try:
        cid = api_nodes.create(str(body.get("label") or ""), str(body.get("provider") or ""),
                               str(body.get("key") or ""), str(body.get("base_url") or ""))
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": cid}


@app.put("/api/model-credentials/{cid}/key")
def rotate_model_credential(cid: int, body: dict):
    try:
        api_nodes.rotate(cid, str(body.get("key") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.patch("/api/model-credentials/{cid}")
def update_model_credential(cid: int, body: dict):
    row = db.get("SELECT * FROM model_credentials WHERE id=?", (cid,))
    if not row:
        raise HTTPException(404, "模型凭证不存在")
    if "disabled" in body:
        db.execute("UPDATE model_credentials SET disabled=?,updated_at=? WHERE id=?",
                   (int(bool(body["disabled"])), db.now(), cid))
    return {"ok": True}


@app.delete("/api/model-credentials/{cid}")
def delete_model_credential(cid: int):
    for raw in [db.get_setting("pipeline"), *[p["pipeline_json"] for p in
                                                db.query("SELECT pipeline_json FROM projects")]]:
        try:
            if any(node.get("kind") == "api" and node.get("credential_id") == cid
                   for step in json.loads(raw or "[]")
                   for node in [step, *step.get("reviewers", [])]):
                raise HTTPException(409, "凭证仍被流水线节点引用，请先解绑")
        except (ValueError, TypeError):
            pass
    try:
        api_nodes.delete(cid)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


def _bounded_text(value, limit: int, label: str) -> str:
    text = str(value or "")
    if len(text) > limit:
        raise HTTPException(400, f"{label}超过 {limit} 字符；请缩短后保存，系统不会静默截断")
    return text


def _clean_model_node(s: dict, purpose: str, fallback: str) -> dict:
    kind = s.get("kind") or "reviewer"
    common = {
        "id": _bounded_text(s.get("id") or fallback, 40, "节点 ID"),
        "kind": kind,
        "name": _bounded_text(s.get("name") or "模型节点", 40, "节点名称"),
        "enabled": bool(s.get("enabled", True)),
        "model": _bounded_text(s.get("model"), 80, "模型 ID"),
        "prompt": _bounded_text(s.get("prompt"), 8000, "节点提示词"),
        "purpose": purpose,
    }
    if kind == "reviewer":
        provider = str(s.get("provider") or "claude")
        if provider not in ("claude", "codex"):
            raise HTTPException(400, "未知 Agent 执行器")
        return {**common, "provider": provider}
    if kind == "api":
        try:
            cid = int(s.get("credential_id"))
            api_nodes.get_active(cid)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"API 节点凭证无效：{exc}") from exc
        if not common["model"].strip():
            raise HTTPException(400, "API 节点必须填写模型 ID")
        return {**common, "credential_id": cid, "model": common["model"].strip()}
    raise HTTPException(400, "未知模型节点类型")


def _clean_pipeline(steps) -> list:
    """全局与项目共用：旧平铺配置读入后规范化，评审组必须是终点。"""
    if not isinstance(steps, list):
        raise HTTPException(400, "steps 必须是节点列表")
    steps = worker.normalize_pipeline(steps)
    if not 1 <= len(steps) <= 20:
        raise HTTPException(400, "steps 必须包含 1 至 20 个节点")
    clean = []
    for s in steps:
        if not isinstance(s, dict):
            raise HTTPException(400, "节点必须是对象")
        kind = s.get("kind") or "reviewer"
        if kind == "gate":
            g = str(s.get("gate") or "tests")
            if g not in ("tests", "build", "semgrep"):
                raise HTTPException(400, "未知门禁类型")
            clean.append({
                "id": _bounded_text(s.get("id") or f"gate{len(clean)}", 40, "门禁 ID"),
                "kind": "gate",
                "name": _bounded_text(s.get("name") or "门禁", 40, "门禁名称"),
                "gate": g,
                "enabled": bool(s.get("enabled", True)),
                "sandbox": "docker" if s.get("sandbox") == "docker" else "off",
                "network": s.get("network") if s.get("network") in ("off", "install", "on") else "install",
                "image": _bounded_text(s.get("image"), 80, "沙盒镜像"),
            })
        elif kind in ("reviewer", "api"):
            if s.get("purpose") != "context":
                raise HTTPException(400, "独立模型节点只能提供上下文；评审器请加入交叉评审组")
            clean.append(_clean_model_node(s, "context", f"context{len(clean)}"))
        elif kind == "review":
            members = s.get("reviewers")
            if not isinstance(members, list) or not 1 <= len(members) <= 20:
                raise HTTPException(400, "交叉评审组需要 1 至 20 个评审器")
            if any(not isinstance(m, dict) for m in members):
                raise HTTPException(400, "评审器必须是对象")
            clean.append({"id": _bounded_text(s.get("id") or "cross-review", 40, "评审组 ID"), "kind": "review",
                          "name": _bounded_text(s.get("name") or "交叉评审", 40, "评审组名称"),
                          "enabled": bool(s.get("enabled", True)),
                          "reviewers": [_clean_model_node(m, "findings", f"reviewer{i}")
                                        for i, m in enumerate(members)]})
        else:
            raise HTTPException(400, "未知节点类型")
    all_nodes = [node for step in clean for node in [step, *step.get("reviewers", [])]]
    if len(all_nodes) > 20:
        raise HTTPException(400, "整条流水线最多 20 个节点（含评审器）")
    if len({s["id"] for s in all_nodes}) != len(all_nodes):
        raise HTTPException(400, "节点 ID 不能重复")
    if len({s["name"] for s in all_nodes}) != len(all_nodes):
        raise HTTPException(400, "节点名称不能重复，报告和交叉裁决需要区分来源")
    groups = [s for s in clean if s["kind"] == "review"]
    if len(groups) != 1 or clean[-1] is not groups[0] or not groups[0]["enabled"]:
        raise HTTPException(400, "需要一个启用的交叉评审组，且必须放在最后")
    if not any(m["enabled"] for m in groups[0]["reviewers"]):
        raise HTTPException(400, "交叉评审组至少启用一个评审器")
    return clean


@app.put("/api/pipeline")
def put_pipeline(body: dict):
    clean = _clean_pipeline(body.get("steps"))
    _save_version("pipeline", json.dumps(clean, ensure_ascii=False), db.get_setting("pipeline"))
    db.set_setting("pipeline", json.dumps(clean, ensure_ascii=False))
    return {"ok": True, "steps": clean}


# ---------------- 项目级编排：覆盖全局默认，存 projects.pipeline_json ----------------

def _pipeline_inherited(pipeline_json) -> bool:
    if not pipeline_json:
        return True
    try:
        parsed = json.loads(pipeline_json)
        return not (isinstance(parsed, list) and parsed)
    except Exception:
        return True


@app.get("/api/projects/{pid}/pipeline")
def get_project_pipeline(pid: int):
    p = db.get("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "项目不存在")
    return {"inherited": _pipeline_inherited(p["pipeline_json"]), "steps": worker.get_pipeline(p)}


class ProjectPipelineIn(BaseModel):
    steps: list


@app.put("/api/projects/{pid}/pipeline")
def put_project_pipeline(pid: int, body: ProjectPipelineIn):
    if not db.get("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
    clean = _clean_pipeline(body.steps)
    value = json.dumps(clean, ensure_ascii=False)
    _save_version(f"project_pipeline:{pid}", value, db.get(
        "SELECT pipeline_json FROM projects WHERE id=?", (pid,))["pipeline_json"])
    db.execute("UPDATE projects SET pipeline_json=? WHERE id=?", (value, pid))
    return {"ok": True, "steps": clean}


@app.delete("/api/projects/{pid}/pipeline")
def delete_project_pipeline(pid: int):
    if not db.get("SELECT id FROM projects WHERE id=?", (pid,)):
        raise HTTPException(404, "项目不存在")
    db.execute("UPDATE projects SET pipeline_json=NULL WHERE id=?", (pid,))
    return {"ok": True}


@app.get("/api/prompt")
def get_prompt():
    rules = ""
    rules_path = config.POLICY_DIR / "REVIEW.md"
    if rules_path.exists():
        rules = rules_path.read_text(encoding="utf-8")
    return {"template": prompts.get_template(),
            "default_template": prompts.DEFAULT_TEMPLATE,
            "rules": rules}


class PromptIn(BaseModel):
    template: str = ""
    rules: str = ""


@app.put("/api/prompt")
def put_prompt(body: PromptIn):
    if body.template.strip():
        if "{diff_file}" not in body.template or "{review_md}" not in body.template:
            raise HTTPException(400, "提示词必须保留 {diff_file} 和 {review_md} 占位符（评审时注入 diff 和规则）")
        _save_version("prompt_template", body.template, db.get_setting("prompt_template"))
        db.set_setting("prompt_template", body.template)
    if body.rules.strip():
        rules_path = config.POLICY_DIR / "REVIEW.md"
        current = rules_path.read_text(encoding="utf-8") if rules_path.exists() else None
        _save_version("rules", body.rules, current)
        rules_path.write_text(body.rules, encoding="utf-8")
    return {"ok": True}


# ---------------- 项目 ----------------

class ProjectIn(BaseModel):
    kind: str = "github"          # github | local
    full_name: str                # owner/repo 或本地路径
    default_branch: str = "main"
    provider: str = "auto"
    auto_revert: bool = False
    enabled: bool = True
    github_connection_id: int | None = None


def _upsert_project(p: ProjectIn) -> int:
    if p.auto_revert:
        raise HTTPException(400, "自动回滚尚未接入默认分支 push 工作流；可在审查报告中手动创建回滚 PR")
    if p.kind == "github":
        connections.validate_id(p.github_connection_id)
    elif p.github_connection_id is not None:
        raise HTTPException(400, "本地项目不能绑定 GitHub 连接")
    full = p.full_name.strip().rstrip("/").removeprefix("https://github.com/").removesuffix(".git")
    if p.kind == "local":
        full = str(Path(full).expanduser().resolve())
        name = Path(full).name
    else:
        name = full.split("/")[-1]
    existing = db.get("SELECT id FROM projects WHERE full_name=?", (full,))
    if existing:
        db.execute("UPDATE projects SET enabled=?, default_branch=?, provider=?, auto_revert=?,github_connection_id=? WHERE id=?",
                   (int(p.enabled), p.default_branch, p.provider, int(p.auto_revert),
                    p.github_connection_id, existing["id"]))
        return existing["id"]
    # GitHub 项目：记录当前 HEAD 作为基线（不审历史），并入初始化队列生成项目画像
    init_status = "pending" if p.kind == "github" else "done"
    return db.execute(
        "INSERT INTO projects(kind,full_name,name,default_branch,enabled,provider,auto_revert,"
        "init_status,last_sha,created_at,github_connection_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (p.kind, full, name, p.default_branch, int(p.enabled), p.provider, int(p.auto_revert),
         init_status, "", db.now(), p.github_connection_id))


@app.get("/api/projects")
def list_projects():
    rows = db.query("SELECT * FROM projects ORDER BY id")
    out = []
    for r in rows:
        d = dict(r)
        last = db.get("SELECT status, important_count, id FROM runs WHERE project_id=? ORDER BY id DESC LIMIT 1",
                      (r["id"],))
        d["last_run"] = dict(last) if last else None
        out.append(d)
    return out


@app.post("/api/projects")
def add_project(p: ProjectIn):
    if p.kind == "github":
        if "/" not in p.full_name:
            raise HTTPException(400, "GitHub 项目请填 owner/repo")
        try:
            p.default_branch = github.default_branch(p.full_name.strip(), token=connections.token_for_id(p.github_connection_id))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            raise HTTPException(400, str(e))
    pid = _upsert_project(p)
    return {"id": pid, "msg": "已接入：正在生成项目画像，之后有新 push 才会审查"}


class ImportOrg(BaseModel):
    org: str
    enabled: bool = True
    github_connection_id: int | None = None


@app.post("/api/projects/import_org")
def import_org(body: ImportOrg):
    try:
        repos = github.list_org_repos(body.org, token=connections.token_for_id(body.github_connection_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except github.NeedToken as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    added = 0
    for r in repos:
        if r.get("archived") or r.get("fork"):
            continue
        full = r["full_name"]
        if not db.get("SELECT id FROM projects WHERE full_name=?", (full,)):
            db.execute(
                "INSERT INTO projects(kind,full_name,name,default_branch,enabled,provider,auto_revert,"
                "init_status,last_sha,created_at,github_connection_id) VALUES('github',?,?,?,?,?,0,'pending','',?,?)",
                (full, r["name"], r.get("default_branch", "main"),
                 int(body.enabled), "auto", db.now(), body.github_connection_id))
            added += 1
    return {"imported": added, "total_in_org": len(repos),
            "msg": "已导入：正在为每个仓库生成项目画像，之后有新 push 才会审查"}


@app.patch("/api/projects/{pid}")
def update_project(pid: int, body: dict):
    current = db.get("SELECT * FROM projects WHERE id=?", (pid,))
    if not current:
        raise HTTPException(404, "项目不存在")
    effective_cid = body.get("github_connection_id", current["github_connection_id"])
    if body.get("auto_revert"):
        raise HTTPException(400, "自动回滚尚未接入默认分支 push 工作流；可在审查报告中手动创建回滚 PR")
    if current["kind"] == "local" and effective_cid is not None:
        raise HTTPException(400, "本地项目不能绑定 GitHub 连接")
    if "github_connection_id" in body and current["kind"] == "github":
        try:
            github.repo_info(current["full_name"], token=connections.token_for_id(effective_cid))
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, f"连接无法访问该仓库：{e}")
    fields, args = [], []
    for key in ("enabled", "provider", "auto_revert", "default_branch", "profile", "github_connection_id"):
        if key in body:
            v = body[key]
            if key in ("enabled", "auto_revert"):
                v = int(bool(v))
            if key == "github_connection_id":
                try:
                    connections.validate_id(v)
                except ValueError as e:
                    raise HTTPException(400, str(e))
            fields.append(f"{key}=?")
            args.append(v)
    if not fields:
        raise HTTPException(400, "没有可更新字段")
    args.append(pid)
    db.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id=?", tuple(args))
    return {"ok": True}


@app.post("/api/projects/{pid}/init")
def reinit_project(pid: int):
    p = db.get("SELECT * FROM projects WHERE id=?", (pid,))
    if not p or p["kind"] != "github":
        raise HTTPException(400, "只有 GitHub 项目支持自动初始化")
    db.execute("UPDATE projects SET init_status='pending' WHERE id=?", (pid,))
    return {"ok": True, "msg": "已加入初始化队列"}


@app.delete("/api/projects/{pid}")
def delete_project(pid: int):
    db.execute("DELETE FROM finding_marks WHERE run_id IN (SELECT id FROM runs WHERE project_id=?)", (pid,))
    db.execute("DELETE FROM run_failure_events WHERE run_id IN (SELECT id FROM runs WHERE project_id=?)", (pid,))
    db.execute("DELETE FROM runs WHERE project_id=?", (pid,))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    return {"ok": True}


@app.post("/api/projects/{pid}/attach_webhook")
def attach_webhook(pid: int):
    if not config.PUBLIC_BASE_URL:
        raise HTTPException(400, "未配置 PUBLIC_BASE_URL（本机部署可用轮询模式代替，或用 cloudflared/ngrok 提供公网地址）")
    if not config.WEBHOOK_SECRET:
        raise HTTPException(400, "公网 webhook 必须配置 WEBHOOK_SECRET")
    p = db.get("SELECT * FROM projects WHERE id=?", (pid,))
    if not p or p["kind"] != "github":
        raise HTTPException(404, "项目不存在或不是 GitHub 项目")
    try:
        github.attach_webhook(p["full_name"], config.PUBLIC_BASE_URL, token=connections.token_for_project(p))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except github.NeedToken as e:
        raise HTTPException(400, str(e))
    db.execute("UPDATE projects SET webhook_attached=1 WHERE id=?", (pid,))
    return {"ok": True, "url": config.PUBLIC_BASE_URL.rstrip("/") + "/api/webhook/github"}


# ---------------- Runs ----------------

@app.get("/api/marks")
def list_marks(disposition: str = "", project_id: int | None = None, limit: int = 100):
    """处置页数据源：待处置队列（blocked run 里未标记的 Important）+ 处置台账 + 统计。
    机械门禁发现是客观结果，不进队列、不需要人工处置。"""
    if disposition not in ("", "confirmed", "false_positive"):
        disposition = ""

    # 待处置：统计所有 blocked run；只返回前 50 条作工作台预览，不能把预览数冒充总数。
    conds, args = ["r.status='blocked'", "r.findings_json IS NOT NULL"], []
    if project_id:
        conds.append("p.id=?")
        args.append(project_id)
    pending, pending_count, cursor = [], 0, None
    while True:
        page_conds = conds + (["r.id<?"] if cursor is not None else [])
        page_args = args + ([cursor] if cursor is not None else [])
        rows = db.query(
            "SELECT r.id AS run_id, r.project_id, r.created_at, r.findings_json, p.name AS project"
            " FROM runs r JOIN projects p ON p.id=r.project_id WHERE " + " AND ".join(page_conds) +
            " ORDER BY r.id DESC LIMIT 200", tuple(page_args))
        if not rows:
            break
        ids = [r["run_id"] for r in rows]
        marked = {(m["run_id"], m["idx"]) for m in db.query(
            "SELECT run_id,idx FROM finding_marks WHERE run_id IN (" + ",".join("?" for _ in ids) + ")",
            tuple(ids))}
        for r in rows:
            try:
                findings = json.loads(r["findings_json"])
            except (TypeError, ValueError):
                raise RuntimeError(f"审查 #{r['run_id']} 的发现数据无法解析，待办数量不可判定")
            if not isinstance(findings, list) or any(not isinstance(f, dict) for f in findings):
                raise RuntimeError(f"审查 #{r['run_id']} 的发现数据格式错误，待办数量不可判定")
            for i, f in enumerate(findings):
                if f.get("severity") != "IMPORTANT" or (r["run_id"], i) in marked:
                    continue
                if any(str(s).startswith("gate:") for s in f.get("sources") or []):
                    continue
                pending_count += 1
                if len(pending) < 50:
                    pending.append({
                        "run_id": r["run_id"], "idx": i, "project": r["project"],
                        "project_id": r["project_id"], "created_at": r["created_at"],
                        "file": f.get("file", ""), "line": f.get("line", 0),
                        "title": f.get("title", ""),
                    })
        cursor = rows[-1]["run_id"]
        if len(rows) < 200:
            break

    # 台账：已处置记录，可按处置类型 / 项目过滤
    conds, args = [], []
    if disposition:
        conds.append("m.disposition=?")
        args.append(disposition)
    if project_id:
        conds.append("p.id=?")
        args.append(project_id)
    w = (" WHERE " + " AND ".join(conds)) if conds else ""
    marks = []
    marks_total = db.get(
        "SELECT COUNT(*) c FROM finding_marks m"
        " JOIN runs r ON r.id=m.run_id JOIN projects p ON p.id=r.project_id" + w,
        tuple(args))["c"]
    for m in db.query(
            "SELECT m.run_id, m.idx, m.disposition, m.updated_at, p.name AS project, p.id AS project_id,"
            " r.findings_json FROM finding_marks m"
            " JOIN runs r ON r.id=m.run_id JOIN projects p ON p.id=r.project_id" + w +
            " ORDER BY m.updated_at DESC, m.run_id DESC, m.idx DESC LIMIT ?", (*args, min(limit, 200))):
        try:
            f = json.loads(m["findings_json"])[m["idx"]]
        except (TypeError, ValueError, IndexError, KeyError) as exc:
            raise RuntimeError(f"审查 #{m['run_id']} 的处置记录找不到原始发现，不能展示空记录") from exc
        marks.append({
            "run_id": m["run_id"], "idx": m["idx"],
            "project": m["project"], "project_id": m["project_id"],
            "disposition": m["disposition"], "updated_at": m["updated_at"],
            "file": f.get("file", ""), "line": f.get("line", 0), "title": f.get("title", ""),
        })

    # 统计（台账口径；项目过滤生效，处置类型过滤不影响统计）
    if project_id:
        st = db.get("SELECT COUNT(*) total,"
                    " COALESCE(SUM(m.disposition='false_positive'),0) fp,"
                    " COALESCE(SUM(m.disposition='confirmed'),0) cf"
                    " FROM finding_marks m JOIN runs r ON r.id=m.run_id WHERE r.project_id=?",
                    (project_id,))
    else:
        st = db.get("SELECT COUNT(*) total,"
                    " COALESCE(SUM(disposition='false_positive'),0) fp,"
                    " COALESCE(SUM(disposition='confirmed'),0) cf FROM finding_marks")
    return {
        "stats": {"pending": pending_count, "total": st["total"],
                  "false_positive": st["fp"], "confirmed": st["cf"]},
        "pending": pending,
        "pending_preview_limit": 50,
        "marks_total": marks_total,
        "marks_has_more": marks_total > len(marks),
        "marks": marks,
    }


@app.get("/api/overview")
def overview():
    stats = {
        "projects": db.get("SELECT COUNT(*) c FROM projects")["c"],
        "runs_today": db.get(
            "SELECT COUNT(*) c FROM runs WHERE created_at >= ?", (_local_day_start_utc(),))["c"],
        "blocked_total": db.get("SELECT COUNT(*) c FROM runs WHERE status='blocked'")["c"],
        "reverted_total": db.get("SELECT COUNT(*) c FROM runs WHERE revert_pr_url IS NOT NULL")["c"],
        "marked_total": db.get("SELECT COUNT(*) c FROM finding_marks")["c"],
        "fp_total": db.get("SELECT COUNT(*) c FROM finding_marks WHERE disposition='false_positive'")["c"],
    }
    recent = db.query(
        "SELECT r.id, r.status, r.trigger, r.tier, r.important_count, r.created_at, r.duration_ms,"
        " r.branch, r.head_sha, r.commit_msg, p.name AS project, p.id AS project_id"
        " FROM runs r JOIN projects p ON p.id=r.project_id ORDER BY r.id DESC LIMIT 20")
    return {"stats": stats, "recent": [dict(r) for r in recent]}


@app.get("/api/metrics")
def metrics():
    """按周聚合：审查量 / 问题 / 已创建回滚 PR / 人工处置。"""
    cutoff = _db_utc_timestamp(datetime.now(timezone.utc) - timedelta(days=56))
    runs_rows = db.query(
        "SELECT strftime('%Y-%W', created_at) w, MIN(date(created_at)) start, COUNT(*) runs,"
        " SUM(status='blocked') blocked, SUM(revert_pr_url IS NOT NULL) reverted,"
        " SUM(trigger='pr') pr_runs FROM runs"
        " WHERE created_at >= ? GROUP BY w ORDER BY w", (cutoff,))
    mark_rows = db.query(
        "SELECT strftime('%Y-%W', updated_at) w, COUNT(*) marked,"
        " SUM(disposition='false_positive') fp FROM finding_marks"
        " WHERE updated_at >= ? GROUP BY w", (cutoff,))
    marks_by_week = {r["w"]: {"marked": r["marked"] or 0, "fp": r["fp"] or 0} for r in mark_rows}
    weeks = []
    for r in runs_rows:
        m = marks_by_week.get(r["w"], {"marked": 0, "fp": 0})
        weeks.append({
            "week": r["w"], "start": r["start"],
            "runs": r["runs"] or 0, "blocked": r["blocked"] or 0,
            "reverted": r["reverted"] or 0, "pr_runs": r["pr_runs"] or 0,
            "marked": m["marked"], "fp": m["fp"],
            "fp_rate": round(m["fp"] / max(m["marked"], 1) * 100),
        })
    weeks.reverse()   # 最新一周在前
    cases_pos = db.get("SELECT COUNT(*) c FROM eval_cases WHERE polarity='positive'")["c"]
    cases_neg = db.get("SELECT COUNT(*) c FROM eval_cases WHERE polarity='negative'")["c"]
    last_eval = db.get("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 1")
    return {"weeks": weeks, "eval": {
        "cases": cases_pos + cases_neg, "positive": cases_pos, "negative": cases_neg,
        "last": (dict(last_eval) if last_eval else None)}}


@app.get("/api/runs")
def list_runs(project_id: int | None = None, limit: int = 50,
              status: str = "", trigger: str = "", reverted: bool = False,
              since: str = "", unhandled: bool = False):
    """审查列表，支持指标页/总览下钻：状态 / 触发方式 / 仅回滚 / 起始日期(YYYY-MM-DD)。"""
    conds, args = [], []
    if project_id:
        conds.append("r.project_id=?")
        args.append(project_id)
    if status in ("queued", "running", "passed", "blocked", "failed"):
        conds.append("r.status=?")
        args.append(status)
    if trigger in ("manual", "push", "pr", "merged"):
        conds.append("r.trigger=?")
        args.append(trigger)
    if reverted:
        conds.append("r.revert_pr_url IS NOT NULL")
    if since:
        conds.append("r.created_at >= ?")
        args.append(since)
    if unhandled:
        conds.append("r.status='failed' AND COALESCE((SELECT e.action FROM run_failure_events e "
                     "WHERE e.run_id=r.id ORDER BY e.id DESC LIMIT 1),'') <> 'handled'")
    w = (" WHERE " + " AND ".join(conds)) if conds else ""
    rows = db.query(
        "SELECT r.*, p.name AS project, "
        "(SELECT e.action FROM run_failure_events e WHERE e.run_id=r.id ORDER BY e.id DESC LIMIT 1) AS failure_action, "
        "(SELECT e.note FROM run_failure_events e WHERE e.run_id=r.id ORDER BY e.id DESC LIMIT 1) AS failure_note, "
        "(SELECT e.created_at FROM run_failure_events e WHERE e.run_id=r.id ORDER BY e.id DESC LIMIT 1) AS failure_at "
        "FROM runs r JOIN projects p ON p.id=r.project_id" + w +
        " ORDER BY r.id DESC LIMIT ?", (*args, min(limit, 200)))
    return [dict(r) for r in rows]


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    run = db.get("SELECT r.*, p.name AS project, p.kind AS project_kind, p.full_name AS repo"
                 " FROM runs r JOIN projects p ON p.id=r.project_id WHERE r.id=?", (run_id,))
    if not run:
        raise HTTPException(404)
    steps = db.query("SELECT * FROM steps WHERE run_id=? ORDER BY idx", (run_id,))
    d = dict(run)
    d["steps"] = [dict(s) for s in steps]
    d["findings"] = json.loads(run["findings_json"]) if run["findings_json"] else []
    d["has_diff"] = bool(run["diff_text"])
    d.pop("diff_text", None)   # diff 走独立端点，避免报告轮询背负大文件
    failure_events = [dict(e) for e in db.query(
        "SELECT action,note,created_at FROM run_failure_events WHERE run_id=? ORDER BY id DESC", (run_id,))]
    d["failure_history"] = failure_events
    d["failure_action"] = failure_events[0]["action"] if failure_events else ""
    d["failure_note"] = failure_events[0]["note"] if failure_events else ""
    d["failure_at"] = failure_events[0]["created_at"] if failure_events else None
    marks = {r["idx"]: r["disposition"] for r in
             db.query("SELECT idx, disposition FROM finding_marks WHERE run_id=?", (run_id,))}
    for i, f in enumerate(d["findings"]):
        f["disposition"] = marks.get(i, "")
    return d


class FailureTriageIn(BaseModel):
    action: str
    note: str = ""


@app.post("/api/runs/{run_id}/failure-triage")
def triage_failed_run(run_id: int, body: FailureTriageIn):
    run = db.get("SELECT id,status FROM runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "审查不存在")
    if run["status"] != "failed":
        raise HTTPException(409, "只有执行失败的审查可以标记处理状态")
    if body.action not in ("handled", "reopen"):
        raise HTTPException(400, "action 只能是 handled 或 reopen")
    note = _bounded_text(body.note.strip(), 500, "处理备注")
    latest = db.get("SELECT action,note FROM run_failure_events WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,))
    if latest and latest["action"] == body.action and latest["note"] == note:
        return {"ok": True, "action": body.action, "unchanged": True}
    db.execute("INSERT INTO run_failure_events(run_id,action,note,created_at) VALUES(?,?,?,?)",
               (run_id, body.action, note, db.now()))
    return {"ok": True, "action": body.action, "unchanged": False}


@app.get("/api/runs/{run_id}/diff")
def run_diff(run_id: int):
    run = db.get("SELECT diff_text,diff_complete FROM runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404)
    return {"diff": run["diff_text"] or "", "complete": run["diff_complete"] == 1}


class MarkIn(BaseModel):
    disposition: str          # confirmed | false_positive | ""（清空标记）
    note: str = ""


@app.post("/api/runs/{run_id}/findings/{idx}/mark")
def mark_finding(run_id: int, idx: int, body: MarkIn):
    """人工处置一条发现：确认（真问题）/ 误报。这是噪声测量的数据源头。"""
    run = db.get("SELECT id FROM runs WHERE id=?", (run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    total = len(json.loads(db.get("SELECT findings_json FROM runs WHERE id=?", (run_id,))["findings_json"] or "[]"))
    if idx < 0 or idx >= total:
        raise HTTPException(400, f"发现下标越界（共 {total} 条）")
    if body.disposition not in ("confirmed", "false_positive", ""):
        raise HTTPException(400, "disposition 只能是 confirmed / false_positive / 空串")
    if body.disposition == "":
        db.execute("DELETE FROM finding_marks WHERE run_id=? AND idx=?", (run_id, idx))
    else:
        db.execute(
            "INSERT INTO finding_marks(run_id,idx,disposition,note,updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(run_id,idx) DO UPDATE SET disposition=excluded.disposition,"
            " note=excluded.note, updated_at=excluded.updated_at",
            (run_id, idx, body.disposition, _bounded_text(body.note.strip(), 500, "处置备注"), db.now()))
    return {"ok": True, "disposition": body.disposition}


# ---------------- 评测集：改提示词/换模型前先过回归 ----------------

class EvalCaseIn(BaseModel):
    run_id: int
    idx: int


@app.post("/api/eval/cases")
def add_eval_case(body: EvalCaseIn):
    """把一条已处置的 Important 存为评测用例：确认 -> 正例（应报出）；误报 -> 负例（不应再报出）。"""
    run = db.get("SELECT r.*, p.kind AS project_kind FROM runs r JOIN projects p ON p.id=r.project_id"
                 " WHERE r.id=?", (body.run_id,))
    if not run:
        raise HTTPException(404, "run 不存在")
    if run["project_kind"] != "github":
        raise HTTPException(400, "评测用例暂只支持 GitHub 项目（跑分需要按 sha 独立检出）")
    if not run["head_sha"]:
        raise HTTPException(400, "该 run 没有提交号（可能没跑到变更采集就失败），无法作为评测用例")
    try:
        findings = json.loads(run["findings_json"] or "[]")
        f = findings[body.idx]
    except (IndexError, TypeError, json.JSONDecodeError):
        raise HTTPException(400, "发现下标无效")
    if f.get("severity") != "IMPORTANT":
        raise HTTPException(400, "只有 Important 发现可以存为评测用例")
    if any(str(s).startswith("gate:") for s in f.get("sources") or []):
        raise HTTPException(400, "门禁发现是机械结果，不进评测集")
    mark = db.get("SELECT disposition FROM finding_marks WHERE run_id=? AND idx=?",
                  (body.run_id, body.idx))
    if not mark:
        raise HTTPException(400, "请先在处置里标记这条发现（确认=正例，误报=负例）")
    polarity = "positive" if mark["disposition"] == "confirmed" else "negative"
    existing = db.get("SELECT id FROM eval_cases WHERE run_id=? AND idx=?", (body.run_id, body.idx))
    if existing:
        return {"ok": True, "id": existing["id"], "existed": True}
    cid = db.execute(
        "INSERT INTO eval_cases(project_id,run_id,idx,polarity,head_sha,intent,expected,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (run["project_id"], body.run_id, body.idx, polarity, run["head_sha"],
         run["intent"] or run["commit_msg"] or "",
         json.dumps({"file": f.get("file"), "line": f.get("line"), "title": f.get("title")},
                    ensure_ascii=False),
         db.now()))
    return {"ok": True, "id": cid, "existed": False, "polarity": polarity}


@app.get("/api/eval/last")
def eval_last():
    """评测页数据源：用例列表 + 跑分历史（含逐用例结果与当时配置快照）+ 当前配置指纹。"""
    cases = []
    results = {}
    history_rows = db.query("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 10")
    if history_rows:
        try:
            for r in json.loads(history_rows[0]["results"] or "[]"):
                results[r.get("case_id")] = r.get("result")
        except Exception:
            pass
    for c in db.query(
            "SELECT c.*, p.full_name AS repo FROM eval_cases c"
            " JOIN projects p ON p.id=c.project_id ORDER BY c.id DESC"):
        cases.append({
            "id": c["id"], "run_id": c["run_id"], "repo": c["repo"],
            "sha": (c["head_sha"] or "")[:8],
            "polarity": c["polarity"], "expected": json.loads(c["expected"]).get("title", ""),
            "last_result": results.get(c["id"], ""),
        })
    history = []
    for r in history_rows:
        try:
            cfg = json.loads(r["config_snapshot"]) if r["config_snapshot"] else {}
        except Exception:
            cfg = {}
        try:
            res = json.loads(r["results"]) if r["results"] else []
        except Exception:
            res = []
        try:
            live_trace = json.loads(r["current_trace"] or "[]")
        except Exception:
            live_trace = []
        history.append({
            "id": r["id"], "created_at": r["created_at"], "status": r["status"],
            "cases_total": r["cases_total"], "hit": r["hit"], "miss": r["miss"],
            "fp": r["fp"], "error": r["error"], "config": cfg, "results": res,
            "error_msg": r["error_msg"],
            "current_case_id": r["current_case_id"],
            "current_stage": r["current_stage"],
            "current_detail": r["current_detail"],
            "current_trace": live_trace,
        })
    return {"cases": cases, "history": history,
            "current_config": json.loads(evaluator._config_snapshot())}


@app.delete("/api/eval/cases/{cid}")
def del_eval_case(cid: int):
    db.execute("DELETE FROM eval_cases WHERE id=?", (cid,))
    return {"ok": True}


@app.post("/api/eval/run")
def eval_run():
    return evaluator.start_eval()


# ---------------- PR 审批状态：人类批准才算数 ----------------

_APPROVAL_TTL = 300   # 秒；GitHub 拉一次有成本，报告页 5 分钟内用缓存


def _db_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _local_day_start_utc() -> str:
    midnight = datetime.combine(datetime.now().date(), datetime.min.time())
    return _db_utc_timestamp(midnight.astimezone(timezone.utc))


def _approval_cache_fresh(approval_at: str | None, now: datetime | None = None) -> bool:
    if not approval_at:
        return False
    try:
        stamp = datetime.fromisoformat(approval_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if stamp.tzinfo is None:
        return False
    age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
    return 0 <= age < _APPROVAL_TTL


@app.get("/api/runs/{run_id}/approvals")
def run_approvals(run_id: int):
    run = db.get("SELECT r.*, p.kind AS project_kind, p.full_name AS repo FROM runs r"
                 " JOIN projects p ON p.id=r.project_id WHERE r.id=?", (run_id,))
    if not run:
        raise HTTPException(404)
    if not run["pr_number"]:
        return {"applicable": False}
    if run["project_kind"] != "github":
        return {"applicable": False}
    cached = None
    if run["approval_json"]:
        try:
            cached = json.loads(run["approval_json"])
        except Exception:
            cached = None
    fresh = bool(cached) and _approval_cache_fresh(run["approval_at"])
    if fresh:
        data = cached
    else:
        try:
            project = db.get("SELECT * FROM projects WHERE id=?", (run["project_id"],))
            data = github.classify_reviews(github.get_reviews(
                run["repo"], run["pr_number"], token=connections.token_for_project(project)))
        except github.NeedToken as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"审批状态拉取失败：{str(e)[-200:]}")
        db.execute("UPDATE runs SET approval_json=?, approval_at=? WHERE id=?",
                   (json.dumps(data, ensure_ascii=False), db.now(), run_id))
    need = run["tier"] in ("medium", "high") and not data.get("human_approved")
    return {"applicable": True, "tier": run["tier"], "need_approval": need, **data}


class RunIn(BaseModel):
    project_id: int
    ref: str = ""
    base_ref: str = ""
    head_sha: str = ""     # 指定提交可审历史中的任意一次；留空 = 默认分支 HEAD


@app.post("/api/runs")
def trigger_run(body: RunIn):
    p = db.get("SELECT * FROM projects WHERE id=?", (body.project_id,))
    if not p:
        raise HTTPException(404, "项目不存在")
    if not p["enabled"]:
        raise HTTPException(400, "项目已停用，请先启用")
    run_id = worker.enqueue(p["id"], "manual", ref=body.ref or p["default_branch"],
                            base_ref=body.base_ref, head_sha=body.head_sha, force=True)
    if run_id is None:
        return {"queued": False, "msg": "已有相同 run 在进行中"}
    return {"queued": True, "run_id": run_id}


@app.get("/api/projects/{pid}/commits")
def project_commits(pid: int, limit: int = 30):
    """默认分支的提交历史（服务端缓存，秒开），附每个提交的审查状态。"""
    p = db.get("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "项目不存在")
    try:
        commits, error = worker.get_commits(p, limit)
    except github.NeedToken as e:
        raise HTTPException(400, str(e))
    # 关联审查状态：同 sha 取最新一次 run（本地查询，快）
    by_sha = {}
    for r in db.query("SELECT * FROM runs WHERE project_id=? ORDER BY id DESC", (pid,)):
        if r["head_sha"] and r["head_sha"] not in by_sha:
            by_sha[r["head_sha"]] = r
    for cmt in commits:
        rr = by_sha.get(cmt["sha"])
        cmt["run"] = ({"id": rr["id"], "status": rr["status"],
                       "important_count": rr["important_count"],
                       "trigger": rr["trigger"]} if rr else None)
    return {"commits": commits, "error": error}


@app.post("/api/runs/{run_id}/revert")
def revert_run(run_id: int):
    run = db.get("SELECT r.*, p.kind AS project_kind, p.full_name AS repo FROM runs r"
                 " JOIN projects p ON p.id=r.project_id WHERE r.id=?", (run_id,))
    if not run:
        raise HTTPException(404)
    if not run["head_sha"]:
        raise HTTPException(400, "该 run 没有 head_sha（本地演示项目请在仓库内手动 git revert）")
    if run["project_kind"] != "github":
        raise HTTPException(400, "本地项目回滚请直接在仓库执行 git revert")
    try:
        url = github.create_revert_pr(
            run["repo"], run["head_sha"], run["trigger"] == "merged",
            f"run #{run_id} 评审发现 {run['important_count']} 个 Important",
            token=connections.token_for_project(db.get("SELECT * FROM projects WHERE id=?", (run["project_id"],))))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except github.NeedToken as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    db.execute("UPDATE runs SET revert_pr_url=? WHERE id=?", (url, run_id))
    return {"ok": True, "pr_url": url}


# ---------------- 系统健康 / 配置版本历史 ----------------

@app.get("/api/health")
def health():
    q = db.get("SELECT SUM(status='queued') q, SUM(status='running') r FROM runs")
    cutoff = _db_utc_timestamp(datetime.now(timezone.utc) - timedelta(days=7))
    tot = db.get("SELECT COUNT(*) t FROM runs WHERE created_at >= ?", (cutoff,))
    fail = db.get("SELECT COUNT(*) f FROM runs WHERE status='failed' AND created_at >= ?", (cutoff,))
    return {"queued": q["q"] or 0, "running": q["r"] or 0,
            "workers": worker.active_worker_count(),
            "poll": worker.poll_status(),
            "fail_7d": fail["f"] or 0, "total_7d": tot["t"] or 0}


def _save_version(key: str, value: str, current) -> None:
    """current 为当前生效值；未变化不记版本。每 key 保留最近 20 份。"""
    if current is not None and value == current:
        return
    db.execute("INSERT INTO config_versions(key,value,created_at) VALUES(?,?,?)", (key, value, db.now()))
    ids = [r["id"] for r in db.query(
        "SELECT id FROM config_versions WHERE key=? ORDER BY id DESC", (key,))]
    for old in ids[20:]:
        db.execute("DELETE FROM config_versions WHERE id=?", (old,))


@app.get("/api/config/versions")
def list_config_versions(key: str):
    rows = db.query("SELECT id, value, created_at FROM config_versions WHERE key=?"
                    " ORDER BY id DESC LIMIT 20", (key,))
    out = []
    for r in rows:
        try:
            steps = json.loads(r["value"])
            summary = " → ".join(str(s.get("name") or s.get("gate") or "?") for s in steps) \
                if isinstance(steps, list) else str(steps)[:80]
        except Exception:
            summary = " ".join(r["value"].split())[:80]
        out.append({"id": r["id"], "created_at": r["created_at"], "summary": summary})
    return {"versions": out}


@app.post("/api/config/versions/{vid}/restore")
def restore_config_version(vid: int):
    v = db.get("SELECT * FROM config_versions WHERE id=?", (vid,))
    if not v:
        raise HTTPException(404, "版本不存在")
    key, value = v["key"], v["value"]
    if key == "prompt_template":
        if "{diff_file}" not in value or "{review_md}" not in value:
            raise HTTPException(400, "该版本缺少必需占位符，不能恢复")
        db.set_setting("prompt_template", value)
    elif key == "rules":
        (config.POLICY_DIR / "REVIEW.md").write_text(value, encoding="utf-8")
    elif key == "pipeline":
        db.set_setting("pipeline", json.dumps(_clean_pipeline(json.loads(value)), ensure_ascii=False))
    elif key.startswith("project_pipeline:"):
        pid = key.split(":", 1)[1]
        if not db.get("SELECT id FROM projects WHERE id=?", (pid,)):
            raise HTTPException(400, "项目已不存在")
        db.execute("UPDATE projects SET pipeline_json=? WHERE id=?",
                   (json.dumps(_clean_pipeline(json.loads(value)), ensure_ascii=False), pid))
    else:
        raise HTTPException(400, "未知配置类型")
    return {"ok": True, "key": key}


# ---------------- GitHub Webhook ----------------

@app.post("/api/webhook/github")
async def github_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("x-hub-signature-256", "")
    if not github.verify_signature(payload, sig):
        return JSONResponse({"ok": False}, status_code=401)
    event = request.headers.get("x-github-event", "")
    data = json.loads(payload or b"{}")
    if event == "ping":
        return {"ok": True}
    full = (data.get("repository") or {}).get("full_name", "")
    p = db.get("SELECT * FROM projects WHERE full_name=? AND enabled=1", (full,))
    if not p:
        return {"ok": True, "ignored": f"{full} 未注册或未启用"}
    if event == "push":
        ref = (data.get("ref") or "").removeprefix("refs/heads/")
        if ref == p["default_branch"] and not data.get("deleted"):
            after = data.get("after", "")
            before = data.get("before", "")
            valid = lambda sha: isinstance(sha, str) and re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", sha)
            if valid(after) and valid(before) and after != p["last_sha"] \
                    and (not p["last_sha"] or before == p["last_sha"]):
                base = "" if set(before) == {"0"} else before
                worker.enqueue(p["id"], "push", ref=ref, base_ref=base, head_sha=after)
                db.execute("UPDATE projects SET last_sha=? WHERE id=?", (after, p["id"]))
    # PR 打开、更新、关闭均不触发：合并进入默认分支会产生 push 事件。
    return {"ok": True}
