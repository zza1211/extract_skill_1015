# 疑点引擎 v1

疑点引擎用于把“可能漏抽、边界不清、参数污染或关联不明”从主 Agent 的自由文本判断，转成可定位、可处理、可复盘的记录。它不调用模型，不替主 Agent 判断设备类别，也不把信号当成业务错误结论。

当前流程是：

```text
prepare / 原文 index
        ↓
term_doubt_scan(index_path)
        ↓
主 Agent 阅读、形成候选和七字段草稿
        ↓
term_doubt_scan(index_path, result_path, evidence_path)
        ↓
term_doubt_record：把风险信号整理为疑点账本
        ↓
局部 read / search / 经验 recall
        ↓
term_doubt_resolve：resolved / dismissed / open
        ↓
validate / commit / 网页人工校验
```

`term_doubt_scan` 当前生成的信号包括：

- 结果为空或原文中存在未覆盖的设备、采购和技术线索；
- 非 `general` 条款区段重叠，可能存在重复抽取或参数污染；
- `general` 未关联对象；
- `separate` 只关联 `general`，可能缺少主设备关联；
- 条款终止位置后紧邻新的设备或技术线索；
- 既有 evidence sidecar 中留下的待确认问题。

扫描结果保存在本次工作目录的 `draft/doubt-signals.json`。每条信号有 `id`、`type`、`impact`、`confidence`、`evidence_tags`、`clause_ids`、触发事实和建议动作。`confidence` 目前是 C0 到 C3 的证据等级，不是校准过的概率：C0 表示只由结构线索触发，C1 表示有原文线索，C2 表示已有局部证据，C3 表示已经完成针对性核验。

主 Agent 用 `term_doubt_record` 保存 `draft/doubt-ledger.json`。账本中的疑点必须引用本次信号、当前结果条款或真实 C 标签，并说明：

- `type`：`missing_object`、`coverage_gap`、`boundary`、`parameter_pollution`、`association_error`、`scope_uncertain` 或 `evidence_gap`；
- `hypothesis`：待检查的业务假设；
- `triggers`：触发该疑点的原文事实；
- `impact`：如果成立，对选型结果的影响；
- `next_action`：应补读、搜索、拆分、合并或请求人工确认的动作；
- `status`：`open`、`resolved` 或 `dismissed`。

`term_doubt_resolve` 只记录主 Agent 已经做出的原文处置，不替它读取原文。`resolved` 和 `dismissed` 都必须带理由和证据标签；证据不足时保持 `open`。第一版账本是质量记录和后续评测数据，尚未把高影响疑点自动接入 `term_result_commit` 的硬阻断，以便先观察误报、漏报和成本，再决定是否加门槛。

经验消费可以使用疑点信号中的未覆盖线索作为查询词，不必等设备已经进入候选。已发布经验仍只提供适用和排除条件，不能取代原文。未发布候选、疑点账本和程序信号不会自动进入生产召回。

后续评测重点是：疑点对真实漏抽和边界错误的召回率、误报率、处理后新增错误、不同影响等级的校准情况，以及每篇增加的 token 和耗时。没有这些数据前，不把 C0-C3 转成数字概率，也不根据单次运行自动跳过人工或局部核验。
