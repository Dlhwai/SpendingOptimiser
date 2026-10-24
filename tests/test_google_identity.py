"""Verify real Google-auth signature/claim checking with ephemeral synthetic keys."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt, jwt
from google.oauth2.credentials import Credentials

from spending_optimiser.gmail_auth import SCOPES, GmailProblem, GoogleOAuth


@pytest.mark.parametrize("invalid", [None, "audience", "issuer", "expired", "nonce", "signature"])
def test_real_signature_and_claim_verification(invalid):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic only")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "synthetic-client",
        "sub": "synthetic-sub",
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 3600,
        "nonce": "synthetic-nonce",
    }
    if invalid == "audience":
        claims["aud"] = "other-client"
    elif invalid == "issuer":
        claims["iss"] = "https://untrusted.invalid"
    elif invalid == "expired":
        claims["iat"], claims["exp"] = 100, 200
    elif invalid == "nonce":
        claims["nonce"] = "wrong-nonce"
    if invalid == "signature":
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = other_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    token = jwt.encode(
        crypt.RSASigner.from_string(private, key_id="synthetic-key"), claims
    ).decode()
    credentials = Credentials(
        "synthetic-token",
        refresh_token="synthetic-refresh",
        client_id="synthetic-client",
        id_token=token,
        scopes=SCOPES,
    )
    flow = SimpleNamespace(
        credentials=credentials,
        fetch_token=lambda **kw: None,
        oauth2session=SimpleNamespace(token={"scope": " ".join(SCOPES)}),
    )

    def certificate_request(url, **kwargs):
        assert url == "https://www.googleapis.com/oauth2/v1/certs"
        return SimpleNamespace(
            status=200,
            data=json.dumps(
                {"synthetic-key": cert.public_bytes(serialization.Encoding.PEM).decode()}
            ).encode(),
        )

    class Profile:
        def get_json(self, url, headers):
            assert url.endswith("/users/me/profile")
            return {"emailAddress": "synthetic@example.invalid"}

    oauth = GoogleOAuth(auth_request=certificate_request, transport=Profile())
    if invalid:
        with pytest.raises(GmailProblem, match="authorization failed"):
            oauth.finish(flow, "synthetic-code", "synthetic-nonce")
    else:
        connection = oauth.finish(flow, "synthetic-code", "synthetic-nonce")
        assert connection.subject == "synthetic-sub"
