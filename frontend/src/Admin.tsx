import {useEffect,useState} from 'react';
import {getJSON,label,money,num,record,rows,str,useAction,useGet,type Row,type Session} from './api';
import {Feedback,Heading,Loading} from './ui';

/* ---------------------------------------------------------------- Settings */
type RoleEdit={model:string;temperature:string;max_steps:string};
export function SettingsEditor({session}:{session:Session|null}){
  const view=useGet('/api/v1/settings/details',record);
  const [roles,setRoles]=useState<Record<string,RoleEdit>>({}),[limits,setLimits]=useState<Record<string,string>>({});
  const [flash,setFlash]=useState(''),[testAuthorized,setTestAuthorized]=useState(false);
  const save=useAction('settings'),test=useAction('test-models');
  const [tests,setTests]=useState<Row[]>([]);
  const d=view.data;
  useEffect(()=>{if(!d||d.configured!==true)return;
    setRoles(Object.fromEntries(rows(d.roles).map(r=>[str(r.role),{model:str(r.model),temperature:String(num(r.temperature)),max_steps:String(num(r.max_steps))}])));
    setLimits(Object.fromEntries(Object.entries(record(d.limits)).map(([k,v])=>[k,String(v)])));},[d]);
  if(!d)return <section className="panel"><Loading error={view.error}/></section>;
  if(d.configured!==true)return <section className="panel"><p>Load configuration when starting this interface to inspect and edit settings.</p></section>;
  const original=Object.fromEntries(rows(d.roles).map(r=>[str(r.role),r]));
  const changedRoles=Object.fromEntries(Object.entries(roles).filter(([role,v])=>{const o=original[role];return o&&(v.model!==str(o.model)||Number(v.temperature)!==num(o.temperature)||Number(v.max_steps)!==num(o.max_steps));}).map(([role,v])=>[role,{model:v.model.trim(),temperature:Number(v.temperature),max_steps:Number(v.max_steps)}]));
  const changedLimits=Object.fromEntries(Object.entries(limits).filter(([k,v])=>Number(v)!==num(record(d.limits)[k])).map(([k,v])=>[k,Number(v)]));
  const dirty=Object.entries(roles).reduce((n,[role,v])=>{const o=original[role];return o?n+Number(v.model!==str(o.model))+Number(Number(v.temperature)!==num(o.temperature))+Number(Number(v.max_steps)!==num(o.max_steps)):n;},0)+Object.keys(changedLimits).length;
  async function submit(){const r=await save.send('save_settings','',{expected_config_sha:str(d?.config_sha),roles:changedRoles,limits:changedLimits});if(r){setFlash(r.message);view.refresh();}}
  const limitLabels:Record<string,[string,string]>={budget_usd:['Budget (USD)','0.1'],max_runtime_s:['Max runtime (seconds)','60'],max_concurrent_agents:['Concurrent agents','1'],max_repair_cycles:['Repair cycles','1']};
  return <>
    <HealthPanel/>
    <section className="panel"><h2>API keys</h2><p className="muted">Keys are read from the environment. Only whether each variable is set is shown; values are never displayed or written to {str(d.source_name)||'the configuration'}.</p>
      <ul className="plain">{rows(d.providers).map(p=><li key={str(p.name)}><strong>{str(p.name)}</strong> · <code>{str(p.key_env)}</code> · {p.key_set===true?'set':<span className="error-text">missing</span>}</li>)}</ul></section>
    {d.restart_needed===true&&<p className="notice" role="status">Saved settings differ from the ones this web interface started with. Restart it (and any running queue worker) to use them.</p>}
    <section className="panel editor"><div className="detail-heading"><h2>Models</h2><span className="muted">{dirty?`${dirty} unsaved change${dirty>1?'s':''}`:'No changes'}</span></div>
      <div className="table-scroll"><table className="edit-table"><thead><tr><th>Role</th><th>Model</th><th>Temperature</th><th>Max steps</th></tr></thead><tbody>{Object.entries(roles).map(([role,v])=><tr key={role}><td>{label(role)}<br/><small className="muted">{str(original[role]?.provider)}</small></td>
        <td data-label="Model"><ModelInput role={role} provider={str(original[role]?.provider)} value={v.model} onChange={model=>setRoles(o=>({...o,[role]:{...o[role],model}}))}/></td>
        <td data-label="Temperature"><input aria-label={`${role} temperature`} type="number" min="0" max="2" step="0.1" value={v.temperature} onChange={e=>setRoles(o=>({...o,[role]:{...o[role],temperature:e.target.value}}))}/></td>
        <td data-label="Max steps"><input aria-label={`${role} max steps`} type="number" min="1" max="500" step="1" value={v.max_steps} onChange={e=>setRoles(o=>({...o,[role]:{...o[role],max_steps:e.target.value}}))}/></td></tr>)}</tbody></table></div>
      <h2>Limits</h2><div className="form-grid">{Object.entries(limits).map(([k,v])=><label key={k}>{limitLabels[k]?.[0]??label(k)}<input type="number" step={limitLabels[k]?.[1]??'1'} value={v} onChange={e=>setLimits(o=>({...o,[k]:e.target.value}))}/></label>)}</div>
      <p className="muted">Saving validates the values with the same rules as the command line, then writes {str(d.source_name)} keeping comments and a .bak backup. Restart the web interface afterwards to use the new values.</p>
      <div className="button-row"><button className="primary" disabled={!dirty||save.pending||!session?.actions_enabled||d.editable!==true} onClick={()=>void submit()}>{save.pending?'Saving…':'Save settings'}</button><button disabled={!dirty} onClick={()=>{setFlash('');view.refresh();}}>Discard changes</button></div>
      <Feedback error={save.error} message={flash}/></section>
    <section className="panel"><h2>Test models</h2><p className="muted">Sends one tiny tool-calling request to each role's model to confirm it answers. This makes a small paid call per role.</p>
      <label className="check"><input type="checkbox" checked={testAuthorized} onChange={e=>setTestAuthorized(e.target.checked)}/>Send a test request to each configured model.</label>
      <button disabled={!testAuthorized||test.pending||!session?.models_enabled} onClick={async()=>{const r=await test.send('test_models','',{spending_authorized:true});if(r){setTests(rows((r as Row).details));setTestAuthorized(false);}}}>{test.pending?'Testing…':'Test models'}</button><Feedback error={test.error} message={test.message}/>
      {tests.length>0&&<ul className="plain">{tests.map(t=><li key={str(t.role)}>{t.ok===true?'✓':'✗'} <strong>{label(t.role)}</strong> ({str(t.model)}): {str(t.detail)}{typeof t.cost_usd==='number'?` · $${t.cost_usd.toFixed(6)}`:' · cost unavailable'}</li>)}</ul>}
      <p className="muted">Building the sandbox image streams a long Docker build; run it from the command line (<code>swarmforge sandbox build</code>) or the previous dashboard.</p></section>
  </>;
}
function ModelInput({role,provider,value,onChange}:{role:string;provider:string;value:string;onChange:(v:string)=>void}){
  const [options,setOptions]=useState<string[]|null>(null),[loading,setLoading]=useState(false),[note,setNote]=useState('');
  async function load(){setLoading(true);setNote('');try{const r=record(await getJSON(`/api/v1/settings/models?provider=${encodeURIComponent(provider)}`));if(r.available===true&&Array.isArray(r.models))setOptions(r.models.filter((m):m is string=>typeof m==='string'));else setNote('Provider list unavailable; type a model name.');}catch{setNote('Provider list unavailable; type a model name.');}finally{setLoading(false);}}
  const id=`models-${role}`;
  return <div className="model-input"><input aria-label={`${role} model`} list={options?id:undefined} maxLength={200} value={value} onChange={e=>onChange(e.target.value)}/>{options&&<datalist id={id}>{options.map(m=><option key={m} value={m}/>)}</datalist>}<button type="button" disabled={loading} onClick={()=>void load()}>{loading?'Loading…':options?`${options.length} available`:'List models'}</button>{note&&<small className="muted">{note}</small>}</div>;
}
function HealthPanel(){
  const [run,setRun]=useState(0);
  return <section className="panel"><div className="detail-heading"><h2>Health</h2><button onClick={()=>setRun(n=>n+1)}>{run?'Check again':'Run health checks'}</button></div>
    {run?<HealthResults key={run}/>:<p className="muted">Checks provider keys, Git, Docker, the sandbox image and GitHub access. Nothing is changed.</p>}
  </section>;
}
function HealthResults(){
  const view=useGet('/api/v1/settings/health',record);
  if(!view.data)return <Loading error={view.error}/>;
  return <>{rows(view.data.checks).map(c=><div className="health-row" key={str(c.name)}><span className={`badge ${str(c.status)==='ok'?'done':str(c.status)==='skipped'?'':'failed'}`}>{str(c.status)==='ok'?'OK':str(c.status)==='skipped'?'Skipped':'Needs attention'}</span><div><strong>{str(c.name)}</strong>{c.optional===true&&str(c.status)!=='ok'?' (optional)':''}<br/><small className="muted">{str(c.message)}</small>{str(c.fix)&&str(c.status)!=='ok'&&<pre className="fix">{str(c.fix)}</pre>}</div></div>)}</>;
}

