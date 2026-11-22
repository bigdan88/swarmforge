import type {ReactNode} from 'react';
import {useAction,type Row,type Result} from './api';
export type Navigate=(page:string,id?:string)=>void;
export function Feedback({error,message}:{error?:string;message?:string}){return <>{error&&<p role="alert" className="error">{error}</p>}{message&&<p role="status" className="success">{message}</p>}</>;}
export function Loading({error}:{error:string}){return error?<Feedback error={error}/>:<p className="empty" role="status">Loading…</p>;}
export function Heading({title,description,children}:{title:string;description:string;children?:ReactNode}){return <header><div><div className="eyebrow">SWARMFORGE WORKSPACE</div><h1>{title}</h1><p>{description}</p></div>{children}</header>;}
export function ActionButton({operation,target='',args={},disabled=false,children,onDone}:{operation:string;target?:string;args?:Row;disabled?:boolean;children:ReactNode;onDone?:(result:Result)=>void}){
  const action=useAction(`${operation}:${target}`);
  return <span className="action-control"><button disabled={disabled||action.pending} onClick={async()=>{const r=await action.send(operation,target,args);if(r)onDone?.(r);}}>{action.pending?'Working…':children}</button><Feedback error={action.error} message={action.message}/></span>;
}
export function Pager({offset,total,setOffset}:{offset:number;total:number;setOffset:(n:number)=>void}){return <footer><span>{total} {total===1?'item':'items'}</span><div className="pager"><button disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-25))}>Previous</button><button disabled={offset+25>=total||offset+25>100000} onClick={()=>setOffset(offset+25)}>Next</button></div></footer>;}
