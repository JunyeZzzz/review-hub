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
    await api(`/api/runs/${runId}/findings/${idx}/mark`, { method: "POST", body: { disposition: next } });
    toast(next === "false_positive" ? "已标记误报（计入误报率）"
      : next === "confirmed" ? "已标记确认" : "已撤销标记");
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
    toast(r.existed ? "这条已经在工作台的评测集里"
      : `已存为评测用例（${r.polarity === "positive" ? "正例：应报出" : "负例：不应再报出"}）`);
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
      <div class="panel stat clickable" data-jump="#/runs?reverted=1" title="看触发回滚的审查">
        <div class="num" style="color:var(--warn)">${s.reverted_total}</div><div class="label">触发回滚</div></div>
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
  if (!runs.length) return `<div class="empty">还没有审查记录 —— 到「项目」里接入仓库，新 push / PR 会自动审查</div>`;
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
        <div class="label">本周回滚（率 ${cur ? cur.revert_rate : 0}%）</div></div>
      <div class="panel stat clickable" data-jump="#/runs?trigger=pr">
        <div class="num">${cur ? cur.pr_runs : 0}</div><div class="label">本周 PR 审查</div></div>
      <div class="panel stat clickable" data-jump="#/marks">
        <div class="num">${cur && cur.marked ? cur.fp_rate + "%" : "—"}</div>
        <div class="label">误报率（处置 ${cur ? cur.marked : 0} 条）</div></div>
    </div>

    <div class="sec-title stagger">周趋势 <span class="faint small mono" style="letter-spacing:0;text-transform:none">点任意数字下钻</span></div>
    <div class="panel stagger">${weeks.length ? `<table><thead><tr>
        <th>周</th><th>审查</th><th>发现问题</th><th>回滚</th><th>回滚率</th><th>PR 审查</th><th>处置 / 误报</th></tr></thead>
      <tbody>${weeks.map((w) => `
        <tr>
          <td class="mono small muted">${esc(w.start || w.week)}</td>
          <td><div class="mrow">${cell(w.runs, `#/runs?since=${esc(w.start || "")}`)}
            <span class="mbar" style="width:${Math.round(w.runs / maxRuns * 60)}px" title="${w.runs} 次审查"></span></div></td>
          <td>${w.blocked ? `<a href="#/runs?since=${esc(w.start || "")}&status=blocked"
            style="display:block;color:inherit"><span class="badge imp">${w.blocked}</span></a>` : '<span class="faint">—</span>'}</td>
          <td>${cell(w.reverted ? `${w.reverted}` : "—", `#/runs?since=${esc(w.start || "")}&reverted=1`)}</td>
          <td class="mono small ${w.revert_rate > 30 ? "del" : "muted"}">${w.reverted ? w.revert_rate + "%" : "—"}</td>
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
  const sel = (id, val, opts, label) => `
    <select id="${id}" style="width:auto">
      <option value="">${label}</option>
      ${Object.entries(opts).map(([v, l]) =>
        `<option value="${v}" ${val === v ? "selected" : ""}>${l}</option>`).join("")}
    </select>`;
  const titleBits = [status && STATUS_LABEL[status], trigger && TRIGGER_LABEL[trigger],
    reverted && "已回滚", since && `自 ${since}`].filter(Boolean).join(" · ");
  $app.innerHTML = `
    ${pageHead({
      title: `审查${titleBits ? ` <span class="muted" style="font-size:14px;font-weight:400">· ${esc(titleBits)}</span>` : ""}`,
      sub: `${runs.length} 条记录 · 点行看报告`,
    })}
    <div class="actions" style="margin-bottom:12px">
      ${sel("rf-status", status, STATUS_LABEL, "全部状态")}
      ${sel("rf-trigger", trigger, TRIGGER_LABEL, "全部触发")}
      <label class="small" style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="rf-reverted" ${reverted ? "checked" : ""} style="width:auto"> 仅看回滚</label>
      <input type="date" id="rf-since" value="${esc(since)}" style="width:auto" title="起始日期">
      <button class="ghost small-btn" id="rf-clear">清除筛选</button>
    </div>
    <div class="panel">${runTable(runs, true)}</div>`;
  const reroute = () => {
    const q = new URLSearchParams();
    const st = $("rf-status").value, tr = $("rf-trigger").value;
    if (st) q.set("status", st);
    if (tr) q.set("trigger", tr);
    if ($("rf-reverted").checked) q.set("reverted", "1");
    if ($("rf-since").value) q.set("since", $("rf-since").value);
    location.hash = `#/runs${q.toString() ? "?" + q.toString() : ""}`;
  };
  $("rf-status").onchange = reroute;
  $("rf-trigger").onchange = reroute;
  $("rf-reverted").onchange = reroute;
  $("rf-since").onchange = reroute;
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
               title="写进评审规则的「一律不报」，并自动存为负例评测用例">回流规则</button>` : ""}</td>
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
  document.querySelectorAll("[data-fb-run]").forEach((b) =>
    (b.onclick = () => feedbackRule(b.dataset.fbRun, b.dataset.fbIdx, b.dataset.fbTitle, b.dataset.fbFile)));
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

async function feedbackRule(runId, idx, title, file) {
  if (!confirm(`把这条误报写进评审规则的「一律不报」清单？\n\n${title}`)) return;
  try {
    const p = await api("/api/prompt");
    let rules = p.rules || "";
    if (rules.includes(title)) { toast("这条模式已在规则里"); }
    else {
      const line = `- ${title}（样例 ${file || "无文件信息"} · run#${runId}，已确认误报）`;
      const anchor = "## 一律不报";
      rules = rules.includes(anchor) ? rules.replace(anchor, anchor + "\n" + line)
                                     : rules + `\n${anchor}\n${line}\n`;
      await api("/api/prompt", { method: "PUT", body: { rules } });
    }
    // 回流的误报自动存为负例评测用例——规则改动有没有压住它,下轮跑分见
    try {
      const r = await api("/api/eval/cases", { method: "POST", body: { run_id: +runId, idx: +idx } });
      toast(r.existed ? "已回流规则（用例已在评测集）"
        : "已回流规则，并自动存为负例评测用例——到工作台跑分验证");
    } catch (e) {
      toast(`规则已回流；自动存评测用例未成功：${e.message}`, true);
    }
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

