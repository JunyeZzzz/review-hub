"""历史反馈 #4/#8/#9 的可执行回归，直接运行应用 JS，不复制其实现。"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), '前端反馈回归需要 Node.js；CLI 审查部署通常已提供')
class FrontendFeedbackTest(unittest.TestCase):
    def check_scenario(self, scenario, source="app.js"):
        result = subprocess.run([shutil.which('node'), str(ROOT/'tests/frontend_feedback.cjs'),
                                 str(ROOT/'static'/source), scenario],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_leaving_during_evaluation_does_not_touch_the_new_page(self):
        self.check_scenario('leave_during_evaluation')

    def test_terminal_report_stops_polling(self):
        self.check_scenario('terminal_report_stops_polling')

    def test_legacy_validation_without_policy_is_explained(self):
        self.check_scenario('legacy_validation_policy_is_visible', 'improvements.js')

    def test_empty_api_credential_cannot_be_saved_as_zero(self):
        self.check_scenario('empty_api_credential_is_rejected')


if __name__ == '__main__':
    unittest.main()
