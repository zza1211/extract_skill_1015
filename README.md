# 条款抽取 Skill POC

**0.8.0：OpenCode `term-extractor` 是唯一执行入口。** 它加载中文 Skill，自主读取原文、抽取、建立结构化疑点账本、核验、总结经验并组织独立验证。Python 只提供定位、校验、疑点信号、比较、存储和准入等原子工具；网页管理记忆与标书结果校验，不发起模型调用。暂不引入向量模型。

先看[抽取与经验演进方案](docs/learning-evolution.md)，了解如何从第一篇产生经验并影响下一篇；疑点信号、账本和处置见[疑点引擎说明](docs/doubt-engine.md)；按标书检查条款、修改边界、同步文件和反馈学习见[结果校验说明](docs/result-review.md)。评测集准备、原生验证流程及前端清单框见[评测指南](docs/evaluation-guide.md)，网页操作见[记忆工作台](docs/memory-dashboard.md)，数据与版本见[记忆架构](docs/memory-architecture.md)。

## 启动

需要 Python 3.10+ 和支持项目 Skill、自定义工具与原生 task 的 OpenCode。工具依赖见 `.opencode/package.json`。在仓库根目录执行，配置变更后重启 OpenCode：

```powershell
git clone https://github.com/zza1211/extract_skill_1015.git
Set-Location extract_skill_1015
opencode --agent term-extractor
```

项目默认代理也是 `term-extractor`，可直接 `opencode`。模型和凭据由 OpenCode 会话管理，不在本项目或网页保存另一套配置。若 Python 不在 PATH：

```powershell
$env:TERM_EXTRACT_PYTHON = 'C:\Python311\python.exe'
opencode --agent term-extractor
```

主代理默认推理档位为 `low`，仍由 OpenCode 选择模型；使用其他模型时选择其支持的档位。**已有会话手动选择的 `high` / `max` 可以覆盖默认值**，更新项目后应重启 OpenCode，并检查当前档位。DeepSeek 的推理内容也占单轮生成预算，档位过高时可能在调用下一工具或输出结果前就耗尽预算。

读取过程中可以保存暂定候选；通读结束后先落盘候选和疑点，再核验归属、精确边界及数量。候选不要求预先解决所有问题，最终提交的读取完整性和证据要求保持不变。

两种模式使用同一主代理：

| 模式 | 输入 | 主代理行为 |
|---|---|---|
| 纯条款抽取 | 标书 JSON | 自主抽取与校验，保存原文支持的观察；观察不是正确标签 |
| 标签学习 | 输入、可信正确结果、案例族 | 先抽取并提交，冻结后看标签，比较复盘，再组织独立验证 |

纯抽取示例：

```text
请使用 term-extract 技能抽取 D:\私有数据\input.json。
自主读取、核验边界和数量、校验并提交，记录有依据的观察或无新经验原因。
```

标签学习示例：

```text
请使用 term-extract 技能进行标签学习。
输入：D:\私有数据\input.json
正确标签：D:\私有数据\labels.json
案例族：campus-template-a
标签已按当前口径确认，且与输入使用同一套 Cxx。
先完成盲抽并冻结预测，再比较标签、总结经验和验证。
```

每篇标签学习使用新会话，避免上一篇答案留在上下文。`begin` 只私有归档标签；主代理通过正常文档工具完成抽取与真实提交后，`compare` 才冻结并交付标签对照。不能把看过答案后的结果替换成首次预测。

如果会话在读取后结束却没有结果，先检查是否真正调用 `term_result_commit` 成功；读完、保存候选或通过结构校验都不等于完成。若会话导出的最后一轮为 `finish=length`，尤其只有推理 token 而没有文本或工具调用，说明这一轮生成被截断，不要直接重复同样的长推理。可在原会话切换到 `low` 并发送：

```text
继续本次抽取，沿用现有 index 和工作目录，不要重新 prepare。
先立即调用 term_result_checkpoint 保存暂定候选；归属、边界和数量未确定的写成疑点。
然后围绕疑点补读核验、validate 和 commit，报告最终结果路径及 run_id。
```

也可使用原生 CLI 恢复原会话：

```powershell
opencode run --session 'ses_你的会话ID' --agent term-extractor --variant low '继续本次抽取，沿用现有 index，先保存暂定候选和疑点，再核验、校验并提交。'
```

恢复原会话可沿用已交付原文及回执；如果另开会话，必须重新实际读取全文，不能把旧会话回执当成本次已读取。恢复指引不是后台重试器，生成上限、上下文不足或工具错误仍需如实处理。

## 评测前清理运行态

如果要在空白记忆上评测新版本，可手动运行仓库外层脚本：

```powershell
python -X utf8 scripts/reset_eval_environment.py       # 只预览
python -X utf8 scripts/reset_eval_environment.py --apply # 预览后输入 DELETE 才删除
```

