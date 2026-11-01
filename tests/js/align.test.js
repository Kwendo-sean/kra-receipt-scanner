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
const blank = reduce(new Float32Array(W * FH).fill(235), W, FH);
a = matchRef(blank, frame(300));
check("blank paper -> reason blank", !a.found && a.reason === "blank", a);

// Live view of the receipt as a hand-held phone sees it: content moved up by
// `top` rows, zoomed by `s` about the centre, shifted `dx` sideways, with the
// phone's shadow (a dark gradient) and sensor noise.
const view = (top, { s = 1, dx = 0, shadow = 0, noise = 0 } = {}) => {
  const f = new Float32Array(W * FH);
  for (let y = 0; y < FH; y++) for (let x = 0; x < W; x++) {
    const sx = Math.round(W / 2 + (x - W / 2) / s - dx), sy = Math.round(top + y / s);
    const v = sx >= 0 && sx < W && sy >= 0 && sy < H ? img[sy * W + sx] : 90;
    const light = 1 - shadow * (x / W) * (1 - y / FH);
    f[y * W + x] = v * light + (rnd() - 0.5) * noise;
  }
  return reduce(f, W, FH);
};
// Track a slow slide down to the target, frame by frame, the way the scanner does.
const slide = (opts, steps = 20) => {
  let pose = { s: 1, tx: 0, ty: 0, vy: 0 }, worst = 0, last = null;
  for (let k = 1; k <= steps; k++) {
    // zoom (phone drifting closer) builds up gradually over the slide
    const moved = 240 * k / steps, sc = 1 + ((opts.s || 1) - 1) * k / steps;
    const r = matchRef(ref, view(100 + moved, Object.assign({}, opts, { s: sc })), pose);
    if (!r.found) return { lost: k };
    pose = r.pose; last = r;
    const want = sc * (FH - moved) / FH;
    worst = Math.max(worst, Math.abs(r.endFrac - want));
  }
  return { worst, end: last.endFrac, aligned: last.aligned };
};
let t = slide({});
check("slide: line follows within 1.5% the whole way", !t.lost && t.worst < 0.015, t);
t = slide({ shadow: 0.55, noise: 30 });
check("slide with phone shadow + noise: within 2.5%", !t.lost && t.worst < 0.025 && t.aligned, t);
t = slide({ dx: 9 });
check("slide with sideways drift: within 2%", !t.lost && t.worst < 0.02, t);
t = slide({ s: 1.08 });
check("slide while drifting 8% closer: within 2.5%", !t.lost && t.worst < 0.025, t);
t = slide({}, 8);
check("fast slide (8 steps): within 2%", !t.lost && t.worst < 0.02, t);
// lost -> full search re-acquires at the right place
a = matchRef(ref, view(100 + 180, { shadow: 0.4, noise: 20 }), null);
check("re-acquire without a track", a.found && Math.abs(a.endFrac - 0.4375) < 0.025, a);

// Repeated identical lines (e.g. "1 x BREAD 50.00" twice): the track must not
// hop to the twin line one pitch away.
const rep = new Float32Array(W * H).fill(235);
for (let y = 10; y < H - 20; y += 22) {
  const motif = Math.floor(y / 88);                // 4 identical lines in a row
  let s2 = motif * 9973 + 1, r2 = () => ((s2 = (s2 * 16807) % 2147483647) / 2147483647), x = 12;
  while (x < W - 20) {
    const len = 6 + Math.floor(r2() * 30);
    for (let yy = y; yy < y + 9; yy++) for (let xx = x; xx < Math.min(W - 10, x + len); xx++) rep[yy * W + xx] = 40;
    x += len + 5 + Math.floor(r2() * 12);
  }
}
const rview = (top) => reduce(rep.subarray(top * W, (top + FH) * W), W, FH);
const rref = rview(100);
let pose = { s: 1, tx: 0, ty: 0, vy: 0 }, worst = 0, lost = false;
for (let k = 1; k <= 20; k++) {
  const moved = 12 * k, r = matchRef(rref, rview(100 + moved), pose);
  if (!r.found) { lost = true; break; }
  pose = r.pose;
  worst = Math.max(worst, Math.abs(r.endFrac - (FH - moved) / FH));
}
check("repeated lines: no hop to the twin line", !lost && worst < 0.02, { lost, worst });
process.exit(fail ? 1 : 0);
