import {useState,type FormEvent} from 'react';
import {collection,label,money,num,record,rows,saveLocal,stamp,str,useAction,useDraft,useGet,type Session} from './api';
import {ActionButton,Feedback,Heading,Loading,Pager,type Navigate} from './ui';
export function ChatPage({session,navigate,selected}:{session:Session|null;navigate:Navigate;selected:string}){
  const [offset,setOffset]=useState(0);const list=useGet(`/api/v1/conversations?offset=${offset}`,collection,6000);
  return <><Heading title="Chat" description="Ask about the swarm, investigate work and keep each conversation separate."><ActionButton operation="create_conversation" disabled={!session?.actions_enabled} onDone={r=>{list.refresh();if(r.conversation_id)navigate('chat',r.conversation_id);}}>New conversation</ActionButton></Heading>
    <div className="chat-layout"><section className="panel thread-list"><h2>Conversations</h2><Feedback error={list.error}/>{list.data?.items.map(c=><button className={`thread ${str(c.id)===selected?'selected':''}`} key={str(c.id)} onClick={()=>navigate('chat',str(c.id))}><strong>{str(c.title)||'New chat'}</strong><small>{stamp(c.updated_at||c.created_at)}</small></button>)}{list.data&&<Pager offset={offset} total={list.data.total} setOffset={setOffset}/>}</section>
      {selected?<Conversation key={selected} id={selected} session={session}/>:<section className="panel empty"><h2>A place to work things through</h2><p>Open a conversation or start a new one. Model calls happen only when you send an authorized message.</p></section>}
    </div></>;
}
function Conversation({id,session}:{id:string;session:Session|null}){
  const view=useGet(`/api/v1/conversations/${encodeURIComponent(id)}`,record,3000);
  const [draft,setDraft]=useDraft(`sf-chat-draft:${id}`,{message:''});const [authorized,setAuthorized]=useState(false);const action=useAction(`chat:${id}`);
  async function submit(e:FormEvent){e.preventDefault();const result=await action.send('send_message',id,{message:draft.message,spending_authorized:authorized});if(result){setDraft({message:''});saveLocal(`sf-chat-draft:${id}`,'{}');setAuthorized(false);view.refresh();}}
  if(!view.data)return <section className="panel"><Loading error={view.error}/></section>;
  const conversation=record(view.data.conversation),costs=record(view.data.costs);
  return <section className="panel conversation"><div className="detail-heading"><h2>{str(conversation.title)||'Conversation'}</h2><span className="muted">{money(costs.total)} recorded usage</span></div><Feedback error={view.error}/>
    <div className="transcript" aria-label="Conversation history">{rows(view.data.messages).map(m=><article className={`message ${str(m.role)}`} key={num(m.id)}><small>{label(m.role)} · {stamp(m.created_at)}</small><p>{str(m.content)}</p>{m.truncated===true&&<small>Long message shortened in this view.</small>}</article>)}{!rows(view.data.messages).length&&<p className="muted">No messages yet.</p>}</div>
    {action.pending&&<p role="status">The configured model is responding. You can return to this conversation later.</p>}
    <form className="composer editor" onSubmit={submit}><fieldset disabled={action.pending||!session?.models_enabled}><label>Your message<textarea required rows={4} maxLength={16000} value={draft.message} onChange={e=>setDraft({message:e.target.value})} placeholder="What is blocking the current task?"/></label><label className="check"><input type="checkbox" checked={authorized} onChange={e=>setAuthorized(e.target.checked)}/>Send this message using the configured model and chat allowance.</label><button className="primary" disabled={!authorized||!draft.message.trim()} type="submit">{action.pending?'Responding…':'Send message'}</button></fieldset><Feedback error={action.error} message={action.message}/><p className="muted">Drafts stay in this browser. Suggested actions never launch work automatically. Showing the latest 100 messages; older history remains retained.</p></form>
  </section>;
}