脚本只清理 `runs/`、`.term-memory/` 和 `reports/`，不由 OpenCode 工具暴露，也不会删除代码、`examples/`、测试、输入标书或标签源文件。它会删除已发布经验快照、候选、反馈、案例索引和历史运行，因此需要保留的可信标签应放在清理范围之外，清理后重新导入。若 `TERM_EXTRACT_MEMORY_DIR` 指向项目外的目录，脚本默认只提示；确认该目录也属于本次评测后，再加 `--allow-external-memory` 执行清理。

## 网页管理记忆与结果校验

另开终端启动，无前端构建依赖：

```powershell
python -X utf8 .opencode/skills/term-extract/scripts/memory_ui.py
```

打开终端地址，默认 `http://127.0.0.1:8765/`。网页可查看学习记录、经验和修订、导入可信案例、回答待办、检查冲突及撤回；“条款校验”按输入展示条款与高亮 C 标签的原文，支持修改、删除、标记非目标及显式整篇确认；不选择模型、不创建或继续抽取。只读浏览可加 `--read-only`。

每次提交后，Agent 提示你校验。网页保存修改并同步索引所在目录下 `review/<run_id>/result.json`，保留原始预测和操作历史；保存局部修改不等于整篇确认。点击“完成整篇校验”后，回到 OpenCode 发送“请从已校验结果学习”，Agent 通过 `term_review_pending/read/reflect` 总结候选或说明无新经验，页面显示当前修订已学习。改动已校验结果需重新确认；“删除本篇结果”仅移除不再需要的结果，不作为纠错。候选仍需评测或人工审核发布。网页没有后台模型和自动唤醒，完整流程见[结果校验说明](docs/result-review.md)。

0.5.0 的 Python 作业保留只读历史，旧 create/start/run/settings/models 入口明确停用。新的抽取与学习不进入旧运行器。网页中的 OpenCode 指引只复制提示词，不自动发给模型。

## 输入与输出

输入为按原文顺序排列的 `text / table` 对象数组，Cxx 直接位于段落和单元格字符串中。多个真实标签可由准备工具拆成定位单元；生产输入缺少真实标签会报错，详见[输入格式](.opencode/skills/term-extract/references/input-format.md)。

合并单元格展开后可以保留重复 C 标签：同一明确合并范围内、与左上角完整带标签字符串逐字符一致的副本，注册为同一逻辑单元的坐标别名。原文及合并范围保留，不重排标签、不改变正确标签；没有合并关系、跨表或内容不同的重复仍报错。首次使用此类输入需用新工作目录 prepare，已有任务不自动修改。[合并单元格示例](examples/merged-cells/README.md)提供可复制数据和命令。

最终为七字段 JSON 数组：`id`、`条款标题`、`条款分类`、`相关条款`、`产品分类`、`定位标识`、`产品数量`。产品分类仅参考；质量重点是目标设备齐全、选型参数完整、不混入其他设备参数、数量和关系正确。机柜与布线不抽取，纯品牌准入不单独抽为 general；连续表格两行不因此成为 separate。完整口径见 [business-rules.md](.opencode/skills/term-extract/references/business-rules.md)。

`prepare / read / checkpoint / search / validate / commit` 是正常自主抽取工具链。疑点引擎通过 `term_doubt_scan` 从 index、草稿和 evidence 生成确定性风险信号，主 agent 用 `term_doubt_record` 保存疑点账本，再用 `term_doubt_resolve` 记录原文核验结果。信号不是语义错误结论，C0-C3 也不是未经校准的概率；第一版不新增子 agent。正式提交附简体中文复盘：最多三条有证据的可迁移观察，或没有新经验的原因。演示 `demo_tags` 仅工作副本可用，不能用于生产提交或可信学习。仓库不提供真实标书、标签、私有库或运行报告。

## 经验如何学习、验证和消费

经验存私有 SQLite，不训练模型。无标签自检只产生观察；有可信标签可对照错误和正确处理方法总结候选。确定重复合并来源，疑似冲突进入待办；保存候选不等于生效。

主代理自行选 development、独立 regression 和触及适用／排除边界的 counterexample，准备 baseline/candidate 票号，用原生 task 创建独立 `term-trial` 子会话。每票只得到原文、静态规则和冻结经验包，使用限定工具自主读取并提交；不传标签、历史答案或父会话复盘。

全部试验完成后，主代理分页复核局部差异，finalize 读取真实 `opencode export` 校验父子会话、唯一票号提示、模型/变体、工具范围与真实提交，再注册配对实验。`term_learning_trial_finalize` 仅登记评测，主代理看到通过后再调用 `term_learning_trial_publish` 发布；无需人逐条批准。至少三个案例角色、回归与反例两个独立案例族；保留测试不参与归纳或晋级。标签范围不同先判断业务含义，明确硬错误不能被“无害”意见覆盖。

