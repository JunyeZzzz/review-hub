"""手动连通性检测：只保存最近一次结果，不保存模型回复或密钥。"""
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import config, connections, db
from .engine import api_nodes, reviewers

_lock = threading.Lock()
_inflight: set[str] = set()
_slots = threading.BoundedSemaphore(2)
_PROMPT = "Connectivity check. Reply with OK only. Do not use tools."


class CheckBusy(RuntimeError):
    pass


def list_checks() -> list[dict]:
    rows = [dict(row) for row in db.query(
        "SELECT target,status,detail,latency_ms,checked_at,model FROM connectivity_checks ORDER BY target")]
    out = []
    for row in rows:
        if row["target"].startswith("api:"):
            credential = db.get("SELECT updated_at,disabled FROM model_credentials WHERE id=?",
                                (int(row["target"][4:]),))
            if not credential:
                continue
            row["stale"] = bool(credential["disabled"] or credential["updated_at"] > row["checked_at"])
        else:
            row["stale"] = False
        out.append(row)
    return out


def _safe_detail(exc: Exception, secrets: list[str]) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "检测超时（60 秒）；远端结果未知，请稍后重试"
    detail = str(exc)
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "[REDACTED]")
    return detail[-300:] or type(exc).__name__


def _record(target: str, model: str, operation, secrets: list[str]) -> dict:
    with _lock:
        if target in _inflight:
            raise CheckBusy("该目标正在检测中")
        if not _slots.acquire(blocking=False):
            raise CheckBusy("已有两项检测进行中，请稍后重试")
        _inflight.add(target)
    start = time.monotonic()
    try:
        try:
            operation()
            status, detail = "ok", "真实请求成功；仅证明本次检测所用模型可响应"
        except Exception as exc:
            status, detail = "failed", _safe_detail(exc, secrets)
        result = {"target": target, "status": status, "detail": detail,
                  "latency_ms": int((time.monotonic() - start) * 1000),
                  "checked_at": db.now(), "model": model, "stale": False}
        db.execute("INSERT INTO connectivity_checks(target,status,detail,latency_ms,checked_at,model) "
                   "VALUES(?,?,?,?,?,?) ON CONFLICT(target) DO UPDATE SET "
                   "status=excluded.status,detail=excluded.detail,latency_ms=excluded.latency_ms,"
                   "checked_at=excluded.checked_at,model=excluded.model",
                   (target, status, detail, result["latency_ms"], result["checked_at"], model))
        return result
    finally:
        with _lock:
            _inflight.remove(target)
            _slots.release()


def check_api(cid: int, model: str) -> dict:
    credential = api_nodes.get_active(cid)
    model = model.strip()
    if not model or len(model) > 100:
        raise ValueError("请填写 1–100 字符的模型 ID")
    secret = connections._secret_path(credential["secret_ref"]).read_text(encoding="utf-8").strip()
    return _record(f"api:{cid}", model,
                   lambda: api_nodes.run({"credential_id": cid, "model": model}, _PROMPT,
                                         timeout=60, max_tokens=32), [secret])


def check_agent(agent: str) -> dict:
    if agent not in ("claude", "codex"):
        raise ValueError("未知 Agent CLI")
    model = config.get_claude_model() if agent == "claude" else config.get_kimi_model()
    def operation():
        with tempfile.TemporaryDirectory(prefix="hub-check-", dir=config.DATA_DIR) as tmp:
            workdir = Path(tmp)
            prompt_file = workdir / "prompt.txt"
            prompt_file.write_text(_PROMPT, encoding="utf-8")
            if agent == "claude":
                reviewers.run_claude(workdir, prompt_file, timeout=60, max_turns=1, allowed_tools="")
            else:
                reviewers.run_codex(workdir, prompt_file, timeout=60, skip_git_check=True)
    return _record(f"agent:{agent}", model, operation,
                   [config.get_claude_token(), config.get_kimi_api_key()])
