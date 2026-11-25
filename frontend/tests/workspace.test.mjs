import assert from 'node:assert/strict';
import {after,test} from 'node:test';
import {mkdtempSync,mkdirSync,readFileSync,readdirSync,writeFileSync,rmSync,realpathSync} from 'node:fs';
import {dirname,join,resolve,sep} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import ts from 'typescript';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const cache=join(root,'node_modules','.cache');mkdirSync(cache,{recursive:true});
const output=mkdtempSync(join(cache,'sf-react-tests-'));
after(()=>{const actual=realpathSync(output),allowed=realpathSync(cache)+sep;if(!actual.startsWith(allowed))throw new Error('Unsafe test cleanup path');rmSync(actual,{recursive:true,force:true});});
writeFileSync(join(output,'package.json'),'\{"type":"module"\}');
for(const name of readdirSync(join(root,'src')).filter(n=>/\.tsx?$/.test(n)&&n!=='main.tsx')){
  const result=ts.transpileModule(readFileSync(join(root,'src',name),'utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  writeFileSync(join(output,name.replace(/\.tsx?$/,'.js')),result.replace(/from '(\.\/[^']+)'/g,(_,path)=>`from '${path}.js'`));
}
globalThis.location={search:''};
globalThis.fetch=()=>{throw new Error('Rendering must not call a model or service');};
const {App}=await import(pathToFileURL(join(output,'App.js')));
const {NewTask,ProjectForm}=await import(pathToFileURL(join(output,'Forms.js')));
const {parseSession,collection}=await import(pathToFileURL(join(output,'api.js')));
const session={actions_enabled:true,models_enabled:true,csrf_token:'session-token',settings:{configured:true,team:null,roles:[],limits:{budget_usd:5},teams:[]}};

test('the React shell exposes core journeys through accessible links',()=>{
  const html=renderToStaticMarkup(createElement(App));
  for(const page of ['work','new-task','projects','chat','prs','failures','history','knowledge','settings','teams','whats-new'])assert.ok(html.includes(`?page=${page}`),page);
  assert.match(html,/aria-label="Workspace"/);
  assert.equal((html.match(/<main>/g)||[]).length,1);
  assert.ok(!html.includes('<iframe'));
});

test('new task explains that its single explicit submission approves the displayed allowance',()=>{
  const html=renderToStaticMarkup(createElement(NewTask,{session,navigate(){}}));
  assert.match(html,/Requirements and acceptance criteria/);
  assert.match(html,/Approve allowance &amp; add to queue/);
  assert.ok(!html.includes('type="checkbox"'));
  assert.match(html,/Adding this task approves its displayed allowance/);
  assert.match(html,/publishing still asks for your approval/i);
});

test('project planning saves a draft and offers no implicit launch',()=>{
  const html=renderToStaticMarkup(createElement(ProjectForm,{session,onSaved(){}}));
  assert.match(html,/Save draft/);
  assert.match(html,/Milestone 3/);
  assert.match(html,/done when/i);
  assert.ok(!html.includes('Approve displayed spec and launch'));
});

test('new projects begin with a conversation instead of a requirements form',async()=>{
  const {ProjectsPage}=await import(pathToFileURL(join(output,'Projects.js')));
  const html=renderToStaticMarkup(createElement(ProjectsPage,{session,selected:'',navigate(){}}));
  assert.match(html,/Start with a conversation/);
  assert.match(html,/>New project</);
  assert.ok(!html.includes('Milestone 3'));
  assert.ok(!html.includes('Total API allowance'));
  assert.ok(!html.includes('Approve and launch'));
});

test('removed plans and cancelled attempts do not masquerade as failed builds or active work',async()=>{
  const {projectTaskCounts}=await import(pathToFileURL(join(output,'Projects.js')));
  assert.deepEqual(projectTaskCounts([{status:'done'},{status:'cancelled'},{status:'cancelled',run_id:'old'},
    {status:'queued'},{status:'tentative'},{status:'failed'},{status:'skipped'}]),{done:1,failed:1,active:2});
});

test('kickoff questions render as a form with optional answers and visible proposal defaults',async()=>{
  const {ProjectQuestions}=await import(pathToFileURL(join(output,'Projects.js')));
  const question={id:10,actions:{project_questions:[
    {id:'goal',label:'What would you like to build?',kind:'textarea',required:true},
    {id:'sharing',label:'Who can see a garden?',kind:'select',required:false,options:['Only me','Invited gardeners']},
    {id:'users',label:'Who is it for?',kind:'text',required:false}],kickoff_defaults:{repo_owner:'bigdan88',budget_usd:30}}};
  const html=renderToStaticMarkup(createElement(ProjectQuestions,{id:'p1',session,spec:'abc',question,onDone(){}}));
  assert.match(html,/<form/);assert.match(html,/<textarea required=""/);
  assert.match(html,/<select/);assert.match(html,/<input maxLength="4000"/);
  assert.match(html,/Only me/);assert.match(html,/Let the lead decide/);assert.match(html,/Optional/);
  assert.match(html,/bigdan88\/project-name/);assert.match(html,/\$30\.00/);
  assert.match(html,/Draft my project/);assert.match(html,/Nothing launches until you approve/);
  assert.ok(!html.includes('ask_before'));assert.ok(!html.includes('Approve and launch'));
});

test('chat renders retained suggestions with review controls and remembers launched tasks',async()=>{
  const {SuggestedTasks,ChatPage}=await import(pathToFileURL(join(output,'Chat.js')));
  const message={id:42,actions:{tasks:[{title:'Export inventory',description:'Acceptance: CSV matches the list',repo:'owner/app'}]}};
  const html=renderToStaticMarkup(createElement(SuggestedTasks,{message,session,onDone(){}}));
  assert.match(html,/Export inventory/);assert.match(html,/Review and launch task/);
  assert.ok(!html.includes('>Launch task<'),'a suggestion must be reviewed before launch');
  const launched=renderToStaticMarkup(createElement(SuggestedTasks,{message:{...message,launched_tasks:{'0':'task-1'}},session,onDone(){}}));
  assert.match(launched,/Task queued/);assert.match(launched,/page=work&amp;item=task-1/);
  assert.ok(!launched.includes('Review and launch task'));
  const page=renderToStaticMarkup(createElement(ChatPage,{session,selected:'',navigate(){}}));
  assert.match(page,/Start a project/);
});

test('workspace responses reject incompatible settings and list payloads',()=>{
  assert.equal(parseSession(session).models_enabled,true);
  assert.throws(()=>parseSession({...session,settings:{...session.settings,roles:[{role:'planner'}]}}),/incompatible/);
  assert.throws(()=>collection({items:[null],total:1}),/incompatible/);
  assert.throws(()=>collection({items:[],total:-1}),/incompatible/);
});

test('milestone decision names unverified checks and distinguishes repair from explicit acceptance',async()=>{
  const {ProjectDecision}=await import(pathToFileURL(join(output,'ProjectDecision.js')));
  const html=renderToStaticMarkup(createElement(ProjectDecision,{id:'p1',session,spec:'abc',project:{budget_usd:50,spent_usd:1},nextMilestone:'Ingest',onDone(){},onAmend(){},navigate(){},
    recovery:{decision_token:'current',can_amend:true,decision:{kind:'checkpoint',title:'Review milestone 1',summary:'This is a checkpoint, not a task crash.',lead_summary:'Docs merged; owner hardware is unavailable.',
      checks:[{text:'A real recording plays correctly',status:'needs you',explanation:'No automated evidence is linked.'}]}}}));
  assert.match(html,/A real recording plays correctly/);
  assert.match(html,/Verify &amp; fix automatically/);assert.match(html,/I verified these checks — accept milestone/);
  assert.match(html,/you personally verified the 1 unverified check/);assert.match(html,/Ingest/);
  assert.match(html,/keeps this milestone open/);assert.ok(!html.includes('type="checkbox"'));
  assert.ok(!html.includes('>Submit checkpoint reply<'));
});

test('task recovery shows retained test progress and a precise bounded extension approval',async()=>{
  const {ProjectDecision}=await import(pathToFileURL(join(output,'ProjectDecision.js')));
  const html=renderToStaticMarkup(createElement(ProjectDecision,{id:'p1',session,spec:'abc',project:{budget_usd:20,spent_usd:7},nextMilestone:'',onDone(){},onAmend(){},navigate(){},
    recovery:{decision_token:'current',can_extend_task:true,decision:{kind:'task',title:'The task reached its spending or time limit',passed_checks:9,task:{id:95,title:'Polish CSS'},allowance:{budget_usd:1,spent_usd:.73},
      checks:[{text:'Browser observation',status:'unverified',explanation:'no tests mapped'}]}}}));
  assert.match(html,/9 acceptance checks passed/);assert.match(html,/Polish CSS/);assert.match(html,/Browser observation/);
  assert.match(html,/Approve \$1.00 more &amp; repair automatically/);assert.match(html,/project cap stays \$20.00/);
  assert.match(html,/Existing costs and evidence stay recorded/);assert.ok(!html.includes('type="checkbox"'));
  assert.ok(!html.includes('Send correction &amp; continue'));
});

test('each workspace route renders without a DOM or automatic network/model calls',async()=>{
  const {bindWorkspace}=await import(pathToFileURL(join(output,'api.js')));
  bindWorkspace({...session,workspace_id:'ws-test',instance_id:'i1'});  // the page has loaded its workspace
  try{
    for(const page of ['work','new-task','projects','chat','prs','failures','history','benchmarks','knowledge','teams','settings','whats-new']){
      globalThis.location.search=`?page=${page}`;
      const html=renderToStaticMarkup(createElement(App));
      assert.equal((html.match(/<main>/g)||[]).length,1,page);
      assert.ok(html.includes('<h1>'),page);
    }
  }finally{globalThis.location.search='';}
});

test('chat reply status helpers report the newest request and readable elapsed time',async()=>{
  const {latestRequest,elapsed}=await import(pathToFileURL(join(output,'api.js')));
  assert.equal(latestRequest([]),null);
  assert.equal(latestRequest([{id:'b',status:'thinking'},{id:'a',status:'done'}]).id,'b');
  assert.equal(latestRequest('not a list'),null);
  assert.equal(elapsed(100,null,145),'45s');
  assert.equal(elapsed(100,230,9999),'2m 10s');
  assert.equal(elapsed(200,null,100),'0s');
});

test('a stale Python server explains how to recover an unsupported action',async()=>{
  const api=await import(pathToFileURL(join(output,'api.js')));
  api.bindWorkspace({...session,workspace_id:'ws-test',instance_id:'i1'});
  globalThis.fetch=async url=>url==='/api/v1/session'?{ok:true,status:200,json:async()=>({...session,workspace_id:'ws-test'})}:{ok:false,status:422,json:async()=>({detail:[{type:'literal_error',loc:['body','operation']}]})};
  try{await assert.rejects(api.performAction('start_project','',{},'unsupported-0000001'),/Restart the Python dashboard server/);}
  finally{globalThis.fetch=()=>{throw new Error('Rendering must not call a model or service');};}
});

test('chat markdown renders structure but never raw HTML or script links',async()=>{
  const {Markdown}=await import(pathToFileURL(join(output,'Messages.js')));
  const html=renderToStaticMarkup(createElement(Markdown,{text:'**Bold** and `code`\n\n- one\n- two\n\n1. first\n\n```\nx<y\n```\n<img src=x onerror=alert(1)> [ok](https://example.com) [bad](javascript:alert(1))'}));
  assert.match(html,/<strong>Bold<\/strong>/);assert.match(html,/<code>code<\/code>/);
  assert.match(html,/<ul><li>one<\/li><li>two<\/li><\/ul>/);assert.match(html,/<ol><li>first<\/li><\/ol>/);
  assert.match(html,/<pre class="md-code"><code[^>]*>x&lt;y<\/code><\/pre>/);
  assert.ok(!html.includes('<img'),'raw HTML must be escaped');assert.match(html,/&lt;img/);
  assert.match(html,/href="https:\/\/example.com"/);assert.ok(!html.includes('javascript:alert(1)"'),'script links must not become hrefs');
});

test('chat messages expose copyable code, files, tables, images and user alignment',async()=>{
  const {ChatMessage,Markdown,MessageFiles,safeLink,downloadName}=await import(pathToFileURL(join(output,'Messages.js')));
  const code=renderToStaticMarkup(createElement(Markdown,{text:'```python\nprint("hello")\n```\n\n```file:notes.csv\nitem,count\nbeans,3\n```\n\n| Item | Count |\n| --- | --- |\n| Beans | 3 |\n\n![Preview](https://example.com/preview.png)'}));
  assert.match(code,/Copy code/);assert.match(code,/data-language="python"/);assert.match(code,/Download file/);assert.match(code,/notes.csv/);
  assert.match(code,/<table>/);assert.match(code,/<img[^>]*src="https:\/\/example.com\/preview.png"/);
  assert.match(code,/referrerPolicy="no-referrer"/);
  const user=renderToStaticMarkup(createElement(ChatMessage,{message:{role:'user',content:'My request'}}));
  assert.match(user,/class="message user"/);assert.match(user,/Your message/);assert.match(user,/Copy message/);
  const assistant=renderToStaticMarkup(createElement(ChatMessage,{message:{role:'assistant',content:'Reply'}}));
  assert.match(assistant,/class="message assistant"/);assert.match(assistant,/Save as Markdown/);
  const files=renderToStaticMarkup(createElement(MessageFiles,{conversation:'c1',items:[{id:'a'.repeat(32),name:'result.png',kind:'image',size:2048}]}));
  assert.match(files,/files\/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/);assert.match(files,/inline=true/);assert.match(files,/Download/);
  assert.equal(safeLink('javascript:alert(1)'), '');assert.equal(safeLink('data:text/html;base64,AA',true),'');
  assert.equal(safeLink('http://localhost/private.png',true),'');assert.equal(downloadName('../../result.py'),'result.py');
  const partial=renderToStaticMarkup(createElement(Markdown,{text:'```file:notes.txt\nStill writing'}));
  assert.ok(!partial.includes('Download file'));assert.match(partial,/Writing file/);
});

test('copy actions write the exact source text and report unavailable clipboard access',async()=>{
  const {copyText}=await import(pathToFileURL(join(output,'Messages.js')));
  const old=Object.getOwnPropertyDescriptor(globalThis,'navigator'),copied=[];
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{writeText:async text=>copied.push(text)}}});
  try{await copyText('print("hello")\n');assert.deepEqual(copied,['print("hello")\n']);Object.defineProperty(globalThis,'navigator',{configurable:true,value:{}});await assert.rejects(copyText('x'),/Clipboard access is unavailable/);}
  finally{if(old)Object.defineProperty(globalThis,'navigator',old);else delete globalThis.navigator;}
});


