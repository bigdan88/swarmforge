import {useState,type FormEvent} from 'react';
import {collection,label,money,num,readLocal,record,rows,saveLocal,stamp,str,useGet,useAction,useDraft,type Session} from './api';
import {ProjectForm} from './Forms';
import {Markdown,Transcript} from './Messages';
import {ActionButton,Feedback,Heading,Loading,Pager,type Navigate} from './ui';
export function ProjectsPage({session,navigate,selected}:{session:Session|null;navigate:Navigate;selected:string}){
  const [offset,setOffset]=useState(0),[search,setSearch]=useState(''),[creating,setCreating]=useState(false);
  const list=useGet(`/api/v1/projects?offset=${offset}&search=${encodeURIComponent(search)}`,collection,8000,true);
  return <><Heading title="Projects" description="Plan milestones, inspect evidence and make explicit launch and checkpoint decisions."><button className="primary" onClick={()=>{setCreating(true);navigate('projects');}}>New project</button></Heading>
    {creating&&!selected?<ProjectForm session={session} onSaved={id=>{setCreating(false);list.refresh();navigate('projects',id);}} onCancel={()=>setCreating(false)}/>:selected?<ProjectDetail key={selected} id={selected} session={session} navigate={navigate}/>:<section className="panel"><div className="toolbar"><h2>Your projects</h2><label><span className="sr-only">Search projects</span><input value={search} onChange={e=>{setSearch(e.target.value);setOffset(0);}} placeholder="Search projects or repositories"/></label></div><Feedback error={list.error}/>{!list.data&&!list.error&&<Loading error=""/>}{list.data?.items.map(p=><button className="task project-row" key={str(p.id)} onClick={()=>navigate('projects',str(p.id))}><span className="task-copy"><strong>{str(p.name)}</strong><small>{str(p.repo)||'No repository selected'}</small></span><span className="badge">{label(p.status)}</span><span>{money(p.spent_usd)} / {money(p.budget_usd)}</span></button>)}{list.data?.items.length===0&&<p className="empty">No projects match. Start with a draft and review its spec before launch.</p>}{list.data&&<Pager offset={offset} total={list.data.total} setOffset={setOffset}/>}</section>}
  </>;
}
type Tab='overview'|'milestones'|'tasks'|'conversation'|'history';
const DONE=new Set(['done','completed','merged']),FAILED=new Set(['failed','blocked','cancelled','budget_exceeded']);
/** A plain-language next step for common blockers, so the error is actionable. */
export function blockerHint(error:string){
  if(/docker/i.test(error))return 'Start Docker Desktop and wait until it says it is running. If it hangs: quit it, run “wsl --shutdown” in a terminal, reopen it. Then resume the project.';
  if(/WinError 206|command line is too long|filename or extension is too long/i.test(error))return 'This was the Windows command-length limit. Update SwarmForge (prompts are now passed as files), then retry.';
  if(/budget|allowance/i.test(error))return 'The allowance ran out. Raise the project budget or narrow the milestone, then resume.';
  if(/rate.?limit|429/i.test(error))return 'The model provider is rate-limiting. Wait a few minutes, then resume.';
  if(/api key|401|unauthori[sz]ed/i.test(error))return 'A provider key is missing or rejected. Check Settings → API keys, then resume.';
  return '';
}
function headline(status:string,current:string,needsYou:boolean){
  if(status==='drafting')return {tone:'',text:'Draft — refine it with the lead or edit it, then approve to launch.'};
  if(needsYou)return {tone:'attention',text:'Waiting for your decision before it continues.'};
  if(status==='running')return {tone:'running',text:current?`Working on “${current}”.`:'Running.'};
  if(status==='paused')return {tone:'',text:'Paused. Resume when you are ready.'};
  if(status==='done')return {tone:'done',text:'Finished. Every milestone was accepted.'};
  if(status==='cancelled')return {tone:'',text:'Cancelled. History is kept below.'};
  return {tone:'failed',text:'Stopped — see what went wrong below.'};
}
function ProjectDetail({id,session,navigate}:{id:string;session:Session|null;navigate:Navigate}){
  const view=useGet(`/api/v1/projects/${encodeURIComponent(id)}`,record,6000);
  const [editing,setEditing]=useState(false),[authorized,setAuthorized]=useState(false),[manual,setManual]=useState(false),[reply,setReply]=useState('continue'),[saved,setSaved]=useState('');
  const [tab,setTabState]=useState<Tab|''>(()=>readLocal(`sf-project-tab:${id}`,'') as Tab|'');
  function setTab(t:Tab){setTabState(t);saveLocal(`sf-project-tab:${id}`,t);}
  if(!view.data)return <Loading error={view.error}/>;
  const p=record(view.data.project),definition=record(view.data.definition),status=str(p.status),ms=rows(definition.milestones);
  if(editing)return <ProjectForm session={session} initial={definition} projectId={id} expectedSpec={str(view.data.spec_sha)} onSaved={()=>{setEditing(false);setSaved('Draft saved. Nothing has launched.');view.refresh();}} onCancel={()=>setEditing(false)}/>;
  const savedMs=rows(view.data.milestones),tasks=rows(view.data.tasks),messages=rows(view.data.messages),events=rows(view.data.events);
  const idx=Math.min(num(p.current_milestone),Math.max(0,ms.length-1)),current=record(ms[idx]??{});
  const needsYou=['at_checkpoint','waiting_for_you'].includes(status),error=str(p.last_error),hint=blockerHint(error);
  const done=tasks.filter(t=>DONE.has(str(t.status))).length,failed=tasks.filter(t=>FAILED.has(str(t.status))).length,active=tasks.length-done-failed;
  const head=headline(status,str(current.name),needsYou);
  const active_tab:Tab=tab||(status==='drafting'?'conversation':'overview');
  const tabs:[Tab,string][]=[['overview','Overview'],['milestones',`Milestones (${ms.length})`],['tasks',`Tasks (${tasks.length})`],['conversation',`Conversation${messages.length?` (${messages.length})`:''}`],['history','Spec & history']];
  const refreshWith=(r:{message:string})=>{setSaved(r.message);view.refresh();};
  return <><button className="back-link" onClick={()=>navigate('projects')}>← All projects</button>
    <section className="panel project-summary"><Feedback error={view.error} message={saved}/>
      <div className="detail-heading"><div className="project-title"><h2>{str(p.name)}</h2><p className="muted">{str(p.repo)||'No repository selected'}</p></div>
        <div className="button-row">{status==='drafting'&&<button disabled={!session?.actions_enabled||view.data.truncated===true} onClick={()=>setEditing(true)}>Edit draft</button>}{status==='running'&&<ActionButton quiet operation="pause_project" target={id} disabled={!session?.actions_enabled} onDone={refreshWith}>Pause</ActionButton>}{!['done','cancelled','drafting'].includes(status)&&<ActionButton quiet operation="cancel_project" target={id} disabled={!session?.actions_enabled} onDone={refreshWith}>Cancel project</ActionButton>}</div></div>
      <p className={`status-line ${head.tone}`}><span className={`dot ${head.tone}`}/> <strong>{label(status)}</strong> · {head.text}</p>
      {error&&!['done','drafting'].includes(status)&&<div className="blocker" role="alert"><strong>What went wrong</strong><p>{error}</p>{hint&&<p className="hint"><strong>Try this:</strong> {hint}</p>}</div>}
      <div className="project-stats">
        <div><span>Milestone</span><strong>{ms.length?`${idx+1} of ${ms.length}`:'—'}</strong></div>
        <div><span>Tasks</span><strong>{done} done{active?` · ${active} open`:''}{failed?` · ${failed} stopped`:''}</strong></div>
        <div className="spend"><span>Spent</span><strong>{money(p.spent_usd)} <small className="muted">of {money(p.budget_usd)}</small></strong><progress max={Math.max(1,num(p.budget_usd))} value={num(p.spent_usd)} aria-label="Recorded project spending"/></div>
      </div>
      {(status==='drafting'||needsYou)&&<div className="approval-card"><h3>{status==='drafting'?'Ready to launch?':'Your decision is needed'}</h3>{needsYou&&<><label>Reply or correction<textarea value={reply} onChange={e=>setReply(e.target.value)}/></label><label className="check"><input type="checkbox" checked={manual} onChange={e=>setManual(e.target.checked)}/>I verified any checks marked “needs you”.</label></>}
        <label className="check"><input type="checkbox" checked={authorized} onChange={e=>setAuthorized(e.target.checked)}/>{status==='drafting'?`I approve the specification (Spec & history tab) and authorize work within ${money(p.budget_usd)}.`:'I authorize continuing within the approved specification and remaining allowance.'}</label>
        <ActionButton operation={status==='drafting'?'launch_project':'resume_project'} target={id} args={{expected_spec:view.data.spec_sha,spending_authorized:authorized,confirm_manual:manual,reply}} disabled={!session?.models_enabled||!authorized||view.data.truncated===true} onDone={r=>{setAuthorized(false);refreshWith(r);}}>{status==='drafting'?'Approve and launch':'Submit checkpoint reply'}</ActionButton>
      </div>}
    </section>
    <div className="tabs" role="tablist" aria-label="Project sections">{tabs.map(([key,text])=><button key={key} role="tab" id={`ptab-${key}`} aria-selected={active_tab===key} aria-controls={`ppanel-${key}`} className={active_tab===key?'selected':''} onClick={()=>setTab(key)}>{text}</button>)}</div>
    <section className="panel" role="tabpanel" id={`ppanel-${active_tab}`} aria-labelledby={`ptab-${active_tab}`}>
      {active_tab==='overview'&&<>
        <ol className="stepper">{ms.map((m,i)=>{const st=str(savedMs.find(x=>num(x.idx)===i)?.status)||(status==='drafting'?'planned':'not started');return <li key={i} className={`${i===idx&&!['done','drafting'].includes(status)?'current':''} ${DONE.has(st)||st==='accepted'?'done':''}`}><button type="button" onClick={()=>setTab('milestones')}><span className="step-num">{i+1}</span><span className="step-name">{str(m.name)}</span><span className="badge">{label(st)}</span></button></li>;})}{!ms.length&&<li className="muted">No milestones yet.</li>}</ol>
        {ms.length>0&&<div className="current-milestone"><h3>{status==='done'?'Last milestone':status==='drafting'?'First milestone':'Current milestone'}: {str(current.name)}</h3><p>{str(current.goal)}</p>{Array.isArray(current.checks)&&current.checks.length>0&&<><p className="muted">Done when:</p><ul>{current.checks.map((c,j)=><li key={j}>{str(c)}</li>)}</ul></>}</div>}
        <h3>Recent activity</h3>{events.slice(0,5).map(e=><p key={num(e.id)} className="activity"><small className="muted">{stamp(e.ts)}</small> {label(e.kind)}{str(e.detail)&&<span className="muted"> — {str(e.detail).split('\n')[0].slice(0,140)}</span>}</p>)}{!events.length&&<p className="muted">Nothing has happened yet.</p>}{events.length>5&&<button type="button" className="link-button" onClick={()=>setTab('history')}>All activity →</button>}
      </>}
      {active_tab==='milestones'&&<>{ms.map((m,i)=>{const sm=savedMs.find(x=>num(x.idx)===i);return <article className="milestone" key={i}><span className="badge">{sm?label(sm.status):'Not launched'}</span><h3>{i+1}. {str(m.name)}</h3><p>{str(m.goal)}</p><ul>{Array.isArray(m.checks)&&m.checks.map((c,j)=><li key={j}>{str(c)}</li>)}</ul><p className="muted">Estimate {money(m.estimate_usd)} · {num(m.estimate_hours)} hours{sm?` · spent ${money(sm.spent_usd)}`:''}</p>{str(sm?.report)&&<details><summary>Checkpoint evidence</summary><pre>{str(sm?.report)}</pre></details>}</article>;})}{!ms.length&&<p className="muted">No milestones yet.</p>}</>}
      {active_tab==='tasks'&&<>{tasks.map(task=><button key={str(task.id)} className="task" onClick={()=>navigate('work',str(task.id))}><span className={`dot ${DONE.has(str(task.status))?'done':FAILED.has(str(task.status))?'failed':str(task.status)==='running'?'running':''}`}/><span className="task-copy"><strong>{str(task.title)}</strong><small>{label(task.status)} · {label(task.stage)}</small></span></button>)}{!tasks.length&&<p className="muted">No tasks yet. Work is queued when the project launches.</p>}</>}
      {active_tab==='conversation'&&<div className="project-conversation"><Transcript count={messages.length} label="Project conversation">{messages.map(m=><article className={`message ${str(m.role)}`} key={num(m.id)}><small>{label(m.role)} · {stamp(m.created_at)}</small>{str(m.role)==='assistant'?<Markdown text={str(m.content)}/>:<p>{str(m.content)}</p>}</article>)}{!messages.length&&<p className="muted">No messages yet.</p>}</Transcript>{status==='drafting'?<div className="sticky-composer"><ProjectComposer id={id} session={session} spec={str(view.data.spec_sha)} onDone={view.refresh}/></div>:<p className="muted">Checkpoint replies go through the decision box above.</p>}</div>}
      {active_tab==='history'&&<><details><summary>Full specification</summary><pre className="spec-text">{str(view.data.spec)}</pre>{view.data.truncated===true&&<p className="error">The spec exceeds this view's limit; editing and launch are disabled here.</p>}</details>
        <h3>Decisions and events</h3>{events.map(e=><details key={num(e.id)}><summary>{label(e.kind)} · {stamp(e.ts)}</summary><pre>{str(e.detail)}</pre></details>)}{!events.length&&<p className="muted">No events yet.</p>}<p className="muted">Latest 50 events.</p></>}
    </section>
  </>;
}

function ProjectComposer({id,session,spec,onDone}:{id:string;session:Session|null;spec:string;onDone:()=>void}){
  const [draft,setDraft]=useDraft(`sf-project-chat:${id}`,{message:''});
  const action=useAction(`project-chat:${id}`);
  async function submit(e?:FormEvent){e?.preventDefault();if(!draft.message.trim()||action.pending)return;const result=await action.send('project_message',id,{message:draft.message,expected_spec:spec});if(result){setDraft({message:''});onDone();}}
  return <form className="editor composer" onSubmit={submit}><fieldset disabled={!session?.models_enabled||action.pending}><label>Refine the project with the lead<textarea required rows={3} maxLength={16000} value={draft.message} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void submit();}}} onChange={e=>setDraft({message:e.target.value})}/></label><div className="send-row"><button className="primary" type="submit" disabled={!draft.message.trim()}>{action.pending?'Lead is responding…':'Send project message'}</button><span className="muted">Enter sends · nothing launches from a reply</span></div></fieldset><Feedback error={action.error} message={action.message}/></form>;
}
