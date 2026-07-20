"""Thin, resilient client for the ShipRush API (XML data-read calls).

Transport is CONFIRMED from the ShipRush SDK (ShipRush.SDK.Transport):
  - POST to {base_url}/{service}.svc/{command}, Content-Type application/xml.
  - Auth via up to four token headers, each sent only when set:
    X-SHIPRUSH-DEVELOPER-TOKEN, X-SHIPRUSH-USER-TOKEN,
    X-SHIPRUSH-SHIPPING-TOKEN,  X-SHIPRUSH-SESSION-TOKEN,
    plus optional X-SHIPRUSH-VERSION. The SDK adds every configured token
    unconditionally, so we do the same.
  - Errors come back as <Error><Message>...</Message></Error>; on HTTP 200 the
    business status is in-band via <IsSuccess> / <Messages>.

STILL UNCONFIRMED (blocked on the kit's XSD / ShipRush.SDK.Proxies): the
GetShipmentsRequest body (filters, paging) and the GetShipmentsResponse wrapper
element. paginate() therefore raises until those are pinned down -- guessing the
request/paging scheme is exactly what standards sections 5 & 6 forbid. The
confirmed building blocks (call(), _xml_to_dict, _extract_records) are
implemented and tested so wiring paginate() is a small, localized change once
the schema is in hand.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator
from xml.etree import ElementTree as ET

import httpx  # injectable transport (httpx.MockTransport) enables offline tests -- standards section 6

logger = logging.getLogger(__name__)


class ShipRushAPIError(RuntimeError):
    """Raised when the ShipRush API returns an error (transport or <Error> body)."""


def _localname(tag: str) -> str:
    """Strip any XML namespace: '{ns}Shipment' -> 'Shipment'."""
    return tag.rsplit("}", 1)[-1]


def _xml_to_dict(element: ET.Element) -> Any:
    """Recursively convert an XML element into JSON-friendly Python.

    Leaf elements -> their text (or "" if empty). Parent elements -> a dict keyed
    by child localname; repeated child names collapse into a list. Namespaces are
    stripped so keys match the ShipRush element names in the docs.
    """
    children = list(element)
    if not children:
        return (element.text or "").strip()
    result: dict[str, Any] = {}
    for child in children:
        key = _localname(child.tag)
        value = _xml_to_dict(child)
        if key in result:
            existing = result[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[key] = [existing, value]
        else:
            result[key] = value
    return result


def _extract_records(root: ET.Element, record_key: str | None) -> list[dict[str, Any]]:
    """Extract records by the confirmed wrapper element localname.

    Mirrors standards section 5: only pull from the explicitly-confirmed
    record_key. A missing/wrong key yields zero records (visible in the run
    summary) rather than a silent wrong guess.
    """
    if not record_key:
        return []
    records = []
    for el in root.iter():
        if _localname(el.tag) == record_key:
            value = _xml_to_dict(el)
            if isinstance(value, dict):
                records.append(value)
    return records


class ShipRushClient:
    def __init__(
        self,
        base_url: str,
        *,
        developer_token: str | None = None,
        user_token: str | None = None,
        shipping_token: str | None = None,
        session_token: str | None = None,
        api_version: str | None = None,
        user_agent: str = "shiprush-sync/0.1 (+https://github.com/ydp-chris/shiprush_ingress)",
        timeout_seconds: int = 60,
        max_retries: int = 5,
        transport=None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required (SHIPRUSH_BASE_URL)")
        if not any((developer_token, user_token, shipping_token, session_token)):
            raise ValueError("at least one ShipRush token is required")
        self._max_retries = max_retries
        # Mirror the SDK: add each token header only when it is set.
        headers = {
            "Content-Type": "application/xml",
            "Accept": "application/xml",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": user_agent,
        }
        if developer_token:
            headers["X-SHIPRUSH-DEVELOPER-TOKEN"] = developer_token
        if user_token:
            headers["X-SHIPRUSH-USER-TOKEN"] = user_token
        if shipping_token:
            headers["X-SHIPRUSH-SHIPPING-TOKEN"] = shipping_token
        if session_token:
            headers["X-SHIPRUSH-SESSION-TOKEN"] = session_token
        if api_version:
            headers["X-SHIPRUSH-VERSION"] = str(api_version)
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

    def _request(self, path: str, *, content: str | bytes) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.post("/" + path.lstrip("/"), content=content)
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

    def call(self, path: str, request_xml: str) -> ET.Element:
        """POST an XML request body and return the parsed response root element.

        Raises ShipRushAPIError on an <Error> envelope or an in-band
        <IsSuccess>false</IsSuccess>.
        """
        resp = self._request(path, content=request_xml.encode("utf-8"))
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise ShipRushAPIError(f"Non-XML response from {path}: {resp.text[:500]}") from exc
        self._raise_for_business_error(path, root)
        return root

    @staticmethod
    def _raise_for_business_error(path: str, root: ET.Element) -> None:
        # Transport-level error envelope: <Error><Message>...</Message></Error>
        if _localname(root.tag) == "Error":
            msg = next((c.text for c in root if _localname(c.tag) == "Message"), None)
            raise ShipRushAPIError(f"{path}: {msg or 'Error'}")
        # In-band business status: <IsSuccess>false</IsSuccess> + <Messages>.
        for child in root.iter():
            if _localname(child.tag) == "IsSuccess":
                if (child.text or "").strip().lower() == "false":
                    messages = [
                        (t.text or "")
                        for m in root.iter()
                        if _localname(m.tag) == "Messages"
                        for t in m.iter()
                        if _localname(t.tag) in ("Text", "Message")
                    ]
                    raise ShipRushAPIError(f"{path}: IsSuccess=false {'; '.join(filter(None, messages))}")
                break

    def paginate(
        self,
        path: str,
        *,
        params=None,
        page_size: int = 100,
        record_key: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield one dict per record across all pages of a ShipRush read call.

        TODO(blocked-on-xsd): build the request XML (e.g. GetShipmentsRequest)
        and paging from the kit's XSD / ShipRush.SDK.Proxies -- filter fields,
        page cursor, and the response wrapper element (record_key). Then this
        becomes: loop -> self.call(path, request_xml) -> _extract_records(root,
        record_key) -> advance the cursor. Do NOT guess the schema (standards
        sections 5 & 6).
        """
        raise NotImplementedError(
            "GetShipmentsRequest/Response schema (filters, paging, record wrapper) "
            "is unconfirmed. Pin it from the kit's XSD or one live shipments/get "
            "response, set Resource.record_key, then implement here using call() + "
            "_extract_records. See resources.py and README open questions."
        )
