"""Where uploaded files live, and why a storage key is not a path.

The old backend built paths from a timestamp and a user-supplied filename. Two
uploads in the same second collided, and the only thing standing between a
filename and the filesystem was a `Path(...).name` call at one of the call
sites.
"""

import pytest

from app.modules.printing.storage import (
    DocumentStore,
    InvalidStorageKey,
    StorageArea,
)


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    return DocumentStore(tmp_path / "storage")


def test_save_writes_the_bytes_and_returns_a_key_that_reads_back(store):
    key = store.save(StorageArea.ORIGINAL, user_id=7, filename="essay.pdf", data=b"%PDF-x")

    assert store.read(key) == b"%PDF-x"
    assert store.exists(key)
    assert store.size(key) == 6


def test_the_root_is_created_on_first_write(tmp_path):
    store = DocumentStore(tmp_path / "not-there-yet")

    key = store.save(StorageArea.ORIGINAL, user_id=1, filename="a.pdf", data=b"x")

    assert store.path(key).is_file()


def test_two_uploads_of_the_same_filename_never_collide(store):
    """The old backend named files by second-resolution timestamp, so a student
    uploading three PDFs at once had all three rows pointing at one file."""
    keys = {
        store.save(StorageArea.ORIGINAL, user_id=7, filename="essay.pdf", data=b"one"),
        store.save(StorageArea.ORIGINAL, user_id=7, filename="essay.pdf", data=b"two"),
        store.save(StorageArea.ORIGINAL, user_id=7, filename="essay.pdf", data=b"three"),
    }

    assert len(keys) == 3


def test_the_key_keeps_the_extension_so_tools_can_tell_what_it_is(store):
    key = store.save(StorageArea.ORIGINAL, user_id=7, filename="essay.pdf", data=b"x")

    assert key.endswith(".pdf")


def test_areas_are_separate_directories(store):
    original = store.save(StorageArea.ORIGINAL, user_id=1, filename="a.pdf", data=b"x")
    normalised = store.save(StorageArea.NORMALISED, user_id=1, filename="a.pdf", data=b"x")

    assert original.startswith("originals/")
    assert normalised.startswith("normalised/")


def test_keys_are_relative_so_the_storage_root_can_move(store):
    key = store.save(StorageArea.ORIGINAL, user_id=1, filename="a.pdf", data=b"x")

    assert not key.startswith("/")
    assert ":" not in key  # nor a Windows drive letter


# ── the filesystem is not reachable from a filename ─────────────────────────


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/passwd",
        "..\\..\\windows\\system32\\config\\sam",
        "/etc/passwd",
        "essay.pdf/../../../root/.ssh/id_rsa",
    ],
)
def test_a_hostile_filename_cannot_escape_the_storage_root(store, hostile):
    key = store.save(StorageArea.ORIGINAL, user_id=1, filename=hostile, data=b"x")

    written = store.path(key).resolve()
    assert written.is_relative_to(store.root.resolve())


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../../etc/passwd",
        "..\\..\\windows\\system32\\sam.pdf",
        "C:\\secrets\\keys.pdf",
        "essay.pdf/../../../root/.ssh/id_rsa",
        "....//....//x.pdf",
        # POSIX path parsing treats backslashes as ordinary characters, so the
        # whole tail comes back as a "suffix" -- a climb on a Windows host.
        "report.pd\\..\\..\\evil",
    ],
)
def test_no_separator_from_a_filename_survives_into_the_key(store, hostile):
    """The guard is that only letters and digits from the filename reach the
    key at all -- checked on the generated name rather than on where it landed,
    because a name that cannot contain a separator cannot describe a climb."""
    key = store.new_key(StorageArea.ORIGINAL, user_id=1, filename=hostile)
    generated_name = key.rsplit("/", 1)[-1]

    assert ".." not in generated_name
    assert "\\" not in generated_name
    assert ":" not in generated_name
    assert generated_name.count(".") <= 1


def test_an_absurdly_long_extension_is_truncated(store):
    """A filename is attacker-controlled, and filesystems have name limits. An
    upload must not be able to fail at write time by being 4kB of "extension"."""
    key = store.new_key(
        StorageArea.ORIGINAL, user_id=1, filename="essay." + "a" * 4000
    )

    assert len(key.rsplit("/", 1)[-1]) < 80


def test_a_filename_that_is_only_dots_still_produces_a_usable_key(store):
    key = store.save(StorageArea.ORIGINAL, user_id=1, filename="..", data=b"x")

    assert store.read(key) == b"x"


@pytest.mark.parametrize(
    "hostile",
    [
        "../secrets.env",
        "originals/../../secrets.env",
        "/etc/passwd",
        "originals/2026/08/../../../../secrets.env",
    ],
)
def test_a_key_that_climbs_out_of_the_root_is_refused(store, hostile):
    """Keys are server-generated, so this can only happen through a bug or a
    tampered database row -- both of which should stop dead rather than read an
    arbitrary file off the disk."""
    with pytest.raises(InvalidStorageKey):
        store.path(hostile)


def test_an_empty_key_is_refused(store):
    with pytest.raises(InvalidStorageKey):
        store.path("")


# ── deleting ────────────────────────────────────────────────────────────────


def test_delete_removes_the_file_and_reports_that_it_did(store):
    key = store.save(StorageArea.ORIGINAL, user_id=1, filename="a.pdf", data=b"x")

    assert store.delete(key) is True
    assert not store.exists(key)


def test_deleting_something_already_gone_is_not_an_error(store):
    """Retention runs repeatedly over the same rows, and a file removed by hand
    must not make the sweep fail for everything behind it."""
    key = store.save(StorageArea.ORIGINAL, user_id=1, filename="a.pdf", data=b"x")
    store.delete(key)

    assert store.delete(key) is False


def test_reading_a_missing_key_raises_rather_than_returning_empty(store):
    key = store.save(StorageArea.ORIGINAL, user_id=1, filename="a.pdf", data=b"x")
    store.delete(key)

    with pytest.raises(FileNotFoundError):
        store.read(key)


def test_size_of_a_missing_key_is_none_rather_than_zero(store):
    """Zero is a real file size. A missing file is a different fact."""
    assert store.size("originals/2026/08/nothing.pdf") is None


# ── legacy paths ────────────────────────────────────────────────────────────


def test_a_legacy_style_key_still_resolves(store):
    """The migration carries the production rows across with their existing
    paths, so the store must be able to read a key it did not generate."""
    legacy = "original/user12_1a2b3c.pdf"
    target = store.path(legacy)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"legacy")

    assert store.read(legacy) == b"legacy"
