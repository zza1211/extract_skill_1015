# 经验复盘、反馈与发布（0.6.2）

OpenCode term-extractor 主 agent 自主编排抽取与学习，网页管理记忆。标签学习使用 begin 登记、正常抽取与 commit、compare 冻结预测后揭示标签，再 judge/reflect；自动发布须由主 agent 调原生 task 的 term-trial 子 agent 独立验证，finalize 通过后调用 publish。人工另可在网页审核指定修订并直接发布，无需先评测。Python 只提供原子工具，不调模型、不自动排后续步骤。可信标签不重复执行旧来源确认。详见[两种模式](learning-modes.md)和[完整演进方案](../../../../docs/learning-evolution.md)。

下文保留正常抽取所需的 `reflection_json` 字段，以及旧反馈、人工来源确认、curator、离线断言与手动发布协议。旧离线评测不生成模型输出；新独立实验核对原生 OpenCode 子会话的工具调用与冻结结果。两者共用存储、修订和发布集合，不能混淆证据门槛。

维护者可以使用[本地工作台](../../../../docs/memory-dashboard.md)查看规则、来源、修订和生效原因。网页“待确认事项”和对话 `term_learning_reply` 共用同一条局部答复记录，不能把局部同意扩大成整篇验收或全局业务口径。

业务口径仍由 `business-rules.md` 定义。经验提醒判断边界与核验动作，不保存其他标书的固定答案，不覆盖当前原文。仓库不附带案例或记忆数据库。技术原理、表结构和完整数据流见 [技术方案](../../../../docs/memory-architecture.md)。

## 语言规范

新生成或修订的 `title`、中文 `triggers`、`applies_when`、`rule`、`exclusions`、`observation` 和 `no_experience_reason` 统一使用简体中文，即使标书或来源复盘使用繁体中文。英文术语、型号与明确标注的原文引用保留原样；`feedback_text` 中的用户原话、原文证据、抽取标题、定位标签及来源编号不做繁简转换。

仅统一已有经验的表述时，通过新 revision 保存简体版本，保留原来源和原有适用范围，不改写历史修订或来源记录；新版本须重新评测准入或由人工明确审核发布。

## 日常抽取如何使用经验

`term_document_prepare` 固定本任务的已发布快照，空库也生成空快照。形成采购对象候选后，针对具体疑点调用 `term_memory_recall`；默认最多三条、6144 字节，OpenCode 工具最多五条、8192 字节。没有疑点不必调用，无匹配时按原文判断。

核对经验的适用与排除条件，再回原文取证。词语分数只表示相关，交付回执不证明采用或采用正确。快照本轮冻结；发布、撤回只影响之后注册的任务。需要切换时使用新目录注册，不覆盖旧快照或回执。未发布候选不进入生产召回；人工审核发布的规则可以正常召回，其来源不因此被确认成整篇正确标签。

## 每次正式提交的主动复盘

由当前主 agent 使用已读原文和本次实际判断复盘，与结果一起传给 `term_result_commit` 的必填 `reflection_json`。工具只校验、持久化并建立候选，不另调模型，也没有后台会话监听。

`reflection_json` 是 JSON 字符串，其解析对象严格只含以下两个字段：

```json
{
  "insights": [],
  "no_experience_reason": "本篇未发现超出现有业务口径与已召回经验的新观察。"
}
```

若有新观察，`insights` 最多三项；每项严格含以下八个字段，不接受额外字段：

| 字段 | 内容与约束 |
|---|---|
| `title` | 简短观察主题，非空，最多 160 字符 |
| `issue_type` | `missing_product`、`boundary`、`quantity` 或 `association` |
| `triggers` | 1–12 个有区分度的中英文触发词，每项最多 100 字符，不重复 |
| `applies_when` | 适用前提，非空，最多 1000 字符 |
| `rule` | 可迁移的判断或核验动作，非空，最多 2400 字符 |
| `exclusions` | 不适用情况，非空，最多 1200 字符 |
| `observation` | 本次真实疑点、核验动作或自我修正的简短事实，非空，最多 1000 字符 |
| `evidence_tags` | 本篇真实存在的 1–30 个 C 标签，不重复 |

`no_experience_reason` 始终保留，最多 1000 字符；有观察时可为空，空 `insights` 时必须非空。整体规范 JSON 不超过 16 KiB；六个规则字段另受单条候选 4096 字节上限约束。

记录本次实际发现，不复述既有业务规则或已召回经验，不每篇凑三条。不要把通过结构校验、设备名称或本篇数量本身当成经验。只保存简短事实和可复核结论，不保存内部思维链。无新经验也正常提交，不需要人为制造错误。

