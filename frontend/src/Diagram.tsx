import {useId} from 'react';

export type DiagramNode={id:string;label:string;kind:string;note?:string;where?:string};
export type DiagramEdge={from:string;to:string;label?:string};

const KIND_LABEL:Record<string,string>={user:'You',ui:'Screens',service:'Code / service',data:'Stored data',external:'Outside service',device:'Device',ai:'AI model'};

/** Splits text into lines of at most `max` characters (long words are cut). */
export function wrap(text:string,max:number,limit=4):string[]{
  const lines:string[]=[];let line='';
  for(const word of text.split(/\s+/).filter(Boolean)){
    const w=word.length>max?word.slice(0,max-1)+'…':word;
    if(!line)line=w;else if((line+' '+w).length<=max)line+=' '+w;else{lines.push(line);line=w;}
  }
  if(line)lines.push(line);
  if(lines.length>limit){const kept=lines.slice(0,limit);kept[limit-1]=kept[limit-1].replace(/.?$/,'…');return kept;}
  return lines;
}

/** Rows of a layered top-down layout: longest path from the starting boxes, back edges ignored. */
export function layers(nodes:DiagramNode[],edges:DiagramEdge[]):{rank:Map<string,number>;back:Set<number>}{
  const ids=new Set(nodes.map(n=>n.id)),out=new Map<string,number[]>(),back=new Set<number>();
  edges.forEach((e,i)=>{if(ids.has(e.from)&&ids.has(e.to))out.set(e.from,[...(out.get(e.from)??[]),i]);});
  const state=new Map<string,number>();// 1 = on the current path, 2 = finished
  const visit=(id:string)=>{state.set(id,1);for(const i of out.get(id)??[]){const to=edges[i].to;if(state.get(to)===1)back.add(i);else if(!state.has(to))visit(to);}state.set(id,2);};
  const incoming=new Set(edges.map(e=>e.to));
  for(const n of nodes)if(!incoming.has(n.id)&&!state.has(n.id))visit(n.id);
  for(const n of nodes)if(!state.has(n.id))visit(n.id);
  const rank=new Map(nodes.map(n=>[n.id,0]));
  for(let pass=0;pass<nodes.length;pass++){let changed=false;
    edges.forEach((e,i)=>{if(back.has(i)||!ids.has(e.from)||!ids.has(e.to))return;const r=(rank.get(e.from)??0)+1;if(r>(rank.get(e.to)??0)){rank.set(e.to,r);changed=true;}});
    if(!changed)break;}
  // Starting boxes (like "You") sit just above what they lead to, not at the very top.
  for(const n of nodes){if(incoming.has(n.id))continue;
    const next=edges.filter((e,i)=>e.from===n.id&&!back.has(i)&&ids.has(e.to)).map(e=>rank.get(e.to)??0);
    if(next.length)rank.set(n.id,Math.max(rank.get(n.id)??0,Math.min(...next)-1));}
  return {rank,back};
}

type Box={node:DiagramNode;x:number;y:number;w:number;h:number;title:string[];note:string[];where:string};

