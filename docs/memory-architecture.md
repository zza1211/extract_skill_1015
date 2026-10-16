# 经验记忆架构（0.6.2）

OpenCode `term-extractor` 是唯一执行入口；Python 原子工具不调模型、不预先编排模型调用。网页管理同一私有记忆库。业务过程见[演进方案](learning-evolution.md)，网页操作见[记忆工作台](memory-dashboard.md)。

## 1. 三层知识，分别保存

| 层 | 保存位置 | 使用方式 |
|---|---|---|
| 固定业务口径 | Git 中 `references/business-rules.md` | 每次读取；明确约束目标设备、general/separate 与排除范围 |
| 可迁移经验 | SQLite 不可变修订与正式发布指针 | 按冻结快照、关键词少量召回 |
| 案例与实验 | 私有输入、标签、运行和会话工件 | 支持学习、复核与晋级，不直接作为生产抽取答案 |

纯抽取的原文观察、用户提供的正确标签、代理的局部判断、人工待办答复属于不同依据。不能把模型自检当作可信标签，也不能把局部答复扩展成整篇验收。

## 2. 控制流与数据流

```text
用户 → OpenCode term-extractor（自主理解与工具选择）
        ├─ document/result → 原文索引、读回执、校验、run、reflection
        ├─ learning begin → 私有可信标签；此时不返回答案
        ├─ learning compare → 检查本会话run，冻结首次预测，再交付对照
        ├─ learning judge/reflect → 局部判断、待办、候选
        └─ trial prepare → 原生task创建全新term-trial会话
              └─ open/read/search/checkpoint/validate/commit
                    → 全部完成后inspect/judge
                    → finalize读取真实OpenCode export并核验
                    → 注册实验 → 门槛通过后发布

网页 → 本机 HTTP → 同一库的查看、案例导入、待办答复、经验维护
        └─ 人工审核指定修订 → 核对来源/冲突 → 保存审核记录 → 发布
```

程序保存和检查事实，代理安排下一步；没有网页后台模型运行器。旧 `learning_runner.py` 只读 0.5.0 历史任务，执行和设置入口已停用并明确报错。

## 3. 私有文件布局

默认使用 `.term-memory/`，可通过 `TERM_EXTRACT_MEMORY_DIR` 外置。OpenCode 和网页必须使用同一目录。

```text
.term-memory/
├─ memory.sqlite3
├─ learning-artifacts/     run、证据、反馈等内容地址归档
├─ evolution-artifacts/    可信案例的输入、标签与内部索引
├─ agent-trials/
│  └─ agentexp_*/trial_*/   独立试验输入、索引、冻结结果与真实会话导出
└─ .workflow/jobs/         0.5.0 历史作业工件；不用于新任务

runs/                     自主抽取的索引、回执、草稿、报告和结果
```

目录为示意，工件以注册记录中的实际路径和摘要为准。源文件、索引和归档都是核验依据；不能只保留数据库就删除外部文件。私有目录、真实标书、标签和运行输出不随代码推送。

## 4. SQLite 事实、事件与当前状态

| 表组 | 保存什么 |
|---|---|
| revisions | 规则 payload、修订号与内容摘要 |
| active | 当前生效修订及发布依据；评测准入或独立人工审核 |
| evaluations / used_evaluations / audit | 报告、消费状态和维护审计 |
| learning_runs / feedback / sources / confirmations / events | 不可变运行、复盘、反馈与旧人工确认 |
| corrections | 兼容旧人工纠错来源 |
| evoz_cases | 可信标签声明、案例族、用途与工件摘要 |
| evoz_reviews / resolutions | 网页与对话共享待办及局部答复 |
| evoz_decisions / origins | 候选维护结论、产生来源与证据 |
| evoz_conflicts / scans | 疑似冲突、扫描签名与周期依据 |
| evoz_experiments | 重新计算的配对实验与准入依据 |
| agent_studies / agent_learning_events | 学习身份、首次冻结、判断、复盘等追加事件 |
| agent_trial_plans / claims / results / judgments / evaluations | 试验票号、限定会话、真实提交和局部复核 |

