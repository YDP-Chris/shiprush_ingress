"""NDJSON writer -- raw_payload / _ingestion_time / _payload_size_bytes envelope.

Flat GCS path: {prefix}/{endpoint}_{epoch}.ndjson

This file is source-agnostic. It matches the raw-ingestion envelope mandated by
Source Pipeline Standards section 7 exactly -- every REST source lands exactly
these three fields per record, nothing flatter, nothing source-specific added
at this layer.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def build_rows(records: Iterable[dict[str, Any]], ingestion_time: str) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        raw_payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        rows.append(
            {
                "raw_payload": raw_payload,
                "_ingestion_time": ingestion_time,
                "_payload_size_bytes": len(raw_payload.encode("utf-8")),
            }
        )
    return rows


def _object_name(prefix: str, endpoint: str, epoch: int) -> str:
    return "/".join(p for p in (prefix, f"{endpoint}_{epoch}.ndjson") if p)


def _spool_ndjson(rows: list[dict[str, Any]], endpoint: str) -> str:
    fd, tmp_path = tempfile.mkstemp(prefix=f"shiprush_{endpoint}_", suffix=".ndjson")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")
    return tmp_path


class GCSWriter:
    def __init__(self, bucket: str, prefix: str) -> None:
        from google.cloud import storage

        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)
        self._bucket_name = bucket
        self._prefix = prefix

    def write(self, rows, endpoint, epoch):
        if not rows:
            logger.info("No rows for %s; skipping upload.", endpoint)
            return "", 0
        tmp_path = _spool_ndjson(rows, endpoint)
        try:
            name = _object_name(self._prefix, endpoint, epoch)
            blob = self._bucket.blob(name)
            blob.upload_from_filename(tmp_path, content_type="application/x-ndjson")
            uri = f"gs://{self._bucket_name}/{name}"
            logger.info("Wrote %d %s rows -> %s", len(rows), endpoint, uri)
            return uri, len(rows)
        finally:
            os.unlink(tmp_path)


class LocalWriter:
    def __init__(self, output_dir: str, prefix: str) -> None:
        self._output_dir = output_dir
        self._prefix = prefix

    def write(self, rows, endpoint, epoch):
        if not rows:
            logger.info("No rows for %s; skipping write.", endpoint)
            return "", 0
        tmp_path = _spool_ndjson(rows, endpoint)
        name = _object_name(self._prefix, endpoint, epoch)
        dest = os.path.join(self._output_dir, name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(tmp_path, dest)
        logger.info("Wrote %d %s rows -> %s", len(rows), endpoint, dest)
        return dest, len(rows)
