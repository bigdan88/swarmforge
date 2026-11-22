import {useEffect,useState,type FormEvent} from 'react';
import {collection,getJSON,label,money,num,record,rows,stamp,str,useAction,useGet,type Result,type Row,type Session} from './api';
import {ActionButton,Feedback,Heading,Loading,Pager,type Navigate} from './ui';

/* ---------------------------------------------------------------- Failures */
export function FailuresPage({session,navigate,selected}:{session:Session|null;navigate:Navigate;selected:string}){
  const [category,setCategory]=useState(''),[offset,setOffset]=useState(0);
  const list=useGet(`/api/v1/failures?offset=${offset}&category=${encodeURIComponent(category)}`,record,10000);
  const counts=record(list.data?.counts??{});
  return <><Heading title="Failures" description="Every unsuccessful run, why it failed and what happened next. Diagnoses also reach the knowledge vault so agents learn from them."/>
    <Feedback error={list.error}/>
    {list.data&&<div className="stats"><div className="stat"><span>Failures</span><strong>{num(counts.total)}</strong></div><div className="stat"><span>Fixed themselves</span><strong>{num(counts.fixed)}</strong></div><div className="stat"><span>Need you</span><strong>{num(counts.needs_you)}</strong></div><div className="stat"><span>Not yet diagnosed</span><strong>{num(list.data.undiagnosed)}</strong></div></div>}
    {num(list.data?.undiagnosed)>0&&<p className="muted">{num(list.data?.undiagnosed)} older failed run(s) have no post-mortem. Open them from History to diagnose one at a time.</p>}
    <div className="split">
      <section className="panel work-panel"><div className="toolbar"><h2>Post-mortems</h2><label><span className="sr-only">Cause</span><select value={category} onChange={e=>{setCategory(e.target.value);setOffset(0);}}><option value="">All causes</option>{rows(list.data?.categories).map(c=><option key={str(c.id)} value={str(c.id)}>{str(c.label)} ({num(c.count)})</option>)}</select></label></div>
        {!list.data&&!list.error&&<Loading error=""/>}
        {rows(list.data?.items).map(f=><button className={`task ${str(f.run_id)===selected?'selected':''}`} aria-current={str(f.run_id)===selected?'true':undefined} key={str(f.run_id)} onClick={()=>navigate('failures',str(f.run_id))}><span className="dot failed"/><span className="task-copy"><strong>{str(f.title)||str(f.run_id)}</strong><small>{str(f.category_label)} · {stamp(f.created_at)}{str(f.outcome)?` · ${str(f.outcome)}`:''}</small></span><span className="muted">{money(f.cost_usd)}</span></button>)}
        {list.data&&!rows(list.data.items).length&&<p className="empty">No failures recorded{category?' for this cause':''}.</p>}
        {list.data&&<Pager offset={offset} total={num(list.data.total)} setOffset={setOffset}/>}
      </section>
      {selected?<FailureDetail key={selected} runId={selected} session={session} navigate={navigate}/>:<section className="panel empty"><h2>Choose a failure</h2><p>See why it failed, what to do differently and the evidence the diagnosis used.</p></section>}
    </div></>;
}
function FailureDetail({runId,session,navigate}:{runId:string;session:Session|null;navigate:Navigate}){
  const view=useGet(`/api/v1/failures/${encodeURIComponent(runId)}`,record);
  const [authorized,setAuthorized]=useState(false);
  if(!view.data)return <section className="panel"><Loading error={view.error}/></section>;
  const f=view.data;
  return <section className="panel details-panel"><div className="detail-heading"><h2>{str(f.title)||runId}</h2><button onClick={()=>navigate('failures')}>Close</button></div>
    <p className="muted">{str(f.repo)||'No repository'} · {label(f.state)}{str(f.reason)?`: ${str(f.reason)}`:''} · diagnosed by {str(f.diagnosed_by)||'unknown'}</p>
    <p><strong>Cause:</strong> {str(f.category_label)}{str(f.outcome)&&<> · <strong>What happened next:</strong> {str(f.outcome)}</>}</p>
    <h3>Why it failed</h3><p className="prose">{str(f.root_cause)||'Not recorded'}</p>
    <h3>What to do differently</h3><p className="prose">{str(f.fix)||'Not recorded'}</p>
    {str(f.lesson)&&<><h3>Lesson</h3><p className="prose">{str(f.lesson)}</p></>}
    <details><summary>Evidence the diagnosis was based on</summary><pre>{str(f.evidence)||'None retained'}</pre>{f.evidence_truncated===true&&<p className="muted">Shortened in this view.</p>}</details>
    <div className="button-row"><button onClick={()=>navigate('work',str(f.work_item))}>Open this run</button>{str(f.heal_work_item)&&<button onClick={()=>navigate('work',str(f.heal_work_item))}>Open the retry</button>}</div>
    <div className="control-box"><label className="check"><input type="checkbox" checked={authorized} onChange={e=>setAuthorized(e.target.checked)}/>Diagnose again using the configured planner model (small model cost).</label><ActionButton operation="diagnose_failure" target={runId} args={{spending_authorized:authorized}} disabled={!session?.models_enabled||!authorized} onDone={()=>{setAuthorized(false);view.refresh();window.dispatchEvent(new Event('sf-badges'));}}>Diagnose again</ActionButton></div>
  </section>;
}

