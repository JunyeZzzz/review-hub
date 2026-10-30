/* 质量改进工作台：反馈 → 候选 → 对照验证 → 采用 → 实际使用。 */
let improveData = null,
  improveSelection = null,
  improveFilter = "all",
  improveProject = null;
let improveDetail = null,
  improvePoll = null;
const IMP_STATUS = {
  draft: "待完善",
  validating: "验证中",
  ready: "待采用",
  needs_changes: "需调整",
  active: "已生效",
  revoked: "已撤销",
  analyzing: "分析中",
};
const IMP_KIND = { knowledge: "项目知识", check: "核验步骤" };
const IMP_RESULT = {
  hit: "符合预期",
  miss: "漏报",
  fp: "误报重现",
  error: "执行错误",
  not_run: "未运行",
};

function improvementWorkbenchHtml(d) {
  improveData = d.improvements;
  return `<div class="im-page">
    <header class="im-page-head"><div><span class="wb-eyebrow">REVIEW QUALITY / 改进行动</span><h1>让每次反馈都有下文</h1>
      <p>从判定依据到改进生效，查看每一步的修改、验证与使用证据。</p></div>
      <div class="actions"><button class="primary small-btn" id="im-missed">补充漏报</button><button class="ghost small-btn" id="im-project">所有项目 ▾</button><a href="#/runs" class="ghost small-btn">审查报告 ↗</a></div></header>
    <div class="im-overview">${[
      [
        "待补充依据",
        improveData.feedback.filter((f) => f.active && !f.reason).length,
      ],
      [
        "候选改进",
        improveData.items.filter(
          (i) => !["active", "revoked"].includes(i.status),
        ).length,
      ],
      ["已生效", improveData.items.filter((i) => i.effective).length],
    ]
      .map(
        ([label, n]) =>
          `<div><strong data-im-count="${esc(label)}">${n}</strong><span>${label}</span></div>`,
      )
      .join("")}
      <span class="im-overview-note">采用前对照验证 · 按项目与路径生效 · 随时撤销</span></div>
    <section class="im-workspace"><aside class="im-rail"><div class="im-rail-head"><strong>反馈与改进</strong><button class="ghost small-btn" id="im-refresh" aria-label="刷新">↻</button></div>
      <input id="im-search" placeholder="搜索问题、原因或改进…" aria-label="搜索反馈与改进">
      <div class="im-filters">${[
        ["all", "全部"],
        ["inbox", "未关联"],
        ["items", "改进项"],
        ["active", "已生效"],
      ]
        .map(
          ([v, l]) =>
            `<button class="ghost small-btn ${improveFilter === v ? "on" : ""}" data-im-filter="${v}">${l}</button>`,
        )
        .join("")}</div>
      <div id="im-list" class="im-list"></div><small class="im-limit">显示最近 ${improveData.limit} 条；反馈 ${improveData.totals.feedback} · 改进 ${improveData.totals.items}</small></aside>
      <div id="im-main" class="im-main"><div class="im-welcome"><span>↖</span><h2>从一条反馈开始</h2><p>选择人工反馈，补充原因和证据，再建立可验证的项目改进。</p><a href="#/runs">打开审查报告，确认问题或标记误报 ↗</a></div></div>
      <aside id="im-evidence" class="im-evidence"><h3>证据与历史</h3><p class="muted small">选中反馈或改进后，原始问题、关联报告和事件记录会显示在这里。</p></aside>
    </section>
    <details class="panel im-support"><summary>其他质量任务 <span class="faint small">失败审查处置 / 项目全量回归 / 历史样本</span></summary>
      <div class="qa-operate-grid"><section><h3>待处理报告</h3>${qualityAttentionHtml(d)}</section><section><h3>全量回归</h3><div id="qa-eval-status">${qualityEvalHtml(d)}</div></section></div>
      <button class="ghost small-btn" data-qa-toggle="qa-details">查看回归样本与历史</button><div id="qa-details" hidden>${qualityDetailsHtml(d)}</div></details></div>`;
}

function imFeedbackState(f) {
  return !f.active
    ? "已撤销"
    : !f.reason
      ? "待补充依据"
      : f.improvement_ids.length
        ? "已关联改进"
        : "待形成改进";
}
function imStatus(value) {
  return `<span class="im-status ${esc(value)}">${IMP_STATUS[value] || esc(value)}</span>`;
}
function imList() {
  imRefreshStats();
  if (!improveData || !document.getElementById("im-list")) return;
  const query = $("im-search").value.trim().toLowerCase();
  const matches = (x) =>
    (!improveProject || x.project_id === improveProject) &&
    JSON.stringify(x).toLowerCase().includes(query);
  const items = improveData.items
    .filter(matches)
    .filter(
      (i) =>
        improveFilter !== "inbox" &&
        (improveFilter !== "active" || i.status === "active"),
    );
  const feedback = improveData.feedback
    .filter(matches)
    .filter(
      (f) =>
        !["items", "active"].includes(improveFilter) &&
        (improveFilter !== "inbox" || (f.active && !f.improvement_ids.length)),
    );
  $("im-list").innerHTML =
    items
      .map(
        (i) =>
          `<button class="im-row ${improveSelection === `i${i.id}` ? "selected" : ""}" data-im-item="${i.id}"><div>${i.status === "active" && !i.effective ? '<span class="im-status needs_changes">暂停 · 配置变化</span>' : imStatus(i.status)}<small>${esc(i.project)}</small></div><strong>${esc(i.title)}</strong><span>${IMP_KIND[i.kind]} · v${i.revision} · ${i.source_count} 条反馈 · ${i.use_count} 次使用</span></button>`,
      )
      .join("") +
      feedback
        .map(
          (f) =>
            `<button class="im-row ${improveSelection === `f${f.id}` ? "selected" : ""}" data-im-feedback="${f.id}"><div><span class="im-status ${f.disposition === "false_positive" ? "needs_changes" : ""}">${f.idx < 0 ? "人工补充漏报" : f.disposition === "confirmed" ? "真实问题" : f.disposition === "false_positive" ? "误报" : "撤销判定"}</span><small>${esc(f.project)}</small></div><strong>${esc(f.finding.title)}</strong><span>${esc(imFeedbackState(f))} · 报告 #${f.run_id}</span></button>`,
        )
        .join("") || '<div class="im-empty">没有匹配记录</div>';
  document
    .querySelectorAll("[data-im-item]")
    .forEach((b) => (b.onclick = () => imOpenItem(+b.dataset.imItem)));
  document
    .querySelectorAll("[data-im-feedback]")
    .forEach((b) => (b.onclick = () => imOpenFeedback(+b.dataset.imFeedback)));
}

