# 经验回归清单

清单、标书和抽取结果存放在私有数据目录，不提交源码仓库。本文仅说明格式，不包含已标注案例或运行成绩。

顶层为 `{"schema_version": 1, "cases": [...]}`；路径相对于清单文件所在目录，也可为绝对路径。

## 案例字段

| 字段 | 内容 |
|---|---|
| `case_id` | 清单内唯一编号 |
| `family_id` | 文档家族；译文/修订版/近重复文件不能拆成不同家族 |
| `role` | `fix`、`success`、`regression` 或 `counterexample` |
| `split` | fix/success 使用 `development`，其他使用 `regression`；拒绝最终测试与 holdout |
| `confirmed_by` | 人工断言确认人记录，不是程序身份认证 |
| `scope_note` | 反例必填，说明为什么不应应用这条经验 |
| `index_path` | 注册后的版本 2 文档索引 |
| `baseline_result_path` | 外部实验产生的基线七字段 JSON |
| `candidate_result_path` | 外部实验产生的候选方案七字段 JSON |
| `assertions` | 非空的采购对象断言数组 |

fix 的输入哈希与家族必须对应此候选引用的纠错来源；success 的输入哈希与家族必须对应其已确认成功来源。success 的 baseline 解析后七字段 JSON 还必须与该来源已确认的不可变结果快照一致，文件排版可不同；candidate 可发生合法区间变化，按断言评价。不能借用已确认输入的名义，另造一份通过断言的 baseline 代替真实快照。

regression/counterexample 须独立于候选引用的全部开发来源（legacy correction_ids 和 source_ids）的家族及输入哈希，包括未在本轮清单写成目标案例的来源。程序识别显式家族和相同输入，不能自动辨认所有近重复文档，维护者须正确分组。

source_ids 来源由运行复盘或反馈登记。机器生成候选不等于来源已确认；任何 source_ids 未经人工确认，均不得评测或发布。旧 correction_ids 继续使用原纠错记录，不必伪造成功来源。

## 对象断言字段

| 字段 | 内容 |
|---|---|
| `object_key` | 人工对象名称或编号，仅作显示 |
| `anchor_tag` | 在设备自身 normal/separate 区段唯一定位对象的标签 |
| `required_tags` | 不能遗漏的选型参数标签，非空且包含 anchor_tag |
| `forbidden_tags` | 不得混入的其他设备参数标签，可为空 |
| `quantity` | 可选，应抽取的采购数量字符串 |
| `clause_category` | 可选，主体记录的期望分类；本版 anchor 定位的是 normal/separate 设备主体，general 通过入边检查参数覆盖 |

按索引原文顺序判断区间，不按 C 数字排序。只检查人工标注的重要要求，不推测未标注参数的重要性，不因合法无害冗余判错。

有效参数范围包括主体、关联的同设备 separate 片段，以及直接指向这些片段的 general；不能沿 general 再扩散到其他设备。数量检查主体记录，不将多个 separate 中的重复数量相加。反例须有不适用说明和 forbidden_tags 或 clause_category 等边界断言。

## 最小完整清单示意

下面是字段完整、JSON 语法合法的两案例 success 清单：一个开发正例，加一个兼作独立回归的反例。**它不是实际案例或已通过的评测**。请替换路径、家族、标签、数量、确认人和不适用说明；所有标签都必须真实存在于各自 index。两个输入必须有不同来源哈希且属于独立家族，不能只改 family_id。

```json
{
  "schema_version": 1,
  "cases": [
    {
      "case_id": "success_example",
      "family_id": "example_development_family",
      "role": "success",
      "split": "development",
      "confirmed_by": "待替换的人工断言确认人",
      "index_path": "success/index.json",
      "baseline_result_path": "success/baseline.json",
      "candidate_result_path": "success/candidate.json",
      "assertions": [
        {
          "object_key": "示意采购对象",
          "anchor_tag": "<C1>",
          "required_tags": ["<C1>", "<C2>"],
          "forbidden_tags": ["<C9>"],
          "quantity": "2",
          "clause_category": "normal"
        }
      ]
    },
    {
      "case_id": "counterexample_example",
      "family_id": "example_independent_family",
      "role": "counterexample",
      "split": "regression",
      "confirmed_by": "待替换的人工断言确认人",
      "scope_note": "示意：此处清单与正文属于不同计量对象，不能套用候选规则跨对象取量，亦不能混入相邻设备参数。",
      "index_path": "counterexample/index.json",
      "baseline_result_path": "counterexample/baseline.json",
      "candidate_result_path": "counterexample/candidate.json",
      "assertions": [
        {
          "object_key": "示意独立采购对象",
          "anchor_tag": "<C10>",
          "required_tags": ["<C10>", "<C11>"],
          "forbidden_tags": ["<C19>"],
          "quantity": "未提及",
          "clause_category": "normal"
        }
      ]
    }
  ]
}
```

success 的 family_id 和输入哈希须匹配候选 source_ids 中已 confirm-run 的来源；baseline.json 从该来源的不可变 result_path 复制，不是另跑或手写一份替代结果。候选仅含成功来源时，该清单才走 success 模式。存在纠错或混合来源时，需加入匹配纠错来源的 fix 开发案例，并满足实际改善门槛；只把上例 role 改成 fix 不会自动成为有效纠错实验。

