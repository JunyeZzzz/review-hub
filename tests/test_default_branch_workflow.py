"""自动审查只由默认分支的完整 push 触发。"""
import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import config, connections, db, github, main, worker
from app.engine import gates, prompts


class DefaultBranchWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db, self.old_data, self.old_secrets = config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR
        if getattr(db._local, "conn", None):
            db._local.conn.close()
        db._local.conn = None
        config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR = (
            self.root / "hub.db", self.root, self.root / "secrets")
        with patch.object(config, "GITHUB_TOKEN", ""):
            db.init()
        self.before = "a" * 40
        self.after = "b" * 40
        self.pid = db.execute(
            "INSERT INTO projects(kind,full_name,name,default_branch,last_sha,created_at) "
            "VALUES('github','owner/repo','repo','main',?,?)", (self.before, db.now()))

    def tearDown(self):
        if getattr(db._local, "conn", None):
            db._local.conn.close()
            db._local.conn = None
        config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR = self.old_db, self.old_data, self.old_secrets
        self.tmp.cleanup()

    def test_webhook_ignores_pr_and_other_branches_then_queues_main_range(self):
        with patch.object(github, "verify_signature", return_value=True):
            def send(event, payload):
                class RequestStub:
                    headers = {"x-github-event": event}
                    async def body(self):
                        return json.dumps({"repository": {"full_name": "owner/repo"}, **payload}).encode()
                return asyncio.run(main.github_webhook(RequestStub()))
            send("pull_request", {"action": "opened", "pull_request": {"number": 3, "head": {"sha": self.after}}})
            send("push", {"ref": "refs/heads/feature", "before": self.before, "after": self.after})
            send("push", {"ref": "refs/heads/main", "before": "c" * 40, "after": "d" * 40})
            self.assertEqual(db.query("SELECT id FROM runs"), [])
            send("push", {"ref": "refs/heads/main", "before": self.before, "after": self.after})
            send("push", {"ref": "refs/heads/main", "before": self.before, "after": self.after})
            send("pull_request", {"action": "closed", "pull_request": {"merged": True}})
            send("push", {"ref": "refs/heads/main", "before": self.after, "after": "c" * 40})
        runs = db.query("SELECT trigger,ref,base_ref,head_sha FROM runs")
        self.assertEqual(len(runs), 2)
        self.assertEqual(dict(runs[0]), {"trigger": "push", "ref": "main",
                                         "base_ref": self.before, "head_sha": self.after})
        self.assertEqual((runs[1]["base_ref"], runs[1]["head_sha"]), (self.after, "c" * 40))

    def test_poll_uses_previous_default_branch_head_without_scanning_prs(self):
        with patch.object(connections, "token_for_project", return_value=None), \
             patch.object(github, "branch_sha", return_value=self.after), \
             patch.object(github, "open_pulls", side_effect=AssertionError("PR polling must be disabled")), \
             patch.object(worker, "prefetch_commits"):
            worker._poll_once()
        run = db.get("SELECT trigger,base_ref,head_sha FROM runs WHERE project_id=?", (self.pid,))
        self.assertEqual((run["trigger"], run["base_ref"], run["head_sha"]),
                         ("push", self.before, self.after))

    def test_new_webhook_subscribes_only_to_push(self):
        with patch.object(github, "gh") as request:
            request.return_value.json.return_value = {}
            github.attach_webhook("owner/repo", "https://hub.example", token="test-token")
        self.assertEqual(request.call_args.kwargs["json"]["events"], ["push"])

    def test_inactive_auto_revert_cannot_be_enabled(self):
        with self.assertRaises(HTTPException):
            main.update_project(self.pid, {"auto_revert": True})

    def test_push_diff_includes_all_commits_since_before(self):
        repo = self.root / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        (repo / "first.txt").write_text("first\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "first")
        before = git("rev-parse", "HEAD")
        (repo / "second.txt").write_text("second\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "second")
        (repo / "third.txt").write_text("third\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "third")
        after = git("rev-parse", "HEAD")
        base, head = worker._diff_range(repo, {"trigger": "push", "base_ref": before, "head_sha": after}, {})
        built = prompts.build(repo, diff_spec=f"{base}..{head}")
        diff = built["diff_file"].read_text()
        self.assertIn("second.txt", diff)
        self.assertIn("third.txt", diff)
        self.assertNotIn("first.txt", diff)
        intent = worker._intent_for_range(repo, {"trigger": "push", "base_ref": before},
                                          f"{before}..{after}", "third")
        self.assertIn("second", intent)
        self.assertIn("third", intent)
        self.assertLess(intent.index("second"), intent.index("third"))
        root_base, root_head = worker._diff_range(
            repo, {"trigger": "push", "base_ref": "", "head_sha": before}, {})
        root_diff = prompts.build(repo, diff_spec=f"{root_base}..{root_head}")["diff_file"].read_text()
        self.assertIn("first.txt", root_diff)

    def test_large_git_diff_keeps_last_line_and_creates_review_parts(self):
        repo = self.root / "large-repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        (repo / "code.py").write_text("base = 1\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
        before = git("rev-parse", "HEAD")
        (repo / "code.py").write_text("base = 1\n" +
                                      "".join(f"changed_line_{i} = {i}\n" for i in range(9001)))
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "large")
        after = git("rev-parse", "HEAD")
        built = prompts.build(repo, diff_spec=f"{before}..{after}")
        self.assertFalse(built["truncated"])
        self.assertGreater(built["diff_lines"], 8000)
        self.assertIn("changed_line_9000 = 9000", built["diff_file"].read_text())
        self.assertGreater(len(built["review_parts"]), 1)
        self.assertIn("changed_line_9000 = 9000", built["review_parts"][-1].read_text())

    def test_binary_change_cannot_receive_text_only_pass(self):
        repo = self.root / "binary-repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        (repo / "asset.bin").write_bytes(b"\0first")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "first")
        before = git("rev-parse", "HEAD")
        (repo / "asset.bin").write_bytes(b"\0second")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "second")
        after = git("rev-parse", "HEAD")
        with self.assertRaisesRegex(RuntimeError, "人工核验"):
            prompts.build(repo, diff_spec=f"{before}..{after}")

    def test_old_report_diff_is_labeled_unknown_until_new_run_proves_complete(self):
        rid = db.execute("INSERT INTO runs(project_id,trigger,diff_text,created_at) "
                         "VALUES(?,'push','legacy diff',?)", (self.pid, db.now()))
        self.assertFalse(main.run_diff(rid)["complete"])
        db.execute("UPDATE runs SET diff_text=?,diff_complete=1 WHERE id=?", ("complete diff", rid))
        self.assertEqual(main.run_diff(rid), {"diff": "complete diff", "complete": True})

    def test_gate_mutations_do_not_reach_model_review(self):
        repo = self.root / "mutable-repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        (repo / "code.py").write_text("value = 1\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
        (repo / "code.py").write_text("value = 2\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "change")
        head = git("rev-parse", "HEAD")
        pid = db.execute("INSERT INTO projects(kind,full_name,name,default_branch,created_at) "
                         "VALUES('local',?,?,?,?)", (str(repo), "mutable-repo", "main", db.now()))
        rid = worker.enqueue(pid, "manual", head_sha=head)
        observed = []
        def dirty_gate(workdir, **_kwargs):
            (workdir / "code.py").write_text("value = 'tampered'\n")
            (workdir / "generated.tmp").write_text("temporary")
            return {"status": "ok", "cmd": "fake test", "log": "passed"}
        def review(_step, _project, workdir, _built, _diff, _prior, _log):
            observed.append(((workdir / "code.py").read_text(), (workdir / "generated.tmp").exists()))
            return [("AI", '```json\n{"findings": []}\n```')]
        pipeline = [{"kind": "gate", "gate": "tests", "name": "test"},
                    {"kind": "review", "reviewers": [{"kind": "reviewer", "name": "AI"}]}]
        with patch.object(worker, "get_pipeline", return_value=pipeline), \
             patch.object(gates, "run_tests", side_effect=dirty_gate), \
             patch.object(worker, "_run_review_group", side_effect=review):
            worker.process_run(rid)
        self.assertEqual(db.get("SELECT status FROM runs WHERE id=?", (rid,))["status"], "passed")
        self.assertEqual(observed, [("value = 2\n", False)])


if __name__ == "__main__":
    unittest.main()
