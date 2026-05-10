'use strict';

const path = require('path');

const ICON_NAME_BY_PLATFORM = {
  darwin: 'icon.icns',
  linux: 'icon.png',
  win32: 'icon.ico',
};

function getAppIconPath(baseDir = __dirname, platform = process.platform, isPackaged = false) {
  const iconName = ICON_NAME_BY_PLATFORM[platform] || ICON_NAME_BY_PLATFORM.win32;
  if (isPackaged && platform === 'win32') {
    return path.join(path.dirname(baseDir), iconName);
  }
  return path.join(baseDir, 'build', iconName);
}

module.exports = {
  getAppIconPath,
};
