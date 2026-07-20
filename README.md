# shiprush_ingress

Extract-layer pipeline that pulls data from the **ShipRush** API and lands it as
NDJSON in GCS, following the team's **Source Pipeline Standards** (the living doc
that starts any pipeline work; reference implementation: `redo-sync`, companion:
`brightpearl`).

> **Status: scaffold — transport confirmed, one schema input outstanding.** The
> source-agnostic standards layer and the full ShipRush *transport* (auth, URLs,
> XML handling, error handling, resource registry) are implemented and tested.
> The only piece still open is the `GetShipmentsRequest`/`Response` **schema**
> (filter fields, paging, record-wrapper element), which lives in the SDK kit's
> XSD / `ShipRush.SDK.Proxies` and isn't guessable — see
> [Open questions](#open-questions-blocking-completion). Per the standards' rule
> (never guess pagination / record-keys / endpoints), `client.paginate()` raises
> until that schema is in hand.

## Architecture (per standards §1–§2)

One Cloud Run Job instance pulls **one** resource, selected by `SHIPRUSH_ENDPOINT`
and validated against the registry in `resources.py`. Fan-out over resources is at
the infra layer (one Job + one Scheduler per resource), not an in-process loop.

```
shiprush/
  main.py                  CLI / Cloud Run Job entrypoint (flat script)
  config.py                Config dataclass + from_env()
  resources.py             Resource registry + get_resource() validation
  client.py                ShipRushClient + ShipRushAPIError + XML helpers
  writer.py                raw-ingestion envelope + GCSWriter/LocalWriter
  test_shiprush_sync.py    offline test suite (mocked transport, no GCP creds)
  Dockerfile
  requirements.txt / requirements-dev.txt
```

## What ShipRush is (and how it maps to the standards)

Two things about ShipRush shaped this build:

1. **It's an XML request/response API, not JSON.** The standards skeleton assumes
   JSON; here every call is an XML `POST` and responses are parsed to dicts before
   landing in the standard envelope. The `_xml_to_dict` / `_extract_records`
   helpers in `client.py` do that conversion (namespaces stripped, repeated
   elements collapsed to lists).
2. **The prose "Web Non-Visual API" guide is transactional** (`rate`, `ship`,
   `void`, `tracking`, …) with **no listable history** — ShipRush states it
   retains shipping history only ~7 days and that persistence "is the job of the
   calling application." The **SDK command catalog**, however, exposes the real
   data-read endpoints this pipeline targets: `shipments/get`,
   `shippingaccounts/get`, `catalog/get`, `inventory/get`,
   `inventory/locations/get`.

Everything else follows the standards unchanged: the raw-ingestion envelope (§7),
flat GCS path `{prefix}/{endpoint}_{epoch}.ndjson` (§8), GCS-state watermarking
(§9), `Config`+`from_env()` (§3), validated registry (§4), plain-text logging
(§12), Cloud Run Job deploy (§11), naming (§13).

### Confirmed transport (from the ShipRush SDK)

| Aspect | Value |
|---|---|
| Base URL (prod) | `https://api.my.shiprush.com` |
| Base URL (sandbox) | `https://sandbox.api.my.shiprush.com` |
| URL shape | `{base}/{service}.svc/{command}`, `POST`, `Content-Type: application/xml` |
| Auth headers (each sent only when set) | `X-SHIPRUSH-DEVELOPER-TOKEN`, `X-SHIPRUSH-USER-TOKEN`, `X-SHIPRUSH-SHIPPING-TOKEN`, `X-SHIPRUSH-SESSION-TOKEN`, `X-SHIPRUSH-VERSION` |
| Data-read auth | eCommerce-style: `DEVELOPER-TOKEN` + `USER-TOKEN` (DeveloperToken must be enabled by ShipRush support) |
| Errors | `<Error><Message>…</Message></Error>`, or in-band `<IsSuccess>false</IsSuccess>` + `<Messages>` on HTTP 200 |
| Rate limit | none published → generic exponential backoff + `Retry-After` |

## Configuration (env vars, §13)

| Env var | Required | Purpose |
|---|---|---|
| `SHIPRUSH_ENDPOINT` | yes | which registered resource this job pulls (`shipments`, …) |
| `SHIPRUSH_DEVELOPER_TOKEN` | * | eCommerce-style auth for data reads |
| `SHIPRUSH_USER_TOKEN` | * | eCommerce-style auth for data reads |
| `SHIPRUSH_SHIPPING_TOKEN` | * | shipping-call token, if used |
| `SHIPRUSH_SESSION_TOKEN` | * | session token, if used |
| `SHIPRUSH_API_VERSION` | no | `X-SHIPRUSH-VERSION` value |
| `SHIPRUSH_BASE_URL` | no | override base URL (defaults to production) |
| `GCS_BUCKET` | prod | destination bucket (or set `LOCAL_OUTPUT_DIR`) |
| `LOCAL_OUTPUT_DIR` | local | write NDJSON to disk instead of GCS |
| `GCP_STATE_BUCKET` | no | watermark state bucket; omit for full pulls |
| `SHIPRUSH_LAST_RUN_FILE_LOCATION` | no | watermark blob path (default `shiprush_last_run/{endpoint}.txt`) |
| `SHIPRUSH_PAGE_SIZE` / `SHIPRUSH_REQUEST_TIMEOUT` / `SHIPRUSH_MAX_RETRIES` | no | client tuning |

\* At least one token is required; data-read endpoints need `DEVELOPER_TOKEN` + `USER_TOKEN`.

## Local development

```bash
cd shiprush
python -m pip install -r requirements-dev.txt
python -m pytest -q          # offline: no network, no GCP credentials
```

21 tests cover the confirmed layer (envelope, flat path, config rules, registry,
token headers + URL on the wire, `<Error>`/`IsSuccess` handling, XML→dict,
record extraction). One `xfail` marks the per-page request/paging regression test
as a tracked gap pending the schema.

## Open questions (blocking completion)

`client.paginate()` and per-resource `record_key`/`updated_since_param` need the
`GetShipmentsRequest`/`Response` schema, which is defined in the SDK kit's **XSD**
("XSD that describes constants and the TShipment schema") and the
`ShipRush.SDK.Proxies` assemblies — not in the pasted API guide or SDK source.
To finish without guessing, one of:

1. **The kit's XSD / proxy request+response classes**, or
2. **One live `shipments/get` request + response sample**,

pinning down, for `shipments` (and each other resource):
- the **request body** filter fields — especially any date-window / "updated
  since" filter (drives watermarking, §9);
- the **paging** scheme (page size + cursor/offset, and where the cursor is);
- the **response wrapper element** that holds the shipment records (the
  `record_key`).

Also needed to validate against the live API: an **enabled** DeveloperToken +
UserToken (sandbox `https://sandbox.api.my.shiprush.com` first — production
shipping must never run on sandbox).
