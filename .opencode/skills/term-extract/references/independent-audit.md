# 提交前独立内容核验

## 目的与边界

正常 `term-extractor` 提交前，由受限 `term-auditor` 独立检查目标设备漏抽、关键选型参数遗漏、其他设备参数混入和错误关联。唯一入口和最终判断者仍是主代理。审计没有人工正确标签，不修改结果、不学习经验，也不代替网页整篇校验；它与 `term-trial` 经验 A/B 验证保持分开。

新 prepare 票固定 `protocol_version=2`，增加设备级清点及逐对象对应记录。旧票没有版本号时按 v1 处理，不重写旧清单或强加 v2 coverage。程序检查交付与记录完整性，不能据此证明对象识别或语义判断正确。

## 主代理：冻结与派发

1. 用本会话 `term_document_read` 读完整原文，形成草稿并通过 `term_result_validate`。业务范围、数量定义、general/separate 和七字段协议保持不变。
2. 调 `term_audit_prepare(index_path,result_path,evidence_path)` 固定索引、原文、规范和经验快照摘要，保存草稿/证据冻结副本。报告期间主工作稿可变化，子会话仍只看本票原稿。
3. 用原生 `task` 新建 `term-auditor` 子会话，prompt 严格只有 `audit_id=返回的票号`。不附草稿、sidecar notes、父推理、标签、历史答案或预期发现；保存原 task 返回的 task_id。

新票使用新子会话，不按设备另起审计者。中断先查询状态并恢复原票，处理办法见下文；预算不足时保留未完成草稿，不把读取或部分报告当成提交成功。

## 子代理：独立阅读全文和设备清点

| 阶段 | 工具与约束 |
|---|---|
| 认领或恢复 | `term_audit_open` 返回本票版本、阶段、游标和冻结规范；不交付主草稿 |
| 原文读取 | `term_audit_read` 按游标读完全文及超长单元格分片；搜索、摘要和父回执不能代替 |
| 固定清单 | `term_audit_inventory` 在全文交付后保存独立采购对象；之后不能根据主草稿反改 |
| 对照草稿 | `term_audit_draft` 分页交付冻结七字段结果，不交付主 sidecar notes 或推理 |
| 补证与报告 | 本票 `read/search` 针对疑点取证，读完全部草稿后 `finish` |

子代理只有票内工具，不能用普通文件、文档/结果/记忆/标签学习/人工网页校验、网络或 task。父子回执分开，不能互相补足。材料或规范漂移时停止旧票，不掩盖变化。

### 清点粒度

每项是具有选型要求的设备，或有明确文档依据的整体系统，不能仅列章节或方案名。

- `Wi-Fi Network` 下若分别有无线控制器和 AP 的要求，分别列为 equipment；同表、同段或成包采购不自动将它们合并。不能用一个 Wi-Fi Network 总项替代这两个对象。
- 标书明确将计算节点、存储和虚拟化等作为同一超融合系统配置时，保留 integrated_system，并说明合并依据。系统服务器、虚拟化软件、内置模块、天线和安装配件不因此强制变为独立采购；若原文明确另列独立目标设备，再单独识别。
- 支持某功能、接口或兼容某设备，不等于采购该设备。明确独立规格的光模块等仍按业务规则识别；机柜、布线等排除项不因独立清单行而进入清点。
- 合并或目标范围不能确认时保留实际设备线索，后续用 scope_uncertain 说明事实缺口，不用猜测写 system_basis。清单检查还应关注正文中的设备要求，不能只依赖采购清单。

v2 的 objects_json 例如：

```json
[
  {"title":"Wireless Controller","object_kind":"equipment","evidence_tags":["<C12>"]},
  {"title":"Access Points","object_kind":"equipment","evidence_tags":["<C20>"]},
  {"title":"Hyper-Converged System","object_kind":"integrated_system","system_basis":"原文要求计算节点与虚拟化软件作为同一超融合系统配置和交付。","evidence_tags":["<C40>","<C48>"]}
]
```

object_kind 必填 equipment 或 integrated_system；后者还须有不超过600字的 system_basis。title 与 evidence_tags 均来自本票原文。工具按顺序生成 o0001 等 object_id 并返回，之后使用返回编号，不自行改号。只存名称、类型和简短事实依据，不复述全部参数，不要求给系统每个部件各建对象。确实没有目标对象时可提交空清单并在最终 summary 说明原因。

## 发现与逐对象对应

先读完冻结草稿，再围绕独立清单对照原文：漏对象、关键参数截断、参数污染及 general/separate 的作用范围。连续表格行、多 C 标签和跨页续表不自动成为 separate；少量不影响选型的说明不自动成为污染。

`term_audit_finish` 的 findings_json 每项字段：

| 字段 | 含义 |
|---|---|
| type | missing_object / boundary_truncated / parameter_pollution / association_error / scope_uncertain |
| clause_ids | 涉及的冻结草稿 id；没有对应记录的漏对象可为空 |
| evidence_tags | 本票真实的原文依据 |
| finding / suggested_action | 可检查的问题及建议动作，不直接改稿 |
| certainty | confirmed 或 uncertain；不能把猜测写成确定错误 |

工具按 findings 数组顺序生成 f0001、f0002 等 finding_id，不在 findings 中自填编号。v2 finish 还须提交 coverage_json，每个清点对象恰好一项，例如：