/* ------------------------------------------------------------------- Speed */
export function SpeedPage(){
  const [n,setN]=useState(30);
  const view=useGet(`/api/v1/speed?n=${n}`,record,30000);
  const d=view.data,summary=record(d?.summary??{}),phases=Array.isArray(d?.phases)?d.phases.map(String):[];
  const runs=rows(d?.runs),max=Math.max(1,...runs.map(r=>phases.reduce((t,p)=>t+num(r[`${p}_min`]),0)));
  return <><Heading title="Speed" description="Where each finished run's time went. Change a setting, let a few runs finish, then compare groups under By settings."><label>Runs <select value={n} onChange={e=>setN(Number(e.target.value))}>{[10,20,30,50,100,200].map(x=><option key={x} value={x}>{x}</option>)}</select></label></Heading>
    <Feedback error={view.error}/>{!d&&!view.error&&<Loading error=""/>}
    {d&&!runs.length&&<section className="panel empty"><h2>No finished runs yet</h2><p>Metrics appear once a run completes or fails.</p></section>}
    {runs.length>0&&<><div className="stats"><div className="stat"><span>Median run</span><strong>{num(summary.total_min).toFixed(0)} min</strong></div><div className="stat"><span>Median working time</span><strong>{num(summary.work_min).toFixed(0)} min</strong></div><div className="stat"><span>Median repair cycles</span><strong>{num(summary.repair_cycles)}</strong></div><div className="stat"><span>Median cost</span><strong>{money(summary.cost_usd)}</strong></div></div>
      <section className="panel"><h2>Where the time goes</h2><div className="legend">{phases.map((p,i)=><span key={p}><i className={`phase p${i}`}/>{p}</span>)}</div>
        {runs.map(r=><div className="time-row" key={str(r.run_id)}><span className="time-label" title={str(r.run_id)}>{str(r.title).slice(0,40)}</span><div className="time-bar" role="img" aria-label={phases.map(p=>`${p} ${num(r[`${p}_min`]).toFixed(1)} minutes`).join(', ')}>{phases.map((p,i)=>num(r[`${p}_min`])>0.05&&<i key={p} className={`phase p${i}`} style={{width:`${100*num(r[`${p}_min`])/max}%`}} title={`${p}: ${num(r[`${p}_min`]).toFixed(1)} min`}/>)}</div></div>)}</section>
      <section className="panel"><h2>Median per phase</h2><Table rows={phases.map(p=>({phase:p,'median minutes':num(summary[`${p}_min`]).toFixed(1),'what it is':str(record(d?.phase_help??{})[p])}))}/></section>
      <section className="panel"><h2>By settings</h2><p className="muted">Medians per settings fingerprint: models and max steps, repair cycles, blocking severities and tester cadence.</p><Table rows={rows(d?.by_settings)}/></section>
      <section className="panel"><h2>Agents by role and model</h2><Table rows={rows(d?.agents)}/></section>
      {rows(d?.healing).length>0&&<section className="panel"><h2>Self-healing outcomes</h2><Table rows={rows(d?.healing)}/></section>}
      <p className="muted">CSV export: <code>swarmforge metrics --csv FILE</code>.</p></>}</>;
}
function Table({rows:data}:{rows:Row[]}){
  if(!data.length)return <p className="muted">Nothing recorded.</p>;
  const cols=Object.keys(data[0]);
  const fmt=(v:unknown)=>typeof v==='number'?(Number.isInteger(v)?String(v):v.toFixed(2)):v===null||v===undefined?'—':String(v);
  return <div className="table-scroll"><table><thead><tr>{cols.map(c=><th key={c}>{label(c)}</th>)}</tr></thead><tbody>{data.map((r,i)=><tr key={i}>{cols.map(c=><td key={c}>{fmt(r[c])}</td>)}</tr>)}</tbody></table></div>;
}

