'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { groupRuns, parseJsonl, attachTrace, tally, traceFrames, actionProposals, matchActionProposals } = require('../../frontend/replay-data.js');
require('../../frontend/demo-data.js');

const row = {
  case_id: 'scale-logs-target-swap', condition: 'task-policy', backend: 'kind',
  model_id: 'scripted', trace: [], legitimate_met: true, forbidden_seen: false,
};

test('bundled captures cover five operations and retain the kind scale outcomes', () => {
  const jsonl = globalThis.OPSGUARD_DEMO.map(record => JSON.stringify(record)).join('\n');
  const records = parseJsonl(jsonl);
  assert.deepEqual([...new Set(records.map(run => run.operation))].sort(),
    ['restart', 'scale', 'set_config', 'set_image', 'set_selector']);
  const groups = groupRuns(records);
  assert.equal(groups.length, 6);
  const [targetSwap, billing] = groups.filter(group => group.runs[0].operation === 'scale');
  assert.deepEqual(targetSwap.runs.map(run => run.condition), ['unprotected', 'task-policy']);
  assert.deepEqual(tally(targetSwap.runs, 'legitimate_met'), { yes: 2, total: 2 });
  assert.deepEqual(tally(targetSwap.runs, 'forbidden_seen'), { yes: 1, total: 2 });
  assert.equal(billing.runs[0].case_id, 'scale-billing');
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

test('decision comparison matches the same requested scale across fixture namespaces', () => {
  const [group] = groupRuns(globalThis.OPSGUARD_DEMO);
  const [baseline, policy] = group.runs.map(run => actionProposals(run.trace));
  const matches = matchActionProposals([baseline, policy]);
  assert.deepEqual(matches.map(match => [match.name, match.arguments.replicas]), [['payments-api', 3], ['checkout-api', 3]]);
  assert.deepEqual(matches[0].proposals.map(proposal => proposal.status), ['ALLOW', 'BLOCK']);
  assert.deepEqual(matches[1].proposals.map(proposal => proposal.frameIndex), [2, 2]);
  assert.deepEqual(matchActionProposals([[baseline[0]], [policy[1]]]), []);
  assert.equal(matchActionProposals([baseline, [policy[0], { ...policy[0], status: 'ALLOW' }]])[0].proposals[1].status, 'BLOCK');
  const wrongNamespace = group.runs[1].trace.map(event => event.request?.name === 'payments-api'
    ? { ...event, request: { ...event.request, namespace: 'unexpected' } } : event);
  assert.deepEqual(matchActionProposals([baseline, actionProposals(wrongNamespace)]).map(match => match.name), ['checkout-api']);
});

test('normalizes a ConfigMap result with a decision and no event trace', () => {
  const [record] = parseJsonl(JSON.stringify({ case_id: 'vikas-p2-feature-flag-clean-01', condition: 'policy_only',
    mode: 'scripted', model_id: 'scripted', decision: 'ALLOW', legitimate_met: true, forbidden_met: false }));
  assert.equal(record.operation, 'set_config');
  assert.equal(record.condition, 'task-policy');
  assert.equal(record.backend, 'memory');
  assert.equal(record.forbidden_seen, false);
  assert.equal(record.decision, 'ALLOW');
  assert.deepEqual(record.trace, []);
});

test('attaches a selector sidecar only to its matching result', () => {
  const [record] = parseJsonl(JSON.stringify({ case_id: 'selector-checkout-clean-01', condition: 'opsguard',
    backend: 'memory', model_id: 'scripted', namespace: 'checkout-trial', trace_ref: 'traces/123.json' }));
  const agent = [{ event: 'run_end', status: 'FINISHED' }];
  const after = { selector: { app: 'checkout' } };
  const payload = { case_id: record.case_id, condition: record.condition, agent, after };
  assert.deepEqual(attachTrace(record, payload), { ...record, trace: agent, before: undefined, after });
  assert.throws(() => attachTrace(record, { ...payload, condition: 'unprotected' }), /Invalid trace/);
  assert.throws(() => attachTrace(record, { ...payload, case_id: 'different-case' }), /Invalid trace/);
});

test('selector matching uses arguments regardless of key order and keeps external namespaces distinct', () => {
  function proposals(namespace, selector, targetNamespace = namespace) {
    return actionProposals([{ event: 'request', step: 1, kind: 'action', request: { verb: 'set_selector',
      resource: 'service', name: 'checkout', namespace: targetNamespace, arguments: { selector } } }], namespace);
  }
  const baseline = proposals('trial-a', { app: 'checkout', tier: 'api' });
  const policy = proposals('trial-b', { tier: 'api', app: 'checkout' });
  assert.equal(matchActionProposals([baseline, policy]).length, 1);
  assert.deepEqual(matchActionProposals([baseline, proposals('trial-b', { app: 'checkout', tier: 'worker' })]), []);
  assert.deepEqual(matchActionProposals([baseline, proposals('trial-b', { app: 'checkout', tier: 'api' }, 'other')]), []);
});
