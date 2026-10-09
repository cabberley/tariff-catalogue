import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import boto3
from moto import mock_aws
from tariff_core import content_hash, from_cdr, to_dict

from tariff_catalogue.harvest.common.archive import LocalArchiveStore, S3ArchiveStore
from tariff_catalogue.publish.build import build
from tariff_catalogue.publish.upload import upload

DETAIL = Path(__file__).parent / "fixtures" / "cdr" / "detail" / "ORI1161031MRE3@EME.json"


def _fixture_archive(root: Path) -> tuple[LocalArchiveStore, str, str]:
    store = LocalArchiveStore(root)
    data = json.loads(DETAIL.read_text())
    plan = from_cdr(
        data,
        retrieved_at=datetime(2026, 10, 9, tzinfo=UTC),
        url="https://example.test/plan",
    )[0]
    plan_id = plan.plan_id
    assert plan_id is not None
    version_hash = content_hash(plan)
    plan_data = to_dict(plan)
    store.put_json(
        f"versions/{quote(plan_id, safe=':@')}/{version_hash}.json",
        plan,
    )
    store.put_json(
        "index/au_cdr/plans.json",
        [
            {
                "plan_id": plan_id,
                "version_hash": version_hash,
                "status": "withdrawn",
                "first_seen": "2026-10-01T00:00:00+00:00",
                "display_name": plan.display_name,
                "supplier": plan_data["supplier"],
                "commodity": plan.commodity.value,
                "customer_type": plan.customer_type.value,
                "region": plan_data["region"],
                "partial": plan.partial,
                "equivalence_group": "same-pricing",
            }
        ],
    )
    store.put_json(
        "state/au_cdr.json",
        {"brands": {"origin": {"last_success": "2026-10-09T00:00:00+00:00"}}},
    )
    return store, plan_id, version_hash


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "v1").rglob("*")
        if path.is_file()
    }


def test_build_is_deterministic_and_keeps_withdrawn_versions_reachable(tmp_path: Path) -> None:
    store, plan_id, version_hash = _fixture_archive(tmp_path / "archive")
    output = tmp_path / "dist"
    report = build(store, output)
    first = _tree(output)
    build(store, output)
    second = _tree(output)

    top = json.loads(first["v1/index.json"])
    top_again = json.loads(second["v1/index.json"])
    top["build_time"] = top_again["build_time"]
    first["v1/index.json"] = json.dumps(
        top, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert first == second
    assert report.plans == 1
    assert report.versions == 1
    assert top["schema_version"] == "v1"
    assert top["countries"][0]["country"] == "au"

    encoded_id = quote(plan_id, safe="")
    assert set(first) == {
        "v1/index.json",
        "v1/au/index.json",
        "v1/au/ergon/index.json",
        f"v1/plans/{encoded_id}/index.json",
        f"v1/plans/{encoded_id}/{version_hash}.json",
    }
    plan_index = json.loads(first[f"v1/plans/{encoded_id}/index.json"])
    assert plan_index["versions"][0]["version_hash"] == version_hash
    assert plan_index["schema_version"] == "v1"
    plan_file = f"v1/plans/{encoded_id}/{version_hash}.json"
    assert first[plan_file] == store.get(
        f"versions/{quote(plan_id, safe=':@')}/{version_hash}.json"
    )
    region_key = f"v1/au/ergon/index.json"
    region_index = json.loads(first[region_key])
    assert region_index["schema_version"] == "v1"
    assert region_index["plans"][0]["status"] == "withdrawn"
    assert json.loads(first["v1/au/index.json"])["schema_version"] == "v1"


def test_upload_sets_cache_headers_and_uploads_versions_before_indexes(tmp_path: Path) -> None:
    archive, _plan_id, _version_hash = _fixture_archive(tmp_path / "archive")
    output = tmp_path / "dist"
    build(archive, output)

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="catalogue")
        store = S3ArchiveStore("catalogue", client=client)
        upload_order: list[str] = []
        client.meta.events.register(
            "before-call.s3.PutObject",
            lambda _model, params, **_kwargs: upload_order.append(params["Key"]),
        )

        report = upload(store, output)
        assert report.uploaded > 1
        assert report.unchanged == 0
        assert upload_order[-1] == "v1/index.json"
        first_index = next(
            index for index, key in enumerate(upload_order) if key.endswith("index.json")
        )
        assert all(not key.endswith("index.json") for key in upload_order[:first_index])
        assert all(key.endswith("index.json") for key in upload_order[first_index:])
        assert client.head_object(Bucket="catalogue", Key="v1/index.json")["CacheControl"] == (
            "public, max-age=300"
        )
        version_key = next(key for key in upload_order if not key.endswith("index.json"))
        assert client.head_object(Bucket="catalogue", Key=version_key)["CacheControl"] == (
            "public, max-age=31536000, immutable"
        )
        assert upload(store, output).uploaded == 0
