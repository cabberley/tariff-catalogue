"""Append-only storage for harvested responses and plan versions."""

from __future__ import annotations

import gzip
import hashlib
import importlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import quote

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


class ArchiveConflictError(Exception):
    """Raised when an immutable archive path already contains different data."""


class ArchiveStore(Protocol):
    def put_raw(self, feed: str, key: str, data: bytes, meta: dict[str, Any]) -> str: ...

    def exists(self, path: str) -> bool: ...

    def get(self, path: str) -> bytes: ...

    def list(self, prefix: str) -> Iterator[str]: ...

    def put_json(self, path: str, obj: Any) -> None: ...

    def get_json(self, path: str) -> Any: ...


def _path_parts(path: str) -> tuple[str, ...]:
    if not path or "\\" in path:
        raise ValueError("Archive paths must be non-empty relative POSIX paths")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError(f"Invalid archive path: {path}")
    return parsed.parts


def _state_path(path: str) -> bool:
    parts = _path_parts(path)
    return len(parts) == 2 and parts[0] == "state" and parts[1].endswith(".json")


def _json_bytes(path: str, obj: Any) -> bytes:
    parts = _path_parts(path)
    if parts[0] == "versions" and not isinstance(
        obj, (dict, list, str, int, float, bool, type(None))
    ):
        dump_plan = cast(Callable[[Any], str], importlib.import_module("tariff_core").dump_plan)
        return dump_plan(obj).encode("utf-8")
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _retrieval_time(meta: Mapping[str, Any]) -> datetime:
    value = meta.get("retrieved_at")
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("meta['retrieved_at'] must be an ISO timestamp or datetime")
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _put_metadata(meta: Mapping[str, Any], digest: str, retrieved_at: datetime) -> bytes:
    stored_meta = dict(meta)
    stored_meta["retrieved_at"] = retrieved_at.isoformat()
    stored_meta["sha256"] = digest
    return json.dumps(
        stored_meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


class LocalArchiveStore:
    """Archive files beneath a local directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _target(self, path: str) -> Path:
        target = self.root.joinpath(*_path_parts(path))
        resolved = target.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"Archive path escapes root: {path}") from error
        return resolved

    def _put(self, path: str, data: bytes) -> None:
        target = self._target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        overwrite = _state_path(path)
        if overwrite:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
                temporary.write(data)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)
            return
        try:
            with target.open("xb") as output:
                output.write(data)
        except FileExistsError:
            if target.read_bytes() != data:
                raise ArchiveConflictError(
                    f"Archive path already contains different data: {path}"
                ) from None

    def put_raw(self, feed: str, key: str, data: bytes, meta: dict[str, Any]) -> str:
        if not feed or not key:
            raise ValueError("feed and key must be non-empty")
        retrieved_at = _retrieval_time(meta)
        digest = hashlib.sha256(data).hexdigest()
        path = (
            f"raw/{quote(feed, safe='')}/{retrieved_at:%Y/%m/%d}/"
            f"{quote(key, safe='')}.{digest[:12]}.json.gz"
        )
        self._put(path, gzip.compress(data, mtime=0))
        self._put(f"{path}.meta.json", _put_metadata(meta, digest, retrieved_at))
        return path

    def exists(self, path: str) -> bool:
        return self._target(path).is_file()

    def get(self, path: str) -> bytes:
        return self._target(path).read_bytes()

    def list(self, prefix: str) -> Iterator[str]:
        normalized_prefix = prefix.rstrip("/")
        if normalized_prefix:
            _path_parts(normalized_prefix)
        if not self.root.exists():
            return iter(())
        return iter(
            sorted(
                path.relative_to(self.root).as_posix()
                for path in self.root.rglob("*")
                if path.is_file()
                and path.resolve().is_relative_to(self.root)
                and path.relative_to(self.root).as_posix().startswith(normalized_prefix)
            )
        )

    def put_json(self, path: str, obj: Any) -> None:
        self._put(path, _json_bytes(path, obj))

    def get_json(self, path: str) -> Any:
        return json.loads(self.get(path))


class S3ArchiveStore:
    """Archive objects in an S3-compatible bucket, including Cloudflare R2."""

    def __init__(
        self,
        bucket: str | None = None,
        endpoint_url: str | None = None,
        credentials: tuple[str, str] | Mapping[str, str] | None = None,
        *,
        client: S3Client | None = None,
    ) -> None:
        configured_bucket = bucket or os.getenv("R2_BUCKET") or os.getenv("R2_BUCKET_NAME")
        if not configured_bucket:
            raise ValueError("An S3 bucket is required (pass bucket or set R2_BUCKET)")
        self.bucket = configured_bucket
        endpoint_url = endpoint_url or os.getenv("R2_ENDPOINT_URL")
        if endpoint_url is None and os.getenv("R2_ACCOUNT_ID"):
            endpoint_url = f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"

        client_options: dict[str, Any] = {
            "region_name": "auto" if endpoint_url else "us-east-1"
        }
        if endpoint_url:
            client_options["endpoint_url"] = endpoint_url
        if credentials is None:
            access_key = os.getenv("R2_ACCESS_KEY_ID")
            secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
            if bool(access_key) != bool(secret_key):
                raise ValueError("R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY must be set together")
            if access_key and secret_key:
                credentials = (access_key, secret_key)
        if isinstance(credentials, Mapping):
            access_key = credentials.get("aws_access_key_id") or credentials.get("access_key_id")
            secret_key = credentials.get("aws_secret_access_key") or credentials.get(
                "secret_access_key"
            )
            if not access_key or not secret_key:
                raise ValueError("credentials must include an access key and secret key")
            client_options["aws_access_key_id"] = access_key
            client_options["aws_secret_access_key"] = secret_key
        elif credentials is not None:
            client_options["aws_access_key_id"], client_options["aws_secret_access_key"] = (
                credentials
            )

        if client is None:
            import boto3

            client = boto3.client("s3", **client_options)
        self.client = client

    def _put(self, path: str, data: bytes) -> None:
        _path_parts(path)
        if _state_path(path):
            self.client.put_object(Bucket=self.bucket, Key=path, Body=data)
            return
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=path, Body=data, IfNoneMatch="*"
            )
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            if self.get(path) != data:
                raise ArchiveConflictError(
                    f"Archive path already contains different data: {path}"
                ) from error

    def put_raw(self, feed: str, key: str, data: bytes, meta: dict[str, Any]) -> str:
        if not feed or not key:
            raise ValueError("feed and key must be non-empty")
        retrieved_at = _retrieval_time(meta)
        digest = hashlib.sha256(data).hexdigest()
        path = (
            f"raw/{quote(feed, safe='')}/{retrieved_at:%Y/%m/%d}/"
            f"{quote(key, safe='')}.{digest[:12]}.json.gz"
        )
        self._put(path, gzip.compress(data, mtime=0))
        self._put(f"{path}.meta.json", _put_metadata(meta, digest, retrieved_at))
        return path

    def exists(self, path: str) -> bool:
        _path_parts(path)
        try:
            self.client.head_object(Bucket=self.bucket, Key=path)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def get(self, path: str) -> bytes:
        _path_parts(path)
        return self.client.get_object(Bucket=self.bucket, Key=path)["Body"].read()

    def list(self, prefix: str) -> Iterator[str]:
        normalized_prefix = prefix.rstrip("/")
        if normalized_prefix:
            _path_parts(normalized_prefix)
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=normalized_prefix):
            for item in page.get("Contents", []):
                yield item["Key"]

    def put_json(self, path: str, obj: Any) -> None:
        self._put(path, _json_bytes(path, obj))

    def get_json(self, path: str) -> Any:
        return json.loads(self.get(path))
