/** Public v1 contracts, validated before a refresh replaces the last good snapshot. */
export type Work = {
  id: string; title: string; repo: string | null; status: string;
  stage: string | null; run_id: string | null; created_at: number | null;
  updated_at: number | null; finished_at: number | null;
  reason: string | null; pr_url: string | null;
  archived?: boolean; attempts?: number;
};
export type Page = {items: Work[]; total: number; counts: Record<string, number>; limit: number; offset: number; attempts_total?: number};

const invalid = () => new Error('The local service returned an incompatible response. Refresh after updating the interface and service together.');
const object = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === 'string';
const nullableText = (v: unknown) => v === null || text(v);
const timestamp = (v: unknown) => v === null || (typeof v === 'number' && Number.isFinite(v));
const integer = (v: unknown, max = Number.MAX_SAFE_INTEGER): v is number =>
  typeof v === 'number' && Number.isSafeInteger(v) && v >= 0 && v <= max;

export function parseWork(value: unknown): Work {
  if (!object(value) || !text(value.id) || !text(value.title) || !text(value.status) ||
      !['repo', 'stage', 'run_id', 'reason', 'pr_url'].every(key => nullableText(value[key])) ||
      !['created_at', 'updated_at', 'finished_at'].every(key => timestamp(value[key])) ||
      !(value.archived === undefined || typeof value.archived === 'boolean') ||
      !(value.attempts === undefined || (integer(value.attempts) && value.attempts >= 1))) throw invalid();
  return value as Work;
}

export function parsePage(value: unknown): Page {
  if (!object(value) || !Array.isArray(value.items) || !integer(value.limit, 100) || value.limit < 1 ||
      value.items.length > value.limit || !integer(value.offset, 100000) || !integer(value.total) ||
      value.total < value.items.length || !object(value.counts) ||
      !Object.values(value.counts).every(count => integer(count))) throw invalid();
  return {...value, items: value.items.map(parseWork)} as Page;
}

export type RunEvent = {id:number; ts:number; level:string; kind:string; message:string|null; truncated:boolean};
export type Allowance = {attempt:number; budget_usd:number; spent_usd:number; reserved_usd:number; available_usd:number;
  deadline:number; last_progress_at:number; current_action:string|null; last_result:string|null;
  next_action:string|null; stop_reason:string|null};
export type ExecutionTask = {task_key:string;cycle:number;kind:string;title:string|null;role:string|null;model:string|null;
  state:string;depends_on:string[];dependencies_complete:boolean;expected_files:string[];started_at:number|null;ended_at:number|null;
  attempts:number;summary:string|null;error:string|null;truncated:boolean};
export type ExecutionBot = {role:string|null;provider:string|null;model:string|null;calls:number;tokens:number;
  known_cost_usd:number;unavailable_calls:number;errors:number;first_call_at:number|null;last_call_at:number|null};
export type Execution = {mode:string;repair_cycle:number;counts:Record<string,number>;tasks:ExecutionTask[];tasks_has_more:boolean;
  bots:ExecutionBot[];bots_has_more:boolean;phases:{id:number;ts:number;state:string}[];phases_has_more:boolean};
