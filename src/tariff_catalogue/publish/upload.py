"""Upload built catalogue files to an S3-compatible static bucket."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from botocore.exceptions import ClientError

from tariff_catalogue.harvest.common.archive import S3ArchiveStore


@dataclass(frozen=True, slots=True)
class UploadReport:
    uploaded: int
    unchanged: int


def upload(store: S3ArchiveStore, out_dir: Path) -> UploadReport:
    """Upload changed files under ``out_dir/v1`` with immutable/versioned cache policy."""
    catalogue_dir = Path(out_dir) / "v1"
    files = sorted(catalogue_dir.rglob("*.json"))
    version_files = [path for path in files if path.name != "index.json"]
    index_files = [path for path in files if path.name == "index.json"]
    uploaded = 0
    unchanged = 0

    for path in [*version_files, *index_files]:
        key = path.relative_to(out_dir).as_posix()
        body = path.read_bytes()
        digest = hashlib.md5(body, usedforsecurity=False).hexdigest()
        try:
            current = store.client.head_object(Bucket=store.bucket, Key=key)
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 404:
                raise
        else:
            etag = str(current.get("ETag", "")).strip('"')
            if etag == digest:
                unchanged += 1
                continue

        cache_control = (
            "public, max-age=31536000, immutable"
            if path.name != "index.json"
            else "public, max-age=300"
        )
        store.client.put_object(
            Bucket=store.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            CacheControl=cache_control,
        )
        uploaded += 1
    return UploadReport(uploaded=uploaded, unchanged=unchanged)
