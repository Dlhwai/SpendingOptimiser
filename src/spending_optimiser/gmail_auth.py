"""Desktop Google authorization and .env-backed credentials. No mail or ledger writes."""

import functools
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Protocol

from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .dotenv_store import DotenvError, DotenvFile
from .gmail import GMAIL_READONLY_SCOPE, UrllibGetTransport

SCOPES = (GMAIL_READONLY_SCOPE, "openid")
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CLIENT_ID_NAME = "SPENDING_GOOGLE_CLIENT_ID"
CLIENT_SECRET_NAME = "SPENDING_GOOGLE_CLIENT_SECRET"
AUTHORIZATION_PREFIX = "SPENDING_GMAIL_AUTHORIZATION_"

MESSAGES = {
    "env_unavailable": (
        "The local .env file cannot be read or written safely. Check its path and permissions."
    ),
    "not_configured": (
        "Google client not configured. Add SPENDING_GOOGLE_CLIENT_ID and "
        "SPENDING_GOOGLE_CLIENT_SECRET to .env."
    ),
    "invalid_configuration": (
        "Invalid Google client in .env. Use the ID and secret of a Desktop app OAuth client."
    ),
    "invalid_callback": "Sign-in could not be verified (invalid, expired or reused). Try again.",
    "consent_denied": "Google sign-in was denied or cancelled. Nothing was loaded.",
    "oauth_failed": "Google authorization failed. Check the client configuration and try again.",
    "scope_denied": (
        "Only Gmail read-only and OpenID identity access are supported. Grant the requested "
        "scopes with a dedicated client and reconnect."
    ),
    "reconnect": "Authorization expired or was revoked. Reconnect Gmail.",
    "disconnected": (
        "Gmail disconnected. Saved mail stays on this device. "
        "Remove the app in your Google Account to revoke access."
    ),
    "no_saved_connection": "No saved connection. Connect Gmail.",
    "cancelled": "Sign-in cancelled.",
    "expired": "Sign-in expired. Reconnect Gmail.",
}


