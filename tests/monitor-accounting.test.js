'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const { readWrites, addWrites } = require('../token-monitor-accounting');
process.env.TZ = 'UTC';
const now = new Date('2026-09-15T12:00:00Z');
function context(model, timestamp) { return { type: 'turn_context', timestamp, payload: { model } }; }
function event(total, last, timestamp = '2026-09-15T10:00:00Z') {
  const usage = ([input, output, cached, write]) => ({ input_tokens: input, output_tokens: output, cached_input_tokens: cached, cache_write_input_tokens: write });
  return { type: 'event_msg', timestamp, payload: { type: 'token_count', info: { total_token_usage: usage(total), last_token_usage: usage(last) } } };
}
function read(events) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cage-accounting-'));
  try { const p = path.join(dir, 'session.jsonl'); fs.writeFileSync(p, events.map(e => JSON.stringify(e)).join('\n')); return readWrites(p, now); }
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
