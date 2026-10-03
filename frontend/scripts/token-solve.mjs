// Finds compliant values for the tokens the contrast audit flagged.
function srgb(c) {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}
function luminance(hex) {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  return 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b);
}
function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
const hex = (r, g, b) =>
  "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");

// Darken a hex by a factor until it clears `target` against `bg`.
function darkenUntil(color, bg, target) {
  const h = color.replace("#", "");
  const [r0, g0, b0] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  for (let f = 1; f > 0.2; f -= 0.01) {
    const c = hex(
      Math.round(r0 * f),
      Math.round(g0 * f),
      Math.round(b0 * f)
    );
    if (contrast(c, bg) >= target) return c;
  }
  return null;
}

// Lighten a hex until it clears `target` against `bg` (for dark themes).
function lightenUntil(color, bg, target) {
  const h = color.replace("#", "");
  const [r0, g0, b0] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  for (let f = 1; f < 3; f += 0.01) {
    const c = hex(
      Math.min(255, Math.round(r0 * f)),
      Math.min(255, Math.round(g0 * f)),
      Math.min(255, Math.round(b0 * f))
    );
    if (contrast(c, bg) >= target) return c;
  }
  return null;
}

// Find a line color at a target ratio against BOTH paper and surface.
function solveLine(hue, targets, target) {
  const h = hue.replace("#", "");
  const [r0, g0, b0] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  // Try both directions (darken and lighten) and keep the first hit.
  for (const step of [0.99, 1.01]) {
    for (let f = 1; f < 5 && f > 0.1; f *= step) {
      const c = hex(
        Math.min(255, Math.round(r0 * f)),
        Math.min(255, Math.round(g0 * f)),
        Math.min(255, Math.round(b0 * f))
      );
      if (targets.every((bg) => contrast(c, bg) >= target)) return c;
    }
  }
  return null;
}

const LIGHT_PAPER = "#e9e3d6";
const LIGHT_SURFACE = "#f4efe3";
const DARK_PAPER = "#171510";
const DARK_SURFACE = "#201d17";

console.log("LIGHT muted (needs 4.5 on paper + surface):",
  darkenUntil("#6d675a", LIGHT_PAPER, 4.5));

console.log("LIGHT line (3:1 on paper + surface):",
  solveLine("#c6bfae", [LIGHT_PAPER, LIGHT_SURFACE], 3));
console.log("LIGHT lineStrong (3:1 on paper + surface):",
  solveLine("#a89f8b", [LIGHT_PAPER, LIGHT_SURFACE], 3));
console.log("LIGHT lineStrong at 4.5:",
  solveLine("#a89f8b", [LIGHT_PAPER, LIGHT_SURFACE], 4.5));

console.log("DARK line (3:1 on paper + surface):",
  solveLine("#37322a", [DARK_PAPER, DARK_SURFACE], 3));
console.log("DARK lineStrong (3:1 on paper + surface):",
  solveLine("#514a3e", [DARK_PAPER, DARK_SURFACE], 3));
console.log("DARK lineStrong at 4.5:",
  solveLine("#514a3a,", [DARK_PAPER, DARK_SURFACE], 4.5));

// Sanity: confirm the fixed light muted on surface too.
const muted = darkenUntil("#6d675a", LIGHT_PAPER, 4.5);
if (muted) {
  console.log("  check muted on surface:", contrast(muted, LIGHT_SURFACE).toFixed(2));
}