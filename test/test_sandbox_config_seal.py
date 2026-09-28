"""The runtime config is sealed read-only for every sandboxed process.

``config.json`` and ``config.local.json`` carry the switches that loosen confinement
(``agent.sandbox``, ``agent.apps_allow_third_party``, ...). The file-edit tool fence
never sees a spawned shell's ``open()``, so the OS disposition is the load-bearing half:
without it an in-sandbox shell could write ``agent.sandbox: "off"`` and run its next
spawn unconfined. Both files are sealed because the loader merges the overlay over the
base with the overlay winning.
"""

from __future__ import annotations

import argparse
import ast
import errno
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import platform_compat, sandbox

_CONFIG_LEAVES = ("config.json", "config.local.json")


@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_config_leaf_is_read_only_in_every_mode(leaf):
    assert leaf in sandbox._CREW_READONLY_LEAVES
    assert leaf not in sandbox._CREW_HIDDEN_LEAVES
    assert leaf not in sandbox._CREW_SANDBOX_VISIBLE_LEAVES
    for prefix in (".kiro/crew", ".kirocrew"):
        assert f"{prefix}/{leaf}" in sandbox._CREW_READONLY_TARGETS


@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_config_leaf_is_precreated_so_the_linux_bind_has_a_file(leaf):
    # An absent overlay is the default state, and an absent name is exactly the one an
    # agent would create to win the merge.
    assert leaf in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES


def _set_args(*, local: bool = True):
    return argparse.Namespace(
        config_action="set", key="agent.sandbox", value="off", file=None, local=local
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A data home with the two config paths pointed at it, and NO sandbox marker.

    ``cli.main()`` pops ``KIROCREW_SANDBOX_ACTIVE`` before dispatch, so the marker is
    never in the environment when ``kirocrew config`` runs -- inside the sandbox or
    out. The hint has to be decided from the failure itself; a test that set the
    marker would pass against a hint that never fires in production.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    d = tmp_path / "crew"
    d.mkdir()
    (d / "config.json").write_text(
        json.dumps({"session": {"autocompact_pct": 90.0}}), encoding="utf-8"
    )
    with (
        patch("kiro_crew.cli_config.config_path", return_value=d / "config.json"),
        patch("kiro_crew.cli_config.config_local_path", return_value=d / "config.local.json"),
        patch("kiro_crew.config.loader.config_path", return_value=d / "config.json"),
        patch("kiro_crew.config.loader.config_local_path", return_value=d / "config.local.json"),
        patch("kiro_crew.config.loader.config_dir", return_value=d),
        patch("kiro_crew.cli_config.sel"),
    ):
        yield d


_HINT = "is read-only here"


def _assert_hint(err: str) -> None:
    """The hint names BOTH causes: the failure alone cannot tell the seal from a chmod."""
    assert _HINT in err
    assert "agent sandbox" in err
    assert "dashboard" in err
    assert "permissions" in err


@pytest.mark.parametrize("code", [errno.EPERM, errno.EACCES, errno.EBUSY, errno.EROFS])
@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_sandboxed_config_set_reports_the_seal_on_an_in_place_write(home, capsys, code, leaf):
    """``open(path, "w")`` refused by the seal names the sealed file as ``filename``."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(code, "denied", str(home / leaf)),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(_set_args(local=leaf == "config.local.json"))
    assert exc.value.code == 1
    _assert_hint(capsys.readouterr().err)


@pytest.mark.parametrize("code", [errno.EPERM, errno.EBUSY])
def test_sandboxed_config_set_reports_the_seal_on_the_publishing_rename(home, capsys, code):
    """``os.replace(tmp, path)`` names the temp as ``filename`` and the seal as ``filename2``.

    That is the shape ``atomic_write`` fails with: the temp lands (the data-home root is
    writable), and the rename over the sealed leaf is what the OS refuses -- ``EPERM``
    from Seatbelt, ``EBUSY`` from a Linux bind mount.
    """
    from kiro_crew.cli_config import _config_cmd

    tmp = home / "tmpa1b2c3.tmp"
    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(code, "denied", str(tmp), None, str(home / "config.local.json")),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(_set_args())
    assert exc.value.code == 1
    _assert_hint(capsys.readouterr().err)


def test_a_denial_against_an_unrelated_file_is_not_relabelled(home):
    """Errno alone is not the seal: the same errno against another path is re-raised."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=PermissionError(errno.EPERM, "denied", str(home / "other.json")),
    ):
        with pytest.raises(PermissionError):
            _config_cmd(_set_args())


