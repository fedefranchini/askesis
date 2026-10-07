"""Dashboard listening addresses that follow a VPN interface, and the reachability check (no real network)."""

from __future__ import annotations

import json
import secrets

import pytest

from askesis.web import health, netwatch

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255
en9: flags=8863<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 100.70.0.9 netmask 0xffc00000
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1420
\tinet 100.64.0.1 --> 100.64.0.1 netmask 0xffc00000
"""


def test_tunnel_ranges_match_only_point_to_point_interfaces():
    ifaces = netwatch.interfaces(IFCONFIG)
    got = netwatch.resolve(["127.0.0.1", "tunnel:100.64.0.0/10"], ifaces)
    assert got == ["127.0.0.1", "100.64.0.1"]  # en9 is in range but not a tunnel: never served


def test_explicit_addresses_are_kept_and_wildcards_refused():
    assert netwatch.resolve(["127.0.0.1", "100.64.0.1"], []) == ["127.0.0.1", "100.64.0.1"]
    for bad in (["0.0.0.0"], ["::"], ["tunnel:0.0.0.0/0"], ["tunnel:100.64.0.1/10"], ["nome-host"]):
        with pytest.raises(ValueError):
            netwatch.check_bind(bad)


def test_watcher_restarts_when_the_tunnel_appears_flaps_or_changes():
    lo, a, b = "127.0.0.1", "100.64.0.1", "100.64.0.2"
    assert netwatch.decide({lo}, {lo}) == ({lo}, False)          # VPN still down: keep serving loopback
    assert netwatch.decide({lo}, {lo, a}) == ({lo}, True)        # VPN came up after login
    served, restart = netwatch.decide({lo, a}, {lo})             # reconnecting: address gone
    assert (served, restart) == ({lo}, False)
    assert netwatch.decide(served, {lo, a})[1] is True           # back again: rebind with fresh sockets
    assert netwatch.decide({lo, a}, {lo, b})[1] is True          # address changed


def test_watch_loop_calls_restart_once_the_address_is_present():
    states = iter([{"127.0.0.1"}, {"127.0.0.1", "100.64.0.1"}])
    msgs = []
    netwatch.watch(["127.0.0.1", "100.64.0.1"], {"127.0.0.1"}, probe=lambda _: next(states),
                   sleep=lambda _: None, on_restart=msgs.append)
    assert msgs and "100.64.0.1" in msgs[0]


def test_rejected_clients_are_read_from_the_log(tmp_path):
    log = tmp_path / "web.log"
    log.write_text("Dashboard su http://127.0.0.1:8765\n"
                   "2026-01-01T10:00:00 client rifiutato: 100.64.0.7\n"
                   "2026-01-03T10:00:00 client rifiutato: 100.64.0.8\n")
    assert health.rejected_clients(log, "2026-01-02T00:00:00") == ["100.64.0.8"]


def test_notifications_only_when_the_state_changes(tmp_path):
    state, sent = tmp_path / "s.json", []
    bad = health.Report(problems=["la dashboard non risponde"])
    assert health.notify_on_change(bad, state, sent.append) is True
    assert health.notify_on_change(bad, state, sent.append) is False      # same problem: silence
    assert health.notify_on_change(health.Report(), state, sent.append) is True
    assert sent == ["la dashboard non risponde", "Dashboard di nuovo raggiungibile"]
    assert json.loads(state.read_text()) == {"problems": []}


def test_rejected_client_is_logged_once(tmp_path, monkeypatch, capsys):
    from starlette.testclient import TestClient

    from askesis import config
    from askesis.web import auth
    from askesis.web.app import create_app, paths

    private = tmp_path / "p.toml"
    private.write_text('timezone = "Europe/Berlin"\nweb_allowed_clients = ["100.64.0.2"]\n')
    monkeypatch.setenv("ASKESIS_CONFIG", str(private))
    monkeypatch.setenv("ASKESIS_DB_PATH", str(tmp_path / "c.db"))
    cfg = config.load()
    auth.set_password(paths(cfg)[0], secrets.token_urlsafe(16))
    other = TestClient(create_app(cfg), base_url="http://127.0.0.1", client=("100.64.0.9", 5000))
    assert other.get("/login").status_code == 403 and other.get("/login").status_code == 403
    assert capsys.readouterr().err.count("client rifiutato: 100.64.0.9") == 1
