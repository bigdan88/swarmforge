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

test('each workspace route renders without a DOM or automatic network/model calls',()=>{
  try{
    for(const page of ['work','new-task','projects','chat','prs','failures','history','benchmarks','knowledge','teams','settings','whats-new']){
      globalThis.location.search=`?page=${page}`;
      const html=renderToStaticMarkup(createElement(App));
      assert.equal((html.match(/<main>/g)||[]).length,1,page);
      assert.ok(html.includes('<h1>'),page);
    }
  }finally{globalThis.location.search='';}
});
