import assert from 'node:assert/strict';
import test from 'node:test';
import {parseEvidence,parseExecution} from '../src/contracts.ts';
import {botRoster,checkPassed,currentChecks,dependencyLevels,phaseProgress,taskProgress} from '../src/WorkProgress.ts';

const task=(key,deps=[],state='PENDING',extra={})=>({task_key:key,cycle:0,kind:'implement',title:key,role:'developer',model:'model-a',state,
  depends_on:deps,dependencies_complete:true,expected_files:[],started_at:null,ended_at:null,attempts:0,summary:null,error:null,truncated:false,...extra});
const execution={mode:'swarm',repair_cycle:0,counts:{PENDING:4},tasks:[task('T4',['T2','T3']),task('T2',['T1']),task('T3',['T1']),task('T1')],
  tasks_has_more:false,bots:[],bots_has_more:false,phases:[],phases_has_more:false};

test('execution contracts keep legacy evidence readable and reject incomplete or unbounded graphs',()=>{
  assert.deepEqual(parseExecution(execution),execution);
  const legacy={item_id:'task-1',run_id:null,costs:null,allowance:null,checks:[],checks_has_more:false,tests:[],tests_has_more:false,
    events:[],next_event_id:0,events_has_more:false};
  assert.equal(parseEvidence(legacy).execution,undefined);
  for(const bad of [{...execution,counts:{}},{...execution,tasks:Array(201).fill(task('T1'))},
    {...execution,tasks:[task('T1'),task('T1')]},{...execution,tasks:[task('T1',Array(51).fill('X'))]},
    {...execution,phases:[{id:2,ts:1,state:'TESTING'},{id:1,ts:2,state:'COMPLETED'}]},
    {...execution,bots:[{role:'developer',model:'m',calls:2}]}])assert.throws(()=>parseExecution(bad),/incompatible/);
});

test('sequence finds parallel dependency levels without relying on insertion order',()=>{
  const graph=dependencyLevels(execution.tasks);
  assert.deepEqual(graph.levels.map(level=>level.map(t=>t.task_key)),[['T1'],['T2','T3'],['T4']]);
  assert.deepEqual(graph.unresolved,[]);
});

test('missing, cyclic and truncated prerequisites never appear ready',()=>{
  const tasks=[task('root'),task('missing',['unknown']),task('A',['B']),task('B',['A']),
    task('clipped',[], 'PENDING',{dependencies_complete:false}),task('after-missing',['missing'])];
  const graph=dependencyLevels(tasks);
  assert.deepEqual(graph.levels.map(l=>l.map(t=>t.task_key)),[['root']]);
  assert.equal(graph.unresolved.length,5);
});

test('repair dependencies can reference a completed earlier cycle without inventing completion',()=>{
  const repair=[task('R1',['T1'],'RUNNING',{cycle:1})];
  assert.equal(dependencyLevels(repair).unresolved.length,1);
  assert.deepEqual(dependencyLevels(repair,['T1']).levels[0].map(t=>t.task_key),['R1']);
});

test('stage progress distinguishes observed stages and the point a run stopped',()=>{
  const item={status:'running',stage:'TESTING'};
  const live=phaseProgress(item,{...execution,phases:[{id:1,ts:1,state:'PLANNING'}]});
  assert.equal(live.find(p=>p.name==='Understand').visited,false);
  assert.equal(live.find(p=>p.name==='Build').label,'Not recorded');
  assert.equal(live.find(p=>p.name==='Test').label,'Now');
  const stopped=phaseProgress({status:'failed',stage:'FAILED'},{...execution,phases:[{id:1,ts:1,state:'TESTING'},{id:2,ts:2,state:'FAILED'}]});
  assert.equal(stopped.find(p=>p.current).name,'Test');
  assert.equal(stopped.find(p=>p.current).label,'Stopped here');
  assert.ok(!phaseProgress({status:'queued',stage:null},null).some(p=>p.current));
});

test('progress counts include all recorded tasks and acceptance uses current cycle results',()=>{
  assert.equal(checkPassed('satisfied'),true);assert.equal(checkPassed('passed'),true);
  assert.equal(checkPassed('unverified'),false);assert.equal(checkPassed('failed'),false);
  assert.deepEqual(taskProgress({...execution,counts:{INTEGRATED:8,SUCCEEDED:1,RUNNING:1,PENDING:2,FAILED:1}}),
    {total:13,finished:9,running:1,waiting:2,attention:1});
  const checks=currentChecks({checks:[{id:3,cycle:1,criterion_id:'A',status:'failed'},
    {id:2,cycle:0,criterion_id:'A',status:'passed'},{id:1,cycle:0,criterion_id:'B',status:'passed'}]});
  assert.equal(checks.cycle,1);assert.equal(checks.checks.length,1);assert.equal(checks.checks[0].status,'failed');assert.equal(checks.older.length,2);
  const waiting=currentChecks({execution:{repair_cycle:2},checks:[{id:4,cycle:1,criterion_id:'A',status:'satisfied'}]});
  assert.equal(waiting.cycle,2);assert.equal(waiting.checks.length,0);assert.equal(waiting.older.length,1);
});

test('bot roster combines assigned models with actual fallback usage and retains unknown costs',()=>{
  const bots=botRoster({...execution,tasks:[task('T1',[],'RUNNING'),task('T2')],bots:[
    {role:'developer',model:'model-a',provider:'fake',calls:3,unavailable_calls:0},
    {role:'developer',model:'model-b',provider:'fake',calls:1,unavailable_calls:1},
    {role:'reviewer',model:'model-c',provider:'fake',calls:2,unavailable_calls:0}]});
  assert.equal(bots.length,3);assert.equal(bots[0].model,'model-a');assert.equal(bots[0].tasks.length,2);
  assert.equal(bots.find(b=>b.model==='model-b').usage[0].unavailable_calls,1);
});
