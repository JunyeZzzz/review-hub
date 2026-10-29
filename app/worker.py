"""后台 worker：消费 runs 队列，执行审查流水线；另有轮询器发现默认分支的新 push。"""
import json
import fcntl
import os
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config, connections, db, github
from .git_auth import auth_env
from .engine import api_nodes, gates, prompts, reviewers, router, verdict

CLONE_DEPTH_ARGS = ["--filter=blob:none"]


def enqueue(project_id: int, trigger: str, ref: str = "", base_ref: str = "",
            head_sha: str = "", pr_number: int | None = None,
            force: bool = False) -> int | None:
    """入队。同一 commit + 同一触发方式的任何状态 run 都视为已审过，不再重复
    （force=True 的手动触发除外：人工要求重审总是执行）。"""
    if head_sha and not force:
        c = db.conn()
        cur = c.execute(
            "INSERT INTO runs(project_id,trigger,ref,base_ref,head_sha,pr_number,created_at) "
            "SELECT ?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM runs "
            "WHERE project_id=? AND head_sha=? AND trigger=?)",
            (project_id, trigger, ref, base_ref, head_sha, pr_number, db.now(),
             project_id, head_sha, trigger))
        c.commit()
        return cur.lastrowid if cur.rowcount else None
    return db.execute(
        "INSERT INTO runs(project_id,trigger,ref,base_ref,head_sha,pr_number,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (project_id, trigger, ref, base_ref, head_sha, pr_number, db.now()))


def _git(workdir: Path, *args: str, check=True):
    return subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True, check=check)


# ---------- 流水线编排：配置中心可读可写，worker 按它执行 ----------

DEFAULT_GATE = {"id": "gate-test", "kind": "gate", "name": "测试门禁", "gate": "tests",
                "sandbox": "off", "enabled": True}

DEFAULT_PIPELINE = [
    dict(DEFAULT_GATE),
    {"id": "cross-review", "kind": "review", "name": "交叉评审", "enabled": True,
     "reviewers": [
         {"id": "claude", "kind": "reviewer", "name": "Claude 评审", "provider": "claude",
          "enabled": True, "model": "", "prompt": ""},
         {"id": "kimi", "kind": "reviewer", "name": "Kimi 评审", "provider": "codex",
          "enabled": True, "model": "", "prompt": ""},
     ]},
]


def _load_steps(raw) -> list | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("流水线配置 JSON 无法解析，不能悄悄改用默认编排") from exc
    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError("流水线配置不是非空节点列表，不能悄悄改用默认编排")
    return parsed


def normalize_pipeline(steps: list) -> list:
    """旧版平铺发现节点收拢为最终交叉评审组；上下文节点保留原顺序。"""
    if not isinstance(steps, list) or any(s.get("kind") == "review" for s in steps if isinstance(s, dict)):
        return steps
    reviewers = [s for s in steps if isinstance(s, dict)
                 and s.get("kind", "reviewer") in ("reviewer", "api")
                 and s.get("purpose", "findings") != "context"]
    if not reviewers:
        return steps
    rest = [s for s in steps if s not in reviewers]
    group_id = "cross-review" if not any(s.get("id") == "cross-review" for s in steps if isinstance(s, dict)) else "review-group"
    return [*rest, {"id": group_id, "kind": "review", "name": "交叉评审",
                    "enabled": True, "reviewers": reviewers}]


def get_pipeline(project: dict | None = None) -> list:
    """生效编排：项目覆盖 > 全局默认；返回用户看到的真实节点顺序。"""
    raw = None
    if project is not None:
        try:
            raw = project["pipeline_json"]
        except (KeyError, IndexError):
            raw = None
    steps = _load_steps(raw) or _load_steps(db.get_setting("pipeline"))
    if steps is None:
        steps = json.loads(json.dumps(DEFAULT_PIPELINE))
    return normalize_pipeline(steps)


MAX_NODE_PROMPT_CHARS = 500_000
MAX_NODE_OUTPUT_CHARS = 200_000


def _node_prompt(step: dict, built: dict, prior: list[dict], workdir: Path,
                 *, api: bool = False) -> str:
    """模型节点只接收上游 LLM 上下文；交叉评审成员之间不交换输出。"""
    purpose = step.get("purpose", "findings")
    if purpose == "context":
        base = (f"你在代码审查流水线中负责：{step.get('name', '分析')}。"
                "只输出供后续节点使用的事实和判断，不必输出 findings JSON。\n"
                f"仓库：{workdir}\n变更 diff：{built['diff_file']}\n"
                f"评审规则：{config.POLICY_DIR / 'REVIEW.md'}\n"
                f"项目背景：{built.get('profile') or '未提供'}\n"
                f"本次变更目的：{built.get('intent') or '未提供'}\n")
    else:
        base = built["prompt_file"].read_text(encoding="utf-8")
    if step.get("prompt"):
        base += "\n## 本节点任务与侧重点\n" + step["prompt"] + "\n"
    if api:
        base += ("\n## API 节点能力边界\n本节点没有 Read/Grep/Glob 或仓库文件工具。"
                 "以下 diff 与规则已经内联；只根据可见证据判断，无法验证的猜测不要报出。\n")
        base += ("\n## API 节点可直接读取的变更 diff\n"
                 + built["diff_file"].read_text(encoding="utf-8")
                 + "\n## 评审规则\n"
                 + (config.POLICY_DIR / "REVIEW.md").read_text(encoding="utf-8"))
    if prior:
        base += "\n## 上游 LLM 节点的最终上下文（按执行顺序）\n"
        for i, item in enumerate(prior, 1):
            base += f"\n### {i}. {item['name']} [{item['status']}]\n{item['output']}\n"
    if len(base) > MAX_NODE_PROMPT_CHARS:
        raise RuntimeError(f"节点输入 {len(base)} 字符超过 {MAX_NODE_PROMPT_CHARS} 上限；请缩小 diff 或前序输出")
    return base


