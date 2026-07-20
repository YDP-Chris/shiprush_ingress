# shiprush_ingress

Extract-layer pipeline that pulls data from the **ShipRush** API and lands it as
NDJSON in GCS, following the team's **Source Pipeline Standards** (the living doc
that starts any pipeline work; reference implementation: `redo-sync`, companion:
`brightpearl`).

> **Status: scaffold — not yet runnable end-to-end.** The source-agnostic,
> standards-mandated layer is complete and tested. The ShipRush-specific wire
> details (SOAP operation names, XML request/response schema, response record
> wrapper, pagination) are **intentionally not guessed** — see
> [Open questions](#open-questions-blocking-completion). This follows the
> standards' central rule: read the source's docs and implement exactly; never
> auto-detect or guess pagination / record-keys / endpoints (§5, §6).

## Architecture (per standards §1–§2)

One Cloud Run Job instance pulls **one** resource, selected by `SHIPRUSH_ENDPOINT`
and validated against the registry in `resources.py`. Fan-out over resources
happens at the infra layer (one Job + one Scheduler per resource), not by looping
in-process.

```
shiprush/
  main.py                  CLI / Cloud Run Job entrypoint (flat script)
  config.py                Config dataclass + from_env()
  resources.py             Resource registry + get_resource() validation
  client.py                ShipRushClient + ShipRushAPIError
  writer.py                raw-ingestion envelope + GCSWriter/LocalWriter
  test_shiprush_sync.py    offline test suite (mocked transport, no GCP creds)
  Dockerfile
  requirements.txt / requirements-dev.txt
```

## What ShipRush actually is (and why it diverges from the standards skeleton)

The standards skeleton assumes a REST/JSON source with query-param pagination and
an `updated_since` filter. ShipRush's **Web Non-Visual API** is different, and the
scaffold is adapted accordingly:

| Aspect | Standards skeleton (REST/JSON) | ShipRush (confirmed) | Handled in |
|---|---|---|---|
| Auth | single `Bearer` `api_secret` | two tokens, `DeveloperToken` + `UserToken` HTTP headers (optional `ShippingToken`/`SessionToken`); DeveloperToken must be enabled by ShipRush support | `config.py`, `client.py` |
| Transport | JSON `GET` | SOAP-style `POST` of an XML body, `Content-Type: application/xml` | `client.py` |
| Record extraction | JSON `record_key` wrapper | XML wrapper element (unconfirmed) | `resources.py`, `client.py` |
| Pagination | query params (source-specific) | **unconfirmed** | `client.py` |
| Rate limit | source-specific | none published → generic exp-backoff + `Retry-After` | `client.py` |

Everything else — the raw-ingestion envelope (§7), flat GCS path `{prefix}/{endpoint}_{epoch}.ndjson` (§8), GCS-state watermarking that defaults to yesterday and advances even on zero results (§9), `Config` dataclass + `from_env()` (§3), validated resource registry (§4), plain-text logging (§12), Cloud Run Job deployment (§11), and naming conventions (§13) — follows the standards unchanged.

**Confirmed** (ShipRush developer docs / support, corroborated by web search):
two-token header auth, `application/xml`, SOAP web-service model.
**Unconfirmed** (below): operation names, request/response XML schema, record
wrapper element, pagination scheme.

## Configuration (env vars, §13)

| Env var | Required | Purpose |
|---|---|---|
| `SHIPRUSH_ENDPOINT` | yes | which registered resource this job pulls |
| `SHIPRUSH_DEVELOPER_TOKEN` | yes | DeveloperToken header |
| `SHIPRUSH_USER_TOKEN` | yes | UserToken header |
| `SHIPRUSH_SHIPPING_TOKEN` | no | ShippingToken header, if used |
| `SHIPRUSH_SESSION_TOKEN` | no | SessionToken header, if used |
| `SHIPRUSH_BASE_URL` | prod | API base URL / SOAP endpoint (unconfirmed placeholder) |
| `GCS_BUCKET` | prod | destination bucket (or set `LOCAL_OUTPUT_DIR`) |
| `LOCAL_OUTPUT_DIR` | local | write NDJSON to disk instead of GCS |
| `GCP_STATE_BUCKET` | no | watermark state bucket; omit for full pulls |
| `SHIPRUSH_LAST_RUN_FILE_LOCATION` | no | watermark blob path (defaults to `shiprush_last_run/{endpoint}.txt`) |
| `SHIPRUSH_PAGE_SIZE` / `SHIPRUSH_REQUEST_TIMEOUT` / `SHIPRUSH_MAX_RETRIES` | no | client tuning |

## Local development

```bash
cd shiprush
python -m pip install -r requirements-dev.txt
python -m pytest -q          # offline: no network, no GCP credentials
```

The suite covers the confirmed layer (envelope, flat path, config rules, registry
validation, two-token auth headers on the wire). The pagination/extraction
regression test is present but `xfail` until the scheme is confirmed, so the gap
is tracked rather than silently missing.

## Open questions (blocking completion)

To finish `client.paginate()` and populate the `resources.py` registry without
guessing, the following need confirming — ideally from the
[Web Non-Visual API Guide](https://docs.shiprush.com/en/for-developers/shipping/my-shiprush-shipping-web-non-visual-api-guide~7395985101005094591)
(it 403s automated fetchers, so a paste/PDF or one live sample response is
needed), or the SDK guides:

1. **Which resources to ingress** — orders, shipments, something else? One Job +
   Scheduler per resource.
2. **Per resource: the SOAP operation** — its name and the exact XML request body.
3. **Response shape** — the element that wraps the record list (the `record_key`
   equivalent), and the per-record element to land.
4. **Pagination** — page counter? continuation token? date window? parameter
   names and where the next-page cursor lives.
5. **Incremental filter** — does the list operation support an "updated since"
   filter, and under what field? (Drives watermarking, §9.)
6. **Base URL** — the real SOAP endpoint for `SHIPRUSH_BASE_URL`.
7. **Credentials** — an enabled DeveloperToken + UserToken to validate against a
   live response before shipping.
