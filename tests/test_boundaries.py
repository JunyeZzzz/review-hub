"""关键边界回归：凭据归属、输出判定、本地仓库只读。"""
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from app import config, connections, db, github, main, worker
from app.engine import verdict


class BoundariesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_db = config.DB_PATH
        self.original_data = config.DATA_DIR
        self.original_secrets = connections.SECRET_DIR
        config.DB_PATH = self.root / "hub.db"
        config.DATA_DIR = self.root
        connections.SECRET_DIR = self.root / "secrets"
        db._local.conn = None
        with patch.object(config, "GITHUB_TOKEN", ""):
            db.init()

    def tearDown(self):
        if getattr(db._local, "conn", None):
            db._local.conn.close()
            db._local.conn = None
        config.DB_PATH = self.original_db
        config.DATA_DIR = self.original_data
        connections.SECRET_DIR = self.original_secrets
        self.tmp.cleanup()

    def test_static_endpoint_serves_only_files_inside_its_root(self):
        web=self.root/'web';(web/'app').mkdir(parents=True);assets=web/'static';assets.mkdir()
        (assets/'app.js').write_text('public asset')
        (web/'secret.txt').write_text('private fixture')
        (assets/'outside.js').symlink_to(web/'secret.txt')
        with patch.object(main,'__file__',str(web/'app/main.py')):
            client=TestClient(main.app,raise_server_exceptions=False)
            response=client.get('/static/app.js')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.text,'public asset')
            self.assertIn('no-cache',response.headers['cache-control'])
            for path in ['/static/%2e%2e','/static/outside.js','/static/%2e%2e%2fsecret.txt']:
                response=client.get(path)
                self.assertEqual(response.status_code,404,path)
                self.assertNotIn('private fixture',response.text)

    def test_credentials_are_explicit_and_never_listed(self):
        a = connections.create("team-a", "secret-a")
        b = connections.create("team-b", "secret-b")
        self.assertEqual(connections.token_for_id(a), "secret-a")
        self.assertEqual(connections.token_for_id(b), "secret-b")
        self.assertIsNone(connections.token_for_id(None))
        self.assertNotIn("secret-a", str(connections.list_public()))
        self.assertNotIn("secret-b", str(connections.list_public()))
        self.assertEqual((connections.SECRET_DIR / db.get(
            "SELECT token_ref FROM github_connections WHERE id=?", (a,))["token_ref"][5:]).stat().st_mode & 0o777, 0o600)
        connections.rotate(a, "new-a")
        self.assertEqual(connections.token_for_id(a), "new-a")
        self.assertEqual(connections.token_for_id(b), "secret-b")

    def test_invalid_reviewer_result_cannot_mean_clean(self):
        self.assertIsNone(verdict.extract_findings("review complete"))
        self.assertIsNone(verdict.extract_findings('```json\n{"findings": "none"}\n```'))
        self.assertEqual(verdict.extract_findings('```json\n{"findings": []}\n```'), [])

    def test_project_binding_does_not_fall_back_to_another_connection(self):
        a = connections.create("team-a", "secret-a")
        b = connections.create("team-b", "secret-b")
        pid = main._upsert_project(main.ProjectIn(full_name="example/repo", github_connection_id=a))
        project = db.get("SELECT * FROM projects WHERE id=?", (pid,))
        self.assertEqual(connections.token_for_project(project), "secret-a")
        with patch.object(github, "repo_info", return_value={"default_branch": "main"}):
            main.update_project(pid, {"github_connection_id": b})
        project = db.get("SELECT * FROM projects WHERE id=?", (pid,))
        self.assertEqual(connections.token_for_project(project), "secret-b")
        db.execute("UPDATE github_connections SET disabled=1 WHERE id=?", (b,))
        with self.assertRaises(ValueError):
            connections.token_for_project(project)

    def test_public_mode_requires_admin_and_webhook_signature(self):
        with patch.object(config, "PUBLIC_BASE_URL", "https://hub.example"), \
             patch.dict("os.environ", {"HUB_ADMIN_PASSWORD": "test-password"}):
            client = TestClient(main.app)
            self.assertEqual(client.get("/api/projects").status_code, 401)
            self.assertEqual(client.get("/api/projects", auth=("admin", "test-password")).status_code, 200)
            with patch.object(config, "WEBHOOK_SECRET", ""):
                self.assertFalse(github.verify_signature(b"{}", ""))

    def test_local_prepare_preserves_source_checkout_and_untracked_file(self):
        source = self.root / "source"
        source.mkdir()
        def git(*args):
            return subprocess.run(["git", *args], cwd=source, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        (source / "main.txt").write_text("original\n")
        git("add", "main.txt")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
        sha = git("rev-parse", "HEAD")
        (source / "scratch.txt").write_text("keep me")
        class Log:
            def start(self, _): return 0
            def finish(self, *_): pass
        copy = worker._prepare({"kind": "local", "full_name": str(source)},
                               {"id": 42, "head_sha": sha}, Log())
        self.assertNotEqual(copy, source)
        self.assertTrue((source / "scratch.txt").exists())
        self.assertEqual(git("branch", "--show-current"), "main")


if __name__ == "__main__":
    unittest.main()
