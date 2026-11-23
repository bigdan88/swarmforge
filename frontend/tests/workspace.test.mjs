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

test('new task starts with spending disabled and a requirements form',()=>{
  const html=renderToStaticMarkup(createElement(NewTask,{session,navigate(){}}));
  assert.match(html,/Requirements and acceptance criteria/);
  assert.match(html,/disabled=""[^>]*>Add to queue/);
  assert.match(html,/publishing still asks for your approval/i);
});

test('project planning saves a draft and offers no implicit launch',()=>{
  const html=renderToStaticMarkup(createElement(ProjectForm,{session,onSaved(){}}));
  assert.match(html,/Save draft/);
  assert.match(html,/Milestone 3/);
  assert.match(html,/done when/i);
  assert.ok(!html.includes('Approve displayed spec and launch'));
});

test('workspace responses reject incompatible settings and list payloads',()=>{
  assert.equal(parseSession(session).models_enabled,true);
  assert.throws(()=>parseSession({...session,settings:{...session.settings,roles:[{role:'planner'}]}}),/incompatible/);
  assert.throws(()=>collection({items:[null],total:1}),/incompatible/);
  assert.throws(()=>collection({items:[],total:-1}),/incompatible/);
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

test('chat markdown renders structure but never raw HTML or script links',async()=>{
  const {Markdown}=await import(pathToFileURL(join(output,'Messages.js')));
  const html=renderToStaticMarkup(createElement(Markdown,{text:'**Bold** and `code`\n\n- one\n- two\n\n1. first\n\n```\nx<y\n```\n<img src=x onerror=alert(1)> [ok](https://example.com) [bad](javascript:alert(1))'}));
  assert.match(html,/<strong>Bold<\/strong>/);assert.match(html,/<code>code<\/code>/);
  assert.match(html,/<ul><li>one<\/li><li>two<\/li><\/ul>/);assert.match(html,/<ol><li>first<\/li><\/ol>/);
  assert.match(html,/<pre class="md-code"><code>x&lt;y<\/code><\/pre>/);
  assert.ok(!html.includes('<img'),'raw HTML must be escaped');assert.match(html,/&lt;img/);
  assert.match(html,/href="https:\/\/example.com"/);assert.ok(!html.includes('javascript:alert(1)"'),'script links must not become hrefs');
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
