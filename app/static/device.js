// Runs in <head> before first paint: phones get the camera scanner, laptops
// and desktops get the dashboard (no camera).
(function () {
  var ua = navigator.userAgent || "";
  var mobileUA = /Android|iPhone|iPod|Mobile|IEMobile|Opera Mini/i.test(ua) ||
    (/iPad|Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  var coarse = window.matchMedia && matchMedia("(pointer: coarse)").matches;
  var small = Math.min(screen.width, screen.height) < 820;
  var phone = mobileUA || (coarse && small);
  var forced = null;
  try { forced = localStorage.getItem("device-mode"); } catch (e) { /* private mode */ }
  if (forced === "phone" || forced === "desk") phone = forced === "phone";
  document.documentElement.classList.add(phone ? "is-phone" : "is-desk");
  window.__isPhone = phone;
})();
