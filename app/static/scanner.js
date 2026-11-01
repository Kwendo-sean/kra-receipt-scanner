// Live receipt scanner: rear camera, sharpness (variance of Laplacian on a
// downscaled frame), stability detection, auto-capture, full-resolution stills.
(function () {
  "use strict";

  const SAMPLE_W = 240;          // downscaled analysis width
  const SAMPLE_MS = 140;
  const MIN_SHARP = 60;          // absolute floor (Laplacian variance at SAMPLE_W)
  const REL_SHARP = 0.8;         // and >= 80% of the recent best
  const STABLE_DIFF = 5;         // mean abs pixel diff between samples
  const STABLE_SAMPLES = 4;      // ~0.55 s steady
  const COOLDOWN_MS = 1800;
  // Long receipts: the bottom STRIP of the last section must reappear at the top
  // of the live frame. TARGET is where its end should sit (~25% overlap).
  const STRIP = 0.25;
  const TARGET = 0.25, ALIGN_LO = 0.15, ALIGN_HI = 0.36;
  const MATCH_W = 80;            // matching resolution (cheap enough for ~7 Hz)
  const MIN_SCORE = 0.68;        // zero-mean normalised cross-correlation

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
      if (this.ref) {
        this.align = matchRef(this.ref, this.lastReduced);
        this.onAlign(this.align);
      }
      this.stableCount = diff < STABLE_DIFF ? this.stableCount + 1 : 0;
      const now = Date.now();
      this.recent.push({ t: now, s: sharp });
      this.recent = this.recent.filter((r) => now - r.t < 3000);
      const best = Math.max(...this.recent.map((r) => r.s));
      const threshold = Math.max(MIN_SHARP, best * REL_SHARP);
      const isSharp = sharp >= threshold && sharp >= MIN_SHARP;
      const stable = this.stableCount >= STABLE_SAMPLES;
      this.onSample({ sharp, threshold: MIN_SHARP, isSharp, stable, diff });
      const placed = !this.ref || (this.align && this.align.aligned);
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
    return [[tl[0], tl[1]], [tr[0] + 1, tr[1]], [br[0] + 1, br[1] + 1], [bl[0], bl[1] + 1]];
  }

  // 3x box-downsample of the SAMPLE_W gray frame to MATCH_W columns
  function reduce(gray, sw, sh) {
    const f = sw / MATCH_W, w = MATCH_W, h = Math.max(1, Math.floor(sh / f));
    const out = new Float32Array(w * h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        let s = 0, n = 0;
        const y0 = Math.floor(y * f), x0 = Math.floor(x * f);
        for (let dy = 0; dy < f; dy++) for (let dx = 0; dx < f; dx++) {
          const yy = y0 + dy, xx = x0 + dx;
          if (yy < sh && xx < sw) { s += gray[yy * sw + xx]; n++; }
        }
        out[y * w + x] = s / n;
      }
    }
    return { g: out, w, h };
  }

  // Where does the bottom STRIP of `ref` appear in `live`? Zero-mean NCC over
  // vertical offsets. endFrac = where the previous section ends, as a fraction
  // of the live frame height (1.0 = not moved yet; TARGET = right overlap).
  function matchRef(ref, live) {
    const w = Math.min(ref.w, live.w);
    const ps = Math.floor(ref.h * (1 - STRIP)), H = ref.h - ps;
    if (H < 4 || live.h < H) return { found: false, reason: "small" };
    // texture check on the whole strip
    let m0 = 0, v0 = 0;
    for (let y = 0; y < H; y++) for (let x = 0; x < w; x++) m0 += ref.g[(ps + y) * ref.w + x];
    m0 /= H * w;
    for (let y = 0; y < H; y++) for (let x = 0; x < w; x++) { const d = ref.g[(ps + y) * ref.w + x] - m0; v0 += d * d; }
    if (Math.sqrt(v0 / (H * w)) < 6) return { found: false, reason: "blank" };   // blank paper: nothing to lock onto
    // oy < 0: only the lower part of the strip is still visible at the top
    // (moved too far); compare just the visible rows (at least 40% of the strip).
    const minRows = Math.ceil(H * 0.4);
    let best = -1, bestY = 0;
    for (let oy = -(H - minRows); oy + H <= live.h; oy++) {
      const r0 = Math.max(0, -oy), rows = H - r0;
      let am = 0, bm = 0;
      for (let y = r0; y < H; y++) for (let x = 0; x < w; x++) { am += ref.g[(ps + y) * ref.w + x]; bm += live.g[(oy + y) * live.w + x]; }
      am /= rows * w; bm /= rows * w;
      let cov = 0, av = 0, bv = 0;
      for (let y = r0; y < H; y++) {
        for (let x = 0; x < w; x++) {
          const p = ref.g[(ps + y) * ref.w + x] - am, q = live.g[(oy + y) * live.w + x] - bm;
          cov += p * q; av += p * p; bv += q * q;
        }
      }
      // partial overlaps must score a bit higher (fewer rows = easier to fake)
      const score = cov / (Math.sqrt(av * bv) + 1e-6) - (r0 ? 0.08 * r0 / H : 0);
      if (score > best) { best = score; bestY = oy; }
    }
    if (best < MIN_SCORE) return { found: false, reason: "lost", score: best };
    const endFrac = (bestY + H) / live.h;
    return { found: true, score: best, endFrac, aligned: endFrac >= ALIGN_LO && endFrac <= ALIGN_HI,
             tooHigh: endFrac < ALIGN_LO, target: TARGET };
  }

  Scanner._reduce = reduce;      // exposed for tests
  Scanner._findPaperQuad = findPaperQuad;
  Scanner._matchRef = matchRef;
  Scanner.STRIP = STRIP;
  Scanner.TARGET = TARGET;
  window.Scanner = Scanner;
})();
