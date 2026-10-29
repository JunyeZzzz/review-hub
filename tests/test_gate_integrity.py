"""门禁不得把未执行、未扫描或不完整响应当成通过。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.engine import gates
from app import config, git_auth, processes, worker


class GateIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_declared_gate_fails_when_runner_is_missing(self):
        with patch.object(gates, "_run_host", side_effect=FileNotFoundError()):
            result = gates._exec(["pytest", "-q"], self.root, 1)
        self.assertEqual(result["status"], "fail")
        self.assertIn("无法执行", result["log"])

    def test_host_gate_runs_without_docker_or_opt_in(self):
        self.assertEqual(worker.DEFAULT_GATE["sandbox"], "off")
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(gates, "_exec", return_value={"status": "ok", "log": "ran"}) as runner:
            result = gates._run_sandboxed(["make", "test"], self.root, 1, {"sandbox": "off"}, "", "测试")
        self.assertEqual(result["status"], "ok")
        runner.assert_called_once()

    def test_concurrent_git_auth_uses_complete_script_and_per_process_token(self):
        with patch.object(config, "DATA_DIR", self.root):
            with ThreadPoolExecutor(max_workers=8) as pool:
                envs = list(pool.map(lambda n: git_auth.auth_env(f"token-{n}"), range(20)))
        script = self.root / "git-askpass.sh"
        self.assertEqual(script.read_text(), git_auth.ASKPASS_BODY)
        self.assertEqual(script.stat().st_mode & 0o777, 0o700)
        for n, env in enumerate(envs):
            result = subprocess.run([str(script), "Password for github"], env=env,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout, f"token-{n}")

    def test_concurrent_projects_have_distinct_home_tmp_and_working_dirs(self):
        projects = [self.root / "project-a", self.root / "project-b"]
        for project in projects:
            project.mkdir()
        code = ("import json, os, pathlib, tempfile, time; "
                "pathlib.Path('environment.json').write_text(json.dumps({"
                "'cwd': os.getcwd(), 'home': os.environ['HOME'], "
                "'tmp': os.environ['TMPDIR'], 'cache': os.environ['XDG_CACHE_HOME']})); "
                "time.sleep(0.2)")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda path: gates._exec([sys.executable, "-c", code], path, 5), projects))
        self.assertEqual([r["status"] for r in results], ["ok", "ok"])
        envs = [json.loads((path / "environment.json").read_text()) for path in projects]
        for field in ("cwd", "home", "tmp", "cache"):
            self.assertNotEqual(envs[0][field], envs[1][field])
        self.assertEqual([Path(e["cwd"]).resolve() for e in envs], [p.resolve() for p in projects])
        self.assertFalse(Path(envs[0]["home"]).exists())
        self.assertFalse(Path(envs[1]["home"]).exists())

    def test_two_projects_run_real_test_gates_in_parallel_without_docker(self):
        projects = [self.root / "gate-a", self.root / "gate-b"]
        for project in projects:
            project.mkdir()
            (project / "Makefile").write_text("test:\n\t@echo passed > result.txt\n")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(gates.run_tests, projects))
        self.assertEqual([r["status"] for r in results], ["ok", "ok"])
        self.assertEqual([(p / "result.txt").read_text().strip() for p in projects],
                         ["passed", "passed"])

    def test_timeout_kills_child_process_in_own_group(self):
        marker = self.root / "escaped.txt"
        child = f"import time,pathlib; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('late')"
        parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(5)"
        result = gates._exec([sys.executable, "-c", parent], self.root, 0.15)
        self.assertEqual(result["status"], "fail")
        time.sleep(1)
        self.assertFalse(marker.exists())

    def test_gate_cannot_pass_with_detached_background_child(self):
        marker = self.root / "background.txt"
        child = f"import time,pathlib; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('late')"
        parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
        result = gates._exec([sys.executable, "-c", parent], self.root, 2)
        self.assertEqual(result["status"], "fail")
        self.assertIn("后台子进程", result["log"])
        time.sleep(1)
        self.assertFalse(marker.exists())

    def test_excessive_output_fails_instead_of_using_prefix(self):
        with self.assertRaisesRegex(processes.OutputLimitExceeded, "不能截断后判定通过"):
            processes.run_grouped([sys.executable, "-c", "print('x' * 3000)"],
                                  cwd=self.root, env=os.environ.copy(), timeout=5,
                                  max_output_bytes=1000)

    def test_lockfile_uses_strict_npm_ci_without_fallback(self):
        (self.root / "package.json").write_text('{}')
        self.assertEqual(gates._install_cmd(self.root), ["npm", "install", "--no-audit", "--no-fund"])
        (self.root / "package-lock.json").write_text('{}')
        self.assertEqual(gates._install_cmd(self.root), ["npm", "ci", "--no-audit", "--no-fund"])

    def test_secret_scan_covers_large_changed_file_and_fine_grained_token(self):
        path = self.root / "large.txt"
        path.write_text("x" * 1_100_000 + "\ngithub_pat_" + "A" * 50)
        result = gates.scan_secrets(self.root, ["large.txt"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["findings"][0]["rule"], "GitHub Token")
        with patch.object(gates, "MAX_SECRET_FILE_BYTES", 100):
            with self.assertRaisesRegex(RuntimeError, "不能判定通过"):
                gates.scan_secrets(self.root, ["large.txt"])

    def test_semgrep_scans_beyond_first_three_hundred_files(self):
        files = [f"file-{i}.py" for i in range(301)]
        for name in files:
            (self.root / name).write_text("pass\n")
        seen = []
        def fake_run(cmd, *_args):
            seen.extend(cmd[cmd.index("--") + 1:])
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"results": [], "errors": []}), "")
        with patch.object(gates, "semgrep_available", return_value=True), \
             patch.object(gates, "_run_host", side_effect=fake_run):
            result = gates.run_semgrep(self.root, files)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(seen, files)

    def test_semgrep_missing_results_or_scan_error_cannot_pass(self):
        for data in ({}, {"results": [], "errors": [{"message": "parse error"}]}):
            result = gates._parse_semgrep({"status": "ok", "returncode": 0,
                                           "stdout": json.dumps(data)}, "scan")
            self.assertEqual(result["status"], "fail")

    def test_semgrep_block_keeps_each_structured_finding(self):
        recorded = []
        findings = [{"severity": "IMPORTANT", "file": "a.py", "line": 3,
                     "rule": "security.rule-a", "condition": "unsafe call"},
                    {"severity": "IMPORTANT", "file": "b.py", "line": 9,
                     "rule": "security.rule-b", "condition": "unsafe call"}]
        with patch.object(worker, "_finish", side_effect=lambda *args, **kwargs: recorded.append(kwargs)):
            worker._block_by_gate({"kind": "local"}, {"id": 1}, "安全门禁", "semgrep",
                                  "semgrep", "scan log", findings)
        self.assertEqual([f["file"] for f in recorded[0]["findings"]], ["a.py", "b.py"])

    def test_nonblocking_semgrep_findings_reach_worker(self):
        finding = {"severity": "NIT", "file": "a.py", "line": 4, "rule": "warning"}
        class Log:
            def start(self, _name): return 0
            def finish(self, *_args, **_kwargs): pass
        with patch.object(gates, "run_semgrep", return_value={
                "status": "ok", "log": "one warning", "findings": [finding]}):
            blocked, status, _, found = worker._run_gate(
                {"kind": "gate", "gate": "semgrep"}, {"id": 1}, {"id": 1}, self.root, ["a.py"], Log())
        self.assertFalse(blocked)
        self.assertEqual(status, "ok")
        self.assertEqual(found, [finding])

    def test_unknown_gate_or_result_status_blocks_run(self):
        class Log:
            def start(self, _name): return 0
            def finish(self, *_args, **_kwargs): pass
        recorded = []
        with patch.object(worker, "_block_by_gate", side_effect=lambda *a: recorded.append(a)):
            blocked, status, _, _ = worker._run_gate(
                {"kind": "gate", "gate": "unknown"}, {"id": 1}, {"id": 1}, self.root, [], Log())
            self.assertTrue(blocked)
            self.assertEqual(status, "fail")
            with patch.object(gates, "run_tests", return_value={"status": "unverified", "log": "?"}):
                blocked, status, _, _ = worker._run_gate(
                    {"kind": "gate", "gate": "tests"}, {"id": 1}, {"id": 1}, self.root, [], Log())
        self.assertTrue(blocked)
        self.assertEqual(status, "fail")
        self.assertEqual(len(recorded), 2)


if __name__ == "__main__":
    unittest.main()
