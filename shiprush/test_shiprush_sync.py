"""Offline test suite -- no network, no GCP credentials required.

Follows Source Pipeline Standards section 10. Covers the parts that are
CONFIRMED and source-agnostic today: the raw-ingestion envelope, the flat GCS
path convention, config parsing rules, and resource-registry validation.

Pagination / record-extraction / auth-header-on-the-wire tests are the ones the
standards care most about (section 10), but they are deferred here because the
ShipRush SOAP operation names, request XML, response wrapper element, and paging
scheme are unconfirmed (see resources.py banner / README). They are marked xfail
so they show up as explicit, tracked gaps rather than silently missing -- fill
them in against the API guide or a live sample, then remove the xfail.
"""
from __future__ import annotations

import json
import os

import pytest

from client import ShipRushAPIError, ShipRushClient
from config import Config
from resources import get_resource
from writer import LocalWriter, build_rows


# --------------------------------------------------------------------------- #
# Writer / envelope (standards section 7 & 8)
# --------------------------------------------------------------------------- #
def test_build_rows_exact_envelope_shape():
    rows = build_rows([{"id": 1, "name": "widget"}], ingestion_time="2026-07-20T00:00:00+00:00")
    assert len(rows) == 1
    row = rows[0]
    # Exactly the three mandated fields, nothing more, nothing flattened.
    assert set(row.keys()) == {"raw_payload", "_ingestion_time", "_payload_size_bytes"}
    assert json.loads(row["raw_payload"]) == {"id": 1, "name": "widget"}
    assert row["_ingestion_time"] == "2026-07-20T00:00:00+00:00"
    assert row["_payload_size_bytes"] == len(row["raw_payload"].encode("utf-8"))


def test_build_rows_compact_json_and_unicode():
    rows = build_rows([{"city": "Montréal"}], ingestion_time="t")
    # Compact separators, non-ASCII preserved (ensure_ascii=False).
    assert rows[0]["raw_payload"] == '{"city":"Montréal"}'
    assert rows[0]["_payload_size_bytes"] == len('{"city":"Montréal"}'.encode("utf-8"))


def test_local_writer_flat_path_convention(tmp_path):
    writer = LocalWriter(str(tmp_path), prefix="shiprush")
    rows = build_rows([{"id": 1}], ingestion_time="t")
    dest, count = writer.write(rows, endpoint="orders", epoch=1700000000)
    assert count == 1
    # {prefix}/{endpoint}_{epoch}.ndjson -- flat, not date-partitioned (section 8).
    assert dest == os.path.join(str(tmp_path), "shiprush", "orders_1700000000.ndjson")
    with open(dest, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["_ingestion_time"] == "t"


def test_local_writer_skips_empty(tmp_path):
    writer = LocalWriter(str(tmp_path), prefix="shiprush")
    dest, count = writer.write([], endpoint="orders", epoch=1)
    assert (dest, count) == ("", 0)


# --------------------------------------------------------------------------- #
# Config (standards section 3 & 13)
# --------------------------------------------------------------------------- #
def _clear_env(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("SHIPRUSH_", "GCS_", "GCP_", "LOCAL_OUTPUT_DIR")):
            monkeypatch.delenv(var, raising=False)


def test_from_env_requires_both_tokens(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SHIPRUSH_ENDPOINT", "orders")
    monkeypatch.setenv("LOCAL_OUTPUT_DIR", "/tmp/out")
    monkeypatch.setenv("SHIPRUSH_DEVELOPER_TOKEN", "dev")
    # UserToken missing -> loud failure.
    with pytest.raises(RuntimeError, match="SHIPRUSH_USER_TOKEN"):
        Config.from_env()


def test_from_env_requires_bucket_or_local(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SHIPRUSH_ENDPOINT", "orders")
    monkeypatch.setenv("SHIPRUSH_DEVELOPER_TOKEN", "dev")
    monkeypatch.setenv("SHIPRUSH_USER_TOKEN", "usr")
    with pytest.raises(RuntimeError, match="GCS_BUCKET .* LOCAL_OUTPUT_DIR"):
        Config.from_env()


def test_from_env_happy_path_and_watermark_defaults(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SHIPRUSH_ENDPOINT", "orders")
    monkeypatch.setenv("SHIPRUSH_DEVELOPER_TOKEN", "dev")
    monkeypatch.setenv("SHIPRUSH_USER_TOKEN", "usr")
    monkeypatch.setenv("GCS_BUCKET", "my-bucket")
    monkeypatch.setenv("GCP_STATE_BUCKET", "state-bucket")
    cfg = Config.from_env()
    assert cfg.developer_token == "dev"
    assert cfg.user_token == "usr"
    assert cfg.gcs_bucket == "my-bucket"
    assert cfg.gcs_prefix == "shiprush"
    # last_run_file derived from endpoint when a state bucket is configured.
    assert cfg.last_run_file == "shiprush_last_run/orders.txt"


def test_from_env_no_state_bucket_means_no_watermark_file(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SHIPRUSH_ENDPOINT", "orders")
    monkeypatch.setenv("SHIPRUSH_DEVELOPER_TOKEN", "dev")
    monkeypatch.setenv("SHIPRUSH_USER_TOKEN", "usr")
    monkeypatch.setenv("LOCAL_OUTPUT_DIR", "/tmp/out")
    cfg = Config.from_env()
    assert cfg.state_bucket is None
    assert cfg.last_run_file is None


# --------------------------------------------------------------------------- #
# Resource registry (standards section 4)
# --------------------------------------------------------------------------- #
def test_unknown_endpoint_raises_with_clear_message():
    with pytest.raises(ValueError, match="Unknown SHIPRUSH_ENDPOINT 'bogus'"):
        get_resource("bogus")


# --------------------------------------------------------------------------- #
# Client auth wiring (CONFIRMED: two-token headers, application/xml)
# --------------------------------------------------------------------------- #
def test_client_sends_both_tokens_and_xml_content_type():
    import httpx

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, text="<ok/>")

    with ShipRushClient(
        "dev-tok",
        "usr-tok",
        base_url="https://example.test",
        shipping_token="ship-tok",
        transport=httpx.MockTransport(handler),
    ) as client:
        client._request("POST", "/whatever", content="<req/>")

    h = captured["headers"]
    assert h["DeveloperToken"] == "dev-tok"
    assert h["UserToken"] == "usr-tok"
    assert h["ShippingToken"] == "ship-tok"
    assert h["Content-Type"] == "application/xml"


def test_client_raises_on_non_retryable_status():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    with ShipRushClient("d", "u", base_url="https://example.test", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ShipRushAPIError, match="400"):
            client._request("POST", "/x", content="<req/>")


# --------------------------------------------------------------------------- #
# Deferred, tracked gaps -- unconfirmed against the ShipRush API guide.
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(reason="ShipRush SOAP pagination/record extraction unconfirmed -- blocked on API guide", strict=True)
def test_pagination_against_documented_scheme():
    # TODO(blocked-on-docs): assert the exact request the client sends per page
    # (operation, XML body, paging param/token) matches the API guide, and that
    # records are extracted from the confirmed response wrapper element. This is
    # the regression test standards section 10 says would catch a wrong scheme.
    raise NotImplementedError
