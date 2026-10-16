import { tool } from "@opencode-ai/plugin";
import { executionContext, output, projectRoot, python, resolveFile, withJsonFile } from "../skills/term-extract/scripts/opencode_bridge.ts";

async function invoke(context: any, action: string, body: any) {
  const root = projectRoot(context.directory), actual = executionContext(context);
  if (!actual) throw new Error("需要真实 OpenCode 会话上下文；学习工具不能由网页或模型自填身份调用。");
  return withJsonFile({ ...body, execution_context: actual }, async file =>
    output(await python(root, "agent_learning.py", ["--project-root", root, action, "--request-file", file])));
}

export const begin = tool({
  description: "绑定用户输入、可信标签与已prepare索引；此时不返回标签。随后由当前agent自主读取原文、核验并提交。每篇学习标书使用新会话，不启动Python抽取流程。",
  args: { input_path:tool.schema.string(), labels_path:tool.schema.string(), family_id:tool.schema.string().min(1), index_path:tool.schema.string() },
  execute(args, context) {
    const root=projectRoot(context.directory);
    return invoke(context,"begin",{...args,input_path:resolveFile(root,args.input_path),labels_path:resolveFile(root,args.labels_path),index_path:resolveFile(root,args.index_path)});
  },
});

export const compare = tool({
  description: "只有当前会话完成全文读取并提交真实run后，才冻结首次预测、对照用户标签。返回分页差异，不可用读标签后修改的结果替换首次预测。",
  args: { study_id:tool.schema.string(), run_id:tool.schema.string(), label_cursor:tool.schema.number().int().optional(), difference_cursor:tool.schema.number().int().optional() },
  execute:(args,context)=>invoke(context,"compare",{...args,max_bytes:32768}),
});

export const comparison = tool({
  description:"继续读取已冻结学习的标签对照分页；未冻结、其他会话或试验agent均不能读取。",
  args:{study_id:tool.schema.string(),label_cursor:tool.schema.number().int().optional(),difference_cursor:tool.schema.number().int().optional()},
  execute:(args,context)=>invoke(context,"comparison",{...args,max_bytes:32768}),
});

export const judge = tool({
  description:"由主agent按局部原文判断待定差异，传[{difference_id,decision:harmless|error|uncertain,reason}]。不能覆盖确定错误；材料不足选uncertain进入人工待办，不伪装成人工确认。",
  args:{study_id:tool.schema.string(),judgments_json:tool.schema.string()},
  execute:(args,context)=>invoke(context,"judge",{study_id:args.study_id,judgments:JSON.parse(args.judgments_json)}),
});

export const reflect = tool({
  description:"标签对照后由当前agent自主总结可迁移经验或说明无新经验；保存候选与真实来源，程序不调模型。未确定差异先核验，候选不直接生效。",
  args:{study_id:tool.schema.string(),reflection_json:tool.schema.string()},
  execute:(args,context)=>invoke(context,"reflect",{study_id:args.study_id,reflection:JSON.parse(args.reflection_json)}),
});

export const status = tool({
  description:"查看学习的冻结、比较、复盘、候选与待办状态；不返回标签。",
  args:{study_id:tool.schema.string()},
  execute:(args,context)=>invoke(context,"status",args),
});

export const cases = tool({
  description:"列出可信案例的用途和案例族，供主agent自行挑选独立回归/反例；不返回标签。不得使用holdout调参。",
  args:{q:tool.schema.string().optional(),cursor:tool.schema.number().int().optional(),limit:tool.schema.number().int().min(1).max(100).optional()},
  execute:(args,context)=>invoke(context,"cases",args),
});

export const import_case = tool({
  description:"仅归档用户明确提供的可信输入和标签，供后续学习或独立回归。标签需同套Cxx且按当前口径确认；不把agent修正稿当人工标签，不调用模型。",
  args:{input_path:tool.schema.string(),labels_path:tool.schema.string(),family_id:tool.schema.string().min(1),split:tool.schema.enum(["development","regression","holdout"])},
  execute(args,context){const root=projectRoot(context.directory);return invoke(context,"import-case",{...args,input_path:resolveFile(root,args.input_path),labels_path:resolveFile(root,args.labels_path)});},
});

export const case_preview = tool({
  description:"查看所选案例的有限原文预览，自主判断是否适用及是否触及排除边界；不返回标签、不算试验全文读取。holdout禁止。",
  args:{case_id:tool.schema.string(),q:tool.schema.string().optional(),max_chars:tool.schema.number().int().min(500).max(12000).optional()},
  execute:(args,context)=>invoke(context,"preview",args),
});

export const reviews = tool({
  description:"默认仅列待办安全编号和状态，不暴露标签或候选上下文。需要具体问题时include_context=true会标记本会话已接触答案，之后新盲抽必须开新会话；不替人答复。",
  args:{state:tool.schema.enum(["open","resolved","all"]).optional(),cursor:tool.schema.number().int().optional(),limit:tool.schema.number().int().min(1).max(100).optional(),include_context:tool.schema.boolean().optional()},
  execute:(args,context)=>invoke(context,"reviews",{...args,max_bytes:32768}),
});

export const reply = tool({
  description:"仅在用户明确回答某待办后，保存其选项与原话到和网页相同的记录。不将agent判断冒充人工答复，不扩大到整篇或全局。",
  args:{review_id:tool.schema.string(),decision:tool.schema.string(),raw_user_reply:tool.schema.string().min(1),expected_revision:tool.schema.number().int().optional()},
  execute:(args,context)=>invoke(context,"reply",args),
});
