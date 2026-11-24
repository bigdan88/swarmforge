import {useState} from 'react';
import {money,num,record,rows,str,useAction,type Row,type Session} from './api';
import {Feedback,type Navigate} from './ui';

export function ProjectDecision({id,recovery,spec,project,session,nextMilestone,onDone,onAmend,navigate}:{id:string;recovery:Row;spec:string;project:Row;session:Session|null;nextMilestone:string;onDone:(r:{message:string})=>void;onAmend:()=>void;navigate:Navigate}){
  const [reply,setReply]=useState(''),[extra,setExtra]=useState(()=>String(Math.max(1,num(record(record(recovery.decision??{}).allowance??{}).budget_usd)))),[hours,setHours]=useState('2');
  const action=useAction(`project-decision:${id}`),decision=record(recovery.decision??{}),kind=str(decision.kind);
  const checkpoint=kind==='checkpoint',extend=recovery.can_extend_task===true,task=record(decision.task??{}),allowance=record(decision.allowance??{});
  const checks=rows(decision.checks),unverified=checks.filter(c=>c.status!=='passed').length;
  const remaining=Math.max(0,num(project.budget_usd)-num(project.spent_usd));
  const disabled=!session?.models_enabled||action.pending||!str(recovery.decision_token);
  async function send(operation:string,args:Row){
    const result=await action.send(operation,id,{expected_spec:spec,expected_recovery:recovery.decision_token,spending_authorized:true,...args});
    if(result){setReply('');onDone(result);}
  }
  const verify='Verify and repair this milestone automatically within the approved scope and remaining allowance. Inspect merged code and existing tests first. Link automated evidence to the exact approved milestone check text. Keep unavailable physical or browser observations separate from build acceptance tests. Ask me only for the concrete fact or physical check you cannot perform after exhausting in-scope alternatives. Do not accept the milestone on my behalf.';
  if(!str(recovery.decision_token))return <section className="project-decision"><h3>Restart the dashboard to load decision details</h3><p>The page has the updated controls, but the Python server is still serving the earlier decision data. Restart it on port 8602, then reload this page to review the actual stop and recovery choices.</p></section>;
  return <section className="project-decision" aria-label="Project decision">
    <div className="decision-heading"><span className="badge">{checkpoint?'Milestone review':extend?'Recovery approval':'Project paused'}</span><h3>{str(decision.title)||'Review the current stop'}</h3></div>
    <p>{str(decision.summary)}</p>
    {str(task.title)&&<p><strong>Stopped task:</strong> {str(task.title)} <button className="link-button" onClick={()=>navigate('work',`task-${task.id}`)}>Inspect task evidence</button></p>}
    {str(decision.lead_summary)&&<details><summary>What the lead completed and why it stopped</summary><p className="decision-prose">{str(decision.lead_summary)}</p></details>}
    {num(decision.passed_checks)>0&&<p className="success">{num(decision.passed_checks)} acceptance checks passed on the last tested attempt.</p>}
    {!!checks.length&&<div className="decision-checks"><h4>{checkpoint?'Checks you are being asked to accept':'What still needs attention'}</h4>{checks.map((c,i)=><article key={i}><span className={`badge ${c.status==='passed'?'passed':''}`}>{c.status==='passed'?'Verified':c.status==='failed'||c.status==='unsatisfied'?'Failed':'Unverified'}</span><p>{str(c.text)}</p><small className="muted">{str(c.explanation)}</small>{rows(c.evidence).length>0&&<details><summary>Recorded evidence</summary>{rows(c.evidence).map((e,j)=><p key={j}>{str(e.evidence)}</p>)}</details>}</article>)}</div>}
    {extend&&<div className="decision-option"><h4>Let the lead revise the plan and try again</h4><p>{str(decision.impact)}</p>
      <p>Task spent <strong>{money(allowance.spent_usd)}</strong> of <strong>{money(allowance.budget_usd)}</strong>. The project has <strong>{money(remaining)}</strong> left in its approved {money(project.budget_usd)} cap.</p>
      <div className="form-grid"><label>Add to this task (USD)<input type="number" min="0.5" max={Math.min(500,remaining)} step="0.5" value={extra} onChange={e=>setExtra(e.target.value)}/></label><label>Recovery window (hours)<input type="number" min="0.05" max="72" step="0.05" value={hours} onChange={e=>setHours(e.target.value)}/></label></div>
      <p className="muted">This approves up to {money(Number(extra))} more for the same task and a {hours || '—'} hour recovery window. Existing costs and evidence stay recorded. The task cap becomes {money(num(allowance.budget_usd)+Number(extra))}. The project cap stays {money(project.budget_usd)}; milestone limits still apply.</p>
      <button className="primary" disabled={disabled||Number(extra)<.5||Number(extra)>remaining||!Number.isFinite(Number(extra))||Number(hours)<.05||Number(hours)>72} onClick={()=>void send('extend_project_task',{extra_budget:Number(extra),hours:Number(hours)})}>{action.pending?'Requesting recovery…':`Approve ${money(Number(extra))} more & repair automatically`}</button>
    </div>}
    {checkpoint&&<div className="decision-option"><h4>What happens next</h4><p>Automatic verification keeps this milestone open and asks the lead to find evidence or repair gaps, using the remaining {money(remaining)} project allowance.</p>
      <div className="button-row"><button className="primary" disabled={disabled} onClick={()=>void send('resume_project',{reply:verify})}>{action.pending?'Working…':'Verify & fix automatically'}</button></div>
      <p className="muted">Accepting confirms {unverified?`you personally verified the ${unverified} unverified check${unverified===1?'':'s'} above and `:''}this milestone is complete. It {nextMilestone?`moves to “${nextMilestone}” and permits work within the remaining ${money(remaining)} allowance`:'marks the project complete'}.</p>
      <button disabled={disabled} onClick={()=>void send('resume_project',{reply:'continue',confirm_manual:unverified>0})}>{unverified?'I verified these checks — accept milestone':'Accept milestone & continue'}</button>
    </div>}
    {!extend&&<form onSubmit={e=>{e.preventDefault();if(reply.trim())void send('resume_project',{reply});}}><label>{checkpoint?'Request a specific correction':kind==='question'?'Your answer':'Correction or instructions for the lead'}<textarea rows={3} required maxLength={16000} value={reply} onChange={e=>setReply(e.target.value)} placeholder={checkpoint?'Describe what needs to change. The milestone will stay open.':'Explain the correction or answer the question above.'}/></label><p className="muted">Sending lets the lead act on your reply within the approved scope and remaining {money(remaining)} allowance.</p><button disabled={disabled||!reply.trim()} type="submit">Send correction & continue</button>{kind==='paused'&&<button type="button" disabled={disabled} onClick={()=>void send('resume_project',{reply:'continue'})}>Resume approved work</button>}</form>}
    {recovery.can_amend===true&&<button className="link-button" disabled={!session?.actions_enabled||action.pending} onClick={onAmend}>Edit approved scope or budget</button>}
    <Feedback error={action.error}/>
  </section>;
}
