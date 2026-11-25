import {useEffect,useRef,useState,type FormEvent} from 'react';
import {collection,label,latestRequest,money,num,readLocal,record,rows,saveLocal,stamp,str,useGet,useAction,useDraft,type Row,type Session} from './api';
import {ProjectForm} from './Forms';
import {ProjectRecovery} from './ProjectRecovery';
import {ProjectDecision} from './ProjectDecision';
import {ProjectChanges} from './ProjectChanges';
import {ProjectReview} from './ProjectReview';
import {LiveProjectActivity} from './ProjectActivity';
import {ChatMessage,downloadText,Markdown,replyIsSaved,Transcript,useLiveReply} from './Messages';
import {ActionButton,Feedback,Heading,Loading,Pager,type Navigate} from './ui';
export function ProjectsPage({session,navigate,selected}:{session:Session|null;navigate:Navigate;selected:string}){
  const [offset,setOffset]=useState(0),[search,setSearch]=useState('');
  const list=useGet(`/api/v1/projects?offset=${offset}&search=${encodeURIComponent(search)}`,collection,8000,true);
  return <><Heading title="Projects" description="Start with a conversation. Agree on requirements and milestones, review the proposal, then launch."><ActionButton operation="start_project" disabled={!session?.actions_enabled} onDone={r=>{if(r.project_id){list.refresh();navigate('projects',r.project_id);}}}>New project</ActionButton></Heading>
    {selected?<ProjectDetail key={selected} id={selected} session={session} navigate={navigate}/>:<section className="panel"><div className="toolbar"><h2>Your projects</h2><label><span className="sr-only">Search projects</span><input value={search} onChange={e=>{setSearch(e.target.value);setOffset(0);}} placeholder="Search projects or repositories"/></label></div><Feedback error={list.error}/>{!list.data&&!list.error&&<Loading error=""/>}{list.data?.items.map(p=><button className="task project-row" key={str(p.id)} onClick={()=>navigate('projects',str(p.id))}><span className="task-copy"><strong>{str(p.name)}</strong><small>{str(p.repo)||'Planning in conversation'}</small></span><span className="badge">{label(p.status)}</span><span>{money(p.spent_usd)} / {money(p.budget_usd)}</span></button>)}{list.data?.items.length===0&&<p className="empty">What would you like to build? New project opens a chat with the lead.</p>}{list.data&&<Pager offset={offset} total={list.data.total} setOffset={setOffset}/>}</section>}
  </>;
}
type Tab='overview'|'review'|'milestones'|'tasks'|'conversation'|'proposal'|'history';
const DONE=new Set(['done','completed','merged']),FAILED=new Set(['failed','blocked','budget_exceeded']);
export function projectTaskCounts(tasks:Row[]){return {done:tasks.filter(t=>DONE.has(str(t.status))).length,failed:tasks.filter(t=>FAILED.has(str(t.status))).length,active:tasks.filter(t=>['running','queued','tentative'].includes(str(t.status))).length};}
/** A plain-language next step for common blockers, so the error is actionable. */
export function blockerHint(error:string){
  if(/docker/i.test(error))return 'Start Docker Desktop and wait until it says it is running. If it hangs: quit it, run “wsl --shutdown” in a terminal, reopen it. Then resume the project.';
  if(/WinError 206|command line is too long|filename or extension is too long/i.test(error))return 'This was the Windows command-length limit. Update SwarmForge (prompts are now passed as files), then retry.';
  if(/budget|allowance/i.test(error))return 'Review which task or project limit stopped the work and choose a bounded recovery from the project decision panel.';
  if(/rate.?limit|429/i.test(error))return 'The model provider is rate-limiting. Wait a few minutes, then resume.';
  if(/api key|401|unauthori[sz]ed/i.test(error))return 'A provider key is missing or rejected. Check Settings → API keys, then resume.';
  return '';
}
function headline(status:string,current:string,needsYou:boolean){
  if(status==='drafting')return {tone:'',text:'Planning with the lead. Discuss your idea, then review the proposal before launching.'};
  if(needsYou)return {tone:'attention',text:'Waiting for your decision before it continues.'};
  if(status==='running')return {tone:'running',text:current?`Working on “${current}”.`:'Running.'};
  if(status==='paused')return {tone:'',text:'Paused. Resume when you are ready.'};
  if(status==='done')return {tone:'done',text:'Finished. Every milestone was accepted.'};
  if(status==='cancelled')return {tone:'',text:'Cancelled. History is kept below.'};
  return {tone:'failed',text:'Stopped — see what went wrong below.'};
}
function ProjectDetail({id,session,navigate}:{id:string;session:Session|null;navigate:Navigate}){
  const view=useGet(`/api/v1/projects/${encodeURIComponent(id)}`,record,6000);
  const conversation=str(record(view.data?.project??{}).conversation_id);
  const live=useLiveReply(conversation,view.data?latestRequest(view.data.requests):null,view.refresh);
  const thinking=live.request?.status==='thinking';
  const previousSpec=useRef<string|null>(null);
  const [editing,setEditing]=useState(false),[saved,setSaved]=useState('');
  const [amending,setAmending]=useState(false);
  const [tab,setTabState]=useState<Tab|''>(()=>readLocal(`sf-project-tab:${id}`,'') as Tab|'');
  useEffect(()=>{if(!view.data)return;const sha=str(view.data.spec_sha);if(previousSpec.current!==null&&sha!==previousSpec.current&&str(view.data.spec)&&record(view.data.project).status==='drafting'){setTabState('proposal');saveLocal(`sf-project-tab:${id}`,'proposal');}previousSpec.current=sha;},[id,view.data?.spec_sha]);
  function setTab(t:Tab){setTabState(t);saveLocal(`sf-project-tab:${id}`,t);}
  if(!view.data)return <Loading error={view.error}/>;
  const p=record(view.data.project),definition=record(view.data.definition),status=str(p.status),ms=rows(definition.milestones);
  const ready=status==='drafting'&&!!str(view.data.spec).trim()&&ms.length>=3;
  if(editing)return <ProjectForm session={session} initial={definition} projectId={id} expectedSpec={str(view.data.spec_sha)} amending={amending} completedMilestones={num(p.current_milestone)} onSaved={()=>{setEditing(false);setAmending(false);setSaved(amending?'Revised specification approved. Continue when ready.':'Draft saved. Nothing has launched.');view.refresh();}} onCancel={()=>{setEditing(false);setAmending(false);}}/>;
  const savedMs=rows(view.data.milestones),tasks=rows(view.data.tasks),messages=rows(view.data.messages),events=rows(view.data.events);
  const idx=Math.min(num(p.current_milestone),Math.max(0,ms.length-1)),current=record(ms[idx]??{});
  const needsYou=['at_checkpoint','waiting_for_you'].includes(status),error=str(p.last_error),hint=blockerHint(error);
  const recovery=record(view.data.recovery??{}),planningRetry=recovery.can_retry===true;
  const {done,failed,active}=projectTaskCounts(tasks);
  const head=headline(status,str(current.name),needsYou);
  const active_tab:Tab=status==='drafting'?(ready&&(!tab||tab==='proposal')?'proposal':'conversation'):(tab==='proposal'?'overview':tab||(['at_checkpoint','done'].includes(status)?'review':'overview'));
  const tabs:[Tab,string][]=status==='drafting'?[['conversation','Plan in chat'],...(ready?[['proposal','Review proposal'] as [Tab,string]]:[])]:[['overview','Overview'],['review','Review what was built'],['milestones',`Milestones (${ms.length})`],['tasks',`Tasks (${tasks.length})`],['conversation',`Conversation${messages.length?` (${messages.length})`:''}`],['history','Spec & history']];
  const refreshWith=(r:{message:string})=>{setSaved(r.message);view.refresh();};
  return <><button className="back-link" onClick={()=>navigate('projects')}>← All projects</button>
    <section className="panel project-summary"><Feedback error={view.error} message={saved}/>
      <div className="detail-heading"><div className="project-title"><h2>{str(p.name)}</h2><p className="muted">{str(p.repo)||'No repository selected'}</p></div>
        <div className="button-row">{ready&&active_tab==='proposal'&&<button disabled={!session?.actions_enabled||view.data.truncated===true} onClick={()=>setEditing(true)}>Edit draft</button>}{status==='running'&&<ActionButton quiet operation="pause_project" target={id} disabled={!session?.actions_enabled} onDone={refreshWith}>Pause</ActionButton>}{!['done','cancelled','drafting'].includes(status)&&<ActionButton quiet operation="cancel_project" target={id} disabled={!session?.actions_enabled} onDone={refreshWith}>Cancel project</ActionButton>}</div></div>
      <p className={`status-line ${head.tone}`}><span className={`dot ${head.tone}`}/> <strong>{label(status)}</strong> · {head.text}</p>
      {error&&!['done','drafting'].includes(status)&&!needsYou&&<div className="blocker" role="alert"><strong>{planningRetry?'Planning response rejected':'What went wrong'}</strong><p>{planningRetry?'The lead could not produce a valid next action within this round’s limits. The rejection reasons and retry controls are below.':error}</p>{planningRetry?<details><summary>Saved stopping reason</summary><p>{error}</p></details>:hint&&<p className="hint"><strong>Try this:</strong> {hint}</p>}</div>}
      {status!=='drafting'&&<div className="project-stats">
        <div><span>Milestone</span><strong>{ms.length?`${idx+1} of ${ms.length}`:'—'}</strong></div>
        <div><span>Task history</span><strong>{done} completed{active?` · ${active} open`:''}{failed?` · ${failed} failed attempts`:''}</strong></div>
        <div className="spend"><span>Spent</span><strong>{money(p.spent_usd)} <small className="muted">of {money(p.budget_usd)}</small></strong><progress max={Math.max(1,num(p.budget_usd))} value={num(p.spent_usd)} aria-label="Recorded project spending"/></div>
      </div>}
      {ready&&active_tab==='conversation'&&<div className="notice"><p>The lead has prepared a proposal. Keep chatting to refine it, or review the scope, milestones and budget.</p><button onClick={()=>setTab('proposal')}>Review proposal</button></div>}
      {status==='running'&&<LiveProjectActivity tasks={tasks} activity={str(recovery.activity)} session={session} navigate={navigate} error={view.error} onRefresh={view.refresh}/>}
      {status==='running'&&record(recovery.automation??{}).enabled===true&&<p className="muted">Automatic delivery is enabled. The swarm repairs routine problems and continues through milestones within the approved {money(p.budget_usd)} cap. Unverified checks stay on the final review list.</p>}
      {ready&&active_tab==='proposal'&&<div className="approval-card"><h3>Approve this project</h3><p>Launch approves the specification shown below and permits work within the {money(p.budget_usd)} project allowance.</p><ActionButton operation="launch_project" target={id} args={{expected_spec:view.data.spec_sha,spending_authorized:true}} disabled={!session?.models_enabled||thinking||view.data.truncated===true} onDone={refreshWith}>Approve specification &amp; launch</ActionButton></div>}
      {['at_checkpoint','done','waiting_for_you'].includes(status)&&<div className="notice"><p>Something not working, or want it improved? Tell the lead in the conversation — it will discuss it with you and propose a plan you can launch.</p><button onClick={()=>{setTab('conversation');setTimeout(()=>{const box=document.getElementById(`talk-to-lead-${id}`);box?.scrollIntoView({behavior:'smooth',block:'center'});(box as HTMLTextAreaElement|null)?.focus();},80);}}>Talk to the lead</button></div>}
      {status==='done'&&<ProjectChanges id={id} project={p} session={session} onDone={refreshWith}/>}
      {needsYou&&!planningRetry&&<ProjectDecision key={str(recovery.decision_token)} id={id} recovery={recovery} spec={str(view.data.spec_sha)} project={p} session={session} nextMilestone={str(ms[idx+1]?.name)} onDone={refreshWith} onAmend={()=>{setAmending(true);setEditing(true);}} navigate={navigate}/>}

      {(planningRetry||rows(recovery.rejections).length>0)&&<ProjectRecovery id={id} project={p} recovery={recovery} spec={str(view.data.spec_sha)} session={session} onDone={refreshWith} onAmend={()=>{setAmending(true);setEditing(true);}}/>}
    </section>
    <div className="tabs" role="tablist" aria-label="Project sections">{tabs.map(([key,text])=><button key={key} role="tab" id={`ptab-${key}`} aria-selected={active_tab===key} aria-controls={`ppanel-${key}`} className={active_tab===key?'selected':''} onClick={()=>setTab(key)}>{text}</button>)}</div>
    <section className="panel" role="tabpanel" id={`ppanel-${active_tab}`} aria-labelledby={`ptab-${active_tab}`}>
      {active_tab==='proposal'&&<><div className="detail-heading"><h2>Proposed specification</h2><button type="button" onClick={()=>downloadText(str(view.data?.spec),'project-spec.md')}>Download specification</button></div><p className="muted">Check the scope, repository, milestones and total API allowance. Return to Plan in chat to request changes.</p><Markdown text={str(view.data.spec)}/>{view.data.truncated===true&&<p className="error">This proposal is shortened; launch is disabled.</p>}</>}
      {active_tab==='overview'&&<>
        <ol className="stepper">{ms.map((m,i)=>{const st=str(savedMs.find(x=>num(x.idx)===i)?.status)||(status==='drafting'?'planned':'not started');return <li key={i} className={`${i===idx&&!['done','drafting'].includes(status)?'current':''} ${DONE.has(st)||st==='accepted'?'done':''}`}><button type="button" onClick={()=>setTab('milestones')}><span className="step-num">{i+1}</span><span className="step-name">{str(m.name)}</span><span className="badge">{label(st)}</span></button></li>;})}{!ms.length&&<li className="muted">No milestones yet.</li>}</ol>
        {ms.length>0&&<div className="current-milestone"><h3>{status==='done'?'Last milestone':status==='drafting'?'First milestone':'Current milestone'}: {str(current.name)}</h3><p>{str(current.goal)}</p>{Array.isArray(current.checks)&&current.checks.length>0&&<><p className="muted">Done when:</p><ul>{current.checks.map((c,j)=><li key={j}>{str(c)}</li>)}</ul></>}</div>}
        <h3>Recent activity</h3>{events.slice(0,5).map(e=><p key={num(e.id)} className="activity"><small className="muted">{stamp(e.ts)}</small> {label(e.kind)}{str(e.detail)&&<span className="muted"> — {str(e.detail).split('\n')[0].slice(0,140)}</span>}</p>)}{!events.length&&<p className="muted">Nothing has happened yet.</p>}{events.length>5&&<button type="button" className="link-button" onClick={()=>setTab('history')}>All activity →</button>}
      </>}
      {active_tab==='milestones'&&<>{ms.map((m,i)=>{const sm=savedMs.find(x=>num(x.idx)===i);return <article className="milestone" key={i}><span className="badge">{sm?label(sm.status):'Not launched'}</span><h3>{i+1}. {str(m.name)}</h3><p>{str(m.goal)}</p><ul>{Array.isArray(m.checks)&&m.checks.map((c,j)=><li key={j}>{str(c)}</li>)}</ul><p className="muted">Estimate {money(m.estimate_usd)} · {num(m.estimate_hours)} hours{sm?` · spent ${money(sm.spent_usd)}`:''}</p>{str(sm?.report)&&<details><summary>Checkpoint evidence</summary><pre>{str(sm?.report)}</pre></details>}</article>;})}{!ms.length&&<p className="muted">No milestones yet.</p>}</>}
      {active_tab==='tasks'&&<><p className="muted">Plans removed before starting are retained as history. They are not failed builds.</p>{tasks.map(task=><button key={str(task.id)} className="task" onClick={()=>navigate('work',str(task.id))}><span className={`dot ${DONE.has(str(task.status))?'done':FAILED.has(str(task.status))?'failed':str(task.status)==='running'?'running':''}`}/><span className="task-copy"><strong>{str(task.title)}</strong><small>{task.status==='cancelled'&&!task.run_id?'Removed from plan — not run':label(task.status)}{str(task.stage)?` · ${label(task.stage)}`:''}</small></span></button>)}{!tasks.length&&<p className="muted">No tasks yet. Work is queued when the project launches.</p>}</>}
      {active_tab==='conversation'&&<div className="project-conversation"><Transcript count={messages.length+(thinking?1:0)} label="Project conversation">{messages.map(m=><ChatMessage message={m} conversation={conversation} key={num(m.id)}/>)}{str(live.request?.partial_text)&&!replyIsSaved(messages,live.request)&&<ChatMessage message={{role:'assistant',content:live.request?.partial_text}} streaming={thinking}/>}{thinking&&<p className="muted" role="status">{live.reconnecting?'Reconnecting to the reply…':'The lead is responding…'}</p>}{live.request&&['failed','interrupted'].includes(str(live.request.status))&&<p className="error" role="alert">{str(live.request.error)}</p>}{!messages.length&&<p className="muted">No messages yet.</p>}</Transcript>{status==='drafting'?<div className="sticky-composer"><ProjectComposer id={id} session={session} busy={thinking} spec={str(view.data.spec_sha)} question={messages.filter(m=>m.role==='assistant').at(-1)} onDone={ready=>{view.refresh();if(ready)setTab('proposal');}}/></div>:status!=='cancelled'?<div className="sticky-composer"><LaunchProposal id={id} project={p} session={session} message={messages.filter(m=>m.role==='assistant').at(-1)} onDone={refreshWith}/><ProjectComposer id={id} session={session} busy={thinking} spec={str(view.data.spec_sha)} launched onDone={()=>view.refresh()}/></div>:<p className="muted">This project was cancelled.</p>}</div>}
      {active_tab==='review'&&<ProjectReview id={id} session={session} onTalk={()=>{setTab('conversation');setTimeout(()=>{const box=document.getElementById(`talk-to-lead-${id}`);box?.scrollIntoView({behavior:'smooth',block:'center'});(box as HTMLTextAreaElement|null)?.focus();},80);}}/>}
      {active_tab==='history'&&<><details><summary>Full specification</summary><pre className="spec-text">{str(view.data.spec)}</pre>{view.data.truncated===true&&<p className="error">The spec exceeds this view's limit; editing and launch are disabled here.</p>}</details>
        <h3>Decisions and events</h3>{events.map(e=><details key={num(e.id)}><summary>{label(e.kind)} · {stamp(e.ts)}</summary><pre>{str(e.detail)}</pre></details>)}{!events.length&&<p className="muted">No events yet.</p>}<p className="muted">Latest 50 events.</p></>}
    </section>
  </>;
}

function LaunchProposal({id,project,session,message,onDone}:{id:string;project:Row;session:Session|null;message?:Row;onDone:(r:{message:string})=>void}){
  const proposal=record(record(message?.actions??{}).change_proposal??{}),changes=Array.isArray(proposal.changes)?proposal.changes.map(str):[];
  const [extra,setExtra]=useState('0');
  const action=useAction(`project-launch:${id}`);
  if(!changes.length)return null;
  const remaining=Math.max(0,num(project.budget_usd)-num(project.spent_usd)),added=Number(extra)||0,status=str(project.status);
  const effect=status==='done'?'The project reopens with these changes as a new milestone.':status==='running'?'The lead takes these changes into its next step.':'The milestone stays open (nothing is accepted) and the lead plans these fixes.';
  return <section className="project-decision" aria-label="Proposed changes">
    <div className="decision-heading"><span className="badge">Proposed by the lead</span><h3>{str(proposal.title)||'Proposed changes'}</h3></div>
    <ul>{changes.map((c,i)=><li key={i}>{c}</li>)}</ul>
    {str(proposal.budget_note)&&<p className="muted">{str(proposal.budget_note)}</p>}
    <p className="muted">{effect} {money(remaining)} of the approved {money(project.budget_usd)} is left{remaining+added<2?' — a build task needs at least $2, so add budget below':''}. Not right yet? Keep talking — the lead updates the plan.</p>
    <div className="form-grid"><label>Add to the budget (USD)<input type="number" min="0" max="500" step="1" value={extra} onChange={e=>setExtra(e.target.value)}/></label></div>
    <button className="primary" disabled={!session?.models_enabled||action.pending||added<0||added>500||remaining+added<2} onClick={()=>{void action.send('launch_project_changes',id,{extra_budget:added,spending_authorized:true}).then(r=>{if(r){setExtra('0');onDone(r);}});}}>{action.pending?'Launching…':'Launch these changes'}</button>
    <Feedback error={action.error} message={action.message}/>
  </section>;
}

function ProjectComposer({id,session,spec,question,onDone,busy=false,launched=false}:{id:string;session:Session|null;spec:string;question?:Row;onDone:(ready:boolean)=>void;busy?:boolean;launched?:boolean}){
  const [draft,setDraft]=useDraft(`sf-project-chat:${id}`,{message:''});
  const action=useAction(`project-chat:${id}`);
  async function submit(e?:FormEvent){e?.preventDefault();if(!draft.message.trim()||action.pending||busy)return;const result=await action.send('project_message',id,{message:draft.message,expected_spec:spec});if(result){setDraft({message:''});onDone(result.draft_ready===true);}}
  const fields=rows(record(question?.actions??{}).project_questions);
  if(fields.length)return <ProjectQuestions key={String(question?.id)} id={id} session={session} busy={busy} spec={spec} question={question!} onDone={onDone}/>;
  return <form className="editor composer" onSubmit={submit}><fieldset disabled={!session?.models_enabled||action.pending||busy}><label>{launched?'Talk to the project lead':'Message the project lead'}<textarea id={launched?`talk-to-lead-${id}`:undefined} placeholder={launched?'What went wrong, what do you want changed or improved? The lead will discuss it and propose a plan…':'Describe what you want to build, or answer the lead’s questions…'} required rows={3} maxLength={16000} value={draft.message} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void submit();}}} onChange={e=>setDraft({message:e.target.value})}/></label><div className="send-row"><button className="primary" type="submit" disabled={!draft.message.trim()}>{action.pending?'Lead is responding…':'Send project message'}</button><span className="muted">Enter sends · nothing launches until you press Launch</span></div></fieldset><Feedback error={action.error} message={action.message}/></form>;
}