正式提交仍须满足结果结构、全文交付、输入绑定与未解决事项为空等条件，无逐篇人工审批。成功返回 `learning.run_id`，下文简称 `run_id`。工具保存不可变运行与来源；每条观察形成 `correction_ids=[]`、`source_ids=[run_id]` 的未验证候选。当前最新 final 可以更新，旧 run 的归档不覆盖。

若 `committed=true` 但 `learning.status=learning_failed`，抽取结果已保存，学习归档或复盘尚未完成；报告这一状态，按 `next_action` 重试或补齐，不能声称经验已保存。演示运行不会登记学习来源。

同一来源的相同复盘可重复提交；不同内容不能静默覆盖。后续补充应登记新反馈或由 curator 创建候选新修订。直接使用 CLI 提交或登记而没传复盘时，运行显示 `pending_reflection`，可用 `term_memory_reflect(run_id, reflection_json)` 或 CLI `reflect` 补齐；没有材料不能猜测复盘。

## 接收用户反馈

收到已提交结果的反馈时，先调用 `term_memory_feedback`：

| 参数 | 要求 |
|---|---|
| `run_id` | 被反馈的旧运行编号，不是修正后新运行 |
| `feedback_text` | 用户反馈原话，不改写成 agent 自己的解释 |
| `after_result_json` | 可选，agent 整理的完整七字段修正稿；先通过结构校验，尚不是人工正确答案 |
| `reflection_json` | 必填，格式同上；没有新经验也说明原因 |

反馈被单独持久化，返回 `feedback_id`（也是该纠错来源的 `source_id`）及候选。需要改结果时，先保存指向旧 run 的反馈，再据原文取证、校验并正式提交，得到新 run。不要覆盖旧运行，不自行填写 `confirmed_by`。

没有修正稿时按两阶段衔接：

1. 第一次仅保存用户原话、旧 run_id 和复盘，省略 after_result_json，保留返回的未确认反馈。
2. 根据原文形成完整修正稿后，再次调用 term_memory_feedback，使用**同一旧 run_id、同一用户原话**，加入完整 after_result_json 与本次复盘。它会产生新的 feedback_id，不会修改第一条。
3. 将新的带稿 feedback_id 交给维护者 confirm-feedback；curator 的纠错候选应引用这个新的 source_id，必要时创建候选修订。旧无稿反馈保留未确认。新结果 commit 不会自动补齐、合并或确认旧反馈。

如果首次已具备完整修正稿，可以一次保存，不必先造一条无稿记录。反馈的 run_id 始终指被纠错的旧运行，不能改成新提交返回的 run_id。

反馈通路的来源类型是 `correction`。只有原话、没有完整且实际不同的修正稿时，不能 `confirm-feedback`；先记录反馈是允许的。若用户只是肯定“这篇正确”，成功来源应由人工 `confirm-run`，curator 可将候选的 `source_ids` 整理为对应 run，而不能把肯定反馈冒充已确认纠错。

## 私有存储与人工来源确认

使用 Python 标准库 SQLite，默认 `.term-memory/`，已被 Git 忽略；可用 `TERM_EXTRACT_MEMORY_DIR` 外置。运行、反馈、复盘、确认、经验修订与评测保存在私有库，结果/证据/run 和反馈修正稿另存内容地址归档。原始输入与 index 保留路径和哈希，不复制到归档；后续读取、确认、评测与发布仍需它们可用且未变。自定义目录若在仓库内，须自行忽略。

来源确认与经验发布是两个动作：确认这篇运行正确，或确认某份反馈修正稿，不代表认可它归纳出的所有规则。维护者通过工作台或 CLI 操作，名字仅是审计记录，不是身份认证；以下是对应 CLI：

```powershell
$learningScript = '.opencode/skills/term-extract/scripts/memory_learning.py'
python -X utf8 $learningScript --project-root . list
python -X utf8 $learningScript --project-root . show-run --run-id '运行编号'
python -X utf8 $learningScript --project-root . show-source --source-id '来源编号'
python -X utf8 $learningScript --project-root . confirm-run --run-id '运行编号' --confirmed-by '人工确认人' --family-id '文档家族编号' --note '已核验原文、结果和适用范围'
python -X utf8 $learningScript --project-root . confirm-feedback --feedback-id '反馈编号' --confirmed-by '人工确认人' --family-id '文档家族编号' --note '已核验修正稿与原文'
```

