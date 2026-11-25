import {useEffect,useState} from 'react';

const entrySource=(doc:Document)=>doc.querySelector<HTMLScriptElement>('script[type="module"][src]')?.getAttribute('src')??'';

/** Notice a new hashed entry file without discarding an open form or interrupting work. */
export function useDashboardUpdate(){
  const [available,setAvailable]=useState(false);
  useEffect(()=>{
    const loaded=entrySource(document);
    if(!loaded)return;
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
    async function check(){
      try{
        const response=await fetch('/',{cache:'no-store',credentials:'same-origin',signal:controller.signal});
        if(response.ok){
          const current=entrySource(new DOMParser().parseFromString(await response.text(),'text/html'));
          if(current&&current!==loaded&&!controller.signal.aborted)setAvailable(true);
        }
      }catch{/* The normal connection banner reports a service outage. */}
      finally{if(!controller.signal.aborted)timer=setTimeout(check,30000);}
    }
    void check();return()=>{controller.abort();clearTimeout(timer);};
  },[]);
  return available;
}

export function DashboardUpdate({available}:{available:boolean}){
  if(!available)return null;
  return <div className="notice dashboard-update" role="status"><strong>Dashboard update available</strong><p>Reload to get the latest status messages and controls. Running projects continue in the background. Copy any unsent text before reloading.</p><button onClick={()=>location.reload()}>Reload dashboard</button></div>;
}
