import type {Evidence,Execution,ExecutionBot,ExecutionTask,Work} from './contracts';

export const finishedTask=(state:string)=>['SUCCEEDED','INTEGRATED'].includes(state);
export const attentionTask=(state:string)=>['FAILED','BLOCKED','CANCELLED'].includes(state);
export const checkPassed=(status:string|null)=>status==='satisfied'||status==='passed';
export const taskLabel=(state:string)=>({SUCCEEDED:'Code ready',INTEGRATED:'Integrated',PENDING:'Waiting',RUNNING:'Working',FAILED:'Failed',BLOCKED:'Blocked',CANCELLED:'Cancelled'}[state]||state.replaceAll('_',' ').toLowerCase());
export function taskProgress(execution:Execution|null|undefined){
  const counts=execution?.counts??{};
  return {total:Object.values(counts).reduce((a,b)=>a+b,0),finished:(counts.SUCCEEDED??0)+(counts.INTEGRATED??0),
    running:counts.RUNNING??0,waiting:counts.PENDING??0,attention:(counts.FAILED??0)+(counts.BLOCKED??0)+(counts.CANCELLED??0)};
}
export function currentChecks(snapshot:Evidence|null){
  const checks=snapshot?.checks??[];const cycle=Math.max(snapshot?.execution?.repair_cycle??0,...checks.map(c=>c.cycle??0),0);
  const latest=new Map<string,Evidence['checks'][number]>();
  for(const c of [...checks].sort((a,b)=>b.id-a.id))if((c.cycle??0)===cycle){const key=c.criterion_id||c.text||String(c.id);if(!latest.has(key))latest.set(key,c);}
  return {cycle,checks:[...latest.values()],older:checks.filter(c=>!Array.from(latest.values()).some(latest=>latest.id===c.id))};
}

const phases=[
  {name:'Understand',states:['ISSUE_RECEIVED','REPOSITORY_INSPECTION']},
  {name:'Plan',states:['PLANNING','TASK_VALIDATION']},
  {name:'Build',states:['TASK_EXECUTION','INTEGRATION','FIX_REQUIRED']},
  {name:'Test',states:['TESTING']},
  {name:'Review',states:['CODE_REVIEW']},
  {name:'Deliver',states:['APPROVED','PULL_REQUEST_CREATION','COMPLETED']},
];
export function phaseProgress(item:Work,execution:Execution|null|undefined){
  const observed=execution?.phases??[];
  const current=phases.findIndex(p=>p.states.includes(item.stage??''));
  const last=[...observed].reverse().find(e=>phases.some(p=>p.states.includes(e.state)));
  const active=current>=0?current:last?phases.findIndex(p=>p.states.includes(last.state)):-1;
  return phases.map((p,index)=>({...p,index,visited:observed.some(e=>p.states.includes(e.state)),
    current:index===active,label:index===active?(item.status==='done'?'Finished':item.status==='running'?'Now':'Stopped here'):
      observed.some(e=>p.states.includes(e.state))?'Reached':'Not recorded'}));
}

/** Dependency layers, with cycles and missing/truncated dependencies kept out of the ready group. */
export function dependencyLevels(tasks:ExecutionTask[],completedKeys:string[]=[]){
  const keys=new Set([...tasks.map(t=>t.task_key),...completedKeys]),placed=new Set<string>(completedKeys);
  let remaining=[...tasks];const levels:ExecutionTask[][]=[];
  while(remaining.length){
    const next=remaining.filter(t=>t.dependencies_complete&&t.depends_on.every(d=>keys.has(d)&&placed.has(d)));
    if(!next.length)break;
    levels.push(next);next.forEach(t=>placed.add(t.task_key));remaining=remaining.filter(t=>!placed.has(t.task_key));
  }
  return {levels,unresolved:remaining};
}

export type Bot={key:string;role:string|null;model:string|null;tasks:ExecutionTask[];usage:ExecutionBot[]};
export function botRoster(execution:Execution|null|undefined):Bot[]{
  const roster=new Map<string,Bot>();
  const get=(role:string|null,model:string|null)=>{const key=JSON.stringify([role,model]);if(!roster.has(key))roster.set(key,{key,role,model,tasks:[],usage:[]});return roster.get(key)!;};
  execution?.tasks.forEach(t=>get(t.role,t.model).tasks.push(t));
  execution?.bots.forEach(b=>get(b.role,b.model).usage.push(b));
  return [...roster.values()].sort((a,b)=>Number(b.tasks.some(t=>t.state==='RUNNING'))-Number(a.tasks.some(t=>t.state==='RUNNING'))||(a.role??'').localeCompare(b.role??'')||(a.model??'').localeCompare(b.model??''));
}
