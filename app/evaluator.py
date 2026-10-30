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
from . import improvements
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        "sample_content": hashlib.sha256(json.dumps([dict(r) for r in db.query('SELECT * FROM eval_cases ORDER BY id')], sort_keys=True).encode()).hexdigest(),
        "active_improvements": [dict(r) for r in db.query("SELECT * FROM review_improvements WHERE status='active' ORDER BY id")],
        "profiles": {str(r['id']): r['profile'] for r in db.query('SELECT id,profile FROM projects ORDER BY id')},
        "cli_models": [config.get_kimi_model(), config.get_claude_model(), config.get_kimi_base_url()],
        "cli_binaries":config.cli_identity(),
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


def recover_interrupted() -> None:
    """进程重启后，旧线程不可能恢复；保留最后进度并标为失败。"""
    db.execute("UPDATE eval_runs SET status='failed',error_msg=?,finished_at=? WHERE status='running'",
               ("服务重启中断了评测；已完成样本仍可查看，请重新运行", db.now()))


def _run_eval(rid: int, cases: list) -> None:
    counters = {"hit": 0, "miss": 0, "fp": 0, "error": 0}
    results = []
    try:
        saved = db.get("SELECT config_snapshot FROM eval_runs WHERE id=?", (rid,))
        expected_config = saved["config_snapshot"] if saved else ""
        if not expected_config:
            raise RuntimeError("评测缺少配置快照，不能给出可信结果")
        for c in cases:
            if _config_snapshot() != expected_config:
                raise RuntimeError("评测运行期间配置或样本发生变化；本轮结果已失效，请重新运行")
            def publish(stage: str, trace: list[dict]) -> None:
                db.execute("UPDATE eval_runs SET current_case_id=?,current_stage=?,current_detail=?,current_trace=? WHERE id=?",
                           (c["id"], stage, json.loads(c["expected"]).get("title", ""),
                            json.dumps(trace, ensure_ascii=False), rid))
            r = _run_case(c, publish)
            counters[r["result"]] += 1
            results.append(r)
            db.execute("UPDATE eval_runs SET hit=?, miss=?, fp=?, error=?, results=?,"
                       "current_case_id=NULL,current_stage=NULL,current_detail=NULL,current_trace=NULL WHERE id=?",
                       (counters["hit"], counters["miss"], counters["fp"], counters["error"],
                        json.dumps(results, ensure_ascii=False), rid))
        if _config_snapshot() != expected_config:
            raise RuntimeError("评测完成时配置或样本已变化；本轮结果已失效，请重新运行")
        db.execute("UPDATE eval_runs SET status='done', finished_at=? WHERE id=?", (db.now(), rid))
    except Exception as e:
        db.execute("UPDATE eval_runs SET status='failed', error_msg=?, finished_at=? WHERE id=?",
                   (str(e)[-1000:], db.now(), rid))
    finally:
        _eval_lock.release()


