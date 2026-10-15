# 经验回归清单

清单、标书和抽取结果存放在私有数据目录，不提交源码仓库。本文仅说明格式，不包含已标注案例或运行成绩。

顶层为 `{"schema_version": 1, "cases": [...]}`；路径相对于清单文件所在目录，也可为绝对路径。

## 案例字段

| 字段 | 内容 |
|---|---|
| `case_id` | 清单内唯一编号 |
| `family_id` | 文档家族；译文/修订版/近重复文件不能拆成不同家族 |
| `role` | `fix`、`regression` 或 `counterexample` |
| `split` | fix 使用 `development`，其他使用 `regression`；拒绝最终测试与 holdout |
| `confirmed_by` | 人工断言确认人记录，不是程序身份认证 |
| `scope_note` | 反例必填，说明为什么不应应用这条经验 |
| `index_path` | 注册后的版本 2 文档索引 |
| `baseline_result_path` | 外部实验产生的基线七字段 JSON |
| `candidate_result_path` | 外部实验产生的候选方案七字段 JSON |
| `assertions` | 非空的采购对象断言数组 |

fix 的输入哈希与家族必须对应此经验引用的人工纠错；其他角色须独立于全部来源纠错家族和输入。程序识别显式家族和相同输入，不能自动辨认所有近重复文档，维护者须正确分组。

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

## 报告含义

记录每个断言的 baseline/candidate 结果、改进、退化和剩余问题，以及清单、索引、来源和输出的哈希。发布时再次校验外部工件及当前经验集合。

独立回归和反例只有在 baseline 与 candidate 都通过全部断言时才计入发布门槛，不能用原先已经失败的案例凑数。

评测不会启动模型，不认证输出由哪条经验导致，不替代人工业务标签或未见文档评测。结构错误、空断言、定位歧义、家族泄露和最终测试集混用均不能被算作通过。
