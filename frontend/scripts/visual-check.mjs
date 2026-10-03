// Drives headless Chrome over the DevTools Protocol (no extra dependencies)
// to render the real app and report runtime errors, layout overflow, corner
// radius, shadows and rendered contrast for both themes at two widths.

const CDP = process.env.CDP_URL || "http://127.0.0.1:9222";
const APP = process.env.APP_URL || "http://127.0.0.1:8000";
const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 390, height: 844 },
].filter(
  (v) => !process.env.ONLY_VIEWPORT || v.name === process.env.ONLY_VIEWPORT
);
// e.g. ONLY_ROUTES=/,/settings to keep a run short.
const ONLY_ROUTES = process.env.ONLY_ROUTES
  ? process.env.ONLY_ROUTES.split(",")
  : null;

// Discover a real person so the detail route renders with data.
// CIRCLE_KEY is needed when the backend runs with ACCESS_KEY set, otherwise
// this 401s and the person route is silently skipped.
async function discoverRoutes() {
  const routes = ["/", "/imports", "/settings"];
  try {
    const key = process.env.CIRCLE_KEY || "";
    const res = await fetch(`${APP}/api/people`, {
      headers: key ? { "X-Circle-Key": key } : {},
    });
    const body = await res.json();
    const people = body.people || [];
    if (people.length) routes.push(`/person/${people[0].id}`);
  } catch {
    /* dashboard only */
  }
  return routes;
}

