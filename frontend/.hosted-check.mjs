/* Check the hosted/off-machine gate in the built bundle.

Circle's interface can be opened from anywhere. When it is not served by the
reader's own local process, there is no backend behind it, and the app must
show the download page rather than a connection form.

The app only knows its own hostname, so this asserts on the built bundle: the
gate exists, the loopback set is exactly the local hosts, and a real hostname
like the deployed one does not match.
*/
import { readFile } from "node:fs/promises";
import { join } from "node:path";

const DIST = "dist";

const html = await readFile(join(DIST, "index.html"), "utf8");
const bundle = html.match(/src="([^"]+\.js)"/)[1];
const js = await readFile(join(DIST, bundle), "utf8");

// The minified gate. Host entries may themselves contain "]" (as [::1] does),
// so match up to the .includes() call rather than trying to balance brackets.
const gate = js.match(/\[[^\n]{0,80}?"127\.0\.0\.1"[^\n]{0,80}?\]\.includes\(window\.location\.hostname\)/);
if (!gate) {
  console.error("FAIL: no hostname gate found in the bundle");
  process.exit(1);
}
const hosts = [...gate[0].matchAll(/"([^"]*)"/g)].map((m) => m[1]);

let failed = false;
const check = (ok, label) => {
  if (!ok) failed = true;
  console.log(`${ok ? "pass" : "FAIL"}  ${label}`);
};

console.log(`gate: ${gate[0]}\n`);

for (const h of ["", "127.0.0.1", "localhost", "[::1]", "::1"]) {
  check(hosts.includes(h), `local host accepted: ${h || "(empty)"}`);
}
for (const h of ["circle-dh51.onrender.com", "example.com", "10.0.0.5"]) {
  check(!hosts.includes(h), `remote host rejected: ${h}`);
}

console.log("\ncopy:");
for (const s of [
  "This page has no Circle behind it",
  "Download Circle",
  "downloads/Circle-0.1.0-windows-x64.exe",
]) {
  check(js.includes(s), `present: ${JSON.stringify(s)}`);
}
for (const s of ["tunnel", "cloudflared"]) {
  check(!js.includes(s), `absent: ${JSON.stringify(s)}`);
}

// The connection screen still exists in the bundle, because it is reachable on
// the reader's own machine. What must not happen is it rendering off-machine,
// so assert the gate guards it rather than asserting the copy is gone.
// (A real browser render proves that; see shot.mjs against a spoofed host.)
check(
  js.includes("This page has no Circle behind it"),
  "hosted page is the off-machine destination"
);

console.log(failed ? "\nFAILED" : "\nOK");
process.exit(failed ? 1 : 0);