def _run_reviewer_step(step: dict, project: dict, workdir: Path,
                       built: dict, diff_text: str, prior: list[dict] | None = None):
    """执行 Agent 或 API 节点，返回 (输出文本, 轨迹, 实际输入)。"""
    prior = prior or []
    if step.get("purpose", "findings") != "context" and built.get("review_parts"):
        full_path = str(built["diff_file"])
        template = built["prompt_file"].read_text(encoding="utf-8")
        findings, traces, inputs, summaries = [], [], [], []
        total = len(built["review_parts"])
        for index, path in enumerate(built["review_parts"], 1):
            part_prompt = workdir / f".hub-prompt-part-{index}-{uuid.uuid4().hex}.txt"
            part_prompt.write_text(template.replace(full_path, str(path)) +
                                   f"\n## 分片审查 {index}/{total}\n"
                                   "这只是完整变更的一部分。请逐行检查本片段；需要时读取仓库其他文件验证。"
                                   "此片段可能从文件或 diff 块中间开始，报告真实文件行号。"
                                   "不要因为其他片段不可见就断言整个变更没有问题。\n",
                                   encoding="utf-8")
            part_built = {**built, "diff_file": path, "prompt_file": part_prompt, "review_parts": []}
            output, trace, actual_input = _run_reviewer_step(
                step, project, workdir, part_built, path.read_text(encoding="utf-8"), prior)
            found = verdict.extract_findings(output)
            if found is None:
                raise RuntimeError(f"审查片段 {index}/{total} 未返回有效 findings JSON，不能判定通过")
            findings.extend(found)
            summaries.append(f"片段 {index}/{total}：{output.split('```json', 1)[0].strip()}")
            if len(json.dumps({"findings": findings}, ensure_ascii=False)) > MAX_NODE_OUTPUT_CHARS:
                raise RuntimeError(f"审查发现超过 {MAX_NODE_OUTPUT_CHARS} 字符上限，不能判定通过")
            if trace:
                traces.append({"t": "meta", "text": f"审查片段 {index}/{total}"})
                traces.extend(trace)
            inputs.append(f"[审查片段 {index}/{total}]\n{actual_input}")
        combined = ("\n\n".join(summaries) + "\n\n```json\n" +
                    json.dumps({"findings": findings}, ensure_ascii=False) + "\n```")
        if len(combined) > MAX_NODE_OUTPUT_CHARS:
            raise RuntimeError(f"合并后的审查发现超过 {MAX_NODE_OUTPUT_CHARS} 字符上限，不能判定通过")
        return combined, traces, "\n\n".join(inputs)
    is_api = step.get("kind") == "api"
    prompt_text = _node_prompt(step, built, prior, workdir, api=is_api)
    if is_api:
        text = api_nodes.run(step, prompt_text)
        if len(text) > MAX_NODE_OUTPUT_CHARS:
            raise RuntimeError(f"节点输出超过 {MAX_NODE_OUTPUT_CHARS} 字符上限")
        return text, None, prompt_text
    provider = step.get("provider", "claude")
    prompt_file = workdir / f".hub-node-{uuid.uuid4().hex}.txt"
    prompt_file.write_text(prompt_text, encoding="utf-8")
    if provider == "claude":
        text, trace = reviewers.run_claude(workdir, prompt_file, step.get("model") or "")
    elif provider == "codex":
        text, trace = reviewers.run_codex(workdir, prompt_file, step.get("model") or "")
    else:
        raise RuntimeError(f"未知 Agent 执行器：{provider}")
    if len(text) > MAX_NODE_OUTPUT_CHARS:
        raise RuntimeError(f"节点输出超过 {MAX_NODE_OUTPUT_CHARS} 字符上限")
    return text, trace, prompt_text


def _run_review_group(group: dict, project: dict, workdir: Path, built: dict,
                      diff_text: str, prior: list[dict], sl: "StepLog") -> list[tuple[str, str]]:
    """成员并行读取同一份上游输入；结束后按配置顺序汇总，任一失败整组失败。"""
    members = [m for m in group.get("reviewers", []) if m.get("enabled", True)]
    if not members:
        return []
    outputs = [None] * len(members)
    errors = []
    # 分批启动，使排队中的成员仍是未开始，而非报告里的 running。
    for offset in range(0, len(members), config.REVIEW_CONCURRENCY):
        batch = members[offset:offset + config.REVIEW_CONCURRENCY]
        slots = []
        for member in batch:
            name = member.get("name") or member.get("id", "评审")
            t0 = sl.start(name)
            slots.append((member, name, t0, getattr(sl, "step_id", None)))
        with ThreadPoolExecutor(max_workers=len(slots), thread_name_prefix="hub-review") as pool:
            futures = {pool.submit(_run_reviewer_step, member, project, workdir,
                                   built, diff_text, [dict(item) for item in prior]): i
                       for i, (member, _, _, _) in enumerate(slots)}
            for future in as_completed(futures):
                i = futures[future]
                _, name, t0, step_id = slots[i]
                try:
                    text, trace, prompt_text = future.result()
                    if verdict.extract_findings(text) is None:
                        raise RuntimeError("评审器未返回有效 findings JSON，不能判定通过")
                except Exception as exc:
                    sl.finish(t0, "fail", str(exc), step_id=step_id)
                    errors.append(f"{name}: {exc}")
                else:
                    sl.finish(t0, "ok", log=text, trace=trace, prompt=prompt_text, step_id=step_id)
                    outputs[offset + i] = (name, text)
    if errors:
        raise RuntimeError("交叉评审有成员失败；" + "；".join(errors))
    return outputs


