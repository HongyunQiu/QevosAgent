'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Minimal API exposed to tabbar.html — only what the tab bar needs.
contextBridge.exposeInMainWorld('tabAPI', {
  /** Receive tab state updates from main.js. */
  onUpdate:  cb => ipcRenderer.on('tabs-update', (_, state) => cb(state)),
  /** Tell main.js to switch to a tab. */
  activate:  id => ipcRenderer.send('tab-activate', id),
  /** Tell main.js to close a view tab (home tab is ignored in main.js). */
  close:     id => ipcRenderer.send('tab-close',    id),
  /** Tell main.js to show the settings page. */
  settings:  ()  => ipcRenderer.send('tab-settings'),

  /** Update: main pushes availability after startup check. */
  onUpdateAvailable: cb => ipcRenderer.on('update:available', (_, info) => cb(info)),
  /** Update: download progress events { percent, file }. */
  onUpdateProgress:  cb => ipcRenderer.on('update:progress',  (_, info) => cb(info)),
  /** Kick off the staged download + apply. Resolves { ok, error? }. */
  startUpdate: () => ipcRenderer.invoke('update:start'),
  /** Relaunch the app after a successful update. */
  relaunch:    () => ipcRenderer.send('update:relaunch'),
  /** Open the GitHub Releases page in the system browser (for major/minor updates). */
  openReleases: () => ipcRenderer.send('update:open-releases'),
});