反例需在 baseline/candidate 中均全通过，才能同时计入 counterexample_cases 和 independent_regression_cases。这是结构上的最小示意，实际覆盖应按规则可能影响的对象和边界扩展。

## 人工准备隔离实验

当前没有自动生成两组结果、复制实验环境或核验推理配置的一键运行器。以下是人工操作顺序，不表示存在未实现的 OpenCode 命令：

1. 保留被维护候选的 memory_id、revision、完整 payload 与已确认来源。根据原文预先准备关键标签、禁止混入项、数量和边界断言，保留独立反例；不要根据 candidate 的错误反复缩小断言来凑通过。
2. 为生成输出建立私有实验工作区、独立任务目录与新会话，实验 `TERM_EXTRACT_MEMORY_DIR` 与生产分开，避免实验 commit 的运行、复盘和反馈写入生产库。维护者需准备与被维护发布集合一致的实验库副本并核对 release 摘要，或在实验上下文明确提供同一份已发布规则；不能把空实验库声称为等价生产基线。备份需保持 SQLite 与归档一致；项目没有自动复制或同步工具。
3. 准备 baseline：success 直接复制已确认来源的不可变结果；纠错目标保留对应原错误结果；独立 regression/counterexample 则按当前固定业务口径和同一已发布规则集生成，不加入本轮候选。保留原始输出，不用人工正确答案替代模型结果。
4. candidate 在独立新会话中明确附上被评测候选的完整规则及适用/排除条件，按相同原文、模型和配置生成，其他条件尽量固定。不向它提供 gold、baseline 结果或人工断言答案，也不要为了让 recall 读到候选而先 publish。候选未发布，生产 recall 本来就不会返回它；此处是人工隔离注入。历史 success baseline 的生成配置若无法追溯，应记录该限制，不能声称已认证可比性。
5. 保存两组结果、输入/index、模型与推理配置、发布集合摘要、候选内容哈希、提示与运行来源。程序不会认证这些配置曾用于生成结果，也不证明候选被实际采用。模型正常提交产生的实验复盘仍留在实验库。
6. 把工件路径填入 manifest，在**被维护候选所在的目标经验库**中 evaluate；先核对终端的 TERM_EXTRACT_MEMORY_DIR，避免误用实验库。审阅完整报告、来源、输出与边界后再由维护者 publish。不要把实验库发布当成生产已生效；生产后续新任务才会固定正式发布集合。

评测调用示意（路径与编号均须替换）：

```powershell
python -X utf8 .opencode/skills/term-extract/scripts/memory_evaluate.py --project-root . --memory-id '待评测经验编号' --revision 1 --manifest 'D:\私有数据\regression.json'
```

发布、撤回及来源确认命令见 [经验维护说明](experience.md)。人工实验只提供可复查工件；当前尚无真实效果验证，不把 success 通过称为收益提升。

## 报告含义

记录每个断言的 baseline/candidate 结果、改进、退化和剩余问题，以及清单、索引、来源和输出的哈希。新报告包含 `evaluation_mode`、`summary.success_cases`、`source_confirmations`；后者精确绑定候选 source_ids 的编号和人工确认记录哈希。success 案例另记录 `success_source_id` 和已确认结果哈希。来源不可变工件、确认依据和当前 learning/evaluate 等相关源码一起绑定；发布时再次校验工件、来源确认及当前经验集合。旧版没有 mode/success_cases 的纠错报告仍由发布器按原纠错门槛解释。

模式由来源自动决定，manifest 不能指定：只要存在纠错来源即为 `correction`，否则为 `success`。混合来源允许，但不能用成功记录绕过纠错改善要求。

| 门槛 | correction 模式 | success 模式 |
|---|---|---|
| `fix_improvements` | 至少 1 个 fix 从 baseline 有断言失败改善为 candidate 全断言通过 | 不要求虚构错误或改善 |
| `success_cases` | 可记录已确认成功案例，不替代 fix 改善 | 至少 1 个 success 的 baseline/candidate 全断言通过 |
| `unfixed_target_cases` | fix/success 目标中 candidate 未全通过的案例数为 0 | 同左 |
| `regressions` | 原已通过断言变失败的数量为 0 | 同左 |
| `independent_regression_cases` | 至少 1 个独立非目标案例前后全通过 | 同左 |
| `counterexample_cases` | 至少 1 个独立反例前后全通过，有 scope_note 和边界断言 | 同左 |

所有 success 目标的 baseline 也必须全断言通过；人工确认不能使本轮已暴露的遗漏自动免责。两组结果任一存在结构 error，整体报告均失败。warnings 本身不阻断；这里不要求 evidence sidecar。

独立回归和反例只有在 baseline 与 candidate 都通过全部断言时才计入发布门槛，不能用原先已经失败的案例凑数。

评测不会启动模型；baseline/candidate 仍由外部运行器提供。成功快照绑定只证明 baseline 对应哪次已确认输出，不证明 candidate 由模型运行产生或确实注入候选经验。success 模式只说明对已确认成功经验的这些断言兼容，不证明性能收益。当前源码/config 哈希也不是生成配置的证明。

通过不等于全文语义准确；清单外参数、错误关联和未标注污染不会被自动发现。其他非目标案例中，原来失败而现在仍失败的断言不会单独增加退化计数。空断言、定位歧义、家族泄露和最终测试集混用不能冒充通过，人工身份和反例的业务充分性仍由维护者负责。
