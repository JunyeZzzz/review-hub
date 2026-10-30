// Execute the actual application functions with controlled browser boundaries.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');

function load(name, globals) {
  const start = source.search(new RegExp(`(?:async\\s+)?function\\s+${name}\\(`));
  assert.ok(start >= 0, `missing application function ${name}`);
  const context = vm.createContext(globals);
  for (let end = source.indexOf('\n}', start); end >= 0; end = source.indexOf('\n}', end + 2)) {
    let script;
    try { script = new vm.Script(`(${source.slice(start, end + 2)})`); }
    catch (error) { if (error instanceof SyntaxError) continue; throw error; }
    return script.runInContext(context);
  }
  throw new Error(`cannot parse complete function ${name}`);
}

const cases = {
  async project_completion_after_navigation_does_not_replace_the_page() {
    for (const fail of [false, true]) {
      let finish, reject, mutations = 0;
      const pending = new Promise((resolve, rejecter) => {finish=resolve;reject=rejecter;});
      const location = {hash: '#/projects/11'};
      const render = load('renderProjectDetail', {
        location, window: {scrollY:0}, jsCommitCache:{}, detailSig:'',
        setTitle() {}, showSkeleton() {},
        api: path => path === '/api/projects' ? pending : path.includes('/commits') ? new Promise(() => {}) : Promise.resolve([]),
        errorPanel: () => {mutations++;}, paintProjectDetail: () => {mutations++;},
        document:{body:{classList:{add(){mutations++;}}},querySelector:()=>null},
        requestAnimationFrame(){}, autoRefresh(){mutations++;},
      });
      const done = render(11, false);
      location.hash = '#/workbench';
      if (fail) reject(new Error('network unavailable')); else finish([{id:11}]);
      await done;
      assert.equal(mutations,0,`late ${fail ? 'error' : 'success'} must not replace the new page`);
    }
  },
  async project_credentials_failure_is_visible() {
    const errors = [];
    const render = load('renderProjectPipeline', {
      setTitle() {}, showSkeleton() {}, errorPanel: message => errors.push(message),
      api: async path => {
        if (path === '/api/projects') return [{id: 11, name: 'hub'}];
        if (path === '/api/projects/11/pipeline') return {steps: []};
        throw new Error('凭证服务暂不可用');
      }, location: {hash: '#/projects/11/pipeline'},
    });
    await render(11);
    assert.deepEqual(errors, ['凭证服务暂不可用']);
  },
  async legacy_validation_policy_is_visible() {
    const render = load('imValidationHtml', {esc: String});
    const validation = {id: 1, status: 'done', results: [], snapshot: {cases: []}};
    assert.match(render(validation), /本轮最初使用旧规则 all-hit-v1/);
    validation.snapshot.adoption_policy = 'incremental-v2';
    assert.doesNotMatch(render(validation), /本轮最初使用旧规则 all-hit-v1/);
  },
  async leave_during_evaluation() {
    let tick, finish, changed = 0;
    const location = {hash: '#/workbench'};
    const pending = new Promise(resolve => { finish = resolve; });
    const poll = load('pollEval', {
      location,
      autoRefresh: fn => { tick = fn; },
      refreshEvalLive: () => pending,
      api: async () => ({cases: []}), evalCasesHtml: () => '',
      bindEvalActions: () => { changed++; },
      renderWorkbench: () => { changed++; },
      toast: () => { changed++; },
      $: () => null,
    });
    poll();
    const result = tick();
    location.hash = '#/runs/99';
    finish(false);
    await result;
    assert.equal(changed, 0, 'completion must not mutate the page after navigation');
  },
  async terminal_report_stops_polling() {
    const timers = [];
    const render = load('renderRun', {
      location: {hash: '#/runs/99'},
      runCache: {99: {id: 99, status: 'running'}},
      api: async () => ({id: 99, status: 'passed'}),
      document: {querySelector: () => null},
      patchRunDynamic: () => {},
      autoRefresh: (fn, active) => { timers.push(active); },
    });
    await render(99, true);
    assert.equal(timers.at(-1), false, 'a terminal response must stop report polling');
  },
  async empty_api_credential_is_rejected() {
    let repaint = 0, notices = 0, closed = 0;
    const form = {};
    const dialog = {
      querySelectorAll: () => [], addEventListener: () => {}, showModal: () => {},
      close: () => { closed++; }, remove: () => {},
      querySelector: selector => selector === 'form' ? form : {focus() {}},
    };
    const values = {name: 'API', model: 'real-model', prompt: '', credential_id: null};
    const editor = load('openNodeEditor', {
      document: {createElement: () => dialog, body: {appendChild() {}}},
      nodeCredentials: [], NODE_KIND: {api: 'API'},
      nodeField: () => '', nodeChoiceField: () => '', esc: String,
      toast: () => { notices++; },
      FormData: class {get(name) {return values[name] ?? null;}},
    });
    const draft = [{kind: 'api', name: 'API', model: 'real-model', purpose: 'findings', credential_id: null}];
    editor(draft, 0, () => { repaint++; });
    // Direct submit also covers an empty radio group bypassing browser required validation.
    form.onsubmit({preventDefault() {}, currentTarget: form});
    assert.equal(draft[0].credential_id, null, 'missing credential must not become numeric zero');
    assert.equal(repaint, 0);
    assert.equal(closed, 0);
    assert.equal(notices, 1);
  },
};

(async () => {
  const requested = process.argv[3];
  assert.ok(cases[requested], 'unknown scenario');
  await cases[requested]();
  process.stdout.write(`${requested}: passed\n`);
})().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