def _git_retry(workdir: Path | None, *args: str, attempts: int = 4,
               env: dict | None = None) -> subprocess.CompletedProcess:
    """带退避重试的 git 命令（直连 github 的 SSL 抖动是常态）。
    退避 2s/5s/10s，总跨度约 20s。workdir 为 None 时不设 cwd。"""
    last = None
    for i in range(attempts):
        if i:
            time.sleep((2, 5, 10, 10)[min(i - 1, 3)])
        r = subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True, env=env)
        if r.returncode == 0:
            return r
        last = r
    err = (last.stderr or last.stdout or "")[-400:].strip()
    hint = ""
    if "SSL" in err or "Could not resolve" in err or "unable to access" in err:
        hint = "（网络到 github.com 不稳定：直连抖动或代理断开；可稍后在项目卡片点「立即审查」重试，或配置稳定的代理）"
    raise RuntimeError(f"git {' '.join(args[:2])} 失败（重试{attempts}次）：{err} {hint}")


def _ensure_clone(project: dict, workdir: Path) -> str:
    """保证 workdir 是健康的克隆（失败清场重试，损坏自动重建）。返回日志。"""
    url = f"https://github.com/{project['full_name']}.git"
    env = auth_env(connections.token_for_project(project))
    healthy = (workdir / ".git").exists()
    if healthy:
        # 旧版本把 token 写进 origin URL；每次 fetch 前覆盖为干净地址。
        reset = subprocess.run(["git", "remote", "set-url", "origin", url], cwd=workdir,
                               capture_output=True, text=True)
        if reset.returncode == 0:
            r = subprocess.run(
                ["git", "fetch", "origin", project["default_branch"],
                 "+refs/pull/*/head:refs/remotes/pr/*"],
                cwd=workdir, capture_output=True, text=True, env=env)
            if r.returncode == 0:
                return f"fetch 更新 {project['full_name']}"
        healthy = False  # 克隆损坏，重建
    shutil.rmtree(workdir, ignore_errors=True)
    _git_retry(workdir.parent, "clone", *CLONE_DEPTH_ARGS, url, str(workdir), env=env)
    _git_retry(workdir, "fetch", "origin", project["default_branch"],
               "+refs/pull/*/head:refs/remotes/pr/*", env=env)
    return f"克隆 {project['full_name']}"


class StepLog:
    """步骤记录器：开始/结束写 steps 表。"""

    def __init__(self, run_id: int):
        self.run_id = run_id
        self.idx = 0

    def start(self, name: str) -> float:
        self.step_id = db.execute(
            "INSERT INTO steps(run_id,idx,name,status,log) VALUES(?,?,?,'running','')",
            (self.run_id, self.idx, name))
        self.idx += 1
        return time.time()

    def finish(self, t0: float, status: str, log: str = "", trace=None, prompt=None,
               step_id: int | None = None) -> None:
        stored_log = log if len(log) <= MAX_NODE_OUTPUT_CHARS else (
            log[:MAX_NODE_OUTPUT_CHARS] + "\n[报告展示日志超过 200000 字符，后续内容未保存；步骤状态基于完整执行结果]")
        db.execute(
            "UPDATE steps SET status=?, log=?, trace=?, prompt=?, duration_ms=?, finished_at=? WHERE id=?",
            (status, stored_log,
             json.dumps(trace, ensure_ascii=False) if trace else None,
             prompt if prompt else None,
             int((time.time() - t0) * 1000), db.now(), step_id or self.step_id))


def _prepare(project: dict, run: dict, sl: StepLog) -> Path:
    """准备工作目录。返回 workdir。"""
    t0 = sl.start("准备代码")
    try:
        if project["kind"] == "local":
            source = Path(project["full_name"])
            if not (source / ".git").exists():
                raise RuntimeError(f"本地 Git 仓库不存在：{source}")
            # 审查只读用户仓库：checkout/clean 只能作用于一次性副本。
            parent = Path(tempfile.mkdtemp(prefix=f"hub-run-{run['id']}-", dir=config.DATA_DIR))
            workdir = parent / "repo"
            result = subprocess.run(["git", "clone", "--no-hardlinks", "--", str(source),
                                     str(workdir)], capture_output=True, text=True)
            if result.returncode:
                shutil.rmtree(parent, ignore_errors=True)
                raise RuntimeError(f"本地仓库复制失败：{result.stderr[-400:]}")
            log = f"审查本地仓库副本 {source}"
        else:
            workdir = config.CLONES_DIR / str(project["id"])
            log = _ensure_clone(project, workdir)
        if run["head_sha"]:
            _git_retry(workdir, "checkout", "--detach", "-f", run["head_sha"])
        _git_retry(workdir, "clean", "-fdx")
        sl.finish(t0, "ok", log)
        return workdir
    except Exception as e:
        sl.finish(t0, "fail", f"{e}")
        raise


