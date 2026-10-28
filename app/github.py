"""GitHub REST 客户端。无 token 时所有函数抛 NeedToken，由调用方降级。"""
import base64
import hmac
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from . import config

API = "https://api.github.com"
COMMENT_MARKER = "<!-- review-hub/ai-review -->"


class NeedToken(RuntimeError):
    pass


def _headers(token: str | None = None) -> dict:
    return {
        **({"Authorization": f"Bearer {token}"} if token else {}),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh(method: str, path: str, *, token: str | None = None, **kw):
    if method.upper() != "GET" and not token:
        raise NeedToken("该仓库未绑定可用的 GitHub 连接")
    headers = {**_headers(token), **kw.pop("headers", {})}
    last_exc = None
    for i in range(3):  # 直连 github 的 SSL 抖动是常态：传输层错误退避重试
        if i:
            time.sleep((1, 3)[min(i - 1, 1)])
        try:
            r = httpx.request(method, f"{API}{path}", headers=headers, timeout=30, **kw)
            break
        except httpx.TransportError as e:
            last_exc = e
    else:
        raise RuntimeError(f"GitHub {method} {path} 网络失败（已重试）：{last_exc}")
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r


# ---------- 仓库 / 组织 ----------

def list_org_repos(org: str, *, token: str | None = None) -> list:
    repos, page = [], 1
    while True:
        r = gh("GET", f"/orgs/{org}/repos", token=token, params={"per_page": 100, "page": page, "sort": "pushed"})
        batch = r.json()
        repos += batch
        if len(batch) < 100:
            return repos
        page += 1


def repo_info(full_name: str, *, token: str | None = None) -> dict:
    return gh("GET", f"/repos/{full_name}", token=token).json()


def default_branch(full_name: str, *, token: str | None = None) -> str:
    return repo_info(full_name, token=token).get("default_branch", "main")


def branch_sha(full_name: str, branch: str, *, token: str | None = None) -> str:
    return gh("GET", f"/repos/{full_name}/branches/{branch}", token=token).json()["commit"]["sha"]


def open_pulls(full_name: str, since: float, *, token: str | None = None) -> list:
    r = gh("GET", f"/repos/{full_name}/pulls", token=token, params={"state": "open", "per_page": 30, "sort": "updated"})
    out = []
    for pr in r.json():
        updated = pr.get("updated_at", "")
        try:
            ts = time.mktime(time.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except ValueError:
            continue
        if ts >= since:
            out.append(pr)
    return out


def get_pull(full_name: str, number: int, *, token: str | None = None) -> dict:
    return gh("GET", f"/repos/{full_name}/pulls/{number}", token=token).json()


# ---------- PR 审批：人类批准才算数，agent 批准不计入 ----------

def get_reviews(full_name: str, number: int, *, token: str | None = None) -> list:
    return gh("GET", f"/repos/{full_name}/pulls/{number}/reviews",
              token=token, params={"per_page": 50}).json() or []


def _is_agent(login: str, user_type) -> bool:
    low = login.lower()
    return (user_type == "Bot" or login.endswith("[bot]") or low.endswith("-bot")
            or "claude" in low or "codex" in low or "github-actions" in low or "agent" in low)


def classify_reviews(reviews: list) -> dict:
    """把 PR reviews 分成 人工/agent 两组计数。agent 的批准不代表人类决策。"""
    out = {"human_approved": [], "human_changes": [], "agent_approved": 0}
    for rv in reviews:
        u = rv.get("user") or {}
        login = str(u.get("login") or "?")
        state = str(rv.get("state") or "")
        if _is_agent(login, u.get("type")):
            if state == "APPROVED":
                out["agent_approved"] += 1
        elif state == "APPROVED":
            out["human_approved"].append(login)
        elif state == "CHANGES_REQUESTED":
            out["human_changes"].append(login)
    return out


# ---------- Webhook 自动挂钩 ----------

def attach_webhook(full_name: str, hub_url: str, *, token: str | None = None) -> dict:
    cfg = {
        "url": f"{hub_url.rstrip('/')}/api/webhook/github",
        "content_type": "json",
    }
    if config.WEBHOOK_SECRET:
        cfg["secret"] = config.WEBHOOK_SECRET
    r = gh("POST", f"/repos/{full_name}/hooks", token=token, json={
        "name": "web", "active": True, "events": ["push", "pull_request"], "config": cfg,
    })
    return r.json()


# ---------- PR 评论 ----------

def upsert_pr_comment(full_name: str, number: int, body: str, *, token: str | None = None) -> None:
    if not token:
        raise NeedToken("该仓库未绑定可用的 GitHub 连接")
    marked = f"{COMMENT_MARKER}\n\n{body}"
    comments = gh("GET", f"/repos/{full_name}/issues/{number}/comments", token=token, params={"per_page": 100}).json()
    for c in comments:
        if c.get("body", "").startswith(COMMENT_MARKER):
            gh("PATCH", f"/repos/{full_name}/issues/comments/{c['id']}", token=token, json={"body": marked})
            return
    gh("POST", f"/repos/{full_name}/issues/{number}/comments", token=token, json={"body": marked})


# ---------- 回滚（revert PR） ----------

def create_revert_pr(full_name: str, sha: str, is_merge: bool, reason: str, *, token: str | None = None) -> str:
    """克隆仓库 -> revert -> 推分支 -> 开 PR。返回 PR URL。"""
    if not token:
        raise NeedToken("该仓库未绑定可用的 GitHub 连接")
    info = repo_info(full_name, token=token)
    base = info["default_branch"]
    from .git_auth import auth_env
    env = auth_env(token)
    with tempfile.TemporaryDirectory(prefix="hub-revert-") as td:
        repo_dir = Path(td) / "repo"
        url = f"https://github.com/{full_name}.git"
        subprocess.run(["git", "clone", "--depth", "50", url, str(repo_dir)],
                       check=True, capture_output=True, env=env)
        cmd = ["git", "revert", "--no-edit", "-m", "1", sha] if is_merge else ["git", "revert", "--no-edit", sha]
        r = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["git", "revert", "--abort"], cwd=repo_dir, capture_output=True)
            raise RuntimeError(f"git revert 失败：{(r.stderr or r.stdout)[-300:]}")
        branch = f"review-hub/revert-{sha[:7]}"
        subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
                       cwd=repo_dir, check=True, capture_output=True, env=env)
        body = f"## Review Hub 自动回滚\n\n原因：{reason}\n回滚对象：{sha}\n\n此 PR 由控制枢纽自动创建，请人工确认后合并。"
        r2 = gh("POST", f"/repos/{full_name}/pulls", token=token, json={
            "title": f"Revert {sha[:7]}（review-hub 自动回滚）",
            "head": branch, "base": base, "body": body,
        })
        return r2.json().get("html_url", "")


# ---------- 内容读取 ----------

def readme_text(full_name: str, branch: str, *, token: str | None = None) -> str:
    """抓 README（常见文件名逐个试），供初始化画像用。找不到返回空串。"""
    for name in ("README.md", "README_CN.md", "README.zh-CN.md", "README.txt", "README", "readme.md"):
        try:
            r = gh("GET", f"/repos/{full_name}/readme",
                   token=token,
                   params={"ref": branch},
                   headers={"Accept": "application/vnd.github.raw"})
            return r.text[:12000]
        except RuntimeError as e:
            if "404" in str(e):
                continue
            raise
    return ""


def get_file(full_name: str, path: str, ref: str, *, token: str | None = None) -> str:
    r = gh("GET", f"/repos/{full_name}/contents/{path}", token=token, params={"ref": ref})
    return base64.b64decode(r.json()["content"]).decode("utf-8", errors="replace")


# ---------- Webhook 签名 ----------

def verify_signature(payload: bytes, header: str) -> bool:
    if not config.WEBHOOK_SECRET:
        return not config.PUBLIC_BASE_URL  # 公开部署必须校验签名
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(config.WEBHOOK_SECRET.encode(), payload, "sha256").hexdigest()
    return hmac.compare_digest(expected, header[7:])
