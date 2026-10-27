"""Gmail's urllib transport verifies TLS against an explicit certifi bundle.

Everything below crosses the real urllib and ssl boundary: a loopback TLS server presents
certificates signed by synthetic CAs, and only the TCP destination of gmail.googleapis.com is
redirected to it. The host trust store is emptied through the OpenSSL environment overrides, as
on a python.org interpreter whose "Install Certificates" step never ran. Synthetic data only.
"""

import datetime as dt
import json
import socket
import ssl
import threading
import time
import urllib.request
from types import SimpleNamespace

import certifi
import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt, jwt

from spending_optimiser.gmail import ClientFailure, IngestionError, UrllibGetTransport
from spending_optimiser.gmail_auth import SCOPES, GmailProblem, GoogleOAuth, client_config

HOST = "gmail.googleapis.com"
PROFILE_URL = f"https://{HOST}/gmail/v1/users/me/profile"
CLIENT_ID = "123456789012-synthetic.apps.googleusercontent.com"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PRIVATE = "SYNTHETIC_PRIVATE_DO_NOT_LOG"
LIMIT = 8 * 1024 * 1024


def _name(common):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _cert(subject, issuer, public, signer, *, ca=False, host=None):
    now = dt.datetime.now(dt.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if host:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False
        )
    return builder.sign(signer, hashes.SHA256())


def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


class Authority:
    """A synthetic CA that is trusted only when a test hands its bundle to the transport."""

    def __init__(self, directory, label):
        key = ec.generate_private_key(ec.SECP256R1())
        self.cert = _cert(_name(label), _name(label), key.public_key(), key, ca=True)
        self.key = key
        self.bundle = directory / f"{label}.pem"
        self.bundle.write_bytes(_pem(self.cert))
        self.directory, self.label = directory, label

    def server_context(self, host):
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        leaf = _cert(_name(host), self.cert.subject, leaf_key.public_key(), self.key, host=host)
        chain, key = self.directory / f"{self.label}-{host}.pem", self.directory / f"{host}.key"
        chain.write_bytes(_pem(leaf))
        key.write_bytes(_key_pem(leaf_key))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(chain, key)
        return context


class TlsServer(threading.Thread):
    """Loopback HTTPS stand-in for gmail.googleapis.com that records what reached it."""

    def __init__(self, context):
        super().__init__(daemon=True)
        self.context = context
        self.status, self.headers, self.body = 200, {}, b'{"emailAddress": "a@example.invalid"}'
        self.requests = []  # (request line, lower-cased header names) per completed request
        self.handshake_failures = 0
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        self.start()

    def run(self):
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            try:
                tls = self.context.wrap_socket(raw, server_side=True)
            except (ssl.SSLError, OSError):
                self.handshake_failures += 1
                raw.close()
                continue
            with tls:
                try:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = tls.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    lines = data.decode().split("\r\n")
                    fields = {line.split(":")[0].lower(): line for line in lines[1:]}
                    self.requests.append((lines[0], fields))
                    head = [
                        f"HTTP/1.1 {self.status} Synthetic",
                        f"Content-Length: {len(self.body)}",
                    ]
                    head += [f"{k}: {v}" for k, v in self.headers.items()] + ["Connection: close"]
                    tls.sendall("\r\n".join(head).encode() + b"\r\n\r\n" + self.body)
                except OSError:  # the client stopped reading, e.g. after the size cap
                    pass


@pytest.fixture(scope="module")
def trusted(tmp_path_factory):
    return Authority(tmp_path_factory.mktemp("trusted-ca"), "trusted")


@pytest.fixture(scope="module")
def stranger(tmp_path_factory):
    return Authority(tmp_path_factory.mktemp("untrusted-ca"), "stranger")


@pytest.fixture
def empty_host_store(tmp_path, monkeypatch):
    """The interpreter's own default trust store holds zero certificate authorities."""
    (tmp_path / "empty-certs").mkdir()
    (tmp_path / "empty.pem").write_bytes(b"")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "empty.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certs"))
    assert ssl.create_default_context().cert_store_stats()["x509_ca"] == 0