export type Evidence = {
  item_id:string; run_id:string|null;
  costs:{calls:number; reported_usd:number; estimated_usd:number; unavailable_calls:number}|null;
  allowance:Allowance|null;
  checks:{id:number; cycle:number|null; criterion_id:string|null; text:string|null; status:string|null; evidence:string|null; truncated:boolean}[];
  checks_has_more:boolean;
  tests:{id:number; ts:number; cycle:number|null; label:string|null; exit_code:number|null; passed:number|null;
    failed:number|null; errors:number|null; skipped:number|null; timed_out:boolean}[];
  tests_has_more:boolean;
  events:RunEvent[]; next_event_id:number; events_has_more:boolean;
  execution?:Execution|null;
};
const finite = (v:unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const nullableNumber = (v:unknown) => v === null || finite(v);
const fields = (v:Record<string,unknown>, keys:string[], check:(value:unknown)=>boolean) => keys.every(key=>check(v[key]));

export function parseExecution(v:unknown):Execution {
  const strings=(a:unknown,max:number)=>Array.isArray(a)&&a.length<=max&&a.every(text);
  if(!object(v)||!text(v.mode)||!integer(v.repair_cycle)||!object(v.counts)||!Object.values(v.counts).every(n=>integer(n))||
    !fields(v,['tasks_has_more','bots_has_more','phases_has_more'],n=>typeof n==='boolean')||
    !Array.isArray(v.tasks)||v.tasks.length>200||!v.tasks.every(t=>object(t)&&
      fields(t,['task_key','kind','state'],text)&&fields(t,['title','role','model','summary','error'],nullableText)&&
      integer(t.cycle)&&integer(t.attempts)&&strings(t.depends_on,50)&&strings(t.expected_files,20)&&
      fields(t,['started_at','ended_at'],nullableNumber)&&fields(t,['truncated','dependencies_complete'],n=>typeof n==='boolean'))||
    !Array.isArray(v.bots)||v.bots.length>40||!v.bots.every(b=>object(b)&&fields(b,['role','provider','model'],nullableText)&&
      fields(b,['calls','tokens','unavailable_calls','errors'],n=>integer(n))&&finite(b.known_cost_usd)&&b.known_cost_usd>=0&&
      fields(b,['first_call_at','last_call_at'],nullableNumber))||
    !Array.isArray(v.phases)||v.phases.length>100||!v.phases.every(p=>object(p)&&integer(p.id)&&finite(p.ts)&&text(p.state)))throw invalid();
  const next=v as Execution;
  if(new Set(next.tasks.map(t=>t.task_key)).size!==next.tasks.length||
    Object.values(next.counts).reduce((a,b)=>a+b,0)<next.tasks.length||
    next.phases.some((p,i)=>i>0&&p.id<=next.phases[i-1].id))throw invalid();
  return next;
}

export function parseEvidence(value: unknown): Evidence {
  if (!object(value) || !text(value.item_id) || !nullableText(value.run_id) ||
      !integer(value.next_event_id) || !fields(value, ['checks_has_more','tests_has_more','events_has_more'], v=>typeof v==='boolean') ||
      !Array.isArray(value.events) || value.events.length>100 ||
      !value.events.every(e=>object(e) && integer(e.id) && finite(e.ts) && text(e.level) && text(e.kind) && nullableText(e.message) && typeof e.truncated==='boolean') ||
      !Array.isArray(value.checks) || value.checks.length>100 ||
      !value.checks.every(c=>object(c) && integer(c.id) && nullableNumber(c.cycle) &&
        fields(c,['criterion_id','text','status','evidence'],nullableText) && typeof c.truncated==='boolean') ||
      !Array.isArray(value.tests) || value.tests.length>25 ||
      !value.tests.every(t=>object(t) && integer(t.id) && finite(t.ts) && nullableText(t.label) &&
        fields(t,['cycle','exit_code','passed','failed','errors','skipped'],nullableNumber) && typeof t.timed_out==='boolean')) throw invalid();
  if (value.costs !== null && (!object(value.costs) || !integer(value.costs.calls) || !integer(value.costs.unavailable_calls) ||
      !fields(value.costs,['reported_usd','estimated_usd'],finite))) throw invalid();
  if (value.allowance !== null && (!object(value.allowance) || !integer(value.allowance.attempt) ||
      !fields(value.allowance,['budget_usd','spent_usd','reserved_usd','available_usd','deadline','last_progress_at'],finite) ||
      !fields(value.allowance,['current_action','last_result','next_action','stop_reason'],nullableText))) throw invalid();
  const events = value.events as RunEvent[];
  if(value.execution!==undefined&&value.execution!==null)parseExecution(value.execution);
  if (events.some((e,i)=>i>0 && e.id<=events[i-1].id) ||
      (events.length>0 && events.at(-1)!.id!==value.next_event_id) ||
      (value.events_has_more && events.length===0)) throw invalid();
  return value as Evidence;
}

export function appendEvents(previous:RunEvent[], incoming:RunEvent[]):RunEvent[] {
  const byId = new Map(previous.map(e=>[e.id,e]));
  incoming.forEach(e=>byId.set(e.id,e));
  return [...byId.values()].sort((a,b)=>a.id-b.id).slice(-200);
}

export type ArtifactListing = {item_id:string; run_id:string|null;
  report_status:'available'|'missing'|'unavailable'; report_bytes:number|null; report_truncated:boolean;
  screenshots:{name:string; size_bytes:number}[]; screenshots_truncated:boolean; screenshots_unavailable:boolean};
export type RetainedReport = {item_id:string; run_id:string; text:string; size_bytes:number; truncated:boolean};
const screenshotName = /^c[0-9]{1,8}-[a-z0-9-]{1,40}-(light|dark)\.png$/;

export function parseArtifacts(value:unknown):ArtifactListing {
  if(!object(value) || !text(value.item_id) || !nullableText(value.run_id) ||
    !text(value.report_status) || !['available','missing','unavailable'].includes(value.report_status) ||
    !(value.report_bytes===null || integer(value.report_bytes)) ||
    !fields(value,['report_truncated','screenshots_truncated','screenshots_unavailable'],v=>typeof v==='boolean') ||
    !Array.isArray(value.screenshots) || value.screenshots.length>24 ||
    !value.screenshots.every(s=>object(s) && text(s.name) && screenshotName.test(s.name) && integer(s.size_bytes,8*1024*1024)) ||
    (value.report_status==='available' && value.report_bytes===null)) throw invalid();
  return value as ArtifactListing;
}

export function parseReport(value:unknown):RetainedReport {
  if(!object(value) || !text(value.item_id) || !text(value.run_id) || !text(value.text) ||
    value.text.length>256*1024 || !integer(value.size_bytes) || typeof value.truncated!=='boolean') throw invalid();
  return value as RetainedReport;
}
