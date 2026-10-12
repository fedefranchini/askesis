// Passkeys (WebAuthn): login with Face ID on the login page, registration on the Access page. The server does every
// check (challenge, origin, signature, counter); this only converts between JSON and the browser API.
(function () {
  "use strict";
  if (!window.PublicKeyCredential || !window.isSecureContext) return;  // needs HTTPS on the dashboard domain

  const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const unb64 = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "==".slice(0, (4 - s.length % 4) % 4)), (c) => c.charCodeAt(0));
  const post = (url, csrf, body) => fetch(url, {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRF": csrf}, body: JSON.stringify(body || {})});
  function show(el, text) { if (el) { el.textContent = text; el.hidden = false; } }

  // Login
  const box = document.getElementById("passkey-login");
  if (box) {
    box.hidden = false;
    const err = document.getElementById("passkey-error");
    document.getElementById("passkey-go").addEventListener("click", async () => {
      err.hidden = true;
      try {
        const r = await post("/passkey/login/options", box.dataset.csrf);
        const o = await r.json();
        if (!r.ok) return show(err, o.error || "Accesso non disponibile.");
        o.challenge = unb64(o.challenge);
        (o.allowCredentials || []).forEach((c) => { c.id = unb64(c.id); });
        const cred = await navigator.credentials.get({publicKey: o});
        const res = cred.response;
        const v = await post("/passkey/login/verify", box.dataset.csrf, {
          remember: document.getElementById("passkey-remember").checked,
          credential: {id: cred.id, rawId: b64(cred.rawId), type: cred.type, clientExtensionResults: {},
            response: {clientDataJSON: b64(res.clientDataJSON), authenticatorData: b64(res.authenticatorData),
              signature: b64(res.signature), userHandle: res.userHandle ? b64(res.userHandle) : null}}});
        const out = await v.json();
        if (v.ok && out.redirect) { location.href = out.redirect; } else { show(err, out.error || "Accesso non riuscito."); }
      } catch (e) {
        show(err, "Accesso annullato o non riuscito.");
      }
    });
  }

  // Registration (Access page, trusted network only: the server refuses otherwise)
  const add = document.getElementById("passkey-add");
  const section = document.getElementById("passkeys");
  if (add && section) {
    add.hidden = false;
    const err = document.getElementById("passkey-error");
    add.addEventListener("click", async () => {
      err.hidden = true;
      try {
        const r = await post("/passkey/register/options", section.dataset.csrf);
        const o = await r.json();
        if (!r.ok) return show(err, o.error || "Non disponibile.");
        o.challenge = unb64(o.challenge);
        o.user.id = unb64(o.user.id);
        (o.excludeCredentials || []).forEach((c) => { c.id = unb64(c.id); });
        const cred = await navigator.credentials.create({publicKey: o});
        const res = cred.response;
        const v = await post("/passkey/register/verify", section.dataset.csrf, {
          credential: {id: cred.id, rawId: b64(cred.rawId), type: cred.type, clientExtensionResults: {},
            response: {clientDataJSON: b64(res.clientDataJSON), attestationObject: b64(res.attestationObject),
              transports: res.getTransports ? res.getTransports() : []}}});
        const out = await v.json();
        if (v.ok) { location.reload(); } else { show(err, out.error || "Passkey non aggiunta."); }
      } catch (e) {
        show(err, "Operazione annullata o non riuscita.");
      }
    });
  }
})();