译文、修订版与近重复材料使用同一家族。确认记录不可覆盖；出现新问题时登记新反馈。同一 run 后续有已确认且修改了结果的纠错时，原成功来源不能继续用于旧 success 评测或该评测准入发布。人工审核仍可核对其中某条规则的证据，但会看到来源曾被纠错的提示，不能将整篇再次默认为正确。这不会自动撤回已发布经验；维护者应审阅受影响规则并决定是否 withdraw，已有任务快照保持不变。

旧运行的登记、补复盘或人工直接记录反馈可用：

```powershell
python -X utf8 $learningScript --project-root . register-run --index 'D:\私有数据\index.json' --result 'D:\私有数据\result.json' --evidence 'D:\私有数据\evidence.json' --run 'D:\私有数据\run.json'
python -X utf8 $learningScript --project-root . reflect --run-id '运行编号' --reflection-file 'D:\私有数据\复盘.json'
python -X utf8 $learningScript --project-root . feedback --run-id '运行编号' --text '用户反馈原话' --after-result 'D:\私有数据\修正稿.json'
```

CLI `feedback` 可省略修正稿及复盘；省略复盘时保存“仅记录反馈、尚无经验”的空观察。OpenCode feedback 工具始终要求显式复盘。

工具内部把较大的复盘和反馈 JSON 写入临时文件传给 Python，避免 Windows 命令行长度限制；对 agent 的 JSON 字符串参数保持不变。直接 CLI 建议使用文件参数，以避免旧版 PowerShell 传递内联 JSON 时的引号问题；上例复盘文件内容即本节说明的 reflection 对象。

## Curator 整理候选

维护者选择 `term-memory-curator`，提供 `source_id` 或旧版 `correction_id`。它通过 `term_memory_source` / `term_memory_correction` 读取指定来源，保留事实与推断的区别，再用 `term_memory_show` 和 `term_memory_propose` 整理候选或新修订。机器复盘中的 observation 与 evidence_tags 是来源证据，不直接成为召回规则正文。

`payload_json` 原七字段必填，仅可额外含 `source_ids`：

| 字段 | 内容 |
|---|---|
| `title`、`issue_type`、`triggers`、`applies_when`、`rule`、`exclusions` | 同复盘六个规则字段 |
| `correction_ids` | 旧版人工导入纠错编号数组，必填，可为 `[]` |
| `source_ids` | 可选，运行或反馈来源编号数组 |

数组不重复，每类来源最多 50 个。单条 payload 的规范 JSON 最多 4096 字节。propose 可先引用未确认来源。旧离线评测及对应 publish 仍要求全部来源确认；人工直发不要求确认整篇，但必须保留真实可核验来源，不能删来源绕过检查。填写 `memory_id` 创建新 revision，不覆盖旧版或当前发布版。

旧版人工纠错导入仍可使用：

```powershell
$memoryScript = '.opencode/skills/term-extract/scripts/memory_store.py'
python -X utf8 $memoryScript --project-root . import-correction --input 'D:\私有数据\input.json' --before 'D:\私有数据\bad.json' --after 'D:\私有数据\gold.json' --reason '人工确认的修改原因' --confirmed-by '人工确认人' --family-id '文档家族编号'
```

这是维护者人工操作，不由 extractor 或 curator 自填确认。导入会记录 before/after 的解析 JSON 以及三个文件路径和哈希；原文件仍须保留。

## 人工审核直接发布

在工作台“经验库”打开候选修订，核对规则、触发词、适用与排除条件及来源证据。需要修改时先保存新 revision。选择“人工审核发布此版本”，填写审核人和具体理由，批准页面显示的精确版本。此路径不要求先评测，不要求 `confirm-run` / `confirm-feedback`；审核单条规则不代表来源整篇抽取结果正确，也不会创建可信标签。

人工发布仍检查来源工件存在且摘要一致，检查当前业务口径和未解决的阻塞冲突或拒绝/合并结论。来源未确认、反馈缺完整修正稿或整篇曾被纠错等会提示维护者，不能冒充已确认正确；这类提示不要求先验收整篇。源材料缺失或变化、阻塞冲突未处理时，需先补齐或解决。

审核记录独立保存 `approval_id`、`reviewer`、`note`、`memory_id`、`revision`、内容摘要及时间。发布依据为 `human_review`，`evaluation_id=null`。没有评测记录时 `evaluation_status=not_evaluated`，显示“未评测”；已有报告但此次没有采用评测准入时为 `not_adopted`，显示“未采用评测准入”。两者均不生成、修改或消费 passed 报告，不将人工批准包装为效果验证。审核姓名是本机审计记录，不是身份认证。

