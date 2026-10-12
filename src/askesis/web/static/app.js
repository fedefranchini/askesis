// Shared behaviour for every page. Without it everything still works; it only removes friction on the phone.
(function () {
  "use strict";

  // 1. Opened from the Home screen there is no reload button: when the page comes back after a while, or on a new
  //    day, reload it so it never shows yesterday's numbers. Never while something is being typed.
  let dirty = false;
  document.addEventListener("input", () => { dirty = true; });
  const opened = new Date();
  let hiddenAt = null;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") { hiddenAt = Date.now(); return; }
    const away = hiddenAt ? Date.now() - hiddenAt : 0;
    const newDay = new Date().toDateString() !== opened.toDateString();
    if (!dirty && (newDay || away > 5 * 60 * 1000) && !document.querySelector(".receipt")) location.reload();
  });

  // 2. One tap, one submission: the button says what is happening and cannot be pressed twice.
  document.addEventListener("submit", (ev) => {
    const form = ev.target;
    if (form.dataset.sent) { ev.preventDefault(); return; }
    form.dataset.sent = "1";
    const btn = ev.submitter;
    // after the browser has read the form (a disabled button would drop its name and value)
    setTimeout(() => {
      form.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      if (btn && form.method.toLowerCase() === "post") btn.textContent = btn.value === "save" ? "Salvataggio…" : "Attendi…";
    }, 0);
  });
  window.addEventListener("pageshow", (ev) => {  // back/forward cache: make the buttons usable again
    if (!ev.persisted) return;
    document.querySelectorAll("form[data-sent]").forEach((f) => {
      delete f.dataset.sent;
      f.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    });
  });

  // 3. Lists that open what is chosen at once (the "Apri" button stays for keyboards and without this script).
  document.querySelectorAll("select[data-autosubmit]").forEach((sel) => {
    sel.addEventListener("change", () => { if (sel.form.requestSubmit) sel.form.requestSubmit(); else sel.form.submit(); });
  });
})();
