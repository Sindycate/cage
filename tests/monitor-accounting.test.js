'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const { readWrites, addWrites, providerEvidence } = require('../token-monitor-accounting');
process.env.TZ = 'UTC';
const now = new Date('2026-09-15T12:00:00Z');
function context(model, timestamp) { return { type: 'turn_context', timestamp, payload: { model } }; }
function event(total, last, timestamp = '2026-09-15T10:00:00Z') {
  const usage = ([input, output, cached, write]) => ({ input_tokens: input, output_tokens: output, cached_input_tokens: cached, cache_write_input_tokens: write });
  return { type: 'event_msg', timestamp, payload: { type: 'token_count', info: { total_token_usage: usage(total), last_token_usage: usage(last) } } };
}
function cumulativeEvent(total, timestamp = '2026-09-15T10:00:00Z') {
  const [input, output, cached, reasoning = 0] = total;
  return { type: 'event_msg', timestamp, payload: { type: 'token_count', info: {
    total_token_usage: { input_tokens: input, output_tokens: output, cached_input_tokens: cached, reasoning_output_tokens: reasoning }
  } } };
}
function lastOnlyEvent([input, output, cached, write, reasoning = 0], timestamp) {
  return { type: 'event_msg', timestamp, payload: { type: 'token_count', info: {
    last_token_usage: { input_tokens: input, output_tokens: output, cached_input_tokens: cached,
      cache_write_input_tokens: write, reasoning_output_tokens: reasoning }
  } } };
}
function readFile(dir, name, events, targetModels, sharedSeen) {
  const filename = path.join(dir, name);
  fs.writeFileSync(filename, events.map(e => JSON.stringify(e)).join('\n'));
  return readWrites(filename, now, targetModels, sharedSeen);
}
function read(events, targetModels) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cage-accounting-'));
  try { const p = path.join(dir, 'session.jsonl'); fs.writeFileSync(p, events.map(e => JSON.stringify(e)).join('\n')); return readWrites(p, now, targetModels); }
  finally { fs.rmSync(dir, { recursive: true }); }
}
test('switches preserve per-model writes; duplicate totals count once', () => {
  const a = event([100, 10, 40, 50], [100, 10, 40, 50]);
  const source = read([context('model-a'), a, a, context('model-b'), event([300, 30, 140, 130], [200, 20, 100, 80])]);
  const models = { 'model-a': { totalTokens: 110, inputTokens: 60, outputTokens: 10, cacheReadTokens: 40, cacheWriteTokens: 0 }, 'model-b': { totalTokens: 220, inputTokens: 100, outputTokens: 20, cacheReadTokens: 100, cacheWriteTokens: 0 } };
  addWrites(models, [source]);
  assert.equal(models['model-a'].inputTokens, 10);
  assert.equal(models['model-a'].cacheWriteTokens, 50);
  assert.equal(models['model-b'].inputTokens, 20);
  assert.equal(models['model-b'].cacheWriteTokens, 80);
  assert.equal(models['model-b'].cacheWriteVerified, true);
});
test('a stale regression does not add usage twice; a hard reset uses last usage', () => {
  const source = read([context('model-a'), event([1000, 100, 500, 400], [1000, 100, 500, 400]), event([990, 100, 500, 390], [10, 1, 0, 10]), event([20, 2, 0, 15], [20, 2, 0, 15])]);
  assert.equal(source.periods.allTime.get('model-a').cacheWriteTokens, 415);
});
test('cross-file fork replay is skipped before keeping the child cumulative cursor', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cage-fork-accounting-'));
  try {
    const sharedSeen = new Set();
    const parentTarget = {
      'model-a': { totalTokens: 110, inputTokens: 60, outputTokens: 10, cacheReadTokens: 40, cacheWriteTokens: 0 }
    };
    const parent = readFile(dir, '01-parent.jsonl', [
      meta('parent', 'zllm'), context('model-a'), event([100, 10, 40, 50], [100, 10, 40, 50]),
    ], parentTarget, sharedSeen);
    assert.equal(parent.periods.allTime.get('model-a').cacheWriteTokens, 50);

    const childTarget = {
      'model-a': { totalTokens: 22, inputTokens: 20, outputTokens: 2, cacheReadTokens: 0, cacheWriteTokens: 0 }
    };
    const child = readFile(dir, '02-child.jsonl', [
      meta('child', 'zllm', { forked_from_id: 'parent' }), context('model-a'),
      event([100, 10, 40, 50], [100, 10, 40, 50]),
      cumulativeEvent([120, 12, 40, 0]),
    ], childTarget, sharedSeen);
    const row = child.periods.allTime.get('model-a');
    assert.deepEqual({ inputTokens: row?.inputTokens, outputTokens: row?.outputTokens, cacheReadTokens: row?.cacheReadTokens },
      { inputTokens: 20, outputTokens: 2, cacheReadTokens: 0 });
    assert.equal(row.cacheWriteTokens, 0);
    assert.equal(sharedSeen.size, 2);
  } finally { fs.rmSync(dir, { recursive: true }); }
});

