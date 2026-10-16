"use strict";
// 原生 DOM 渲染：来源正文只写 textContent，不解释为 HTML。
const $ = (id) => document.getElementById(id);
const labels = {
  candidate: "候选 · 未生效",
  published: "已发布",
  withdrawn: "已撤回",
  superseded: "已被新版替代",
  error: "需检查",
  pending: "待核验",
  confirmed: "已确认",
  passed: "评测通过",
  failed: "未通过",
  invalidated: "来源失效",
  quantity: "数量关联",
  boundary: "条款边界",
  association: "条款关系",
  missing_product: "产品漏抽",
  needs_review: "需要确认",
  merged: "已合并",
  rejected: "已拒绝",
};
const state = {
  section: "memories",
  runTab: "runs",
  items: [],
  selected: null,
  detail: null,
  tab: "rule",
  token: null,
  readOnly: true,
  overview: null,
  hasMore: false,
  total: 0,
  request: 0,
  detailRequest: 0,
  cursor: 0,
};
function el(tag, cls, text, ...children) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== null && text !== undefined) n.textContent = String(text);
  n.append(...children.filter(Boolean));
  return n;
}
function button(
  text,
  action,
  cls = "button secondary small",
  disabled = false,
) {
  const n = el("button", cls, text);
  n.type = "button";
  n.disabled = disabled;
  n.addEventListener("click", () =>
    Promise.resolve()
      .then(action)
      .catch((error) => toast(error.message, true)),
  );
  return n;
}
function badge(value, text) {
  return el("span", `badge ${value}`, text || labels[value] || value);
}
function pretty(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
function date(value) {
  if (!value) return "时间未记录";
  const d = new Date(value);
  return Number.isNaN(d.valueOf())
    ? value
    : d.toLocaleString("zh-CN", { hour12: false });
}
function field(label, value, emphasis = false) {
  return el(
    "section",
    "field-block",
    null,
    el("h3", "field-label", label),
    el("div", `field-content${emphasis ? " emphasis" : ""}`, value || "未填写"),
  );
}
function note(title, text, kind = "") {
  return el(
    "div",
    `callout ${kind}`,
    null,
    el("h3", null, title),
    el("p", null, text),
  );
}
function empty(title, text) {
  return el(
    "div",
    "empty-state",
    null,
    el("span", "empty-icon", "◇"),
    el("h2", null, title),
    el("p", null, text),
  );
}
function jsonView(title, value, opened = false) {
  const n = el(
    "details",
    "details-advanced",
    null,
    el("summary", null, title),
    el("pre", "json-block", pretty(value)),
  );
  n.open = opened;
  return n;
}
function metadata(values) {
  const n = el("dl", "metadata");
  for (const [k, v] of Object.entries(values))
    n.append(el("dt", null, k), el("dd", null, v ?? "未记录"));
  return n;
}
async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? { cache: "no-store" }
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": state.token,
          },
          body: JSON.stringify(body),
        },
  );
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}
let toastTimer;
function toast(text, error = false) {
  clearTimeout(toastTimer);
  $("toast").replaceChildren(el("span", null, text));
  $("toast").className = `toast${error ? " error" : ""}`;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => ($("toast").hidden = true), 10000);
}
function dialog(title, body) {
  $("dialog-title").textContent = title;
  $("dialog-body").replaceChildren(body);
  if (!$("action-dialog").open) $("action-dialog").showModal();
}
$("dialog-close").addEventListener("click", () => $("action-dialog").close());
async function overview() {
  const o = await api("/api/overview");
  state.overview = o;
  for (const [id, key] of [
    ["published", "published"],
    ["candidate", "candidate"],
    ["feedback", "pending_reviews"],
    ["runs", "runs"],
  ])
    $("count-" + id).textContent = o.counts[key];
  $("nav-memory-count").textContent =
    o.counts.memories ?? o.counts.published + o.counts.candidate + o.counts.withdrawn;
  $("overview-notice").replaceChildren(
    el("span", "notice-icon", "i"),
    el(
      "div",
      null,
      null,
      el(
        "strong",
        null,
        o.counts.published
          ? `${o.counts.published} 条经验已发布，后续新任务可按需召回`
          : `当前没有已发布经验 · ${o.counts.candidate} 条候选尚未生效`,
      ),
      el(
        "p",
        null,
        "观察 → 候选 → 标签验证与独立回归 → 自动发布。没有标签或验证案例不足时保留候选，疑点进入待确认事项。",
      ),
    ),
  );
  $("project-label").textContent = o.project.path;
  $("project-label").title = `经验库：${o.project.store_dir}`;
  const changes = o.policy_changed_runs || [];
  $("policy-alert").hidden = !changes.length;
  $("policy-alert").replaceChildren(
    el(
      "span",
      null,
      `${changes.length} 次历史运行使用的业务口径与当前不同。版本变化不等于自动判错。`,
    ),
    button("查看口径变化", () => navigate("policy"), "text-button"),
  );
}
const sections = {
  learning: ["抽取与学习", "选择纯抽取或标签学习，追踪盲抽、比较、经验验证和生效。", "学习任务"],
  reviews: ["待确认事项", "只处理程序无法确定的差异和冲突；答复保留适用范围与审计记录。", "待确认事项"],
  memories: [
    "经验库",
    "查看候选、核验来源，让经验在发布后用于下一篇。",
    "全部经验",
  ],
  runs: [
    "运行与反馈",
    "追溯每条观察的原始结果、用户原话与核验依据。",
    "运行记录",
  ],
  policy: [
    "固定业务口径",
    "查看技能中的当前业务规则，以及真实的历史文本变化。",
    "口径版本",
  ],
  history: [
    "更新历史",
    "核对保存、修订、确认、发布和撤回分别发生了什么。",
    "维护事件",
  ],
};
async function navigate(section, options = {}) {
  state.section = section;
  state.selected = options.id || null;
  state.items = [];
  state.tab = "rule";
  state.request++;
  state.detailRequest++;
  if (options.runTab) state.runTab = options.runTab;
  $("search-input").value = "";
  if (options.filter) $("state-filter").value = options.filter;
  else $("state-filter").value = "all";
  for (const n of document.querySelectorAll(".nav-item")) {
    const on = n.dataset.section === section;
    n.classList.toggle("active", on);
    if (on) n.setAttribute("aria-current", "page");
    else n.removeAttribute("aria-current");
  }
  const info = sections[section];
  $("page-title").textContent = info[0];
  $("page-description").textContent = info[1];
  $("list-title").textContent = info[2];
  $("run-tabs").hidden = section !== "runs";
  $("state-filter-wrap").hidden = section !== "memories";
  $("search-form").hidden = ["policy", "learning", "reviews"].includes(section);
  $("new-memory-button").hidden = section !== "memories";
  $("new-memory-button").disabled = state.readOnly;
  for (const n of document.querySelectorAll("[data-run-tab]"))
    n.classList.toggle("selected", n.dataset.runTab === state.runTab);
  await loadList();
}
function itemId(item) {
  if (item.kind && item.created_at)
    return `${item.kind}:${item.evaluation_id || item.memory_id || item.source_id || ""}:${item.revision || item.created_at}`;
  return item.memory_id || item.feedback_id || item.run_id || item.key;
}
function card(item) {
  const id = itemId(item);
  let title, preview, category, status, meta;
  if (state.section === "memories") {
    title = item.title;
    preview = item.rule_preview;
    category = labels[item.issue_type] || "经验规则";
    status = item.state;
    meta = `v${item.latest_revision} · ${item.source_count} 个来源`;
  } else if (state.section === "runs") {
    const feedback = state.runTab === "feedback";
    title = feedback
      ? "用户反馈"
      : item.input_path?.split(/[\\/]/).pop() || "抽取运行";
    preview = feedback
      ? item.feedback_text
      : item.no_experience_reason || item.status || "查看抽取结果与复盘";
    category = feedback ? "纠错来源" : "运行来源";
    status = item.error ? "error" : item.confirmed ? "confirmed" : "pending";
    meta = feedback
      ? item.has_corrected_result
        ? "已有修正稿"
        : "缺完整修正稿"
      : `${(item.candidates || []).length} 条候选`;
  } else {
    title = item.title;
    preview = item.summary;
    category =
      {
        revision: "经验修订",
        feedback: "用户反馈",
        confirmation: "来源确认",
        publish: "发布",
        withdraw: "撤回",
        run: "抽取运行",
        evaluation: "离线评测",
      }[item.kind] || "固定口径";
    status = null;
    meta = state.section === "policy" ? "技能文件" : "已留存";
  }
  const n = button(
    "",
    () => select(item),
    `record-card${state.selected === id ? " selected" : ""}`,
  );
  n.append(
    el(
      "div",
      "record-topline",
      null,
      el("span", "record-category", category),
      status ? badge(status) : null,
    ),
    el("h3", "record-title", title),
    el("p", "record-preview", preview),
    el(
      "div",
      "record-meta",
      null,
      el("span", null, meta),
      el(
        "span",
        null,
        state.section === "policy" ? "本地文件" : date(item.created_at),
      ),
    ),
  );
  if (item.active_revision)
    n.append(
      el(
        "p",
        "record-active",
        `当前生效 v${item.active_revision}${item.active_revision !== item.latest_revision ? " · 新候选待验证" : ""}`,
      ),
    );
  return n;
}
async function loadList(append = false) {
  if (["learning", "reviews"].includes(state.section)) {
    return window.TermEvolution.load();
  }
  const ticket = ++state.request;
  state.detailRequest++;
  $("record-list").setAttribute("aria-busy", "true");
  if (!append)
    $("record-list").replaceChildren(
      el("div", "loading-state", "正在读取记录…"),
    );
  try {
    let data;
    const q = $("search-input").value.trim();
    if (state.section === "policy")
      data = {
        items: [
          {
            key: "current",
            title: "当前业务口径",
            summary: "查看完整业务范围和抽取规则",
          },
          {
            key: "diff",
            title: "与上一版的变化",
            summary: "来自本地 Git 的真实文本差异",
          },
        ],
        total: 2,
        has_more: false,
      };
    else {
      const resource = state.section === "runs" ? state.runTab : state.section;
      const params = new URLSearchParams({
        limit: "50",
        offset: String(append ? state.cursor : 0),
      });
      if (resource === "memories") {
        params.set("q", q);
        params.set("state", $("state-filter").value);
      }
      data = await api(`/api/${resource}?${params}`);
      data.nextOffset = (append ? state.cursor : 0) + data.items.length;
      if (resource !== "memories" && q)
        data.items = data.items.filter((x) =>
          pretty(x).toLowerCase().includes(q.toLowerCase()),
        );
    }
    if (ticket !== state.request) return;
    state.items = append ? state.items.concat(data.items) : data.items;
    state.cursor = data.nextOffset || 0;
    state.total = data.total;
    state.hasMore = data.has_more;
    $("list-total").textContent = `${data.total} 条记录`;
    $("list-title").textContent =
      state.section === "runs"
        ? state.runTab === "feedback"
          ? "用户反馈"
          : "运行记录"
        : sections[state.section][2];
    $("record-list").replaceChildren(
      ...(state.items.length
        ? state.items.map(card)
        : [
            el(
              "div",
              "list-empty",
              "没有符合条件的记录。候选只在真实抽取复盘后产生。看不到刚保存的记录时可刷新。",
            ),
          ]),
    );
    $("load-more-button").hidden = !data.has_more;
    $("list-footer-note").textContent =
      state.section !== "memories" && state.section !== "policy" && q
        ? "搜索限已加载页；可加载更多继续查找"
        : "来自本地私有经验库";
    const chosen =
      state.items.find((x) => itemId(x) === state.selected) || state.items[0];
    if (chosen) await select(chosen, false);
    else
      $("detail-panel").replaceChildren(
        empty("暂无记录", "换一个筛选条件，或完成一篇抽取后刷新查看。"),
      );
  } catch (e) {
    if (ticket === state.request) {
      $("record-list").replaceChildren(
        el("div", "list-empty", e.message),
        button("重试", () => loadList()),
      );
      $("detail-panel").replaceChildren(empty("读取失败", e.message));
    }
    throw e;
  } finally {
    if (ticket === state.request)
      $("record-list").setAttribute("aria-busy", "false");
  }
}
async function select(item, scroll = false) {
  state.selected = itemId(item);
  for (const [i, n] of [...$("record-list").children].entries())
    n.classList.toggle(
      "selected",
      itemId(state.items[i] || {}) === state.selected,
    );
  const ticket = ++state.detailRequest;
  $("detail-panel").setAttribute("aria-busy", "true");
  try {
    let body;
    if (state.section === "memories") {
      const data = await api(
        `/api/memories/${encodeURIComponent(item.memory_id)}`,
      );
      if (ticket !== state.detailRequest) return;
      state.detail = data;
      body = memoryView(data);
    } else if (state.section === "runs") {
      if (state.runTab === "feedback") {
        const data = await api(
          `/api/sources/${encodeURIComponent(item.source_id)}`,
        );
        if (ticket !== state.detailRequest) return;
        body = sourceView(data);
      } else {
        const data = await api(`/api/runs/${encodeURIComponent(item.run_id)}`);
        if (ticket !== state.detailRequest) return;
        body = runView(data);
      }
    } else if (state.section === "policy") {
      const data = await api("/api/policy");
      if (ticket !== state.detailRequest) return;
      body = policyView(data, item.key);
    } else {
      body = el(
        "div",
        null,
        null,
        header(item.title, item.kind, date(item.created_at)),
        field("记录内容", item.summary),
      );
      if (item.memory_id)
        body.append(
          button("打开经验修订", () =>
            navigate("memories", { id: item.memory_id }),
          ),
        );
      if (item.source_id)
        body.append(button("查看来源", () => openSource(item.source_id)));
      if (item.evaluation_id)
        body.append(
          button("查看完整评测报告", () => openEvaluation(item.evaluation_id)),
        );
    }
    if (ticket !== state.detailRequest) return;
    $("detail-panel").replaceChildren(body);
    if (scroll)
      $("detail-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    if (ticket === state.detailRequest)
      $("detail-panel").replaceChildren(empty("详情无法读取", e.message));
    throw e;
  } finally {
    if (ticket === state.detailRequest)
      $("detail-panel").setAttribute("aria-busy", "false");
  }
}
function header(title, kicker, subtitle) {
  return el(
    "div",
    "detail-header",
    null,
    el(
      "div",
      "detail-heading",
      null,
      el("div", "detail-kicker", kicker),
      el("h2", null, title),
      el("div", "detail-subtitle mono", subtitle),
    ),
  );
}
function memoryView(d) {
  const m = d.memory,
    p = m.payload;
  const root = el(
    "div",
    null,
    null,
    header(
      p.title,
      `${labels[p.issue_type]} / ${labels[m.state] || m.state}`,
      m.memory_id,
    ),
  );
  const versions = el("select", "version-select");
  versions.setAttribute("aria-label", "查看经验修订");
  for (const v of d.versions) {
    const o = el(
      "option",
      null,
      `v${v.revision} · ${labels[v.state] || v.state}`,
    );
    o.value = v.revision;
    o.selected = v.revision === m.revision;
    versions.append(o);
  }
  versions.addEventListener("change", async () => {
    try {
      const value = await api(
        `/api/memories/${m.memory_id}?revision=${versions.value}`,
      );
      state.detail = value;
      $("detail-panel").replaceChildren(memoryView(value));
    } catch (e) {
      toast(e.message, true);
    }
  });
  root.firstChild.append(versions);
  root.append(
    el(
      "div",
      "detail-actions",
      null,
      button(
        "编辑为新修订",
        () => editMemory(d),
        "button secondary small",
        state.readOnly,
      ),
      button("查看来源", () => {
        state.tab = "sources";
        $("detail-panel").replaceChildren(memoryView(d));
      }),
      button("评测与发布", () => {
        state.tab = "evaluation";
        $("detail-panel").replaceChildren(memoryView(d));
      }),
      button("进入标签学习", () => navigate("learning")),
    ),
  );
  if (d.active_revision)
    root.append(
      note(
        `当前生效 v${d.active_revision}`,
        d.active_revision !== m.revision
          ? `当前查看 v${m.revision}。新修订尚未发布时，后续新任务仍使用 v${d.active_revision}。`
          : "该版本可被之后新注册的任务按需召回；已注册任务仍保留原快照。",
        "success",
      ),
    );
  else
    root.append(
      note(
        "尚未生效",
        d.blockers.map((b) => b.message).join("\n") ||
          "该经验没有当前发布版。完成来源核验、离线评测和人工发布后才可用于新任务。",
      ),
    );
  const tabs = el("div", "tabs");
  for (const [key, text] of [
    ["rule", "规则正文"],
    ["sources", `来源 (${d.sources.length})`],
    ["changes", "修订差异"],
    ["evaluation", "评测与发布"],
  ])
    tabs.append(
      button(
        text,
        () => {
          state.tab = key;
          $("detail-panel").replaceChildren(memoryView(d));
        },
        key === state.tab ? "active" : "",
      ),
    );
  root.append(tabs);
  const content = el("div", "tab-content");
  if (state.tab === "rule") {
    content.append(
      field("执行规则", p.rule, true),
      field("适用前提", p.applies_when),
      field("排除情况", p.exclusions),
      el(
        "section",
        "field-block",
        null,
        el("h3", "field-label", "召回触发词"),
        el(
          "div",
          "chips",
          null,
          ...p.triggers.map((t) => el("span", "chip", t)),
        ),
      ),
      metadata({ 创建时间: date(m.created_at), 内容摘要: m.content_sha256 }),
      note(
        "下一步",
        d.can_publish
          ? "已有可用评测。请审阅来源、规则边界和报告，再由维护者发布。"
          : d.blockers.map((b) => b.message).join("\n") ||
              "已发布，无需重复发布。",
      ),
    );
  } else if (state.tab === "sources") {
    if (!d.sources.length)
      content.append(
        note(
          "缺少来源",
          "编辑候选并关联本次运行或纠错反馈。来源必须真实，发布前需要人工核验。",
        ),
      );
    for (const s of d.sources) {
      const c = el(
        "div",
        "source-card",
        null,
        el(
          "div",
          "source-card-header",
          null,
          el("h3", null, s.title || "经验来源"),
          badge(
            s.invalidated
              ? "invalidated"
              : s.confirmed
                ? "confirmed"
                : "pending",
          ),
        ),
        el("p", "mono", s.source_id),
        el(
          "p",
          null,
          s.error ||
            s.feedback_text ||
            (s.kind === "success"
              ? "运行结果作为成功来源，须核对全文和结果后确认。"
              : "反馈修正结果作为纠错来源。"),
        ),
        button("查看依据与核验", () => openSource(s.source_id), "text-button"),
      );
      content.append(c);
    }
  } else if (state.tab === "changes") {
    if (!d.changes.length)
      content.append(
        note(
          "首次修订",
          "没有更早的规则正文可比较。本次仅保存候选，还未发布。",
        ),
      );
    for (const c of d.changes) {
      const grid = el(
        "div",
        "diff-grid",
        null,
        el(
          "div",
          "diff-side",
          null,
          el("h4", "field-label", "修改前"),
          el("pre", null, pretty(c.before)),
        ),
        el(
          "div",
          "diff-side",
          null,
          el("h4", "field-label", "修改后"),
          el("pre", null, pretty(c.after)),
        ),
      );
      content.append(
        el(
          "section",
          "diff-item",
          null,
          el(
            "h3",
            null,
            {
              rule: "执行规则",
              title: "标题",
              applies_when: "适用前提",
              exclusions: "排除情况",
              triggers: "触发词",
              source_ids: "来源",
            }[c.field] || c.field,
          ),
          grid,
        ),
      );
    }
    for (const a of d.audit)
      content.append(
        el(
          "div",
          "event-card",
          null,
          el(
            "h3",
            "event-kind",
            `${a.action === "publish" ? "发布" : "撤回"} · ${date(a.created_at)}`,
          ),
          el("p", null, `${a.reviewer}：${a.note}`),
        ),
      );
  } else {
    content.append(evaluationView(d));
  }
  root.append(content);
  return root;
}
function evaluationView(d) {
  const root = el("div");
  if (d.blockers.length)
    root.append(
      note("未完成事项", d.blockers.map((b) => "• " + b.message).join("\n")),
    );
  for (const e of d.evaluations) {
    const c = el(
      "section",
      "evaluation-card",
      null,
      el(
        "div",
        "evaluation-header",
        null,
        el(
          "h3",
          null,
          `${e.evaluation_mode === "success" ? "成功经验保护" : "纠错改善"}评测`,
        ),
        badge(e.status),
      ),
      el("p", "mono", e.evaluation_id),
      el(
        "p",
        "inline-note",
        e.usable ? "报告当前可用于人工发布。" : e.blocking_reason,
      ),
      jsonView("查看门槛统计", e.summary, true),
      button(
        "查看完整评测报告",
        () => openEvaluation(e.evaluation_id),
        "text-button",
      ),
    );
    root.append(c);
  }
  const form = el(
    "form",
    "form-card",
    null,
    el("h3", null, "执行离线评测"),
    el(
      "p",
      null,
      "先准备真实 baseline、candidate 与人工断言清单。这里检查已有工件，不运行抽取模型。",
    ),
  );
  const manifest = inputField(
    form,
    "manifest_path",
    "评测清单在本机的路径",
    "",
    "text",
    true,
  );
  manifest.placeholder = "D:\\私有实验\\manifest.json";
  submitForm(form, "执行评测", async () => {
    const r = await api("/api/evaluate", {
      memory_id: d.memory.memory_id,
      revision: d.memory.revision,
      manifest_path: manifest.value,
    });
    toast(
      `评测${r.status === "passed" ? "通过" : "未通过"}；请审阅报告后决定是否发布。`,
    );
    await refresh();
  });
  root.append(
    form,
    button(
      "审阅并发布此版本",
      () => reviewAction("publish", d),
      "button",
      state.readOnly || !d.can_publish,
    ),
  );
  if (d.can_withdraw)
    root.append(
      button(
        `撤回当前生效 v${d.active_revision}`,
        () => reviewAction("withdraw", d),
        "button danger",
        state.readOnly,
      ),
    );
  root.append(
    el(
      "p",
      "action-help",
      "发布只影响之后新注册的任务。来源确认、离线通过与人工发布分别记录，页面不会自动批准。",
    ),
  );
  return root;
}
async function openEvaluation(id) {
  const data = await api(`/api/evaluations/${encodeURIComponent(id)}`);
  dialog(
    "完整离线评测报告",
    el(
      "div",
      null,
      null,
      note(
        "核对案例与边界",
        "报告只验证给定工件的人工断言，不自动认证模型输出来源或全面业务正确性。发布前请审阅案例、退化项、反例与生成方式。",
      ),
      el("pre", "json-block", pretty(data.report)),
    ),
  );
}
function inputField(
  form,
  name,
  label,
  value = "",
  type = "text",
  required = false,
) {
  const wrapper = el("div", "form-field");
  const id = `f-${name}-${Math.random().toString(36).slice(2)}`;
  const l = el("label", null, label);
  l.htmlFor = id;
  const n = el(type === "textarea" ? "textarea" : "input");
  if (type !== "textarea") n.type = type;
  n.id = id;
  n.name = name;
  n.value = value ?? "";
  n.required = required;
  n.disabled = state.readOnly;
  wrapper.append(l, n);
  form.append(wrapper);
  return n;
}
function submitForm(form, text, action) {
  const error = el("p", "form-error");
  error.hidden = true;
  const submit = el("button", "button", text);
  submit.type = "submit";
  submit.disabled = state.readOnly;
  form.append(error, el("div", "form-actions", null, submit));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.readOnly) return;
    error.hidden = true;
    submit.disabled = true;
    try {
      await action();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
    } finally {
      submit.disabled = state.readOnly;
    }
  });
}
function acknowledgement(form, text) {
  const label = el("label", "check-field");
  const input = el("input");
  input.type = "checkbox";
  input.required = true;
  input.disabled = state.readOnly;
  label.append(input, el("span", null, text));
  form.append(label);
  return input;
}
async function editMemory(d = null) {
  const p = d?.memory.payload || {
    title: "",
    issue_type: "boundary",
    triggers: [],
    applies_when: "",
    rule: "",
    exclusions: "",
    source_ids: [],
    correction_ids: [],
  };
  const form = el(
    "form",
    null,
    null,
    note(
      d ? "保存为新候选修订" : "新建未验证候选",
      "不会覆盖旧版或改变当前发布集合；规则、来源和评测要分别核验。",
    ),
  );
  const title = inputField(form, "title", "经验标题", p.title, "text", true);
  const typeWrap = el("div", "form-field", null, el("label", null, "问题类型"));
  const type = el("select");
  type.setAttribute("aria-label", "问题类型");
  type.disabled = state.readOnly;
  for (const v of ["missing_product", "boundary", "quantity", "association"]) {
    const o = el("option", null, labels[v]);
    o.value = v;
    o.selected = p.issue_type === v;
    type.append(o);
  }
  typeWrap.append(type);
  form.append(typeWrap);
  const triggers = inputField(
      form,
      "triggers",
      "触发词（每行一个）",
      p.triggers.join("\n"),
      "textarea",
      true,
    ),
    when = inputField(
      form,
      "applies_when",
      "适用前提",
      p.applies_when,
      "textarea",
      true,
    ),
    rule = inputField(form, "rule", "具体执行规则", p.rule, "textarea", true),
    exclusions = inputField(
      form,
      "exclusions",
      "不适用或禁止的情形",
      p.exclusions,
      "textarea",
      true,
    );
  const sources = inputField(
    form,
    "source_ids",
    "关联来源（每行一个，可从下方选择）",
    (p.source_ids || []).join("\n"),
    "textarea",
  );
  const choices = el(
    "div",
    "source-choices",
    null,
    el("p", "field-hint", "正在读取可关联的运行与反馈…"),
  );
  form.append(choices);
  const split = (n) =>
    n.value
      .split(/\r?\n/)
      .map((x) => x.trim())
      .filter(Boolean);
  submitForm(form, "保存候选修订", async () => {
    const payload = {
      title: title.value,
      issue_type: type.value,
      triggers: split(triggers),
      applies_when: when.value,
      rule: rule.value,
      exclusions: exclusions.value,
      source_ids: split(sources),
      correction_ids: p.correction_ids || [],
    };
    const body = { payload };
    if (d) {
      body.memory_id = d.memory.memory_id;
      body.expected_revision = Math.max(...d.versions.map((x) => x.revision));
    }
    const result = await api("/api/memories", body);
    $("action-dialog").close();
    toast(`已保存候选 v${result.revision}，尚未生效。请核验来源并完成评测。`);
    await overview();
    await navigate("memories", { id: result.memory_id });
  });
  dialog(d ? "编辑经验 · 创建新修订" : "新建经验候选", form);
  try {
    const [runs, feedback] = await Promise.all([
      api("/api/runs?limit=200"),
      api("/api/feedback?limit=200"),
    ]);
    choices.replaceChildren(
      el(
        "p",
        "field-hint",
        "选择来源会追加到上方列表。未确认来源可暂存，发布前须核验。",
      ),
    );
    for (const item of [...feedback.items, ...runs.items]) {
      const id = item.feedback_id || item.run_id;
      choices.append(
        button(
          `${item.feedback_id ? "反馈" : "运行"} · ${(item.feedback_text || item.input_path || id).slice(0, 65)}`,
          () => {
            const values = new Set(split(sources));
            values.add(id);
            sources.value = [...values].join("\n");
          },
          "text-button",
          state.readOnly,
        ),
      );
    }
  } catch (e) {
    choices.replaceChildren(el("p", "field-hint", e.message));
  }
}
function reviewAction(action, d) {
  const publish = action === "publish";
  const form = el(
    "form",
    null,
    null,
    note(
      publish ? "将发布此经验版本" : "将撤回当前发布版",
      publish
        ? `发布 v${d.memory.revision}，后端会重新核对来源、评测与文件。之后新任务才会使用。`
        : `撤回 v${d.active_revision}，保留历史，不恢复旧版；已注册任务继续使用其快照。`,
    ),
  );
  const reviewer = inputField(form, "reviewer", "维护者姓名", "", "text", true),
    reason = inputField(form, "note", "审核说明", "", "textarea", true);
  const ack = acknowledgement(
    form,
    "我已审阅规则、来源及评测边界，并确认执行此操作。审核姓名用于留痕，不是登录认证。",
  );
  submitForm(form, publish ? "确认发布" : "确认撤回", async () => {
    const body = {
      memory_id: d.memory.memory_id,
      reviewer: reviewer.value,
      note: reason.value,
      acknowledged: ack.checked,
    };
    if (publish)
      Object.assign(body, {
        revision: d.memory.revision,
        evaluation_id: d.publish_evaluation_id,
      });
    else body.expected_revision = d.active_revision;
    await api(`/api/${action}`, body);
    $("action-dialog").close();
    toast(
      publish
        ? "经验已发布，之后新注册任务可按需召回。"
        : "经验已撤回，历史和已注册任务的快照保留。",
    );
    await refresh();
  });
  dialog(publish ? "人工审阅与发布" : "撤回当前经验", form);
}
async function openSource(id) {
  dialog("读取来源", el("p", null, "正在读取真实来源与证据…"));
  try {
    const d = await api(`/api/sources/${encodeURIComponent(id)}`);
    dialog("来源依据与人工核验", sourceView(d));
  } catch (e) {
    dialog("来源读取失败", note("暂时无法读取", e.message, "error"));
  }
}
function resultView(title, value) {
  const root = el(
    "section",
    "field-block",
    null,
    el("h3", "field-label", title),
  );
  if (!Array.isArray(value)) {
    root.append(el("p", "inline-note", "未提供完整结果。"));
    return root;
  }
  root.append(
    el(
      "p",
      "result-summary",
      `${value.length} 条记录 · 核验时还需对照完整原文，表格摘要不代表全部参数。`,
    ),
  );
  const table = el("table", "result-table"),
    head = el("tr");
  for (const v of ["条款 / 设备", "数量", "定位 / 关系"])
    head.append(el("th", null, v));
  table.append(el("thead", null, null, head));
  const body = el("tbody");
  for (const row of value)
    body.append(
      el(
        "tr",
        null,
        null,
        el("td", null, `${row.id} · ${row["条款标题"]}`),
        el("td", null, row["产品数量"]),
        el(
          "td",
          null,
          `${(row["定位标识"] || []).join(" – ")} · ${row["条款分类"]}${row["相关条款"]?.length ? " → " + row["相关条款"].join(", ") : ""}`,
        ),
      ),
    );
  table.append(body);
  root.append(
    el("div", "table-scroll", null, table),
    jsonView("查看完整 JSON", value),
  );
  return root;
}
function sourceView(d) {
  const s = d.source;
  const root = el(
    "div",
    null,
    null,
    header(
      s.title || "来源记录",
      s.kind === "success" ? "成功运行来源" : "纠错反馈来源",
      s.source_id,
    ),
    badge(
      s.invalidated ? "invalidated" : s.confirmed ? "confirmed" : "pending",
    ),
  );
  if (s.error) root.append(note("来源文件需要检查", s.error, "error"));
  if (s.feedback_text) root.append(field("用户原话", s.feedback_text, true));
  if (s.confirmation)
    root.append(
      metadata({
        确认人: s.confirmation.confirmed_by,
        文档家族: s.confirmation.family_id,
        确认说明: s.confirmation.note,
      }),
    );
  const insights = s.reflection_event?.reflection?.insights || [];
  for (const i of insights)
    root.append(
      el(
        "section",
        "observation",
        null,
        el("h4", null, i.title),
        el("p", null, i.observation),
        el(
          "div",
          "chips",
          null,
          ...(i.evidence_tags || []).map((t) => el("span", "chip", t)),
        ),
      ),
    );
  root.append(
    el(
      "div",
      "detail-actions",
      null,
      button(
        "核验并确认来源",
        () => confirmSource(s),
        "button small",
        state.readOnly || !s.can_confirm,
      ),
      s.kind === "correction"
        ? button(
            "补充完整修正稿",
            () => completeFeedback(s.source_id),
            "button secondary small",
            state.readOnly || s.confirmed,
          )
        : null,
    ),
  );
  if (!s.confirmed)
    root.append(
      note(
        "确认前需要核对",
        s.kind === "success"
          ? "请对照原文核验整个结果。已经发现遗漏或错误的运行应通过完整修正稿形成纠错来源，不要确认原结果正确。"
          : "只有实际不同且核验过的完整修正稿可以确认。补充稿会形成新的反馈来源，原记录保留。",
      ),
    );
  root.append(
    resultView(
      s.kind === "success" ? "本次抽取结果" : "原抽取结果",
      d.before_result,
    ),
  );
  if (s.kind !== "success")
    root.append(resultView("完整修正稿", d.after_result));
  root.append(
    el("h3", "field-label", "原文证据摘录"),
    el("p", "inline-note", d.excerpt_notice),
  );
  for (const snippet of d.snippets)
    root.append(
      el(
        "div",
        "snippet",
        null,
        el("span", "chip", snippet.tag),
        el(
          "span",
          "snippet-meta",
          snippet.truncated ? "已截断，请回全文核验" : "原文节选",
        ),
        el("p", null, snippet.text),
      ),
    );
  if (s.excerpt_error)
    root.append(note("摘录读取失败", s.excerpt_error, "error"));
  root.append(jsonView("来源元数据与归档路径", s));
  return root;
}
function confirmSource(s) {
  const form = el(
    "form",
    null,
    null,
    note(
      "确认的是整份来源结果",
      s.kind === "success"
        ? "请确认整篇结果已对照原文核验。已知错误的原结果应走纠错反馈。"
        : "请确认完整修正稿已对照原文核验；这不代表批准由其归纳出的全部经验。",
    ),
  );
  const reviewer = inputField(form, "reviewer", "人工确认人", "", "text", true),
    family = inputField(form, "family_id", "文档家族编号", "", "text", true),
    reason = inputField(form, "note", "核验说明", "", "textarea", true);
  form.append(
    el(
      "p",
      "field-hint",
      "译文、修订版和近重复模板使用同一家族编号，不能当作独立回归材料。",
    ),
  );
  const ack = acknowledgement(
    form,
    "我已核验完整原文与结果，确认该来源可用于经验评测。此操作不发布规则。",
  );
  submitForm(form, "确认来源", async () => {
    await api(`/api/sources/${encodeURIComponent(s.source_id)}/confirm`, {
      reviewer: reviewer.value,
      family_id: family.value,
      note: reason.value,
      acknowledged: ack.checked,
    });
    $("action-dialog").close();
    toast("来源已确认，经验尚需评测和人工发布。");
    await refresh();
  });
  dialog("人工核验来源", form);
}
function completeFeedback(id) {
  const form = el(
    "form",
    null,
    null,
    note(
      "补充完整七字段 JSON 数组",
      "上传完整修正后的结果，不是局部条款。服务保留旧反馈，新建带稿反馈；不会覆盖原 final，也不会直接发布经验。",
    ),
  );
  const file = inputField(
    form,
    "corrected_file",
    "选择完整修正稿 JSON 文件",
    "",
    "file",
    true,
  );
  file.accept = ".json,application/json";
  const preview = el("p", "file-preview", "尚未选择文件。");
  form.append(preview);
  let value = null;
  file.addEventListener("change", async () => {
    try {
      const f = file.files[0];
      if (!f) return;
      if (f.size > 2 * 1024 * 1024)
        throw new Error("文件超过 2 MiB，请核对是否选择了结果 JSON。");
      value = JSON.parse(await f.text());
      if (!Array.isArray(value)) throw new Error("修正稿必须为 JSON 数组。");
      preview.textContent = `已读取 ${value.length} 条记录。提交时会检查标签和七字段结构。`;
    } catch (e) {
      value = null;
      preview.textContent = e.message;
    }
  });
  submitForm(form, "保存为新的带稿反馈", async () => {
    if (!value) throw new Error("请先选择可解析的完整结果数组。");
    const r = await api(`/api/feedback/${encodeURIComponent(id)}/complete`, {
      after_result: value,
    });
    toast("已保存新的带稿反馈。原反馈保留，下一步核验并确认新来源。");
    await overview();
    await navigate("runs", { runTab: "feedback", id: r.new_feedback_id });
    $("action-dialog").close();
  });
  dialog("完善反馈 · 上传完整修正稿", form);
}
function runView(d) {
  const r = d.run;
  const root = el(
    "div",
    null,
    null,
    header(
      r.input_path?.split(/[\\/]/).pop() || "抽取运行",
      "不可变运行记录",
      r.run_id,
    ),
    metadata({
      提交时间: date(r.created_at),
      复盘状态: r.status,
      输入路径: r.input_path,
      使用模型: r.generation?.model_id || "未记录",
    }),
  );
  if (r.artifact_warning)
    root.append(note("归档检查提示", r.artifact_warning, "error"));
  const help = () =>
    dialog(
      "交给 OpenCode 整理候选",
      el(
        "div",
        null,
        null,
        note(
          "先审阅，再整理",
          "下面是可交给经验维护 agent 的任务。来源未确认时，不能声称已完成发布。",
        ),
        el(
          "pre",
          "json-block",
          `请读取来源 ${r.run_id}，核对复盘与当前固定业务口径，整理最小可迁移规则及适用/排除条件。保留真实 source_ids；列出来源确认、完整修正稿和回归评测的缺项。不要自行确认来源或发布。`,
        ),
      ),
    );
  root.append(
    el(
      "div",
      "detail-actions",
      null,
      button("查看来源与核验", () => openSource(r.run_id)),
      button("整理候选的操作提示", help),
    ),
  );
  for (const c of r.candidates || [])
    root.append(
      button(
        `查看候选 ${c.memory_id} · v${c.revision}`,
        () => navigate("memories", { id: c.memory_id }),
        "text-button",
      ),
    );
  root.append(
    resultView("本次提交结果", d.result),
    jsonView("证据与未解决事项", d.evidence),
    jsonView("运行元数据", r),
  );
  return root;
}
function policyView(d, key) {
  const root = el(
    "div",
    null,
    null,
    header(
      key === "diff" ? "业务口径变更" : "当前固定业务口径",
      "技能文件 · 独立于候选经验",
      d.path,
    ),
    note("业务口径与经验发布分别维护", d.notice),
  );
  if (key === "diff") {
    if (!d.history_available)
      root.append(
        empty(
          "没有可比较的历史版本",
          "当前 Git 历史没有不同的上一版正文，未构造变化。",
        ),
      );
    else {
      const changes = d.changes.filter((x) => x.type !== "unchanged");
      root.append(
        el(
          "p",
          "result-summary",
          `${changes.filter((x) => x.type === "added").length} 行新增 · ${changes.filter((x) => x.type === "removed").length} 行移除`,
        ),
      );
      const diff = el("div", "diff-document");
      for (const line of changes)
        diff.append(
          el(
            "span",
            `diff-line ${line.type}`,
            `${line.type === "added" ? "+" : line.type === "removed" ? "−" : " "} ${line.text}`,
          ),
        );
      root.append(diff);
    }
  } else
    root.append(
      el("pre", "policy-content", d.content || "没有找到业务口径文件。"),
    );
  return root;
}
async function refresh() {
  await overview();
  await loadList();
}
for (const n of document.querySelectorAll("[data-section]"))
  n.addEventListener("click", () =>
    navigate(n.dataset.section).catch((e) => toast(e.message, true)),
  );
