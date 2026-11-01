// node tests/js/quad.test.js -> live "document box" paper detector
const fs = require("fs");
const path = require("path");
global.window = {};
global.document = { createElement: () => ({ getContext: () => ({}) }) };
eval(fs.readFileSync(path.join(__dirname, "../../app/static/scanner.js"), "utf8"));
const find = window.Scanner._findPaperQuad;

const W = 160, H = 213;
function scene(corners, paper = 225, table = 70) {
  const d = new Uint8ClampedArray(W * H * 4);
  const inside = (x, y) => {           // point-in-convex-polygon
    let sign = 0;
    for (let i = 0; i < 4; i++) {
      const [x1, y1] = corners[i], [x2, y2] = corners[(i + 1) % 4];
      const c = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1);
      if (c !== 0) { if (sign === 0) sign = Math.sign(c); else if (Math.sign(c) !== sign) return false; }
    }
    return true;
  };
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    let v = inside(x + 0.5, y + 0.5) ? paper : table;
    if (v === paper && (y % 9 === 0) && x % 4 && inside(x - 6, y) && inside(x + 6, y)) v = 40; // "print"
    const i = (y * W + x) * 4; d[i] = d[i + 1] = d[i + 2] = v; d[i + 3] = 255;
  }
  return d;
}
let fail = 0;
const check = (name, cond, got) => { if (!cond) { fail++; console.log("FAIL", name, JSON.stringify(got)); } else console.log("ok  ", name); };
const near = (a, b, tol) => Math.hypot(a[0] - b[0], a[1] - b[1]) <= tol;

const tilted = [[52, 30], [118, 38], [104, 190], [38, 182]];       // tl, tr, br, bl
let q = find(scene(tilted), W, H);
check("tilted receipt corners found", q && q.every((p, i) => near(p, tilted[i], 4)), q);
q = find(scene([[0, 0], [1, 0], [1, 1], [0, 1]]), W, H);
check("no paper -> null", q === null, q);
q = find(scene([[0, 0], [W, 0], [W, H], [0, H]]), W, H);
check("paper fills frame -> null", q === null, q);
process.exit(fail ? 1 : 0);
