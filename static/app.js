/* Review Hub 前端：hash 路由 SPA，无构建依赖 */
"use strict";

const $app = document.getElementById("app");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------------- 全局反馈：加载进度条 ---------------- */

let inflight = 0;
function busy(on) {
  const el = document.getElementById("topbar");
  if (el) el.classList.toggle("on", on);
}

async function api(path, opts = {}) {
  inflight++;
  busy(true);
  try {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    return data;
  } finally {
    inflight--;
    if (!inflight) busy(false);
  }
}

let toastTimer = null;
function toast(msg, isErr = false) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), 4200);
}

/* 统一对话框：操作前说明结果，避免浏览器原生 prompt/confirm。 */
function askDialog({ title, message = "", inputType = "", choices = null, confirmLabel = "确认" }) {
  return new Promise((resolve) => {
    const priorFocus = document.activeElement;
    const dialog = document.createElement("dialog");
    dialog.className = "rh-dialog";
    dialog.setAttribute("aria-labelledby", "rh-dialog-title");
    const field = inputType
      ? `<input class="rh-dialog-input" type="${inputType === "password" ? "password" : "text"}"
          aria-label="${esc(title)}" autocomplete="off">` : "";
    const options = choices
      ? `<div class="rh-dialog-choices">${choices.map((c) =>
          `<button type="button" class="rh-choice" data-choice="${esc(c.value)}">${esc(c.label)}</button>`).join("")}</div>` : "";
    dialog.innerHTML = `<div class="rh-dialog-inner"><div class="rh-dialog-title" id="rh-dialog-title">${esc(title)}</div>
      ${message ? `<p>${esc(message)}</p>` : ""}${field}${options}
      <div class="rh-dialog-actions"><button type="button" class="ghost" data-cancel>取消</button>
        ${choices ? "" : `<button type="button" class="primary" data-confirm>${esc(confirmLabel)}</button>`}</div></div>`;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      dialog.close();
      dialog.remove();
      priorFocus?.focus?.();
      resolve(value);
    };
    dialog.querySelector("[data-cancel]").onclick = () => finish(null);
    dialog.querySelector("[data-confirm]")?.addEventListener("click", () =>
      finish(inputType ? dialog.querySelector(".rh-dialog-input").value.trim() : true));
    dialog.querySelectorAll("[data-choice]").forEach((b) => b.onclick = () => finish(b.dataset.choice));
    dialog.addEventListener("cancel", (e) => { e.preventDefault(); finish(null); });
    dialog.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && inputType) { e.preventDefault(); dialog.querySelector("[data-confirm]").click(); }
    });
    document.body.appendChild(dialog);
    dialog.showModal();
    (dialog.querySelector(".rh-dialog-input") || dialog.querySelector("[data-choice]") ||
      dialog.querySelector("[data-confirm]"))?.focus();
  });
}
async function uiConfirm(message, title = "确认操作") {
  return (await askDialog({ title, message })) === true;
}
async function uiPrompt(title, { message = "", type = "text" } = {}) {
  return askDialog({ title, message, inputType: type, confirmLabel: "继续" });
}
async function uiChoice(title, choices, message = "") {
  return askDialog({ title, message, choices });
}

/* 兜底：任何异步异常都可见，绝不让交互静默失效 */
window.addEventListener("unhandledrejection", (e) => {
  toast(`页面内部错误：${String(e.reason).slice(0, 160)}`, true);
});
window.addEventListener("error", (e) => {
  if (e.message && !e.filename) return;   // 资源加载错误不弹
  toast(`页面内部错误：${String(e.message).slice(0, 160)}`, true);
});

/* ---------------- 主题 ---------------- */

function initTheme() {
  const saved = localStorage.getItem("rh-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  syncThemeBtns();
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("rh-theme", next);
  syncThemeBtns();
}
function syncThemeBtns() {
  const dark = document.documentElement.dataset.theme !== "light";
  for (const id of ["theme-btn", "theme-btn-m"]) {
    const b = document.getElementById(id);
    if (b) b.textContent = dark ? "浅色" : "深色";
  }
}
initTheme();
for (const id of ["theme-btn", "theme-btn-m"]) {
  const b = document.getElementById(id);
  if (b) b.addEventListener("click", toggleTheme);
}

/* 静默刷新不重放入场动画；仅路由切换时淡入一次 */

/* ---------------- 通用组件 ---------------- */

const $ = (id) => document.getElementById(id);
function setTitle(t) { document.title = t ? `${t} · Review Hub` : "Review Hub · 质量控制枢纽"; }
function showSkeleton() {
  $app.innerHTML = `<div class="skel"><div class="skel-line w30"></div><div class="skel-line w60"></div><div class="skel-panel"></div><div class="skel-panel h2"></div></div>`;
}
function errorPanel(msg) {
  $app.innerHTML = `<div class="panel empty" style="margin-top:40px">加载失败：${esc(msg)}<br><br><button class="primary" onclick="navigate()">重试</button></div>`;
}
function pageHead({ back, crumbs, title, sub, actions }) {
  return `<div class="page-head">
    <div>
      ${back ? `<div><button class="ghost back-btn" onclick="location.hash='${esc(back.hash)}'">← ${esc(back.label)}</button></div>` : ""}
      ${crumbs ? `<div class="crumbs">${crumbs}</div>` : ""}
      <h1>${title}</h1>
      ${sub ? `<div class="sub">${sub}</div>` : ""}
    </div>
    ${actions ? `<div class="actions">${actions}</div>` : ""}
  </div>`;
}

const TRIGGER_LABEL = { manual: "手动", push: "push", pr: "PR", merged: "合并后" };
const STATUS_LABEL = { queued: "排队中", running: "审查中", passed: "通过", blocked: "发现问题", failed: "执行失败" };
const TIER_LABEL = { trivial: "琐碎", low: "低", medium: "中", high: "高" };

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso.replace(" ", "T").replace(/(\.\d+)?Z?$/, "Z"));
  if (isNaN(d)) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtDur(ms) {
  if (ms == null) return "—";
  return ms >= 60000 ? `${(ms / 60000).toFixed(1)}m` : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}
function ageDays(iso) {
  if (!iso) return 0;
  const d = new Date(iso.replace(" ", "T").replace(/(\.\d+)?Z?$/, "Z"));
  return isNaN(d) ? 0 : (Date.now() - d.getTime()) / 86400000;
}
function fmtAgo(iso) {
  const days = ageDays(iso);
  if (!iso) return "—";
  if (days < 1 / 24) return `${Math.max(1, Math.round(days * 1440))} 分钟前`;
  if (days < 1) return `${Math.round(days * 24)} 小时前`;
  return `${Math.round(days)} 天前`;
}
function statusHtml(s) {
  return `<span class="status"><span class="dot ${esc(s)}"></span>${STATUS_LABEL[s] || esc(s)}</span>`;
}
function commitUrl(repo, sha) {
  return repo && sha ? `https://github.com/${esc(repo)}/commit/${esc(sha)}` : "";
}
function shaLink(repo, sha) {
  if (!sha) return '<span class="faint">—</span>';
  const short = esc(sha.slice(0, 8));
  const u = commitUrl(repo, sha);
  return u ? `<a class="mono" href="${u}" target="_blank">${short}</a>` : `<span class="mono">${short}</span>`;
}
function fileLink(run, file, line) {
  if (!file) return `<span class="badge gate">门禁</span>`;
  const text = `${esc(file)}:${esc(line)}`;
  if (run.project_kind === "github" && run.repo && run.head_sha) {
    return `<a class="mono fname" target="_blank" title="在 GitHub 打开这一行"
      href="https://github.com/${esc(run.repo)}/blob/${esc(run.head_sha)}/${esc(file)}#L${esc(line)}">${text}</a>`;
  }
  return `<span class="mono fname">${text}</span>`;
}

/* 发现处置标记：全局事件委托（报告页/处置页共用）。
   按钮携带 data-cur（当前标记），点击已选中的按钮 = 撤销标记。 */
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-mark]");
  if (!b) return;
  e.stopPropagation();
  const runId = +b.dataset.run, idx = +b.dataset.idx, val = b.dataset.mark;
  const next = (b.dataset.cur || "") === val ? "" : val;
  try {
    const detail = next ? await imMarkDialog(next) : {};
    if (detail === null) return;
    await api(`/api/runs/${runId}/findings/${idx}/mark`, { method: "POST", body: { disposition: next, ...detail } });
    toast(next ? "反馈与回归样本已保存；到工作台追踪改进进度" : "判定已撤销；关联验证失效，生效改进已撤销");
    if (location.hash === `#/runs/${runId}`) renderRun(runId, true);
    else if (location.hash === "#/workbench") refreshWorkbenchData();
  } catch (err) { toast(err.message, true); }
});

/* 门禁发现的日志直达：展开第一个失败步骤并滚过去 */
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-goto-log]");
  if (!b) return;
  e.stopPropagation();
  const failStep = document.querySelector(".step.s-fail");
  if (!failStep) { toast("页面上没有失败步骤"); return; }
  const det = failStep.querySelector("details");
  if (det) det.open = true;
  failStep.scrollIntoView({ behavior: "smooth", block: "center" });
});

/* 评测用例：全局事件委托（报告页发现卡片上的「存为评测用例」） */
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-eval-run]");
  if (!b) return;
  e.stopPropagation();
  try {
    const r = await api("/api/eval/cases", { method: "POST",
      body: { run_id: +b.dataset.evalRun, idx: +b.dataset.evalIdx } });
    toast(r.existed ? "这条已经在工作台的回归样本里"
      : `已保存为回归样本（${r.polarity === "positive" ? "应报出" : "不应再报出"}）`);
    if (location.hash === "#/workbench") refreshWorkbenchData();
  } catch (err) { toast(err.message, true); }
});

/* ---------------- 路由 ---------------- */