经验发布有两条路径：

| 方式 | 门槛与结果 |
|---|---|
| Agent 自动发布 | 完成上述独立试验并通过评测，保留评测准入记录 |
| 人工审核发布 | 在网页“经验库”打开候选，核对规则、适用/排除条件和来源证据，选择“人工审核发布此版本”，填写审核人和理由；无需先评测 |

人工批准绑定精确修订，未评测时标记“人工审核发布／未评测”；已有评测但未用于该次准入时标记“未采用评测准入”，不会伪造 passed。来源材料缺失、变化或未解决的阻塞冲突仍需先处理。批准一条经验不等于确认来源整篇抽取正确，不会把原运行变成可信标签。主代理不能代人调用人工发布入口。

无法确定的局部差异和经验冲突交给人。网页与 OpenCode 对话写入同一待办，保存真实用户原话；代理不能假装人工确认。回答后回原会话继续，可信标签不再重复逐篇确认。

每篇固定已发布经验快照，主代理按疑点通过关键词、有限繁简与中英别名召回短规则；候选只明确注入试验。发布、撤回不改变已有快照。可写网页服务存活期间维护冲突检查，候选或口径变更时重扫，否则默认七天一次；`--no-maintenance` 可禁用。不存在监听所有聊天的学习后台。

## 文件与成本边界

| 位置 | 内容 |
|---|---|
| `.opencode/skills/term-extract/` | 中文 Skill、规则与原子脚本 |
| `.opencode/agents/` | 唯一主代理与限定试验子代理 |
| `.opencode/tools/` | 文档、提交、记忆、标签学习与试验工具 |
| `web/memory/` | 本地记忆管理页面 |
| `runs/` | 自主抽取索引、回执、草稿和结果 |
| `.term-memory/` | 版本库、可信案例、来源与独立试验工件 |

默认私有数据被 Git 忽略。外置用 `TERM_EXTRACT_MEMORY_DIR`，OpenCode 与网页须一致；自定义仓库内目录需另加入忽略。

模型调用和资源由 OpenCode 会话管理，没有旧网页运行器预算配置。三个案例的 A/B 至少六个子会话，不含首次主代理盲抽，还需主代理比较与复盘；应按可接受成本限制验证规模。长文本仍受所选模型窗口约束，关闭会话压缩不会扩大窗口。实际用量缺失不能按零费用计算。

旧反馈、人工来源确认、离线断言、评测后手动发布与撤回继续兼容；旧断言不生成模型输出，不能代替 agent 自动晋级的独立试验门槛。人工直发使用独立审核记录，不借用旧评测报告。详见[经验维护说明](.opencode/skills/term-extract/references/experience.md)。

## 评测集怎么准备

日常推荐导入**原文 JSON＋可信完整七字段标签＋案例族 family＋用途 split**，再由 OpenCode 组织原生验证；不需要填写网页“离线评测清单 JSON”。同模板、译文和近重复文档归同一 family，保留测试不参与经验归纳或晋级。

每条待验证经验至少覆盖其全部开发来源、独立回归和独立反例，回归与反例来自另外两个独立 family。反例应真正触及该规则的适用或排除条件；三个角色只是最小门槛，不代表已经充分验证真实效果。

网页的清单路径属于兼容离线维护：你已经有 baseline / candidate 输出时，用 manifest 指定索引、两组结果和关键参数断言，评测器只比较文件，不运行模型。它不是原文路径，也不是 `labels.json`。仓库提供[可运行的合成示例](examples/evaluation/README.md)，隔离准备后会打印可填写的 manifest 绝对路径；不要把示例用于生产候选。

详细数据目录、完整标签与断言的区别、可复制的 OpenCode 提示词和两条路线见[评测集准备与经验验证指南](docs/evaluation-guide.md)。

## 软件检查与效果评估

可选工具链检查需要 Node.js 24+：

```powershell
npm ci --prefix .opencode
python -X utf8 -m unittest discover -s .opencode/skills/term-extract/scripts -p 'test_*.py'
node tests/tool_smoke.mjs
node tests/checkpoint_smoke.mjs
node tests/memory_tools_smoke.mjs
node tests/learning_tools_smoke.mjs
```

只检查真实文件解析与工具链、不调模型：

```powershell
node tests/structured_json_smoke.mjs 'D:\私有数据\structured_json'
```

软件测试验证工具边界、定位、来源与准入逻辑，不代表真实标书准确率或成本收益。真实效果需用独立标注集比较漏产品、参数遗漏、污染、数量、关系、模型调用与人工纠错时间。

OpenCode 规范：[Skills](https://opencode.ai/docs/skills/)、[Custom Tools](https://opencode.ai/docs/custom-tools/)、[Agents](https://opencode.ai/docs/agents/)。
