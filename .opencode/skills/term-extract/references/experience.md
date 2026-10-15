# 经验记忆与发布（0.2.0）

业务口径仍由 `business-rules.md` 定义。经验用于提醒判断边界和核验动作，不保存某份标书的固定答案，不覆盖当前原文。仓库不附带案例或记忆数据库。

## 存储与职责

使用 Python 标准库 SQLite，无需额外模型、向量库或服务。默认存于项目 `.term-memory/`，已被 Git 忽略；可用 `TERM_EXTRACT_MEMORY_DIR` 指定私有目录。自定义路径若在仓库内，须自行加入 `.gitignore`。

- 纠错：记录人工确认人、原因、文档家族、输入及错误/正确输出的引用与哈希。原始标书不复制进技能；外部文件缺失或哈希变化时需重新导入和评测。
- 经验：不可变的编号和修订，包含触发词、适用条件、处理原则、不适用情况及来源纠错。新修订不覆盖当前已发布修订。
- 评测与审核：报告绑定经验内容、当前已发布集合和评测工件哈希；发布记录保留审核人、说明和历史。

日常 `term-extractor` 只允许召回；`term-memory-curator` 可读取指定纠错、整理候选和运行离线回归，两者都无发布、撤回或 shell 权限。审核人字符串是维护记录，不是身份认证；实际权限边界依赖 agent 工具权限、操作系统和私有目录访问控制。

## 抽取时使用

`term_document_prepare` 固定本任务已发布经验快照，空库也生成空快照。形成采购对象候选后，针对具体疑点调用 `term_memory_recall`：

- 按触发词、文本相关性和可选错误类型筛选，默认最多三条、6 KiB；工具上限五条、8 KiB。不匹配则返回空列表，不把全库加入上下文。
- 核对适用条件和不适用情况，再回原文取证；相关性分数不代表规则适用或正确。
- 记录实际交付的经验编号、修订和哈希。交付不等于采用，也不证明采用正确；`run.json` 保存快照及交付清单。
- 快照在本任务冻结。发布和撤回只影响之后注册的任务；已有任务可按原快照回放。需要立即切换时使用新的任务目录注册，不覆盖旧快照和回执。

检索不读取历史 `runs/`，也不把当前结果自动写回全局经验。候选不会进入日常召回。

## 导入纠错与形成候选

维护者先导入已确认输入、错误结果、正确结果和原因。下面的路径和人员仅说明参数，需替换为私有文件和真实记录：

```powershell
$memoryScript = '.opencode/skills/term-extract/scripts/memory_store.py'
python -X utf8 $memoryScript --project-root . import-correction --input 'D:\数据\input.json' --before 'D:\数据\bad.json' --after 'D:\数据\gold.json' --reason '人工确认的修改原因' --confirmed-by '标注人' --family-id '文档家族编号'
```

选择 `term-memory-curator`，提供返回的 correction_id，整理最小经验候选。已确认的正确结果可直接作为来源，无需重复确认同一标签；泛化后的适用范围仍需核验。

候选 `payload_json` 字段：

| 字段 | 内容 |
|---|---|
| `title` | 简短错误描述 |
| `issue_type` | `missing_product`、`boundary`、`quantity`、`association` |
| `triggers` | 少量有区分度的中英文触发词数组 |
| `applies_when` | 适用前提 |
| `rule` | 判断或核验动作，避免复制具体参数、数量和答案 |
| `exclusions` | 不适用情况和容易混淆的反例 |
| `correction_ids` | 已导入的人工纠错编号数组 |

`term_memory_propose` 只创建候选。修订已有经验时填写 memory_id，会生成新的 revision；当前已发布版本不变。

## 回归与人工发布

清单格式见 [经验回归清单](memory-evaluation.md)。按人工关键参数断言检查给定 baseline/candidate 输出，允许无害冗余，不要求整段文本或边界与金标准完全一致。

```powershell
python -X utf8 .opencode/skills/term-extract/scripts/memory_evaluate.py --project-root . --memory-id '经验编号' --revision 1 --manifest 'D:\数据\regression.json'
```

curator 也可调用 `term_memory_evaluate`。工具不自动启动抽取模型；两组输出应由外部实验在可比较配置下产生，保留运行来源。人工正确输出不能冒充采用候选经验后的模型输出。

发布要求：来源错误得到改善，所有目标纠错断言通过，已有通过断言没有退化；独立家族回归和不适用反例都需在 baseline 与 candidate 中通过全部断言，保护原来已正确的场景。反例可兼作独立回归。译文、修订版和近重复文档属于同一家族；最终测试集不得参与本轮经验归纳和发布调参。

维护者审阅候选、报告、输出来源和适用边界后，通过 CLI 发布；agent 没有对应工具：

```powershell
python -X utf8 $memoryScript --project-root . show --memory-id '经验编号' --revision 1
python -X utf8 $memoryScript --project-root . show-evaluation --evaluation-id '评测编号'
python -X utf8 $memoryScript --project-root . publish --memory-id '经验编号' --revision 1 --evaluation-id '评测编号' --reviewer '审核人' --note '已核对适用边界、反例、回归和输出来源'
```

发布会重新核对修订、报告、外部工件和当前经验集合，拒绝过期报告或退化。不要求每次日常抽取等待审批。报告只证明给定输出满足清单断言，不证明全面业务正确或改善一定由经验导致。

```powershell
python -X utf8 $memoryScript --project-root . withdraw --memory-id '经验编号' --reviewer '维护人' --note '撤回原因'
```

撤回停止新任务使用经验，不删除历史。恢复旧原则应明确选择修订，针对当前经验集合重新评测并发布，不自动恢复旧版本。
