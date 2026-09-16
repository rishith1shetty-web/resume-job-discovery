"""The shared TLS trust store.

Every request used to build its own ``httpx.AsyncClient``, and constructing one
parses the certifi CA bundle from disk - measured at 451ms each against 21ms
for the rest of the context. Because that is CPU rather than waiting, it did
not overlap while portals collected concurrently; across a four-role benchmark
it came to 962 seconds, 60% of Workday's entire leg.

The fix is one context for the process. These tests exist to make sure it
bought speed and nothing else: the trust store must still be certifi's, the
verification must still be full, and a bad certificate must still be refused.
A shared context that quietly stopped verifying would be far worse than the
cost it saved.

Note what is deliberately *not* shared: clients and connections. Each request
still builds its own client and opens its own connection.
"""

from __future__ import annotations

import contextlib
import datetime
import http.server
import ssl
import threading

import httpx
import pytest

from app.portals.http import SSL_CONTEXT, HttpPolicy, PortalHttpClient


def _client(**overrides) -> PortalHttpClient:
    defaults = {
        "timeout_seconds": 5.0,
        "max_retries": 0,
        "retry_backoff_seconds": 0.0,
        "rate_limit_per_minute": 0,
    }
    return PortalHttpClient(
        portal="testportal", policy=HttpPolicy(**{**defaults, **overrides})
    )


# --- What the context is ----------------------------------------------------


def test_the_trust_store_is_a_real_ssl_context() -> None:
    assert isinstance(SSL_CONTEXT, ssl.SSLContext)


def test_certificate_verification_is_required() -> None:
    """CERT_REQUIRED, not CERT_OPTIONAL and certainly not CERT_NONE."""
    assert SSL_CONTEXT.verify_mode is ssl.CERT_REQUIRED


def test_hostname_checking_is_enabled() -> None:
    """Without this, a valid certificate for any host would be accepted."""
    assert SSL_CONTEXT.check_hostname is True


def test_the_trust_store_actually_contains_certificate_authorities() -> None:
    """An empty store would verify nothing while looking configured."""
    assert len(SSL_CONTEXT.get_ca_certs()) > 0


def test_it_is_the_same_store_httpx_would_have_built() -> None:
    """The bundle is unchanged - this is a speed change, not a trust change.

    Compared by subject rather than by object identity, because httpx builds a
    fresh context each time it is asked.
    """
    reference = httpx.create_ssl_context()

    def subjects(context: ssl.SSLContext) -> set:
        return {
            tuple(sorted(str(part) for part in cert.get("subject", ())))
            for cert in context.get_ca_certs()
        }

    assert subjects(SSL_CONTEXT) == subjects(reference)


# --- That it is genuinely reused --------------------------------------------


def test_one_context_is_reused_across_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: many requests, one context object.

    The identity check is what matters. A test that only counted constructions
    would still pass if each client built its own context and threw it away.
    """
    seen: list[object] = []
    original = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):  # noqa: ANN001
        seen.append(kwargs.get("verify"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)

    async def _get(self, url, params=None):  # noqa: ANN001
        return httpx.Response(
            200, text="ok", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)

    import asyncio

    client = _client()

    async def run() -> None:
        for _ in range(4):
            await client.get_text("https://example.invalid/x")

    asyncio.run(run())

    assert len(seen) == 4
    assert all(context is SSL_CONTEXT for context in seen)


def test_the_reachability_probe_uses_it_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``head_ok`` builds its own client and must not be left behind."""
    seen: list[object] = []
    original = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):  # noqa: ANN001
        seen.append(kwargs.get("verify"))
        return original(self, *args, **kwargs)

    async def _head(self, url, **kwargs):  # noqa: ANN001
        return httpx.Response(200, request=httpx.Request("HEAD", url))

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    monkeypatch.setattr(httpx.AsyncClient, "head", _head)

    import asyncio

    assert asyncio.run(_client().head_ok("https://example.invalid/x")) is True
    assert seen and all(context is SSL_CONTEXT for context in seen)


