"""失败待办状态与评测进度的可恢复边界。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import config, connections, db, evaluator, main, worker
from app.engine import gates, prompts


class WorkbenchQualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db, self.old_data, self.old_secrets = config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR
        config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR = (
            self.root / "hub.db", self.root, self.root / "secrets")
        if getattr(db._local, "conn", None):
            db._local.conn.close()
        db._local.conn = None
        with patch.object(config, "GITHUB_TOKEN", ""):
            db.init()
        self.pid = db.execute(
            "INSERT INTO projects(kind,full_name,name,created_at) VALUES('github','owner/repo','repo',?)",
            (db.now(),))

    def tearDown(self):
        if getattr(db._local, "conn", None):
            db._local.conn.close()
            db._local.conn = None
        config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR = self.old_db, self.old_data, self.old_secrets
        self.tmp.cleanup()

    def test_failed_run_can_be_handled_and_reopened_without_changing_result(self):
        rid = db.execute("INSERT INTO runs(project_id,trigger,status,created_at,error) "
                         "VALUES(?,'manual','failed',?,'clone error')", (self.pid, db.now()))
        self.assertEqual([r["id"] for r in main.list_runs(status="failed", unhandled=True)], [rid])
        main.triage_failed_run(rid, main.FailureTriageIn(action="handled", note="网络故障已排查"))
        self.assertEqual(main.list_runs(status="failed", unhandled=True), [])
        report = main.get_run(rid)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failure_action"], "handled")
        self.assertEqual(report["failure_note"], "网络故障已排查")
        main.triage_failed_run(rid, main.FailureTriageIn(action="reopen"))
        self.assertEqual([r["id"] for r in main.list_runs(status="failed", unhandled=True)], [rid])
        self.assertEqual(len(main.get_run(rid)["failure_history"]), 2)
        with self.assertRaises(HTTPException):
            main.triage_failed_run(rid, main.FailureTriageIn(action="wrong"))

    def test_workbench_pending_count_includes_runs_beyond_old_300_limit(self):
        findings = json.dumps([{"severity": "IMPORTANT", "title": "真实问题", "sources": ["A", "B"]}])
        for _ in range(301):
            db.execute("INSERT INTO runs(project_id,trigger,status,findings_json,created_at) "
                       "VALUES(?,'push','blocked',?,?)", (self.pid, findings, db.now()))
        marks = main.list_marks()
        self.assertEqual(marks["stats"]["pending"], 301)
        self.assertEqual(len(marks["pending"]), 50)
        self.assertEqual(marks["pending_preview_limit"], 50)

    def test_eval_case_reports_stages_and_comparison(self):
        diff = self.root / "diff.patch"
        diff.write_text("+++ b/x.py\n+value = 1", encoding="utf-8")
        source_run = db.execute(
            "INSERT INTO runs(project_id,trigger,base_ref,head_sha,created_at) VALUES(?,'push','base123','abc123',?)",
            (self.pid, db.now()))
        case = {"id": 1, "project_id": self.pid, "run_id": source_run,
                "head_sha": "abc123", "intent": "fix",
                "polarity": "positive", "expected": json.dumps({"file": "x.py", "line": 10,
                                                       "title": "空指针错误"})}
        pipeline = [{"kind": "gate", "gate": "tests", "name": "测试门禁"},
                    {"kind": "review", "reviewers": [{"kind": "reviewer", "name": "A"}]}]
        answer = '```json\n{"findings":[{"severity":"Important","file":"x.py","line":10,"title":"空指针错误"}]}\n```'
        seen = []
        with patch.object(worker, "_ensure_clone"), patch.object(worker, "_git_retry"), \
             patch.object(worker, "get_pipeline", return_value=pipeline), \
             patch.object(prompts, "build", return_value={"diff_file": diff}), \
             patch.object(gates, "run_tests", return_value={"status": "ok", "log": "passed"}), \
             patch.object(worker, "_run_reviewer_step", return_value=(answer, None, "prompt")):
            result = evaluator._run_case(case, lambda stage, trace: seen.append(stage))
        self.assertEqual(result["result"], "hit")
        self.assertIn("检出原提交", seen)
        self.assertIn("独立评审 · A", seen)
        self.assertEqual(result["steps"][-1], {"name": "与人工答案比对", "status": "ok"})
        self.assertEqual(result["actual"][0]["title"], "空指针错误")

    def test_eval_replays_original_push_range(self):
        source_run = db.execute(
            "INSERT INTO runs(project_id,trigger,base_ref,head_sha,created_at) VALUES(?,'push','oldsha','newsha',?)",
            (self.pid, db.now()))
        case = {"id": 1, "project_id": self.pid, "run_id": source_run,
                "head_sha": "newsha", "intent": "", "polarity": "positive",
                "expected": json.dumps({"file": "x.py", "line": 1, "title": "问题"})}
        diff = self.root / "diff.patch"
        diff.write_text("+++ b/x.py\n+value = 1")
        observed = []
        def build(_workdir, **kwargs):
            observed.append(kwargs["diff_spec"])
            return {"diff_file": diff}
        with patch.object(worker, "_ensure_clone"), patch.object(worker, "_git_retry"), \
             patch.object(worker, "get_pipeline", return_value=[]), \
             patch.object(prompts, "build", side_effect=build):
            result = evaluator._run_case(case)
        self.assertEqual(observed, ["oldsha..newsha"])
        self.assertEqual(result["result"], "error")  # 没有评审器，不可误判为通过

    def test_eval_progress_persists_and_interrupted_run_is_visible(self):
        rid = db.execute("INSERT INTO eval_runs(status,cases_total,config_snapshot,created_at) VALUES('running',1,?,?)",
                         (evaluator._config_snapshot(), db.now()))
        case = {"id": 3, "expected": json.dumps({"title": "边界错误"})}
        observed = []
        def fake_case(c, progress):
            progress("独立评审 · A", [{"name": "独立评审 · A", "status": "running"}])
            observed.append(dict(db.get("SELECT current_case_id,current_stage,current_detail FROM eval_runs WHERE id=?", (rid,))))
            return {"case_id": c["id"], "result": "hit", "steps": [{"name": "独立评审 · A", "status": "ok"}]}
        evaluator._eval_lock.acquire()
        with patch.object(evaluator, "_run_case", side_effect=fake_case):
            evaluator._run_eval(rid, [case])
        self.assertEqual(observed[0], {"current_case_id": 3, "current_stage": "独立评审 · A", "current_detail": "边界错误"})
        self.assertEqual(db.get("SELECT status,current_case_id FROM eval_runs WHERE id=?", (rid,))["status"], "done")
        interrupted = db.execute("INSERT INTO eval_runs(status,cases_total,current_stage,created_at) "
                                 "VALUES('running',1,'检出原提交',?)", (db.now(),))
        evaluator.recover_interrupted()
        row = db.get("SELECT status,current_stage,error_msg FROM eval_runs WHERE id=?", (interrupted,))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["current_stage"], "检出原提交")

    def test_eval_configuration_drift_is_reported_as_failed(self):
        rid = db.execute("INSERT INTO eval_runs(status,cases_total,config_snapshot,created_at) VALUES('running',1,?,?)",
                         (evaluator._config_snapshot(), db.now()))
        case = {"id": 1, "expected": json.dumps({"title": "问题"})}
        def change_config(_case, _progress):
            db.set_setting("prompt_template", "changed during eval")
            return {"case_id": 1, "result": "hit", "steps": []}
        evaluator._eval_lock.acquire()
        with patch.object(evaluator, "_run_case", side_effect=change_config):
            evaluator._run_eval(rid, [case])
        row = db.get("SELECT status,error_msg FROM eval_runs WHERE id=?", (rid,))
        self.assertEqual(row["status"], "failed")
        self.assertIn("配置或样本已变化", row["error_msg"])


if __name__ == "__main__":
    unittest.main()
