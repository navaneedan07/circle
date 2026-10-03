// Finds the elements that overflow the viewport horizontally.
const CDP = "http://127.0.0.1:9222";
const APP = process.env.APP_URL || "http://127.0.0.1:8000/";
const WIDTH = Number(process.env.W || 1280);
let msgId = 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const targets = await (await fetch(`${CDP}/json/list`)).json();
const page = targets.find((t) => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
const pending = new Map();
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    const { res } = pending.get(m.id);
    pending.delete(m.id);
    res(m.result);
  }
});
await new Promise((r) => ws.addEventListener("open", r));
const send = (method, params = {}) =>
  new Promise((res) => {
    const id = ++msgId;
    pending.set(id, { res });
    ws.send(JSON.stringify({ id, method, params }));
  });

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: WIDTH, height: 900, deviceScaleFactor: 1, mobile: false,
});
await send("Page.navigate", { url: `${APP}?ov=${Date.now()}` });
// wait for real content
for (let i = 0; i < 120; i++) {
  await sleep(500);
  const p = await send("Runtime.evaluate", {
    expression: "(document.body ? document.body.innerText.trim().length : 0)",
    returnByValue: true,
  });
  if ((p.result.value || 0) > 600) break;
}

const out = await send("Runtime.evaluate", {
  expression: `JSON.stringify((() => {
    const docW = document.documentElement.clientWidth;
    const offenders = [];
    for (const el of document.querySelectorAll("*")) {
      const r = el.getBoundingClientRect();
      if (r.height === 0) continue;
      const overRight = r.right - docW;
      const tooWide = r.width - docW;
      const scrolls = el.scrollWidth - el.clientWidth;
      const over = Math.max(overRight, tooWide, scrolls);
      if (over > 2) {
        offenders.push({
          tag: el.tagName.toLowerCase(),
          cls: (el.className || '').toString().slice(0, 110),
          right: Math.round(r.right),
          width: Math.round(r.width),
          over: Math.round(over),
          text: (el.textContent || '').trim().slice(0, 40),
          scrollW: el.scrollWidth,
          clientW: el.clientWidth,
          pos: getComputedStyle(el).position,
        });
      }
    }
    offenders.sort((a, b) => b.over - a.over);
    return { docW, scrollW: document.documentElement.scrollWidth, top: offenders.slice(0, 14) };
  })())`,
  returnByValue: true,
});

const data = JSON.parse(out.result.value);
console.log("viewport width:", data.docW, "document scrollWidth:", data.scrollW);
for (const o of data.top) {
  console.log(`  over=${o.over}px w=${o.width} tag=${o.tag} cls="${o.cls}"`);
  console.log(`      text="${o.text}" scrollW=${o.scrollW} clientW=${o.clientW}`);
}
ws.close();