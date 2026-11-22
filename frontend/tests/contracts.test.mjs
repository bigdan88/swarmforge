import assert from 'node:assert/strict';
import test from 'node:test';
import {appendEvents, parseArtifacts, parseEvidence, parsePage, parseReport, parseWork} from '../src/contracts.ts';

const item = {id:'task-1', title:'A task', repo:null, status:'queued', stage:null,
  run_id:null, created_at:null, updated_at:null, finished_at:null, reason:null, pr_url:null};
const page = {items:[item], total:1, counts:{queued:1}, limit:25, offset:0};

test('accepts undispatched legacy tasks and independent global counts', () => {
  assert.deepEqual(parseWork(item), item);
  assert.deepEqual(parsePage({...page, counts:{queued:40, done:8}}).items, [item]);
});

const event = id => ({id, ts:100, kind:'progress', level:'info', message:'Tests passed', truncated:false});
const evidence = {item_id:'task-1', run_id:'run-1', costs:null, allowance:null, checks:[], checks_has_more:false,
  tests:[], tests_has_more:false, events:[event(10)], next_event_id:10, events_has_more:false};

test('validates evidence and prevents cursor stalls, regressions and malformed usage', () => {
  assert.deepEqual(parseEvidence(evidence), evidence);
  for (const bad of [{...evidence, costs:{}}, {...evidence, allowance:{attempt:2}},
    {...evidence, events:[event(10), event(9)]}, {...evidence, next_event_id:9},
    {...evidence, events:[], events_has_more:true}, {...evidence, tests:[{id:1}]}]) {
    assert.throws(()=>parseEvidence(bad), /incompatible response/);
  }
});

test('reconnect overlap deduplicates and retains a bounded recent event window', () => {
  const previous=Array.from({length:190}, (_,i)=>event(i+1));
  const incoming=Array.from({length:50}, (_,i)=>event(i+185));
  const merged=appendEvents(previous,incoming);
  assert.equal(merged.length,200);
  assert.equal(new Set(merged.map(e=>e.id)).size,200);
  assert.equal(merged[0].id,35);
  assert.equal(merged.at(-1).id,234);
});

test('artifact contracts reject remote/traversing image names and unbounded reports', () => {
  const listing={item_id:'task-1',run_id:'run-1',report_status:'available',report_bytes:100,
    report_truncated:false,screenshots:[{name:'c1-home-dark.png',size_bytes:100}],
    screenshots_truncated:false,screenshots_unavailable:false};
  assert.deepEqual(parseArtifacts(listing),listing);
  for(const name of ['https://elsewhere.test/image.png','../c1-home-dark.png','c1-home-dark.svg']) {
    assert.throws(()=>parseArtifacts({...listing,screenshots:[{name,size_bytes:100}]}),/incompatible/);
  }
  assert.throws(()=>parseArtifacts({...listing,report_bytes:null}),/incompatible/);
  assert.throws(()=>parseArtifacts({...listing,screenshots:Array(25).fill(listing.screenshots[0])}),/incompatible/);
  const report={item_id:'task-1',run_id:'run-1',text:'<script>kept as text</script>',size_bytes:100,truncated:false};
  assert.deepEqual(parseReport(report),report);
  assert.throws(()=>parseReport({...report,text:'x'.repeat(256*1024+1)}),/incompatible/);
});

test('rejects contract drift instead of rendering broken or misleading status', () => {
  const {finished_at, ...missing} = item;
  for (const bad of [null, [], missing, {...item, title:15}, {...item, updated_at:Infinity}]) {
    assert.throws(() => parseWork(bad), /incompatible response/);
  }
  for (const bad of [{...page, items:[missing]}, {...page, total:'1'}, {...page, total:0},
    {...page, counts:{running:-1}}, {...page, limit:101}, {...page, offset:100001},
    {...page, items:[item, item], limit:1}]) {
    assert.throws(() => parsePage(bad), /incompatible response/);
  }
});
