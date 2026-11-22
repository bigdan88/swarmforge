import {useEffect,useState} from 'react';
import {parseSession,readLocal,saveLocal,useGet} from './api';
import {WorkPage} from './Work';
import {ProjectsPage} from './Projects';
import {ChatPage} from './Chat';
import {NewTask} from './Forms';
import {Benchmarks,SettingsPage,WhatsNew,Knowledge} from './Catalog';
import {Feedback,Heading} from './ui';

const navigation=[['work','Work'],['new-task','New task'],['projects','Projects'],['chat','Chat'],['prs','Pull requests'],['failures','Failures'],['history','History'],['benchmarks','Outcomes'],['knowledge','Knowledge'],['teams','Model teams'],['settings','Settings'],['whats-new',"What's new"]];
function currentRoute(){const p=new URLSearchParams(location.search);return {page:p.get('page')||'work',selected:p.get('item')||p.get('project')||p.get('conversation')||''};}
// Follow the operating-system preference until the user explicitly picks a theme.
const systemTheme=()=>{try{return window.matchMedia?.('(prefers-color-scheme: light)').matches?'light':'dark';}catch{return 'dark';}};
export function App(){
  const [generation,setGeneration]=useState(0);const [storageOK,setStorageOK]=useState(true);
  const [route,setRoute]=useState(currentRoute),[theme,setTheme]=useState(()=>readLocal('sf-theme',systemTheme()));
  const session=useGet('/api/v1/session',parseSession);
  useEffect(()=>{const failed=()=>setStorageOK(false);window.addEventListener('sf-storage-unavailable',failed);setStorageOK(saveLocal('sf-storage-check','1'));return()=>window.removeEventListener('sf-storage-unavailable',failed);},[]);
  useEffect(()=>{const changed=()=>{setGeneration(n=>n+1);setRoute(currentRoute());};window.addEventListener('popstate',changed);return()=>window.removeEventListener('popstate',changed);},[]);
  useEffect(()=>{document.documentElement.dataset.theme=theme;},[theme]);
  function navigate(page:string,id=''){const params=new URLSearchParams({page});if(id)params.set(page==='projects'?'project':page==='chat'?'conversation':'item',id);history.pushState(null,'',`?${params}`);setGeneration(n=>n+1);setRoute(currentRoute());window.scrollTo({top:0});}
  let content;
  if(['work','prs','failures','history'].includes(route.page))content=<WorkPage key={route.page+':'+generation} view={route.page} session={session.data} navigate={navigate}/>;
  else if(route.page==='new-task')content=<NewTask session={session.data} navigate={navigate}/>;
  else if(route.page==='projects')content=<ProjectsPage session={session.data} navigate={navigate} selected={route.selected}/>;
  else if(route.page==='chat')content=<ChatPage session={session.data} navigate={navigate} selected={route.selected}/>;
  else if(route.page==='settings'||route.page==='teams')content=<SettingsPage session={session.data} teams={route.page==='teams'}/>;
  else if(route.page==='whats-new')content=<WhatsNew/>;
  else if(route.page==='benchmarks')content=<Benchmarks/>;
  else if(route.page==='knowledge')content=<Knowledge navigate={navigate} selected={route.selected}/>;
  else content=<><Heading title="Page unavailable" description="Choose a workspace page from the navigation."/><button onClick={()=>navigate('work')}>Open Work</button></>;
  return <div className="shell"><aside className="sidebar"><a className="brand" href="?page=work" onClick={e=>{e.preventDefault();navigate('work');}}><img src="/brand/banner.png" alt="SwarmForge"/></a><div className="workspace">ENGINEERING WORKSPACE</div><nav aria-label="Workspace">{navigation.map(([id,name])=><a key={id} href={`?page=${id}`} className={route.page===id?'active':''} aria-current={route.page===id?'page':undefined} onClick={e=>{if(!e.ctrlKey&&!e.metaKey&&!e.shiftKey){e.preventDefault();navigate(id);}}}>{name}</a>)}</nav><span className="mode-label">{session.data?.demo_mode?'Interactive demo':session.data?.actions_enabled?'Local controls enabled':'Browsing mode'} · Preview</span><button className="theme" onClick={()=>{const next=theme==='dark'?'light':'dark';saveLocal('sf-theme',next);setTheme(next);}}>{theme==='dark'?'Light theme':'Dark theme'}</button></aside><main>{!storageOK&&<p className="error" role="alert">Browser storage is unavailable. Copy unsaved text before navigating; drafts cannot survive a reload.</p>}{session.data?.demo_mode&&<div className="demo-banner" role="status">Interactive demo · Sample data, simulated model replies and no real workers. Your real workspace is untouched.</div>}<Feedback error={session.error}/>{content}</main></div>;
}