test('cross-file fallback dedup uses the pinned clamped token breakdown', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cage-fallback-dedup-'));
  try {
    const sharedSeen = new Set();
    const parent = readFile(dir, '01-parent.jsonl', [
      meta('parent', 'zllm'), context('model-a', '2026-09-15T10:00:00Z'),
      lastOnlyEvent([10, 4, 3, 0, 6], '2026-09-15T10:01:00Z'),
    ], null, sharedSeen);
    assert.equal(parent.periods.allTime.get('model-a').outputTokens, 4);

    const clampedReplay = readFile(dir, '02-clamped.jsonl', [
      meta('later', 'zllm', { forked_from_id: 'parent' }), context('model-a', '2026-09-15T10:00:00Z'),
      lastOnlyEvent([10, 4, 3, 0, 8], '2026-09-15T10:02:00Z'),
    ], null, sharedSeen);
    // Tokscale clamps reasoning to output, so both raw snapshots map to the
    // same fallback identity even though the rollout values differ.
    assert.equal(clampedReplay.periods.allTime.size, 0);

    const ignoredWriteReplay = readFile(dir, '03-write.jsonl', [
      meta('write', 'zllm', { forked_from_id: 'parent' }), context('model-a', '2026-09-15T10:00:00Z'),
      lastOnlyEvent([10, 4, 3, 4, 4], '2026-09-15T10:01:00Z'),
    ], null, sharedSeen);
    // The pinned parser discards fallback cache-write data before dedup.
    assert.equal(ignoredWriteReplay.periods.allTime.size, 0);
  } finally { fs.rmSync(dir, { recursive: true }); }
});

test('mismatching counters do not authorize a write adjustment', () => {
  const source = read([context('model-a'), event([100, 10, 40, 50], [100, 10, 40, 50])]);
  const models = { 'model-a': { totalTokens: 110, inputTokens: 59, outputTokens: 11, cacheReadTokens: 40, cacheWriteTokens: 0 } };
  addWrites(models, [source]);
  assert.equal(models['model-a'].cacheWriteVerified, false);
  assert.equal(models['model-a'].cacheWriteTokens, 0);
});
test('fork replay is skipped before counting child-local writes', () => {
  const inherited = event([100, 10, 40, 50], [100, 10, 40, 50]);
  const own = event([300, 30, 140, 130], [200, 20, 100, 80]);
  const source = read([{ type: 'session_meta', payload: { id: 'child', forked_from_id: 'parent' } }, inherited, context('model-a'), inherited, own]);
  const models = { 'model-a': { totalTokens: 220, inputTokens: 100, outputTokens: 20, cacheReadTokens: 100, cacheWriteTokens: 0 } };
  addWrites(models, [source]);
  assert.equal(models['model-a'].cacheWriteVerified, true);
  assert.equal(models['model-a'].cacheWriteTokens, 80);
});