test('work details use accessible tabs and keep controls outside the initial overview',async()=>{
  const {WorkDetails}=await import(pathToFileURL(join(output,'WorkDetails.js')));
  const item={id:'task-1',title:'Build a dashboard',repo:'example/app',status:'running',stage:'TESTING',run_id:'r1',
    reason:'Checking the result',created_at:100,updated_at:120,finished_at:null,pr_url:null};
  const html=renderToStaticMarkup(createElement(WorkDetails,{item,session,onDone(){},onArchived(){}}));
  assert.equal((html.match(/role="tab"/g)||[]).length,7);
  for(const tab of ['Overview','Sequence','Bots','Checks','Report &amp; screenshots','Activity','Controls'])assert.ok(html.includes(tab),tab);
  assert.equal((html.match(/aria-selected="true"/g)||[]).length,1);
  assert.match(html,/role="tabpanel"[^>]*hidden=""/);
  assert.match(html,/Happening now/);assert.match(html,/Up next/);
  assert.ok(!html.includes('Recent events'),'the event wall does not render by default');
  assert.ok(!html.includes('Read retained report'),'files are loaded only on their tab');
});

test('a finished task opens on its report and screenshots',async()=>{
  const {WorkDetails}=await import(pathToFileURL(join(output,'WorkDetails.js')));
  const item={id:'task-2',title:'Redesign',repo:'example/app',status:'done',stage:'COMPLETED',run_id:'r2',
    reason:'draft PR created',created_at:100,updated_at:120,finished_at:130,pr_url:'https://github.com/example/app/pull/3'};
  const html=renderToStaticMarkup(createElement(WorkDetails,{item,session,onDone(){},onArchived(){}}));
  assert.match(html,/aria-selected="true"[^>]*>Report &amp; screenshots/);
});

