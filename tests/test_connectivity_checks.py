"""检测结果必须来自真实调用路径，且不会把密钥或旧状态当作可用。"""
import tempfile
import threading
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, connections, db, diagnostics, main
from app.engine import api_nodes, reviewers


class ConnectivityChecksTest(unittest.TestCase):
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

    def tearDown(self):
        if getattr(db._local, "conn", None):
            db._local.conn.close()
            db._local.conn = None
        config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR = self.old_db, self.old_data, self.old_secrets
        self.tmp.cleanup()

    def test_api_probe_records_success_and_expires_after_rotation(self):
        cid = api_nodes.create("team", "openai", "test-secret")
        with patch.object(api_nodes, "run", return_value="OK") as runner:
            result = diagnostics.check_api(cid, "test-model")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(runner.call_args.kwargs, {"timeout": 60, "max_tokens": 32})
        self.assertEqual(runner.call_args.args[0], {"credential_id": cid, "model": "test-model"})
        self.assertEqual(diagnostics.list_checks()[0]["model"], "test-model")
        main.rotate_model_credential(cid, {"key": "replacement-secret"})
        self.assertEqual(diagnostics.list_checks(), [])

    def test_failed_api_probe_redacts_key_and_survives_refresh(self):
        cid = api_nodes.create("team", "openai", "test-secret")
        with patch.object(api_nodes, "run", side_effect=RuntimeError("bad test-secret response")):
            result = diagnostics.check_api(cid, "test-model")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("test-secret", str(result))
        self.assertNotIn("test-secret", str(diagnostics.list_checks()))

    def test_agent_probe_uses_temp_dir_and_bounded_cli_call(self):
        with patch.object(reviewers, "run_codex", return_value=("OK", [])) as runner:
            result = diagnostics.check_agent("codex")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(runner.call_args.kwargs, {"timeout": 60, "skip_git_check": True})
        workdir, prompt = runner.call_args.args
        self.assertEqual(prompt.name, "prompt.txt")
        self.assertFalse(workdir.exists())
        self.assertEqual(diagnostics.list_checks()[0]["target"], "agent:codex")

    def test_same_target_cannot_start_twice(self):
        cid = api_nodes.create("team", "openai", "test-secret")
        entered = threading.Event()
        release = threading.Event()
        def slow_run(*_args, **_kwargs):
            entered.set()
            release.wait(2)
            return "OK"
        with patch.object(api_nodes, "run", side_effect=slow_run):
            thread = threading.Thread(target=lambda: diagnostics.check_api(cid, "test-model"))
            thread.start()
            self.assertTrue(entered.wait(2))
            with self.assertRaises(diagnostics.CheckBusy):
                diagnostics.check_api(cid, "test-model")
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_codex_uses_saved_model_endpoint_for_probe_and_review(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("OK")
        db.set_setting("kimi_api_key", "test-key")
        db.set_setting("kimi_model", "saved-model")
        db.set_setting("kimi_base_url", "https://api.example.org/v1")
        with patch.object(config, "CODEX_HOME", self.root / "codex-home"), \
             patch.object(config, "CODEX_BIN", "/usr/bin/codex"), \
             patch.object(reviewers, "run_grouped", return_value=subprocess.CompletedProcess([], 0,
                 '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}', "")) as runner:
            result, _trace = reviewers.run_codex(self.root, prompt, skip_git_check=True)
            cfg = (self.root / "codex-home" / "config.toml").read_text()
        self.assertEqual(result, "OK")
        self.assertIn('model = "saved-model"', cfg)
        self.assertIn('base_url = "https://api.example.org/v1"', cfg)
        self.assertIn("--skip-git-repo-check", runner.call_args.args[0])
