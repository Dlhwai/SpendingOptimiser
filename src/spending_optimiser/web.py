"""Loopback ledger, explicit Gmail loading and safe-snapshot-only remote analysis."""

import argparse
import hmac
import json
import os
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
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ai_inputs import CATALOG, digest
from .ai_service import AIService
from .codex_client import MESSAGES as AI_MESSAGES
from .codex_client import AIProblem
from .common import EvidenceRef
from .document import walk
from .gmail_auth import MESSAGES, GmailProblem, NativeStore, configure_google
from .gmail_service import GmailService
from .ledger import CORRECTION_FIELDS, Conflict, Ledger, heads, needs_review, uncertain
from .monthly import SINGAPORE, calendar_month, format_money, grouping, monthly_report

MAX_BODY = 2 * 1024 * 1024
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
                        b"default-src 'none'; style-src 'unsafe-inline'; "
                        b"form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                    ),
                ]
            await send(message)

        await self.app(scope, receive, secured)


def create_app(
    database: str | Path | None = None, *, gmail_service=None, ai_service=None, codex_client=None
) -> FastAPI:
    ledger = Ledger(database or os.environ.get("SPENDING_OPTIMISER_DB", DEFAULT_DB))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ledger = ledger
    gmail = gmail_service if gmail_service is not None else GmailService(ledger.path)
    app.state.gmail = gmail
    ai = ai_service if ai_service is not None else AIService(ledger, binary=codex_client)
    app.state.ai = ai
    app.add_event_handler("shutdown", ai.close)
    gmail_lock = threading.Lock()
    csrf = secrets.token_urlsafe(32)
    templates = Jinja2Templates(directory=str(files("spending_optimiser").joinpath("templates")))
    templates.env.globals["money"] = format_money
    templates.env.globals["singapore"] = SINGAPORE
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
            context={"csrf": csrf, **context},
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

    @app.get("/gmail", response_class=HTMLResponse)
    async def gmail_page(request: Request):
        request.session.setdefault("id", secrets.token_urlsafe(32))
        month = calendar_month(request.query_params.get("month"))
        view = copy(gmail)  # one account identity per rendered response, even during switches
        candidates, scan = view.saved_month(month)
        return render(
            request,
            "gmail.html",
            gmail=view,
            messages=MESSAGES,
            month=month,
            candidates=candidates,
            scan=scan,
        )

    @app.post("/gmail/connect", response_class=HTMLResponse)
    async def gmail_connect(request: Request):
        await form_data(request, set())
        if request.url.scheme != "http" or request.url.hostname != "127.0.0.1":
            raise HTTPException(
                400, "Open this application on http://127.0.0.1 at its local port to connect Gmail"
            )
        session = request.session.setdefault("id", secrets.token_urlsafe(32))
        redirect = str(request.url.replace(path="/gmail/callback", query=""))
        url = await run_in_threadpool(gmail_operation, gmail.begin, session, redirect)
        if url is None:
            return RedirectResponse("/gmail", status_code=303)
        # Use an explicit top-level link, not an external POST/form redirect, to
        # retain the existing form-action 'self' and Origin protections.
        return render(request, "gmail_connect.html", authorization_url=url)

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
        response = RedirectResponse("/gmail", status_code=303)
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

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

    @app.post("/gmail/month")
    async def gmail_month(request: Request):
        form = await form_data(request, {"month", "pages", "connection_id", "action"})
        try:
            pages = int(text_field(form, "pages"))
        except ValueError:
            raise HTTPException(422, "Choose 1-5 pages per action") from None
        month = calendar_month(text_field(form, "month"))
        await run_in_threadpool(
            gmail_operation,
            gmail.load_month,
            month.key,
            pages,
            text_field(form, "connection_id"),
            text_field(form, "action"),
        )
        return RedirectResponse("/gmail?" + urlencode({"month": month.key}), status_code=303)

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
    async def ai_page(request: Request, snapshot: str = ""):
        preview = ai.preview(snapshot) if snapshot else None
        return render(
            request,
            "ai.html",
            ai=ai,
            messages=AI_MESSAGES,
            catalog=CATALOG,
            snapshots=ai.snapshots(),
            selected=snapshot,
            preview=preview,
            fingerprint=digest(preview) if preview else None,
            proposals=ai.proposals(),
        )

    @app.post("/ai/connect")
    async def ai_connect(request: Request):
        await form_data(request, set())
        if request.url.hostname != "127.0.0.1" or request.url.scheme != "http":
            raise HTTPException(400, "Use the app's http://127.0.0.1 loopback address to connect")
        await run_in_threadpool(ai_operation, ai.connect)
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
        form = await form_data(request, {"key"})
        identity = ai.save(text_field(form, "key"))
        return RedirectResponse("/ai?" + urlencode({"snapshot": identity}), 303)

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
    async def ai_proposal(request: Request, id: str):
        record = next((p for p in ai.proposals() if p["id"] == id), None)
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
    async def index(request: Request):
        document = ledger.load()
        month = calendar_month(request.query_params.get("month"))
        view = copy(gmail)
        candidates, scan = view.saved_month(month)
        return render(
            request,
            "overview.html",
            month=month,
            report=monthly_report(document, month),
            candidates=candidates,
            scan=scan,
            added=request.query_params.get("added"),
            unchanged=request.query_params.get("unchanged"),
            gmail=view,
        )

    @app.get("/ledger", response_class=HTMLResponse)
    async def ledger_page(request: Request):
        document = ledger.load()
        transactions = heads(document)
        month = (
            calendar_month(request.query_params["month"])
            if "month" in request.query_params
            else None
        )
        if month:
            transactions = [row["transaction"] for row in monthly_report(document, month)["rows"]]
        return render(
            request,
            "ledger.html",
            transactions=transactions,
            review_count=sum(needs_review(t) for t in transactions),
            revision_count=len(document.transactions),
            review_only=False,
            month=month,
            selected_month=month or calendar_month(),
            groups={t.event_id: grouping(document, t)[1] for t in transactions},
        )

    @app.get("/imports", response_class=HTMLResponse)
    async def imports_page(request: Request):
        return render(request, "imports.html")

    @app.get("/review", response_class=HTMLResponse)
    async def queue(request: Request):
        document = ledger.load()
        transactions = [t for t in heads(document) if needs_review(t)]
        return render(
            request,
            "ledger.html",
            transactions=transactions,
            review_count=len(transactions),
            revision_count=len(document.transactions),
            added=None,
            review_only=True,
        )

    @app.get("/transaction", response_class=HTMLResponse)
    async def detail(request: Request, event_id: str):
        document = ledger.load()
        history = [t for t in document.transactions if t.event_id == event_id]
        transaction = next((t for t in heads(document) if t.event_id == event_id), None)
        if transaction is None:
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
            return import_result(text)
        finally:
            await form.close()

    @app.post("/example")
    async def example(request: Request):
        await form_data(request, set())
        text = files("spending_optimiser").joinpath("examples/synthetic.json").read_text()
        return import_result(text)

    @app.post("/review")
    async def review(request: Request):
        fields = {"revision_id", "actor", "reason", "status", "field", "fact_json"}
        form = await form_data(request, fields)
        values = {name: text_field(form, name) for name in fields}
        transaction = ledger.review(**values)
        return RedirectResponse(
            "/transaction?" + urlencode({"event_id": transaction.event_id}), status_code=303
        )

    @app.get("/public", response_class=HTMLResponse)
    async def public(request: Request):
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
        help="Import Desktop OAuth client JSON from stdin to the native OS store, then exit",
    )
    parser.add_argument(
        "--database", type=Path, help=f"SQLite path (default: {DEFAULT_DB}; env supported)"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--codex-client",
        type=Path,
        help="Opt in to the proof-qualified official Codex binary; see docs/subscription-ai.md",
    )
    args = parser.parse_args()
    if args.configure_google:
        try:
            if sys.stdin.isatty():
                parser.error("--configure-google requires downloaded client JSON on stdin")
            raw = sys.stdin.read(65537)
            if len(raw) > 65536:
                raise GmailProblem("invalid_configuration")
            configure_google(NativeStore(), raw)
        except GmailProblem as exc:
            print(f"error: {exc.code}; {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        print(
            "Google client saved to native OS credential store. "
            "Next: start the app and Connect Gmail."
        )
        return
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    uvicorn.run(
        create_app(args.database, codex_client=args.codex_client),
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
    )
