"""Use real Git commits and revert; replace only remote clone/push/PR boundaries."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import github, worker


class RevertCommitTest(unittest.TestCase):
    def test_push_revert_detects_real_merge_parents(self):
        with tempfile.TemporaryDirectory() as td:
            repo=Path(td)/'origin';repo.mkdir()
            def git(*args):
                return subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True,text=True).stdout.strip()
            git('init','-b','main');git('config','user.name','Test');git('config','user.email','test@example.com')
            (repo/'base.txt').write_text('base\n');git('add','.');git('commit','-m','base')
            git('checkout','-b','feature');(repo/'feature.txt').write_text('feature\n');git('add','.');git('commit','-m','feature')
            feature=git('rev-parse','HEAD')
            git('checkout','main');(repo/'main.txt').write_text('main\n');git('add','.');git('commit','-m','main')
            git('merge','--no-ff','feature','-m','merge feature');merge=git('rev-parse','HEAD')
            real_run=subprocess.run
            for sha,expected_merge in [(merge,True),(feature,False)]:
                calls=[]
                def execute(args,**kw):
                    calls.append(args)
                    if args[:2]==['git','clone']:
                        return real_run(['git','clone',str(repo),args[-1]],**kw)
                    if args[:2]==['git','push']:
                        files=real_run(['git','ls-tree','--name-only','HEAD'],cwd=kw['cwd'],check=True,capture_output=True,text=True).stdout
                        self.assertIn('main.txt',files)
                        self.assertNotIn('feature.txt',files)
                        return subprocess.CompletedProcess(args,0,'','')
                    return real_run(args,**kw)
                env={**os.environ,'GIT_AUTHOR_NAME':'Test','GIT_AUTHOR_EMAIL':'test@example.com',
                     'GIT_COMMITTER_NAME':'Test','GIT_COMMITTER_EMAIL':'test@example.com'}
                with patch.object(github,'repo_info',return_value={'default_branch':'main'}), \
                     patch('app.git_auth.auth_env',return_value=env), \
                     patch.object(github.subprocess,'run',side_effect=execute), \
                     patch.object(github,'gh') as gh, patch.dict(os.environ,env):
                    gh.return_value.json.return_value={'html_url':'https://github.com/owner/repo/pull/1'}
                    self.assertEqual(github.create_revert_pr('owner/repo',sha,'test',token='test-token'),
                                     'https://github.com/owner/repo/pull/1')
                revert=next(c for c in calls if c[:3]==['git','revert','--no-edit'])
                self.assertEqual('-m' in revert,expected_merge)
            with patch.object(github,'repo_info',return_value={'default_branch':'main'}), \
                 patch('app.git_auth.auth_env',return_value=env), \
                 patch.object(github.subprocess,'run',side_effect=execute), patch.object(github,'gh') as gh:
                with self.assertRaisesRegex(RuntimeError,'无法读取回滚目标提交'):
                    github.create_revert_pr('owner/repo','f'*40,'test',token='test-token')
                gh.assert_not_called()

    def test_legacy_merged_followup_uses_current_signature(self):
        project={'full_name':'owner/repo','auto_revert':True}
        run={'id':1,'pr_number':None,'trigger':'merged','head_sha':'a'*40}
        with patch.object(github,'create_revert_pr',autospec=True,return_value='https://example.com/pr') as create, \
             patch('app.connections.token_for_project',return_value='test-token'), \
             patch('app.db.execute') as save:
            worker._github_followup(project,run,{'important_count':1},'blocked')
        create.assert_called_once_with('owner/repo','a'*40,
            '合并后评审发现 1 个 Important（run #1）',token='test-token')
        save.assert_called_once()



if __name__=='__main__':
    unittest.main()
