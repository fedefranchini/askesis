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

// Session length measured by the gym sheet (first to last set, kept in this browser only): offered in place of the
// plan's estimate while nothing has been saved. Under 15 minutes it is not a measure (the sheet was filled at the end).
(function () {
  "use strict";
  const inp = document.querySelector("input[data-measure]");
  if (!inp) return;
  let span = null;
  try { span = JSON.parse(localStorage.getItem("gym-" + inp.dataset.measure)); } catch (e) { return; }
  if (!span || !span.first || !span.last) return;
  const min = Math.round((span.last - span.first) / 60000);
  if (min < 15 || min > 300) return;
  const hm = (t) => new Date(t).toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });
  inp.value = String(min);
  const note = inp.closest("form").querySelector(".measured");
  if (note) {
    note.textContent = `Misurata dalla scheda della palestra: prima serie alle ${hm(span.first)}, ultima alle ${hm(span.last)}.`;
    note.hidden = false;
  }
})();

// "#peso" (e.g. a morning reminder that opens the dashboard): straight to the weight field, if it is still missing.
(function () {
  "use strict";
  if (location.hash !== "#peso") return;
  const w = document.getElementById("peso");
  if (w) { w.scrollIntoView({ block: "center" }); w.focus(); }
})();
