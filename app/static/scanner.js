// Live receipt scanner: rear camera, sharpness (variance of Laplacian on a
// downscaled frame), stability detection, auto-capture, full-resolution stills.
(function () {
  "use strict";

  const SAMPLE_W = 240;          // downscaled analysis width
  const SAMPLE_MS = 100;          // ~10 Hz: small steps between samples keep the overlap track locked
  const MIN_SHARP = 60;          // absolute floor (Laplacian variance at SAMPLE_W)
  const REL_SHARP = 0.8;         // and >= 80% of the recent best
  const STABLE_DIFF = 5;         // mean abs pixel diff between samples
  const STABLE_SAMPLES = 4;      // ~0.55 s steady
  const COOLDOWN_MS = 1800;
  // Long receipts: the bottom STRIP of the last section must reappear at the top
  // of the live frame. TARGET is where its end should sit (~25% overlap).
  const STRIP = 0.25;
  const TARGET = 0.25, ALIGN_LO = 0.15, ALIGN_HI = 0.36;
  const MATCH_W = 96;            // matching resolution
  const HP_R = 6;                // local-mean radius removed before matching (shadows, light falloff)
  const MIN_SCORE = 0.72;        // zero-mean normalised cross-correlation of the print texture
  const MIN_OVERLAP = 0.12;      // fraction of the previous section that must still be in view
  const DRIFT_C = 5;             // sideways search, half-size pixels (~10% of the width)
  const PULL = 0.006;            // NCC penalty per row away from the predicted position
  const ALIGN_HOLD = 2;          // consecutive aligned samples before auto-capture

  class Scanner {
    constructor(opts) {
      this.video = opts.video;
      this.guide = opts.guide;
      this.onSample = opts.onSample || (() => {});
      this.onAutoCapture = opts.onAutoCapture || (() => {});
      this.onAlign = opts.onAlign || (() => {});
      this.onQuad = opts.onQuad || (() => {});
      this.qcanvas = document.createElement("canvas");
      this.qctx = this.qcanvas.getContext("2d", { willReadFrequently: true });
      this.quad = null;
      this.tick = 0;
      this.ref = null;           // reduced gray of the last long-receipt section
      this.align = null;
      this.lastReduced = null;
      this.pose = null;          // tracked position of the previous section
      this.misses = 0;
      this.alignHold = 0;
      this.shownEnd = null;
      this.stream = null;
      this.track = null;
      this.imageCapture = null;
      this.timer = null;
      this.small = document.createElement("canvas");
      this.sctx = this.small.getContext("2d", { willReadFrequently: true });
      this.prev = null;
      this.stableCount = 0;
      this.recent = [];
      this.armed = true;
      this.auto = true;
      this.lastCapture = 0;
    }

    async start() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error(window.isSecureContext
          ? "This browser cannot open the camera."
          : "Camera needs HTTPS. Install and trust the Pi's certificate (see README), then reload.");
      }
      const constraints = {
        audio: false,
        video: { facingMode: { ideal: "environment" }, width: { ideal: 4096 }, height: { ideal: 3072 } },
      };
      this.stream = await navigator.mediaDevices.getUserMedia(constraints);
      this.track = this.stream.getVideoTracks()[0];
      // ask for the highest resolution the camera reports
      try {
        const caps = this.track.getCapabilities ? this.track.getCapabilities() : {};
        const adv = {};
        if (caps.width && caps.width.max) adv.width = caps.width.max;
        if (caps.height && caps.height.max) adv.height = caps.height.max;
        if (caps.focusMode && caps.focusMode.includes("continuous")) adv.focusMode = "continuous";
        if (Object.keys(adv).length) await this.track.applyConstraints({ advanced: [adv] });
      } catch (e) { /* keep what we got */ }
      if ("ImageCapture" in window) {
        try { this.imageCapture = new window.ImageCapture(this.track); } catch (e) { this.imageCapture = null; }
      }
      this.video.srcObject = this.stream;
      await this.video.play().catch(() => {});
      this.timer = setInterval(() => this.sample(), SAMPLE_MS);
    }

    stop() {
      clearInterval(this.timer);
      this.timer = null;
      if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
    }

    get running() { return !!this.stream; }

    // Region of the video under the on-screen guide frame (video is object-fit: cover)
    guideRect() {
      const v = this.video, vw = v.videoWidth, vh = v.videoHeight;
      if (!vw || !vh) return null;
      const vr = v.getBoundingClientRect(), gr = this.guide.getBoundingClientRect();
      const scale = Math.max(vr.width / vw, vr.height / vh);
      const dispW = vw * scale, dispH = vh * scale;
      const offX = (vr.width - dispW) / 2, offY = (vr.height - dispH) / 2;
      const x = Math.max(0, (gr.left - vr.left - offX) / scale);
      const y = Math.max(0, (gr.top - vr.top - offY) / scale);
      const w = Math.min(vw - x, gr.width / scale);
      const h = Math.min(vh - y, gr.height / scale);
      return { x, y, w, h, vw, vh };
    }

    sample() {
      const g = this.guideRect();
      if (!g || this.video.readyState < 2) return;
      if (this.tick++ % 2 === 0) this.detectQuad();
      const sw = SAMPLE_W, sh = Math.max(1, Math.round(g.h * sw / g.w));
      this.small.width = sw; this.small.height = sh;
      this.sctx.drawImage(this.video, g.x, g.y, g.w, g.h, 0, 0, sw, sh);
      const d = this.sctx.getImageData(0, 0, sw, sh).data;
      const n = sw * sh, gray = new Float32Array(n);
      for (let i = 0, j = 0; i < n; i++, j += 4) gray[i] = 0.299 * d[j] + 0.587 * d[j + 1] + 0.114 * d[j + 2];
      // variance of the 4-neighbour Laplacian
      let sum = 0, sum2 = 0, cnt = 0;
      for (let y = 1; y < sh - 1; y++) {
        for (let x = 1; x < sw - 1; x++) {
          const i = y * sw + x;
          const l = gray[i - 1] + gray[i + 1] + gray[i - sw] + gray[i + sw] - 4 * gray[i];
          sum += l; sum2 += l * l; cnt++;
        }
      }
      const mean = sum / cnt, sharp = sum2 / cnt - mean * mean;
      // stability: mean abs difference vs previous sample
      let diff = 255;
      if (this.prev && this.prev.length === n) {
        let s = 0;
        for (let i = 0; i < n; i += 3) s += Math.abs(gray[i] - this.prev[i]);
        diff = s / (n / 3);
      }
      this.prev = gray;
      this.lastReduced = reduce(gray, sw, sh);
      if (this.ref) this.followRef();
      this.stableCount = diff < STABLE_DIFF ? this.stableCount + 1 : 0;
      const now = Date.now();
      this.recent.push({ t: now, s: sharp });
      this.recent = this.recent.filter((r) => now - r.t < 3000);
      const best = Math.max(...this.recent.map((r) => r.s));
      const threshold = Math.max(MIN_SHARP, best * REL_SHARP);
      const isSharp = sharp >= threshold && sharp >= MIN_SHARP;
      const stable = this.stableCount >= STABLE_SAMPLES;
      this.onSample({ sharp, threshold: MIN_SHARP, isSharp, stable, diff });
      const placed = !this.ref || (this.align && this.align.aligned && this.alignHold >= ALIGN_HOLD);
      if (this.auto && this.armed && placed && isSharp && stable && now - this.lastCapture > COOLDOWN_MS) {
        this.lastCapture = now;
        this.onAutoCapture();
      }
    }

    // Live "document box": find the receipt paper in the whole frame and report
    // its corners in screen pixels (like the iPhone's document scanner outline).
    detectQuad() {
      const v = this.video, vw = v.videoWidth, vh = v.videoHeight;
      if (!vw || !vh) return;
      const qw = 160, qh = Math.round(vh * qw / vw);
      this.qcanvas.width = qw; this.qcanvas.height = qh;
      this.qctx.drawImage(v, 0, 0, qw, qh);
      const pts = findPaperQuad(this.qctx.getImageData(0, 0, qw, qh).data, qw, qh);
      if (!pts) {
        this.quad = null;
        this.onQuad(null);
        return;
      }
      // smooth corner jitter between samples
      if (this.quad) for (let i = 0; i < 4; i++) {
        pts[i][0] = 0.5 * pts[i][0] + 0.5 * this.quad[i][0];
        pts[i][1] = 0.5 * pts[i][1] + 0.5 * this.quad[i][1];
      }
      this.quad = pts;
      const vr = v.getBoundingClientRect();
      const scale = Math.max(vr.width / vw, vr.height / vh);
      const offX = (vr.width - vw * scale) / 2, offY = (vr.height - vh * scale) / 2;
      const k = vw / qw;
      this.onQuad(pts.map(([x, y]) => [offX + x * k * scale, offY + y * k * scale]));
    }

    // Long receipts: remember the section just captured (reduced gray, for live
    // matching) and return a crisp crop of its bottom strip for the overlay.
    snapshot() {
      if (!this.lastReduced) return null;
      const g = this.guideRect();
      let ghost = null;
      if (g) {
        const w = 540, h = Math.round(g.h * w / g.w), sh = Math.max(1, Math.round(h * STRIP));
        const c = document.createElement("canvas");
        c.width = w; c.height = sh;
        c.getContext("2d").drawImage(this.video, g.x, g.y + g.h * (1 - STRIP), g.w, g.h * STRIP, 0, 0, w, sh);
        ghost = c.toDataURL("image/jpeg", 0.8);
      }
      return { ref: this.lastReduced, ghost };
    }

    setReference(snap) {
      this.ref = snap ? snap.ref : null;
      this.align = null;
      // right after a capture the view IS the reference: start tracking from "not moved"
      this.pose = { s: 1, tx: 0, ty: 0, vy: 0 };
      this.misses = 0;
      this.alignHold = 0;
      this.shownEnd = null;
    }

    // Follow the previous section frame to frame; smooth the line, but let real
    // jumps (re-acquired after a loss) through at once.
    followRef() {
      const a = matchRef(this.ref, this.lastReduced, this.pose);
      if (a.found) {
        this.pose = a.pose;
        this.misses = 0;
        const e = a.endFrac;
        this.shownEnd = this.shownEnd === null || Math.abs(e - this.shownEnd) > 0.08 ? e : 0.55 * e + 0.45 * this.shownEnd;
        a.endFrac = this.shownEnd;
        a.aligned = a.endFrac >= ALIGN_LO && a.endFrac <= ALIGN_HI;
        a.tooHigh = a.endFrac < ALIGN_LO;
        this.alignHold = a.aligned ? this.alignHold + 1 : 0;
      } else {
        this.alignHold = 0;
        if (++this.misses >= 3) { this.pose = null; this.shownEnd = null; }   // re-acquire with a full search
      }
      this.align = a;
      this.onAlign(a);
    }

    // Full-resolution JPEG still. Uses ImageCapture.takePhoto when available
    // (true sensor resolution on Android Chrome), else a frame from the video.
    async capture() {
      this.lastCapture = Date.now();
      let blob = null;
      if (this.imageCapture && this.imageCapture.takePhoto) {
        try {
          const photo = await this.imageCapture.takePhoto();
          if (photo && photo.size > 50000) blob = await this.reencode(photo);
        } catch (e) { blob = null; }
      }
      if (!blob) {
        const v = this.video, c = document.createElement("canvas");
        c.width = v.videoWidth; c.height = v.videoHeight;
        c.getContext("2d").drawImage(v, 0, 0);
        blob = await new Promise((res) => c.toBlob(res, "image/jpeg", 0.92));
      }
      return { blob, sharpness: this.recent.length ? this.recent[this.recent.length - 1].s : null };
    }

    // takePhoto may return PNG/WebP on some devices; the Pi accepts JPEG only.
    async reencode(photo) {
      if (photo.type === "image/jpeg") return photo;
      const bmp = await createImageBitmap(photo);
      const c = document.createElement("canvas");
      c.width = bmp.width; c.height = bmp.height;
      c.getContext("2d").drawImage(bmp, 0, 0);
      return await new Promise((res) => c.toBlob(res, "image/jpeg", 0.92));
    }
  }

  // Largest bright region (paper) via Otsu threshold + flood fill, then its
  // four extreme corners. Returns [[x,y] x4] (tl,tr,br,bl) or null.
  function findPaperQuad(d, w, h) {
    const n = w * h, gray = new Uint8Array(n), hist = new Uint32Array(256);
    for (let i = 0, j = 0; i < n; i++, j += 4) {
      const g = (d[j] * 77 + d[j + 1] * 150 + d[j + 2] * 29) >> 8;
      gray[i] = g; hist[g]++;
    }
    // Otsu
    let sum = 0;
    for (let t = 0; t < 256; t++) sum += t * hist[t];
    let sumB = 0, wB = 0, best = 0, thr = 128;
    for (let t = 0; t < 256; t++) {
      wB += hist[t]; if (!wB) continue;
      const wF = n - wB; if (!wF) break;
      sumB += t * hist[t];
      const mB = sumB / wB, mF = (sum - sumB) / wF, between = wB * wF * (mB - mF) * (mB - mF);
      if (between > best) { best = between; thr = t; }
    }
    const lab = new Int32Array(n), stack = new Int32Array(n);
    let bestLab = 0, bestArea = 0, cur = 0;
    for (let s = 0; s < n; s++) {
      if (lab[s] || gray[s] <= thr) continue;
      cur++; let sp = 0, area = 0; stack[sp++] = s; lab[s] = cur;
      while (sp) {
        const p = stack[--sp]; area++;
        const x = p % w, y = (p - x) / w;
        if (x > 0 && !lab[p - 1] && gray[p - 1] > thr) { lab[p - 1] = cur; stack[sp++] = p - 1; }
        if (x < w - 1 && !lab[p + 1] && gray[p + 1] > thr) { lab[p + 1] = cur; stack[sp++] = p + 1; }
        if (y > 0 && !lab[p - w] && gray[p - w] > thr) { lab[p - w] = cur; stack[sp++] = p - w; }
        if (y < h - 1 && !lab[p + w] && gray[p + w] > thr) { lab[p + w] = cur; stack[sp++] = p + w; }
      }
      if (area > bestArea) { bestArea = area; bestLab = cur; }
    }
    const frac = bestArea / n;
    if (frac < 0.06 || frac > 0.95) return null;   // no paper, or paper fills the frame
    let tl = [0, 0, Infinity], tr = [0, 0, -Infinity], br = [0, 0, -Infinity], bl = [0, 0, Infinity];
    let minX = w, maxX = 0, minY = h, maxY = 0;
    for (let p = 0; p < n; p++) {
      if (lab[p] !== bestLab) continue;
      const x = p % w, y = (p - x) / w, s1 = x + y, s2 = x - y;
      if (x < minX) minX = x; if (x > maxX) maxX = x; if (y < minY) minY = y; if (y > maxY) maxY = y;
      if (s1 < tl[2]) tl = [x, y, s1];
      if (s1 > br[2]) br = [x, y, s1];
      if (s2 > tr[2]) tr = [x, y, s2];
      if (s2 < bl[2]) bl = [x, y, s2];
    }
    // touching 3+ frame edges = paper fills the view: no outline to draw
    const edges = (minX === 0) + (maxX === w - 1) + (minY === 0) + (maxY === h - 1);
    if (edges >= 3) return null;
    const rough = [[tl[0], tl[1]], [tr[0] + 1, tr[1]], [br[0] + 1, br[1] + 1], [bl[0], bl[1] + 1]];
    return refineQuad(lab, bestLab, w, h, rough);
  }

  // Document-scanner style corners: fit a straight line to each side of the
  // paper's outline (ignoring the frame border and the corner zones, where
  // torn/curled corners live) and intersect neighbouring sides.
  function refineQuad(lab, id, w, h, q) {
    const sides = [[], [], [], []];
    const segDist = (x, y, a, b) => {
      const vx = b[0] - a[0], vy = b[1] - a[1], L2 = vx * vx + vy * vy || 1;
      const t = ((x - a[0]) * vx + (y - a[1]) * vy) / L2;
      if (t < 0.12 || t > 0.88) return Infinity;            // skip corner zones
      const px = a[0] + t * vx - x, py = a[1] + t * vy - y;
      return Math.sqrt(px * px + py * py);
    };
    const diag = Math.hypot(w, h);
    // true outside = non-paper pixels reachable from the frame border (holes
    // left by printed text inside the paper are not "outside")
    const out = new Uint8Array(w * h), st = new Int32Array(w * h);
    let sp = 0;
    const push = (p) => { if (!out[p] && lab[p] !== id) { out[p] = 1; st[sp++] = p; } };
    for (let x = 0; x < w; x++) { push(x); push((h - 1) * w + x); }
    for (let y = 0; y < h; y++) { push(y * w); push(y * w + w - 1); }
    while (sp) {
      const p = st[--sp], x = p % w;
      if (x > 0) push(p - 1);
      if (x < w - 1) push(p + 1);
      if (p >= w) push(p - w);
      if (p < w * (h - 1)) push(p + w);
    }
    for (let y = 1; y < h - 1; y++) {
      for (let x = 1; x < w - 1; x++) {
        const p = y * w + x;
        if (lab[p] !== id) continue;
        if (!(out[p - 1] || out[p + 1] || out[p - w] || out[p + w])) continue;
        let bi = -1, bd = 0.08 * diag;
        for (let i = 0; i < 4; i++) {
          const d = segDist(x, y, q[i], q[(i + 1) % 4]);
          if (d < bd) { bd = d; bi = i; }
        }
        if (bi >= 0) sides[bi].push(x, y);
      }
    }
    const lines = sides.map((pts) => {
      const n = pts.length / 2;
      if (n < 6) return null;
      let mx = 0, my = 0;
      for (let i = 0; i < pts.length; i += 2) { mx += pts[i]; my += pts[i + 1]; }
      mx /= n; my /= n;
      let sxx = 0, syy = 0, sxy = 0;
      for (let i = 0; i < pts.length; i += 2) {
        const dx = pts[i] - mx, dy = pts[i + 1] - my;
        sxx += dx * dx; syy += dy * dy; sxy += dx * dy;
      }
      const ang = 0.5 * Math.atan2(2 * sxy, sxx - syy);      // principal direction
      return { x: mx, y: my, dx: Math.cos(ang), dy: Math.sin(ang) };
    });
    const meet = (a, b) => {
      const den = a.dx * b.dy - a.dy * b.dx;
      if (Math.abs(den) < 1e-3) return null;
      const t = ((b.x - a.x) * b.dy - (b.y - a.y) * b.dx) / den;
      return [a.x + t * a.dx, a.y + t * a.dy];
    };
    // corner i joins side i-1 (into the corner) and side i (out of it)
    return q.map((c, i) => {
      const a = lines[(i + 3) % 4], b = lines[i];
      if (!a || !b) return c;
      const m = meet(a, b);
      return m && Math.hypot(m[0] - c[0], m[1] - c[1]) < 0.12 * diag ? m : c;
    });
  }

  // ---------------------------------------------------------------- flatten
  // Homography mapping the output rectangle (0..W, 0..H) onto the source quad.
  function homography(quad, W, H) {
    const src = [[0, 0], [W, 0], [W, H], [0, H]], A = [], bv = [];
    for (let i = 0; i < 4; i++) {
      const [x, y] = src[i], [u, v] = quad[i];
      A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); bv.push(u);
      A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); bv.push(v);
    }
    for (let c = 0; c < 8; c++) {                            // Gaussian elimination
      let piv = c;
      for (let r = c + 1; r < 8; r++) if (Math.abs(A[r][c]) > Math.abs(A[piv][c])) piv = r;
      [A[c], A[piv]] = [A[piv], A[c]]; [bv[c], bv[piv]] = [bv[piv], bv[c]];
      for (let r = 0; r < 8; r++) {
        if (r === c) continue;
        const f = A[r][c] / A[c][c];
        for (let k = c; k < 8; k++) A[r][k] -= f * A[c][k];
        bv[r] -= f * bv[c];
      }
    }
    const hh = bv.map((v, i) => v / A[i][i]);
    return [...hh, 1];
  }

  // Render the quad of `img` (ImageData) as a flat W x H grayscale "scan".
  function flatten(img, quad, W, H) {
    const m = homography(quad, W, H), out = new ImageData(W, H), s = img.data, sw = img.width, sh = img.height;
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const z = m[6] * x + m[7] * y + 1;
        const u = (m[0] * x + m[1] * y + m[2]) / z, v = (m[3] * x + m[4] * y + m[5]) / z;
        const xi = Math.min(sw - 1, Math.max(0, u | 0)), yi = Math.min(sh - 1, Math.max(0, v | 0));
        const k = (yi * sw + xi) * 4, o = (y * W + x) * 4;
        const g = (s[k] * 77 + s[k + 1] * 150 + s[k + 2] * 29) >> 8;
        out.data[o] = out.data[o + 1] = out.data[o + 2] = g; out.data[o + 3] = 255;
      }
    }
    return out;
  }

  // Output size from the quad's side lengths (keeps the receipt's proportions).
  function flatSize(q, maxW) {
    const d = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
    const w = Math.max(d(q[0], q[1]), d(q[3], q[2])), h = Math.max(d(q[0], q[3]), d(q[1], q[2]));
    const f = Math.min(1, maxW / w);
    return [Math.max(1, Math.round(w * f)), Math.max(1, Math.round(h * f))];
  }

  // ---------------------------------------------------------------- overlap tracker
  // Box-downsample the SAMPLE_W gray frame to MATCH_W columns, then keep only the
  // print texture (gray minus its local mean): the phone's shadow and uneven
  // light move with the phone, the print doesn't. Plus a half-size level for
  // the coarse search.
  function reduce(gray, sw, sh) {
    const f = sw / MATCH_W, w = MATCH_W, h = Math.max(1, Math.floor(sh / f));
    const lo = new Float32Array(w * h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        let s = 0, n = 0;
        const y0 = Math.floor(y * f), x0 = Math.floor(x * f);
        for (let dy = 0; dy < f; dy++) for (let dx = 0; dx < f; dx++) {
          const yy = y0 + dy, xx = x0 + dx;
          if (yy < sh && xx < sw) { s += gray[yy * sw + xx]; n++; }
        }
        lo[y * w + x] = s / n;
      }
    }
    // integral image -> local mean over a (2R+1)^2 window
    const R = HP_R, I = new Float64Array((w + 1) * (h + 1));
    for (let y = 0; y < h; y++) {
      let row = 0;
      for (let x = 0; x < w; x++) { row += lo[y * w + x]; I[(y + 1) * (w + 1) + x + 1] = I[y * (w + 1) + x + 1] + row; }
    }
    const g = new Float32Array(w * h);
    for (let y = 0; y < h; y++) {
      const y0 = Math.max(0, y - R), y1 = Math.min(h, y + R + 1);
      for (let x = 0; x < w; x++) {
        const x0 = Math.max(0, x - R), x1 = Math.min(w, x + R + 1);
        const s = I[y1 * (w + 1) + x1] - I[y0 * (w + 1) + x1] - I[y1 * (w + 1) + x0] + I[y0 * (w + 1) + x0];
        g[y * w + x] = lo[y * w + x] - s / ((y1 - y0) * (x1 - x0));
      }
    }
    const cw = w >> 1, ch = h >> 1, c = new Float32Array(cw * ch);
    for (let y = 0; y < ch; y++) for (let x = 0; x < cw; x++) {
      const i = 2 * y * w + 2 * x;
      c[y * cw + x] = 0.25 * (g[i] + g[i + 1] + g[i + w] + g[i + w + 1]);
    }
    return { g, w, h, c: { g: c, w: cw, h: ch } };
  }

  // NCC of ref vs live under: ref (x, y) -> live (cx + s(x - cx) + tx, s*y + ty).
  // Only the rows that overlap are compared; small overlaps pay a penalty
  // (fewer rows = easier to match by chance).
  function nccAt(ref, live, s, tx, ty, xstep, ystep = 1) {
    const cx = ref.w / 2;
    const y0 = Math.max(0, Math.ceil(-ty / s)), y1 = Math.min(ref.h, Math.floor((live.h - 1 - ty) / s) + 1);
    const rows = y1 - y0;
    if (rows < MIN_OVERLAP * ref.h) return -2;
    let n = 0, sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0;
    for (let y = y0; y < y1; y += ystep) {
      const ly = Math.round(s * y + ty) * live.w, ry = y * ref.w;
      for (let x = 0; x < ref.w; x += xstep) {
        const lx = Math.round(cx + s * (x - cx) + tx);
        if (lx < 0 || lx >= live.w) continue;
        const a = ref.g[ry + x], b = live.g[ly + lx];
        n++; sa += a; sb += b; saa += a * a; sbb += b * b; sab += a * b;
      }
    }
    if (n < 30) return -2;
    const cov = sab - sa * sb / n, va = saa - sa * sa / n, vb = sbb - sb * sb / n;
    const full = 0.25 * ref.h;
    return cov / (Math.sqrt(va * vb) + 1e-6) - (rows < full ? 0.1 * (full - rows) / full : 0);
  }

  // Best (s, tx, ty) in a small box around a guess, at full matching resolution.
  // `pull` > 0 prefers rows near the guess: a hand moves smoothly, so a match one
  // printed line away from the prediction is usually the line's twin, not a jump.
  function refine(ref, live, g, rTy, rTx, scales, ystep = 1, pull = 0) {
    let best = { score: -2, fit: -2 };
    for (const s of scales) {
      for (let ty = Math.round(g.ty) - rTy; ty <= Math.round(g.ty) + rTy; ty++) {
        for (let tx = Math.round(g.tx) - rTx; tx <= Math.round(g.tx) + rTx; tx++) {
          const sc = nccAt(ref, live, s, tx, ty, 2, ystep), fit = sc - pull * Math.abs(ty - g.ty);
          if (fit > best.fit) best = { score: sc, fit, s, tx, ty };
        }
      }
    }
    return best;
  }

  // Coarse exhaustive search (half size, no scale) -> a few distinct peaks.
  function globalPeaks(ref, live) {
    const r = ref.c, l = live.c, peaks = [];
    const tyMin = -Math.floor(r.h * (1 - MIN_OVERLAP)), tyMax = Math.ceil(r.h * 0.12);
    for (let ty = tyMin; ty <= tyMax; ty++) {
      let b = { score: -2 };
      for (let tx = -DRIFT_C; tx <= DRIFT_C; tx++) {
        const sc = nccAt(r, l, 1, tx, ty, 1);
        if (sc > b.score) b = { score: sc, tx: 2 * tx, ty: 2 * ty };
      }
      peaks.push(b);
    }
    peaks.sort((a, b) => b.score - a.score);
    const out = [];
    for (const p of peaks) {
      if (out.length >= 3) break;
      if (out.every((q) => Math.abs(q.ty - p.ty) >= 6)) out.push(p);
    }
    return out;
  }

  // Where does the previous section end in the live view?
  // `prior` = last tracked pose {s, tx, ty, vy} (fine-level pixels). Tracking
  // starts from "not moved" right after a capture, so each step only has to
  // follow a small motion (no confusing one printed line for the next); a full
  // search re-acquires when the track is lost.
  // endFrac = where the previous section ends, as a fraction of the live frame
  // height (1.0 = not moved yet; TARGET = right overlap).
  function matchRef(ref, live, prior) {
    if (!ref.c || ref.h < 8 || live.h < 8) return { found: false, reason: "small" };
    // texture check on the bottom strip: that's what must overlap at the end
    const ps = Math.floor(ref.h * (1 - STRIP));
    let m0 = 0, v0 = 0, cnt = 0;
    for (let i = ps * ref.w; i < ref.h * ref.w; i++) { m0 += ref.g[i]; cnt++; }
    m0 /= cnt;
    for (let i = ps * ref.w; i < ref.h * ref.w; i++) { const d = ref.g[i] - m0; v0 += d * d; }
    if (Math.sqrt(v0 / cnt) < 3) return { found: false, reason: "blank" };   // blank paper: nothing to lock onto

    let local = null;
    if (prior) {
      const pred = { ty: prior.ty + (prior.vy || 0), tx: prior.tx };
      const ds = [-0.02, -0.01, 0, 0.01, 0.02].map((d) => prior.s + d).map((s) => Math.min(1.15, Math.max(0.85, s)));
      // translation first on a sparse grid, then scale around the best spot
      const t = refine(ref, live, pred, 6 + Math.min(10, Math.abs(Math.round(prior.vy || 0))), 3, [prior.s], 2, PULL);
      local = refine(ref, live, t, 1, 1, ds);
    }
    let best = local;
    if (!local || local.score < 0.8) {
      // track weak or missing: full search, refine its peaks
      for (const p of globalPeaks(ref, live)) {
        if (p.score < MIN_SCORE - 0.15) continue;
        const r = refine(ref, live, p, 2, 2, [0.95, 1, 1.05]);
        // a far-away match must clearly beat a usable local track
        const margin = local && local.score >= MIN_SCORE ? 0.08 : 0;
        if (!best || r.score > best.score + margin) best = r;
      }
    }
    if (!best || best.score < MIN_SCORE) return { found: false, reason: "lost", score: best ? best.score : -1 };
    // sub-pixel row offset from a parabola through the neighbouring scores
    const sU = nccAt(ref, live, best.s, best.tx, best.ty - 1, 2), sD = nccAt(ref, live, best.s, best.tx, best.ty + 1, 2);
    const den = sU - 2 * best.score + sD;
    const sub = den < 0 ? Math.max(-0.5, Math.min(0.5, 0.5 * (sU - sD) / den)) : 0;
    const ty = best.ty + sub;
    const endFrac = (best.s * ref.h + ty) / live.h;
    return { found: true, score: best.score, endFrac, aligned: endFrac >= ALIGN_LO && endFrac <= ALIGN_HI,
             tooHigh: endFrac < ALIGN_LO, target: TARGET, drift: best.tx / live.w, scale: best.s,
             pose: { s: best.s, tx: best.tx, ty, vy: prior ? ty - prior.ty : 0 } };
  }

  Scanner._reduce = reduce;      // exposed for tests
  Scanner._findPaperQuad = findPaperQuad;
  Scanner.findPaperQuad = findPaperQuad;
  Scanner.flatten = flatten;
  Scanner.flatSize = flatSize;
  Scanner._homography = homography;
  Scanner._matchRef = matchRef;
  Scanner.STRIP = STRIP;
  Scanner.TARGET = TARGET;
  window.Scanner = Scanner;
})();