/* 工作台：队列决定阅读顺序；同屏显示发现、证据和处置。 */
const wbState = { projects: [], runs: [], marks: null, metrics: null, eval: null,
  run: null, finding: 0, diff: new Map() };

function wbMatches(item) {
  if (marksFilter.project_id && item.project_id !== marksFilter.project_id) return false;
  return !workbenchSearch || `${item.project || ""} ${item.title || ""} ${item.file || ""}
    ${item.pr_number || ""} ${item.head_sha || ""} ${item.run_id || item.id || ""}`
    .toLowerCase().includes(workbenchSearch);
}

function wbPending() { return (wbState.marks?.pending || []).filter(wbMatches); }
function wbRuns() {
  return wbState.runs.filter((r) => wbMatches(r) && (!workbenchPrOnly || r.pr_number));
}
function wbQueueHtml() {
  const pending = wbPending(), runs = wbRuns();
  return `<div class="wb-queue-head"><b>待处理发现 <span class="wb-count">${pending.length}</span></b>
      <span class="small muted">先处理 Important</span></div>
    <div class="wb-pending-list">${pending.length ? pending.map((f) => `
      <button class="wb-queue-item ${wbState.run?.id === f.run_id && wbState.finding === f.idx ? "active" : ""}"
        data-wb-run="${f.run_id}" data-wb-idx="${f.idx}">
        <span class="wb-item-top"><span class="badge imp">Important</span><span class="muted small">${fmtAgo(f.created_at)}</span></span>
        <strong>${esc(f.title)}</strong>
        <span class="mono muted">${esc(f.project)} · ${esc(f.file || "门禁")}${f.line ? ":" + esc(f.line) : ""}</span>
      </button>`).join("") : '<div class="wb-queue-empty">当前没有待处理 Important</div>'}</div>
    <div class="wb-queue-head wb-recent-head"><b>近期审查</b><a href="#/runs" class="small">全部审查 →</a></div>
    <label class="wb-toggle"><input id="wb-pr-only" type="checkbox" ${workbenchPrOnly ? "checked" : ""}> 只看 PR</label>
    <div class="wb-run-list">${runs.length ? runs.map((r) => `
      <button class="wb-run-item ${wbState.run?.id === r.id ? "active" : ""}" data-wb-run="${r.id}">
        <span class="wb-run-main">${statusHtml(r.status)}<strong>${esc(r.project)} · ${r.pr_number ? "PR #" + r.pr_number : esc((r.head_sha || r.ref || "").slice(0, 8))}</strong></span>
        <span class="wb-run-meta">${r.important_count ? '<span class="badge imp">' + r.important_count + ' Important</span>' : ""}
          <span>${fmtAgo(r.created_at)}</span></span>
      </button>`).join("") : '<div class="wb-queue-empty">当前筛选下没有审查</div>'}</div>`;
}

function wbFindingListHtml(run) {
  const items = run.findings || [];
  if (!items.length) return `<div class="wb-empty-review"><b>${run.status === "passed" ? "没有阻断发现" : "尚无发现"}</b>
    <span>${run.status === "running" ? "审查完成后这里会更新" : "可打开完整报告检查执行过程"}</span></div>`;
  return items.map((f, i) => `<button class="wb-finding-item ${wbState.finding === i ? "active" : ""}"
      data-wb-finding="${i}">
      <span class="wb-item-top"><span class="badge ${f.severity === "IMPORTANT" ? "imp" : ""}">${esc(f.severity || "发现")}</span>
        ${f.disposition ? '<span class="badge ' + (f.disposition === "confirmed" ? "confirmed" : "fp") + '">' + (f.disposition === "confirmed" ? "已确认" : "误报") + '</span>' : ""}</span>
      <strong>${esc(f.title)}</strong>
      <span class="mono muted">${esc(f.file || "门禁")}${f.line ? ":" + esc(f.line) : ""}</span>
    </button>`).join("");
}

function wbEvidenceHtml(run, f, idx) {
  if (!f) return `<div class="wb-empty-review">选择左侧发现，查看触发条件、代码和建议</div>`;
  const important = f.severity === "IMPORTANT";
  return `<div class="wb-evidence-head">
      <div><span class="badge ${important ? "imp" : ""}">${esc(f.severity || "发现")}</span>
        <span class="small muted">${esc((f.sources || []).join(" + "))}</span></div>
      <button class="ghost small-btn" data-copy-run="${run.id}" data-copy-idx="${idx}">复制问题</button>
    </div>
    <h2>${esc(f.title)}</h2>
    <div class="wb-file-line">${fileLink(run, f.file, f.line)}</div>
    <section class="wb-evidence-section"><div class="ctx-label">触发条件 / 影响</div><p>${esc(f.condition || "未提供触发条件")}</p></section>
    <div class="wb-evidence-actions">
      ${important ? `<button class="${f.disposition === "confirmed" ? "primary" : "ghost"} small-btn"
        data-mark="confirmed" data-run="${run.id}" data-idx="${idx}" data-cur="${esc(f.disposition || "")}">${f.disposition === "confirmed" ? "已确认 ✓" : "确认问题"}</button>
      <button class="${f.disposition === "false_positive" ? "primary" : "ghost"} small-btn"
        data-mark="false_positive" data-run="${run.id}" data-idx="${idx}" data-cur="${esc(f.disposition || "")}">${f.disposition === "false_positive" ? "已标误报 ✓" : "标记误报"}</button>
      ${f.disposition ? `<button class="ghost small-btn" data-eval-run="${run.id}" data-eval-idx="${idx}">存为评测用例</button>` : ""}` : ""}
      <a href="#/runs/${run.id}" class="wb-report-link">完整报告 →</a>
    </div>
    <section class="wb-evidence-section"><div class="ctx-label">建议修复</div><p>${esc(f.fix || "未提供建议")}</p></section>
    <section class="wb-evidence-section"><div class="ctx-label">对应代码</div>
      <div id="wb-code" class="wb-code">${run.has_diff && f.file ? "正在定位 diff…" : "本次审查没有保存可定位的 diff"}</div></section>`;
}

