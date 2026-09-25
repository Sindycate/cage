'use strict';

// Preserve the pinned collector's session/model rows before its archive folds
// their token components into a session total. This sidecar is private, contains
// no source paths or prompts, and is joined only on exact accounting evidence.
const fs = require('node:fs');
const path = require('node:path');
const { StringDecoder } = require('node:string_decoder');
const FIELDS = ['totalTokens', 'inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens'];
const MAX_BYTES = 32 * 1024 * 1024;
const integer = value => Number.isSafeInteger(value) && value >= 0;
const timestampMs = value => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null;
const injectedPrefixes = ['<environment_context>', '<system-reminder>', '<user_instructions>'];

function lines(filename, visit) {
  const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try {
    if (!fs.fstatSync(fd).isFile()) throw new Error('unsafe accounting source');
    const buffer = Buffer.alloc(65536), decoder = new StringDecoder('utf8');
    let pending = '', skipping = false, count;
    while ((count = fs.readSync(fd, buffer)) > 0) {
      pending += decoder.write(buffer.subarray(0, count));
      let end;
      while ((end = pending.indexOf('\n')) >= 0) {
        if (!skipping) visit(pending.slice(0, end));
        pending = pending.slice(end + 1); skipping = false;
      }
      // Oversized prompt/tool lines are irrelevant to token accounting.
      if (pending.length > MAX_BYTES) { pending = ''; skipping = true; }
    }
    pending += decoder.end();
    if (pending && !skipping) visit(pending);
  } finally { fs.closeSync(fd); }
}

function usage(value) {
  if (!value || !integer(value.input_tokens) || !integer(value.output_tokens)) return null;
  const cached = Math.max(value.cached_input_tokens || 0, value.cache_read_input_tokens || 0);
  const reasoning = value.reasoning_output_tokens || 0;
  if (![cached, reasoning].every(integer)) return null;
  return [value.input_tokens, value.output_tokens, cached, reasoning];
}
const equal = (a, b) => a && b && a.every((v, i) => v === b[i]);
const regresses = (a, b) => a.some((v, i) => v < b[i]);
const sum = values => values.reduce((a, b) => a + b, 0);
const day = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;