let msgId = 0;

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    ws.addEventListener("open", () => resolve({ ws, send }));
    ws.addEventListener("error", reject);
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) {
        const { resolve: res, reject: rej } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) rej(new Error(JSON.stringify(msg.error)));
        else res(msg.result);
      }
    });

    function send(method, params = {}) {
      const id = ++msgId;
      return new Promise((res, rej) => {
        pending.set(id, { resolve: res, reject: rej });
        ws.send(JSON.stringify({ id, method, params }));
      });
    }
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Runs in the page: collects everything we want to assert on.
function pageProbe(theme) {
  const cs = (el) => getComputedStyle(el);
  const parse = (c) => {
    const m = c.match(/(\d+(?:\.\d+)?)/g);
    return m ? m.slice(0, 3).map(Number) : null;
  };
  const srgb = (v) => {
    const c = v / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const lum = ([r, g, b]) =>
    0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b);
  const ratio = (a, b) => {
    const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  // Walk up for the first opaque background behind an element.
  const bgBehind = (el) => {
    let node = el;
    while (node) {
      const c = cs(node).backgroundColor;
      const rgb = parse(c);
      if (rgb && !/rgba\(0, 0, 0, 0\)/.test(c)) return rgb;
      node = node.parentElement;
    }
    return [255, 255, 255];
  };

  const body = cs(document.body);
  const samples = [];
  const seen = new Set();
  for (const el of document.querySelectorAll("p, span, h1, h2, h3, a, button, td, th, dt, dd, div, code, summary, label, input, select, textarea")) {
    const text = (el.textContent || "").trim();
    if (!text || text.length > 80) continue;
    // only leaf-ish text nodes
    if ([...el.children].some((c) => (c.textContent || "").trim().length > 0)) continue;
    const s = cs(el);
    if (s.display === "none" || s.visibility === "hidden") continue;
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    const key = `${el.tagName}|${text.slice(0, 20)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const fg = parse(s.color);
    const bg = bgBehind(el);
    if (!fg || !bg) continue;
    samples.push({
      tag: el.tagName.toLowerCase(),
      text: text.slice(0, 34),
      fontSize: parseFloat(s.fontSize),
      ratio: +ratio(fg, bg).toFixed(2),
      fg: s.color,
    });
  }

  // Corner radius and shadows anywhere in the rendered tree.
  let rounded = 0;
  let shadowed = 0;
  for (const el of document.querySelectorAll("*")) {
    const s = cs(el);
    if (s.borderRadius && parseFloat(s.borderRadius) > 0.5) {
      rounded += 1;
      if (rounded <= 3) {
        samples.push({ tag: el.tagName.toLowerCase(), text: "[rounded]", radius: s.borderRadius });
      }
    }
    if (s.boxShadow && s.boxShadow !== "none") shadowed += 1;
  }

  const doc = document.documentElement;
  const overflow = doc.scrollWidth - doc.clientWidth;

  return {
    theme,
    title: document.title,
    nav: [...document.querySelectorAll("nav a")].map((a) => a.textContent.trim()),
    bodyBg: body.backgroundColor,
    bodyColor: body.color,
    fontFamily: body.fontFamily,
    textLength: document.body.innerText.length,
    hasCircle: document.body.innerText.includes("CIRCLE"),
    overflowPx: overflow,
    rounded,
    shadowed,
    samples: samples.filter((s) => s.ratio !== undefined),
    counts: { text: samples.length },
  };
}

async function run() {
  const targets = await (await fetch(`${CDP}/json/list`)).json();
  const page = targets.find((t) => t.type === "page");
  if (!page) throw new Error("no page target");
  const { ws, send } = await connect(page.webSocketDebuggerUrl);
  await send("Page.enable");
  await send("Runtime.enable");

  const consoleErrors = [];
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === "Runtime.exceptionThrown") {
      consoleErrors.push(
        m.params.exceptionDetails?.exception?.description ||
          m.params.exceptionDetails?.text ||
          "exception"
      );
    }
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      consoleErrors.push(
        m.params.args.map((a) => a.value ?? a.description ?? "").join(" ")
      );
    }
  });

  const discovered = await discoverRoutes();
  console.error(`[visual-check] discovered=${JSON.stringify(discovered)} ONLY_ROUTES=${JSON.stringify(ONLY_ROUTES)}`);
  const routes = ONLY_ROUTES
    ? discovered.filter((r) => ONLY_ROUTES.includes(r))
    : discovered;
  console.error(`[visual-check] routes=${JSON.stringify(routes)} viewports=${JSON.stringify(VIEWPORTS.map((v) => v.name))}`);
  const results = [];
  for (const route of routes) {
  for (const theme of ["light", "dark"]) {
    for (const vp of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", {
        width: vp.width,
        height: vp.height,
        deviceScaleFactor: 1,
        mobile: vp.name === "mobile",
      });
      // Set the persisted theme (and access key, if the backend needs one)
      // before the app boots.
      const boot = `try { localStorage.setItem('circle-theme', ${JSON.stringify(theme)}); } catch (e) {}`;
      const key = process.env.CIRCLE_KEY || "";
      const bootWithKey = key
        ? boot +
          `try { localStorage.setItem('circle-connection', JSON.stringify({baseUrl:'',accessKey:${JSON.stringify(key)}})); } catch (e) {}`
        : boot;
      await send("Page.addScriptToEvaluateOnNewDocument", {
        source: bootWithKey,
      });
      await send("Page.navigate", { url: `${APP}${route}?probe=${theme}-${vp.name}` });
      // Wait for the app to actually paint content, not just the shell.
      // Cold loads on this machine can take a while, so be patient.
      let ready = false;
      for (let i = 0; i < 120; i++) {
        await sleep(500);
        let len = 0;
        try {
          const probe = await send("Runtime.evaluate", {
            expression:
              "(document.body ? document.body.innerText.trim().length : 0)",
            returnByValue: true,
          });
          len = probe.result.value || 0;
        } catch {
          len = 0; // execution context torn down mid-navigation
        }
        // Must clear the app's loading placeholder ("Loading person",
        // "Loading…"), otherwise we would measure an empty shell.
        if (len > 600) {
          ready = true;
          break;
        }
      }
      if (!ready) {
        results.push({ route, viewport: vp.name, theme, notReady: true });
        continue;
      }
      const res = await send("Runtime.evaluate", {
        expression: `(${pageProbe.toString()})(${JSON.stringify(theme)})`,
        returnByValue: true,
        awaitPromise: false,
      });
      results.push({ route, viewport: vp.name, ...res.result.value });
    }
  }
  }

  ws.close();
  return { results, consoleErrors };
}

const out = await run();
console.log(JSON.stringify(out, null, 2));
if (out.results.some((r) => r.notReady)) process.exitCode = 1;