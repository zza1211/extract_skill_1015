# 标签学习与独立验证（0.6.0）

## 编排职责

唯一主入口是 OpenCode term-extractor。你自己选择何时读全文、搜索疑点、查看相关经验、提交、对照和归纳。Python 原子工具不调用抽取模型、不自动挑案例或启动后续阶段；网页仅管理经验、可信案例与人工待办。使用当前 OpenCode 模型，不读取旧网页模型配置。

没有标签时，按 SKILL 的正常流程抽取并在 commit 中留下最多三条有证据的观察。自检不是真值；没有可验证的新经验不必启动独立试验。

## 带标签学习

正确标签必须由用户提供，按当前口径确认，且与输入使用同一套 Cxx。每篇新标书使用新 OpenCode 会话，用户只给标签文件路径，不提前粘贴正确答案。

1. term_document_prepare 注册输入和新目录，获得 index_path。
2. term_learning_begin 传入 input_path、labels_path、family_id、index_path。工具私有归档标签，不交付标签。
3. 自主用 term_document_read 读原文，保存 checkpoint，围绕疑点 search/补读，必要时 recall；validate 后 term_result_commit 得到 learning.run_id。会话身份由工具自动记录，不可自行填写。
4. term_learning_compare(study_id,run_id) 核对提交、全文回执和同会话身份后冻结首次预测，再交付分页标签与差异。不能在看标签后修改预测并替换首次对照。按 next_label_cursor/next_difference_cursor 继续读取必要分页。
5. 主动核验局部差异。漏产品、多设备、错误关联及明确数量错误不能被局部判断覆盖；范围不同本身不代表缺关键参数。term_learning_judge 的 judgments_json 为 [{difference_id,decision:"harmless"|"error"|"uncertain",reason}]。有证据才确定，未知进入人工待办。
6. term_learning_reflect 提交简短 reflection_json，总结新经验及适用/排除条件，或空 insights 与原因。工具保存候选、去重和来源，不发布。

以上顺序约束保证盲抽和标签隔离；具体读取、核验动作及次数由你按原文和疑点安排。已揭示学习或验证标签的会话不能再开始另一篇盲抽；原学习的分页、局部核验和复盘可以继续。

## 经验的独立验证

只对新的、值得保留的候选做验证。先用 term_memory_show 查看本次候选，term_learning_cases / case_preview 按需找相关案例。用户明确提供的其他正确标签可用 term_learning_import_case 归档为 development、regression 或 holdout；标签不重复走旧人工确认。预览不提供标签，也不计作独立试验的全文交付。

案例不足或排除条件不清时保留候选并说明缺口，不凑无关案例。至少需要开发目标、独立回归与触及排除边界的反例；回归和反例来自两个独立案例族，且与归纳来源、开发目标独立。同模板、译文、项目变体归同一族。holdout 不参与经验归纳、反复试验或晋级。

用 term_learning_trial_prepare 传候选编号、修订和 cases_json：
```json
[
  {"case_id":"case_开发","role":"development"},
  {"case_id":"case_回归","role":"regression"},
  {"case_id":"case_反例","role":"counterexample","scope_note":"说明原文如何触及此经验的排除条件"}
]
```

工具固定本项实验的口径、正式基线和唯一候选，返回 A/B 票号，不执行模型。随后自主用 OpenCode 内置 task 为每张票调用 term-trial，**每票新子会话，prompt 严格只有 trial_id=对应票号**。不能传父会话上下文、答案、标签或修复提示；不能复用 task_id 做另一票。子 agent 自主 open/read/checkpoint/search/validate/commit。它只访问本票原文和经验包，不读取标签，也不生成学习来源。

最小三案例产生六个独立试验子会话，费用由当前模型的实际输入输出决定；不是每篇抽取都必须跑六轮。可按用户预算串行或并行；遇成本或上下文限制保存已完成工件，报告缺口，不声称已验证。

## 核验与发布

全部票冻结后，term_learning_trial_inspect 才向主 agent 提供分页局部比较；按 trial_id 和 difference_cursor（字符串）读取。由你判断是否需要补证据或核验差异，使用 term_learning_trial_judge 保存 [{trial_id,judgments:[...]}]。未知项仍进待办，不能冒充人工答案。

term_learning_trial_finalize 导出并核对真实 OpenCode 子会话、父子关系、每票唯一会话、实际模型、允许的工具、全文交付、正式提交、经验包、口径和工件哈希，再重新计算质量门槛。导出不可用、会话污染、案例不独立或有未确定问题时拒绝晋级，不以模型自述代替来源。

评测 passed 后，由你调用 term_learning_trial_publish 发布本实验绑定的候选，无需逐条人工批准。工具仍核对当前修订和基线；发布/撤回只影响新生产任务。下一候选需另准备实验，固定新的正式集合，不修改已提交生产任务快照。

success_cases 包含保持原本正确，只能说明本次验证未退化；fix_improvements 才记录本实验观察到的修复改善，均不等于全场景准确率保证。

## 人工与恢复

局部事实、适用范围争议和经验冲突通过网页待办处理；用户也可在对话中明确答复某个 review_id，再调用 term_learning_reply 保留其原话。agent 的语义判断使用 judge，不使用人工 reply。局部答复不确认整篇、不改全局业务口径。

恢复原学习使用 study_id，恢复原试验使用 experiment_id 和原任务会话；查看已冻结记录不会重做抽取。已解封标签后需要新预测时另开新会话，不覆盖历史。旧 Python job 的启动入口已停用，历史只供追溯。详细存储和数据流见 [方案说明](../../../../docs/learning-evolution.md)。
