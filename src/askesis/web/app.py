"""Local dashboard (F4a): a thin web interface around the same core as the CLI.

No computation lives here: logging goes through askesis.services, numbers come from the analytics engine, reports
are the validated files in reports/. Access: localhost only, one password, session cookie, CSRF token on every form,
strict Content-Security-Policy (no external resources).
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from askesis import config as config_mod
from askesis import services
from askesis.analytics import body, engine
from askesis.core.timeutil import now_utc
from askesis.interventions import present
from askesis.interventions import registry as reg
from askesis.parsers.text import ParseError
from askesis.plan import rules as plan_rules
from askesis.plan import store as plan_store
from askesis.safety import rules as safety
from askesis.store.db import connect

from . import auth, md

HERE = Path(__file__).parent
REPORT_NAME = re.compile(r"^(review|retro|daily|proposta)[\w.-]*\.md$")
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
NAV = [("/", "Oggi"), ("/andamenti", "Andamenti"), ("/review", "Review"), ("/piano", "Piano"),
       ("/interventi", "Interventi"), ("/safety", "Safety")]


def check_bind(addresses: list[str]) -> list[str]:
    """Refuse wildcard addresses: the dashboard listens only on explicitly chosen interfaces."""
    for a in addresses:
        if a in ("0.0.0.0", "::", "", "*"):
            raise ValueError(f"indirizzo di ascolto non ammesso: {a!r} (esporrebbe la dashboard su ogni rete)")
    return addresses


def bind_sockets(addresses: list[str], port: int) -> tuple[list, list[str]]:
    """Listening sockets for the chosen addresses. Addresses not present right now (e.g. a VPN interface that is
    down) are returned as missing instead of failing: loopback keeps working."""
    import socket

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


def exercise_name(key: str) -> str:
    from askesis.reference import catalog

    e = next((x for x in catalog()["exercises"] if x["id"] == key), None)
    return (e.get("name_it") or e["name"]) if e else key.removeprefix("raw:")


def paths(cfg: config_mod.Config) -> tuple[Path, Path]:
    data = cfg.db_path.parent
    return data / "web_auth.json", data / "web_session.key"


def create_app(cfg: config_mod.Config | None = None, allowed_hosts: list[str] | None = None) -> Starlette:
    cfg = cfg or config_mod.load()
    auth_file, secret_file = paths(cfg)
    throttle = auth.Throttle()
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))

    def today() -> date:
        return now_utc().astimezone(ZoneInfo(cfg.timezone)).date()

    def conn():
        return connect(cfg.db_path)

    def csrf(request: Request) -> str:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        return request.session["csrf"]

    def page(request: Request, template: str, **ctx) -> HTMLResponse:
        c = conn()
        ctx |= {"nav": NAV, "path": request.url.path, "csrf": csrf(request),
                "open_flags": len(safety.open_flags(c)), "user": request.session.get("user")}
        return HTMLResponse(env.get_template(template).render(**ctx))

    async def form_checked(request: Request) -> dict | None:
        form = dict(await request.form())
        ok = hmac.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "!"))
        return form if ok else None

    def guard(handler):
        async def wrapped(request: Request) -> Response:
            if not request.session.get("user"):
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"error": "non autenticato"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
            return await handler(request)
        return wrapped

    # ------------------------------------------------------------------ login
    async def login(request: Request) -> Response:
        if not auth_file.exists():
            return page(request, "login.html", error="Password non impostata. Nel terminale: bin/ak web set-password")
        if request.method == "GET":
            return page(request, "login.html", error=None)
        form = await form_checked(request)
        if form is None:
            return page(request, "login.html", error="Sessione scaduta: ricarica la pagina e riprova.")
        wait = throttle.blocked_for()
        if wait > 0:
            return page(request, "login.html", error=f"Troppi tentativi: riprova tra {wait:.0f} secondi.")
        if not auth.check_password(auth_file, str(form.get("password", ""))):
            throttle.fail()
            return page(request, "login.html", error="Password non corretta.")
        throttle.success()
        request.session.clear()
        request.session["user"] = "athlete"
        return RedirectResponse("/", status_code=303)

    async def logout(request: Request) -> Response:
        if request.method == "POST" and await form_checked(request) is not None:
            request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------------ today
    def status_today(c, day: date) -> dict:
        def has(entity: str, d: date) -> bool:
            return bool(c.execute("SELECT 1 FROM v_current WHERE entity_type = ? AND local_date = ? LIMIT 1",
                                  (entity, d.isoformat())).fetchone())
        return {"day": day, "weight": has("body_weight", day), "food": has("nutrition_day", day - timedelta(days=1)),
                "flags": [dict(f) | {"actions": json.loads(f["actions"])} for f in safety.open_flags(c)]}

    def compose_line(f: dict) -> str:
        """Ready-made fields → the same dictation line the CLI parses (one path for every interface)."""
        parts = []
        if f.get("peso"):
            parts.append(f"p {f['peso']}")
        if f.get("kcal"):
            food = f"cibo {f['kcal']}" + (f" {f['proteine']}" if f.get("proteine") else "")
            food += " parz" if f.get("parziale") else ""
            if f.get("libero_kcal"):
                food += f" +{f['libero_kcal']}" + (f"/{f['libero_prot']}" if f.get("libero_prot") else "")
            parts.append(food)
        readings = " ".join(x for x in (f.get("vita1"), f.get("vita2"), f.get("vita3")) if x)
        if readings:
            parts.append(f"vita {readings}")
        for key in ("sonno", "passi", "fcr"):
            if f.get(key):
                parts.append(f"{key} {f[key]}")
        if f.get("pesi"):
            parts.append("pesi: " + " ".join(str(f["pesi"]).split()))
        if f.get("corsa"):
            parts.append(f"corsa {f['corsa']}")
        return " · ".join(parts)

    async def oggi(request: Request) -> Response:
        c = conn()
        ctx: dict = {"status": status_today(c, today()), "line": "", "day_value": today().isoformat(),
                     "preview": None, "saved": None, "error": None}
        if request.method == "POST":
            form = await form_checked(request)
            if form is None:
                ctx["error"] = "Sessione scaduta: ricarica la pagina e ripeti l'inserimento."
                return page(request, "oggi.html", **ctx)
            line = str(form.get("line") or "").strip() or compose_line(form)
            ctx["line"] = line
            try:
                day = date.fromisoformat(str(form.get("day") or today().isoformat()))
                ctx["day_value"] = day.isoformat()
                if not line:
                    raise ParseError("scrivi una riga oppure compila almeno un campo")
                records = services.build_day(line, cfg, day)
            except (ParseError, ValueError) as exc:
                ctx["error"] = f"Riga non valida: {exc}"
                return page(request, "oggi.html", **ctx)
            pv = services.preview(records)
            confirmed = form.get("confirmed") == "1"
            if form.get("action") == "save" and (confirmed or not pv.needs_confirmation):
                saved = services.save(c, records, "web_day")
                ctx |= {"saved": saved, "line": "", "status": status_today(c, today())}
            else:
                ctx["preview"] = pv
        return page(request, "oggi.html", **ctx)

    # ------------------------------------------------------------------ trends
    async def andamenti(request: Request) -> Response:
        return page(request, "andamenti.html")

    async def api_series(request: Request) -> Response:
        c = conn()
        inp = engine.load_inputs(c)
        if not inp.dates:
            return JSONResponse({"empty": True})
        start, end = min(inp.dates), max(inp.dates)  # series stop at the last recorded data
        values = engine.compute(inp, start, end)
        daily = body.daily_weights([x for x in inp.weighins if x[0] <= end])
        ema = body.weight_ema_series(daily)
        noise = body.weight_noise_sd(daily, max(daily)) if daily else None  # up to the last weigh-in

        def epoch(d: date) -> int:
            return int((d - date(1970, 1, 1)).total_seconds())

        def weekly(metric: str, subject: str = "global") -> dict:
            ms = sorted((m for m in values if m.metric_id == metric and m.subject == subject),
                        key=lambda m: m.period_end)
            return {"t": [epoch(m.period_end) for m in ms], "v": [m.value for m in ms],
                    "lo": [m.lo for m in ms], "hi": [m.hi for m in ms], "ref": f"{metric}@1"}

        days = sorted(daily)
        z = 1.96
        out = {
            "weight": {"t": [epoch(d) for d in days], "daily": [daily[d] for d in days], "ema": [ema[d] for d in days],
                       "band_lo": [ema[d] - z * noise[0] for d in days] if noise else None,
                       "band_hi": [ema[d] + z * noise[0] for d in days] if noise else None,
                       "noise_sd": noise[0] if noise else None, "noise_n": noise[1] if noise else 0,
                       "refs": ["weight_daily@1", "weight_ema@1"]},
            "tdee": weekly("adaptive_tdee"), "intake": weekly("intake_mean_7d"),
            "intake_vs_target": weekly("intake_vs_target_week"), "steps": weekly("steps_mean_week"),
            "sleep": weekly("sleep_mean_week"), "run_km": weekly("run_volume_km"),
            "e1rm": {exercise_name(m.subject.split(":", 1)[1]): weekly("e1rm_best_week", m.subject)
                     for m in values if m.metric_id == "e1rm_best_week"},
            "volume": {present.MUSCLES_IT.get(m.subject.split(":", 1)[1], m.subject.split(":", 1)[1]): m.value
                       for m in values if m.metric_id == "volume_per_muscle_week" and m.period_end == max(
                           x.period_end for x in values if x.metric_id == "volume_per_muscle_week")},
            "window_start": epoch(max(inp.dates) - timedelta(days=120)),  # last 120 days of data
        }
        return JSONResponse(out)

    # ------------------------------------------------------------------ reports, plan, interventions, safety
    async def review(request: Request) -> Response:
        rdir = config_mod.ROOT / "reports"
        files = sorted((p.name for p in rdir.glob("*.md") if REPORT_NAME.match(p.name)), reverse=True) \
            if rdir.exists() else []
        name = request.query_params.get("f") or next((f for f in files if f.startswith("review-")), None)
        body_html = None
        if name and name in files:  # only listed files: no path traversal
            body_html = md.render((rdir / name).read_text())
        return page(request, "review.html", files=files, name=name, body=body_html)

    async def piano(request: Request) -> Response:
        c = conn()
        day = today()
        rows = {k: plan_store.active(c, k, day) for k in ("phase", "nutrition_target", "programme")}
        content = {k: plan_store.content(r) if r else None for k, r in rows.items()}
        week = present._session_lines(content["programme"]["microcycle"], minutes=True) if content["programme"] else []
        nxt = plan_rules.next_session(c, day, record=False)
        return page(request, "piano.html", content=content, rows=rows, week=week, nxt=nxt, day=day)

    async def interventi(request: Request) -> Response:
        c = conn()
        items = [dict(r) for r in c.execute(
            "SELECT i.number, i.title, s.status FROM intervention i JOIN v_intervention_status s ON s.id = i.id "
            "ORDER BY i.number DESC")]
        n = request.query_params.get("n")
        why = md.render(reg.render_why(reg.why(c, int(n)))) if n and n.isdigit() and reg.get(c, int(n)) else None
        return page(request, "interventi.html", items=items, why=why, n=n)

    async def safety_page(request: Request) -> Response:
        c = conn()
        flags = [dict(f) | {"actions": json.loads(f["actions"])} for f in safety.open_flags(c)]
        return page(request, "safety.html", flags=flags)

    routes = [
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/", guard(oggi), methods=["GET", "POST"]),
        Route("/andamenti", guard(andamenti)),
        Route("/api/series", guard(api_series)),
        Route("/review", guard(review)),
        Route("/piano", guard(piano)),
        Route("/interventi", guard(interventi)),
        Route("/safety", guard(safety_page)),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
    ]
    app = Starlette(routes=routes, middleware=[
        Middleware(SecurityHeaders),
        Middleware(ClientAllowlist, allowed=cfg.web_allowed_clients),
        Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or cfg.web_allowed_hosts),
        Middleware(SessionMiddleware, secret_key=auth.session_secret(secret_file), session_cookie="askesis_session",
                   max_age=12 * 3600, same_site="strict", https_only=cfg.web_secure_cookies),
    ])
    return app


LOOPBACK = {"127.0.0.1", "::1"}


class ClientAllowlist(BaseHTTPMiddleware):
    """Only this Mac (loopback) and the explicitly allowed client addresses may connect."""

    def __init__(self, app, allowed: list[str]) -> None:
        super().__init__(app)
        self.allowed = LOOPBACK | set(allowed)

    async def dispatch(self, request: Request, call_next) -> Response:
        host = request.client.host if request.client else ""
        if host not in self.allowed and host != "testclient":
            return Response("Dispositivo non autorizzato.", status_code=403)
        return await call_next(request)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