def _run_case(case: dict, progress=None, improvement_items=None) -> dict:
    base = {"case_id": case["id"], "repo": "", "sha": case["head_sha"][:8],
            "polarity": case["polarity"], "expected": json.loads(case["expected"]).get("title", "")}
    trace = []
    def stage(name: str) -> None:
        if trace and trace[-1]["status"] == "running":
            trace[-1]["status"] = "ok"
        trace.append({"name": name, "status": "running"})
        if progress:
            progress(name, trace)
    project = db.get("SELECT * FROM projects WHERE id=?", (case["project_id"],))
    if not project or project["kind"] != "github":
        return {**base, "result": "error", "detail": "用例所属项目不是 GitHub 项目（MVP 只支持 GitHub）",
                "steps": [{"name": "验证项目", "status": "fail"}]}
    base["repo"] = project["full_name"]
    project={**dict(project),'_cli_identity':config.cli_identity()}
    try:
        source = db.get("SELECT trigger,base_ref,head_sha FROM runs WHERE id=? AND project_id=?",
                        (case.get("run_id"), case["project_id"]))
        if not source or source["head_sha"] != case["head_sha"]:
            raise RuntimeError("评测用例的原审查记录或提交号已失效，不能用其他变更代替")
        if source["trigger"] == "pr" and not source["base_ref"]:
            raise RuntimeError("旧 PR 审查没有保存不可变的基线 SHA，无法精确重放评测")
        # 评测使用独立目录，避免 checkout/clean 覆盖正在执行的审查。
        stage("拉取仓库代码")
        parent = Path(tempfile.mkdtemp(prefix=f"hub-eval-{case['id']}-", dir=config.DATA_DIR))
        workdir = parent / "repo"
        worker._ensure_clone(project, workdir)
        stage("检出原提交")
        worker._git_retry(workdir, "checkout", "--detach", "-f", case["head_sha"])
        worker._git_retry(workdir, "clean", "-fdx")
        stage("生成审查输入")
        base_ref, head_ref = worker._diff_range(workdir, source, project)
        diff_spec = f"{base_ref}{'...' if source['trigger'] == 'pr' else '..'}{head_ref}"
        built = prompts.build(workdir, intent=case["intent"] or "",
                              profile=project["profile"] or "", diff_spec=diff_spec)
        diff_text = built["diff_file"].read_text(encoding="utf-8")
        outs = []
        review_details = []
        prior = []
        diff_files = built.get('changed_files', [line[6:] for line in diff_text.splitlines()
                                               if line.startswith(('+++ b/', '--- a/'))])
        from . import improvements
        items = improvements.active_for_project(project['id']) if improvement_items is None else improvement_items
        built, selected = improvements.attach(built, items, diff_files)
        base['improvements'] = [{'id': i['id'], 'revision': i['revision']} for i in selected]
        for step in worker.get_pipeline(project):
            if not step.get("enabled", True):
                continue
            if step.get("kind") == "gate":
                stage("门禁 · " + (step.get("name") or step.get("gate", "测试")))
                kw = {"cfg": step, "cache_id": f"eval-{case['id']}"}
                gtype = step.get("gate", "tests")
                if gtype == "semgrep":
                    gate_result = gates.run_semgrep(workdir, diff_files, **kw)
                elif gtype == "build":
                    gate_result = gates.run_build(workdir, **kw)
                elif gtype == "tests":
                    gate_result = gates.run_tests(workdir, **kw)
                else:
                    raise RuntimeError(f"评测遇到未知门禁类型：{gtype}")
                if gate_result.get("status") not in ("ok", "skip"):
                    raise RuntimeError(f"评测门禁未通过：{step.get('name') or gtype}"
                                       f"（状态 {gate_result.get('status')!r}）")
                trace[-1].update(status=gate_result['status'], detail=gate_result.get('log', ''),
                                 command=gate_result.get('cmd', ''))
                worker._git_retry(workdir, "checkout", "--detach", "-f", case["head_sha"])
                worker._git_retry(workdir, "clean", "-fdx")
                built = prompts.build(workdir, intent=case["intent"] or "",
                                      profile=project["profile"] or "", diff_spec=diff_spec)
                built, _ = improvements.attach(built, items, diff_files)
                diff_text = built["diff_file"].read_text(encoding="utf-8")
                continue
            if step.get("kind") == "review":
                members = [m for m in step.get("reviewers", []) if m.get("enabled", True)]
                stage("并行交叉评审")
                group_trace = trace[-1]
                group_outputs = [None] * len(members)
                group_details = [None] * len(members)
                errors = []
                for offset in range(0, len(members), config.REVIEW_CONCURRENCY):
                    batch = members[offset:offset + config.REVIEW_CONCURRENCY]
                    entries = [{"name": "独立评审 · " + (m.get("name") or m.get("id", "评审")),
                                "status": "running"} for m in batch]
                    trace.extend(entries)
                    if progress:
                        progress("并行交叉评审", trace)
                    with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="hub-eval-review") as pool:
                        futures = {}
                        part_lock = threading.Lock()
                        for i, member in enumerate(batch):
                            states = {}
                            def part_progress(index, total, status, entry=entries[i], states=states):
                                with part_lock:
                                    states[index] = status
                                    entry['part'] = {'index':index,'total':total,'status':status,
                                                     'done':sum(s=='ok' for s in states.values()),
                                                     'running':[i for i,s in states.items() if s=='running'],
                                                     'failed':[i for i,s in states.items() if s=='fail'],
                                                     'queued':sum(s=='queued' for s in states.values())}
                                    if progress:
                                        progress(f"{entry['name']} · 分片完成 {entry['part']['done']}/{total}", trace)
                            futures[pool.submit(worker._run_reviewer_step, member, project, workdir,
                                                {**built, 'part_progress':part_progress}, diff_text,
                                                [dict(item) for item in prior])] = i
                        for future in as_completed(futures):
                            i = futures[future]
                            name = batch[i].get("name") or batch[i].get("id", "评审")
                            try:
                                text, model_trace, actual_input = future.result()
                                if verdict.extract_findings(text) is None:
                                    raise worker._unparsed_review_error(text,model_trace,actual_input)
                            except Exception as exc:
                                entries[i]["status"] = "fail"
                                entries[i]['trace'] = getattr(exc, 'trace', None)
                                if getattr(exc,'output',None) is not None:
                                    group_details[offset+i]={'name':name+' · 未解析的原始片段','output':exc.output,
                                        'incomplete':True,'evidence_id':improvements.save_review_evidence(
                                            exc.output,getattr(exc,'trace',[]),getattr(exc,'input_evidence',''))}
                                errors.append(f"{name}: {exc}")
                            else:
                                entries[i]["status"] = "ok"
                                group_outputs[offset + i] = (name, text)
                                group_details[offset + i] = {'name':name,'output':text,
                                    'evidence_id':improvements.save_review_evidence(text,model_trace,actual_input)}
                            if progress:
                                progress("并行交叉评审", trace)
                group_trace["status"] = "fail" if errors else "ok"
                base['reviews'] = review_details + [d for d in group_details if d is not None]
                if errors:
                    partial = [out for out in group_outputs if out is not None]
                    if partial:
                        base['actual'] = verdict.merge_multi(outs + partial)['findings']
                    raise RuntimeError("交叉评审有成员失败；" + "；".join(errors))
                outs.extend(group_outputs)
                review_details.extend(group_details)
                continue
            stage("上游上下文 · " + (step.get("name") or step.get("id", "分析")))
            text, _, _ = worker._run_reviewer_step(step, project, workdir, built, diff_text, prior)
            name = step.get("name") or step.get("id", "评审")
            prior.append({"name": name, "status": "ok", "output": text})
        stage("合并评审发现")
        if not outs:
            raise RuntimeError("评测没有有效的评审器输出，不能判定通过")
        merged = verdict.merge_multi(outs)
        base['reviews'] = review_details
        stage("与人工答案比对")
    except Exception as e:
        running = [entry for entry in trace if entry["status"] == "running"]
        if running:
            for entry in running:
                entry["status"] = "fail"
        elif not any(entry["status"] == "fail" for entry in trace):
            trace.append({"name": "评测失败", "status": "fail"})
        return {**base, "result": "error", "detail": str(e)[-300:], "steps": trace}
    finally:
        if "parent" in locals():
            shutil.rmtree(parent, ignore_errors=True)
    try:
        expected = json.loads(case["expected"])
        got_list = [f for f in merged["findings"] if verdict.similar(f, expected)]
        actual = [{"severity": f.get("severity"), "file": f.get("file"),
                   "line": f.get("line"), "title": f.get("title"),
                   "condition": f.get("condition"), "fix": f.get("fix")}
                  for f in merged["findings"]]
    except Exception as e:
        trace[-1]["status"] = "fail"
        return {**base, "result": "error", "detail": f"人工答案比对失败：{e}"[-300:], "steps": trace}
    if case["polarity"] == "positive":
        trace[-1]["status"] = "ok"
        return {**base, "result": "hit" if got_list else "miss",
                "detail": f"共报 {len(merged['findings'])} 条；匹配预期 {len(got_list)} 条",
                "actual": actual, "steps": trace}
    trace[-1]["status"] = "ok"
    return {**base, "result": "hit" if not got_list else "fp",
            "detail": "负例通过：未重现相似发现" if not got_list else "误报重现：出现相似发现",
            "actual": actual, "steps": trace}
