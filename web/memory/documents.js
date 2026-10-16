"use strict";
// 校验工作副本与原始提交分离；所有来源文本只通过 textContent / Text 节点展示。
window.TermDocuments = (() => {
  let items = [], current = null, selectedKey = null, dirty = false, pendingRun = null;
  let serial = 0, detailSerial = 0, sourceSerial = 0, filter = "active";
  let sourcePanel = null, sourceState = null, sourcePrevious = [], editorFields = null, pickBoundary = null;
  const names = { pending: "待校验", modified: "修改中 · 需校验", verified: "已校验", discarded: "已丢弃" };
  const actions = (...nodes) => el("div", "document-actions", null, ...nodes);
  const writable = () => !state.readOnly && current?.status !== "discarded" && !current?.sync?.dirty && !current?.sync?.blocked;
  const documentUrl = (suffix = "", run = current?.run_id) =>
    `/api/documents/${encodeURIComponent(current.document_id)}${suffix}?${new URLSearchParams({ run_id: run || "" })}`;
  const statusBadge = status => badge(status === "verified" ? "confirmed" : status === "discarded" ? "withdrawn" : "pending", names[status] || status);
  const labelLearning = status => ({ pending: "待总结经验", learned: "已总结经验", not_ready: "校验后可学习" })[status] || "校验后可学习";

  function setRoute(runId) { pendingRun = runId; }
  function currentItem() { return items.find(x => x.document_id === current?.document_id); }
  function route() {
    const params = location.hash.startsWith("#documents")
      ? new URLSearchParams(location.hash.split("?")[1] || "")
      : new URLSearchParams(location.search);
    if (params.get("run_id")) pendingRun = params.get("run_id");
  }
  function writeRoute() {
    if (!current) return;
    const query = new URLSearchParams(location.search);
    for (const key of ["view", "run_id", "document_id"]) query.delete(key);
    history.replaceState(null, "", `${location.pathname}${query.size ? `?${query}` : ""}#documents?${new URLSearchParams({ run_id: current.run_id, document_id: current.document_id })}`);
  }
  async function canLeave() {
    if (!dirty) return true;
    return new Promise(resolve => {
      const body = el("div", "document-confirm", null,
        note("有尚未保存的条款修改", "保存后的修改已留痕。当前表单尚未保存，离开会丢弃这部分草稿。"));
      let answered = false;
      const finish = answer => {
        if (answered) return;
        answered = true;
        if (answer) { dirty = false; editorFields = null; pickBoundary = null; }
        $("action-dialog").close(); resolve(answer);
      };
      body.append(actions(button("继续编辑", () => finish(false)), button("丢弃草稿并继续", () => finish(true), "button danger small")));
      $("action-dialog").addEventListener("close", () => finish(false), { once: true });
      dialog("离开未保存的修改", body);
    });
  }
  function renderList() {
    const q = $("search-input").value.trim().toLowerCase();
    const visible = items.filter(x => (filter === "all" || filter === "active" && x.status !== "discarded" || x.status === filter) &&
      (!q || `${x.title} ${x.input_path} ${x.document_id}`.toLowerCase().includes(q)));
    $("list-total").textContent = `${items.filter(x => x.status !== "discarded").length} 篇标书 · ${items.filter(x => ["pending", "modified"].includes(x.status)).length} 篇待校验`;
    const select = el("select", "document-list-filter");
    select.setAttribute("aria-label", "按校验状态筛选标书");
    for (const [value, text] of [["active", "全部有效结果"], ["pending", "待校验"], ["modified", "修改中"], ["verified", "已校验"], ["discarded", "已丢弃"], ["all", "所有标书"]]) {
      const option = el("option", null, text); option.value = value; option.selected = value === filter; select.append(option);
    }
    select.addEventListener("change", () => { filter = select.value; renderList(); });
    const nodes = visible.map(item => {
      const card = button("", () => choose(item, item.run_id), `record-card${item.document_id === current?.document_id ? " selected" : ""}`);
      card.append(el("div", "record-topline", null, el("span", "record-category", "标书"), statusBadge(item.status)),
        el("h3", "record-title", item.title || item.input_path?.split(/[\\/]/).pop() || "未命名标书"),
        el("p", "record-preview", `${item.clause_count ?? 0} 条有效条款 · ${item.run_count || item.runs?.length || 1} 次抽取`),
        el("div", "record-meta", null, el("span", null, item.status === "discarded" ? "不参与学习" : labelLearning(item.learning_status)), el("span", null, date(item.updated_at || item.created_at))));
      return card;
    });
    $("record-list").replaceChildren(el("div", "document-list-controls", null, select), ...nodes);
    if (!visible.length) $("record-list").append(empty("没有符合条件的标书", "在 OpenCode 中完成并提交抽取后，刷新即可看到待校验结果。"));
    $("load-more-button").hidden = true;
    $("list-footer-note").textContent = "按原文归组 · 每次抽取独立留痕";
  }
  async function load() {
    route(); const ticket = ++serial;
    $("record-list").setAttribute("aria-busy", "true");
    try {
      const response = await api("/api/documents");
      if (ticket !== serial || state.section !== "documents") return;
      items = response.documents || [];
      renderList();
      const run = pendingRun;
      const target = run ? items.find(x => x.run_id === run || (x.runs || []).some(r => r.run_id === run)) : currentItem();
      pendingRun = null;
      if (run && !target) toast("未找到该运行的已提交结果。请确认抽取已成功提交。", true);
      if (target) {
        if (dirty && current?.document_id === target.document_id && (!run || run === current.run_id)) {
          toast("列表已刷新；保留当前未保存的草稿。保存或取消草稿后可重新读取本地结果。"); return;
        }
        await choose(target, run || current?.run_id || target.run_id, true);
      } else if (items.some(x => x.status !== "discarded")) {
        const first = items.find(x => x.status !== "discarded"); await choose(first, first.run_id);
      } else {
        current = null; $("detail-panel").replaceChildren(empty("抽取完成后在这里校验", "校验状态由用户主动确认。网页保存修正，不调用模型；整篇丢弃与条款纠错分别记录。"));
      }
    } finally { if (ticket === serial) $("record-list").setAttribute("aria-busy", "false"); }
  }
  async function choose(item, runId, refresh = false) {
    const changed = current?.document_id !== item.document_id || current?.run_id !== runId;
    if ((changed || refresh) && !(await canLeave())) return;
    const ticket = ++detailSerial; $("detail-panel").setAttribute("aria-busy", "true");
    try {
      const data = await api(`/api/documents/${encodeURIComponent(item.document_id)}?${new URLSearchParams({ run_id: runId || item.run_id })}`);
      if (ticket !== detailSerial || state.section !== "documents") return;
      if (changed) { sourcePanel = null; sourceState = null; sourcePrevious = []; selectedKey = null; sourceSerial++; }
      current = data; dirty = false; editorFields = null; pickBoundary = null;
      writeRoute(); renderList(); renderDetail();
      if (!sourceState) {
        try { await loadSource({ cursor: 0 }); }
        catch (error) {
          getSourcePanel().querySelector(".document-json").textContent = error.message;
          getSourcePanel().querySelector(".document-source-location").textContent = "原文暂时无法读取";
          toast(error.message, true);
        }
      }
    } finally { if (ticket === detailSerial) $("detail-panel").setAttribute("aria-busy", "false"); }
  }
  function updateSummary() {
    const item = currentItem(); if (!item) return;
    const run = (item.runs || []).find(x => x.run_id === current.run_id);
    if (run) Object.assign(run, { status: current.status, revision: current.revision, clause_count: current.result?.length || 0, learning_status: current.learning?.status });
    if (item.run_id === current.run_id) Object.assign(item, { status: current.status, revision: current.revision, clause_count: current.result?.length || 0, learning_status: current.learning?.status });
    renderList();
  }
  async function mutate(action, payload = {}) {
    if (state.readOnly) throw new Error("当前连接为只读模式，不能修改结果。");
    const ticket = ++detailSerial, runId = current.run_id, docId = current.document_id;
    try {
      const response = await api(documentUrl(`/${action}`).split("?")[0], { run_id: runId, expected_revision: current.revision, ...payload });
      if (ticket !== detailSerial || state.section !== "documents" || current?.run_id !== runId || current?.document_id !== docId) return;
      current = response.document || response.review || response;
      dirty = false; editorFields = null; pickBoundary = null;
      if (selectedKey === "__new__") selectedKey = null;
      updateSummary(); renderDetail();
      if (action === "discard" || action === "restore") await refreshSummaries();
      return current;
    } catch (error) {
      toast(`${error.message}。草稿仍保留；如有其他操作修改了同一结果，请先复制草稿再刷新。`, true); throw error;
    }
  }
  async function refreshSummaries() {
    const response = await api("/api/documents");
    if (state.section === "documents") { items = response.documents || []; renderList(); }
  }
  function renderDetail() {
    if (!current || state.section !== "documents") return;
    editorFields = null; pickBoundary = null;
    const d = current, item = currentItem();
    const root = el("div", "document-review", null);
    const heading = el("div", "document-heading", null,
      el("div", null, null, el("div", "detail-kicker", "标书结果 / 人工校验"), el("h2", null, d.title || item?.title || "抽取结果"), el("p", "document-subtitle mono", d.run_id)),
      el("div", "document-statuses", null, statusBadge(d.status), badge(d.status === "discarded" ? "withdrawn" : d.learning?.status === "learned" ? "confirmed" : "pending", d.status === "discarded" ? "不参与学习" : labelLearning(d.learning?.status))));
    root.append(heading);
    const versions = el("select", "document-version"); versions.setAttribute("aria-label", "选择本篇标书的抽取运行");
    for (const run of item?.runs || [{ run_id: d.run_id, status: d.status }]) {
      const option = el("option", null, `${run.run_id === item?.run_id ? "最近 · " : ""}${date(run.created_at)} · ${names[run.status] || "待校验"} · ${run.run_id}`);
      option.value = run.run_id; option.selected = run.run_id === d.run_id; versions.append(option);
    }
    versions.addEventListener("change", async () => {
      try { if (!(await canLeave())) { versions.value = d.run_id; return; } await choose(item || { document_id: d.document_id }, versions.value); }
      catch (e) { toast(e.message, true); }
    });
    root.append(el("label", "document-run-picker", "抽取运行", versions));
    const statusText = d.status === "verified" ? "本版已由用户完成整篇校验。再次修改后会回到需校验状态。"
      : d.status === "discarded" ? "本篇全部运行的结果已丢弃，保留可恢复记录；此次操作不作为纠错，也不用于总结经验。"
      : "请核对漏产品、关键参数覆盖、其他设备参数混入、数量与关联。保存修改后仍需主动点击“完成整篇校验”。";
    root.append(note(names[d.status] || "等待校验", statusText, d.status === "verified" ? "success" : ""));
    if ((d.history?.[0]?.operations || []).some(operation => operation.working_file_preserved))
      root.append(note("问题工作文件已保留", "整篇操作只隐藏或恢复记录。有未同步修改、缺失或来源问题的工作文件未被覆盖；恢复后请检查并同步，再继续校验。"));
    const path = el("details", "document-paths", null, el("summary", null, "本地结果同步与原始快照"));
    path.append(metadata({ "当前可编辑结果（保存同步）": d.current_result_path, "原始抽取快照（保持不变）": d.original_result_path, "当前修订": `r${d.revision}` }),
      el("p", "inline-note", "工作结果独立保存，不覆盖原始 final。手动修改本地工作结果后，刷新并导入修改；导入或再次保存都会要求重新校验。"));
    root.append(path);
    if (d.sync?.dirty || d.sync?.blocked) root.append(el("div", "callout error", null, el("h3", null, "检测到本地文件变化"),
      el("p", null, d.sync.reason || "磁盘工作结果已与当前修订不同。请先导入文件修改，再继续编辑。"),
      button("导入本地文件修改", async () => { if (!(await canLeave())) return; await mutate("sync"); toast("本地修改已导入并留痕，请重新完成整篇校验。"); }, "button secondary small", state.readOnly || !!d.sync?.blocked)));
    if (d.validation && !d.validation.ok) root.append(validationView(d.validation));
    const controls = actions(button("补充漏抽条款", () => newClause(), "button secondary small", !writable()),
      button("完成整篇校验", () => verifyDialog(), "button small", !writable() || d.validation?.ok === false || d.status === "verified"),
      button("修订记录", () => historyDialog(), "text-button"),
      button("刷新本地结果", () => load(), "text-button"));
    if (d.status === "discarded") controls.append(button("恢复本篇结果", () => discardDialog(true), "button secondary small", state.readOnly));
    else controls.append(button("删除本篇结果", () => discardDialog(false), "text-button document-danger", state.readOnly));
    root.append(controls);
    if (d.status === "verified") root.append(learningView());
    const grid = el("div", "document-review-grid");
    const clausePanel = el("section", "document-clauses", null, el("div", "document-panel-heading", null, el("h3", null, "抽取条款"), el("span", "muted", `${d.result?.length || 0} 条有效 · ${d.clauses?.filter(x => x.state !== "active").length || 0} 条已排除`)));
    if (selectedKey === "__new__") clausePanel.append(el("article", "document-clause selected", null,
      el("h3", null, "补充漏抽条款"), el("p", "document-pick-note", "补漏会保留为修正；可从右侧原文选择边界。"), editor(null, true)));
    if (!(d.clauses || []).length) clausePanel.append(empty("暂无条款", "可补充漏抽条款；无目标设备也可以在确认后完成校验。"));
    for (const clause of d.clauses || []) clausePanel.append(clauseCard(clause));
    grid.append(clausePanel, getSourcePanel()); root.append(grid);
    $("detail-panel").replaceChildren(root);
  }
  function validationView(validation) {
    const n = el("div", "callout error", null, el("h3", null, "结果需要修正后才能完成校验"));
    for (const error of validation.errors || []) n.append(el("p", null, typeof error === "string" ? error : error.message || pretty(error)));
    for (const warning of validation.warnings || []) n.append(el("p", "muted", typeof warning === "string" ? warning : warning.message || pretty(warning)));
    return n;
  }
  function clauseCard(entry) {
    const clause = entry.clause || {}, selected = selectedKey === entry.clause_key, excluded = entry.state !== "active";
    const card = el("article", `document-clause${selected ? " selected" : ""}${excluded ? " excluded" : ""}`);
    const title = button(clause["条款标题"] || "未命名条款", async () => {
      if (!(await canLeave())) return; selectedKey = selected ? null : entry.clause_key; renderDetail();
    }, "document-clause-title");
    card.append(el("div", "document-clause-heading", null, el("span", "document-clause-number", clause.id == null ? "新增" : `#${clause.id}`), title));
    card.append(el("div", "document-clause-meta", null,
      el("span", null, clause["条款分类"] || "normal"), el("span", null, clause["产品分类"] || "类别未提及"), el("span", null, `数量：${clause["产品数量"] || "未提及"}`)));
    const tags = clause["定位标识"] || [];
    card.append(el("div", "document-boundaries", null,
      button(tags[0] || "缺起点", () => loadSource({ tag: tags[0] }), "document-tag", !tags[0]), el("span", "muted", "→"),
      button(tags[1] || "缺终点", () => loadSource({ tag: tags[1] }), "document-tag", !tags[1])));
    if ((entry.related_keys || []).length) card.append(el("p", "document-relation", `关联：${entry.related_keys.map(key => {
      const other = current.clauses.find(x => x.clause_key === key); return other ? `#${other.clause.id} ${other.clause["条款标题"]}` : key;
    }).join("、")}`));
    if (excluded) {
      card.append(el("p", "document-exclusion", `${entry.state === "non_target" ? "已标记非目标条款" : "已删除"}${entry.reason ? `：${entry.reason}` : ""}`),
        button("恢复此条款", async () => { if (!(await canLeave())) return; await mutate("edit", { action: "restore", clause_key: entry.clause_key }); toast("条款已恢复，请重新校验。"); }, "text-button", !writable()));
    } else {
      card.append(actions(button(selected ? "收起编辑" : "编辑条款", async () => {
        if (!(await canLeave())) return; selectedKey = selected ? null : entry.clause_key; renderDetail();
      }, "text-button", !writable()),
      button("标记非目标", () => excludeDialog(entry, "non_target"), "text-button", !writable()),
      button("删除", () => excludeDialog(entry, "delete"), "text-button document-danger", !writable())));
      if (selected) card.append(editor(entry));
    }
    return card;
  }
  function formInput(form, name, label, value, required = false) {
    const wrap = el("label", "document-form-field", label), input = el("input");
    input.name = name; input.value = value ?? ""; input.required = required; input.setAttribute("aria-label", label); wrap.append(input); form.append(wrap); return input;
  }
  function editor(entry, adding = false) {
    const clause = entry?.clause || { "条款标题": "", "条款分类": "normal", "产品分类": "未提及", "定位标识": ["", ""], "产品数量": "未提及" };
    const form = el("form", "document-clause-form");
    const title = formInput(form, "title", "条款标题", clause["条款标题"], true);
    const boundary = el("div", "document-form-pair"); form.append(boundary);
    const start = formInput(boundary, "start", "起始 C 标签", clause["定位标识"]?.[0], true);
    const end = formInput(boundary, "end", "终止 C 标签", clause["定位标识"]?.[1], true);
    start.pattern = end.pattern = "<C[0-9]+>";
    const pickNote = el("p", "document-pick-note", "可直接输入标签，也可选择下方按钮后点击右侧原文中的 C 标签。");
    const setPick = which => { pickBoundary = which; pickNote.textContent = `请点击右侧原文标签，填入${which === "start" ? "起点" : "终点"}。`; };
    form.append(actions(button("从原文选择起点", () => setPick("start"), "text-button"), button("从原文选择终点", () => setPick("end"), "text-button")), pickNote);
    const pair = el("div", "document-form-pair"); form.append(pair);
    const quantity = formInput(pair, "quantity", "产品数量", clause["产品数量"] || "未提及", true);
    const categoryWrap = el("label", "document-form-field", "条款分类"), category = el("select"); category.setAttribute("aria-label", "条款分类");
    for (const value of ["normal", "general", "separate"]) { const option = el("option", null, value); option.value = value; option.selected = value === clause["条款分类"]; category.append(option); }
    categoryWrap.append(category); pair.append(categoryWrap);
    const product = formInput(form, "product", "产品分类（仅供参考）", clause["产品分类"] || "未提及");
    const related = el("fieldset", "document-related", null, el("legend", null, "相关条款"));
    const checks = [];
    for (const other of current.clauses.filter(x => x.state === "active" && x.clause_key !== entry?.clause_key)) {
      const label = el("label", "document-related-option"), check = el("input"); check.type = "checkbox"; check.value = other.clause_key; check.checked = (entry?.related_keys || []).includes(other.clause_key);
      label.append(check, el("span", null, `#${other.clause.id} ${other.clause["条款标题"]}`)); related.append(label); checks.push(check);
    }
    if (!checks.length) related.append(el("p", "muted", "暂无其他有效条款。"));
    form.append(related);
    const reason = formInput(form, "note", "修正说明（可选，用于保留反馈）", "");
    const changed = () => { dirty = true; form.classList.add("has-draft"); };
    form.addEventListener("input", changed); form.addEventListener("change", changed);
    editorFields = { start, end, pickNote, changed };
    const save = el("button", "button small", adding ? "新增并保存" : "保存条款修正"); save.type = "submit"; save.disabled = !writable();
    form.append(actions(save, button("取消草稿", async () => { if (!(await canLeave())) return; selectedKey = null; renderDetail(); }, "text-button")),
      el("p", "muted", "保存即同步当前工作结果并记录修订，不代表整篇校验完成。"));
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (!form.reportValidity()) return;
      save.disabled = true;
      try {
        const patch = { "条款标题": title.value.trim(), "定位标识": [start.value.trim(), end.value.trim()], "产品数量": quantity.value.trim(), "条款分类": category.value, "产品分类": product.value.trim() || "未提及" };
        const relatedKeys = checks.filter(x => x.checked).map(x => x.value);
        await mutate("edit", { action: adding ? "add" : "update", ...(adding ? { clause: patch } : { clause_key: entry.clause_key, patch }), related_keys: relatedKeys, note: reason.value.trim() });
        toast("已保存到本地工作结果，修正记录已保留；请完成整篇校验。");
      } catch (e) { toast(e.message, true); } finally { if (save.isConnected) save.disabled = !writable(); }
    });
    if (!writable()) for (const control of form.querySelectorAll("input,select,fieldset")) control.disabled = true;
    return form;
  }
  async function newClause() {
    if (!(await canLeave())) return;
    selectedKey = "__new__"; renderDetail();
  }
  async function excludeDialog(entry, action) {
    if (!(await canLeave())) return;
    const form = el("form", "document-form", null, note(action === "non_target" ? "标记为非目标条款" : "删除单条抽取结果", "此操作作为条款修正记录，不删除原文；可以恢复。若其他条款引用此条，请一并核对关联。"));
    form.append(el("p", null, entry.clause["条款标题"]));
    const reason = formInput(form, "reason", "原因（保留为用户反馈）", "", true);
    const submit = el("button", "button small", action === "non_target" ? "确认非目标并保存" : "删除此条款并保存"); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (!form.reportValidity()) return; submit.disabled = true;
      try { await mutate("edit", { action, clause_key: entry.clause_key, reason: reason.value.trim() }); $("action-dialog").close(); toast("修正已保存，可在条款卡片中恢复。"); }
      catch (e) { toast(e.message, true); } finally { submit.disabled = state.readOnly; }
    }); dialog(action === "non_target" ? "非目标条款" : "删除条款", form);
  }
  async function verifyDialog() {
    if (!(await canLeave())) return;
    const form = el("form", "document-form", null, note("完成当前版本的整篇校验", "确认所有目标设备已覆盖，关键参数、数量与关联符合业务口径。只有主动确认后，这一版才作为可信反馈供 Agent 学习。"));
    const reviewer = formInput(form, "reviewer", "校验人", current.reviewer || "维护者", true);
    const family = formInput(form, "family", "文档家族（同模板、修订版使用同一名称）", current.family_id || "", true);
    reviewer.maxLength = 160; family.maxLength = 200;
    const noteInput = formInput(form, "note", "校验说明（可选）", current.note || "");
    const acknowledgment = el("label", "document-ack"), check = el("input"); check.type = "checkbox"; check.required = true;
    acknowledgment.append(check, el("span", null, "我已按当前业务口径完成整篇校验，确认当前工作结果可用于学习。")); form.append(acknowledgment);
    const submit = el("button", "button small", "确认已校验"); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (!form.reportValidity()) return; submit.disabled = true;
      try { await mutate("verify", { reviewer: reviewer.value.trim(), family_id: family.value.trim(), note: noteInput.value.trim(), acknowledged: check.checked }); $("action-dialog").close(); toast("整篇校验已记录。请回到 OpenCode 读取反馈并总结候选经验。"); }
      catch (e) { toast(e.message, true); } finally { submit.disabled = state.readOnly; }
    }); dialog("完成整篇校验", form);
  }
  async function discardDialog(restore) {
    if (!(await canLeave())) return;
    const form = el("form", "document-form", null,
      note(restore ? "恢复本篇结果" : "丢弃整篇结果，不作为纠错", restore ? "恢复本篇标书所有运行的结果。恢复后需重新确认整篇校验；存在问题的本地工作文件继续保留，修复并同步后再校验。" : "本篇标书所有运行的结果将移入“已丢弃”，可以恢复。正常工作结果清空，存在未同步修改或其他问题的文件保留。此操作表示不要这篇结果，不表示抽取错误，不生成学习反馈。"));
    const noteInput = formInput(form, "note", restore ? "恢复说明（可选）" : "丢弃说明（可选）", "");
    const submit = el("button", restore ? "button small" : "button danger small", restore ? "恢复本篇结果" : "确认丢弃本篇所有结果"); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", async event => {
      event.preventDefault(); submit.disabled = true;
      try { await mutate(restore ? "restore" : "discard", { note: noteInput.value.trim() }); $("action-dialog").close(); toast(restore ? "本篇结果已恢复，请重新校验。" : "本篇结果已移入已丢弃；未生成纠错经验。"); }
      catch (e) { toast(e.message, true); } finally { submit.disabled = state.readOnly; }
    }); dialog(restore ? "恢复标书结果" : "删除本篇结果", form);
  }
  function historyDialog() {
    const body = el("div", "document-history");
    if (!(current.history || []).length) body.append(el("p", "muted", "暂无人工修订。原始抽取快照已单独保留。"));
    for (const event of current.history || []) body.append(el("section", "document-history-item", null,
      el("h3", null, `r${event.revision} · ${{ update: "修改条款", add: "补漏新增", non_target: "标记非目标", delete: "删除条款", restore: "恢复", verified: "整篇校验", verify: "整篇校验", discard: "丢弃整篇", sync: "导入本地修改" }[event.action] || event.action}`),
      el("p", "muted", `${date(event.created_at)} · ${event.reviewer || event.actor || "本地维护"}`),
      event.note ? el("p", null, event.note) : null, jsonView("修订内容", event.operations || event)));
    dialog("结果修订与校验记录", body);
  }
  function learningView() {
    const d = current, prompt = `请读取已校验的标书结果并总结候选经验。\n运行 ID：${d.run_id}\n标书 ID：${d.document_id}\n当前修订：${d.revision}\n请通过 term_review_read 读取此版本的已校验结果和修正记录，与原始提交对照，先区分可迁移经验与个案事实，再通过 term_review_reflect 生成有证据支持的候选经验并记录反馈已处理；无可迁移经验时明确记录原因。不要将丢弃整篇作为错误，不要重复总结同一修订。候选保存后，检查可信案例是否齐全，齐全则按独立评测准入继续验证；材料不足时明确报告缺项。也可由用户在经验页人工审核具体规则后直接发布。Agent 不得代用户执行人工审核或直接发布未经评测的候选。`;
    const node = el("section", "document-learning", null,
      el("h3", null, labelLearning(d.learning?.status)),
      el("p", null, d.learning?.status === "learned" ? "此校验版本的反馈已由 Agent 处理。再次修改并校验后，会形成新一版待处理反馈。" : "校验反馈已保存。网页不运行模型；回到 OpenCode 让 Agent 读取反馈、总结候选经验。"));
    const area = el("textarea", "document-prompt"); area.value = prompt; area.readOnly = true; area.rows = 4; area.setAttribute("aria-label", "总结已校验结果的 OpenCode 提示词");
    node.append(area, button("复制给 OpenCode 的提示词", async () => {
      try { await navigator.clipboard.writeText(prompt); toast("已复制，请粘贴到 OpenCode；网页不会自动启动模型。"); }
      catch { area.focus(); area.select(); toast("请复制已选中的提示词并粘贴到 OpenCode。"); }
    }, "button secondary small"));
    if (d.learning?.record?.candidates?.length) node.append(jsonView("本次反馈产生的候选", d.learning.record.candidates));
    if (d.learning?.record?.no_experience_reason) node.append(el("p", null, `未生成候选：${d.learning.record.no_experience_reason}`));
    return node;
  }
  function getSourcePanel() {
    if (sourcePanel) return sourcePanel;
    sourcePanel = el("section", "document-source", null, el("div", "document-panel-heading", null, el("h3", null, "标书原文 · 格式化 JSON")));
    const search = el("form", "document-source-search"), input = el("input"); input.placeholder = "跳转至 <C123>"; input.setAttribute("aria-label", "原文定位 C 标签");
    const go = el("button", "button secondary small", "跳转"); go.type = "submit"; search.append(input, go);
    search.addEventListener("submit", event => { event.preventDefault(); const raw = input.value.trim(); const tag = /^\d+$/.test(raw) ? `<C${raw}>` : raw; loadSource({ tag }).catch(e => toast(e.message, true)); });
    sourcePanel.append(search, el("p", "document-source-help", "C 标签高亮可点击；编辑边界时先选择起点或终点。合并单元格可能多次显示同一标签。"),
      el("div", "document-source-location", "正在读取原文…"), el("pre", "document-json", "正在读取…"), el("div", "document-source-pages"));
    return sourcePanel;
  }
  async function loadSource({ cursor, tag, previous = false } = {}) {
    if (!current) return;
    if (tag !== undefined && !/^<C\d+>$/.test(tag)) throw new Error("请输入完整 C 标签，例如 <C123>。");
    const ticket = ++sourceSerial, docId = current.document_id, runId = current.run_id;
    const params = new URLSearchParams({ run_id: runId });
    if (tag) params.set("tag", tag); else params.set("cursor", String(cursor ?? 0));
    const panel = getSourcePanel(); panel.setAttribute("aria-busy", "true");
    try {
      const page = await api(`/api/documents/${encodeURIComponent(docId)}/source?${params}`);
      if (ticket !== sourceSerial || current?.document_id !== docId || current?.run_id !== runId || state.section !== "documents") return;
      if (tag) sourcePrevious = [];
      else if (previous) sourcePrevious.pop();
      else if (sourceState && (cursor ?? 0) !== sourceState.start_cursor) sourcePrevious.push(sourceState.start_cursor);
      sourceState = page; renderSource(page);
      if (tag) {
        const target = [...panel.querySelectorAll("mark[data-c-tag]")].find(mark => mark.dataset.cTag === tag);
        if (target) { target.classList.add("document-tag-target"); target.scrollIntoView({ block: "center", behavior: "smooth" }); target.focus({ preventScroll: true }); }
      }
    } finally { if (ticket === sourceSerial) panel.setAttribute("aria-busy", "false"); }
  }
  function renderSource(page) {
    const panel = getSourcePanel(), pre = panel.querySelector(".document-json"), text = page.text || "", fragment = document.createDocumentFragment();
    const regex = /<C\d+>/g; let offset = 0, match;
    while ((match = regex.exec(text))) {
      fragment.append(document.createTextNode(text.slice(offset, match.index)));
      const mark = el("mark", "document-c-tag", match[0]); mark.dataset.cTag = match[0]; mark.tabIndex = 0; mark.setAttribute("role", "button"); mark.setAttribute("aria-label", `定位标签 ${match[0]}，点击可用于边界`);
      const selectTag = () => {
        if (editorFields && pickBoundary) {
          const input = editorFields[pickBoundary]; input.value = mark.dataset.cTag; editorFields.changed();
          editorFields.pickNote.textContent = `已填入${pickBoundary === "start" ? "起点" : "终点"} ${mark.dataset.cTag}，保存条款后生效。`; pickBoundary = null;
        } else toast(`已定位 ${mark.dataset.cTag}。编辑条款后，可选择“从原文选择起点／终点”填入边界。`);
        for (const n of pre.querySelectorAll(".document-tag-target")) n.classList.remove("document-tag-target"); mark.classList.add("document-tag-target");
      };
      mark.addEventListener("click", selectTag); mark.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); selectTag(); } });
      fragment.append(mark); offset = match.index + match[0].length;
    }
    fragment.append(document.createTextNode(text.slice(offset))); pre.replaceChildren(fragment); pre.scrollTop = 0;
    panel.querySelector(".document-source-location").textContent = `${Number(page.start_cursor || 0).toLocaleString()}–${(Number(page.start_cursor || 0) + text.length).toLocaleString()} / ${Number(page.total_chars || text.length).toLocaleString()} 字符${page.tag ? ` · ${page.tag}` : ""}`;
    panel.querySelector(".document-source-pages").replaceChildren(
      button("上一页", () => loadSource({ cursor: sourcePrevious[sourcePrevious.length - 1], previous: true }), "button secondary small", !sourcePrevious.length),
      el("span", "muted", "原文分页展示，保留完整格式与定位标签"),
      button("下一页", () => loadSource({ cursor: page.next_cursor }), "button secondary small", page.next_cursor === null || page.next_cursor === undefined));
  }
  window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
  return { load, canLeave, setRoute };
})();
