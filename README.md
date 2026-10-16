# 条款抽取 Skill POC

由 OpenCode 判断设备、技术条款边界、general/separate 关系和采购数量，Python 负责定位、结构检查、任务和经验存储。当前版本 **0.5.0** 增加两种运行模式、可信标签对照、自动实验与局部人工待办；暂不引入向量模型。

先看[抽取与经验演进方案](docs/learning-evolution.md)，了解从第一篇标书产生观察、验证候选、影响下一篇的完整流程。页面操作见[本地工作台说明](docs/memory-dashboard.md)，存储和旧维护入口见[记忆架构](docs/memory-architecture.md)。

## 启动

需要 Python 3.10+ 和支持项目 skills、custom tools 的 OpenCode。工具依赖版本见 `.opencode/package.json`。在仓库根目录启动，配置修改后重新启动 OpenCode：

```powershell
git clone https://github.com/zza1211/extract_skill_1015.git
Set-Location extract_skill_1015
opencode
```

默认 agent 为 `term-extractor`。通过 OpenCode `/connect` 配置模型服务、`/models` 选择模型；密钥由 OpenCode 管理，不写进本项目。若 Python 不在 PATH：

```powershell
$env:TERM_EXTRACT_PYTHON = 'C:\Python311\python.exe'
opencode
```

工作台在另一个终端启动，无 Web 框架或前端构建依赖：

```powershell
python -X utf8 .opencode/skills/term-extract/scripts/memory_ui.py
```

打开终端显示的本机地址，默认 `http://127.0.0.1:8765/`。先在“抽取与学习 → 模型设置”选择本机 OpenCode 的 `provider/model` 和预算；网页任务使用这个显式配置。仅查看时可加 `--read-only`。

## 两种运行模式

| 模式 | 输入 | 流程 |
|---|---|---|
| 纯条款抽取 | 标书 JSON | 抽取、自检、保存有证据的观察，候选等待可信案例验证 |
| 标签学习 | 标书 JSON＋当前口径正确结果＋案例族 | 隔离盲抽、冻结预测、标签对照、归纳候选、自动实验 |

在工作台“抽取与学习”新建任务即可；也可对 OpenCode 说明：

```text
加载 term-extract skill，用纯条款抽取模式处理 D:\私有数据\input.json。
通过 term_learning_create / start 创建并启动任务，完成后说明结果、经验和待确认事项。
```

```text
用标签学习模式处理 D:\私有数据\input.json。
正确标签为 D:\私有数据\labels.json，已按当前口径确认且使用同一套 Cxx。
案例族为 campus-template-a。先创建隔离任务，不要在当前抽取会话提前读取标签。
```

高层任务调用隔离 OpenCode 工作器，完整交付原文，记录模型事件、预测和实验工件。标签范围不同不直接判错，先进行局部语义判断；无法确定的问题进入“待确认事项”。可以在网页作答，也可明确答复某项待办，由工具保存到同一记录。回答后继续原任务，不重做已冻结预测。

可信标签导入无需重复人工确认。学习案例、独立回归与反例仍需区分案例族，保留测试不参与归纳。自动发布取决于实验门槛和设置；仅自检通过或保存候选不等于经验已经生效。

## 输入与输出

输入为按原文顺序排列的 `text` / `table` 对象数组，标签直接位于段落和表格单元格字符串中。多个真实标签可由准备程序拆成定位单元。生产输入缺少真实标签会报错，详见[输入格式](.opencode/skills/term-extract/references/input-format.md)。

最终结果仍是七字段 JSON 数组：`id`、`条款标题`、`条款分类`、`相关条款`、`产品分类`、`定位标识`、`产品数量`。产品分类仅参考；质量重点是目标设备齐全、选型参数完整、不混入其他设备参数、数量与关系正确。机柜、布线和纯品牌准入的范围按 [business-rules.md](.opencode/skills/term-extract/references/business-rules.md) 执行。

原有交互式 `prepare / read / checkpoint / search / validate / commit` 继续可用，适合逐步查看和核验原文。每次正式提交须提供简体中文复盘：最多三条有标签证据的可迁移观察，或明确说明本篇没有新经验。字段见[经验维护说明](.opencode/skills/term-extract/references/experience.md)。

演示可显式请求 `demo_tags`，只在工作副本生成定位，不能作为生产提交或可信学习案例。仓库不提供真实标书、正确标签、私有数据库或运行报告。

## 经验如何生效和消费

经验保存在私有 SQLite，不训练模型。新增观察形成候选，重复内容合并来源，疑似冲突进入待办。自动运行器用可信标签比较真实 baseline/candidate 输出，检查学习目标、独立回归和反例；满足门槛且开启自动发布后更新正式集合。

纯成功案例可证明本次未退化，不能仅凭它声称提高准确率。案例不足、语义不确定、预算不足或存在冲突时，保存候选并说明下一步；人工无需填写日常实验 manifest。

每篇生产任务固定已发布经验快照，按本地关键词、有限繁简和中英别名召回短规则。候选只能显式注入学习实验，每项实验另固定启动时的正式基线。新发布或撤回不改已有生产快照。可写网页服务存活期间会维护冲突检查：候选或口径变化时重扫，否则默认七天一次；可用 `--no-maintenance` 禁用。没有后台监听所有聊天。

旧运行的反馈、人工来源确认、离线断言、手动发布和撤回继续兼容，供特殊维护使用。它们与新自动模式的证据来源及门槛不同，详见[经验维护说明](.opencode/skills/term-extract/references/experience.md)和[旧离线回归清单](.opencode/skills/term-extract/references/memory-evaluation.md)。

## 文件与预算

| 位置 | 内容 |
|---|---|
| `.opencode/skills/term-extract/` | 中文 skill、业务规则、脚本和经验维护协议 |
| `.opencode/tools/term_learning.ts` | 高层任务、案例导入、待办和对话答复 |
| `web/memory/` | 本地中文工作台，原生 HTML/CSS/JavaScript |
| `runs/` | 交互抽取的索引、回执、草稿、报告和结果 |
| `.term-memory/` | SQLite、案例归档、不可变来源和自动任务工件 |

私有数据默认被 Git 忽略；外置可使用 `TERM_EXTRACT_MEMORY_DIR`，OpenCode 与页面须使用同一路径。自定义目录若位于仓库内，须自行加入忽略。

高层任务可限制调用次数、累计模型时间、单次超时、提示字节和回归案例数，并记录服务返回的 token、费用；未返回的指标显示未记录。完整提示超限会停止，不自动截断标书。字节预算不等于模型窗口，配置关闭会话压缩也不会扩大上下文。

## 检查

可选 Node 工具链检查需要 Node.js 24+；日常 OpenCode 不需另装 Node.js：

```powershell
npm ci --prefix .opencode
python -X utf8 -m unittest discover -s .opencode/skills/term-extract/scripts -p 'test_*.py'
node tests/tool_smoke.mjs
node tests/checkpoint_smoke.mjs
node tests/memory_tools_smoke.mjs
node tests/learning_tools_smoke.mjs
```

只检查真实文件的解析与工具链，不调用模型：

```powershell
node tests/structured_json_smoke.mjs 'D:\私有数据\structured_json'
```

合成测试验证流程、定位和准入边界，不代表真实抽取准确率或已证明成本收益。真实效果需使用独立标注案例比较漏产品、参数遗漏、参数污染、数量、关系、调用成本和人工纠错时间。

OpenCode 规范：[Skills](https://opencode.ai/docs/skills/)、[Custom Tools](https://opencode.ai/docs/custom-tools/)、[Agents](https://opencode.ai/docs/agents/)。