function wbDiffExcerpt(diff, file, line) {
  const src = String(diff || "").split("\n"), rows = [];
  let active = false, current = 0;
  for (const raw of src) {
    if (raw.startsWith("diff --git ")) { active = raw.endsWith(" b/" + file); continue; }
    if (!active) continue;
    if (raw.startsWith("@@")) {
      const m = raw.match(/\+(\d+)/);
      current = m ? +m[1] : 0;
      continue;
    }
    if (!current || raw.startsWith("---") || raw.startsWith("+++")) continue;
    const number = current;
    if (!raw.startsWith("-")) current++;
    if (Math.abs(number - line) <= 4 && !raw.startsWith("\\")) rows.push({ number, raw });
  }
  return rows.length ? `<div class="wb-code-lines">${rows.map(({number, raw}) => `
    <div class="wb-code-row ${raw.startsWith("+") ? "add" : raw.startsWith("-") ? "del" : ""} ${number === +line ? "focus" : ""}">
      <span>${raw.startsWith("-") ? "" : number}</span><code>${esc(raw)}</code></div>`).join("")}</div>`
    : '<span class="muted">未在保存的 diff 中找到该行；可从文件链接查看完整代码。</span>';
}

async function wbLoadDiff() {
  const run = wbState.run, f = run?.findings?.[wbState.finding], box = $("wb-code");
  if (!run || !f?.file || !run.has_diff || !box) return;
  const selected = wbState.finding;
  try {
    if (!wbState.diff.has(run.id)) wbState.diff.set(run.id, (await api(`/api/runs/${run.id}/diff`)).diff || "");
    if (wbState.run?.id === run.id && wbState.finding === selected && $("wb-code")) {
      $("wb-code").innerHTML = wbDiffExcerpt(wbState.diff.get(run.id), f.file, +f.line);
    }
  } catch (e) { if ($("wb-code")) $("wb-code").textContent = "代码加载失败：" + e.message; }
}

function wbReviewHtml() {
  const run = wbState.run;
  if (!run) return '<div class="wb-empty-review">暂无审查记录。接入项目后，审查会显示在这里。</div>';
  const f = (run.findings || [])[wbState.finding];
  return `<div class="wb-review-head">
      <div><span class="wb-eyebrow">当前审查 · RUN #${run.id}</span>
        <h2>${esc(run.project)} ${run.pr_number ? "· PR #" + run.pr_number : "· " + esc((run.head_sha || "").slice(0, 8))}</h2>
        <div class="small muted">${esc(run.repo || run.branch || "")} · ${fmtTime(run.created_at)}
          ${run.files_changed != null ? " · " + run.files_changed + " 文件" : ""}</div></div>
      <div class="wb-review-status">${statusHtml(run.status)}<a href="#/runs/${run.id}">完整报告 ↗</a></div>
    </div>
    <div class="wb-review-split">
      <div class="wb-findings"><div class="wb-pane-label">审查发现 <span>${(run.findings || []).length}</span></div>
        <div class="wb-findings-list">${wbFindingListHtml(run)}</div></div>
      <div class="wb-evidence" id="wb-evidence">${wbEvidenceHtml(run, f, wbState.finding)}</div>
    </div>`;
}

function wbAnalysisHtml() {
  const marks = (wbState.marks?.marks || []).filter(wbMatches);
  const s = { total: marks.length,
    confirmed: marks.filter((m) => m.disposition === "confirmed").length,
    false_positive: marks.filter((m) => m.disposition === "false_positive").length };
  const hist = wbState.eval?.history || [], cases = wbState.eval?.cases || [];
  const latest = hist[0], weeks = wbState.metrics?.weeks || [];
  const maxRuns = Math.max(1, ...weeks.map((w) => +w.runs || 0));
  return `<section class="wb-analysis">
    <div class="wb-analysis-title"><div><span class="wb-eyebrow">质量闭环</span><h2>处置 · 评测 · 趋势</h2></div>
      <span class="small muted">处置按筛选展示；评测与趋势为全局</span></div>
    <div class="wb-analysis-grid">
      <div class="panel wb-analysis-card"><div class="wb-card-head"><h3>处置记录</h3><span class="small muted">已处置 ${s.total || 0}</span></div>
        <div class="wb-mini-stats"><span>确认 <b>${s.confirmed || 0}</b></span><span>误报 <b>${s.false_positive || 0}</b></span></div>
        <div class="wb-ledger">${marks.length ? marks.slice(0, 5).map((m) => `
          <div class="wb-ledger-row"><span class="badge ${m.disposition === "confirmed" ? "confirmed" : "fp"}">${m.disposition === "confirmed" ? "确认" : "误报"}</span>
            <a href="#/runs/${m.run_id}" title="${esc(m.title)}">${esc(m.title)}</a>
            ${m.disposition === "false_positive" ? `<button class="ghost small-btn" data-fb-run="${m.run_id}" data-fb-idx="${m.idx}" data-fb-title="${esc(m.title)}" data-fb-file="${esc(m.file || "")}">回流</button>` : ""}</div>`).join("")
          : '<div class="wb-queue-empty">还没有处置记录</div>'}</div>
        <details class="wb-expand"><summary>查看完整处置台账</summary><div class="wb-overflow">${ledgerMarksTable(marks)}</div></details></div>
      <div class="panel wb-analysis-card"><div class="wb-card-head"><h3>回归评测</h3>
          <button class="primary small-btn" id="eval-run" ${!cases.length || latest?.status === "running" ? "disabled" : ""}>${latest?.status === "running" ? "跑分中…" : "运行评测"}</button></div>
        <div id="eval-count" class="small muted">${cases.length} 个用例 · 正例 ${cases.filter((c) => c.polarity === "positive").length} · 负例 ${cases.filter((c) => c.polarity !== "positive").length}</div>
        <div class="wb-eval-score">${latest ? `<strong>${latest.status === "running" ? "进行中" : latest.status === "failed" ? "失败" : (latest.hit ?? "—") + " / " + (latest.cases_total ?? "—")}</strong>
          <span>${latest.status === "done" ? "命中用例" : "最近一轮"} · 漏报 ${latest.miss ?? "—"} · 误报重现 ${latest.fp ?? "—"}</span>`
          : '<strong>尚无基线</strong><span>处置发现后存为用例，再运行评测</span>'}</div>
        <details class="wb-expand"><summary>用例与逐条结果</summary>
          <div id="eval-cases" class="wb-overflow">${evalCasesHtml(cases)}</div>
          ${latest ? '<div class="wb-overflow">' + evalRunHtml(latest, hist[1], {id:"eval-latest"}) + '</div>' : ""}
          ${hist.slice(1).map((h, i) => '<div class="wb-overflow">' + evalRunHtml(h, hist[i + 2]) + '</div>').join("")}
        </details></div>
      <div class="panel wb-analysis-card"><div class="wb-card-head"><h3>近 8 周趋势</h3><a href="#/runs" class="small">审查记录 →</a></div>
        <div class="wb-trend">${weeks.length ? weeks.slice(-8).map((w) => `
          <div class="wb-trend-row"><span class="mono">${esc(w.start || w.week)}</span>
            <span class="wb-trend-track"><i style="width:${Math.round((+w.runs || 0) / maxRuns * 100)}%"></i></span>
            <b>${w.runs}</b></div>`).join("") : '<div class="wb-queue-empty">还没有审查数据</div>'}</div>
        <div class="wb-trend-foot">${weeks.length ? `最近一周：问题 ${weeks.at(-1).blocked} / 审查 ${weeks.at(-1).runs}
          · 误报 ${weeks.at(-1).fp} / 已处置 ${weeks.at(-1).marked}` : "等待首轮审查"}</div>
      </div>
    </div></section>`;
}

