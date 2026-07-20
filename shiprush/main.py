"""CLI / Cloud Run Job entrypoint: `python main.py`.

One job instance pulls exactly one resource, selected by SHIPRUSH_ENDPOINT
(Source Pipeline Standards section 1). Fan-out over resources happens at the
infra layer (one Job + one Scheduler per resource), not by looping here.

This wiring is complete and standards-conformant; it will run end-to-end once
ShipRushClient.paginate() and the resource registry are filled in from the API
guide (both currently raise/empty pending confirmation -- see resources.py and
client.py).
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from client import ShipRushClient
from config import Config
from resources import get_resource
from writer import GCSWriter, LocalWriter, build_rows

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def get_last_run_timestamp(config: Config) -> str:
    from google.cloud import storage

    blob = storage.Client().bucket(config.state_bucket).blob(config.last_run_file)
    if blob.exists():
        return blob.download_as_text().strip()
    return (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")


def set_last_run_timestamp(config: Config, timestamp: str) -> None:
    from google.cloud import storage

    storage.Client().bucket(config.state_bucket).blob(config.last_run_file).upload_from_string(timestamp)


def make_writer(config: Config):
    if config.local_output_dir:
        return LocalWriter(config.local_output_dir, config.gcs_prefix)
    return GCSWriter(config.gcs_bucket, config.gcs_prefix)


def main() -> int:
    logger.info("Job starting...")
    now = datetime.now(tz=timezone.utc)
    job_start_epoch = int(now.timestamp())
    ingestion_time = now.isoformat()

    config = Config.from_env()
    resource = get_resource(config.endpoint)
    writer = make_writer(config)

    watermarking_enabled = bool(config.state_bucket and resource.updated_since_param)
    params: dict[str, str] = {}
    if watermarking_enabled:
        since = get_last_run_timestamp(config)
        logger.info("Incremental pull for %s since %s", resource.name, since)
        params[resource.updated_since_param] = since
    else:
        logger.info("Full pull for %s", resource.name)

    path = resource.path.format()  # substitute any store/account id as needed

    with ShipRushClient(
        config.base_url,
        developer_token=config.developer_token,
        user_token=config.user_token,
        shipping_token=config.shipping_token,
        session_token=config.session_token,
        api_version=config.api_version,
        timeout_seconds=config.request_timeout_seconds,
        max_retries=config.max_retries,
    ) as client:
        records = client.paginate(
            path, params=params, page_size=config.page_size, record_key=resource.record_key
        )
        rows = build_rows(records, ingestion_time)

    if not rows:
        logger.info("No records returned for %s", resource.name)
        if watermarking_enabled:
            set_last_run_timestamp(config, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        logger.info("Job complete!")
        return 0

    writer.write(rows, resource.name, job_start_epoch)
    if watermarking_enabled:
        set_last_run_timestamp(config, now.strftime("%Y-%m-%dT%H:%M:%SZ"))

    logger.info("Job complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
