#!/usr/bin/env node
'use strict';

/**
 * QevosAgent — Embedded Python Setup  (cross-platform)
 * ───────────────────────────────────────────────────────
 * Windows  : Downloads Python 3.11 Embeddable (python.org), bootstraps pip.
 * macOS    : Downloads python-build-standalone (includes pip & full stdlib).
 * Linux    : Downloads python-build-standalone (includes pip & full stdlib).
 *
 * Run once before building:
 *   cd desktop && npm run setup
 *
 * Options:
 *   --force   Re-download and reinstall even if vendor/python already exists
 *
 * If downloads are slow, set an npm proxy first:
 *   npm config set proxy http://127.0.0.1:<port>
 */

const https        = require('https');
const http         = require('http');
const fs           = require('fs');
const path         = require('path');
const { execSync } = require('child_process');

// ── Config ─────────────────────────────────────────────────────────────────

const PYTHON_VERSION      = '3.11.9';
const STANDALONE_RELEASE  = '20240814';
const [MAJOR, MINOR]      = PYTHON_VERSION.split('.');

const DESKTOP_DIR    = path.resolve(__dirname, '..');
const REPO_ROOT      = path.resolve(DESKTOP_DIR, '..');
const VENDOR_DIR     = path.join(DESKTOP_DIR, 'vendor', 'python');
const VENDOR_APP_DIR = path.join(DESKTOP_DIR, 'vendor', 'app');
const REQS_FILE      = path.join(REPO_ROOT, 'requirements.txt');
const FORCE          = process.argv.includes('--force');

// Files/dirs to copy from repo root → vendor/app/ for the packaged build.
const APP_COPY_MAP = [
  { src: path.join(REPO_ROOT, 'agent'),       dest: path.join(VENDOR_APP_DIR, 'agent') },
  { src: path.join(REPO_ROOT, 'dashboard'),   dest: path.join(VENDOR_APP_DIR, 'dashboard') },
  { src: path.join(REPO_ROOT, 'run_goal.py'), dest: path.join(VENDOR_APP_DIR, 'run_goal.py') },
  { src: path.join(REPO_ROOT, 'AGENTS.md'),   dest: path.join(VENDOR_APP_DIR, 'AGENTS.md') },
  { src: path.join(REPO_ROOT, 'ADVISOR.md'),  dest: path.join(VENDOR_APP_DIR, 'ADVISOR.md') },
];

// ── Platform detection ─────────────────────────────────────────────────────

/**
 * Returns platform-specific download/path config.
 *
 * Windows  → official Python.org embeddable zip (python.exe at root)
 * macOS    → python-build-standalone install_only tarball (bin/python3)
 * Linux    → python-build-standalone install_only tarball (bin/python3)
 */
function getPlatformConfig() {
  const { platform, arch } = process;

  if (platform === 'win32') {
    const zip = `python-${PYTHON_VERSION}-embed-amd64.zip`;
    return {
      url:         `https://www.python.org/ftp/python/${PYTHON_VERSION}/${zip}`,
      archiveName: zip,
      format:      'zip',
      pythonExe:   path.join(VENDOR_DIR, 'python.exe'),
      isEmbeddable: true,
    };
  }

  // macOS / Linux — use python-build-standalone
  const tripleMap = {
    darwin: {
      x64:   'x86_64-apple-darwin',
      arm64: 'aarch64-apple-darwin',
    },
    linux: {
      x64:   'x86_64-unknown-linux-gnu',
      arm64: 'aarch64-unknown-linux-gnu',
    },
  };
  const triple = tripleMap[platform]?.[arch];
  if (!triple) {
    throw new Error(`Unsupported platform/arch: ${platform}/${arch}`);
  }

  const filename =
    `cpython-${PYTHON_VERSION}+${STANDALONE_RELEASE}-${triple}-install_only.tar.gz`;
  const url =
    `https://github.com/indygreg/python-build-standalone/releases/download/${STANDALONE_RELEASE}/${filename}`;

  return {
    url,
    archiveName:  filename,
    format:       'tar.gz',
    // python-build-standalone extracts as vendor/python/bin/python3
    pythonExe:    path.join(VENDOR_DIR, 'bin', 'python3'),
    isEmbeddable: false,
  };
}

