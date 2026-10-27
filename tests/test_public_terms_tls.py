"""Official issuer-terms downloads verify TLS against an explicit certifi bundle.

`https_get` runs unmodified over real http.client and ssl: the pinned public address is
redirected to a loopback TLS server presenting certificates from synthetic CAs, and the host
trust store is emptied as on a python.org interpreter whose "Install Certificates" step never
ran. No real issuer is contacted. Synthetic data only.
"""

import socket
import ssl

import certifi
import pytest
from test_gmail_tls import Authority, TlsServer, rejected_handshakes

from spending_optimiser import public_terms
from spending_optimiser.public_terms import Budget, FetchProblem, https_get

HOST = "www.dbs.com.sg"
URL = f"https://{HOST}/personal/cards/credit-cards/dbs-yuu-terms"
PINNED = "8.8.8.8"


def resolve(host, port, *, type):
    assert host == HOST and port == 443 and type == socket.SOCK_STREAM
    return [(None, None, None, None, (PINNED, port))]


@pytest.fixture(scope="module")
def trusted(tmp_path_factory):
    return Authority(tmp_path_factory.mktemp("terms-trusted-ca"), "trusted")


@pytest.fixture(scope="module")
def stranger(tmp_path_factory):
    return Authority(tmp_path_factory.mktemp("terms-untrusted-ca"), "stranger")


@pytest.fixture
def server(tmp_path, monkeypatch, trusted):
    """Serve the pinned issuer address from loopback with an empty host trust store."""
    (tmp_path / "empty-certs").mkdir()
    (tmp_path / "empty.pem").write_bytes(b"")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "empty.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-certs"))
    assert ssl.create_default_context().cert_store_stats()["x509_ca"] == 0
    servers = []
    real_connect = socket.create_connection

    def serve(authority=trusted, host=HOST):
        servers.append(TlsServer(authority.server_context(host)))
        servers[-1].headers = {"Content-Type": "text/plain"}
        servers[-1].body = b"Synthetic terms and conditions"
        return servers[-1]

    def connect(address, *args, **kwargs):
        if address != (PINNED, 443) or not servers:
            raise AssertionError(f"unexpected socket destination: {address[0]}")
        return real_connect(("127.0.0.1", servers[-1].port), *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", connect)
    yield serve
    for each in servers:
        each.listener.close()


def trust_only(monkeypatch, authority):
    monkeypatch.setattr(certifi, "where", lambda: str(authority.bundle))


def refused(tls):
    with pytest.raises(FetchProblem, match="retrieval failed") as caught:
        https_get(URL, Budget(), resolve=resolve)
    assert caught.value.__cause__ is None
    assert rejected_handshakes(tls) == 1 and not tls.requests


def test_interpreter_default_trust_fails_when_the_host_store_is_empty(server, trusted, monkeypatch):
    tls = server()
    trust_only(monkeypatch, trusted)
    default = ssl.create_default_context
    monkeypatch.setattr(public_terms.ssl, "create_default_context", lambda cafile: default())
    refused(tls)


def test_explicit_bundle_downloads_when_the_host_store_is_empty(server, trusted, monkeypatch):
    tls = server()
    trust_only(monkeypatch, trusted)
    status, headers, body = https_get(URL, Budget(), resolve=resolve)
    assert (status, headers["content-type"], body) == (
        200,
        "text/plain",
        b"Synthetic terms and conditions",
    )
    line, fields = tls.requests[0]
    assert line == "GET /personal/cards/credit-cards/dbs-yuu-terms HTTP/1.1"
    assert fields["host"] == f"Host: {HOST}"
    assert not {"authorization", "cookie"} & fields.keys()
    assert tls.handshake_failures == 0


def test_hostname_mismatch_is_rejected_with_a_trusted_bundle(server, trusted, monkeypatch):
    tls = server(host="wrong-host.invalid")
    trust_only(monkeypatch, trusted)
    refused(tls)


def test_certificate_from_an_authority_outside_the_bundle_is_rejected(server, stranger):
    refused(server(stranger))  # certifi.where() is the real bundle, which cannot know this CA


def test_trust_comes_from_the_bundle_and_not_the_environment(
    server, trusted, stranger, monkeypatch
):
    tls = server(stranger)
    trust_only(monkeypatch, trusted)
    monkeypatch.setenv("SSL_CERT_FILE", str(stranger.bundle))
    refused(tls)
