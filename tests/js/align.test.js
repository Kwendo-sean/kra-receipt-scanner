// node tests/js/align.test.js  -> checks the live long-receipt overlap tracker
const fs = require("fs");
const path = require("path");
global.window = {};
global.document = { createElement: () => ({ getContext: () => ({}) }) };
eval(fs.readFileSync(path.join(__dirname, "../../app/static/scanner.js"), "utf8"));
const { _reduce: reduce, _matchRef: matchRef } = window.Scanner;

// synthetic receipt: 240 px wide, 1200 tall, white paper with irregular "text" lines
let seed = 7;
const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const W = 240, H = 1200, FH = 320;
const img = new Float32Array(W * H).fill(235);
for (let y = 10; y < H - 20; y += 22) {
  let x = 10 + Math.floor(rnd() * 20);
  while (x < W - 20) {
    const len = 6 + Math.floor(rnd() * 30);
    for (let yy = y; yy < y + 9; yy++) for (let xx = x; xx < Math.min(W - 10, x + len); xx++) img[yy * W + xx] = 30 + rnd() * 40;
    x += len + 5 + Math.floor(rnd() * 12);
  }
}
const frame = (top) => reduce(img.subarray(top * W, (top + FH) * W), W, FH);

const ref = frame(100);
let fail = 0;
const check = (name, cond, got) => { if (!cond) { fail++; console.log("FAIL", name, JSON.stringify(got)); } else console.log("ok  ", name); };

let a = matchRef(ref, frame(100));                       // not moved yet
check("unmoved -> line at bottom", a.found && a.endFrac > 0.95 && !a.aligned, a);
a = matchRef(ref, frame(100 + 240));                     // moved 75% -> 25% overlap
check("25% overlap -> aligned near 0.25", a.found && a.aligned && Math.abs(a.endFrac - 0.25) < 0.04, a);
a = matchRef(ref, frame(100 + 120));                     // moved 37.5% -> line at ~0.625
check("halfway -> not aligned, line ~0.62", a.found && !a.aligned && Math.abs(a.endFrac - 0.625) < 0.05, a);
a = matchRef(ref, frame(100 + 300));                     // moved too far: 6% overlap
check("too far -> tooHigh or lost", !a.found || a.tooHigh, a);
a = matchRef(ref, frame(100 + 700));                     // completely different part
check("unrelated -> lost", !a.found || !a.aligned, a);
// different lighting + sensor noise + 2 px sideways drift, still 25% overlap
const noisy = (top) => {
  const f = new Float32Array(W * FH);
  for (let y = 0; y < FH; y++) for (let x = 0; x < W; x++) {
    const sx = Math.min(W - 1, x + 2);
    f[y * W + x] = img[(top + y) * W + sx] * 0.8 + 15 + (rnd() - 0.5) * 30;
  }
  return reduce(f, W, FH);
};
a = matchRef(ref, noisy(100 + 240));
check("noisy, darker, drifted -> still aligned", a.found && a.aligned, a);
const blank = { g: new Float32Array(80 * 106).fill(235), w: 80, h: 106 };
a = matchRef(blank, frame(300));
check("blank paper -> reason blank", !a.found && a.reason === "blank", a);
process.exit(fail ? 1 : 0);
