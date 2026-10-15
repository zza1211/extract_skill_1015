# 条款抽取 Skill POC

初版由 OpenCode 主 agent 判断设备、条款边界、general/separate 关联和采购数量。Python 程序负责原文读取、定位映射、结构校验和保存；不调用外部抽取模型。

首次理解经验机制，先看 [经验记忆技术方案与完整数据流](docs/memory-architecture.md)：涵盖存储形式、数据库结构、抽取与演进两条流程、回归门槛及当前限制。具体操作命令见 [经验维护说明](.opencode/skills/term-extract/references/experience.md)。

## 启动

需要 Python 3.10+ 和支持项目 skills、custom tools 的 OpenCode。工具依赖版本见 `.opencode/package.json`。

如果已经在运行 OpenCode，新增或修改配置后退出并重新启动，以加载本项目配置。

当前版本为 0.2.0：保留同一段落/单元格中的多个真实 `<Cxx>` 标签和紧凑原文交付，七字段输出不变；新增独立的经验维护、离线回归和人工发布流程。旧任务需在新工作目录重新 `prepare`，以固定经验快照；文档索引仍为版本 2。

克隆仓库后，从仓库根目录启动 OpenCode，避免父目录项目配置影响技能发现：

```powershell
git clone https://github.com/zza1211/extract_skill_1015.git
Set-Location extract_skill_1015
opencode
```

默认 agent 为 term-extractor，继承当前选择的模型。在 OpenCode 中通过 `/connect` 连接模型服务，通过 `/models` 选择能容纳本次文档与输出预算的模型。API 密钥通过 OpenCode 管理，不写进本项目。

DeepSeek Flash 的项目默认 `reasoningEffort` 为 `low`。会话中显式选择的 variant 可能覆盖项目默认。配置说明见 [DeepSeek 官方说明](https://api-docs.deepseek.com/guides/thinking_mode/)和 [OpenCode 模型文档](https://opencode.ai/docs/models/)。

输入示例：

```text
加载 term-extract skill，抽取 D:\你的目录\input.json 的设备技术条款。
由你根据原文判断，使用文档和结果工具；将结果保存在本次 runs 工作目录。
```

如果 Python 不在 PATH 中，启动前设置解释器路径：

```powershell
$env:TERM_EXTRACT_PYTHON = 'C:\Python311\python.exe'
opencode
```

## 文件位置

- `.opencode/skills/term-extract/SKILL.md`：中文入口与执行方法。
- `.opencode/skills/term-extract/references/`：业务口径、输出协议、输入与经验说明。
- `.opencode/agents/term-extractor.md`：主 agent 的职责与工具范围。
- `.opencode/agents/term-memory-curator.md`：经验候选整理与回归 agent，无发布权限。
- `.opencode/tools/`：OpenCode 文档与结果工具。
- `.opencode/skills/term-extract/scripts/`：无模型调用的 Python 辅助程序。
- `runs/`：每次运行的索引、读取回执、草稿、校验报告和最终结果；不提交版本库。
- `.term-memory/`：私有 SQLite 经验、审核及评测记录，Git 忽略；可外置到 `TERM_EXTRACT_MEMORY_DIR`。

配置关闭自动会话压缩，以免原文变成摘要。关闭压缩不会扩大模型窗口；如果总上下文超限，任务应报告未完成。每篇标书开新会话。读取回执只证明原文已交付，不证明主模型正确理解。项目显式设置工具输出阈值，文档工具再控制分页大小，避免原文被 OpenCode 截断。

## 输入与演示

自行提供包含 `text`、`table` 和真实 `<Cxx>` 标签的 JSON 输入，格式见 `.opencode/skills/term-extract/references/input-format.md`。仓库不包含标书数据、抽取结果、会话日志或运行报告。生产输入缺少标签会报错。

若已有不带标签的输入，仅检查工具链时可以明确请求演示：

```text
加载 term-extract，用 D:\你的目录\input_data.json 做演示，允许 demo_tags。
演示标签不属于原标书，请将提交标记为 demo。
```

程序只在工作副本生成模拟定位，不修改原始输入。演示结果不会被标记为可用于生产。

## 脚本检查

下面的可选 Node 工具链检查需要 Node.js 24+；日常 OpenCode 执行不需要另装 Node.js。

```powershell
npm ci --prefix .opencode
python -X utf8 -m unittest discover -s .opencode/skills/term-extract/scripts -p 'test_*.py'
node tests/tool_smoke.mjs
node tests/checkpoint_smoke.mjs
node tests/memory_tools_smoke.mjs
```

真实文件检查需要另行提供带标签的 JSON 目录，不会调用模型：

```powershell
node tests/structured_json_smoke.mjs 'D:\你的目录\structured_json'
```

脚本也可单独执行，输入格式与参数见 `.opencode/skills/term-extract/references/input-format.md`。工具使用参数数组调用 Python，不经过 shell 拼接。

## 执行与维护约定

文档工具保留全部原文与表格布局，向模型交付紧凑视图，将完整定位元数据保存在读取回执。读取全文后，使用 `term_result_checkpoint` 保存短候选和具体疑点，再按需补读、搜索、校验与提交。

主 agent 关闭 glob/grep/todowrite；原文只通过文档工具读取，普通 read 限于技能资料和候选。search 必须先有与当前 index 哈希匹配的候选，候选可以保留疑点；Python 诊断 CLI 不受该阶段约束。

工具测试检查格式、定位映射、原文交付和执行约束，不能代替对漏产品、关键参数覆盖、参数归属与数量的人工验收。

连续表格行、同格多标签和跨页续表不自动构成 separate。同一表的相邻或重叠行被抽成 separate 时，校验报告会提示 `SEPARATE_LOCAL_LAYOUT_REVIEW`，要求核对真实业务区段；仍由主 agent 根据原文决定归属。

同一采购对象的 separate 片段不能仅因局部没有数量就填写“未提及”，应核对关联条款与采购清单；校验器以 `SEPARATE_QUANTITY_MISSING_REVIEW` 提示可疑缺失，不自动复制数量。

每次提交在 `run.json` 记录模型标识、输入与结果哈希、技能/规则哈希、经验快照和实际交付的经验修订。仓库不附带案例或记忆数据库。

## 经验演进

流程为：人工导入已确认纠错 → `term-memory-curator` 整理候选 → 对给定 baseline/candidate 输出运行回归 → 维护者通过 CLI 审核发布 → 后续抽取按需召回。

候选不参与日常抽取，agent 不能批准自身经验。发布需修复目标问题、没有已有断言退化，并通过独立反例；报告绑定经验修订和评测文件哈希。评测工具不自动启动模型，也不能证明改善必然由经验造成。日常抽取无需等待人工审批。

每篇任务注册时冻结已发布集合，召回默认最多三条、约 6 KiB。检索在本地执行，不调用辅助模型，但仍占用主 agent 的工具交互轮次和上下文。撤回影响之后注册的任务；已有任务保留原快照。操作命令、数据格式和权限边界见 [经验记忆说明](.opencode/skills/term-extract/references/experience.md)及[回归清单](.opencode/skills/term-extract/references/memory-evaluation.md)。

使用人工确认文档比较漏产品、关键参数覆盖、其他设备参数混入、关联、数量以及累计费用和耗时。分类仅参考。纠错保留原预测、正确结果和原因；在独立文档回归后发布新的经验或口径版本，避免仅在参与改进的文档上报成绩。

按 OpenCode 官方规范：

- https://opencode.ai/docs/skills/
- https://opencode.ai/docs/custom-tools/
- https://opencode.ai/docs/agents/
