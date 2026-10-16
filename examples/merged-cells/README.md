# 合并单元格重复 C 标签示例

这是手写的合成输入，用于检查注册和定位行为，不包含模型预测、人工正确标签或运行结果。

`input.json` 的第二个块是一张三列表格。第 1 列的“接入交换机”纵向合并两行，解析器把相同的 `<C4>接入交换机` 写在 `(1,0)`、`(2,0)`；`merges` 使用从 0 开始的坐标说明它们属于同一个单元格。

在仓库根目录执行下面的原子工具命令即可检查，不会调用模型或产生经验。`runs/merged-cells-demo-01` 必须是新的工作目录，已有目录请换一个名称。

```powershell
python -X utf8 .opencode/skills/term-extract/scripts/document_io.py prepare --input examples/merged-cells/input.json --out-dir runs/merged-cells-demo-01
python -X utf8 .opencode/skills/term-extract/scripts/document_io.py read --index runs/merged-cells-demo-01/index.json --cursor 0
python -X utf8 .opencode/skills/term-extract/scripts/document_io.py search --index runs/merged-cells-demo-01/index.json --query "接入交换机" --cursor 0 --neighbors 1
```

预期结果：

- 保留完整输入、rows 和 merges，不修改任何 C 标签；它是带真实标签的可提交输入格式，无需 `--demo-tags`。
- 只有 9 个定位单元、9 个唯一标签。`<C4>` 指向左上角 `(1,0)` 的单元，`(2,0)` 是其别名，不重复计数。
- 索引包含 `merged_cell_alias_policy="exact_merged_cell_v1"` 和 `merged_cell_aliases`；两个坐标的 `cell_unit_ids` 相同。
- 读取或搜索保留相关合并范围和别名，供模型判断同一设备两行参数的归属。别名不把 `<C4>` 的定位顺序移动到最后出现处，也不构成分散条款。

只有明确合并范围内与左上角**整个原字符串完全相同**的副本才能共享标签；空格、换行也不能不同。缺少合并关系、跨表重复、同标签不同内容、字符串内部重复，以及用来解释重复的无效或重叠合并范围均应报错。不要为了通过注册手动重排标签；用户的原有标签答案继续使用原 C 标签。

完整约定见[原文输入与读取契约](../../.opencode/skills/term-extract/references/input-format.md)。
