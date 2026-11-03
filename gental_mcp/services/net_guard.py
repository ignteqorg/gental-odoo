"""Outbound HTTP to URLs chosen by others (client metadata documents, webhooks), guarded against SSRF.

Only ``https`` URLs without user-info whose host resolves to public (global) addresses only are fetched.
The connection goes to the address that was checked (no second DNS lookup, so no DNS rebinding), with
TLS still verified against the host name. Redirects and proxies are never used, and a watchdog shuts the
sockets down at the deadline, so the whole fetch (handshake and headers included) is bounded. Errors
returned to callers are generic; the detail is logged, with the URL cut to ``scheme://host[:port]``: a
path or query (a webhook URL's token, for example) never reaches the log.
"""
import ipaddress
import logging
import socket
import threading
import time
from urllib.parse import urlsplit

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool

_logger = logging.getLogger(__name__)

BLOCKED_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64: embeds any IPv4 address
    ipaddress.ip_network("64:ff9b:1::/48"),  # local-use NAT64
    ipaddress.ip_network("2002::/16"),  # 6to4: embeds any IPv4 address
)
NOT_ALLOWED = "URL is not allowed"
FETCH_FAILED = "could not fetch the URL"
SEND_FAILED = "could not reach the URL"
POST_MAX_RESPONSE_BYTES = 65536


def _is_global(address):
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in BLOCKED_NETWORKS):
            return False
    return ip.is_global


def log_url(url):
    """``url`` as logs may show it: scheme, host and port only."""
    try:
        parts = urlsplit(url or "")
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "<invalid URL>"
    return f"{parts.scheme}://{_host_literal(host) if host else ''}{port}"


def _redact(text, url):
    """``text`` (an error message) without the path and query of ``url``, which urllib3 repeats in its errors."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return text
    secret = parts.path + (f"?{parts.query}" if parts.query else "")
    if len(secret) > 1:
        text = text.replace(secret, "/…")
    if parts.query:
        text = text.replace(parts.query, "…")
    return text


def _deny(url, detail, message=NOT_ALLOWED):
    _logger.info("net_guard refused %s: %s", log_url(url), _redact(str(detail), url))
    raise ValueError(message)


def _resolve(url):
    """Check ``url`` and return ``(parts, port, addresses)``; every resolved address is global."""
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError as exc:
        _deny(url, f"invalid URL: {exc}")
    if parts.scheme != "https":
        _deny(url, "not https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        _deny(url, "user info in URL")
    host = parts.hostname
    if not host:
        _deny(url, "no host")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        _deny(url, f"host does not resolve: {exc}")
    addresses = [info[4][0] for info in infos]
    if not addresses:
        _deny(url, "host does not resolve")
    for address in addresses:
        if not _is_global(address):
            _deny(url, f"host resolves to a non-public address {address}")
    return parts, port, addresses


def check_url(url: str) -> None:
    """Raise ``ValueError`` unless ``url`` is https, has no user-info and resolves only to global IPs."""
    _resolve(url)


class PinnedHostAdapter(HTTPAdapter):
    """Connects to an IP literal while doing SNI and certificate checks for ``hostname``.

    Every socket it opens is tracked so ``expire()`` can shut them down: that is the overall deadline,
    which also covers the TLS handshake and the status line and headers (``timeout`` is only per recv).
    """

    def __init__(self, hostname):
        self.hostname = hostname
        self._lock = threading.Lock()
        self._sockets = []  # duplicates of the connection sockets, safe to shut down later
        self.expired = False
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["server_hostname"] = self.hostname
        kwargs["assert_hostname"] = self.hostname
        super().init_poolmanager(*args, **kwargs)
        adapter = self

        class TrackedConnection(HTTPSConnection):
            def _new_conn(self):
                sock = super()._new_conn()
                adapter._track(sock)
                return sock

        class TrackedPool(HTTPSConnectionPool):
            ConnectionCls = TrackedConnection

        self.poolmanager.pool_classes_by_scheme = {**self.poolmanager.pool_classes_by_scheme, "https": TrackedPool}

    def _track(self, sock):
        # A duplicate keeps the socket alive after TLS wrapping detaches the original object.
        dup = sock.dup()
        with self._lock:
            self._sockets.append(dup)
            expired = self.expired
        if expired:
            _shutdown(dup)

    def expire(self):
        with self._lock:
            self.expired = True
            sockets = list(self._sockets)
        for sock in sockets:
            _shutdown(sock)

    def close(self):
        super().close()
        with self._lock:
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            sock.close()


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _pinned_url(parts, port, address):
    return parts._replace(netloc=f"{_host_literal(address)}:{port}", fragment="").geturl()


def _host_literal(host):
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return host
    return f"[{ip}]" if ip.version == 6 else str(ip)


def _ordered(addresses):
    """Unique addresses, IPv4 first."""
    unique = list(dict.fromkeys(addresses))
    return sorted(unique, key=lambda a: ":" in a)


def _connect_failed(exc):
    """Whether ``exc`` happened while opening the connection, before anything was sent."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    return isinstance(reason, (urllib3.exceptions.NewConnectionError, urllib3.exceptions.ConnectTimeoutError))


