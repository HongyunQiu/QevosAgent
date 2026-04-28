'use strict';

const https    = require('https');
const fs       = require('fs');
const path     = require('path');
const { app }  = require('electron');

const GITHUB_RAW   = 'https://raw.githubusercontent.com/HongyunQiu/QevosAgent/main';
const MANIFEST_URL = `${GITHUB_RAW}/update-manifest.json`;

function fetchText(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { timeout: 10000 }, res => {
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${res.statusCode}`));
      }
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(data));
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('请求超时')); });
  });
}

function downloadFile(url, destPath) {
  return new Promise((resolve, reject) => {
    fs.mkdirSync(path.dirname(destPath), { recursive: true });
    const req = https.get(url, { timeout: 30000 }, res => {
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${res.statusCode}: ${path.basename(destPath)}`));
      }
      const stream = fs.createWriteStream(destPath);
      res.pipe(stream);
      stream.on('finish', () => stream.close(resolve));
      stream.on('error', reject);
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error(`下载超时: ${path.basename(destPath)}`)); });
  });
}

function parseVersion(v) {
  return (v || '0.0.0').replace(/^v/, '').split('.').map(n => parseInt(n, 10) || 0);
}

function isNewer(remote, local) {
  const r = parseVersion(remote);
  const l = parseVersion(local);
  for (let i = 0; i < 3; i++) {
    if (r[i] > l[i]) return true;
    if (r[i] < l[i]) return false;
  }
  return false;
}

// True when only the patch segment differs — safe to apply via incremental update.
// False when major or minor changed — user must download a new installer.
function isSameMajorMinor(remote, local) {
  const r = parseVersion(remote);
  const l = parseVersion(local);
  return r[0] === l[0] && r[1] === l[1];
}

class Updater {
  constructor(vendorAppDir) {
    this.vendorAppDir = vendorAppDir;
    this.stagingDir   = path.join(vendorAppDir, '.update_staging');
    this.versionFile  = path.join(vendorAppDir, '.content_version');
  }

  getLocalVersion() {
    try { return fs.readFileSync(this.versionFile, 'utf8').trim(); }
    catch {
      // First run after a fresh install — use the embedded app version as
      // baseline so we don't re-download files already bundled in the installer.
      const base = `v${app.getVersion()}`;
      this.setLocalVersion(base);
      return base;
    }
  }

  setLocalVersion(v) {
    fs.writeFileSync(this.versionFile, v, 'utf8');
  }

  async checkForUpdate() {
    const text     = await fetchText(MANIFEST_URL);
    const manifest = JSON.parse(text);
    const current  = this.getLocalVersion();
    const latest   = manifest.version;
    return {
      current,
      latest,
      hasUpdate:       isNewer(latest, current),
      isContentUpdate: isSameMajorMinor(latest, current),
      manifest,
    };
  }

  async downloadUpdate(manifest, onProgress) {
    if (fs.existsSync(this.stagingDir)) {
      fs.rmSync(this.stagingDir, { recursive: true, force: true });
    }
    fs.mkdirSync(this.stagingDir, { recursive: true });

    const files = manifest.files;
    for (let i = 0; i < files.length; i++) {
      const filePath = files[i];
      const url  = `${GITHUB_RAW}/${filePath}`;
      const dest = path.join(this.stagingDir, filePath);
      await downloadFile(url, dest);
      if (onProgress) onProgress(Math.round(((i + 1) / files.length) * 100), filePath);
    }
  }

  applyUpdate(manifest) {
    for (const filePath of manifest.files) {
      const src  = path.join(this.stagingDir, filePath);
      const dest = path.join(this.vendorAppDir, filePath);
      if (!fs.existsSync(src)) continue;
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.copyFileSync(src, dest);
    }
    this.setLocalVersion(manifest.version);
    fs.rmSync(this.stagingDir, { recursive: true, force: true });
  }
}

module.exports = Updater;
