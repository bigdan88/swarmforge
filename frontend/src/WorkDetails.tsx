import {useEffect,useId,useState,type KeyboardEvent} from 'react';
import {Artifacts} from './Artifacts';
import {WorkControls} from './Controls';
import {ActivityPanel,ChecksPanel,CostsPanel,useEvidence} from './Evidence';
import {money,readLocal,saveLocal,type Session} from './api';
import type {Evidence,Execution,ExecutionTask,RunEvent,Work} from './contracts';
import {ActionButton} from './ui';
import {attentionTask,botRoster,checkPassed,currentChecks,dependencyLevels,finishedTask,phaseProgress,taskLabel,taskProgress} from './WorkProgress';

const name=(value:string|null)=>value?.replaceAll('_',' ').toLowerCase()||'Not recorded';
const when=(value:number|null)=>value===null?'Not recorded':new Date(value*1000).toLocaleString();
const tabs=[['overview','Overview'],['sequence','Sequence'],['bots','Bots'],['checks','Checks'],['files','Files'],['activity','Activity'],['controls','Controls']] as const;
type Tab=typeof tabs[number][0];
function Brief({text,fallback}:{text?:string|null;fallback:string}){
  return !text?<p className="muted">{fallback}</p>:text.length>180?<details className="work-note"><summary>{text.slice(0,160)}…</summary><p>{text}</p></details>:<p>{text}</p>;
}
export function PhaseStrip({item,execution}:{item:Work;execution?:Execution|null}){
  return <ol className="work-phases" aria-label="Run stages">{phaseProgress(item,execution).map(p=><li key={p.name} className={p.current?'current':p.visited?'visited':''} aria-current={p.current?'step':undefined}><span className="phase-number">{p.visited&&!p.current?'✓':p.index+1}</span><div><strong>{p.name}</strong><small>{p.label}</small></div></li>)}</ol>;
}
export function WorkOverview({item,snapshot,events,onTab}:{item:Work;snapshot:Evidence|null;events:RunEvent[];onTab:(tab:Tab)=>void}){
  const execution=snapshot?.execution,progress=taskProgress(execution),checks=currentChecks(snapshot).checks;
  const passed=checks.filter(c=>checkPassed(c.status)).length;
  const tasks=execution?.tasks??[],running=tasks.filter(t=>t.state==='RUNNING'),finished=tasks.filter(t=>finishedTask(t.state));
  const done=new Set(finished.map(t=>t.task_key));
  const next=tasks.filter(t=>t.state==='PENDING'&&t.dependencies_complete&&t.depends_on.every(d=>done.has(d))).slice(0,3);
  const last=[...finished].sort((a,b)=>(b.ended_at??0)-(a.ended_at??0))[0];
  const allowance=snapshot?.allowance,cost=snapshot?.costs;
  const active=item.status==='running';
  const currentAction=active&&running.length?running.slice(0,2).map(t=>`${t.task_key}: ${t.title||'Bot assignment'}`).join('\n'):allowance?.current_action||item.reason||item.stage&&name(item.stage);
  return <div className="work-overview">
    <div className="work-metrics"><button onClick={()=>onTab('sequence')}><span>Bot tasks</span><strong>{progress.total?`${progress.finished} / ${progress.total}`:'—'}</strong><small>{progress.total?'finished assignments':'Plan not recorded yet'}</small>{progress.total>0&&<progress aria-label="Finished bot assignments" value={progress.finished} max={progress.total}/>}</button>
      <button onClick={()=>onTab('bots')}><span>Bots</span><strong>{botRoster(execution).length||'—'}</strong><small>{active&&running.length?`${running.length} ${running.length===1?'assignment':'assignments'} working`:'Assigned or recorded'}</small></button>
      <button onClick={()=>onTab('checks')}><span>Acceptance</span><strong>{checks.length?`${passed} / ${checks.length}`:'—'}</strong><small>{checks.length?'checks passed this cycle':'Awaiting recorded checks'}</small></button>
      <button onClick={()=>onTab('controls')}><span>{allowance?'Task spend':'Run model usage'}</span><strong>{allowance?money(allowance.spent_usd):cost?.calls?money(cost.reported_usd+cost.estimated_usd):'—'}</strong><small>{allowance?`of ${money(allowance.budget_usd)} allowance`:cost?.unavailable_calls?`${cost.unavailable_calls} calls without cost`:'Recorded cost only'}</small></button></div>
    <div className="work-focus-grid"><section className="work-focus now"><h3>{active?'Happening now':item.status==='done'?'Outcome':'Current state'}</h3><Brief text={currentAction} fallback={item.run_id?'Waiting for a recorded update.':'Waiting to be dispatched.'}/>{!!running.length&&<div className="task-chips">{running.slice(0,4).map(t=><span key={t.task_key}>{t.role||'Bot'} · {t.task_key}{!active?' · last recorded':''}</span>)}</div>}{active&&running.length>0&&allowance?.current_action&&<details className="task-record"><summary>Current action details</summary><p>{allowance.current_action}</p></details>}</section>
      <section className="work-focus"><h3>Last result</h3><Brief text={allowance?.last_result||last?.summary||events.at(-1)?.message} fallback="No result recorded yet."/></section>
      <section className="work-focus"><h3>Up next</h3><Brief text={allowance?.next_action||(item.status==='done'?'Review the checks, report and pull request.':next.length?next.map(t=>`${t.task_key}: ${t.title||'Untitled task'}`).join('\n'):null)} fallback={item.status==='queued'?'Waiting for an authorized queue worker.':item.status==='tentative'?'Waiting for the project lead to promote this task.':'Waiting for the next recorded action.'}/></section></div>
    {(allowance?.stop_reason||['failed','cancelled','skipped'].includes(item.status))&&<div className="work-attention"><strong>{item.status==='cancelled'?'Task cancelled':'Review the stopping reason'}</strong><Brief text={allowance?.stop_reason||item.reason} fallback="No stopping reason was recorded."/><button onClick={()=>onTab('controls')}>Open controls</button></div>}
    {execution&&progress.total>0&&<div className="work-section-heading work-small-summary"><span>{progress.running} last recorded working · {progress.waiting} waiting · {progress.attention} need attention</span><button onClick={()=>onTab('sequence')}>View sequence →</button></div>}
    <p className="work-footnote">Assignment progress and acceptance checks are separate. A finished run may still have a pull request awaiting merge.</p>
  </div>;
}

