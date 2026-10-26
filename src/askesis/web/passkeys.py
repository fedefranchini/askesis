"""Passkeys (WebAuthn) for the single dashboard user: Face ID / Touch ID instead of the password.

Stored next to the password file, owner-only: per credential its public key, signature counter, a coarse label and
dates — never anything secret (the private key never leaves the phone's secure enclave / keychain). A passkey is
registered only from a logged-in session on a trusted network (the VPN), so a stolen password alone can never add
one from the internet. Passkeys need HTTPS on a real domain: without `web_origin` the feature is off.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from askesis.store import state

RP_NAME = "Askesis"
USER_NAME = "atleta"
MAX_PASSKEYS = 10


class PasskeyError(ValueError):
    pass


@dataclass(frozen=True)
class Party:
    origin: str  # e.g. https://example.app
    rp_id: str  # the domain

    @classmethod
    def from_origin(cls, origin: str) -> Party | None:
        if not origin:
            return None
        u = urlparse(origin)
        if u.scheme != "https" or not u.hostname:
            raise ValueError("web_origin deve essere un indirizzo https://dominio")
        return cls(origin.rstrip("/"), u.hostname)


def _write_private(path: Path, data: dict) -> None:
    state.write_private(path, json.dumps(data, indent=1))


def _load(path: Path) -> dict:
    data = json.loads(state.read_text(path)) if state.exists(path) else {}
    data.setdefault("user_id", bytes_to_base64url(secrets.token_bytes(16)))
    data.setdefault("credentials", {})
    return data


def listing(path: Path) -> list[dict]:
    creds = _load(path)["credentials"]
    return sorted(({"id": k, **{x: v[x] for x in ("label", "created", "last_used")}} for k, v in creds.items()),
                  key=lambda c: c["created"])


def alive(path: Path, cred_id: str) -> bool:
    return cred_id in _load(path)["credentials"]


def revoke(path: Path, cred_id: str | None) -> None:
    """One passkey, or all of them (None). The phone keeps its copy: remove it there too if wanted."""
    data = _load(path)
    data["credentials"] = {} if cred_id is None else {k: v for k, v in data["credentials"].items() if k != cred_id}
    _write_private(path, data)


# ------------------------------------------------------------------ registration (trusted network, logged in)
def registration_options(path: Path, party: Party) -> tuple[str, bytes]:
    data = _load(path)
    if len(data["credentials"]) >= MAX_PASSKEYS:
        raise PasskeyError(f"al massimo {MAX_PASSKEYS} passkey: revocane una prima")
    opts = generate_registration_options(
        rp_id=party.rp_id, rp_name=RP_NAME, user_name=USER_NAME, user_display_name="Askesis",
        user_id=base64url_to_bytes(data["user_id"]),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k)) for k in data["credentials"]])
    _write_private(path, data)  # the user id is stable from the first registration on
    return options_to_json(opts), opts.challenge


def register(path: Path, party: Party, credential: dict, challenge: bytes, label: str) -> str:
    try:
        v = verify_registration_response(credential=credential, expected_challenge=challenge,
                                         expected_rp_id=party.rp_id, expected_origin=party.origin,
                                         require_user_verification=True)
    except Exception as exc:  # noqa: BLE001  any verification failure: refuse, never store
        raise PasskeyError("passkey non valida") from exc
    data = _load(path)
    cid = bytes_to_base64url(v.credential_id)
    now = time.time()
    data["credentials"][cid] = {"public_key": bytes_to_base64url(v.credential_public_key),
                                "sign_count": v.sign_count, "label": label[:60], "created": now, "last_used": now}
    _write_private(path, data)
    return cid


# ------------------------------------------------------------------ authentication (public login page)
def authentication_options(path: Path, party: Party) -> tuple[str, bytes]:
    creds = _load(path)["credentials"]
    if not creds:
        raise PasskeyError("nessuna passkey registrata")
    opts = generate_authentication_options(
        rp_id=party.rp_id, user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k)) for k in creds])
    return options_to_json(opts), opts.challenge


def authenticate(path: Path, party: Party, credential: dict, challenge: bytes) -> str:
    """The credential id on success; PasskeyError otherwise (unknown key, bad signature, replayed counter)."""
    data = _load(path)
    cid = str(credential.get("id", ""))
    rec = data["credentials"].get(cid)
    if rec is None:
        raise PasskeyError("passkey non riconosciuta")
    try:
        v = verify_authentication_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=party.rp_id,
            expected_origin=party.origin, credential_public_key=base64url_to_bytes(rec["public_key"]),
            credential_current_sign_count=rec["sign_count"], require_user_verification=True)
    except Exception as exc:  # noqa: BLE001
        raise PasskeyError("verifica della passkey non riuscita") from exc
    rec["sign_count"], rec["last_used"] = v.new_sign_count, time.time()
    _write_private(path, data)
    return cid