人工 CLI 与网页使用同一路径：

```powershell
$memoryScript = '.opencode/skills/term-extract/scripts/memory_store.py'
python -X utf8 $memoryScript --project-root . show --memory-id '经验编号' --revision 1
python -X utf8 $memoryScript --project-root . publish-reviewed --memory-id '经验编号' --revision 1 --content-sha256 '已审核修订的64位内容哈希' --reviewer '实际审核人' --note '已核对本条规则、适用与排除条件及来源证据；接受尚未评测的发布'
```

网页请求和人工 CLI 都必须绑定正在审核的内容摘要，防止审核期间规则变化；CLI 的 `--content-sha256` 从上一步 show 返回的 `content_sha256` 复制，示例占位符不能直接执行。Agent 没有人工直发工具，不能代填审核人、调用网页 HTTP 或人工 CLI 执行审核；普通待办答复也不自动发布。自动路径仍须通过独立试验门槛。人工发布可后来补评测、可撤回；发布或撤回只影响后续新任务的冻结快照。

## 旧离线评测与评测后发布

清单格式见 [经验回归清单](memory-evaluation.md)。评测器只检查给定 baseline/candidate 输出和人工关键参数断言，不启动模型，不自动生成两组实验输出。候选实验应在隔离上下文明确提供候选规则，保留外部运行来源；候选不会通过生产 recall 交付。人工正确答案不能冒充 candidate 模型输出。

模式根据已确认来源自动推导，不在 manifest 中硬填：

| 来源与模式 | 开发目标要求 | 共同要求 |
|---|---|---|
| 纯成功来源：`success` | 至少一个 `role=success, split=development` 案例；baseline 解析结果须等于该来源已确认归档结果，两组断言均通过 | 独立回归与不适用反例的两组断言均通过；无退化；所有目标 candidate 通过 |
| 任意纠错或混合来源：`correction` | 至少一个 fix 从 baseline 失败变为 candidate 全通过，仍保留原改善门槛 | 同上 |

success 开发案例的输入哈希与家族匹配已确认成功来源；fix 匹配纠错来源。独立回归和反例须同时与全部来源的家族及输入哈希隔离；同一合格反例可兼作独立回归。成功路径只证明在已确认案例上保持一致、在列出的断言上未退化，不宣称收益提升。所有模式的未标注参数、实际经验采用及因果效果都不由评测认证。

```powershell
python -X utf8 .opencode/skills/term-extract/scripts/memory_evaluate.py --project-root . --memory-id '经验编号' --revision 1 --manifest 'D:\私有数据\regression.json'
```

curator 也可调用 `term_memory_evaluate`。报告绑定规则、当前发布集合、来源确认和评测工件。维护者核对内容、来源、输出生成方式与边界后，在工作台发布，或执行对应 CLI：

```powershell
python -X utf8 $memoryScript --project-root . show --memory-id '经验编号' --revision 1
python -X utf8 $memoryScript --project-root . show-evaluation --evaluation-id '评测编号'
python -X utf8 $memoryScript --project-root . publish --memory-id '经验编号' --revision 1 --evaluation-id '评测编号' --reviewer '审核人' --note '已核对来源、断言、反例、回归及输出生成方式'
python -X utf8 $memoryScript --project-root . withdraw --memory-id '经验编号' --reviewer '维护人' --note '撤回原因'
```

旧 `publish` 重新核对修订、内部评测报告、来源确认、外部文件和当前发布集合，拒绝过期或已使用报告；不因新增人工路径放宽。撤回不删历史、不自动恢复旧版；恢复可重新人工审核发布，或针对当前集合重新评测发布，不复用旧审核动作或已消费报告。第二篇新任务才会固定新集合并按疑点召回。

## 权限与能力边界

term-extractor 主 agent 可调用当前学习原子工具，并仅通过 task 启动 term-trial 独立子 agent；子 agent 只访问本票工具。标签对照、独立验证和 agent 自动发布遵守 learning-modes.md 的门槛。旧 curator 仅兼容指定来源、修订及离线断言，不批准新经验。人工可网页或人工 CLI 审核直发；agent 不使用人工确认接口、shell 或编辑权限代替审核。实际本地访问控制由操作系统承担，哈希用于一致性与追溯，不是管理员防篡改签名。

当前已实现持久化、快照、回执、主动复盘及主 agent 编排的独立试验；Python 不提供模型调度或后台聊天监听。日常结构通过、可信标签、评测通过、经验发布分别记录；没有全面语义验收或真实标书效果保证，不能把流程跑通等同于质量提升。
