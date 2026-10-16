import { tool } from "@opencode-ai/plugin";
import { executionContext, output, projectRoot, python, withJsonFile } from "../skills/term-extract/scripts/opencode_bridge.ts";

async function invoke(context:any,action:string,body:any){
  const root=projectRoot(context.directory), actual=executionContext(context);
  if(!actual) throw new Error("需要真实 OpenCode 会话上下文。");
  return withJsonFile({action,...body,execution_context:actual},async file=>
    output(await python(root,"agent_trials.py",["--project-root",root,"--request-file",file])));
}
export const prepare=tool({
  description:"由主agent自主挑选案例后准备独立A/B试验票，不调用模型。cases_json为[{case_id,role:development|regression|counterexample,scope_note?}]；反例说明原文如何触及排除边界。返回票号，随后主agent用内置task调用term-trial，每票新会话且prompt只能为trial_id=票号。",
  args:{memory_id:tool.schema.string(),revision:tool.schema.number().int(),cases_json:tool.schema.string(),memory_ids_json:tool.schema.string().optional()},
  execute:(args,context)=>invoke(context,"prepare",{memory_id:args.memory_id,revision:args.revision,cases:JSON.parse(args.cases_json),...(args.memory_ids_json?{memory_ids:JSON.parse(args.memory_ids_json)}:{})}),
});
export const inspect=tool({
  description:"全部独立试验提交后，由主agent读取冻结结果与标签的局部比较；不会提前将标签交给试验子agent。按票和差异游标分页，不把范围差异直接判为业务错误。",
  args:{experiment_id:tool.schema.string(),trial_id:tool.schema.string().optional(),difference_cursor:tool.schema.string().optional()},
  execute:(args,context)=>invoke(context,"inspect",{...args,max_bytes:32768}),
});
export const judge=tool({
  description:"主agent在试验冻结后，对局部待定差异给出有依据的判断。judgments_json为[{trial_id,judgments:[{difference_id,decision:harmless|error|uncertain,reason}]}]。不覆盖确定错误；uncertain形成待办，不冒充人工答复。",
  args:{experiment_id:tool.schema.string(),judgments_json:tool.schema.string()},
  execute:(args,context)=>invoke(context,"judge",{experiment_id:args.experiment_id,judgments:JSON.parse(args.judgments_json)}),
});
export const finalize=tool({
  description:"主agent完成独立试验和局部核验后登记评测。工具核对OpenCode真实子会话、模型、全文交付、唯一候选、case独立性、原文和结果哈希，重新计算质量门槛；通过后可以调用publish，失败保持候选。程序不运行抽取模型。",
  args:{experiment_id:tool.schema.string()},
  execute:(args,context)=>invoke(context,"finalize",args),
});
export const publish=tool({
  description:"仅发布本次独立实验已通过门槛的候选。无需逐条人工批准，不接受手填评测报告或任意evaluation_id，发布可撤回；不修改当前生产任务经验快照。",
  args:{experiment_id:tool.schema.string()},
  execute:(args,context)=>invoke(context,"publish",args),
});
