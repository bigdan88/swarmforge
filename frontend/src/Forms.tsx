import {type FormEvent} from 'react';
import {num,record,rows,saveLocal,str,useAction,useDraft,type Row,type Session} from './api';
import {Feedback,Heading,type Navigate} from './ui';

export function NewTask({session,navigate}:{session:Session|null;navigate:Navigate}){
  const limits=session?.settings.limits??{};
  const [draft,setDraft]=useDraft('sf-new-task',{source:'describe',issue_url:'',title:'',body:'',repo:'',budget:String(limits.budget_usd??5),mode:'swarm',team:'',max_cycles:String(limits.max_repair_cycles??''),hours:limits.max_runtime_s?String(Math.round(limits.max_runtime_s/360)/10):'',heal_attempts:''});
  const action=useAction('new-task');
  const field=(key:string,value:string)=>setDraft(old=>({...old,[key]:value}));
  const issue=draft.source==='issue';
  async function submit(e:FormEvent){
    e.preventDefault();
    const optional=(v:string)=>v.trim()===''?null:Number(v);
    const args={title:draft.title,body:draft.body,repo:draft.repo,budget:Number(draft.budget),mode:draft.mode,team:draft.team||null,
      max_cycles:optional(draft.max_cycles),hours:optional(draft.hours),heal_attempts:optional(draft.heal_attempts),
      ...(issue?{issue_url:draft.issue_url}:{}),spending_authorized:true};
    const result=await action.send('queue_task','',args);if(result?.item_id){saveLocal('sf-new-task','{}');navigate('work',result.item_id);}
  }
  const teams=session?.settings.teams??[];
  return <><Heading title="New task" description="Describe the outcome. The team will plan, build, test and request publication approval."/>
    <form className="panel editor" onSubmit={submit}><fieldset disabled={action.pending||!session?.models_enabled}>
      <div className="segmented" role="radiogroup" aria-label="What should the team work on?"><label className="check"><input type="radio" name="source" checked={!issue} onChange={()=>field('source','describe')}/>Describe it here</label><label className="check"><input type="radio" name="source" checked={issue} onChange={()=>field('source','issue')}/>GitHub issue</label></div>
      {issue?<><label>Issue URL<input required type="url" maxLength={500} value={draft.issue_url} onChange={e=>field('issue_url',e.target.value)} placeholder="https://github.com/owner/repo/issues/42"/></label>
        <label>Repository override (optional)<input value={draft.repo} onChange={e=>field('repo',e.target.value)} placeholder="Leave empty to use the issue's repository"/></label></>
      :<><label>Title<input required maxLength={300} value={draft.title} onChange={e=>field('title',e.target.value)} placeholder="Add CSV export to reports"/></label>
        <label>Repository<input required value={draft.repo} onChange={e=>field('repo',e.target.value)} placeholder="owner/repository or a local repository path"/></label>
        <label>Requirements and acceptance criteria<textarea required rows={10} maxLength={30000} value={draft.body} onChange={e=>field('body',e.target.value)} placeholder="What should users be able to do? How should the team verify it?"/></label></>}
      <div className="form-grid"><label>Task allowance (USD)<input required type="number" min="0.5" max="500" step="0.5" value={draft.budget} onChange={e=>field('budget',e.target.value)}/></label><label>Workflow<select value={draft.mode} onChange={e=>field('mode',e.target.value)}><option value="swarm">Swarm (5 roles)</option><option value="reviewed">One developer + test/review</option><option value="single">Single agent (baseline)</option><option value="auto">Auto (pick by task size)</option><option value="ralph">Ralph loop (one agent, fresh passes)</option></select></label>
        <label>Model team<select value={draft.team} onChange={e=>field('team',e.target.value)}><option value="">{session?.settings.team?`Default (${session.settings.team})`:'Default (configuration)'}</option>{teams.map(t=><option key={t.name} value={t.name}>{t.name}</option>)}</select></label></div>
      <details className="advanced"><summary>Advanced: repair cycles, time limit and auto-fix</summary><div className="form-grid">
        <label>Max repair cycles<input type="number" min="0" max="10" step="1" value={draft.max_cycles} onChange={e=>field('max_cycles',e.target.value)} placeholder="Configured default"/></label>
        <label>Time limit (hours)<input type="number" min="0.05" max="72" step="0.05" value={draft.hours} onChange={e=>field('hours',e.target.value)} placeholder="Configured default"/></label>
        <label>Auto-fix attempts<input type="number" min="0" max="5" step="1" value={draft.heal_attempts} onChange={e=>field('heal_attempts',e.target.value)} placeholder="Configured default"/></label></div>
        <p className="muted">The whole run stops at the time limit. Auto-fix retries a failed run with its diagnosis, within the task allowance. Test command and sandbox image overrides start a run directly and remain in the previous dashboard.</p></details>
      <p className="muted">Retries share the original task allowance. Publishing still asks for your approval. A queue worker must be running to start queued work.</p>
      <p className="muted">Adding this task approves its displayed allowance.</p>
      <button className="primary" type="submit" disabled={action.pending}>{action.pending?'Adding task…':'Approve allowance & add to queue'}</button>
    </fieldset><Feedback error={action.error} message={action.message}/><p className="muted">Your form is saved in this browser while you navigate.</p>{!session?.models_enabled&&<p className="muted">Configured controls must be enabled to queue model work.</p>}</form></>;
}