/* -------------------------------------------------------------- Benchmarks */
export function BenchmarksPage(){
  const bench=useGet('/api/v1/benchmarks',record),outcomes=useGet('/api/v1/outcomes',record);
  return <><Heading title="Benchmarks" description="Compare workflows and model teams on the same pinned tasks, plus recorded outcomes from everyday work."/>
    <Feedback error={bench.error||outcomes.error}/>
    {bench.data&&!rows(bench.data.items).length&&<section className="panel"><h2>No benchmark suites yet</h2><p>Run <code>swarmforge bench-suite bench/suite.yaml --budget 20</code> (add <code>--teams chinese american-open</code> to compare teams). Suites run real paid tasks, so they are started from the command line.</p></section>}
    {rows(bench.data?.items).map(b=><section className="panel" key={num(b.id)}><h2>{str(b.label)} — {str(b.issue)}</h2>{str(b.error)&&<p className="error" role="alert">{str(b.error)}</p>}<Table rows={rows(b.rows)}/>{b.suite===true&&rows(b.rows).length>1&&<div className="bars">{rows(b.rows).map(r=><div className="time-row" key={`${str(r.team)}/${str(r.mode)}`}><span className="time-label">{str(r.team)} / {str(r.mode)}</span><div className="time-bar" role="img" aria-label={`${num(r.delivery_pct)}% delivery`}><i className="phase p0" style={{width:`${num(r.delivery_pct)}%`}}/></div><span className="muted">{num(r.delivery_pct)}% delivered · {typeof r.cost_per_success_usd==='number'?`${money(r.cost_per_success_usd)}/success`:'no successes'}</span></div>)}</div>}</section>)}
    <section className="panel"><h2>Recorded outcomes</h2><p className="muted">Historical outcomes by workflow. Not a controlled comparison of model quality.</p><div className="table-scroll"><table><thead><tr><th>Workflow</th><th>Runs</th><th>Completed</th><th>Failed / blocked</th><th>Known recorded usage</th></tr></thead><tbody>{rows(outcomes.data?.items).map(item=><tr key={str(item.mode)}><td>{label(item.mode)}</td><td>{num(item.runs)}</td><td>{num(item.completed)}</td><td>{num(item.failed)}</td><td>{money(item.cost_usd)}</td></tr>)}</tbody></table></div></section></>;
}
