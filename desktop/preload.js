"use strict";
// The only things the page can ask of the app. Nothing else from Electron or Node is exposed.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("casefileDesktop", {
  pickFolder: () => ipcRenderer.invoke("pick-folder"),
  startModelServer: () => ipcRenderer.invoke("start-model-server"),
  openOllamaDownload: () => ipcRenderer.invoke("open-ollama-download"),
});
