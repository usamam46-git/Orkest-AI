"""
`core/storage._client()` — which boto3 arguments each deployment mode produces.

The conftest stubs the object-store functions, so nothing else covers the client
construction, and that is exactly the code that differs between the local MinIO
stack and the AWS S3 deployment. These tests build the client for real (boto3
makes no network call at construction) and inspect it.
"""

import pytest

from src.core import storage
from src.core.config import settings

# conftest's autouse `_stub_object_storage` replaces `storage.ensure_bucket_sync` with a
# no-op for every test. Bind the real function at import time — collection happens
# before any fixture runs — or the bucket-creation test exercises the stub.
_real_ensure_bucket_sync = storage.ensure_bucket_sync


@pytest.fixture(autouse=True)
def _fresh_client():
    storage._client.cache_clear()
    yield
    storage._client.cache_clear()


def _set(monkeypatch, **values):
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)


def test_minio_mode_uses_endpoint_and_static_keys(monkeypatch):
    _set(
        monkeypatch,
        MINIO_ENDPOINT="minio:9000",
        MINIO_ACCESS_KEY="k",
        MINIO_SECRET_KEY="s",
        MINIO_SECURE=False,
        AWS_REGION="",
    )
    client = storage._client()
    assert client.meta.endpoint_url == "http://minio:9000"
    assert client._request_signer._credentials.access_key == "k"


def test_minio_mode_honours_secure_flag(monkeypatch):
    _set(monkeypatch, MINIO_ENDPOINT="minio:9000", MINIO_ACCESS_KEY="k", MINIO_SECRET_KEY="s", MINIO_SECURE=True)
    assert storage._client().meta.endpoint_url == "https://minio:9000"


def test_aws_mode_targets_real_s3_with_no_static_credentials(monkeypatch):
    # Clear ambient credentials so the assertion is about OUR arguments, not the
    # developer's shell.
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    _set(monkeypatch, MINIO_ENDPOINT="", MINIO_ACCESS_KEY="", MINIO_SECRET_KEY="", AWS_REGION="us-east-1")
    client = storage._client()
    assert client.meta.endpoint_url == "https://s3.amazonaws.com"
    assert client.meta.region_name == "us-east-1"


def test_half_a_key_pair_is_not_pinned(monkeypatch):
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    _set(monkeypatch, MINIO_ENDPOINT="", MINIO_ACCESS_KEY="orphan", MINIO_SECRET_KEY="", AWS_REGION="us-east-1")
    creds = storage._client()._request_signer._credentials
    assert creds is None or creds.access_key != "orphan"


def test_create_bucket_sends_location_constraint_only_on_aws_outside_us_east_1(monkeypatch):
    calls: list[dict] = []

    class FakeClient:
        def head_bucket(self, **_):
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

        def create_bucket(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(storage, "_client", lambda: FakeClient())

    _set(monkeypatch, MINIO_ENDPOINT="", AWS_REGION="eu-west-1", MINIO_BUCKET="b")
    _real_ensure_bucket_sync()
    assert calls[-1]["CreateBucketConfiguration"] == {"LocationConstraint": "eu-west-1"}

    _set(monkeypatch, MINIO_ENDPOINT="", AWS_REGION="us-east-1")
    _real_ensure_bucket_sync()
    assert "CreateBucketConfiguration" not in calls[-1]

    _set(monkeypatch, MINIO_ENDPOINT="minio:9000", AWS_REGION="eu-west-1")
    _real_ensure_bucket_sync()
    assert "CreateBucketConfiguration" not in calls[-1]