const fields=[['name','Project name'],['goal','Goal'],['users','Who it is for'],['repo','GitHub repository (owner/name)'],['out_of_scope','Out of scope'],['stack','Stack'],['hosting','Hosting'],['deadline','Deadline'],['latitude','What the team may decide'],['ask_before','What needs your approval']] as const;
export function ProjectForm({session,initial={},projectId='',expectedSpec='',amending=false,completedMilestones=0,onSaved,onCancel}:{session:Session|null;initial?:Row;projectId?:string;expectedSpec?:string;amending?:boolean;completedMilestones?:number;onSaved:(id:string)=>void;onCancel?:()=>void}){
  const milestones=rows(initial.milestones);
  const defaults:Record<string,string>={budget_usd:String(initial.budget_usd??30),repository_kind:str(initial.repository_kind)||'existing',count:String(milestones.length||3),base_spec:expectedSpec,developers:String(record(initial.execution??{}).developers??2),validation:str(record(initial.execution??{}).validation)||'independent'};
  for(const [key] of fields)defaults[key]=str(initial[key])||(['out_of_scope','stack','hosting','deadline','latitude','ask_before'].includes(key)?'None':'');
  for(let i=0;i<6;i++){const m=milestones[i]??{};defaults[`name${i}`]=str(m.name);defaults[`goal${i}`]=str(m.goal);defaults[`checks${i}`]=Array.isArray(m.checks)?m.checks.join('\n'):'';defaults[`usd${i}`]=String(m.estimate_usd??10);defaults[`hours${i}`]=String(m.estimate_hours??1);}
  const storageKey=`sf-project-form:${amending?'amend:':''}${projectId||'new'}`;
  const [draft,setDraft]=useDraft(storageKey,defaults);const action=useAction(`project-form:${projectId||'new'}`);
  const field=(key:string,value:string)=>setDraft(old=>({...old,[key]:value}));
  async function submit(e:FormEvent){
    e.preventDefault();const definition:Row={};for(const [key] of fields)definition[key]=draft[key];
    definition.repository_kind=draft.repository_kind;definition.budget_usd=Number(draft.budget_usd);
    definition.execution={developers:Number(draft.developers??defaults.developers),validation:draft.validation??defaults.validation};
    definition.milestones=Array.from({length:Number(draft.count)},(_,i)=>({...milestones[i],name:draft[`name${i}`],goal:draft[`goal${i}`],checks:draft[`checks${i}`].split('\n').map(s=>s.trim()).filter(Boolean),estimate_usd:Number(draft[`usd${i}`]),estimate_hours:Number(draft[`hours${i}`])}));
    const result=await action.send(amending?'amend_project':projectId?'save_project':'create_project',projectId,{definition,expected_spec:draft.base_spec,spending_authorized:amending});
    if(result?.project_id){saveLocal(storageKey,'{}');onSaved(result.project_id);}
  }
  return <form className="panel editor" onSubmit={submit}><h2>{amending?'Revise the approved specification':projectId?'Edit project draft':'Plan a project'}</h2><p className="muted">{amending?'Review the scope and allowance before approving this revision. Completed milestones and the repository are preserved; the project stays paused.':'Saving creates a draft. Repository work starts only after a separate approval of the displayed spec.'}</p>
    <fieldset disabled={action.pending||!session?.actions_enabled}>
      <div className="form-grid">{fields.map(([key,title])=><label key={key}>{title}{['goal','users','out_of_scope','latitude','ask_before'].includes(key)?<textarea required rows={3} maxLength={5000} value={draft[key]} onChange={e=>field(key,e.target.value)}/>:<input required maxLength={1000} readOnly={amending&&key==='repo'} value={draft[key]} onChange={e=>field(key,e.target.value)}/>}</label>)}
      <label>Total API allowance (USD)<input type="number" required min="1" max="10000" value={draft.budget_usd} onChange={e=>field('budget_usd',e.target.value)}/></label><label>Repository<select disabled={amending} value={draft.repository_kind} onChange={e=>field('repository_kind',e.target.value)}><option value="existing">Existing repository</option><option value="new">New private repository at launch</option></select></label>
      <label>Developers<select value={draft.developers??defaults.developers} onChange={e=>field('developers',e.target.value)}><option value="1">One developer</option><option value="2">Up to two developers</option></select></label>
      <label>Validation<select value={draft.validation??defaults.validation} onChange={e=>field('validation',e.target.value)}><option value="self">Developer self tests</option><option value="independent">Independent tester and reviewer</option></select></label>
      <label>Milestones<select disabled={amending} value={draft.count} onChange={e=>field('count',e.target.value)}>{[3,4,5,6].map(n=><option value={n} key={n}>{n}</option>)}</select></label></div>
      <p className="muted">Self tests skip the separate tester and reviewer. The orchestrator still executes tests and approved application checks. Budget and delivery rules still apply.</p>
      {Array.from({length:Number(draft.count)},(_,i)=><fieldset className="milestone-editor" key={i} disabled={amending&&i<completedMilestones}><h3>Milestone {i+1}</h3><div className="form-grid"><label>Name<input required value={draft[`name${i}`]} onChange={e=>field(`name${i}`,e.target.value)}/></label><label>Goal<input required value={draft[`goal${i}`]} onChange={e=>field(`goal${i}`,e.target.value)}/></label><label>Estimated API cost (USD)<input type="number" required min="0.01" step="0.01" value={draft[`usd${i}`]} onChange={e=>field(`usd${i}`,e.target.value)}/></label><label>Estimated hours<input type="number" required min="0.01" step="0.01" value={draft[`hours${i}`]} onChange={e=>field(`hours${i}`,e.target.value)}/></label></div><label>Done when (2–5 observable checks, one per line)<textarea required rows={4} value={draft[`checks${i}`]} onChange={e=>field(`checks${i}`,e.target.value)}/></label></fieldset>)}
<div className="button-row"><button type="submit" className="primary">{action.pending?'Saving…':amending?'Approve revised specification':'Save draft'}</button>{onCancel&&<><button type="button" onClick={onCancel}>Close editor</button><button type="button" onClick={()=>{setDraft(defaults);}}>Replace form with saved spec</button></>}</div>
    </fieldset><Feedback error={action.error} message={action.message}/><p className="muted">{amending?'Unsaved text stays in this browser. Approval does not resume work; continue after reviewing the saved revision.':'Unsaved text stays in this browser. Saving does not launch the project.'}</p></form>;
}
