# 提交前独立内容核验

## 目的与边界

本流程用于正常 `term-extractor` 抽取提交前的第二次内容核验，重点查目标设备漏抽、必要选型参数遗漏、其他设备参数混入和错误关联。唯一入口和最终判断者仍是主代理；`term-auditor` 是其受限子任务，不是另一套抽取或学习入口。

它与 `term-trial` 的经验独立 A/B 验证、`term_review_*` 的人工结果学习不同。审计没有人工正确标签，不训练或发布经验，不代替用户网页校验。全文独立读取会增加模型与 token 消耗；回执证明交付，不保证语义完整或查尽漏抽。

## 主代理：冻结与派发

1. 使用本会话 `term_document_read` 完整读取，形成草稿并通过 `term_result_validate` 保存七字段结果和证据。继续遵守原有业务口径、数量定义、general/separate 规则。
2. 调用 `term_audit_prepare`，传当前 `index_path / result_path / evidence_path`。工具固定原文、索引、口径和经验快照的摘要，另保存工作索引及草稿/证据的冻结副本。主代理工作稿在报告期间可以变化，子会话始终只看本票冻结原稿；不能把工作文件改动当成子会话已看到的新版本。
3. 用原生 `task` 创建新的 `term-auditor` 子会话，prompt 严格只有 `audit_id=工具返回的票号`。不附带草稿、证据答案、父会话推理、人工标签、历史同文档答案、预期错误或修复建议。审计原文和规范由票内工具交付。

每张新票使用新会话，一会话只认领一票。不要复用 `term-trial` 或已看过标签的子会话，不按每台设备另起审计者。预算不足时保留草稿并说明尚未完成审计，不能声称已成功提交。

## 子代理：先独立识别对象，再查看草稿

| 阶段 | 允许动作 | 关键约束 |
|---|---|---|
| `term_audit_open` | 认领票、接收冻结的业务参考包 | 不获取主草稿或人工标签 |
| `term_audit_read` | 按游标读完本票原文 | 到 `has_more=false`，超长单元格分片也读全；回执独立 |
| `term_audit_inventory` | 固定 `objects_json=[{title,evidence_tags}]` | 先有独立对象清单，再解封草稿；无目标对象可为空 |
| `term_audit_draft` | 分页读冻结的七字段草稿 | 不交付主代理 sidecar notes 或推理；读完全部分页才可结束 |
| `term_audit_read/search` | 针对疑点补读、查指代和上下文 | 搜索命中不取代全文读取 |
| `term_audit_finish` | 提交有证据的发现与简短总结 | 不改结果、不归纳经验、不执行 resolve |

子代理只有以上专用工具，默认拒绝普通文件 read、文档/结果/记忆/标签学习/人工网页校验、联网和 task。父会话回执不能补齐子会话覆盖，子会话回执也不能补齐父会话覆盖。冻结规则包和原文若变化，应停止并由主代理重新建票，不掩盖漂移。

对象清单只保存名称及定位依据，例如：

```json
[
  {"title": "Firewall", "evidence_tags": ["<C20>", "<C28>"]},
  {"title": "Router", "evidence_tags": ["<C29>"]}
]
```

它不是另一份完整七字段结果，也不是人为正确标签。此阶段不复述所有技术参数。

## 发现格式

`term_audit_finish` 的 `findings_json` 是数组，每项字段如下；`finding_id` 由工具产生。

| 字段 | 含义 |
|---|---|
| `type` | missing_object / boundary_truncated / parameter_pollution / association_error / scope_uncertain |
| `clause_ids` | 涉及的冻结草稿 id；漏对象等没有对应记录时可为空 |
| `evidence_tags` | 本票原文中真实的定位依据 |
| `finding` | 可检查的问题与其业务影响 |
| `suggested_action` | 建议补读、补全、拆分、缩边或核对关联，不直接改稿 |
| `certainty` | confirmed 或 uncertain；疑问不能伪装成确定错误 |

同时传简短 `summary`。无真实发现时用 `[]`，说明检查范围及局限，不为凑疑点造错。少量不影响选型的说明文字不自动算污染；跨页续表、连续两行和多个 C 标签不自动意味着 separate。

## 主代理：回读、处理与提交

1. 用 `term_audit_inspect` 沿 `next_cursor` 分页读完发现，不能只采信子会话的结束消息或摘要。
2. 对发现通过主会话 `term_document_read` 回读其证据及必要邻域；主代理独立决定对象身份、边界和关联。不要只凭“审计者说错了”改稿或采纳。
3. 落实需要的修正，再调用 `term_result_validate` 保存最终结果和 sidecar。若确无发现，仍可使用相同的最终稿。
4. 调用 `term_audit_resolve(audit_id, result_path, evidence_path, decisions_json)`。`decisions_json` 必须逐项覆盖全部发现：

```json
[
  {
    "finding_id": "工具返回的发现编号",
    "decision": "fixed",
    "reason": "回读续表确认同一防火墙的必要要求，已延长终止边界且未跨入 Router。",
    "evidence_tags": ["<C20>", "<C28>", "<C29>"]
  }
]
```

`fixed` 必须落实针对该问题的结果修正，不能只改 notes、清空疑点或修改 sidecar；`dismissed` 要说明原文支持的不采纳理由；`unresolved` 表示尚不确定，会阻断提交并保留草稿。无发现时 decisions_json 可为 `[]`。主代理必须在报告完成后回读原发现与处置决定两组证据标签的并集，不能只读无关标签、借用先前回执或子会话回执。工具核对处置依据、当前最终文件和未决状态；它不能证明每条语义决定正确。

首次处置可为真实 `fixed` 发现接受修改后的结果；该结果与冻结稿不同时，记录 `final_draft_independently_reviewed=false`。无发现或全部 `dismissed` 时，结果若相对冻结稿发生变化，必须新建核验票，不能把无发现报告套到另一份结果上。

`cleared` 表示本票已关闭，记录绑定最终结果与 sidecar 的哈希。已经 cleared 后，任一文件哈希再变都必须新票、新子会话，不能用旧报告重新 resolve。尚未 cleared 的 unresolved 记录可以补齐证据和决定，但不能省略核验或把未知问题自动变为无问题。先确定本次可承受的轮数，通常集中处理一轮，不能无上限多代理往返；预算或证据不足时保留未完成草稿并交代缺口。

最终稿通常包含主代理根据报告所做的修改。因此只能说“已独立核验冻结草稿，主代理回读并处置全部发现”；不能说子代理看过修改后版本，除非确实创建新票完成了新一轮核验。

正常真实 OpenCode 主代理 `term_result_commit` 强制核对本阶段。`term-trial` 保持原盲抽 A/B 协议，不加此审计；没有 `execution_context` 的历史 Python CLI 保持兼容，也不能作为 OpenCode 主代理规避门槛的方法。原始结果七字段、证据 sidecar、复盘、候选发布和网页人工校验协议保持不变。

提交记录中的 `agent_audit` 保存核验票、报告与处置路径、发现数、处理统计，以及“最终稿是否再次独立核验”的标记。主代理交付时报告这些信息，便于追查。核验票据和状态在私有 SQLite 追加保存，冻结索引、草稿、报告和独立读取回执位于私有库目录的 `agent-audits/<audit_id>/`；它们不写入 Git，也不会作为可信标签自动入库。

草稿按完整条目交付，单条超过 24 KiB 会在 prepare 阶段明确拒绝。应精简被误用来复述全文的标题等结果字段；原文技术参数由原文读取工具分片交付，不截断原文。