class GmailProblem(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


class CredentialStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class EnvStore:
    """Credential store over the application's gitignored .env: plaintext, owner-only.

    Values are read and written through DotenvFile and never exported to os.environ,
    so no child process (such as the GPT client) inherits them.
    """

    def __init__(self, env):
        self.env = env if isinstance(env, DotenvFile) else DotenvFile(env)

    @staticmethod
    def _name(key):
        if not re.fullmatch(r"connection-[0-9a-f]{64}", key):
            raise ValueError("Unknown credential key")
        return AUTHORIZATION_PREFIX + key.removeprefix("connection-").upper()

    def get(self, key):
        try:
            if key == "desktop-client":
                found = self.env.values((CLIENT_ID_NAME, CLIENT_SECRET_NAME))
                if len(found) != 2 or not all(found.values()):
                    return None  # blank placeholders from .env.example count as unset
                return json.dumps(
                    {
                        "installed": {
                            "client_id": found[CLIENT_ID_NAME],
                            "client_secret": found[CLIENT_SECRET_NAME],
                        }
                    }
                )
            name = self._name(key)
            return self.env.values((name,)).get(name)
        except DotenvError:
            raise GmailProblem("env_unavailable") from None

    def put(self, key, value):
        try:
            if key == "desktop-client":
                installed = json.loads(value)["installed"]
                self.env.update(
                    {
                        CLIENT_ID_NAME: installed["client_id"],
                        CLIENT_SECRET_NAME: installed["client_secret"],
                    }
                )
            else:
                self.env.update({self._name(key): value})
        except DotenvError:
            raise GmailProblem("env_unavailable") from None

    def delete(self, key):
        try:
            names = (
                (CLIENT_ID_NAME, CLIENT_SECRET_NAME)
                if key == "desktop-client"
                else (self._name(key),)
            )
            self.env.update(remove=names)
        except DotenvError:
            raise GmailProblem("env_unavailable") from None


def client_config(raw):
    try:
        installed = json.loads(raw)["installed"]
        client_id, secret = installed["client_id"], installed["client_secret"]
        if (
            not isinstance(client_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]+\.apps\.googleusercontent\.com", client_id)
            or not isinstance(secret, str)
            or not 1 <= len(secret) <= 1024
        ):
            raise ValueError()
        # Never accept configurable token/auth endpoints from a downloaded file/store.
        return {
            "installed": {
                "client_id": client_id,
                "client_secret": secret,
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
            }
        }
    except (ValueError, KeyError, TypeError, RecursionError):
        raise GmailProblem("invalid_configuration") from None


def configure_google(store, raw):
    config = client_config(raw)
    store.put("desktop-client", json.dumps(config))


@dataclass(repr=False)
class GoogleConnection:
    subject: str
    email: str
    credentials: Credentials = field(repr=False)

    def saved(self):
        # No access/ID token, message data, or secret in the database/browser cookie.
        return json.dumps(
            {
                "subject": self.subject,
                "email": self.email,
                "refresh_token": self.credentials.refresh_token,
                "client_id": self.credentials.client_id,
                "scopes": list(SCOPES),
            }
        )


class GoogleOAuth:
    def __init__(self, *, transport=None, auth_request=None, verify=None):
        self.transport = transport if transport is not None else UrllibGetTransport()
        self.auth_request = auth_request if auth_request is not None else Request()
        self.verify = (
            verify
            if verify is not None
            else functools.partial(id_token.verify_oauth2_token, clock_skew_in_seconds=10)
        )

    def begin(self, config, redirect):
        try:
            flow = Flow.from_client_config(
                config, scopes=list(SCOPES), autogenerate_code_verifier=True, redirect_uri=redirect
            )
            nonce = secrets.token_urlsafe(32)
            url, state = flow.authorization_url(
                access_type="offline",
                prompt="consent select_account",
                nonce=nonce,
                include_granted_scopes="false",
            )
            return flow, url, state, nonce
        except Exception:
            raise GmailProblem("oauth_failed") from None

    def finish(self, flow, code, nonce):
        try:
            flow.fetch_token(code=code, timeout=30)
            scope = flow.oauth2session.token.get("scope", [])
            granted = scope.split() if isinstance(scope, str) else scope
            if set(granted) != set(SCOPES):
                raise GmailProblem("scope_denied")
            credentials = flow.credentials
            if not credentials.refresh_token or not credentials.token or not credentials.id_token:
                raise ValueError()
            claims = self.verify(credentials.id_token, self.auth_request, credentials.client_id)
            subject = claims["sub"]
            if (
                not isinstance(subject, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", subject)
                or not secrets.compare_digest(claims.get("nonce", ""), nonce)
            ):
                raise ValueError()
            profile = self.transport.get_json(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                {"Authorization": f"Bearer {credentials.token}", "Accept": "application/json"},
            )
            email = profile["emailAddress"]
            if not isinstance(email, str) or not re.fullmatch(
                r"[^\s@]{1,128}@[^\s@]{1,190}", email
            ):
                raise ValueError()
            return GoogleConnection(subject, email, credentials)
        except GmailProblem:
            raise
        except Warning:
            # OAuthlib rejects a changed scope grant before credentials are built.
            raise GmailProblem("scope_denied") from None
        except Exception:
            raise GmailProblem("oauth_failed") from None

    def restore(self, raw, config):
        try:
            saved = json.loads(raw)
            client = config["installed"]
            if (
                saved["client_id"] != client["client_id"]
                or set(saved["scopes"]) != set(SCOPES)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", saved["subject"])
                or not isinstance(saved["email"], str)
                or not saved["refresh_token"]
            ):
                raise ValueError()
            credentials = Credentials(
                None,
                refresh_token=saved["refresh_token"],
                token_uri=TOKEN_URI,
                client_id=client["client_id"],
                client_secret=client["client_secret"],
                scopes=list(SCOPES),
            )
            return GoogleConnection(saved["subject"], saved["email"], credentials)
        except Exception:
            raise GmailProblem("reconnect") from None

    def refresh(self, connection):
        try:
            connection.credentials.refresh(self.auth_request)
            granted = connection.credentials.granted_scopes
            if granted is not None and set(granted) != set(SCOPES):
                raise ValueError()
        except Exception:
            raise GmailProblem("reconnect") from None
