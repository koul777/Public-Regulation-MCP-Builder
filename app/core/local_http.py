"""HTTP helpers for model servers (Ollama, llama.cpp, OpenAI-compatible, AI review providers).

``urllib.request.urlopen`` uses the process-wide default opener, which honors the
``HTTP(S)_PROXY`` environment variables and the Windows system proxy. A local model
call carries the user question and retrieved regulation text, so it must reach the
loopback socket directly and must never be re-routed by a proxy or a redirect.
``local_urlopen`` does that.

``provider_urlopen`` is for the configurable AI review provider. A loopback endpoint is
treated like ``local_urlopen``. A remote endpoint keeps the proxy settings, because an
institution may require a proxy for outbound traffic, but never follows a redirect:
urllib re-sends the ``Authorization``/API-key headers to the redirect target.

Callers are still responsible for validating that a local endpoint is a loopback host;
this module only controls how the request travels.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, OpenerDirector, ProxyHandler, Request, build_opener

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class _RefuseRedirect(HTTPRedirectHandler):
    """Fail closed on every redirect instead of following it to another host."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        raise HTTPError(
            req.full_url,
            code,
            "Redirects are not allowed for model API calls",
            headers,
            fp,
        )


def build_local_opener() -> OpenerDirector:
    """Return an opener that ignores every proxy setting and rejects redirects."""
    # An explicit empty mapping disables environment and system proxy discovery.
    return build_opener(ProxyHandler({}), _RefuseRedirect())


@lru_cache(maxsize=1)
def _shared_opener() -> OpenerDirector:
    # Like the stdlib default opener, build once: constructing the HTTPS handler
    # creates an SSL context, which is wasteful to repeat for every model call.
    return build_local_opener()


def local_urlopen(request: Request | str, timeout: float) -> Any:
    """Open ``request`` without any proxy and without following redirects.

    Drop-in replacement for ``urllib.request.urlopen(request, timeout=...)``: the
    result is a context manager and a 3xx response raises ``HTTPError``. The stdlib
    opener would also open ``file:`` and ``ftp:`` URLs, so anything but http(s) is refused.
    """
    _require_http_url(request)
    return _shared_opener().open(request, timeout=timeout)


def provider_urlopen(request: Request, timeout: float) -> Any:
    """Open an AI review provider request.

    Loopback hosts behave like ``local_urlopen``. Remote hosts keep proxy discovery
    (the opener is built per call, so a changed proxy setting is picked up) and still
    refuse redirects so credentials never follow a redirect to another host.
    """
    url = _require_http_url(request)
    if (urlparse(url).hostname or "").lower() in _LOOPBACK_HOSTS:
        return _shared_opener().open(request, timeout=timeout)
    return build_opener(_RefuseRedirect()).open(request, timeout=timeout)


def _require_http_url(request: Request | str) -> str:
    url = request if isinstance(request, str) else request.full_url
    if urlparse(url).scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError("Model API calls support only http and https URLs.")
    return url