def _diff_range(workdir: Path, run: dict, project: dict) -> tuple[str, str]:
    """返回 (base_ref, head_ref) 的 git 区间，用于 diff。"""
    if run["trigger"] == "pr" and run["head_sha"]:
        return run["base_ref"] or f"origin/{project['default_branch']}", run["head_sha"]
    head = run["head_sha"] or "HEAD"
    if run["trigger"] in ("push", "manual") and run["base_ref"]:
        return run["base_ref"], head
    parents = _git(workdir, "rev-list", "--parents", "-n", "1", head).stdout.split()
    if len(parents) == 1:
        empty = subprocess.run(["git", "hash-object", "-t", "tree", "--stdin"],
                               cwd=workdir, input="", capture_output=True, text=True, check=True)
        return empty.stdout.strip(), head
    base = f"{head}~1" if len(parents) == 2 else f"{head}^1"
    return base, head


MAX_INTENT_CHARS = 60_000


def _intent_for_range(workdir: Path, run: dict, diff_spec: str, fallback: str) -> str:
    """累计区间用整段提交说明；手动树基线以完整代码审查为目标。"""
    if run["trigger"] not in ("push", "manual") or not run["base_ref"]:
        return fallback
    if run["trigger"] == "manual":
        base_type = _git(workdir, "cat-file", "-t", run["base_ref"]).stdout.strip()
        if base_type == "tree":
            return ("手动指定代码树基线审查：目标是检查该基线到当前提交的完整代码差异，"
                    "找出可验证的真实缺陷。最新提交说明只对应最后一次提交，"
                    "不能代表这次完整审查的变更目的。")
        if base_type != "commit":
            raise RuntimeError(f"手动审查基线必须是提交或代码树，实际为 {base_type}")
    history = _git(workdir, "log", "--reverse", "--format=%h %s%n%b%x00", diff_spec).stdout
    messages = [item.strip() for item in history.split("\x00") if item.strip()]
    if not messages:
        raise RuntimeError(f"审查区间 {diff_spec} 没有可读取的提交说明，无法确认变更意图")
    intent = f"本次审查覆盖 {len(messages)} 个提交（按时间顺序）：\n" + "\n\n".join(messages)
    if len(intent) > MAX_INTENT_CHARS:
        raise RuntimeError(f"提交说明共 {len(intent)} 字符，超过 {MAX_INTENT_CHARS} 上限；请拆分审查，不能只保留最后一个提交说明")
    return intent


def _checkout(workdir: Path, run: dict) -> None:
    if run["head_sha"]:
        _git(workdir, "checkout", "--detach", "-f", run["head_sha"])
    _git(workdir, "clean", "-fd")


# 同一项目的多个 run 共享一个克隆目录（data/clones/{id}）与 .hub-* 工作文件，
# 并发会在 git checkout/clean、prompt 覆盖上互踩——按项目加锁：
# 不同项目并行审查，同一项目自动串行。
_PROJECT_LOCKS: dict = {}
_PROJECT_LOCKS_GUARD = threading.Lock()


def _project_lock(project_id: int) -> threading.Lock:
    with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(project_id)
        if lock is None:
            lock = _PROJECT_LOCKS[project_id] = threading.Lock()
        return lock


def process_run(run_id: int) -> None:
    run = db.get("SELECT * FROM runs WHERE id=?", (run_id,))
    if not run:
        return
    project = db.get("SELECT * FROM projects WHERE id=?", (run["project_id"],))
    with _project_lock(project["id"]):
        try:
            _process_run(run, project)
        finally:
            if project["kind"] == "local":
                # 只清理由本次 run 创建、位于 data 下的临时目录。
                for path in config.DATA_DIR.glob(f"hub-run-{run_id}-*"):
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)


