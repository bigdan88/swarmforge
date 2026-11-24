import {useEffect,useState} from 'react';
import {appendEvents,parseEvidence,type Evidence as Snapshot,type RunEvent} from './contracts';
import {checkPassed,currentChecks} from './WorkProgress';

const money=(value:number)=>`$${value.toFixed(2)}`;
const when=(value:number)=>new Date(value*1000).toLocaleString();

export function useEvidence(itemId:string,runId:string|null){
  const [snapshot,setSnapshot]=useState<Snapshot|null>(null),[events,setEvents]=useState<RunEvent[]>([]),[error,setError]=useState('');
  const [updated,setUpdated]=useState<number|null>(null);
  useEffect(()=>{
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;let cursor=0;
    setSnapshot(null);setEvents([]);setError('');setUpdated(null);
    async function refresh(){
      let more=false;
      try{
        const response=await fetch(`/api/v1/work/${encodeURIComponent(itemId)}/evidence?after=${cursor}`,{signal:controller.signal});
        if(!response.ok)throw new Error(response.status===404?'This work item is no longer available.':'Could not refresh task progress.');
        const next=parseEvidence(await response.json());
        if(next.item_id!==itemId||next.next_event_id<cursor)throw new Error('The local service returned evidence for a different task or cursor.');
        if(next.run_id!==runId)throw new Error('This task changed attempts. Waiting for refreshed task details.');
        if(!controller.signal.aborted){setSnapshot(next);setEvents(previous=>appendEvents(previous,next.events));setError('');setUpdated(Date.now());cursor=next.next_event_id;more=next.events_has_more;}
      }catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:'Progress refresh failed');}
      finally{if(!controller.signal.aborted)timer=setTimeout(refresh,more?250:4000);}
    }
    void refresh();return()=>{controller.abort();clearTimeout(timer);};
  },[itemId,runId]);
  return {snapshot,events,error,updated};
}

function Check({check}:{check:Snapshot['checks'][number]}){
  return <details className={`check-card ${checkPassed(check.status)?'passed':check.status==='failed'?'failed':''}`}><summary><span className={`result-tag ${checkPassed(check.status)?'good':check.status==='failed'?'bad':''}`}>{checkPassed(check.status)?'Passed':check.status||'Unverified'}</span><span>{check.text||check.criterion_id||`Check ${check.id}`}</span></summary><div><p className="muted">{check.criterion_id||`Check ${check.id}`} · Cycle {check.cycle??0}</p><pre>{check.evidence||'No evidence recorded.'}</pre>{check.truncated&&<p className="muted">Long evidence is shortened in this preview.</p>}</div></details>;
}
export function ChecksPanel({snapshot}:{snapshot:Snapshot|null}){
  const current=currentChecks(snapshot);
  return <div className="work-checks"><section aria-label="Acceptance checks"><div className="work-section-heading"><h3>Acceptance checks</h3><span className="muted">Cycle {current.cycle}</span></div><p className="muted">The latest recorded result for each criterion in this cycle. Expand a check to read its evidence.</p>
    {!current.checks.length&&<p className="work-empty">No acceptance checks recorded yet.</p>}
    {current.checks.map(check=><Check key={check.id} check={check}/>)}
    {!!current.older.length&&<details className="work-disclosure"><summary>Earlier check results ({current.older.length})</summary>{current.older.map(check=><Check key={check.id} check={check}/>)}</details>}
    {snapshot?.checks_has_more&&<p className="muted">Showing the latest 100 checks. Earlier evidence remains retained.</p>}
  </section><section aria-label="Recorded tests"><h3>Test runs</h3>{!snapshot?.tests.length&&<p className="work-empty">No test results recorded yet.</p>}
    {snapshot?.tests.map(test=><div className="test-card" key={test.id}><div><strong>{test.label||'Test run'}</strong><small>{when(test.ts)} · Cycle {test.cycle??0}</small></div><span className={`result-tag ${test.exit_code===0&&!test.timed_out?'good':'bad'}`}>{test.timed_out?'Timed out':test.exit_code===null?'Unknown result':test.exit_code===0?'Passed':'Failed'}</span><div className="test-counts"><span>{test.passed??'—'} passed</span><span>{test.failed??'—'} failed</span><span>{test.errors??'—'} errors</span><span>{test.skipped??'—'} skipped</span></div></div>)}
    {snapshot?.tests_has_more&&<p className="muted">Showing the latest 25 test runs.</p>}</section></div>;
}

