import {useState,type FormEvent} from 'react';
import {num,record,rows,saveLocal,str,useAction,useDraft,type Row,type Session} from './api';
import {Feedback,Heading,type Navigate} from './ui';

export function NewTask({session,navigate}:{session:Session|null;navigate:Navigate}){
  const [draft,setDraft]=useDraft('sf-new-task',{title:'',body:'',repo:'',budget:String(session?.settings.limits.budget_usd??5),mode:'swarm'});
  const [authorized,setAuthorized]=useState(false);const action=useAction('new-task');
  const field=(key:string,value:string)=>setDraft(old=>({...old,[key]:value}));
  async function submit(e:FormEvent){e.preventDefault();const result=await action.send('queue_task','',{...draft,budget:Number(draft.budget),spending_authorized:authorized});if(result?.item_id){saveLocal('sf-new-task','{}');navigate('work',result.item_id);}}
  return <><Heading title="New task" description="Describe the outcome. The team will plan, build, test and request publication approval."/>
    <form className="panel editor" onSubmit={submit}><fieldset disabled={action.pending||!session?.models_enabled}>
      <label>Title<input required maxLength={300} value={draft.title} onChange={e=>field('title',e.target.value)} placeholder="Add CSV export to reports"/></label>
      <label>Repository<input required value={draft.repo} onChange={e=>field('repo',e.target.value)} placeholder="owner/repository or a local repository path"/></label>
      <label>Requirements and acceptance criteria<textarea required rows={10} maxLength={30000} value={draft.body} onChange={e=>field('body',e.target.value)} placeholder="What should users be able to do? How should the team verify it?"/></label>
      <div className="form-grid"><label>Task allowance (USD)<input required type="number" min="0.5" max="500" step="0.5" value={draft.budget} onChange={e=>field('budget',e.target.value)}/></label><label>Workflow<select value={draft.mode} onChange={e=>field('mode',e.target.value)}><option value="swarm">Swarm</option><option value="reviewed">One developer + test/review</option><option value="single">Single agent</option><option value="auto">Choose by task size</option></select></label></div>
      <p className="muted">Retries share the original task allowance. Publishing still asks for your approval. A queue worker must be running to start queued work.</p>
      <label className="check"><input type="checkbox" checked={authorized} onChange={e=>setAuthorized(e.target.checked)}/>I authorize this task to use its displayed allowance.</label>
      <button className="primary" type="submit" disabled={!authorized||action.pending}>{action.pending?'Adding task…':'Add to queue'}</button>
    </fieldset><Feedback error={action.error} message={action.message}/><p className="muted">Your form is saved in this browser while you navigate.</p>{!session?.models_enabled&&<p className="muted">Configured controls must be enabled to queue model work.</p>}</form></>;
}