def _process_run(run, project) -> None:
    run_id = run["id"]
    sl = StepLog(run_id)
    workdir = None
    try:
        workdir = _prepare(project, run, sl)
        base_ref, head_ref = _diff_range(workdir, run, project)
        diff_spec = f"{base_ref}{'...' if run['trigger'] == 'pr' else '..'}{head_ref}"

        # 变更上下文：分支 / 提交 / 作者 / commitmsg / 增删行数 / 变更声明目的
        t0 = sl.start("变更上下文")
        head = run["head_sha"] or "HEAD"
        parts = _git(workdir, "log", "-1", "--format=%H%x00%an%x00%aI%x00%s%x00%b", head).stdout.split("\x00")
        sha, author, date, subject, body = (parts + ["", "", "", "", ""])[:5]
        branch = run["ref"] or project["default_branch"]
        intent = _intent_for_range(workdir, run, diff_spec,
                                   (subject + "\n\n" + body.strip()).strip())
        if run["pr_number"] and project["kind"] == "github":
            try:
                pr = github.get_pull(project["full_name"], run["pr_number"],
                                     token=connections.token_for_project(project))
                branch = pr["head"]["ref"]
                intent = f"PR #{run['pr_number']}  {pr['title']}\n\n{(pr.get('body') or '').strip()}"
            except Exception as e:
                print(f"[hub] PR 信息获取失败：{e}")
        additions = deletions = 0
        for line in _git(workdir, "diff", "--numstat", diff_spec).stdout.splitlines():
            p = line.split("\t")
            if len(p) >= 2:
                if p[0].isdigit():
                    additions += int(p[0])
                if p[1].isdigit():
                    deletions += int(p[1])
        db.execute(
            "UPDATE runs SET branch=?, commit_msg=?, commit_author=?, commit_date=?,"
            " intent=?, additions=?, deletions=?, head_sha=COALESCE(NULLIF(head_sha,''),?) WHERE id=?",
            (branch, (subject + "\n" + body.strip()).strip(), author, date, intent,
             additions, deletions, sha if sha != "HEAD" else None, run_id))
        sl.finish(t0, "ok", f"{branch}@{(sha or '?')[:8]} · {author} · +{additions} −{deletions}")

        # 风险路由
        t0 = sl.start("风险路由")
        diff_files = _git(workdir, "diff", "--name-only", diff_spec).stdout.splitlines()
        # 完整 diff 落库（报告页可查，评审对象本体）
        db.execute("UPDATE runs SET diff_text=?, diff_complete=1 WHERE id=?",
                   (_git(workdir, "diff", diff_spec).stdout, run_id))
        changed_lines = additions + deletions
        tier_info = router.classify(diff_files, changed_lines)
        db.execute("UPDATE runs SET tier=?, files_changed=? WHERE id=?",
                   (tier_info["tier"], len(diff_files), run_id))
        sl.finish(t0, "ok", f"tier={tier_info['tier']}  files={len(diff_files)}  lines={changed_lines}")

        # 密钥扫描
        t0 = sl.start("密钥扫描")
        scan = gates.scan_secrets(workdir, diff_files)
        if scan["ok"]:
            sl.finish(t0, "ok", "未发现密钥泄漏")
        else:
            sl.finish(t0, "fail", json.dumps(scan["findings"], ensure_ascii=False, indent=1))
            _finish(run_id, "blocked", summary="密钥扫描未通过，禁止合入（fail loud）",
                    findings=[{"severity": "IMPORTANT", **f, "title": f"密钥泄漏：{f['rule']}",
                               "condition": f["preview"], "fix": "移除密钥并轮换", "sources": ["gitleaks-like"]}
                              for f in scan["findings"]])
            return

        # 门禁只拦截；上下文节点传递输出；交叉评审组成员共享上游输入、互不读取结果。
        pipeline = [s for s in get_pipeline(project) if s.get("enabled", True)]
        # 编排回显：本次到底按哪层配置执行的（coder 验证"自编排真的生效"的依据）
        db.execute("UPDATE runs SET pipeline_snapshot=? WHERE id=?",
                   (json.dumps({
                       "source": "project" if (project["pipeline_json"] or "").strip() else "global",
                       "steps": pipeline}, ensure_ascii=False), run_id))
        review_groups = [s for s in pipeline if s.get("kind") == "review"]
        if len(review_groups) != 1 or not any(m.get("enabled", True) for m in review_groups[0].get("reviewers", [])):
            raise RuntimeError("流水线需要一个启用且包含评审器的交叉评审组")

        reviewers_out = []   # (步骤名, 输出文本)
        gate_nits = []
        prior = []           # 只包含上游 LLM 上下文节点的最终输出
        built = None
        for step in pipeline:
            if step.get("kind") == "gate":
                blocked, _, _, scan_findings = _run_gate(step, project, run, workdir, diff_files, sl)
                if blocked:
                    return   # 门禁红灯已收尾（fail loud），后续步骤不再执行
                # 测试/构建可能修改源文件、生成忽略文件；后续模型只能读原提交。
                _git_retry(workdir, "checkout", "--detach", "-f", sha)
                _git_retry(workdir, "clean", "-fdx")
                built = None  # 若门禁位于上下文节点之后，重新生成被清理的 prompt/diff 文件。
                for f in scan_findings:
                    if f.get("severity") == "NIT":
                        gate_nits.append({
                            "severity": "NIT", "file": f.get("file", ""), "line": f.get("line", 0),
                            "title": f"安全扫描：{f.get('rule', '?')}",
                            "condition": f.get("condition") or f.get("preview") or "规则命中",
                            "fix": "核对安全扫描提示", "sources": ["gate:semgrep"],
                        })
                continue
            if built is None:
                built = prompts.build(workdir, intent=intent, profile=project["profile"] or "",
                                      diff_spec=diff_spec)
            diff_text = built["diff_file"].read_text(encoding="utf-8")
            if step.get("kind") == "review":
                reviewers_out.extend(_run_review_group(step, project, workdir, built, diff_text, prior, sl))
                continue
            name = step.get("name") or step.get("id", "评审")
            t0 = sl.start(name)
            try:
                text, trace, prompt_text = _run_reviewer_step(step, project, workdir, built, diff_text, prior)
                sl.finish(t0, "ok", log=text, trace=trace, prompt=prompt_text)
                prior.append({"name": name, "status": "ok", "output": text})
            except Exception as e:
                sl.finish(t0, "fail", str(e))
                raise

        # 裁决：多评审者交叉合并
        t0 = sl.start("交叉裁决")
        try:
            if not reviewers_out:
                raise RuntimeError("流水线没有有效的发现节点")
            if any(verdict.extract_findings(output) is None for _, output in reviewers_out):
                raise RuntimeError("评审器未返回有效 findings JSON，不能判定通过")
            merged = verdict.merge_multi(reviewers_out)
            if gate_nits:
                merged["findings"].extend(gate_nits)
                merged["summary"] += f"；安全扫描提示 {len(gate_nits)} 条"
            sl.finish(t0, "ok", f"评审来源：{'、'.join(s for s, _ in reviewers_out) or '无'}；{merged['summary']}")
        except Exception as e:
            sl.finish(t0, "fail", str(e))
            raise
        _write_findings(run_id, merged)

        # 状态：有 Important = blocked（fail loud，可见）；否则 passed
        status = "blocked" if merged["important_count"] > 0 else "passed"
        db.execute("UPDATE runs SET status=?, important_count=?, summary=?, finished_at=?, duration_ms=? WHERE id=?",
                   (status, merged["important_count"], merged["summary"], db.now(), _duration(run_id), run_id))

        # GitHub 写回；旧 merged 类型仍保留原有自动回滚处理。
        if project["kind"] == "github":
            _github_followup(project, run, merged, status)
    except Exception as e:
        traceback.print_exc()
        db.execute("UPDATE runs SET status='failed', error=?, finished_at=?, duration_ms=? WHERE id=?",
                   (str(e)[-1000:], db.now(), _duration(run_id), run_id))
    finally:
        db.execute("UPDATE projects SET last_run_at=? WHERE id=?", (db.now(), project["id"]))