test('work overview, sequence and bots use recorded results without implying finished acceptance',async()=>{
  const {WorkOverview,SequencePanel,BotsPanel}=await import(pathToFileURL(join(output,'WorkDetails.js')));
  const item={id:'task-1',title:'Build',status:'failed',stage:'FAILED',run_id:'r1',reason:'Layout failed'};
  const makeTask=(key,state,deps=[])=>({task_key:key,cycle:0,kind:'implement',title:key,state,role:'developer',model:'recorded-model',
    depends_on:deps,dependencies_complete:true,expected_files:[],started_at:null,ended_at:null,attempts:1,summary:null,error:null,truncated:false});
  const execution={mode:'swarm',repair_cycle:0,counts:{INTEGRATED:1,BLOCKED:1},tasks:[makeTask('T1','INTEGRATED'),makeTask('T2','BLOCKED',['T1'])],bots:[],phases:[]};
  const snapshot={execution,allowance:null,costs:null,checks:[{id:1,cycle:0,criterion_id:'C1',status:'failed'}]};
  const overview=renderToStaticMarkup(createElement(WorkOverview,{item,snapshot,events:[],onTab(){}}));
  assert.match(overview,/1 \/ 2/);assert.match(overview,/0 \/ 1/);assert.match(overview,/Review the stopping reason/);
  const sequence=renderToStaticMarkup(createElement(SequencePanel,{item,execution}));
  assert.match(sequence,/Dependency level 2/);assert.match(sequence,/After/);assert.match(sequence,/Blocked/);assert.match(sequence,/recorded-model/);
  const bots=renderToStaticMarkup(createElement(BotsPanel,{item,execution}));
  assert.match(bots,/recorded-model/);assert.ok(!bots.includes('result-tag active'),'a stopped run never shows a bot as working');
});