function bindImprovements() {
  $("im-search").oninput = imList;
  $("im-missed").onclick = imMissedDialog;
  $("im-refresh").onclick = () => renderWorkbench();
  $("im-project").onclick = async () => {
    const result = await uiChoice("选择项目", [
      { value: "", label: "所有项目" },
      ...qualityData.projects.map((p) => ({
        value: String(p.id),
        label: p.name,
      })),
    ]);
    if (result === null) return;
    improveProject = result ? +result : null;
    $("im-project").textContent =
      (qualityData.projects.find((p) => p.id === improveProject)?.name ||
        "所有项目") + " ▾";
    imList();
  };
  document.querySelectorAll("[data-im-filter]").forEach(
    (b) =>
      (b.onclick = () => {
        improveFilter = b.dataset.imFilter;
        document
          .querySelectorAll("[data-im-filter]")
          .forEach((x) => x.classList.toggle("on", x === b));
        imList();
      }),
  );
  imList();
  if (
    improveSelection?.startsWith("i") &&
    improveData.items.some((i) => `i${i.id}` === improveSelection)
  )
    imOpenItem(+improveSelection.slice(1));
  else if (
    improveSelection?.startsWith("f") &&
    improveData.feedback.some((f) => `f${f.id}` === improveSelection)
  )
    imOpenFeedback(+improveSelection.slice(1));
  else if (improveData.items.length)
    imOpenItem(
      improveData.items.find(
        (i) => i.status === "ready" || i.status === "validating",
      )?.id || improveData.items[0].id,
    );
  else if (improveData.feedback.length)
    imOpenFeedback(improveData.feedback[0].id);
}

function imSourceHtml(f) {
  return `<article class="im-source"><a href="#/runs/${f.run_id}">报告 #${f.run_id} ↗</a><strong>${esc(f.finding.title)}</strong><code>${esc(f.finding.file || "无文件")} : ${esc(f.finding.line || "—")}</code>
    <dl><dt>触发条件</dt><dd>${esc(f.finding.condition || "未记录")}</dd><dt>原建议</dt><dd>${esc(f.finding.fix || "未记录")}</dd><dt>人工原因</dt><dd>${esc(f.reason || "待补充")}</dd><dt>证据</dt><dd>${esc(f.evidence || "待补充")}</dd></dl>
    <small>${esc(imFeedbackState(f))} · ${fmtTime(f.updated_at)}</small></article>`;
}

function imCompactSourceHtml(f) {
  return `<details class="im-source-fold"><summary><small>反馈 #${f.id} · ${f.disposition === "confirmed" ? "真实问题" : "误报"}</small><strong>${esc(f.finding.title)}</strong></summary>${imSourceHtml(f)}</details>`;
}

function imEventHtml(e) {
  const labels = {
    analysis_started: "开始分析",
    analysis_finished: "分析完成",
    analysis_failed: "分析失败",
    comparison_reviewed: "核对通过",
    comparison_corrected: "纠正匹配判定",
    created: "建立候选",
    edited: "编辑版本",
    validation_started: "开始验证",
    validation_finished: "验证完成",
    validation_failed: "验证失败",
    validation_resumed: "继续未完成验证",
    adopted: "采用生效",
    revoked: "撤销生效",
    feedback_changed: "来源反馈变化",
    analysis_evidence_recovered: "补全真实核查证据",
    execution_evidence_recovered: "补全真实执行证据",
    baseline_contract_recorded: "锁定真实基线契约",
    adoption_policy_reassessed: "复核采用规则",
    baseline_checkpoint_recovered: "恢复完整基线检查点",
    parser_reprocessed: "重新解析真实模型回复",
    code_regressions_added: "历史反馈已加入代码回归",
    interrupted: "任务中断",
  };
  const d = e.detail;
  const message =
    d.message ||
    (d.instruction
      ? "候选内容已保存，展开查看这次修改"
      : `${d.cases || ""} 条样本 · ${d.executions || ""} 次执行`);
  return `<article><small>${fmtTime(e.created_at)}</small><strong>${labels[e.action] || esc(e.action)}</strong><p>${esc(message)}</p>${d.instruction ? `<details><summary>查看这次候选修改</summary><pre class="im-raw">${esc(JSON.stringify({ title: d.title, kind: d.kind, scope: d.scope, rationale: d.rationale, instruction: d.instruction, evidence: d.evidence, feedback_ids: d.feedback_ids }, null, 2))}</pre></details>` : ""}${d.test_links?.length ? `<details><summary>关联的可执行回归 · ${d.test_links.length}</summary>${d.test_links.map((t) => `<p class="small">反馈 #${t.feedback_id} · ${esc(t.scenario)}<br><code>${esc(t.path)}</code><br>原提交复现失败 → 当前树通过</p>`).join("")}</details>` : ""}${imAnalysisEvidenceHtml(d)}</article>`;
}