def _run_gate(step: dict, project: dict, run: dict, workdir: Path,
              diff_files: list, sl: StepLog) -> tuple[bool, str, str, list]:
    """返回 (是否阻断, 状态, 日志, 结构化发现)；日志不传给模型。"""
    name = step.get("name") or f"门禁:{step.get('gate', 'tests')}"
    gtype = step.get("gate") or "tests"
    t0 = sl.start(name)
    try:
        kw = {"cfg": step, "cache_id": str(project["id"])}
        if gtype == "semgrep":
            gr = gates.run_semgrep(workdir, diff_files, **kw)
        elif gtype == "build":
            gr = gates.run_build(workdir, **kw)
        elif gtype == "tests":
            gr = gates.run_tests(workdir, **kw)
        else:
            raise RuntimeError(f"未知门禁类型：{gtype}；不能改为测试门禁继续执行")
        if gr.get("status") not in ("ok", "skip", "fail"):
            raise RuntimeError(f"门禁 {gtype} 返回无效状态 {gr.get('status')!r}，不能判定通过")
    except Exception as e:
        sl.finish(t0, "fail", str(e))
        _block_by_gate(project, run, name, gtype, "门禁执行器异常（fail loud）", str(e)[-2000:])
        return True, "fail", str(e), []
    sl.finish(t0, gr["status"], gr.get("log", ""))
    if gr["status"] != "fail":
        return False, gr["status"], gr.get("log", ""), gr.get("findings") or []
    _block_by_gate(project, run, name, gtype,
                   gr.get("cmd") or "门禁失败", gr.get("log", ""), gr.get("findings"))
    return True, "fail", gr.get("log", ""), gr.get("findings") or []


def _block_by_gate(project: dict, run: dict, name: str, gtype: str,
                   condition: str, log: str, scan_findings: list | None = None) -> None:
    """门禁红灯收尾：扫描命中保留逐条证据，执行故障生成一条门禁发现。"""
    if gtype == "semgrep" and scan_findings:
        findings = [{"severity": f["severity"], "file": f["file"], "line": f["line"],
                     "title": f"安全扫描：{f['rule']}", "condition": f.get("condition") or f.get("preview") or "规则命中",
                     "fix": "核对扫描证据并修复；确认误报时保留处置依据",
                     "sources": ["gate:semgrep"]} for f in scan_findings]
    else:
        findings = [{"severity": "IMPORTANT", "file": "", "line": 0,
                     "title": f"{name}未通过",
                     "condition": f"{condition}（日志末尾：{log[-300:]}）" if log else condition,
                     "fix": "修复门禁执行或检查失败原因后重审；在报告页展开步骤日志",
                     "sources": [f"gate:{gtype}"]}]
    _finish(run["id"], "blocked", summary=f"{name}未通过（fail loud）：{condition}", findings=findings)
    if project["kind"] == "github":
        _github_followup(project, run,
                         {"findings": findings, "important_count": sum(f["severity"] == "IMPORTANT" for f in findings),
                          "summary": f"{name}未通过"},
                         "blocked")


def _write_findings(run_id: int, merged: dict) -> None:
    db.execute("UPDATE runs SET findings_json=? WHERE id=?",
               (json.dumps(merged["findings"], ensure_ascii=False), run_id))


def _finish(run_id: int, status: str, summary: str, findings: list) -> None:
    imp = sum(1 for f in findings if f.get("severity") == "IMPORTANT")
    db.execute("UPDATE runs SET status=?, summary=?, important_count=?, findings_json=?, finished_at=?, duration_ms=? WHERE id=?",
               (status, summary, imp, json.dumps(findings, ensure_ascii=False), db.now(), _duration(run_id), run_id))


def _duration(run_id: int) -> int:
    row = db.get("SELECT started_at, created_at FROM runs WHERE id=?", (run_id,))
    from datetime import datetime
    start = row["started_at"] or row["created_at"]
    t0 = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S.%fZ")
    return int((datetime.utcnow() - t0).total_seconds() * 1000)


