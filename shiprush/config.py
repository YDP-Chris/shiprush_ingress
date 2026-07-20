"""Configuration, sourced from environment variables.

Follows Source Pipeline Standards section 3: a frozen dataclass plus from_env().
Parsing env vars (from_env) is kept separate from using them (the dataclass), so
tests construct Config(...) directly with no real env vars set.

ShipRush auth note (confirmed from ShipRush developer docs / support): the Web
Non-Visual API uses eCommerce-style authentication -- a DeveloperToken and a
UserToken passed as HTTP headers (with optional ShippingToken / SessionToken).
That is why this Config carries two required secrets instead of the single
`api_secret` in the standards skeleton. The DeveloperToken must be explicitly
enabled by ShipRush support for these calls.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}.")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    # --- auth (ShipRush eCommerce-style, two tokens) ---
    developer_token: str
    user_token: str
    shipping_token: str | None = None
    session_token: str | None = None

    # --- what to pull ---
    endpoint: str = ""
    # UNCONFIRMED default: real SOAP web-service endpoint for the Web Non-Visual
    # API. Set SHIPRUSH_BASE_URL explicitly; do not rely on this placeholder in
    # production. See resources.py / README open questions.
    base_url: str = ""

    # --- destination ---
    gcs_bucket: str = ""
    gcs_prefix: str = "shiprush"

    # --- watermark state ---
    state_bucket: str | None = None
    last_run_file: str | None = None

    # --- client tuning ---
    page_size: int = 100
    request_timeout_seconds: int = 30
    max_retries: int = 5

    # --- local testing ---
    local_output_dir: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        endpoint = _env("SHIPRUSH_ENDPOINT", required=True)
        local_dir = _env("LOCAL_OUTPUT_DIR") or None
        bucket = _env("GCS_BUCKET", "") or ""
        if not bucket and not local_dir:
            raise RuntimeError("Set GCS_BUCKET (production) or LOCAL_OUTPUT_DIR (local testing).")

        state_bucket = _env("GCP_STATE_BUCKET") or None
        last_run_file = _env("SHIPRUSH_LAST_RUN_FILE_LOCATION") or (
            f"shiprush_last_run/{endpoint}.txt" if state_bucket else None
        )

        return cls(
            developer_token=_env("SHIPRUSH_DEVELOPER_TOKEN", required=True),
            user_token=_env("SHIPRUSH_USER_TOKEN", required=True),
            shipping_token=_env("SHIPRUSH_SHIPPING_TOKEN") or None,
            session_token=_env("SHIPRUSH_SESSION_TOKEN") or None,
            endpoint=endpoint,
            base_url=_env("SHIPRUSH_BASE_URL", "") or "",
            gcs_bucket=bucket,
            gcs_prefix=(_env("GCS_PREFIX", "shiprush") or "shiprush").strip("/"),
            state_bucket=state_bucket,
            last_run_file=last_run_file,
            page_size=_env_int("SHIPRUSH_PAGE_SIZE", 100),
            request_timeout_seconds=_env_int("SHIPRUSH_REQUEST_TIMEOUT", 30),
            max_retries=_env_int("SHIPRUSH_MAX_RETRIES", 5),
            local_output_dir=local_dir,
        )
