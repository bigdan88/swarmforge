import { WorkControls, QueueControls } from './Controls';
import type { Session } from './api';
import type { Navigate } from './ui';
import { useEffect, useState } from 'react';
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
  const [page,setPage]=useState<Page|null>(null);
  const [detail,setDetail]=useState<Work|null>(null);
  const [error,setError]=useState('');
  const [detailError,setDetailError]=useState('');
  const [updated,setUpdated]=useState<Date|null>(null);
  useEffect(()=>{
    const params=new URLSearchParams({page:view,search,status,offset:String(offset)});
    if(selected)params.set('item',selected);
    history.replaceState(null,'',`?${params}`);
  },[search,status,offset,selected]);
  useEffect(()=>{
    const controller=new AbortController(); let timer:ReturnType<typeof setTimeout>;
    setPage(null);setError('');
    async function refresh(){
      try{
        const query=new URLSearchParams({search,offset:String(offset),limit:'25'});
        if(status)query.set('status',status);if(view!=='work')query.set('view',view);
        const r=await fetch(`/api/v1/work?${query}`,{signal:controller.signal});
        if(!r.ok)throw new Error('Could not refresh work history. Check that the local service is available.');
        const data=parsePage(await r.json());
        if(!controller.signal.aborted){setPage(data);setError('');setUpdated(new Date());}
      }catch(e){if(!controller.signal.aborted)setError(e instanceof Error?e.message:'Refresh failed');}
      finally{if(!controller.signal.aborted)timer=setTimeout(refresh,4000);}
    }
    timer=setTimeout(refresh,200);
    return()=>{controller.abort();clearTimeout(timer);};
  },[search,status,offset,view]);
  useEffect(()=>{
    const controller=new AbortController();setDetail(previous=>previous?.id===selected?previous:null);setDetailError('');
    if(selected)fetch(`/api/v1/work/${encodeURIComponent(selected)}`,{signal:controller.signal})
      .then(async r=>{if(!r.ok)throw new Error(r.status===404?'This work item is no longer available.':'Could not load task details.');return parseWork(await r.json());})
      .then(data=>{if(!controller.signal.aborted)setDetail(data);})
      .catch(e=>{if(!controller.signal.aborted)setDetailError(e.message);});
    return()=>controller.abort();
  },[selected,updated]);
  const pr=detail?.pr_url && /^https:\/\/github\.com\//i.test(detail.pr_url) ? detail.pr_url:null;
  return <><header><div><div className="eyebrow">YOUR SWARM, AT A GLANCE</div><h1>{view==='prs'?'Pull requests':view==='failures'?'Failures':view==='history'?'History':'Work'}</h1><p>Follow what’s happening. Understand what comes next.</p></div><button className="primary" onClick={()=>navigate('new-task')}>New task</button></header>
      <QueueControls session={session}/><section className="stats" aria-label="Task summary">{[['running','In progress'],['queued','Ready to start'],['failed','Needs attention'],['done','Completed']].map(([key,name])=><div className="stat" key={key}><span>{name}</span><strong>{page?.counts[key]??'—'}</strong></div>)}</section>
      <section className="work-panel"><div className="toolbar"><h2>Task activity</h2><div className="filters"><label><span className="sr-only">Search work</span><input placeholder="Search tasks or repositories…" value={search} maxLength={200} onChange={e=>{setSearch(e.target.value);setOffset(0);}}/></label><label><span className="sr-only">Filter status</span><select value={status} onChange={e=>{setStatus(e.target.value);setOffset(0);}}><option value="">All statuses</option>{['running','queued','tentative','done','failed','cancelled','skipped'].map(s=><option key={s} value={s}>{label(s)}</option>)}</select></label></div></div>
      {error&&<p role="alert" className="error">{error} {page&&'Showing the last successful snapshot.'}</p>}
      {!page&&!error&&<p className="empty" role="status">Loading work…</p>}
      {page?.items.length===0&&<div className="empty"><h3>{search||status?'No matching tasks':'Your next idea starts here'}</h3><p>{search||status?'Try a different search or status.':'Create work in the existing dashboard. It will appear here automatically.'}</p></div>}
      <div className="tasks">{page?.items.map(w=><button className={`task ${selected===w.id?'selected':''}`} key={w.id} onClick={()=>setSelected(w.id)} aria-pressed={selected===w.id}><span className={`dot ${w.status}`} aria-hidden="true"/><span className="task-copy"><strong>{w.title}</strong><small>{w.repo||'No repository'} · {w.id}</small></span><span className={`badge ${w.status}`}>{label(w.status)}</span><span className="stage">{w.stage?label(w.stage):'Not started'}</span><span aria-hidden="true">↗</span></button>)}</div>
      <footer><span>{page?`${page.total} matching items`:'Work history'} · {updated?`Updated ${updated.toLocaleTimeString()}`:'Connecting'}</span><div className="pager"><button disabled={!page||offset===0} onClick={()=>setOffset(Math.max(0,offset-25))}>Previous</button><button disabled={!page||offset+25>=page.total||offset+25>100000} onClick={()=>setOffset(offset+25)}>Next</button></div></footer></section>
      {selected&&<section className="details" aria-label="Task details"><div className="detail-heading"><h2>Task details</h2><button onClick={()=>setSelected(null)}>Close details</button></div>{detailError?<p role="alert" className="error">{detailError}</p>:!detail?<p>Loading details…</p>:<><h3>{detail.title}</h3><p>{detail.reason||'No status explanation has been recorded.'}</p><dl><div><dt>Status</dt><dd>{label(detail.status)}</dd></div><div><dt>Stage</dt><dd>{detail.stage?label(detail.stage):'Not started'}</dd></div><div><dt>Created</dt><dd>{stamp(detail.created_at)}</dd></div><div><dt>Last updated</dt><dd>{stamp(detail.updated_at)}</dd></div><div><dt>Finished</dt><dd>{detail.finished_at?stamp(detail.finished_at):'Not finished'}</dd></div><div><dt>Run</dt><dd>{detail.run_id||'Not dispatched'}</dd></div></dl>{pr&&<a href={pr} target="_blank" rel="noreferrer">View pull request ↗</a>}<WorkControls key={selected} itemId={selected} session={session} onDone={()=>setUpdated(new Date())}/><Evidence key={selected+':'+detail.run_id} itemId={selected} runId={detail.run_id}/></>}</section>}
  </>;
}
