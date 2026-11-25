import {useEffect,useRef,useState,type ReactNode} from 'react';
import {money,num,record,rows,str,useAction,useGet,type Row,type Session} from './api';
import {Diagram,type DiagramEdge,type DiagramNode} from './Diagram';
import {Feedback,Loading} from './ui';

const STATUS_ICON:Record<string,string>={passed:'✅',failed:'❌'};
type Shot={src:string;title:string;caption:string};

/** Text with `code` spans, without rendering any markup the model wrote. */
function Inline({text}:{text:string}){
  return <>{text.split(/(`[^`]+`)/g).map((part,i)=>part.startsWith('`')&&part.endsWith('`')&&part.length>2?<code key={i}>{part.slice(1,-1)}</code>:<span key={i}>{part}</span>)}</>;
}

const strings=(value:unknown)=>Array.isArray(value)?value.map(str).filter(Boolean):[];
const nodes=(value:unknown):DiagramNode[]=>rows(value).map(n=>({id:str(n.id),label:str(n.label),kind:str(n.kind),note:str(n.note),where:str(n.where)})).filter(n=>n.id&&n.label);
const edges=(value:unknown):DiagramEdge[]=>rows(value).map(e=>({from:str(e.from),to:str(e.to),label:str(e.label)}));
const shotUrl=(item:string,run:string,name:string)=>`/api/v1/work/${encodeURIComponent(item)}/screenshots/${encodeURIComponent(name)}?run_id=${encodeURIComponent(run)}`;
const prettyName=(name:string)=>name.replace(/^c\d+-/,'').replace(/\.png$/,'').replace(/[-_]/g,' ');

function Card({title,tone,children}:{title:string;tone:string;children:ReactNode}){
  return <section className={`explainer-card ${tone}`}><h4>{title}</h4>{children}</section>;
}

/** The plain-English part on top: what exists, how to try it, what's missing, how it's built and how it works. */
function Explainer({id,state,session,shots,hasWork,onChanged,onZoom}:{id:string;state:Row|null;session:Session|null;shots:Shot[];hasWork:boolean;onChanged:()=>void;onZoom:(s:Shot)=>void}){
  const action=useAction(`project-explainer:${id}`),asked=useRef(false);
  const status=str(state?.status),e=record(state?.explainer??{}),canWrite=session?.models_enabled===true;
  const write=()=>{void action.send('explain_project',id,{}).then(r=>{if(r)onChanged();});};
  useEffect(()=>{if(!state&&canWrite&&hasWork&&!asked.current){asked.current=true;write();}});// first visit: ask the lead once
  const arch=record(e.architecture??{}),flow=record(e.flow??{}),archNodes=nodes(arch.nodes),flowNodes=nodes(flow.nodes);
  const picked:Shot[]=rows(e.screenshots).map(s=>({src:shotUrl(str(s.item_id),str(s.run_id),str(s.name)),title:str(s.task_title),caption:str(s.caption)||prettyName(str(s.name))}));
  const gallery=picked.length?picked:shots.slice(0,8);
  const ready=!!str(e.headline)||strings(e.what_you_can_do).length>0;
  return <div className="explainer">
    {status==='writing'&&<p className="explainer-status" role="status"><span className="dot running"/> The lead is writing a plain-English explainer with diagrams. This usually takes about a minute…</p>}
    {status==='failed'&&<div className="blocker" role="alert"><strong>The explainer could not be written</strong><p>{str(state?.error)}</p><button disabled={!canWrite||action.pending} onClick={write}>Try again</button></div>}
    {!state&&!canWrite&&<p className="muted">Start SwarmForge with <code>--allow-actions</code> and a model configuration to get a plain-English explainer with diagrams.</p>}
    {!state&&canWrite&&!hasWork&&<p className="muted">Nothing has been delivered yet. Once tasks finish, the lead explains what was built here, with diagrams and screenshots.</p>}
    {!state&&canWrite&&action.pending&&<p className="explainer-status" role="status">Asking the lead to explain what was built…</p>}
    <Feedback error={action.error}/>
    {ready&&<>
      {state?.stale===true&&status!=='writing'&&<div className="notice"><p>New work was delivered after this explainer was written.</p><button disabled={!canWrite||action.pending} onClick={write}>Update the explainer</button></div>}
      {str(e.headline)&&<p className="explainer-headline">{str(e.headline)}</p>}
      <div className="explainer-grid">
        {strings(e.what_you_can_do).length>0&&<Card title="What you can do now" tone="good"><ul className="ticks">{strings(e.what_you_can_do).map((t,i)=><li key={i}><Inline text={t}/></li>)}</ul></Card>}
        {strings(e.how_to_try).length>0&&<Card title="Try it yourself" tone="try"><ol>{strings(e.how_to_try).map((t,i)=><li key={i}><Inline text={t}/></li>)}</ol></Card>}
        {strings(e.not_yet).length>0&&<Card title="Not done yet" tone="warn"><ul>{strings(e.not_yet).map((t,i)=><li key={i}><Inline text={t}/></li>)}</ul></Card>}
        {strings(e.needs_you).length>0&&<Card title="Needs you" tone="you"><ul>{strings(e.needs_you).map((t,i)=><li key={i}><Inline text={t}/></li>)}</ul></Card>}
      </div>
      {archNodes.length>0&&<section className="explainer-section"><h3>How it's built</h3><p className="muted">The main pieces and what flows between them.</p>
        <Diagram variant="architecture" nodes={archNodes} edges={edges(arch.edges)} title="How it's built"/></section>}
      {flowNodes.length>0&&<section className="explainer-section"><h3>How it works{str(flow.title)?`: ${str(flow.title)}`:''}</h3>
        <Diagram variant="flow" nodes={flowNodes} edges={edges(flow.edges)} title={`How it works: ${str(flow.title)}`}/></section>}
    </>}
    {gallery.length>0&&<section className="explainer-section"><h3>Screenshots</h3>
      <div className="explainer-shots">{gallery.map(s=><button key={s.src} className="explainer-shot" onClick={()=>onZoom(s)}><img src={s.src} alt={s.caption} loading="lazy"/><span>{s.caption}</span><small className="muted">{s.title}</small></button>)}</div></section>}
    {ready&&status!=='writing'&&state?.stale!==true&&<p className="muted explainer-footer">Written by the project lead from the code, task reports and checks. <button className="link-button" disabled={!canWrite||action.pending} onClick={write}>Rewrite it</button></p>}
  </div>;
}

/** One place to review what a project built: a plain-English explainer first, the evidence underneath. */
export function ProjectReview({id,session,onTalk}:{id:string;session:Session|null;onTalk:()=>void}){
  const [writing,setWriting]=useState(false);
  const review=useGet(`/api/v1/projects/${encodeURIComponent(id)}/review`,record,writing?4000:0,true);
  const [showAll,setShowAll]=useState(false);
  const [zoom,setZoom]=useState<Shot|null>(null);
  const status=str(review.data?.explainer&&record(review.data.explainer).status);
  useEffect(()=>{setWriting(status==='writing');},[status]);
  if(!review.data)return <Loading error={review.error}/>;
  const r=review.data,counts=record(r.counts??{}),milestones=rows(r.milestones),tasks=rows(r.tasks);
  const inReview=milestones.filter(m=>m.in_review===true),shown=showAll||!inReview.length?milestones:inReview;
  const allShots:Shot[]=tasks.flatMap(t=>(Array.isArray(t.screenshots)?t.screenshots.map(str):[]).map(name=>({src:shotUrl(str(t.item_id),str(t.run_id),name),title:str(t.title),caption:prettyName(name)})));
  const explainer=r.explainer?record(r.explainer):null;
  return <div className="project-review">
    <Feedback error={review.error}/>
    <Explainer id={id} state={explainer} session={session} shots={allShots} hasWork={tasks.length>0} onChanged={()=>{setWriting(true);review.refresh();}} onZoom={setZoom}/>
    <div className="review-stats">
      <div><strong>{num(counts.tasks)}</strong><span>tasks delivered</span></div>
      <div><strong>{num(counts.merged)}</strong><span>merged</span></div>
      <div><strong>{num(counts.checks_passed)}</strong><span>checks proven</span></div>
      <div><strong>{num(counts.checks_need_you)}</strong><span>need you</span></div>
      {num(counts.checks_failed)>0&&<div><strong>{num(counts.checks_failed)}</strong><span>failed</span></div>}
      <div><strong>{money(r.spent_usd)}</strong><span>of {money(r.budget_usd)}</span></div>
    </div>
    <div className="review-actions">
      <a className="button" href={`/api/v1/projects/${encodeURIComponent(id)}/review.md`} download>Download the report</a>
      <button onClick={onTalk}>Something wrong? Talk to the lead</button>
    </div>
    {allShots.length===0&&<p className="muted">No screenshots yet: this repository doesn't tell SwarmForge how to start the app. Ask the lead to "add a preview section to swarmforge.project.yml" and future runs will include light and dark screenshots.</p>}
    <details className="review-details">
      <summary>Details for engineers: every check, task, pull request and test result</summary>
      {inReview.length>0&&inReview.length<milestones.length&&<button onClick={()=>setShowAll(v=>!v)}>{showAll?'Show only milestones under review':'Show all milestones'}</button>}
      {shown.map(m=>{
        const checks=rows(m.checks),mine=tasks.filter(t=>num(t.milestone)===num(m.index));
        return <section className="review-milestone" key={num(m.index)}>
          <h3>Milestone {num(m.index)+1}: {str(m.name)} <span className="badge">{str(m.status).replace(/_/g,' ')}</span></h3>
          {str(m.goal)&&<p>{str(m.goal)}</p>}
          {str(m.summary)&&<details><summary>Lead's summary</summary><p>{str(m.summary)}</p></details>}
          {checks.length>0&&<><h4>Done when</h4><ul className="review-checks">{checks.map((c,i)=><li key={i}><span aria-hidden="true">{STATUS_ICON[str(c.status)]??'👤'}</span> {str(c.text)} <small className="muted">{str(c.status)||'needs you'}{Array.isArray(c.evidence)&&c.evidence.length?` · proved by ${c.evidence.map(str).join(', ')}`:''}</small></li>)}</ul></>}
          {mine.length>0&&<><h4>What was built</h4>{mine.map(t=>{
            const tests=t.tests?record(t.tests):null,acc=record(t.acceptance??{}),shots=Array.isArray(t.screenshots)?t.screenshots.map(str):[];
            const item=str(t.item_id),run=str(t.run_id);
            return <article className="review-task" key={item}>
              <h5>{str(t.title)}</h5>
              <p className="muted">{str(t.pr_url)?<a href={str(t.pr_url)} target="_blank" rel="noreferrer">Pull request{t.merged===true?' (merged)':''}</a>:'No pull request'}
                {' · '}{tests?`${num(tests.passed)} tests passed${num(tests.failed)?`, ${num(tests.failed)} failed`:''}`:'no test run recorded'}
                {Object.keys(acc).length>0&&` · acceptance: ${Object.entries(acc).map(([k,v])=>`${num(v)} ${k}`).join(', ')}`}
                {' · '}{money(t.cost_usd)}</p>
              {str(t.what_it_did)&&<p>{str(t.what_it_did)}</p>}
              {shots.length>0&&<div className="review-shots">{shots.map(name=>{const src=shotUrl(item,run,name);
                return <button key={name} className="shot" onClick={()=>setZoom({src,title:str(t.title),caption:prettyName(name)})}><img src={src} alt={name} loading="lazy"/><small>{prettyName(name)}</small></button>;})}</div>}
            </article>;})}</>}
        </section>;
      })}
    </details>
    {zoom&&<div className="shot-zoom" role="dialog" aria-label={zoom.caption} onClick={()=>setZoom(null)}><figure><img src={zoom.src} alt={zoom.caption}/><figcaption>{zoom.caption} — {zoom.title} · click to close</figcaption></figure></div>}
  </div>;
}
