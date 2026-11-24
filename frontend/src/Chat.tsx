import {useEffect,useRef,useState,type FormEvent,type KeyboardEvent} from 'react';
import {collection,elapsed,label,latestRequest,money,num,readLocal,record,rows,saveLocal,stamp,str,uploadAttachment,useAction,useDraft,useGet,type Row,type Session,type Staged} from './api';
import {ActionButton,Feedback,Heading,Loading,Pager,type Navigate} from './ui';
import {ChatMessage,Markdown,replyIsSaved,Transcript,useFitToWindow,useLiveReply} from './Messages';
export function ChatPage({session,navigate,selected}:{session:Session|null;navigate:Navigate;selected:string}){
  const [offset,setOffset]=useState(0);const list=useGet(`/api/v1/conversations?offset=${offset}`,collection,6000,true);
  return <><Heading title="Chat" description="Discuss ideas, generate tasks, and review them before launching."><div className="button-row"><ActionButton operation="start_project" disabled={!session?.actions_enabled} onDone={r=>{if(r.project_id)navigate('projects',r.project_id);}}>Start a project</ActionButton><ActionButton operation="create_conversation" disabled={!session?.actions_enabled} onDone={r=>{list.refresh();if(r.conversation_id)navigate('chat',r.conversation_id);}}>New conversation</ActionButton></div></Heading>
    <div className="chat-layout"><section className="panel thread-list"><h2>Conversations</h2><Feedback error={list.error}/>{list.data?.items.map(c=><button className={`thread ${str(c.id)===selected?'selected':''}`} aria-current={str(c.id)===selected?'true':undefined} key={str(c.id)} onClick={()=>navigate('chat',str(c.id))}><strong>{str(c.title)||'New chat'}</strong><small>{stamp(c.updated_at||c.created_at)}</small></button>)}{list.data&&<Pager offset={offset} total={list.data.total} setOffset={setOffset}/>}</section>
      {selected?<Conversation key={selected} id={selected} session={session} onRenamed={list.refresh}/>:<section className="panel empty"><h2>A place to work things through</h2><p>Open a conversation or start a new one. Model calls happen only when you send an authorized message.</p></section>}
    </div></>;
}
function Attachments({conversation,items}:{conversation:string;items:Row[]}){
  if(!items.length)return null;
  return <div className="attachments">{items.map(a=>str(a.kind)==='image'?<a key={str(a.id)} href={`/api/v1/conversations/${encodeURIComponent(conversation)}/attachments/${encodeURIComponent(str(a.id))}`} target="_blank" rel="noreferrer"><img className="thumb" src={`/api/v1/conversations/${encodeURIComponent(conversation)}/attachments/${encodeURIComponent(str(a.id))}`} alt={str(a.name)} loading="lazy"/></a>:<span className="chip" key={str(a.id)}>{str(a.name)}</span>)}</div>;
}
function ReplyStatus({request,now,onReuse}:{request:Row;now:number;onReuse:(text:string)=>void}){
  const status=str(request.status),steps=rows(request.steps);
  if(status==='thinking')return <div className="reply-status" role="status"><span>{str(request.partial_text)?'Generating':'Thinking'}… {elapsed(request.started_at,null,now)}</span>{steps.length>0&&<details><summary>{steps.length} tool {steps.length===1?'step':'steps'}</summary><ul>{steps.map((s,i)=><li key={i}>{s.ok===false?'Could not use':'Checked'} {label(s.tool)}</li>)}</ul></details>}{num(now-num(request.started_at))>45&&<p className="muted">You can leave this page. The reply will be saved here.</p>}</div>;
  if(status==='failed'||status==='interrupted')return <div className="reply-status error" role="alert"><strong>{status==='failed'?'The reply failed':'The reply was interrupted'}</strong><p>{str(request.error)||'No reason was recorded.'}</p><button onClick={()=>onReuse(str(request.question))}>Put this message back in the composer</button></div>;
  return null;
}
function ConversationTitle({id,title,enabled,onRenamed}:{id:string;title:string;enabled:boolean;onRenamed:()=>void}){
  const [editing,setEditing]=useState(false),[value,setValue]=useState(title);const action=useAction(`rename:${id}`);
  useEffect(()=>{if(!editing)setValue(title);},[title,editing]);
  async function save(){
    const next=value.trim();if(!next||next===title){setEditing(false);setValue(title);return;}
    if(await action.send('rename_conversation',id,{title:next})){setEditing(false);onRenamed();}
  }
  if(!editing)return <div className="title-edit"><h2>{title||'New chat'}</h2>{enabled&&<button type="button" className="icon-button" aria-label="Rename conversation" title="Rename" onClick={()=>setEditing(true)}>✎</button>}</div>;
  return <form className="title-edit" onSubmit={e=>{e.preventDefault();void save();}}>
    <input autoFocus aria-label="Conversation name" maxLength={120} value={value} disabled={action.pending} onChange={e=>setValue(e.target.value)} onBlur={()=>void save()} onKeyDown={e=>{if(e.key==='Escape'){e.preventDefault();setValue(title);setEditing(false);}}}/>
    <Feedback error={action.error}/></form>;
}
function Conversation({id,session,onRenamed}:{id:string;session:Session|null;onRenamed:()=>void}){
  const [fast,setFast]=useState(false);
  const view=useGet(`/api/v1/conversations/${encodeURIComponent(id)}`,record,fast?1000:3000);
  const [draft,setDraft]=useDraft(`sf-chat-draft:${id}`,{message:''});const action=useAction(`chat:${id}`);
  const stagedKey=`sf-chat-attachments:${id}`;
  const [staged,setStaged]=useState<Staged[]>(()=>{try{const v=JSON.parse(readLocal(stagedKey,'[]'));return Array.isArray(v)?v.filter(a=>a&&typeof a.id==='string'&&typeof a.name==='string'):[];}catch{return [];}});
  const [uploading,setUploading]=useState(0),[uploadError,setUploadError]=useState('');
  const fileInput=useRef<HTMLInputElement>(null);
  const panel=useFitToWindow<HTMLElement>(!!view.data);
  useEffect(()=>{saveLocal(stagedKey,JSON.stringify(staged));},[stagedKey,staged]);
  const stream=useLiveReply(id,view.data?latestRequest(view.data.requests):null,view.refresh);
  const latest=stream.request;
  const thinking=str(latest?.status)==='thinking';
  useEffect(()=>setFast(thinking),[thinking]);
  const wasThinking=useRef(false);
  useEffect(()=>{if(wasThinking.current&&!thinking)onRenamed();wasThinking.current=thinking;},[thinking,onRenamed]);
  const now=Date.now()/1000;
  async function attach(files:FileList|null){
    if(!files?.length)return;setUploadError('');
    const room=6-staged.length;if(files.length>room){setUploadError('Attach at most 6 files to one message.');return;}
    setUploading(n=>n+files.length);
    for(const file of Array.from(files)){try{const made=await uploadAttachment(file);setStaged(s=>[...s,made]);}catch(e){setUploadError(e instanceof Error?e.message:'Upload failed');}finally{setUploading(n=>n-1);}}
    if(fileInput.current)fileInput.current.value='';
  }
  async function submit(e?:FormEvent){
    e?.preventDefault();if(!draft.message.trim()||thinking||uploading||action.pending)return;
    const result=await action.send('send_message',id,{message:draft.message,attachments:staged.map(a=>a.id)});
    if(result){setDraft({message:''});saveLocal(`sf-chat-draft:${id}`,'{}');setStaged([]);setFast(true);view.refresh();}
  }
  // Enter sends; Shift+Enter starts a new line (Ctrl/Cmd+Enter also sends). Not while composing IME text.
  function keys(e:KeyboardEvent<HTMLTextAreaElement>){if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void submit();}}
  if(!view.data)return <section className="panel"><Loading error={view.error}/></section>;
  const conversation=record(view.data.conversation),costs=record(view.data.costs);
  return <section className="panel conversation" ref={panel}><div className="detail-heading"><ConversationTitle id={id} title={str(conversation.title)} enabled={!!session?.actions_enabled} onRenamed={()=>{view.refresh();onRenamed();}}/><span className="muted">{money(costs.total)} recorded usage</span></div><Feedback error={view.error}/>
    <Transcript count={rows(view.data.messages).length+(thinking?1:0)}>{rows(view.data.messages).map(m=><ChatMessage message={m} conversation={id} key={num(m.id)}><SuggestedTasks message={m} session={session} onDone={view.refresh}/><Attachments conversation={id} items={rows(m.attachments)}/></ChatMessage>)}{!rows(view.data.messages).length&&<p className="muted">No messages yet.</p>}
      {str(latest?.partial_text)&&!replyIsSaved(rows(view.data.messages),latest)&&<ChatMessage message={{role:'assistant',content:latest?.partial_text}} streaming={thinking}/>}
      {stream.reconnecting&&thinking&&<p className="muted" role="status">Reconnecting to the reply…</p>}
      {latest&&<ReplyStatus request={latest} now={now} onReuse={text=>setDraft({message:text})}/>}</Transcript>
    <form className="composer editor" onSubmit={submit}><fieldset disabled={action.pending||thinking||!session?.models_enabled}>
      <label><span className="sr-only">Your message</span><textarea required rows={2} maxLength={16000} value={draft.message} onKeyDown={keys} onChange={e=>setDraft({message:e.target.value})} placeholder="Message SwarmForge…" aria-describedby="composer-help"/></label>
      <div className="composer-actions"><div className="attach-row"><input ref={fileInput} id={`attach-${id}`} className="sr-only" tabIndex={-1} aria-hidden="true" type="file" multiple accept=".png,.jpg,.jpeg,.gif,.webp,.txt,.md,.py,.js,.ts,.tsx,.json,.yaml,.yml,.toml,.csv,.log,.sql,.sh,.html,.css" onChange={e=>void attach(e.target.files)}/><label htmlFor={`attach-${id}`} className="button-like" tabIndex={0} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();fileInput.current?.click();}}}>Attach files</label>
        {staged.map(a=><span className="chip" key={a.id}>{a.name}<button type="button" aria-label={`Remove ${a.name}`} onClick={()=>setStaged(s=>s.filter(x=>x.id!==a.id))}>×</button></span>)}{uploading>0&&<span role="status">Uploading…</span>}</div><div className="send-row"><button className="primary" disabled={!draft.message.trim()||uploading>0} type="submit">{action.pending?'Sending…':thinking?'Waiting for reply…':'Send message'}</button></div></div>
      <Feedback error={uploadError}/></fieldset>
      <Feedback error={action.error}/><p className="muted composer-help" id="composer-help">Enter sends · Shift+Enter for a new line · images and text files up to 5 MB · drafts stay in this browser</p></form>
  </section>;
}