/** A box-and-arrow diagram drawn from plain data: how something is built, or how it works step by step. */
export function Diagram({nodes,edges,variant,title}:{nodes:DiagramNode[];edges:DiagramEdge[];variant:'architecture'|'flow';title:string}){
  const marker=`arrow-${useId().replace(/[^a-zA-Z0-9]/g,'')}`;
  if(!nodes.length)return null;
  const {rank,back}=layers(nodes,edges);
  const W=variant==='flow'?230:210,GX=30,GY=64,PAD=24;
  const rows:DiagramNode[][]=[];
  for(const n of nodes){const r=rank.get(n.id)??0;(rows[r]??=[]).push(n);}
  const order=new Map<string,number>();
  rows.forEach((row,r)=>{
    if(r>0){const parents=(id:string)=>edges.filter(e=>e.to===id&&order.has(e.from)).map(e=>order.get(e.from)!);
      const score=(id:string)=>{const p=parents(id);return p.length?p.reduce((a,b)=>a+b,0)/p.length:Number.MAX_SAFE_INTEGER;};
      row.sort((a,b)=>score(a.id)-score(b.id));}
    row.forEach((n,i)=>order.set(n.id,i-(row.length-1)/2));
  });
  const boxes=new Map<string,Box>();
  const widest=Math.max(...rows.filter(Boolean).map(row=>row.length*W+(row.length-1)*GX));
  let y=PAD;
  for(const row of rows.filter(Boolean)){
    const measured=row.map(node=>{const title=wrap(node.label,variant==='flow'?26:22,3),note=variant==='architecture'?wrap(node.note??'',30,3):[],where=variant==='architecture'?(node.where??'').slice(0,34):'';
      const h=Math.max(variant==='flow'&&node.kind==='decision'?64:44,18+title.length*17+(note.length?6+note.length*15:0)+(where?16:0));return {node,title,note,where,h};});
    const rowH=Math.max(...measured.map(m=>m.h)),rowW=row.length*W+(row.length-1)*GX;
    let x=PAD+(widest-rowW)/2;
    for(const m of measured){boxes.set(m.node.id,{...m,x,y:y+(rowH-m.h)/2,w:W});x+=W+GX;}
    y+=rowH+GY;
  }
  const width=widest+PAD*2,height=y-GY+PAD;
  const links=edges.map((e,i)=>{const a=boxes.get(e.from),b=boxes.get(e.to);if(!a||!b)return null;
    let d:string,lx:number,ly:number;
    if(back.has(i)||b.y<=a.y){const sx=a.x+a.w,sy=a.y+a.h/2,tx=b.x+b.w,ty=b.y+b.h/2,bend=Math.max(sx,tx)+46;
      d=`M${sx},${sy} C${bend},${sy} ${bend},${ty} ${tx+2},${ty}`;lx=bend-8;ly=(sy+ty)/2;}
    else if((rank.get(e.to)??0)-(rank.get(e.from)??0)>1){// skips a row: go around the boxes in between
      const left=a.x+a.w/2<PAD+widest/2,side=left?PAD-14:PAD+widest+14,sx=left?a.x:a.x+a.w,sy=a.y+a.h/2,tx=left?b.x:b.x+b.w,ty=b.y+b.h/2;
      d=`M${sx},${sy} C${side},${sy} ${side},${ty} ${tx+(left?-2:2)},${ty}`;lx=side;ly=(sy+ty)/2;}
    else{const sx=a.x+a.w/2,sy=a.y+a.h,tx=b.x+b.w/2,ty=b.y-2,mid=(sy+ty)/2;
      d=`M${sx},${sy} C${sx},${mid} ${tx},${mid} ${tx},${ty}`;lx=(sx+tx)/2;ly=mid;}
    return {key:i,d,lx,ly,label:(e.label??'').slice(0,28)};}).filter(l=>l!==null);
  const kinds=[...new Set(nodes.map(n=>n.kind))].filter(k=>k in KIND_LABEL);
  return <figure className={`diagram diagram-${variant}`}>
    <div className="diagram-scroll">
      <svg viewBox={`-30 0 ${width+70} ${height}`} width={width+70} style={{maxWidth:'100%',height:'auto'}} role="img" aria-label={title}>
        <title>{title}</title>
        <defs><marker id={marker} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" className="dg-arrowhead"/></marker></defs>
        {links.map(l=><path key={l.key} d={l.d} className="dg-link" markerEnd={`url(#${marker})`}/>)}
        {[...boxes.values()].map(b=>{const k=b.node.kind,cx=b.x+b.w/2;
          const shape=k==='decision'?<polygon points={`${b.x+14},${b.y} ${b.x+b.w-14},${b.y} ${b.x+b.w},${b.y+b.h/2} ${b.x+b.w-14},${b.y+b.h} ${b.x+14},${b.y+b.h} ${b.x},${b.y+b.h/2}`}/>
            :<rect x={b.x} y={b.y} width={b.w} height={b.h} rx={['start','end','user'].includes(k)?Math.min(22,b.h/2):10}/>;
          const lines=b.title.length;
          return <g key={b.node.id} className={`dg-node dg-${k}`}>{shape}
            <text x={cx} y={b.y+(b.note.length||b.where?22:b.h/2-(lines-1)*8.5+5)} className="dg-title">{b.title.map((t,i)=><tspan key={i} x={cx} dy={i?17:0}>{t}</tspan>)}</text>
            {b.note.length>0&&<text x={cx} y={b.y+22+lines*17+4} className="dg-note">{b.note.map((t,i)=><tspan key={i} x={cx} dy={i?15:0}>{t}</tspan>)}</text>}
            {b.where&&<text x={cx} y={b.y+b.h-9} className="dg-where">{b.where}</text>}
          </g>;})}
        {links.filter(l=>l.label).map(l=>{const w=l.label.length*6.4+12;return <g key={`l${l.key}`} className="dg-label"><rect x={l.lx-w/2} y={l.ly-10} width={w} height={20} rx={10}/><text x={l.lx} y={l.ly+4}>{l.label}</text></g>;})}
      </svg>
    </div>
    {variant==='architecture'&&kinds.length>1&&<figcaption className="diagram-legend">{kinds.map(k=><span key={k} className={`dg-chip dg-${k}`}>{KIND_LABEL[k]}</span>)}</figcaption>}
  </figure>;
}
