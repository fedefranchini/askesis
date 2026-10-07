// Questionnaire scales on the Today page: show the words for the chosen value under each scale. The descriptions come
// from the server (askesis/checkin.py); nothing is computed here.
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
})();
