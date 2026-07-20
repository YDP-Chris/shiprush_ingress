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

Request/paging schema is CONFIRMED from the kit's XSD: the request carries
ItemsPerPage + PageNumber (1-based) and a Modified* date window; the response
carries a DataPaging <HasMoreData> flag and wraps records in a container element
whose DIRECT children are the records (see resources.py). paginate() loops pages
until HasMoreData is false. The per-resource request bodies live in resources.py
so the transport here stays source-agnostic.
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


def _find_container(root: ET.Element, container: str | None) -> ET.Element | None:
    """Find the first element whose localname is `container` (anywhere in tree)."""
    if not container:
        return None
    for el in root.iter():
        if _localname(el.tag) == container:
            return el
    return None


def _extract_records(root: ET.Element, container: str | None) -> list[dict[str, Any]]:
    """Extract records as the DIRECT children of the confirmed container element.

    ShipRush wraps a page of records in a container element (ShipTransactions,
    CatalogItems, ...); each record is a direct child of that container. We take
    only direct children -- crucially NOT any-descendant -- because a record type
    such as TShipTransaction also appears *nested* inside a shipment, and an
    any-descendant scan would double-count those nested copies (the wrong-list
    trap standards section 5 warns about).

    Mirrors standards section 5: a missing/wrong container yields zero records
    (visible in the run summary) rather than a silent wrong guess.
    """
    parent = _find_container(root, container)
    if parent is None:
        return []
    records = []
    for child in list(parent):
        value = _xml_to_dict(child)
        if isinstance(value, dict):
            records.append(value)
    return records


def _response_has_more(root: ET.Element) -> bool:
    """Read the DataPaging <HasMoreData> flag from the response's <Paging> block.

    Confirmed from the XSD: paged responses carry a <Paging> (DataPaging) block
    with a <HasMoreData> boolean. We read HasMoreData only from *inside* a Paging
    element so an unrelated element of that name in a record payload can't be
    mistaken for the paging flag. Absence of the flag is treated as "no more" so
    the loop terminates rather than spinning forever.
    """
    for paging in root.iter():
        if _localname(paging.tag) != "Paging":
            continue
        for child in paging.iter():
            if _localname(child.tag) == "HasMoreData":
                return (child.text or "").strip().lower() == "true"
    return False


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
        max_pages: int = 10_000,
        transport=None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required (SHIPRUSH_BASE_URL)")
        if not any((developer_token, user_token, shipping_token, session_token)):
            raise ValueError("at least one ShipRush token is required")
        self._max_retries = max_retries
        self._max_pages = max_pages
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
        resource,
        *,
        since: str,
        until: str,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Yield one dict per record across all pages of a ShipRush read call.

        For each page the resource builds the XML request body (carrying the
        page number, page size and Modified* date window); we POST it, extract
        the container's direct children, and advance to the next page while the
        response's <HasMoreData> flag is true. Non-paged resources
        (resource.paged is False) make a single call.

        Confirmed from the XSD: request paging is ItemsPerPage + PageNumber
        (1-based); response paging is DataPaging/<HasMoreData>. Records are the
        direct children of resource.container.
        """
        page = 1
        while True:
            request_xml = resource.build_request(
                page=page, page_size=page_size, since=since, until=until
            )
            root = self.call(resource.path, request_xml)
            records = _extract_records(root, resource.container)
            for record in records:
                yield record

            if not resource.paged:
                return
            # Stop when the server says there is no more data. Also stop on an
            # empty page as a belt-and-suspenders guard against a missing flag.
            if not records or not _response_has_more(root):
                return
            # Hard safety cap: a server that ignores PageNumber and keeps
            # returning HasMoreData=true would otherwise loop forever (re-yielding
            # the same page until OOM). Fail loudly instead so it is investigated.
            if page >= self._max_pages:
                raise ShipRushAPIError(
                    f"{resource.path}: pagination exceeded max_pages={self._max_pages} "
                    f"(server may be ignoring PageNumber); aborting to avoid an infinite loop."
                )
            page += 1
