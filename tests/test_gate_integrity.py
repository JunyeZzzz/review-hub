"""门禁不得把未执行、未扫描或不完整响应当成通过。"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.engine import gates
from app import worker


class GateIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_declared_gate_fails_when_runner_is_missing(self):
        with patch.object(gates.subprocess, "run", side_effect=FileNotFoundError()):
            result = gates._exec(["pytest", "-q"], self.root, 1)
        self.assertEqual(result["status"], "fail")
        self.assertIn("无法执行", result["log"])

    def test_host_gate_requires_explicit_trust_opt_in(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(gates, "_exec") as runner:
            result = gates._run_sandboxed(["make", "test"], self.root, 1, {"sandbox": "off"}, "", "测试")
        self.assertEqual(result["status"], "fail")
        runner.assert_not_called()
        with patch.dict(os.environ, {"HUB_ALLOW_HOST_GATES": "1"}), \
             patch.object(gates, "_exec", return_value={"status": "ok", "log": "ran"}) as runner:
            result = gates._run_sandboxed(["make", "test"], self.root, 1, {"sandbox": "off"}, "", "测试")
        self.assertEqual(result["status"], "ok")
        runner.assert_called_once()

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
        def fake_run(cmd, **kwargs):
            seen.extend(cmd[cmd.index("--") + 1:])
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"results": [], "errors": []}), "")
        with patch.object(gates, "semgrep_available", return_value=True), \
             patch.object(gates.subprocess, "run", side_effect=fake_run):
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
