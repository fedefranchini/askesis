// Questionnaire on the Today page. Without this script every scale is visible and the form works the same.
// - Under each scale: the words for the chosen value (descriptions from the server, askesis/checkin.py).
// - Stepper: one question at a time, a tap moves to the next one; "Salva" stays available at every step.
// - Links to #checkin (the "Da fare" list) open the questionnaire.
(function () {
  "use strict";
  document.querySelectorAll("fieldset.scale").forEach((fs) => {
    let desc = {};
    try { desc = JSON.parse(fs.dataset.desc || "{}"); } catch (e) { return; }
    const out = fs.querySelector(".picked");
    fs.addEventListener("change", (ev) => {
      const v = ev.target.value;
      if (out && desc[v]) { out.textContent = v + " · " + desc[v]; out.classList.remove("none"); }
    });
  });

  document.querySelectorAll("form[data-stepper]").forEach((form) => {
    const steps = Array.from(form.querySelectorAll(":scope > fieldset.scale"));
    if (steps.length < 2) return;
    let at = 0;
    const bar = document.createElement("div");
    bar.className = "stepbar";
    bar.innerHTML = '<span class="stepcount" aria-live="polite"></span>' +
      '<button type="button" class="secondary step-back">Indietro</button>' +
      '<button type="button" class="secondary step-next">Salta</button>';
    form.insertBefore(bar, steps[steps.length - 1].nextSibling);
    const count = bar.querySelector(".stepcount");
    const back = bar.querySelector(".step-back");
    const next = bar.querySelector(".step-next");
    const answered = (fs) => !!fs.querySelector("input:checked");
    function show(i) {
      at = Math.max(0, Math.min(steps.length - 1, i));
      steps.forEach((fs, k) => { fs.hidden = k !== at; });
      count.textContent = "Domanda " + (at + 1) + " di " + steps.length;
      back.disabled = at === 0;
      next.hidden = at === steps.length - 1;
      next.textContent = answered(steps[at]) ? "Avanti" : "Salta";
    }
    form.classList.add("stepping");
    back.addEventListener("click", () => show(at - 1));
    next.addEventListener("click", () => show(at + 1));
    steps.forEach((fs, k) => fs.addEventListener("change", () => {
      next.textContent = "Avanti";
      if (k < steps.length - 1) setTimeout(() => { if (at === k) show(k + 1); }, 220);
    }));
    show(Math.max(0, steps.findIndex((fs) => !answered(fs))));
  });

  function openCheckin() {
    const d = document.querySelector("#checkin details");
    if (d) { d.open = true; }
  }
  document.querySelectorAll('a[href="#checkin"]').forEach((a) => a.addEventListener("click", openCheckin));
  window.addEventListener("hashchange", () => { if (location.hash === "#checkin") openCheckin(); });
  if (location.hash === "#checkin") openCheckin();
})();
