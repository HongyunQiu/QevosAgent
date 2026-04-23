'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Expose a minimal, safe API to the renderer process.
// All filesystem access goes through the main process.
contextBridge.exposeInMainWorld('electronAPI', {
  /** Read current .env values → { OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL, MAX_ITERS } */
  readEnv: () => ipcRenderer.invoke('env:read'),

  /** Save settings to .env file. Returns { ok: true } or throws. */
  saveEnv: data => ipcRenderer.invoke('env:save', data),

  /** Start the dashboard server and navigate the window to it. */
  openDashboard: () => ipcRenderer.invoke('dashboard:open'),

  /** Test connectivity to the LLM endpoint. Returns { ok, status?, error? }. */
  testConnection: data => ipcRenderer.invoke('env:test', data),
});