function readWrites(filename, now = new Date()) {
  const periods = { today: new Map(), month: new Map(), allTime: new Map() };
  const providerPeriods = { today: new Map(), month: new Map(), allTime: new Map() };
  let sessionId, configuredProvider, turnProvider, sawSettings = false;
  const providerName = value => typeof value === 'string' && value.trim() &&
    value.length <= 256 && !/[\x00-\x1f]/.test(value) ? value : 'unattributed';
  let model, previous, unsupported = false, sawWrites = false, tokenStart = null;
  let childId, waiting = false, replayId, inherited, inheritedReported, userFork = false;
  const startedTurns = new Set();
  const v7Time = id => typeof id === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)
    ? parseInt(id.replaceAll('-', '').slice(0, 12), 16) : null;
  function ownTurn(id) {
    if (!replayId || !childId) return true;
    const childTime = v7Time(childId), turnTime = v7Time(id);
    if (childTime === null || !id) return true;
    if (turnTime === null) return userFork || startedTurns.has(id);
    return turnTime > childTime || (turnTime === childTime && (userFork || startedTurns.has(id)));
  }
  const seen = new Set(), today = day(now);
  lines(filename, line => {
    let entry; try { entry = JSON.parse(line); } catch { return; }
    const p = entry.payload || {};
    if (entry.type === 'session_meta' && !sessionId) {
      sessionId = p.id;
      configuredProvider = providerName(p.model_provider);
    }
    if (entry.type === 'event_msg' && p.type === 'thread_settings_applied' &&
        sessionId && (p.thread_id === sessionId || (!p.thread_id && !waiting))) {
      configuredProvider = providerName(p.thread_settings?.model_provider_id);
      sawSettings = true;
    }
    // Follow the pinned Codex parser's replay gate. Exact per-model bucket
    // reconciliation below remains mandatory even after this event selection.
    if (Object.hasOwn(p.info?.last_token_usage || {}, 'cache_write_input_tokens')) sawWrites = true;
    if (waiting) {
      if (entry.type === 'turn_context' && ownTurn(p.turn_id)) {
        waiting = false; replayId = null; startedTurns.clear();
      } else {
        if (entry.type === 'session_meta' && p.id && p.id !== childId) replayId = p.id;
        if (entry.type === 'event_msg' && p.type === 'task_started' && p.turn_id) {
          const childTime = v7Time(childId), turnTime = v7Time(p.turn_id);
          if (childTime === null || (turnTime !== null ? turnTime >= childTime : p.started_at >= Math.floor(childTime / 1000))) startedTurns.add(p.turn_id);
        }
        if (entry.type === 'event_msg' && p.type === 'token_count' && p.info) {
          const total = usage(p.info.total_token_usage);
          if (total) { previous = inherited = total; inheritedReported = p.info.total_token_usage.total_tokens; }
        }
        return;
      }
    }
    if (entry.type === 'session_meta' && (p.forked_from_id || p.source?.subagent?.thread_spawn?.parent_thread_id)) {
      if (childId !== p.id) {
        childId = p.id; waiting = true; replayId = null;
        inherited = null; inheritedReported = null; startedTurns.clear();
        userFork = p.thread_source === 'user';
      }
    }
    if (entry.type === 'turn_context') {
      model = p.model_info?.slug || p.model || p.model_name;
      tokenStart = timestampMs(entry.timestamp);
      turnProvider = Object.hasOwn(p, 'model_provider') ? providerName(p.model_provider) : configuredProvider;
    }
    if (entry.type === 'event_msg' && ['task_complete', 'turn_complete', 'turn_aborted'].includes(p.type)) {
      turnProvider = undefined;
    }
    if (entry.type === 'event_msg' && p.type === 'user_message' && typeof p.message === 'string' &&
        !injectedPrefixes.some(prefix => p.message.trimStart().startsWith(prefix))) {
      tokenStart = timestampMs(entry.timestamp);
    }
    if (entry.type !== 'event_msg' || p.type !== 'token_count' || !p.info) return;
    model = p.model || p.info.model || p.info.model_name || model;
    const info = p.info, total = usage(info.total_token_usage), last = usage(info.last_token_usage);
    if (Object.hasOwn(info.last_token_usage || {}, 'cache_write_input_tokens')) sawWrites = true;
    if (inherited && total) {
      const reported = info.total_token_usage.total_tokens;
      if ((integer(reported) && integer(inheritedReported) && reported <= inheritedReported) || total.every((v, i) => v <= inherited[i])) return;
    }
    inherited = null; inheritedReported = null;
    let increment = last;
    if (total && previous) {
      if (equal(total, previous)) return;
      if (last && regresses(total, previous) && sum(total) > 0 && sum(previous) > 0 && sum(last) > 0 &&
          (sum(total) * 100 >= sum(previous) * 98 || sum(total) + sum(last) * 2 >= sum(previous))) return;
      if (!last) {
        if (regresses(total, previous)) { previous = total; return; }
        increment = total.map((v, i) => v - previous[i]);
      }
    } else if (!last) increment = total;
    if (!increment || !sum(increment)) return;
    previous = total || (previous && previous.map((v, i) => v + increment[i]));
    // Tokscale assigns usage to the request's start, not the token_count
    // completion time. Only accepted positive snapshots advance this cursor;
    // replayed/zero totals must not move an August request into September.
    const completed = timestampMs(entry.timestamp);
    const started = tokenStart ?? completed;
    if (completed !== null && (tokenStart === null || completed > tokenStart)) tokenStart = completed;
    if (!model || started === null) { unsupported = true; return; }
    const identity = JSON.stringify([model, total || [entry.timestamp, ...increment]]);
    if (seen.has(identity)) return;
    seen.add(identity);
    const cached = Math.min(increment[0], increment[2]);
    const input = increment[0] - cached;
    const rawWrite = info.last_token_usage?.cache_write_input_tokens;
    // A total-only record cannot establish writes for this increment.
    const known = last && (rawWrite === undefined || (integer(rawWrite) && rawWrite <= input));
    const writes = known ? (rawWrite || 0) : 0;
    const date = day(new Date(started));
    for (const [period, rows] of Object.entries(periods)) {
      if (period === 'today' && date !== today) continue;
      if (period === 'month' && date.slice(0, 7) !== today.slice(0, 7)) continue;
      const row = rows.get(model) || { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, cacheWriteVerified: true };
      row.inputTokens += input;
      row.outputTokens += increment[1];
      row.cacheReadTokens += cached;
      row.cacheWriteTokens += writes;
      row.cacheWriteVerified &&= Boolean(known);
      rows.set(model, row);
      const provider = turnProvider || configuredProvider || 'unattributed';
      const buckets = providerPeriods[period];
      const bucket = buckets.get(provider) || { models: new Map(), messageCount: 0, reasoningTokens: 0 };
      const parts = bucket.models.get(model) || { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, cacheWriteVerified: true };
      parts.inputTokens += input;
      parts.outputTokens += increment[1];
      parts.cacheReadTokens += cached;
      parts.cacheWriteTokens += writes;
      parts.cacheWriteVerified &&= Boolean(known);
      bucket.models.set(model, parts);
      bucket.messageCount++;
      bucket.reasoningTokens += increment[3];
      buckets.set(provider, bucket);
    }
  });
  return { periods, providerPeriods, sawSettings, unsupported, sawWrites };
}