function imOpenFeedback(fid) {
  clearTimeout(improvePoll);
  improveSelection = `f${fid}`;
  improveDetail = null;
  imList();
  const f = improveData.feedback.find((f) => f.id === fid);
  if (!f) return;
  const related = improveData.items.filter(
    (i) =>
      i.project_id === f.project_id &&
      !["active", "validating", "analyzing"].includes(i.status),
  );
  $("im-main").innerHTML =
    `<div class="im-detail-head"><div><span class="wb-eyebrow">反馈 #${f.id} / ${esc(f.project)}</span><h2>${esc(f.finding.title)}</h2></div><span class="im-status">${esc(imFeedbackState(f))}</span></div>
    <div class="im-feedback-path"><span class="done">人工判定</span><i>→</i><span class="${f.reason ? "done" : "current"}">原因与证据</span><i>→</i><span>候选改进</span></div>
    <form id="im-feedback-form"><div class="im-choice-row"><button type="button" data-im-disposition="confirmed" class="${f.disposition === "confirmed" ? "on" : ""}">✓ 真实问题</button><button type="button" data-im-disposition="false_positive" ${f.idx < 0 ? "disabled" : ""} class="${f.disposition === "false_positive" ? "on" : ""}">× 误报</button></div>
    <label>为什么这样判断？<textarea name="reason" required maxlength="3000" rows="4" placeholder="例如：入口已通过统一中间件鉴权，评审遗漏了上游调用链">${esc(f.reason)}</textarea></label>
    <label>证据或核验位置<textarea name="evidence" maxlength="3000" rows="3" placeholder="仓库路径、函数名、测试或业务约束；后续审查仍需核实">${esc(f.evidence)}</textarea></label>
    <div class="im-action-bar"><button class="primary" type="submit">保存判定依据</button><button type="button" class="ghost" id="im-start-candidate" ${!f.active ? "disabled" : ""}>手动建立候选</button><button type="button" class="primary" id="im-ai-draft" ${!f.active ? "disabled" : ""}>AI 分析并起草 →</button></div></form>
    <section class="im-section"><h3>这条反馈会改变什么？</h3><p>保存判定会更新正／负样本。只有关联候选、完成验证并采用后，后续审查才会使用改进。改判会使关联验证失效，并撤销已生效项。</p>
    ${f.improvement_ids.map((id) => `<button class="ghost small-btn" data-im-jump="${id}">查看关联改进 #${id}</button>`).join("")}
    ${related.length ? '<p class="small muted">选择下面的候选合并这条反馈，共用一次改进与验证。</p>' : ""}${related
      .filter((i) => !f.improvement_ids.includes(i.id))
      .map(
        (i) =>
          `<button class="ghost small-btn" data-im-merge="${i.id}">合并到 #${i.id} · ${esc(i.title)}</button>`,
      )
      .join("")}</section>`;
  $("im-evidence").innerHTML =
    `<h3>原始发现</h3>${imSourceHtml(f)}${f.history?.length ? `<h3>判定历史</h3>${f.history.map((h) => `<article class="im-source"><small>${fmtTime(h.created_at)}</small><p class="small">${h.snapshot.disposition === "confirmed" ? "真实问题" : h.snapshot.disposition === "false_positive" ? "误报" : "撤销判定"} · ${esc(h.snapshot.reason)}</p><p class="small muted">${esc(h.snapshot.evidence)}</p></article>`).join("")}` : ""}`;
  let disposition = f.disposition || "false_positive";
  document.querySelectorAll("[data-im-disposition]").forEach(
    (b) =>
      (b.onclick = () => {
        disposition = b.dataset.imDisposition;
        document
          .querySelectorAll("[data-im-disposition]")
          .forEach((x) => x.classList.toggle("on", x === b));
      }),
  );
  const save = async () => {
    const form = $("im-feedback-form");
    if (!form.reportValidity()) return false;
    const values = new FormData(form);
    await api(`/api/feedback/${f.id}`, {
      method: "PUT",
      body: {
        disposition,
        note: values.get("reason"),
        evidence: values.get("evidence"),
      },
    });
    improveData = await api("/api/improvements");
    qualityData.improvements = improveData;
    return true;
  };
  $("im-feedback-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      if (await save()) {
        toast("依据已保存，关联策略与样本已同步");
        imOpenFeedback(fid);
      }
    } catch (e) {
      toast(e.message, true);
    }
  };
  $("im-start-candidate").onclick = async () => {
    try {
      if (await save()) imEditor(null, [fid]);
    } catch (e) {
      toast(e.message, true);
    }
  };
  $("im-ai-draft").onclick = async () => {
    try {
      if (!(await save())) return;
      if (
        !(await uiConfirm(
          "使用本项目首个启用的评审器，在原提交独立副本中只读核验反馈并起草。会产生一次模型调用，结果需要核对和验证。",
          "AI 分析反馈",
        ))
      )
        return;
      const item = await api("/api/improvements/draft", {
        method: "POST",
        body: { feedback_ids: [fid] },
      });
      improveData = await api("/api/improvements");
      qualityData.improvements = improveData;
      imOpenItem(item.id);
    } catch (e) {
      toast(e.message, true);
    }
  };
  document.querySelectorAll("[data-im-merge]").forEach(
    (b) =>
      (b.onclick = async () => {
        try {
          if (!(await save())) return;
          const item = await api(`/api/improvements/${+b.dataset.imMerge}`);
          imEditor(item, [...new Set([...item.feedback_ids, fid])]);
        } catch (e) {
          toast(e.message, true);
        }
      }),
  );
  document
    .querySelectorAll("[data-im-jump]")
    .forEach((b) => (b.onclick = () => imOpenItem(+b.dataset.imJump)));
}

function imEditor(item, ids) {
  clearTimeout(improvePoll);
  const first = improveData.feedback.find((f) => f.id === ids[0]);
  const sources = improveData.feedback.filter(
    (f) => f.active && f.project_id === (item?.project_id || first?.project_id),
  );
  $("im-main").innerHTML =
    `<div class="im-detail-head"><div><span class="wb-eyebrow">${item ? `改进 #${item.id} / 新版本` : "从反馈形成项目改进"}</span><h2>${item ? "编辑候选改进" : "建立候选改进"}</h2></div></div>
    <form id="im-candidate-form"><label>改进名称<input name="title" required maxlength="160" value="${esc(item?.title || "")}" placeholder="例如：检查权限前追踪统一鉴权入口"></label>
    <div class="im-choice-row"><button type="button" data-im-kind="check" class="${item?.kind !== "knowledge" ? "on" : ""}">核验步骤</button><button type="button" data-im-kind="knowledge" class="${item?.kind === "knowledge" ? "on" : ""}">项目知识</button></div>
    <div class="im-form-grid"><label>适用的仓库路径<input name="scope" required maxlength="1000" value="${esc(item?.scope || first?.finding.file || "*")}" placeholder="app/**,tests/**"><small>逗号分隔；app/* 仅一层，app/** 含子目录，单独 * 为整个项目。</small></label>
    <label>为什么需要这项改进？<textarea name="rationale" required maxlength="3000" rows="3">${esc(item?.rationale || first?.reason || "")}</textarea></label></div>
    <label>后续审查应执行什么？<textarea name="instruction" required maxlength="5000" rows="4" placeholder="写清核验顺序、成立条件和仍应报告的例外；避免‘这类问题一律不报’">${esc(item?.instruction || "")}</textarea></label>
    <label>可核实的证据线索<textarea name="evidence" required maxlength="3000" rows="2">${esc(item?.evidence || first?.evidence || "")}</textarea></label>
    <fieldset class="im-source-picker"><legend>来源反馈 · 同项目可合并</legend>${sources.map((f) => `<label><input type="checkbox" name="feedback_ids" value="${f.id}" ${ids.includes(f.id) ? "checked" : ""}><span>${esc(f.finding.title)}<small>${f.disposition === "confirmed" ? "真实问题" : "误报"} · #${f.run_id}${!f.reason ? " · 缺判定原因" : ""}</small></span></label>`).join("")}</fieldset>
    <div class="im-action-bar"><button class="primary" type="submit">保存候选${item ? "新版本" : ""}</button><button class="ghost" type="button" id="im-editor-cancel">取消</button></div></form>`;
  let kind = item?.kind || "check";
  document.querySelectorAll("[data-im-kind]").forEach(
    (b) =>
      (b.onclick = () => {
        kind = b.dataset.imKind;
        document
          .querySelectorAll("[data-im-kind]")
          .forEach((x) => x.classList.toggle("on", x === b));
      }),
  );
  $("im-editor-cancel").onclick = () =>
    item ? imOpenItem(item.id) : imOpenFeedback(first.id);
  $("im-candidate-form").onsubmit = async (e) => {
    e.preventDefault();
    const form = e.target;
    if (!form.reportValidity()) return;
    const f = new FormData(form);
    const body = {
      ...Object.fromEntries(f),
      kind,
      feedback_ids: f.getAll("feedback_ids").map(Number),
      revision: item?.revision,
    };
    try {
      const saved = await api(
        item ? `/api/improvements/${item.id}` : "/api/improvements",
        { method: item ? "PUT" : "POST", body },
      );
      improveData = await api("/api/improvements");
      qualityData.improvements = improveData;
      toast("候选已保存，尚未影响正式审查");
      imOpenItem(saved.id);
    } catch (e) {
      toast(e.message, true);
    }
  };
}

async function imOpenItem(iid, quiet = false) {
  clearTimeout(improvePoll);
  improveSelection = `i${iid}`;
  imList();
  try {
    const item = await api(`/api/improvements/${iid}`);
    if (improveSelection !== `i${iid}` || location.hash !== "#/workbench")
      return;
    improveDetail = item;
    const existing = improveData.items.find((i) => i.id === iid);
    if (existing) Object.assign(existing, item);
    imList();
    const openDetails = quiet
      ? [...$("im-main").querySelectorAll("details[open]")].map(
          (d) => d.querySelector("summary")?.textContent,
        )
      : [];
    const scrollY = window.scrollY;
    const v = item.validations[0],
      sources = improveData.feedback.filter((f) =>
        item.feedback_ids.includes(f.id),
      );
    const stage = ["draft", "needs_changes", "revoked"].includes(item.status)
      ? 1
      : item.status === "validating"
        ? 2
        : item.status === "ready"
          ? 3
          : item.status === "active"
            ? 4
            : 0;
    const changeHtml = `<details class="im-section" ${!v || v.stale ? "open" : ""}><summary>具体改变 · ${esc(item.scope)}</summary><p class="small muted">${item.context_version === "per-part-v2" ? "按相关文件提供知识 · 仅进入匹配分片，完整代码仍全部审查" : "旧版输入契约 · 匹配后向全部分片提供；编辑后重新验证"}</p><div class="im-change-grid"><div><span>反馈揭示的原因</span><p>${esc(item.rationale)}</p></div><div><span>采用后的审查要求</span><p>${esc(item.instruction)}</p></div></div><div class="im-evidence-note"><b>核验线索</b><p>${esc(item.evidence)}</p></div></details>`;
    const comparisonHtml = `<section class="im-section"><div class="im-section-head"><h3>当前策略 vs 候选策略</h3><span class="small muted">同一提交 · 同一配置 · 成对执行</span></div>${item.status === "analyzing" ? '<div class="im-running">◌ 正在原提交核查反馈并起草候选，尚未进入验证…</div>' : imValidationHtml(v, item.status !== "active")}${imRemainingHtml(v)}</section>`;
    const usesHtml = `<section class="im-section"><div class="im-section-head"><h3>生效后的实际使用</h3><span class="small muted">最近 ${item.uses.length} 次</span></div>${item.uses.length ? `<div class="im-use-list">${item.uses.map((u) => `<a href="#/runs/${u.run_id}"><b>报告 #${u.run_id}</b><span>使用 v${u.revision} · ${STATUS_LABEL[u.status] || esc(u.status)}</span><small>${fmtTime(u.created_at)}</small></a>`).join("")}</div><p class="small muted">这些记录证明策略被选用，不代表该次所有问题都由它改善。</p>` : '<p class="im-empty">尚无正式审查使用记录；采用后只在匹配路径的审查中使用。</p>'}
      ${item.followup.length ? `<details><summary>使用后的人工反馈 · ${item.followup.length} 条</summary>${item.followup.map((f) => `<a class="im-followup" href="#/runs/${f.run_id}">${f.disposition === "confirmed" ? "真实问题" : "误报"} · ${esc(f.finding.title)} · #${f.run_id}</a>`).join("")}<p class="small muted">用于观察是否再犯，尚未自动归因到某项策略。</p></details>` : ""}</section>`;
    $("im-main").innerHTML =
      `<div class="im-detail-head"><div><span class="wb-eyebrow">改进 #${item.id} / v${item.revision} / ${IMP_KIND[item.kind]}</span><h2>${esc(item.title)}</h2></div>${imStatus(item.status)}</div>
      <div class="im-lifecycle">${["反馈依据", "候选修改", "对照验证", "采用决策", "实际生效"].map((l, n) => `<div class="${n < stage ? "done" : n === stage ? "current" : ""}"><i>${n < stage ? "✓" : n + 1}</i><span>${l}</span></div>`).join("")}</div>
      ${item.status === "active" && !item.effective ? '<p class="im-warning">采用后的模型、规则或项目配置已变化，本项已暂停用于新审查。先撤销，再重新验证和采用。</p>' : ""}<div class="im-action-bar">${!["active", "validating", "analyzing"].includes(item.status) ? `<button class="ghost" id="im-edit">编辑候选</button><button class="${item.status === "ready" && !v?.stale ? "ghost" : "primary"}" id="im-validate">运行对照验证</button>` : ""}
      ${v?.status === "failed" && !v.stale ? '<button class="primary" id="im-resume">继续未完成验证</button>' : ""}
      ${item.status === "ready" && v?.review_note ? `<button class="primary" id="im-adopt" ${v?.stale || !v?.review_note ? "disabled" : ""}>采用此版本</button>` : ""}
      ${item.status === "ready" && !v?.stale && !v?.review_note ? '<button class="primary" id="im-review">核对结果并确认</button>' : ""}
      ${v?.status === "done" && v.snapshot.adoption_policy === "all-hit-v1" && !v.stale && item.status !== "active" ? '<button class="ghost" id="im-reassess">按增量规则复核</button>' : ""}
      ${item.status === "active" ? '<button class="danger-ghost" id="im-revoke">撤销生效</button>' : ""}<span class="small muted">${item.status === "active" ? "后续审查按适用路径选择；已开始的审查保留原快照" : item.status === "validating" ? "候选尚未用于正式审查" : "只影响本项目，公共规则不变"}</span></div>
      ${item.status === "active" ? usesHtml + comparisonHtml + changeHtml : (v && !v.stale) || item.status === "analyzing" ? comparisonHtml + changeHtml + usesHtml : changeHtml + comparisonHtml + usesHtml}
      <details class="im-section"><summary>历次验证记录 · ${item.validations.length}</summary>${
        item.validations
          .slice(1)
          .map(
            (old) =>
              `<details><summary>#${old.id} · v${old.revision} · ${fmtTime(old.created_at)}</summary>${imValidationHtml(old)}</details>`,
          )
          .join("") || '<p class="im-empty">没有更早的验证。</p>'
      }</details>`;
    $("im-evidence").innerHTML =
      `<h3>来源反馈 · ${sources.length}</h3>${sources.map(imCompactSourceHtml).join("")}<h3 class="im-history-title">操作历史</h3><div class="im-timeline">${item.events.map(imEventHtml).join("")}</div>`;
    $("im-reassess")?.addEventListener("click", async () => {
      const note = await uiPrompt("复核采用规则", {
        message:
          "来源完成且有改善、已有正确结果不得退化、执行无错误；遗留漏报仍列为待改进。说明此次复核依据，原始模型输出与结果不改写。",
      });
      if (!note) return;
      try {
        await api(`/api/improvement-validations/${v.id}/reassess`, {
          method: "POST",
          body: { note },
        });
        imOpenItem(iid);
      } catch (e) {
        toast(e.message, true);
      }
    });
    $("im-review")?.addEventListener("click", async () => {
      const note = await uiPrompt("核对对照结果", {
        message:
          "展开逐项结果，核实来源问题已解决、已有正确结果不退化，并明确仍未解决的样本，说明你的核对依据。确认后才能采用。",
      });
      if (!note) return;
      try {
        await api(`/api/improvement-validations/${v.id}/review`, {
          method: "POST",
          body: { note },
        });
        imOpenItem(iid);
      } catch (e) {
        toast(e.message, true);
      }
    });
    document.querySelectorAll("[data-im-correct]").forEach(
      (b) =>
        (b.onclick = async () => {
          const pair = v.results.find(
            (r) => r.case_id === +b.dataset.imCorrect,
          );
          if (!pair) return;
          const options = [
            { value: "hit", label: "符合预期" },
            {
              value: pair.polarity === "positive" ? "miss" : "fp",
              label: pair.polarity === "positive" ? "漏报" : "误报重现",
            },
          ];
          const baseline = await uiChoice("核对当前策略结果", options);
          if (!baseline) return;
          const candidate = await uiChoice("核对候选策略结果", options);
          if (!candidate) return;
          const note = await uiPrompt("为什么需要纠正匹配？", {
            message:
              "原始模型输出和自动匹配结果会保留。执行错误不能通过人工改判绕过。",
          });
          if (!note) return;
          try {
            await api(`/api/improvement-validations/${v.id}/review`, {
              method: "POST",
              body: { case_id: pair.case_id, baseline, candidate, note },
            });
            imOpenItem(iid);
          } catch (e) {
            toast(e.message, true);
          }
        }),
    );
    document
      .querySelectorAll("[data-im-remaining]")
      .forEach(
        (b) => (b.onclick = () => imOpenFeedback(+b.dataset.imRemaining)),
      );
    $("im-resume")?.addEventListener("click", async () => {
      if (
        !(await uiConfirm(
          "仅沿用本轮完整成功的已完成侧；未完成侧重新调用。配置、反馈或样本变化后不能续跑，原失败原因继续保留。",
          "继续验证",
        ))
      )
        return;
      try {
        await api(`/api/improvement-validations/${v.id}/resume`, {
          method: "POST",
        });
        imOpenItem(iid);
      } catch (e) {
        toast(e.message, true);
      }
    });
    $("im-edit")?.addEventListener("click", () =>
      imEditor(item, item.feedback_ids),
    );
    for (const action of ["validate", "adopt", "revoke"])
      $(`im-${action}`)?.addEventListener("click", async () => {
        const messages = {
          validate:
            "将对本项目全部样本分别运行当前策略和候选策略，每个原审查区间分别运行两侧，多条样本复用完整输出。必须有正负样本及额外保护样本；本轮只验证，不自动采用。",
          adopt: "采用后，后续本项目审查按路径使用此版本。可以随时撤销。",
          revoke: "后续审查将停止使用此项改进，进行中的审查保留原始输入。",
        };
        if (
          !(await uiConfirm(
            messages[action],
            {
              validate: "运行对照验证",
              adopt: "采用候选改进",
              revoke: "撤销生效",
            }[action],
          ))
        )
          return;
        try {
          await api(`/api/improvements/${iid}/${action}`, { method: "POST" });
          improveData = await api("/api/improvements");
          qualityData.improvements = improveData;
          imOpenItem(iid);
        } catch (e) {
          toast(e.message, true);
        }
      });
    if (quiet) {
      $("im-main")
        .querySelectorAll("details")
        .forEach((d) => {
          if (openDetails.includes(d.querySelector("summary")?.textContent))
            d.open = true;
        });
      window.scrollTo(0, scrollY);
    }
    if (["validating", "analyzing"].includes(item.status))
      improvePoll = setTimeout(() => {
        if (location.hash === "#/workbench" && improveSelection === `i${iid}`)
          imOpenItem(iid, true);
      }, 3500);
  } catch (e) {
    toast(e.message, true);
  }
}

function imValidationHtml(v, editable = false) {
  if (!v)
    return '<div class="im-empty">尚未验证。需要正例保护真实问题、负例检查误报，并至少有一条未参与起草的保护样本。</div>';
  const p = v.progress || {},
    labels = { baseline: "当前策略", candidate: "候选策略" };
  const completed = v.results.filter(
    (r) => r.baseline.result !== "not_run" && r.candidate.result !== "not_run",
  );
  const improved = completed.filter(
    (r) => r.baseline.result !== "hit" && r.candidate.result === "hit",
  ).length;
  const regressed = completed.filter(
    (r) =>
      r.baseline.result === "hit" &&
      ["miss", "fp", "error"].includes(r.candidate.result),
  ).length;
  const errors = v.results.filter(
    (r) => r.baseline.result === "error" || r.candidate.result === "error",
  ).length;
  return `<div class="im-validation-summary"><span class="im-status ${v.status === "running" ? "validating" : v.eligible ? "ready" : "needs_changes"}">${v.status === "running" ? "验证中" : v.status === "failed" ? "验证失败" : v.eligible ? "达到采用条件" : "未达到采用条件"}</span><span>改善 <b>${improved}</b></span><span>退化 <b>${regressed}</b></span><span>执行错误 <b>${errors}</b></span>${v.results.some((r) => r.baseline.reused_from_validation) ? `<span>基线复用 ${v.results.filter((r) => r.baseline.reused_from_validation).length} 个样本</span>` : ""}</div>
    ${!v.review_note ? '<p class="im-method-note">以下为自动匹配结果，尚未核对；标题变化可能造成假漏报，请展开原始输出确认。</p>' : ""}
    ${v.stale ? '<p class="im-warning">配置、样本或反馈已变化；此结果不能用于采用，请重新验证。</p>' : ""}
    ${v.review_note ? `<p class="im-review-note">✓ 已人工核对：${esc(v.review_note)}</p>` : ""}
    ${v.error ? `<p class="im-warning">${esc(v.error)}</p>` : ""}
    ${v.status === "running" ? `<div class="im-running"><div class="qa-progress"><i style="width:${Math.round(((p.done || 0) / (p.total || 1)) * 100)}%"></i></div><strong>${p.done || 0} / ${p.total || v.snapshot.cases.length} 条完成</strong><span>${labels[p.arm] || "准备"} · 样本 #${p.case_id || "—"} · ${esc(p.stage || "等待开始")}</span>${(p.trace || []).map((t) => `<small>${t.status === "ok" ? "✓" : t.status === "fail" ? "×" : t.status === "skip" ? "—" : "◌"} ${esc(t.name)}${t.part ? ` · 完成 ${t.part.done ?? 0}/${t.part.total} · 运行 ${(t.part.running || []).join("、") || "无"} · 排队 ${t.part.queued || 0}${t.part.failed?.length ? ` · 失败 ${t.part.failed.join("、")}` : ""}` : ""}</small>`).join("")}</div>` : ""}
    <div class="im-pairs">${v.results.map((r) => `<details class="im-pair"><summary><span class="im-sample-type">${r.polarity === "positive" ? "应报出" : "不应报"} · ${r.source ? "来源" : "保护"}</span><strong>${esc(r.expected.title)}</strong><span>${IMP_RESULT[r.baseline.result]} → ${IMP_RESULT[r.candidate.result]}</span></summary>${editable && v.status === "done" && !v.stale ? `<div style="padding:8px 12px"><button class="ghost small-btn" data-im-correct="${r.case_id}">纠正匹配判定</button>${r.human_judgement ? `<p class="small muted">人工纠正：${esc(r.human_judgement)}</p>` : ""}</div>` : ""}<p class="im-pair-expected">预期：${esc(r.expected.file)}:${r.expected.line || "—"} · ${esc(r.expected.condition || r.expected.title)}</p><div class="im-pair-columns">${["baseline", "candidate"].map((arm) => `<div><h4>${labels[arm]} · ${IMP_RESULT[r[arm].result]}</h4>${r[arm].machine_result ? `<small>原自动判定：${IMP_RESULT[r[arm].machine_result]}</small>` : ""}<p>${esc(r[arm].detail)}</p>${r[arm].reused_from_validation ? `<small>原验证 #${r[arm].reused_from_validation} · 原样本 #${r[arm].original_case_id} · ${fmtTime(r[arm].collected_at)}${r[arm].reused_from_improvement ? ` · 改进 #${r[arm].reused_from_improvement}` : ""}</small>` : ""}${r[arm].prior_human_judgement ? `<p class="small muted">沿用原样本核对依据：${esc(r[arm].prior_human_judgement)}</p>` : ""}${r[arm].continued_from_validation ? "<small>沿用本轮已完成侧；不是新的模型调用</small>" : ""}${imActualFindingsHtml(r[arm], r.expected)}<small>使用改进：${(r[arm].improvements || []).map((i) => `#${i.id} v${i.revision}`).join("、") || "无"}</small><details><summary>完整评审输出</summary>${(r[arm].reviews || []).map((out) => `<h4>${esc(out.name)}</h4><pre class="im-raw">${esc(out.output)}</pre>${out.evidence_id ? `<button class="ghost small-btn" data-im-evidence="${esc(out.evidence_id)}">查看工具轨迹与输入证据</button><div data-im-evidence-body></div>` : ""}`).join("") || "<p>本轮未产生评审输出</p>"}</details><details><summary>执行阶段</summary>${(r[arm].steps || []).map((s) => `<p class="small">${esc(s.status)} · ${esc(s.name)}</p>${s.command ? `<code>${esc(s.command)}</code>` : ""}${s.detail ? `<pre class="im-raw">${esc(s.detail)}</pre>` : ""}${s.trace?.length ? imAnalysisEvidenceHtml({ trace: s.trace }) : ""}`).join("")}</details></div>`).join("")}</div></details>`).join("") || '<p class="im-empty">逐样本对照结果将实时保存在这里。</p>'}</div>
    ${v.status === "done" && v.snapshot.adoption_policy === "all-hit-v1" ? '<p class="im-method-note">本轮最初使用旧规则 all-hit-v1；若要按增量规则复核，将另行记录规则变更，原始结果保留。</p>' : ""}
    ${v.status === "done" && !v.eligible ? '<p class="im-warning">采用要求：来源样本全部完成且至少一条改善；已有正确结果不退化；双方执行无错误。遗留问题继续列为待改进。</p>' : ""}
    <details class="im-snapshot"><summary>本轮输入快照 · #${v.id} / v${v.revision}</summary><pre class="im-raw">${esc(JSON.stringify(v.snapshot, null, 2))}</pre></details>
    <p class="im-method-note">当前匹配使用文件、相邻行号和标题相似度；对照不等于因果证明，单轮结果受模型波动影响。保护样本未参与本候选起草，不等于独立盲测集。</p>`;
}

function imMarkDialog(disposition) {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.className = "rh-dialog";
    d.setAttribute("aria-label", "记录人工判定");
    d.innerHTML = `<form class="rh-dialog-inner" id="im-mark-form"><div class="rh-dialog-title">${disposition === "confirmed" ? "确认真实问题" : "标记为误报"}</div><p>记录依据，自动保存反馈和可重放样本；后续在工作台形成改进并验证。</p><label class="small">判定原因<textarea name="note" required maxlength="3000" rows="3" style="display:block;width:100%;margin:8px 0 14px" placeholder="为什么是问题／为什么是误报？"></textarea></label><label class="small">证据线索（可稍后补充）<textarea name="evidence" maxlength="3000" rows="2" style="display:block;width:100%;margin-top:8px" placeholder="文件、函数、测试或业务约束"></textarea></label><div class="rh-dialog-actions"><button type="button" class="ghost" data-cancel>取消</button><button class="primary" type="submit">保存反馈</button></div></form>`;
    const prior = document.activeElement;
    const finish = (v) => {
      d.close();
      d.remove();
      prior?.focus();
      resolve(v);
    };
    d.querySelector("[data-cancel]").onclick = () => finish(null);
    d.addEventListener("cancel", (e) => {
      e.preventDefault();
      finish(null);
    });
    d.querySelector("form").onsubmit = (e) => {
      e.preventDefault();
      finish(Object.fromEntries(new FormData(e.target)));
    };
    document.body.appendChild(d);
    d.showModal();
    d.querySelector("textarea").focus();
  });
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-feedback-run]");
  if (!b) return;
  e.stopPropagation();
  try {
    const data = await api("/api/improvements");
    const f = data.feedback.find(
      (f) =>
        f.run_id === +b.dataset.feedbackRun && f.idx === +b.dataset.feedbackIdx,
    );
    if (!f) {
      toast("该反馈不在最近记录中，请到工作台检索", true);
      return;
    }
    improveSelection = `f${f.id}`;
    location.hash = "#/workbench";
  } catch (err) {
    toast(err.message, true);
  }
});

function imRefreshStats() {
  if (!improveData) return;
  const values = {
    待补充依据: improveData.feedback.filter((f) => f.active && !f.reason)
      .length,
    候选改进: improveData.items.filter(
      (i) => !["active", "revoked"].includes(i.status),
    ).length,
    已生效: improveData.items.filter((i) => i.effective).length,
  };
  document
    .querySelectorAll("[data-im-count]")
    .forEach((e) => (e.textContent = values[e.dataset.imCount] || 0));
}

async function imMissedDialog() {
  try {
    const project = await uiChoice(
      "为哪个项目补充漏报？",
      qualityData.projects
        .filter((p) => p.kind === "github")
        .map((p) => ({ value: String(p.id), label: p.name })),
    );
    if (!project) return;
    const runs = (
      await api(`/api/runs?project_id=${+project}&limit=30`)
    ).filter((r) => r.head_sha && !["running", "queued"].includes(r.status));
    if (!runs.length) {
      toast("该项目尚无可重放的已结束审查", true);
      return;
    }
    const run = await uiChoice(
      "选择问题出现时的原始审查",
      runs.map((r) => ({
        value: String(r.id),
        label: `#${r.id} · ${r.head_sha.slice(0, 8)} · ${r.commit_msg?.split("\n")[0] || r.status}`,
      })),
    );
    if (!run) return;
    const d = document.createElement("dialog");
    d.className = "rh-dialog";
    d.setAttribute("aria-label", "补充AI漏掉的问题");
    d.innerHTML = `<form class="rh-dialog-inner"><div class="rh-dialog-title">补充 AI 漏掉的问题</div><p>关联原审查 #${+run}，保存为应报出的正例。不会修改原报告的 AI 输出。</p>${[
      ["title", "问题标题", 160],
      ["file", "仓库相对文件路径", 500],
      ["line", "行号", null],
      ["condition", "触发条件", 3000],
      ["reason", "为什么确定是遗漏的真实问题？", 3000],
      ["evidence", "核验证据或测试", 3000],
    ]
      .map(
        ([key, label, max]) =>
          `<label style="display:block;font-size:12px;margin-top:12px">${label}${key === "line" ? '<input type="number" name="line" min="1" required style="width:100%;margin-top:5px">' : `<textarea name="${key}" rows="${key === "title" || key === "file" ? 1 : 2}" required maxlength="${max}" style="width:100%;margin-top:5px"></textarea>`}</label>`,
      )
      .join(
        "",
      )}<div class="rh-dialog-actions"><button type="button" class="ghost" data-cancel>取消</button><button class="primary" type="submit">保存漏报反馈</button></div></form>`;
    const close = () => {
      d.close();
      d.remove();
    };
    d.querySelector("[data-cancel]").onclick = close;
    d.addEventListener("cancel", (e) => {
      e.preventDefault();
      close();
    });
    d.querySelector("form").onsubmit = async (e) => {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(e.target));
      body.line = +body.line;
      body.run_id = +run;
      try {
        const saved = await api("/api/feedback/missed", {
          method: "POST",
          body,
        });
        close();
        improveData = await api("/api/improvements");
        qualityData.improvements = improveData;
        imOpenFeedback(saved.feedback_id);
        toast("漏报已作为正例保存，可继续形成候选改进");
      } catch (err) {
        toast(err.message, true);
      }
    };
    document.body.appendChild(d);
    d.showModal();
    d.querySelector("textarea").focus();
  } catch (e) {
    toast(e.message, true);
  }
}

function imAnalysisEvidenceHtml(detail) {
  if (!detail.output && !detail.trace?.length && !detail.prompt) return "";
  return `<details><summary class="small">模型核查原文与工具轨迹</summary>${detail.output ? `<pre class="im-raw">${esc(detail.output)}</pre>` : ""}${(detail.trace || []).map((t) => `<p class="small">${esc(t.name || t.t)} · ${esc(t.detail || t.text || "")}</p>`).join("")}${detail.prompt ? `<details><summary class="small">本次核查输入</summary><pre class="im-raw">${esc(detail.prompt)}</pre></details>` : ""}</details>`;
}

function imRemainingHtml(v) {
  if (!v || v.status !== "done") return "";
  const unresolved = v.results.filter((r) =>
    ["miss", "fp"].includes(r.candidate.result),
  );
  if (!unresolved.length) return "";
  return `<div class="im-warning"><b>仍有 ${unresolved.length} 个已知样本待改进</b><p>采用一项有效改进不会把这些问题标为解决。逐项核对后，可继续建立下一项改进。</p>${unresolved
    .map((r) => {
      const c = v.snapshot.cases.find((c) => c.id === r.case_id);
      const f = improveData.feedback.find(
        (f) => f.run_id === c?.run_id && f.idx === c?.idx,
      );
      return `<div class="im-remaining-row"><span>${esc(r.expected.title)}</span>${f ? `<button class="ghost small-btn" data-im-remaining="${f.id}">继续改进 →</button>` : ""}</div>`;
    })
    .join("")}</div>`;
}

function imActualFindingsHtml(result, expected) {
  const actual = result.actual || [];
  if (!actual.length) return '<p class="muted">没有发现</p>';
  const related = [],
    other = [];
  for (const a of actual) {
    const near =
      a.file === expected.file &&
      Math.abs((+a.line || 0) - (+expected.line || 0)) <= 30;
    (near ? related : other).push(a);
  }
  const render = (items) =>
    items
      .map(
        (a) =>
          `<article><b>${esc(a.title)}</b><code>${esc(a.file)}:${esc(a.line)}</code><p>${esc(a.condition || "")}</p>${a.fix ? `<details><summary>建议修法</summary><p>${esc(a.fix)}</p></details>` : ""}</article>`,
      )
      .join("");
  return `<small>同文件相邻位置 · ${related.length} 条（需核对是否同一问题）</small>${related.length ? render(related) : '<p class="muted">没有同位置发现，可展开其他发现核对语义。</p>'}${other.length ? `<details><summary>其他发现 · ${other.length} 条 / 全部可查看</summary>${render(other)}</details>` : ""}`;
}

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-im-evidence]");
  if (!b) return;
  b.disabled = true;
  b.textContent = "读取证据…";
  try {
    const evidence = await api(`/api/review-evidence/${b.dataset.imEvidence}`);
    b.nextElementSibling.innerHTML = imAnalysisEvidenceHtml({
      trace: evidence.trace,
      prompt: evidence.prompt,
      output: "",
    });
    const box = b.nextElementSibling.querySelector("details");
    if (box) box.open = true;
    b.remove();
  } catch (err) {
    b.disabled = false;
    b.textContent = "重试读取证据";
    toast(err.message, true);
  }
});