def test_a_denial_with_no_filename_is_not_relabelled(home):
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=PermissionError(errno.EPERM, "denied"),
    ):
        with pytest.raises(PermissionError):
            _config_cmd(_set_args())


def test_a_non_denial_errno_against_the_config_is_not_relabelled(home):
    """A full data home names the same file but is not the seal."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(errno.ENOSPC, "full", str(home / "config.local.json")),
    ):
        with pytest.raises(OSError) as exc:
            _config_cmd(_set_args())
    assert exc.value.errno == errno.ENOSPC


def test_config_edit_editor_exec_denial_is_not_relabelled(home):
    """``config edit`` failing to exec the editor is an editor problem, not the seal."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.os.execvp",
        side_effect=PermissionError(errno.EACCES, "denied", "vi"),
    ):
        with pytest.raises(PermissionError):
            _config_cmd(argparse.Namespace(config_action="edit"))


def test_sandboxed_config_defaults_adopt_reports_the_seal(home, capsys):
    """``config defaults --adopt`` writes ``config.json`` through its own error path."""
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(errno.EPERM, "denied", str(home / "config.json")),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(
                argparse.Namespace(config_action="defaults", keys=[], adopt=True, keep=False)
            )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    _assert_hint(err)
    assert "Could not write" not in err


def test_config_defaults_adopt_keeps_its_own_message_for_other_failures(home, capsys):
    from kiro_crew.cli_config import _config_cmd

    with patch(
        "kiro_crew.cli_config.update_config_locked",
        side_effect=OSError(errno.ENOSPC, "full", str(home / "config.json")),
    ):
        with pytest.raises(SystemExit) as exc:
            _config_cmd(
                argparse.Namespace(config_action="defaults", keys=[], adopt=True, keep=False)
            )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Could not write" in err
    assert _HINT not in err


# ── The seal must survive the owner's own saves ───────────────────────────────
# On Linux the read-only seal is a per-file bind mount, and a file bind is pinned to
# the INODE it was made over. A temp+rename publish installs a new inode at the name,
# so every sandbox already running would see the new file unsealed. The owner's write
# therefore has to land through the inode that is already there -- on Linux, for the
# two sealed files, and nowhere else: Seatbelt seals by path and Windows has no bind
# mounts, so every other write keeps the atomic rename.


@pytest.fixture
def sealed_home(tmp_path):
    """A data home whose two config paths ARE the sealed set, on Linux.

    ``platform_compat.IS_LINUX`` is forced so the in-place branch is exercised on
    every CI platform, and the loader's path accessors point at *tmp_path* so
    ``_sealed_config_realpaths`` -- the one set the writer decides from -- names
    these files.
    """
    with (
        patch.object(platform_compat, "IS_LINUX", True),
        patch("kiro_crew.config.loader.config_path", return_value=tmp_path / "config.json"),
        patch(
            "kiro_crew.config.loader.config_local_path",
            return_value=tmp_path / "config.local.json",
        ),
    ):
        yield tmp_path