function providerEvidence(models, sources, session) {
  const matches = [];
  for (const source of sources) {
    if (source.unsupported || !source.sawSettings) continue;
    for (const [period, rows] of Object.entries(source.periods)) {
      if (rows.size !== Object.keys(models).length || !Object.entries(models).every(([model, m]) => {
        const r = rows.get(model);
        return r && r.inputTokens === m.inputTokens + m.cacheWriteTokens &&
          r.outputTokens === m.outputTokens && r.cacheReadTokens === m.cacheReadTokens;
      })) continue;
      const buckets = [...source.providerPeriods[period]].sort(([a], [b]) => a.localeCompare(b));
      if (sum(buckets.map(([, b]) => b.messageCount)) !== session.messageCount ||
          sum(buckets.map(([, b]) => b.reasoningTokens)) !== session.reasoningTokens) continue;
      matches.push(Object.fromEntries(buckets.map(([provider, bucket]) => [provider, {
        messageCount: bucket.messageCount, reasoningTokens: bucket.reasoningTokens,
        models: Object.fromEntries([...bucket.models].sort(([a], [b]) => a.localeCompare(b)).map(([model, parts]) => {
          const writes = parts.cacheWriteVerified ? parts.cacheWriteTokens : 0;
          return [model, { ...parts, totalTokens: parts.inputTokens + parts.outputTokens + parts.cacheReadTokens,
            inputTokens: parts.inputTokens - writes, cacheWriteTokens: writes }];
        }))
      }])));
    }
  }
  const distinct = new Map(matches.map(m => [JSON.stringify(m), m]));
  return distinct.size === 1 ? distinct.values().next().value : undefined;
}

function sourceIndex(home) {
  const result = new Map();
  let count = 0;
  function walk(dir) {
    if (!fs.existsSync(dir)) return;
    if (fs.lstatSync(dir).isSymbolicLink()) return;
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      if (++count > 200000) throw new Error('too many accounting source entries');
      const filename = path.join(dir, e.name);
      if (e.isDirectory()) walk(filename);
      else if (e.isFile() && e.name.endsWith('.jsonl')) {
        const id = e.name.slice(0, -6);
        if (!result.has(id)) result.set(id, []);
        result.get(id).push(filename);
      }
    }
  }
  for (const name of ['sessions', 'archived_sessions']) walk(path.join(home, name));
  return result;
}