def _github_followup(project: dict, run: dict, merged: dict, status: str) -> None:
    """评论写回 + 旧 merged 类型的自动回滚；失败只记 error，不影响 run 状态。"""
    try:
        if run["pr_number"]:
            body = _comment_markdown(run, merged, status)
            github.upsert_pr_comment(project["full_name"], run["pr_number"], body,
                                     token=connections.token_for_project(project))
    except Exception as e:
        print(f"[hub] 评论写回失败：{e}")
    if (run["trigger"] == "merged" and merged["important_count"] > 0
            and project["auto_revert"] and run["head_sha"]):
        try:
            url = github.create_revert_pr(
                project["full_name"], run["head_sha"], True,
                f"合并后评审发现 {merged['important_count']} 个 Important（run #{run['id']}）",
                token=connections.token_for_project(project))
            db.execute("UPDATE runs SET revert_pr_url=? WHERE id=?", (url, run["id"]))
            print(f"[hub] 已自动创建回滚 PR：{url}")
        except Exception as e:
            print(f"[hub] 自动回滚失败：{e}")


def _comment_markdown(run: dict, merged: dict, status: str) -> str:
    imp = [f for f in merged["findings"] if f["severity"] == "IMPORTANT"]
    nit = [f for f in merged["findings"] if f["severity"] == "NIT"]
    lines = [f"## Review Hub 交叉评审 — {'❌ 发现问题' if status == 'blocked' else '✅ 通过'}",
             f"风险等级 `{run['tier']}`；Important **{len(imp)}**，Nit {len(nit)}", ""]
    for i, f in enumerate(imp, 1):
        loc = f"`{f['file']}:{f['line']}` " if f.get("file") else ""
        lines.append(f"{i}. **{loc}{f['title']}** — 触发：{f['condition']}；建议：{f['fix']}")
    if nit:
        lines.append("\n<details><summary>Nit（单模型，未交叉验证）</summary>")
        for f in nit:
            lines.append(f"- `{f['file']}:{f['line']}` {f['title']}")
        lines.append("</details>")
    return "\n".join(lines)


# ---------- 轮询器：只在"接入之后发生了新 push"才审查（导入时的现状只做基线，不审） ----------

_POLL_STATE = {"ok": None, "ts": 0.0, "err": ""}   # 进程内健康状态，重启即重置


def poll_status() -> dict:
    import time as _t
    ok, age = _POLL_STATE["ok"], int(_t.time() - _POLL_STATE["ts"]) if _POLL_STATE["ts"] else None
    return {"ok": ok, "age_s": age, "err": _POLL_STATE["err"][:200]}


def active_worker_count() -> int:
    return sum(t.is_alive() and t.name.startswith("hub-worker-") for t in threading.enumerate())


def poll_loop() -> None:
    import time as _t
    while True:
        try:
            _poll_once()
            _POLL_STATE.update(ok=True, ts=_t.time(), err="")
        except Exception as e:
            _POLL_STATE.update(ok=False, ts=_t.time(), err=str(e))
            print(f"[hub] 轮询异常：{e}")
        time.sleep(config.POLL_INTERVAL)


def _poll_once() -> None:
    projects = db.query("SELECT * FROM projects WHERE kind='github' AND enabled=1")
    errors = []
    for p in projects:
        full = p["full_name"]
        try:
            token = connections.token_for_project(p)
            sha = github.branch_sha(full, p["default_branch"], token=token)
            if not p["last_sha"]:
                # 首次见到该仓库：只记基线，不审查
                db.execute("UPDATE projects SET last_sha=? WHERE id=?", (sha, p["id"]))
            elif sha != p["last_sha"]:
                enqueue(p["id"], "push", ref=p["default_branch"],
                        base_ref=p["last_sha"], head_sha=sha)
                db.execute("UPDATE projects SET last_sha=? WHERE id=?", (sha, p["id"]))
            prefetch_commits(p)
        except Exception as e:
            print(f"[hub] 轮询 {full} 失败：{e}")
            errors.append(f"{full}: {e}")
    if errors:
        raise RuntimeError(f"{len(errors)} 个项目轮询失败：" + "；".join(errors))


# ---------- 提交历史缓存：项目页秒开的关键 ----------

_COMMITS_CACHE = {}    # (pid, branch, last_sha) -> {"ts": float, "data": [...], "err": ""}
_PREFETCH_AT = {}      # 同键 -> 上次预热时间


def get_commits(p: dict, limit: int = 30) -> tuple[list, str]:
    """默认分支提交历史。缓存键含 last_sha：有新 push 自动换新缓存。
    TTL 300s；网络失败回退旧缓存（stale-while-revalidate），并对失败结果做 15s 负缓存防打爆。"""
    key = (p["id"], p["default_branch"], p["last_sha"] or "")
    now = time.time()
    hit = _COMMITS_CACHE.get(key)
    if hit and now - hit["ts"] < 300 and not hit.get("err"):
        return hit["data"], ""
    if hit and hit.get("err") and now - hit["ts"] < 15:
        return hit["data"], hit["err"]

    commits, err = [], ""
    try:
        if p["kind"] == "github":
            r = github.gh("GET", f"/repos/{p['full_name']}/commits",
                          token=connections.token_for_project(p),
                          params={"sha": p["default_branch"], "per_page": limit})
            for c in r.json():
                commits.append({
                    "sha": c["sha"],
                    "author": c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"],
                    "msg": (c["commit"]["message"] or "").split("\n")[0],
                })
        else:
            r = subprocess.run(["git", "log", f"-n{limit}", "--format=%H%x00%an%x00%aI%x00%s"],
                               cwd=p["full_name"], capture_output=True, text=True)
            for line in r.stdout.splitlines():
                parts = (line.split("\x00") + ["", "", ""])[:4]
                sha, author, date, msg = parts
                if sha:
                    commits.append({"sha": sha, "author": author, "date": date, "msg": msg})
    except github.NeedToken:
        raise
    except Exception as e:
        err = str(e)
        if hit and now - hit["ts"] < 3600:   # 有旧缓存就先用旧的
            return hit["data"], f"获取最新提交失败，当前显示缓存：{err}"
    if not err:
        _COMMITS_CACHE[key] = {"ts": now, "data": commits}
    else:
        _COMMITS_CACHE[key] = {"ts": now - 285, "data": commits, "err": err}   # 负缓存 15s
    for k in [k for k in list(_COMMITS_CACHE) if k[0] == p["id"] and k != key]:
        _COMMITS_CACHE.pop(k, None)
    return commits, err