0.5.0 的 `workflow_jobs / workflow_job_events / workflow_settings / workflow_claims` 作为历史保留，新流程不以它们调度模型。

事实和修订表通过触发器阻止 UPDATE/DELETE。修改规则产生新 revision，状态根据事件重建。`active` 是有意可变化的正式指针，因此全库不是完全不可变。

上下文由 OpenCode 工具注入 `session_id / message_id / agent`；模型不能通过工具参数自填。读取和提交回执也绑定上下文。晋级时再读取真实导出会话，核对父子关系、工具、模型、变体和完成的提交。哈希证明文件一致，不能证明语义正确，也不是抵抗本机管理员篡改的签名。

## 5. 一条经验实际长什么样

规则修订保存短规则及适用条件，例如：

```json
{
  "title": "按明确采购编号关联数量",
  "issue_type": "quantity",
  "triggers": ["采购清单", "数量", "BOQ"],
  "applies_when": "规格与采购清单分开，且有明确编号对应同一设备",
  "rule": "核对该采购项的数量，并保留原文依据。",
  "exclusions": "不把端口数、授权数或其他设备数量作为整机数量。",
  "correction_ids": [],
  "source_ids": ["可信来源编号"]
}
```

这是结构示例，不是内置已发布经验。候选另有 `memory_id / revision / content_sha256`；观察原话、证据标签、首次预测、标签差异、人工答复和实验结果保存在来源或事件中，不在每次召回展开全部历史。单条 payload 规范 JSON 上限 4096 字节。

来源增加或规则修改会形成新修订；确定重复可合并来源，相似但条件不同不会仅凭关键词自动合并。候选不自动进入正式集合。

## 6. 标签学习的隔离和冻结

`agent_learning.begin_study` 绑定输入、索引、案例族和当前会话，私有导入可信标签；安全摘要不含标签正文、标签路径或对照原文。

主代理继续正常自主抽取。`compare_study` 只接受同一会话完成全文读取并正式提交的 run；首次预测冻结后才能分页读取标签对照。其他会话和试验代理不能读取该学习标签。看过另一篇答案的会话不得继续新一篇盲抽学习。

`judge_study` 保存主代理局部语义结论，未决项建共享待办；`reflect_study` 保存可迁移候选或无新经验原因。这个阶段不由 Python 再调用一个总结模型。

可信标签无需旧版逐条人工确认，但结构校验不证明标签完全正确。网页显示安全学习摘要，可信案例详情供维护者管理；盲抽代理不能把网页接口当作任意数据通道。

## 7. 独立试验与自动晋级

主代理选择案例角色及相关正式经验，`prepare_experiment` 冻结明确的试验计划。每个案例产生 baseline、candidate 两张票；原生 task 提示只能包含票号，每票独占全新 `term-trial` 子会话。

试验子代理只读取本票原文、静态规范和冻结经验包，用限定工具自主抽取。全部结果提交后才允许 inspect 交付局部标签差异。主代理复核 review 项，无法确定的转人工；确定硬错误不能被局部意见覆盖。

`finalize` 不重新抽取，而是通过 `opencode export` 读取真实已发生的会话，验证父子会话、唯一票号提示、工具范围、真实模型/变体、完成的 commit、读取记录与工件。全轮 A/B 和局部复核需使用同一实际模型及变体。没有可用导出不允许晋级。

注册实验重新比较完整对象、有效范围、数量和关联，产品分类不计分。至少包括来源绑定的 development、独立 regression 和触及适用／排除边界的 counterexample；回归、反例与归纳来源独立，回归和反例来自两个独立案例族，holdout 禁止进入本轮调参或晋级。

