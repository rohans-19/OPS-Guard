'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { groupRuns, parseJsonl, tally, traceFrames } = require('../../frontend/replay-data.js');
require('../../frontend/demo-data.js');

const row = {
  case_id: 'scale-logs-target-swap', condition: 'task-policy', backend: 'kind',
  model_id: 'scripted', trace: [], legitimate_met: true, forbidden_seen: false,
};

test('bundled captures reproduce the target-swap comparison and the clean case', () => {
  const jsonl = globalThis.OPSGUARD_DEMO.map(record => JSON.stringify(record)).join('\n');
  const groups = groupRuns(parseJsonl(jsonl));
  assert.equal(groups.length, 2);
  assert.deepEqual(groups[0].runs.map(run => run.condition), ['unprotected', 'task-policy']);
  assert.deepEqual(tally(groups[0].runs, 'legitimate_met'), { yes: 2, total: 2 });
  assert.deepEqual(tally(groups[0].runs, 'forbidden_seen'), { yes: 1, total: 2 });
  assert.equal(groups[1].runs[0].case_id, 'scale-billing');
});

test('invalid imports report the line and do not accept string outcomes', () => {
  assert.throws(() => parseJsonl(`${JSON.stringify(row)}\n{broken`), /Line 2.*invalid JSON/);
  assert.throws(() => parseJsonl(JSON.stringify({ ...row, forbidden_seen: 'false' })), /Line 1.*outcome/);
  assert.throws(() => parseJsonl(JSON.stringify({ ...row, trace: [{ event: 'gateway', status: {} }] })), /trace array/);
});

test('imports retain UTF-8 BOM files and preserve evidence as text', () => {
  const text = '<img src=x onerror=alert(1)>';
  const trace = [{ event: 'gateway', step: 0, synthetic_result: { data: { observation: { text } } } }];
  const [record] = parseJsonl(`\uFEFF${JSON.stringify({ ...row, trace })}\r\n`);
  assert.equal(record.trace[0].synthetic_result.data.observation.text, text);
});

test('groups keep backends, model seeds, and duplicate condition sets distinct', () => {
  const unprotected = { ...row, condition: 'unprotected' };
  const duplicate = { ...row };
  const groups = groupRuns([row, duplicate, unprotected, { ...unprotected },
    { ...row, backend: 'memory' }, { ...unprotected, seed: 42 }, { ...row, seed: 43 }]);
  assert.deepEqual(groups.map(group => group.runs.length), [2, 2, 1, 1, 1]);
  assert.equal(groups[1].runs[1], duplicate);
  assert.equal(groups[1].occurrence, 2);
});

test('metrics exclude errors and outcomes without a score', () => {
  assert.deepEqual(tally([row, { ...row, forbidden_seen: null },
    { ...row, error: 'LAB_ERROR', forbidden_seen: true }], 'forbidden_seen'), { yes: 0, total: 1 });
});

test('a replay retains step zero, combines proposal and response, and includes the run end', () => {
  const trace = [{ event: 'gateway', step: 0 }, { event: 'model', step: 1 }, { event: 'request', step: 1 },
    { event: 'gateway', step: 1 }, { event: 'run_end', status: 'FINISHED' }];
  const frames = traceFrames(trace);
  assert.deepEqual(frames.map(frame => frame.step), [0, 1, null]);
  assert.deepEqual(frames[1].events.map(event => event.event), ['model', 'request', 'gateway']);
  assert.equal(frames[2].events[0].status, 'FINISHED');
});