// ── Helpers ────────────────────────────────────────────────────────────────

const step = msg => console.log(`\n▶  ${msg}`);
const ok   = msg => console.log(`   ✓  ${msg}`);
const info = msg => console.log(`   ${msg}`);

/**
 * Download url → destPath with a progress bar.
 * Follows HTTP redirects and respects https_proxy / http_proxy env vars.
 */
function download(url, destPath, label) {
  return new Promise((resolve, reject) => {
    let lastPct = -1;

    function get(u, out) {
      const mod = u.startsWith('https://') ? https : http;
      const req = mod.get(u, { headers: { 'User-Agent': 'QevosAgent-setup/1.0' } }, res => {
        if ([301, 302, 307, 308].includes(res.statusCode)) {
          res.resume();
          out.close();
          return get(res.headers.location, fs.createWriteStream(destPath));
        }
        if (res.statusCode !== 200) {
          out.close();
          return reject(new Error(`HTTP ${res.statusCode}: ${u}`));
        }
        const total  = parseInt(res.headers['content-length'] || '0', 10);
        let received = 0;
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
        res.pipe(out);
        out.on('finish', () => { out.close(); process.stdout.write('\n'); resolve(); });
        out.on('error', reject);
      });
      req.on('error', reject);
    }

    get(url, fs.createWriteStream(destPath));
  });
}

function run(cmd, opts = {}) {
  execSync(cmd, { stdio: 'inherit', ...opts });
}

/**
 * Extract archive to VENDOR_DIR.
 *
 * zip  (Windows embeddable) → PowerShell Expand-Archive into VENDOR_DIR
 * tar.gz (python-build-standalone) → `tar xzf` into vendor/ parent;
 *   the tarball root is `python/`, so it lands at vendor/python/ directly.
 */
function extract(archivePath, format) {
  if (format === 'zip') {
    run(
      `powershell -NoProfile -Command "Expand-Archive -LiteralPath '${archivePath}' -DestinationPath '${VENDOR_DIR}' -Force"`
    );
  } else {
    // tar.gz: extract to vendor/ — tarball's top-level dir is `python/`
    const parentDir = path.dirname(VENDOR_DIR); // .../desktop/vendor/
    fs.mkdirSync(parentDir, { recursive: true });
    run(`tar xzf "${archivePath}" -C "${parentDir}"`);
  }
}

/**
 * Write sitecustomize.py into Python's site-packages.
 *
 * Only needed for Windows Embeddable Python: its ._pth file overrides
 * sys.path entirely, so PYTHONPATH and cwd are silently ignored.
 * sitecustomize.py restores normal behaviour.
 */