const routes = [
  [/^#?\/?$/, () => renderHome()],
  [/^#\/projects$/, () => renderProjects()],
  [/^#\/projects\/(\d+)$/, (m) => renderProjectDetail(+m[1])],
  [/^#\/projects\/(\d+)\/pipeline$/, (m) => renderProjectPipeline(+m[1])],
  [/^#\/runs(?:\?(.*))?$/, (m) => renderRunsList(m[1] || "")],
  [/^#\/runs\/(\d+)$/, (m) => renderRun(+m[1])],
  [/^#\/workbench$/, () => renderWorkbench()],
  [/^#\/(marks|eval|metrics)$/, () => { location.hash = "#/workbench"; }],
  [/^#\/settings$/, () => renderSettings()],
  [/^#\/settings\/(pipeline|engine|creds)$/, (m) => renderSettings(m[1])],
];

function navigate() {
  const hash = location.hash || "#/";
  document.body.classList.remove("no-anim");
  document.body.classList.add("nav-anim");
  setTimeout(() => document.body.classList.remove("nav-anim"), 400);
  for (const [rx, fn] of routes) {
    const m = hash.match(rx);
    if (m) {
      const h = location.hash || "#/";
      const activeNav =
        h === "#/" ? "home"
        : h.startsWith("#/projects") ? "projects"
        : h.startsWith("#/workbench") ? "workbench"
        : h.startsWith("#/settings") ? "settings"
        : h.startsWith("#/runs/") ? "projects"   // 报告页：从项目/run 表下钻
        : h.startsWith("#/runs") ? "workbench"     // 列表页：工作台下钻
        : "";
      document.querySelectorAll("#nav a, .mobilebar nav a").forEach((a) => {
        a.classList.toggle("active", !!activeNav && a.dataset.nav === activeNav);
      });
      fn(m);
      return;
    }
  }
  location.hash = "#/";
}
window.addEventListener("hashchange", navigate);

let refreshTimer = null;
function autoRefresh(fn, active) {
  clearInterval(refreshTimer);
  if (active) refreshTimer = setInterval(fn, 2500);
}

/* ---------------- 总览 ---------------- */

async function renderHome() {
  clearInterval(refreshTimer);
  setTitle("总览");
  showSkeleton();
  const data = await api("/api/overview").catch((e) => { errorPanel(e.message); return null; });
  if (!data) return;
  const s = data.stats;
  const fpRate = s.marked_total ? Math.round((s.fp_total / s.marked_total) * 100) + "%" : "—";
  $app.innerHTML = `
    ${pageHead({
      title: "总览",
      sub: "每次变更回答三个问题：改了什么、为什么改、改对了吗",
      actions: `<button class="ghost" id="metrics-btn">指标明细 →</button>`,
    })}
    <div class="stats five stagger">
      <div class="panel stat clickable" data-jump="#/projects" title="进入项目列表">
        <div class="num">${s.projects}</div><div class="label">在管项目</div></div>
      <div class="panel stat clickable" data-jump="#/runs?since=${new Date().toLocaleDateString("sv")}" title="看今天的审查">
        <div class="num">${s.runs_today}</div><div class="label">今日审查</div></div>
      <div class="panel stat clickable" data-jump="#/runs?status=blocked" title="看被阻断的审查">
        <div class="num" style="color:var(--bad)">${s.blocked_total}</div><div class="label">发现问题（累计）</div></div>
      <div class="panel stat clickable" data-jump="#/runs?reverted=1" title="查看已创建回退 PR 的审查">
        <div class="num" style="color:var(--warn)">${s.reverted_total}</div><div class="label">已创建回退 PR</div></div>
      <div class="panel stat clickable" data-jump="#/marks" title="打开处置台账">
        <div class="num" style="color:${s.fp_total > s.marked_total / 2 ? "var(--warn)" : "inherit"}">${fpRate}</div>
        <div class="label">误报率（已处置 ${s.marked_total || 0} 条）</div></div>
    </div>
    <div class="panel stagger" id="health-bar" style="padding:10px 18px;margin-bottom:24px"></div>
    <div class="sec-title stagger">最近审查 <span class="faint small mono" style="letter-spacing:0;text-transform:none">点行看报告</span></div>
    <div class="panel stagger">${runTable(data.recent, true)}</div>`;
  $("metrics-btn").onclick = () => { location.hash = "#/metrics"; };
  document.querySelectorAll("[data-jump]").forEach((el) =>
    (el.onclick = () => { location.hash = el.dataset.jump; }));
  bindRunRows();
  updateHealth();
  autoRefresh(updateHealth, true);
}

/* 系统健康条：队列/worker/轮询/近7天失败率——自反馈系统对自己健康的反馈 */
function updateHealth() {
  api("/api/health").then((h) => {
    const el = document.getElementById("health-bar");
    if (!el) return;
    const poll = h.poll || {};
    const pollTxt = poll.ok === true ? `<span class="add">轮询 ✓</span>（${poll.age_s}s 前）`
      : poll.ok === false ? `<span class="del">轮询 ✗</span> ${esc(poll.err || "异常")}`
      : "轮询未启用（未配置 GitHub Token）";
    const failRate = h.total_7d ? Math.round((h.fail_7d / h.total_7d) * 100) : 0;
    const bad = h.queued > 3 || failRate > 20 || poll.ok === false;
    el.style.borderColor = bad ? "color-mix(in srgb, var(--bad) 50%, transparent)" : "";
    el.innerHTML = `<div class="mono small" style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
      <span class="ctx-label" style="margin:0">系统状态</span>
      <span>队列 ${h.queued}</span><span>进行中 ${h.running}</span><span>worker ×${h.workers}</span>
      <span>${pollTxt}</span>
      <span>近 7 天失败 ${failRate}%（${h.fail_7d}/${h.total_7d}）</span>
      ${bad ? '<span class="badge imp">需要关注</span>' : '<span class="badge confirmed">正常</span>'}
    </div>`;
  }).catch(() => {});
}

function runTable(runs, withProject) {
  if (!runs.length) return `<div class="empty">还没有审查记录 —— 到「项目」里接入仓库，默认分支的新 push 会自动审查</div>`;
  return `<table><thead><tr>
      <th style="width:118px">状态</th>
      ${withProject ? "<th>项目</th>" : ""}
      <th>变更（分支 · 提交 · 目的）</th>
      <th>触发</th><th>风险</th><th>发现</th><th>时间</th><th style="width:26px"></th></tr></thead>
    <tbody>${runs.map((r) => {
      const meta = [r.branch || "", r.head_sha ? r.head_sha.slice(0, 8) : ""].filter(Boolean)
        .map(esc).join(" · ");
      const subject = (r.commit_msg || "").split("\n")[0] || "";
      return `
      <tr class="rowlink" data-run="${r.id}">
        <td>${statusHtml(r.status)}</td>
        ${withProject ? `<td><a href="#/projects/${r.project_id}" onclick="event.stopPropagation()">${esc(r.project)}</a></td>` : ""}
        <td class="ctx-cell">
          ${meta ? `<div class="mono small">${meta}</div>` : ""}
          ${subject ? `<div class="small subject">${esc(subject)}</div>` : ""}
        </td>
        <td class="muted small">${TRIGGER_LABEL[r.trigger] || r.trigger}${r.pr_number ? ` <span class="mono">#${r.pr_number}</span>` : ""}</td>
        <td>${r.tier ? `<span class="badge tier">${TIER_LABEL[r.tier] || esc(r.tier)}</span>` : '<span class="faint">—</span>'}</td>
        <td>${r.important_count > 0 ? `<span class="badge imp">${r.important_count} Important</span>` : '<span class="muted small">—</span>'}</td>
        <td class="mono small muted">${fmtTime(r.created_at)}</td>
        <td><span class="go">→</span></td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

/* ---------------- 指标页：周聚合 + 每格可点进明细 ---------------- */

const RESULT_LABEL = { hit: "命中", miss: "漏报", fp: "误报重现", error: "执行错误" };

async function renderMetrics() {
  clearInterval(refreshTimer);
  setTitle("指标");
  showSkeleton();
  let data;
  try { data = await api("/api/metrics"); }
  catch (e) { errorPanel(e.message); return; }
  const weeks = data.weeks || [];
  const maxRuns = Math.max(1, ...weeks.map((w) => w.runs));
  const cur = weeks[0] || null;
  const prev = weeks[1] || null;
  const delta = (a, b) => {
    if (b == null || b === 0) return "";
    const d = Math.round(((a - b) / b) * 100);
    if (!d) return "";
    return `<span class="small ${d > 0 ? "add" : "del"}" title="较上周">${d > 0 ? "↑" : "↓"}${Math.abs(d)}%</span>`;
  };
  const cell = (text, href, extra = "") =>
    `<a href="${href}" class="mono small" style="display:block;color:inherit" onclick="event.stopPropagation()">${text}${extra}</a>`;
  $app.innerHTML = `
    ${pageHead({
      title: "指标",
      sub: "按周聚合（近 8 周）· 每个数字可点进对应明细",
    })}
    <div class="stats stagger">
      <div class="panel stat clickable" data-jump="#/runs">
        <div class="num">${cur ? cur.runs : 0}${cur && prev ? delta(cur.runs, prev.runs) : ""}</div>
        <div class="label">本周审查</div></div>
      <div class="panel stat clickable" data-jump="#/runs?status=blocked">
        <div class="num" style="color:var(--bad)">${cur ? cur.blocked : 0}</div>
        <div class="label">本周发现问题</div></div>
      <div class="panel stat clickable" data-jump="#/runs?reverted=1">
        <div class="num" style="color:var(--warn)">${cur ? cur.reverted : 0}</div>
        <div class="label">本周创建回退 PR</div></div>
      <div class="panel stat clickable" data-jump="#/runs?trigger=pr">
        <div class="num">${cur ? cur.pr_runs : 0}</div><div class="label">本周 PR 触发审查</div></div>
      <div class="panel stat clickable" data-jump="#/marks">
        <div class="num">${cur && cur.marked ? cur.fp_rate + "%" : "—"}</div>
        <div class="label">误报率（处置 ${cur ? cur.marked : 0} 条）</div></div>
    </div>

    <div class="sec-title stagger">周趋势 <span class="faint small mono" style="letter-spacing:0;text-transform:none">点任意数字下钻</span></div>
    <div class="panel stagger">${weeks.length ? `<table><thead><tr>
        <th>周</th><th>审查</th><th>发现问题</th><th>已创建回退 PR</th><th>PR 触发审查</th><th>处置 / 误报</th></tr></thead>
      <tbody>${weeks.map((w) => `
        <tr>
          <td class="mono small muted">${esc(w.start || w.week)}</td>
          <td><div class="mrow">${cell(w.runs, `#/runs?since=${esc(w.start || "")}`)}
            <span class="mbar" style="width:${Math.round(w.runs / maxRuns * 60)}px" title="${w.runs} 次审查"></span></div></td>
          <td>${w.blocked ? `<a href="#/runs?since=${esc(w.start || "")}&status=blocked"
            style="display:block;color:inherit"><span class="badge imp">${w.blocked}</span></a>` : '<span class="faint">—</span>'}</td>
          <td>${cell(w.reverted ? `${w.reverted}` : "—", `#/runs?since=${esc(w.start || "")}&reverted=1`)}</td>
          <td>${cell(w.pr_runs || "—", `#/runs?since=${esc(w.start || "")}&trigger=pr`)}</td>
          <td>${w.marked ? cell(`${w.marked} / <span class="del">${w.fp}</span>（${w.fp_rate}%）`, "#/marks") : '<span class="faint">—</span>'}</td>
        </tr>`).join("")}</tbody></table>`
      : `<div class="empty">还没有可聚合的审查数据</div>`}</div>

    <div class="sec-title stagger">评测集 <span class="faint small mono" style="letter-spacing:0;text-transform:none">评审系统的回归测试 · 配置中心·评审引擎 里跑分</span></div>
    <div class="panel stagger">${evalSummaryHtml(data.eval)}</div>`;
  document.querySelectorAll("[data-jump]").forEach((el) =>
    (el.onclick = () => { location.hash = el.dataset.jump; }));
}

function evalSummaryHtml(ev) {
  if (!ev || !ev.cases) {
    return `<div class="empty">还没有评测用例 —— 在报告页把「确认/误报」的 Important 存为用例<br>
      <span class="small">改提示词 / 换模型前跑一轮，数据说话</span></div>`;
  }
  const last = ev.last;
  let line = "还没有跑过分";
  if (last) {
    if (last.status === "running") line = `<span class="badge mock">跑分进行中</span>`;
    else if (last.status === "failed") line = `<span class="badge imp">失败</span> ${esc(last.error_msg || "")}`;
    else line = `命中 <b class="add">${last.hit}</b> / 漏报 <b class="del">${last.miss}</b> / 误报重现 ${last.fp} / 错误 ${last.error} · 共 ${last.cases_total} 条`;
  }
  return `<div style="padding:14px 18px">
    <div>用例 ${ev.cases} 条（正例 ${ev.positive} · 负例 ${ev.negative}）</div>
    <div class="small muted" style="margin-top:4px">上次：${line}${last ? ` · ${fmtTime(last.created_at)}` : ""}</div>
    <div class="actions" style="margin-top:10px"><a class="ghost small-btn" href="#/eval" style="display:inline-block;padding:2px 10px;font-size:12px;text-decoration:none">去评测页 · 跑分 / 管理用例 →</a></div>
  </div>`;
}

/* ---------------- 审查列表页（总览/指标的下钻目标） ---------------- */

async function renderRunsList(qs = "") {
  clearInterval(refreshTimer);
  setTitle("审查");
  showSkeleton();
  const params = new URLSearchParams(qs);
  const status = params.get("status") || "";
  const trigger = params.get("trigger") || "";
  const reverted = params.get("reverted") === "1";
  const since = params.get("since") || "";
  const fetchRuns = () => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    if (trigger) q.set("trigger", trigger);
    if (reverted) q.set("reverted", "1");
    if (since) q.set("since", since);
    q.set("limit", "100");
    return api(`/api/runs?${q.toString()}`);
  };
  let runs;
  try { runs = await fetchRuns(); }
  catch (e) { errorPanel(e.message); return; }
  const chips = (key, val, opts, label) => `<div class="rf-chip-group">
    <span>${label}</span>
    <div class="rf-chips"><button class="rf-chip ${!val ? "on" : ""}" data-rf="${key}" data-value="">全部</button>
      ${Object.entries(opts).map(([v, l]) => `<button class="rf-chip ${val === v ? "on" : ""}"
        data-rf="${key}" data-value="${v}">${l}</button>`).join("")}</div></div>`;
  const titleBits = [status && STATUS_LABEL[status], trigger && TRIGGER_LABEL[trigger],
    reverted && "已创建回退 PR", since && `自 ${since}`].filter(Boolean).join(" · ");
  $app.innerHTML = `
    ${pageHead({
      title: `审查${titleBits ? ` <span class="muted" style="font-size:14px;font-weight:400">· ${esc(titleBits)}</span>` : ""}`,
      sub: `${runs.length} 条记录 · 点行看报告`,
    })}
    <div class="rf-toolbar">
      ${chips("status", status, STATUS_LABEL, "结果")}
      ${chips("trigger", trigger, TRIGGER_LABEL, "触发")}
      <div class="rf-extra"><label class="small"><input type="checkbox" id="rf-reverted" ${reverted ? "checked" : ""}> 仅看已创建回退 PR</label>
        <label class="small">起始日期 <input type="date" id="rf-since" value="${esc(since)}"></label>
        <button class="ghost small-btn" id="rf-clear">清除筛选</button></div>
    </div>
    <div class="panel run-table-desktop">${runTable(runs, true)}</div>
    <div class="run-cards-mobile">${runs.map((r) => `<a class="panel run-card-mobile" href="#/runs/${r.id}">
      <span class="run-card-line">${statusHtml(r.status)}<span class="muted small">${fmtTime(r.created_at)}</span></span>
      <strong>${esc(r.project)} · ${r.pr_number ? "PR #" + r.pr_number : esc((r.head_sha || r.ref || "").slice(0, 8))}</strong>
      <span class="run-card-sub">${esc((r.commit_msg || "").split("\n")[0] || r.branch || r.ref || "审查记录")}</span>
      <span class="run-card-line"><span class="small muted">${TRIGGER_LABEL[r.trigger] || esc(r.trigger)}${r.tier ? " · 风险 " + (TIER_LABEL[r.tier] || esc(r.tier)) : ""}</span>
        ${r.important_count ? `<span class="badge imp">${r.important_count} Important</span>` : ""}</span>
    </a>`).join("")}</div>`;
  const reroute = (override = {}) => {
    const q = new URLSearchParams();
    const st = override.status ?? status, tr = override.trigger ?? trigger;
    if (st) q.set("status", st);
    if (tr) q.set("trigger", tr);
    if ($("rf-reverted").checked) q.set("reverted", "1");
    if ($("rf-since").value) q.set("since", $("rf-since").value);
    location.hash = `#/runs${q.toString() ? "?" + q.toString() : ""}`;
  };
  document.querySelectorAll("[data-rf]").forEach((b) => b.onclick = () => reroute({ [b.dataset.rf]: b.dataset.value }));
  $("rf-reverted").onchange = () => reroute();
  $("rf-since").onchange = () => reroute();
  $("rf-clear").onclick = () => { location.hash = "#/runs"; };
  bindRunRows();
}

/* ---------------- 处置台账（独立页）：待处置队列 + 台账 + 过滤 ---------------- */

let marksFilter = { disposition: "", project_id: 0 };
const marksSel = new Set();   // "runId:idx" -> 选中，供批量处置
let workbenchSearch = "";
let marksData = null;
let workbenchPrOnly = false;

function marksStatsHtml(s) {
  const fpPct = s.total ? Math.round((s.false_positive / s.total) * 100) + "%" : "—";
  return `
      <div class="panel stat"><div class="num" style="color:var(--accent)">${s.pending}</div><div class="label">待处置 Important</div></div>
      <div class="panel stat"><div class="num">${s.total}</div><div class="label">已处置</div></div>
      <div class="panel stat"><div class="num" style="color:var(--warn)">${s.false_positive}</div><div class="label">误报（${fpPct}）</div></div>
      <div class="panel stat"><div class="num" style="color:var(--ok)">${s.confirmed}</div><div class="label">确认</div></div>`;
}

function pendingMarksTable(items) {
  if (!items.length) return `<div class="empty">队列空 —— blocked run 的 Important 都已处置 ✓</div>`;
  return `<table><thead><tr><th style="width:26px"></th><th style="width:132px">快速处置</th><th>发现</th><th>项目</th><th>时间</th></tr></thead>
    <tbody>${items.map((f) => {
      const key = `${f.run_id}:${f.idx}`;
      return `
      <tr class="rowlink" data-run="${f.run_id}">
        <td><input type="checkbox" class="mk-sel" data-key="${key}" ${marksSel.has(key) ? "checked" : ""}></td>
        <td>
          <button class="ghost small-btn mark-btn" data-mark="confirmed"
            data-run="${f.run_id}" data-idx="${f.idx}" data-cur="">确认</button>
          <button class="ghost small-btn mark-btn" data-mark="false_positive"
            data-run="${f.run_id}" data-idx="${f.idx}" data-cur="">误报</button>
        </td>
        <td class="small">${f.file ? `<span class="mono muted">${esc(f.file)}:${esc(f.line)}</span> — ` : ""}${esc(f.title)}</td>
        <td class="small"><a href="#/projects/${f.project_id}">${esc(f.project)}</a></td>
        <td class="mono small muted">${fmtAgo(f.created_at)}${ageDays(f.created_at) > 2
          ? ' <span class="badge imp">滞留</span>' : ""}</td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

function ledgerMarksTable(marks) {
  if (!marks.length) {
    return `<div class="empty">${marksFilter.disposition || marksFilter.project_id ? "没有符合条件的处置记录" : "还没有处置记录 —— 先在上方队列或报告页标记"}</div>`;
  }
  return `<table><thead><tr><th style="width:96px">处置</th><th>发现</th><th>项目</th><th>时间</th><th style="width:96px"></th></tr></thead>
    <tbody>${marks.map((m) => `
      <tr class="rowlink" data-run="${m.run_id}">
        <td>${m.disposition === "false_positive"
          ? `<span class="badge fp">误报</span>` : `<span class="badge confirmed">确认</span>`}</td>
        <td class="small">${m.file ? `<span class="mono muted">${esc(m.file)}:${esc(m.line)}</span> — ` : ""}${esc(m.title)}</td>
        <td class="small"><a href="#/projects/${m.project_id}">${esc(m.project)}</a></td>
        <td class="mono small muted">${fmtTime(m.updated_at)}</td>
        <td>${m.disposition === "false_positive"
          ? `<button class="ghost small-btn" data-fb-run="${m.run_id}" data-fb-idx="${m.idx}"
               data-fb-title="${esc(m.title)}" data-fb-file="${esc(m.file || "")}"
               title="写入不再报告规则，并保存为负例样本">修正规则</button>` : ""}</td>
      </tr>`).join("")}</tbody></table>`;
}

/* 筛选/标记后只重写有变化的区域：内容 diff 过再写，容器不带入场动画，
   页面壳与滚动不动——不做任何"看起来像整页刷新"的事 */
function paintMarks(data) {
  marksData = data;
  const $s = $("mk-stats"), $p = $("mk-pending"), $l = $("mk-ledger");
  if (!$s) return;
  const match = (f) => !workbenchSearch || `${f.title} ${f.file || ""} ${f.project} ${f.run_id}`.toLowerCase().includes(workbenchSearch);
  const next = { s: marksStatsHtml(data.stats),
    p: pendingMarksTable(data.pending.filter(match)),
    l: ledgerMarksTable(data.marks.filter(match)) };
  const changed = $s.innerHTML !== next.s || $p.innerHTML !== next.p || $l.innerHTML !== next.l;
  if ($s.innerHTML !== next.s) $s.innerHTML = next.s;
  if ($p.innerHTML !== next.p) $p.innerHTML = next.p;
  if ($l.innerHTML !== next.l) $l.innerHTML = next.l;
  document.querySelectorAll("[data-mf]").forEach((b) =>
    b.classList.toggle("on", b.dataset.mf === marksFilter.disposition));
  if (!changed) { syncBatchBar(); return; }
  bindRunRows();
  document.querySelectorAll(".mk-sel").forEach((c) =>
    (c.onchange = () => { c.checked ? marksSel.add(c.dataset.key) : marksSel.delete(c.dataset.key); syncBatchBar(); }));
  syncBatchBar();
}

function syncBatchBar() {
  const bar = $("mk-batch");
  if (!bar) return;
  bar.style.display = marksSel.size ? "" : "none";
  $("mk-batch-count").textContent = marksSel.size;
}

async function batchMark(val) {
  if (!marksSel.size) return;
  const entries = [...marksSel].map((k) => k.split(":").map(Number));
  try {
    for (const [runId, idx] of entries) {
      await api(`/api/runs/${runId}/findings/${idx}/mark`, { method: "POST", body: { disposition: val } });
    }
    marksSel.clear();
    toast(`已批量${val === "confirmed" ? "确认" : "标误报"} ${entries.length} 条`);
    if (location.hash === "#/workbench") refreshWorkbenchData(); else refreshMarks();
  } catch (e) { toast(e.message, true); }
}

async function refreshMarks() {
  const q = new URLSearchParams();
  if (marksFilter.disposition) q.set("disposition", marksFilter.disposition);
  if (marksFilter.project_id) q.set("project_id", String(marksFilter.project_id));
  let data;
  try { data = await api(`/api/marks?${q.toString()}`); }
  catch (e) { toast(e.message, true); return; }
  paintMarks(data);
}

/* 工作台只负责回答「现在做什么、质量有没有改善」；报告页负责逐条审查。 */
let qualityData = null;

function qualityLatest(d) { return d.eval.history?.[0] || null; }
function qualityNeedsRerun(d) {
  const h = qualityLatest(d);
  return !!h && h.status === "done" &&
    (d.eval.cases.length !== h.cases_total ||
      JSON.stringify(h.config || {}) !== JSON.stringify(d.eval.current_config || {}));
}
function qualityResultLabel(h) {
  if (!h) return "尚未运行";
  if (h.status === "running") return "评测进行中";
  if (h.status === "failed") return "评测执行失败";
  const issues = (h.miss || 0) + (h.fp || 0) + (h.error || 0);
  return issues ? `${issues} 项需要检查` : `${h.hit || 0} / ${h.cases_total || 0} 个样本符合预期`;
}
function qualityShortResult(h) {
  if (!h) return "未运行";
  if (h.status === "running") return "进行中";
  if (h.status === "failed") return "失败";
  const issues = (h.miss || 0) + (h.fp || 0) + (h.error || 0);
  return issues ? `${issues} 项异常` : `${h.hit || 0}/${h.cases_total || 0} 符合预期`;
}
function qualityNext(d) {
  const pending = d.marks.stats.pending, cases = d.eval.cases.length, h = qualityLatest(d);
  if (pending) return {
    tone: "attention", label: "先处理审查结果",
    title: `${pending} 条 Important 等待人工判断`,
    text: "到审查报告核对完整理由，再标记为真实问题或误报。",
    href: d.marks.pending[0] ? `#/runs/${d.marks.pending[0].run_id}` : "#/runs",
    action: "打开待处理报告",
  };
  const failedReview = d.failures[0];
  if (failedReview) return { tone: "attention", label: "排查审查执行失败",
    title: "最近有审查没有完成", text: "打开报告查看失败步骤；确认原因后再决定是否重新审查。",
    href: `#/runs/${failedReview.id}`, action: "查看失败报告" };
  if (h?.status === "failed" || (h?.status === "done" && ((h.miss || 0) + (h.fp || 0) + (h.error || 0)))) {
    return { tone: "attention", label: "检查回归结果", title: "已有样本未达到预期",
      text: "查看漏报、误报重现或执行错误，修正规则或模型配置后再跑一轮。",
      action: "查看逐项结果", toggle: true };
  }
  if (h?.status === "running") return { tone: "ready", label: "等待评测完成",
    title: "回归评测正在运行", text: "逐条样本会重新评审；完成后检查漏报、误报重现和执行错误。",
    action: "查看评测进度", toggle: true };
  if (qualityNeedsRerun(d)) return { tone: "ready", label: "重新验证当前配置",
    title: "配置或样本已变化", text: "上次结果不能代表当前节点、规则和样本，运行新一轮回归。",
    action: "重新运行评测", runEval: true };
  const positiveCases = d.eval.cases.filter((c) => c.polarity === "positive").length;
  const confirmed = d.marks.marks.find((m) => m.disposition === "confirmed");
  if (d.marks.stats.confirmed && !positiveCases && confirmed) return {
    tone: "ready", label: "补足漏报检验",
    title: "还没有“应报出”的正例样本",
    text: "从已确认的真实问题里选一条保存，之后才能检验评审是否漏报。",
    href: `#/runs/${confirmed.run_id}`, action: "打开已确认报告" };
  if (cases && !h) return { tone: "ready", label: "建立质量基线",
    title: `${cases} 条样本已准备好`,
    text: "运行首轮评测，后续改规则或模型时用相同样本检查退化。",
    action: "运行首次评测", runEval: true };
  if (!cases && d.marks.stats.total) return {
    tone: "ready", label: "把判断变成可重复的检验",
    title: "选择已判定的 Important 保存为样本",
    text: "真实问题存为应报出的正例；误报存为不应再出现的负例。",
    href: d.marks.marks[0] ? `#/runs/${d.marks.marks[0].run_id}` : "#/runs",
    action: "打开已判定报告",
  };
  return { tone: "clear", label: "当前无需人工处理",
    title: cases ? "继续关注下一轮审查" : d.runs.length ? "当前没有待判断的 Important" : "等待首轮审查结果",
    text: cases ? `现有 ${cases} 条样本可在规则或模型变更后重跑检验。`
      : d.projects.length ? "可以在项目页启动审查；有需要判断的结果时，这里会给出下一步。"
        : "接入仓库并触发审查后，这里会给出下一步。",
    href: cases ? "#/runs" : "#/projects",
    action: cases ? "查看审查记录" : d.projects.length ? "查看项目" : "接入仓库" };
}
function qualityAttentionHtml(d) {
  const grouped = new Map();
  for (const item of d.marks.pending) {
    if (!grouped.has(item.run_id)) grouped.set(item.run_id, { ...item, count: 0 });
    grouped.get(item.run_id).count++;
  }
  const pending = [...grouped.values()].slice(0, 4);
  const failed = d.failures.slice(0, 3);
  if (!pending.length && !failed.length) return '<div class="qa-empty">暂无需要处理的报告。新的 Important 或执行失败会出现在这里。</div>';
  return `${pending.map((r) => `<div class="qa-task-row">
      <span class="qa-task-dot alert"></span><div class="qa-task-main"><strong>${esc(r.project)} · run #${r.run_id}</strong>
        <span>${r.count} 条 Important 待判定 · ${fmtAgo(r.created_at)}</span></div>
      <a href="#/runs/${r.run_id}">打开报告 →</a></div>`).join("")}
    ${failed.map((r) => `<div class="qa-task-row"><span class="qa-task-dot"></span>
      <div class="qa-task-main"><strong>${esc(r.project)} · run #${r.id}</strong><span>审查执行失败 · ${fmtAgo(r.created_at)}</span></div>
      <a href="#/runs/${r.id}">查看原因 →</a><button class="ghost small-btn" data-failure-handle="${r.id}">标记已处理</button></div>`).join("")}`;
}
function qualityEvalHtml(d) {
  const h = qualityLatest(d), cases = d.eval.cases, positives = cases.filter((c) => c.polarity === "positive").length;
  const changed = qualityNeedsRerun(d);
  const done = h?.results?.length || 0;
  return `<div class="qa-eval-top"><span class="qa-status ${h?.status === "failed" || (h?.miss || 0) + (h?.fp || 0) + (h?.error || 0) ? "warn" : ""}">${qualityResultLabel(h)}</span>
    <span class="muted small">${h ? "上次 " + fmtTime(h.created_at) : "还没有基线"}</span></div>
    <div class="qa-eval-flow"><div><b>① 固定样本</b><span>取人工判定时的提交</span></div>
      <div><b>② 用当前配置复审</b><span>门禁、上下文、独立评审</span></div>
      <div><b>③ 对照人工答案</b><span>查漏报与误报重现</span></div></div>
    <p>${cases.length} 条回归样本：${positives} 条应报出，${cases.length - positives} 条不应再报出。</p>
    ${h?.status === "running" ? `<div class="qa-eval-live"><div class="qa-eval-live-head"><strong>已完成 ${done} / ${h.cases_total}</strong><span>${esc(h.current_detail || "准备下一条样本")}</span></div>
      <div class="qa-progress"><i style="width:${Math.round(done / Math.max(h.cases_total, 1) * 100)}%"></i></div>
      <small>当前：${esc(h.current_stage || "等待开始")}</small></div>` : ""}
    ${h?.status === "failed" && h.error_msg ? `<div class="qa-eval-note">评测中断：${esc(h.error_msg)}。已完成样本仍可在逐项结果中查看。</div>` : ""}
    ${cases.length && !positives ? '<div class="qa-eval-note">目前只有负例，还不能检验真实问题是否漏报。</div>' : ""}
    ${h ? `<div class="qa-eval-counts"><span>漏报 <b>${h.miss || 0}</b></span><span>误报重现 <b>${h.fp || 0}</b></span><span>执行错误 <b>${h.error || 0}</b></span></div>` : ""}
    ${changed ? '<div class="qa-eval-note">流水线、提示词、规则或样本数量已变化，建议重新运行评测。</div>' : ""}
    <div class="qa-eval-actions">
      <button class="primary small-btn" id="eval-run" ${!cases.length || h?.status === "running" ? "disabled" : ""}>${h?.status === "running" ? "评测进行中…" : "运行回归评测"}</button>
      <button class="ghost small-btn" data-qa-toggle="qa-details" aria-expanded="false">查看样本与逐项结果</button>
    </div>`;
}
function qualityDetailsHtml(d) {
  const h = qualityLatest(d), cases = d.eval.cases;
  return `<div class="qa-details-head"><div><h3>回归样本与结果</h3>
      <p>在原提交上用当前编排重新审查；同文件、相差不超过 15 行且标题相似，算重现。正例要报出，负例不能报出。删除样本只影响之后的评测。</p></div>
      <button class="ghost small-btn" data-qa-toggle="qa-details" aria-expanded="true">收起</button></div>
    ${h?.status === "running" ? `<div class="qa-current-trace"><strong>当前样本：${esc(h.current_detail || "准备中")}</strong><span>${esc(h.current_stage || "等待开始")}</span>
      <div>${(h.current_trace || []).map((s) => `<span class="qa-trace-step ${s.status}">${s.status === "ok" ? "✓" : s.status === "fail" ? "✕" : "◐"} ${esc(s.name)}</span>`).join("")}</div></div>` : ""}
    <div class="qa-detail-grid"><div><h4>保存的样本</h4>
      ${cases.length ? cases.map((c) => `<div class="qa-case-row">
        <span class="badge ${c.polarity === "positive" ? "confirmed" : "fp"}">${c.polarity === "positive" ? "应报出" : "不应再报"}</span>
        ${c.run_id ? `<a href="#/runs/${c.run_id}" title="${esc(c.expected)}">${esc(c.expected)}</a>`
          : `<span title="${esc(c.expected)}">${esc(c.expected)}</span>`}
        <button class="danger-ghost small-btn" data-eval-del="${c.id}">删除</button></div>`).join("") : '<div class="qa-empty">还没有样本。先在报告页判定 Important，再保存为样本。</div>'}</div>
      <div><h4>最近一轮逐项结果</h4>
      ${h?.results?.length ? h.results.map((r) => `<details class="qa-result-row"><summary><span class="badge ${r.result === "hit" ? "confirmed" : "imp"}">${r.result === "hit" ? "符合预期" : RESULT_LABEL[r.result] || esc(r.result)}</span>
        <strong title="${esc(r.expected)}">${esc(r.expected)}</strong><span class="muted small">${r.polarity === "positive" ? "应报出" : "不应再报"} · ${esc(r.sha || "")}</span></summary>
        <div class="qa-result-detail"><p>${esc(r.detail || "")}</p>
          <div class="qa-trace">${(r.steps || []).map((s) => `<span class="qa-trace-step ${s.status}">${s.status === "ok" ? "✓" : "✕"} ${esc(s.name)}</span>`).join("") || '<span class="muted small">旧评测未记录执行步骤</span>'}</div>
          <small>本轮实际发现：${r.actual?.length ? r.actual.map((a) => `${esc(a.file || "?")}:${esc(a.line ?? "?")} ${esc(a.title || "")}`).join("；") : "无"}</small></div></details>`).join("")
        : '<div class="qa-empty">运行评测后可在这里看每条样本的结果。</div>'}</div></div>`;
}
function qualityTrendHtml(d) {
  const weeks = [...(d.metrics.weeks || [])].reverse().slice(-8);
  const max = Math.max(1, ...weeks.map((w) => w.runs || 0));
  const marked = weeks.reduce((n, w) => n + (w.marked || 0), 0);
  const fp = weeks.reduce((n, w) => n + (w.fp || 0), 0);
  return `<div class="qa-trend-head"><div><h3>近 8 周的审查与人工判断</h3>
      <p>只用来发现审查量和误报变化；样本太少时不把比例当作质量结论。</p></div>
      <span>误报 ${fp} / 已判定 ${marked}</span></div>
    <div class="qa-week-grid">${weeks.length ? weeks.map((w) => `<div class="qa-week">
      <div class="qa-week-bar"><i style="height:${Math.max(4, Math.round((w.runs || 0) / max * 100))}%"></i></div>
      <b>${w.runs}</b><span>${esc(w.start || w.week)}</span></div>`).join("")
      : '<div class="qa-empty">还没有足够的审查数据。</div>'}</div>`;
}
function qualityLoopHtml(d) {
  const cases = d.eval.cases || [], h = qualityLatest(d);
  const positive = cases.filter((c) => c.polarity === "positive").length;
  const negative = cases.length - positive;
  return `<section class="qa-loop"><div class="qa-section-heading"><div><span class="wb-eyebrow">人工判断 → 可复用样本 → 回归信号</span>
    <h2>判断怎样影响下一轮审查</h2></div><p>样本用于检验规则与模型改动；误报还可手动加入“不再报告”规则。不会自动训练模型。</p></div>
    <div class="qa-loop-lanes">
      <div class="qa-loop-lane positive"><div class="qa-loop-source"><span>报告中确认真实问题</span><strong>${d.marks.stats.confirmed}</strong><small>人工确认</small></div>
        <span class="qa-loop-arrow">→</span><div class="qa-loop-sample"><span>应报出样本</span><strong>${positive}</strong><small>保存后成为回归基线</small></div>
        <span class="qa-loop-arrow">→</span><div class="qa-loop-outcome"><span>改动后重新评审</span><strong>${h?.status === "done" && positive ? h.miss || 0 : "—"}</strong><small>漏报 · 应报却没报</small></div></div>
      <div class="qa-loop-lane negative"><div class="qa-loop-source"><span>报告中标记误报</span><strong>${d.marks.stats.false_positive}</strong><small>人工排除</small></div>
        <span class="qa-loop-arrow">→</span><div class="qa-loop-sample"><span>不应再报样本</span><strong>${negative}</strong><small>可同时修正“不再报告”规则</small></div>
        <span class="qa-loop-arrow">→</span><div class="qa-loop-outcome"><span>改动后重新评审</span><strong>${h?.status === "done" && negative ? h.fp || 0 : "—"}</strong><small>误报重现 · 不应报却报了</small></div></div>
    </div><div class="qa-loop-foot"><span>节点、提示词或规则改动</span><span>↶ 用同一批样本再次验证</span>
      <a href="#/runs">到报告中判定并保存样本 →</a></div></section>`;
}
function qualityWorkbenchHtml(d) { return improvementWorkbenchHtml(d); }
async function refreshWorkbenchData() {
  if (location.hash === "#/workbench") renderWorkbench();
}
async function renderWorkbench() {
  clearInterval(refreshTimer);
  setTitle("质量工作台");
  showSkeleton();
  try {
    const [projects, runs, failures, marks, evaluation, metrics, improvements] = await Promise.all([
      api("/api/projects"), api("/api/runs?limit=40"), api("/api/runs?status=failed&unhandled=true&limit=40"), api("/api/marks"),
      api("/api/eval/last"), api("/api/metrics"), api("/api/improvements"),
    ]);
    if (location.hash !== "#/workbench") return;
    qualityData = { projects, runs, failures, marks, eval: evaluation, metrics, improvements };
    $app.innerHTML = qualityWorkbenchHtml(qualityData);
    bindImprovements();
    bindQualityActions();
    if (evaluation.history?.[0]?.status === "running") pollEval();
  } catch (e) { errorPanel(e.message); }
}
function bindQualityActions() {
  document.querySelectorAll("[data-qa-toggle]").forEach((b) => b.onclick = () => {
    const box = $("qa-details"), opening = box.hidden;
    box.hidden = !opening;
    document.querySelectorAll("[data-qa-toggle]").forEach((el) => el.setAttribute("aria-expanded", String(opening)));
    if (opening) box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  const run = async () => {
    try {
      const result = await api("/api/eval/run", { method: "POST" });
      if (!result.ok) { toast(result.msg, true); return; }
      toast("评测已开始；完成后会显示漏报、误报重现和执行错误");
      renderWorkbench();
    } catch (e) { toast(e.message, true); }
  };
  document.querySelectorAll("[data-qa-run], #eval-run").forEach((b) => b.onclick = run);
  document.querySelectorAll("[data-eval-del]").forEach((b) => b.onclick = async () => {
    if (!await uiConfirm("删除这条回归样本？之后的评测将不再检查它。")) return;
    try {
      await api(`/api/eval/cases/${b.dataset.evalDel}`, { method: "DELETE" });
      toast("样本已删除"); renderWorkbench();
    } catch (e) { toast(e.message, true); }
  });
  document.querySelectorAll("[data-failure-handle]").forEach((b) => b.onclick = () =>
    triageFailure(+b.dataset.failureHandle, "handled", renderWorkbench));
}

async function renderMarks() {
  clearInterval(refreshTimer);
  setTitle("处置");
  showSkeleton();
  let projects;
  try { projects = await api("/api/projects"); }
  catch (e) { errorPanel(e.message); return; }
  const seg = (val, label) =>
    `<button class="ghost small-btn mark-btn${marksFilter.disposition === val ? " on" : ""}" data-mf="${val}">${label}</button>`;
  $app.innerHTML = `
    ${pageHead({
      title: "处置",
      sub: "判定反馈 → 项目候选改进 → 对照验证 → 采用生效",
    })}
    <div class="stats" id="mk-stats"></div>

    <div class="sec-title stagger">待处置 <span class="faint small mono" style="letter-spacing:0;text-transform:none">点行进报告 · 按钮直接标记 · 可勾选批量</span></div>
    <div class="actions" id="mk-batch" style="display:none;margin-bottom:10px;border:1px solid var(--line);border-radius:6px;padding:8px 12px">
      <span class="small">已选 <b id="mk-batch-count">0</b> 条</span>
      <button class="small-btn" data-batch="confirmed">批量确认</button>
      <button class="small-btn" data-batch="false_positive">批量标误报</button>
    </div>
    <div class="panel" id="mk-pending"></div>

    <div class="sec-title stagger">台账 <span class="faint small mono" style="letter-spacing:0;text-transform:none">全部处置记录 · 点行进报告</span></div>
    <div class="actions" style="margin-bottom:10px">
      ${seg("", "全部")}${seg("false_positive", "误报")}${seg("confirmed", "确认")}
      <select id="mf-project" style="width:auto">
        <option value="">全部项目</option>
        ${projects.map((p) => `<option value="${p.id}" ${marksFilter.project_id === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
      </select>
    </div>
    <div class="panel" id="mk-ledger"></div>`;
  document.querySelectorAll("[data-mf]").forEach((b) =>
    (b.onclick = () => { marksFilter.disposition = b.dataset.mf; refreshMarks(); }));
  document.querySelectorAll("[data-batch]").forEach((b) =>
    (b.onclick = () => batchMark(b.dataset.batch)));
  $("mf-project").onchange = (e) => {
    marksFilter.project_id = +e.target.value || 0;
    refreshMarks();
  };
  refreshMarks();
}

function bindRunRows() {
  document.querySelectorAll("tr.rowlink[data-run]").forEach((tr) =>
    tr.addEventListener("click", (ev) => {
      if (ev.target.closest("button, a, input")) return;   // 行内按钮/链接/勾选各自处理
      location.hash = `#/runs/${tr.dataset.run}`;
    }));
}

/* ---------------- 项目列表 ---------------- */

let githubConnections = [];
function connectionOptions(selected = null) {
  return `<option value="" ${selected == null ? "selected" : ""}>不使用 token（公开仓库只读）</option>` +
    githubConnections.map((c) => `<option value="${c.id}" ${selected === c.id ? "selected" : ""} ${c.disabled ? "disabled" : ""}>${esc(c.label)}${c.disabled ? "（停用）" : ""}</option>`).join("");
}

async function renderProjects() {
  clearInterval(refreshTimer);
  setTitle("项目");
  showSkeleton();
  const [projects, connections] = await Promise.all([api("/api/projects"), api("/api/github-connections")]);
  githubConnections = connections;
  $app.innerHTML = `
    ${pageHead({
      title: "项目",
      sub: "每个项目独立审查配置；GitHub 组织可一键全部接入。点卡片进入项目",
      actions: `<button id="import-btn">从组织导入</button>
        <button class="primary" id="add-btn">接入项目</button>`,
    })}
    <div class="proj-layout">
      <div id="proj-list" class="stagger">
        ${projects.length ? projects.map(projectCard).join("")
          : `<div class="panel empty">还没有接入项目 —— 点「接入项目」添加 GitHub 仓库或本地路径</div>`}
      </div>
      <aside class="panel proj-overview">
        <div class="wb-eyebrow">REPOSITORY ACCESS</div>
        <h2>接入概况</h2>
        <div class="proj-overview-stats"><div><b>${projects.length}</b><span>仓库</span></div>
          <div><b>${projects.filter((p) => p.enabled).length}</b><span>已启用</span></div>
          <div><b>${connections.filter((c) => !c.disabled).length}</b><span>可用连接</span></div></div>
        <div class="proj-overview-head"><b>GitHub 连接与项目</b><a href="#/settings/creds">管理连接 →</a></div>
        ${connections.length ? connections.map((c) => `<div class="proj-connection">
          <span class="dot ${c.disabled || c.last_error ? "failed" : "passed"}"></span>
          <span><strong>${esc(c.label)}</strong><small>${esc(c.account_login || "账户未验证")} · ${c.project_count || 0} 个项目${c.disabled ? " · 已停用" : ""}</small></span>
        </div>`).join("") : '<div class="wb-queue-empty">尚未配置连接，公开仓库可匿名只读审查。</div>'}
        <div class="proj-overview-note">每个仓库可单独绑定一个连接；私有仓库和 webhook 需要对应连接具备权限。</div>
      </aside>
    </div>
    <div id="add-area" style="margin-top:22px"></div>`;

  $("add-btn").onclick = () => showAddForm();
  const importBtn = $("import-btn");
  if (importBtn) importBtn.onclick = () => showImportForm();
  document.querySelectorAll("[data-connection]").forEach((el) =>
    el.addEventListener("change", async () => {
      try {
        await api(`/api/projects/${el.dataset.connection}`, {method: "PATCH",
          body: {github_connection_id: el.value ? +el.value : null}});
        toast("项目连接已更新");
        renderProjects();
      } catch (e) { toast(e.message, true); renderProjects(); }
    }));
  bindProfileEditors(() => renderProjects());
  document.querySelectorAll("[data-goto]").forEach((el) =>
    el.addEventListener("click", () => { location.hash = `#/projects/${el.dataset.goto}`; }));

  document.querySelectorAll("[data-toggle]").forEach((el) =>
    el.addEventListener("change", async () => {
      await api(`/api/projects/${el.dataset.toggle}`, { method: "PATCH", body: { enabled: el.checked } });
      toast(el.checked ? "已启用：之后有新 push 就会自动审查" : "已停用");
      renderProjects();
    }));
  document.querySelectorAll("[data-del]").forEach((el) =>
    el.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!await uiConfirm(`移除项目 ${el.dataset.name}？（审查记录一并删除）`)) return;
      await api(`/api/projects/${el.dataset.del}`, { method: "DELETE" });
      renderProjects();
    }));
  document.querySelectorAll("[data-run-now]").forEach((el) =>
    el.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const r = await api("/api/runs", { method: "POST", body: { project_id: +el.dataset.runNow } });
      toast(r.queued ? `已入队（run #${r.run_id}）` : r.msg);
      if (r.run_id) location.hash = `#/runs/${r.run_id}`;
    }));
  document.querySelectorAll("[data-hook]").forEach((el) =>
    el.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      try {
        const r = await api(`/api/projects/${el.dataset.hook}/attach_webhook`, { method: "POST" });
        toast(`webhook 已挂到 ${r.url}`);
        renderProjects();
      } catch (e) { toast(e.message, true); }
    }));
}

function projectCard(p) {
  const last = p.last_run;
  const lastHtml = last
    ? `<span class="status"><span class="dot ${esc(last.status)}"></span>${STATUS_LABEL[last.status] || last.status}
       ${last.important_count ? `<span class="badge imp">${last.important_count}</span>` : ""}</span>`
    : `<span class="muted small">尚无审查（等新 push）</span>`;
  const initChip = p.kind === "github" ? ({
    pending: `<span class="badge mock">画像生成中</span>`,
    running: `<span class="badge mock">画像生成中</span>`,
    failed: `<span class="badge imp">画像失败</span>`,
  }[p.init_status] || "") : "";
  return `<div class="panel proj-card-wrap" data-goto="${p.id}" style="cursor:pointer">
    <div class="proj-card">
      <div class="grow">
        <div class="name">${esc(p.name)}
          ${p.kind === "local" ? `<span class="badge">本地</span>` : ""}
          ${p.auto_revert ? `<span class="badge" title="当前默认分支 push 工作流尚未接入自动回滚">自动回滚未接入</span>` : ""}
          ${p.webhook_attached ? `<span class="badge">webhook 已挂</span>` : ""}
          ${initChip}
        </div>
        <div class="meta mono">${esc(p.full_name)} · ${esc(p.default_branch)}</div>
        ${p.kind === "github" ? `<div class="field" style="margin-top:8px;max-width:310px" onclick="event.stopPropagation()">
          <label>GitHub 连接</label><select data-connection="${p.id}">${connectionOptions(p.github_connection_id)}</select></div>` : ""}
      </div>
      ${lastHtml}
      <div class="actions" onclick="event.stopPropagation()">
        <button data-run-now="${p.id}">立即审查</button>
        ${p.kind === "github" && !p.webhook_attached ? `<button class="ghost" data-hook="${p.id}">挂 webhook</button>` : ""}
        <button class="danger-ghost" data-del="${p.id}" data-name="${esc(p.name)}">移除</button>
        <label class="switch" title="启用/停用">
          <input type="checkbox" data-toggle="${p.id}" ${p.enabled ? "checked" : ""}>
          <span class="track"><span class="knob"></span></span>
        </label>
      </div>
    </div>
    <div class="profile-block">
      <div class="profile-head">
        <span class="ctx-label" style="margin:0">项目画像（评审时作为背景上下文，可编辑）</span>
        <span class="actions">
          <button class="ghost small-btn" data-edit-profile="${p.id}">编辑</button>
          ${p.kind === "github" && p.init_status === "failed" ? `<button class="ghost small-btn" data-reinit="${p.id}">重新生成</button>` : ""}
        </span>
      </div>
      <div class="profile-text" id="profile-view-${p.id}">${esc(p.profile || "（还没有画像：自动生成中，或点「编辑」手写）")}</div>
    </div>
  </div>`;
}

function showAddForm() {
  const area = $("add-area");
  area.innerHTML = `<div class="panel" style="padding:22px;animation:rise .3s ease both">
    <div class="sec-title" style="margin-top:0">接入项目</div>
    <div class="form-row">
      <div class="field"><label>类型</label>
        <select id="f-kind"><option value="github">GitHub 仓库</option><option value="local">本地路径</option></select></div>
    </div>
    <div class="field"><label id="f-target-label">仓库（owner/repo 或完整 URL）</label>
      <input id="f-target" placeholder="your-org/your-repo"></div>
    <div class="form-row">
      <div class="field"><label>默认分支</label><input id="f-branch" value="main"></div>
      <div class="field"><label>自动回滚（当前工作流未接入）</label>
        <select id="f-autorevert"><option value="no">关闭（可在报告中手动回滚）</option><option value="yes" disabled>后续接入</option></select></div>
    </div>
    <div class="field" id="f-connection-wrap"><label>GitHub 连接</label>
      <select id="f-connection">${connectionOptions()}</select>
      <div class="hint">私有仓库需绑定连接；公开仓库可匿名只读审查。</div></div>
    <div class="actions"><button class="primary" id="f-save">接入</button>
      <button class="ghost" id="f-cancel">取消</button></div>
  </div>`;
  $("f-cancel").onclick = () => (area.innerHTML = "");
  $("f-kind").onchange = (e) => {
    $("f-target-label").textContent =
      e.target.value === "local" ? "本地仓库绝对路径" : "仓库（owner/repo 或完整 URL）";
    $("f-connection-wrap").style.display = e.target.value === "local" ? "none" : "";
  };
  $("f-save").onclick = async () => {
    try {
      await api("/api/projects", { method: "POST", body: {
        kind: $("f-kind").value,
        full_name: $("f-target").value.trim(),
        default_branch: $("f-branch").value.trim() || "main",
        provider: "auto",
        auto_revert: $("f-autorevert").value === "yes",
        github_connection_id: $("f-kind").value === "github" && $("f-connection").value ? +$("f-connection").value : null,
      }});
      toast("已接入：正在生成项目画像，之后有新 push 才会审查");
      renderProjects();
    } catch (e) { toast(e.message, true); }
  };
}

async function showImportForm() {
  const org = await uiPrompt("导入 GitHub 组织", { message: "输入组织名，系统会接入该组织下的仓库。" });
  if (!org) return;
  const choice = await uiChoice("选择 GitHub 连接",
    [{ value: "", label: "匿名接入公开仓库" },
      ...githubConnections.filter((c) => !c.disabled).map((c) => ({ value: String(c.id), label: c.label }))],
    "私有仓库需要具备访问权限的连接。");
  if (choice === null) return;
  const cid = choice ? Number(choice) : null;
  try {
    const r = await api("/api/projects/import_org", { method: "POST", body: { org, github_connection_id: cid } });
    toast(`已导入 ${r.imported} 个仓库：正在生成项目画像（不审历史提交），之后有新 push 才审查`);
    renderProjects();
  } catch (e) { toast(e.message, true); }
}

function bindProfileEditors(refresh = () => renderProjects()) {
  document.querySelectorAll("[data-edit-profile]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const pid = btn.dataset.editProfile;
      const view = document.getElementById(`profile-view-${pid}`);
      if (!view || view.querySelector("textarea")) return;
      const placeholder = "（还没有画像：自动生成中，或点「编辑」手写）";
      const current = view.textContent.trim() === placeholder ? "" : view.textContent;
      view.innerHTML = `<textarea class="profile-edit" rows="3"></textarea>
        <div class="actions" style="margin-top:8px">
          <button class="primary small-btn" data-save-profile="${pid}">保存</button>
          <button class="ghost small-btn" data-cancel-profile="${pid}">取消</button>
        </div>`;
      const ta = view.querySelector("textarea");
      ta.value = current.trim();
      ta.focus();
      view.querySelector(`[data-cancel-profile="${pid}"]`).onclick = () => refresh();
      view.querySelector(`[data-save-profile="${pid}"]`).onclick = async () => {
        try {
          await api(`/api/projects/${pid}`, { method: "PATCH", body: { profile: ta.value.trim() } });
          toast("画像已保存，之后的评审都会带上它");
          refresh();
        } catch (e) { toast(e.message, true); }
      };
    }));
  document.querySelectorAll("[data-reinit]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const r = await api(`/api/projects/${btn.dataset.reinit}/init`, { method: "POST" });
      toast(r.msg);
      refresh();
    }));
}

/* ---------------- 项目详情 ---------------- */

const jsCommitCache = {};   // pid -> {commits, error}：记上次结果，回访秒出

let detailSig = "";   // 上次渲染的数据签名：没变化就一根手指都不动

async function renderProjectDetail(pid, silent) {
  if (location.hash !== `#/projects/${pid}`) return;
  if (!silent) { setTitle("项目"); showSkeleton(); }
  const savedY = window.scrollY;
  const hadCache = !!jsCommitCache[pid];
  // 提交历史异步预热：不再阻塞整页。有缓存先用缓存秒开；首次拉到数据后静默补画
  const freshP = api(`/api/projects/${pid}/commits`)
    .then((d) => { jsCommitCache[pid] = { commits: d.commits || [], error: d.error || "" }; })
    .catch((e) => { jsCommitCache[pid] = { commits: jsCommitCache[pid]?.commits || [], error: e.message }; });
  if (!silent) {
    freshP.finally(() => {
      if (location.hash === `#/projects/${pid}`) renderProjectDetail(pid, true);
    });
  }
  let p, runs;
  try {
    [p, runs] = await Promise.all([
      api("/api/projects").then((l) => l.find((x) => x.id === pid)),
      api(`/api/runs?project_id=${pid}`),
    ]);
  } catch (e) {
    if (location.hash === `#/projects/${pid}`) errorPanel(e.message);
    return;
  }
  if (location.hash !== `#/projects/${pid}`) return;
  if (!p) { location.hash = "#/projects"; return; }
  const cached = jsCommitCache[pid];

  // 数据签名：静默刷新时没变化就直接返回（不重画、不闪）
  const sig = JSON.stringify([
    p.enabled, p.profile, p.init_status, p.webhook_attached, p.last_run,
    runs.map((r) => [r.id, r.status, r.important_count]),
    (cached?.commits || []).map((c) => [c.sha, c.run && c.run.status, c.run && c.run.important_count]),
    cached?.error || "",
  ]);
  if (silent && sig === detailSig) return;
  if (silent && document.querySelector(".profile-edit")) { detailSig = sig; return; }   // 编辑中不打断

  document.body.classList.add("no-anim");
  paintProjectDetail(p, cached?.commits || [], runs, cached?.error || "", !cached);
  detailSig = sig;
  if (silent) window.scrollTo(0, savedY);
  requestAnimationFrame(() => document.body.classList.remove("no-anim"));
  autoRefresh(() => renderProjectDetail(pid, true),
    runs.some((r) => r.status === "queued" || r.status === "running"));
}

function paintProjectDetail(p, commits, runs, commitErr = "", pending = false) {
  const pid = p.id;   // 修复：详情页所有绑定都依赖项目 id，之前引用了未定义的 pid
  setTitle(p.name);
  const last = p.last_run;
  const reviewed = commits.filter((c) => c.run).length;
  const blockedCount = runs.filter((r) => r.status === "blocked").length;
  const otherRuns = runs.filter((r) => !r.head_sha ||
    !commits.some((c) => c.sha === r.head_sha));
  const initChip = p.kind === "github" ? ({
    pending: `<span class="badge mock">画像生成中</span>`,
    running: `<span class="badge mock">画像生成中</span>`,
    failed: `<span class="badge imp">画像失败</span>`,
  }[p.init_status] || "") : "";

  $app.innerHTML = `
    ${pageHead({
      back: { label: "返回项目列表", hash: "#/projects" },
      crumbs: `项目 / ${esc(p.name)}`,
      title: `${esc(p.name)} ${p.kind === "local" ? `<span class="badge">本地</span>` : ""}
        ${p.auto_revert ? `<span class="badge" title="当前默认分支 push 工作流尚未接入自动回滚">自动回滚未接入</span>` : ""} ${initChip}
        ${p.enabled ? "" : `<span class="badge imp">已停用</span>`}`,
      sub: `<a href="https://github.com/${esc(p.full_name)}" target="_blank">${esc(p.full_name)}</a> · ${esc(p.default_branch)}`,
      actions: `<button class="primary" data-run-now="${p.id}">立即审查 HEAD</button>
        <button class="ghost" data-pipe="${p.id}">流水线</button>
        ${p.kind === "github" && !p.webhook_attached ? `<button class="ghost" data-hook="${p.id}">挂 webhook</button>` : ""}
        <button class="danger-ghost" data-del="${p.id}" data-name="${esc(p.name)}">移除</button>
        <label class="switch" title="启用/停用">
          <input type="checkbox" data-toggle="${p.id}" ${p.enabled ? "checked" : ""}>
          <span class="track"><span class="knob"></span></span>
        </label>`,
    })}
    <div class="stats stagger">
      <div class="panel stat"><div class="num">${runs.length}</div><div class="label">累计审查</div></div>
      <div class="panel stat"><div class="num" style="color:var(--bad)">${blockedCount}</div><div class="label">发现问题</div></div>
      <div class="panel stat"><div class="num">${reviewed}<span class="muted" style="font-size:16px">/${commits.length}</span></div><div class="label">近 30 次提交已覆盖</div></div>
      <div class="panel stat"><div class="num" style="font-size:17px;padding-top:8px">${last ? statusHtml(last.status) : '<span class="muted small">等新 push</span>'}</div><div class="label">最近一次</div></div>
    </div>

    <div class="sec-title">项目画像（评审的背景上下文，可编辑）</div>
    <div class="panel" style="padding:14px 18px">
      <div class="profile-block" style="padding:0;border:none">
        <div class="profile-head">
          ${p.kind === "github" && p.init_status === "failed" ? `<span class="badge imp">自动生成失败</span>` : ""}
          <span class="actions">
            ${p.kind === "github" ? `<button class="ghost small-btn" data-reinit="${p.id}">重新生成</button>` : ""}
            <button class="ghost small-btn" data-edit-profile="${p.id}">编辑</button>
          </span>
        </div>
        <div class="profile-text" id="profile-view-${p.id}">${esc(p.profile || "（还没有画像：自动生成中，或点「编辑」手写）")}</div>
      </div>
    </div>

    <div class="sec-title">Git 历史 <span class="faint small mono" style="letter-spacing:0;text-transform:none">点行看审查报告；未审查的可当场发起</span></div>
    <div id="commit-section">
    ${pending
      ? `<div class="panel" style="padding:16px 20px">
           <div class="skel" style="padding:0"><div class="skel-line w60"></div><div class="skel-line w60"></div><div class="skel-line w30"></div></div>
           <div class="muted small" style="margin-top:10px">正在从 GitHub 拉取提交历史（直连较慢，约 2–3 秒，之后有缓存秒开；页面其余部分不受影响）</div>
         </div>`
      : commitErr
        ? `<div class="panel empty">提交历史暂时拉取失败（网络抖动）：${esc(commitErr)}<br>稍后刷新重试；下面的审查动态不受影响</div>`
        : `<div class="panel stagger">${commitTable(p, commits)}</div>`}
    </div>

    ${otherRuns.length ? `
      <div class="sec-title">PR / 手动审查动态（不在分支历史里的提交）</div>
      <div class="panel stagger">${runMiniTable(otherRuns)}</div>` : ""}
  `;

  bindProfileEditors(() => renderProjectDetail(pid, true));
  bindRunRows();
  document.querySelectorAll("[data-toggle]").forEach((el) =>
    el.addEventListener("change", async () => {
      await api(`/api/projects/${el.dataset.toggle}`, { method: "PATCH", body: { enabled: el.checked } });
      renderProjectDetail(pid, true);
    }));
  document.querySelectorAll("[data-del]").forEach((el) =>
    el.addEventListener("click", async () => {
      if (!await uiConfirm(`移除项目 ${el.dataset.name}？（审查记录一并删除）`)) return;
      await api(`/api/projects/${el.dataset.del}`, { method: "DELETE" });
      location.hash = "#/projects";
    }));
  document.querySelectorAll("[data-run-now]").forEach((el) =>
    el.addEventListener("click", async () => {
      const r = await api("/api/runs", { method: "POST", body: { project_id: +el.dataset.runNow } });
      toast(r.queued ? `已入队（run #${r.run_id}）` : r.msg);
      if (r.run_id) location.hash = `#/runs/${r.run_id}`;
    }));
  document.querySelectorAll("[data-hook]").forEach((el) =>
    el.addEventListener("click", async () => {
      try {
        const r = await api(`/api/projects/${el.dataset.hook}/attach_webhook`, { method: "POST" });
        toast(`webhook 已挂到 ${r.url}`);
        renderProjectDetail(pid, true);
      } catch (e) { toast(e.message, true); }
    }));
  document.querySelectorAll("[data-pipe]").forEach((el) =>
    el.addEventListener("click", () => { location.hash = `#/projects/${pid}/pipeline`; }));
  document.querySelectorAll("tr.commit-row[data-run-id]").forEach((tr) =>
    tr.addEventListener("click", (ev) => {
      if (ev.target.closest("button,a")) return;
      location.hash = `#/runs/${tr.dataset.runId}`;
    }));
  document.querySelectorAll("[data-review-sha]").forEach((btn) =>
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const r = await api("/api/runs", { method: "POST",
        body: { project_id: pid, head_sha: btn.dataset.reviewSha, ref: p.default_branch } });
      toast(r.queued ? `已入队审查 ${btn.dataset.reviewSha.slice(0, 8)}` : r.msg);
      if (r.run_id) location.hash = `#/runs/${r.run_id}`;
    }));
}

