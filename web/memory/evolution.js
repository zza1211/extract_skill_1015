"use strict";
// 与 app.js 共用同源 API / CSRF 与安全 DOM 构建器，不将来源文本解释为 HTML。
window.TermEvolution = (() => {
  let tab = "studies", reviewState = "open", timer, serial = 0;
  const names = {
    queued: "待启动", running: "正在执行", completed: "已完成", failed: "执行失败",
    awaiting_blind_extraction: "等待隔离盲抽", compared: "已完成标签比较", reflected: "已记录学习经验",
    needs_configuration: "需要模型配置", needs_examples: "等待验证案例", needs_review: "等待确认",
    budget_exhausted: "预算已用完", interrupted: "已中断", open: "待答复", resolved: "已答复",
    development: "学习案例", regression: "独立回归", holdout: "保留测试",
    extract: "纯条款抽取", learn_labels: "标签学习",
    harmless: "无害差异", error: "业务错误", uncertain: "证据不足",
    comparison: "结果差异", experience_conflict: "经验冲突", case: "当前案例", memory: "指定经验", policy: "业务口径",
    missing_objects: "漏产品", extra_objects: "多抽设备", parameter_omissions: "关键参数遗漏",
    parameter_pollution: "混入其他设备参数", quantity_errors: "数量错误", association_errors: "关联错误",
    ambiguous_differences: "待判断差异", harmless_differences: "无害差异",
  };
  const display = value => names[value] || value || "未记录";
  const idOf = x => x.study_id || x.job_id || x.review_id || x.case_id;
  const controls = (...nodes) => el("div", "evolution-actions", null, ...nodes);

  function selectField(form, label, values, selected) {
    const wrap = el("label", "form-field", label), input = el("select"); input.setAttribute("aria-label",label);
    for (const [key, name] of values) {
      const option = el("option", null, name); option.value = key; option.selected = key === selected;
      input.append(option);
    }
    wrap.append(input); form.append(wrap); return input;
  }
  function formDialog(title, help, build, submitText, action) {
    const form = el("form", "evolution-form", null, note(title, help));
    const values = build(form);
    submitForm(form, submitText, async () => { await action(values); $("action-dialog").close(); await overview(); await load(); });
    dialog(title, form);
  }
  function openCodeGuide() {
    const body=el("div","evolution-guide",null,
      note("OpenCode 是统一执行入口","在项目目录执行 opencode --agent term-extractor（默认启动也使用此代理）。代理负责读取原文、调用工具、自检与学习；网页负责查看记录、管理经验和回答待办。"));
    for(const [title,text] of [
      ["纯条款抽取",'请使用 term-extract 技能抽取以下文件：\n输入：D:\\path\\input.json\n请自主完成读取、抽取、校验与提交，并记录有证据支持的观察。'],
      ["标签学习",'请使用 term-extract 技能进行标签学习：\n输入：D:\\path\\input.json\n正确标签：D:\\path\\labels.json\n案例族：项目或模板名称\n标签已按当前业务口径确认。请先隔离盲抽并冻结结果，再比较标签、归纳经验并验证。'],
      ["继续已有学习",'请检查学习记录和待确认事项，读取我已保存的答复，继续指定的学习流程。\n学习记录 ID：请粘贴对应 ID'],
    ]) {
      const block=el("section","evolution-prompt",null,el("h3",null,title));
      const area=el("textarea","prompt-text"); area.value=text; area.readOnly=true; area.rows=6; area.setAttribute("aria-label",`${title}提示词`);
      block.append(area,button("复制提示词",async()=>{
        try { await navigator.clipboard.writeText(text); toast("已复制；请粘贴到 OpenCode，并替换文件路径。"); }
        catch { area.focus(); area.select(); toast("请复制已选中的提示词，再粘贴到 OpenCode。"); }
      },"button secondary small")); body.append(block);
    }
    dialog("在 OpenCode 中执行",body);
  }
  function importCase() {
    formDialog("导入可信案例", "用于验证经验是否有效。相同标书、模板或改写版本归为同一案例族；保留测试不参与经验归纳和自动晋级。", form => ({
      input:inputField(form,"input","输入 JSON 的本机路径","","text",true),
      labels:inputField(form,"labels","正确标签 JSON 的本机路径","","text",true),
      family:inputField(form,"family","案例族","","text",true),
      split:selectField(form,"案例用途",[["development","学习案例"],["regression","独立回归"],["holdout","保留测试"]],"regression"),
    }), "导入案例", async v => {
      const result = await api("/api/learning/cases", {input_path:v.input.value.trim(),labels_path:v.labels.value.trim(),family_id:v.family.value.trim(),split:v.split.value});
      tab="cases"; state.selected=result.case_id; toast("案例已归档。请在 OpenCode 中继续等待案例的学习流程。");
    });
  }
  function renderCard(item) {
    const id=idOf(item), title=item.title || item.input_path?.split(/[\\/]/).pop() || item.case_id || "学习记录";
    const node=button("",async () => { state.selected=id; await detail(item); highlight(); },"record-card");
    node.dataset.evolutionId=id;
    node.append(el("span","record-title",title),el("span","record-meta",item.mode ? display(item.mode) : item.family_id || display(item.kind)),
      el("span","record-preview",item.next_action || item.question || display(item.split)),
      badge(item.state || item.status || "pending",display(item.state || item.status || item.split)),el("small","muted",date(item.created_at)));
    return node;
  }
  function highlight() { for (const n of document.querySelectorAll("[data-evolution-id]")) n.classList.toggle("selected",n.dataset.evolutionId===state.selected); }
  async function load() {
    clearTimeout(timer); const request=++serial, section=state.section;
    if (!["learning","reviews"].includes(section)) return;
    $("load-more-button").hidden=true; $("record-list").setAttribute("aria-busy","true");
    const actions = section === "learning"
      ? controls(button("OpenCode 使用指引",openCodeGuide,"button secondary small"),
          button("学习记录",()=>{tab="studies";state.selected=null;return load();},"text-button"),
          button("可信案例",()=>{tab="cases";state.selected=null;return load();},"text-button"),
          button("历史流程",()=>{tab="jobs";state.selected=null;return load();},"text-button"),
          button("导入案例",importCase,"text-button",state.readOnly))
      : controls(button("待答复",()=>{reviewState="open";state.selected=null;return load();},"text-button"),button("已答复",()=>{reviewState="resolved";state.selected=null;return load();},"text-button"),
          button("检查经验冲突",async()=>{const r=await api("/api/experiences/scan",{});toast(r.notice || "冲突检查完成，具体疑点已进入待确认事项。");await load();},"button secondary small",state.readOnly));
    $("record-list").replaceChildren(actions,el("p","muted","正在读取…"));
    try {
      const d=await api(section === "reviews" ? `/api/reviews?state=${reviewState}` : `/api/learning/${tab}`);
      if (request!==serial || state.section!==section) return;
      const items=d.items || d.studies || d.jobs || d.cases || d.reviews || (Array.isArray(d)?d:[]);
      state.items=items; $("list-total").textContent=`${items.length} 条记录`;
      $("record-list").replaceChildren(actions,...items.map(renderCard));
      if (!items.length) $("record-list").append(empty(section === "reviews" ? "没有待处理事项" : "还没有学习记录",section === "reviews" ? "只有需要你判断的问题才会出现在这里。" : "请在 OpenCode 中进行抽取或标签学习；也可在本页导入可信案例供经验验证。"));
      const item=items.find(x=>idOf(x)===state.selected);
      if (item) { await detail(item); highlight(); }
      else $("detail-panel").replaceChildren(empty(section === "reviews" ? "按问题处理例外" : "OpenCode 执行，网页管理记忆",section === "reviews" ? "答复只作用于指定案例或经验；不会隐式修改全局业务口径。" : "在 OpenCode 中使用 term-extract 技能。这里查看学习记录、维护验证案例和处理人工待办，不发起模型调用。"));
    } catch(error) {
      if(request===serial && state.section===section) {
        $("record-list").replaceChildren(actions,empty("暂时无法读取记录",error.message || "本地服务返回未知错误，请检查服务状态后刷新。"));
        $("detail-panel").replaceChildren(empty("记录尚未载入","刷新重试；失败的读取不会启动模型或修改抽取结果。"));
      }
      throw error;
    } finally { if(request===serial) $("record-list").setAttribute("aria-busy","false"); }
  }
  async function detail(item) {
    const id=idOf(item), section=state.section, request=++state.detailRequest;
    const resource=section === "reviews" ? "reviews" : `learning/${tab}`;
    const d=await api(`/api/${resource}/${encodeURIComponent(id)}`);
    if(state.section!==section || state.selected!==id || request!==state.detailRequest) return;
    $("detail-panel").replaceChildren(section === "reviews" ? reviewView(d) : tab === "cases" ? caseView(d) : tab === "studies" ? studyView(d) : jobView(d));
    if(section === "learning" && tab === "studies" && ["awaiting_blind_extraction","prepared","running","extracting","blind_extracting","comparing","evaluating"].includes(d.state)) {
      clearTimeout(timer); timer=setTimeout(()=>{if(state.section===section && state.selected===id)detail(item).catch(e=>toast(e.message,true));},5000);
    }
  }
  function caseView(d) {
    return el("div",null,null,header(d.input_path?.split(/[\\/]/).pop() || "可信标签案例","验证案例",d.case_id),
      metadata({案例族:d.family_id,用途:display(d.split),归档时间:date(d.created_at)}),
      note("可信来源与独立性","标签由用户提供并按当前口径确认。相同案例族不能同时充当学习依据和独立回归；保留测试不用于自动归纳。"),jsonView("案例工件与标签",d));
  }
  function studyView(d) {
    const n=el("div",null,null,header(d.input_path?.split(/[\\/]/).pop() || "自主学习记录","OpenCode 自主编排",d.study_id),
      metadata({当前状态:display(d.state),创建时间:date(d.created_at),案例族:d.family_id || "未指定",关联运行:d.run_id || "尚未提交"}),
      note("下一步",d.next_action || "请在 OpenCode 中继续此学习流程；网页不会启动模型。"),
      controls(button("OpenCode 使用指引",openCodeGuide,"button secondary small"),button("处理待确认事项",()=>navigate("reviews"),"text-button")));
    if(d.comparison_summary) n.append(jsonView("与标签比较的摘要",d.comparison_summary,true));
    for(const [key,title] of [["candidates","经验候选"],["review_ids","待确认事项"],["events","执行过程"]])
      if(d[key]?.length || (d[key] && !Array.isArray(d[key]))) n.append(jsonView(title,d[key]));
    n.append(jsonView("学习记录",d)); return n;
  }
  function jobView(d) {
    const n=el("div",null,null,header(d.input_path?.split(/[\\/]/).pop() || "历史流程",display(d.mode),d.job_id),
      metadata({历史状态:display(d.state),创建时间:date(d.created_at),案例族:d.family_id || "未指定",模型:d.prediction?.provenance?.model_id || d.generation?.model_id || d.model_id || "未记录",模型调用次数:d.usage?.calls ?? "未记录"}),
      note("历史记录只读","此记录来自旧版 Python 编排流程。网页已停止创建、启动和继续模型任务；新的抽取与学习统一在 OpenCode 中完成。"));
    if(d.error) n.append(note("历史执行信息",typeof d.error==="string"?d.error:pretty(d.error),"error"));
    if(d.result_path) n.append(field("结果文件",d.result_path));
    if(d.result) n.append(jsonView("抽取结果",d.result,true));
    if(d.comparison) {
      n.append(field("与正确标签比较",display(d.comparison.status)),metadata(Object.fromEntries(Object.entries(d.comparison.summary || {}).map(([k,v])=>[display(k),v]))));
      for(const x of d.comparison.differences || []) n.append(note(`${x.severity === "info" ? "可接受差异" : "差异"} · ${x.kind}`,x.message));
    }
    for(const [key,title] of [["candidates","产生的经验候选"],["experiments","验证与发布"],["review_ids","待确认事项"],["usage","模型用量"],["events","执行过程"]]) if(d[key]?.length || (d[key] && !Array.isArray(d[key]))) n.append(jsonView(title,d[key]));
    n.append(jsonView("完整历史记录",d)); return n;
  }
  function reviewView(d) {
    const r=d.review || d;
    const n=el("div",null,null,header(r.title || "待确认事项",display(r.kind),r.review_id),metadata({状态:display(r.state),适用范围:display(r.scope),创建时间:date(r.created_at)}),
      note("需要你判断",r.question || "请核对下方上下文。"));
    const context=r.context || {};
    for(const memory of context.memories || []) n.append(field(`经验 · ${memory.title}`,memory.rule,true),field("适用条件",memory.applies_when),field("排除条件",memory.exclusions));
    if(context.policy_excerpt) n.append(field("相关固定业务口径",context.policy_excerpt));
    for(const snippet of context.snippets || []) n.append(field(`${snippet.tag} 原文${snippet.truncated ? "（有截断）" : ""}`,snippet.text));
    if(context.source) n.append(field("原文",context.source));
    if(context.context_truncated) n.append(note("上下文不完整","当前只提供局部原文；材料不足时请选择证据不足。"));
    n.append(jsonView("差异与来源的完整记录",context));
    if(r.state === "resolved" || r.resolution) n.append(jsonView("已保存的答复",r.resolution || r.decisions || d));
    else {
      const form=el("form","evolution-form");
      const choices=(r.choices || []).map(x=>typeof x==="string"?[x,display(x)]:[x.value || x.id || x.decision || x.label,x.label || x.title || display(x.value || x.id)]);
      const choice=selectField(form,"处理意见",choices.length ? choices : [["defer","暂不确定"]],choices[0]?.[0]);
      const answer=inputField(form,"answer","补充说明（保留你输入的原话）","","textarea",false);
      const actor=inputField(form,"actor","答复人","维护者","text",true);
      form.append(note("答复范围","本次答复按此事项的适用范围保存。全局业务规则仍需显式修改，不会因为一次局部答复而自动改写。"));
      submitForm(form,"保存答复",async()=>{await api(`/api/reviews/${encodeURIComponent(r.review_id)}/resolve`,{decision:choice.value,answer:answer.value,actor:actor.value.trim(),expected_revision:r.revision || 1});toast("答复已保存。请回到 OpenCode，继续对应的学习流程。");await overview();await load();});
      if(state.readOnly) for(const control of form.querySelectorAll("input,textarea,select,button")) control.disabled=true;
      n.append(form);
    }
    return n;
  }
  return {load};
})();
