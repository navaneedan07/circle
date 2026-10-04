// Preload: the only bridge between the renderer and the main process.
// contextIsolation is on, so the renderer gets a tiny, explicit surface.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("circleDesktop", {
  isDesktop: true,
  chooseFolder: () => ipcRenderer.invoke("circle:choose-folder"),
  dataDir: () => ipcRenderer.invoke("circle:data-dir"),
});
