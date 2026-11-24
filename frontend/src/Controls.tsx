import {useState} from 'react';
import {label,record,rows,str,num,useGet,type Session} from './api';
import {ActionButton,Feedback} from './ui';
export function WorkControls({itemId,session,onDone}:{itemId:string;session:Session|null;onDone:()=>void}){
  const {data,error,refresh}=useGet(`/api/v1/work/${encodeURIComponent(itemId)}/controls`,record,4000);
  const [authorized,setAuthorized]=useState(false);
  const done=()=>{refresh();onDone();};
  return <section className="controls" aria-label="Task controls"><h3>Task controls</h3><Feedback error={error}/>
    {!session?.actions_enabled&&<p className="muted">Browsing mode. Enable controls when starting the web interface to take actions.</p>}
    <div className="button-row">{data?.can_cancel===true&&<ActionButton operation="cancel_work" target={itemId} disabled={!session?.actions_enabled} onDone={done}>Cancel task</ActionButton>}
      {data?.can_retry===true&&<><label className="check"><input type="checkbox" checked={authorized} onChange={e=>setAuthorized(e.target.checked)}/>Authorize a retry within the original remaining task allowance</label><ActionButton operation="retry_work" target={itemId} args={{spending_authorized:authorized}} disabled={!session?.models_enabled||!authorized} onDone={done}>Queue retry</ActionButton></>}
    </div>{str(data?.project_id)&&<p className="muted">Project retries stay with the project lead to preserve budgets and merge barriers.</p>}
    {rows(data?.approvals).map(approval=><div className="approval-card" key={num(approval.id)}><h4>Approval requested</h4><p>{str(approval.question)}</p>{approval.truncated===true&&<p className="muted">This request is shortened. Review it in full in the existing dashboard before approving.</p>}<div className="button-row"><ActionButton operation="decide_approval" target={String(approval.id)} args={{approved:true}} disabled={!session?.actions_enabled||approval.truncated===true} onDone={done}>Approve this action</ActionButton><ActionButton operation="decide_approval" target={String(approval.id)} args={{approved:false}} disabled={!session?.actions_enabled} onDone={done}>Decline</ActionButton></div></div>)}
  </section>;
}
export function QueueControls({session}:{session:Session|null}){
  const {data,error,refresh}=useGet('/api/v1/queue',record,4000);
  const [authorized,setAuthorized]=useState(false);
  return <section className="queue-controls"><details><summary>Queue controls {data?.paused===true?'· paused':data?.worker_running===true?'· worker running':'· worker stopped'}</summary><Feedback error={error}/>
    <p className="muted">Starting the queue permits already approved tasks and launched projects to run under their existing allowances.</p>
    <label className="check"><input type="checkbox" checked={authorized} onChange={e=>setAuthorized(e.target.checked)}/>I authorize the queued work to run within its existing budgets.</label>
    <div className="button-row"><ActionButton operation="start_queue" args={{spending_authorized:authorized}} disabled={!session?.models_enabled||!authorized} onDone={refresh}>Start / resume queue</ActionButton><ActionButton operation="pause_queue" disabled={!session?.actions_enabled} onDone={refresh}>Pause new starts</ActionButton></div>
    <div className="lane-list">{rows(data?.lanes).map((lane,i)=><span className="badge" key={i}>{str(lane.name)} · {label(lane.status)} {num(lane.count)}</span>)}</div>
  </details></section>;
}
