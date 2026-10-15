# 条款抽取 Skill POC

初版由 OpenCode 主 agent 判断设备、条款边界、general/separate 关联和采购数量。Python 程序负责原文读取、定位映射、结构校验和保存；不调用外部抽取模型。

## 启动

需要 Python 3.10+ 和支持项目 skills、custom tools 的 OpenCode。本机制作时验证环境为 Python 3.11、OpenCode 1.18.30。

如果已经在运行 OpenCode，新增或修改配置后退出并重新启动，以加载本项目配置。

当前版本为 0.1.2：支持同一 `text` 字符串或表格单元格中的多个真实 `<Cxx>` 标签，原文件无需改写。模型读取时同一段落/单元格只标一次位置，全部原文和表格布局保留，完整定位偏移留在读取回执；七字段输出接口不变。版本 1 的旧索引需重新 `prepare`，已有版本 2 索引可用。

克隆仓库后，从仓库根目录启动 OpenCode，避免父目录项目配置影响技能发现：

```powershell
git clone https://github.com/zza1211/extract_skill_1015.git
Set-Location extract_skill_1015
opencode
```

默认 agent 为 term-extractor，继承当前选择的模型。在 OpenCode 中通过 `/connect` 连接模型服务，通过 `/models` 选择能容纳本次文档与输出预算的模型。API 密钥通过 OpenCode 管理，不写进本项目。

DeepSeek Flash 的项目默认 `reasoningEffort` 为 `low`，疑难问题另行评测是否需要更高 effort。如果会话中显式选择了 `high` 或 `max` variant，它仍可能覆盖项目默认；本次复测建议显式选 `low`。原测试会话曾以 `max` 生成 32,000 个推理 token、正文为 0，结束原因为 `length`；不能把这种结束称为“后台仍在组装结果”。effort 的支持范围见 [DeepSeek 官方说明](https://api-docs.deepseek.com/guides/thinking_mode/)，variant 配置见 [OpenCode 模型文档](https://opencode.ai/docs/models/)。

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
- `.opencode/tools/`：OpenCode 文档与结果工具。
- `.opencode/skills/term-extract/scripts/`：无模型调用的 Python 辅助程序。
- `runs/`：每次运行的索引、读取回执、草稿、校验报告和最终结果；不提交版本库。
- `reports/`：会话链路分析和结果修订建议，与正式运行产物分开。

配置关闭自动会话压缩，以免原文变成摘要。关闭压缩不会扩大模型窗口；如果总上下文超限，任务应报告未完成。每篇标书开新会话。读取回执只证明原文已交付，不证明主模型正确理解。项目显式设置工具输出阈值，文档工具再控制分页大小，避免原文被 OpenCode 截断。

## 现有样例与演示

原开发工作区的上级 `example/input_data.json` 没有 Cxx，`output.txt` 是早期文本格式，不能直接用于验收当前七字段 JSON。真实标书、运行产物和原始会话日志不随仓库发布；克隆后需自行提供输入文件。生产输入缺少标签会报错。

若已有不带标签的输入，仅检查工具链时可以明确请求演示：

```text
加载 term-extract，用 D:\你的目录\input_data.json 做演示，允许 demo_tags。
演示标签不属于原标书，请将提交标记为 demo。
```

程序只在工作副本生成模拟定位，不修改原始 example。演示结果不会被标记为可用于生产。

## 脚本检查

下面的可选 Node 工具链检查需要 Node.js 24+；日常 OpenCode 执行不需要另装 Node.js。

```powershell
npm ci --prefix .opencode
python -X utf8 -m unittest discover -s .opencode/skills/term-extract/scripts -p 'test_*.py'
node tests/tool_smoke.mjs
node tests/checkpoint_smoke.mjs
```

真实文件检查需要另行提供带标签的 JSON 目录，不会调用模型：

```powershell
node tests/structured_json_smoke.mjs 'D:\你的目录\structured_json'
```

脚本也可单独执行，输入格式与参数见 `references/input-format.md`。工具使用参数数组调用 Python，不经过 shell 拼接。

## 评测与后续更新

已通过 53 项 Python 回归测试、实际工具适配链检查，以及 OpenCode 1.18.30 的 agent/skill 加载检查。`structured_json_smoke.mjs` 默认检查上级 `structured_json` 下的所有 JSON：注册、分页完整性、逐字符原文恢复和单元格内部标签范围。8 份真实输入均已通过，全文读取合计从 214 次降到 17 次；这些检查证明工具和原文交付可运行，不证明抽取语义正确。

0.1.2 针对真实 HK-M01 会话优化：首次全文读取的模型返回从 204,171 字节降至 21,315 字节，读取从 12 次降至 1 次。已通过 OpenCode 实际调用 DeepSeek Flash / low 完成对照运行，生成 13 条结果；AP 误判 separate 已消除。初次完成用时 141.478 秒，之后核对数量和原文标题另用 42.195 秒。可选工具参数由执行函数补默认值，避免模型省略参数后传出 `undefined`。全文读取后使用 `term_result_checkpoint` 保存短候选与疑点，再核验和提交，证据不重复抄写整套参数。详见 [会话审查报告](reports/session-audit-HK-M01.md)。这是参与改进的开发样例，整篇结果尚未通过人工金标准验收。

在一次 high 对照运行中，agent 主动发现历史产物及输入外目录，出现非交互权限请求拒绝，未产出候选。因此当前主 agent 关闭 glob/grep/todowrite；原文只通过文档工具读取，普通 read 限于技能资料和候选。search 必须先有与本次 index 哈希匹配的候选，可保留疑点；Python 诊断 CLI 不受该阶段约束。这样把执行资源用于指定输入和具体待核验问题。

连续表格行、同格多标签和跨页续表不自动构成 separate。同一表的相邻或重叠行被抽成 separate 时，校验报告会提示 `SEPARATE_LOCAL_LAYOUT_REVIEW`，要求核对真实业务区段；仍由主 agent 根据原文决定归属。

同一采购对象的 separate 片段不能仅因局部没有数量就填写“未提及”，应核对关联条款与采购清单；校验器以 `SEPARATE_QUANTITY_MISSING_REVIEW` 提示可疑缺失，不自动复制数量。

每次提交在 `run.json` 记录模型标识、输入哈希、技能版本及业务口径/经验说明的文件哈希。经验库已加入用户确认的 E001 表格连续性案例；已有上百篇人工案例尚未导入。

使用已有人工确认文档比较漏产品、关键参数覆盖、其他设备参数混入、关联、数量以及累计费用和耗时。分类仅参考。纠错保留原预测、正确结果和原因；在独立文档回归后发布新的经验或口径版本，避免仅在参与改进的文档上报成绩。

按 OpenCode 官方规范：

- https://opencode.ai/docs/skills/
- https://opencode.ai/docs/custom-tools/
- https://opencode.ai/docs/agents/
