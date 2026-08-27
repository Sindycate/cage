#!/usr/bin/env node
'use strict';

process.umask(0o077);

// Cage runs this adapter as the entrypoint of the pinned Token Monitor image.
// The upstream agent remains the accounting authority.  Its hub request is
// terminated on loopback so the collector can retain its local archive without
// receiving Cage's real hub secret or network access.

const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const { spawn } = require('node:child_process');

const MAX_BYTES = 1024 * 1024;
const outputPath = String(process.env.CAGE_MONITOR_OUTPUT || '/out/summary.json');
const loopbackSecret = crypto.randomBytes(32).toString('hex');
let received = false;
let closed = false;
let child;

function finish(code) {
  if (closed) return;
  closed = true;
  process.exitCode = code;
  if (server.listening) {
    server.close();
  }
}

function fail(message) {
  process.stderr.write(`cage-token-monitor: ${message}\n`);
  process.exitCode = 1;
  if (child && child.exitCode === null) child.kill('SIGTERM');
  finish(1);
}

function writeOutput(body) {
  const info = fs.lstatSync(outputPath);
  if (!info.isFile() || info.nlink !== 1) throw new Error('collector output path is unsafe');
  if (info.mode & 0o077) throw new Error('collector output permissions are too broad');
  const descriptor = fs.openSync(outputPath, 'w');
  try {
    fs.writeFileSync(descriptor, body, 'utf8');
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function ensureScanDirectory(pathname) {
  let info;
  try {
    info = fs.lstatSync(pathname);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    fs.mkdirSync(pathname, { recursive: true, mode: 0o700 });
    info = fs.lstatSync(pathname);
  }
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new Error(`scan directory is unsafe: ${pathname}`);
  }
}

const server = http.createServer((request, response) => {
  if (request.method !== 'POST' || request.url !== '/api/ingest') {
    response.writeHead(404);
    response.end();
    return;
  }
  if (request.headers.authorization !== `Bearer ${loopbackSecret}`) {
    response.writeHead(401);
    response.end();
    return;
  }
  let size = 0;
  const chunks = [];
  request.on('data', (chunk) => {
    size += chunk.length;
    if (size > MAX_BYTES) {
      response.writeHead(413);
      response.end();
      request.destroy();
      return;
    }
    chunks.push(chunk);
  });
  request.on('end', () => {
    if (received) {
      response.writeHead(409);
      response.end();
      return;
    }
    try {
      const body = Buffer.concat(chunks).toString('utf8');
      const value = JSON.parse(body);
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('summary is not an object');
      writeOutput(JSON.stringify(value));
      received = true;
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end('{"ok":true}');
    } catch (error) {
      response.writeHead(400);
      response.end();
      fail(`invalid upstream summary: ${error.message}`);
    }
  });
});

server.on('error', (error) => fail(`loopback receiver failed: ${error.message}`));
server.listen(17321, '127.0.0.1', () => {
  const codexHome = String(process.env.CODEX_HOME || '/scan/codex');
  try {
    ensureScanDirectory(`${codexHome}/sessions`);
    ensureScanDirectory(`${codexHome}/archived_sessions`);
  } catch (error) {
    fail(`scan directory is unsafe: ${error.message}`);
    return;
  }
  const environment = {
    ...process.env,
    TOKEN_MONITOR_HUB_URL: 'http://127.0.0.1:17321',
    TOKEN_MONITOR_SECRET: loopbackSecret,
    TOKEN_MONITOR_CLIENTS: 'codex',
    TOKEN_MONITOR_LIMITS_ENABLED: '0',
    TOKEN_MONITOR_PROJECTS_ENABLED: '0',
    TOKEN_MONITOR_HISTORY_ENABLED: '1',
    TOKEN_MONITOR_SESSION_USAGE_ARCHIVE_ENABLED: '1',
    TOKEN_MONITOR_OPENCODE_AMBIENT: '0',
    TOKEN_MONITOR_OPENCODE_LOCAL_LIMITS: '0',
    TOKEN_MONITOR_WSL_SCAN: '0',
    TOKEN_MONITOR_WATCH: '0',
    TOKEN_MONITOR_DEVICE_ID: String(process.env.TOKEN_MONITOR_DEVICE_ID || 'cage'),
    CODEX_HOME: codexHome,
    TOKEN_MONITOR_SHARED_DIR: String(process.env.TOKEN_MONITOR_SHARED_DIR || '/state')
  };
  child = spawn(process.execPath, ['/opt/token-monitor/src/agent/agent.js', '--once'], {
    env: environment,
    stdio: ['ignore', 'ignore', 'pipe']
  });
  let stderr = '';
  child.stderr.on('data', (chunk) => {
    if (stderr.length < 8192) stderr += chunk.toString('utf8').slice(0, 8192 - stderr.length);
  });
  child.on('error', (error) => fail(`upstream agent failed to start: ${error.message}`));
  child.on('close', (code) => {
    if (code !== 0 && stderr.trim()) process.stderr.write(stderr.trim().slice(0, 8192) + '\n');
    if (code !== 0 && !received) fail(`upstream agent exited with status ${code}`);
    else if (!received) fail('upstream agent did not deliver a usage summary');
    else finish(code || 0);
  });
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    child?.kill(signal);
    finish(1);
  });
}
