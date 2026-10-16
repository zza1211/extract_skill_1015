# 离线评测清单的可运行合成示例

这套文件帮助理解网页中的“评测 JSON 路径”。**它演示离线断言接口，不演示模型抽取效果。** 所有标书、标签、baseline 和 candidate 都是手写合成 fixture；没有调用模型，也不能据此声称经验提升了准确率。日常优先使用 OpenCode 的可信案例与独立子任务验证流程，详见[评测说明](../../docs/evaluation-guide.md)。

## 先生成隔离演示库

在仓库根目录运行：

```powershell
python examples/evaluation/prepare_demo.py
```

也可以指定一个仓库外、尚不存在或为空的目录：

```powershell
python examples/evaluation/prepare_demo.py --out-dir 'D:\私有数据\term-eval-demo'
```

脚本只准备，不运行模型、不执行评测、不发布经验；不改实际 `.term-memory`。即使环境变量 `TERM_EXTRACT_MEMORY_DIR` 指向其他库，准备过程也强制使用演示目录中的独立库，并恢复原环境设置。已有非空目录会被拒绝，不会被清空。

输出 JSON 和演示目录中的 `demo-info.json` 提供：

- `demo_root`：隔离项目根目录。
- `memory_id`、`revision`：仅属于该演示库的候选身份。
- `manifest_path`：真实可用的 `evaluation.manifest.json` 绝对路径。
- `evaluation_command`：实际调用当前 `memory_evaluate.py` 的命令，手动运行后才会评测并登记报告。
- `ui_command`：使用当前程序启动隔离工作台的命令，端口为 `8766`，不替换日常使用的 `8765`。

复制执行 `ui_command` 后，在隔离工作台中选择“合成示例：同一设备连续参数表的边界”，把 `manifest_path` 填入评测路径框即可。也可以直接复制执行 `evaluation_command`。命令显式指定隔离记忆目录；PowerShell 命令会在进程结束后用 `finally` 恢复原环境变量，不会让终端遗留演示库设置。

**不要把这个演示清单填给生产库里的真实候选。** 候选绑定的来源和开发集不同，会被拒绝；不要修改来源记录来绕过它。演示报告仍只是接口演示，不要发布为真实经验。

## 三个案例的分工

| 目录 | role / split | 内容和预期 |
|---|---|---|
| `development/` | `fix` / `development` | AP 的原边界漏掉射频、PoE要求；示意候选边界补齐，不能带入下一防火墙。 |
| `regression/` | `regression` / `regression` | 另一模板下交换机与防火墙原来正确；示意候选仍须保留完整参数和采购数量。 |
| `counterexample/` | `counterexample` / `regression` | 同一表格的相邻两行分别采购AP和工业交换机；不能滥用连续表行规则把不同设备合并。 |

三个 `family_id` 分别是独立合成家族。真实数据要按来源、模板及复制关系划分，不能只改家族名制造独立性。反例是有正确标签的“不适用场景”，不是故意写错的标签。

每个目录包含：

- `input.json`：原始 text/table JSON，段落和单元格有真实格式的 `<Cxx>`。
- `labels.json`：示意人工正确答案。真实使用时必须是与输入同套标签、按当前口径确认的结果。
- `baseline.json`、`candidate.json`：为说明差异而分别手写的两组输出。真实离线评测必须换成对应基线及候选条件下的实际独立运行结果，保留生成来源；不能把 labels 复制进去冒充抽取结果。
- `annotations.json`：评测器使用的人工关键断言，与清单中内嵌 `assertions` 保持一致。

`manifest.example.json` 是静态格式模板，路径相对其所在目录；`prepared/index.json` 由脚本生成，因此直接把仓库里的静态模板提交评测会缺索引。脚本会输出包含有效路径的 `evaluation.manifest.json`。`rule.json` 是待绑定来源的规则模板，脚本登记纠错后才生成 `rule.bound.json`。

## 如何读断言

```json
{
  "object_key": "indoor-ap",
  "anchor_tag": "<C1>",
  "required_tags": ["<C1>", "<C5>", "<C7>"],
  "forbidden_tags": ["<C8>", "<C9>"],
  "quantity": "12台",
  "clause_category": "normal"
}
```

`anchor_tag` 应能唯一定位到该采购对象。`required_tags` 表示选型必需的关键参数；`forbidden_tags` 表示混入就会影响选型的其他设备内容；`quantity` 核对该对象的采购数量，不能拿端口数代替；`clause_category` 可选，用于约束 normal/general/separate。产品分类不参与本示例的业务对错评判。

这些断言只覆盖人工写出的检查点，不能证明整篇无漏抽、无其他语义问题。离线评测器不会自行生成输出，也不会证明某条经验导致了改善。