const fields=[['name','Project name'],['goal','Goal'],['users','Who it is for'],['repo','GitHub repository (owner/name)'],['out_of_scope','Out of scope'],['stack','Stack'],['hosting','Hosting'],['deadline','Deadline'],['latitude','What the team may decide'],['ask_before','What needs your approval']] as const;
export function ProjectForm({session,initial={},projectId='',expectedSpec='',onSaved,onCancel}:{session:Session|null;initial?:Row;projectId?:string;expectedSpec?:string;onSaved:(id:string)=>void;onCancel?:()=>void}){
  const milestones=rows(initial.milestones);
  const defaults:Record<string,string>={budget_usd:String(initial.budget_usd??30),repository_kind:str(initial.repository_kind)||'existing',count:String(milestones.length||3),base_spec:expectedSpec};
  for(const [key] of fields)defaults[key]=str(initial[key])||(['out_of_scope','stack','hosting','deadline','latitude','ask_before'].includes(key)?'None':'');
  for(let i=0;i<6;i++){const m=milestones[i]??{};defaults[`name${i}`]=str(m.name);defaults[`goal${i}`]=str(m.goal);defaults[`checks${i}`]=Array.isArray(m.checks)?m.checks.join('\n'):'';defaults[`usd${i}`]=String(m.estimate_usd??10);defaults[`hours${i}`]=String(m.estimate_hours??1);}
  const storageKey=`sf-project-form:${projectId||'new'}`;
  const [draft,setDraft]=useDraft(storageKey,defaults);const action=useAction(`project-form:${projectId||'new'}`);
  const field=(key:string,value:string)=>setDraft(old=>({...old,[key]:value}));
  async function submit(e:FormEvent){
    e.preventDefault();const definition:Row={};for(const [key] of fields)definition[key]=draft[key];
    definition.repository_kind=draft.repository_kind;definition.budget_usd=Number(draft.budget_usd);
    definition.milestones=Array.from({length:Number(draft.count)},(_,i)=>({name:draft[`name${i}`],goal:draft[`goal${i}`],checks:draft[`checks${i}`].split('\n').map(s=>s.trim()).filter(Boolean),estimate_usd:Number(draft[`usd${i}`]),estimate_hours:Number(draft[`hours${i}`])}));
    const result=await action.send(projectId?'save_project':'create_project',projectId,{definition,expected_spec:draft.base_spec});
    if(result?.project_id){saveLocal(storageKey,'{}');onSaved(result.project_id);}
  }
  return <form className="panel editor" onSubmit={submit}><h2>{projectId?'Edit project draft':'Plan a project'}</h2><p className="muted">Saving creates a draft. Repository work starts only after a separate approval of the displayed spec.</p>
    <fieldset disabled={action.pending||!session?.actions_enabled}>
      <div className="form-grid">{fields.map(([key,title])=><label key={key}>{title}{['goal','users','out_of_scope','latitude','ask_before'].includes(key)?<textarea required rows={3} maxLength={5000} value={draft[key]} onChange={e=>field(key,e.target.value)}/>:<input required maxLength={1000} value={draft[key]} onChange={e=>field(key,e.target.value)}/>}</label>)}
      <label>Total API allowance (USD)<input type="number" required min="1" max="10000" value={draft.budget_usd} onChange={e=>field('budget_usd',e.target.value)}/></label><label>Repository<select value={draft.repository_kind} onChange={e=>field('repository_kind',e.target.value)}><option value="existing">Existing repository</option><option value="new">New private repository at launch</option></select></label>
      <label>Milestones<select value={draft.count} onChange={e=>field('count',e.target.value)}>{[3,4,5,6].map(n=><option value={n} key={n}>{n}</option>)}</select></label></div>
      {Array.from({length:Number(draft.count)},(_,i)=><section className="milestone-editor" key={i}><h3>Milestone {i+1}</h3><div className="form-grid"><label>Name<input required value={draft[`name${i}`]} onChange={e=>field(`name${i}`,e.target.value)}/></label><label>Goal<input required value={draft[`goal${i}`]} onChange={e=>field(`goal${i}`,e.target.value)}/></label><label>Estimated API cost (USD)<input type="number" required min="0.01" step="0.01" value={draft[`usd${i}`]} onChange={e=>field(`usd${i}`,e.target.value)}/></label><label>Estimated hours<input type="number" required min="0.01" step="0.01" value={draft[`hours${i}`]} onChange={e=>field(`hours${i}`,e.target.value)}/></label></div><label>Done when (2–5 observable checks, one per line)<textarea required rows={4} value={draft[`checks${i}`]} onChange={e=>field(`checks${i}`,e.target.value)}/></label></section>)}
      <div className="button-row"><button type="submit" className="primary">{action.pending?'Saving…':'Save draft'}</button>{onCancel&&<><button type="button" onClick={onCancel}>Close editor</button><button type="button" onClick={()=>setDraft(defaults)}>Replace form with saved spec</button></>}</div>
    </fieldset><Feedback error={action.error} message={action.message}/><p className="muted">Unsaved text stays in this browser. Saving does not launch the project.</p></form>;
}