for (const n of document.querySelectorAll("[data-stat]"))
  n.addEventListener("click", () => {
    const key = n.dataset.stat;
    const task =
      key === "feedback"
        ? navigate("reviews")
        : key === "runs"
          ? navigate("runs")
          : navigate("memories", { filter: key });
    task.catch((e) => toast(e.message, true));
  });
for (const n of document.querySelectorAll("[data-run-tab]"))
  n.addEventListener("click", () =>
    navigate("runs", { runTab: n.dataset.runTab }).catch((e) =>
      toast(e.message, true),
    ),
  );
$("refresh-button").addEventListener("click", () =>
  refresh().then(
    () => toast("已刷新当前记录。"),
    (e) => toast(e.message, true),
  ),
);
$("new-memory-button").addEventListener("click", () =>
  editMemory().catch((e) => toast(e.message, true)),
);
$("load-more-button").addEventListener("click", () =>
  loadList(true).catch((e) => toast(e.message, true)),
);
$("state-filter").addEventListener("change", () =>
  loadList().catch((e) => toast(e.message, true)),
);
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadList().catch((e) => toast(e.message, true));
});
let searchTimer;
$("search-input").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(
    () => loadList().catch((e) => toast(e.message, true)),
    280,
  );
});
(async () => {
  try {
    const session = await api("/api/session");
    state.token = session.csrf_token;
    state.readOnly = session.read_only;
    $("access-mode").textContent = state.readOnly ? "只读模式" : "本地人工维护";
    $("connection-label").textContent = "已连接本地经验库";
    $("connection-dot").className = "connection-dot connected";
    await overview();
    await navigate("memories");
  } catch (e) {
    $("connection-label").textContent = "连接失败，请检查服务";
    $("connection-dot").className = "connection-dot failed";
    $("overview-notice").replaceChildren(
      note("无法读取本地记录", e.message, "error"),
    );
    toast(e.message, true);
  }
})();
