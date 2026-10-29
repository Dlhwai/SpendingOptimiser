"""Loopback ledger, explicit Gmail loading and safe-snapshot-only remote analysis."""

import argparse
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
from copy import copy
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ai_inputs import digest
from .ai_service import AIService
from .cards import product_options
from .codex_client import MESSAGES as AI_MESSAGES
from .codex_client import AIProblem
from .common import EvidenceRef
from .document import walk
from .dotenv_store import DotenvError, DotenvFile, default_env_path
from .gmail_auth import MESSAGES, EnvStore, GmailProblem, configure_google
from .gmail_service import GmailService
from .ledger import CORRECTION_FIELDS, Conflict, Ledger, heads, needs_review, uncertain
from .monthly import (
    SINGAPORE,
    foreign_events,
    format_money,
    grouping,
    latest_end,
    range_report,
    selected_range,
    shortcuts,
)
from .public_terms import ISSUERS
from .rewards import card_rewards
from .storage import Storage

MAX_BODY = 2 * 1024 * 1024
CODEX_CLIENT_NAME = "SPENDING_CODEX_CLIENT"
DEFAULT_DB = Path.home() / ".local" / "share" / "spending-optimiser" / "ledger.sqlite3"


class LocalBoundary:
    """Bound request bodies, block cross-origin mutations, add defensive headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        if scope["method"] not in {"GET", "HEAD"}:
            origin = headers.get(b"origin")
            expected = scope["scheme"].encode() + b"://" + headers.get(b"host", b"")
            if (origin and origin != expected) or headers.get(b"sec-fetch-site") == b"cross-site":
                return await PlainTextResponse("Cross-origin request rejected", 403)(
                    scope, receive, send
                )
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > MAX_BODY:
                    return await PlainTextResponse("Request exceeds 2 MiB limit", 413)(
                        scope, receive, send
                    )
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)

            async def buffered():
                return {"type": "http.request", "body": body, "more_body": False}

            receive = buffered

        async def secured(message):
            if message["type"] == "http.response.start":
                # Callback redirects must not leak codes through a Referer. Ordinary
                # forms retain same-origin (no-referrer gives Chrome Origin:null).
                if not any(key.lower() == b"referrer-policy" for key, _ in message["headers"]):
                    message["headers"].append((b"referrer-policy", b"same-origin"))
                message["headers"] += [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (
                        b"content-security-policy",
                        b"default-src 'none'; script-src 'self'; connect-src 'self'; "
                        b"style-src 'unsafe-inline'; form-action 'self'; "
                        b"frame-ancestors 'none'; base-uri 'none'",
                    ),
                    # Provider pages in the sign-in window cannot keep a handle to this app.
                    (b"cross-origin-opener-policy", b"same-origin"),
                ]
            await send(message)

        await self.app(scope, receive, secured)


def create_app(
    database: str | Path | None = None,
    *,
    gmail_service=None,
    ai_service=None,
    codex_client=None,
    env_file: str | Path | None = None,
) -> FastAPI:
    ledger = Ledger(database or os.environ.get("SPENDING_OPTIMISER_DB", DEFAULT_DB))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ledger = ledger
    gmail = (
        gmail_service
        if gmail_service is not None
        else GmailService(
            ledger.path, store=None if env_file is None else EnvStore(DotenvFile(env_file))
        )
    )
    app.state.gmail = gmail
    ai = ai_service if ai_service is not None else AIService(ledger, binary=codex_client)
    app.state.ai = ai
    app.add_event_handler("shutdown", ai.close)
    storage = Storage(ledger)
    app.state.storage = storage
    app.state.storage_result = None
    gmail_lock = threading.Lock()
    csrf = secrets.token_urlsafe(32)
    templates = Jinja2Templates(directory=str(files("spending_optimiser").joinpath("templates")))
    templates.env.globals["money"] = format_money
    templates.env.globals["singapore"] = SINGAPORE
    templates.env.globals["shortcuts"] = shortcuts
    templates.env.globals["latest_end"] = latest_end
    scripts = {
        name: files("spending_optimiser").joinpath(f"static/{name}").read_text()
        for name in ("provider-connect.js", "period-picker.js", "refresh.js")
    }
    # Only an opaque session ID goes in this signed, HttpOnly cookie. Lax permits
    # Google's top-level callback GET; existing mutation CSRF cookie stays Strict.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.token_urlsafe(32),
        session_cookie="gmail_session",
        same_site="lax",
        max_age=3600,
    )
    app.add_middleware(LocalBoundary)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    def render(request, name, status_code=200, **context):
        response = templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "csrf": csrf,
                "maintenance_warning": (
                    ledger.maintenance_warning or gmail.cache.ledger.maintenance_warning
                ),
                **context,
            },
            status_code=status_code,
        )
        response.set_cookie("ledger_csrf", csrf, httponly=True, samesite="strict")
        return response

    async def form_data(request, allowed):
        content_type = request.headers.get("content-type", "").split(";")[0]
        if content_type not in {"application/x-www-form-urlencoded", "multipart/form-data"}:
            raise HTTPException(415, "Use the browser form encoding")
        form = await request.form(max_files=1, max_fields=10, max_part_size=MAX_BODY)
        try:
            pairs = list(form.multi_items())
            if len(pairs) != len(form) or set(form) - allowed - {"csrf"}:
                raise HTTPException(422, "Unknown or repeated form fields")
            if any(isinstance(value, UploadFile) and key != "file" for key, value in pairs):
                raise HTTPException(422, "Only the file field may contain an upload")
            token = form.get("csrf")
            cookie = request.cookies.get("ledger_csrf", "")
            if (
                not isinstance(token, str)
                or not hmac.compare_digest(token.encode("utf-8"), csrf.encode())
                or not hmac.compare_digest(cookie.encode("utf-8"), csrf.encode())
            ):
                raise HTTPException(403, "Invalid form token; reload the page")
            return form
        except HTTPException:
            await form.close()
            raise

    def text_field(form, name):
        value = form.get(name, "")
        if not isinstance(value, str):
            raise HTTPException(422, f"{name} must be text")
        return value

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        if isinstance(exc, Conflict):
            code = 409
        else:
            code = 422
        if isinstance(exc, ValidationError):
            message = json.dumps(
                exc.errors(include_input=False, include_context=False, include_url=False), indent=2
            )
        else:
            message = str(exc)
        return render(request, "error.html", status_code=code, message=message)

    @app.exception_handler(HTTPException)
    async def request_error(request, exc):
        response = render(request, "error.html", status_code=exc.status_code, message=exc.detail)
        response.headers.update(exc.headers or {})
        return response

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        return render(
            request,
            "error.html",
            status_code=503,
            message=(
                "Local database unavailable; failed writes are rolled back. "
                "Earlier completed mail pages remain saved. Check disk/access and retry."
            ),
        )

    def gmail_operation(action, *args):
        # Serialize local account switches, refresh and disconnect. Network calls
        # run off the event loop, so the loading page remains readable.
        with gmail_lock:
            try:
                return action(*args)
            except GmailProblem as exc:
                gmail.problem = exc.code
                return None

    @app.get("/static/{name}")
    def script(name: str):
        if name not in scripts:
            raise HTTPException(404, "Not found")
        return Response(scripts[name], media_type="text/javascript")

    def period_fields(form):
        """The period a form names, in the fields the pages link with. Never a silent default."""
        chosen = {
            name: text_field(form, name) for name in ("month", "start", "end") if name in form
        }
        if not chosen:
            raise HTTPException(422, "Choose a calendar month or a date range")
        return selected_range(chosen)

    def display_mode(form):
        # "popup" targets the sign-in window; "window" is the explicit same-window fallback.
        display = text_field(form, "display") or "window"
        if display not in {"popup", "window"}:
            raise HTTPException(422, "Unknown sign-in display")
        return display

    def provider_outcome(request, provider, back, connected, problem, messages):
        """Final state of a sign-in as shown in the sign-in window. No codes, no tokens."""
        if connected and not problem:
            state, message = "connected", f"{provider} connected."
        elif problem in {"cancelled", "consent_denied"}:
            state, message = "cancelled", messages[problem]
        else:
            state, message = "failed", messages.get(problem, "Sign-in did not complete.")
        response = render(
            request,
            "provider_result.html",
            provider=provider,
            state=state,
            message=message,
            back=back,
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def card_view(view):
        """Stated cards and wallets of the connected mailbox, and the product chosen for each."""
        if view.namespace is None:
            return {}, {}
        return view.cache.cards.sources(view.namespace), view.cache.cards.choices(view.namespace)

    @app.get("/gmail", response_class=HTMLResponse)
    def gmail_page(request: Request):
        session = request.session.setdefault("id", secrets.token_urlsafe(32))
        waiting = gmail.status(session)["state"] == "pending"  # may record an expired attempt
        view = copy(gmail)  # one account identity per rendered response, even during switches
        return render(
            request,
            "gmail.html",
            gmail=view,
            messages=MESSAGES,
            refresh=view.refresh_view(),
            waiting=waiting,
        )

    @app.get("/saved")
    def saved_page():
        return RedirectResponse("/cards", status_code=303)

    @app.get("/cards", response_class=HTMLResponse)
    def cards_page(request: Request):
        request.session.setdefault("id", secrets.token_urlsafe(32))
        view = copy(gmail)
        document = ledger.load()
        sources, choices = card_view(view)
        return render(
            request,
            "cards.html",
            gmail=view,
            cards={a: c for a, c in sources.items() if c["kind"] == "card"},
            choices=choices,
            options=product_options(document),
        )

    @app.post("/cards/choose")
    async def cards_choose(request: Request):
        form = await form_data(request, {"account_id", "product_id", "connection_id"})
        account_id, product_id = text_field(form, "account_id"), text_field(form, "product_id")
        connection_id = text_field(form, "connection_id")

        def choose():
            if gmail.connection is None or not secrets.compare_digest(
                connection_id.encode(), gmail.connection_id.encode()
            ):
                raise HTTPException(409, "Account changed; reload before choosing a card")
            gmail.cache.cards.choose(gmail.namespace, account_id, product_id)

        await run_in_threadpool(gmail_operation, choose)
        return RedirectResponse("/cards", status_code=303)

    @app.get("/storage", response_class=HTMLResponse)
    def storage_page(request: Request):
        return render(
            request,
            "storage.html",
            storage=storage.status(),
            result=app.state.storage_result,
            gmail=copy(gmail),
            period=selected_range(request.query_params),
        )

    @app.post("/storage/compress")
    async def storage_compress(request: Request):
        await form_data(request, set())
        app.state.storage_result = await run_in_threadpool(storage.compress_if_needed)
        for owner in (ledger, gmail.cache.ledger):
            owner.maintenance_warning = app.state.storage_result.get("warning")
        return RedirectResponse("/storage", status_code=303)

    @app.post("/storage/restore")
    async def storage_restore(request: Request):
        form = await form_data(request, {"month", "start", "end", "connection_id"})
        period = period_fields(form)
        connection_id = text_field(form, "connection_id")

        def restore():
            if gmail.connection is None or not secrets.compare_digest(
                connection_id.encode(), gmail.connection_id.encode()
            ):
                raise HTTPException(409, "Account changed; reload before restoring saved mail")
            restored = storage.restore_mail(gmail.namespace, period)
            gmail.notice_range = period.key
            gmail.notice = (
                f"Restored {restored} saved mail versions for {period.label}. No mail fetched."
            )

        await run_in_threadpool(gmail_operation, restore)
        return RedirectResponse("/storage?" + urlencode(period.params), status_code=303)

    @app.post("/gmail/connect", response_class=HTMLResponse)
    async def gmail_connect(request: Request):
        form = await form_data(request, {"display"})
        display = display_mode(form)
        if request.url.scheme != "http" or request.url.hostname != "127.0.0.1":
            raise HTTPException(
                400, "Open this application on http://127.0.0.1 at its local port to connect Gmail"
            )
        session = request.session.setdefault("id", secrets.token_urlsafe(32))
        request.session["display"] = display
        redirect = str(request.url.replace(path="/gmail/callback", query=""))
        url = await run_in_threadpool(gmail_operation, gmail.begin, session, redirect)
        if url is None:
            if display == "window":
                return RedirectResponse("/gmail", status_code=303)
            return provider_outcome(request, "Gmail", "/gmail", False, gmail.problem, MESSAGES)
        # Continue with a top-level refresh and link, not an external POST/form redirect, to
        # retain the existing form-action 'self' and Origin protections.
        return render(request, "provider_forward.html", provider="Google", url=url, back="/gmail")

    @app.get("/gmail/callback")
    async def gmail_callback(request: Request):
        pairs = list(request.query_params.multi_items())
        if (
            len(pairs) != len(request.query_params)
            or len(pairs) > 12
            or len(request.url.query) > 8192
        ):
            gmail.problem = "invalid_callback"
        else:
            await run_in_threadpool(
                gmail_operation,
                gmail.callback,
                request.session.get("id", ""),
                dict(pairs),
                str(request.url.replace(query="")),
            )
        # Never render or propagate the authorization code, state or upstream errors.
        if request.session.get("display") == "popup":
            return provider_outcome(
                request, "Gmail", "/gmail", gmail.connection is not None, gmail.problem, MESSAGES
            )
        response = RedirectResponse("/gmail", status_code=303)
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/gmail/status")
    def gmail_status(request: Request):
        return JSONResponse(gmail.status(request.session.get("id", "")))

    @app.post("/gmail/cancel")
    async def gmail_cancel(request: Request):
        await form_data(request, set())
        await run_in_threadpool(gmail_operation, gmail.cancel, request.session.get("id", ""))
        return RedirectResponse("/gmail", status_code=303)

    @app.post("/gmail/restore")
    async def gmail_restore(request: Request):
        await form_data(request, set())
        await run_in_threadpool(gmail_operation, gmail.restore)
        return RedirectResponse("/gmail", status_code=303)

    @app.post("/gmail/disconnect")
    async def gmail_disconnect(request: Request):
        await form_data(request, set())
        await run_in_threadpool(gmail_operation, gmail.disconnect)
        return RedirectResponse("/gmail", status_code=303)

    @app.post("/gmail/refresh")
    async def gmail_refresh(request: Request):
        form = await form_data(request, {"connection_id"})
        await run_in_threadpool(gmail_operation, gmail.refresh, text_field(form, "connection_id"))
        return RedirectResponse("/gmail", status_code=303)

    @app.post("/gmail/month")
    async def gmail_month(request: Request):
        form = await form_data(
            request, {"month", "start", "end", "pages", "connection_id", "action"}
        )
        try:
            pages = int(text_field(form, "pages"))
        except ValueError:
            raise HTTPException(422, "Choose 1-5 pages per action") from None
        period = period_fields(form)
        await run_in_threadpool(
            gmail_operation,
            gmail.load_range,
            period,
            pages,
            text_field(form, "connection_id"),
            text_field(form, "action"),
        )
        return RedirectResponse("/gmail", status_code=303)

    def ai_operation(action, *args):
        try:
            result = action(*args)
            ai.problem = None
            return result
        except AIProblem as exc:
            ai.problem = exc.code
            if exc.code in {"timeout", "connection_failed", "unsafe_configuration"}:
                ai.close()
            return None
        except OSError:
            ai.close()
            ai.problem = "connection_failed"
            return None

    @app.get("/ai", response_class=HTMLResponse)
    def ai_page(request: Request, snapshot: str = ""):
        preview = ai.preview(snapshot) if snapshot else None
        return render(
            request,
            "ai.html",
            ai=ai,
            messages=AI_MESSAGES,
            issuers=ISSUERS,
            snapshots=ai.snapshots(),
            selected=snapshot,
            preview=preview,
            fingerprint=digest(preview) if preview else None,
            proposals=ai.proposals(),
            waiting=ai.status()["state"] == "pending",
        )

    @app.post("/ai/connect", response_class=HTMLResponse)
    async def ai_connect(request: Request):
        form = await form_data(request, {"display"})
        display = display_mode(form)
        if request.url.hostname != "127.0.0.1" or request.url.scheme != "http":
            raise HTTPException(400, "Use the app's http://127.0.0.1 loopback address to connect")
        await run_in_threadpool(ai_operation, ai.connect)
        if ai.authorization_url:
            # The official client owns the callback and token exchange, not this app.
            return render(
                request,
                "provider_forward.html",
                provider="ChatGPT",
                url=ai.authorization_url,
                back="/ai",
            )
        if display == "window":
            return RedirectResponse("/ai", 303)
        return provider_outcome(
            request, "ChatGPT", "/ai", ai.connection is not None, ai.problem, AI_MESSAGES
        )

    @app.get("/ai/status")
    async def ai_status():
        await run_in_threadpool(ai.poll)
        return JSONResponse(ai.status())

    @app.post("/ai/cancel")
    async def ai_cancel(request: Request):
        await form_data(request, set())
        await run_in_threadpool(ai.cancel)
        return RedirectResponse("/ai", 303)

    @app.post("/ai/confirm")
    async def ai_confirm(request: Request):
        await form_data(request, set())
        await run_in_threadpool(ai_operation, ai.confirm)
        return RedirectResponse("/ai", 303)

    @app.post("/ai/disconnect")
    async def ai_disconnect(request: Request):
        await form_data(request, set())
        await run_in_threadpool(ai_operation, ai.disconnect)
        return RedirectResponse("/ai", 303)

    @app.post("/ai/save")
    async def ai_save(request: Request):
        form = await form_data(request, {"content"})
        identity = await run_in_threadpool(ai.save_format, text_field(form, "content"))
        return RedirectResponse("/ai?" + urlencode({"snapshot": identity}), 303)

    @app.post("/ai/import")
    async def ai_import(request: Request):
        form = await form_data(request, {"issuer", "query"})
        await run_in_threadpool(
            ai.import_terms, text_field(form, "issuer"), text_field(form, "query")
        )
        return RedirectResponse("/ai", 303)

    @app.post("/ai/analyse")
    async def ai_analyse(request: Request):
        form = await form_data(request, {"snapshot_id", "fingerprint", "connection_id", "consent"})
        identity = await run_in_threadpool(
            ai_operation,
            ai.analyse,
            *(
                text_field(form, name)
                for name in ("snapshot_id", "fingerprint", "connection_id", "consent")
            ),
        )
        return RedirectResponse(
            "/ai/proposal?" + urlencode({"id": identity}) if identity else "/ai", 303
        )

    @app.get("/ai/proposal", response_class=HTMLResponse)
    def ai_proposal(request: Request, id: str):
        record = ai.find_proposal(id)
        if record is None:
            raise HTTPException(404, "Proposal is missing or belongs to another connection")
        return render(
            request,
            "ai_review.html",
            ai=ai,
            record=record,
            rendered=json.dumps(record["result"], indent=2),
        )

    @app.post("/ai/review")
    async def ai_review(request: Request):
        form = await form_data(request, {"proposal_id", "action"})
        await run_in_threadpool(
            ai_operation, ai.review, text_field(form, "proposal_id"), text_field(form, "action")
        )
        return RedirectResponse("/ai", 303)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        document = ledger.load()
        period = selected_range(request.query_params)
        view = copy(gmail)
        hidden = foreign_events(document, view.namespace)
        sources, choices = card_view(view)
        names = product_options(document)
        labels = {
            account: card["label"]
            + (f" ({names[choices[account]]})" if choices.get(account) in names else "")
            for account, card in sources.items()
        }
        cards = {a: c for a, c in sources.items() if c["kind"] == "card"}
        return render(
            request,
            "overview.html",
            period=period,
            report=range_report(document, period, hidden, labels),
            rewards=card_rewards(document, period, cards, choices, hidden),
            mail=view.refresh_view(),
            added=request.query_params.get("added"),
            unchanged=request.query_params.get("unchanged"),
            gmail=view,
        )

    @app.get("/ledger", response_class=HTMLResponse)
    def ledger_page(request: Request):
        document = ledger.load()
        hidden = foreign_events(document, gmail.namespace)
        transactions = [t for t in heads(document) if t.event_id not in hidden]
        chosen = any(name in request.query_params for name in ("month", "start", "end"))
        period = selected_range(request.query_params) if chosen else None
        if period:
            transactions = [
                row["transaction"] for row in range_report(document, period, hidden)["rows"]
            ]
        return render(
            request,
            "ledger.html",
            transactions=transactions,
            review_count=sum(needs_review(t) for t in transactions),
            revision_count=sum(t.event_id not in hidden for t in document.transactions),
            review_only=False,
            period=period,
            selected=period or selected_range({}),
            groups={t.event_id: grouping(document, t)[1] for t in transactions},
        )

    @app.get("/imports", response_class=HTMLResponse)
    async def imports_page(request: Request):
        return render(request, "imports.html")

    @app.get("/review", response_class=HTMLResponse)
    def queue(request: Request):
        document = ledger.load()
        hidden = foreign_events(document, gmail.namespace)
        transactions = [t for t in heads(document) if needs_review(t) and t.event_id not in hidden]
        return render(
            request,
            "ledger.html",
            transactions=transactions,
            review_count=len(transactions),
            revision_count=sum(t.event_id not in hidden for t in document.transactions),
            added=None,
            review_only=True,
        )

    @app.get("/transaction", response_class=HTMLResponse)
    def detail(request: Request, event_id: str):
        document = ledger.load()
        history = [t for t in document.transactions if t.event_id == event_id]
        transaction = next((t for t in heads(document) if t.event_id == event_id), None)
        if transaction is None or event_id in foreign_events(document, gmail.namespace):
            raise HTTPException(404, "Unknown event")
        observation_ids = {o.observation_id for t in history for o in t.origins}
        refs = [m for t in history for m in walk(t) if isinstance(m, EvidenceRef)]
        observation_ids.update(r.evidence_revision_id for r in refs if r.target == "observation")
        public_ids = {r.evidence_revision_id for r in refs if r.target == "source_evidence"}
        return render(
            request,
            "detail.html",
            transaction=transaction,
            history=history,
            facts=[(name, getattr(transaction, name)) for name in CORRECTION_FIELDS],
            uncertain=uncertain(transaction),
            fields=CORRECTION_FIELDS,
            observations=[o for o in document.observations if o.observation_id in observation_ids],
            evidence=[e for e in document.evidence if e.revision_id in public_ids],
            mail_sources=sorted(
                {
                    (r.evidence_revision_id, r.locator.split(":", 2)[1])
                    for r in refs
                    if r.target == "observation"
                    and re.match(r"mail-version:[a-f0-9]{64}:", r.locator)
                }
            ),
        )

    @app.get("/mail-source", response_class=HTMLResponse)
    def mail_source(request: Request, observation_id: str, version: str):
        observation = next(
            (o for o in ledger.load().observations if o.observation_id == observation_id), None
        )
        if (
            not observation
            or observation.source_system != "gmail"
            or observation.source_namespace != gmail.namespace
        ):
            raise HTTPException(404, "Unknown saved source")
        candidate = gmail.cache.version(gmail.namespace, observation.source_record_id, version)
        if candidate is None:
            raise HTTPException(404, "Unknown saved source version")
        return render(
            request,
            "source.html",
            candidate=candidate,
            version=version,
            back="/ledger",
        )

    def import_result(text):
        result = ledger.import_document(text)
        return RedirectResponse("/?" + urlencode(result), status_code=303)

    @app.post("/import")
    async def import_file(request: Request):
        form = await form_data(request, {"document", "file"})
        try:
            text = text_field(form, "document")
            upload = form.get("file")
            if isinstance(upload, UploadFile) and upload.filename:
                if text.strip():
                    raise HTTPException(422, "Supply a file OR pasted JSON, not both")
                text = (await upload.read()).decode("utf-8")
            elif upload is not None and not isinstance(upload, UploadFile):
                raise HTTPException(422, "file must be an uploaded JSON document")
            if not text.strip():
                raise HTTPException(422, "Select a JSON file or paste a schema document")
            return await run_in_threadpool(import_result, text)
        finally:
            await form.close()

    @app.post("/example")
    async def example(request: Request):
        await form_data(request, set())
        text = files("spending_optimiser").joinpath("examples/synthetic.json").read_text()
        return await run_in_threadpool(import_result, text)

    @app.post("/review")
    async def review(request: Request):
        fields = {"revision_id", "actor", "reason", "status", "field", "fact_json"}
        form = await form_data(request, fields)
        values = {name: text_field(form, name) for name in fields}

        def save_review():
            document = ledger.load()
            target = next(
                (t for t in document.transactions if t.revision_id == values["revision_id"]), None
            )
            if target and target.event_id in foreign_events(document, gmail.namespace):
                raise HTTPException(404, "Unknown event")
            return ledger.review(**values)

        transaction = await run_in_threadpool(gmail_operation, save_review)
        return RedirectResponse(
            "/transaction?" + urlencode({"event_id": transaction.event_id}), status_code=303
        )

    @app.get("/public", response_class=HTMLResponse)
    def public(request: Request):
        document = ledger.load()
        return render(
            request,
            "public.html",
            # Deliberately no private accounts, observations, transactions or quotes.
            public={
                name: document.model_dump(mode="json")[name]
                for name in ("products", "evidence", "rules")
            },
        )

    return app


def main():
    parser = argparse.ArgumentParser(description="Local ledger with optional read-only Gmail")
    parser.add_argument(
        "--configure-google",
        action="store_true",
        help="Save Desktop OAuth client JSON from stdin to the .env file, then exit",
    )
    parser.add_argument(
        "--database", type=Path, help=f"SQLite path (default: {DEFAULT_DB}; env supported)"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--codex-client",
        type=Path,
        help="Official Codex binary (default: SPENDING_CODEX_CLIENT in .env); see "
        "docs/subscription-ai.md",
    )
    args = parser.parse_args()
    try:
        env_file = default_env_path()
    except DotenvError as exc:
        print(f"error: application folder for .env not found ({exc.reason})", file=sys.stderr)
        raise SystemExit(2) from None
    if args.configure_google:
        try:
            if sys.stdin.isatty():
                parser.error("--configure-google requires downloaded client JSON on stdin")
            raw = sys.stdin.read(65537)
            if len(raw) > 65536:
                raise GmailProblem("invalid_configuration")
            configure_google(EnvStore(DotenvFile(env_file)), raw)
        except GmailProblem as exc:
            print(f"error: {exc.code}; {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        print("Google client saved to the .env file. Next: start the app and Connect Gmail.")
        return
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    codex_client = args.codex_client
    if codex_client is None:
        try:
            configured = DotenvFile(env_file).values((CODEX_CLIENT_NAME,))
        except DotenvError as exc:
            print(f"error: .env cannot be read safely ({exc.reason})", file=sys.stderr)
            raise SystemExit(2) from None
        codex_client = (
            Path(configured[CODEX_CLIENT_NAME]) if configured.get(CODEX_CLIENT_NAME) else None
        )
    uvicorn.run(
        create_app(args.database, codex_client=codex_client, env_file=env_file),
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
    )
