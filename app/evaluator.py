"""评审评测集：用已知正确答案回归测试当前评审配置（提示词/模型/编排）。

范围与规则：
- 用例来自报告页的人工处置：确认的 Important = 正例（应报出）；误报 = 负例（不应再报出）；
- 按当前生效编排运行节点；门禁只决定是否继续，评审组成员独立使用同一份上游上下文；
- 命中判定与裁决层同一把尺子（verdict.similar：同文件 ±15 行 + 标题相似度）；
- 后台线程跑，逐用例落库进度；同一时间只允许一轮评测。
"""
import hashlib
import json
import threading
import shutil
import tempfile
from pathlib import Path

from . import config, db, worker
from .engine import gates, prompts, verdict

_eval_lock = threading.Lock()


def _config_snapshot() -> str:
    def signature(steps):
        def semantic_step(step):
            if step.get("kind") != "review":
                return step
            return {**step, "reviewers": sorted(step.get("reviewers", []), key=lambda m: m.get("id", ""))}
        return [f"{s.get('id')}:{s.get('kind')}:{s.get('gate') or s.get('provider') or s.get('credential_id')}:"
                f"{s.get('model')}:{s.get('purpose','findings')}:"
                f"{hashlib.sha256(json.dumps(semantic_step(s), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]}"
                for s in steps if s.get("enabled", True)]
    project_ids = [r["project_id"] for r in db.query("SELECT DISTINCT project_id FROM eval_cases ORDER BY project_id")]
    global_steps = worker.get_pipeline()
    project_steps = {str(pid): worker.get_pipeline(db.get("SELECT * FROM projects WHERE id=?", (pid,)))
                     for pid in project_ids if db.get("SELECT id FROM projects WHERE id=?", (pid,))}
    used_credentials = {node.get("credential_id") for steps in [global_steps, *project_steps.values()]
                        for step in steps for node in ([step, *step.get("reviewers", [])])
                        if node.get("kind") == "api"}
    credentials = {str(r["id"]): r["updated_at"] for r in db.query(
        "SELECT id,updated_at FROM model_credentials ORDER BY id") if r["id"] in used_credentials}
    return json.dumps({
        "prompt": hashlib.md5(prompts.get_template().encode()).hexdigest()[:8],
        "rules": hashlib.sha256((config.POLICY_DIR / "REVIEW.md").read_bytes()).hexdigest()[:10],
        "cases": [r["id"] for r in db.query("SELECT id FROM eval_cases ORDER BY id")],
        "credentials": credentials,
        "pipeline": signature(global_steps),
        "projects": {pid: signature(steps) for pid, steps in project_steps.items()},
    }, ensure_ascii=False)


def start_eval() -> dict:
    cases = [dict(c) for c in db.query("SELECT * FROM eval_cases")]
    if not cases:
        return {"ok": False, "msg": "还没有评测用例：先在报告页把确认/误报的 Important 存为用例"}
    if not _eval_lock.acquire(blocking=False):
        return {"ok": False, "msg": "评测正在进行中，稍后再看结果"}
    rid = db.execute(
        "INSERT INTO eval_runs(status,cases_total,config_snapshot,created_at)"
        " VALUES('running',?,?,?)", (len(cases), _config_snapshot(), db.now()))
    threading.Thread(target=_run_eval, args=(rid, cases), daemon=True, name="hub-eval").start()
    return {"ok": True, "eval_id": rid}


def _run_eval(rid: int, cases: list) -> None:
    counters = {"hit": 0, "miss": 0, "fp": 0, "error": 0}
    results = []
    try:
        for c in cases:
            r = _run_case(c)
            counters[r["result"]] += 1
            results.append(r)
            db.execute("UPDATE eval_runs SET hit=?, miss=?, fp=?, error=?, results=? WHERE id=?",
                       (counters["hit"], counters["miss"], counters["fp"], counters["error"],
                        json.dumps(results, ensure_ascii=False), rid))
        db.execute("UPDATE eval_runs SET status='done', finished_at=? WHERE id=?", (db.now(), rid))
    except Exception as e:
        db.execute("UPDATE eval_runs SET status='failed', error_msg=?, finished_at=? WHERE id=?",
                   (str(e)[-1000:], db.now(), rid))
    finally:
        _eval_lock.release()


def _run_case(case: dict) -> dict:
    base = {"case_id": case["id"], "repo": "", "sha": case["head_sha"][:8],
            "polarity": case["polarity"], "expected": json.loads(case["expected"]).get("title", "")}
    project = db.get("SELECT * FROM projects WHERE id=?", (case["project_id"],))
    if not project or project["kind"] != "github":
        return {**base, "result": "error", "detail": "用例所属项目不是 GitHub 项目（MVP 只支持 GitHub）"}
    base["repo"] = project["full_name"]
    try:
        # 评测使用独立目录，避免 checkout/clean 覆盖正在执行的审查。
        parent = Path(tempfile.mkdtemp(prefix=f"hub-eval-{case['id']}-", dir=config.DATA_DIR))
        workdir = parent / "repo"
        worker._ensure_clone(project, workdir)
        worker._git_retry(workdir, "checkout", "--detach", "-f", case["head_sha"])
        worker._git_retry(workdir, "clean", "-fd")
        built = prompts.build(workdir, intent=case["intent"] or "", profile=project["profile"] or "")
        diff_text = built["diff_file"].read_text(encoding="utf-8")
        outs = []
        prior = []
        diff_files = [line[6:] for line in diff_text.splitlines() if line.startswith("+++ b/")]
        for step in worker.get_pipeline(project):
            if not step.get("enabled", True):
                continue
            if step.get("kind") == "gate":
                kw = {"cfg": step, "cache_id": f"eval-{case['id']}"}
                gtype = step.get("gate", "tests")
                if gtype == "semgrep":
                    gate_result = gates.run_semgrep(workdir, diff_files, **kw)
                elif gtype == "build":
                    gate_result = gates.run_build(workdir, **kw)
                else:
                    gate_result = gates.run_tests(workdir, **kw)
                if gate_result["status"] == "fail":
                    raise RuntimeError(f"评测门禁失败：{step.get('name') or gtype}")
                continue
            if step.get("kind") == "review":
                for member in step.get("reviewers", []):
                    if not member.get("enabled", True):
                        continue
                    text, _, _ = worker._run_reviewer_step(member, project, workdir, built, diff_text, prior)
                    name = member.get("name") or member.get("id", "评审")
                    if verdict.extract_findings(text) is None:
                        raise RuntimeError(f"{name} 未返回有效 findings JSON")
                    outs.append((name, text))
                continue
            text, _, _ = worker._run_reviewer_step(step, project, workdir, built, diff_text, prior)
            name = step.get("name") or step.get("id", "评审")
            prior.append({"name": name, "status": "ok", "output": text})
        merged = verdict.merge_multi(outs)
    except Exception as e:
        return {**base, "result": "error", "detail": str(e)[-300:]}
    finally:
        if "parent" in locals():
            shutil.rmtree(parent, ignore_errors=True)
    expected = json.loads(case["expected"])
    got_list = [f for f in merged["findings"] if verdict.similar(f, expected)]
    actual = [{"severity": f.get("severity"), "file": f.get("file"),
               "line": f.get("line"), "title": f.get("title")}
              for f in merged["findings"][:8]]
    if case["polarity"] == "positive":
        return {**base, "result": "hit" if got_list else "miss",
                "detail": f"共报 {len(merged['findings'])} 条", "actual": actual}
    return {**base, "result": "hit" if not got_list else "fp",
            "detail": "负例通过" if not got_list else "误报重现", "actual": actual}