export function SuggestedTasks({message,session,onDone}:{message:Row;session:Session|null;onDone:()=>void}){
  return <>{rows(record(message.actions??{}).tasks).slice(0,8).map((task,index)=><SuggestedTask key={index} task={task} messageId={String(message.id)} index={index} launched={str(record(message.launched_tasks??{})[String(index)])} session={session} onDone={onDone}/>)}</>;
}
function SuggestedTask({task,messageId,index,launched,session,onDone}:{task:Row;messageId:string;index:number;launched:string;session:Session|null;onDone:()=>void}){
  const [reviewing,setReviewing]=useState(false),[queued,setQueued]=useState('');
  const [draft,setDraft]=useDraft(`sf-chat-task:${messageId}:${index}`,{title:str(task.title),body:str(task.description),repo:str(task.repo),budget:String(session?.settings.limits.budget_usd??5)});
  const action=useAction(`chat-task:${messageId}:${index}`);
  const field=(key:string,value:string)=>setDraft(old=>({...old,[key]:value}));
  async function launch(e:FormEvent){
    e.preventDefault();if(action.pending)return;
    const result=await action.send('launch_chat_task',messageId,{...draft,budget:Number(draft.budget),index,spending_authorized:true});
    if(result?.item_id){setQueued(result.item_id);onDone();}
  }
  const item=launched||queued;
  return <section className="suggested-task"><h3>{str(task.title)}</h3>{str(task.why)&&<p className="muted">{str(task.why)}</p>}
    {item?<p role="status">Task queued. <a href={`?page=work&item=${encodeURIComponent(item)}`}>Open task in Work</a></p>:<>
      <Markdown text={str(task.description)}/><button type="button" disabled={!session?.models_enabled} onClick={()=>setReviewing(v=>!v)}>{reviewing?'Close task review':'Review and launch task'}</button>
      {reviewing&&<form className="editor" onSubmit={launch}><fieldset disabled={action.pending||!session?.models_enabled}>
        <label>Task title<input required maxLength={300} value={draft.title} onChange={e=>field('title',e.target.value)}/></label>
        <label>Repository<input required value={draft.repo} onChange={e=>field('repo',e.target.value)} placeholder="owner/repository"/></label>
        <label>Requirements and acceptance criteria<textarea required rows={6} maxLength={30000} value={draft.body} onChange={e=>field('body',e.target.value)}/></label>
        <label>Task allowance (USD)<input required type="number" min="0.5" max="500" step="0.5" value={draft.budget} onChange={e=>field('budget',e.target.value)}/></label>
        <p className="muted">Launching approves this task and its displayed allowance.</p>
        <p className="muted">Launch adds this task to the normal queue and starts its worker. Publication still requires approval.</p>
        <button className="primary" type="submit">{action.pending?'Launching…':'Approve & launch task'}</button>
      </fieldset></form>}
    </>}<Feedback error={action.error} message={action.message}/>
  </section>;
}
