"""自动审查只由默认分支的完整 push 触发。"""
import asyncio
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def test_profile_initialization_uses_managed_kimi_runner(self):
        db.set_setting('kimi_api_key', 'managed-test-key')
        profile='面向小团队的代码审查服务，使用 Python 与浏览器界面。'
        def run(workdir, prompt_file, **kwargs):
            self.assertTrue(prompt_file.is_file())
            self.assertIn('owner/repo', prompt_file.read_text())
            self.assertEqual(kwargs, {'timeout':300,'skip_git_check':True})
            return profile, []
        with patch.object(config,'KIMI_API_KEY',''), \
             patch.object(connections,'token_for_project',return_value='test-token'), \
             patch.object(github,'repo_info',return_value={'default_branch':'main'}), \
             patch.object(github,'readme_text',return_value='Hub'), \
             patch.object(worker.reviewers,'run_codex',side_effect=run) as call:
            worker.process_init(self.pid)
        row=db.get('SELECT profile,init_status FROM projects WHERE id=?',(self.pid,))
        self.assertEqual(row['init_status'],'done')
        self.assertEqual(row['profile'],profile)
        call.assert_called_once()

    def test_profile_initialization_rejects_overlong_output_without_silent_cut(self):
        with patch.object(connections,'token_for_project',return_value='test-token'), \
             patch.object(github,'repo_info',return_value={'default_branch':'main'}), \
             patch.object(github,'readme_text',return_value='Hub'), \
             patch.object(worker.reviewers,'run_codex',return_value=('中'*601, [])):
            worker.process_init(self.pid)
        row=db.get('SELECT profile,init_status FROM projects WHERE id=?',(self.pid,))
        self.assertEqual(row['init_status'],'failed')
        self.assertIn('超过600字符',row['profile'])

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

    def test_secret_scan_block_keeps_important_evidence(self):
        repo = self.root / "secret-repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        (repo / "safe.txt").write_text("safe\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "first")
        before = git("rev-parse", "HEAD")
        (repo / "leak.txt").write_text("aws_access_key_id=" + "AKIA" + "IOSFODNN7EXAMPLE" + "\n")
        git("add", ".")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "leak")
        after = git("rev-parse", "HEAD")
        rid = db.execute(
            "INSERT INTO runs(project_id,trigger,ref,base_ref,head_sha,status,created_at) "
            "VALUES(?,?,?,?,?,'queued',?)", (self.pid, "push", "main", before, after, db.now()))
        with patch.object(worker, "_prepare", return_value=repo):
            worker._process_run(db.get("SELECT * FROM runs WHERE id=?", (rid,)),
                                db.get("SELECT * FROM projects WHERE id=?", (self.pid,)))
        result = db.get("SELECT status,important_count,findings_json FROM runs WHERE id=?", (rid,))
        self.assertEqual((result["status"], result["important_count"]), ("blocked", 1))
        self.assertEqual(json.loads(result["findings_json"])[0]["severity"], "IMPORTANT")

    def test_human_review_names_and_readme_404(self):
        reviews = [
            {"user": {"login": "claude-agent", "type": "User"}, "state": "APPROVED"},
            {"user": {"login": "github-actions[bot]", "type": "Bot"}, "state": "APPROVED"},
        ]
        classified = github.classify_reviews(reviews)
        self.assertEqual(classified["human_approved"], ["claude-agent"])
        self.assertEqual(classified["agent_approved"], 1)
        with patch.object(github, "gh", side_effect=RuntimeError("GitHub GET /readme -> 404: missing")) as request:
            self.assertEqual(github.readme_text("owner/repo", "main"), "")
            request.assert_called_once()

    def test_utc_cache_and_local_day_boundary(self):
        now = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
        self.assertTrue(main._approval_cache_fresh("2026-10-29T07:56:00.000Z", now))
        self.assertFalse(main._approval_cache_fresh("2026-10-29T07:54:00.000Z", now))
        self.assertFalse(main._approval_cache_fresh("2026-10-29T08:01:00.000Z", now))
        self.assertFalse(main._approval_cache_fresh("bad", now))
        boundary = main._db_utc_timestamp(now - timedelta(hours=8))
        db.execute("INSERT INTO runs(project_id,trigger,status,created_at) VALUES(?,'manual','passed',?)",
                   (self.pid, "2026-10-28T23:59:59.000Z"))
        db.execute("INSERT INTO runs(project_id,trigger,status,created_at) VALUES(?,'manual','passed',?)",
                   (self.pid, boundary))
        with patch.object(main, "_local_day_start_utc", return_value=boundary):
            self.assertEqual(main.overview()["stats"]["runs_today"], 1)

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
        manual_base, manual_head = worker._diff_range(
            repo, {"trigger": "manual", "base_ref": before, "head_sha": after}, {})
        self.assertEqual((manual_base, manual_head), (before, after))
        manual_diff = prompts.build(repo, diff_spec=f"{manual_base}..{manual_head}")["diff_file"].read_text()
        self.assertIn("second.txt", manual_diff)
        self.assertIn("third.txt", manual_diff)
        manual_intent = worker._intent_for_range(
            repo, {"trigger": "manual", "base_ref": before}, f"{before}..{after}", "third")
        self.assertIn("second", manual_intent)
        self.assertIn("third", manual_intent)
        root_base, root_head = worker._diff_range(
            repo, {"trigger": "push", "base_ref": "", "head_sha": before}, {})
        root_diff = prompts.build(repo, diff_spec=f"{root_base}..{root_head}")["diff_file"].read_text()
        self.assertIn("first.txt", root_diff)
        tree_intent = worker._intent_for_range(
            repo, {"trigger": "manual", "base_ref": root_base},
            f"{root_base}..{after}", "third")
        self.assertIn("完整代码差异", tree_intent)
        self.assertNotIn("third", tree_intent)
        # A force-reset removes commits yet still changes the reviewed tree.
        rollback_intent = worker._intent_for_range(
            repo, {"trigger": "push", "base_ref": after}, f"{after}..{before}", "first")
        self.assertIn("回退", rollback_intent)
        self.assertIn("移除", rollback_intent)
        self.assertIn("second", rollback_intent)
        self.assertIn("third", rollback_intent)
        rollback_diff = prompts.build(repo, diff_spec=f"{after}..{before}")["diff_file"].read_text()
        self.assertIn("deleted file", rollback_diff)
        with self.assertRaises(RuntimeError):
            worker._intent_for_range(repo, {"trigger": "push", "base_ref": before},
                                     f"{before}..{before}", "first")

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
        self.assertEqual(built['review_part_paths'], [['code.py']] * len(built['review_parts']))

    def test_part_scope_preserves_git_paths_for_rename_unicode_and_deleted_files(self):
        repo = self.root / 'path-repo'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', *args], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git('init', '-b', 'main')
        (repo / 'old.txt').write_text('rename this\n')
        (repo / 'deleted.txt').write_text('removed\n')
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'base')
        before = git('rev-parse', 'HEAD')
        (repo / 'old.txt').rename(repo / '新 名称.txt')
        (repo / 'deleted.txt').unlink()
        (repo / 'space name.py').write_text(''.join(f'line{i}\n' for i in range(25)))
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '-m', 'change')
        with patch.object(prompts, 'MAX_REVIEW_PART_LINES', 10):
            built = prompts.build(repo, diff_spec=f'{before}..HEAD')
        self.assertEqual(built['changed_files'], ['deleted.txt', 'space name.py', '新 名称.txt'])
        self.assertEqual({p for paths in built['review_part_paths'] for p in paths},
                         set(built['changed_files']))
        self.assertEqual(built['review_part_paths'][0], ['deleted.txt', 'space name.py'])
        self.assertEqual(built['review_part_paths'][-1][-1], '新 名称.txt')

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
