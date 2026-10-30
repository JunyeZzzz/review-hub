"""反馈到生效的状态、并发、验证隔离及运行时执行契约。"""
import json
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from app import config, connections, db, evaluator, improvements, main, worker
from app.engine import api_nodes
from app.engine import prompts


class ImprovementsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR
        config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR = self.root/'hub.db', self.root, self.root/'secrets'
        if getattr(db._local, 'conn', None):
            db._local.conn.close()
        db._local.conn = None
        with patch.object(config, 'GITHUB_TOKEN', ''):
            db.init()
        self.pid = db.execute("INSERT INTO projects(kind,full_name,name,created_at) VALUES('github','a/b','b',?)", (db.now(),))
        self.rid = db.execute("INSERT INTO runs(project_id,trigger,status,head_sha,base_ref,findings_json,created_at) VALUES(?,'push','blocked','head','base',?,?)",
            (self.pid, json.dumps([{'severity':'IMPORTANT','file':'app/auth.py','line':10,'title':'缺少鉴权','condition':'入口可被直接访问','fix':'检查鉴权','sources':['a','b']},
                                   {'severity':'IMPORTANT','file':'app/auth.py','line':30,'title':'真实越权','sources':['a','b']}]), db.now()))
        self.fp = main.mark_finding(self.rid, 0, main.MarkIn(disposition='false_positive', note='入口已有统一中间件鉴权', evidence='app/router.py: guard'))['feedback_id']
        self.tp = main.mark_finding(self.rid, 1, main.MarkIn(disposition='confirmed', note='内部接口绕过了中间件'))['feedback_id']
        self.body = {'feedback_ids':[self.fp], 'title':'先追踪鉴权路径', 'kind':'check','scope':'app/*','rationale':'评审遗漏统一鉴权',
                     'instruction':'追踪路由注册和鉴权中间件；确认绕过路径才报告，直接暴露的入口仍应报告。','evidence':'app/router.py: guard'}
        self.item = improvements.save_candidate(self.body)

    def tearDown(self):
        if evaluator._eval_lock.locked():
            evaluator._eval_lock.release()
        if getattr(db._local, 'conn', None):
            db._local.conn.close()
        db._local.conn = None
        config.DB_PATH, config.DATA_DIR, connections.SECRET_DIR = self.old
        self.tmp.cleanup()

    def validate(self, answer=None):
        def run(case, progress, improvement_items=None):
            progress('独立评审', [{'name':'独立评审','status':'running'}])
            is_candidate = bool(improvement_items)
            result = 'fp' if case['polarity']=='negative' and not is_candidate else 'hit'
            actual=[json.loads(r['expected']) for r in db.query("SELECT expected FROM eval_cases WHERE polarity='positive' OR ?=0",(int(is_candidate),))]
            return {'case_id':case['id'],'result':result,'actual':actual,'detail':'测试输出','steps':[],
                    'improvements':[{'id':i['id'],'revision':i['revision']} for i in improvement_items]}
        with patch.object(improvements.threading, 'Thread'):
            vid = improvements.start_validation(self.item['id'])['validation_id']
        with patch.object(evaluator, '_run_case', side_effect=answer or run):
            improvements._validate(vid)
        return vid

    def test_automatic_capture_and_atomic_sample_relabel(self):
        self.assertEqual(len(db.query('SELECT * FROM review_feedback')), 2)
        self.assertEqual(len(db.query('SELECT * FROM eval_cases')), 2)
        main.mark_finding(self.rid, 0, main.MarkIn(disposition='confirmed', note='确认绕过'))
        self.assertEqual(db.get('SELECT polarity FROM eval_cases WHERE idx=0')['polarity'], 'positive')
        main.mark_finding(self.rid, 0, main.MarkIn(disposition=''))
        self.assertIsNone(db.get('SELECT id FROM eval_cases WHERE idx=0'))
        self.assertEqual(db.get('SELECT active FROM review_feedback WHERE id=?', (self.fp,))['active'], 0)
        self.assertEqual(len(db.query('SELECT * FROM feedback_events WHERE feedback_id=?', (self.fp,))), 3)

    def test_resaving_same_feedback_restores_deleted_sample_without_changing_candidate(self):
        db.execute('DELETE FROM eval_cases WHERE idx=0')
        main.mark_finding(self.rid, 0, main.MarkIn(disposition='false_positive',
                         note='入口已有统一中间件鉴权', evidence='app/router.py: guard'))
        self.assertIsNotNone(db.get('SELECT id FROM eval_cases WHERE idx=0'))
        self.assertEqual(improvements.get_item(self.item['id'])['revision'],1)
        self.assertEqual(len(db.query('SELECT * FROM feedback_events WHERE feedback_id=?',(self.fp,))),1)

    def test_saving_identical_feedback_does_not_invalidate_validation(self):
        before=improvements.fingerprint(self.item['id'])
        main.mark_finding(self.rid, 0, main.MarkIn(disposition='false_positive',
                         note='入口已有统一中间件鉴权', evidence='app/router.py: guard'))
        self.assertEqual(improvements.fingerprint(self.item['id']),before)

    def test_legacy_gate_feedback_can_be_removed_but_cannot_become_candidate(self):
        findings=json.loads(db.get('SELECT findings_json FROM runs WHERE id=?',(self.rid,))['findings_json'])
        findings[0]['sources']=['gitleaks-like']
        db.execute('UPDATE runs SET findings_json=? WHERE id=?',(json.dumps(findings),self.rid))
        db.execute('UPDATE review_feedback SET finding=? WHERE id=?',(json.dumps(findings[0]),self.fp))
        with self.assertRaises(HTTPException):
            improvements.save_candidate(self.body)
        main.mark_finding(self.rid,0,main.MarkIn(disposition=''))
        self.assertEqual(db.get('SELECT active FROM review_feedback WHERE id=?',(self.fp,))['active'],0)

    def test_candidate_cannot_adopt_without_validated_improvement_and_human_review(self):
        iid = self.item['id']
        with self.assertRaises(HTTPException):
            improvements.adopt(iid)
        vid = self.validate()
        self.assertEqual(improvements.get_item(iid)['status'], 'ready')
        with self.assertRaises(HTTPException):
            improvements.adopt(iid)
        improvements.review_validation(vid, {'note':'核实原始输出：误报消失且真实越权仍报出'})
        improvements.adopt(iid)
        improvements.adopt(iid)  # 幂等采用不重复记录
        self.assertEqual(len(improvements.active_for_project(self.pid)), 1)
        self.assertEqual(len(db.query("SELECT * FROM improvement_events WHERE action='adopted'")), 1)
        improvements.revoke(iid)
        self.assertEqual(improvements.active_for_project(self.pid), [])

    def test_feedback_change_revokes_active_and_invalidates_versions(self):
        vid = self.validate()
        improvements.review_validation(vid, {'note':'核对通过'})
        improvements.adopt(self.item['id'])
        main.mark_finding(self.rid, 0, main.MarkIn(disposition='confirmed', note='之前的人工判定有误'))
        updated = improvements.get_item(self.item['id'])
        self.assertEqual(updated['status'], 'revoked')
        self.assertEqual(updated['revision'], 2)
        with self.assertRaises(HTTPException):
            improvements.adopt(self.item['id'])

    def test_configuration_change_blocks_stale_adoption(self):
        vid = self.validate()
        improvements.review_validation(vid, {'note':'核对通过'})
        db.execute("UPDATE projects SET profile='项目约束发生变化' WHERE id=?", (self.pid,))
        self.assertTrue(improvements.get_detail(self.item['id'])['validations'][0]['stale'])
        with self.assertRaises(HTTPException):
            improvements.adopt(self.item['id'])

    def test_failed_execution_or_no_improvement_never_eligible(self):
        def fail(case, progress, improvement_items=None):
            return {'case_id':case['id'], 'result':'error' if not improvement_items else 'hit','actual':[]}
        vid = self.validate(fail)
        self.assertFalse(db.get('SELECT eligible FROM improvement_validations WHERE id=?', (vid,))['eligible'])
        with self.assertRaises(HTTPException):
            improvements.review_validation(vid, {'note':'试图把错误当成功'})
        def same(case, progress, improvement_items=None):
            return {'case_id':case['id'], 'result':'hit','actual':[]}
        vid = self.validate(same)
        self.assertFalse(db.get('SELECT eligible FROM improvement_validations WHERE id=?', (vid,))['eligible'])

    def test_scope_isolation_and_actual_prompt_and_report_use(self):
        iid = self.item['id']
        vid = self.validate()
        improvements.review_validation(vid, {'note':'核对通过'})
        improvements.adopt(iid)
        self.assertEqual(improvements.active_for_project(self.pid+1), [])
        items = improvements.active_for_project(self.pid)
        self.assertEqual(improvements.select(items, ['docs/help.md']), [])
        diff = self.root/'diff.patch'; diff.write_text('+++ b/app/auth.py\n+changed')
        prompt = self.root/'prompt'; prompt.write_text('基础审查')
        built, selected = improvements.attach({'diff_file':diff,'prompt_file':prompt}, items, ['app/auth.py'])
        built.update(review_run_id=self.rid, improvement_selected=selected)
        with patch.object(api_nodes, 'run', return_value='```json\n{"findings":[]}\n```') as run:
            worker._run_reviewer_step({'kind':'api'}, {}, self.root, built, '')
        actual = run.call_args.args[1]
        self.assertIn('追踪路由注册和鉴权中间件', actual)
        self.assertIn(f'改进 #{iid} · v1', actual)
        self.assertEqual(main.get_run(self.rid)['improvements'][0]['revision'], 1)
        improvements.revoke(iid)
        self.assertEqual(main.get_run(self.rid)['improvements'][0]['snapshot']['instruction'], self.body['instruction'])

    def test_missing_protection_and_cross_project_sources_rejected(self):
        db.execute('DELETE FROM eval_cases WHERE idx=1')
        with self.assertRaises(HTTPException):
            improvements.start_validation(self.item['id'])
        self.assertFalse(evaluator._eval_lock.locked())
        db.execute('UPDATE review_feedback SET project_id=? WHERE id=?', (self.pid+1,self.tp))
        with self.assertRaises(HTTPException):
            improvements.save_candidate({**self.body,'feedback_ids':[self.fp,self.tp]})

    def test_stale_edit_and_oversize_input_rejected(self):
        with self.assertRaises(HTTPException):
            improvements.save_candidate({**self.body,'revision':0}, self.item['id'])
        with self.assertRaises(HTTPException):
            improvements.save_candidate({**self.body,'instruction':'x'*5001})
        with self.assertRaises(HTTPException):
            improvements.save_candidate({**self.body,'feedback_ids':['invalid']})

    def test_mutation_mid_validation_and_restart_fail_visibly(self):
        def change(case, progress, improvement_items=None):
            db.execute("UPDATE eval_cases SET note='变化' WHERE id=?", (case['id'],))
            return {'case_id':case['id'],'result':'hit'}
        vid = self.validate(change)
        self.assertEqual(db.get('SELECT status FROM improvement_validations WHERE id=?', (vid,))['status'],'failed')
        with patch.object(improvements.threading, 'Thread'):
            vid = improvements.start_validation(self.item['id'])['validation_id']
        improvements.recover_interrupted()
        self.assertEqual(db.get('SELECT status FROM improvement_validations WHERE id=?', (vid,))['status'],'failed')
        self.assertEqual(improvements.get_item(self.item['id'])['status'],'needs_changes')

    def test_manual_matching_correction_preserves_machine_result(self):
        vid = self.validate()
        case = db.get("SELECT id FROM eval_cases WHERE polarity='negative'")['id']
        improvements.review_validation(vid, {'case_id':case,'baseline':'fp','candidate':'fp','note':'模型换标题仍报了同一误报'})
        row = db.get('SELECT * FROM improvement_validations WHERE id=?', (vid,))
        pair = next(p for p in json.loads(row['results']) if p['case_id']==case)
        self.assertEqual(pair['candidate']['machine_result'], 'hit')
        self.assertEqual(pair['candidate']['result'], 'fp')
        self.assertFalse(row['eligible'])
        self.assertEqual(row['review_note'],'')

    def test_active_policy_pauses_after_environment_change(self):
        vid = self.validate()
        improvements.review_validation(vid, {'note':'核对通过'})
        improvements.adopt(self.item['id'])
        db.set_setting('claude_model', 'changed-model')
        self.assertEqual(improvements.active_for_project(self.pid), [])
        self.assertFalse(improvements.get_detail(self.item['id'])['effective'])
        with self.assertRaises(HTTPException):
            improvements.adopt(self.item['id'])

    def test_history_migration_is_idempotent_and_never_reenables_deleted_samples(self):
        db.init()
        self.assertEqual(len(db.query('SELECT * FROM review_feedback')),2)
        db.execute('DELETE FROM eval_cases')
        db.init()
        self.assertEqual(db.query('SELECT * FROM eval_cases'),[])

    def test_human_missed_issue_replays_without_modifying_original_findings(self):
        original = db.get('SELECT findings_json FROM runs WHERE id=?', (self.rid,))['findings_json']
        body = {'run_id':self.rid,'title':'漏掉的事务错误','file':'app/auth.py','line':40,
                'condition':'事务写入后失败','reason':'失败后未回滚','evidence':'事务测试证实部分写入'}
        fid = main.feedback_missed(body)['feedback_id']
        row = db.get('SELECT * FROM review_feedback WHERE id=?', (fid,))
        self.assertLess(row['idx'],0)
        self.assertEqual(db.get('SELECT polarity FROM eval_cases WHERE idx=?', (row['idx'],))['polarity'],'positive')
        self.assertEqual(db.get('SELECT findings_json FROM runs WHERE id=?', (self.rid,))['findings_json'],original)
        main.feedback_update(fid, main.MarkIn(disposition=''))
        self.assertIsNone(db.get('SELECT id FROM eval_cases WHERE idx=?', (row['idx'],)))

    def test_ai_draft_uses_readonly_reviewer_and_never_activates(self):
        with patch.object(improvements.threading, 'Thread'):
            iid = improvements.start_draft([self.fp])['id']
        output = '```json\n'+json.dumps({k:self.body[k] for k in ['title','kind','scope','rationale','instruction','evidence']})+'\n```'
        with patch.object(worker, '_ensure_clone', side_effect=lambda project, repo: repo.mkdir()), \
             patch.object(worker, '_git_retry'), patch.object(worker,'_run_reviewer_step',return_value=(output,None,'prompt')) as reviewer:
            improvements._draft(iid)
        self.assertEqual(improvements.get_item(iid)['status'], 'draft')
        self.assertIn('人工反馈', reviewer.call_args.args[0]['prompt'])
        self.assertEqual(improvements.active_for_project(self.pid), [])
        analysis=db.get("SELECT detail FROM improvement_events WHERE improvement_id=? AND action='analysis_finished'",(iid,))
        self.assertEqual(json.loads(analysis['detail'])['output'],output)
        self.assertEqual(json.loads(analysis['detail'])['prompt'],'prompt')

    def test_deleted_and_unicode_paths_use_same_scope_in_replay(self):
        repo=self.root/'repo'; repo.mkdir(); (repo/'app').mkdir()
        def git(*args):
            return subprocess.run(['git',*args],cwd=repo,capture_output=True,text=True,check=True).stdout.strip()
        git('init')
        for name in ('权限.py','deleted.py'):
            (repo/'app'/name).write_text('value = 1\n')
        git('add','.')
        git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','base')
        before=git('rev-parse','HEAD')
        (repo/'app/deleted.py').unlink()
        (repo/'app/权限.py').write_text('value = 2\n')
        git('add','.')
        git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','change')
        built=prompts.build(repo,diff_spec=f'{before}..HEAD')
        self.assertEqual(set(built['changed_files']),{'app/权限.py','app/deleted.py'})
        selected=improvements.select([{**self.item,'scope':'app/deleted.py'}],built['changed_files'])
        self.assertEqual(len(selected),1)

    def test_path_scope_star_does_not_leak_into_unrelated_nested_directories(self):
        self.assertTrue(improvements._path_matches('app/auth.py','app/*'))
        self.assertFalse(improvements._path_matches('app/internal/auth.py','app/*'))
        self.assertFalse(improvements._path_matches('other/app/auth.py','app/*'))
        self.assertTrue(improvements._path_matches('app/internal/auth.py','app/**'))
        self.assertTrue(improvements._path_matches('auth.py','**/*.py'))
        self.assertTrue(improvements._path_matches('app/auth.py','**/*.py'))
        self.assertTrue(improvements._path_matches('app/internal/auth.py','*'))

    def test_baseline_reuse_requires_same_contract_source_and_complete_real_output(self):
        case=dict(db.get("SELECT * FROM eval_cases WHERE polarity='negative'"))
        expected=json.loads(case['expected'])
        output='```json\n'+json.dumps({'findings':[expected]})+'\n```'
        result={'result':'fp','actual':[expected],'reviews':[{'name':'A','output':output}]}
        contract=improvements.baseline_contract(self.pid)
        source=dict(db.get('SELECT trigger,base_ref,head_sha FROM runs WHERE id=?',(self.rid,)))
        snapshot={'baseline_contract':contract,'cases':[case],'replay_sources':{str(self.rid):source}}
        vid=db.execute("INSERT INTO improvement_validations(improvement_id,revision,status,fingerprint,snapshot,results,created_at) VALUES(?,1,'done','test',?,?,?)",
                       (self.item['id'],json.dumps(snapshot),json.dumps([{'case_id':case['id'],'baseline':result}]),db.now()))
        cached=improvements.reuse_baseline(self.item['id'],vid+1,case,contract)
        self.assertEqual((cached['result'],cached['reused_from_validation']),('fp',vid))
        self.assertIn('不是本轮重新调用',cached['detail'])
        cached['prior_human_judgement']='原始样本已核对'
        db.execute('UPDATE improvement_validations SET results=? WHERE id=?',
                   (json.dumps([{'case_id':case['id'],'baseline':cached}]),vid))
        chained=improvements.reuse_baseline(self.item['id'],vid+1,case,contract)
        self.assertEqual(chained['collected_at'],cached['collected_at'])
        self.assertEqual(chained['reused_from_validation'],vid)
        self.assertEqual(chained['prior_human_judgement'],'原始样本已核对')
        cached['collected_at']='2000-01-01T00:00:00Z'
        db.execute('UPDATE improvement_validations SET results=? WHERE id=?',
                   (json.dumps([{'case_id':case['id'],'baseline':cached}]),vid))
        self.assertIsNone(improvements.reuse_baseline(self.item['id'],vid+1,case,contract))
        db.execute('UPDATE improvement_validations SET results=? WHERE id=?',
                   (json.dumps([{'case_id':case['id'],'baseline':result}]),vid))
        db.set_setting('claude_model','new-model')
        self.assertIsNone(improvements.reuse_baseline(self.item['id'],vid+1,case,improvements.baseline_contract(self.pid)))
        db.set_setting('claude_model','')
        db.execute("UPDATE runs SET base_ref='different-base' WHERE id=?",(self.rid,))
        self.assertIsNone(improvements.reuse_baseline(self.item['id'],vid+1,case,contract))

        db.execute("UPDATE runs SET base_ref='base' WHERE id=?",(self.rid,))
        result['reviews']=[]
        db.execute('UPDATE improvement_validations SET results=? WHERE id=?',(json.dumps([{'case_id':case['id'],'baseline':result}]),vid))
        self.assertIsNone(improvements.reuse_baseline(self.item['id'],vid+1,case,contract))

    def test_incremental_adoption_keeps_existing_misses_visible_and_blocks_new_regressions(self):
        source={'source':True,'baseline':{'result':'fp'},'candidate':{'result':'hit'}}
        remaining={'source':False,'baseline':{'result':'miss'},'candidate':{'result':'miss'}}
        protected={'source':False,'baseline':{'result':'hit'},'candidate':{'result':'hit'}}
        self.assertTrue(improvements.eligible_results([source,remaining,protected]))
        self.assertFalse(improvements.eligible_results([source,remaining,protected],'all-hit-v1'))
        protected['candidate']['result']='miss'
        self.assertFalse(improvements.eligible_results([source,remaining,protected]))
        protected['candidate']['result']='error'
        self.assertFalse(improvements.eligible_results([source,remaining,protected]))

    def test_reused_baseline_keeps_each_samples_own_review_even_in_same_interval(self):
        output='```json\n{"findings":[]}\n```'
        def complete(case,progress,improvement_items=None):
            return {'result':'hit' if case['polarity']=='negative' else 'miss',
                    'actual':[], 'reviews':[{'name':'A','output':output}]}
        first=self.validate(complete)
        cases=[dict(c) for c in db.query('SELECT * FROM eval_cases ORDER BY id')]
        for c in cases:
            improvements.review_validation(first,{'case_id':c['id'],'baseline':'hit','candidate':'hit',
                'note':f"该样本单独核对 {c['id']}"})
        second=self.validate(complete)
        pairs=json.loads(db.get('SELECT results FROM improvement_validations WHERE id=?',(second,))['results'])
        self.assertEqual([p['baseline']['result'] for p in pairs],['hit','hit'])
        for pair in pairs:
            self.assertEqual(pair['baseline']['prior_human_judgement'],f"该样本单独核对 {pair['case_id']}")

    def test_policy_reassessment_preserves_raw_results_and_requires_fresh_review(self):
        vid=self.validate()
        row=db.get('SELECT * FROM improvement_validations WHERE id=?',(vid,))
        snapshot=json.loads(row['snapshot']);snapshot['adoption_policy']='all-hit-v1'
        results=json.loads(row['results'])
        guard=next(p for p in results if not p['source'])
        guard['baseline']['result']=guard['candidate']['result']='miss'
        raw=improvements.pack(results)
        db.execute('UPDATE improvement_validations SET snapshot=?,results=?,eligible=0,review_note=\'old review\' WHERE id=?',
                   (improvements.pack(snapshot),raw,vid))
        db.execute("UPDATE review_improvements SET status='needs_changes' WHERE id=?",(self.item['id'],))
        improvements.reassess_validation(vid,'原门槛把单项改进与其他领域遗留漏报混为一谈；保留未解决样本，只阻止新增退化')
        updated=db.get('SELECT * FROM improvement_validations WHERE id=?',(vid,))
        self.assertEqual(updated['results'],raw)
        self.assertEqual(updated['review_note'],'')
        self.assertTrue(updated['eligible'])
        with self.assertRaises(HTTPException):improvements.adopt(self.item['id'])

    def test_adopted_candidate_output_can_be_next_improvement_baseline(self):
        def review(case,progress,improvement_items=None):
            actual=[json.loads(r['expected']) for r in db.query("SELECT expected FROM eval_cases WHERE polarity='positive' OR ?=0",(int(bool(improvement_items)),))]
            output='```json\n'+json.dumps({'findings':actual})+'\n```'
            return {'case_id':case['id'],'result':'fp' if case['polarity']=='negative' and not improvement_items else 'hit',
                    'actual':actual,'reviews':[{'name':'A','output':output}]}
        vid=self.validate(review)
        improvements.review_validation(vid,{'note':'核对完整发现，无新增退化'})
        improvements.adopt(self.item['id'])
        next_item=improvements.save_candidate({**self.body,'title':'下一项独立改进','feedback_ids':[self.tp]})
        case=dict(db.get("SELECT * FROM eval_cases WHERE polarity='negative'"))
        cached=improvements.reuse_baseline(next_item['id'],vid+1,case,improvements.baseline_contract(self.pid))
        self.assertEqual((cached['reused_arm'],cached['result']),('candidate','hit'))
        self.assertEqual(cached['reused_from_improvement'],self.item['id'])

    def test_large_evidence_is_deduplicated_private_and_hash_checked(self):
        digest=improvements.save_review_evidence('model output',[{'t':'tool','name':'Read'}],'full input')
        self.assertEqual(digest,improvements.save_review_evidence('model output',[{'t':'tool','name':'Read'}],'full input'))
        file=self.root/'review-evidence'/f'{digest}.json'
        self.assertEqual(file.stat().st_mode & 0o777,0o600)
        self.assertEqual(improvements.read_review_evidence(digest)['prompt'],'full input')
        with self.assertRaises(HTTPException):improvements.read_review_evidence('../hub.db')
        file.write_text('corrupt')
        with self.assertRaises(HTTPException):improvements.read_review_evidence(digest)
        improvements.save_review_evidence('model output',[{'t':'tool','name':'Read'}],'full input')
        self.assertEqual(improvements.read_review_evidence(digest)['output'],'model output')

    def test_new_experience_only_enters_relevant_parts_while_legacy_keeps_its_contract(self):
        scoped={**self.item,'scope':'app/github.py','context_version':'per-part-v2'}
        legacy={**self.item,'id':999,'scope':'app/github.py','context_version':'whole-diff-v1'}
        self.assertEqual(improvements.for_part([scoped],['static/app.js']),[])
        self.assertEqual(improvements.for_part([legacy],['static/app.js']),[legacy])
        full=self.root/'full.patch';full.write_text('+++ b/app/github.py\n+adapter\n+++ b/static/app.js\n+frontend')
        template=self.root/'prompt';template.write_text(f'完整审查 {full}')
        parts=[]
        for index,(file,marker) in enumerate([('app/github.py','adapter'),('static/app.js','frontend')]):
            p=self.root/f'part-{index}.patch';p.write_text(f'+++ b/{file}\n+{marker}');parts.append(p)
        built,_=improvements.attach({'diff_file':full,'prompt_file':template,'review_parts':parts,
            'review_part_paths':[['app/github.py'],['static/app.js']]},[scoped],['app/github.py','static/app.js'])
        seen=[]
        def call(step,prompt):
            seen.append(prompt);return '```json\n{"findings":[]}\n```'
        with patch.object(api_nodes,'run',side_effect=call):
            worker._run_reviewer_step({'kind':'api'},{},self.root,built,full.read_text())
        adapter=next(p for p in seen if '+adapter' in p)
        frontend=next(p for p in seen if '+frontend' in p)
        self.assertIn(scoped['instruction'],adapter)
        self.assertNotIn(scoped['instruction'],frontend)

    def test_failed_validation_resumes_only_unfinished_sides_with_audit_and_lock(self):
        expected=[json.loads(r['expected']) for r in db.query('SELECT expected FROM eval_cases')]
        def first(case,progress,improvement_items=None):
            if improvement_items:
                return {'result':'error','detail':'provider failure'}
            output='```json\n'+json.dumps({'findings':expected})+'\n```'
            return {'result':'fp','actual':expected,'reviews':[{'name':'A','output':output}]}
        vid=self.validate(first)
        failed=db.get('SELECT * FROM improvement_validations WHERE id=?',(vid,))
        self.assertEqual(failed['status'],'failed')
        saved=json.loads(failed['results'])
        with patch.object(improvements.threading,'Thread'):
            resumed=improvements.resume_validation(vid)
        self.assertEqual(resumed['retained_arms'],1)
        with self.assertRaises(HTTPException):improvements.resume_validation(vid)
        def rest(case,progress,improvement_items=None):
            self.assertTrue(improvement_items)  # 基线已完成，不能再次调用。
            actual=[e for e in expected if e['title']=='真实越权']
            output='```json\n'+json.dumps({'findings':actual})+'\n```'
            return {'result':'hit','actual':actual,'reviews':[{'name':'A','output':output}]}
        with patch.object(evaluator,'_run_case',side_effect=rest) as calls:
            improvements._validate(vid,saved)
        self.assertEqual(calls.call_count,1)  # 同一区间的候选只新执行一次。
        self.assertEqual(db.get('SELECT status FROM improvement_validations WHERE id=?',(vid,))[0],'done')
        actions=[r['action'] for r in db.query('SELECT action FROM improvement_events')]
        self.assertIn('validation_failed',actions)
        self.assertIn('validation_resumed',actions)

    def test_failed_validation_cannot_resume_after_input_changes(self):
        vid=self.validate(lambda *a,**kw:{'result':'error','detail':'provider failure'})
        db.set_setting('prompt_template','changed input')
        with self.assertRaises(HTTPException):improvements.resume_validation(vid)

    def test_cli_file_replacement_pauses_active_knowledge_even_if_model_name_is_same(self):
        binary=self.root/'claude';binary.write_text('version one')
        with patch.object(config,'CLAUDE_BIN',str(binary)):
            vid=self.validate()
            improvements.review_validation(vid,{'note':'逐项核对输出'})
            improvements.adopt(self.item['id'])
            self.assertTrue(improvements.get_detail(self.item['id'])['effective'])
            binary.write_text('same version, replaced executable file')
            self.assertFalse(improvements.get_detail(self.item['id'])['effective'])


if __name__ == '__main__':
    unittest.main()
