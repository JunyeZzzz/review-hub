"""评审执行器：claude / codex(经 Kimi)。
返回 (输出文本, 轨迹)；轨迹是结构化的思考过程（thinking/tool 事件流），供报告页可视化。
fail loud——失败抛异常，由 worker 记为红灯。
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import config
from ..processes import run_grouped
from .api_nodes import validate_base_url


class ReviewerError(RuntimeError):
    pass


def _timeout_error(exc, parser, label):
    _, trace = parser(exc.output or '', allow_partial=True)
    error = ReviewerError(f'{label} 调用超时（{exc.timeout} 秒），未得出完整结论；已保留超时前的工具轨迹')
    error.trace = trace or []
    return error


def _subprocess_env() -> dict:
    """显式 PATH：把 CLI 所在目录放在最前，杜绝 PATH 漂移。"""
    env = dict(os.environ)
    env["PATH"] = config.SUBPROCESS_PATH
    return env


def claude_available() -> bool:
    return bool(config.CLAUDE_BIN) or shutil.which("claude")


def codex_available() -> bool:
    return bool(config.CODEX_BIN) or shutil.which("codex")


def claude_ready() -> bool:
    """env 凭证或本机 Claude Code 登录态任一即可。"""
    if claude_available() and (config.get_claude_token() or (Path.home() / ".claude").exists()):
        return True
    return False


def codex_ready() -> bool:
    """Kimi key 驱动的 codex（走 hub 专属 CODEX_HOME 配置）。"""
    return codex_available() and bool(config.get_kimi_api_key())


def _sync_codex_config() -> None:
    """按当前数据库配置原子更新 Hub 专属 Codex Home，检测与评审共用。"""
    home = config.CODEX_HOME
    home.mkdir(parents=True, exist_ok=True)
    base = validate_base_url("moonshot", config.get_kimi_base_url())
    body = (f'model_provider = "kimi"\nmodel = {json.dumps(config.get_kimi_model())}\n\n'
            '[model_providers.kimi]\nname = "Kimi"\n'
            f'base_url = {json.dumps(base)}\nenv_key = "KIMI_API_KEY"\nwire_api = "responses"\n')
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=home,
                                     prefix=".config-", delete=False) as tmp:
        tmp.write(body)
        temp_path = Path(tmp.name)
    temp_path.replace(home / "config.toml")


def _parse_claude_stream(raw: str, *, allow_partial=False):
    """解析 claude -p --output-format stream-json 的 JSONL → (最终文本, 轨迹)。"""
    trace, final = [], None
    saw_json = False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_json = True
        t = d.get("type")
        if t == "assistant":
            for block in (d.get("message") or {}).get("content") or []:
                bt = block.get("type")
                if bt == "thinking" and block.get("thinking"):
                    trace.append({"t": "think", "text": block["thinking"][:4000]})
                elif bt == "tool_use":
                    inp = block.get("input") or {}
                    detail = (inp.get("file_path") or inp.get("pattern")
                              or inp.get("path") or json.dumps(inp, ensure_ascii=False))
                    trace.append({"t": "tool", "name": block.get("name", "?"),
                                  "detail": str(detail)[:200]})
                elif bt == "text" and block.get("text"):
                    trace.append({"t": "text", "text": block["text"][:2000]})
        elif t == "result":
            final = d.get("result") or ""
            if d.get("num_turns"):
                trace.append({"t": "meta", "text": f"共 {d['num_turns']} 轮"})
    if allow_partial and saw_json and final is None:
        return '', trace
    if not saw_json or final is None:
        return raw, None
    return final, trace


def _parse_codex_stream(raw: str, *, allow_partial=False):
    """解析 codex exec --json 的 JSONL → (最终文本, 轨迹)。
    兼容两种事件形态：新版 thread/item 流 和 旧版 msg 包装。宽容解析，认不出就当纯文本。"""
    trace, finals, saw_json = [], [], False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_json = True
        m = d.get("msg") if isinstance(d.get("msg"), dict) else d
        t = str(m.get("type") or "")
        item = m.get("item") if isinstance(m.get("item"), dict) else None
        if item:
            it = str(item.get("type") or "")
            if it == "reasoning" and item.get("text"):
                trace.append({"t": "think", "text": str(item["text"])[:4000]})
            elif it == "agent_message" and item.get("text"):
                finals.append(item["text"])
            elif it == "command_execution":
                trace.append({"t": "tool", "name": "exec",
                              "detail": str(item.get("command"))[:200]})
            continue
        if "reason" in t and m.get("text"):
            trace.append({"t": "think", "text": str(m["text"])[:4000]})
        elif t == "exec_command_begin":
            trace.append({"t": "tool", "name": "exec",
                          "detail": " ".join(m.get("command") or [])[:200]})
        elif t in ("agent_message", "assistant_message") and (m.get("message") or m.get("text")):
            finals.append(m.get("message") or m.get("text"))
        elif t == "turn.completed" or t == "token_count":
            usage = m.get("usage") or {}
            if usage.get("total_tokens") or usage.get("input_tokens"):
                n = usage.get("total_tokens") or usage.get("input_tokens")
                trace.append({"t": "meta", "text": f"tokens: {n}"})
    if allow_partial and saw_json and not finals:
        return '', trace
    if not saw_json or not finals:
        return raw, None
    return "\n".join(finals), trace


def run_claude(workdir: Path, prompt_file: Path, model: str = "", *,
               timeout: int | None = None, max_turns: int = 40, allowed_tools: str = "Read,Grep,Glob"):
    timeout = config.REVIEW_AGENT_TIMEOUT_SECONDS if timeout is None else timeout
    if not claude_available():
        raise ReviewerError(
            f"claude CLI 未找到（启动时的 PATH={os.environ.get('PATH', '')[:300]}）；"
            "请确认 npm -g 安装位置在 PATH 里，然后重启 hub")
    cmd = [config.CLAUDE_BIN, "-p", prompt_file.read_text(encoding="utf-8"),
           "--output-format", "stream-json", "--verbose",
           "--allowedTools", allowed_tools, "--max-turns", str(max_turns)]
    model = model or config.get_claude_model()
    if model:
        cmd += ["--model", model]
    env=_subprocess_env()
    env['DISABLE_AUTOUPDATER']='1'  # 仅 Hub 子进程，避免验证过程中自行替换执行器。
    try:
        r = run_grouped(cmd, cwd=workdir, env=env, timeout=timeout,
                        stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise _timeout_error(exc, _parse_claude_stream, 'Claude') from exc
    if r.returncode != 0:
        raise ReviewerError(f"claude 退出码 {r.returncode}：{(r.stderr or r.stdout)[-500:]}")
    text, trace = _parse_claude_stream(r.stdout)
    if not text.strip():
        raise ReviewerError("claude 无有效输出")
    return text, trace


def run_codex(workdir: Path, prompt_file: Path, model: str = "", *,
              timeout: int | None = None, skip_git_check: bool = False):
    timeout = config.REVIEW_AGENT_TIMEOUT_SECONDS if timeout is None else timeout
    if not codex_available():
        raise ReviewerError(
            f"codex CLI 未找到（启动时的 PATH={os.environ.get('PATH', '')[:300]}）")
    if not config.get_kimi_api_key():
        raise ReviewerError("缺少 Kimi API Key（配置中心或 .env）——Codex 侧评审由 Kimi 驱动")
    _sync_codex_config()
    env = _subprocess_env()
    env["CODEX_HOME"] = str(config.CODEX_HOME)
    env["KIMI_API_KEY"] = config.get_kimi_api_key()
    cmd = [config.CODEX_BIN, "exec", "--sandbox", "read-only", "--json",
           prompt_file.read_text(encoding="utf-8")]
    if skip_git_check:
        cmd.insert(-1, "--skip-git-repo-check")
    model = model or config.get_kimi_model()
    if model:
        cmd = cmd[:1] + ["--model", model] + cmd[1:]
    try:
        r = run_grouped(cmd, cwd=workdir, env=env, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise _timeout_error(exc, _parse_codex_stream, 'Kimi CLI') from exc
    if r.returncode != 0:
        raise ReviewerError(f"codex 退出码 {r.returncode}：{(r.stderr or r.stdout)[-500:]}")
    text, trace = _parse_codex_stream(r.stdout)
    if not text.strip():
        raise ReviewerError("codex 无有效输出")
    return text, trace