/* ---------------------------------------------------------- Pull requests */
export function PullRequestsPage({session,navigate}:{session:Session|null;navigate:Navigate}){
  const view=useGet('/api/v1/prs',record,60000);
  const [flash,setFlash]=useState(''),[refreshing,setRefreshing]=useState(false);
  const [method,setMethod]=useState('squash'),[remove,setRemove]=useState(true),[reviewed,setReviewed]=useState(false);
  const merges=useAction('merge-all');const [progress,setProgress]=useState('');
  const items=rows(view.data?.items),ready=items.filter(p=>p.ready===true);
  // Ask the server to re-check GitHub, then reload in place so the list never blanks.
  async function reload(){setRefreshing(true);try{await getJSON('/api/v1/prs?refresh=true');}catch{/* The view shows its own error. */}finally{setRefreshing(false);view.refresh();}}
  const done=(r:Result)=>{setFlash(r.message);void reload();};
  async function mergeAll(){
    const done:string[]=[];
    for(const pr of ready){setProgress(`Merging ${str(pr.repo)} #${num(pr.number)}…`);const r=await merges.send('merge_pr',`${str(pr.repo)}#${num(pr.number)}`,{method,delete_branch:remove,reviewed:true,expected_sha:str(pr.head_sha)});if(!r)break;done.push(`#${num(pr.number)}`);}
    setProgress('');if(done.length)setFlash(`Merged ${done.join(', ')}`);setReviewed(false);void reload();
  }
  return <><Heading title="Pull requests" description="Review and merge the swarm's pull requests. Merging changes the main branch on GitHub."><button disabled={refreshing} onClick={()=>{setFlash('');void reload();}}>{refreshing?'Checking GitHub…':'Refresh from GitHub'}</button></Heading>
    <Feedback error={view.error} message={flash}/>
    {view.data&&view.data.configured===false&&<section className="panel"><p>Load configuration when starting this interface to list pull requests.</p></section>}
    {view.data&&view.data.configured!==false&&view.data.token===false&&<p className="error" role="alert">No GitHub token. Add GITHUB_TOKEN to .env (or run gh auth login) with Contents and Pull requests write access, then restart this interface.</p>}
    {Object.entries(record(view.data?.errors??{})).map(([repo,msg])=><p className="error" role="alert" key={repo}>{repo}: {str(msg)}</p>)}
    {!view.data&&!view.error&&<Loading error=""/>}
    {view.data&&view.data.configured!==false&&<section className="panel control-box"><p><strong>{items.length} open</strong> · {ready.length} ready to merge · {items.filter(p=>str(p.state)==='dirty').length} with conflicts{typeof view.data.fetched_at==='number'?` · checked ${stamp(view.data.fetched_at)}`:''}</p>
      <div className="filters"><label>How to merge <select value={method} onChange={e=>setMethod(e.target.value)}><option value="squash">Squash (one commit per PR)</option><option value="merge">Merge commit</option><option value="rebase">Rebase</option></select></label><label className="check"><input type="checkbox" checked={remove} onChange={e=>setRemove(e.target.checked)}/>Delete the swarmforge/* branch after merging</label></div>
      <label className="check"><input type="checkbox" checked={reviewed} onChange={e=>setReviewed(e.target.checked)}/>I've reviewed these changes on GitHub</label>
      <button className="primary" disabled={!reviewed||!ready.length||merges.pending||!session?.actions_enabled} onClick={()=>void mergeAll()}>Merge all ready ({ready.length})</button><Feedback error={merges.error} message={progress}/></section>}
    {items.map(pr=><PullRequest key={`${str(pr.repo)}#${num(pr.number)}`} pr={pr} method={method} remove={remove} reviewed={reviewed} session={session} navigate={navigate} onDone={done}/>)}
    {view.data&&view.data.configured!==false&&!items.length&&!Object.keys(record(view.data.errors??{})).length&&<section className="panel empty"><h2>No open pull requests</h2><p>{rows(view.data.repos).length||(Array.isArray(view.data.repos)&&view.data.repos.length)?'Everything is merged.':'Pull requests appear here once a run opens one.'}</p></section>}
  </>;
}
function PullRequest({pr,method,remove,reviewed,session,navigate,onDone}:{pr:Row;method:string;remove:boolean;reviewed:boolean;session:Session|null;navigate:Navigate;onDone:(r:Result)=>void}){
  const target=`${str(pr.repo)}#${num(pr.number)}`,url=str(pr.url);
  return <section className="panel pr"><div className="detail-heading"><h2>{/^https:\/\/github\.com\//.test(url)?<a href={url} target="_blank" rel="noreferrer">#{num(pr.number)} {str(pr.title)} ↗</a>:<>#{num(pr.number)} {str(pr.title)}</>}</h2><span className={`badge ${pr.ready===true?'done':str(pr.state)==='dirty'?'failed':''}`}>{str(pr.status)}</span></div>
    <p className="muted">{str(pr.repo)} · <code>{str(pr.head)}</code> → <code>{str(pr.base)}</code> · by {str(pr.author)} · {str(pr.created_at).slice(0,10)} · +{num(pr.additions)} −{num(pr.deletions)} in {num(pr.changed_files)} file(s) · head <code>{str(pr.head_sha).slice(0,8)}</code></p>
    <div className="button-row"><ActionButton operation="merge_pr" target={target} args={{method,delete_branch:remove,reviewed,expected_sha:str(pr.head_sha)}} disabled={!reviewed||pr.ready!==true||!session?.actions_enabled} onDone={onDone}>Merge</ActionButton>
      {str(pr.state)==='behind'&&<ActionButton operation="update_pr" target={target} args={{expected_sha:str(pr.head_sha)}} disabled={!session?.actions_enabled} onDone={onDone}>Update branch</ActionButton>}
      {str(pr.work_item)&&<button onClick={()=>navigate('work',str(pr.work_item))}>What the swarm did</button>}</div>
    {str(pr.state)==='dirty'&&<p className="muted">Resolve conflicts on GitHub, or retry its task so it rebuilds on the latest main.</p>}
    {!reviewed&&pr.ready===true&&<p className="muted">Tick “I've reviewed these changes” above to enable merging.</p>}
  </section>;
}

/* -------------------------------------------------------------- What's new */
const featurePage:Record<string,string>={'Work':'work','Projects':'projects','Pull requests':'prs','New task':'new-task','Failures':'failures','Chat':'chat','Knowledge':'knowledge','Benchmarks':'benchmarks','Settings':'settings',"What's new":'whats-new','Speed':'speed'};
export function WhatsNew({navigate,session}:{navigate:Navigate;session:Session|null}){
  const view=useGet('/api/v1/features',record);const [query,setQuery]=useState(''),[category,setCategory]=useState(''),[flash,setFlash]=useState('');
  const q=query.trim().toLowerCase();
  const items=rows(view.data?.items).filter(f=>(!category||str(f.category)===category)&&(!q||['title','summary','benefit','where','category'].some(k=>str(f[k]).toLowerCase().includes(q))));
  let day='';
  return <><Heading title="What's new" description="User-visible changes in this installed build. Unmerged work is labelled preview.">{num(view.data?.unread)>0&&<ActionButton operation="mark_features_read" disabled={!session?.actions_enabled} onDone={r=>{setFlash(r.message);view.refresh();window.dispatchEvent(new Event('sf-badges'));}}>Mark all as read ({num(view.data?.unread)})</ActionButton>}</Heading><Feedback error={view.error} message={flash}/>{!view.data&&!view.error&&<Loading error=""/>}
    <section className="panel toolbar"><label><span className="sr-only">Search features</span><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Try chat, screenshots, benchmarks…"/></label><label><span className="sr-only">Category</span><select value={category} onChange={e=>setCategory(e.target.value)}><option value="">All categories</option>{(Array.isArray(view.data?.categories)?view.data.categories:[]).map(c=><option key={str(c)} value={str(c)}>{str(c)}</option>)}</select></label></section>
    {view.data&&!items.length&&<p className="empty">No highlights match. Try a different search or category.</p>}
    <div className="card-grid">{items.map(feature=>{const heading=str(feature.date)!==day;day=str(feature.date);const page=featurePage[str(feature.page)];return <article className="panel" key={str(feature.id)}>{heading&&<h3 className="day">{day}</h3>}<small className="muted">{str(feature.category)}{feature.unread===true?' · New to you':''}{feature.preview===true?' · Preview':''}</small><h2>{str(feature.title)}</h2><p>{str(feature.summary)}</p><p><strong>Why it helps:</strong> {str(feature.benefit)}</p><p className="muted">Find it: {str(feature.where)}</p><div className="button-row">{page&&page!=='speed'&&<button onClick={()=>navigate(page)}>Open {str(feature.page)}</button>}{/^https:\/\/github\.com\//.test(str(feature.url))&&<a href={str(feature.url)} target="_blank" rel="noreferrer">Source change ↗</a>}</div></article>;})}</div></>;
}

/* --------------------------------------------------------------- Knowledge */
const noteTypes=['user-note','decision','pitfall','pattern','repo-fact','review-lesson','model-behavior'];
export function Knowledge({selected,navigate,session}:{selected:string;navigate:Navigate;session:Session|null}){
  const [search,setSearch]=useState(''),[type,setType]=useState(''),[repo,setRepo]=useState(''),[offset,setOffset]=useState(0),[creating,setCreating]=useState(false);
  const view=useGet(`/api/v1/knowledge?search=${encodeURIComponent(search)}&type=${encodeURIComponent(type)}&repo=${encodeURIComponent(repo)}&offset=${offset}`,collection);
  const facets=record(view.data??{});
  const refreshList=view.refresh;
  // Returning from a note (after an edit or delete) must show the vault as it is now.
  useEffect(()=>{if(!selected)refreshList();},[selected,refreshList]);
  if(selected)return <><Heading title="Knowledge" description="Find, edit and add notes and lessons the team reuses."/><Note key={selected} id={selected} navigate={navigate} session={session}/></>;
  return <><Heading title="Knowledge" description="Find, edit and add notes and lessons the team reuses."><button className="primary" disabled={!session?.actions_enabled} onClick={()=>setCreating(c=>!c)}>{creating?'Close new note':'New note'}</button></Heading>
    {creating&&<NoteForm session={session} onDone={id=>{setCreating(false);view.refresh();if(id)navigate('knowledge',id);}}/>}
    <section className="panel"><div className="toolbar"><h2>Notes</h2><div className="filters"><label><span className="sr-only">Search knowledge</span><input value={search} onChange={e=>{setSearch(e.target.value);setOffset(0);}} placeholder="Search note text"/></label><label><span className="sr-only">Type</span><select value={type} onChange={e=>{setType(e.target.value);setOffset(0);}}><option value="">All types</option>{(Array.isArray(facets.types)?facets.types:[]).map(t=><option key={str(t)} value={str(t)}>{label(t)}</option>)}</select></label><label><span className="sr-only">Repository</span><select value={repo} onChange={e=>{setRepo(e.target.value);setOffset(0);}}><option value="">All repositories</option>{(Array.isArray(facets.repos)?facets.repos:[]).map(r=><option key={str(r)} value={str(r)}>{str(r)}</option>)}</select></label></div></div>
      <Feedback error={view.error}/>{view.data?.items.map(note=><button className="task" key={str(note.id)} onClick={()=>navigate('knowledge',str(note.id))}><span className="task-copy"><strong>{str(note.title)}</strong><small>{label(note.type)}{str(note.repo)?` · ${str(note.repo)}`:''}{str(note.folder)?` · ${str(note.folder)}/`:''} — {str(note.excerpt)}</small></span></button>)}
      {view.data?.items.length===0&&<p className="empty">No readable notes match. Knowledge browsing requires a configured vault.</p>}{view.data&&<Pager offset={offset} total={view.data.total} setOffset={setOffset}/>}
      <p className="muted">Notes are plain Markdown in your vault, including subfolders; open the same folder in Obsidian if you like. Hidden folders and linked folders are skipped.{facets.limited===true?' Only the first 2048 vault entries are scanned.':''}</p></section></>;
}
function NoteForm({session,note,onDone}:{session:Session|null;note?:Row;onDone:(id?:string)=>void}){
  const [title,setTitle]=useState(str(note?.title)),[type,setType]=useState(str(note?.type)||'user-note'),[repo,setRepo]=useState(str(note?.repo)),[tags,setTags]=useState((Array.isArray(note?.tags)?note.tags:[]).join(', ')),[body,setBody]=useState(()=>{const t=str(note?.text);return t.startsWith('---')?t.replace(/^---[\s\S]*?\n---\n?/,'').replace(/^\n/,''):t;});
  const action=useAction(note?`note:${str(note.id)}`:'note:new');
  async function submit(e:FormEvent){e.preventDefault();const args={title,type,repo:repo.trim()||null,tags:tags.split(',').map(t=>t.trim()).filter(Boolean),body,...(note?{expected_sha:str(note.sha)}:{})};const r=await action.send(note?'save_note':'create_note',note?str(note.id):'',args);if(r)onDone(str((r as Row).note_id)||undefined);}
  return <form className="panel editor" onSubmit={submit}><fieldset disabled={action.pending||!session?.actions_enabled}><h2>{note?'Edit note':'New note'}</h2>
    <label>Title (state the single idea as a claim)<input required maxLength={300} value={title} onChange={e=>setTitle(e.target.value)}/></label>
    <div className="filters"><label>Type<select value={type} onChange={e=>setType(e.target.value)}>{noteTypes.map(t=><option key={t} value={t}>{label(t)}</option>)}</select></label><label>Repository (blank applies everywhere)<input maxLength={300} value={repo} onChange={e=>setRepo(e.target.value)}/></label></div>
    <label>Tags (comma separated)<input value={tags} onChange={e=>setTags(e.target.value)}/></label>
    <label>Body (Markdown; link with [[note title]])<textarea required rows={12} maxLength={60000} value={body} onChange={e=>setBody(e.target.value)}/></label>
    <div className="button-row"><button className="primary" type="submit">{note?'Save note':'Create note'}</button><button type="button" onClick={()=>onDone()}>Cancel</button></div></fieldset><Feedback error={action.error}/></form>;
}
function Note({id,navigate,session}:{id:string;navigate:Navigate;session:Session|null}){
  const view=useGet(`/api/v1/knowledge/${encodeURIComponent(id)}`,record);
  const [editing,setEditing]=useState(false),[confirm,setConfirm]=useState(false);
  const n=view.data;
  return <><button className="back-link" onClick={()=>navigate('knowledge')}>← All notes</button>
    {!n?<section className="panel"><Loading error={view.error}/></section>:editing?<NoteForm session={session} note={n} onDone={()=>{setEditing(false);view.refresh();}}/>:
    <section className="panel"><div className="detail-heading"><h2>{str(n.title)}</h2>{n.editable===true&&<button disabled={!session?.actions_enabled} onClick={()=>setEditing(true)}>Edit</button>}</div>
      <p className="muted">{label(n.type)} · repository {str(n.repo)||'any'} · source {str(n.source)||'unknown'}{str(n.created)?` · created ${str(n.created)}`:''}{rows(n.tags).length||(Array.isArray(n.tags)&&n.tags.length)?` · tags ${(n.tags as string[]).join(', ')}`:''}{str(n.folder)?` · folder ${str(n.folder)}/`:''}</p>
      <pre className="prose">{str(n.text)}</pre>{n.truncated===true&&<p className="muted">Long note shortened in this preview, so it cannot be edited here. The file is unchanged.</p>}
      {n.editable!==true&&n.truncated!==true&&<p className="muted">{label(n.type)} notes are maintained by SwarmForge and are read-only here.</p>}
      {n.editable===true&&<div className="control-box"><label className="check"><input type="checkbox" checked={confirm} onChange={e=>setConfirm(e.target.checked)}/>I want to delete this note permanently</label><ActionButton operation="delete_note" target={id} args={{expected_sha:str(n.sha),confirm,type:str(n.type)}} disabled={!confirm||!session?.actions_enabled} onDone={()=>navigate('knowledge')}>Delete note</ActionButton></div>}
    </section>}</>;
}