def prefetch_commits(p: dict) -> None:
    """轮询周期里静默预热提交历史缓存，用户打开项目页时已经是热的。"""
    key = (p["id"], p["default_branch"], p["last_sha"] or "")
    now = time.time()
    if now - _PREFETCH_AT.get(key, 0) < 240:
        return
    _PREFETCH_AT[key] = now

    def _run():
        try:
            get_commits(p)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def _claim(sql: str, args: tuple) -> int | None:
    """原子认领：UPDATE ... RETURNING，多 worker 线程并发下不重复消费同一任务。"""
    c = db.conn()
    cur = c.execute(sql + " RETURNING id", args)
    row = cur.fetchone()
    c.commit()
    return row[0] if row else None


def worker_loop() -> None:
    while True:
        try:
            # 初始化任务优先：项目画像轻量且用户在等
            pid = _claim(
                "UPDATE projects SET init_status='running' WHERE id="
                "(SELECT id FROM projects WHERE init_status='pending' ORDER BY id LIMIT 1)", ())
            if pid:
                process_init(pid)
                continue
            rid = _claim(
                "UPDATE runs SET status='running', started_at=? WHERE id="
                "(SELECT id FROM runs WHERE status='queued' ORDER BY id LIMIT 1)", (db.now(),))
            if rid:
                process_run(rid)
                continue
            time.sleep(1)
        except Exception:
            traceback.print_exc()
            time.sleep(3)


def process_init(project_id: int) -> None:
    """初始化项目画像：用 Kimi 快速概括这个仓库是干什么的（可被人工编辑覆盖）。"""
    from .engine import prompts as _p
    p = db.get("SELECT * FROM projects WHERE id=?", (project_id,))
    if not p or p["kind"] != "github":
        db.execute("UPDATE projects SET init_status='done' WHERE id=?", (project_id,))
        return
    db.execute("UPDATE projects SET init_status='running' WHERE id=?", (project_id,))
    try:
        token = connections.token_for_project(p)
        info = github.repo_info(p["full_name"], token=token)
        readme = github.readme_text(p["full_name"], info.get("default_branch", "main"), token=token)
        prompt = _p.INIT_PROMPT.format(
            name=p["full_name"],
            description=info.get("description") or "（无）",
            language=info.get("language") or "（未知）",
            readme=readme.strip() or "（没有 README）",
        )
        if not config.KIMI_API_KEY or not config.CODEX_BIN:
            raise RuntimeError("Kimi/codex 不可用，无法自动生成画像；可在项目卡片手动填写")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(config.CODEX_HOME)
        env["KIMI_API_KEY"] = config.KIMI_API_KEY
        env["PATH"] = config.SUBPROCESS_PATH
        r = subprocess.run(
            [config.CODEX_BIN, "exec", "--sandbox", "read-only", "--skip-git-repo-check", prompt],
            cwd="/tmp", capture_output=True, text=True, timeout=300, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"codex 退出码 {r.returncode}：{(r.stderr or r.stdout)[-300:]}")
        text = r.stdout.split("tokens used")[0].strip()
        if not text:
            raise RuntimeError("模型返回为空")
        db.execute("UPDATE projects SET profile=?, init_status='done' WHERE id=?",
                   (text[-600:], project_id))
        print(f"[hub] 项目画像完成：{p['full_name']} -> {text[:60]}…")
    except Exception as e:
        traceback.print_exc()
        db.execute("UPDATE projects SET init_status='failed' WHERE id=?", (project_id,))
        db.execute("UPDATE projects SET profile=? WHERE id=?",
                   (f"（自动初始化失败：{str(e)[:200]}）", project_id))


_WORKER_LOCK_FILE = None


def start_background() -> None:
    global _WORKER_LOCK_FILE
    if os.environ.get("HUB_BACKGROUND", "1") == "0":
        return
    lock = open(config.DATA_DIR / "worker.lock", "a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        print("[hub] 另一进程已持有后台 worker 锁；本进程只提供 API")
        return
    _WORKER_LOCK_FILE = lock
    # 唯一 worker 进程在启动时恢复被进程退出打断的任务。
    db.execute("UPDATE runs SET status='queued',started_at=NULL WHERE status='running'")
    db.execute("UPDATE projects SET init_status='pending' WHERE init_status='running'")
    db.execute("UPDATE eval_runs SET status='failed',error_msg='服务重启中断',finished_at=? WHERE status='running'", (db.now(),))
    threading.Thread(target=poll_loop, daemon=True, name="hub-poller").start()
    # 并发 worker：门禁有沙盒与资源上限后，多项目同时审查才安全
    for i in range(max(1, config.WORKER_CONCURRENCY)):
        threading.Thread(target=worker_loop, daemon=True, name=f"hub-worker-{i + 1}").start()
