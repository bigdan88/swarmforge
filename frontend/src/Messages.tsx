import {useLayoutEffect,useRef,useState,type ReactNode} from 'react';

/* Safe Markdown for chat messages: builds React elements only (never raw HTML).
   Supports fenced code, headings, bullet/numbered lists, quotes, paragraphs,
   inline code, bold, italics and http(s) links. */
function inline(text:string,key:string):ReactNode[]{
  const out:ReactNode[]=[];const pattern=/(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\(https?:\/\/[^\s)]+\))|(\*[^*\s][^*]*\*)/g;
  let last=0,m:RegExpExecArray|null,i=0;
  while((m=pattern.exec(text))){
    if(m.index>last)out.push(text.slice(last,m.index));
    const t=m[0],k=`${key}-${i++}`;
    if(m[1])out.push(<code key={k}>{t.slice(1,-1)}</code>);
    else if(m[2])out.push(<strong key={k}>{t.slice(2,-2)}</strong>);
    else if(m[3]){const label=t.slice(1,t.indexOf(']('));const href=t.slice(t.indexOf('](')+2,-1);out.push(<a key={k} href={href} target="_blank" rel="noreferrer">{label}</a>);}
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
    if(/^```/.test(line)){const body:string[]=[];i++;while(i<lines.length&&!/^```/.test(lines[i]))body.push(lines[i++]);i++;blocks.push(<pre key={key} className="md-code"><code>{body.join('\n')}</code></pre>);continue;}
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
