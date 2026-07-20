"""Offline test suite -- no network, no GCP credentials required.

Follows Source Pipeline Standards section 10. Covers the parts that are CONFIRMED
(from the ShipRush SDK) and source-agnostic today: the raw-ingestion envelope,
the flat GCS path, config parsing, resource-registry validation, the confirmed
transport (token headers, URL, XML parsing, <Error> handling, record extraction).

The per-page request/paging assertion (section 10's key regression test) is
deferred with xfail because the GetShipmentsRequest/Response schema is
unconfirmed (see resources.py / README) -- fill it in from the XSD or a live
sample, then remove the xfail.
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

from client import ShipRushAPIError, ShipRushClient, _extract_records, _xml_to_dict
from config import Config
from resources import RESOURCES, get_resource
from writer import LocalWriter, build_rows
from xml.etree import ElementTree as ET


# --------------------------------------------------------------------------- #
# Writer / envelope (standards section 7 & 8)
# --------------------------------------------------------------------------- #
def test_build_rows_exact_envelope_shape():
    rows = build_rows([{"id": 1, "name": "widget"}], ingestion_time="2026-07-20T00:00:00+00:00")
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == {"raw_payload", "_ingestion_time", "_payload_size_bytes"}
    assert json.loads(row["raw_payload"]) == {"id": 1, "name": "widget"}
    assert row["_ingestion_time"] == "2026-07-20T00:00:00+00:00"
    assert row["_payload_size_bytes"] == len(row["raw_payload"].encode("utf-8"))


def test_build_rows_compact_json_and_unicode():
    rows = build_rows([{"city": "Montréal"}], ingestion_time="t")
    assert rows[0]["raw_payload"] == '{"city":"Montréal"}'
    assert rows[0]["_payload_size_bytes"] == len('{"city":"Montréal"}'.encode("utf-8"))


def test_local_writer_flat_path_convention(tmp_path):
    writer = LocalWriter(str(tmp_path), prefix="shiprush")
    rows = build_rows([{"id": 1}], ingestion_time="t")
    dest, count = writer.write(rows, endpoint="shipments", epoch=1700000000)
    assert count == 1
    assert dest == os.path.join(str(tmp_path), "shiprush", "shipments_1700000000.ndjson")
    with open(dest, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["_ingestion_time"] == "t"


def test_local_writer_skips_empty(tmp_path):
    writer = LocalWriter(str(tmp_path), prefix="shiprush")
    assert writer.write([], endpoint="shipments", epoch=1) == ("", 0)


# --------------------------------------------------------------------------- #
# Config (standards section 3 & 13)
# --------------------------------------------------------------------------- #
def _clear_env(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("SHIPRUSH_", "GCS_", "GCP_", "LOCAL_OUTPUT_DIR")):
            monkeypatch.delenv(var, raising=False)


def test_from_env_requires_at_least_one_token(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SHIPRUSH_ENDPOINT", "shipments")
    monkeypatch.setenv("LOCAL_OUTPUT_DIR", "/tmp/out")
    with pytest.raises(RuntimeError, match="at least one ShipRush token"):
        Config.from_env()


def test_from_env_requires_bucket_or_local(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SHIPRUSH_ENDPOINT", "shipments")
    monkeypatch.setenv("SHIPRUSH_DEVELOPER_TOKEN", "dev")
    monkeypatch.setenv("SHIPRUSH_USER_TOKEN", "usr")
    with pytest.raises(RuntimeError, match="GCS_BUCKET .* LOCAL_OUTPUT_DIR"):
        Config.from_env()


def test_from_env_happy_path_defaults(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SHIPRUSH_ENDPOINT", "shipments")
    monkeypatch.setenv("SHIPRUSH_DEVELOPER_TOKEN", "dev")
    monkeypatch.setenv("SHIPRUSH_USER_TOKEN", "usr")
    monkeypatch.setenv("GCS_BUCKET", "my-bucket")
    monkeypatch.setenv("GCP_STATE_BUCKET", "state-bucket")
    cfg = Config.from_env()
    assert cfg.developer_token == "dev"
    assert cfg.user_token == "usr"
    assert cfg.base_url == "https://api.my.shiprush.com"  # confirmed production default
    assert cfg.gcs_prefix == "shiprush"
    assert cfg.last_run_file == "shiprush_last_run/shipments.txt"


def test_from_env_base_url_override_strips_slash(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SHIPRUSH_ENDPOINT", "shipments")
    monkeypatch.setenv("SHIPRUSH_SHIPPING_TOKEN", "ship")
    monkeypatch.setenv("LOCAL_OUTPUT_DIR", "/tmp/out")
    monkeypatch.setenv("SHIPRUSH_BASE_URL", "https://sandbox.api.my.shiprush.com/")
    cfg = Config.from_env()
    assert cfg.base_url == "https://sandbox.api.my.shiprush.com"


# --------------------------------------------------------------------------- #
# Resource registry (standards section 4) -- confirmed SDK endpoints
# --------------------------------------------------------------------------- #
def test_unknown_endpoint_raises():
    with pytest.raises(ValueError, match="Unknown SHIPRUSH_ENDPOINT 'bogus'"):
        get_resource("bogus")


def test_shipments_resource_path_matches_sdk():
    assert get_resource("shipments").path == "shipmentservice.svc/shipments/get"


def test_every_resource_has_a_service_and_command_path():
    for r in RESOURCES:
        assert r.path.endswith("/get")
        assert ".svc/" in r.path


# --------------------------------------------------------------------------- #
# Client transport (CONFIRMED from the SDK)
# --------------------------------------------------------------------------- #
def _client(handler, **kw):
    kw.setdefault("developer_token", "dev-tok")
    kw.setdefault("user_token", "usr-tok")
    return ShipRushClient("https://api.my.shiprush.com", transport=httpx.MockTransport(handler), **kw)


def test_client_sends_configured_token_headers_and_xml():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["url"] = str(request.url)
        return httpx.Response(200, text="<Ok/>")

    with _client(handler, shipping_token="ship-tok", api_version="84114") as client:
        client.call("shipmentservice.svc/shipments/get", "<GetShipmentsRequest/>")

    h = captured["headers"]
    assert h["X-SHIPRUSH-DEVELOPER-TOKEN"] == "dev-tok"
    assert h["X-SHIPRUSH-USER-TOKEN"] == "usr-tok"
    assert h["X-SHIPRUSH-SHIPPING-TOKEN"] == "ship-tok"
    assert h["X-SHIPRUSH-VERSION"] == "84114"
    assert h["Content-Type"] == "application/xml"
    assert captured["url"] == "https://api.my.shiprush.com/shipmentservice.svc/shipments/get"


def test_client_omits_unset_token_headers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, text="<Ok/>")

    # Only a shipping token configured.
    c = ShipRushClient(
        "https://api.my.shiprush.com", shipping_token="s", transport=httpx.MockTransport(handler)
    )
    with c as client:
        client.call("x", "<R/>")
    assert "X-SHIPRUSH-DEVELOPER-TOKEN" not in captured["headers"]
    assert "X-SHIPRUSH-USER-TOKEN" not in captured["headers"]


def test_client_requires_a_token():
    with pytest.raises(ValueError, match="at least one ShipRush token"):
        ShipRushClient("https://api.my.shiprush.com")


def test_call_raises_on_error_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<Error><Message>bad token</Message></Error>")

    with _client(handler) as client:
        with pytest.raises(ShipRushAPIError, match="bad token"):
            client.call("x", "<R/>")


def test_call_raises_on_is_success_false():
    body = "<GetShipmentsResponse><Messages><ShippingMessage><Text>Address1 required</Text>" \
           "</ShippingMessage></Messages><IsSuccess>false</IsSuccess></GetShipmentsResponse>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    with _client(handler) as client:
        with pytest.raises(ShipRushAPIError, match="Address1 required"):
            client.call("x", "<R/>")


def test_client_retries_then_raises_on_500():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    with _client(handler, max_retries=2) as client:
        with pytest.raises(ShipRushAPIError, match="500"):
            client.call("x", "<R/>")
    assert calls["n"] == 3  # initial + 2 retries


# --------------------------------------------------------------------------- #
# XML helpers (CONFIRMED -- shape-independent of the unconfirmed schema)
# --------------------------------------------------------------------------- #
def test_xml_to_dict_nested_and_repeated():
    xml = (
        "<Shipment><ShipmentId>abc</ShipmentId><Package><Weight>1</Weight></Package>"
        "<Package><Weight>2</Weight></Package><Empty/></Shipment>"
    )
    d = _xml_to_dict(ET.fromstring(xml))
    assert d["ShipmentId"] == "abc"
    assert d["Package"] == [{"Weight": "1"}, {"Weight": "2"}]
    assert d["Empty"] == ""


def test_extract_records_by_confirmed_key():
    xml = (
        "<GetShipmentsResponse><Shipments>"
        "<Shipment><ShipmentId>a</ShipmentId></Shipment>"
        "<Shipment><ShipmentId>b</ShipmentId></Shipment>"
        "</Shipments></GetShipmentsResponse>"
    )
    root = ET.fromstring(xml)
    records = _extract_records(root, "Shipment")
    assert [r["ShipmentId"] for r in records] == ["a", "b"]


def test_extract_records_missing_key_returns_empty_not_a_guess():
    xml = "<GetShipmentsResponse><Shipments><Shipment><Id>a</Id></Shipment></Shipments></GetShipmentsResponse>"
    root = ET.fromstring(xml)
    # Wrong/unknown key -> zero records (standards section 5), never a silent guess.
    assert _extract_records(root, None) == []
    assert _extract_records(root, "Order") == []


def test_xml_namespaces_are_stripped():
    xml = '<Shipment xmlns="http://ns"><ShipmentId>x</ShipmentId></Shipment>'
    assert _xml_to_dict(ET.fromstring(xml)) == {"ShipmentId": "x"}


# --------------------------------------------------------------------------- #
# Deferred, tracked gap -- unconfirmed request/paging schema.
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(reason="GetShipmentsRequest/paging schema unconfirmed -- blocked on kit XSD", strict=True)
def test_paginate_sends_documented_request_and_pages():
    # TODO(blocked-on-xsd): assert the exact GetShipmentsRequest body and paging
    # params the client sends, and that records are extracted from the confirmed
    # response wrapper element. This is standards section 10's regression test.
    raise NotImplementedError
