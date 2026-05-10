'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');

const preserveListPath = path.join(__dirname, 'build', 'preserve-paths.nsh');

function readMacroPaths(macroName) {
  const raw = fs.readFileSync(preserveListPath, 'utf8');
  const lines = raw.split(/\r?\n/).map(line => line.trim());
  const start = lines.indexOf(`!macro ${macroName} Callback`);
  const end = lines.indexOf('!macroend', start);

  assert.notEqual(start, -1, `macro ${macroName} should exist`);
  assert.notEqual(end, -1, `macro ${macroName} should terminate`);

  return lines
    .slice(start + 1, end)
    .map(line => line.match(/^!insertmacro \$\{Callback\} "([^"]+)"$/))
    .filter(Boolean)
    .map(([, preservePath]) => preservePath);
}

test('preserve path list contains the required protected paths', () => {
  const paths = readMacroPaths('DefinePreservePaths');

  assert.deepEqual(paths, [
    'resources/app/vendor/app/AGENTS.md',
    'resources/app/vendor/app/ADVISOR.md',
    'resources/app/vendor/app/SKILLS',
    'resources/app/vendor/app/runs',
    'resources/app/vendor/app/memory_episodic.jsonl',
    'resources/app/vendor/app/memory_macro.md',
    'resources/app/vendor/app/agent_tools.json',
  ]);
});

test('preserve path list does not contain duplicates', () => {
  const paths = readMacroPaths('DefinePreservePaths');

  assert.equal(new Set(paths).size, paths.length);
});

test('install overwrite protected path list contains only shipped mutable files', () => {
  const paths = readMacroPaths('DefineInstallOverwriteProtectedPaths');

  assert.deepEqual(paths, [
    'resources/app/vendor/app/AGENTS.md',
    'resources/app/vendor/app/ADVISOR.md',
    'resources/app/vendor/app/SKILLS',
  ]);
});
