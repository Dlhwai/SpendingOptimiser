"""Offline, loopback-only browser application. No fetching or model integrations."""

import argparse
import hmac
import json
import os
import secrets
import sqlite3
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .common import EvidenceRef
from .document import walk
from .ledger import CORRECTION_FIELDS, Conflict, Ledger, heads, needs_review, uncertain

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
                message["headers"] += [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    # no-referrer makes Chrome send Origin:null on same-origin form POSTs.
                    (b"referrer-policy", b"same-origin"),
                    (
                        b"content-security-policy",
                        b"default-src 'none'; style-src 'unsafe-inline'; "
                        b"form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                    ),
                ]
            await send(message)

        await self.app(scope, receive, secured)


def create_app(database: str | Path | None = None) -> FastAPI:
    ledger = Ledger(database or os.environ.get("SPENDING_OPTIMISER_DB", DEFAULT_DB))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ledger = ledger
    csrf = secrets.token_urlsafe(32)
    templates = Jinja2Templates(directory=str(files("spending_optimiser").joinpath("templates")))
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

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        return render(
            request,
            "error.html",
            status_code=503,
            message=(
                "Local database unavailable; no partial changes saved. Check disk/access and retry."
            ),
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        document = ledger.load()
        transactions = heads(document)
        return render(
            request,
            "ledger.html",
            transactions=transactions,
            review_count=sum(needs_review(t) for t in transactions),
            revision_count=len(document.transactions),
            added=request.query_params.get("added"),
            unchanged=request.query_params.get("unchanged"),
            review_only=False,
        )

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
    parser = argparse.ArgumentParser(description="Offline local schema-document ledger")
    parser.add_argument(
        "--database", type=Path, help=f"SQLite path (default: {DEFAULT_DB}; env supported)"
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    uvicorn.run(
        create_app(args.database),
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
    )