`finalize` 仅核验并登记评测；主代理看到通过后，再调用 `term_learning_trial_publish` 读取本计划绑定的最新 passed 评测发布。达到正确、无已检查退化、无未决冲突、工件与当前口径/发布集合一致，才允许生效。自动发布无需逐条人工批准，证据不足仍保留候选。`success_cases` 可能只是保持原本正确，只有 `fix_improvements` 表示本轮观察到修复，均不能直接证明长期效果。

人工直发另有独立路径：网页或人工 `publish-reviewed` CLI 绑定精确 revision 和内容摘要，核对真实来源工件、当前口径及未决冲突，再保存审核人、理由、时间和 `approval_id`。它不要求整篇来源已确认或先产生评测；不会更新来源确认表、生成可信标签或消费评测报告。发布依据为 `human_review`，`evaluation_id=null`；无评测记录标记 `not_evaluated`，有报告但未采用本次评测准入标记 `not_adopted`。该事实与规则一起保留，不能把人工批准渲染成评测通过。代理无这项人工审核工具。

## 8. 正式经验怎样消费

每次准备输入固定 `memory_snapshot.json`，绑定 index 和正式集合。规则内容与版本在快照中，历史标书全文不在。

`term_memory_recall` 使用有限繁简及中英别名归一化、英文词项和中文双字词；触发词交集权重 3，规则正文交集权重 1，只选正分项并稳定排序。默认最多三条、6144 字节，完整规则在预算内交付，放不下的明确说明。没有向量模型或全文向量索引。

主代理围绕边界、数量、关系等实际疑点消费经验。记录交付版本不等于证明已采用；召回仍可能遗漏语义相近场景。

候选只在明确试验中注入。实验另固定当时正式基线；集合中途变化需重新建立该项基线，不能改写已有生产结果。发布或撤回只影响之后新准备的任务，旧快照仍可追溯。

## 9. 冲突、人工与网页

新增候选及业务口径变化时扫描；可写网页服务运行期间每 60 秒检查到期，默认七天复扫，服务停止时不执行。维护只记录疑点，不调用模型，不自动改写、发布或撤回。可手动强制扫描，或通过 `--read-only / --no-maintenance` 禁用后台维护。

网页答复与 `term_learning_reply` 写入相同待办，保留用户原话、作用范围和审计。代理的 judge 是模型判断，不能包装成用户答复。局部答复不确认整篇、所有经验或全局口径。

网页可导入可信案例、修订候选、人工审核发布、撤回和维护旧来源；没有模型设置、创建或继续任务按钮，旧 HTTP 执行入口明确拒绝。模型、调用和子任务由 OpenCode 管理。人工审核只批准单条修订，不批准来源整篇输出；未解决的阻塞冲突、来源缺失或变化仍阻止人工发布。

## 10. 兼容与局限

旧运行反馈、人工来源确认、离线断言、评测后手动发布与撤回继续可用。旧断言评测只检查提供的输出，不生成模型结果；不能据此跳过新自动晋级的独立试验和来源门槛。人工直发使用独立审核依据，不放宽旧 `publish` 的评测验证。撤回后恢复需重新审核或重新验证发布。详见[经验维护说明](../.opencode/skills/term-extract/references/experience.md)与[旧断言清单](../.opencode/skills/term-extract/references/memory-evaluation.md)。

当前不自动保证全部标签可靠、案例族语义独立、反例充分或所有语义冲突被发现。局部摘录可能截断，需要代理继续读取或人工判断；长上下文与总成本仍取决于模型和任务规模。没有旧网页运行器那套自动预算配置，实际调用以 OpenCode 记录为准。

软件测试检查数据边界、阶段门槛和工件一致性；真实质量、长期稳定性、成本收益仍需独立标注集评估。

建议阅读顺序：`SKILL.md → agent_learning.py → agent_trials.py → learning_compare.py → evolution_store.py → memory_store.py`。网页适配层为 `memory_ui.py / memory_dashboard.py / evolution_dashboard.py`，它们不驱动模型。
