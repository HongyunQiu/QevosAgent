'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const fs = require('fs');

const { getAppIconPath } = require('./icon-path');

test('windows icon path points to build/icon.ico', () => {
  const iconPath = getAppIconPath(__dirname, 'win32');

  assert.equal(iconPath, path.join(__dirname, 'build', 'icon.ico'));
  assert.equal(fs.existsSync(iconPath), true);
});

test('packaged windows icon path points to resources/icon.ico', () => {
  const iconPath = getAppIconPath('E:\\app\\resources\\app', 'win32', true);

  assert.equal(iconPath, path.join('E:\\app\\resources', 'icon.ico'));
});

test('mac and linux use platform-specific icon files', () => {
  assert.equal(getAppIconPath(__dirname, 'darwin'), path.join(__dirname, 'build', 'icon.icns'));
  assert.equal(getAppIconPath(__dirname, 'linux'), path.join(__dirname, 'build', 'icon.png'));
});
