"""Passkeys with a software authenticator (a P-256 key made in the test, never a real one): registration only from
the trusted network, login with Face ID semantics (user verification), counter and origin checks, revocation ending
sessions, public requests limited to passkeys, every attempt in the access log."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import struct

import cbor2
import pytest
import synthetic as syn
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from askesis import config
from askesis.ingestion.pipeline import ingest
from askesis.store.db import connect
from askesis.web import auth
from askesis.web.app import create_app, paths

ORIGIN = "https://askesis.test"
RP_ID = "askesis.test"
PUBLIC = {"x-askesis-edge": "public", "x-forwarded-for": "198.51.100.7"}  # documentation address range


class SoftAuthenticator:
    def __init__(self, origin: str = ORIGIN, rp_id: str = RP_ID):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = secrets.token_bytes(16)
        self.origin, self.rp_id, self.count = origin, rp_id, 0

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin}).encode()

    def create(self, options: dict) -> dict:
        cd = self._client_data("webauthn.create", options["challenge"])
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x45]) + struct.pack(">I", self.count)
                     + bytes(16) + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cid = bytes_to_base64url(self.cred_id)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cd), "attestationObject": bytes_to_base64url(att),
                             "transports": ["internal"]}}

    def get(self, options: dict, bump: int = 1) -> dict:
        self.count += bump
        cd = self._client_data("webauthn.get", options["challenge"])
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + struct.pack(">I", self.count)
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.cred_id)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cd),
                             "authenticatorData": bytes_to_base64url(auth_data),
                             "signature": bytes_to_base64url(sig), "userHandle": None}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    private = tmp_path / "private.toml"
    private.write_text('timezone = "Europe/Berlin"\nweb_secure_cookies = true\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "pk.db"))
    monkeypatch.setenv("ASKESIS_WEB_ORIGIN", ORIGIN)
    cfg = config.load()
    ingest(connect(cfg.db_path), syn.athlete(), "synthetic")
    password = secrets.token_urlsafe(16)  # generated test credential
    auth.set_password(paths(cfg)[0], password)
    return cfg, password


def client(cfg) -> TestClient:
    """Requests arrive through the local HTTPS front (loopback), which marks the public ones."""
    return TestClient(create_app(cfg), base_url=ORIGIN, client=("127.0.0.1", 50000))


def csrf(c: TestClient, path: str = "/login", headers: dict | None = None) -> str:
    return re.search(r'(?:name="csrf" value|data-csrf)="([^"]+)"', c.get(path, headers=headers or {}).text).group(1)


def password_login(c: TestClient, password: str):
    return c.post("/login", data={"csrf": csrf(c), "password": password}, follow_redirects=False)


def register(c: TestClient, a: SoftAuthenticator):
    tok = csrf(c, "/accesso")
    o = c.post("/passkey/register/options", headers={"x-csrf": tok}, json={})
    assert o.status_code == 200, o.text
    return c.post("/passkey/register/verify", headers={"x-csrf": tok}, json={"credential": a.create(o.json())})


def passkey_login(c: TestClient, a: SoftAuthenticator, headers: dict | None = None, bump: int = 1, remember=False):
    h = headers or {}
    tok = csrf(c, "/login", h)
    o = c.post("/passkey/login/options", headers=h | {"x-csrf": tok}, json={})
    if o.status_code != 200:
        return o
    opts = o.json()
    assert opts["userVerification"] == "required"
    return c.post("/passkey/login/verify", headers=h | {"x-csrf": tok},
                  json={"credential": a.get(opts, bump), "remember": remember})


def test_register_then_login_with_the_passkey_only(env):
    cfg, password = env
    c = client(cfg)
    assert password_login(c, password).status_code == 303
    a = SoftAuthenticator()
    assert register(c, a).json() == {"ok": True}
    assert "Ultimo uso" not in c.get("/accesso").text and "Revoca" in c.get("/accesso").text

    pub = client(cfg)  # another browser, from the internet
    page = pub.get("/login", headers=PUBLIC).text
    assert "Accedi con Face ID" in page and 'name="password"' not in page  # no password form from the internet
    r = passkey_login(pub, a, PUBLIC, remember=True)
    assert r.status_code == 200 and r.json()["redirect"] == "/"
    assert pub.get("/", headers=PUBLIC).status_code == 200
    device = next(x for x in pub.cookies.jar if x.name == "askesis_device")
    assert device.secure  # HTTPS: cookies only over HTTPS
    log = auth.recent_attempts(cfg.db_path.parent / "web_access.log")
    assert {"method": "passkey", "ok": True, "ip": "198.51.100.7"}.items() <= log[-1].items()


def test_password_is_refused_from_the_internet_and_logged(env):
    cfg, password = env
    c = client(cfg)
    page = c.get("/login", headers=PUBLIC).text
    assert 'name="password"' not in page and "solo con una passkey" in page
    tok = csrf(c, "/login")  # same browser session, token from the trusted page
    r = c.post("/login", headers=PUBLIC, data={"csrf": tok, "password": password}, follow_redirects=False)
    assert r.status_code == 403 and "solo con la passkey" in r.text
    last = auth.recent_attempts(cfg.db_path.parent / "web_access.log")[-1]
    assert last["ok"] is False and last["detail"] == "rete pubblica" and password not in json.dumps(last)


def test_a_passkey_is_added_only_from_the_trusted_network(env):
    cfg, password = env
    a = SoftAuthenticator()
    c = client(cfg)
    password_login(c, password)
    assert register(c, a).status_code == 200
    pub = client(cfg)
    assert passkey_login(pub, a, PUBLIC).status_code == 200
    tok = csrf(pub, "/accesso", PUBLIC)
    r = pub.post("/passkey/register/options", headers=PUBLIC | {"x-csrf": tok}, json={})
    assert r.status_code == 403 and "VPN" in r.json()["error"]


def test_wrong_origin_replayed_counter_and_missing_csrf_are_refused(env):
    cfg, password = env
    c = client(cfg)
    password_login(c, password)
    a = SoftAuthenticator()
    register(c, a)
    evil = SoftAuthenticator(origin="https://askesis.example")
    evil.key, evil.cred_id = a.key, a.cred_id
    assert passkey_login(client(cfg), evil).status_code == 401  # signed for another site
    assert passkey_login(client(cfg), a, bump=5).status_code == 200
    assert passkey_login(client(cfg), a, bump=0).status_code == 401  # counter did not grow: cloned key
    other = client(cfg)
    assert other.post("/passkey/login/options", json={}).status_code == 403  # no CSRF header


def test_revoking_the_passkey_ends_its_sessions(env):
    cfg, password = env
    c = client(cfg)
    password_login(c, password)
    a = SoftAuthenticator()
    register(c, a)
    phone = client(cfg)
    passkey_login(phone, a)
    assert phone.get("/", follow_redirects=False).status_code == 200
    cid = bytes_to_base64url(a.cred_id)
    c.post("/accesso", data={"csrf": csrf(c, "/accesso"), "revoke_passkey": cid})
    assert phone.get("/", follow_redirects=False).status_code == 303  # back to the login page
    assert base64url_to_bytes(cid) == a.cred_id


def test_failures_slow_down_further_attempts(env):
    cfg, password = env
    c = client(cfg)
    password_login(c, password)
    a = SoftAuthenticator()
    register(c, a)
    stranger = SoftAuthenticator()  # a key the server has never seen
    stranger.cred_id = a.cred_id
    pub = client(cfg)
    assert passkey_login(pub, stranger, PUBLIC).status_code == 401
    assert passkey_login(pub, a, PUBLIC).status_code == 429  # even the right key waits after a failure


def test_without_a_domain_passkeys_are_off(env, monkeypatch):
    cfg, password = env
    monkeypatch.delenv("ASKESIS_WEB_ORIGIN")
    cfg = config.load()
    c = TestClient(create_app(cfg), base_url="http://127.0.0.1")
    assert "Accedi con Face ID" not in c.get("/login").text
    assert c.post("/passkey/login/options", json={}).status_code == 404


def test_offline_page_is_static_and_registered_only_over_https(env):
    cfg, _ = env
    c = client(cfg)
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["content-type"].startswith("text/javascript")
    assert 'req.method !== "GET"' in sw.text and "offline.html" in sw.text  # forms never cached or queued
    page = c.get("/static/offline.html")
    assert page.status_code == 200 and "Askesis non è raggiungibile" in page.text and "<script" not in page.text
    js = (config.ROOT / "src/askesis/web/static/app.js").read_text()
    assert 'window.isSecureContext && location.protocol === "https:"' in js


def test_forwarded_requests_are_public_unless_marked_by_the_vpn_site(env, monkeypatch):
    """Fail-closed behind the HTTPS front: a forwarded request without the VPN site's secret mark is public, whatever
    it claims in X-Forwarded-For; the client-written part of that header is never trusted."""
    cfg, password = env
    monkeypatch.setenv("ASKESIS_EDGE_SECRET", "s3cr3t-test-mark")
    for headers in ({"x-forwarded-for": "127.0.0.1"},  # no mark: e.g. a tunnel or a misconfigured front
                    {"x-forwarded-for": "127.0.0.1", "x-askesis-edge": "vpn-wrong"},
                    {"x-forwarded-for": "127.0.0.1, 198.51.100.9", "x-askesis-edge": "public"}):
        c = client(cfg)
        tok = csrf(c, "/login")
        r = c.post("/login", headers=headers, data={"csrf": tok, "password": password}, follow_redirects=False)
        assert r.status_code == 403, headers
    c = client(cfg)
    tok = csrf(c, "/login")
    vpn = {"x-forwarded-for": "100.64.0.9", "x-askesis-edge": "vpn-s3cr3t-test-mark"}
    assert c.post("/login", headers=vpn, data={"csrf": tok, "password": password},
                  follow_redirects=False).status_code == 303
    last = auth.recent_attempts(cfg.db_path.parent / "web_access.log")[-1]
    assert last["ip"] == "100.64.0.9" and last["ok"] is True


def test_a_session_without_csrf_token_accepts_no_post(env):
    """A session just rebuilt from a remembered device has no CSRF token yet: no value, not even a guessed
    placeholder, may pass the check."""
    cfg, password = env
    c = client(cfg)
    tok = csrf(c, "/login")
    c.post("/login", data={"csrf": tok, "password": password, "remember": "1"})
    c.cookies.delete("askesis_session")  # the 12-hour session expired; the device cookie is still there
    for guess in ("!", "", "None"):
        r = c.post("/", data={"csrf": guess, "action": "save", "line": "p 240"})
        assert "Sessione scaduta" in r.text and "Salvato" not in r.text

