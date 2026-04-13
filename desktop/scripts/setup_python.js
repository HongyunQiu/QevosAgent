#!/usr/bin/env node
'use strict';

/**
 * simpleAgent — Embedded Python Setup
 * ─────────────────────────────────────
 * Downloads Python 3.11 Embeddable (Windows x64), bootstraps pip,
 * and installs simpleAgent's requirements into desktop/vendor/python/.
 *
 * Run once before building:
 *   cd desktop && npm run setup
 *
 * Options:
 *   --force   Re-download and reinstall even if vendor/python already exists
 *
 * If downloads are slow, set an npm proxy first:
 *   npm config set proxy http://127.0.0.1:<port>
 * Then this script will inherit the https_proxy / http_proxy env vars.
 */

const https        = require('https');
const http         = require('http');
const fs           = require('fs');
const path         = require('path');
const { execSync } = require('child_process');

// ── Config ─────────────────────────────────────────────────────────────────

const PYTHON_VERSION = '3.11.9';
const [MAJOR, MINOR] = PYTHON_VERSION.split('.');
const PYTHON_ZIP     = `python-${PYTHON_VERSION}-embed-amd64.zip`;
const PYTHON_URL     = `https://www.python.org/ftp/python/${PYTHON_VERSION}/${PYTHON_ZIP}`;
const GET_PIP_URL    = 'https://bootstrap.pypa.io/get-pip.py';
// e.g. python311._pth
const PTH_FILENAME   = `python${MAJOR}${MINOR}._pth`;

const DESKTOP_DIR    = path.resolve(__dirname, '..');
const REPO_ROOT      = path.resolve(DESKTOP_DIR, '..');
const VENDOR_DIR     = path.join(DESKTOP_DIR, 'vendor', 'python');
const VENDOR_APP_DIR = path.join(DESKTOP_DIR, 'vendor', 'app');
const PYTHON_EXE     = path.join(VENDOR_DIR, 'python.exe');
const REQS_FILE      = path.join(REPO_ROOT, 'requirements.txt');
const FORCE          = process.argv.includes('--force');

// Files/dirs to copy from repo root → vendor/app/ for the packaged build.
const APP_COPY_MAP = [
  { src: path.join(REPO_ROOT, 'agent'),       dest: path.join(VENDOR_APP_DIR, 'agent') },
  { src: path.join(REPO_ROOT, 'dashboard'),   dest: path.join(VENDOR_APP_DIR, 'dashboard') },
  { src: path.join(REPO_ROOT, 'run_goal.py'), dest: path.join(VENDOR_APP_DIR, 'run_goal.py') },
];

// ── Helpers ────────────────────────────────────────────────────────────────

const step = msg => console.log(`\n▶  ${msg}`);
const ok   = msg => console.log(`   ✓  ${msg}`);
const info = msg => console.log(`   ${msg}`);

/**
 * Download url → destPath with a progress bar.
 * Follows HTTP redirects and respects https_proxy / http_proxy env vars
 * (set automatically when you configure npm proxy).
 */
function download(url, destPath, label) {
  return new Promise((resolve, reject) => {
    const file  = fs.createWriteStream(destPath);
    let lastPct = -1;

    function get(u) {
      const mod = u.startsWith('https://') ? https : http;
      const req = mod.get(u, { headers: { 'User-Agent': 'simpleAgent-setup/1.0' } }, res => {
        if ([301, 302, 307, 308].includes(res.statusCode)) {
          res.resume();
          return get(res.headers.location);
        }
        if (res.statusCode !== 200) {
          file.close();
          return reject(new Error(`HTTP ${res.statusCode}: ${u}`));
        }
        const total    = parseInt(res.headers['content-length'] || '0', 10);
        let received   = 0;
        res.on('data', chunk => {
          received += chunk.length;
          if (total) {
            const pct = Math.round(received / total * 100);
            if (pct !== lastPct) {
              process.stdout.write(
                `\r   ${label}: ${pct}%  (${(received / 1048576).toFixed(1)} / ${(total / 1048576).toFixed(1)} MB)`
              );
              lastPct = pct;
            }
          }
        });
        res.pipe(file);
        file.on('finish', () => { file.close(); process.stdout.write('\n'); resolve(); });
        file.on('error', reject);
      });
      req.on('error', reject);
    }

    get(url);
  });
}

function run(cmd, opts = {}) {
  execSync(cmd, { stdio: 'inherit', ...opts });
}

/**
 * Write sitecustomize.py into Python's site-packages.
 *
 * Python Embeddable uses a ._pth file that overrides sys.path completely,
 * which causes PYTHONPATH and the working directory to be silently ignored.
 * sitecustomize.py is executed by the `site` module on startup and lets us
 * restore the normal behaviour: cwd + PYTHONPATH entries go into sys.path.
 */
function createSitecustomize() {
  const sitePackages = path.join(VENDOR_DIR, 'Lib', 'site-packages');
  fs.mkdirSync(sitePackages, { recursive: true });

  const content = [
    '# sitecustomize.py — auto-executed by Python at startup.',
    '# Restores cwd + PYTHONPATH into sys.path for embeddable Python.',
    'import sys, os',
    '',
    '# 1. Add empty string (= cwd) so bare "import agent" works when',
    '#    run_goal.py is executed from the vendor/app/ directory.',
    'if "" not in sys.path:',
    '    sys.path.insert(0, "")',
    '',
    '# 2. Honour PYTHONPATH (ignored by embeddable Python._pth by default).',
    'for _p in os.environ.get("PYTHONPATH", "").split(os.pathsep):',
    '    _p = _p.strip()',
    '    if _p and _p not in sys.path:',
    '        sys.path.insert(0, _p)',
  ].join('\n') + '\n';

  const dest = path.join(sitePackages, 'sitecustomize.py');
  fs.writeFileSync(dest, content, 'utf8');
}

