"""CLI / Cloud Run Job entrypoint: `python main.py`.

One job instance pulls exactly one resource, selected by SHIPRUSH_ENDPOINT
(Source Pipeline Standards section 1). Fan-out over resources happens at the
infra layer (one Job + one Scheduler per resource), not by looping here.

One job instance opens a ShipRushClient, paginates the selected resource over a
[since, until) window, wraps each record in the standard raw envelope, and lands
one NDJSON object in GCS (or a local dir for testing). Watermarking (section 9)
advances the stored `since` to this run's start time -- even on a zero-record
run -- for incremental resources.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

from client import ShipRushClient
from config import Config
from resources import _EPOCH_START, _FAR_FUTURE, get_resource
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
    return (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")


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

    # The pull window is [since, until). `until` is this run's start time; `since`
    # is the stored watermark for incremental resources with state, else the epoch
    # start (a full pull). shippingaccounts has no date filter, so its builder
    # ignores the window entirely.
    until = now.strftime("%Y-%m-%dT%H:%M:%S")
    watermarking_enabled = bool(config.state_bucket and resource.incremental)
    if watermarking_enabled:
        since = get_last_run_timestamp(config)
        logger.info("Incremental pull for %s over [%s, %s)", resource.name, since, until)
    else:
        since = _EPOCH_START
        until = _FAR_FUTURE
        logger.info("Full pull for %s", resource.name)

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
            resource, since=since, until=until, page_size=config.page_size
        )
        rows = build_rows(records, ingestion_time)

    if not rows:
        logger.info("No records returned for %s", resource.name)
        if watermarking_enabled:
            set_last_run_timestamp(config, until)
        logger.info("Job complete!")
        return 0

    writer.write(rows, resource.name, job_start_epoch)
    if watermarking_enabled:
        set_last_run_timestamp(config, until)

    logger.info("Job complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