class _Exchange:
    """One guarded request: ``url`` checked, sockets pinned to the checked addresses, a total deadline."""

    def __init__(self, url, timeout):
        self.url = url
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.parts, self.port, self.addresses = _resolve(url)
        host = _host_literal(self.parts.hostname)
        self.host_header = host if self.port == 443 else f"{host}:{self.port}"
        self.adapter = PinnedHostAdapter(self.parts.hostname)
        self.session = requests.Session()
        self.session.trust_env = False  # no proxies or netrc from the environment
        self.session.mount("https://", self.adapter)
        self.watchdog = threading.Timer(timeout, self.adapter.expire)
        self.watchdog.daemon = True
        self.watchdog.start()
        self.response = None

    def expired(self):
        return self.adapter.expired or time.monotonic() > self.deadline

    def send(self, method, headers, connect_errors_only=False, **kwargs):
        """Send to each checked address in turn, IPv4 first, until one connects; return the response.

        With ``connect_errors_only`` the next address is tried only when the connection could not be
        opened, never after the request may have gone out (a POST must not be sent twice by us).
        """
        for address in _ordered(self.addresses):
            try:
                self.response = getattr(self.session, method)(
                    _pinned_url(self.parts, self.port, address),
                    headers={**headers, "Host": self.host_header},
                    timeout=self.timeout, allow_redirects=False, stream=True, proxies={}, **kwargs,
                )
                return self.response
            except requests.ConnectionError as exc:
                if self.expired() or (connect_errors_only and not _connect_failed(exc)):
                    raise
                _logger.info(
                    "net_guard could not connect to %s for %s: %s", address, log_url(self.url),
                    _redact(repr(exc), self.url),
                )
        return None

    def failure_reason(self, exc):
        # Past the deadline any failure is the deadline's: the watchdog and a server error can race.
        return "deadline exceeded" if self.expired() else f"request failed: {exc!r}"

    def close(self):
        self.watchdog.cancel()
        if self.response is not None:
            self.response.close()
        self.session.close()


def safe_get(url: str, timeout: float = 5, max_bytes: int = 65536) -> bytes:
    """GET ``url`` after ``check_url``: no redirects, a 200 answer of at most ``max_bytes``, else ``ValueError``.

    ``timeout`` bounds each socket operation and also the whole exchange (connect, TLS, headers, body).
    Each checked address is tried in turn, IPv4 first, until one connects.
    """
    exchange = _Exchange(url, timeout)
    try:
        resp = exchange.send("get", {"Accept": "application/json"})
        if resp is None:
            _deny(url, "no address connected", FETCH_FAILED)
        if resp.status_code != 200:
            _deny(url, f"HTTP status {resp.status_code}", FETCH_FAILED)
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            _deny(url, f"Content-Length {length} over {max_bytes}", FETCH_FAILED)
        body = bytearray()
        for chunk in resp.iter_content(chunk_size=4096):
            body += chunk
            if len(body) > max_bytes:
                _deny(url, f"body over {max_bytes} bytes", FETCH_FAILED)
            if time.monotonic() > exchange.deadline:
                _deny(url, "deadline exceeded", FETCH_FAILED)
        if exchange.adapter.expired:
            _deny(url, "deadline exceeded", FETCH_FAILED)
        return bytes(body)
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as exc:
        _deny(url, exchange.failure_reason(exc), FETCH_FAILED)
    finally:
        exchange.close()


def safe_post(url: str, data: bytes, headers: dict, timeout: float) -> requests.Response:
    """POST ``data`` to ``url`` after ``check_url``, the same way ``safe_get`` fetches: pinned address, no
    redirects (a 3xx comes back as is), no proxies, ``timeout`` bounding the whole exchange.

    Any status is returned; the caller decides what counts as success. At most
    ``POST_MAX_RESPONSE_BYTES`` of the answer are read into ``content``, and a body that is slow or cut
    after the status line arrived is kept as read so far. ``ValueError`` (generic message) when the URL is
    refused or no answer arrives.
    """
    exchange = _Exchange(url, timeout)
    try:
        resp = exchange.send("post", headers, connect_errors_only=True, data=data)
        if resp is None:
            _deny(url, "no address connected", SEND_FAILED)
        body = bytearray()
        try:
            for chunk in resp.iter_content(chunk_size=4096):
                body += chunk
                if len(body) >= POST_MAX_RESPONSE_BYTES or exchange.expired():
                    break
        except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as exc:
            _logger.info(
                "net_guard stopped reading the answer of %s: %s", log_url(url),
                _redact(exchange.failure_reason(exc), url),
            )
        resp.close()  # before marking the body consumed, or the raw stream would stay open
        resp._content = bytes(body[:POST_MAX_RESPONSE_BYTES])
        resp._content_consumed = True
        return resp
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as exc:
        _deny(url, exchange.failure_reason(exc), SEND_FAILED)
    finally:
        exchange.close()