export function ProjectQuestions({id,session,spec,question,onDone,busy=false}:{id:string;session:Session|null;spec:string;question:Row;onDone:(ready:boolean)=>void;busy?:boolean}){
  const actions=record(question.actions??{}),fields=rows(actions.project_questions),choices=record(actions.kickoff_defaults??{});
  const initial:Record<string,string>={};for(const f of fields)initial[str(f.id)]=str(f.default);
  const [answers,setAnswers]=useDraft(`sf-project-answers:${id}:${question.id}`,initial);
  const action=useAction(`project-answers:${id}:${question.id}`);
  async function submit(e:FormEvent){
    e.preventDefault();if(action.pending||busy)return;
    const result=await action.send('project_message',id,{answers,expected_question:question.id,expected_spec:spec});
    if(result){saveLocal(`sf-project-answers:${id}:${question.id}`,'{}');onDone(result.draft_ready===true);}
  }
  return <form className="editor kickoff-form" onSubmit={submit}><h3>Shape the project</h3><p className="muted">Answer what you know. The lead will propose the rest for your review.</p>
    <fieldset disabled={!session?.models_enabled||action.pending||busy}><div className="form-grid">{fields.map(f=>{
      const key=str(f.id),value=answers[key]??'',required=f.required===true;
      const common={required,value,onChange:(e:{target:{value:string}})=>setAnswers(old=>({...old,[key]:e.target.value}))};
      return <label key={key} className={f.kind==='textarea'?'wide':''}>{str(f.label)}{!required&&<span className="optional">Optional</span>}
        {f.kind==='select'?<select {...common}><option value="">{required?'Choose an option':'Let the lead decide'}</option>{(Array.isArray(f.options)?f.options:[]).map((o,i)=><option key={i} value={str(o)}>{str(o)}</option>)}</select>:f.kind==='text'?<input {...common} maxLength={4000} placeholder={str(f.placeholder)}/>:<textarea {...common} rows={3} maxLength={4000} placeholder={str(f.placeholder)}/>}
        {str(f.help)&&<small className="muted">{str(f.help)}</small>}</label>;
    })}</div>
      {(str(choices.repo_owner)||num(choices.budget_usd)>0)&&<p className="muted kickoff-defaults">{str(choices.repo_owner)&&<>New repositories default to <strong>{str(choices.repo_owner)}/project-name</strong>. </>}{num(choices.budget_usd)>0&&<>If you have not chosen an allowance, the proposed default is {money(choices.budget_usd)}. </>}You can change these in the proposal before launch.</p>}
      <button className="primary" type="submit">{action.pending?'Drafting your proposal…':'Draft my project'}</button>
      <span className="muted kickoff-hint">Nothing launches until you approve the proposal.</span>
    </fieldset><Feedback error={action.error} message={action.message}/>
  </form>;
}