test('planning recovery explains rejection and offers an optional clarification without manual-check claims',async()=>{
  const {ProjectRecovery}=await import(pathToFileURL(join(output,'ProjectRecovery.js')));
  const props={id:'p1',project:{budget_usd:50,spent_usd:.51},spec:'reviewed',session,onDone(){},onAmend(){},
    recovery:{can_retry:true,can_amend:true,latest_decision_id:12,attempts:3,rejections:[{id:12,action:'investigate',
      explanation:'The response used fields in the wrong part of an action.',error:'Unexpected hypothesis',reason:'Research the recorder'}]}};
  const html=renderToStaticMarkup(createElement(ProjectRecovery,props));
  assert.match(html,/Retry planning automatically/);assert.match(html,/\$49\.49/);assert.match(html,/Optional clarification/);
  assert.match(html,/Planning issues/);assert.match(html,/Unexpected hypothesis/);assert.match(html,/Edit approved scope/);
  assert.ok(!html.includes('I verified any checks'));
  const uncertain=renderToStaticMarkup(createElement(ProjectRecovery,{...props,recovery:{can_retry:false,rejections:[]}}));
  assert.ok(!uncertain.includes('Retry planning automatically'));
});

test('approved scope editor preserves repository and milestone count and requires explicit revised approval',()=>{
  const initial={name:'Inventory',repo:'owner/inventory',budget_usd:30,repository_kind:'existing',
    milestones:[0,1,2].map(i=>({name:`M${i}`,goal:'Build',checks:['A','B'],estimate_usd:10,estimate_hours:1}))};
  const html=renderToStaticMarkup(createElement(ProjectForm,{session,initial,projectId:'amend1',expectedSpec:'reviewed',amending:true,completedMilestones:1,onSaved(){}}));
  assert.match(html,/Revise the approved specification/);assert.ok(!html.includes('type="checkbox"'));assert.match(html,/Approve revised specification/);
  assert.match(html,/>Approve revised specification<\/button>/);assert.match(html,/readOnly=""/i);
  assert.match(html,/<fieldset[^>]*class="milestone-editor"[^>]*disabled=""/);
  assert.match(html,/The project stays paused|the project stays paused/);assert.ok(!html.includes('Saving creates a draft'));
});

