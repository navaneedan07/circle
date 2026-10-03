// WCAG contrast audit for the Circle design tokens and text-base
// float.
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

const LIGHT = {
  paper: "#e9e3d6",
  surface: "#f4efe3",
  ink: "#1b1914",
  muted: "#6b6558",
  line: "#d5cebf",
  lineStrong: "#868176",
  accent: "#77301f",
  accentContrast: "#f4efe3",
  moss: "#3f6349",
  ochre: "#7a5c1c",
  bronze: "#85582b",
};

const DARK = {
  paper: "#171510",
  surface: "#201d17",
  ink: "#e9e3d6",
  muted: "#a49c8a",
  line: "#3b362e",
  lineStrong: "#726757",
  accent: "#d98b6a",
  accentContrast: "#171510",
  moss: "#7fb08d",
  ochre: "#c9a24a",
  bronze: "#c98a5a",
};

// Every text-on-background pair the UI actually renders.
const PAIRS = [
  ["ink on paper", "ink", "paper", 4.5],
  ["ink on surface", "ink", "surface", 4.5],
  ["muted on paper", "muted", "paper", 4.5],
  ["muted on surface", "muted", "surface", 4.5],
  ["accent on paper", "accent", "paper", 4.5],
  ["accent on surface", "accent", "surface", 4.5],
  ["moss on surface", "moss", "surface", 4.5],
  ["moss on paper", "moss", "paper", 4.5],
  ["ochre on surface", "ochre", "surface", 4.5],
  ["ochre on paper", "ochre", "paper", 4.5],
  ["bronze on surface", "bronze", "surface", 4.5],
  ["bronze on paper", "bronze", "paper", 4.5],
  ["paper on ink (buttons)", "paper", "ink", 4.5],
  ["accentContrast on accent", "accentContrast", "accent", 4.5],
  // line-strong is the boundary that must be perceivable (3:1).
  ["lineStrong on paper", "lineStrong", "paper", 3],
  ["lineStrong on surface", "lineStrong", "surface", 3],
  // Status dots must be distinguishable as non-text graphics.
  ["moss dot on paper", "moss", "paper", 3],
  ["ochre dot on paper", "ochre", "paper", 3],
  ["bronze dot on paper", "bronze", "paper", 3],
];

let failures = 0;
for (const [themeName, tokens] of [
  ["LIGHT", LIGHT],
  ["DARK", DARK],
]) {
  console.log(`\n${themeName}`);
  for (const [label, fg, bg, min] of PAIRS) {
    const ratio = contrast(tokens[fg], tokens[bg]);
    const ok = ratio >= min;
    if (!ok) failures += 1;
    console.log(
      `  ${ok ? "pass" : "FAIL"}  ${ratio.toFixed(2).padStart(5)}  (min ${min})  ${label}`
    );
  }
}

console.log(`\n${failures === 0 ? "all pairs pass" : `${failures} failing pair(s)`}`);
process.exit(failures === 0 ? 0 : 1);