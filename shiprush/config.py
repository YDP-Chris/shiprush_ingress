"""Configuration, sourced from environment variables.

Follows Source Pipeline Standards section 3: a frozen dataclass plus from_env().
Parsing env vars (from_env) is kept separate from using them (the dataclass), so
tests construct Config(...) directly with no real env vars set.

ShipRush auth (confirmed from the ShipRush SDK, ShipRush.SDK.Transport):
every request may carry up to four token headers, each sent only when set --
    X-SHIPRUSH-DEVELOPER-TOKEN, X-SHIPRUSH-USER-TOKEN,
    X-SHIPRUSH-SHIPPING-TOKEN,  X-SHIPRUSH-SESSION-TOKEN
plus an optional X-SHIPRUSH-VERSION. The data-plane read calls this pipeline
targets (shipments/get, shippingaccounts/get, ...) use eCommerce-style auth --
DeveloperToken + UserToken -- and that DeveloperToken must be explicitly enabled
by ShipRush support. We accept all four tokens (require at least one) and let the
client send whichever are configured, mirroring the SDK exactly rather than
hard-coding one combination.

Base URLs (confirmed from the SDK's SetIsProduction):
    production  https://api.my.shiprush.com
    sandbox     https://sandbox.api.my.shiprush.com
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.my.shiprush.com"


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
    # --- auth (all optional individually; from_env requires at least one) ---
    developer_token: str | None = None
    user_token: str | None = None
    shipping_token: str | None = None
    session_token: str | None = None
    api_version: str | None = None  # X-SHIPRUSH-VERSION

    # --- what to pull ---
    endpoint: str = ""
    base_url: str = DEFAULT_BASE_URL

    # --- destination ---
    gcs_bucket: str = ""
    gcs_prefix: str = "shiprush"

    # --- watermark state ---
    state_bucket: str | None = None
    last_run_file: str | None = None

    # --- client tuning ---
    page_size: int = 100
    request_timeout_seconds: int = 60  # SDK uses a 60s call timeout
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

        developer_token = _env("SHIPRUSH_DEVELOPER_TOKEN") or None
        user_token = _env("SHIPRUSH_USER_TOKEN") or None
        shipping_token = _env("SHIPRUSH_SHIPPING_TOKEN") or None
        session_token = _env("SHIPRUSH_SESSION_TOKEN") or None
        if not any((developer_token, user_token, shipping_token, session_token)):
            raise RuntimeError(
                "Set at least one ShipRush token: SHIPRUSH_DEVELOPER_TOKEN / "
                "SHIPRUSH_USER_TOKEN / SHIPRUSH_SHIPPING_TOKEN / SHIPRUSH_SESSION_TOKEN. "
                "The data-read calls (shipments/get, ...) use DEVELOPER_TOKEN + USER_TOKEN."
            )

        state_bucket = _env("GCP_STATE_BUCKET") or None
        last_run_file = _env("SHIPRUSH_LAST_RUN_FILE_LOCATION") or (
            f"shiprush_last_run/{endpoint}.txt" if state_bucket else None
        )

        return cls(
            developer_token=developer_token,
            user_token=user_token,
            shipping_token=shipping_token,
            session_token=session_token,
            api_version=_env("SHIPRUSH_API_VERSION") or None,
            endpoint=endpoint,
            base_url=(_env("SHIPRUSH_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/"),
            gcs_bucket=bucket,
            gcs_prefix=(_env("GCS_PREFIX", "shiprush") or "shiprush").strip("/"),
            state_bucket=state_bucket,
            last_run_file=last_run_file,
            page_size=_env_int("SHIPRUSH_PAGE_SIZE", 100),
            request_timeout_seconds=_env_int("SHIPRUSH_REQUEST_TIMEOUT", 60),
            max_retries=_env_int("SHIPRUSH_MAX_RETRIES", 5),
            local_output_dir=local_dir,
        )
