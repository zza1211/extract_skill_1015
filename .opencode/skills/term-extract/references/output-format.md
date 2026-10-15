# 最终结果与证据文件

## 严格七字段结果

最终结果为 UTF-8 JSON 数组。每条记录只有以下七个字段，不输出 Markdown 围栏、注释或额外证据字段。

| 字段 | 类型与约束 |
| --- | --- |
| `id` | 整数，按结果数组顺序从 0 连续递增。布尔值不是有效整数。 |
| `条款标题` | 非空字符串，使用原文设备或系统名称；不能自行以分类名称代替。 |
| `条款分类` | `normal`、`general`、`separate` 之一。 |
| `相关条款` | 不重复的整数 id 数组，不能引用自身或不存在的 id。normal 必须为空；general 和 separate 必须有有效关联。 |
| `产品分类` | 非空中文参考分类字符串；general 可以填 `未提及`。分类是否正确不是程序验收项。 |
| `定位标识` | 恰好两个字符串，如 `["<C12>", "<C18>"]`。使用输入中的真实标签，起始在原文顺序上不得晚于终止；单标签区段可以首尾相同。 |
| `产品数量` | 字符串，保留原文采购数量表达；不能可靠确认时填 `未提及`，不要输出数字类型、空字符串、null 或猜测值。 |

标签数字只用于标识，不能用于排序。原文先出现 `<C100>`、再出现 `<C2>` 时，`["<C100>", "<C2>"]` 是合法顺序。

separate 记录直接关联另一条 separate 时应互相引用。如果数条 separate 指向共同 normal，主 agent 应核对是否是同一采购对象、是否还应互联；程序不能仅凭标题证明对象一致。

separate 与关联记录位于同一表格的重叠或相邻行时，程序报告 SEPARATE_LOCAL_LAYOUT_REVIEW，提醒复查连续规格与系统共同要求。该提示不是自动决定分类；需用证据说明真正的不连续业务区段，不能仅以标签数字不挨或表格分页作为依据。

separate 数量填“未提及”、关联 normal/separate 已有具体数量时，程序提示 SEPARATE_QUANTITY_MISSING_REVIEW；主 agent 需回到真实数量来源核对计量对象，并补证据标签。程序不自动继承数量，general 不触发此提示。

不同产品区段出现重叠时先检查参数污染。general 的作用范围可能与设备区段重叠，不机械判为错误。其他重叠由程序提示并由主 agent 解释，不要求结果与人工样例的 C 区间逐字一致。

## 分类参考概览

可参考以下中文名称，但这不是新的完整分类目录，也不是强制枚举：盒式交换机、框式交换机、光纤通道交换机、路由器、WLAN AP、WLAN AC、安全产品、网络管理设备、集中式、分布式、超融合、数据保护、IdeaHub、智真会议平台、智真会议终端、光模块、光传送、ODN、ONU、OLT、SDC、Others。

`Others` 的范围疑点按业务口径记录待确认问题。不得为了得到一个熟悉的分类而删除有设备要求的独立采购候选。

## 证据 sidecar

证据单独保存，不改变七字段接口。初版结构为：

```json
{
  "clauses": [
    {
      "id": 0,
      "quantity_source_tags": ["<C3>"],
      "related_source_tags": [],
      "notes": "数量来自采购清单；其他疑点及边界理由也写在此处。"
    }
  ],
  "unresolved": []
}
```

- `clauses` 中的 id 对应最终结果，不能重复或引用不存在的 id。
- 数量及关联证据标签必须存在于输入，可位于产品区段以外。
- `notes` 记录证据理由和必要的原文摘录。`unresolved` 保存尚未确定的数量、目标范围、关联或边界问题。
- `notes` 保持简短，不重复抄写整段规格；separate 需写同一对象与区段不连续的依据，general 需写适用范围。
- 初版程序只检查结构、id 与标签存在，不证明引文蕴含所给数量或关联。
- OpenCode 本 skill 的校验和提交流程必须提供 sidecar；单独运行 Python 校验 CLI 时可以不提供。数量不为 `未提及` 而缺少数量证据时报告 warning。`unresolved` 非空时报告需人工/主 agent 复核的 warning，提交工具会保留草稿并拒绝提交，不能声称语义已经验收。

## 校验与 demo

```text
python .opencode/skills/term-extract/scripts/validate_result.py --index work/index.json --result work/result.json --evidence work/evidence.json --report work/validation.json
```

报告和 stdout 均为 machine-readable JSON，消息使用中文。exit 0 表示本次结构检查无 error；exit 2 表示结构检查失败。warnings 仍需复核。报告固定说明结构通过不等于业务语义正确。

上面命令从 `skill-poc` 根目录执行；若以技能目录为当前目录，则脚本相对路径为 `scripts/validate_result.py`。

合成标签的 demo 输入禁止生产提交。只有演示或测试时才可显式添加 `--allow-demo`；报告会继续标记 demo，不能因通过结构校验而称为可生产提交。
