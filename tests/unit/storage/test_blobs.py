import pytest

from app.storage.blobs import LocalFilesystemBlobStore, build_blob_key


def test_build_blob_key_is_owner_prefixed():
    key = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")
    assert key == "users/user-a/raw-slack/msg-1"


def test_build_blob_key_rejects_path_traversal():
    with pytest.raises(ValueError):
        build_blob_key(owner_user_id="../etc", kind="raw-slack", blob_id="msg-1")
    with pytest.raises(ValueError):
        build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="../../secret")


def test_build_blob_key_rejects_backslash():
    with pytest.raises(ValueError):
        build_blob_key(
            owner_user_id="user-a", kind="raw-slack", blob_id="..\\..\\secret"
        )


def test_local_filesystem_store_put_then_get_round_trips(tmp_path):
    store = LocalFilesystemBlobStore(root_dir=tmp_path)
    key = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")

    store.put(key, b"raw slack payload")

    assert store.get(key) == b"raw slack payload"


def test_local_filesystem_store_isolates_owners_by_directory(tmp_path):
    store = LocalFilesystemBlobStore(root_dir=tmp_path)
    key_a = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")
    key_b = build_blob_key(owner_user_id="user-b", kind="raw-slack", blob_id="msg-1")

    store.put(key_a, b"owner a's payload")
    store.put(key_b, b"owner b's payload")

    assert store.get(key_a) == b"owner a's payload"
    assert store.get(key_b) == b"owner b's payload"


def test_local_filesystem_store_signed_url_points_at_the_key(tmp_path):
    store = LocalFilesystemBlobStore(root_dir=tmp_path)
    key = build_blob_key(owner_user_id="user-a", kind="raw-slack", blob_id="msg-1")
    store.put(key, b"data")

    url = store.signed_url(key)

    assert key in url
