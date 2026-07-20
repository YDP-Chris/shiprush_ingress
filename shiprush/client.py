"""Thin, resilient client for the ShipRush Web Non-Visual API.

Per Source Pipeline Standards section 6: implement the source's DOCUMENTED
pagination scheme exactly -- do not auto-detect across generic guesses. The
retry/backoff below is a generic exponential-backoff + Retry-After strategy
because no published ShipRush rate limit was found; swap it for a documented
scheme if one turns up (the way Brightpearl throttles to its 125-calls/minute).

CONFIRMED (from ShipRush developer docs / support):
  - Auth is two tokens sent as HTTP headers: DeveloperToken + UserToken
    (optionally ShippingToken + SessionToken).
  - Requests/responses are XML: Content-Type "application/xml".
  - It is a SOAP-style web service (POST an XML request body per operation).

UNCONFIRMED (blocked on the API guide, which 403s automated fetchers and needs
an enabled DeveloperToken to exercise): the operation names, the XML request
body schema per operation, the response element that wraps the record list, and
the pagination mechanism. paginate() therefore raises NotImplementedError until
those are pinned down against the docs or one live response -- guessing them is
exactly the failure mode standards sections 5 & 6 warn against.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx  # injectable transport (httpx.MockTransport) enables offline tests -- standards section 6

logger = logging.getLogger(__name__)


class ShipRushAPIError(RuntimeError):
    """Raised when the ShipRush API returns a non-retryable error."""


class ShipRushClient:
    def __init__(
        self,
        developer_token: str,
        user_token: str,
        base_url: str,
        *,
        shipping_token: str | None = None,
        session_token: str | None = None,
        timeout_seconds: int = 30,
        max_retries: int = 5,
        transport=None,
    ) -> None:
        if not developer_token:
            raise ValueError("developer_token is required")
        if not user_token:
            raise ValueError("user_token is required")
        if not base_url:
            raise ValueError("base_url is required (SHIPRUSH_BASE_URL)")
        self._max_retries = max_retries
        # eCommerce-style auth: both tokens sent unconditionally as headers,
        # plus the optional session-scoped tokens when available.
        headers = {
            "DeveloperToken": developer_token,
            "UserToken": user_token,
            "Content-Type": "application/xml",
            "Accept": "application/xml",
            "User-Agent": "shiprush-sync/0.1 (+https://github.com/ydp-chris/shiprush_ingress)",
        }
        if shipping_token:
            headers["ShippingToken"] = shipping_token
        if session_token:
            headers["SessionToken"] = session_token
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers=headers,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, *, params=None, content: str | bytes | None = None) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.request(method, path, params=params, content=content)
            except httpx.TransportError as exc:
                if attempt > self._max_retries:
                    raise ShipRushAPIError(f"Network error after {attempt} attempts: {exc}") from exc
                self._sleep_backoff(attempt)
                continue
            if resp.status_code < 400:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self._max_retries:
                    raise ShipRushAPIError(
                        f"{resp.status_code} from {path} after {attempt} attempts: {resp.text[:500]}"
                    )
                self._sleep_backoff(attempt, retry_after=resp.headers.get("Retry-After"))
                continue
            raise ShipRushAPIError(f"{resp.status_code} from {path}: {resp.text[:500]}")

    def _sleep_backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 2.0 * attempt
        else:
            delay = min(2.0 ** attempt, 60.0)
        logger.warning("Backing off %.1fs (attempt %d)", delay, attempt)
        time.sleep(delay)

    def paginate(
        self,
        path: str,
        *,
        params=None,
        page_size: int = 100,
        record_key: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield one dict per record across all pages of a ShipRush list operation.

        TODO(blocked-on-docs): implement ShipRush's DOCUMENTED request/pagination
        scheme here -- the SOAP operation's XML request body, the response element
        that wraps the record list (record_key), how the next page is requested
        (page counter? continuation token? date window?), and XML->dict parsing
        of each record. Confirm against the Web Non-Visual API Guide or one live
        response before shipping (standards sections 5 & 6). Do not auto-detect.
        """
        raise NotImplementedError(
            "ShipRush pagination/record extraction is unconfirmed. Pin the SOAP "
            "operation, request XML, response wrapper element, and paging scheme "
            "against the Web Non-Visual API Guide (or a live sample) before "
            "implementing -- see resources.py banner and README open questions."
        )
