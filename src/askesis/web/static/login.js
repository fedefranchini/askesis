// Login: while the server-side delay after failed attempts is running, show the countdown and keep the button off.
// The server enforces the delay anyway; this only spares a useless attempt.
(function () {
  "use strict";
  const form = document.getElementById("login");
  if (!form) return;
  let left = Number(form.dataset.wait || 0);
  const btn = document.getElementById("login-submit");
  const out = document.getElementById("login-wait");
  const pw = document.getElementById("password");
  function tick() {
    if (left <= 0) {
      btn.disabled = false; out.textContent = "";
      if (document.activeElement !== pw) pw.focus();
      return;
    }
    btn.disabled = true;
    out.textContent = "Puoi riprovare tra " + left + (left === 1 ? " secondo." : " secondi.");
    left -= 1;
    setTimeout(tick, 1000);
  }
  tick();
})();
