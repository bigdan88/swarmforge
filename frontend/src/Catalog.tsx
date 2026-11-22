import {useState} from 'react';
import {label,money,num,record,rows,str,useGet,collection,type Session} from './api';
import {Feedback,Heading,Loading,Pager,type Navigate} from './ui';
export function SettingsPage({session,teams=false}:{session:Session|null;teams?:boolean}){
  const settings=session?.settings;
  return <><Heading title={teams?'Model teams':'Settings'} description={teams?'Inspect the named lineups available to this workspace.':'Inspect the active policy and model assignments without exposing credentials.'}/>
    {!settings?.configured&&<section className="panel"><p>Load configuration when starting this interface to inspect models and policy.</p></section>}
    {settings?.configured&&<><section className="panel"><h2>Current team: {settings.team||'Configured roles'}</h2><div className="table-scroll"><table><thead><tr><th>Role</th><th>Model</th><th>Fallbacks</th></tr></thead><tbody>{settings.roles.map(role=><tr key={role.role}><td>{label(role.role)}</td><td>{role.model}</td><td>{role.fallbacks.join(', ')||'None'}</td></tr>)}</tbody></table></div></section>
      {!teams&&<section className="panel"><h2>Execution defaults</h2><dl>{Object.entries(settings.limits).map(([key,value])=><div key={key}><dt>{label(key)}</dt><dd>{key==='budget_usd'?money(value):key==='max_runtime_s'?`${value/3600} hours`:String(value)}</dd></div>)}</dl><p className="muted">Individual task/project allowances still apply. Configuration editing, secrets and experimental speed options remain in the existing dashboard.</p></section>}
      {teams&&<div className="card-grid">{settings.teams.map(team=><section className="panel" key={team.name}><h2>{team.name}</h2><p>{team.description}</p><dl>{Object.entries(team.models).map(([role,model])=><div key={role}><dt>{label(role)}</dt><dd>{model}</dd></div>)}</dl></section>)}</div>}
    </>}
  </>;
}
export function WhatsNew(){
  const view=useGet('/api/v1/features',record);return <><Heading title="What's new" description="User-visible changes in this installed build. Unmerged work is labelled preview."/><Feedback error={view.error}/>{!view.data&&!view.error&&<Loading error=""/>}<div className="card-grid">{rows(view.data?.items).map(feature=><article className="panel" key={str(feature.id)}><small className="muted">{str(feature.date)} · {str(feature.category)} {feature.preview===true?'· Preview':''}</small><h2>{str(feature.title)}</h2><p>{str(feature.summary)}</p><p><strong>Why it helps:</strong> {str(feature.benefit)}</p><p className="muted">{str(feature.where)}</p>{/^https:\/\/github\.com\//.test(str(feature.url))&&<a href={str(feature.url)} target="_blank" rel="noreferrer">Source change ↗</a>}</article>)}</div></>;
}
export function Benchmarks(){
  const view=useGet('/api/v1/outcomes',record);return <><Heading title="Recorded outcomes" description="Historical run outcomes by workflow. This is not a controlled comparison of model quality."/><section className="panel"><Feedback error={view.error}/><div className="table-scroll"><table><thead><tr><th>Workflow</th><th>Runs</th><th>Completed</th><th>Failed / blocked</th><th>Known recorded usage</th></tr></thead><tbody>{rows(view.data?.items).map(item=><tr key={str(item.mode)}><td>{label(item.mode)}</td><td>{num(item.runs)}</td><td>{num(item.completed)}</td><td>{num(item.failed)}</td><td>{money(item.cost_usd)}</td></tr>)}</tbody></table></div><p className="muted">Includes reported and estimated costs. Incomplete and cancelled runs are included in the run count. Controlled benchmark suites remain in the existing dashboard.</p></section></>;
}

export function Knowledge({selected,navigate}:{selected:string;navigate:Navigate}){
  const [search,setSearch]=useState(''),[offset,setOffset]=useState(0);
  const view=useGet(`/api/v1/knowledge?search=${encodeURIComponent(search)}&offset=${offset}`,collection);
  return <><Heading title="Knowledge" description="Find retained notes and lessons the team can reuse."/>{selected?<Note key={selected} id={selected} navigate={navigate}/>:<section className="panel"><div className="toolbar"><h2>Knowledge notes</h2><label><span className="sr-only">Search knowledge</span><input value={search} onChange={e=>{setSearch(e.target.value);setOffset(0);}} placeholder="Search note text"/></label></div><Feedback error={view.error}/>{view.data?.items.map(note=><button className="task" key={str(note.id)} onClick={()=>navigate('knowledge',str(note.id))}><span className="task-copy"><strong>{str(note.title)}</strong><small>{str(note.excerpt)}</small></span></button>)}{view.data?.items.length===0&&<p className="empty">No readable notes match. Knowledge browsing requires a configured vault.</p>}{view.data&&<Pager offset={offset} total={view.data.total} setOffset={setOffset}/>}<p className="muted">Search covers up to 1024 top-level vault entries. Nested notes and note editing remain available in your vault editor or the existing dashboard.</p></section>}</>;
}
function Note({id,navigate}:{id:string;navigate:Navigate}){
  const view=useGet(`/api/v1/knowledge/${encodeURIComponent(id)}`,record);
  return <><button className="back-link" onClick={()=>navigate('knowledge')}>← All notes</button><section className="panel">{!view.data?<Loading error={view.error}/>:<><h2>{str(view.data.title)}</h2><pre>{str(view.data.text)}</pre>{view.data.truncated===true&&<p className="muted">Long note shortened in this preview. The retained file is unchanged.</p>}</>}</section></>;
}
