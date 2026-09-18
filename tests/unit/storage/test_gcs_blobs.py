# tests/unit/storage/test_gcs_blobs.py — exercises GCSBlobStore's wrapper logic
# against a mocked google.cloud.storage client, since this environment has no
# real GCS bucket. This tests that OUR code calls the SDK correctly, not that
# the SDK itself works.
from unittest.mock import MagicMock, patch

from app.storage.blobs import GCSBlobStore, build_blob_key


def test_gcs_store_put_calls_upload_from_string_with_owner_prefixed_key():
    key = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")
    with patch("app.storage.blobs.storage.Client") as mock_client_cls:
        mock_bucket = MagicMock()
        mock_client_cls.return_value.bucket.return_value = mock_bucket
        store = GCSBlobStore(bucket_name="mentor-blobs")

        store.put(key, b"payload")

        mock_bucket.blob.assert_called_once_with(key)
        mock_bucket.blob.return_value.upload_from_string.assert_called_once_with(
            b"payload"
        )


def test_gcs_store_signed_url_delegates_to_blob_generate_signed_url():
    key = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")
    with patch("app.storage.blobs.storage.Client") as mock_client_cls:
        mock_bucket = MagicMock()
        mock_client_cls.return_value.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value.generate_signed_url.return_value = (
            "https://signed"
        )
        store = GCSBlobStore(bucket_name="mentor-blobs")

        url = store.signed_url(key, expires_in=120)

        assert url == "https://signed"
        mock_bucket.blob.return_value.generate_signed_url.assert_called_once()
