import {useState} from 'react';
import {money,num,rows,stamp,str,type Row,type Session} from './api';
import {ActionButton} from './ui';

export function ProjectRecovery({id,project,recovery,spec,session,onDone,onAmend}:{id:string;project:Row;recovery:Row;spec:string;session:Session|null;onDone:(r:{message:string})=>void;onAmend:()=>void}){
  const [correction,setCorrection]=useState('');
  const rejected=rows(recovery.rejections),retry=recovery.can_retry===true;
  return <section className="project-recovery" aria-label="Lead recovery"><div className="detail-heading"><h3>{retry?'Automatic planning needs another attempt':'Recovery history'}</h3>{retry&&num(recovery.attempts)>0&&<span className="muted">{num(recovery.attempts)} attempts in the current planning round</span>}</div>
    {!retry&&<p className="muted">Earlier issues are kept for reference. These entries do not mean the project is currently blocked; its current status is shown above.</p>}
    {retry&&<><p>The lead could not produce a valid next action. Retry repairs the response and can continue an existing accepted task. It keeps the approved scope and remaining {money(Math.max(0,num(project.budget_usd)-num(project.spent_usd)))} allowance.</p><details><summary>Optional clarification for the next attempt</summary><label>Correction or missing context<textarea aria-label="Optional clarification" rows={3} maxLength={16000} value={correction} onChange={e=>setCorrection(e.target.value)} placeholder="Leave empty to let the lead repair and continue on its own."/></label></details>
      <div className="button-row"><ActionButton operation="retry_project_lead" target={id} args={{expected_spec:spec,expected_decision:recovery.latest_decision_id,correction,spending_authorized:true}} disabled={!session?.models_enabled} onDone={onDone}>Retry planning automatically</ActionButton>{recovery.can_amend===true&&<button onClick={onAmend} disabled={!session?.actions_enabled}>Edit approved scope</button>}</div></>}
    {!!rejected.length&&<details className="rejection-history" open={retry}><summary>Planning issues ({rejected.length})</summary>{rejected.map(r=><article key={num(r.id)}>{r.resolved===true&&<span className="badge done">Corrected automatically</span>}<strong>{str(r.explanation)}</strong><p className="muted">SwarmForge's plan validator · Decision #{num(r.id)} · {stamp(r.ts)}{r.resolved===true?` · corrected by decision #${num(r.resolved_by)}`:''}</p><details><summary>Technical reason and the lead's proposed approach</summary><p>{str(r.error)}</p><p className="muted">{str(r.reason)}</p></details></article>)}</details>}
  </section>;
}
