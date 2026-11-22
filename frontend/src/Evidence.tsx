import {useEffect, useState} from 'react';
import {appendEvents, parseEvidence, type Evidence as Snapshot, type RunEvent} from './contracts';
import {Artifacts} from './Artifacts';

const money = (value:number) => `$${value.toFixed(2)}`;
const when = (value:number) => new Date(value*1000).toLocaleString();

export function Evidence({itemId,runId}:{itemId:string; runId:string|null}) {
  const [snapshot,setSnapshot] = useState<Snapshot|null>(null);
  const [events,setEvents] = useState<RunEvent[]>([]);
  const [error,setError] = useState('');
  useEffect(()=>{
    const controller = new AbortController();
    let timer:ReturnType<typeof setTimeout>;
    let cursor=0;
    async function refresh() {
      let more=false;
      try {
        const response=await fetch(`/api/v1/work/${encodeURIComponent(itemId)}/evidence?after=${cursor}`,{signal:controller.signal});
        if(!response.ok)throw new Error(response.status===404?'This work item is no longer available.':'Could not refresh recorded evidence.');
        const next=parseEvidence(await response.json());
        if(next.item_id!==itemId || next.next_event_id<cursor)throw new Error('The local service returned evidence for a different task or cursor.');
        if(next.run_id!==runId)throw new Error('This task changed attempts. Waiting for refreshed task details.');
        if(!controller.signal.aborted) {
          setSnapshot(next);setEvents(previous=>appendEvents(previous,next.events));setError('');
          cursor=next.next_event_id;more=next.events_has_more;
        }
      } catch(e) {
        if(!controller.signal.aborted)setError(e instanceof Error?e.message:'Evidence refresh failed');
      } finally {
        if(!controller.signal.aborted)timer=setTimeout(refresh,more?250:4000);
      }
    }
    void refresh();
    return()=>{controller.abort();clearTimeout(timer);};
  },[itemId,runId]);
  const allowance=snapshot?.allowance;
  const cost=snapshot?.costs;
  return <div className="evidence">
    <h3>Recorded evidence</h3>
    {error&&<p role="alert" className="error">{error} {snapshot&&'Showing the last successful snapshot.'}</p>}
    {!snapshot&&!error&&<p role="status">Loading recorded evidence.</p>}
    {snapshot&&!snapshot.run_id&&<p className="muted">This task has not been dispatched. Run evidence will appear here.</p>}
    {snapshot?.run_id&&<>
      <div className="evidence-grid"><section aria-label="Task allowance"><h4>Shared task allowance</h4>
        {allowance?<><dl>
          <div><dt>Attempt</dt><dd>{allowance.attempt}</dd></div>
          <div><dt>Total allowance</dt><dd>{money(allowance.budget_usd)}</dd></div>
          <div><dt>Charged across attempts</dt><dd>{money(allowance.spent_usd)}</dd></div>
          <div><dt>Reserved for pending calls</dt><dd>{money(allowance.reserved_usd)}</dd></div>
          <div><dt>Available for another call</dt><dd>{money(allowance.available_usd)}</dd></div>
          <div><dt>Recorded deadline</dt><dd>{when(allowance.deadline)}</dd></div>
          <div><dt>Last meaningful progress</dt><dd>{when(allowance.last_progress_at)}</dd></div>
        </dl>{[['Current action',allowance.current_action],['Last result',allowance.last_result],['Next action',allowance.next_action],['Stopped because',allowance.stop_reason]].map(([name,value])=>value&&<p key={name}><strong>{name}: </strong>{value}</p>)}</>:<p className="muted">No shared task allowance was recorded for this run.</p>}
      </section><section aria-label="Run costs"><h4>This run's model usage</h4>
        {cost?.calls?<><dl><div><dt>Recorded calls</dt><dd>{cost.calls}</dd></div><div><dt>Provider reported</dt><dd>{money(cost.reported_usd)}</dd></div><div><dt>Estimated</dt><dd>{money(cost.estimated_usd)}</dd></div><div><dt>Calls with unavailable cost</dt><dd>{cost.unavailable_calls}</dd></div></dl><p className="muted">Run usage and task allowance cover different scopes. A charged allowance can include uncertain calls.</p></>:<p className="muted">No model usage has been recorded.</p>}
      </section></div>
      <section aria-label="Acceptance checks"><h4>Acceptance checks</h4>
        {!snapshot.checks.length&&<p className="muted">No acceptance checks recorded.</p>}
        {snapshot.checks.map(check=><details key={check.id}><summary>{check.status||'Unverified'} · {check.criterion_id||`Check ${check.id}`} · cycle {check.cycle??'—'}</summary><p>{check.text||'No criterion text recorded.'}</p><pre>{check.evidence||'No evidence recorded.'}</pre>{check.truncated&&<p className="muted">Long evidence is shortened in this preview.</p>}</details>)}
        {snapshot.checks_has_more&&<p className="muted">Showing the latest 100 checks. Earlier evidence remains in the existing dashboard.</p>}
      </section>
      <section aria-label="Recorded tests"><h4>Test results</h4>
        {!snapshot.tests.length&&<p className="muted">No test results recorded.</p>}
        {snapshot.tests.map(test=><p key={test.id}><strong>{test.label||'Test run'}</strong> · {when(test.ts)} · exit {test.exit_code??'unknown'}{test.timed_out?' · timed out':''}<br/>{test.passed??'—'} passed · {test.failed??'—'} failed · {test.errors??'—'} errors · {test.skipped??'—'} skipped</p>)}
        {snapshot.tests_has_more&&<p className="muted">Showing the latest 25 test results.</p>}
      </section>
      <Artifacts key={snapshot.run_id} itemId={itemId} runId={snapshot.run_id}/>
      <section aria-label="Recent run events"><h4>Recent events</h4><p className="muted">Starts with the latest 50 events and retains up to 200 while open. Earlier history remains in the existing dashboard.</p>
        {!events.length&&<p className="muted">No events recorded.</p>}
        <ol className="events">{[...events].reverse().map(event=><li key={event.id}><small>{when(event.ts)} · {event.level} · {event.kind}</small><p>{event.message||'No message recorded.'}</p>{event.truncated&&<small>Long message shortened.</small>}</li>)}</ol>
      </section>
    </>}
  </div>;
}
