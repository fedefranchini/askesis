"""Local dashboard (F4a): a thin web interface around the same core as the CLI.

No computation lives here: logging goes through askesis.services, numbers come from the analytics engine, reports
are the validated files in reports/. Access: localhost only, one password, session cookie, CSRF token on every form,
strict Content-Security-Policy (no external resources).
"""

from __future__ import annotations

import hmac
import json
import secrets
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote
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

from askesis import coach as coach_mod
from askesis import config as config_mod
from askesis import services
from askesis.analytics import body, engine
from askesis.analytics import today as today_mod
from askesis.analytics.params import p
from askesis.core.timeutil import now_utc
from askesis.interventions import present
from askesis.interventions import registry as reg
from askesis.parsers.text import ParseError
from askesis.plan import rules as plan_rules
from askesis.plan import store as plan_store
from askesis.safety import rules as safety
from askesis.store.db import connect

from . import auth, md
from .netwatch import bind_sockets, check_bind  # noqa: F401  (public: used by the CLI and tests)

HERE = Path(__file__).parent
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
NAV = [("/", "Oggi"), ("/andamenti", "Andamenti"), ("/coach", "Coach"), ("/piano", "Piano"),
       ("/interventi", "Interventi"), ("/safety", "Safety")]


def asset_version() -> str:
    """Short hash of the dashboard's own static files: a new version busts the browser cache."""
    import hashlib

    h = hashlib.sha256()
    for f in sorted((HERE / "static").glob("*.*")):
        h.update(f.read_bytes())
    return h.hexdigest()[:10]


def seconds(wait: float) -> str:
    n = max(1, int(wait + 0.999))
    if n < 60:
        return f"{n} second{'o' if n == 1 else 'i'}"
    m = (n + 59) // 60
    return f"{m} minut{'o' if m == 1 else 'i'}"


