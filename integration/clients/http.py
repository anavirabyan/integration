"""Shared HTTP layer: timeouts, retries with exponential backoff, typed errors.

Retry policy (technical doc, section 10):
  * 429 / 5xx / timeout / connection error -> retry 1, 2, 4, 8, 16 s (5 attempts)
  * 400 / 404 / 422 and other 4xx         -> no retry, raise ApiError
  * non-idempotent calls (POST that creates something) are retried only on 429,
    because a 5xx after a POST may mean the object was already created.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
BACKOFF = [1, 2, 4, 8, 16]
IDEMPOTENT = {"GET", "PUT", "DELETE", "HEAD"}


class ApiError(Exception):
    def __init__(self, service: str, status: int | None, message: str, body: Any = None):
        super().__init__(f"{service} {status}: {message}")
        self.service = service
        self.status = status
        self.body = body

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status in RETRY_STATUS


class HttpClient:
    def __init__(self, service: str, base_url: str, timeout: int = 30,
                 auth_header: Callable[[], dict] | None = None,
                 basic_auth: tuple[str, str] | None = None,
                 on_unauthorized: Callable[[], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.service = service
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.auth_header = auth_header
        self.basic_auth = basic_auth
        self.on_unauthorized = on_unauthorized
        self.sleep = sleep
        self.session = requests.Session()

    def request(self, method: str, path: str, *, idempotent: bool | None = None,
                **kw) -> requests.Response:
        method = method.upper()
        if idempotent is None:
            idempotent = method in IDEMPOTENT
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        base_headers = dict(kw.pop("headers", None) or {})
        refreshed = False
        last_exc: Exception | None = None

        for attempt, delay in enumerate([0] + BACKOFF):
            if delay:
                self.sleep(delay)
            headers = dict(base_headers)
            if self.auth_header:
                headers.update(self.auth_header())
            try:
                resp = self.session.request(method, url, headers=headers, auth=self.basic_auth,
                                            timeout=self.timeout, **kw)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                if not idempotent:
                    raise ApiError(self.service, None, f"{type(exc).__name__} on non-idempotent call") from exc
                log.warning("%s %s %s: %s (attempt %d)", self.service, method, path, exc, attempt + 1)
                continue

            # Expired token: refresh once and repeat immediately.
            if resp.status_code == 401 and self.on_unauthorized and not refreshed:
                self.on_unauthorized()
                refreshed = True
                continue
            if resp.status_code < 400:
                return resp
            retry = resp.status_code == 429 or (idempotent and resp.status_code in RETRY_STATUS)
            if not retry:
                raise ApiError(self.service, resp.status_code, resp.text[:500], _safe_json(resp))
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                self.sleep(min(int(retry_after), 60))
            log.warning("%s %s %s -> %s (attempt %d)", self.service, method, path, resp.status_code, attempt + 1)
            last_exc = ApiError(self.service, resp.status_code, resp.text[:500], _safe_json(resp))

        if isinstance(last_exc, ApiError):
            raise last_exc
        raise ApiError(self.service, None, f"giving up: {last_exc}")

    def get(self, path: str, **kw) -> Any:
        return _safe_json(self.request("GET", path, **kw))

    def post(self, path: str, **kw) -> Any:
        return _safe_json(self.request("POST", path, **kw))

    def put(self, path: str, **kw) -> Any:
        return _safe_json(self.request("PUT", path, **kw))


def _safe_json(resp: requests.Response) -> Any:
    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"_text": resp.text[:500]}
