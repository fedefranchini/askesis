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