export function TaskCard({task,item}:{task:ExecutionTask;item:Work}){
  return <article className={`sequence-task ${task.state.toLowerCase()}`}><div className="sequence-task-heading"><span className="task-key">{task.task_key}</span><strong>{task.title||'Untitled bot task'}</strong><span className={`result-tag ${finishedTask(task.state)?'good':attentionTask(task.state)?'bad':task.state==='RUNNING'?'active':''}`}>{task.state==='RUNNING'&&item.status!=='running'?'Last recorded working':taskLabel(task.state)}</span></div>
    <p className="task-assignment"><span>{task.role||'Role not recorded'}</span><span>{task.model||'Model not recorded'}</span></p>
    <div className="task-dependencies">{task.depends_on.length?<><span>After</span>{task.depends_on.map(d=><code key={d}>{d}</code>)}</>:<span>{task.dependencies_complete?'No recorded prerequisites':'Prerequisites unavailable'}</span>}</div>
    <details className="task-record"><summary>Results and task details{task.attempts>1?` · ${task.attempts} attempts`:''}</summary><dl><div><dt>Started</dt><dd>{when(task.started_at)}</dd></div><div><dt>Finished</dt><dd>{when(task.ended_at)}</dd></div></dl><Brief text={task.summary} fallback="No result recorded yet."/>{task.error&&<div className="work-attention"><strong>Problem recorded</strong><Brief text={task.error} fallback=""/></div>}{!!task.expected_files.length&&<div className="task-chips">{task.expected_files.map((f,i)=><code key={i}>{f}</code>)}</div>}{task.truncated&&<p className="muted">Long task details are shortened in this preview.</p>}</details>
  </article>;
}
export function SequencePanel({item,execution}:{item:Work;execution?:Execution|null}){
  const [filter,setFilter]=useState('all');
  const tasks=execution?.tasks??[];
  const visible=(t:ExecutionTask)=>filter==='all'||(filter==='working'?t.state==='RUNNING':filter==='finished'?finishedTask(t.state):filter==='attention'?attentionTask(t.state):t.state==='PENDING');
  const cycles=[...new Set(tasks.map(t=>t.cycle))].sort((a,b)=>a-b);
  const show=(items:ExecutionTask[])=>items.filter(visible).map(t=><TaskCard key={t.task_key} task={t} item={item}/>);
  return <section aria-label="Task sequence"><div className="work-section-heading"><div><h3>Execution sequence</h3><p className="muted">Dependency levels from the recorded plan. Tasks at the same level have no dependency on one another; runtime limits still apply.</p></div><label><span className="sr-only">Filter bot tasks</span><select value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">All bot tasks</option><option value="working">Working</option><option value="waiting">Waiting</option><option value="finished">Finished</option><option value="attention">Needs attention</option></select></label></div>
    {!tasks.length&&<p className="work-empty">{item.run_id?'The planner has not recorded bot tasks yet.':'The plan will appear after this task starts.'}</p>}
    {tasks.length>0&&!tasks.some(visible)&&<p className="work-empty">No bot tasks match this filter.</p>}
    {cycles.map(cycle=>{const current=tasks.filter(t=>t.cycle===cycle),plan=current.filter(t=>!['test','review'].includes(t.kind));const others=current.filter(t=>['test','review'].includes(t.kind));
      const graph=dependencyLevels(plan,tasks.filter(t=>t.cycle<cycle&&finishedTask(t.state)).map(t=>t.task_key));
      return current.some(visible)&&<section className="sequence-cycle" key={cycle}><h4>{cycle===0?'Initial plan':`Repair cycle ${cycle}`}</h4>{graph.levels.map((level,i)=>level.some(visible)&&<div className="sequence-level" key={i}><div className="level-label"><span>{i+1}</span><strong>Dependency level {i+1}</strong><small>{level.length} {level.length===1?'task':'tasks'}{filter!=='all'?' before filtering':''}</small></div><div className="sequence-cards">{show(level)}</div></div>)}{graph.unresolved.some(visible)&&<div className="sequence-unresolved"><h4>Dependencies need context</h4><p className="muted">These prerequisites are outside the available plan, incomplete, or form a cycle. No execution order is assumed.</p>{show(graph.unresolved)}</div>}{others.some(visible)&&<div className="sequence-level"><div className="level-label"><strong>Validation assignments</strong></div><div className="sequence-cards">{show(others)}</div></div>}</section>;
    })}{execution?.tasks_has_more&&<p className="work-attention">Showing the most recent 200 assignments. Summary counts cover the whole run; omitted prerequisites may prevent grouping.</p>}</section>;
}

