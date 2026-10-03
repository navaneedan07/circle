// Focused diagnostic: what is actually on the page during the first
// navigation, and does it change over time?
const CDP = "http://127.0.0.1:9222";
const APP = process.env.APP_URL || "http://127.0.0.1:8000/";
let msgId = 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const targets = await (await fetch(`${CDP}/json/list`)).json();
const page = targets.find((t) => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map();
const events = [];
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    const { res, rej } = pending.get(m.id);
    pending.delete(m.id);
    m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
  } else if (m.method) {
    events.push(m.method);
  }
});
await new Promise((r) => ws.addEventListener("open", r));
const send = (method, params = {}) =>
  new Promise((res, rej) => {
    const id = ++msgId;
    pending.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params }));
  });

await send("Page.enable");
await send("Runtime.enable");
await send("Network.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: 1280, height: 900, deviceScaleFactor: 1, mobile: false,
});
await send("Page.addScriptToEvaluateOnNewDocument", {
  source: `try { localStorage.setItem('circle-theme', 'light'); } catch (e) {}`,
});

console.log("navigating to", APP);
await send("Page.navigate", { url: APP });

for (let i = 0; i < 12; i++) {
  await sleep(1000);
  const r = await send("Runtime.evaluate", {
    expression: `JSON.stringify({
      ready: document.readyState,
      url: location.href,
      htmlLen: document.documentElement.outerHTML.length,
      bodyLen: (document.body ? document.body.innerText : '').trim().length,
      rootChildren: document.getElementById('root') ? document.getElementById('root').children.length : -1,
      cls: document.documentElement.className,
      scripts: [...document.scripts].map(s => s.src.split('/').pop()).slice(0,4)
    })`,
    returnByValue: true,
  });
  console.log(`t+${i + 1}s`, r.result.value);
}

const failed = await send("Runtime.evaluate", {
  expression: `window.__errs ? JSON.stringify(window.__errs) : 'none'`,
  returnByValue: true,
});
console.log("page errors:", failed.result.value);
console.log("event kinds:", [...new Set(events)].join(", "));
ws.close();