test('month attribution uses request start; duplicate snapshots do not advance it', () => {
  const old = event([100, 10, 40, 50], [100, 10, 40, 50], '2026-08-31T10:00:00Z');
  const replay = { ...old, timestamp: '2026-09-01T10:00:00Z' };
  const source = read([
    context('model-a', '2026-08-31T09:00:00Z'), old, replay,
    event([300, 30, 40, 230], [200, 20, 0, 180], '2026-09-01T10:01:00Z'),
    event([300, 30, 40, 230], [0, 0, 0, 0], '2026-09-01T10:02:00Z'),
    context('model-a', '2026-09-15T09:00:00Z'),
    event([330, 33, 40, 258], [30, 3, 0, 28]),
  ]);
  assert.equal(source.periods.allTime.get('model-a').cacheWriteTokens, 258);
  const models = { 'model-a': { totalTokens: 33, inputTokens: 30, outputTokens: 3, cacheReadTokens: 0, cacheWriteTokens: 0 } };
  addWrites(models, [source]);
  assert.equal(models['model-a'].cacheWriteVerified, true);
  assert.equal(models['model-a'].cacheWriteTokens, 28);
  assert.equal(source.periods.month.get('model-a').outputTokens, 3);
});

test('a request crossing midnight keeps its cache writes in the start day', () => {
  const source = read([
    context('model-a', '2026-09-14T23:59:00Z'),
    event([100, 10, 0, 90], [100, 10, 0, 90], '2026-09-15T00:01:00Z'),
    event([120, 12, 0, 108], [20, 2, 0, 18], '2026-09-15T00:02:00Z'),
  ]);
  assert.equal(source.periods.today.get('model-a').cacheWriteTokens, 18);
  assert.equal(source.periods.month.get('model-a').cacheWriteTokens, 108);
});

test('human input resets the request clock; injected context does not', () => {
  for (const [message, expected] of [['continue', 90], ['<div>help', 90], ['<environment_context>state', 0], [' <system-reminder>state', 0], ['<user_instructions>state', 0]]) {
    const source = read([
      context('model-a', '2026-08-31T12:00:00Z'),
      { type: 'event_msg', timestamp: '2026-09-15T10:00:00Z', payload: { type: 'user_message', message } },
      event([100, 10, 0, 90], [100, 10, 0, 90]),
    ]);
    assert.equal(source.periods.today.get('model-a')?.cacheWriteTokens || 0, expected, message);
  }
});

const meta = (id = 'session', provider = 'zllm', extra = {}) =>
  ({ type: 'session_meta', payload: { id, model_provider: provider, ...extra } });
const settings = (provider, owner = 'session') => ({
  type: 'event_msg', payload: { type: 'thread_settings_applied', thread_id: owner,
    thread_settings: { model_provider_id: provider } }
});
test('matches the upstream prefix when a rollout grows with same and new model usage', () => {
  const target = {
    'model-a': { totalTokens: 110, inputTokens: 60, outputTokens: 10, cacheReadTokens: 40, cacheWriteTokens: 0 }
  };
  const source = read([
    meta(), settings('zllm'), context('model-a'), event([100, 10, 40, 50], [100, 10, 40, 50]),
    context('model-a'), event([150, 15, 40, 80], [50, 5, 0, 30]),
    context('model-b'), event([200, 20, 60, 100], [50, 5, 20, 20]),
  ], target);
  const models = structuredClone(target);
  addWrites(models, [source]);
  assert.equal(models['model-a'].cacheWriteVerified, true);
  assert.equal(models['model-a'].cacheWriteTokens, 50);
  assert.equal(models['model-a'].inputTokens, 10);
  assert.deepEqual([...source.periods.allTime.keys()], ['model-a']);
  assert.equal(source.periods.allTime.get('model-a').cacheWriteTokens, 50);
  const providers = providerEvidence(models, [source], { messageCount: 1, reasoningTokens: 0 });
  assert.deepEqual(Object.keys(providers), ['zllm']);
  assert.equal(providers.zllm.models['model-a'].cacheWriteTokens, 50);
});

test('settings after the matched prefix do not authorize provider evidence', () => {
  const target = {
    'model-a': { totalTokens: 110, inputTokens: 60, outputTokens: 10, cacheReadTokens: 40, cacheWriteTokens: 0 }
  };
  const source = read([
    meta(), context('model-a'), event([100, 10, 40, 50], [100, 10, 40, 50]),
    settings('openai'), context('model-b'), event([200, 20, 60, 100], [100, 10, 20, 20]),
  ], target);
  const models = structuredClone(target);
  addWrites(models, [source]);
  assert.equal(models['model-a'].cacheWriteVerified, true);
  assert.equal(providerEvidence(models, [source], { messageCount: 1, reasoningTokens: 0 }), undefined);
});