export function BotsPanel({item,execution}:{item:Work;execution?:Execution|null}){
  const bots=botRoster(execution);
  return <section aria-label="Bots used by this run"><h3>The bots on this task</h3><p className="muted">Models from recorded assignments and calls for this run. Fallback or escalated models appear separately.</p>{!bots.length&&<p className="work-empty">Bots will appear when the run records an assignment or model call.</p>}
    <div className="bot-grid">{bots.map(bot=>{const working=bot.tasks.filter(t=>t.state==='RUNNING'),done=bot.tasks.filter(t=>finishedTask(t.state)).length,calls=bot.usage.reduce((n,b)=>n+b.calls,0),unknown=bot.usage.reduce((n,b)=>n+b.unavailable_calls,0),errors=bot.usage.reduce((n,b)=>n+b.errors,0);
      return <article className={`bot-card ${working.length&&item.status==='running'?'working':''}`} key={bot.key}><div className="bot-card-heading"><span className="bot-avatar" aria-hidden="true">{(bot.role||'?').slice(0,2).toUpperCase()}</span><div><h4>{name(bot.role)}</h4><span className={`result-tag ${working.length&&item.status==='running'?'active':''}`}>{working.length?(item.status==='running'?'Working':'Last active'):bot.tasks.length&&done===bot.tasks.length?'Assignments finished':calls?'Recorded usage':'Assigned'}</span></div></div><strong className="bot-model">{bot.model||'Model not recorded'}</strong><p className="bot-provider">{[...new Set(bot.usage.map(b=>b.provider).filter(Boolean))].join(' · ')||'Provider not recorded yet'}</p><div className="bot-stats"><span><strong>{calls}</strong> calls</span><span><strong>{money(bot.usage.reduce((n,b)=>n+b.known_cost_usd,0))}</strong> known usage</span><span><strong>{done}/{bot.tasks.length}</strong> assignments finished</span></div>{unknown>0&&<p className="muted">{unknown} calls have no recorded cost.</p>}{errors>0&&<p className="bot-call-errors">{errors} calls recorded an error.</p>}{!!bot.tasks.length&&<div className="task-chips">{bot.tasks.map(t=><span key={t.task_key} title={t.title??''}>{t.task_key} · {taskLabel(t.state)}</span>)}</div>}</article>;
    })}</div>{execution?.bots_has_more&&<p className="muted">Showing the 40 most-used role/model/provider combinations. Run cost totals cover all calls.</p>}</section>;
}

