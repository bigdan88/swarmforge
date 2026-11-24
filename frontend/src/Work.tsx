import { WorkControls, QueueControls } from './Controls';
import type { Session } from './api';
import { ActionButton, type Navigate } from './ui';
import { useEffect, useRef, useState } from 'react';
import { Evidence } from './Evidence';
import { parsePage, parseWork, type Page, type Work } from './contracts';

const label = (s:string) => s.replaceAll('_',' ').toLowerCase();
const stamp = (t:number|null) => t !== null ? new Date(t*1000).toLocaleString() : 'Not recorded';
export function WorkPage({session,navigate,view='work'}:{session:Session|null;navigate:Navigate;view?:string}){
  const initial = new URLSearchParams(location.search);
  const [search,setSearch]=useState(initial.get('search')||'');
  const [status,setStatus]=useState(initial.get('status')||'');
  const [offset,setOffset]=useState(Math.max(0,Math.min(100000,Number(initial.get('offset'))||0)));
  const [selected,setSelected]=useState<string|null>(initial.get('item'));
  const [everyAttempt,setEveryAttempt]=useState(initial.get('attempts')==='all');
  const [showArchived,setShowArchived]=useState(initial.get('archived')==='1');
  const [page,setPage]=useState<Page|null>(null);
  const [detail,setDetail]=useState<Work|null>(null);
  const [error,setError]=useState('');
  const [detailError,setDetailError]=useState('');
  const [updated,setUpdated]=useState<Date|null>(null);
  useEffect(()=>{
    const params=new URLSearchParams({page:view,search,status,offset:String(offset)});
    if(selected)params.set('item',selected);
    if(everyAttempt)params.set('attempts','all');
    if(showArchived)params.set('archived','1');
    history.replaceState(null,'',`?${params}`);
  },[search,status,offset,selected,everyAttempt,showArchived]);
  useEffect(()=>{
    const controller=new AbortController(); let timer:ReturnType<typeof setTimeout>;
    setError('');
    async function refresh(){
      try{
        const query=new URLSearchParams({search,offset:String(offset),limit:'25'});
        if(status)query.set('status',status);if(view!=='work')query.set('view',view);
        if(!everyAttempt)query.set('fold','true');if(showArchived)query.set('archived','true');
        const r=await fetch(`/api/v1/work?${query}`,{signal:controller.signal});
        if(!r.ok)throw new Error('Could not refresh work history. Check that the local service is available.');
        const data=parsePage(await r.json());
        if(!controller.signal.aborted){setPage(data);setError('');setUpdated(new Date());}
      }catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:'Refresh failed');}
      finally{if(!controller.signal.aborted)timer=setTimeout(refresh,4000);}
    }
    timer=setTimeout(refresh,200);
    return()=>{controller.abort();clearTimeout(timer);};
  },[search,status,offset,view,everyAttempt,showArchived]);
  useEffect(()=>{
    const controller=new AbortController();setDetail(previous=>previous?.id===selected?previous:null);setDetailError('');
    if(selected)fetch(`/api/v1/work/${encodeURIComponent(selected)}`,{signal:controller.signal})
      .then(async r=>{if(!r.ok)throw new Error(r.status===404?'This work item is no longer available.':'Could not load task details.');return parseWork(await r.json());})
      .then(data=>{if(!controller.signal.aborted)setDetail(data);})
      .catch(e=>{if(!controller.signal.aborted)setDetailError(e.message);});
    return()=>controller.abort();
  },[selected,updated]);
  const pr=detail?.pr_url && /^https:\/\/github\.com\//i.test(detail.pr_url) ? detail.pr_url:null;
  const openRow=useRef<HTMLDivElement>(null);
  // Escape closes details from anywhere on the page, unless the user is typing in a field.
  useEffect(()=>{const close=(e:KeyboardEvent)=>{const t=e.target as HTMLElement|null;if(e.key==='Escape'&&selected&&!(t&&['INPUT','TEXTAREA','SELECT'].includes(t.tagName)))setSelected(null);};window.addEventListener('keydown',close);return()=>window.removeEventListener('keydown',close);},[selected]);
  // Bring an opened row (including a deep link) into view once its details load.
  useEffect(()=>{if(detail&&openRow.current)openRow.current.scrollIntoView({block:'nearest',behavior:'smooth'});},[detail?.id]);
  const inPage=!!selected&&!!page?.items.some(w=>w.id===selected);
  const details=selected&&<section className="details" aria-label="Task details"><div className="detail-heading"><h2>Task details</h2><button onClick={()=>setSelected(null)}>Close details</button></div>{detailError?<p role="alert" className="error">{detailError}</p>:!detail?<p>Loading details…</p>:<><h3>{detail.title}</h3><p>{detail.reason||'No status explanation has been recorded.'}</p><dl><div><dt>Status</dt><dd>{label(detail.status)}</dd></div><div><dt>Stage</dt><dd>{detail.stage?label(detail.stage):'Not started'}</dd></div><div><dt>Created</dt><dd>{stamp(detail.created_at)}</dd></div><div><dt>Last updated</dt><dd>{stamp(detail.updated_at)}</dd></div><div><dt>Finished</dt><dd>{detail.finished_at?stamp(detail.finished_at):'Not finished'}</dd></div><div><dt>Run</dt><dd>{detail.run_id||'Not dispatched'}</dd></div></dl>{pr&&<a href={pr} target="_blank" rel="noreferrer">View pull request ↗</a>}<WorkControls key={selected} itemId={selected} session={session} onDone={()=>setUpdated(new Date())}/>{detail.run_id&&!['running','queued','tentative'].includes(detail.status)&&<div className="button-row"><ActionButton operation="set_archived" target={selected} args={{archived:!detail.archived}} disabled={!session?.actions_enabled} onDone={()=>{if(!detail.archived&&!showArchived)setSelected(null);setUpdated(new Date());}}>{detail.archived?'Unarchive':'Archive'}</ActionButton><span className="muted">{detail.archived?'Archived runs are hidden from Work; their history, report and transcripts stay.':'Hide this run from Work. Its history, report and transcripts stay.'}</span></div>}<Evidence key={selected+':'+detail.run_id} itemId={selected} runId={detail.run_id}/></>}</section>;
  return <><header><div><div className="eyebrow">YOUR SWARM, AT A GLANCE</div><h1>{view==='prs'?'Pull requests':view==='failures'?'Failures':view==='history'?'History':'Work'}</h1><p>Follow what’s happening. Understand what comes next.</p></div><button className="primary" onClick={()=>navigate('new-task')}>New task</button></header>
      <QueueControls session={session}/><section className="stats" aria-label="Task summary">{[['running','In progress'],['queued','Ready to start'],['failed','Needs attention'],['done','Completed']].map(([key,name])=><div className="stat" key={key}><span>{name}</span><strong>{page?.counts[key]??'—'}</strong></div>)}</section>
      <section className="work-panel"><div className="toolbar"><h2>Task activity</h2><div className="filters"><label><span className="sr-only">Search work</span><input placeholder="Search tasks or repositories…" value={search} maxLength={200} onChange={e=>{setSearch(e.target.value);setOffset(0);}}/></label><label><span className="sr-only">Filter status</span><select value={status} onChange={e=>{setStatus(e.target.value);setOffset(0);}}><option value="">All statuses</option>{['running','queued','tentative','done','failed','cancelled','skipped'].map(s=><option key={s} value={s}>{label(s)}</option>)}</select></label><label className="check"><input type="checkbox" checked={everyAttempt} onChange={e=>{setEveryAttempt(e.target.checked);setOffset(0);}}/>Every attempt</label><label className="check"><input type="checkbox" checked={showArchived} onChange={e=>{setShowArchived(e.target.checked);setOffset(0);}}/>Show archived</label></div></div>
      {error&&<p role="alert" className="error">{error} {page&&'Showing the last successful snapshot.'}</p>}
      {!page&&!error&&<p className="empty" role="status">Loading work…</p>}
      {page?.items.length===0&&<div className="empty"><h3>{search||status?'No matching tasks':'Your next idea starts here'}</h3><p>{search||status?'Try a different search or status.':'Create work in the existing dashboard. It will appear here automatically.'}</p></div>}
      <div className="tasks">{page?.items.map(w=><div key={w.id} className={selected===w.id?'task-open':''} ref={selected===w.id?openRow:undefined}><button className={`task ${selected===w.id?'selected':''}`} onClick={()=>setSelected(selected===w.id?null:w.id)} aria-expanded={selected===w.id} aria-controls={selected===w.id?'task-details':undefined}><span className={`dot ${w.status}`} aria-hidden="true"/><span className="task-copy"><strong>{w.title}{(w.attempts??1)>1&&<span className="chip attempts" title="Repeated attempts of this task are folded into this row. Tick Every attempt to list them.">×{w.attempts} attempts</span>}{w.archived&&<span className="chip">Archived</span>}</strong><small>{w.repo||'No repository'} · {w.id}</small></span><span className={`badge ${w.status}`}>{label(w.status)}</span><span className="stage">{w.stage?label(w.stage):'Not started'}</span><span className="chevron" aria-hidden="true">{selected===w.id?'▾':'▸'}</span></button>{selected===w.id&&<div id="task-details" className="inline-details">{details}</div>}</div>)}</div>
      <footer><span>{page?`${page.total} matching ${page.total===1?'task':'tasks'}${page.attempts_total&&page.attempts_total>page.total?` (${page.attempts_total} attempts; retries of one task share a row)`:''}`:'Work history'} · {updated?`Updated ${updated.toLocaleTimeString()}`:'Connecting'}</span><div className="pager"><button disabled={!page||offset===0} onClick={()=>setOffset(Math.max(0,offset-25))}>Previous</button><button disabled={!page||offset+25>=page.total||offset+25>100000} onClick={()=>setOffset(offset+25)}>Next</button></div></footer></section>
      {selected&&!inPage&&page&&details}
  </>;
}