function commitTable(p, commits) {
  if (!commits.length) return `<div class="empty">该分支还没有提交</div>`;
  return `<table><thead><tr>
      <th style="width:112px">审查状态</th><th>提交说明</th>
      <th>提交</th><th>作者</th><th>时间</th><th style="width:104px"></th></tr></thead>
    <tbody>${commits.map((c) => {
      const rr = c.run;
      const status = rr
        ? `<span class="status"><span class="dot ${esc(rr.status)}"></span>${STATUS_LABEL[rr.status] || rr.status}${rr.important_count ? ` <span class="badge imp">${rr.important_count}</span>` : ""}</span>`
        : `<span class="muted small">未审查</span>`;
      const action = rr
        ? `<span class="goto-hint">查看报告 →</span>`
        : `<button class="ghost small-btn" data-review-sha="${esc(c.sha)}">审查此提交</button>`;
      return `<tr class="commit-row" ${rr ? `data-run-id="${rr.id}" style="cursor:pointer"` : ""}>
        <td>${status}</td>
        <td class="small" style="max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.msg)}</td>
        <td class="mono small">${p.kind === "github" && p.full_name ? `<a href="https://github.com/${esc(p.full_name)}/commit/${esc(c.sha)}" target="_blank">${esc(c.sha.slice(0, 8))}</a>` : esc(c.sha.slice(0, 8))}</td>
        <td class="small muted">${esc(c.author)}</td>
        <td class="mono small muted">${fmtTime(c.date)}</td>
        <td>${action}</td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

function runMiniTable(runs) {
  return `<table><tbody>${runs.map((r) => `
    <tr class="rowlink" data-run="${r.id}">
      <td style="width:118px">${statusHtml(r.status)}</td>
      <td class="small">${TRIGGER_LABEL[r.trigger] || r.trigger}${r.pr_number ? ` <span class="mono">#${r.pr_number}</span>` : ""}</td>
      <td class="mono small muted">${r.head_sha ? esc(r.head_sha.slice(0, 8)) : "—"}</td>
      <td>${r.important_count > 0 ? `<span class="badge imp">${r.important_count} Important</span>` : ""}</td>
      <td class="mono small muted">${fmtTime(r.created_at)}</td>
      <td><span class="go">→</span></td>
    </tr>`).join("")}</tbody></table>`;
}

/* ---------------- 审查报告 ---------------- */

const runCache = {};   // runId -> 上一次的 run JSON，用于增量 diff

async function renderRun(id, silent) {
  if (!silent) {
    setTitle(`审查报告 #${id}`);
    if (!runCache[id]) showSkeleton();
  }
  let fresh;
  try { fresh = await api(`/api/runs/${id}`); }
  catch (e) { if (!silent) errorPanel(e.message); return; }
  if (location.hash !== `#/runs/${id}`) return;

  const prev = runCache[id];
  if (!prev || !silent) {
    // 首次/手动刷新：整页画一次
    paintRunShell(fresh);
    patchRunDynamic(null, fresh);
    runCache[id] = fresh;
    autoRefresh(() => renderRun(id, true),
      fresh.status === "queued" || fresh.status === "running");
    return;
  }
  // 静默刷新：增量更新，只动有变化的部分
  if (document.querySelector(".profile-edit")) return;
  const changed = JSON.stringify(prev) !== JSON.stringify(fresh);
  if (changed) {
    patchRunDynamic(prev, fresh);
    runCache[id] = fresh;
  }
  if (fresh.status !== "queued" && fresh.status !== "running") autoRefresh(null, false);
}

function runBannerHtml(run) {
  const banner = {
    passed: run.diff_complete === 1
      ? `<div class="banner passed"><span class="mark"></span><div><b>审查完成</b> —— 完整变更已输入评审，未报告 Important；这不保证没有遗漏。</div></div>`
      : `<div class="banner failed"><span class="mark"></span><div><b>历史记录标记为通过</b> —— 当时未记录 diff 完整性，旧版可能截断审查输入；需要可信结论请重新审查。</div></div>`,
    blocked: `<div class="banner blocked"><span class="mark"></span><div><b>发现问题</b> —— ${run.important_count} 个 Important；${run.revert_pr_url ? `已自动创建回滚 PR：<a href="${esc(run.revert_pr_url)}" target="_blank">${esc(run.revert_pr_url)}</a>` : "请修复发现；已进入默认分支的变更可按需创建回滚 PR"}${run.diff_complete === 1 ? "" : "。历史输入完整性未知，可能还有未检查的变更"}</div></div>`,
    failed: `<div class="banner failed"><span class="mark"></span><div><b>执行失败</b> —— ${esc(run.error || "未知错误")}（引擎异常不会判定通过）
      <div class="failure-triage-state">${run.failure_action === "handled" ? `✓ 已标记处理 · ${fmtTime(run.failure_at)}${run.failure_note ? ` · ${esc(run.failure_note)}` : ""}`
        : "仍在工作台待处理；标记处理只移出待办，不会把这次审查改为通过。"}</div>
      ${(run.failure_history || []).length ? `<details class="failure-history"><summary>查看处置记录</summary>${run.failure_history.map((e) =>
        `<div>${fmtTime(e.created_at)} · ${e.action === "handled" ? "标记已处理" : "重新列为待处理"}${e.note ? ` · ${esc(e.note)}` : ""}</div>`).join("")}</details>` : ""}</div></div>`,
    running: `<div class="banner running"><span class="mark"></span><div><b>审查进行中</b>… 只刷新变化的步骤，页面不再整页重载</div></div>`,
    queued: `<div class="banner running"><span class="mark"></span><div><b>排队中</b>…</div></div>`,
  }[run.status] || "";
  return banner + ((run.improvements || []).length ? `<details class="panel" style="padding:12px;margin-bottom:12px"><summary class="small">本次使用的项目改进 · ${run.improvements.length} 项</summary>${run.improvements.map(u=>`<div class="small" style="padding:8px"><b>#${u.improvement_id} v${u.revision} · ${esc(u.snapshot.title)}</b><p>${esc(u.snapshot.instruction)}</p><code>${esc(u.snapshot.scope)}</code></div>`).join("")}</details>` : "");
}

async function triageFailure(runId, action, onDone) {
  let note = "";
  if (action === "handled") {
    note = await uiPrompt("标记失败审查已处理", {
      message: "可选填处理说明。此操作仅从工作台移除待办；审查仍保留“执行失败”，不会变为通过。",
    });
    if (note === null) return;
  } else if (!await uiConfirm("重新放回工作台待处理？审查仍保留“执行失败”。")) return;
  try {
    await api(`/api/runs/${runId}/failure-triage`, {method: "POST", body: {action, note}});
    toast(action === "handled" ? "已从工作台待办移除；失败记录仍保留" : "已重新列入待处理");
    onDone?.();
  } catch (e) { toast(e.message, true); }
}

function runKvHtml(run) {
  const add = run.additions ?? 0, del = run.deletions ?? 0;
  const total = Math.max(add + del, 1);
  const commitMsg = (run.commit_msg || "").trim();
  const subject = commitMsg.split("\n")[0] || "";
  const bodyRest = commitMsg.split("\n").slice(1).join("\n").trim();
  const intent = (run.intent || "").trim();
  const intentExtra = intent && intent !== commitMsg;
  return `
      <div class="ctx-row">
        <div class="ctx-item"><div class="ctx-label">分支</div><div><span class="branch-chip">${esc(run.branch || "—")}</span></div></div>
        <div class="ctx-item"><div class="ctx-label">提交</div><div>${shaLink(run.repo, run.head_sha)}</div></div>
        <div class="ctx-item"><div class="ctx-label">提交者</div><div>${esc(run.commit_author || "—")}</div></div>
        <div class="ctx-item"><div class="ctx-label">提交时间</div><div class="mono small">${fmtTime(run.commit_date) || "—"}</div></div>
        <div class="ctx-item"><div class="ctx-label">变更量</div><div class="diffstat">
          <span class="mono small">${run.files_changed ?? "—"} 文件</span>
          <span class="mono small add">+${add}</span><span class="bar"><i class="bar-add" style="width:${(add / total) * 100}%"></i></span>
          <span class="mono small del">−${del}</span><span class="bar"><i class="bar-del" style="width:${(del / total) * 100}%"></i></span>
        </div></div>
      </div>
      <div class="commit-block">
        <div class="ctx-label">提交信息（变更的声明目的）</div>
        ${subject ? `<div class="commit-subject">${esc(subject)}</div>` : `<div class="muted small">—（旧数据无提交信息；新审查会自动采集）</div>`}
        ${bodyRest ? `<pre class="commit-body">${esc(bodyRest)}</pre>` : ""}
        ${intentExtra ? `<div class="ctx-label" style="margin-top:14px">PR 描述</div><pre class="commit-body">${esc(intent)}</pre>` : ""}
      </div>`;
}

/* 本次生效的编排快照 + 执行进度：coder 验证"自编排真的生效"+"进行到哪一步"的依据 */
function runPipelineHtml(run) {
  let snap = null;
  try { snap = JSON.parse(run.pipeline_snapshot || "null"); } catch (e) { /* 旧数据 */ }
  if (!snap) return `<span class="faint small">旧记录未存编排快照（新审查起可见）</span>`;
  const src = snap.source === "project"
    ? '<span class="badge confirmed">项目编排生效</span>' : '<span class="badge">全局默认</span>';
  const byName = new Map((run.steps || []).map((s) => [s.name, s]));
  let done = 0;
  const visibleSteps = (snap.steps || []).flatMap((s) => s.kind === "review"
    ? (s.reviewers || []).filter((m) => m.enabled !== false).map((m) => ({ ...m, group: s.name || "交叉评审" })) : [s]);
  const chips = visibleSteps.map((s, i) => {
    const meta = s.kind === "gate"
      ? `${GATE_LABEL[s.gate] || s.gate}${s.sandbox === "docker" ? " · Docker" : " · 本机"}`
      : `${s.group ? "交叉评审 · " : "上下文 · "}${s.kind === "api" ? "模型 API" : s.provider}${s.model ? " · " + s.model : ""}`;
    const st = byName.get(s.name || s.id);
    const status = st ? st.status : "pending";
    if (status === "ok" || status === "fail" || status === "skip") done++;
    const mark = { ok: "✓", fail: "✗", skip: "—" }[status]
      || (status === "running" ? '<span style="animation:pulse 1.2s ease infinite">◐</span>' : "·");
    const cls = status === "ok" ? "confirmed" : status === "fail" ? "imp"
      : status === "running" ? "mock" : "";
    return `<span class="badge ${cls}" title="${esc(meta)}">${mark} ${i + 1}. ${esc(s.name || s.id)}</span>`;
  });
  const total = visibleSteps.length;
  const progress = `<span class="muted small mono">${done}/${total}</span>`;
  return `<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">${src}${chips.join(" ")}${progress}</div>`;
}

function findingsAreaHtml(run) {
  // _idx：发现在 findings_json 数组里的下标，处置标记按它定位
  const items = (run.findings || []).map((f, i) => ({ ...f, _idx: i }));
  const imp = items.filter((f) => f.severity === "IMPORTANT");
  const nit = items.filter((f) => f.severity === "NIT");
  const pre = items.filter((f) => f.severity === "PRE-EXISTING");
  if (!items.length) {
    return run.status === "passed"
      ? `<div class="sec-title">发现</div><div class="panel empty">无</div>` : "";
  }
  return `<div class="sec-title">发现（${imp.length} Important · ${nit.length} Nit · ${pre.length} Pre-existing）</div>
    ${findingGroup(run, "Important · 优先处理", imp, "imp")}
    ${findingGroup(run, "Nit · 供参考", nit, "nit")}
    ${findingGroup(run, "Pre-existing · 历史问题", pre, "pre")}`;
}

async function loadApprovals(run) {
  const box = $("approval-box");
  const cached = runCache[run.id];
  if (!box || !cached || cached._approvals_loaded) return;
  cached._approvals_loaded = true;   // 先置位防重复；失败时回滚允许重试
  try {
    const a = await api(`/api/runs/${run.id}/approvals`);
    if (location.hash !== `#/runs/${run.id}`) return;
    box.innerHTML = approvalsHtml(a);
  } catch (e) {
    cached._approvals_loaded = false;
    box.innerHTML = `<div class="panel small" style="padding:10px 18px;margin-bottom:22px">
      <span class="muted small">审批状态拉取失败：${esc(e.message)}</span></div>`;
  }
}

function approvalsHtml(a) {
  if (!a || a.applicable === false) return "";
  const human = a.human_approved || [];
  const chips = [];
  chips.push(human.length
    ? `<span class="badge confirmed">人工批准 ${human.length}（${esc(human.join("、"))}）</span>`
    : (a.need_approval ? `<span class="badge imp">待人工批准</span>` : `<span class="badge">尚无人工批准</span>`));
  if ((a.human_changes || []).length) {
    chips.push(`<span class="badge imp">要求修改 ${a.human_changes.length}</span>`);
  }
  if (a.agent_approved) {
    chips.push(`<span class="badge mock" title="agent 的批准不代表人类决策">agent 批准 ${a.agent_approved}（不计）</span>`);
  }
  const warn = a.need_approval
    ? `<div class="hint" style="color:var(--warn)">风险 ${esc(a.tier || "")}：按规则需人工批准后合入</div>` : "";
  return `<div class="panel" style="padding:12px 18px;margin-bottom:22px">
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <span class="ctx-label" style="margin:0">PR 审批</span>${chips.join(" ")}</div>
    ${warn}</div>`;
}

function paintRunShell(run) {
  $app.innerHTML = `
    ${pageHead({
      back: { label: "返回项目", hash: `#/projects/${run.project_id}` },
      crumbs: `项目 / <a href="#/projects/${run.project_id}">${esc(run.project)}</a> / 审查报告`,
      title: `审查报告 <span class="muted" style="font-size:15px;font-weight:400">run #${run.id}</span>`,
      sub: `${TRIGGER_LABEL[run.trigger] || run.trigger}${run.pr_number ? ` · PR #${run.pr_number}` : ""}
        · ${shaLink(run.repo, run.head_sha)}
        ${run.tier ? ` · <span class="badge tier">风险 ${TIER_LABEL[run.tier] || esc(run.tier)}</span>` : ""}`,
      actions: `<div class="actions"><a class="ghost small-btn run-workbench-link" href="#/workbench">返回质量工作台</a>
        <div class="actions" id="run-actions"></div></div>`,
    })}
    <div id="banner-box">${runBannerHtml(run)}</div>
    <div id="approval-box"></div>
    <div class="run-layout">
      <div class="run-main">
        <div id="findings-box"></div>
        <div class="sec-title">审查过程 <span class="small muted">展开步骤查看日志与推理</span></div>
        <div class="panel" style="padding:6px 20px"><div class="steps" id="steps-list"></div></div>
      </div>
      <aside class="run-side">
        <div class="sec-title">变更上下文</div>
        <div class="panel ctx" id="run-kv">${runKvHtml(run)}</div>
        <div id="run-diff-control"></div>
        <div class="sec-title">执行流水线</div>
        <div class="panel" id="run-pipeline" style="padding:12px 18px"></div>
      </aside>
    </div>
    <section class="panel run-diff-panel" id="run-diff-panel" hidden></section>`;
}

async function showRunDiff(run) {
  const box = $("run-diff-panel");
  if (!box) return;
  box.hidden = false;
  box.textContent = "正在读取本次 diff…";
  try {
    const result = await api(`/api/runs/${run.id}/diff`);
    if (location.hash !== `#/runs/${run.id}` || !box.isConnected) return;
    box.innerHTML = `<div class="run-diff-head"><div><h3>${result.complete ? "本次完整 diff" : "历史 diff（完整性未知）"}</h3>
      ${result.complete ? "" : '<p>旧版可能只保存了部分 diff，也可能只给评审器看了前半段；这份历史结果不能证明完整审查。</p>'}</div>
      <div class="actions"><button class="ghost small-btn" data-copy-diff>复制 diff</button>
      <button class="ghost small-btn" data-close-diff>收起</button></div></div><pre class="run-diff-content"></pre>`;
    box.querySelector("pre").textContent = result.diff;
    box.querySelector("[data-copy-diff]").onclick = async () => {
      try { await navigator.clipboard.writeText(result.diff); toast("完整 diff 已复制"); }
      catch (e) { toast("复制失败：" + e.message, true); }
    };
    box.querySelector("[data-close-diff]").onclick = () => { box.hidden = true; box.innerHTML = ""; };
    box.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { box.textContent = `读取 diff 失败：${e.message}`; }
}

function patchRunDynamic(prev, fresh) {
  const pv = prev || {};
  // PR 终态后拉一次审批状态（人类批准才算数；agent 批准只展示不计入）
  if (["passed", "blocked", "failed"].includes(fresh.status) && fresh.pr_number) {
    loadApprovals(fresh);
  }
  // banner
  const $b = $("banner-box");
  if ($b && (pv.status !== fresh.status || pv.diff_complete !== fresh.diff_complete || pv.revert_pr_url !== fresh.revert_pr_url
      || pv.important_count !== fresh.important_count || pv.failure_action !== fresh.failure_action)) {
    $b.innerHTML = runBannerHtml(fresh);
  }
  // 头部操作区（回滚按钮随状态出现/消失）
  const $a = $("run-actions");
  if ($a) {
    const html = `${fresh.status === "blocked" && fresh.head_sha && fresh.project_kind === "github" && !fresh.revert_pr_url
      ? `<button id="revert-btn">创建回滚 PR</button>` : ""}
      ${fresh.status === "failed" ? `<button id="failure-triage-btn" class="${fresh.failure_action === "handled" ? "ghost" : "primary"}">${fresh.failure_action === "handled" ? "重新列为待处理" : "标记已处理"}</button>` : ""}`;
    if ($a.innerHTML !== html) {
      $a.innerHTML = html;
      const revertBtn = $("revert-btn");
      if (revertBtn) revertBtn.onclick = async () => {
        if (!await uiConfirm("基于该 run 的 commit 创建回滚 PR？")) return;
        try {
          const r = await api(`/api/runs/${fresh.id}/revert`, { method: "POST" });
          toast(`回滚 PR 已创建：${r.pr_url}`);
          renderRun(fresh.id, true);
        } catch (e) { toast(e.message, true); }
      };
      const triageBtn = $("failure-triage-btn");
      if (triageBtn) triageBtn.onclick = () => triageFailure(fresh.id,
        fresh.failure_action === "handled" ? "reopen" : "handled", () => renderRun(fresh.id, true));
    }
  }
  // 本次流水线快照
  const $pl = $("run-pipeline");
  if ($pl) {
    const html = runPipelineHtml(fresh);
    if ($pl.innerHTML !== html) $pl.innerHTML = html;
  }
  // 变更上下文
  const $k = $("run-kv");
  const kv = runKvHtml(fresh);
  if ($k && $k.innerHTML !== kv) $k.innerHTML = kv;
  const $dc = $("run-diff-control");
  if ($dc) {
    const html = fresh.has_diff
      ? `<button class="ghost small-btn" id="load-run-diff">${fresh.diff_complete === 1 ? "查看完整 diff" : "查看历史 diff（完整性未知）"}</button>`
      : "";
    if ($dc.innerHTML !== html) $dc.innerHTML = html;
    const btn = $("load-run-diff");
    if (btn) btn.onclick = () => showRunDiff(fresh);
  }
  // 步骤：按 id 对账，只替换变化的步骤节点
  const sc = $("steps-list");
  if (sc) {
    // 首次渲染：失败步骤默认展开（结论异常先给证据）；此后尊重用户的手动开合
    const openSet = new Set([...sc.querySelectorAll("details[open]")].map((d) => d.dataset.step));
    if (!pv.steps) {
      (fresh.steps || []).forEach((s) => { if (s.status === "fail") openSet.add(String(s.id)); });
    }
    const ids = new Set((fresh.steps || []).map((s) => String(s.id)));
    [...sc.querySelectorAll("[data-step-id]")].forEach((el) => {
      if (!ids.has(el.dataset.stepId)) el.remove();
    });
    (fresh.steps || []).forEach((s) => {
      const prevS = (pv.steps || []).find((x) => String(x.id) === String(s.id));
      const changed = !prevS || prevS.status !== s.status || prevS.duration_ms !== s.duration_ms
        || (prevS.trace || "") !== (s.trace || "") || (prevS.log || "") !== (s.log || "")
        || (prevS.prompt || "") !== (s.prompt || "");
      let el = sc.querySelector(`[data-step-id="${s.id}"]`);
      if (!el || changed) {
        const tmp = document.createElement("div");
        tmp.innerHTML = stepHtml(s, fresh, openSet);
        const node = tmp.firstElementChild;
        node.dataset.stepId = String(s.id);
        if (el) el.replaceWith(node); else sc.appendChild(node);
      }
    });
    (fresh.steps || []).forEach((s) => {
      const el = sc.querySelector(`[data-step-id="${s.id}"]`);
      if (el) sc.appendChild(el);   // 按顺序归位（appendChild 移动已有节点）
    });
  }
  // 发现区
  const $f = $("findings-box");
  const fHtml = findingsAreaHtml(fresh);
  if ($f && $f.innerHTML !== fHtml) $f.innerHTML = fHtml;
  const revertBtn = $("revert-btn");
  if (revertBtn && !revertBtn.onclick) revertBtn.onclick = async () => {
    if (!await uiConfirm("基于该 run 的 commit 创建回滚 PR？")) return;
    try {
      const r = await api(`/api/runs/${fresh.id || prev.id}/revert`, { method: "POST" });
      toast(`回滚 PR 已创建：${r.pr_url}`);
      renderRun(fresh.id || prev.id, true);
    } catch (e) { toast(e.message, true); }
  };
}

function paintRun(run, openSet) {
  setTitle(`审查报告 #${run.id} ${run.project}`);
  paintRunShell(run);
  patchRunDynamic(null, run);
}

function stepHtml(s, run, openSet) {
  const isOpen = openSet.has(String(s.id)) ? "open" : "";
  const hasLog = s.log && s.log.trim();
  const isReviewer = s.name.includes("评审") && s.status === "ok";
  // 思考过程时间线：默认折叠，摘要显示规模；点开看全貌
  let main = "";
  let hasTrace = false, traceStats = null;
  if (s.prompt) {
    // 输入提示词：模型实际收到的完整 prompt（初始模板渲染后的全文，含 diff/规则/画像/意图）
    main += `<details data-step="${s.id}"><summary>输入提示词（实际发给模型的全文）</summary>
      <pre>${esc(s.prompt)}</pre></details>`;
  }
  if (s.trace) {
    try {
      const events = JSON.parse(s.trace);
      if (Array.isArray(events) && events.length) {
        hasTrace = true;
        const think = events.filter((e) => e.t === "think").length;
        const tool = events.filter((e) => e.t === "tool").length;
        traceStats = { think, tool, total: events.length };
        main += `<details class="trace-wrap" data-step="${s.id}" ${isOpen ? "open" : ""}>
          <summary>思考过程 · ${think} 次思考 · ${tool} 次工具调用 · 共 ${events.length} 步</summary>
          <div class="trace">${events.map((e) => {
            if (e.t === "think") return `<div class="tr-think"><span class="tr-tag">思考</span><span>${esc(e.text)}</span></div>`;
            if (e.t === "tool") return `<div class="tr-tool"><span class="tr-tag">工具</span><span class="mono">${esc(e.name)}</span><span class="mono muted">${esc(e.detail || "")}</span></div>`;
            if (e.t === "text") return `<div class="tr-text">${esc(e.text)}</div>`;
            if (e.t === "meta") return `<div class="tr-meta">${esc(e.text)}</div>`;
            return "";
          }).join("")}</div>
        </details>`;
      }
    } catch (e) { /* 轨迹解析失败就只显示原文 */ }
  }
  if (isReviewer && hasLog) {
    const parsed = parseReview(s.log);
    if (parsed.summary) main += `<div class="review-summary">${esc(parsed.summary)}</div>`;
    if (parsed.findings && parsed.findings.length) {
      main += `<div class="mini-findings">${parsed.findings.map((f) => `
        <div class="mini-finding sev-${esc((f.severity || "nit").toLowerCase())}">
          <span class="badge ${f.severity === "Important" ? "imp" : f.severity === "Pre-existing" ? "pre" : "nit"}">${esc(f.severity || "Nit")}</span>
          ${fileLink(run, f.file, f.line)}
          <span class="small">${esc(f.title)}</span>
        </div>`).join("")}</div>`;
    }
    // 原始输出已删除：与思考轨迹 + 总评 + 发现完全重复
  } else if (hasLog && !hasTrace) {
    main += `<details data-step="${s.id}" ${isOpen}><summary>展开输出</summary><pre>${esc(s.log)}</pre></details>`;
  }
  return `<div class="step s-${esc(s.status)}" data-step-id="${esc(s.id)}">
    <div class="rail"><span class="sdot"></span></div>
    <div>
      <div class="sname">${esc(s.name)}
        <span class="badge" style="margin-left:8px">${esc(s.status)}</span>
        ${s.duration_ms != null ? `<span class="muted small mono" style="margin-left:8px">${fmtDur(s.duration_ms)}</span>` : ""}
      </div>
      ${main ? `<div class="step-main">${main}</div>` : ""}
    </div>
    <div class="sinfo mono small">${s.finished_at ? fmtTime(s.finished_at) : ""}</div>
  </div>`;
}

function parseReview(log) {
  const i = log.indexOf("```json");
  const summary = (i >= 0 ? log.slice(0, i) : log).trim();
  let findings = null;
  if (i >= 0) {
    try {
      const m = log.slice(i).match(/```json\s*([\s\S]*?)```/);
      findings = JSON.parse(m[1]).findings;
    } catch (e) { /* 原样展示 */ }
  }
  return { summary, findings };
}

function canMark(run) {
  return ["passed", "blocked", "failed"].includes(run.status);
}

function markButtonsHtml(run, f) {
  if ((f.sources || []).some((s) => String(s).startsWith("gate:"))) return "";   // 机械门禁是客观结果
  const cur = f.disposition || "";
  const btn = (val, label) =>
    `<button class="ghost small-btn mark-btn${cur === val ? " on" : ""}"
       data-mark="${val}" data-run="${run.id}" data-idx="${f._idx}" data-cur="${esc(cur)}"
       title="${cur === val ? "再点一次撤销标记" : "标记为" + label}">${cur === val ? "✓ " : ""}${label}</button>`;
  const evalBtn = (run.project_kind === "github" && ["IMPORTANT", "NIT"].includes(f.severity) && cur)
    ? `<button class="ghost small-btn" data-eval-run="${run.id}" data-eval-idx="${f._idx}"
        title="确认=应报出样本；误报=不应再报样本">存为样本</button>` : "";
  const progressBtn = cur ? `<button class="ghost small-btn" data-feedback-run="${run.id}" data-feedback-idx="${f._idx}">反馈进度 ↗</button>` : "";
  return `${btn("confirmed", "确认")}${btn("false_positive", "误报")}${evalBtn}${progressBtn}`;
}

/* 复制完整问题（标题/位置/触发/建议），发给改代码的人直接能看懂 */
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy-run]");
  if (!b) return;
  e.stopPropagation();
  const run = runCache[+b.dataset.copyRun];
  const f = run && run.findings && run.findings[+b.dataset.copyIdx];
  if (!f) { toast("未取到该发现的完整内容（刷新页面后再试）", true); return; }
  const text = `【${f.severity === "IMPORTANT" ? "Important" : f.severity || "Nit"}】${f.title}\n`
    + `位置：${f.file || "?"}:${f.line ?? "?"}\n`
    + (f.condition ? `触发：${f.condition}\n` : "")
    + (f.fix ? `建议：${f.fix}\n` : "")
    + `—— Review Hub run #${run.id}（${run.project || ""}）`;
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制完整问题");
  } catch (err) { toast("复制失败：" + err.message, true); }
});

