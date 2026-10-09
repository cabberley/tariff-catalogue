import gzip
import hashlib
from pathlib import Path
from unittest.mock import Mock, patch

import boto3
import pytest
from moto import mock_aws

from tariff_catalogue.harvest.common.archive import (
    ArchiveConflictError,
    LocalArchiveStore,
    S3ArchiveStore,
)


@pytest.fixture(params=["local", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "local":
        yield LocalArchiveStore(tmp_path / "archive")
        return
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-archive")
        yield S3ArchiveStore("test-archive", client=client)


def test_json_append_only_and_state_overwrite(store) -> None:
    store.put_json("versions/plan/abc.json", {"name": "first"})
    store.put_json("versions/plan/abc.json", {"name": "first"})
    assert store.exists("versions/plan/abc.json")
    assert store.get_json("versions/plan/abc.json") == {"name": "first"}
    with pytest.raises(ArchiveConflictError):
        store.put_json("versions/plan/abc.json", {"name": "different"})

    store.put_json("state/feed.json", {"updated_since": "first"})
    store.put_json("state/feed.json", {"updated_since": "second"})
    assert store.get_json("state/feed.json") == {"updated_since": "second"}


def test_raw_gzip_metadata_and_listing(store) -> None:
    payload = b'{"plan":"example"}'
    retrieved_at = "2026-10-09T05:00:00+00:00"
    path = store.put_raw(
        "au_cdr",
        "plan/1",
        payload,
        {
            "url": "https://example.com/plan/1",
            "status": 200,
            "headers": {"content-type": "application/json"},
            "retrieved_at": retrieved_at,
        },
    )

    assert path.startswith("raw/au_cdr/2026/10/09/plan%2F1.")
    assert gzip.decompress(store.get(path)) == payload
    metadata = store.get_json(f"{path}.meta.json")
    assert metadata["url"] == "https://example.com/plan/1"
    assert metadata["sha256"] == hashlib.sha256(payload).hexdigest()
    assert list(store.list("raw/au_cdr/")) == [path, f"{path}.meta.json"]
    assert store.put_raw(
        "au_cdr",
        "plan/1",
        payload,
        {
            "url": "https://example.com/plan/1",
            "status": 200,
            "headers": {"content-type": "application/json"},
            "retrieved_at": retrieved_at,
        },
    ) == path


def test_append_only_raw_detects_collision(store) -> None:
    payload = b"first"
    path = store.put_raw(
        "feed", "key", payload, {"retrieved_at": "2026-10-09T00:00:00Z"}
    )
    with pytest.raises(ArchiveConflictError):
        store.put_json(path, {"not": "gzip"})


def test_local_paths_cannot_escape_root(tmp_path: Path) -> None:
    store = LocalArchiveStore(tmp_path / "archive")
    with pytest.raises(ValueError):
        store.get("../outside")


def test_s3_store_uses_r2_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("R2_BUCKET", "archive-bucket")
    monkeypatch.setenv("R2_ACCOUNT_ID", "account")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "access")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    client = Mock()

    with patch("boto3.client", return_value=client) as create_client:
        store = S3ArchiveStore()

    assert store.client is client
    create_client.assert_called_once_with(
        "s3",
        region_name="auto",
        endpoint_url="https://account.r2.cloudflarestorage.com",
        aws_access_key_id="access",
        aws_secret_access_key="secret",
    )


def test_archive_ls_cli_lists_matching_files(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from tariff_catalogue.cli import main

    LocalArchiveStore(tmp_path).put_json("raw/feed/item.json", {"ok": True})
    assert main(["archive-ls", "raw/feed", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "raw/feed/item.json"
