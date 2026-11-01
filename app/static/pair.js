// Fetches a fresh single-use pairing token and refreshes the QR before it expires.
(function () {
  const img = document.getElementById("pair-qr");
  const cd = document.getElementById("pair-countdown");
  if (!img) return;
  let expires = 0;

  async function refresh() {
    try {
      const r = await fetch("/api/pair/new", { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      const j = await r.json();
      img.src = "/api/pair/qr.svg?token=" + encodeURIComponent(j.token);
      expires = Date.now() + (j.ttl - 15) * 1000;
    } catch (e) {
      cd.textContent = "could not create a code";
      setTimeout(refresh, 10000);
    }
  }
  function tick() {
    const left = Math.max(0, Math.round((expires - Date.now()) / 1000));
    if (expires && left <= 0) refresh();
    cd.textContent = expires ? `new code in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "";
  }
  refresh();
  setInterval(tick, 1000);
})();
