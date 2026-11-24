import {useEffect} from 'react';
import {label,record,rows,str,num,useGet,type Session} from './api';
import {ActionButton,Feedback} from './ui';
export function WorkControls({itemId,session,onDone,onPendingChange}:{itemId:string;session:Session|null;onDone:()=>void;onPendingChange?:(count:number)=>void}){
  const {data,error,refresh}=useGet(`/api/v1/work/${encodeURIComponent(itemId)}/controls`,record,4000);
  const pending=rows(data?.approvals).length;
  useEffect(()=>{onPendingChange?.(pending);},[pending,onPendingChange]);
  const done=()=>{refresh();onDone();};
  return <section className="controls" aria-label="Task controls"><h3>Task controls</h3><Feedback error={error}/>
    {!session?.actions_enabled&&<p className="muted">Browsing mode. Enable controls when starting the web interface to take actions.</p>}
    <div className="button-row">{data?.can_cancel===true&&<ActionButton operation="cancel_work" target={itemId} disabled={!session?.actions_enabled} onDone={done}>Cancel task</ActionButton>}
      {data?.can_retry===true&&<><p className="muted">Retry uses the original task’s remaining allowance.</p><ActionButton operation="retry_work" target={itemId} args={{spending_authorized:true}} disabled={!session?.models_enabled} onDone={done}>Approve retry within remaining allowance</ActionButton></>}
    </div>{str(data?.project_id)&&<p className="muted">Project retries stay with the project lead to preserve budgets and merge barriers.</p>}
    {rows(data?.approvals).map(approval=><div className="approval-card" key={num(approval.id)}><h4>Approval requested</h4><p>{str(approval.question)}</p>{approval.truncated===true&&<p className="muted">This request is shortened. Review it in full in the existing dashboard before approving.</p>}<div className="button-row"><ActionButton operation="decide_approval" target={String(approval.id)} args={{approved:true}} disabled={!session?.actions_enabled||approval.truncated===true} onDone={done}>Approve this action</ActionButton><ActionButton operation="decide_approval" target={String(approval.id)} args={{approved:false}} disabled={!session?.actions_enabled} onDone={done}>Decline</ActionButton></div></div>)}
  </section>;
}
export function QueueControls({session}:{session:Session|null}){
  const {data,error,refresh}=useGet('/api/v1/queue',record,4000);
  return <section className="queue-controls">{rows(data?.blocked_projects).map(p=><div className="blocker" role="status" key={str(p.id)}><strong>{str(p.name)} needs your decision</strong><p>{str(p.reason)||'This project is paused at a decision or checkpoint.'}</p><a href={`?page=projects&project=${encodeURIComponent(str(p.id))}`}>Open project recovery and decisions</a></div>)}<details><summary>Queue controls {data?.paused===true?'· paused':data?.worker_running===true?'· worker running':'· worker stopped'}</summary><Feedback error={error}/>
    {data?.queued_count===0&&<p className="muted">No tasks are ready in the queue. Tentative project tasks wait for the lead; starting a worker does not approve a project decision.</p>}
    <p className="muted">Starting the queue permits already approved tasks and launched projects to run under their existing allowances.</p>
    <div className="button-row"><ActionButton operation="start_queue" args={{spending_authorized:true}} disabled={!session?.models_enabled} onDone={refresh}>Run approved work</ActionButton><ActionButton operation="pause_queue" disabled={!session?.actions_enabled} onDone={refresh}>Pause new starts</ActionButton></div>
    <div className="lane-list">{rows(data?.lanes).map((lane,i)=><span className="badge" key={i}>{str(lane.name)} · {label(lane.status)} {num(lane.count)}</span>)}</div>
  </details></section>;
}
