"""项目级审查改进：反馈、版本化候选、配对验证、采用、撤销和执行证据。"""
import fnmatch
import hashlib
import json
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import HTTPException
from . import config, db

ADOPTION_POLICY = 'incremental-v2'


def pack(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def event(c, iid, action, detail):
    detail = {'actor': os.environ.get('HUB_ADMIN_USER', 'admin'), **detail}
    c.execute('INSERT INTO improvement_events(improvement_id,action,detail,created_at) VALUES(?,?,?,?)',
              (iid, action, pack(detail), db.now()))


def get_item(iid):
    row = db.get('SELECT * FROM review_improvements WHERE id=?', (iid,))
    if not row:
        raise HTTPException(404, '改进项不存在')
    return dict(row)


def capture(c, run, idx, disposition, note='', evidence='', finding_override=None):
    """与判定共用事务。改判/撤销使关联候选验证失效，已采用项立即撤销。"""
    finding = finding_override if finding_override is not None else json.loads(run['findings_json'] or '[]')[idx]
    old = c.execute('SELECT * FROM review_feedback WHERE run_id=? AND idx=?', (run['id'], idx)).fetchone()
    changed = not old or not (old['disposition'] == disposition and old['reason'] == note
                             and old['evidence'] == evidence and old['active'] == bool(disposition))
    if old and changed:
        for row in c.execute('SELECT i.* FROM review_improvements i JOIN improvement_sources s ON s.improvement_id=i.id WHERE s.feedback_id=?', (old['id'],)).fetchall():
            status = 'revoked' if row['status'] == 'active' else 'draft'
            c.execute('UPDATE review_improvements SET status=?,revision=revision+1,updated_at=? WHERE id=?', (status, db.now(), row['id']))
            event(c, row['id'], 'feedback_changed', {'message': '来源反馈发生变化，原验证失效；已生效策略已撤销', 'feedback_id': old['id']})
    if changed:
        c.execute('INSERT INTO review_feedback(project_id,run_id,idx,disposition,reason,evidence,finding,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) '
                  'ON CONFLICT(run_id,idx) DO UPDATE SET disposition=excluded.disposition,reason=excluded.reason,evidence=excluded.evidence,finding=excluded.finding,active=excluded.active,updated_at=excluded.updated_at',
                  (run['project_id'], run['id'], idx, disposition, note, evidence, pack(finding), int(bool(disposition)), db.now(), db.now()))
    fid = c.execute('SELECT id FROM review_feedback WHERE run_id=? AND idx=?', (run['id'], idx)).fetchone()['id']
    if changed:
        c.execute('INSERT INTO feedback_events(feedback_id,snapshot,created_at) VALUES(?,?,?)',
                  (fid, pack({'disposition': disposition, 'reason': note, 'evidence': evidence, 'finding': finding}), db.now()))
    # 所有可重放的人工发现都可作样本，包括未获交叉确认的 Nit。
    if disposition and run['head_sha'] and not any(str(s).startswith('gate:') for s in finding.get('sources', [])):
        project = c.execute('SELECT kind FROM projects WHERE id=?', (run['project_id'],)).fetchone()
        if project and project['kind'] == 'github':
            expected = {k: finding.get(k) for k in ('file', 'line', 'title', 'condition', 'fix')}
            c.execute('INSERT INTO eval_cases(project_id,run_id,idx,polarity,head_sha,intent,expected,created_at) VALUES(?,?,?,?,?,?,?,?) '
                      'ON CONFLICT(run_id,idx) DO UPDATE SET polarity=excluded.polarity,expected=excluded.expected',
                      (run['project_id'], run['id'], idx, 'positive' if disposition == 'confirmed' else 'negative', run['head_sha'], run['intent'] or '', pack(expected), db.now()))
    elif not disposition:
        c.execute('DELETE FROM eval_cases WHERE run_id=? AND idx=?', (run['id'], idx))
    return fid


def save_candidate(body, iid=None):
    raw_ids = body.get('feedback_ids', [])
    if not isinstance(raw_ids, list) or any(type(i) is not int for i in raw_ids):
        raise HTTPException(400, 'feedback_ids 必须是整数列表')
    ids = sorted(set(raw_ids))
    if not ids or len(ids) > 100:
        raise HTTPException(400, '请选择 1–100 条来源反馈')
    fields = {}
    for key, limit in [('title', 160), ('kind', 20), ('scope', 1000), ('rationale', 3000), ('instruction', 5000), ('evidence', 3000)]:
        raw = body.get(key, '')
        if not isinstance(raw, str):
            raise HTTPException(400, f'{key} 必须是文本')
        value = raw.strip()
        if not value or len(value) > limit:
            raise HTTPException(400, f'{key} 必填且最多 {limit} 字符')
        fields[key] = value
    if fields['kind'] not in ('knowledge', 'check'):
        raise HTTPException(400, '改进类型必须是项目知识或核验步骤')
    patterns = [p.strip() for p in fields['scope'].split(',') if p.strip()]
    if not patterns or len(patterns) > 20 or any(p.startswith('/') or '..' in p.split('/') for p in patterns):
        raise HTTPException(400, '适用范围使用仓库相对路径 glob，最多 20 项，以逗号分隔')
    c = db.conn()
    with c:
        c.execute('BEGIN IMMEDIATE')
        sources = [c.execute('SELECT * FROM review_feedback WHERE id=? AND active=1', (fid,)).fetchone() for fid in ids]
        if any(s is None for s in sources) or len({s['project_id'] for s in sources}) != 1:
            raise HTTPException(400, '来源反馈必须有效且属于同一个项目')
        if any(not s['reason'].strip() for s in sources):
            raise HTTPException(400, '请先为所有来源反馈补充判定原因')
        if any(any(str(x).startswith('gate:') or x == 'gitleaks-like'
                   for x in json.loads(s['finding']).get('sources', [])) for s in sources):
            raise HTTPException(400, '机械门禁记录不能作为模型改进来源，请撤销旧判定')
        pid = sources[0]['project_id']
        if iid:
            old = get_item(iid)
            if old['project_id'] != pid or old['status'] in ('active', 'validating', 'analyzing'):
                raise HTTPException(409, '生效或运行中的改进不能编辑；先撤销或等待完成')
            if body.get('revision') != old['revision']:
                raise HTTPException(409, '改进已被其他操作更新，请刷新')
            c.execute('UPDATE review_improvements SET title=?,kind=?,scope=?,rationale=?,instruction=?,evidence=?,context_version=\'per-part-v2\',status=\'draft\',revision=revision+1,updated_at=? WHERE id=?',
                      (*fields.values(), db.now(), iid))
        else:
            iid = c.execute('INSERT INTO review_improvements(project_id,title,kind,scope,rationale,instruction,evidence,created_at,updated_at,context_version) VALUES(?,?,?,?,?,?,?,?,?,\'per-part-v2\')',
                            (pid, *fields.values(), db.now(), db.now())).lastrowid
        c.execute('DELETE FROM improvement_sources WHERE improvement_id=?', (iid,))
        c.executemany('INSERT INTO improvement_sources(improvement_id,feedback_id) VALUES(?,?)', [(iid, fid) for fid in ids])
        event(c, iid, 'edited' if body.get('revision') else 'created', {**fields, 'feedback_ids': ids,'context_version':'per-part-v2'})
    return get_detail(iid)


def environment_signature(pid):
    from . import worker
    from .engine import prompts
    project = db.get('SELECT * FROM projects WHERE id=?', (pid,))
    if not project:
        return ''
    data = {'pipeline': worker.get_pipeline(project), 'profile': project['profile'],
            'prompt': prompts.get_template(), 'rules': (config.POLICY_DIR / 'REVIEW.md').read_text(),
            'credentials': [dict(r) for r in db.query('SELECT id,updated_at,disabled FROM model_credentials ORDER BY id')],
            'cli_binaries':config.cli_identity(),
            'cli': [config.get_kimi_model(), config.get_kimi_base_url(), config.get_claude_model(),
                    hashlib.sha256((config.get_kimi_api_key()+config.get_claude_token()).encode()).hexdigest()]}
    return hashlib.sha256(pack(data).encode()).hexdigest()


def active_for_project(pid):
    signature = environment_signature(pid)
    return [dict(r) for r in db.query("SELECT * FROM review_improvements WHERE project_id=? AND status='active' ORDER BY id", (pid,))
            if r['activation_signature'] == signature]


def select(items, paths):
    return [i for i in items if any(_path_matches(path, pattern.strip())
            for path in paths for pattern in i['scope'].split(',') if pattern.strip())]


def _path_matches(path, pattern):
    """根相对 glob：* 不跨目录，** 跨目录；单独 * 是显式项目全域。"""
    if pattern == '*':
        return True
    segments = path.split('/')
    positions = {0}
    for token in pattern.split('/'):
        if token == '**':
            positions = set(range(min(positions), len(segments) + 1)) if positions else set()
        else:
            positions = {pos + 1 for pos in positions if pos < len(segments)
                         and fnmatch.fnmatchcase(segments[pos], token)}
    return len(segments) in positions


def render(items):
    if not items:
        return ''
    text = '\n## 本次适用的项目审查改进（有范围、有来源；必须核验证据，不能据此豁免真实缺陷）\n'
    for i in items:
        text += (f"\n### 改进 #{i['id']} · v{i['revision']} · {i['title']}\n"
                 f"类型：{'项目知识' if i['kind'] == 'knowledge' else '核验步骤'}；适用路径：{i['scope']}\n"
                 f"原因：{i['rationale']}\n执行要求：{i['instruction']}\n证据线索（需在当前提交核实）：{i['evidence']}\n")
    if len(text) > 40_000:
        raise RuntimeError('适用的项目改进超过 40000 字符，请收敛范围或撤销过期项；不会静默截断')
    return text


def attach(built, items, paths):
    selected = select(items, paths)
    return {**built, 'improvement_context': render(selected), 'applicable_improvements':selected}, selected


def for_part(items, paths):
    return [i for i in items if i.get('context_version','whole-diff-v1')=='whole-diff-v1' or select([i],paths)]


def fingerprint(iid):
    from . import evaluator, worker
    item = get_item(iid)
    pid = item['project_id']
    project = dict(db.get('SELECT * FROM projects WHERE id=?', (pid,)))
    payload = {'candidate': item, 'config': evaluator._config_snapshot(),
               'profile': project.get('profile'), 'pipeline': worker.get_pipeline(project),
               'active': active_for_project(pid),
               'cases': [dict(r) for r in db.query('SELECT * FROM eval_cases WHERE project_id=? ORDER BY id', (pid,))],
               'sources': [dict(r) for r in db.query('SELECT f.* FROM review_feedback f JOIN improvement_sources s ON s.feedback_id=f.id WHERE s.improvement_id=? ORDER BY f.id', (iid,))],
               'cli': [config.get_kimi_model(), config.get_kimi_base_url(), config.get_claude_model(),
                       hashlib.sha256((config.get_kimi_api_key() + config.get_claude_token()).encode()).hexdigest()]}
    # 运行状态/更新时间不属于策略内容。
    payload['candidate'] = {k: v for k, v in item.items() if k not in ('status', 'updated_at')}
    if payload['candidate'].get('context_version') == 'whole-diff-v1':
        payload['candidate'].pop('context_version',None)  # 旧版本原有输入契约不变。
    return hashlib.sha256(pack(payload).encode()).hexdigest()


def baseline_contract(pid, items=None):
    """只标识基线审查输入，反馈期望不进入模型输入；同一改进内最多复用六小时。"""
    import inspect
    from . import worker
    from .engine import prompts
    data = {'environment':environment_signature(pid),
            'active_text':render(active_for_project(pid) if items is None else items),
            'node_prompt':inspect.getsource(worker._node_prompt),
            'splitter':inspect.getsource(prompts._review_parts),
            'part_limits':[prompts.MAX_REVIEW_PART_LINES,prompts.MAX_REVIEW_PART_CHARS,prompts.MAX_REVIEW_PARTS],
            'part_input_contract':'full-part-v1',
            'cli_binaries':[(str(p), p.stat().st_mtime_ns if p.exists() else None)
                            for p in (Path(config.CLAUDE_BIN or '/nonexistent-claude'),
                                      Path(config.CODEX_BIN or '/nonexistent-codex'))]}
    scoped=[(i['id'],i['revision'],i['context_version']) for i in (active_for_project(pid) if items is None else items)
            if i.get('context_version','whole-diff-v1')!='whole-diff-v1']
    if scoped:
        data['scoped_context_versions']=scoped
        data['scope_contract']=[inspect.getsource(f) for f in (for_part,select,_path_matches,prompts._part_paths)]
    return hashlib.sha256(pack(data).encode()).hexdigest()


def reuse_baseline(iid, vid, case, contract):
    from .engine import verdict
    if get_item(iid)['project_id'] != case['project_id'] or not contract or contract != baseline_contract(case['project_id']):
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    source = db.get('SELECT trigger,base_ref,head_sha FROM runs WHERE id=?', (case['run_id'],))
    if not source or source['head_sha'] != case['head_sha']:
        return None
    for row in db.query("SELECT v.* FROM improvement_validations v JOIN review_improvements i ON i.id=v.improvement_id WHERE i.project_id=? AND v.id<>? AND v.status IN ('done','failed') ORDER BY v.id DESC LIMIT 10", (case['project_id'],vid)):
        snapshot = json.loads(row['snapshot'])
        arm = 'baseline' if snapshot.get('baseline_contract') == contract else 'candidate' if snapshot.get('candidate_contract') == contract else None
        if arm is None:
            continue
        if snapshot.get('replay_sources',{}).get(str(case['run_id'])) != dict(source):
            continue
        old_cases = {v['id']:v for v in snapshot['cases']}
        for pair in sorted(json.loads(row['results']), key=lambda p:p['case_id'] != case['id']):
            old = old_cases.get(pair['case_id'])
            result = pair.get(arm, {})
            if not old or any(old.get(k) != case.get(k) for k in ('run_id','head_sha','intent')):
                continue
            if result.get('result') == 'error' or 'actual' not in result or not result.get('reviews'):
                continue
            if not all(verdict.extract_findings(r.get('output','')) is not None for r in result['reviews']):
                continue
            collected_at=result.get('collected_at') or row['created_at']
            if datetime.fromisoformat(collected_at.replace('Z','+00:00')) < cutoff:
                continue
            expected = json.loads(case['expected'])
            matched = any(verdict.similar(f,expected) for f in result['actual'])
            copied = json.loads(pack(result))
            judgement=pair.get('human_judgement') or result.get('prior_human_judgement')
            same_judgement = (judgement and old.get('expected') == case.get('expected')
                              and old.get('idx') == case.get('idx') and old.get('polarity') == case.get('polarity'))
            if not same_judgement:
                copied.pop('machine_result', None)
                copied.pop('prior_human_judgement', None)
            copied.update(case_id=case['id'],polarity=case['polarity'],expected=expected['title'],
                          result=result['result'] if same_judgement else 'hit' if matched == (case['polarity']=='positive') else ('miss' if case['polarity']=='positive' else 'fp'),
                          detail=f"复用同配置真实完整输出作为基线；不是本轮重新调用，原始采集时间不因复用延长",
                          reused_from_validation=result.get('reused_from_validation',row['id']),
                          reused_arm=result.get('reused_arm',arm),
                          original_case_id=result.get('original_case_id',pair['case_id']),collected_at=collected_at)
            copied['reused_from_improvement'] = result.get('reused_from_improvement',row['improvement_id'])
            if same_judgement:
                copied['prior_human_judgement'] = judgement
            return copied
    return None


def start_validation(iid):
    from . import evaluator, worker
    if not evaluator._eval_lock.acquire(blocking=False):
        raise HTTPException(409, '已有评测或改进验证运行中')
    try:
        c = db.conn()
        with c:
            c.execute('BEGIN IMMEDIATE')
            item = get_item(iid)
            if item['status'] in ('active', 'analyzing', 'validating'):
                raise HTTPException(409, '当前状态不能发起验证')
            cases = [dict(r) for r in db.query('SELECT * FROM eval_cases WHERE project_id=? ORDER BY id', (item['project_id'],))]
            if len(cases) > 100:
                raise HTTPException(400, '单次最多验证 100 条样本，请整理项目评测集')
            if {r['polarity'] for r in cases} != {'positive', 'negative'}:
                raise HTTPException(400, '采用前必须同时具备真实问题正例与误报负例；请先补充反馈样本')
            sources = db.query('SELECT f.* FROM review_feedback f JOIN improvement_sources s ON s.feedback_id=f.id WHERE s.improvement_id=?', (iid,))
            if not sources or any(not f['active'] for f in sources):
                raise HTTPException(400, '来源反馈已失效')
            source_keys = {(f['run_id'], f['idx']) for f in sources}
            if not source_keys.issubset({(r['run_id'], r['idx']) for r in cases}):
                raise HTTPException(400, '来源样本已被删除，请从报告重新保存')
            if not any((r['run_id'], r['idx']) not in source_keys for r in cases):
                raise HTTPException(400, '至少需要一条未参与起草的保护样本')
            from .engine import prompts
            snapshot = {'candidate': item, 'baseline': active_for_project(item['project_id']), 'cases': cases,
                        'execution_limits':{'global_concurrency':config.REVIEW_CONCURRENCY,
                                            'per_cli_provider_concurrency':config.REVIEW_CLI_CONCURRENCY,
                                            'agent_timeout_seconds':config.REVIEW_AGENT_TIMEOUT_SECONDS},
                        'adoption_policy':ADOPTION_POLICY,
                        'baseline_contract':baseline_contract(item['project_id']),
                        'candidate_contract':baseline_contract(item['project_id'], [*active_for_project(item['project_id']),item]),
                        'replay_sources':{str(c['run_id']):dict(db.get('SELECT trigger,base_ref,head_sha FROM runs WHERE id=?',(c['run_id'],))) for c in cases},
                        'environment': {'prompt': prompts.get_template(), 'rules': (config.POLICY_DIR / 'REVIEW.md').read_text(),
                                        'pipeline': worker.get_pipeline(dict(db.get('SELECT * FROM projects WHERE id=?', (item['project_id'],))))},
                        'sources': [dict(s) for s in sources], 'source_keys': [list(k) for k in source_keys]}
            vid = c.execute('INSERT INTO improvement_validations(improvement_id,revision,fingerprint,snapshot,created_at) VALUES(?,?,?,?,?)',
                            (iid, item['revision'], fingerprint(iid), pack(snapshot), db.now())).lastrowid
            c.execute("UPDATE review_improvements SET status='validating',updated_at=? WHERE id=?", (db.now(), iid))
            event(c, iid, 'validation_started', {'validation_id': vid, 'cases': len(cases),
                  'executions': len({(v['run_id'], v['head_sha'], v['intent']) for v in cases}) * 2})
        threading.Thread(target=_validate, args=(vid,), daemon=True, name=f'hub-improve-{vid}').start()
        return {'ok': True, 'validation_id': vid}
    except Exception:
        evaluator._eval_lock.release()
        raise


def _completed_arm(result):
    from .engine import verdict
    if result.get('collected_at') and datetime.fromisoformat(result['collected_at'].replace('Z','+00:00')) < datetime.now(timezone.utc)-timedelta(hours=6):
        return False
    return (result.get('result') in ('hit','miss','fp') and isinstance(result.get('actual'),list)
            and bool(result.get('reviews')) and all(not r.get('incomplete') and
            verdict.extract_findings(r.get('output','')) is not None for r in result['reviews']))


def resume_validation(vid):
    from . import evaluator
    if not evaluator._eval_lock.acquire(blocking=False):
        raise HTTPException(409,'已有评测或改进验证运行中')
    try:
        with db.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM improvement_validations WHERE id=?',(vid,)).fetchone()
            if not row:
                raise HTTPException(404,'验证不存在')
            iid=row['improvement_id'];item=get_item(iid)
            latest=db.get('SELECT MAX(id) n FROM improvement_validations WHERE improvement_id=?',(iid,))['n']
            if row['status']!='failed' or latest!=vid or item['status']!='needs_changes' or row['fingerprint']!=fingerprint(iid):
                raise HTTPException(409,'只能继续当前候选的最新失败验证；配置或反馈变化后需重新验证')
            if datetime.fromisoformat(row['created_at'].replace('Z','+00:00')) < datetime.now(timezone.utc)-timedelta(hours=6):
                raise HTTPException(409,'本轮已超过六小时，请重新验证')
            saved=json.loads(row['results'])
            retained=sum(_completed_arm(p.get(a,{})) for p in saved for a in ('baseline','candidate'))
            c.execute("UPDATE improvement_validations SET status='running',error='',finished_at=NULL,review_note='',eligible=0 WHERE id=?",(vid,))
            c.execute("UPDATE review_improvements SET status='validating',updated_at=? WHERE id=?",(db.now(),iid))
            event(c,iid,'validation_resumed',{'validation_id':vid,'previous_error':row['error'],
                  'retained_arms':retained,'message':f'继续未完成的验证；保留 {retained} 个有完整原始输出的已完成侧，其他侧重新调用；原失败原因保留在历史。'})
        threading.Thread(target=_validate,args=(vid,saved),daemon=True,name=f'hub-improve-{vid}').start()
        return {'ok':True,'validation_id':vid,'retained_arms':retained}
    except Exception:
        evaluator._eval_lock.release()
        raise


def _validate(vid, saved=None):
    from . import evaluator
    row = db.get('SELECT * FROM improvement_validations WHERE id=?', (vid,))
    iid, results, replay_cache = row['improvement_id'], [], {}
    snapshot = json.loads(row['snapshot'])
    previous={p['case_id']:p for p in (saved or [])}
    try:
        for case in snapshot['cases']:
            pair = {'case_id': case['id'], 'polarity': case['polarity'], 'expected': json.loads(case['expected']),
                    'source': [case['run_id'], case['idx']] in snapshot['source_keys'],
                    'baseline':{'result':'not_run','detail':'当前策略尚未完成'},
                    'candidate':{'result':'not_run','detail':'候选策略尚未完成'}}
            for arm in ('baseline', 'candidate'):
                if fingerprint(iid) != row['fingerprint']:
                    raise RuntimeError('反馈、策略、配置或样本已变化，本轮验证失效')
                def progress(stage, trace):
                    db.execute('UPDATE improvement_validations SET progress=? WHERE id=?',
                               (pack({'case_id': case['id'], 'arm': arm, 'stage': stage, 'trace': trace,
                                      'done': len(results), 'total': len(snapshot['cases'])}), vid))
                items = snapshot['baseline'] + ([snapshot['candidate']] if arm == 'candidate' else [])
                key = (case['run_id'], case['head_sha'], case['intent'], arm)
                cached = reuse_baseline(iid,vid,case,snapshot.get('baseline_contract')) if arm=='baseline' else None
                retained=previous.get(case['id'],{}).get(arm,{})
                if _completed_arm(retained):
                    pair[arm]={**retained,'continued_from_validation':vid}
                    replay_cache[key]=pair[arm]
                    if previous[case['id']].get('human_judgement') and all(_completed_arm(previous[case['id']].get(a,{})) for a in ('baseline','candidate')):
                        pair['human_judgement']=previous[case['id']]['human_judgement']
                elif cached:
                    pair[arm] = cached
                elif key in replay_cache:
                    # 同一原始区间的多条样本共用完整评审输出，分别比对各自期望。
                    from .engine import verdict
                    result = dict(replay_cache[key])
                    result.pop('machine_result',None)
                    result.pop('prior_human_judgement',None)
                    matched = any(verdict.similar(f, pair['expected']) for f in result['actual'])
                    result.update(case_id=case['id'], polarity=case['polarity'], expected=pair['expected']['title'],
                                  result='hit' if matched == (case['polarity'] == 'positive') else ('miss' if case['polarity'] == 'positive' else 'fp'),
                                  detail='复用同一原始审查区间的完整输出；匹配预期' if matched else '复用同一原始审查区间的完整输出；未匹配预期')
                    pair[arm] = result
                else:
                    pair[arm] = evaluator._run_case(case, progress, improvement_items=items)
                    if pair[arm]['result'] != 'error' and 'actual' in pair[arm]:
                        replay_cache[key] = pair[arm]
                if pair[arm]['result'] == 'error':
                    if arm == 'baseline':
                        pair['candidate'] = {'result':'not_run','detail':'基线执行失败，本轮不再调用候选模型'}
                    results.append(pair)
                    db.execute('UPDATE improvement_validations SET results=? WHERE id=?',(pack(results),vid))
                    raise RuntimeError(f"{'基线' if arm == 'baseline' else '候选'}执行失败：{pair[arm].get('detail','未知错误')}；已停止后续调用，请修复执行问题后重试")
                db.execute('UPDATE improvement_validations SET progress=? WHERE id=?',
                           (pack({'case_id': case['id'], 'arm': arm, 'stage': '本侧完成', 'partial': pair,
                                  'done': len(results), 'total': len(snapshot['cases'])}), vid))
                db.execute('UPDATE improvement_validations SET results=? WHERE id=?',
                           (pack([*results, pair]), vid))
            results.append(pair)
            db.execute('UPDATE improvement_validations SET results=? WHERE id=?', (pack(results), vid))
        if fingerprint(iid) != row['fingerprint']:
            raise RuntimeError('验证完成时配置已变化，请重新验证')
        # 来源完成、至少一项来源改善、已正确的结果不退化；遗留漏报明确保留。
        eligible = eligible_results(results)
        c = db.conn()
        with c:
            c.execute("UPDATE improvement_validations SET status='done',eligible=?,finished_at=? WHERE id=?", (int(eligible), db.now(), vid))
            c.execute('UPDATE review_improvements SET status=?,updated_at=? WHERE id=? AND revision=? AND status=\'validating\'',
                      ('ready' if eligible else 'needs_changes', db.now(), iid, row['revision']))
            event(c, iid, 'validation_finished', {'validation_id': vid, 'eligible': bool(eligible),
                  'policy':ADOPTION_POLICY,
                  'message': '可采用：来源完成且有改善，无新增退化；遗留问题仍需继续改进' if eligible else '未达到采用条件：来源需完成并改善，既有正确结果不得退化，执行不能出错'})
    except Exception as exc:
        c = db.conn()
        with c:
            c.execute("UPDATE improvement_validations SET status='failed',error=?,finished_at=? WHERE id=?", (str(exc), db.now(), vid))
            c.execute("UPDATE review_improvements SET status='needs_changes',updated_at=? WHERE id=? AND revision=? AND status='validating'", (db.now(), iid, row['revision']))
            event(c, iid, 'validation_failed', {'validation_id': vid, 'message': str(exc)})
    finally:
        evaluator._eval_lock.release()
        db.conn().close()
        db._local.conn = None


def adopt(iid):
    c = db.conn()
    with c:
        c.execute('BEGIN IMMEDIATE')
        item = get_item(iid)
        if item['status'] == 'active':
            if item['activation_signature'] != environment_signature(item['project_id']):
                raise HTTPException(409, '运行配置已变化，本项暂停生效；请先撤销并重新验证')
            return {'ok': True}
        val = db.get('SELECT * FROM improvement_validations WHERE improvement_id=? ORDER BY id DESC LIMIT 1', (iid,))
        if item['status'] != 'ready' or not val or not val['review_note'] or not val['eligible'] or val['status'] != 'done' or val['fingerprint'] != fingerprint(iid):
            raise HTTPException(409, '没有适用于当前配置和反馈的通过验证；请重新验证')
        c.execute("UPDATE review_improvements SET status='active',activation_signature=?,updated_at=? WHERE id=?", (environment_signature(item['project_id']), db.now(), iid))
        event(c, iid, 'adopted', {'revision': item['revision'], 'validation_id': val['id'], 'message': '仅对之后开始的本项目审查生效，按路径选择，不修改公共规则'})
    return {'ok': True}


def revoke(iid):
    c = db.conn()
    with c:
        c.execute('BEGIN IMMEDIATE')
        item = get_item(iid)
        if item['status'] != 'active':
            raise HTTPException(409, '只有已生效改进可以撤销')
        c.execute("UPDATE review_improvements SET status='revoked',updated_at=? WHERE id=?", (db.now(), iid))
        event(c, iid, 'revoked', {'message': '后续审查停止使用；已开始审查保留原版本与使用记录'})
    return {'ok': True}


def recover_interrupted():
    c = db.conn()
    with c:
        for r in c.execute("SELECT * FROM review_improvements WHERE status IN ('validating','analyzing')").fetchall():
            c.execute("UPDATE review_improvements SET status='needs_changes' WHERE id=?", (r['id'],))
            event(c, r['id'], 'interrupted', {'message': '服务重启中断任务，保留已有结果，请重试'})
        c.execute("UPDATE improvement_validations SET status='failed',error='服务重启中断验证',finished_at=? WHERE status='running'", (db.now(),))


def get_detail(iid):
    item = get_item(iid)
    item['effective'] = item['status'] == 'active' and item['activation_signature'] == environment_signature(item['project_id'])
    item['feedback_ids'] = [r['feedback_id'] for r in db.query('SELECT feedback_id FROM improvement_sources WHERE improvement_id=?', (iid,))]
    item['events'] = [dict(r) | {'detail': json.loads(r['detail'])} for r in db.query('SELECT * FROM improvement_events WHERE improvement_id=? ORDER BY id DESC', (iid,))]
    item['validations'] = []
    for r in db.query('SELECT * FROM improvement_validations WHERE improvement_id=? ORDER BY id DESC LIMIT 10', (iid,)):
        v = dict(r)
        for key in ('snapshot', 'results', 'progress'):
            v[key] = json.loads(v[key]) if v[key] else None
        v['stale'] = v['fingerprint'] != fingerprint(iid) if item['status'] != 'active' else not item['effective']
        item['validations'].append(v)
    item['uses'] = [dict(r) for r in db.query('SELECT u.run_id,u.revision,u.created_at,r.status,r.important_count FROM improvement_uses u JOIN runs r ON r.id=u.run_id WHERE improvement_id=? ORDER BY u.run_id DESC LIMIT 50', (iid,))]
    item['followup'] = [dict(r) | {'finding': json.loads(r['finding'])} for r in db.query('SELECT f.* FROM review_feedback f JOIN improvement_uses u ON u.run_id=f.run_id WHERE u.improvement_id=? AND f.active=1 ORDER BY f.id DESC LIMIT 50', (iid,))]
    return item


def overview():
    feedback = []
    for r in db.query('SELECT f.*,p.name AS project FROM review_feedback f JOIN projects p ON p.id=f.project_id ORDER BY f.updated_at DESC LIMIT 500'):
        value = dict(r)
        value['finding'] = json.loads(r['finding'])
        value['history'] = [dict(h) | {'snapshot': json.loads(h['snapshot'])} for h in db.query('SELECT * FROM feedback_events WHERE feedback_id=? ORDER BY id DESC', (r['id'],))]
        value['improvement_ids'] = [x['improvement_id'] for x in db.query('SELECT improvement_id FROM improvement_sources WHERE feedback_id=?', (r['id'],))]
        feedback.append(value)
    items = [dict(r) for r in db.query('SELECT i.*,p.name AS project,(SELECT COUNT(*) FROM improvement_sources s WHERE s.improvement_id=i.id) AS source_count,(SELECT COUNT(*) FROM improvement_uses u WHERE u.improvement_id=i.id) AS use_count FROM review_improvements i JOIN projects p ON p.id=i.project_id ORDER BY i.updated_at DESC LIMIT 500')]
    for item in items:
        item['effective'] = item['status'] == 'active' and item['activation_signature'] == environment_signature(item['project_id'])
    return {'feedback': feedback, 'items': items, 'limit': 500,
            'totals': {'feedback': db.get('SELECT COUNT(*) n FROM review_feedback')[0], 'items': db.get('SELECT COUNT(*) n FROM review_improvements')[0]}}


def start_draft(feedback_ids):
    """显式启动一次只读分析；模型只能起草，不能采用。复用评测并发预算。"""
    from . import evaluator
    if not evaluator._eval_lock.acquire(blocking=False):
        raise HTTPException(409, '已有评测或改进分析运行中')
    try:
        ids = sorted(set(feedback_ids))
        rows = [db.get('SELECT * FROM review_feedback WHERE id=? AND active=1', (fid,)) for fid in ids]
        if not rows or len(rows) > 20 or any(r is None or not r['reason'].strip() for r in rows):
            raise HTTPException(400, '请选择 1–20 条有判定原因的有效反馈')
        first = rows[0]
        item = save_candidate({'feedback_ids': ids, 'title': json.loads(first['finding'])['title'],
                               'kind': 'check', 'scope': json.loads(first['finding']).get('file') or '*',
                               'rationale': first['reason'], 'instruction': '等待模型分析后填写具体核验要求',
                               'evidence': first['evidence'] or '等待在原始提交中核验证据'})
        iid = item['id']
        with db.conn() as c:
            c.execute("UPDATE review_improvements SET status='analyzing' WHERE id=?", (iid,))
            event(c, iid, 'analysis_started', {'message': '在原始提交的独立副本只读核查反馈，结果仅保存为候选'})
        threading.Thread(target=_draft, args=(iid,), daemon=True, name=f'hub-draft-{iid}').start()
        return {'ok': True, 'id': iid}
    except Exception:
        evaluator._eval_lock.release()
        raise


def _draft(iid):
    import re
    import shutil
    import tempfile
    from pathlib import Path
    from . import evaluator, worker
    parent = None
    initial = get_item(iid)
    try:
        project = db.get('SELECT * FROM projects WHERE id=?', (initial['project_id'],))
        if not project or project['kind'] != 'github':
            raise RuntimeError('自动分析需要可独立检出的 GitHub 项目；本地项目可手动建立候选')
        feedback = [dict(r) for r in db.query('SELECT f.* FROM review_feedback f JOIN improvement_sources s ON s.feedback_id=f.id WHERE s.improvement_id=? ORDER BY f.id', (iid,))]
        run = db.get('SELECT * FROM runs WHERE id=?', (feedback[0]['run_id'],))
        if not run or not run['head_sha']:
            raise RuntimeError('来源缺少可检出的提交')
        members = [m for s in worker.get_pipeline(project) if s.get('enabled', True) and s.get('kind') == 'review'
                   for m in s.get('reviewers', []) if m.get('enabled', True)]
        if not members:
            raise RuntimeError('项目没有启用的评审器，请配置后重试')
        parent = Path(tempfile.mkdtemp(prefix=f'hub-improve-{iid}-', dir=config.DATA_DIR))
        repo = parent / 'repo'
        worker._ensure_clone(project, repo)
        worker._git_retry(repo, 'checkout', '--detach', '-f', run['head_sha'])
        diff = repo / '.hub-feedback.patch'
        diff.write_text(run['diff_text'] or '', encoding='utf-8')
        task = ('核查以下人工反馈及其证据，提炼一个本项目的审查改进。你是只读分析员，不修改仓库。'
                '人工判定可能不完整，不能把误报标题改写成禁止报告。描述成立条件、核验路径、仍应报告的例外。'
                '只基于可核验内容起草；证据不足须在 evidence 中明确写明待核实。其他来源提交可能不同，必须区分。'
                '只输出一个 JSON 代码块，字段 title,kind(knowledge 或 check),scope(逗号分隔的仓库路径 glob),'
                'rationale,instruction,evidence。每字段最多3000字符，title最多160，scope最多1000。\n'
                + pack(feedback))
        step = {**members[0], 'purpose': 'context', 'name': '分析反馈并起草候选', 'prompt': task}
        output, model_trace, actual_input = worker._run_reviewer_step(step, project, repo, {'diff_file': diff, 'profile': project['profile'], 'intent': run['intent']}, diff.read_text())
        blocks = re.findall(r'```(?:json)?\s*(.*?)```', output, flags=re.S)
        candidate = json.loads(blocks[-1] if blocks else output)
        if not isinstance(candidate, dict):
            raise RuntimeError('模型未返回候选对象')
        # 借用同一保存校验；在事务内确认反馈没有改变。
        c = db.conn()
        with c:
            c.execute('BEGIN IMMEDIATE')
            current = get_item(iid)
            if current['status'] != 'analyzing' or current['revision'] != initial['revision']:
                raise RuntimeError('分析期间反馈已变化，请基于新反馈重新起草')
            # save_candidate 自己持有事务，因此此处只提交状态转移。
            c.execute("UPDATE review_improvements SET status='draft' WHERE id=?", (iid,))
        candidate.update(feedback_ids=[f['id'] for f in feedback], revision=initial['revision'])
        save_candidate(candidate, iid)
        with db.conn() as c:
            event(c, iid, 'analysis_finished', {'message': '模型已起草；请核对原因、路径和证据，验证通过前不会生效',
                  'provider': members[0].get('provider') or members[0].get('kind'), 'source_sha': run['head_sha'],
                  'output': output, 'trace': model_trace or [], 'prompt': actual_input})
    except Exception as exc:
        with db.conn() as c:
            c.execute("UPDATE review_improvements SET status='needs_changes' WHERE id=? AND status='analyzing'", (iid,))
            event(c, iid, 'analysis_failed', {'message': str(exc), 'trace': getattr(exc, 'trace', [])})
    finally:
        if parent:
            shutil.rmtree(parent, ignore_errors=True)
        evaluator._eval_lock.release()
        db.conn().close()
        db._local.conn = None


def eligible_results(results, policy=ADOPTION_POLICY):
    improved = any(p['source'] and p['baseline']['result'] in ('fp','miss') and p['candidate']['result']=='hit' for p in results)
    if policy == 'all-hit-v1':
        return bool(results) and improved and all(p['candidate']['result']=='hit' and p['baseline']['result']!='error' for p in results)
    return bool(results) and improved and all(
        p['baseline']['result'] in ('hit','miss','fp') and p['candidate']['result'] in ('hit','miss','fp')
        and (not p['source'] or p['candidate']['result']=='hit')
        and (p['baseline']['result']!='hit' or p['candidate']['result']=='hit') for p in results)


def review_validation(vid, body):
    note = str(body.get('note', '')).strip()
    if not note or len(note) > 3000:
        raise HTTPException(400, '请填写核对依据，最多 3000 字符')
    c = db.conn()
    with c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM improvement_validations WHERE id=?', (vid,)).fetchone()
        if not row:
            raise HTTPException(404, '验证记录不存在')
        iid = row['improvement_id']
        item = get_item(iid)
        latest = db.get('SELECT MAX(id) id FROM improvement_validations WHERE improvement_id=?', (iid,))['id']
        if latest != vid or row['status'] != 'done' or item['status'] not in ('ready', 'needs_changes') or row['fingerprint'] != fingerprint(iid):
            raise HTTPException(409, '只能核对当前候选的最新有效验证')
        results = json.loads(row['results'])
        if 'case_id' in body:
            pair = next((p for p in results if p['case_id'] == body['case_id']), None)
            if not pair:
                raise HTTPException(400, '样本不存在')
            for arm in ('baseline', 'candidate'):
                result = body.get(arm)
                if result not in ('hit', 'miss' if pair['polarity'] == 'positive' else 'fp') or pair[arm]['result'] == 'error':
                    raise HTTPException(400, '不能更改执行错误，或使用与样本类型不符的结果')
                pair[arm].setdefault('machine_result', pair[arm]['result'])
                pair[arm]['result'] = result
            pair['human_judgement'] = note
            eligible = eligible_results(results, json.loads(row['snapshot']).get('adoption_policy','all-hit-v1'))
            c.execute('UPDATE improvement_validations SET results=?,eligible=?,review_note=\'\' WHERE id=?', (pack(results), int(eligible), vid))
            c.execute('UPDATE review_improvements SET status=? WHERE id=?', ('ready' if eligible else 'needs_changes', iid))
            event(c, iid, 'comparison_corrected', {'validation_id': vid, 'case_id': pair['case_id'],
                  'baseline': body['baseline'], 'candidate': body['candidate'], 'message': note})
        else:
            if not row['eligible']:
                raise HTTPException(409, '验证未达到采用条件，请调整候选或纠正错误的匹配判定')
            c.execute('UPDATE improvement_validations SET review_note=? WHERE id=?', (note, vid))
            event(c, iid, 'comparison_reviewed', {'validation_id': vid, 'message': note})
    return {'ok': True}


def reassess_validation(vid, note):
    note = str(note or '').strip()
    if not note or len(note)>3000:
        raise HTTPException(400,'请说明采用规则复核的依据，最多3000字符')
    c=db.conn()
    with c:
        c.execute('BEGIN IMMEDIATE')
        row=db.get('SELECT * FROM improvement_validations WHERE id=?',(vid,))
        if not row:
            raise HTTPException(404,'验证不存在')
        item=get_item(row['improvement_id'])
        latest=db.get('SELECT MAX(id) n FROM improvement_validations WHERE improvement_id=?',(item['id'],))['n']
        if latest!=vid or row['status']!='done' or item['status'] not in ('ready','needs_changes') or row['fingerprint']!=fingerprint(item['id']):
            raise HTTPException(409,'只能复核当前候选的最新完整有效验证')
        snapshot=json.loads(row['snapshot']);old_policy=snapshot.get('adoption_policy','all-hit-v1')
        results=json.loads(row['results']);eligible=eligible_results(results)
        snapshot['adoption_policy']=ADOPTION_POLICY
        c.execute('UPDATE improvement_validations SET snapshot=?,eligible=?,review_note=\'\' WHERE id=?',
                  (pack(snapshot),int(eligible),vid))
        c.execute('UPDATE review_improvements SET status=? WHERE id=?',('ready' if eligible else 'needs_changes',item['id']))
        event(c,item['id'],'adoption_policy_reassessed',{'validation_id':vid,'previous_policy':old_policy,'policy':ADOPTION_POLICY,
              'previous_eligible':bool(row['eligible']),'eligible':eligible,
              'remaining_samples':sum(p['candidate']['result'] in ('miss','fp') for p in results),
              'message':note+'；仅重算采用判断，原始模型输出与逐样本结果未改写，仍需填写最终核对依据。'})
    return {'ok':True,'eligible':eligible,'policy':ADOPTION_POLICY}


def save_review_evidence(output, trace, prompt):
    """大证据按内容去重，数据库只存引用，页面按需读取，避免每条样本重复加载。"""
    import tempfile
    payload=pack({'output':output,'trace':trace or [],'prompt':prompt}).encode('utf-8')
    if len(payload)>96_000_000:
        raise RuntimeError('完整评审证据超过96MB保存上限，不能丢弃证据后判定验证成功')
    digest=hashlib.sha256(payload).hexdigest()
    folder=config.DATA_DIR/'review-evidence';folder.mkdir(mode=0o700,exist_ok=True)
    target=folder/f'{digest}.json'
    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=folder,delete=False) as handle:
                temporary=Path(handle.name);handle.write(payload)
            temporary.replace(target)
        finally:
            if temporary and temporary.exists():temporary.unlink()
    return digest


def read_review_evidence(digest):
    import re
    if not re.fullmatch('[a-f0-9]{64}',digest):
        raise HTTPException(404,'证据不存在')
    file=config.DATA_DIR/'review-evidence'/f'{digest}.json'
    if not file.is_file():
        raise HTTPException(404,'证据不存在或已清理')
    raw=file.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:
        raise HTTPException(500,'证据文件校验失败，请检查存储')
    return json.loads(raw)
