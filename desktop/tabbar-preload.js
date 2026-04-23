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
});
