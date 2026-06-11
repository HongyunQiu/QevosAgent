'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Expose a minimal, safe API to the renderer process.
// LLM/connection config is handled by the dashboard's /api/env HTTP API; the
// only remaining IPC need is the native folder picker (nicer than the
// browser-mode server-side directory browser).
contextBridge.exposeInMainWorld('electronAPI', {
  /** Open a native folder picker. Returns { canceled, path? }. */
  pickFolder: () => ipcRenderer.invoke('dialog:pickFolder'),
});