```json
[
  {"object_id":"o0001","status":"covered","clause_ids":[0],"finding_ids":[],"reason":"条款0承载该无线控制器的设备要求。"},
  {"object_id":"o0002","status":"missing","clause_ids":[],"finding_ids":["f0001"],"reason":"草稿没有AP对象，第一条发现定位到该AP的要求。"},
  {"object_id":"o0003","status":"covered","clause_ids":[1],"finding_ids":[],"reason":"条款1涵盖有据的超融合整体系统。"}
]
```

- covered 至少对应一条非 general 条款，不能只凭通用约束认为设备已抽到；同一对象真实 separate 片段可对应多条。
- missing 的 clause_ids 必须为空，并关联 missing_object 发现。uncertain 必须关联真实发现，说明尚不能确定对应关系的原因。
- missing/uncertain 所关联发现的证据，必须与该对象的 evidence_tags 相交；不能用另一设备的发现凑映射。finding_ids 按本次 findings 的顺序编号引用。
- reason 简述对应或缺失依据。coverage 齐全不等于每项匹配正确，covered 也不保证参数、数量和关系正确。

没有真实发现时 findings_json 可为 `[]`，有对象仍须逐项给 coverage。确实没有目标对象时两个数组都可为空，summary 说明独立检查范围和原因。无对象、空报告或程序通过都不成为人工正确标签。v1 旧票继续其原发现协议。

报告保存清单和逐对象映射。主代理默认用 `term_audit_inspect(section="findings")` 分页读发现；核对设备清点和对应关系时用 `section="coverage"`，按该 section 的 next_cursor 继续，不能把一页当成全部。不要只采信子会话结束摘要。

## 中断恢复与废弃

主代理先调 `term_audit_status(audit_id)`。它只给阶段、worker_session_id、continue_task_id 和读取/草稿进度，不交付报告答案。常见阶段及动作：

status 是持久化进度，不证明原生任务是否仍在运行。原任务仍在进行时先等待，不并发恢复或废弃；确认中断后才续接。

| phase | 主代理动作 |
|---|---|
| prepared | 尚无 worker 时新建 term-auditor task，只传本票 audit_id |
| reading / inventory_frozen / draft_reading | 用返回的 continue_task_id 作为原生 task 的 task_id，恢复同一票、同一子会话；prompt 仍只有 audit_id=票号 |
| reported / needs_review | 报告已完成，优先 inspect 和处理发现，不重开审计 |
| cleared | 使用绑定的最终稿继续 commit，不重做已完成核验 |
| abandoned | 旧票不可继续、不可提交，也不能继承其覆盖 |
| invalid / integrity_error 非空 | 冻结材料或回执有问题，停止旧票；未 cleared 的票可废弃后重新准备 |

恢复后的子代理再次 open，沿工具给的阶段和首个覆盖缺口继续；已冻结 inventory 不重写，已读完原文不从零再读。continue_task_id 绑定已认领 worker；如果原生 task 因找不到旧任务而创建新会话，票的 claim 检查会拒绝，不允许新子会话接旧票。

确实无法恢复原 worker，或 status 报 integrity_error 时，未关闭票的主代理记录原因并用 `term_audit_abandon(audit_id,reason)` 废弃旧票；重新 validate 当前工作稿，再 prepare 新票、新子会话并独立读全文。已 cleared 的票不能废弃，依据变化需另准备新票。不要直接换 child 继承旧回执，不自动反复重启。废弃不删除历史、不表示审计完成，也不负责取消原生任务；应说明已完成什么、还缺什么和是否有预算继续。

## 主代理：处置和提交

1. 分页读完发现，再通过主会话 `term_document_read` 回读原发现与处置证据两组标签的并集。不能只读无关标签、借用之前回执或子回执。
2. 按原文修订并 `term_result_validate`，再调用 `term_audit_resolve` 一次覆盖全部 finding_id。每项含 decision=fixed/dismissed/unresolved、reason 和 evidence_tags；无发现时可为 `[]`。
3. fixed 必须落实对应结果修正，不能只改 notes 或 sidecar；dismissed 要有原文支持的不采纳理由；unresolved 阻断提交。程序检查记录和交付，不证明语义决定正确。

首次处置可为真实 fixed 发现接受修订稿；若与冻结稿不同，记录 final_draft_independently_reviewed=false。无发现或全部 dismissed 而结果变化时必须新票。cleared 后，最终结果或 sidecar 任一哈希再变都必须新票、新子会话，不能用旧报告重新 resolve。尚未 cleared 的未决项可以继续补证和处置，但不做无上限往返。

主代理修改后的版本，不能称为子代理已再次看过。正常真实 OpenCode 主代理 commit 核对本门槛；term-trial 保持原 A/B 协议，无 execution_context 的历史 Python CLI 保持兼容，不是绕过入口。核验增加全文读取与模型成本，不承诺省 token 或找尽漏抽。

提交记录的 agent_audit 保留核验票、报告与处置路径、发现/处置统计、对象清点数与覆盖统计，以及最终稿是否再次独立核验的标记。覆盖统计针对子代理看到的冻结草稿，不是修订后成稿的复核成绩；原始 missing 发现被修正后不改写历史映射。工件在私有库 agent-audits/<audit_id>/，不进入 Git 或自动成为标签。单条草稿超过24 KiB会在 prepare 早拒；应精简复述原文的过长标题，原文参数仍由读取工具分片交付。