@pytest.fixture
def server(monkeypatch, empty_host_store, trusted):
    """Serve `gmail.googleapis.com:443` from loopback; any other destination fails loudly."""
    servers = []
    real_connect = socket.create_connection

    def serve(authority=trusted, host=HOST):
        servers.append(TlsServer(authority.server_context(host)))
        return servers[-1]

    def connect(address, *args, **kwargs):
        if address != (HOST, 443) or not servers:
            raise AssertionError(f"unexpected socket destination: {address[0]}")
        return real_connect(("127.0.0.1", servers[-1].port), *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", connect)
    yield serve
    for each in servers:
        each.listener.close()


def rejected_handshakes(tls):
    """The server counts a failed handshake slightly after the client has already given up."""
    deadline = time.monotonic() + 5
    while not tls.handshake_failures and time.monotonic() < deadline:
        time.sleep(0.01)
    return tls.handshake_failures


def trust_only(monkeypatch, authority):
    """Stand in for certifi's bundle: the transport must take its anchors from certifi.where()."""
    monkeypatch.setattr(certifi, "where", lambda: str(authority.bundle))


def get(url=PROFILE_URL):
    return UrllibGetTransport().get_json(url, {"Authorization": "Bearer synthetic-token"})


def failure_code(url=PROFILE_URL):
    with pytest.raises(ClientFailure) as caught:
        get(url)
    assert caught.value.__cause__ is None and PRIVATE not in repr(caught.value.error)
    return caught.value.error


def test_stdlib_default_trust_fails_when_the_host_store_is_empty(server):
    tls = server()
    with pytest.raises(urllib.error.URLError) as caught:
        urllib.request.build_opener().open(PROFILE_URL, timeout=30)
    assert isinstance(caught.value.reason, ssl.SSLCertVerificationError)
    assert rejected_handshakes(tls) == 1 and not tls.requests


def test_explicit_bundle_connects_when_the_host_store_is_empty(server, trusted, monkeypatch):
    tls = server()
    trust_only(monkeypatch, trusted)
    assert get() == {"emailAddress": "a@example.invalid"}
    line, headers = tls.requests[0]
    assert line == "GET /gmail/v1/users/me/profile HTTP/1.1"
    assert headers["authorization"] == "Authorization: Bearer synthetic-token"
    assert tls.handshake_failures == 0


def test_hostname_mismatch_is_rejected_with_a_trusted_bundle(server, trusted, monkeypatch):
    tls = server(host="wrong-host.invalid")
    trust_only(monkeypatch, trusted)
    assert failure_code() == IngestionError("transport_failed", True)
    assert rejected_handshakes(tls) == 1 and not tls.requests


def test_certificate_from_an_authority_outside_the_bundle_is_rejected(server, trusted, stranger):
    tls = server(stranger)  # certifi.where() is the real bundle, which cannot know this CA
    assert failure_code() == IngestionError("transport_failed", True)
    assert rejected_handshakes(tls) == 1 and not tls.requests


def test_trust_comes_from_the_bundle_and_not_the_environment(
    server, trusted, stranger, monkeypatch
):
    tls = server(stranger)
    trust_only(monkeypatch, trusted)
    monkeypatch.setenv("SSL_CERT_FILE", str(stranger.bundle))
    assert failure_code() == IngestionError("transport_failed", True)
    assert rejected_handshakes(tls) == 1


def test_redirect_is_refused_over_tls_without_contacting_its_target(server, trusted, monkeypatch):
    tls = server()
    tls.status, tls.headers = 302, {"Location": "https://redirect-target.invalid/steal"}
    trust_only(monkeypatch, trusted)
    assert failure_code() == IngestionError("api_error", False, 302)
    assert len(tls.requests) == 1


def test_response_cap_applies_over_tls(server, trusted, monkeypatch):
    tls = server()
    tls.body = b" " * (LIMIT + 1)
    trust_only(monkeypatch, trusted)
    assert failure_code() == IngestionError("response_too_large")


def test_http_errors_stay_redacted_over_tls(server, trusted, monkeypatch):
    tls = server()
    tls.status, tls.body = 403, json.dumps({"error": PRIVATE}).encode()
    trust_only(monkeypatch, trusted)
    assert failure_code() == IngestionError("permission_denied", False, 403)


def test_the_bundle_context_keeps_verification_on(trusted):
    from spending_optimiser.gmail import _trusted_context

    context = _trusted_context(str(trusted.bundle))
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()["x509_ca"] == 1


class Sdk:
    """Real Flow.fetch_token, ID-token verification and GoogleOAuth.finish; fake token endpoint."""

    def __init__(self, monkeypatch):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.cert = _cert(_name("signer"), _name("signer"), key.public_key(), key)
        self.signer = crypt.RSASigner.from_string(_key_pem(key), key_id="synthetic-key")
        self.oauth = GoogleOAuth(auth_request=self.certificates)
        config = client_config(
            json.dumps({"installed": {"client_id": CLIENT_ID, "client_secret": "synthetic"}})
        )
        self.flow, _, _, self.nonce = self.oauth.begin(config, "http://127.0.0.1:8765/callback")
        adapter = SimpleNamespace(send=self.token_endpoint, close=lambda: None)
        monkeypatch.setattr(requests.Session, "get_adapter", lambda session, url: adapter)

    def certificates(self, url, **kwargs):
        assert url == "https://www.googleapis.com/oauth2/v1/certs"
        data = json.dumps({"synthetic-key": _pem(self.cert).decode()}).encode()
        return SimpleNamespace(status=200, data=data)

    def token_endpoint(self, request, **kwargs):
        assert request.url == TOKEN_URL
        now = int(dt.datetime.now(dt.UTC).timestamp())
        id_token = jwt.encode(
            self.signer,
            {
                "iss": "https://accounts.google.com",
                "aud": CLIENT_ID,
                "sub": "112233445566778899001",
                "nonce": self.nonce,
                "iat": now,
                "exp": now + 3600,
            },
        ).decode()
        response = requests.Response()
        response.status_code, response.request, response.url = 200, request, request.url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {
                "access_token": "ya29.synthetic",
                "refresh_token": "1//synthetic",
                "expires_in": 3599,
                "scope": " ".join(SCOPES),
                "token_type": "Bearer",
                "id_token": id_token,
            }
        ).encode()
        return response

    def finish(self):
        return self.oauth.finish(self.flow, "4/synthetic-code", self.nonce)


def test_sign_in_profile_step_succeeds_through_the_real_sdk_and_tls(server, trusted, monkeypatch):
    sdk = Sdk(monkeypatch)
    tls = server()
    trust_only(monkeypatch, trusted)
    connection = sdk.finish()
    assert connection.email == "a@example.invalid" and len(tls.requests) == 1


@pytest.mark.parametrize("fault", ["hostname", "authority"])
def test_sign_in_profile_step_still_fails_on_bad_certificates(
    server, trusted, stranger, monkeypatch, fault
):
    sdk = Sdk(monkeypatch)
    tls = server(host="wrong-host.invalid") if fault == "hostname" else server(stranger)
    trust_only(monkeypatch, trusted)
    with pytest.raises(GmailProblem) as caught:
        sdk.finish()
    assert caught.value.code == "oauth_failed" and caught.value.__cause__ is None
    assert rejected_handshakes(tls) == 1 and not tls.requests