/** Recursively copy src directory/file → dest. */
function copyRecursive(src, dest) {
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const name of fs.readdirSync(src)) {
      copyRecursive(path.join(src, name), path.join(dest, name));
    }
  } else {
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(src, dest);
  }
}

/** Copy agent code into vendor/app/ so the packaged build is self-contained. */
function copyAppFiles() {
  fs.rmSync(VENDOR_APP_DIR, { recursive: true, force: true });
  fs.mkdirSync(VENDOR_APP_DIR, { recursive: true });
  for (const { src, dest } of APP_COPY_MAP) {
    info(`  ${path.relative(REPO_ROOT, src)}`);
    copyRecursive(src, dest);
  }
}

// ── Main ───────────────────────────────────────────────────────────────────

async function main() {
  console.log('');
  console.log('  simpleAgent — Embedded Python Setup');
  console.log('  ════════════════════════════════════');
  info(`Python  : ${PYTHON_VERSION}  (Windows x64 embeddable)`);
  info(`Target  : ${VENDOR_DIR}`);
  info(`Reqs    : ${REQS_FILE}`);

  // ── Skip Python download if already done; always re-copy app files ──────
  if (fs.existsSync(PYTHON_EXE) && !FORCE) {
    console.log('');
    info('vendor/python already exists — skipping Python download.');
    info('Pass --force to re-download and reinstall.\n');
    step('Ensuring packages are up to date...');
    run(`"${PYTHON_EXE}" -m pip install -r "${REQS_FILE}" --no-warn-script-location -q`);
    ok('All packages up to date.');
    step('Writing sitecustomize.py...');
    createSitecustomize();
    ok('sitecustomize.py written.');
    step('Copying agent code to vendor/app/...');
    copyAppFiles();
    ok('Agent code copied.');
    return;
  }

  // ── 1. Prepare directory ──────────────────────────────────────────────────
  step('Preparing vendor/python/...');
  fs.rmSync(VENDOR_DIR, { recursive: true, force: true });
  fs.mkdirSync(VENDOR_DIR, { recursive: true });
  ok('Directory ready.');

  // ── 2. Download Python embeddable ─────────────────────────────────────────
  step(`Downloading Python ${PYTHON_VERSION} embeddable...`);
  info(`URL: ${PYTHON_URL}`);
  const zipPath = path.join(VENDOR_DIR, PYTHON_ZIP);
  await download(PYTHON_URL, zipPath, 'python embeddable');
  ok('Download complete.');

  // ── 3. Extract ────────────────────────────────────────────────────────────
  step('Extracting...');
  run(
    `powershell -NoProfile -Command "Expand-Archive -LiteralPath '${zipPath}' -DestinationPath '${VENDOR_DIR}' -Force"`
  );
  fs.rmSync(zipPath);
  ok('Extracted.');

  // ── 4. Patch .pth to enable site-packages ────────────────────────────────
  step(`Patching ${PTH_FILENAME} to enable pip / site-packages...`);
  const pthPath = path.join(VENDOR_DIR, PTH_FILENAME);
  if (!fs.existsSync(pthPath)) {
    throw new Error(
      `Expected .pth file not found: ${pthPath}\n` +
      `Extraction may have failed — check that PowerShell Expand-Archive succeeded.`
    );
  }
  let pth = fs.readFileSync(pthPath, 'utf8');
  if (pth.includes('#import site')) {
    pth = pth.replace('#import site', 'import site');
    fs.writeFileSync(pthPath, pth, 'utf8');
    ok(`Uncommented "import site" in ${PTH_FILENAME}.`);
  } else if (pth.includes('import site')) {
    ok('"import site" already enabled.');
  } else {
    fs.appendFileSync(pthPath, '\nimport site\n', 'utf8');
    ok(`Appended "import site" to ${PTH_FILENAME}.`);
  }

  // ── 5. Bootstrap pip ──────────────────────────────────────────────────────
  step('Bootstrapping pip...');
  info(`URL: ${GET_PIP_URL}`);
  const getPipPath = path.join(VENDOR_DIR, 'get-pip.py');
  await download(GET_PIP_URL, getPipPath, 'get-pip.py    ');
  run(`"${PYTHON_EXE}" "${getPipPath}" --no-warn-script-location -q`, { cwd: VENDOR_DIR });
  fs.rmSync(getPipPath);
  ok('pip installed.');

  // ── 6. Install requirements ───────────────────────────────────────────────
  step('Installing requirements...');
  run(`"${PYTHON_EXE}" -m pip install -r "${REQS_FILE}" --no-warn-script-location`);
  ok('All packages installed.');

  // ── 7. Write sitecustomize.py ─────────────────────────────────────────────
  step('Writing sitecustomize.py...');
  createSitecustomize();
  ok('sitecustomize.py written.');

  // ── 8. Copy agent code into vendor/app/ ──────────────────────────────────
  step('Copying agent code to vendor/app/...');
  copyAppFiles();
  ok('Agent code copied.');

  console.log('');
  console.log('  ✅  Setup complete!');
  console.log(`      Python : ${PYTHON_EXE}`);
  console.log('');
  console.log('  Next:');
  console.log('    npm start         — test in dev mode (will use embedded Python)');
  console.log('    npm run build     — build the Windows installer (.exe)');
  console.log('');
}

main().catch(err => {
  console.error('\n  ✗  Setup failed:', err.message);
  process.exit(1);
});