def fmt_date(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y")


def fmt_num(v, nd: int = 0) -> str:
    """Italian number format: comma for decimals, narrow no-break space for thousands; '—' when missing."""
    if v is None:
        return "—"
    s = f"{float(v):,.{nd}f}".replace(",", "\u202f").replace(".", ",")
    return s


def sparkline(c, day: date, days: int = 28, w: float = 300, h: float = 96) -> dict | None:
    """Geometry (SVG viewBox units) of the trend weight and its noise band over the last `days` days up to `day`.
    Presentation only: values come from weight_ema@1 and body.weight_noise_sd."""
    inp = engine.load_inputs(c)
    daily = {d: v for d, v in body.daily_weights(inp.weighins).items() if d <= day}
    if len(daily) < 2:
        return None
    ema = body.weight_ema_series(daily)
    last = max(daily)
    noise = body.weight_noise_sd(daily, last)
    half = 1.96 * noise[0] if noise else 0.0
    ds = sorted(d for d in ema if d > last - timedelta(days=days))
    lo = min(min(ema[d] - half for d in ds), min(daily[d] for d in ds if d in daily))
    hi = max(max(ema[d] + half for d in ds), max(daily[d] for d in ds if d in daily))
    pad = (hi - lo) * 0.12 or 0.5
    lo, hi = lo - pad, hi + pad
    x0 = ds[0]
    span = max((ds[-1] - x0).days, 1)

    def x(d: date) -> float:
        return round(4 + (d - x0).days / span * (w - 8), 1)

    def y(v: float) -> float:
        return round(h - (v - lo) / (hi - lo) * h, 1)

    trend = " ".join(f"{'M' if i == 0 else 'L'}{x(d)},{y(ema[d])}" for i, d in enumerate(ds))
    band = None
    if half:
        top = [f"{x(d)},{y(ema[d] + half)}" for d in ds]
        bottom = [f"{x(d)},{y(ema[d] - half)}" for d in reversed(ds)]
        band = "M" + " L".join(top + bottom) + " Z"
    dots = [(x(d), y(daily[d])) for d in ds if d in daily]
    return {"w": w, "h": h, "trend": trend, "band": band, "dots": dots, "last": (x(ds[-1]), y(ema[ds[-1]])),
            "days": days, "half": half}


def exercise_name(key: str) -> str:
    from askesis.reference import catalog

    e = next((x for x in catalog()["exercises"] if x["id"] == key), None)
    return (e.get("name_it") or e["name"]) if e else key.removeprefix("raw:")


def paths(cfg: config_mod.Config) -> tuple[Path, Path]:
    data = cfg.db_path.parent
    return data / "web_auth.json", data / "web_session.key", data / "web_devices.json"


def create_app(cfg: config_mod.Config | None = None, allowed_hosts: list[str] | None = None) -> Starlette:
    cfg = cfg or config_mod.load()
    auth_file, secret_file, devices_file = paths(cfg)
    if not 1 <= cfg.web_remember_days <= 90:
        raise ValueError("web_remember_days deve essere tra 1 e 90")
    throttle = auth.Throttle()
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    env.filters["num"] = fmt_num
    env.filters["date"] = fmt_date
    env.globals["md"] = md.render
    asset_v = asset_version()

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
        user = request.session.get("user")
        ctx |= {"nav": NAV, "asset_v": asset_v, "path": request.url.path, "csrf": csrf(request),
                "open_flags": len(safety.open_flags(c)), "user": user,
                "pending_count": len(coach_mod.pending(c)) if user else 0}
        return HTMLResponse(env.get_template(template).render(**ctx))

    async def form_checked(request: Request) -> dict | None:
        form = dict(await request.form())
        ok = hmac.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "!"))
        return form if ok else None

    def session_valid(request: Request) -> bool:
        """A session counts only if made with the current password and, if it came from a remembered device,
        while that device is still remembered (revocation and password change end it at the next request)."""
        sess = request.session
        if not sess.get("user") or sess.get("epoch") != auth.credential_epoch(auth_file):
            return False
        return not sess.get("device") or auth.device_alive(devices_file, sess["device"])

    def start_session(request: Request, device: str | None = None) -> None:
        request.session.clear()
        request.session.update({"user": "athlete", "epoch": auth.credential_epoch(auth_file)})
        if device:
            request.session["device"] = device

    def guard(handler):
        async def wrapped(request: Request) -> Response:
            if not session_valid(request):
                dev = auth.device_for(devices_file, request.cookies.get(DEVICE_COOKIE), touch=True)
                if dev is None:
                    request.session.clear()
                    if request.url.path.startswith("/api/"):
                        return JSONResponse({"error": "non autenticato"}, status_code=401)
                    return RedirectResponse("/login", status_code=303)
                start_session(request, dev["id"])  # remembered device: no password, every other check unchanged
            return await handler(request)
        return wrapped

    # ------------------------------------------------------------------ login
    def login_page(request: Request, error: str | None = None, wait: float = 0.0, status: int = 200) -> HTMLResponse:
        r = page(request, "login.html", error=error, wait=int(wait + 0.999),
                 wait_text=seconds(wait) if wait > 0 else "", remember_days=cfg.web_remember_days)
        r.status_code = status
        return r

    async def login(request: Request) -> Response:
        if not auth_file.exists():
            return login_page(request, "Password non impostata. Nel terminale: bin/ak web set-password")
        if request.method == "GET":
            if session_valid(request):
                return RedirectResponse("/", status_code=303)
            return login_page(request, wait=throttle.blocked_for())
        form = await form_checked(request)
        if form is None:
            return login_page(request, "Sessione scaduta: ricarica la pagina e riprova.")
        wait = throttle.blocked_for()
        if wait > 0:
            return login_page(request, "Troppi tentativi con la password sbagliata.", wait, 429)
        if not auth.check_password(auth_file, str(form.get("password", ""))):
            throttle.fail()
            wait = throttle.blocked_for()
            return login_page(request, "Password non corretta.", wait, 401)
        throttle.success()
        device = None
        cookie = None
        if form.get("remember") == "1":
            cookie, rec = auth.remember(devices_file, auth.device_label(request.headers.get("user-agent", "")),
                                        cfg.web_remember_days)
            device = rec["id"]
        start_session(request, device)
        resp = RedirectResponse("/", status_code=303)
        if cookie:
            resp.set_cookie(DEVICE_COOKIE, cookie, max_age=cfg.web_remember_days * 86400, httponly=True,
                            samesite="strict", secure=cfg.web_secure_cookies, path="/")
        return resp

    async def logout(request: Request) -> Response:
        """Logging out also forgets this device (a remembered device would otherwise log straight back in)."""
        resp = RedirectResponse("/login", status_code=303)
        if request.method == "POST" and await form_checked(request) is not None:
            dev = auth.device_for(devices_file, request.cookies.get(DEVICE_COOKIE))
            if dev:
                auth.revoke(devices_file, dev["id"])
            request.session.clear()
            resp.delete_cookie(DEVICE_COOKIE, path="/")
        return resp

    async def accesso(request: Request) -> Response:
        current = request.session.get("device")
        if request.method == "POST":
            form = await form_checked(request)
            if form is not None:
                target = str(form.get("revoke", ""))
                auth.revoke(devices_file, None if target == "*" else target)
                if target in ("*", current):
                    resp = RedirectResponse("/login", status_code=303)
                    resp.delete_cookie(DEVICE_COOKIE, path="/")
                    request.session.clear()
                    return resp
            return RedirectResponse("/accesso", status_code=303)
        items = auth.devices(devices_file)
        return page(request, "accesso.html", devices=items, current=current, remember_days=cfg.web_remember_days)

    # ------------------------------------------------------------------ today
    def status_today(c, day: date) -> dict:
        def has(entity: str, d: date) -> bool:
            return bool(c.execute("SELECT 1 FROM v_current WHERE entity_type = ? AND local_date = ? LIMIT 1",
                                  (entity, d.isoformat())).fetchone())
        return {"day": day, "weight": has("body_weight", day), "food": has("nutrition_day", day - timedelta(days=1)),
                "flags": [dict(f) | {"actions": json.loads(f["actions"])} for f in safety.open_flags(c)],
                "summary": today_mod.summary(c, day), "spark": sparkline(c, day)}

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

        def by_metric(metric: str) -> list:
            return sorted((m for m in values if m.metric_id == metric), key=lambda m: m.period_end)

        def vs_target(metric: str) -> dict:
            ms = [m for m in by_metric(metric) if m.value is not None]
            return {"t": [epoch(m.period_end) for m in ms], "actual": [m.detail.get("actual_mean") for m in ms],
                    "target": [m.detail.get("target_mean") for m in ms], "ref": f"{metric}@1"}

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
            "last": epoch(max(inp.dates)),
            "energy_vs_target": vs_target("intake_vs_target_week"),
            "protein_vs_target": vs_target("protein_vs_target_week"),
            "sessions": {"t": [epoch(m.period_end) for m in by_metric("sessions_vs_plan_week")],
                         "done": [m.value for m in by_metric("sessions_vs_plan_week")],
                         "planned": [m.detail.get("planned") for m in by_metric("sessions_vs_plan_week")],
                         "ref": "sessions_vs_plan_week@1"},
            "sleep_threshold": {"value": p("sleep_recommended_min_h"), "ref": "sleep_recommended_min_h"},
            "volume_ref": {"range": p("volume_reference_sets_week"), "ref": "volume_reference_sets_week",
                           "claim": "rt.volume_range_12_20"},
        }
        top = sorted(out["e1rm"], key=lambda k: -sum(v is not None for v in out["e1rm"][k]["v"]))[:3]
        out["e1rm"] = {k: out["e1rm"][k] for k in top}  # at most three lifts: the most tracked ones
        return JSONResponse(out)

    # ------------------------------------------------------------------ reports, plan, interventions, safety
    async def review(request: Request) -> Response:  # old address: the texts now live in Coach
        f = request.query_params.get("f")
        return RedirectResponse("/coach" + (f"?f={quote(f)}" if f else ""), status_code=303)

    def coach_page(request: Request, **ctx) -> HTMLResponse:
        c = conn()
        docs = coach_mod.documents(config_mod.reports_dir(), c)
        by_name = {d.name: d for d in docs}
        name = request.query_params.get("f")
        current = by_name.get(name) if name else None  # only listed files: no path traversal
        latest = []
        for kind in ("daily", "review", "retro"):
            latest += [d for d in docs if d.kind == kind][:2 if kind == "review" else 1]
        if current is None and not ctx.get("confirm"):
            current = next((d for d in docs if d.kind == "review" and "commento" not in d.name),
                           docs[0] if docs else None)
        return page(request, "coach.html", pending=coach_mod.pending(c), docs=docs, latest=latest, current=current,
                    current_html=md.render(current.body) if current else None, **ctx)

    async def coach_view(request: Request) -> Response:
        return coach_page(request)

    async def coach_decision(request: Request) -> Response:
        """Step 1: the typed answer is checked and a confirmation is asked (nothing is recorded yet)."""
        form = await form_checked(request)
        if form is None:
            return coach_page(request, error="Sessione scaduta: ricarica la pagina e ripeti.")
        kind, ref = str(form.get("kind", "")), str(form.get("ref", ""))
        text, reason = str(form.get("text", "")).strip()[:500], str(form.get("reason", "")).strip()[:1000]
        item = next((x for x in coach_mod.pending(conn()) if x.kind == kind and x.ref == ref), None)
        if item is None:
            return coach_page(request, error="Questa proposta non è più in attesa.")
        try:
            action = coach_mod.intent(text)
        except coach_mod.DecisionError as exc:
            return coach_page(request, error=f"{item.title}: {exc}.", draft={"key": f"{kind}:{ref}", "text": text,
                                                                           "reason": reason})
        if action == "approve" and not item.validated:
            return coach_page(request, error="Questa proposta è da verificare: si può solo rifiutare.")
        nonce = secrets.token_urlsafe(16)
        request.session["decision"] = {"nonce": nonce, "kind": kind, "ref": ref, "text": text, "reason": reason,
                                       "exp": now_utc().timestamp() + 600}
        return coach_page(request, confirm={"nonce": nonce, "item": item, "action": action, "text": text,
                                            "reason": reason})

    async def coach_confirm(request: Request) -> Response:
        """Step 2: only the confirmation issued for this exact answer, within 10 minutes, records the decision."""
        form = await form_checked(request)
        pending_d = request.session.pop("decision", None)
        if form is None or not pending_d or not hmac.compare_digest(str(form.get("nonce", "")), pending_d["nonce"]) \
                or pending_d["exp"] < now_utc().timestamp():
            return coach_page(request, error="Conferma scaduta o non valida: nulla è stato registrato. Ripeti.")
        try:
            msg = coach_mod.decide(conn(), pending_d["kind"], pending_d["ref"], pending_d["text"], pending_d["reason"],
                                   today=today())
        except (coach_mod.DecisionError, ValueError) as exc:
            return coach_page(request, error=f"Nulla è stato registrato: {exc}.")
        return coach_page(request, done=msg)

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
        Route("/coach", guard(coach_view)),
        Route("/coach/decisione", guard(coach_decision), methods=["POST"]),
        Route("/coach/conferma", guard(coach_confirm), methods=["POST"]),
        Route("/piano", guard(piano)),
        Route("/interventi", guard(interventi)),
        Route("/safety", guard(safety_page)),
        Route("/accesso", guard(accesso), methods=["GET", "POST"]),
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
DEVICE_COOKIE = "askesis_device"
REJECTED = "client rifiutato:"


class ClientAllowlist(BaseHTTPMiddleware):
    """Only this Mac (loopback) and the explicitly allowed client addresses may connect."""

    def __init__(self, app, allowed: list[str]) -> None:
        super().__init__(app)
        self.allowed = LOOPBACK | set(allowed)
        self.logged: dict[str, float] = {}

    def _log(self, host: str) -> None:
        """One line per address per hour in the server log: `web check` reports it (e.g. a peer whose address
        changed). The address is never added automatically."""
        import sys
        import time

        now = time.time()
        if now - self.logged.get(host, 0) >= 3600:
            self.logged[host] = now
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            print(f"{stamp} {REJECTED} {host}", file=sys.stderr, flush=True)

    async def dispatch(self, request: Request, call_next) -> Response:
        host = request.client.host if request.client else ""
        if host not in self.allowed and host != "testclient":
            self._log(host)
            return Response("Dispositivo non autorizzato.", status_code=403)
        return await call_next(request)


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Static files are versioned (?v=content hash) and contain no data: cacheable. Everything else: never stored.
        static = request.url.path.startswith("/static/")
        response.headers["Cache-Control"] = "public, max-age=604800" if static else "no-store"
        return response
