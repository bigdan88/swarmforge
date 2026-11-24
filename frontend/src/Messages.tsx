import {useEffect,useLayoutEffect,useRef,useState,type ReactNode} from 'react';
import {num,record,rows,stamp,str,type Row} from './api';

export async function copyText(text:string){
  if(!navigator.clipboard?.writeText)throw new Error('Clipboard access is unavailable. Select the text to copy it.');
  await navigator.clipboard.writeText(text);
}
export function downloadName(name:string){return name.split(/[\\/]/).at(-1)?.replace(/[\x00-\x1f<>:"|?*]/g,'_').slice(0,160)||'download.txt';}
export function downloadText(text:string,name:string){
  const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download=downloadName(name);link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function CopyButton({text,label='Copy'}:{text:string;label?:string}){
  const [copied,setCopied]=useState(false),[error,setError]=useState('');const timer=useRef<ReturnType<typeof setTimeout>|null>(null);
  useEffect(()=>()=>{if(timer.current)clearTimeout(timer.current);},[]);
  return <><button type="button" className="copy-button" aria-label={label} onClick={async()=>{try{await copyText(text);setCopied(true);setError('');if(timer.current)clearTimeout(timer.current);timer.current=setTimeout(()=>setCopied(false),1800);}catch(e){setError(e instanceof Error?e.message:'Copy failed');}}}>{copied?'Copied!':label}</button>{error&&<span className="copy-error" role="status">{error}</span>}</>;
}
export function CodeBlock({code,info='',closed=true}:{code:string;info?:string;closed?:boolean}){
  const file=info.startsWith('file:')?downloadName(info.slice(5).trim()):'';
  const language=file?(file.split('.').at(-1)||'text'):(info.split(/\s/)[0]||'text');
  return <div className="code-block"><div className="code-toolbar"><span>{file||language}</span><div><CopyButton text={code} label="Copy code"/>{closed&&<button type="button" onClick={()=>downloadText(code,file||`snippet.${({python:'py',javascript:'js',typescript:'ts',bash:'sh',shell:'sh',text:'txt'} as Record<string,string>)[language]||language}`)}>{file?'Download file':'Download'}</button>}</div></div><pre className="md-code"><code data-language={language}>{code}</code></pre>{file&&!closed&&<small className="muted">Writing file…</small>}</div>;
}
export function safeLink(url:string,image=false){
  if(/^\/api\/v1\/(?:conversations\/[^/]+\/(?:attachments|files)\/[a-f0-9]{32}|work\/[^/]+\/artifacts\/)/.test(url))return url;
  if(image&&/^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$/.test(url))return url;
  try{const u=new URL(url);return (image?u.protocol==='https:':['http:','https:'].includes(u.protocol))?url:'';}catch{return '';}
}
function InlineImage({src,alt}:{src:string;alt:string}){
  const [failed,setFailed]=useState(false);const url=safeLink(src,true);
  if(!url)return <span>{alt}</span>;
  return failed?<a href={url} target="_blank" rel="noreferrer">{alt||'Open image'}</a>:<a className="chat-image" href={url} target="_blank" rel="noreferrer"><img src={url} alt={alt||'Image'} loading="lazy" referrerPolicy="no-referrer" onError={()=>setFailed(true)}/></a>;
}

export function MessageFiles({conversation,items}:{conversation:string;items:Row[]}){
  if(!items.length)return null;
  return <div className="message-files">{items.map(f=>{const url=`/api/v1/conversations/${encodeURIComponent(conversation)}/files/${encodeURIComponent(str(f.id))}`;return <div className="output-file" key={str(f.id)}>{f.kind==='image'&&<InlineImage src={`${url}?inline=true`} alt={str(f.name)}/>}<a className="file-download" href={url} download={str(f.name)}><span className="file-icon" aria-hidden="true">↓</span><span><strong>{str(f.name)}</strong><small>{num(f.size)<1024?`${num(f.size)} B`:`${Math.ceil(num(f.size)/1024)} KB`} · Download</small></span></a></div>;})}</div>;
}
export function replyIsSaved(messages:Row[],request:Row|null){return !!request&&messages.some(m=>m.id===request.assistant_message_id||(m.role==='assistant'&&num(request.started_at)>0&&num(m.created_at)>=num(request.started_at)));}
export function ChatMessage({message,conversation='',streaming=false,children}:{message:Row;conversation?:string;streaming?:boolean;children?:ReactNode}){
  const role=str(message.role),user=role==='user',text=str(message.content);
  return <article className={`message ${user?'user':'assistant'}${streaming?' streaming':''}`} aria-label={user?'Your message':streaming?'Assistant reply in progress':'Assistant message'}>
    <div className="message-meta"><strong>{user?'You':'SwarmForge'}</strong>{!!message.created_at&&<time title={stamp(message.created_at)}>{stamp(message.created_at)}</time>}{streaming&&<span className="stream-indicator" aria-label="Generating"/>}</div>
    <div className="message-content">{user?<p>{text}</p>:<Markdown text={text}/>}</div>
    <MessageFiles conversation={conversation} items={rows(message.files)}/>{children}
    {message.truncated===true&&<small className="muted">Long message shortened in this view.</small>}
    {!streaming&&text&&<div className="message-actions"><CopyButton text={text} label="Copy message"/>{!user&&<button type="button" onClick={()=>downloadText(text,'response.md')}>Save as Markdown</button>}</div>}
  </article>;
}

/* Safe Markdown for chat messages: builds React elements only (never raw HTML).
   Supports fenced code, headings, bullet/numbered lists, quotes, paragraphs,
   inline code, bold, italics and http(s) links. */
function inline(text:string,key:string):ReactNode[]{
  const out:ReactNode[]=[];const pattern=/(!\[[^\]]*\]\([^\s)]+\))|(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^\s)]+\))|(\*[^*\s][^*]*\*)/g;
  let last=0,m:RegExpExecArray|null,i=0;
  while((m=pattern.exec(text))){
    if(m.index>last)out.push(text.slice(last,m.index));
    const t=m[0],k=`${key}-${i++}`;
    if(m[1])out.push(<InlineImage key={k} alt={t.slice(2,t.indexOf(']('))} src={t.slice(t.indexOf('](')+2,-1)}/>);
    else if(m[2])out.push(<code key={k}>{t.slice(1,-1)}</code>);
    else if(m[3])out.push(<strong key={k}>{t.slice(2,-2)}</strong>);
    else if(m[4]){const label=t.slice(1,t.indexOf(']('));const href=safeLink(t.slice(t.indexOf('](')+2,-1));out.push(href?<a key={k} href={href} target="_blank" rel="noreferrer">{label}</a>:<span key={k}>{label}</span>);}
    else out.push(<em key={k}>{t.slice(1,-1)}</em>);
    last=m.index+t.length;
  }
  if(last<text.length)out.push(text.slice(last));
  return out;
}
export function Markdown({text}:{text:string}){
  const lines=text.replace(/\r\n/g,'\n').split('\n');const blocks:ReactNode[]=[];let i=0,n=0;
  while(i<lines.length){
    const line=lines[i];const key=`b${n++}`;
    if(/^\s*```/.test(line)){const info=line.trim().slice(3).trim();const body:string[]=[];i++;while(i<lines.length&&!/^\s*```/.test(lines[i]))body.push(lines[i++]);const closed=i<lines.length;i++;if(info!=='actions')blocks.push(<CodeBlock key={key} code={body.join('\n')} info={info} closed={closed}/>);continue;}
    if(/^\s*(?:---+|\*\*\*+)\s*$/.test(line)){blocks.push(<hr key={key}/>);i++;continue;}
    if(i+1<lines.length&&line.includes('|')&&/^\s*\|?\s*:?-{3,}/.test(lines[i+1])){
      const cells=(s:string)=>s.trim().replace(/^\||\|$/g,'').split('|').map(s=>s.trim());const head=cells(line),body:string[][]=[];i+=2;
      while(i<lines.length&&lines[i].trim()&&lines[i].includes('|'))body.push(cells(lines[i++]));
      blocks.push(<div className="table-scroll" key={key}><table><thead><tr>{head.map((s,j)=><th key={j}>{inline(s,`${key}h${j}`)}</th>)}</tr></thead><tbody>{body.map((r,j)=><tr key={j}>{head.map((_,c)=><td key={c}>{inline(r[c]||'',`${key}-${j}-${c}`)}</td>)}</tr>)}</tbody></table></div>);continue;
    }
    const h=/^(#{1,4})\s+(.*)$/.exec(line);
    if(h){blocks.push(<p key={key} className={`md-h md-h${h[1].length}`}><strong>{inline(h[2],key)}</strong></p>);i++;continue;}
    if(/^\s*([-*+]|\d+[.)])\s+/.test(line)){
      const ordered=/^\s*\d+[.)]\s+/.test(line);const items:ReactNode[]=[];
      while(i<lines.length&&/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])&&ordered===/^\s*\d+[.)]\s+/.test(lines[i])){items.push(<li key={i}>{inline(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/,''),`${key}-${i}`)}</li>);i++;}
      blocks.push(ordered?<ol key={key}>{items}</ol>:<ul key={key}>{items}</ul>);continue;
    }
    if(/^>\s?/.test(line)){const body:string[]=[];while(i<lines.length&&/^>\s?/.test(lines[i]))body.push(lines[i++].replace(/^>\s?/,''));blocks.push(<blockquote key={key}>{inline(body.join(' '),key)}</blockquote>);continue;}
    if(!line.trim()){i++;continue;}
    const para:string[]=[];
    while(i<lines.length&&lines[i].trim()&&!/^```|^#{1,4}\s|^\s*([-*+]|\d+[.)])\s+|^>\s?/.test(lines[i]))para.push(lines[i++]);
    blocks.push(<p key={key}>{para.flatMap((l,j)=>j?[<br key={`${key}br${j}`}/>,...inline(l,`${key}-${j}`)]:inline(l,`${key}-${j}`))}</p>);
  }
  return <div className="md">{blocks}</div>;
}

/** A scrolling transcript that opens at the newest message and stays pinned to it as messages
 *  arrive, unless the reader has scrolled up (then a "new messages" button appears). */
export function Transcript({count,children,label='Conversation history'}:{count:number;children:ReactNode;label?:string}){
  const box=useRef<HTMLDivElement>(null);const pinned=useRef(true);const seen=useRef(0);
  const [unseen,setUnseen]=useState(false);
  useLayoutEffect(()=>{
    const el=box.current;if(!el)return;
    if(pinned.current||seen.current===0){el.scrollTop=el.scrollHeight;setUnseen(false);}
    else if(count>seen.current)setUnseen(true);
    seen.current=count;
  });
  // Re-pin when the box or its content changes size (panel fitted to the window, images loading).
  useLayoutEffect(()=>{
    const el=box.current;if(!el||typeof ResizeObserver==='undefined')return;
    const observer=new ResizeObserver(()=>{if(pinned.current)el.scrollTop=el.scrollHeight;});
    observer.observe(el);for(const child of Array.from(el.children))observer.observe(child);
    return()=>observer.disconnect();
  });
  function onScroll(){const el=box.current;if(!el)return;pinned.current=el.scrollHeight-el.scrollTop-el.clientHeight<48;if(pinned.current)setUnseen(false);}
  return <div className="transcript-wrap"><div className="transcript" ref={box} onScroll={onScroll} aria-label={label} role="log" tabIndex={0}>{children}</div>
    {unseen&&<button className="jump-latest" onClick={()=>{const el=box.current;if(el){el.scrollTop=el.scrollHeight;pinned.current=true;setUnseen(false);}}}>New messages ↓</button>}</div>;
}

export function useLiveReply(conversation:string,request:Row|null,onDone:()=>void){
  const [live,setLive]=useState<Row|null>(null),[reconnecting,setReconnecting]=useState(false);
  const callback=useRef(onDone);callback.current=onDone;
  const id=str(request?.id),thinking=request?.status==='thinking';
  useEffect(()=>{
    setLive(null);setReconnecting(false);
    if(!id||!thinking||typeof EventSource==='undefined')return;
    const source=new EventSource(`/api/v1/conversations/${encodeURIComponent(conversation)}/replies/${encodeURIComponent(id)}/events`);
    source.onopen=()=>setReconnecting(false);
    source.onerror=()=>setReconnecting(true);
    source.onmessage=e=>{try{const next=record(JSON.parse(e.data));if(next.id!==id)return;setLive(next);setReconnecting(false);if(next.status!=='thinking'){source.close();callback.current();}}catch{/* A malformed event cannot erase the retained reply. */}};
    return()=>source.close();
  },[conversation,id,thinking]);
  return {request:request?{...request,...(live?.id===id&&thinking&&!reconnecting?live:{})}:null,reconnecting};
}

/** Size a panel to the window so its last child (the composer) is always visible.
 *  Desktop: fill the space below the panel's top. Phone: fill the screen and scroll to it. */
export function useFitToWindow<T extends HTMLElement>(active:boolean){
  const ref=useRef<T>(null);
  useLayoutEffect(()=>{
    const el=ref.current;if(!el||!active)return;
    const phone=()=>window.matchMedia('(max-width: 900px)').matches;
    const fit=()=>{
      if(phone()){el.style.height=`${Math.max(380,window.innerHeight-16)}px`;return;}
      const top=el.getBoundingClientRect().top+window.scrollY;
      el.style.height=`${Math.max(420,window.innerHeight-top-24)}px`;
    };
    fit();
    if(phone())el.scrollIntoView({block:'start'});
    window.addEventListener('resize',fit);
    return()=>{window.removeEventListener('resize',fit);el.style.height='';};
  },[active]);
  return ref;
}