function createSitecustomize() {
  const sitePackages = path.join(VENDOR_DIR, 'Lib', 'site-packages');
  fs.mkdirSync(sitePackages, { recursive: true });

  const content = [
    '# sitecustomize.py — auto-executed by Python at startup.',
    '# Restores cwd + PYTHONPATH into sys.path for embeddable Python.',
    'import sys, os',
    '',
    'if "" not in sys.path:',
    '    sys.path.insert(0, "")',
    '',
    'for _p in os.environ.get("PYTHONPATH", "").split(os.pathsep):',
    '    _p = _p.strip()',
    '    if _p and _p not in sys.path:',
    '        sys.path.insert(0, _p)',
  ].join('\n') + '\n';

  fs.writeFileSync(path.join(sitePackages, 'sitecustomize.py'), content, 'utf8');
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
  const config = getPlatformConfig();
  const { platform, arch } = process;

  console.log('');
  console.log('  QevosAgent — Embedded Python Setup');
  console.log('  ════════════════════════════════════');
  info(`Platform: ${platform} / ${arch}`);
  info(`Python  : ${PYTHON_VERSION}`);
  info(`Target  : ${VENDOR_DIR}`);
  info(`Reqs    : ${REQS_FILE}`);

  // ── Skip Python download if already done; always re-copy app files ──────
  if (fs.existsSync(config.pythonExe) && !FORCE) {
    console.log('');
    info('vendor/python already exists — skipping Python download.');
    info('Pass --force to re-download and reinstall.\n');
    step('Ensuring packages are up to date...');
    run(`"${config.pythonExe}" -m pip install -r "${REQS_FILE}" --no-warn-script-location -q`);
    ok('All packages up to date.');
    if (config.isEmbeddable) {
      step('Writing sitecustomize.py...');
      createSitecustomize();
      ok('sitecustomize.py written.');
    }
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

  // ── 2. Download Python ────────────────────────────────────────────────────
  step(`Downloading Python ${PYTHON_VERSION}...`);
  info(`URL: ${config.url}`);
  const archivePath = path.join(path.dirname(VENDOR_DIR), config.archiveName);
  await download(config.url, archivePath, 'python');
  ok('Download complete.');

  // ── 3. Extract ────────────────────────────────────────────────────────────
  step('Extracting...');
  extract(archivePath, config.format);
  fs.rmSync(archivePath);
  ok('Extracted.');

  // ── 4. Make executable (Mac/Linux only) ───────────────────────────────────
  if (platform !== 'win32') {
    fs.chmodSync(config.pythonExe, 0o755);
    ok(`chmod +x ${path.relative(DESKTOP_DIR, config.pythonExe)}`);
  }

  // ── 5. Windows only: patch .pth + bootstrap pip ──────────────────────────
  if (config.isEmbeddable) {
    const PTH_FILENAME = `python${MAJOR}${MINOR}._pth`;
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

    step('Bootstrapping pip...');
    const GET_PIP_URL  = 'https://bootstrap.pypa.io/get-pip.py';
    info(`URL: ${GET_PIP_URL}`);
    const getPipPath = path.join(VENDOR_DIR, 'get-pip.py');
    await download(GET_PIP_URL, getPipPath, 'get-pip.py    ');
    run(`"${config.pythonExe}" "${getPipPath}" --no-warn-script-location -q`, { cwd: VENDOR_DIR });
    fs.rmSync(getPipPath);
    ok('pip installed.');
  }

  // ── 6. Install requirements ───────────────────────────────────────────────
  // python-build-standalone includes pip; Windows embeddable pip was bootstrapped above.
  step('Installing requirements...');
  run(`"${config.pythonExe}" -m pip install -r "${REQS_FILE}" --no-warn-script-location`);
  ok('All packages installed.');

  // ── 7. Windows only: write sitecustomize.py ──────────────────────────────
  if (config.isEmbeddable) {
    step('Writing sitecustomize.py...');
    createSitecustomize();
    ok('sitecustomize.py written.');
  }

  // ── 8. Copy agent code into vendor/app/ ──────────────────────────────────
  step('Copying agent code to vendor/app/...');
  copyAppFiles();
  ok('Agent code copied.');

  console.log('');
  console.log('  ✅  Setup complete!');
  console.log(`      Python : ${config.pythonExe}`);
  console.log('');
  console.log('  Next steps:');
  if (platform === 'win32') {
    console.log('    npm start           — test in dev mode');
    console.log('    npm run build       — build Windows installer (.exe)');
  } else if (platform === 'darwin') {
    console.log('    npm start           — test in dev mode');
    console.log('    npm run build:mac   — build macOS DMG');
  } else {
    console.log('    npm start           — test in dev mode');
    console.log('    npm run build:linux — build Linux AppImage');
  }
  console.log('');
}

main().catch(err => {
  console.error('\n  ✗  Setup failed:', err.message);
  process.exit(1);
});
