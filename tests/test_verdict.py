"""真实回复的修复建议可以包含 Markdown 代码块，仍需严格完整的 JSON。"""
import json
import unittest
from app.engine import verdict


class FindingsParserTest(unittest.TestCase):
    def test_fenced_fix_inside_json_string_does_not_end_outer_json(self):
        finding={'title':'账号分类错误','file':'app/github.py','line':99,
                 'condition':'User 名称含 "agent"','fix':'使用完整代码：\n```python\nreturn user.type == "Bot"\n```'}
        text='总评\n```json\n'+json.dumps({'findings':[finding]},ensure_ascii=False)+'\n```'
        self.assertEqual(verdict.extract_findings(text),[finding])
        merged=verdict.merge_multi([('A',text)])
        self.assertEqual(merged['findings'][0]['fix'],finding['fix'])

    def test_missing_closing_fence_truncated_or_malformed_json_cannot_pass(self):
        for text in ['```json\n{"findings":[]}', '```json\n{"findings":',
                     '```json\n{"findings":[{"title":"x","fix":"unterminated}]}\n```']:
            self.assertIsNone(verdict.extract_findings(text))

    def test_valid_example_cannot_hide_malformed_final_result(self):
        prefix='```json\n{"findings":[]}\n```\n'
        for tail in ['```json\n{"findings":',
                     '```json\n{"findings":[{"title":"bad","fix":"unescaped "quote""}]}\n```']:
            self.assertIsNone(verdict.extract_findings(prefix+tail))

    def test_last_valid_complete_block_wins(self):
        text='```json\n{"findings":[{"title":"earlier"}]}\n```\n```json\n{"findings":[]}\n```'
        self.assertEqual(verdict.extract_findings(text),[])


if __name__ == '__main__':
    unittest.main()
