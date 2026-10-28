"""节点编排、上下文和模型 API 凭证的边界测试。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from app import config, connections, db, main, worker
from app.engine import api_nodes, verdict


class PipelineNodesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_db, self.old_data, self.old_secrets = config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR
        config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR = (
            self.root / "hub.db", self.root, self.root / "secrets")
        db._local.conn = None
        with patch.object(config, "GITHUB_TOKEN", ""):
            db.init()

    def tearDown(self):
        if getattr(db._local, "conn", None):
            db._local.conn.close()
            db._local.conn = None
        config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR = self.old_db, self.old_data, self.old_secrets
        self.tmp.cleanup()

    def test_api_credential_is_explicit_and_secret_stays_private(self):
        cid = api_nodes.create("team-openai", "openai", "secret-123")
        self.assertNotIn("secret-123", str(api_nodes.list_public()))
        steps = main._clean_pipeline([{"id": "g", "kind": "gate", "gate": "tests"},
                                      {"id": "a", "kind": "api", "credential_id": cid,
                                       "name": "API review", "model": "test-model"}])
        self.assertEqual(steps[1]["kind"], "review")
        self.assertEqual(steps[1]["reviewers"][0]["credential_id"], cid)
        self.assertNotIn("secret-123", json.dumps(steps))
        with self.assertRaises(HTTPException):
            main._clean_pipeline([{"kind": "api", "credential_id": cid + 1, "model": "test-model"}])
        api_nodes.rotate(cid, "new-secret")
        self.assertNotIn("new-secret", str(api_nodes.list_public()))

    def test_node_prompt_contains_every_prior_result_and_fails_when_over_budget(self):
        prompt = self.root / "prompt.txt"
        diff = self.root / "diff.patch"
        prompt.write_text("审查基础指令")
        diff.write_text("diff --git a/x b/x")
        built = {"prompt_file": prompt, "diff_file": diff}
        prior = [{"name": "需求分析", "status": "ok", "output": "业务目标 A"},
                 {"name": "风险分析", "status": "ok", "output": "边界条件 B"}]
        result = worker._node_prompt({"kind": "reviewer", "purpose": "findings", "prompt": "检查回归"},
                                     built, prior, self.root)
        self.assertIn("业务目标 A", result)
        self.assertIn("边界条件 B", result)
        self.assertLess(result.index("业务目标 A"), result.index("边界条件 B"))
        with patch.object(worker, "MAX_NODE_PROMPT_CHARS", 10):
            with self.assertRaisesRegex(RuntimeError, "超过"):
                worker._node_prompt({"kind": "reviewer"}, built, prior, self.root)

    def test_api_node_receives_diff_and_all_prior_outputs(self):
        prompt = self.root / "prompt.txt"
        diff = self.root / "diff.patch"
        prompt.write_text("审查基础指令")
        diff.write_text("diff --git a/x b/x\n+def changed(): pass")
        built = {"prompt_file": prompt, "diff_file": diff}
        prior = [{"name": "分析", "status": "ok", "output": "发现边界 B"}]
        with patch.object(api_nodes, "run", return_value='```json\n{"findings": []}\n```') as run:
            text, trace, actual_prompt = worker._run_reviewer_step(
                {"kind": "api", "credential_id": 1, "model": "model"}, {}, self.root, built, "", prior)
        self.assertIsNone(trace)
        self.assertIn("findings", text)
        self.assertIn("发现边界 B", actual_prompt)
        self.assertIn("+def changed", actual_prompt)
        self.assertEqual(run.call_args.args[1], actual_prompt)

    def test_chat_api_node_uses_bound_key_and_parses_text(self):
        cid = api_nodes.create("team-openai", "openai", "secret-123")
        observed = {}
        def handler(request):
            observed["auth"] = request.headers["authorization"]
            observed["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n{\"findings\": []}\n```"}}]})
        client_class = httpx.Client
        with patch.object(api_nodes.httpx, "Client", side_effect=lambda **kw: client_class(
                transport=httpx.MockTransport(handler), **kw)):
            text = api_nodes.run({"credential_id": cid, "model": "test-model"}, "full context")
        self.assertIn("findings", text)
        self.assertEqual(observed["auth"], "Bearer secret-123")
        self.assertEqual(observed["body"]["messages"][0]["content"], "full context")

    def test_anthropic_api_node_uses_messages_protocol(self):
        cid = api_nodes.create("team-claude", "anthropic", "anthropic-secret")
        observed = {}
        def handler(request):
            observed["url"] = str(request.url)
            observed["version"] = request.headers["anthropic-version"]
            observed["key"] = request.headers["x-api-key"]
            return httpx.Response(200, json={"content": [{"type": "text", "text": "analysis"}]})
        client_class = httpx.Client
        with patch.object(api_nodes.httpx, "Client", side_effect=lambda **kw: client_class(
                transport=httpx.MockTransport(handler), **kw)):
            self.assertEqual(api_nodes.run({"credential_id": cid, "model": "test-model"}, "prompt"), "analysis")
        self.assertEqual(observed["url"], "https://api.anthropic.com/v1/messages")
        self.assertTrue(observed["version"])
        self.assertEqual(observed["key"], "anthropic-secret")

    def test_azure_api_node_uses_account_endpoint_and_api_key_header(self):
        cid = api_nodes.create("team-azure", "azure", "azure-secret",
                               "https://sample.openai.azure.com/openai/v1")
        observed = {}
        def handler(request):
            observed["url"] = str(request.url)
            observed["key"] = request.headers["api-key"]
            return httpx.Response(200, json={"choices": [{"message": {"content": "result"}}]})
        client_class = httpx.Client
        with patch.object(api_nodes.httpx, "Client", side_effect=lambda **kw: client_class(
                transport=httpx.MockTransport(handler), **kw)):
            self.assertEqual(api_nodes.run({"credential_id": cid, "model": "deployment"}, "prompt"), "result")
        self.assertEqual(observed["url"], "https://sample.openai.azure.com/openai/v1/chat/completions")
        self.assertEqual(observed["key"], "azure-secret")

    def test_invalid_or_unsafe_nodes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "内网"):
            api_nodes.validate_base_url("custom", "https://127.0.0.1/v1")
        with self.assertRaises(HTTPException):
            main._clean_pipeline([{"id": "one", "kind": "reviewer", "provider": "claude", "purpose": "context"}])
        with self.assertRaises(HTTPException):
            main._clean_pipeline([{"id": "one", "kind": "reviewer", "provider": "unknown"}])
        with self.assertRaises(HTTPException):
            main._clean_pipeline([{"id": "review", "kind": "review", "reviewers": [
                {"id": "r", "kind": "reviewer", "provider": "claude"}]},
                {"id": "late", "kind": "gate", "gate": "tests"}])

    def test_legacy_gate_migration_runs_once(self):
        legacy = [{"id": "review", "kind": "reviewer", "name": "评审", "provider": "claude"}]
        db.set_setting("pipeline", json.dumps(legacy))
        db.set_setting("pipeline_node_migration_v1", "")
        db.init()
        self.assertEqual(worker.get_pipeline()[0]["kind"], "gate")
        db.set_setting("pipeline", json.dumps(legacy))
        db.init()
        self.assertEqual(worker.get_pipeline()[0]["kind"], "review")
        self.assertEqual(worker.get_pipeline()[0]["reviewers"], legacy)
        self.assertEqual(json.loads(db.get_setting("pipeline")), legacy)

    def test_legacy_flat_reviews_are_grouped_after_context(self):
        old = [
            {"id": "g", "kind": "gate", "gate": "tests"},
            {"id": "a", "kind": "reviewer", "name": "A", "provider": "claude"},
            {"id": "ctx", "kind": "api", "name": "上下文", "purpose": "context"},
            {"id": "b", "kind": "reviewer", "name": "B", "provider": "codex"},
        ]
        grouped = worker.normalize_pipeline(old)
        self.assertEqual([s["id"] for s in grouped], ["g", "ctx", "cross-review"])
        self.assertEqual([s["id"] for s in grouped[-1]["reviewers"]], ["a", "b"])
        self.assertEqual(old[1]["kind"], "reviewer")

    def test_referenced_model_credential_cannot_be_deleted(self):
        cid = api_nodes.create("bound", "openai", "secret")
        steps = main._clean_pipeline([{"kind": "api", "name": "API review",
                                      "credential_id": cid, "model": "test-model"}])
        db.set_setting("pipeline", json.dumps(steps))
        with self.assertRaises(HTTPException) as ctx:
            main.delete_model_credential(cid)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(api_nodes.get_active(cid)["label"], "bound")

    def test_cross_review_members_do_not_receive_each_others_output(self):
        group = {"kind": "review", "reviewers": [
            {"id": "a", "kind": "reviewer", "name": "A"},
            {"id": "b", "kind": "reviewer", "name": "B"}]}
        prior = [{"name": "上下文", "status": "ok", "output": "shared facts"}]
        seen = []
        class FakeLog:
            def start(self, name): return 0
            def finish(self, *args, **kwargs): pass
        def execute(step, project, workdir, built, diff_text, received):
            seen.append((step["name"], list(received)))
            return f'```json\n{{"findings": []}}\n```\n{step["name"]}', None, "prompt"
        with patch.object(worker, "_run_reviewer_step", side_effect=execute):
            outs = worker._run_review_group(group, {}, self.root, {}, "", prior, FakeLog())
        self.assertEqual([name for name, _ in outs], ["A", "B"])
        self.assertEqual(seen[0][1], prior)
        self.assertEqual(seen[1][1], prior)
        self.assertEqual(len(prior), 1)

    def test_reordering_reviewers_does_not_change_verdict_or_eval_fingerprint(self):
        a = ('A', '```json\n{"findings":[{"severity":"Important","file":"x.py","line":10,"title":"空指针错误"}]}\n```')
        b = ('B', '```json\n{"findings":[{"severity":"Important","file":"x.py","line":11,"title":"空指针错误"}]}\n```')
        self.assertEqual(verdict.merge_multi([a, b]), verdict.merge_multi([b, a]))
        from app import evaluator
        one = {"id": "review", "kind": "review", "name": "交叉评审", "reviewers": [
            {"id": "a", "kind": "reviewer", "name": "A", "provider": "claude"},
            {"id": "b", "kind": "reviewer", "name": "B", "provider": "codex"}]}
        db.set_setting("pipeline", json.dumps([one]))
        first = evaluator._config_snapshot()
        one["reviewers"].reverse()
        db.set_setting("pipeline", json.dumps([one]))
        self.assertEqual(first, evaluator._config_snapshot())


if __name__ == "__main__":
    unittest.main()
