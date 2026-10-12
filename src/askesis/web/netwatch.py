"""Listening addresses that follow the network: a VPN interface that comes up late, reconnects or changes address.

`web_bind` entries are either explicit IP addresses or `tunnel:<CIDR>`, meaning "this machine's current addresses in
that range, on point-to-point (tunnel) interfaces only". Wildcards stay forbidden. The client allowlist is never
derived from the network: a peer whose address changes stays rejected until the configuration says otherwise.

The watcher never rebinds in place: when the set of addresses that should be served differs from the set being
served, the process exits with RESTART so the service manager (launchd, systemd) starts it again with fresh sockets.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
from collections.abc import Callable, Iterable

RESTART = 75  # EX_TEMPFAIL: launchd (KeepAlive on failure) or systemd (Restart=on-failure) starts the dashboard again
TUNNEL = "tunnel:"
WILDCARDS = ("0.0.0.0", "::", "", "*")


def check_bind(entries: Iterable[str]) -> list[str]:
    """Refuse wildcard addresses and malformed ranges: the dashboard listens only on explicitly chosen interfaces."""
    out = []
    for e in entries:
        if e in WILDCARDS:
            raise ValueError(f"indirizzo di ascolto non ammesso: {e!r} (esporrebbe la dashboard su ogni rete)")
        if e.startswith(TUNNEL):
            net = ipaddress.ip_network(e.removeprefix(TUNNEL), strict=True)
            if net.prefixlen < 8:
                raise ValueError(f"intervallo troppo ampio: {e!r}")
        else:
            ipaddress.ip_address(e)
        out.append(e)
    return out


IP_PATHS = ("/usr/sbin/ip", "/sbin/ip", "/usr/bin/ip", "/bin/ip")
_LINUX_HEAD = re.compile(r"^\d+:\s+([^\s:]+):\s+<([^>]*)>", re.M)


def parse_ip_addr(text: str) -> list[tuple[str, bool, str]]:
    """Rows from Linux `ip -4 addr show`: interface name without `@suffix`, POINTOPOINT flag, IPv4 address."""
    rows, name, p2p = [], "", False
    for line in text.splitlines():
        head = re.match(r"^\d+:\s+([^\s:]+):\s+<([^>]*)>", line)
        if head:
            name, p2p = head.group(1).split("@")[0], "POINTOPOINT" in head.group(2).split(",")
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if m and name:
            rows.append((name, p2p, m.group(1)))
    return rows


def _read_interfaces() -> str:
    if sys.platform.startswith("linux"):
        for exe in IP_PATHS:  # absolute path: services run with a minimal PATH
            try:
                return subprocess.run([exe, "-4", "addr", "show"], capture_output=True, text=True, check=False,
                                      timeout=10).stdout
            except (OSError, subprocess.TimeoutExpired):
                continue
        return ""
    try:  # absolute path: launchd agents run with a minimal PATH without /sbin
        return subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, check=False, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""  # unknown: serve loopback, the watcher retries


def interfaces(ifconfig_text: str | None = None) -> list[tuple[str, bool, str]]:
    """(interface, is point-to-point, IPv4 address) for every configured IPv4 address of this machine. Accepts macOS
    `ifconfig` or Linux `ip -4 addr show` text (format detected); without text, reads the running system."""
    if ifconfig_text is None:
        ifconfig_text = _read_interfaces()
    if _LINUX_HEAD.search(ifconfig_text):
        return parse_ip_addr(ifconfig_text)
    rows, name, p2p = [], "", False
    for line in ifconfig_text.splitlines():
        head = re.match(r"^(\S+): flags=\w+<([^>]*)>", line)
        if head:
            name, p2p = head.group(1), "POINTOPOINT" in head.group(2).split(",")
            continue
        m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)", line)
        if m and name:
            rows.append((name, p2p, m.group(1)))
    return rows


def resolve(entries: Iterable[str], ifaces: list[tuple[str, bool, str]] | None = None) -> list[str]:
    """Concrete addresses for the configured entries, in order and without duplicates. Explicit addresses are always
    returned (binding tells whether they are present); `tunnel:` ranges only match tunnel interfaces."""
    ifaces = interfaces() if ifaces is None else ifaces
    out: list[str] = []
    for e in check_bind(entries):
        if e.startswith(TUNNEL):
            net = ipaddress.ip_network(e.removeprefix(TUNNEL))
            found = sorted(a for _, p2p, a in ifaces if p2p and ipaddress.ip_address(a) in net)
        else:
            found = [e]
        out += [a for a in found if a not in out]
    return out


def bind_sockets(addresses: list[str], port: int) -> tuple[list, list[str]]:
    """Listening sockets for concrete addresses. Addresses not present right now (e.g. a VPN interface that is down)
    are returned as missing instead of failing: loopback keeps working."""
    sockets, missing = [], []
    for addr in check_bind(addresses):
        fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
        sock = socket.socket(fam, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((addr, port))
        except OSError:
            sock.close()
            missing.append(addr)
            continue
        sockets.append(sock)
    return sockets, missing


def present(addresses: list[str]) -> set[str]:
    """Addresses that exist on this machine right now (a bind on an ephemeral port succeeds)."""
    socks, _ = bind_sockets(addresses, 0)
    found = {s.getsockname()[0] for s in socks}
    for s in socks:
        s.close()
    return found


def decide(served: set[str], wanted_now: set[str]) -> tuple[set[str], bool]:
    """One watcher step. `served`: addresses whose socket is still trustworthy. An address that disappears is dropped
    (its socket may be stale after the interface comes back); any wanted address not served means restart."""
    still = served & wanted_now
    return still, bool(wanted_now - still)


def watch(entries: list[str], served: set[str], probe: Callable[[list[str]], set[str]] = present,
          sleep: Callable[[float], None] | None = None, interval: float = 15.0,
          on_restart: Callable[[str], None] | None = None) -> None:
    """Runs until a restart is needed, then calls on_restart (default: exit RESTART). Loopback is always served."""
    import os
    import time

    sleep = sleep or time.sleep
    while True:
        sleep(interval)
        wanted = probe(resolve(entries))
        served, restart = decide(served, wanted)
        if restart:
            msg = f"indirizzi da servire cambiati ({', '.join(sorted(wanted - served))}): riavvio"
            if on_restart:
                on_restart(msg)
                return
            print(msg, flush=True)
            os._exit(RESTART)
