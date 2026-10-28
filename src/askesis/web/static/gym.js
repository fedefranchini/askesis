// Gym sheet: the load typed in a set is copied into the following empty sets of the same exercise (they usually
// match); a value copied this way is replaced again if the first one changes, never one typed by hand.
(function () {
  "use strict";
  document.querySelectorAll("input[data-lift][data-set]").forEach((inp) => {
    inp.addEventListener("input", () => {
      delete inp.dataset.copied;
      const lift = inp.dataset.lift, from = Number(inp.dataset.set);
      document.querySelectorAll(`input[data-lift="${lift}"]`).forEach((other) => {
        if (Number(other.dataset.set) <= from) return;
        if (other.value === "" || other.dataset.copied) { other.value = inp.value; other.dataset.copied = "1"; }
      });
    });
  });
})();

// Rest timer and session length. Writing the reps of a set ends that set: the rest timer starts, counting up against
// the plan's rest range for that exercise (amber once the shorter rest has passed, green at the longer one). The time
// of the first and the last set of the day stays in this browser only; the "after the workout" card in Oggi offers
// it as the session length, to be confirmed there. Nothing is sent from here.
(function () {
  "use strict";
  const box = document.querySelector(".rest-timer");
  if (!box) return;
  const day = box.dataset.day;
  const clock = box.querySelector(".rt-clock"), goal = box.querySelector(".rt-goal");
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode: timer only */ } },
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* nothing to do */ } },
  };
  const fmt = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const mins = (s) => `${(s / 60).toLocaleString("it-IT", { maximumFractionDigits: 1 })}`;
  let tick = null;

  function render() {
    const t = store.get("rest");
    if (!t || Date.now() - t.at > 30 * 60 * 1000) { box.hidden = true; clearInterval(tick); tick = null; return; }
    const s = Math.floor((Date.now() - t.at) / 1000);
    box.hidden = false;
    clock.textContent = fmt(s);
    goal.textContent = ` · recupero ${mins(t.lo)}–${mins(t.hi)} min`;
    box.classList.toggle("ready", s >= t.lo && s < t.hi);
    box.classList.toggle("go", s >= t.hi);
  }
  function start(lo, hi) {
    store.set("rest", { at: Date.now(), lo, hi });
    if (!tick) tick = setInterval(render, 1000);
    render();
  }
  box.querySelector(".rt-stop").addEventListener("click", () => { store.del("rest"); render(); });

  document.querySelectorAll("input[data-reps]").forEach((inp) => {
    inp.addEventListener("change", () => {
      if (!/^\d+$/.test(inp.value.trim())) return;
      const [lo, hi] = (inp.closest("[data-rest]")?.dataset.rest || "90-120").split("-").map(Number);
      start(lo, hi);
      const span = store.get("gym-" + day) || {};
      span.first = span.first || Date.now();
      span.last = Date.now();
      store.set("gym-" + day, span);
    });
  });
  if (store.get("rest")) tick = setInterval(render, 1000);
  render();
})();

// Draft kept on the phone: every value typed in the sheet is copied to this browser's storage at once, and restored
// into empty fields when the page comes back (Safari may discard a page left in the background during the session).
// The draft is removed once the sheet is saved ("Salvato") and is never sent anywhere by itself.
(function () {
  "use strict";
  const form = document.querySelector("form.gymform");
  const day = document.querySelector(".rest-timer")?.dataset.day || form?.querySelector("input[name=day]")?.value;
  if (!day) return;
  const key = "gym-draft-" + day;
  const store = {
    get() { try { return JSON.parse(localStorage.getItem(key)) || {}; } catch (e) { return {}; } },
    set(v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (e) { /* private mode: no draft */ } },
    del() { try { localStorage.removeItem(key); } catch (e) { /* nothing to do */ } },
  };
  if (document.querySelector(".receipt.saved")) { store.del(); return; }
  if (!form) return;
  const fields = () => form.querySelectorAll("input[name^=kg_], input[name^=reps_], input[name^=rir_], " +
                                              "input[name=run], textarea[name=notes]");
  const draft = store.get();
  let restored = 0;
  fields().forEach((f) => {
    if (!f.value && draft[f.name]) { f.value = draft[f.name]; restored += 1; }
  });
  if (restored) {
    const note = document.createElement("p");
    note.className = "hint draft-note";
    note.textContent = `Ripresi ${restored} valori scritti prima sul telefono (non ancora salvati).`;
    form.prepend(note);
  }
  form.addEventListener("input", () => {
    const d = store.get();
    fields().forEach((f) => { if (f.value) d[f.name] = f.value; else delete d[f.name]; });
    store.set(d);
  });
})();
