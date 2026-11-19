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
    // The .nsh uses Windows separators; compare on a normalised form.
    .map(([, preservePath]) => preservePath.replace(/\\/g, '/'));
}

test('preserve path list contains the required protected paths', () => {
  const paths = readMacroPaths('DefinePreservePaths');

  assert.deepEqual(paths, [
    'resources/app/vendor/app/AGENTS.md',
    'resources/app/vendor/app/ADVISOR.md',
    'resources/app/vendor/app/SKILLS',
    'resources/app/vendor/app/apps',
    'resources/app/vendor/app/app-data',
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

// apps/ is deliberately absent above: the shipped apps are samples, so each
// install should refresh them. It stays in DefinePreservePaths so a user's own
// apps (different filenames) survive an uninstall.
test('example apps are not install-overwrite protected', () => {
  const protectedPaths = readMacroPaths('DefineInstallOverwriteProtectedPaths');
  const preservedPaths = readMacroPaths('DefinePreservePaths');

  assert.ok(!protectedPaths.includes('resources/app/vendor/app/apps'));
  assert.ok(preservedPaths.includes('resources/app/vendor/app/apps'));
});

// AGENTS_*.md — per-OS/locale variant agent specs (e.g. AGENTS_WIN_CN.md).
// Unlike AGENTS.md itself (protected so a user's own edits survive an upgrade),
// these variant docs are meant to be published and refreshed on every install:
// the user receives the updated variant docs. So they must NOT appear in either
// protected path list — that is what keeps them updatable on install/upgrade.
test('AGENTS_*.md variant specs are not protected (updatable on install)', () => {
  const protectedPaths = readMacroPaths('DefineInstallOverwriteProtectedPaths');
  const preservedPaths = readMacroPaths('DefinePreservePaths');

  const variantPrefix = 'resources/app/vendor/app/AGENTS_';
  for (const p of [...protectedPaths, ...preservedPaths]) {
    assert.ok(
      !p.startsWith(variantPrefix),
      `${p} must not be protected so AGENTS_*.md stay updatable on install`
    );
  }
  // AGENTS.md itself must remain install-overwrite protected.
  assert.ok(protectedPaths.includes('resources/app/vendor/app/AGENTS.md'));
});

// macOS equivalent of the Windows install-overwrite protection: the pkg
// preinstall/postinstall scripts back up (and later restore) a fixed list of
// user-modifiable paths so a fresh bundle install does not clobber them.
// AGENTS.md must be preserved; AGENTS_*.md variant specs must NOT be, so they
// stay updatable on macOS install/upgrade just like on Windows.
function readMacosPreservePaths(scriptName) {
  const raw = fs.readFileSync(
    path.join(__dirname, 'build', 'pkg-scripts', scriptName), 'utf8');
  const m = raw.match(/PRESERVE_PATHS=\(([^)]*)\)/s);
  assert.ok(m, `${scriptName} should define PRESERVE_PATHS`);
  return m[1].split('\n')
    .map((line) => line.trim().match(/"([^"]+)"$/))
    .filter(Boolean)
    .map(([, p]) => p);
}

test('macOS preinstall/postinstall preserve AGENTS.md but not AGENTS_*.md', () => {
  for (const script of ['preinstall', 'postinstall']) {
    const paths = readMacosPreservePaths(script);

    // AGENTS.md stays protected on macOS (user edits survive an upgrade).
    assert.ok(paths.includes('AGENTS.md'),
      `${script}: AGENTS.md must be preserved`);

    // AGENTS_*.md variant specs are not preserved → updatable on install.
    for (const p of paths) {
      assert.ok(!/^AGENTS_.*\.md$/.test(p),
        `${script}: ${p} must not be preserved so AGENTS_*.md stay updatable`);
    }
  }
});
