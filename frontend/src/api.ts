import {useEffect,useRef,useState} from 'react';

export type Row=Record<string,unknown>;
export type Collection={items:Row[];total:number;limit:number;offset:number};
export type Session={actions_enabled:boolean;models_enabled:boolean;csrf_token:string|null;settings:Settings;demo_mode?:boolean};
export type Settings={configured:boolean;team:string|null;roles:{role:string;model:string;fallbacks:string[]}[];limits:Record<string,number>;teams:{name:string;description:string;models:Record<string,string>}[]};
export type Result={ok:boolean;message:string;item_id?:string|null;project_id?:string|null;conversation_id?:string|null};
export const isRow=(value:unknown):value is Row=>!!value&&typeof value==='object'&&!Array.isArray(value);
export const str=(value:unknown)=>typeof value==='string'?value:'';
export const num=(value:unknown)=>typeof value==='number'&&Number.isFinite(value)?value:0;
export const rows=(value:unknown):Row[]=>Array.isArray(value)?value.filter(isRow):[];
export const record=(value:unknown):Row=>{if(!isRow(value))throw new Error('The local service returned an incompatible response.');return value;};
export function parseSession(value:unknown):Session{
  const v=record(value),s=record(v.settings);
  if(typeof v.actions_enabled!=='boolean'||typeof v.models_enabled!=='boolean'||
    !(v.csrf_token===null||typeof v.csrf_token==='string')||typeof s.configured!=='boolean'||
    !(s.team===null||typeof s.team==='string')||!Array.isArray(s.roles)||!Array.isArray(s.teams)||!isRow(s.limits)||
    !s.roles.every(r=>isRow(r)&&typeof r.role==='string'&&typeof r.model==='string'&&Array.isArray(r.fallbacks)&&r.fallbacks.every(f=>typeof f==='string'))||
    !s.teams.every(t=>isRow(t)&&typeof t.name==='string'&&typeof t.description==='string'&&isRow(t.models)&&Object.values(t.models).every(m=>typeof m==='string')))throw new Error('The local service returned incompatible workspace settings.');
  return v as Session;
}
export const collection=(value:unknown):Collection=>{
  const v=record(value);
  if(!Array.isArray(v.items)||!v.items.every(isRow)||typeof v.total!=='number'||!Number.isInteger(v.total)||v.total<0)throw new Error('The local service returned an incompatible list.');
  return v as Collection;
};
export const label=(value:unknown)=>str(value).replaceAll('_',' ');
export const money=(value:unknown)=>`$${num(value).toFixed(2)}`;
export const stamp=(value:unknown)=>typeof value==='number'?new Date(value*1000).toLocaleString():'Not recorded';
export function readLocal(key:string,fallback:string){try{return localStorage.getItem(key)??fallback;}catch{return fallback;}}
export function saveLocal(key:string,value:string){try{localStorage.setItem(key,value);return true;}catch{if(typeof window!=='undefined')window.dispatchEvent(new Event('sf-storage-unavailable'));return false;}}
export function useDraft(key:string,initial:Record<string,string>){
  const [value,setValue]=useState(()=>{try{const saved=JSON.parse(readLocal(key,'{}'));return Object.fromEntries(Object.entries(initial).map(([k,v])=>[k,typeof saved?.[k]==='string'?saved[k]:v]));}catch{return initial;}});
  useEffect(()=>{saveLocal(key,JSON.stringify(value));},[key,value]);
  return [value,setValue] as const;
}
export async function getJSON(url:string,signal?:AbortSignal):Promise<unknown>{
  const response=await fetch(url,{signal,credentials:'same-origin'});
  if(!response.ok)throw new Error(response.status===404?'This item is no longer available.':'Could not load this view. Check the local service and try again.');
  return response.json();
}
export function useGet<T>(url:string,parse:(value:unknown)=>T,poll=0){
  const [state,setState]=useState<{url:string;data:T|null;error:string}>({url,data:null,error:''});
  const [revision,setRevision]=useState(0);
  useEffect(()=>{
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
    async function load(){
      try{const data=parse(await getJSON(url,controller.signal));if(!controller.signal.aborted)setState({url,data,error:''});}
      catch(e){if(!controller.signal.aborted)setState(old=>({url,data:old.url===url?old.data:null,error:e instanceof Error?e.message:'Refresh failed'}));}
      finally{if(poll&&!controller.signal.aborted)timer=setTimeout(load,poll);}
    }
    void load();return()=>{controller.abort();clearTimeout(timer);};
  },[url,parse,poll,revision]);
  return {...(state.url===url?state:{data:null,error:''}),refresh:()=>setRevision(n=>n+1)};
}
export async function performAction(operation:string,target:string,args:Row,key:string):Promise<Result>{
  const session=record(await getJSON('/api/v1/session'));
  if(!session.actions_enabled||typeof session.csrf_token!=='string')throw new Error('Controls are disabled. Start the web interface with --allow-actions to enable them.');
  const response=await fetch('/api/v1/actions',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf_token,'Idempotency-Key':key},body:JSON.stringify({operation,target,args})});
  const value=record(await response.json());
  if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:'The request was rejected. Review the form and try again.');
  if(typeof value.ok!=='boolean'||typeof value.message!=='string')throw new Error('The action response was incompatible. Inspect current state before retrying.');
  return value as Result;
}
export function useAction(scope:string){
  const [pending,setPending]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  const busy=useRef(false);
  const retained=useRef<{body:string;key:string}|null>(null);
  async function send(operation:string,target:string,args:Row={}){
    if(busy.current)return null;
    busy.current=true;setPending(true);setError('');setMessage('');
    const storageKey=`sf-request:${scope}`;const body=JSON.stringify([operation,target,args]);
    let key=retained.current?.body===body?retained.current.key:crypto.randomUUID();
    try{const saved=JSON.parse(readLocal(storageKey,'{}'));if(saved.body===body&&typeof saved.key==='string')key=saved.key;}catch{/* New request. */}
    saveLocal(storageKey,JSON.stringify({body,key}));
    retained.current={body,key};
    try{const result=await performAction(operation,target,args,key);if(!result.ok){retained.current=null;saveLocal(storageKey,'{}');throw new Error(result.message);}retained.current=null;saveLocal(storageKey,'{}');setMessage(result.message);return result;}
    catch(e){setError(e instanceof Error?e.message:'Action failed. Inspect current state before retrying.');return null;}
    finally{busy.current=false;setPending(false);}
  }
  return {pending,error,message,send};
}
