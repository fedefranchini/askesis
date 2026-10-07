"""Dashboard reachability check, run by a launchd agent: everything that can be verified from this Mac.

What it cannot see: the phone's side (its VPN app, its permissions). An HTTP request from this Mac to its own tunnel
address does not test that path either (it is routed into the tunnel), so listening sockets are read with lsof.
Notifications are sent only when the state changes (problem appears, changes, or is solved), never on every run.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import netwatch

LABEL = "local.askesis.dashboard"


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def agent_running(label: str = LABEL) -> bool:
    out = _run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"])
    return bool(re.search(r"^\s*state = running", out, re.M))


def listening(port: int) -> set[str]:
    """Local addresses with a TCP socket listening on the port (any process of this user)."""
    out = _run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"])
    return {m.group(1) for m in re.finditer(r"^n(\S+):\d+$", out, re.M)}


def loopback_answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET /login HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            return s.recv(64).startswith(b"HTTP/1.")
    except OSError:
        return False


def firewall_blocks(executable: str) -> bool:
    """True when the macOS application firewall would block incoming connections to this executable."""
    tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    state = _run([tool, "--getglobalstate"])
    if "enabled" not in state.lower():
        return False
    if "set to enabled" in _run([tool, "--getblockall"]).lower():
        return True
    return "is permitted" not in _run([tool, "--getappblocked", os.path.realpath(executable)])


NE_PREFS = Path("/Library/Preferences/com.apple.networkextension.plist")


def connected_vpns(nc_list: str | None = None) -> list[str]:
    """Names of the VPN configurations macOS reports as connected."""
    if nc_list is None:
        nc_list = _run(["/usr/sbin/scutil", "--nc", "list"])
    return re.findall(r'^\*?\s*\(Connected\).*?"([^"]+)"', nc_list, re.M)


def include_all_networks(prefs: Path = NE_PREFS) -> dict[str, bool]:
    """VPN configuration name -> includeAllNetworks ("all traffic through the tunnel", how VPN apps implement a
    system-wide kill switch on macOS). Read-only; unknown when the file cannot be read."""
    import plistlib

    try:
        data = plistlib.loads(prefs.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    objs = data.get("$objects", [])

    def res(x):
        return objs[x.data] if isinstance(x, plistlib.UID) else x

    def parent_of(target):
        return next((o for o in objs if isinstance(o, dict) and any(
            isinstance(v, plistlib.UID) and objs[v.data] is target for v in o.values())), None)

    out = {}
    for o in objs:
        if isinstance(o, dict) and "IncludeAllNetworks" in o:
            proto = parent_of(o)
            conf = parent_of(proto) if proto is not None else None
            if conf is not None and "Name" in conf:
                out[str(res(conf["Name"]))] = bool(res(o["IncludeAllNetworks"]))
    return out


def full_tunnel_vpns(nc_list: str | None = None, flags: dict[str, bool] | None = None) -> list[str]:
    """Connected VPNs that force all traffic through the tunnel. Observed (NordVPN, Kill Switch on): the phone's
    request reaches the Mac but macOS drops the reply before any interface, so connections opened by the phone never
    complete; with the VPN connected and the kill switch off, they do."""
    flags = include_all_networks() if flags is None else flags
    return [n for n in connected_vpns(nc_list) if flags.get(n)]


def half_open(port: int, clients: list[str], netstat_text: str | None = None) -> list[str]:
    """Allowed clients with connections stuck in SYN_RCVD: their request arrives, the reply never completes."""
    if netstat_text is None:
        netstat_text = _run(["/usr/sbin/netstat", "-an", "-p", "tcp"])
    found = []
    for line in netstat_text.splitlines():
        cols = line.split()
        if len(cols) >= 6 and cols[3].endswith(f".{port}") and cols[5] == "SYN_RCVD":
            peer = cols[4].rsplit(".", 1)[0]
            if peer in clients and peer not in found:
                found.append(peer)
    return found


def rejected_clients(log: Path, since: str) -> list[str]:
    """Addresses refused by the client allowlist after `since` (ISO timestamp), from the server log."""
    from .app import REJECTED

    if not log.exists():
        return []
    found = []
    for line in log.read_text(errors="replace").splitlines()[-2000:]:
        m = re.match(rf"^(\S+) {re.escape(REJECTED)} (\S+)$", line)
        if m and m.group(1) > since and m.group(2) not in found:
            found.append(m.group(2))
    return found


def check(cfg, port: int, since: str = "", executable: str = sys.executable) -> Report:
    r = Report()
    if not agent_running():
        r.problems.append("l'agente della dashboard non è in esecuzione")
    if not loopback_answers(port):
        r.problems.append(f"la dashboard non risponde su 127.0.0.1:{port}")
    remote = [a for a in netwatch.resolve(cfg.web_bind) if a not in ("127.0.0.1", "::1")]
    tunnels = [e for e in cfg.web_bind if e.startswith(netwatch.TUNNEL)]
    if tunnels and not remote:
        r.problems.append("nessun indirizzo della VPN sul Mac (Meshnet spenta o disconnessa?)")
    up = netwatch.present(remote)
    socks = listening(port)
    for a in remote:
        if a not in up:
            r.problems.append(f"{a} non è presente sul Mac (Meshnet spenta o indirizzo cambiato?)")
        elif a not in socks:
            r.problems.append(f"{a} è attivo ma la dashboard non è in ascolto lì (riavvio in corso?)")
        else:
            r.info.append(f"in ascolto su {a}:{port}")
    for name in (full_tunnel_vpns() if remote else []):
        r.problems.append(f"Kill Switch attivo sulla VPN «{name}» (tutto il traffico forzato nel tunnel): macOS scarta "
                          "le risposte alle connessioni dall'iPhone; spegni il Kill Switch, la VPN può restare accesa")
    for host in half_open(port, cfg.web_allowed_clients):
        r.problems.append(f"connessioni da {host} bloccate a metà (la risposta del Mac non arriva)")
    if remote and firewall_blocks(executable):
        r.problems.append("il firewall di macOS blocca le connessioni in entrata verso Python")
    for host in rejected_clients(cfg.db_path.parent / "web.log", since):
        r.problems.append(f"connessione rifiutata da {host}: se è il tuo iPhone, il suo indirizzo è cambiato "
                          "(web_allowed_clients)")
    return r


def notify_on_change(report: Report, state_file: Path, send=None) -> bool:
    """Notify only when the set of problems differs from the last run. Returns True if a notification was sent."""
    previous = json.loads(state_file.read_text()) if state_file.exists() else {"problems": []}
    if previous.get("problems", []) == report.problems:
        return False
    state_file.write_text(json.dumps({"problems": report.problems}, ensure_ascii=False))
    text = (report.problems[0] + (f" (+{len(report.problems) - 1})" if len(report.problems) > 1 else "")
            if report.problems else "Dashboard di nuovo raggiungibile")
    (send or _osascript)(text)
    return True


def _osascript(text: str) -> None:
    safe = text.replace("\\", "").replace('"', "'")
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "Askesis — dashboard"'],
                   check=False)