function paintWorkbenchV2() {
  if (!$("wb-queue")) return;
  if ($("wb-pending-total")) $("wb-pending-total").textContent = wbPending().length;
  if ($("wb-failed-total")) $("wb-failed-total").textContent = wbRuns().filter((r) => r.status === "failed").length;
  if ($("wb-running-total")) $("wb-running-total").textContent = wbRuns().filter((r) => ["running", "queued"].includes(r.status)).length;
  $("wb-queue").innerHTML = wbQueueHtml();
  $("wb-review").innerHTML = wbReviewHtml();
  $("wb-analysis").innerHTML = wbAnalysisHtml();
  $("wb-pr-only").onchange = (e) => { workbenchPrOnly = e.target.checked; paintWorkbenchV2(); };
  document.querySelectorAll("[data-fb-run]").forEach((b) =>
    (b.onclick = () => feedbackRule(b.dataset.fbRun, b.dataset.fbIdx, b.dataset.fbTitle, b.dataset.fbFile)));
  bindEvalActions();
  if (wbState.run) wbLoadDiff();
}

async function refreshWorkbenchData() {
  if (location.hash !== "#/workbench") return;
  try {
    const [marks, run, evaluation] = await Promise.all([
      api("/api/marks"), wbState.run ? api(`/api/runs/${wbState.run.id}`) : Promise.resolve(null),
      api("/api/eval/last"),
    ]);
    wbState.marks = marks;
    if (run) wbState.run = run;
    wbState.eval = evaluation;
    paintWorkbenchV2();
  } catch (e) { toast(e.message, true); }
}