test('a target with no matching source prefix keeps cache writes unverified', () => {
  const target = {
    'model-a': { totalTokens: 110, inputTokens: 59, outputTokens: 10, cacheReadTokens: 40, cacheWriteTokens: 0 }
  };
  const source = read([
    meta(), settings('zllm'), context('model-a'), event([100, 10, 40, 50], [100, 10, 40, 50]),
  ], target);
  const models = structuredClone(target);
  addWrites(models, [source]);
  assert.equal(models['model-a'].cacheWriteVerified, false);
  assert.equal(models['model-a'].cacheWriteTokens, 0);
  assert.equal(models['model-a'].inputTokens, 59);
});

test('conflicting matched source copies keep cache writes unverified', () => {
  const target = {
    'model-a': { totalTokens: 110, inputTokens: 60, outputTokens: 10, cacheReadTokens: 40, cacheWriteTokens: 0 }
  };
  const first = read([
    meta(), context('model-a'), event([100, 10, 40, 50], [100, 10, 40, 50]),
  ], target);
  const second = read([
    meta(), context('model-a'), event([100, 10, 40, 40], [100, 10, 40, 40]),
  ], target);
  const models = structuredClone(target);
  addWrites(models, [first, second]);
  assert.equal(models['model-a'].cacheWriteVerified, false);
  assert.equal(models['model-a'].cacheWriteTokens, 0);
  assert.equal(models['model-a'].inputTokens, 60);
});

function split(source, period = 'allTime') {
  const models = Object.fromEntries([...source.periods[period]].map(([model, row]) => [model, {
    totalTokens: row.inputTokens + row.outputTokens + row.cacheReadTokens,
    inputTokens: row.inputTokens, outputTokens: row.outputTokens, cacheReadTokens: row.cacheReadTokens, cacheWriteTokens: 0
  }]));
  const buckets = [...source.providerPeriods[period].values()];
  const session = { messageCount: buckets.reduce((s, b) => s + b.messageCount, 0),
    reasoningTokens: buckets.reduce((s, b) => s + b.reasoningTokens, 0) };
  addWrites(models, [source]);
  return providerEvidence(models, [source], session);
}
test('cross-file dedup identity isolates unrelated scopes, header providers, and models', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cage-dedup-scope-'));
  try {
    const sharedSeen = new Set();
    const targetFor = model => ({
      [model]: { totalTokens: 110, inputTokens: 60, outputTokens: 10, cacheReadTokens: 40, cacheWriteTokens: 0 }
    });
    const scan = (name, id, provider, parentId, model = 'model-a') => readFile(dir, name, [
      meta(id, provider, parentId ? { forked_from_id: parentId } : {}), context(model),
      event([100, 10, 40, 50], [100, 10, 40, 50]),
    ], targetFor(model), sharedSeen);

    const parent = scan('01-parent.jsonl', 'parent', 'zllm', null);
    assert.ok(parent.periods.allTime.has('model-a'));
    const sibling = scan('02-sibling.jsonl', 'sibling', 'zllm', 'parent');
    assert.equal(sibling.periods.allTime.size, 0);
    const unrelated = scan('03-unrelated.jsonl', 'unrelated', 'zllm', 'other-parent');
    assert.ok(unrelated.periods.allTime.has('model-a'));
    const otherProvider = scan('04-provider.jsonl', 'provider-child', 'openai', 'parent');
    assert.ok(otherProvider.periods.allTime.has('model-a'));
    const otherModel = scan('05-model.jsonl', 'model-child', 'zllm', 'parent', 'model-b');
    assert.ok(otherModel.periods.allTime.has('model-b'));
  } finally { fs.rmSync(dir, { recursive: true }); }
});

