(() => {
  'use strict';

  const maxBytes = 8 * 1024 * 1024;
  const maxRuns = 5000;
  const conditions = { unprotected: 'Unprotected', hardened: 'Hardened prompt', 'llm-judge': 'LLM judge',
    'task-policy': 'Task policy', opsguard: 'OpsGuard', 'no-graph': 'No dependency graph',
    'no-dry-run': 'No dry-run', controlled: 'Controlled impact replay' };
  const operations = { scale: 'Scale Deployment', restart: 'Restart Deployment', set_image: 'Update container image',
    set_config: 'Update ConfigMap', set_selector: 'Repair Service selector' };
  const outcomeFields = ['legitimate_met', 'forbidden_seen', 'safe_completion', 'evidence_exposed'];

  function isObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function hasText(value) {
    return typeof value === 'string' && value.trim().length > 0;
  }

  function hasOptionalTextFields(object, keys) {
    return keys.every(key => object[key] == null || typeof object[key] === 'string');
  }

  function isTraceEvent(event) {
    if (!isObject(event) || !hasText(event.event) ||
        !hasOptionalTextFields(event, ['kind', 'model', 'status', 'reason', 'error_type'])) return false;
    if (event.request != null && (!isObject(event.request) ||
        !hasOptionalTextFields(event.request, ['verb', 'tool', 'namespace', 'name']))) return false;
    const observation = event.synthetic_result?.data?.observation;
    if (observation == null) return true;
    if (!isObject(observation) || !hasOptionalTextFields(observation, ['text', 'channel'])) return false;
    return observation.source == null || (isObject(observation.source) &&
      hasOptionalTextFields(observation.source, ['namespace', 'pod', 'container', 'deployment']));
  }

  function operationFor(row) {
    if (typeof row.operation === 'string' && Object.hasOwn(operations, row.operation)) return row.operation;
    const prefix = row.case_id.split('-')[0];
    const byPrefix = { scale: 'scale', restart: 'restart', image: 'set_image', vikas: 'set_config',
      configmap: 'set_config', selector: 'set_selector' };
    return Object.hasOwn(byPrefix, prefix) ? byPrefix[prefix] : 'other';
  }

  function parseJsonl(text) {
    const rows = [];
    const lines = text.replace(/^\uFEFF/, '').split(/\r?\n/);
    for (const [index, line] of lines.entries()) {
      if (!line.trim()) continue;
      const lineLabel = `Line ${index + 1}`;
      let row;
      try {
        row = JSON.parse(line);
      } catch {
        throw new Error(`${lineLabel} contains invalid JSON.`);
      }
      if (!isObject(row) || !['case_id', 'condition', 'model_id'].every(key => hasText(row[key]))) {
        throw new Error(`${lineLabel} is missing a case ID, condition, or model ID.`);
      }
      if (row.mode === 'live' || row.mode === 'scripted') {
        row.backend ??= row.mode === 'live' ? 'kind' : 'memory';
        row.delivery ??= row.evidence_delivery;
        row.forbidden_seen ??= row.forbidden_met;
      }
      row.condition = row.condition.replaceAll('_', '-');
      if (row.condition === 'policy-only') row.condition = 'task-policy';
      if (row.channel === 'clean') row.channel = null;
      row.operation = operationFor(row);
      if (!hasText(row.backend)) throw new Error(`${lineLabel} is missing a backend.`);
      if (row.trace === undefined && (hasText(row.trace_ref) || hasText(row.decision) ||
          ['trial', 'impact_replay'].includes(row.kind))) row.trace = [];
      if (!Array.isArray(row.trace) || row.trace.length > 2000 || !row.trace.every(isTraceEvent)) {
        throw new Error(`${lineLabel} is missing a valid trace array.`);
      }
      if (outcomeFields.some(key => row[key] != null && typeof row[key] !== 'boolean')) {
        throw new Error(`${lineLabel} has an invalid outcome flag.`);
      }
      if ((row.repeat != null && (!Number.isSafeInteger(row.repeat) || row.repeat < 1)) ||
          (row.elapsed_ms != null && (!Number.isFinite(row.elapsed_ms) || row.elapsed_ms < 0)) ||
          !hasOptionalTextFields(row, ['channel', 'delivery', 'agent_status', 'fixture_image']) ||
          (row.error != null && !hasText(row.error))) {
        throw new Error(`${lineLabel} has invalid run metadata.`);
      }
      rows.push(row);
      if (rows.length > maxRuns) throw new Error(`Load up to ${maxRuns.toLocaleString()} runs per file.`);
    }
    if (!rows.length) throw new Error('This file contains no benchmark runs.');
    return rows;
  }

  function attachTrace(row, payload) {
    if (!isObject(payload) || payload.case_id !== row.case_id || payload.condition !== row.condition ||
        !Array.isArray(payload.agent) || payload.agent.length > 2000 || !payload.agent.every(isTraceEvent)) {
      throw new Error(`Invalid trace for ${row.case_id} (${row.condition}).`);
    }
    return { ...row, trace: payload.agent, before: payload.before, after: payload.after };
  }

  function groupRuns(rows) {
    const buckets = new Map();
    const groups = [];
    for (const row of rows) {
      const fixture = isObject(row.fixture_sha256)
        ? Object.keys(row.fixture_sha256).sort().map(key => [key, row.fixture_sha256[key]])
        : null;
      const key = JSON.stringify([row.case_id, row.backend, row.provider, row.model_id, row.repeat ?? 1,
        row.seed ?? null, row.fixture_image ?? null, fixture, row.experiment_sha256 ?? row.config_sha256,
        row.attempt ?? 1]);
      if (!buckets.has(key)) buckets.set(key, { groups: [], occurrences: new Map() });
      const bucket = buckets.get(key);
      const occurrence = bucket.occurrences.get(row.condition) ?? 0;
      bucket.occurrences.set(row.condition, occurrence + 1);
      let group = bucket.groups[occurrence];
      if (!group) {
        group = { index: groups.length, occurrence: occurrence + 1, runs: [] };
        bucket.groups.push(group);
        groups.push(group);
      }
      group.runs.push(row);
    }
    const order = Object.keys(conditions);
    for (const group of groups) {
      const rank = condition => order.includes(condition) ? order.indexOf(condition) : order.length;
      group.runs.sort((left, right) => rank(left.condition) - rank(right.condition));
    }
    return groups;
  }

  function tally(rows, field) {
    const scored = rows.filter(row => !row.error && typeof row[field] === 'boolean');
    return { yes: scored.filter(row => row[field]).length, total: scored.length };
  }

  function traceFrames(trace) {
    const frames = [];
    const steps = new Map();
    for (const event of trace) {
      if (!Number.isSafeInteger(event.step)) {
        frames.push({ step: null, events: [event] });
        continue;
      }
      if (!steps.has(event.step)) {
        const frame = { step: event.step, events: [] };
        steps.set(event.step, frame);
        frames.push(frame);
      }
      steps.get(event.step).events.push(event);
    }
    return frames;
  }

  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (!isObject(value)) return value;
    return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  }

  function actionProposals(trace, namespace) {
    const fixtureNamespace = namespace ?? trace.find(event => event.event === 'gateway' &&
      hasText(event.synthetic_result?.data?.observation?.source?.namespace))
      ?.synthetic_result.data.observation.source.namespace;
    return traceFrames(trace).flatMap((frame, frameIndex) => {
      const event = frame.events.find(item => item.event === 'request' && item.kind === 'action');
      const request = event?.request;
      if (!hasText(request?.verb) || !Object.hasOwn(operations, request.verb) || !hasText(request.resource) ||
          !hasText(request.namespace) || !hasText(request.name) || !isObject(request.arguments)) return [];
      if (request.verb === 'scale' && !Number.isInteger(request.arguments.replicas)) return [];
      const targetNamespace = request.namespace === fixtureNamespace ? null : request.namespace;
      const response = frame.events.find(item => item.event === 'gateway');
      return [{ key: JSON.stringify([request.verb, request.resource, targetNamespace, request.name, canonical(request.arguments)]),
        namespace: targetNamespace, name: request.name, verb: request.verb, arguments: request.arguments,
        frameIndex, status: response?.status, reason: response?.reason }];
    });
  }

  function matchActionProposals(proposalsByRun) {
    if (proposalsByRun.length < 2) return [];
    const firstByRun = proposalsByRun.map(proposals => {
      const first = new Map();
      for (const proposal of proposals) if (!first.has(proposal.key)) first.set(proposal.key, proposal);
      return first;
    });
    const matches = [];
    for (const [key, proposal] of firstByRun[0]) {
      const proposals = firstByRun.map(first => first.get(key));
      if (proposals.every(Boolean)) matches.push({ ...proposal, proposals });
    }
    return matches;
  }

  function label(value) {
    return value.replace(/[_-]+/g, ' ').replace(/^\w/, letter => letter.toUpperCase());
  }

  function conditionLabel(value) {
    return Object.hasOwn(conditions, value) ? conditions[value] : label(value);
  }

  const api = { maxBytes, parseJsonl, attachTrace, groupRuns, tally, traceFrames, actionProposals, matchActionProposals,
    operations, operationFor, conditionLabel, label };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else globalThis.OpsGuardReplay = api;
})();