async function renderWorkbench() {
  clearInterval(refreshTimer);
  setTitle("工作台");
  showSkeleton();
  try {
    const [projects, metrics, evaluation, runs, marks] = await Promise.all([
      api("/api/projects"), api("/api/metrics"), api("/api/eval/last"),
      api("/api/runs?limit=40"), api("/api/marks"),
    ]);
    Object.assign(wbState, { projects, metrics, eval: evaluation, runs, marks });
    const first = wbPending()[0]?.run_id || wbRuns().find((r) => r.status === "blocked")?.id || wbRuns()[0]?.id;
    const selected = wbPending().some((f) => f.run_id === wbState.run?.id) ||
      wbRuns().some((r) => r.id === wbState.run?.id) ? wbState.run.id : first;
    wbState.run = selected ? await api(`/api/runs/${selected}`) : null;
    wbState.finding = wbState.run?.id === wbPending()[0]?.run_id ? wbPending()[0].idx : 0;
  } catch (e) { errorPanel(e.message); return; }
  $app.innerHTML = `<div class="wb-page">
    <div class="wb-top"><div><span class="wb-eyebrow">REVIEW OPERATIONS</span><h1>审查工作台</h1>
      <p>先确认值得处理的发现，再用代码和评测判断下一步。</p></div>
      <a class="wb-all-link" href="#/runs">浏览全部审查 ↗</a></div>
    <div class="wb-toolbar">
      <label class="wb-search"><span>⌕</span><input id="wb-search" aria-label="搜索审查和发现" placeholder="搜索发现、文件、项目、PR 或 run"
        value="${esc(workbenchSearch)}"></label>
      <select id="mf-project" aria-label="筛选项目"><option value="">全部项目</option>
        ${wbState.projects.map((p) => `<option value="${p.id}" ${marksFilter.project_id === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
      <div class="wb-summary"><span><b id="wb-pending-total">${wbPending().length}</b> 待处置</span>
        <span><b id="wb-failed-total">${wbRuns().filter((r) => r.status === "failed").length}</b> 近期失败</span>
        <span><b id="wb-running-total">${wbRuns().filter((r) => r.status === "running" || r.status === "queued").length}</b> 近期进行中</span></div>
    </div>
    <div class="wb-grid"><aside class="panel wb-queue" id="wb-queue"></aside>
      <section class="panel wb-review" id="wb-review"></section></div>
    <div id="wb-analysis"></div></div>`;
  $("wb-search").oninput = (e) => { workbenchSearch = e.target.value.trim().toLowerCase(); paintWorkbenchV2(); };
  $("mf-project").onchange = async (e) => {
    marksFilter.project_id = +e.target.value || 0;
    const first = wbPending()[0]?.run_id || wbRuns()[0]?.id;
    if (!first) { wbState.run = null; wbState.finding = 0; }
    if (first && wbState.run?.id !== first) {
      try { wbState.run = await api(`/api/runs/${first}`); wbState.finding = 0; }
      catch (err) { toast(err.message, true); }
    }
    paintWorkbenchV2();
  };
  paintWorkbenchV2();
  if (wbState.eval.history?.[0]?.status === "running") pollEval();
}

document.addEventListener("click", async (e) => {
  const runButton = e.target.closest("[data-wb-run]");
  if (runButton) {
    const id = +runButton.dataset.wbRun, idx = +(runButton.dataset.wbIdx || 0);
    if (wbState.run?.id === id) { wbState.finding = idx; paintWorkbenchV2(); return; }
    try {
      wbState.run = await api(`/api/runs/${id}`);
      wbState.finding = idx;
      paintWorkbenchV2();
    } catch (err) { toast(err.message, true); }
    return;
  }
  const findingButton = e.target.closest("[data-wb-finding]");
  if (findingButton) { wbState.finding = +findingButton.dataset.wbFinding; paintWorkbenchV2(); }
});

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
      sub: "人工反馈闭环：处置队列里的发现 → 确认 / 误报 → 高误报模式回流评审规则",
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
      if (!confirm(`移除项目 ${el.dataset.name}？（审查记录一并删除）`)) return;
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
          ${p.auto_revert ? `<span class="badge">自动回滚</span>` : ""}
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
      <div class="field"><label>合并后评审发现 Important 时自动回滚</label>
        <select id="f-autorevert"><option value="no">关闭（回滚走按钮）</option><option value="yes">开启</option></select></div>
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
  const org = prompt("要导入的组织名：");
  if (!org) return;
  const label = prompt("使用哪个 GitHub 连接 ID？留空表示匿名导入公开仓库。\n" +
    githubConnections.map((c) => `${c.id}: ${c.label}${c.disabled ? "（停用）" : ""}`).join("\n"));
  const cid = label && label.trim() ? Number(label.trim()) : null;
  if (cid !== null && !githubConnections.some((c) => c.id === cid && !c.disabled)) {
    toast("连接 ID 无效", true); return;
  }
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
  } catch (e) { errorPanel(e.message); return; }
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
        ${p.auto_revert ? `<span class="badge">自动回滚</span>` : ""} ${initChip}
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
      if (!confirm(`移除项目 ${el.dataset.name}？（审查记录一并删除）`)) return;
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
}

function runBannerHtml(run) {
  return {
    passed: `<div class="banner passed"><span class="mark"></span><div><b>通过</b> —— 未发现会导致线上问题的事项</div></div>`,
    blocked: `<div class="banner blocked"><span class="mark"></span><div><b>发现问题</b> —— ${run.important_count} 个 Important；${run.revert_pr_url ? `已自动创建回滚 PR：<a href="${esc(run.revert_pr_url)}" target="_blank">${esc(run.revert_pr_url)}</a>` : "处理完再合入，或对本 run 执行回滚"}</div></div>`,
    failed: `<div class="banner failed"><span class="mark"></span><div><b>执行失败</b> —— ${esc(run.error || "未知错误")}（fail loud：引擎异常绝不静默放行）</div></div>`,
    running: `<div class="banner running"><span class="mark"></span><div><b>审查进行中</b>… 只刷新变化的步骤，页面不再整页重载</div></div>`,
    queued: `<div class="banner running"><span class="mark"></span><div><b>排队中</b>…</div></div>`,
  }[run.status] || "";
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
  const chips = (snap.steps || []).map((s, i) => {
    const meta = s.kind === "gate"
      ? `${GATE_LABEL[s.gate] || s.gate}${s.sandbox === "docker" ? " · Docker" : " · 本机"}`
      : `${s.provider}${s.model ? " · " + s.model : ""}`;
    const st = byName.get(s.name || s.id);
    const status = st ? st.status : "pending";
    if (status === "ok" || status === "fail" || status === "skip") done++;
    const mark = { ok: "✓", fail: "✗", skip: "—" }[status]
      || (status === "running" ? '<span style="animation:pulse 1.2s ease infinite">◐</span>' : "·");
    const cls = status === "ok" ? "confirmed" : status === "fail" ? "imp"
      : status === "running" ? "mock" : "";
    return `<span class="badge ${cls}" title="${esc(meta)}">${mark} ${i + 1}. ${esc(s.name || s.id)}</span>`;
  });
  const total = (snap.steps || []).length;
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
    ${findingGroup(run, "Important —— 交叉确认，或门禁确认", imp, "imp")}
    ${findingGroup(run, "Nit —— 单模型发现，未交叉验证", nit, "nit")}
    ${findingGroup(run, "Pre-existing —— 非本次引入，不阻断", pre, "pre")}`;
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
      actions: `<div class="actions" id="run-actions"></div>`,
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
        <div class="sec-title">执行流水线</div>
        <div class="panel" id="run-pipeline" style="padding:12px 18px"></div>
      </aside>
    </div>`;
}

function patchRunDynamic(prev, fresh) {
  const pv = prev || {};
  // PR 终态后拉一次审批状态（人类批准才算数；agent 批准只展示不计入）
  if (["passed", "blocked", "failed"].includes(fresh.status) && fresh.pr_number) {
    loadApprovals(fresh);
  }
  // banner
  const $b = $("banner-box");
  if ($b && (pv.status !== fresh.status || pv.revert_pr_url !== fresh.revert_pr_url
      || pv.important_count !== fresh.important_count)) {
    $b.innerHTML = runBannerHtml(fresh);
  }
  // 头部操作区（回滚按钮随状态出现/消失）
  const $a = $("run-actions");
  if ($a) {
    const html = `${fresh.status === "blocked" && fresh.head_sha && fresh.project_kind === "github" && !fresh.revert_pr_url
      ? `<button id="revert-btn">创建回滚 PR</button>` : ""}`;
    if ($a.innerHTML !== html) {
      $a.innerHTML = html;
      const revertBtn = $("revert-btn");
      if (revertBtn) revertBtn.onclick = async () => {
        if (!confirm("基于该 run 的 commit 创建回滚 PR？")) return;
        try {
          const r = await api(`/api/runs/${fresh.id}/revert`, { method: "POST" });
          toast(`回滚 PR 已创建：${r.pr_url}`);
          renderRun(fresh.id, true);
        } catch (e) { toast(e.message, true); }
      };
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
    if (!confirm("基于该 run 的 commit 创建回滚 PR？")) return;
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
  const evalBtn = (run.project_kind === "github" && f.severity === "IMPORTANT" && cur)
    ? `<button class="ghost small-btn" data-eval-run="${run.id}" data-eval-idx="${f._idx}"
        title="存进评测集：确认=正例（应报出），误报=负例（不应再报出）；改提示词前跑回归">存为评测用例</button>` : "";
  return `<div class="f-mark"><span class="f-key">处置</span>${btn("confirmed", "确认")}${btn("false_positive", "误报")}${evalBtn}</div>`;
}

/* 长文折叠：卡片保持紧凑，但永远有"展开全部"看完整内容 */
function clampHtml(text, n = 160) {
  if (!text) return "";
  if (text.length <= n) return esc(text);
  return `<span class="clamp-short">${esc(text.slice(0, n))}…<a href="javascript:void(0)" data-clamp class="small" style="margin-left:4px">展开全部</a></span>` +
    `<span class="clamp-full" style="display:none">${esc(text)}<a href="javascript:void(0)" data-clamp class="small" style="margin-left:4px">收起</a></span>`;
}
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-clamp]");
  if (!t) return;
  e.preventDefault();
  e.stopPropagation();
  const short = t.closest(".clamp-short, .clamp-full");
  const wrap = short.parentElement;
  const isShort = short.classList.contains("clamp-short");
  wrap.querySelector(".clamp-short").style.display = isShort ? "none" : "";
  wrap.querySelector(".clamp-full").style.display = isShort ? "" : "none";
});

/* 复制完整问题（标题/位置/触发/建议），发给改代码的人直接能看懂 */
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-copy-run]");
  if (!b) return;
  e.stopPropagation();
  const run = runCache[+b.dataset.copyRun] ||
    (wbState.run?.id === +b.dataset.copyRun ? wbState.run : null);
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
  return `<div class="sec-title" style="letter-spacing:.14em">${esc(title)}</div>
  <div class="finding-grid">${list.map((f) => {
    const isGate = (f.sources || []).some((s) => String(s).startsWith("gate:"));
    return `
    <div class="panel finding f-${cls}">
      <div class="f-head">
        ${fileLink(run, f.file, f.line)}
        <span class="f-src small muted">来源 ${esc((f.sources || []).join(" + "))}
          <button class="ghost small-btn" data-copy-run="${run.id}" data-copy-idx="${f._idx}"
            title="复制完整问题（标题/位置/触发/建议）—— 直接发给改代码的人">复制</button></span>
      </div>
      <div class="f-title">${clampHtml(f.title)}</div>
      ${f.condition ? `<div class="f-row"><span class="f-key">触发</span>${clampHtml(f.condition, 200)}</div>` : ""}
      ${f.fix ? `<div class="f-row"><span class="f-key">建议</span>${clampHtml(f.fix, 200)}</div>` : ""}
      ${isGate
        ? `<div class="f-mark"><span class="f-key">详情</span><button class="ghost small-btn" data-goto-log>查看门禁日志</button></div>`
        : markButtonsHtml(run, f)}
    </div>`;
  }).join("")}</div>`;
}

/* ---------------- 配置中心 ---------------- */

function cfgBadge(set) {
  return set ? `<span class="badge">已配置</span>` : `<span class="badge imp">未配置</span>`;
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
    const running = await refreshEvalLive();
    if (!running) {
      autoRefresh(null, false);
      toast("评测完成：结果已写入跑分历史");
      if (location.hash === "#/workbench") { renderWorkbench(); return; }
      const d = await api("/api/eval/last");
      const pos = d.cases.filter((c) => c.polarity === "positive").length;
      $("eval-cases").innerHTML = evalCasesHtml(d.cases);
      bindEvalActions();
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
    $app.innerHTML = `
    ${pageHead({ title: "流水线编排", sub: "全局默认；项目可在自己的「流水线」页覆盖" })}
    ${tabsHtml(tab)}
    <div class="panel stagger" style="padding:12px 20px">
      <div id="pipe-list"></div>
      <div class="actions" style="margin-top:12px">
        <button id="pipe-add">添加评审步骤</button>
        <button id="pipe-add-gate">添加门禁</button>
        <button class="ghost" id="pipe-reset">恢复默认</button>
        <button class="primary" id="pipe-save">保存全局默认</button>
      </div>
      <div class="hint" title="从上到下依次执行；风险路由/密钥扫描固定在流水线之前，裁决固定在最后。被 ≥2 个评审步骤同时报出的发现才升 Important（交叉确认），只启用一个评审步骤时不降级。列表里一个门禁都没有时，保存会自动补默认测试门禁——想关闭请停用而非删除。">门禁红灯即阻断、不调用模型；≥2 个评审报出同一问题才升 Important</div>
    </div>
    <div class="sec-title stagger">修改历史 <span class="faint small mono" style="letter-spacing:0;text-transform:none">每次保存自动留档 · 可一键恢复</span></div>
    <div class="panel stagger" style="padding:4px 18px" id="hist-pipeline"></div>`;
    const draft = JSON.parse(JSON.stringify(pipe.steps));
    const list = $("pipe-list");
    const paint = () => { list.innerHTML = pipeListHTML(draft); bindPipeList(list, draft, paint); };
    paint();
    $("pipe-add").onclick = () => {
      draft.push({ id: `custom${Date.now() % 100000}`, kind: "reviewer",
        name: `评审步骤 ${draft.length + 1}`, provider: "claude", enabled: true, model: "", prompt: "" });
      paint();
    };
    $("pipe-add-gate").onclick = () => {
      draft.push({ id: `gate${Date.now() % 100000}`, kind: "gate",
        name: `门禁 ${draft.length}`, gate: "tests", enabled: true });
      paint();
    };
    $("pipe-reset").onclick = () => {
      draft.splice(0, draft.length,
        { id: "gate-test", kind: "gate", name: "测试门禁", gate: "tests", enabled: true },
        { id: "claude", kind: "reviewer", name: "Claude 评审", provider: "claude", enabled: true, model: "", prompt: "" },
        { id: "kimi", kind: "reviewer", name: "Kimi 评审", provider: "codex", enabled: true, model: "", prompt: "" });
      paint();
    };
    $("pipe-save").onclick = async () => {
      collectPipeDom(list, draft);
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
      <div class="hint" title="每次线上事故后加一条「已知陷阱」；处置页里反复误报的模式写进「一律不报」">事故回流写这里（REVIEW.md）</div>
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
  let conns;
  try { conns = await api("/api/github-connections"); }
  catch (e) { errorPanel(e.message); return; }
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
  <div class="sec-title">模型凭证</div>
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
    const token = prompt("输入新的 GitHub Token（原 token 将立即失效）：");
    if (!token) return;
    try { await api(`/api/github-connections/${b.dataset.cRotate}/token`, {method: "PUT", body: {token}}); toast("token 已更换"); renderSettings("creds"); }
    catch (e) { toast(e.message, true); }
  });
  document.querySelectorAll("[data-c-toggle]").forEach((b) => b.onclick = async () => {
    try { await api(`/api/github-connections/${b.dataset.cToggle}`, {method: "PATCH", body: {disabled: b.dataset.disabled !== "1"}}); renderSettings("creds"); }
    catch (e) { toast(e.message, true); }
  });
  document.querySelectorAll("[data-c-delete]").forEach((b) => b.onclick = async () => {
    if (!confirm("删除此连接？")) return;
    try { await api(`/api/github-connections/${b.dataset.cDelete}`, {method: "DELETE"}); renderSettings("creds"); }
    catch (e) { toast(e.message, true); }
  });
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
        if (!confirm("恢复到这个版本？当前生效配置将被覆盖")) return;
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
  setTitle("流水线编排");
  showSkeleton();
  let p, pp;
  try {
    [p, pp] = await Promise.all([
      api("/api/projects").then((l) => l.find((x) => x.id === pid)),
      api(`/api/projects/${pid}/pipeline`),
    ]);
  } catch (e) { errorPanel(e.message); return; }
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
        <button id="pp-add">添加评审步骤</button>
        <button id="pp-add-gate">添加门禁</button>
      </div>
      <div class="hint" title="生效顺序：项目编排 > 全局默认；与全局相同，列表里没有门禁时会自动补默认测试门禁">只影响此项目；全局默认在配置中心维护</div>
    </div>

    <div class="sec-title stagger">修改历史 <span class="faint small mono" style="letter-spacing:0;text-transform:none">此项目编排 · 每次保存自动留档</span></div>
    <div class="panel stagger" style="padding:4px 18px" id="hist-ppipeline"></div>`;

  const list = $("pp-list");
  const paint = () => { list.innerHTML = pipeListHTML(draft); bindPipeList(list, draft, paint); };
  paint();
  $("pp-add").onclick = () => {
    draft.push({ id: `custom${Date.now() % 100000}`, kind: "reviewer",
      name: `评审步骤 ${draft.length + 1}`, provider: "claude", enabled: true, model: "", prompt: "" });
    paint();
  };
  $("pp-add-gate").onclick = () => {
    draft.push({ id: `gate${Date.now() % 100000}`, kind: "gate",
      name: `门禁 ${draft.length}`, gate: "tests", enabled: true });
    paint();
  };
  $("pp-save").onclick = async () => {
    collectPipeDom(list, draft);
    try {
      await api(`/api/projects/${pid}/pipeline`, { method: "PUT", body: { steps: draft } });
      toast("已保存：此项目之后的审查按此编排执行");
      renderProjectPipeline(pid);
    } catch (e) { toast(e.message, true); }
  };
  mountConfigHistory("hist-ppipeline", `project_pipeline:${pid}`, () => renderProjectPipeline(pid));
  const inheritBtn = $("pp-inherit");
  if (inheritBtn) inheritBtn.onclick = async () => {
    if (!confirm("恢复后此项目将跟随全局默认编排（删除独立编排）？")) return;
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
  { code: "{truncated}", label: "截断标记", required: false },
];

const GATE_LABEL = { tests: "测试", build: "构建", semgrep: "安全扫描" };
const GATE_TIP = "机械门禁：失败即红灯阻断，不调用模型；仓库无入口或部署机缺工具记 skip（安全扫描缺 semgrep 记红灯）";
const SANDBOX_TIP = "本机=白名单环境变量执行（token 不进子进程）；Docker=一次性容器，断网+CPU/内存上限+依赖缓存卷，多项目并发互不影响。选 Docker 需本机装 Docker，缺失时该门禁红灯";

function pipeListHTML(draft) {
  return draft.map((s, i) => {
    if (s.kind === "gate") {
      return `<div class="pipe-step ${s.enabled ? "" : "off"}" data-idx="${i}">
        <div class="pipe-main">
          <span class="pipe-handle" draggable="true" data-drag="${i}" title="拖拽排序">⠿</span>
          <span class="pipe-num mono">${i + 1}</span>
          <input type="checkbox" class="pipe-en" data-en="${i}" ${s.enabled ? "checked" : ""} title="启用/停用">
          <input class="pipe-name" data-name="${i}" value="${esc(s.name || "")}">
          <select class="pipe-provider" data-gate="${i}" title="${GATE_TIP}">
            ${Object.entries(GATE_LABEL).map(([g, label]) =>
              `<option value="${g}" ${s.gate === g ? "selected" : ""}>${label}门禁</option>`).join("")}
          </select>
          <select class="pipe-provider" data-sandbox="${i}" title="${SANDBOX_TIP}">
            <option value="off" ${s.sandbox !== "docker" ? "selected" : ""}>本机隔离</option>
            <option value="docker" ${s.sandbox === "docker" ? "selected" : ""}>Docker 沙盒</option>
          </select>
          <select class="pipe-provider" data-network="${i}" ${s.sandbox !== "docker" ? "disabled" : ""}
                  title="仅 Docker 沙盒生效：断网=完全离线（需仓库自带依赖）；仅安装期放网=先联网装依赖再断网跑测试（默认）；全程放网=最弱（go 拉模块需要）">
            <option value="install" ${(s.network || "install") === "install" ? "selected" : ""}>安装期放网</option>
            <option value="off" ${s.network === "off" ? "selected" : ""}>断网</option>
            <option value="on" ${s.network === "on" ? "selected" : ""}>全程放网</option>
          </select>
          <input class="pipe-model" data-image="${i}" value="${esc(s.image || "")}"
                 placeholder="镜像（留空自动：node/go/python）" title="Docker 镜像；留空按仓库特征自动选择">
          <span class="pipe-ops">
            <button class="danger-ghost small-btn" data-del="${i}">删除</button>
          </span>
        </div>
      </div>`;
    }
    return `<div class="pipe-step ${s.enabled ? "" : "off"}" data-idx="${i}">
      <div class="pipe-main">
        <span class="pipe-handle" draggable="true" data-drag="${i}" title="拖拽排序">⠿</span>
        <span class="pipe-num mono">${i + 1}</span>
        <input type="checkbox" class="pipe-en" data-en="${i}" ${s.enabled ? "checked" : ""} title="启用/停用">
        <input class="pipe-name" data-name="${i}" value="${esc(s.name || "")}">
        <select class="pipe-provider" data-provider="${i}">
          <option value="claude" ${s.provider !== "codex" ? "selected" : ""}>Claude</option>
          <option value="codex" ${s.provider === "codex" ? "selected" : ""}>Kimi</option>
        </select>
        <input class="pipe-model" data-model="${i}" value="${esc(s.model || "")}" placeholder="模型（留空=默认）">
        <span class="pipe-ops">
          <button class="danger-ghost small-btn" data-del="${i}">删除</button>
        </span>
      </div>
      <details class="pipe-prompt">
        <summary>此步骤专属提示词（留空用全局）</summary>
        <textarea class="profile-edit" data-pp="${i}" rows="5">${esc(s.prompt || "")}</textarea>
      </details>
    </div>`;
  }).join("");
}

/* 从 DOM 收集未触发 oninput 的最新值回填 draft */
function collectPipeDom(root, draft) {
  root.querySelectorAll("[data-name]").forEach((el) => draft[+el.dataset.name].name = el.value);
  root.querySelectorAll("[data-model]").forEach((el) => draft[+el.dataset.model].model = el.value);
  root.querySelectorAll("[data-pp]").forEach((el) => draft[+el.dataset.pp].prompt = el.value);
  root.querySelectorAll("[data-provider]").forEach((el) => draft[+el.dataset.provider].provider = el.value);
  root.querySelectorAll("[data-gate]").forEach((el) => draft[+el.dataset.gate].gate = el.value);
  root.querySelectorAll("[data-sandbox]").forEach((el) => draft[+el.dataset.sandbox].sandbox = el.value);
  root.querySelectorAll("[data-network]").forEach((el) => draft[+el.dataset.network].network = el.value);
  root.querySelectorAll("[data-image]").forEach((el) => draft[+el.dataset.image].image = el.value);
}

/* 编辑器绑定：结构性变更（拖拽/删除）后调 repaint() 重画 */
function bindPipeList(root, draft, repaint) {
  let dragIdx = null;
  root.querySelectorAll(".pipe-handle").forEach((h) => {
    h.addEventListener("dragstart", (e) => {
      dragIdx = +h.dataset.drag;
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", String(dragIdx));
    });
    h.addEventListener("dragend", () => {
      dragIdx = null;
      root.querySelectorAll(".pipe-step").forEach((el) => el.classList.remove("drag-over"));
    });
  });
  root.querySelectorAll(".pipe-step").forEach((el) => {
    el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drag-over"); });
    el.addEventListener("dragleave", () => el.classList.remove("drag-over"));
    el.addEventListener("drop", (e) => {
      e.preventDefault();
      el.classList.remove("drag-over");
      if (dragIdx == null) return;
      const from = dragIdx, to = +el.dataset.idx;
      if (from === to) return;
      const [moved] = draft.splice(from, 1);
      draft.splice(to, 0, moved);
      dragIdx = null;
      repaint();
    });
  });
  root.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => {
    draft.splice(+b.dataset.del, 1);
    repaint();
  });
  root.querySelectorAll("[data-en]").forEach((c) => c.onchange = () => {
    draft[+c.dataset.en].enabled = c.checked;
    c.closest(".pipe-step").classList.toggle("off", !c.checked);
  });
  root.querySelectorAll("[data-name]").forEach((el) => el.oninput = () => { draft[+el.dataset.name].name = el.value; });
  root.querySelectorAll("[data-gate]").forEach((el) => el.onchange = () => { draft[+el.dataset.gate].gate = el.value; });
  root.querySelectorAll("[data-sandbox]").forEach((el) => el.onchange = () => {
    draft[+el.dataset.sandbox].sandbox = el.value;
    repaint();   // 切沙盒模式时刷新「网络」下拉的可用状态
  });
  root.querySelectorAll("[data-network]").forEach((el) => el.onchange = () => { draft[+el.dataset.network].network = el.value; });
  root.querySelectorAll("[data-image]").forEach((el) => el.oninput = () => { draft[+el.dataset.image].image = el.value; });
  root.querySelectorAll("[data-model]").forEach((el) => el.oninput = () => { draft[+el.dataset.model].model = el.value; });
  root.querySelectorAll("[data-provider]").forEach((el) => el.onchange = () => { draft[+el.dataset.provider].provider = el.value; });
  root.querySelectorAll("[data-pp]").forEach((el) => el.oninput = () => { draft[+el.dataset.pp].prompt = el.value; });
}

navigate();
