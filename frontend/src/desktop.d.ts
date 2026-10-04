/** The bridge the Electron preload exposes. Absent in a browser. */
interface CircleDesktopBridge {
  isDesktop: boolean;
  chooseFolder(): Promise<string | null>;
  dataDir(): Promise<string>;
}

interface Window {
  circleDesktop?: CircleDesktopBridge;
}
