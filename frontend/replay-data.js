(() => {
  'use strict';

  const maxBytes = 8 * 1024 * 1024;
  const maxRuns = 5000;
  const conditions = { unprotected: 'Unprotected', hardened: 'Hardened prompt', 'task-policy': 'Task policy' };
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
      if (!isObject(row) || !['case_id', 'condition', 'backend', 'model_id'].every(key => hasText(row[key]))) {
        throw new Error(`${lineLabel} is missing a case ID, condition, backend, or model ID.`);
      }
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

  function groupRuns(rows) {
    const buckets = new Map();
    const groups = [];
    for (const row of rows) {
      const fixture = isObject(row.fixture_sha256)
        ? Object.keys(row.fixture_sha256).sort().map(key => [key, row.fixture_sha256[key]])
        : null;
      const key = JSON.stringify([row.case_id, row.backend, row.model_id, row.repeat ?? 1,
        row.seed ?? null, row.fixture_image ?? null, fixture]);
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

  function scaleProposals(trace) {
    const fixtureNamespace = trace.find(event => event.event === 'gateway' &&
      hasText(event.synthetic_result?.data?.observation?.source?.namespace))
      ?.synthetic_result.data.observation.source.namespace;
    return traceFrames(trace).flatMap((frame, frameIndex) => {
      const event = frame.events.find(item => item.event === 'request' && item.kind === 'action');
      const request = event?.request;
      const replicas = request?.arguments?.replicas;
      if (request?.verb !== 'scale' || request.resource !== 'deployment' ||
          !hasText(request.namespace) || !hasText(request.name) || !Number.isInteger(replicas)) return [];
      const namespace = request.namespace === fixtureNamespace ? null : request.namespace;
      const response = frame.events.find(item => item.event === 'gateway');
      return [{ key: JSON.stringify([namespace, request.name, replicas]), namespace, name: request.name, replicas,
        frameIndex, status: response?.status, reason: response?.reason }];
    });
  }

  function matchScaleProposals(proposalsByRun) {
    if (proposalsByRun.length < 2) return [];
    const firstByRun = proposalsByRun.map(proposals => {
      const first = new Map();
      for (const proposal of proposals) if (!first.has(proposal.key)) first.set(proposal.key, proposal);
      return first;
    });
    const matches = [];
    for (const [key, proposal] of firstByRun[0]) {
      const proposals = firstByRun.map(first => first.get(key));
      if (proposals.every(Boolean)) matches.push({ namespace: proposal.namespace, name: proposal.name,
        replicas: proposal.replicas, proposals });
    }
    return matches;
  }

  function label(value) {
    return value.replace(/[_-]+/g, ' ').replace(/^\w/, letter => letter.toUpperCase());
  }

  function conditionLabel(value) {
    return Object.hasOwn(conditions, value) ? conditions[value] : label(value);
  }

  const api = { maxBytes, parseJsonl, groupRuns, tally, traceFrames, scaleProposals, matchScaleProposals, conditionLabel, label };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else globalThis.OpsGuardReplay = api;
})();
