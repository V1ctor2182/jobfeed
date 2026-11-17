"""Resolve dashboard requests to the HTTP loopback address used by diagnostics."""

from ipaddress import IPv6Address, ip_address

from fastapi import Request

from jobfeed.onboarding_batch_setup import loopback_origin

_PUBLIC_ORIGINS = {
    "http://jobfeed.localhost:1355",
    "https://jobfeed.localhost:1355",
}


def _socket_origin(request: Request) -> str | None:
    server = request.scope.get("server")
    if server is None or request.client is None:
        return None
    host, port = server
    try:
        address = ip_address(host)
        if not address.is_loopback or not ip_address(request.client.host).is_loopback:
            return None
    except ValueError:
        return None
    authority = (
        f"[{host}]:{port}" if isinstance(address, IPv6Address) else f"{host}:{port}"
    )
    return loopback_origin(f"http://{authority}")


def _proxy_origin(request: Request, browser_origin: str | None) -> str | None:
    headers = request.headers
    if any(
        len(headers.getlist(header)) != 1
        for header in ("host", "x-forwarded-host", "x-forwarded-proto")
    ):
        return None
    forwarded_clients = headers.getlist("x-forwarded-for")
    if len(forwarded_clients) > 1 or any("," in value for value in forwarded_clients):
        return None
    public = f"{headers['x-forwarded-proto']}://{headers['x-forwarded-host']}"
    if public not in _PUBLIC_ORIGINS:
        return None
    if browser_origin is not None and (
        len(headers.getlist("origin")) != 1 or browser_origin != public
    ):
        return None
    backend = _socket_origin(request)
    if backend is None or headers["host"] not in {
        headers["x-forwarded-host"],
        backend.removeprefix("http://"),
    }:
        return None
    return backend


def dashboard_loopback_origin(
    request: Request, *, browser_origin: str | None = None
) -> str | None:
    """Keep the fixture HTTP-only while accepting the verified local proxy UI.

    Args:
        request: Incoming dashboard request.
        browser_origin: Origin header sent by the browser, if any.

    Returns:
        The loopback origin the request came through, or None when it is
        neither a direct loopback request nor the verified proxy.
    """
    direct = loopback_origin(str(request.base_url))
    if direct is not None and (browser_origin is None or browser_origin == direct):
        return direct
    return _proxy_origin(request, browser_origin)