function findingGroup(run, title, list, cls) {
  if (!list.length) return "";
  return `<div class="sec-title finding-group-title">${esc(title)} <span class="small">${list.length}</span></div>
  <div class="panel finding-list">${list.map((f) => {
    const isGate = (f.sources || []).some((s) => String(s).startsWith("gate:"));
    return `<article class="finding-row f-${cls}">
      <div class="finding-row-main">
        <div class="finding-row-info">
          <div class="finding-row-title"><span class="finding-severity ${cls}">${esc(f.severity || "发现")}</span>
            <strong title="${esc(f.title)}">${esc(f.title)}</strong>
            ${f.disposition ? `<span class="badge ${f.disposition === "confirmed" ? "confirmed" : "fp"}">${f.disposition === "confirmed" ? "已确认" : "误报"}</span>` : ""}</div>
          <div class="finding-row-meta">${fileLink(run, f.file, f.line)}
            <span>${esc((f.sources || []).join(" + "))}</span></div>
        </div>
        <div class="finding-row-actions">
          <button class="fr-copy" data-copy-run="${run.id}" data-copy-idx="${f._idx}"
            title="复制标题、位置、触发条件和建议">复制</button>
          ${isGate ? '<button class="ghost small-btn" data-goto-log>门禁日志</button>' : markButtonsHtml(run, f)}
          <button class="ghost small-btn" data-finding-expand aria-expanded="false">查看全部</button>
        </div>
      </div>
      <div class="finding-row-detail" hidden>
        <div><span class="f-key">触发条件与影响</span><p>${esc(f.condition || "未提供")}</p></div>
        <div><span class="f-key">建议修复</span><p>${esc(f.fix || "未提供")}</p></div>
      </div>
    </article>`;
  }).join("")}</div>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-finding-expand]");
  if (!b) return;
  const detail = b.closest(".finding-row").querySelector(".finding-row-detail");
  const open = detail.hidden;
  detail.hidden = !open;
  b.setAttribute("aria-expanded", String(open));
  b.textContent = open ? "收起" : "查看全部";
});

/* ---------------- 配置中心 ---------------- */

function cfgBadge(set) {
  return set ? `<span class="badge">已配置</span>` : `<span class="badge imp">未配置</span>`;
}

function checkStatusHtml(check) {
  if (!check) return '<span class="muted">未检测</span>';
  const expired = Date.now() - Date.parse(check.checked_at) > 10 * 60 * 1000;
  const stale = check.stale || expired;
  const label = stale ? "结果已过期" : check.status === "ok" ? "可用" : "检测失败";
  const badge = stale ? "mock" : check.status === "ok" ? "confirmed" : "imp";
  const cost = Number.isFinite(check.latency_ms) ? ` · ${(check.latency_ms / 1000).toFixed(1)}s` : "";
  return `<span class="badge ${badge}">${label}</span> <span class="muted">${fmtTime(check.checked_at)}${cost}</span>
    ${check.detail ? `<span class="check-detail" title="${esc(check.detail)}">${esc(check.detail)}</span>` : ""}`;
}

const SETTINGS_TABS = [
  ["pipeline", "流水线编排"],
  ["engine", "评审引擎"],
  ["creds", "凭证与模型"],
];

/* ---------------- 评测页（#/eval）：处置沉淀用例 → 改配置前跑回归 ---------------- */

async function renderEval() {
  clearInterval(refreshTimer);
  setTitle("评测");
  showSkeleton();
  let d;
  try { d = await api("/api/eval/last"); }
  catch (e) { errorPanel(e.message); return; }
  paintEval(d);
  if (d.history.length && d.history[0].status === "running") pollEval();
}

function evalRunHtml(h, prev, opts = {}) {
  const changes = [];
  let diffHtml = "";
  if (prev) {
    const prevMap = new Map((prev.results || []).map((r) => [r.case_id, r.result]));
    (h.results || []).forEach((r) => {
      if (prevMap.has(r.case_id) && prevMap.get(r.case_id) !== r.result) {
        changes.push({ r, from: prevMap.get(r.case_id) });
      }
    });
    diffHtml = changes.length
      ? `<div style="margin-top:12px"><span class="ctx-label">与上一轮的差异（${changes.length} 条变化）</span>
        ${changes.map((c) => `<div class="small" style="margin-top:3px">· ${esc(c.r.expected)}：
          <span class="badge">${RESULT_LABEL[c.from] || esc(c.from)}</span> →
          <span class="badge ${c.r.result === "hit" ? "confirmed" : "imp"}">${RESULT_LABEL[c.r.result] || esc(c.r.result)}</span></div>`).join("")}</div>`
      : `<div class="hint">与上一轮逐用例结果完全一致</div>`;
  }
  const configChanges = [];
  if (prev) {
    if ((h.config || {}).prompt !== (prev.config || {}).prompt) configChanges.push('<span class="badge imp">提示词已改</span>');
    if (JSON.stringify((h.config || {}).pipeline) !== JSON.stringify((prev.config || {}).pipeline)) {
      configChanges.push('<span class="badge mock">编排/模型已改</span>');
    }
    if (!configChanges.length) configChanges.push('<span class="badge">配置未变</span>');
  }
  const stats = h.status === "running" ? '<span class="badge mock">进行中</span>'
    : h.status === "failed" ? `<span class="badge imp">失败</span> ${esc(h.error_msg || "")}`
    : `命中 <b class="add">${h.hit}</b> · 漏报 <b class="del">${h.miss}</b> · 误报重现 ${h.fp} · 错误 ${h.error} / 共 ${h.cases_total}`;
  const rows = (h.results || []).map((r) => `
    <tr>
      <td>${r.polarity === "positive" ? '<span class="badge confirmed">正</span>' : '<span class="badge fp">负</span>'}</td>
      <td class="small" style="max-width:240px">${esc(r.expected)}</td>
      <td><span class="badge ${r.result === "hit" ? "confirmed" : "imp"}">${RESULT_LABEL[r.result] || esc(r.result)}</span>
        <span class="faint small mono">${esc(r.detail || "")}</span></td>
      <td class="small">${r.actual === undefined
        ? '<span class="faint small">（旧版跑分未记录实际输出）</span>'
        : r.actual.length
          ? r.actual.map((a) => `<span class="mono small">${esc(a.severity || "")} ${esc(a.file || "?")}:${esc(a.line ?? "")} ${esc(a.title)}</span>`).join("<br>")
          : '<span class="faint small">—（本轮没有报出任何发现）</span>'}</td>
      <td class="mono small muted">${esc(r.repo)}·${esc(r.sha)}</td>
    </tr>`).join("");
  const cfgLine = `提示词 <span class="mono">${esc((h.config || {}).prompt || "—")}</span> · ${esc(((h.config || {}).pipeline || []).join(" / ") || "—")}`;
  return `<details ${opts.id ? `id="${opts.id}"` : ""} ${opts.open ? "open" : ""}
      class="panel" style="padding:12px 18px;margin-bottom:10px">
    <summary style="cursor:pointer;display:flex;gap:12px;align-items:center;flex-wrap:wrap;list-style:none">
      <span class="mono small muted">${fmtTime(h.created_at)}</span>
      <span class="small">${stats}</span>${configChanges.join(" ")}
    </summary>
    ${rows ? `<table style="margin-top:10px"><thead><tr>
        <th style="width:48px">类型</th><th>期望</th><th style="width:220px">结果</th><th>本轮实际报出</th><th>仓库</th></tr></thead>
      <tbody>${rows}</tbody></table>`
      : `<div class="empty small" style="padding:14px">${h.status === "running" ? "逐用例结果实时写入…" : "无逐条结果"}</div>`}
    ${diffHtml}
    <div class="hint">当时的配置：${cfgLine}</div>
  </details>`;
}

function evalCasesHtml(cases) {
  if (!cases.length) {
    return `<div class="empty">还没有用例 —— 在报告页对已处置的 Important 点「存为评测用例」<br>
      <span class="small">正例 = 必须报出的问题；负例 = 不该再出现的误报</span></div>`;
  }
  return `<table><thead><tr><th style="width:64px">类型</th><th>期望</th><th>来源</th><th style="width:96px">最近结果</th><th style="width:64px"></th></tr></thead>
    <tbody>${cases.map((c) => `
      <tr>
        <td>${c.polarity === "positive" ? '<span class="badge confirmed">正例</span>' : '<span class="badge fp">负例</span>'}</td>
        <td class="small" style="max-width:320px">${esc(c.expected)}</td>
        <td>${c.run_id
          ? `<a class="mono small" href="#/runs/${c.run_id}">${esc(c.repo)} · ${esc(c.sha)}</a>`
          : `<span class="mono small muted">${esc(c.repo)} · ${esc(c.sha)}</span>`}</td>
        <td>${c.last_result
          ? `<span class="badge ${c.last_result === "hit" ? "confirmed" : "imp"}">${RESULT_LABEL[c.last_result] || esc(c.last_result)}</span>`
          : '<span class="faint small">—</span>'}</td>
        <td><button class="danger-ghost small-btn" data-eval-del="${c.id}">删除</button></td>
      </tr>`).join("")}</tbody></table>`;
}

function paintEval(d) {
  const cases = d.cases || [];
  const hist = d.history || [];
  const cfg = d.current_config || {};
  const running = hist.length && hist[0].status === "running";
  const pos = cases.filter((c) => c.polarity === "positive").length;
  $app.innerHTML = `
    ${pageHead({
      title: "评测",
      sub: "处置沉淀用例 → 改提示词/换模型前跑回归 → 命中不降再上线",
    })}
    <div class="panel stagger" style="padding:16px 20px;margin-bottom:22px">
      <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
        <span id="eval-count">用例 <b>${cases.length}</b>（正 ${pos} · 负 ${cases.length - pos}）</span>
        <span class="muted small mono" title="当前生效的提示词指纹与流水线；跑分会记录当时的快照用于对比">
          当前：${esc(cfg.prompt || "")} · ${esc((cfg.pipeline || []).join(" / ") || "")}</span>
        <button class="primary small-btn" id="eval-run" ${running ? "disabled" : ""}>${running ? "跑分进行中…" : "跑一轮评测"}</button>
      </div>
      <div class="hint" title="每条用例会检出对应提交、用当前配置重新评审、与期望比对（同文件 ±15 行 + 标题相似）。漏报=该抓的没抓到；误报重现=该忘了的又报了">正例必须报出、负例不许重现；每次跑分都记录配置快照，和上次对比一眼看出"是哪里改了导致的"</div>
    </div>

    <div class="sec-title stagger">跑分历史 <span class="faint small mono" style="letter-spacing:0;text-transform:none">点开看逐用例：期望 vs 本轮实际报出</span></div>
    ${hist.length
      ? hist.map((h, i) => evalRunHtml(h, hist[i + 1], i === 0 ? { id: "eval-latest", open: h.status === "running" } : {})).join("")
      : `<div class="panel empty">还没跑过 —— 存几条用例后跑第一轮，拿到基线</div>`}

    <div class="sec-title stagger">用例管理</div>
    <div class="panel stagger" id="eval-cases">${evalCasesHtml(cases)}</div>`;
  bindEvalActions();
}

function bindEvalActions() {
  const btn = $("eval-run");
  if (btn && !btn.disabled) {
    btn.onclick = async () => {
      try {
        const r = await api("/api/eval/run", { method: "POST" });
        if (!r.ok) { toast(r.msg, true); return; }
        toast("评测已开跑：逐用例用当前配置重新评审");
        pollEval();
      } catch (e) { toast(e.message, true); }
    };
  }
  document.querySelectorAll("[data-eval-del]").forEach((b) =>
    (b.onclick = async () => {
      try {
        await api(`/api/eval/cases/${b.dataset.evalDel}`, { method: "DELETE" });
        const d = await api("/api/eval/last");
        const pos = d.cases.filter((c) => c.polarity === "positive").length;
        $("eval-count").innerHTML = `用例 <b>${d.cases.length}</b>（正 ${pos} · 负 ${d.cases.length - pos}）`;
        $("eval-cases").innerHTML = evalCasesHtml(d.cases);
        bindEvalActions();
      } catch (e) { toast(e.message, true); }
    }));
}

/* 轮询只更新顶部按钮和进行中的那一条，不整页重画 */
async function refreshEvalLive() {
  let d;
  try { d = await api("/api/eval/last"); }
  catch (e) { return false; }
  const running = d.history.length && d.history[0].status === "running";
  if (location.hash === "#/workbench" && qualityData) {
    qualityData.eval = d;
    const status = $("qa-eval-status"), details = $("qa-details");
    if (status) status.innerHTML = qualityEvalHtml(qualityData);
    if (details && !details.hidden) details.innerHTML = qualityDetailsHtml(qualityData);
    bindQualityActions();
    document.querySelectorAll("[data-qa-toggle]").forEach((b) =>
      b.setAttribute("aria-expanded", String(!!details && !details.hidden)));
    return running;
  }
  const btn = $("eval-run");
  if (btn) { btn.disabled = running; btn.textContent = running ? "跑分进行中…" : "跑一轮评测"; }
  const latest = $("eval-latest");
  if (latest && d.history.length) {
    const wasOpen = latest.open;
    const tmp = document.createElement("div");
    tmp.innerHTML = evalRunHtml(d.history[0], d.history[1], { id: "eval-latest", open: wasOpen || running });
    latest.replaceWith(tmp.firstElementChild);
  }
  return running;
}

function pollEval() {
  autoRefresh(async () => {
    if (location.hash !== "#/workbench") return;
    const running = await refreshEvalLive();
    if (location.hash !== "#/workbench") return;
    if (!running) {
      autoRefresh(null, false);
      toast("评测完成：结果已写入跑分历史");
      renderWorkbench();
    }
  }, true);
}

function tabsHtml(active) {
  return `<div class="tabs">${SETTINGS_TABS.map(([t, label]) =>
    `<a href="#/settings/${t}" class="${t === active ? "on" : ""}">${label}</a>`).join("")}</div>`;
}

async function renderSettings(tab = "pipeline") {
  clearInterval(refreshTimer);
  if (!SETTINGS_TABS.some(([t]) => t === tab)) tab = "pipeline";
  setTitle("配置中心");
  showSkeleton();
  let s, cfg, pipe, prompt;
  try {
    [s, cfg, pipe, prompt] = await Promise.all([
      api("/api/settings"), api("/api/config"), api("/api/pipeline"), api("/api/prompt"),
    ]);
  } catch (e) { errorPanel(e.message); return; }

  if (tab === "pipeline") {
    nodeCredentials = await api("/api/model-credentials");
    $app.innerHTML = `
    ${pageHead({ title: "流水线编排", sub: "全局默认；项目可在自己的「流水线」页覆盖" })}
    ${tabsHtml(tab)}
    <div class="panel stagger" style="padding:12px 20px">
      <div id="pipe-list"></div>
      <div class="actions" style="margin-top:12px">
        <button id="pipe-add">+ 添加节点</button>
        <button class="ghost" id="pipe-reset">恢复默认</button>
        <button class="primary" id="pipe-save">保存全局默认</button>
      </div>
      <div class="hint">门禁失败直接打回；上下文节点供评审读取；评审组内部互不传递结果，最后统一裁决。</div>
    </div>
    <div class="sec-title stagger">修改历史 <span class="faint small mono" style="letter-spacing:0;text-transform:none">每次保存自动留档 · 可一键恢复</span></div>
    <div class="panel stagger" style="padding:4px 18px" id="hist-pipeline"></div>`;
    const draft = JSON.parse(JSON.stringify(pipe.steps));
    const list = $("pipe-list");
    const paint = () => { list.innerHTML = pipeListHTML(draft); bindPipeList(list, draft, paint); };
    paint();
    $("pipe-add").onclick = () => addPipeNode(draft, paint);
    $("pipe-reset").onclick = () => {
      draft.splice(0, draft.length,
        { id: "gate-test", kind: "gate", name: "测试门禁", gate: "tests", enabled: true },
        { id: "cross-review", kind: "review", name: "交叉评审", enabled: true, reviewers: [
          { id: "claude", kind: "reviewer", name: "Claude 评审", provider: "claude", enabled: true, model: "", prompt: "", purpose: "findings" },
          { id: "kimi", kind: "reviewer", name: "Kimi 评审", provider: "codex", enabled: true, model: "", prompt: "", purpose: "findings" },
        ] });
      paint();
    };
    $("pipe-save").onclick = async () => {
      try {
        await api("/api/pipeline", { method: "PUT", body: { steps: draft } });
        toast("全局默认编排已保存，下次评审生效");
        mountConfigHistory("hist-pipeline", "pipeline");
      } catch (e) { toast(e.message, true); }
    };
    mountConfigHistory("hist-pipeline", "pipeline");
    return;
  }

  if (tab === "engine") {
    $app.innerHTML = `
    ${pageHead({ title: "评审引擎", sub: "提示词与规则（评测与跑分在「评测」页）" })}
    ${tabsHtml(tab)}

    <div class="sec-title" style="margin-top:0">初始提示词</div>
    <div class="panel stagger" style="padding:18px 20px">
      <div class="ph-bar" id="ph-bar">
        <span class="ph-hint">占位符：</span>
        ${PLACEHOLDERS.map((ph) => `
          <span class="ph-chip" draggable="true" data-code="${esc(ph.code)}"
                title="${esc(ph.label)}${ph.required ? "（必需）" : ""}">${esc(ph.label)} <code>${esc(ph.code)}</code></span>`).join("")}
      </div>
      <div id="prompt-editor" class="profile-edit prompt-editor" contenteditable="true"></div>
      <div class="ph-status" id="ph-status"></div>
      <div class="actions" style="margin-top:10px">
        <button class="primary" id="prompt-save">保存提示词</button>
        <button class="ghost" id="prompt-reset">恢复默认</button>
      </div>
    </div>

    <div class="sec-title">评审规则</div>
    <div class="panel stagger" style="padding:18px 20px">
      <textarea id="rules-ta" class="profile-edit" rows="12">${esc(prompt.rules)}</textarea>
      <div class="hint" title="每次线上事故后加一条「已知陷阱」；反复误报的模式写进「一律不报」">把事故教训写成评审规则（REVIEW.md）</div>
      <div class="actions" style="margin-top:10px">
        <button class="primary" id="rules-save">保存规则</button>
      </div>
    </div>

    <div class="sec-title">修改历史 <span class="faint small mono" style="letter-spacing:0;text-transform:none">提示词与规则 · 每次保存自动留档</span></div>
    <div class="panel stagger" style="padding:4px 18px">
      <details style="padding:8px 0"><summary style="cursor:pointer;color:var(--muted);font-size:12px;user-select:none">初始提示词历史</summary><div id="hist-prompt"></div></details>
      <details style="padding:8px 0;border-top:1px solid var(--line)"><summary style="cursor:pointer;color:var(--muted);font-size:12px;user-select:none">评审规则历史</summary><div id="hist-rules"></div></details>
    </div>`;
    bindPromptEditor(prompt);
    $("rules-save").onclick = async () => {
      try {
        await api("/api/prompt", { method: "PUT", body: { rules: $("rules-ta").value } });
        toast("评审规则已保存");
        mountConfigHistory("hist-rules", "rules");
      } catch (e) { toast(e.message, true); }
    };
    mountConfigHistory("hist-prompt", "prompt_template");
    mountConfigHistory("hist-rules", "rules");
    return;
  }

  // 凭证与模型
  const v = cfg.values;
  let conns, modelCreds, providers, checks;
  try { [conns, modelCreds, providers, checks] = await Promise.all([
    api("/api/github-connections"), api("/api/model-credentials"), api("/api/model-providers"),
    api("/api/connectivity-checks")]); }
  catch (e) { errorPanel(e.message); return; }
  const checkMap = new Map(checks.map((c) => [c.target, c]));
  const modelByCredential = new Map();
  for (const step of pipe.steps || []) {
    for (const item of [step, ...(step.reviewers || [])]) {
      if (item.kind === "api" && item.credential_id && item.model && !modelByCredential.has(item.credential_id))
        modelByCredential.set(item.credential_id, item.model);
    }
  }
  $app.innerHTML = `
  ${pageHead({ title: "凭证与模型", sub: "存库即生效，无需重启" })}
  ${tabsHtml(tab)}
  <div class="sec-title">GitHub 连接 <span class="faint small">每个仓库显式绑定；token 不回显</span></div>
  <div class="panel" style="padding:18px 20px;margin-bottom:18px">
    ${conns.length ? `<table><thead><tr><th>连接</th><th>项目数</th><th>账号 / 最近验证</th><th>操作</th></tr></thead><tbody>
      ${conns.map((c) => `<tr><td>${esc(c.label)} <span class="mono small muted">#${c.id}</span></td>
        <td>${c.project_count}</td><td class="small">${c.disabled ? "停用 · " : ""}${c.last_error ? `<span class="del" title="${esc(c.last_error)}">验证失败</span>` : c.last_checked_at ? `${esc(c.account_login || "已验证")} · ${fmtTime(c.last_checked_at)}` : "尚未验证"}</td>
        <td class="actions"><button class="ghost small-btn" data-c-test="${c.id}">测试</button>
          <button class="ghost small-btn" data-c-rotate="${c.id}">更换 token</button>
          <button class="ghost small-btn" data-c-toggle="${c.id}" data-disabled="${c.disabled}">${c.disabled ? "启用" : "停用"}</button>
          <button class="danger-ghost small-btn" data-c-delete="${c.id}" ${c.project_count ? "disabled title='先解绑项目'" : ""}>删除</button></td></tr>`).join("")}</tbody></table>` : `<div class="hint">尚无连接。公开仓库可不绑定，私有仓库请先添加。</div>`}
    <div class="form-row" style="margin-top:14px">
      <div class="field"><label>连接名称</label><input id="c-new-label" placeholder="例如：团队 A / 服务账号"></div>
      <div class="field"><label>GitHub Token</label><input id="c-new-token" type="password" autocomplete="new-password" placeholder="仅在创建时输入"></div>
    </div><button class="primary small-btn" id="c-new-save">添加连接</button>
  </div>
  <div class="sec-title">模型 API 凭证 <span class="faint small">一个服务商可保存多组；节点按 ID 绑定</span></div>
  <div class="panel model-credentials" style="padding:18px 20px;margin-bottom:18px">
    ${modelCreds.length ? `<div class="model-credential-list">${modelCreds.map((c) => `<div class="model-credential-row">
      <div class="mc-identity"><strong>${esc(c.label)}</strong><small>${esc(providers.find((p) => p.id === c.provider)?.label || c.provider)} · ${esc(c.base_url)}${c.disabled ? " · 已停用" : ""}</small></div>
      <div class="mc-controls"><div class="actions"><button class="ghost small-btn" data-mc-rotate="${c.id}">更换 Key</button>
        <button class="ghost small-btn" data-mc-toggle="${c.id}" data-disabled="${c.disabled}">${c.disabled ? "启用" : "停用"}</button>
        <button class="danger-ghost small-btn" data-mc-delete="${c.id}">删除</button></div>
        <div class="check-line"><input aria-label="${esc(c.label)} 检测模型 ID" data-mc-model="${c.id}" maxlength="100"
          value="${esc(modelByCredential.get(c.id) || checkMap.get(`api:${c.id}`)?.model || "")}" placeholder="检测模型 ID">
          <button class="ghost small-btn" data-mc-check="${c.id}" ${c.disabled ? "disabled" : ""}>检测 API</button></div>
        <div class="check-status" data-check-status="api:${c.id}">${checkStatusHtml(checkMap.get(`api:${c.id}`))}</div></div></div>`).join("")}</div>`
      : '<p class="hint">还没有 API 凭证。添加后可在流水线中选择“模型 API”节点。</p>'}
    <div class="model-credential-add"><div class="form-row">
      <div class="field"><label>连接名称</label><input id="mc-label" placeholder="例如：团队 OpenAI / 预算 A"></div>
      <div class="field"><span class="field-label">服务商</span><div class="choice-grid provider-choice">${providers.map((p, i) => `<label class="choice-tile"><input type="radio" name="mc-provider" value="${esc(p.id)}" ${i ? "" : "checked"}><span>${esc(p.label)}</span></label>`).join("")}</div></div>
    </div><div class="form-row"><div class="field"><label>API Key</label><input type="password" id="mc-key" autocomplete="new-password" placeholder="保存后不再回显"></div>
      <div class="field"><label>Base URL</label><input id="mc-url" placeholder="HTTPS API Base URL"></div></div>
      <button class="primary small-btn" id="mc-save">添加模型凭证</button>
      <span class="hint">模型 ID 在每个节点的配置弹窗填写；请求失败会在审查报告记录状态。</span></div>
  </div>
  <div class="sec-title">Agent CLI 连通性 <span class="faint small">运行短任务，验证命令、认证与模型响应</span></div>
  <div class="agent-check-grid">
    ${[["claude", "Claude Code", s.claude_cli], ["codex", "Kimi / Codex CLI", s.codex_cli]].map(([key, label, installed]) => `
      <div class="panel agent-check-card"><div><strong>${label}</strong><span class="badge ${installed ? "confirmed" : "imp"}">${installed ? "命令存在" : "命令缺失"}</span></div>
        <div class="check-status" data-check-status="agent:${key}">${checkStatusHtml(checkMap.get(`agent:${key}`))}</div>
        <button class="ghost small-btn" data-agent-check="${key}" ${installed ? "" : "disabled"}>检测 Agent CLI</button></div>`).join("")}
  </div>
  <div class="sec-title">现有 Agent CLI 凭证与默认模型</div>
  <div class="panel stagger" style="padding:18px 20px">
    <div class="form-row">
      <div class="field"><label>Claude Token ${cfgBadge(v.claude_token_set)}</label>
        <input type="password" id="c-claude_token" placeholder="${esc(v.claude_token || "本机已登录 Claude Code，可留空")}"></div>
    </div>
    <div class="form-row">
      <div class="field"><label>Kimi API Key ${cfgBadge(v.kimi_api_key_set)}</label>
        <input type="password" id="c-kimi_api_key" placeholder="${esc(v.kimi_api_key || "未配置（Moonshot）")}"></div>
      <div class="field"><label>Kimi Base URL</label>
        <input id="c-kimi_base_url" value="${esc(v.kimi_base_url || "")}"></div>
    </div>
    <div class="form-row">
      <div class="field"><label>Claude 模型（留空 = 默认）</label>
        <input id="c-claude_model" value="${esc(v.claude_model || "")}" placeholder="例：sonnet / opus / claude-sonnet-4-5"></div>
      <div class="field"><label>Kimi 模型</label>
        <input id="c-kimi_model" value="${esc(v.kimi_model || "")}" placeholder="例：kimi-k2.7-code"></div>
    </div>
    <div class="actions">
      <button class="primary" id="cfg-save">保存</button>
      <div class="hint" style="margin:0" title="密钥输入框留空 = 不改动；模型留空 = 用各自默认">留空 = 不改动</div>
    </div>
  </div>
  <div class="sec-title">触发与 CLI</div>
  <div class="panel" style="padding:14px 20px">
    <div class="mono small" style="line-height:2">
      轮询每 ${s.poll_interval}s · webhook ${s.webhook_url ? "已配置" : "未配置"} ·
      claude ${s.claude_cli ? "✓" : "✗"} · codex ${s.codex_cli ? "✓" : "✗"} ·
      semgrep ${s.semgrep_cli ? "✓" : "✗（安全扫描门禁需要）"}
    </div>
    <div class="hint" title="手动触发：项目页「立即审查 HEAD」，或 Git 历史里对任意提交「审查此提交」；webhook 需要 PUBLIC_BASE_URL">手动触发入口在项目页与 Git 历史</div>
  </div>`;
  $("c-new-save").onclick = async () => {
    try {
      await api("/api/github-connections", {method: "POST", body: {label: $("c-new-label").value, token: $("c-new-token").value}});
      toast("连接已添加；请到项目页绑定"); renderSettings("creds");
    } catch (e) { toast(e.message, true); }
  };
  document.querySelectorAll("[data-c-test]").forEach((b) => b.onclick = async () => {
    try { const r = await api(`/api/github-connections/${b.dataset.cTest}/test`, {method: "POST"}); toast(`连接有效：${r.login}`); }
    catch (e) { toast(e.message, true); }
  });
  document.querySelectorAll("[data-c-rotate]").forEach((b) => b.onclick = async () => {
    const token = await uiPrompt("更换 GitHub Token",
      { message: "保存后原 token 立即失效。", type: "password" });
    if (!token) return;
    try { await api(`/api/github-connections/${b.dataset.cRotate}/token`, {method: "PUT", body: {token}}); toast("token 已更换"); renderSettings("creds"); }
    catch (e) { toast(e.message, true); }
  });
  document.querySelectorAll("[data-c-toggle]").forEach((b) => b.onclick = async () => {
    try { await api(`/api/github-connections/${b.dataset.cToggle}`, {method: "PATCH", body: {disabled: b.dataset.disabled !== "1"}}); renderSettings("creds"); }
    catch (e) { toast(e.message, true); }
  });
  document.querySelectorAll("[data-c-delete]").forEach((b) => b.onclick = async () => {
    if (!await uiConfirm("删除此连接？")) return;
    try { await api(`/api/github-connections/${b.dataset.cDelete}`, {method: "DELETE"}); renderSettings("creds"); }
    catch (e) { toast(e.message, true); }
  });
  const urlInput = $("mc-url");
  const selectedProvider = () => document.querySelector('input[name="mc-provider"]:checked')?.value || "openai";
  const syncProvider = () => { const provider = selectedProvider();
    urlInput.value = providers.find((p) => p.id === provider)?.base_url || "";
    urlInput.placeholder = provider === "custom" ? "https://your-api.example/v1" : "HTTPS API Base URL"; };
  document.querySelectorAll('input[name="mc-provider"]').forEach((input) => input.onchange = syncProvider);
  syncProvider();
  $("mc-save").onclick = async () => {
    try {
      await api("/api/model-credentials", { method: "POST", body: {
        label: $("mc-label").value.trim(), provider: selectedProvider(),
        key: $("mc-key").value, base_url: urlInput.value.trim() } });
      toast("模型凭证已保存；可在流水线的 API 节点中使用"); renderSettings("creds");
    } catch (e) { toast(e.message, true); }
  };
  document.querySelectorAll("[data-mc-rotate]").forEach((b) => b.onclick = async () => {
    const key = await uiPrompt("更换模型 API Key", { message: "保存后旧 Key 会立即从服务端替换。", type: "password" });
    if (!key) return;
    try { await api(`/api/model-credentials/${b.dataset.mcRotate}/key`, { method: "PUT", body: { key } });
      toast("API Key 已更换"); renderSettings("creds"); }
    catch (e) { toast(e.message, true); }
  });
  document.querySelectorAll("[data-mc-toggle]").forEach((b) => b.onclick = async () => {
    try { await api(`/api/model-credentials/${b.dataset.mcToggle}`, { method: "PATCH",
      body: { disabled: b.dataset.disabled !== "1" } }); renderSettings("creds"); }
    catch (e) { toast(e.message, true); }
  });
  document.querySelectorAll("[data-mc-delete]").forEach((b) => b.onclick = async () => {
    if (!await uiConfirm("删除这组模型凭证？已绑定它的流水线节点需先改绑。")) return;
    try { await api(`/api/model-credentials/${b.dataset.mcDelete}`, { method: "DELETE" });
      toast("模型凭证已删除"); renderSettings("creds"); }
    catch (e) { toast(e.message, true); }
  });
  const runCheck = async (button, target, path, body = null) => {
    const status = document.querySelector(`[data-check-status="${target}"]`);
    button.disabled = true;
    button.textContent = "检测中…";
    if (status) status.innerHTML = '<span class="badge mock">检测中</span> <span class="muted">最长等待 60 秒</span>';
    try {
      const result = await api(path, { method: "POST", ...(body ? { body } : {}) });
      if (status?.isConnected) status.innerHTML = checkStatusHtml(result);
    } catch (e) {
      if (status?.isConnected) status.innerHTML = `<span class="badge imp">请求失败</span> ${esc(e.message)}`;
    } finally {
      if (button.isConnected) { button.disabled = false; button.textContent = target.startsWith("api:") ? "检测 API" : "检测 Agent CLI"; }
    }
  };
  document.querySelectorAll("[data-mc-check]").forEach((b) => b.onclick = () => {
    const id = b.dataset.mcCheck;
    const model = document.querySelector(`[data-mc-model="${id}"]`)?.value.trim();
    if (!model) { toast("请先填写要检测的模型 ID", true); return; }
    runCheck(b, `api:${id}`, `/api/model-credentials/${id}/check`, { model });
  });
  document.querySelectorAll("[data-agent-check]").forEach((b) => b.onclick = () =>
    runCheck(b, `agent:${b.dataset.agentCheck}`, `/api/agents/${b.dataset.agentCheck}/check`));
  $("cfg-save").onclick = async () => {
    const values = {};
    for (const k of EDITABLE_KEYS) {
      const el = $(`c-${k}`);
      if (el && el.value.trim() !== "") values[k] = el.value.trim();
    }
    try {
      const r = await api("/api/config", { method: "PUT", body: { values } });
      toast(`已保存：${(r.changed || []).join("、") || "无改动"}`);
      renderSettings("creds");
    } catch (e) { toast(e.message, true); }
  };
}

/* 提示词富文本编辑器：占位符是可拖动的芯片，Ctrl+Z 走自管理撤销栈 */
function bindPromptEditor(prompt) {
  const ed = $("prompt-editor");
  const chipHTML = (code) =>
    `<span class="ph-chip inline" contenteditable="false" draggable="true" data-code="${esc(code)}">${esc(code)}</span>`;
  const renderEditor = (template) => {
    ed.innerHTML = esc(template).replace(/\{[a-z_]+\}/g, (m) => chipHTML(m));
  };
  const serialize = () => ed.innerText.replace(/\u00a0/g, " ");
  const validatePlaceholders = () => {
    const text = serialize();
    const missing = PLACEHOLDERS.filter((ph) => ph.required && !text.includes(ph.code));
    document.querySelectorAll("#ph-bar .ph-chip").forEach((chip) => {
      const ph = PLACEHOLDERS.find((p) => p.code === chip.dataset.code);
      chip.classList.toggle("miss", !!ph && ph.required && !text.includes(ph.code));
    });
    $("ph-status").innerHTML = missing.length
      ? `<span class="badge imp">缺少必需占位符：${missing.map((m) => esc(m.code)).join("、")} —— 点上方红色标签插回</span>`
      : `<span class="muted small">占位符完整 ✓</span>`;
    return missing.length === 0;
  };
  let undoStack = [], undoPtr = -1, undoTimer = null;
  const snapshot = () => {
    if (!ed.innerHTML) return;
    if (undoStack[undoPtr] && undoStack[undoPtr].html === ed.innerHTML) return;
    undoStack = undoStack.slice(0, undoPtr + 1);
    undoStack.push({ html: ed.innerHTML });
    if (undoStack.length > 50) { undoStack.shift(); }
    undoPtr = undoStack.length - 1;
  };
  const scheduleSnapshot = () => { clearTimeout(undoTimer); undoTimer = setTimeout(snapshot, 350); };
  const applyState = (st) => {
    ed.innerHTML = st.html;
    validatePlaceholders();
    ed.focus();
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(ed); range.collapse(false);
    sel.removeAllRanges(); sel.addRange(range);
  };
  const doUndo = () => { snapshot(); if (undoPtr > 0) { undoPtr--; applyState(undoStack[undoPtr]); } };
  const doRedo = () => { if (undoPtr < undoStack.length - 1) { undoPtr++; applyState(undoStack[undoPtr]); } };
  ed.addEventListener("keydown", (e) => {
    if (!(e.metaKey || e.ctrlKey)) return;
    const k = e.key.toLowerCase();
    if (k === "z" && !e.shiftKey) { e.preventDefault(); doUndo(); }
    else if ((k === "z" && e.shiftKey) || k === "y") { e.preventDefault(); doRedo(); }
  });
  const insertChip = (code) => {
    snapshot();
    ed.focus();
    document.execCommand("insertHTML", false, chipHTML(code) + "&nbsp;");
    snapshot();
    validatePlaceholders();
  };
  document.querySelectorAll("#ph-bar .ph-chip").forEach((chip) => {
    chip.addEventListener("click", () => insertChip(chip.dataset.code));
    chip.addEventListener("dragstart", (e) => e.dataTransfer.setData("text/plain", chip.dataset.code));
  });
  ed.addEventListener("dragover", (e) => e.preventDefault());
  ed.addEventListener("drop", (e) => {
    e.preventDefault();
    const code = e.dataTransfer.getData("text/plain");
    if (!code) return;
    snapshot();
    const range = document.caretRangeFromPoint
      ? document.caretRangeFromPoint(e.clientX, e.clientY) : null;
    if (range) {
      range.deleteContents();
      const tmp = document.createElement("template");
      tmp.innerHTML = chipHTML(code);
      range.insertNode(tmp.content.firstElementChild);
    } else insertChip(code);
    snapshot();
    validatePlaceholders();
  });
  ed.addEventListener("input", scheduleSnapshot);
  renderEditor(prompt.template);
  validatePlaceholders();
  $("prompt-save").onclick = async () => {
    if (!validatePlaceholders()) { toast("必需占位符缺失，点上方红色标签插回后再保存", true); return; }
    try {
      await api("/api/prompt", { method: "PUT", body: { template: serialize() } });
      toast("初始提示词已保存");
      const ev = await api("/api/eval/last").catch(() => null);
      if (ev && ev.cases && ev.cases.length) {
        toast(`评测集有 ${ev.cases.length} 条用例：到「评测」页跑一轮回归，命中不降再上线`);
      }
    } catch (e) { toast(e.message, true); }
  };
  $("prompt-reset").onclick = () => {
    api("/api/prompt").then((p) => {
      renderEditor(p.default_template);
      validatePlaceholders();
      toast("已填入默认提示词，点保存生效");
    });
  };
}

/* ---------------- 配置修改历史：每次保存自动留档，可一键恢复 ---------------- */

function configHistoryHtml(versions) {
  if (!versions.length) return `<div class="faint small" style="padding:8px 0">还没有修改历史</div>`;
  return `<table><tbody>${versions.map((v) => `
    <tr>
      <td class="mono small muted" style="width:112px">${fmtTime(v.created_at)}</td>
      <td class="small" style="max-width:540px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${esc(v.summary)}">${esc(v.summary)}</td>
      <td style="width:64px"><button class="ghost small-btn" data-restore="${v.id}">恢复</button></td>
    </tr>`).join("")}</tbody></table>`;
}

async function mountConfigHistory(containerId, key, onRestored) {
  const el = document.getElementById(containerId);
  if (!el) return;
  try {
    const d = await api(`/api/config/versions?key=${encodeURIComponent(key)}`);
    el.innerHTML = configHistoryHtml(d.versions);
    el.querySelectorAll("[data-restore]").forEach((b) =>
      (b.onclick = async () => {
        if (!await uiConfirm("恢复到这个版本？当前生效配置将被覆盖")) return;
        try {
          const r = await api(`/api/config/versions/${b.dataset.restore}/restore`, { method: "POST" });
          toast(`已恢复（${r.key === "rules" ? "评审规则" : r.key === "pipeline" ? "全局流水线"
            : r.key === "prompt_template" ? "初始提示词" : "项目编排"}）`);
          onRestored ? onRestored() : mountConfigHistory(containerId, key, onRestored);
        } catch (e) { toast(e.message, true); }
      }));
  } catch (e) {
    el.innerHTML = `<span class="faint small">${esc(e.message)}</span>`;
  }
}

/* ---------------- 项目级流水线编排（覆盖全局默认） ---------------- */

async function renderProjectPipeline(pid) {
  if (location.hash !== `#/projects/${pid}/pipeline`) return;
  setTitle("流水线编排");
  showSkeleton();
  let p, pp;
  try {
    [p, pp, nodeCredentials] = await Promise.all([
      api("/api/projects").then((l) => l.find((x) => x.id === pid)),
      api(`/api/projects/${pid}/pipeline`),
      api("/api/model-credentials"),
    ]);
  } catch (e) {
    if (location.hash === `#/projects/${pid}/pipeline`) errorPanel(e.message);
    return;
  }
  if (location.hash !== `#/projects/${pid}/pipeline`) return;
  if (!p) { location.hash = "#/projects"; return; }
  const draft = JSON.parse(JSON.stringify(pp.steps));
  $app.innerHTML = `
    ${pageHead({
      back: { label: `返回 ${p.name}`, hash: `#/projects/${pid}` },
      crumbs: `项目 / <a href="#/projects/${pid}">${esc(p.name)}</a> / 流水线编排`,
      title: "流水线编排",
      sub: pp.inherited ? "当前跟随全局默认；保存后此项目独立生效" : `独立编排 · ${pp.steps.length} 步（只影响此项目）`,
      actions: `<button class="primary" id="pp-save">保存为此项目的编排</button>
        ${pp.inherited ? "" : `<button class="ghost" id="pp-inherit">恢复跟随默认</button>`}`,
    })}
    <div class="panel stagger" style="padding:12px 20px">
      <div id="pp-list"></div>
      <div class="actions" style="margin-top:12px">
        <button id="pp-add">+ 添加节点</button>
      </div>
      <div class="hint">只影响此项目；门禁直接打回，上下文节点给交叉评审组提供输入，组内评审器互不读取结果。</div>
    </div>

    <div class="sec-title stagger">修改历史 <span class="faint small mono" style="letter-spacing:0;text-transform:none">此项目编排 · 每次保存自动留档</span></div>
    <div class="panel stagger" style="padding:4px 18px" id="hist-ppipeline"></div>`;

  const list = $("pp-list");
  const paint = () => { list.innerHTML = pipeListHTML(draft); bindPipeList(list, draft, paint); };
  paint();
  $("pp-add").onclick = () => addPipeNode(draft, paint);
  $("pp-save").onclick = async () => {
    try {
      await api(`/api/projects/${pid}/pipeline`, { method: "PUT", body: { steps: draft } });
      toast("已保存：此项目之后的审查按此编排执行");
      renderProjectPipeline(pid);
    } catch (e) { toast(e.message, true); }
  };
  mountConfigHistory("hist-ppipeline", `project_pipeline:${pid}`, () => renderProjectPipeline(pid));
  const inheritBtn = $("pp-inherit");
  if (inheritBtn) inheritBtn.onclick = async () => {
    if (!await uiConfirm("恢复后此项目将跟随全局默认编排（删除独立编排）？")) return;
    try {
      await api(`/api/projects/${pid}/pipeline`, { method: "DELETE" });
      toast("已恢复跟随全局默认");
      renderProjectPipeline(pid);
    } catch (e) { toast(e.message, true); }
  };
}

const EDITABLE_KEYS = ["claude_token", "kimi_api_key",
  "kimi_base_url", "claude_model", "kimi_model"];

const PLACEHOLDERS = [
  { code: "{diff_file}", label: "本次变更 diff", required: true },
  { code: "{review_md}", label: "评审规则", required: true },
  { code: "{profile_section}", label: "项目画像", required: false },
  { code: "{intent_section}", label: "声明目的", required: false },
  { code: "{repo_root}", label: "仓库路径", required: false },
  { code: "{truncated}", label: "完整性状态（旧模板兼容）", required: false },
];

const GATE_LABEL = { tests: "测试", build: "构建", semgrep: "安全扫描" };
const GATE_TIP = "机械门禁：失败即红灯阻断，不调用模型；仅仓库没有对应入口才跳过。执行工具缺失或安全扫描未完成都记失败";
const SANDBOX_TIP = "默认本机执行：不同项目使用各自的仓库目录，每条门禁命令使用独立 HOME、临时目录和工具缓存；超时清理该命令进程组。本机代码仍可读取服务账户有权读取的文件，请只接入可信仓库。旧配置可继续使用 Docker";

let nodeCredentials = [];
const NODE_KIND = { gate: "门禁", reviewer: "Agent", api: "模型 API", review: "交叉评审" };
const NODE_PURPOSE = { findings: "独立评审", context: "提供上游上下文" };
function newPipeNode(kind, index) {
  const base = { id: `node-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    kind, name: `${NODE_KIND[kind]}节点 ${index}`, enabled: true };
  if (kind === "gate") return { ...base, gate: "tests", sandbox: "off", network: "install", image: "" };
  if (kind === "api") return { ...base, credential_id: nodeCredentials.find((c) => !c.disabled)?.id || null,
    model: "", purpose: "context", prompt: "" };
  return { ...base, provider: "claude", model: "", purpose: "context", prompt: "" };
}
function pipeListHTML(draft) {
  return `<div class="node-flow-intro">门禁决定是否继续 → 可选的 LLM 上下文 → 交叉评审独立判断</div>
    ${draft.map((s, i) => {
      if (s.kind === "review") return `<div class="node-flow-item"><div class="review-stage">
        <div class="review-stage-head"><div><span class="node-kind review">交叉评审</span><strong>${esc(s.name || "交叉评审")}</strong>
          <small>评审器并行读取同一份上游上下文；超出并发上限时按排列分批，不传递彼此的结论</small></div>
          <button class="ghost small-btn" data-member-add>+ 添加评审器</button></div>
        <span class="review-scroll-hint">左右滑动查看评审器 →</span><div class="review-members">${(s.reviewers || []).map((m, j) => {
          const cred = nodeCredentials.find((c) => c.id === m.credential_id);
          const detail = m.kind === "api" ? `${cred ? esc(cred.label) : "未绑定凭证"} · ${esc(m.model || "未填模型")}`
            : `${m.provider === "codex" ? "Kimi / Codex" : "Claude Code"} · ${esc(m.model || "默认模型")}`;
          return `<div class="review-member ${m.enabled ? "" : "off"}"><div class="review-member-top"><span class="node-kind ${m.kind}">${NODE_KIND[m.kind]}</span><span class="mono small muted">${j + 1}</span></div>
            <strong>${esc(m.name || "未命名评审器")}</strong><small>${detail}</small>
            <div class="review-member-actions"><label title="启用 / 停用"><input type="checkbox" data-member-en="${j}" ${m.enabled ? "checked" : ""}></label>
              <button class="ghost small-btn" data-member-left="${j}" ${j ? "" : "disabled"} title="前移排列">←</button>
              <button class="ghost small-btn" data-member-right="${j}" ${j < s.reviewers.length - 1 ? "" : "disabled"} title="后移排列">→</button>
              <button class="ghost small-btn" data-member-edit="${j}">配置</button>
              <button class="danger-ghost small-btn" data-member-del="${j}">删除</button></div></div>`;
        }).join("")}</div></div></div>`;
      const cred = nodeCredentials.find((c) => c.id === s.credential_id);
      const detail = s.kind === "gate" ? `${GATE_LABEL[s.gate] || s.gate} · ${s.sandbox === "docker" ? "Docker 沙盒" : "本机独立环境"}`
        : s.kind === "api" ? `${cred ? esc(cred.label) : "未绑定凭证"} · ${esc(s.model || "未填模型")}`
        : `${s.provider === "codex" ? "Kimi / Codex" : "Claude Code"} · ${esc(s.model || "默认模型")}`;
      return `<div class="node-flow-item"><div class="pipe-step ${s.enabled ? "" : "off"}" data-idx="${i}">
        <div class="node-order"><span class="pipe-handle" draggable="true" data-drag="${i}" title="拖动调整顺序">⠿</span><b>${i + 1}</b></div>
        <div class="node-body"><div class="node-title"><span class="node-kind ${s.kind}">${NODE_KIND[s.kind] || esc(s.kind)}</span>
          <strong>${esc(s.name || "未命名节点")}</strong>${s.kind !== "gate" ? `<span class="node-purpose">${NODE_PURPOSE.context}</span>` : ""}</div>
          <div class="node-description">${detail}</div></div>
        <div class="node-actions"><label title="启用 / 停用"><input type="checkbox" data-en="${i}" ${s.enabled ? "checked" : ""}></label>
          <button class="ghost small-btn" data-up="${i}" ${i ? "" : "disabled"} title="上移">↑</button>
          <button class="ghost small-btn" data-down="${i}" ${i < draft.length - 2 ? "" : "disabled"} title="下移">↓</button>
          <button class="ghost small-btn" data-edit="${i}">配置</button>
          <button class="danger-ghost small-btn" data-del="${i}" title="从草稿删除节点">删除</button></div>
      </div>${i < draft.length - 1 ? `<div class="node-connector"><span>${s.kind === "gate" ? "通过后继续 ↓" : "上游 LLM 上下文 ↓"}</span></div>` : ""}</div>`;
    }).join("")}`;
}
function nodeField(label, input, hint = "") {
  return `<label class="node-field"><span>${label}</span>${input}${hint ? `<small>${hint}</small>` : ""}</label>`;
}
function nodeChoiceField(label, name, items, selected, hint = "") {
  return `<div class="node-field"><span>${label}</span><div class="choice-grid">${items.map(([value, text]) =>
    `<label class="choice-tile"><input type="radio" name="${esc(name)}" value="${esc(value)}" ${String(value) === String(selected) ? "checked" : ""} required><span>${esc(text)}</span></label>`).join("")}</div>${hint ? `<small>${hint}</small>` : ""}</div>`;
}
function openNodeEditor(draft, index, repaint, onCancel = null) {
  const s = { ...draft[index] };
  const dialog = document.createElement("dialog");
  dialog.className = "rh-dialog node-dialog";
  const common = nodeField("节点名称", `<input name="name" value="${esc(s.name || "")}" maxlength="40" required>`);
  let specific = "";
  if (s.kind === "gate") {
    specific = nodeChoiceField("门禁任务", "gate", Object.entries(GATE_LABEL), s.gate, GATE_TIP)
      + (s.sandbox === "docker"
        ? nodeChoiceField("执行环境", "sandbox", [["off", "本机独立环境"], ["docker", "Docker（旧配置）"]], "docker", SANDBOX_TIP)
        : nodeField("执行环境", '<div class="muted">本机独立环境</div><input type="hidden" name="sandbox" value="off">', SANDBOX_TIP))
      + `<div data-docker-options ${s.sandbox === "docker" ? "" : "hidden"}>`
      + nodeChoiceField("Docker 网络", "network", [["install", "仅安装依赖时联网"], ["off", "完全断网"], ["on", "全程联网"]], s.network || "install")
      + nodeField("Docker 镜像", `<input name="image" value="${esc(s.image || "")}" maxlength="80" placeholder="留空按仓库自动选择">`) + `</div>`;
  } else {
    specific = (s.kind === "api"
        ? nodeChoiceField("模型凭证", "credential_id", nodeCredentials.filter((c) => !c.disabled).map((c) => [c.id, `${c.label} · ${c.provider}`]), s.credential_id,
          '在“凭证与模型”里可添加 OpenAI、Anthropic、Gemini、DeepSeek 等连接。')
        : nodeChoiceField("Agent 执行器", "provider", [["claude", "Claude Code"], ["codex", "Kimi / Codex"]], s.provider || "claude"))
      + nodeField("模型 ID", `<input name="model" value="${esc(s.model || "")}" maxlength="80" ${s.kind === "api" ? "required" : ""} placeholder="${s.kind === "api" ? "填写服务商模型 ID" : "留空使用默认模型"}">`)
      + nodeField(s.purpose === "findings" ? "独立评审任务" : "上下文整理任务", `<textarea name="prompt" rows="7" maxlength="8000" placeholder="${s.purpose === "findings" ? "说明独立评审要检查的重点；不会看到其他评审器结果" : "说明要从仓库与 diff 中提取什么信息，供评审组读取"}">${esc(s.prompt || "")}</textarea>`);
  }
  dialog.innerHTML = `<form method="dialog" class="rh-dialog-inner"><div class="node-dialog-head"><div><span class="node-kind ${s.kind}">${NODE_KIND[s.kind]}</span>
      <h2>配置节点 ${index + 1}</h2></div><button type="button" class="ghost" data-close aria-label="关闭">×</button></div>
      ${common}${specific}<div class="rh-dialog-actions"><button type="button" class="ghost" data-close>取消</button>
      <button type="submit" class="primary" ${s.kind === "api" && !nodeCredentials.some((c) => !c.disabled) ? "disabled" : ""}>应用到草稿</button></div></form>`;
  document.body.appendChild(dialog);
  if (s.kind === "gate") dialog.querySelectorAll('input[name="sandbox"]').forEach((radio) => {
    radio.onchange = () => { dialog.querySelector("[data-docker-options]").hidden = radio.value !== "docker"; };
  });
  const close = (cancelled = false) => { dialog.close(); dialog.remove(); if (cancelled) onCancel?.(); };
  dialog.querySelectorAll("[data-close]").forEach((b) => b.onclick = () => close(true));
  dialog.addEventListener("cancel", (e) => { e.preventDefault(); close(true); });
  dialog.querySelector("form").onsubmit = (e) => {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    s.name = String(data.get("name") || "").trim();
    if (!s.name) return;
    if (s.kind === "gate") {
      s.gate = data.get("gate"); s.sandbox = data.get("sandbox");
      s.network = data.get("network"); s.image = String(data.get("image") || "").trim();
    } else {
      s.model = String(data.get("model") || "").trim();
      s.prompt = String(data.get("prompt") || "").trim();
      if (s.kind === "api") {
        const cid = data.get("credential_id");
        if (!cid || !nodeCredentials.some((c) => !c.disabled && String(c.id) === cid)) {
          toast("请先选择可用的模型 API 凭证", true); return;
        }
        s.credential_id = Number(cid);
      }
      else s.provider = data.get("provider");
    }
    draft[index] = s;
    close(); repaint();
  };
  dialog.showModal();
  dialog.querySelector('[name="name"]').focus();
}
async function addPipeNode(draft, repaint) {
  const kind = await uiChoice("添加流水线节点", [
    { value: "gate", label: "门禁 · 执行测试、构建或安全扫描" },
    { value: "reviewer", label: "Agent 上下文 · 读取仓库并提取信息" },
    { value: "api", label: "模型 API 上下文 · 通过凭证提取信息" },
  ], "上游上下文会提供给交叉评审组；门禁只负责拦截。");
  if (!kind) return;
  const node = newPipeNode(kind, draft.length);
  draft.splice(draft.length - 1, 0, node);
  repaint();
  openNodeEditor(draft, draft.length - 2, repaint, () => {
    const i = draft.findIndex((s) => s.id === node.id);
    if (i >= 0) draft.splice(i, 1);
    repaint();
  });
}
async function addReviewMember(group, repaint) {
  const kind = await uiChoice("添加独立评审器", [
    { value: "reviewer", label: "Agent · 能读取仓库源码" },
    { value: "api", label: "模型 API · 根据内联 diff 评审" },
  ], "所有评审器读取同一份上游上下文，不会看到彼此的判断。");
  if (!kind) return;
  const member = { ...newPipeNode(kind, group.reviewers.length + 1), purpose: "findings" };
  group.reviewers.push(member);
  repaint();
  openNodeEditor(group.reviewers, group.reviewers.length - 1, repaint, () => {
    const i = group.reviewers.findIndex((m) => m.id === member.id);
    if (i >= 0) group.reviewers.splice(i, 1);
    repaint();
  });
}
function bindPipeList(root, draft, repaint) {
  const move = (from, to) => {
    if (to < 0 || to >= draft.length - 1 || from === to) return;
    const [node] = draft.splice(from, 1);
    draft.splice(to, 0, node);
    repaint();
  };
  root.querySelectorAll("[data-up]").forEach((b) => b.onclick = () => move(+b.dataset.up, +b.dataset.up - 1));
  root.querySelectorAll("[data-down]").forEach((b) => b.onclick = () => move(+b.dataset.down, +b.dataset.down + 1));
  root.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => openNodeEditor(draft, +b.dataset.edit, repaint));
  root.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => { draft.splice(+b.dataset.del, 1); repaint(); });
  root.querySelectorAll("[data-en]").forEach((c) => c.onchange = () => {
    draft[+c.dataset.en].enabled = c.checked;
    c.closest(".pipe-step").classList.toggle("off", !c.checked);
  });
  const group = draft.find((s) => s.kind === "review");
  if (group) {
    root.querySelector("[data-member-add]").onclick = () => addReviewMember(group, repaint);
    const members = group.reviewers;
    const moveMember = (from, to) => {
      if (to < 0 || to >= members.length) return;
      const [m] = members.splice(from, 1); members.splice(to, 0, m); repaint();
    };
    root.querySelectorAll("[data-member-left]").forEach((b) => b.onclick = () => moveMember(+b.dataset.memberLeft, +b.dataset.memberLeft - 1));
    root.querySelectorAll("[data-member-right]").forEach((b) => b.onclick = () => moveMember(+b.dataset.memberRight, +b.dataset.memberRight + 1));
    root.querySelectorAll("[data-member-edit]").forEach((b) => b.onclick = () => openNodeEditor(members, +b.dataset.memberEdit, repaint));
    root.querySelectorAll("[data-member-del]").forEach((b) => b.onclick = () => { members.splice(+b.dataset.memberDel, 1); repaint(); });
    root.querySelectorAll("[data-member-en]").forEach((c) => c.onchange = () => {
      members[+c.dataset.memberEn].enabled = c.checked; c.closest(".review-member").classList.toggle("off", !c.checked);
    });
  }
  let dragIndex = null;
  root.querySelectorAll("[data-drag]").forEach((h) => {
    h.ondragstart = (e) => { dragIndex = +h.dataset.drag; e.dataTransfer.setData("text/plain", String(dragIndex)); };
  });
  root.querySelectorAll(".pipe-step").forEach((el) => {
    el.ondragover = (e) => { e.preventDefault(); el.classList.add("drag-over"); };
    el.ondragleave = () => el.classList.remove("drag-over");
    el.ondrop = (e) => { e.preventDefault(); el.classList.remove("drag-over");
      if (dragIndex != null) move(dragIndex, +el.dataset.idx); dragIndex = null; };
  });
}

navigate();
