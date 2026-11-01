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

  class Scanner {
    constructor(opts) {
      this.video = opts.video;
      this.guide = opts.guide;
      this.onSample = opts.onSample || (() => {});
      this.onAutoCapture = opts.onAutoCapture || (() => {});
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
      this.stableCount = diff < STABLE_DIFF ? this.stableCount + 1 : 0;
      const now = Date.now();
      this.recent.push({ t: now, s: sharp });
      this.recent = this.recent.filter((r) => now - r.t < 3000);
      const best = Math.max(...this.recent.map((r) => r.s));
      const threshold = Math.max(MIN_SHARP, best * REL_SHARP);
      const isSharp = sharp >= threshold && sharp >= MIN_SHARP;
      const stable = this.stableCount >= STABLE_SAMPLES;
      this.onSample({ sharp, threshold: MIN_SHARP, isSharp, stable, diff });
      if (this.auto && this.armed && isSharp && stable && now - this.lastCapture > COOLDOWN_MS) {
        this.lastCapture = now;
        this.onAutoCapture();
      }
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

  window.Scanner = Scanner;
})();