export function CostsPanel({snapshot}:{snapshot:Snapshot|null}){
  const allowance=snapshot?.allowance,cost=snapshot?.costs;
  return <div className="work-costs"><section><h4>Shared task allowance</h4>{allowance?<><dl>
    <div><dt>Attempt</dt><dd>{allowance.attempt}</dd></div><div><dt>Total allowance</dt><dd>{money(allowance.budget_usd)}</dd></div>
    <div><dt>Charged across attempts</dt><dd>{money(allowance.spent_usd)}</dd></div><div><dt>Reserved for pending calls</dt><dd>{money(allowance.reserved_usd)}</dd></div>
    <div><dt>Available for another call</dt><dd>{money(allowance.available_usd)}</dd></div><div><dt>Recorded deadline</dt><dd>{when(allowance.deadline)}</dd></div>
    <div><dt>Last meaningful progress</dt><dd>{when(allowance.last_progress_at)}</dd></div></dl>{allowance.stop_reason&&<p><strong>Stopped because: </strong>{allowance.stop_reason}</p>}</>:<p className="muted">No shared task allowance was recorded for this run.</p>}</section>
    <section><h4>This run's model usage</h4>{cost?.calls?<><dl><div><dt>Recorded calls</dt><dd>{cost.calls}</dd></div><div><dt>Provider reported</dt><dd>{money(cost.reported_usd)}</dd></div><div><dt>Estimated</dt><dd>{money(cost.estimated_usd)}</dd></div><div><dt>Calls with unavailable cost</dt><dd>{cost.unavailable_calls}</dd></div></dl><p className="muted">Run usage and task allowance cover different scopes. A charged allowance can include uncertain calls.</p></>:<p className="muted">No model usage has been recorded.</p>}</section></div>;
}

export function ActivityPanel({events}:{events:RunEvent[]}){
  const [filter,setFilter]=useState('all'),[search,setSearch]=useState(''),[limit,setLimit]=useState(25);
  const filtered=[...events].reverse().filter(e=>(filter==='all'||(filter==='attention'?['warn','warning','error'].includes(e.level):e.kind==='state'))&&`${e.message} ${e.kind}`.toLowerCase().includes(search.toLowerCase()));
  return <section aria-label="Recent run events"><div className="work-section-heading"><h3>Activity</h3><span className="muted">Newest first</span></div><div className="activity-filters"><label><span className="sr-only">Search activity</span><input placeholder="Find an event…" value={search} onChange={e=>{setSearch(e.target.value);setLimit(25);}}/></label><label><span className="sr-only">Filter activity</span><select value={filter} onChange={e=>{setFilter(e.target.value);setLimit(25);}}><option value="all">All activity</option><option value="attention">Warnings and errors</option><option value="state">Stage changes</option></select></label></div>
    {!filtered.length&&<p className="work-empty">{events.length?'No events match this filter.':'No events recorded yet.'}</p>}
    <ol className="work-events">{filtered.slice(0,limit).map(event=><li key={event.id}><div><time>{new Date(event.ts*1000).toLocaleTimeString()}</time><span className={`result-tag ${event.level==='error'?'bad':''}`}>{event.kind}</span></div><details><summary>{event.message?.split('\n')[0]||'No message recorded.'}</summary><p>{event.message}</p><small>{when(event.ts)} · {event.level}{event.truncated?' · Long message shortened.':''}</small></details></li>)}</ol>
    {filtered.length>limit&&<button onClick={()=>setLimit(n=>n+25)}>Show 25 more events</button>}<p className="muted">Latest 50 events on open; up to 200 retained here while you watch. Earlier history stays retained.</p></section>;
}
