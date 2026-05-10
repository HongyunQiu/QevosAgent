'use strict';

/**
 * electron-builder afterPack hook — embeds the custom icon and version info
 * into the .exe PE resources on Windows without relying on winCodeSign
 * (which requires symbolic-link privileges to extract on Windows).
 */

const path       = require('path');
const { execFileSync } = require('child_process');

const RCEDIT_EXE = path.join(
  __dirname, '..', 'node_modules', 'rcedit', 'bin',
  process.arch === 'x64' ? 'rcedit-x64.exe' : 'rcedit.exe'
);

module.exports = async function afterPack(context) {
  if (context.electronPlatformName !== 'win32') return;

  const { version } = context.packager.appInfo;
  const exeName  = context.packager.appInfo.productFilename + '.exe';
  const exePath  = path.join(context.appOutDir, exeName);
  const iconPath = path.join(__dirname, '..', 'build', 'icon.ico');

  console.log(`[afterPack] Embedding icon into ${exePath}`);

  execFileSync(RCEDIT_EXE, [
    exePath,
    '--set-icon',            iconPath,
    '--set-version-string',  'CompanyName',      'QevosAgent',
    '--set-version-string',  'FileDescription',  'QevosAgent Desktop App',
    '--set-version-string',  'ProductName',      'QevosAgent',
    '--set-version-string',  'InternalName',     'QevosAgent',
    '--set-version-string',  'OriginalFilename', exeName,
    '--set-file-version',    version,
    '--set-product-version', version,
  ]);

  console.log('[afterPack] Icon embedded successfully');
};