test('resolved planning failures are history and do not appear to be an active approval request',async()=>{
  const {ProjectRecovery}=await import(pathToFileURL(join(output,'ProjectRecovery.js')));
  const html=renderToStaticMarkup(createElement(ProjectRecovery,{id:'p1',project:{budget_usd:20},spec:'abc',session,onDone(){},onAmend(){},
    recovery:{can_retry:false,attempts:1,rejections:[{id:1,action:'plan',ts:1795381315,resolved:true,resolved_by:2,explanation:'Provisional task ordering was corrected.'}]}}));
  assert.match(html,/Recovery history/);assert.match(html,/Corrected automatically/);assert.match(html,/corrected by decision #2/);
  assert.match(html,/do not mean the project is currently blocked/);assert.match(html,/SwarmForge&#x27;s plan validator/);
  assert.ok(!html.includes('attempts in the current planning round'));assert.ok(!html.includes('Retry planning automatically'));
});

test('automatic planning is clearly informational and an active task replaces an old planning message',async()=>{
  const {ProjectActivity,projectActivity}=await import(pathToFileURL(join(output,'ProjectActivity.js')));
  const props={tasks:[],activity:'Choosing the next bounded project action',queue:{paused:false,worker_running:true},session,navigate(){},onRefresh(){}};
  const planning=renderToStaticMarkup(createElement(ProjectActivity,props));
  assert.match(planning,/No input needed/);assert.match(planning,/lead is planning automatically/);
  assert.match(planning,/do not need to select or approve anything/);assert.match(planning,/Refresh status/);
  assert.ok(!planning.includes('Run approved work'));assert.ok(!planning.includes('Choosing the next bounded'));
  const tasks=[{id:'task-111',status:'running',title:'Write README',stage:'DEVELOPING'}];
  assert.equal(projectActivity(tasks,props.activity).title,'The team is working');
  const running=renderToStaticMarkup(createElement(ProjectActivity,{...props,tasks}));
  assert.match(running,/Write README/);assert.match(running,/View active task/);assert.match(running,/developing/);
  assert.ok(!running.includes('lead is planning'));
});

test('a stopped or paused worker offers a concrete way to continue and outages never claim work is healthy',async()=>{
  const {ProjectActivity}=await import(pathToFileURL(join(output,'ProjectActivity.js')));
  const props={tasks:[],activity:'Choosing the next bounded project action',session,navigate(){},onRefresh(){}};
  for(const queue of [{worker_running:false,paused:false},{worker_running:true,paused:true}]){
    const html=renderToStaticMarkup(createElement(ProjectActivity,{...props,queue}));
    assert.match(html,/Worker needs attention/);assert.match(html,/Run approved work/);
    assert.match(html,/existing allowances/);assert.ok(!html.includes('No input needed'));
  }
  const offline=renderToStaticMarkup(createElement(ProjectActivity,{...props,queue:null,error:'Disconnected'}));
  assert.match(offline,/Could not refresh live progress/);assert.match(offline,/Refresh status/);
  assert.ok(!offline.includes('No input needed'));
});

test('a new dashboard build offers reload without discarding unsent text automatically',async()=>{
  const {DashboardUpdate}=await import(pathToFileURL(join(output,'DashboardUpdate.js')));
  assert.equal(renderToStaticMarkup(createElement(DashboardUpdate,{available:false})), '');
  const html=renderToStaticMarkup(createElement(DashboardUpdate,{available:true}));
  assert.match(html,/Dashboard update available/);assert.match(html,/Reload dashboard/);
  assert.match(html,/Running projects continue/);assert.match(html,/Copy any unsent text/);
});

test('drafts are stored per workspace and a stale tab cannot act on a replacement workspace',async()=>{
  const api=await import(pathToFileURL(join(output,'api.js')));
  const store=new Map();
  globalThis.localStorage={getItem:k=>store.has(k)?store.get(k):null,setItem:(k,v)=>store.set(k,String(v))};
  api.bindWorkspace({...session,workspace_id:'ws-test',instance_id:'i1'});
  api.saveLocal('sf-new-task','draft for A');api.saveLocal('sf-theme','dark');
  assert.equal(store.get('ws-test:sf-new-task'),'draft for A');assert.equal(store.get('sf-theme'),'dark');
  assert.equal(api.readLocal('sf-new-task',''),'draft for A');
  const calls=[];
  globalThis.fetch=async(url,init)=>{calls.push([url,init]);return {ok:true,status:200,json:async()=>({...session,workspace_id:'ws-other',instance_id:'i9'})};};
  await assert.rejects(api.performAction('pause_queue','',{},'stale-request-000001'),/different workspace/);
  assert.equal(calls.filter(([u])=>u==='/api/v1/actions').length,0,'no write is sent from a stale tab');
  globalThis.fetch=async(url,init)=>{calls.push([url,init]);return url==='/api/v1/session'?{ok:true,status:200,json:async()=>({...session,workspace_id:'ws-test'})}:{ok:true,status:200,json:async()=>({ok:true,message:'done'})};};
  await api.performAction('pause_queue','',{},'fresh-request-0000001');
  const sent=calls.find(([u])=>u==='/api/v1/actions')[1];
  assert.equal(sent.headers['X-SwarmForge-Workspace'],'ws-test');
  globalThis.fetch=()=>{throw new Error('Rendering must not call a model or service');};
});