@pytest.mark.skipif(
    not platform_compat.IS_POSIX,
    reason="the in-place writer uses POSIX fd semantics; Windows never takes it",
)
@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_config_write_keeps_the_inode_the_seal_is_pinned_to(sealed_home, leaf):
    from kiro_crew.config.loader import write_config_atomically

    p = sealed_home / leaf
    p.write_text(json.dumps({"agent": {"model": "a" * 50}}) + "\n", encoding="utf-8")
    os.chmod(p, 0o600)
    before = p.stat()

    # A shorter document, then a longer one: both directions of the in-place write.
    write_config_atomically(p, {"agent": {"model": "b"}})
    assert json.loads(p.read_text(encoding="utf-8")) == {"agent": {"model": "b"}}
    longer = {"agent": {"model": "c" * 400}}
    write_config_atomically(p, longer, fsync=True)
    after = p.stat()

    assert json.loads(p.read_text(encoding="utf-8")) == longer
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert stat.S_IMODE(after.st_mode) == 0o600


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="in-place writer is POSIX-only")
def test_in_place_write_always_fsyncs(sealed_home):
    """An unflushed in-place write can read back empty after power loss.

    A rename leaves the old file until the new one is durable; an in-place write
    does not, so it flushes even when the caller did not ask for fsync.
    """
    from kiro_crew.config.loader import write_config_atomically

    p = sealed_home / "config.json"
    p.write_text("{}\n", encoding="utf-8")
    with patch("kiro_crew.config.loader.os.fsync", wraps=os.fsync) as flush:
        write_config_atomically(p, {"agent": {"model": "b"}})
    flush.assert_called()


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="in-place writer is POSIX-only")
def test_in_place_write_strictly_advances_mtime(sealed_home):
    """The fingerprint keys on mtime; a same-inode rewrite must not look unchanged.

    Both writes land within one coarse-timestamp tick, and the second keeps the
    size, so nothing but the mtime bump distinguishes them for another process's
    cache.
    """
    from kiro_crew.config.loader import write_config_atomically

    p = sealed_home / "config.json"
    write_config_atomically(p, {"agent": {"model": "aaa"}})
    first = p.stat()
    # Pin mtime far ahead so the natural clock cannot advance past it: the bump
    # must still be strictly greater than what was there before.
    ahead = first.st_mtime_ns + 10**12
    os.utime(p, ns=(first.st_atime_ns, ahead))
    write_config_atomically(p, {"agent": {"model": "bbb"}})
    second = p.stat()
    assert second.st_size == first.st_size
    assert second.st_ino == first.st_ino
    assert second.st_mtime_ns > ahead


