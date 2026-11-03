"""URL comparison helpers. Pure Python; no Odoo imports."""
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORTS = {"https": 443, "http": 80}


def normalize_url(url):
    """``url`` as it is compared: lowercase scheme and host, no default port, no trailing slash.

    A URL with user info is returned unchanged (stripped), so it never equals a server URL.
    """
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return url
    if parts.username is not None or parts.password is not None or not parts.hostname:
        return url
    scheme = parts.scheme.lower()
    host = parts.hostname  # lowercase
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and DEFAULT_PORTS.get(scheme) != port:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, parts.fragment))
