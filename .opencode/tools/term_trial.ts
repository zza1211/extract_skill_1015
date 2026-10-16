import { tool } from "@opencode-ai/plugin";
import path from "node:path";
import * as documents from "./term_document.ts";
import * as results from "./term_result.ts";
import { executionContext, output, projectRoot, python, withJsonFile } from "../skills/term-extract/scripts/opencode_bridge.ts";

async function invoke(context:any,action:string,body:any){
  const root=projectRoot(context.directory), actual=executionContext(context);
  if(!actual || actual.agent!=="term-trial") throw new Error("该工具只供OpenCode独立term-trial子agent使用。");
  return withJsonFile({action,...body,execution_context:actual},async file=>{
    const response=await python(root,"agent_trials.py",["--project-root",root,"--request-file",file]);
    if(response.error || response.ok===false) throw new Error(response.error || "试验工具失败。");
    return response;
  });
}
async function ticket(context:any,trial_id:string){return invoke(context,"trial",{trial_id});}

export const open=tool({
  description:"认领本次独立试验票，交付固定业务口径、输出协议和经验包，不交付标签。只允许一个新会话处理一张票；不能回到父会话或别的票。",
  args:{trial_id:tool.schema.string()},
  async execute(args,context){
    const response=await invoke(context,"trial",{...args,claim:true});
    const serialized=output(response);
    await invoke(context,"delivery",args);
    return serialized;
  },
});
export const read=tool({
  description:"按游标或真实标签区间读取本票原文，保留表格位置并记录当前试验会话的交付。连续分页至完整；不能读其他票或标签。",
  args:{trial_id:tool.schema.string(),cursor:tool.schema.string().optional(),start_tag:tool.schema.string().optional(),end_tag:tool.schema.string().optional(),max_bytes:tool.schema.number().int().min(2048).max(24576).optional()},
  async execute(args,context){const t=await ticket(context,args.trial_id);return documents.read.execute({...args,index_path:t.index_path},context);},
});
export const checkpoint=tool({
  description:"保存本票的简短设备候选、范围、数量证据和疑点；可以暂不完整。",
  args:{trial_id:tool.schema.string(),candidates_json:tool.schema.string(),notes:tool.schema.string().optional()},
  async execute(args,context){const t=await ticket(context,args.trial_id);return results.checkpoint.execute({...args,index_path:t.index_path},context);},
});
export const search=tool({
  description:"保存候选后，围绕具体疑点搜索本票原文；不代替全文读取。",
  args:{trial_id:tool.schema.string(),query:tool.schema.string(),cursor:tool.schema.string().optional(),limit:tool.schema.number().int().optional(),max_bytes:tool.schema.number().int().optional()},
  async execute(args,context){const t=await ticket(context,args.trial_id);return documents.search.execute({...args,index_path:t.index_path},context);},
});
export const validate=tool({
  description:"保存本票七字段草稿并验证结构与证据；设备、边界和数量由当前子agent判断。",
  args:{trial_id:tool.schema.string(),result_json:tool.schema.string(),evidence_json:tool.schema.string()},
  async execute(args,context){const t=await ticket(context,args.trial_id);return results.validate.execute({...args,index_path:t.index_path},context);},
});
export const commit=tool({
  description:"核对本票全文读取并冻结试验结果。强制空复盘，禁止试验结果生成学习来源；只返回提交信息。真实工具会话稍后由主agent核对。",
  args:{trial_id:tool.schema.string()},
  async execute(args,context){
    const t=await ticket(context,args.trial_id);
    const raw=await results.commit.execute({index_path:t.index_path,result_path:path.join(path.dirname(t.index_path),"draft","result.json"),evidence_path:path.join(path.dirname(t.index_path),"draft","evidence.json"),reflection_json:JSON.stringify({insights:[],no_experience_reason:"独立盲抽试验不归纳经验。"}),model_id:"由OpenCode会话导出核验"},context);
    const committed=JSON.parse(typeof raw==="string"?raw:raw.output);
    if(!committed.committed) return output(committed);
    const record=await invoke(context,"record",{trial_id:args.trial_id,committed});
    return output({...committed,trial_id:args.trial_id,trial_record:record});
  },
});
