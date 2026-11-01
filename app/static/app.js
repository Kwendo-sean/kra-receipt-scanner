// Phone UI. All data is inserted with textContent / DOM APIs (never innerHTML).
(function () {
  "use strict";
  const CSRF = document.querySelector('meta[name="csrf"]').content;
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

  // ---------------------------------------------------------------- helpers
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(n.dataset, v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }
  const kes = (v) => (v === null || v === undefined ? "—" :
    Number(v).toLocaleString("en-KE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "");

  async function api(method, url, body, isForm) {
    const opts = { method, credentials: "same-origin", headers: { "X-CSRF-Token": CSRF } };
    if (body !== undefined) {
      if (isForm) opts.body = body;
      else { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
    }
    const r = await fetch(url, opts);
    if (r.status === 401) { location.href = "/"; throw new Error("Not paired"); }
    let j = null;
    try { j = await r.json(); } catch (e) { j = null; }
    if (!r.ok) throw new Error((j && j.detail) || `Error ${r.status}`);
    return j;
  }

  let toastTimer;
  function toast(msg, ms = 2600) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
  }

  const thisMonth = () => new Date().toISOString().slice(0, 7);
  const lastMonth = () => { const d = new Date(); d.setDate(1); d.setMonth(d.getMonth() - 1); return d.toISOString().slice(0, 7); };

  // ---------------------------------------------------------------- theme
  const THEMES = ["auto", "light", "dark"];
  function applyTheme(t) {
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    $("#theme-toggle").textContent = "Theme: " + t;
  }
  let theme = "auto";
  try { theme = localStorage.getItem("theme") || "auto"; } catch (e) { /* private mode */ }
  applyTheme(theme);
  $("#theme-toggle").addEventListener("click", () => {
    theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    try { localStorage.setItem("theme", theme); } catch (e) { /* ignore */ }
    applyTheme(theme);
  });

  // ---------------------------------------------------------------- router
  const views = ["scan", "receipts", "reports", "email", "settings"];
  let current = "scan";
  function show(view) {
    current = view;
    for (const v of views) $("#view-" + v).classList.toggle("hidden", v !== view);
    $$(".tabbar button").forEach((b) => { if (b.dataset.view !== view) b.removeAttribute("aria-current"); else b.setAttribute("aria-current", "page"); });
    if (view === "scan") startCamera(); else stopCamera();
    if (view === "receipts") loadReceipts();
    if (view === "reports") loadReports();
    if (view === "email") loadEmail();
    if (view === "settings") loadSettings();
  }
  $$(".tabbar button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

  // ================================================================ SCAN
  const scan = {
    mode: "single", receiptId: null, frames: [], retakeIdx: null, uploads: new Set(), busy: false,
  };
  const scanner = new window.Scanner({
    video: $("#video"), guide: $("#guide"),
    onSample: ({ sharp, threshold, isSharp, stable }) => {
      const m = $("#meter");
      m.firstElementChild.style.width = Math.min(100, (sharp / (threshold * 2)) * 100) + "%";
      m.classList.toggle("ok", isSharp);
      $("#guide").classList.toggle("sharp", isSharp && stable);
      if (scan.busy) return;
      const waiting = scan.mode === "multi" && !scanner.armed;
      $("#hint").textContent = waiting ? "Tap “Next section”, slide down so ~25% overlaps, hold steady"
        : !isSharp ? "Move closer / let it focus" : !stable ? "Hold steady…" : (scanner.auto ? "Capturing…" : "Tap the shutter");
    },
    onAutoCapture: () => captureNow(),
  });

  async function startCamera() {
    if (scanner.running) return;
    $("#cam-error").classList.add("hidden");
    try {
      await scanner.start();
      $("#hint").textContent = "Fit the receipt inside the frame";
    } catch (e) {
      const box = $("#cam-error");
      box.replaceChildren(el("div", {}, el("p", { text: e.message || "Camera unavailable." }),
        el("p", { class: "small", text: "Check that camera permission is allowed for this site." })));
      box.classList.remove("hidden");
    }
  }
  function stopCamera() { scanner.stop(); }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopCamera(); else if (current === "scan") startCamera();
  });

  function setMode(m) {
    if (scan.frames.length && m !== scan.mode) { toast("Finish or discard the current receipt first"); return; }
    scan.mode = m;
    $("#mode-single").setAttribute("aria-pressed", m === "single");
    $("#mode-multi").setAttribute("aria-pressed", m === "multi");
    scanner.armed = true;
    updateScanButtons();
  }
  $("#mode-single").addEventListener("click", () => setMode("single"));
  $("#mode-multi").addEventListener("click", () => setMode("multi"));
  $("#auto-toggle").addEventListener("click", (e) => {
    scanner.auto = !scanner.auto;
    e.currentTarget.setAttribute("aria-pressed", scanner.auto);
  });
  $("#shutter").addEventListener("click", () => captureNow());
  $("#btn-next").addEventListener("click", () => {
    scanner.armed = true; scan.retakeIdx = null;
    $("#hint").textContent = "Slide down, keep ~25% overlap with the previous section";
    updateScanButtons();
  });
  $("#btn-done").addEventListener("click", () => finishReceipt());

  function updateScanButtons() {
    const multi = scan.mode === "multi";
    $("#btn-next").classList.toggle("hidden", !multi);
    $("#btn-done").classList.toggle("hidden", !multi);
    $("#btn-next").disabled = !scan.frames.length || scanner.armed;
    $("#btn-done").disabled = !scan.frames.length;
    $("#thumbs").classList.toggle("hidden", !multi);
    const ghost = $("#ghost");
    const last = scan.frames[scan.frames.length - 1];
    if (multi && last && scanner.armed && scan.retakeIdx === null) {
      ghost.style.backgroundImage = `url("${last.url}")`;
      ghost.classList.remove("hidden");
    } else ghost.classList.add("hidden");
  }

  async function ensureReceipt() {
    if (!scan.receiptId) scan.receiptId = (await api("POST", "/api/receipts", { mode: scan.mode })).id;
    return scan.receiptId;
  }

  async function captureNow() {
    if (scan.busy || !scanner.running) return;
    if (scan.mode === "multi" && !scanner.armed && scan.retakeIdx === null) { toast("Tap “Next section” first"); return; }
    scan.busy = true;
    $("#shutter").classList.add("busy");
    $("#hint").textContent = "Captured";
    try {
      const { blob, sharpness } = await scanner.capture();
      const rid = await ensureReceipt();
      const idx = scan.retakeIdx !== null ? scan.retakeIdx : (scan.frames.length ? Math.max(...scan.frames.map((f) => f.idx)) + 1 : 0);
      const url = URL.createObjectURL(blob);
      const existing = scan.frames.find((f) => f.idx === idx);
      if (existing) { URL.revokeObjectURL(existing.url); existing.url = url; existing.state = "uploading"; }
      else scan.frames.push({ idx, url, state: "uploading" });
      scan.retakeIdx = null;
      scanner.armed = false;
      renderThumbs();
      const fd = new FormData();
      fd.append("file", blob, `frame${idx}.jpg`);
      fd.append("idx", String(idx));
      if (sharpness) fd.append("sharpness", String(Math.round(sharpness)));
      const up = api("POST", `/api/receipts/${rid}/frames`, fd, true).then(() => {
        const f = scan.frames.find((x) => x.idx === idx); if (f) f.state = "ok"; renderThumbs();
      }).catch((e) => { toast("Upload failed: " + e.message); const f = scan.frames.find((x) => x.idx === idx); if (f) f.state = "failed"; renderThumbs(); });
      scan.uploads.add(up);
      up.finally(() => scan.uploads.delete(up));
      if (scan.mode === "single") await finishReceipt();
    } catch (e) {
      toast(e.message);
    } finally {
      scan.busy = false;
      $("#shutter").classList.remove("busy");
      if (scan.mode === "single") scanner.armed = true;
      updateScanButtons();
    }
  }

  function renderThumbs() {
    const box = $("#thumbs");
    box.replaceChildren(...scan.frames.sort((a, b) => a.idx - b.idx).map((f, i) =>
      el("button", { type: "button", class: "thumb" + (f.state === "uploading" ? " uploading" : ""),
        "aria-label": `Section ${i + 1}`, onclick: () => thumbMenu(f) },
        el("img", { src: f.url, alt: "" }), el("b", { text: f.state === "failed" ? "!" : String(i + 1) }))));
    updateScanButtons();
  }

  function thumbMenu(f) {
    openSheet("Section " + (scan.frames.indexOf(f) + 1), el("div", { class: "stack" },
      el("img", { src: f.url, class: "detail-img", alt: "Captured section" }),
      el("div", { class: "row" },
        el("button", { class: "btn primary", type: "button", onclick: () => {
          scan.retakeIdx = f.idx; scanner.armed = true; closeSheet();
          $("#hint").textContent = "Retake: line up this section again";
          updateScanButtons();
        } }, "Retake this section"),
        el("button", { class: "btn danger", type: "button", onclick: async () => {
          try { await api("DELETE", `/api/receipts/${scan.receiptId}/frames/${f.idx}`); } catch (e) { /* ignore */ }
          scan.frames = scan.frames.filter((x) => x !== f); URL.revokeObjectURL(f.url); closeSheet(); renderThumbs();
        } }, "Delete"))));
  }

  async function finishReceipt() {
    if (!scan.receiptId) return;
    await Promise.allSettled(Array.from(scan.uploads));
    if (scan.frames.some((f) => f.state === "failed")) { toast("A section failed to upload: retake it"); return; }
    try {
      await api("POST", `/api/receipts/${scan.receiptId}/submit`, { ocr_mode: "auto" });
      toast(`Receipt #${scan.receiptId} queued for reading`);
      watching.add(scan.receiptId);
    } catch (e) { toast(e.message); return; }
    scan.frames.forEach((f) => URL.revokeObjectURL(f.url));
    scan.frames = []; scan.receiptId = null; scan.retakeIdx = null; scanner.armed = true;
    renderThumbs();
    pollQueue();
  }

  // queue chips (per-receipt status while scanning continues)
  const watching = new Set();
  const announced = new Set();
  async function pollQueue() {
    if (!watching.size) { $("#queue").replaceChildren(); return; }
    try {
      const q = await api("GET", "/api/queue");
      const rows = q.receipts.filter((r) => watching.has(r.id));
      $("#queue").replaceChildren(...rows.slice(0, 4).map((r) => {
        const label = r.status === "done" ? `#${r.id} ${r.seller_name || "Receipt"} · KES ${kes(r.total)} · tap to check`
          : r.status === "failed" ? `#${r.id} could not be read: tap for details`
            : `#${r.id} ${r.status === "processing" ? (r.stage || "reading") : "waiting"}…`;
        return el("button", { type: "button", class: "chip " + r.status, onclick: () => { watching.delete(r.id); openReceipt(r.id); pollQueue(); } },
          el("span", { class: "dot" }), el("span", { text: label }));
      }));
      for (const r of rows) {
        if (r.status === "done" && !announced.has(r.id)) { announced.add(r.id); if (navigator.vibrate) navigator.vibrate(60); }
      }
    } catch (e) { /* offline blip */ }
  }
  setInterval(pollQueue, 2000);

  // ================================================================ SHEET
  function openSheet(title, body) {
    $("#sheet-title").textContent = title;
    $("#sheet-body").replaceChildren(body);
    $("#sheet").classList.remove("hidden");
    $("#sheet-backdrop").classList.remove("hidden");
    $("#sheet").scrollTop = 0;
  }
  function closeSheet() {
    $("#sheet").classList.add("hidden");
    $("#sheet-backdrop").classList.add("hidden");
  }
  $("#sheet-close").addEventListener("click", closeSheet);
  $("#sheet-backdrop").addEventListener("click", closeSheet);
  $("#zoom-close").addEventListener("click", () => $("#zoom").classList.add("hidden"));
  function zoom(src) { $("#zoom-img").src = src; $("#zoom").classList.remove("hidden"); }

  // ================================================================ RECEIPT DETAIL
  const FIELD_DEFS = [
    ["seller_name", "Seller", "text", true], ["seller_pin", "Seller KRA PIN", "text"], ["buyer_pin", "Buyer PIN", "text"],
    ["receipt_no", "Receipt / invoice no.", "text"], ["cu_invoice_no", "CU invoice no.", "text"],
    ["cu_serial_no", "CU serial / SCU ID", "text"], ["receipt_date", "Date", "date"], ["receipt_time", "Time", "time"],
    ["total", "Total (KES)", "number"], ["total_vat", "Total VAT printed (KES)", "number"],
    ["payment_method", "Payment", "text"], ["mpesa_code", "M-PESA code", "text"],
    ["receipt_signature", "Receipt signature", "text", true],
  ];

  async function openReceipt(id) {
    let r;
    try { r = await api("GET", `/api/receipts/${id}`); } catch (e) { toast(e.message); return; }
    openSheet(`Receipt #${id}`, receiptBody(r));
    if (r.status === "queued" || r.status === "processing") setTimeout(() => {
      if (!$("#sheet").classList.contains("hidden") && $("#sheet-title").textContent === `Receipt #${id}`) openReceipt(id);
    }, 2500);
  }

  function receiptBody(r) {
    const root = el("div", { class: "stack" });
    const imgSrc = r.has_stitched ? `/media/receipts/${r.id}/stitched.jpg` : (r.frames.length ? `/media/receipts/${r.id}/frame/${r.frames[0].idx}.jpg` : null);
    if (imgSrc) root.append(el("img", { class: "detail-img", src: imgSrc + "?w=900", alt: "Receipt photo", onclick: () => zoom(imgSrc) }));
    if (r.frames.length > 1) root.append(el("div", { class: "row small" }, "Sections:",
      r.frames.map((f) => el("button", { type: "button", class: "btn small", onclick: () => zoom(`/media/receipts/${r.id}/frame/${f.idx}.jpg`) }, String(f.idx + 1)))));
    if (r.status !== "done") {
      root.append(el("div", { class: "card" }, el("b", { text: r.status === "failed" ? "Could not read this receipt" : "Reading…" }),
        el("p", { class: "small muted", text: r.error || r.stage || "Waiting in queue" })));
      if (r.status === "failed") root.append(el("button", { class: "btn", type: "button", onclick: async () => { await api("POST", `/api/receipts/${r.id}/reprocess`, {}); watching.add(r.id); openReceipt(r.id); } }, "Try again"));
      return root;
    }
    if (r.claim) {
      root.append(el("div", { class: "claim " + (r.claim.claimable ? "yes" : "no") },
        r.claim.claimable ? `Input VAT claimable · KES ${kes(r.vat)}` : "Not claimable",
        r.claim.reasons.length ? el("div", { class: "small", text: r.claim.reasons.join(" · ") }) : null,
        r.claim.warnings.length ? el("div", { class: "small", text: r.claim.warnings.join(" · ") }) : null,
        r.claim.deadline ? el("div", { class: "small muted", text: "Claim deadline " + r.claim.deadline }) : null));
    }
    // checks
    root.append(el("div", { class: "card" }, el("h2", { text: "Checks" }),
      el("ul", { class: "list checks" }, r.checks.map((c) => el("li", {},
        el("span", { class: "icon " + (c.ok === true ? "ok" : c.ok === false ? "bad" : "na"), text: c.ok === true ? "✓" : c.ok === false ? "✗" : "–" }),
        el("div", { class: "main" }, el("div", { text: c.label }), el("div", { class: "small muted", text: c.detail })))))));

    // editable fields
    const f = r.fields, corr = r.corrections || {};
    const form = el("form", { class: "card stack" }, el("h2", { text: "Fields" }),
      el("p", { class: "small muted", text: "Fix anything that doesn't match the photo. Leave blank if it isn't printed: nothing is guessed." }));
    const grid = el("div", { class: "fields" });
    for (const [key, label, type, full] of FIELD_DEFS) {
      const v = f[key];
      const input = el("input", { type: type === "number" ? "text" : type, name: key, value: v === null || v === undefined ? "" : String(v),
        inputmode: type === "number" ? "decimal" : null, step: type === "time" ? "1" : null, autocomplete: "off" });
      grid.append(el("label", { class: "field" + (full ? " full" : "") + (key in corr ? " corrected" : "") + ((v === null || v === undefined) && ["seller_pin", "cu_invoice_no", "total", "receipt_date"].includes(key) ? " missing-field" : "") },
        el("span", { text: label }), input));
    }
    const cats = (window.__settings && window.__settings.categories) || [];
    const catSel = el("select", { name: "category" }, el("option", { value: "", text: "(none)" }),
      cats.map((c) => el("option", { value: c, text: c, selected: r.category === c })));
    grid.append(el("label", { class: "field full" }, el("span", { text: "Category" }), catSel));
    form.append(grid);

    // bands table
    const bandsT = editTable(["Band", "Rate %", "Taxable", "VAT"], (f.bands || []).map((b) => [b.band || "", b.rate ?? "", b.taxable ?? "", b.vat ?? ""]));
    form.append(el("h3", { text: "Tax bands (rate as printed)" + ("bands" in corr ? " · corrected" : "") }), bandsT.node);
    const itemsT = editTable(["Description", "Qty", "Unit", "Total", "Band"], (f.items || []).map((i) => [i.description, i.quantity ?? "", i.unit_price ?? "", i.line_total, i.band || ""]));
    form.append(el("details", {}, el("summary", { text: `Line items (${(f.items || []).length})` + ("items" in corr ? " · corrected" : "") }), itemsT.node));

    const save = el("button", { class: "btn primary", type: "submit" }, "Save corrections");
    form.append(el("div", { class: "row" }, save,
      Object.keys(corr).length ? el("button", { class: "btn small", type: "button", onclick: async () => {
        try { await api("PATCH", `/api/receipts/${r.id}`, { revert: Object.keys(corr) }); toast("Reverted to OCR values"); openReceipt(r.id); } catch (e) { toast(e.message); }
      } }, "Revert all") : null));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const changes = {};
      for (const [key] of FIELD_DEFS) {
        const val = form.elements[key].value.trim();
        const orig = f[key] === null || f[key] === undefined ? "" : String(f[key]);
        if (val !== orig) changes[key] = val === "" ? null : val;
      }
      if ((catSel.value || null) !== (r.category || null)) changes.category = catSel.value || null;
      const nb = bandsT.values().map(([band, rate, taxable, vat]) => ({ band, rate, taxable, vat }));
      if (JSON.stringify(nb) !== JSON.stringify(bandsT.initial.map(([band, rate, taxable, vat]) => ({ band, rate, taxable, vat })))) changes.bands = nb;
      const ni = itemsT.values().map(([description, quantity, unit_price, line_total, band]) => ({ description, quantity, unit_price, line_total, band }));
      if (JSON.stringify(itemsT.values()) !== JSON.stringify(itemsT.initial)) changes.items = ni;
      if (!Object.keys(changes).length) { toast("Nothing changed"); return; }
      try { await api("PATCH", `/api/receipts/${r.id}`, { changes }); toast("Saved"); openReceipt(r.id); } catch (e) { toast(e.message, 4000); }
    });
    root.append(form);

    // actions
    root.append(el("div", { class: "card row" },
      el("button", { class: "btn", type: "button", onclick: () => { closeSheet(); emailSelection([r.id]); } }, "Email this receipt"),
      el("button", { class: "btn", type: "button", onclick: async () => {
        try { await api("POST", "/api/reports", { kind: "receipt", params: { ids: [r.id] } }); toast("Report queued: see Reports"); } catch (e) { toast(e.message); }
      } }, "Receipt report"),
      el("button", { class: "btn", type: "button", onclick: async () => {
        try { await api("POST", `/api/receipts/${r.id}/reprocess`, {}); watching.add(r.id); toast("Re-reading (your corrections are kept)"); openReceipt(r.id); } catch (e) { toast(e.message); }
      } }, "Re-read photo")));
    const del = el("button", { class: "btn danger small", type: "button" }, "Delete receipt");
    let armedDel = false;
    del.addEventListener("click", async () => {
      if (!armedDel) { armedDel = true; del.textContent = "Tap again to delete"; setTimeout(() => { armedDel = false; del.textContent = "Delete receipt"; }, 3000); return; }
      try { await api("DELETE", `/api/receipts/${r.id}`); closeSheet(); toast("Deleted"); if (current === "receipts") loadReceipts(); } catch (e) { toast(e.message); }
    });
    root.append(del);
    // raw text + audit
    root.append(el("details", { class: "card" }, el("summary", { text: "Raw OCR text" }), el("pre", { class: "ocr", text: r.ocr_text || "" }),
      el("p", { class: "small muted", text: `Stitching: ${(r.stitch && (r.stitch.method || "")) || "single"}${r.stitch && r.stitch.fallback ? " → per-frame de-duplication" : ""}` })));
    const auditBox = el("details", { class: "card" }, el("summary", { text: "History" }));
    auditBox.addEventListener("toggle", async () => {
      if (!auditBox.open || auditBox.dataset.loaded) return;
      auditBox.dataset.loaded = "1";
      try {
        const a = await api("GET", `/api/receipts/${r.id}/audit`);
        auditBox.append(el("ul", { class: "list small" }, a.audit.map((x) => el("li", {},
          el("div", { class: "main" }, el("div", { text: `${x.action}${x.field ? " · " + x.field : ""}` }),
            x.new ? el("div", { class: "muted", text: `${x.old ?? ""} → ${x.new}` }) : null),
          el("span", { class: "muted", text: when(x.at) })))));
      } catch (e) { /* ignore */ }
    });
    root.append(auditBox);
    return root;
  }

  function editTable(cols, rows) {
    const initial = rows.map((r) => r.map((v) => String(v ?? "")));
    const tbody = el("tbody");
    const addRow = (vals) => {
      const tr = el("tr", {}, cols.map((c, i) => el("td", {}, el("input", { type: "text", value: vals[i] ?? "", "aria-label": c, inputmode: i > 0 && c !== "Band" ? "decimal" : null }))),
        el("td", {}, el("button", { type: "button", class: "btn small", "aria-label": "Remove row", onclick: () => tr.remove() }, "×")));
      tbody.append(tr);
    };
    initial.forEach(addRow);
    const node = el("div", { class: "table-wrap" }, el("table", { class: "edit-table" },
      el("thead", {}, el("tr", {}, cols.map((c) => el("th", { text: c })), el("th"))), tbody),
      el("button", { type: "button", class: "btn small", onclick: () => addRow(cols.map(() => "")) }, "+ Add row"));
    return { node, initial, values: () => $$("tr", tbody).map((tr) => $$("input", tr).map((i) => i.value.trim())) };
  }

  // ================================================================ RECEIPTS LIST
  const selected = new Set();
  let selecting = false;
  $("#sel-toggle").addEventListener("click", () => {
    selecting = !selecting; if (!selecting) selected.clear();
    $("#sel-toggle").textContent = selecting ? "Cancel" : "Select"; loadReceipts();
  });
  ["#f-month", "#f-supplier", "#f-claimable"].forEach((s) => $(s).addEventListener("change", loadReceipts));
  $("#f-supplier").addEventListener("input", debounce(loadReceipts, 400));
  function debounce(fn, ms) { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; }

  async function loadReceipts() {
    let list;
    try { list = (await api("GET", "/api/receipts?limit=500")).receipts; } catch (e) { toast(e.message); return; }
    const month = $("#f-month").value, sup = $("#f-supplier").value.trim().toLowerCase();
    if (month) list = list.filter((r) => (r.receipt_date || "").startsWith(month));
    if (sup) list = list.filter((r) => (r.seller_name || "").toLowerCase().includes(sup));
    let claimMap = null;
    if ($("#f-claimable").checked) {
      claimMap = new Set();
      await Promise.all(list.filter((r) => r.status === "done").map(async (r) => {
        try { const d = await api("GET", `/api/receipts/${r.id}`); if (d.claim && d.claim.claimable) claimMap.add(r.id); } catch (e) { /* skip */ }
      }));
      list = list.filter((r) => claimMap.has(r.id));
    }
    const ul = $("#receipt-list");
    ul.replaceChildren(...(list.length ? list.map((r) => {
      const pill = r.status === "done" ? null : el("span", { class: "pill " + (r.status === "failed" ? "bad" : "warn"), text: r.status });
      const cb = selecting ? el("input", { type: "checkbox", checked: selected.has(r.id), "aria-label": "Select receipt",
        onchange: (e) => { e.target.checked ? selected.add(r.id) : selected.delete(r.id); updateSel(); } }) : null;
      return el("li", {}, cb, el("button", { type: "button", class: "rowlink", onclick: () => selecting ? (cb.checked = !cb.checked, cb.dispatchEvent(new Event("change"))) : openReceipt(r.id) },
        el("div", { class: "main" }, el("div", { class: "title", text: r.seller_name || `Receipt #${r.id}` }),
          el("div", { class: "small muted" }, (r.receipt_date || "no date") + (r.category ? " · " + r.category : "") + " ", pill)),
        el("div", { class: "amount" }, el("div", { text: kes(r.total) }), el("div", { class: "small muted", text: r.vat !== null && r.vat !== undefined ? "VAT " + kes(r.vat) : "no VAT" }))));
    }) : [el("li", { class: "muted", text: "No receipts yet: scan one!" })]));
    updateSel();
  }
  function updateSel() {
    $("#sel-bar").classList.toggle("hidden", !selecting);
    $("#sel-count").textContent = `${selected.size} selected`;
    $("#e-sel-n").textContent = selected.size;
  }
  $("#sel-email").addEventListener("click", () => emailSelection(Array.from(selected)));
  $("#sel-report").addEventListener("click", () => { reportIds = Array.from(selected); show("reports"); });

  // ================================================================ REPORTS
  let reportIds = null;
  $("#r-month").value = thisMonth();
  $("#report-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const kind = $("#r-kind").value;
    const params = reportIds && reportIds.length ? { ids: reportIds } : { period: $("#r-month").value };
    try { await api("POST", "/api/reports", { kind, params }); toast("Report queued"); reportIds = null; loadReports(); } catch (e) { toast(e.message); }
  });
  let reportPoll = null;
  async function loadReports() {
    $("#r-sel-note").textContent = reportIds && reportIds.length ? `Using ${reportIds.length} selected receipt(s) instead of a month.` : "";
    $("#r-month").required = !(reportIds && reportIds.length);
    let j;
    try { j = await api("GET", "/api/reports"); } catch (e) { toast(e.message); return; }
    $("#report-list").replaceChildren(...(j.reports.length ? j.reports.map((r) => el("li", {},
      el("div", { class: "main" }, el("div", { class: "title", text: r.title }),
        el("div", { class: "small muted", text: `${when(r.created_at)} · ${r.status}${r.prose_status ? " · " + r.prose_status : ""}${r.error ? " · " + r.error : ""}` })),
      r.status === "done" ? el("div", { class: "row" },
        el("a", { class: "btn small", href: `/reports/${r.id}/view`, target: "_blank", rel: "noopener" }, "View"),
        el("a", { class: "btn small", href: `/reports/${r.id}/download/pdf` }, "PDF"),
        el("a", { class: "btn small", href: `/reports/${r.id}/download/xlsx` }, "XLSX"),
        el("a", { class: "btn small", href: `/reports/${r.id}/download/csv` }, "CSV")) : el("span", { class: "pill warn", text: r.status })))
      : [el("li", { class: "muted", text: "No reports yet." })]));
    clearTimeout(reportPoll);
    if (j.reports.some((r) => r.status === "queued" || r.status === "running") && current === "reports") reportPoll = setTimeout(loadReports, 3000);
  }

  // ================================================================ EMAIL
  $("#e-month").value = lastMonth();
  function emailSelection(ids) {
    selected.clear(); ids.forEach((i) => selected.add(i));
    show("email");
    $('input[name="scope"][value="selected"]').checked = true;
    $$('input[name="kind"]').forEach((c) => { c.checked = ids.length === 1 ? c.value === "receipt" : ["vat_schedule", "compliance"].includes(c.value); });
    $$('input[name="inc"]').forEach((c) => { c.checked = ids.length === 1 ? ["pdf", "images"].includes(c.value) : ["pdf", "xlsx"].includes(c.value); });
    updateSel();
  }
  $("#email-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const scope = $('input[name="scope"]:checked').value;
    const params = {};
    if (scope === "month") params.period = $("#e-month").value;
    if (scope === "selected") { if (!selected.size) { toast("Select receipts in the Receipts tab first"); return; } params.ids = Array.from(selected); }
    if ($("#e-supplier").value.trim()) params.supplier = $("#e-supplier").value.trim();
    if ($("#e-claimable").checked) params.claimable = true;
    const include = $$('input[name="inc"]:checked').map((c) => c.value);
    const report_kinds = $$('input[name="kind"]:checked').map((c) => c.value);
    try {
      await api("POST", "/api/email", { recipient_id: Number($("#e-recipient").value), params, include, report_kinds });
      toast("Email queued: it sends when the Pi is online");
      setTimeout(loadOutbox, 1500);
    } catch (e) { toast(e.message, 4000); }
  });
  async function loadEmail() {
    try {
      const [rec, st] = await Promise.all([api("GET", "/api/recipients"), api("GET", "/api/status")]);
      const conf = rec.recipients.filter((r) => r.confirmed);
      $("#e-recipient").replaceChildren(...(conf.length ? conf.map((r) => el("option", { value: r.id, text: r.email }))
        : [el("option", { value: "", text: "No confirmed address: add one in Settings" })]));
      const pill = $("#online-pill");
      pill.textContent = st.smtp_configured ? `${st.outbox_queued} waiting` : "SMTP not set up";
      pill.className = "pill " + (st.smtp_configured ? (st.outbox_queued ? "warn" : "ok") : "bad");
    } catch (e) { toast(e.message); }
    updateSel();
    loadOutbox();
  }
  async function loadOutbox() {
    let j;
    try { j = await api("GET", "/api/outbox"); } catch (e) { return; }
    const items = [];
    for (const jb of j.compose_jobs.filter((x) => x.status !== "done")) {
      items.push(el("li", {}, el("div", { class: "main" }, el("div", { class: "title", text: "Preparing attachments…" }),
        el("div", { class: "small muted", text: jb.status + (jb.error ? " · " + jb.error : "") })),
        el("span", { class: "pill " + (jb.status === "failed" ? "bad" : "warn"), text: jb.status })));
    }
    for (const o of j.outbox) {
      const cls = o.status === "sent" ? "ok" : o.status === "failed" ? "bad" : "warn";
      items.push(el("li", {}, el("div", { class: "main" }, el("div", { class: "title", text: o.subject }),
        el("div", { class: "small muted", text: `${o.recipient} · ${(o.size_bytes / 1048576).toFixed(1)} MB · ${o.attachments.length} file(s)` +
          (o.status === "sent" ? ` · sent ${when(o.sent_at)}` : o.status === "queued" && o.attempts ? ` · retry ${when(o.next_attempt)}` : "") }),
        o.last_error ? el("div", { class: "small", text: o.last_error }) : null),
        el("div", { class: "row" }, el("span", { class: "pill " + cls, text: o.status }),
          o.status === "failed" ? el("button", { class: "btn small", type: "button", onclick: async () => { await api("POST", `/api/outbox/${o.id}/retry`); loadOutbox(); } }, "Retry") : null,
          (o.status === "failed" || o.status === "queued") ? el("button", { class: "btn small danger", type: "button", onclick: async () => { await api("DELETE", `/api/outbox/${o.id}`); loadOutbox(); } }, "Cancel") : null)));
    }
    $("#outbox-list").replaceChildren(...(items.length ? items : [el("li", { class: "muted", text: "Outbox is empty." })]));
  }

  // ================================================================ SETTINGS
  let pendingConfirm = null;
  async function loadSettings() {
    let s, st, rec;
    try { [s, st, rec] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/status"), api("GET", "/api/recipients")]); }
    catch (e) { toast(e.message); return; }
    window.__settings = s;
    $("#status-list").replaceChildren(
      el("li", {}, el("div", { class: "main", text: "Local language model (report prose)" }), el("span", { class: "pill " + (st.llm ? "ok" : "warn"), text: st.llm ? "running" : "offline: templates used" })),
      el("li", {}, el("div", { class: "main", text: "Email (SMTP)" }), el("span", { class: "pill " + (st.smtp_configured ? "ok" : "bad"), text: st.smtp_configured ? "configured" : "not configured" })),
      el("li", {}, el("div", { class: "main", text: "Jobs in queue" }), el("span", { class: "pill", text: String(st.jobs_pending) })),
      el("li", {}, el("div", { class: "main", text: "Version" }), el("span", { class: "pill", text: st.version })));
    $("#recipient-list").replaceChildren(...rec.recipients.map((r) => el("li", {},
      el("div", { class: "main" }, el("div", { class: "title", text: r.email })),
      el("span", { class: "pill " + (r.confirmed ? "ok" : "warn"), text: r.confirmed ? "confirmed" : "awaiting code" }),
      !r.confirmed ? el("button", { class: "btn small", type: "button", onclick: () => { pendingConfirm = r.email; $("#confirm-form").classList.remove("hidden"); $("#confirm-code").focus(); } }, "Enter code") : null,
      el("button", { class: "btn small danger", type: "button", onclick: async () => { await api("DELETE", `/api/recipients/${r.id}`); loadSettings(); } }, "Remove"))));
    $("#auto-monthly").checked = !!(s.auto_monthly && s.auto_monthly.enabled);
    $("#categories").value = s.categories.join("\n");
    $("#session-list").replaceChildren(...s.sessions.map((x) => el("li", {},
      el("div", { class: "main" }, el("div", { class: "title", text: (x.label || "Device") + (x.current ? " (this phone)" : "") }),
        el("div", { class: "small muted", text: "Last seen " + when(x.last_seen) })),
      x.current ? null : el("button", { class: "btn small danger", type: "button", onclick: async () => { await api("DELETE", `/api/sessions/${encodeURIComponent(x.id)}`); loadSettings(); } }, "Revoke"))));
    // rates
    const cols = ["key", "rate", "label", "effective_from", "effective_to", "note", "source"];
    $("#rates-table").replaceChildren(el("thead", {}, el("tr", {}, ["Key", "Rate %", "Label", "From", "To", "Note", "Source"].map((c) => el("th", { text: c })))),
      el("tbody", {}, s.rates.map((r) => el("tr", {}, cols.map((c) => el("td", {}, el("input", { type: "text", value: r[c] ?? "", "data-col": c, "aria-label": c })))))));
    const rb = $("#rules-box");
    rb.replaceChildren(
      el("p", { text: `VAT3 return and payment due by the ${s.rules.vat3_due_day}th of the following month. Input VAT claimable within ${s.rules.claim_window_months} months after the end of the month of purchase, only on an eTIMS invoice showing the supplier's PIN. Researched ${s.rules.researched}.` }),
      ...s.rules.notes.map((n) => el("p", { class: "muted", text: n })),
      el("ul", {}, Object.entries(s.rules.sources).map(([k, v]) => el("li", { text: `${k}: ${v}` }))));
  }
  $("#recipient-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const email = $("#new-recipient").value.trim();
    try { await api("POST", "/api/recipients", { email }); pendingConfirm = email; $("#new-recipient").value = "";
      $("#confirm-form").classList.remove("hidden"); toast("Code queued to " + email); loadSettings(); } catch (e) { toast(e.message); }
  });
  $("#confirm-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try { await api("POST", "/api/recipients/confirm", { email: pendingConfirm, code: $("#confirm-code").value });
      $("#confirm-form").classList.add("hidden"); $("#confirm-code").value = ""; toast("Address confirmed"); loadSettings(); } catch (e) { toast(e.message); }
  });
  $("#auto-monthly").addEventListener("change", async (e) => {
    try { await api("POST", "/api/settings", { auto_monthly: { enabled: e.target.checked } }); toast("Saved"); } catch (er) { toast(er.message); }
  });
  $("#save-categories").addEventListener("click", async () => {
    const categories = $("#categories").value.split("\n").map((s) => s.trim()).filter(Boolean);
    try { await api("POST", "/api/settings", { categories }); toast("Saved"); loadSettings(); } catch (e) { toast(e.message); }
  });
  $("#save-rates").addEventListener("click", async () => {
    const rates = $$("#rates-table tbody tr").map((tr) => {
      const o = {}; $$("input", tr).forEach((i) => { o[i.dataset.col] = i.value.trim() || null; });
      o.rate = o.rate === null ? 0 : Number(o.rate); return o;
    });
    try { await api("POST", "/api/settings", { rates }); toast("Rates saved"); } catch (e) { toast(e.message); }
  });
  let pairTimer = null;
  $("#pair-another").addEventListener("click", async () => {
    try {
      const j = await api("GET", "/api/pair/new");
      $("#pair-another-qr").src = "/api/pair/qr.svg?token=" + encodeURIComponent(j.token);
      $("#pair-another-box").classList.remove("hidden");
      clearTimeout(pairTimer);
      pairTimer = setTimeout(() => $("#pair-another-box").classList.add("hidden"), j.ttl * 1000);
    } catch (e) { toast(e.message); }
  });
  $("#logout").addEventListener("click", async () => {
    try { await api("POST", "/api/logout"); } catch (e) { /* ignore */ }
    location.href = "/";
  });

  // ---------------------------------------------------------------- boot
  api("GET", "/api/settings").then((s) => { window.__settings = s; }).catch(() => {});
  updateScanButtons();
  if (new URLSearchParams(location.search).get("paired")) { toast("Phone paired: point it at a receipt"); history.replaceState(null, "", "/"); }
  show("scan");
})();
