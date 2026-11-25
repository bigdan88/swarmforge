import {useState,type FormEvent} from 'react';
import {money,num,useAction,type Row,type Session} from './api';
import {Feedback} from './ui';

/** A finished project stays open for change requests: each becomes a new milestone the lead builds. */
export function ProjectChanges({id,project,session,onDone}:{id:string;project:Row;session:Session|null;onDone:(r:{message:string})=>void}){
  const [request,setRequest]=useState(''),[extra,setExtra]=useState('0');
  const action=useAction(`project-changes:${id}`);
  const remaining=Math.max(0,num(project.budget_usd)-num(project.spent_usd)),added=Number(extra)||0;
  const valid=request.trim().length>=10&&added>=0&&added<=500&&remaining+added>=0.5;
  async function submit(e:FormEvent){
    e.preventDefault();
    if(!valid)return;
    const r=await action.send('request_project_changes',id,{request,extra_budget:added,spending_authorized:true});
    if(r){setRequest('');setExtra('0');onDone(r);}
  }
  return <section className="project-decision" aria-label="Request changes">
    <div className="decision-heading"><span className="badge">Finished</span><h3>Request changes or improvements</h3></div>
    <p>Describe what you want changed. The project reopens with a new milestone built from your request, using the same lead, history and repository. One change per line becomes one check.</p>
    <form onSubmit={e=>void submit(e)}>
      <label>What should change?<textarea rows={5} required maxLength={16000} value={request} onChange={e=>setRequest(e.target.value)} placeholder={'- Make the cards bigger on desktop\n- Add a dark mode toggle\n- Search should also match partial words'}/></label>
      <div className="form-grid"><label>Add to the budget (USD)<input type="number" min="0" max="500" step="1" value={extra} onChange={e=>setExtra(e.target.value)}/></label></div>
      <p className="muted">{money(remaining)} of the approved {money(project.budget_usd)} is left{added>0?`; this adds ${money(added)} (new cap ${money(num(project.budget_usd)+added)})`:''}. {remaining+added<0.5?'Add budget to start a change round.':'The change round may use up to the remaining amount.'}</p>
      <button className="primary" type="submit" disabled={!session?.models_enabled||action.pending||!valid}>{action.pending?'Reopening…':'Reopen project with these changes'}</button>
    </form>
    <Feedback error={action.error} message={action.message}/>
  </section>;
}