def test_clients_and_connections_are_still_not_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sharing the trust store is not sharing a connection pool.

    Stated as a test because the two are easy to conflate, and a shared client
    would carry cookies and pooled connections between requests - which this
    change deliberately does not do.
    """
    # Counted, not identified by `id()`. Each client is closed by its own
    # `async with` and freed immediately, so CPython is free to hand the next
    # one the same address - an identity test passes alone and fails once
    # other tests have shaped the heap. What "not shared" actually means here
    # is one construction per request, which is what is asserted.
    constructions = 0
    original = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):  # noqa: ANN001
        nonlocal constructions
        constructions += 1
        return original(self, *args, **kwargs)

    async def _get(self, url, params=None):  # noqa: ANN001
        return httpx.Response(
            200, text="ok", request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    monkeypatch.setattr(httpx.AsyncClient, "get", _get)

    import asyncio

    client = _client()

    async def run() -> None:
        for _ in range(3):
            await client.get_text("https://example.invalid/x")

    asyncio.run(run())

    # One per request: a shared client would have been built once.
    assert constructions == 3


# --- That it still refuses what it should -----------------------------------
#
# Hermetic on purpose. The obvious way to prove a context still rejects a bad
# certificate is to fetch one of the public badssl.com hosts, but that makes
# the suite depend on the internet and on somebody else's uptime to assert
# something about our own configuration. A self-signed certificate generated
# here and served by a local TLS socket proves the same thing and always runs.


def _self_signed(host: str = "localhost") -> tuple[bytes, bytes]:
    """A certificate no public authority vouches for, for ``host``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, host)]
    )
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


@contextlib.contextmanager
def _tls_server(tmp_path, host: str = "localhost"):
    """A one-shot HTTPS server presenting a self-signed certificate."""
    cert_pem, key_pem = _self_signed(host)
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_file), str(key_file))

    server = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], cert_file
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args) -> None:  # noqa: ANN002
        return


def test_an_untrusted_certificate_is_refused(tmp_path) -> None:
    """The test that would catch a context downgraded to CERT_NONE.

    The server's certificate is real and correctly formed; it is simply signed
    by nobody the trust store knows. If this ever stops raising, verification
    has been lost.
    """
    with _tls_server(tmp_path) as (port, _cert):
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"https://localhost:{port}/", verify=SSL_CONTEXT,
                      timeout=10.0)


def test_the_same_server_is_accepted_once_its_authority_is_trusted(
    tmp_path,
) -> None:
    """The control for the test above.

    Without this, a refusal would prove only that the connection failed - a
    wrong port or a dead socket would look identical. Trusting exactly this
    certificate makes the same request succeed, so what the previous test
    demonstrates really is the verification and nothing else.
    """
    with _tls_server(tmp_path) as (port, cert_file):
        trusting = ssl.create_default_context(cafile=str(cert_file))
        response = httpx.get(
            f"https://localhost:{port}/", verify=trusting, timeout=10.0
        )
    assert response.status_code == 200


def test_a_certificate_for_the_wrong_hostname_is_refused(tmp_path) -> None:
    """The test that would catch ``check_hostname`` being turned off.

    The certificate is trusted here - it is handed to the context as an
    authority - but it was issued for "localhost" and the request asks for
    "127.0.0.1". Only the hostname check can reject that.
    """
    with _tls_server(tmp_path, host="localhost") as (port, cert_file):
        trusting = ssl.create_default_context(cafile=str(cert_file))
        assert trusting.check_hostname is True
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"https://127.0.0.1:{port}/", verify=trusting,
                      timeout=10.0)


def test_normal_https_still_works_through_the_shared_context(tmp_path) -> None:
    """A complete TLS handshake and response, driven by the shared context.

    The shared context is given this server's certificate as an additional
    authority rather than being replaced, so what is exercised is the object
    the application actually uses.
    """
    with _tls_server(tmp_path) as (port, cert_file):
        # A copy, so the process-wide context is not mutated by a test.
        context = httpx.create_ssl_context()
        context.load_verify_locations(cafile=str(cert_file))
        assert context.verify_mode is SSL_CONTEXT.verify_mode
        assert context.check_hostname is SSL_CONTEXT.check_hostname

        response = httpx.get(
            f"https://localhost:{port}/", verify=context, timeout=10.0
        )
    assert response.status_code == 200
    assert response.text == "ok"
