/* Screenshot a route over CDP so we can see what the browser really renders. */
import { writeFileSync } from "node:fs";

const [, , route = "/", out = "shot.png", width = "1280", height = "900"] = process.argv;
const CDP = process.env.CDP_URL || "http://127.0.0.1:9222";
const APP = process.env.APP_URL || "http://127.0.0.1:8000";

const targets = await (await fetch(`${CDP}/json/list`)).json();
let page = targets.find((t) => t.type === "page");
if (!page) {
  page = await (await fetch(`${CDP}/json/new?about:blank`)).json();
}

// Node 22+ exposes a global WebSocket
const ws = new WebSocket(page.webSocketDebuggerUrl);
let id = 0;
const pending = new Map();
const logs = [];

ws.addEventListener("open", async () => {
  send("Page.enable");
  send("Runtime.enable");
  send("Log.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width: Number(width),
    height: Number(height),
    deviceScaleFactor: 1,
    mobile: Number(width) < 600,
  });
  await send("Page.navigate", { url: APP + route });
  await new Promise((r) => setTimeout(r, 4500));
  const shot = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(out, Buffer.from(shot.data, "base64"));
  const text = await send("Runtime.evaluate", {
    expression: "document.body.innerText.slice(0, 3000)",
    returnByValue: true,
  });
  console.log("--- rendered text ---");
  console.log(text.result.value);
  if (logs.length) {
    console.log("--- console ---");
    console.log(logs.join("\n"));
  }
  ws.close();
});

ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg);
    pending.delete(msg.id);
  }
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
    logs.push("console.error: " + JSON.stringify(msg.params.args?.[0]?.value ?? ""));
  }
  if (msg.method === "Runtime.exceptionThrown") {
    logs.push("exception: " + (msg.params.exceptionDetails?.text ?? ""));
  }
});

function send(method, params = {}) {
  return new Promise((resolve) => {
    const msgId = ++id;
    pending.set(msgId, (m) => resolve(m.result));
    ws.send(JSON.stringify({ id: msgId, method, params }));
  });
}