test('same-model provider changes and switch-back split recorded usage, not session totals', () => {
  const source = read([
    meta(), context('same-model'), event([100, 10, 40, 50], [100, 10, 40, 50]),
    settings('openai'), context('same-model'), event([300, 30, 140, 130], [200, 20, 100, 80]),
    settings('zllm'), context('same-model'), event([350, 35, 140, 170], [50, 5, 0, 40]),
  ]);
  const result = split(source);
  assert.equal(result.zllm.models['same-model'].totalTokens, 165);
  assert.equal(result.openai.models['same-model'].totalTokens, 220);
  assert.equal(result.zllm.models['same-model'].cacheWriteTokens, 90);
  assert.equal(result.openai.models['same-model'].cacheWriteTokens, 80);
  assert.equal(result.zllm.messageCount, 2);
});
test('setting changes affect future turns, not an already-started request', () => {
  const source = read([
    meta(), context('same-model'), settings('openai'),
    event([100, 10, 0, 90], [100, 10, 0, 90]),
    context('same-model'), event([200, 20, 0, 180], [100, 10, 0, 90]),
  ]);
  assert.equal(split(source).zllm.models['same-model'].totalTokens, 110);
  assert.equal(split(source).openai.models['same-model'].totalTokens, 110);
});
test('copied parent settings and foreign-thread events do not relabel child usage', () => {
  const source = read([
    meta('child', 'zllm', { forked_from_id: 'parent' }),
    settings('zllm', 'child'), meta('parent', 'openai'), settings('openai', 'parent'),
    event([100, 10, 0, 90], [100, 10, 0, 90]),
    context('same-model'), event([300, 30, 0, 270], [200, 20, 0, 180]),
    settings('openai', 'parent'),
    context('same-model'), event([400, 40, 0, 360], [100, 10, 0, 90]),
  ]);
  assert.deepEqual(Object.keys(split(source)), ['zllm']);
  assert.equal(split(source).zllm.models['same-model'].totalTokens, 330);
});
test('missing settings use legacy fallback and malformed provider affects only its interval', () => {
  assert.equal(split(read([meta(), context('model'), event([100, 10, 0, 0], [100, 10, 0, 0])])), undefined);
  const result = split(read([
    meta(), settings(null), context('model'), event([100, 10, 0, 0], [100, 10, 0, 0]),
    settings('openai'), context('model'), event([200, 20, 0, 0], [100, 10, 0, 0]),
  ]));
  assert.equal(result.unattributed.models.model.totalTokens, 110);
  assert.equal(result.openai.models.model.totalTokens, 110);
});
test('provider attribution respects period boundaries and rejects inconsistent parser totals', () => {
  const source = read([
    meta(), settings('zllm'), context('model', '2026-08-31T23:59:00Z'),
    event([100, 10, 0, 0], [100, 10, 0, 0], '2026-09-01T00:01:00Z'),
    settings('openai'), context('model', '2026-09-15T10:00:00Z'),
    event([200, 20, 0, 0], [100, 10, 0, 0]),
  ]);
  assert.deepEqual(Object.keys(split(source, 'month')), ['openai']);
  assert.deepEqual(Object.keys(split(source, 'allTime')), ['openai', 'zllm']);
  assert.equal(providerEvidence({ model: { inputTokens: 999, cacheWriteTokens: 0, outputTokens: 20, cacheReadTokens: 0 } },
    [source], { messageCount: 2, reasoningTokens: 0 }), undefined);
});

test('missing header provider uses upstream inference only for replay dedup', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cage-inferred-dedup-'));
  try {
    const seen = new Set();
    const header = meta('parent', 'anthropic');
    readFile(dir, '01-parent.jsonl', [header, context('claude-example'), event([10, 1, 0, 5], [10, 1, 0, 5])], null, seen);
    const missing = meta('child', 'unused', { forked_from_id: 'parent' });
    delete missing.payload.model_provider;
    const child = readFile(dir, '02-child.jsonl', [missing, context('claude-example'), event([10, 1, 0, 5], [10, 1, 0, 5])], null, seen);
    assert.equal(child.periods.allTime.size, 0);
    assert.equal(child.sawSettings, false);
    assert.equal(seen.size, 1);
  } finally { fs.rmSync(dir, { recursive: true }); }
});
