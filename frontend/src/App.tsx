import {useEffect,useState} from 'react';
import {WORKSPACE_CHANGED,bindWorkspace,boundWorkspace,noteInstance,num,parseSession,readLocal,record,saveLocal,useGet} from './api';
import {WorkPage} from './Work';
import {ProjectsPage} from './Projects';
import {ChatPage} from './Chat';
import {NewTask} from './Forms';
import {SettingsPage} from './Catalog';
import {BenchmarksPage,SettingsEditor,SpeedPage} from './Admin';
import {FailuresPage,Knowledge,PullRequestsPage,WhatsNew} from './Secondary';
import {Feedback,Heading} from './ui';
import {DashboardUpdate,useDashboardUpdate} from './DashboardUpdate';

const navigation=[['work','Work'],['new-task','New task'],['projects','Projects'],['chat','Chat'],['prs','Pull requests'],['failures','Failures'],['history','History'],['speed','Speed'],['benchmarks','Benchmarks'],['knowledge','Knowledge'],['teams','Model teams'],['settings','Settings'],['whats-new',"What's new"]];
function currentRoute(){const p=new URLSearchParams(location.search);return {page:p.get('page')||'work',selected:p.get('item')||p.get('project')||p.get('conversation')||''};}
// Follow the operating-system preference until the user explicitly picks a theme.
const systemTheme=()=>{try{return window.matchMedia?.('(prefers-color-scheme: light)').matches?'light':'dark';}catch{return 'dark';}};
export function App(){
  const updateAvailable=useDashboardUpdate();
  const [generation,setGeneration]=useState(0);const [storageOK,setStorageOK]=useState(true);
  const [route,setRoute]=useState(currentRoute),[theme,setTheme]=useState(()=>readLocal('sf-theme',systemTheme()));
  // Re-read the session periodically to notice a server restart or a different workspace.
  const session=useGet('/api/v1/session',parseSession,15000);
  if(session.data)bindWorkspace(session.data);  // before any page reads its drafts
  const [stale,setStale]=useState(false),[restarted,setRestarted]=useState(false);
  useEffect(()=>{const b=boundWorkspace(),d=session.data;if(!b||!d)return;
    if(d.workspace_id!==b.workspace)setStale(true);
    else if(d.instance_id&&d.instance_id!==b.instance){noteInstance(d.instance_id);setRestarted(true);}},[session.data]);
  useEffect(()=>{const mark=()=>setStale(true);window.addEventListener(WORKSPACE_CHANGED,mark);return()=>window.removeEventListener(WORKSPACE_CHANGED,mark);},[]);
  const badges=useGet('/api/v1/badges',record,30000);
  useEffect(()=>{const update=()=>badges.refresh();window.addEventListener('sf-badges',update);return()=>window.removeEventListener('sf-badges',update);},[badges.refresh]);
  const badge:Record<string,string>={failures:num(badges.data?.failures_need_you)?`${num(badges.data?.failures_need_you)} need you`:'', 'whats-new':num(badges.data?.features_unread)?`${num(badges.data?.features_unread)} new`:''};
  useEffect(()=>{const failed=()=>setStorageOK(false);window.addEventListener('sf-storage-unavailable',failed);setStorageOK(saveLocal('sf-storage-check','1'));return()=>window.removeEventListener('sf-storage-unavailable',failed);},[]);
  useEffect(()=>{const changed=()=>{setGeneration(n=>n+1);setRoute(currentRoute());};window.addEventListener('popstate',changed);return()=>window.removeEventListener('popstate',changed);},[]);
  useEffect(()=>{document.documentElement.dataset.theme=theme;},[theme]);
  useEffect(()=>{const name=navigation.find(([id])=>id===route.page)?.[1]??'SwarmForge';document.title=`${name} · SwarmForge`;},[route.page]);
  function navigate(page:string,id=''){const params=new URLSearchParams({page});if(id)params.set(page==='projects'?'project':page==='chat'?'conversation':'item',id);history.pushState(null,'',`?${params}`);setGeneration(n=>n+1);setRoute(currentRoute());window.scrollTo({top:0});}
  let content;
  if(!boundWorkspace()&&!session.error)content=<p className="empty" role="status">Connecting to SwarmForge…</p>;
  else   if(['work','history'].includes(route.page))content=<WorkPage key={route.page+':'+generation} view={route.page} session={session.data} navigate={navigate}/>;
  else if(route.page==='failures')content=<FailuresPage session={session.data} navigate={navigate} selected={route.selected}/>;
  else if(route.page==='prs')content=<PullRequestsPage session={session.data} navigate={navigate}/>;
  else if(route.page==='new-task')content=<NewTask session={session.data} navigate={navigate}/>;
  else if(route.page==='projects')content=<ProjectsPage session={session.data} navigate={navigate} selected={route.selected}/>;
  else if(route.page==='chat')content=<ChatPage session={session.data} navigate={navigate} selected={route.selected}/>;
  else if(route.page==='teams')content=<SettingsPage session={session.data} teams/>;
  else if(route.page==='settings')content=<><Heading title="Settings" description="Health checks, models and limits. Credentials are never shown."/><SettingsEditor session={session.data}/></>;
  else if(route.page==='speed')content=<SpeedPage/>;
  else if(route.page==='whats-new')content=<WhatsNew navigate={navigate} session={session.data}/>;
  else if(route.page==='benchmarks')content=<BenchmarksPage/>;
  else if(route.page==='knowledge')content=<Knowledge navigate={navigate} selected={route.selected} session={session.data}/>;
  else content=<><Heading title="Page unavailable" description="Choose a workspace page from the navigation."/><button onClick={()=>navigate('work')}>Open Work</button></>;
  return <div className="shell"><aside className="sidebar"><a className="brand" href="?page=work" onClick={e=>{e.preventDefault();navigate('work');}}><img src="/brand/banner.png" alt="SwarmForge"/></a><div className="workspace">ENGINEERING WORKSPACE</div><nav aria-label="Workspace">{navigation.map(([id,name])=><a key={id} href={`?page=${id}`} className={route.page===id?'active':''} aria-current={route.page===id?'page':undefined} onClick={e=>{if(!e.ctrlKey&&!e.metaKey&&!e.shiftKey){e.preventDefault();navigate(id);}}}>{name}{badge[id]&&<span className="nav-badge">{badge[id]}</span>}</a>)}</nav><span className="mode-label">{session.data?.demo_mode?'Interactive demo':session.data?.actions_enabled?'Local controls enabled':'Browsing mode'} · Preview</span><button className="theme" onClick={()=>{const next=theme==='dark'?'light':'dark';saveLocal('sf-theme',next);setTheme(next);}}>{theme==='dark'?'Light theme':'Dark theme'}</button></aside><main><DashboardUpdate available={updateAvailable}/>{stale&&<div className="workspace-banner" role="alert"><strong>This page is out of date.</strong> SwarmForge here is now serving a different workspace, so actions from this tab are blocked. Reload to review the current workspace.<button className="primary" onClick={()=>location.reload()}>Reload</button></div>}{restarted&&!stale&&<div className="notice" role="status">The SwarmForge service restarted. Your drafts are kept.<button onClick={()=>setRestarted(false)}>Dismiss</button></div>}{!storageOK&&<p className="error" role="alert">Browser storage is unavailable. Copy unsaved text before navigating; drafts cannot survive a reload.</p>}{session.data?.demo_mode&&<div className="demo-banner" role="status">Interactive demo · Sample data, simulated model replies and no real workers. Your real workspace is untouched.</div>}<Feedback error={session.error}/>{content}</main></div>;
}