export function WorkDetails({item,session,onDone,onArchived}:{item:Work;session:Session|null;onDone:()=>void;onArchived:()=>void}){
  const key=`sf-work-tab:${item.id}`;
  const [tab,setTab]=useState<Tab>(()=>{const saved=readLocal(key,'overview');return tabs.some(t=>t[0]===saved)?saved as Tab:'overview';});
  const [opened,setOpened]=useState(()=>new Set<Tab>([tab,'controls']));
  const [pending,setPending]=useState(0);const id=useId();
  const {snapshot,events,error,updated}=useEvidence(item.id,item.run_id);
  const execution=snapshot?.execution,progress=taskProgress(execution),checks=currentChecks(snapshot).checks;
  useEffect(()=>{saveLocal(key,tab);setOpened(previous=>previous.has(tab)?previous:new Set([...previous,tab]));},[key,tab]);
  const pr=item.pr_url&&/^https:\/\/github\.com\//i.test(item.pr_url)?item.pr_url:null;
  const count=(name:Tab)=>name==='sequence'?progress.total:name==='bots'?botRoster(execution).length:name==='checks'?checks.length:name==='controls'?pending:0;
  function keyDown(event:KeyboardEvent<HTMLButtonElement>,index:number){
    const next=event.key==='ArrowRight'?(index+1)%tabs.length:event.key==='ArrowLeft'?(index+tabs.length-1)%tabs.length:event.key==='Home'?0:event.key==='End'?tabs.length-1:-1;
    if(next<0)return;event.preventDefault();setTab(tabs[next][0]);event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role=tab]')[next]?.focus();
  }
  return <div className="work-detail-workspace"><div className="work-detail-title"><div><p className="work-item-meta">{item.repo||'No repository'} · {item.id}{execution?` · ${execution.mode} mode`:''}</p><h3>{item.title}</h3></div><div className="work-title-actions"><span className={`badge ${item.status}`}>{name(item.status)}</span>{pr&&<a className="work-pr" href={pr} target="_blank" rel="noreferrer">Pull request ↗</a>}</div></div>
    <div className="work-live-line"><span className={item.status==='running'?'live-dot':''}/><span>{item.stage?name(item.stage):'Not started'}</span>{(execution?.repair_cycle??0)>0&&<span className="repair-badge">Repair cycle {execution?.repair_cycle}</span>}<small>{updated?`Updated ${new Date(updated).toLocaleTimeString()}`:'Connecting to progress…'}</small></div>
    <PhaseStrip item={item} execution={execution}/>
    {pending>0&&<div className="work-decision" role="status"><strong>{pending} {pending===1?'approval needs':'approvals need'} your decision</strong><button onClick={()=>setTab('controls')}>Review approvals</button></div>}
    {error&&<p role="alert" className="error">{error} {snapshot&&'Showing the last successful snapshot.'}</p>}
    {snapshot&&item.run_id&&snapshot.execution===undefined&&<p className="work-attention">Restart the Python dashboard server to load task sequencing and bot details. Existing evidence is available below.</p>}
    <div className="work-tabs" role="tablist" aria-label="Task detail views">{tabs.map(([name,label],i)=><button key={name} id={`${id}-${name}`} type="button" role="tab" tabIndex={tab===name?0:-1} aria-selected={tab===name} aria-controls={`${id}-panel-${name}`} onClick={()=>setTab(name)} onKeyDown={e=>keyDown(e,i)}>{label}{count(name)>0&&<span className={name==='controls'?'needs-you':''}>{count(name)}</span>}</button>)}</div>
    {tabs.map(([name,label])=><section key={name} id={`${id}-panel-${name}`} className="work-tab-panel" role="tabpanel" aria-labelledby={`${id}-${name}`} tabIndex={0} hidden={tab!==name}>
      {name==='controls'?<><WorkControls itemId={item.id} session={session} onDone={onDone} onPendingChange={setPending}/>{item.run_id&&!['running','queued','tentative'].includes(item.status)&&<div className="work-archive"><ActionButton operation="set_archived" target={item.id} args={{archived:!item.archived}} disabled={!session?.actions_enabled} onDone={onArchived}>{item.archived?'Unarchive task':'Archive task'}</ActionButton><p className="muted">{item.archived?'This task is archived.':'Archive hides this task from Work.'} Reports and evidence stay retained.</p></div>}<CostsPanel snapshot={snapshot}/><details className="work-disclosure"><summary>Run identifiers and timestamps</summary><dl><div><dt>Run</dt><dd>{item.run_id||'Not dispatched'}</dd></div><div><dt>Created</dt><dd>{when(item.created_at)}</dd></div><div><dt>Last updated</dt><dd>{when(item.updated_at)}</dd></div><div><dt>Finished</dt><dd>{when(item.finished_at)}</dd></div></dl></details></>:
        opened.has(name)&&(name==='overview'?<WorkOverview item={item} snapshot={snapshot} events={events} onTab={setTab}/>:name==='sequence'?<SequencePanel item={item} execution={execution}/>:name==='bots'?<BotsPanel item={item} execution={execution}/>:name==='checks'?<ChecksPanel snapshot={snapshot}/>:name==='files'?item.run_id?<Artifacts itemId={item.id} runId={item.run_id}/>:<p className="work-empty">Files will appear after a run retains a report or screenshots.</p>:<ActivityPanel events={events}/>)}</section>)}
  </div>;
}
