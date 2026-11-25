import {label,record,str,useGet,type Row,type Session} from './api';
import {ActionButton,type Navigate} from './ui';

export function projectActivity(tasks:Row[],activity:string){
  // A current task takes precedence over a leftover lead activity message.
  const task=tasks.find(t=>t.status==='running');
  if(task)return {title:'The team is working',description:str(task.title),task};
  if(/Choosing|planning|replan|Repairing|alternative/i.test(activity))return {
    title:'The lead is planning automatically',description:'It is choosing the next task or repairing its plan.',task:null};
  if(activity)return {title:'The project is working automatically',description:activity,task:null};
  return {title:'Waiting for the next automatic step',description:'The worker checks the plan and starts eligible tasks automatically.',task:null};
}

export function ProjectActivity({tasks,activity,queue,session,navigate,onRefresh,error=''}:{tasks:Row[];activity:string;queue:Row|null;session:Session|null;navigate:Navigate;onRefresh:()=>void;error?:string}){
  const state=projectActivity(tasks,activity),paused=queue?.paused===true,stopped=queue?.worker_running===false;
  const needsWorker=paused||stopped;
  return <section className={`project-activity ${needsWorker||error?'attention':''}`} aria-label="Project progress">
    <div role="status"><span className="badge">{error?'Status unavailable':needsWorker?'Worker needs attention':'No input needed'}</span>
      <h3>{error?'Could not refresh live progress':paused?'New work is paused':stopped?'The project worker is stopped':state.title}</h3>
      <p>{error?'The information above may be out of date. Refresh to check the current project.':needsWorker?`${state.task?'The current task can continue, but new steps need the worker. ':''}Run approved work to continue launched projects and already approved tasks within their existing allowances.`:state.description}</p>
      {!error&&!needsWorker&&<p className="muted">This is an automatic step. You do not need to select or approve anything here.</p>}
      {!error&&state.task&&str(state.task.stage)&&<p className="muted">Current stage: {label(state.task.stage).toLowerCase()}</p>}
    </div>
    <div className="button-row">
      {needsWorker&&!error&&<ActionButton operation="start_queue" args={{spending_authorized:true}} disabled={!session?.models_enabled} onDone={onRefresh}>Run approved work</ActionButton>}
      {state.task&&<button onClick={()=>navigate('work',str(state.task.id))}>View active task</button>}
      <button onClick={onRefresh}>Refresh status</button>
    </div>
    {needsWorker&&!session?.models_enabled&&<p className="muted">Controls are unavailable in this session. Start the dashboard with --allow-actions in the configured SwarmForge environment to run approved work.</p>}
  </section>;
}

export function LiveProjectActivity(props:{tasks:Row[];activity:string;session:Session|null;navigate:Navigate;onRefresh:()=>void;error:string}){
  const queue=useGet('/api/v1/queue',record,6000);
  return <ProjectActivity {...props} queue={queue.data} error={props.error||queue.error} onRefresh={()=>{queue.refresh();props.onRefresh();}}/>;
}
