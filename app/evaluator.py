"""评审评测集：用已知正确答案回归测试当前评审配置（提示词/模型/编排）。

MVP 范围与规则：
- 用例来自报告页的人工处置：确认的 Important = 正例（应报出）；误报 = 负例（不应再报出）；
- 只跑评审步骤（门禁是确定性的，不参与跑分），复用当前生效编排（项目覆盖 > 全局默认）；
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
from .engine import prompts, verdict

_eval_lock = threading.Lock()


def _config_snapshot() -> str:
    return json.dumps({
        "prompt": hashlib.md5(prompts.get_template().encode()).hexdigest()[:8],
        "pipeline": [f"{s.get('kind')}:{s.get('gate') or s.get('provider')}:{s.get('model')}"
                     for s in worker.get_pipeline() if s.get("enabled", True)],
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
        for step in worker.get_pipeline(project):
            if not step.get("enabled", True) or step.get("kind") == "gate":
                continue
            text, _ = worker._run_reviewer_step(step, project, workdir, built, diff_text)
            outs.append((step.get("name") or step.get("id", "评审"), text))
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