function componentsFor(json, normalize) {
  if (json?.groupBy !== 'client,session,model' || !Array.isArray(json.entries)) return {};
  const sessions = Object.create(null);
  for (const row of json.entries) {
    if (row.client !== 'codex') continue;
    // Reuse the pinned normalizer, including its disjoint reasoning handling.
    const values = Object.values(normalize([row]).sessions);
    if (values.length !== 1) continue;
    const s = values[0], models = Object.keys(s.models);
    if (models.length !== 1 || !FIELDS.every(k => integer(s[k]))) continue;
    const model = models[0], key = `codex:${s.sessionId}`;
    const target = sessions[key] ||= Object.create(null);
    const parts = target[model] ||= Object.fromEntries(FIELDS.map(k => [k, 0]));
    for (const k of FIELDS) parts[k] += s[k];
  }
  return sessions;
}

function addWrites(models, sources) {
  // Match each model's components, not only the total: a coincidental total
  // must never authorize a cache-write adjustment from a different window.
  const matches = [];
  for (const source of sources) {
    if (source.unsupported) continue;
    for (const rows of Object.values(source.periods)) {
      if (rows.size !== Object.keys(models).length) continue;
      if (!Object.entries(models).every(([model, m]) => {
        const r = rows.get(model);
        return r && ['inputTokens', 'outputTokens', 'cacheReadTokens'].every(k => r[k] === m[k]) && m.cacheWriteTokens === 0;
      })) continue;
      matches.push(Object.fromEntries([...rows].map(([model, r]) => [model, { writes: r.cacheWriteTokens, verified: r.cacheWriteVerified }])));
    }
  }
  const distinct = new Map(matches.map(m => [JSON.stringify(m), m]));
  const evidence = distinct.size === 1 ? distinct.values().next().value : null;
  const needsEvidence = sources.length === 0 || sources.some(s => s.sawWrites);
  for (const [model, parts] of Object.entries(models)) {
    parts.cacheWriteVerified = evidence ? evidence[model].verified : !needsEvidence;
    if (evidence && parts.cacheWriteVerified) {
      parts.cacheWriteTokens = evidence[model].writes;
      parts.inputTokens -= evidence[model].writes;
    }
  }
  return models;
}

function install() {
  const upstream = require('/opt/token-monitor/src/shared/usage');
  const normalize = upstream.extractUsageFromTokscale;
  const sources = sourceIndex(process.env.CODEX_HOME || '/scan/codex');
  const cache = new Map(), observations = [];
  for (const name of ['extractUsageFromTokscale', 'extractUsageBundleFromTokscale']) {
    const original = upstream[name];
    upstream[name] = function(json) {
      const result = original(json), period = result.period || result;
      const components = componentsFor(json, normalize);
      const sessions = Object.create(null);
      for (const [key, models] of Object.entries(components)) {
        const session = period.sessions?.[key];
        if (!session) continue;
        const filenames = sources.get(session.sessionId) || [];
        const evidence = filenames.map(filename => {
          if (!cache.has(filename)) cache.set(filename, readWrites(filename));
          return cache.get(filename);
        });
        const parts = addWrites(models, evidence);
        sessions[key] = { ...Object.fromEntries(FIELDS.map(k => [k, session[k]])),
          models: session.models, providers: session.providers,
          modelTokenUsage: parts, providerTokenUsage: providerEvidence(parts, evidence, session) };
      }
      observations.push(sessions);
      if (observations.length > 16) throw new Error('too many accounting observations');
      const body = JSON.stringify({ version: 1, observations });
      if (Buffer.byteLength(body) > MAX_BYTES) throw new Error('accounting sidecar is too large');
      const destination = path.join(process.env.TOKEN_MONITOR_SHARED_DIR || '/state', 'model-token-usage.json');
      const fd = fs.openSync(destination, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_TRUNC | fs.constants.O_NOFOLLOW, 0o600);
      try { fs.fchmodSync(fd, 0o600); fs.writeFileSync(fd, body); } finally { fs.closeSync(fd); }
      return result;
    };
  }
}

module.exports = { componentsFor, readWrites, addWrites, providerEvidence, install };
if (process.env.CAGE_MONITOR_ACCOUNTING_PRELOAD === '1') install();