def test_in_place_write_advances_mtime_on_a_second_granularity_filesystem(sealed_home):
    """A filesystem that keeps whole seconds truncates a 1 ns bump back to the old value.

    The writer must notice the bump did not take and step a whole second instead,
    or another process's fingerprint never busts on a same-size rewrite.
    """
    from kiro_crew.config.loader import write_config_atomically

    p = sealed_home / "config.json"
    write_config_atomically(p, {"agent": {"model": "aaa"}})
    first = p.stat()
    # Pin mtime far ahead, on a whole-second boundary, so the natural clock
    # cannot advance past it and only the bump can move it.
    whole = ((first.st_mtime_ns + 10**12) // 10**9) * 10**9
    os.utime(p, ns=(first.st_atime_ns, whole))
    real_utime = os.utime

    def coarse_utime(target, *args, ns=None, **kwargs):
        # Model a second-granularity filesystem: sub-second nanoseconds are lost.
        if ns is not None:
            ns = tuple((v // 10**9) * 10**9 for v in ns)
        return real_utime(target, *args, ns=ns, **kwargs)

    with patch("kiro_crew.config.loader.os.utime", coarse_utime):
        write_config_atomically(p, {"agent": {"model": "bbb"}})
    second = p.stat()
    assert second.st_size == first.st_size
    assert second.st_ino == first.st_ino
    assert second.st_mtime_ns >= whole + 10**9


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="in-place writer is POSIX-only")
def test_a_failing_in_place_write_restores_the_previous_document(sealed_home):
    """ENOSPC mid-write must leave the old config, not a torn one, and re-raise."""
    from kiro_crew.config import loader

    p = sealed_home / "config.json"
    original = json.dumps({"agent": {"model": "keep-me" * 20}}, indent=2) + "\n"
    p.write_text(original, encoding="utf-8")
    real_ftruncate = os.ftruncate
    calls = {"n": 0}

    def failing_ftruncate(fd, length):
        # The first truncate is the publish (payload already written over the old
        # bytes); fail it. The second is the rollback, which must go through.
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_ftruncate(fd, length)

    with patch("kiro_crew.config.loader.os.ftruncate", failing_ftruncate):
        with pytest.raises(OSError) as exc:
            loader.write_config_atomically(p, {"agent": {"model": "x"}})
    assert exc.value.errno == errno.ENOSPC
    assert p.read_text(encoding="utf-8") == original


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="in-place writer is POSIX-only")
def test_a_failing_first_in_place_write_leaves_no_file(sealed_home):
    """A first save that fails must leave the name ABSENT, as the rename path did.

    ``O_CREAT`` makes the file before the write; restoring its (empty) prior bytes
    would leave a zero-byte document that fails to parse and marks the config
    degraded for the life of the process. Absent loads cleanly as defaults.
    """
    from kiro_crew.config import loader

    p = sealed_home / "config.local.json"
    assert not p.exists()

    def failing_fsync(fd):
        raise OSError(errno.EIO, "Input/output error")

    with patch("kiro_crew.config.loader.os.fsync", failing_fsync):
        with pytest.raises(OSError) as exc:
            loader.write_config_atomically(p, {"agent": {"model": "x"}})
    assert exc.value.errno == errno.EIO
    assert not p.exists()


def test_a_torn_read_is_re_read_before_the_file_is_judged_corrupt(tmp_path):
    """One read that fails to parse is re-read once; only a second failure is corrupt.

    An in-place publish leaves a lock-free reader a microsecond window on a
    half-written document. Marking that degraded is sticky for the process life,
    so the loader must tell a torn read from a corrupt file.
    """
    from kiro_crew.config import loader

    p = tmp_path / "config.json"
    good = json.dumps({"agent": {"model": "x"}})
    p.write_text(good, encoding="utf-8")
    reads = iter([good[: len(good) // 2], good])

    def torn_then_whole(self, encoding="utf-8"):
        return next(reads)

    def no_sleep_on_the_event_loop(_seconds):
        raise AssertionError("_read_config_text must not sleep: load() runs on the event loop")

    with (
        patch.object(Path, "read_text", torn_then_whole),
        patch("time.sleep", no_sleep_on_the_event_loop),
    ):
        assert json.loads(loader._read_config_text(p)) == {"agent": {"model": "x"}}

    p.write_text("{not json", encoding="utf-8")
    with patch("time.sleep", no_sleep_on_the_event_loop):
        with pytest.raises(json.JSONDecodeError):
            json.loads(loader._read_config_text(p))


def _snapshot_every_step(p: Path, run) -> list[bytes]:
    """Run *run* with the loader's writes split one byte at a time, snapshotting *p*.

    Every ``os.write`` / ``os.pwrite`` / ``os.ftruncate`` the writer issues against
    *p*'s inode is followed by a read of the whole file, and ``os.write`` is made
    to land ONE byte per call so the snapshots cover every prefix a racing reader
    could see. The patches target the loader's ``os`` binding, which is the real
    module, so calls on any other fd are passed straight through.
    """
    from kiro_crew.config import loader

    target = p.stat().st_ino if p.exists() else None
    seen: list[bytes] = []
    real_write, real_pwrite, real_ftruncate = os.write, os.pwrite, os.ftruncate

    def ours(fd) -> bool:
        try:
            return os.fstat(fd).st_ino == (target if target is not None else p.stat().st_ino)
        except OSError:
            return False

    def snap(fd):
        if ours(fd):
            seen.append(p.read_bytes())

    def write_one_byte(fd, data):
        if not ours(fd):
            return real_write(fd, data)
        n = real_write(fd, bytes(data[:1]))
        snap(fd)
        return n

    def pwrite(fd, data, offset):
        n = real_pwrite(fd, data, offset)
        snap(fd)
        return n

    def ftruncate(fd, length):
        real_ftruncate(fd, length)
        snap(fd)

    with (
        patch("kiro_crew.config.loader.os.write", write_one_byte),
        patch("kiro_crew.config.loader.os.pwrite", pwrite),
        patch("kiro_crew.config.loader.os.ftruncate", ftruncate),
    ):
        run(loader)
    return seen


def _parses(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="in-place writer is POSIX-only")
@pytest.mark.parametrize(
    "old, new",
    [
        # Same length: a tear after the first field parses as {"sandbox": "off",
        # "approval": "interactive"} -- a pair NEITHER document authorized -- unless
        # the writer withholds the opening brace until the rest has landed.
        (
            {"sandbox": "auto", "approval": "interactive"},
            {"sandbox": "off!", "approval": "auto-approve"},
        ),
        # Shrinking: the old tail outlives the new bytes until the truncate.
        ({"agent": {"model": "long-" * 200}}, {"agent": {"model": "x"}}),
        # Growing.
        ({"agent": {"model": "x"}}, {"agent": {"model": "y" * 300}}),
    ],
    ids=["same-length", "shrinking", "growing"],
)
def test_no_snapshot_of_an_in_place_write_parses_as_a_hybrid(sealed_home, old, new):
    """At every byte of the publish the file is the old document, the new one, or unparseable.

    A racing reader (``_read_config_text``) accepts whatever parses. So the one
    thing the writer must guarantee is that nothing parseable exists between the
    two documents: a prefix of the new bytes followed by a tail of the old ones
    is valid JSON whenever the tear lands between two fields of the same shape,
    and would start a session on a settings pair nobody saved.
    """
    p = sealed_home / "config.json"
    old_text = json.dumps(old, indent=2) + "\n"
    p.write_text(old_text, encoding="utf-8")

    seen = _snapshot_every_step(p, lambda loader: loader.write_config_atomically(p, new))

    assert len(seen) > 3, "the spy saw no per-byte writes; the in-place branch did not run"
    for raw in seen:
        parsed = _parses(raw)
        assert parsed in (None, old, new), f"parseable hybrid exposed: {raw!r}"
    assert json.loads(p.read_text(encoding="utf-8")) == new
    assert seen[-1].decode("utf-8") == json.dumps(new, indent=2) + "\n"


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="in-place writer is POSIX-only")
def test_a_rolled_back_in_place_write_never_exposes_a_hybrid_either(sealed_home):
    """The rollback re-installs the old document the same brace-last way."""
    p = sealed_home / "config.json"
    old = {"sandbox": "auto", "approval": "interactive"}
    new = {"sandbox": "off!", "approval": "auto-approve"}
    old_text = json.dumps(old, indent=2) + "\n"
    p.write_text(old_text, encoding="utf-8")
    real_fsync = os.fsync

    def failing_fsync(fd):
        raise OSError(errno.EIO, "Input/output error")

    def run(loader):
        with patch("kiro_crew.config.loader.os.fsync", failing_fsync):
            with pytest.raises(OSError):
                loader.write_config_atomically(p, new)

    seen = _snapshot_every_step(p, run)
    assert real_fsync is os.fsync
    for raw in seen:
        assert _parses(raw) in (None, old, new), f"parseable hybrid exposed: {raw!r}"
    assert p.read_text(encoding="utf-8") == old_text


@pytest.mark.parametrize("leaf", _CONFIG_LEAVES)
def test_a_non_sealed_path_is_published_by_atomic_rename(tmp_path, leaf):
    """A document that is not one of the two sealed files keeps the tmp+rename.

    ``tmp_path/<leaf>`` shares the sealed leaf NAME but is not ``config_path()``,
    so it is a new inode after the write: the same shape as an agent spec written
    through the shared writer (``dashboard/handlers/agents.py``).
    """
    from kiro_crew.config import loader

    p = tmp_path / leaf
    p.write_text("{}\n", encoding="utf-8")
    before = p.stat()
    with (
        patch.object(platform_compat, "IS_LINUX", True),
        patch("kiro_crew.config.loader.atomic_write", wraps=loader.atomic_write) as rename,
        patch("kiro_crew.config.loader._write_config_in_place") as in_place,
    ):
        loader.write_config_atomically(p, {"agent": {"model": "b"}})
    rename.assert_called_once()
    in_place.assert_not_called()
    assert json.loads(p.read_text(encoding="utf-8")) == {"agent": {"model": "b"}}
    if platform_compat.IS_POSIX:
        after = p.stat()
        assert (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)


def test_a_sealed_path_off_linux_is_published_by_atomic_rename(tmp_path):
    """macOS seals by path (Seatbelt), so the in-place write buys nothing there."""
    from kiro_crew.config import loader

    p = tmp_path / "config.json"
    p.write_text("{}\n", encoding="utf-8")
    before = p.stat()
    with (
        patch.object(platform_compat, "IS_LINUX", False),
        patch("kiro_crew.config.loader.config_path", return_value=p),
        patch("kiro_crew.config.loader.config_local_path", return_value=tmp_path / "x.json"),
        patch("kiro_crew.config.loader.atomic_write", wraps=loader.atomic_write) as rename,
        patch("kiro_crew.config.loader._write_config_in_place") as in_place,
    ):
        loader.write_config_atomically(p, {"agent": {"model": "b"}})
    rename.assert_called_once()
    in_place.assert_not_called()
    assert json.loads(p.read_text(encoding="utf-8")) == {"agent": {"model": "b"}}
    if platform_compat.IS_POSIX:
        after = p.stat()
        assert (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)


def test_config_write_creates_an_absent_file_owner_only(tmp_path):
    from kiro_crew.config.loader import write_config_atomically

    p = tmp_path / "crew" / "config.local.json"
    write_config_atomically(p, {"agent": {"sandbox": "auto"}})
    assert json.loads(p.read_text(encoding="utf-8")) == {"agent": {"sandbox": "auto"}}
    if platform_compat.IS_POSIX:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
#: Zero-argument path accessors for the two sealed leaves. An app's own
#: ``config_path(root)`` takes an argument and names a different file.
_CONFIG_ACCESSORS = frozenset({"config_path", "config_local_path"})
#: Calls that install a NEW inode at their destination (or truncate-write it
#: outside the shared writer): ``atomic_write(path, ...)``, ``os.replace(src, dst)``
#: and the ``Path`` methods on the destination itself.
_FIRST_ARG_PUBLISHERS = frozenset({"atomic_write", "atomic_write_at"})
_DESTINATION_PUBLISHERS = frozenset({"replace", "rename"})  # os.replace / os.rename
_PATH_METHOD_PUBLISHERS = frozenset({"replace", "rename", "write_text", "write_bytes"})


def _is_config_accessor_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or node.args or node.keywords:
        return False
    fn = node.func
    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
    return name in _CONFIG_ACCESSORS


def _config_publishes_outside_the_shared_writer(scope: ast.AST) -> list[int]:
    """Line numbers in *scope* that publish a sealed config leaf directly."""
    bound: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and _is_config_accessor_call(node.value):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))

    def is_config(target: ast.AST | None) -> bool:
        if target is None:
            return False
        if isinstance(target, ast.Name):
            return target.id in bound
        return _is_config_accessor_call(target)

    hits: list[int] = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in _FIRST_ARG_PUBLISHERS:
            target = node.args[0] if node.args else None
            for kw in node.keywords:
                if kw.arg == "path":
                    target = kw.value
            if is_config(target):
                hits.append(node.lineno)
        elif isinstance(fn, ast.Attribute):
            receiver = fn.value
            if (
                isinstance(receiver, ast.Name)
                and receiver.id == "os"
                and fn.attr in _DESTINATION_PUBLISHERS
            ):
                if len(node.args) > 1 and is_config(node.args[1]):
                    hits.append(node.lineno)
            elif fn.attr in _PATH_METHOD_PUBLISHERS and is_config(receiver):
                hits.append(node.lineno)
    return hits


def test_no_source_publishes_the_sealed_config_outside_write_config_atomically():
    """Every writer of ``config.json`` / ``config.local.json`` uses the shared writer.

    ``write_config_atomically`` is what keeps the inode (and so the seal) on Linux. A
    direct ``atomic_write`` / ``os.replace`` onto either leaf would re-open the hole
    on the next save. Scoped per function so an unrelated local named ``cp`` in
    another function is not a hit; the loader's own writer takes ``path`` as a
    parameter and is not bound from an accessor, so it needs no exemption.
    """
    offenders: list[str] = []
    for source in sorted(_SRC_ROOT.rglob("*.py")):
        if "_vendor" in source.parts:
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        if "config_path()" not in text and "config_local_path()" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        scopes = [
            n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for scope in scopes:
            for lineno in _config_publishes_outside_the_shared_writer(scope):
                rel = source.relative_to(_SRC_ROOT.parent.parent)
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "config.json / config.local.json must be published through "
        "write_config_atomically, which writes them in place on Linux so the sandbox's "
        "read-only bind (pinned to the inode) survives the save:\n  " + "\n  ".join(offenders)
    )
