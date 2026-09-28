"""Slack Reconnect: ``GatewayOrchestrator.reconnect_slack`` + ``POST /api/slack/reconnect``.

Slack credentials are hoisted once, in ``GatewayOrchestrator.__init__``, and
nothing reassigned them afterwards, so a token or owner ID saved from the
dashboard could take effect only through ``POST /api/restart``. These tests
pin the in-place alternative:

* the orchestrator re-reads the store, recomputes ``_slack_enabled`` from the
  tokens now on disk (a stale ``False`` would make ``init_socket_mode`` a
  silent no-op), tears the old socket client down, re-awaits
  ``init_socket_mode`` on the running loop and records the outcome where the
  settings badge reads it;
* a store that cannot be read leaves the live connection untouched;
* the dashboard's Slack client mirror is cleared with the old socket and
  published again only behind a connected one, and the dashboard's
  ``owner_id`` follows the saved owner -- so a rejected workspace or a former
  owner never keeps dashboard access through a reconnect;
* the handler module's authorization subject (owner + allowlist) follows the
  saved owner on EVERY reconnect, including the ones that stop before a
  handshake, and an old socket client that will not close aborts the attempt
  with that subject cleared -- so a listener that outlives its credentials
  accepts no privileged command from the former owner;
* concurrent callers share one handshake;
* the route carries the PUT's direct-local gate and answers the
  ``connected`` / ``connect_error`` shape ``GET /api/slack/config`` documents.

The orchestrator is built through ``__new__`` (its ``__init__`` boots the
world); ``init_socket_mode`` and ``_connect_slack`` are replaced by recorders
because a real handshake needs Slack.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config.loader import CRED_OWNER_ID, CRED_SLACK_APP_TOKEN, CRED_SLACK_BOT_TOKEN

# Obvious placeholders: nothing here reaches Slack.
NEW_CREDS = {
    CRED_SLACK_APP_TOKEN: "xapp-new-not-a-real-value",
    CRED_SLACK_BOT_TOKEN: "xoxb-new-not-a-real-value",
    CRED_OWNER_ID: "U0NEWOWNER",
}
FORMER_OWNER = "U0FORMEROWNER"


_STATE_DIRS: list[tempfile.TemporaryDirectory[str]] = []


@pytest.fixture(autouse=True)
def _remove_state_dirs() -> Any:
    """Every ``_orch()`` state dir of a test goes with the test."""
    yield
    while _STATE_DIRS:
        _STATE_DIRS.pop().cleanup()


def _orch(creds: dict[str, str] | Exception = NEW_CREDS, *, old_client: Any = None) -> Any:
    """A GatewayOrchestrator in the state a failed boot leaves it in.

    ``_slack_enabled`` is False and the tokens are stale, exactly the state the
    issue describes: ``init_socket_mode`` early-returns on that flag, so a
    reconnect that does not recompute it is a no-op.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch._cfg = MagicMock()
    if isinstance(creds, Exception):
        orch._cfg.load_credentials.side_effect = creds
    else:
        orch._cfg.load_credentials.return_value = dict(creds)
    orch._app_token = "xapp-stale"
    orch._bot_token = "xoxb-stale"
    orch._owner_id = ""
    orch._allowed_users = set()
    orch._slack_enabled = False
    orch._slack_connect_error = "invalid_auth"
    orch.slack = None
    orch._socket_client = old_client
    orch._slack_seen = MagicMock(name="boot-seen-cache")
    orch._slack_reconnect_task = None
    orch.dashboard_state = MagicMock()
    orch.dashboard_state.slack_socket_connected = False
    orch.dashboard_state.slack_connect_error = "invalid_auth"
    orch.dashboard_state.slack_client = None
    orch.dashboard_state.owner_id = "U0FORMEROWNER"
    orch._tracking_channels = set()
    orch._background_tasks = set()
    orch._slack_links_team_id = ""
    orch._slack_workspace_record_damaged = False
    # A private, empty state dir: the workspace record starts absent, and a
    # test can read back what a switch persisted. Removed after the test by
    # ``_remove_state_dirs``.
    state_dir = tempfile.TemporaryDirectory()
    _STATE_DIRS.append(state_dir)
    orch._slack_workspace_state_path = Path(state_dir.name) / "slack_workspace.json"
    orch.sessions = MagicMock(name="sessions")
    orch.sessions.clear_all_slack_links.return_value = []
    orch.sessions.snapshot_slack_links.return_value = []
    orch.sessions.restore_slack_links.return_value = []
    orch.sessions.aflush = AsyncMock(name="aflush")
    return orch


class _Recorder:
    """Stand-in for ``init_socket_mode`` that behaves like the real one's edges.

    On ``owner_missing`` it mirrors the real early return (flag off, no
    client); otherwise it installs a fresh client and records where it ran.
    """

    def __init__(self, *, owner_missing: bool = False, hold: asyncio.Event | None = None):
        self.calls: list[tuple[Any, Any]] = []
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.threads: list[threading.Thread] = []
        self.owner_missing = owner_missing
        self.hold = hold
        self.client = MagicMock(name="new-socket-client")

    async def __call__(self, orch: Any, seen: Any) -> None:
        self.calls.append((orch, seen))
        self.loops.append(asyncio.get_running_loop())
        self.threads.append(threading.current_thread())
        if self.hold is not None:
            await self.hold.wait()
        if not orch._slack_enabled:
            return
        if self.owner_missing or not orch._owner_id:
            orch._slack_enabled = False
            orch.slack = None
            return
        orch._socket_client = self.client


def _patches(recorder: _Recorder, connect: Any = None, client_cls: Any = None):
    """Patch the handshake seams; ``connect`` replaces ``_connect_slack``."""
    connect_mock = connect if connect is not None else AsyncMock(return_value=True)
    return (
        patch("kiro_crew.slack.events.init_socket_mode", recorder),
        patch("kiro_crew.slack.gateway.RealSlackClient", client_cls or MagicMock()),
        patch("kiro_crew.slack.gateway.GatewayOrchestrator._connect_slack", connect_mock),
    )


async def _run(
    orch: Any, recorder: _Recorder, connect: Any = None, client_cls: Any = None
) -> dict[str, object]:
    p_init, p_client, p_connect = _patches(recorder, connect, client_cls)
    with p_init, p_client, p_connect:
        return await orch.reconnect_slack()


# ── orchestrator ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconnect_recomputes_enabled_and_hoists_new_credentials() -> None:
    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock()
    orch = _orch(old_client=old)
    rec = _Recorder()
    client_cls = MagicMock(name="RealSlackClient")

    result = await _run(orch, rec, client_cls=client_cls)

    # The stale False is recomputed from the tokens now on disk -- the whole
    # point: without it init_socket_mode returns before doing anything.
    assert orch._slack_enabled is True
    assert orch._app_token == NEW_CREDS[CRED_SLACK_APP_TOKEN]
    assert orch._bot_token == NEW_CREDS[CRED_SLACK_BOT_TOKEN]
    assert orch._owner_id == NEW_CREDS[CRED_OWNER_ID]
    assert orch._allowed_users == {NEW_CREDS[CRED_OWNER_ID]}
    # Old client torn down before the new handshake; new one installed.
    old.close.assert_awaited_once()
    assert orch._socket_client is rec.client
    # The boot-time dedup cache is reused, so redelivered envelopes stay deduped.
    assert rec.calls == [(orch, orch._slack_seen)]
    # Web API client rebuilt on the new bot token and, the socket being
    # connected, mirrored to the dashboard; the dashboard's owner follows.
    client_cls.assert_called_once_with(NEW_CREDS[CRED_SLACK_BOT_TOKEN])
    assert orch.slack is client_cls.return_value
    assert orch.dashboard_state.slack_client is orch.slack
    assert orch.dashboard_state.owner_id == NEW_CREDS[CRED_OWNER_ID]
    # Outcome recorded where GET /api/slack/config reads it, and returned.
    assert orch.dashboard_state.slack_socket_connected is True
    assert orch.dashboard_state.slack_connect_error == ""
    assert result == {"connected": True, "connect_error": ""}


@pytest.mark.asyncio
async def test_reconnect_awaits_init_socket_mode_on_the_running_loop() -> None:
    """WSSocketModeClient needs a current loop in the constructing thread."""
    orch = _orch()
    rec = _Recorder()

    await _run(orch, rec)

    assert rec.loops == [asyncio.get_running_loop()]
    assert rec.threads == [threading.current_thread()]


@pytest.mark.asyncio
async def test_load_failure_leaves_the_live_connection_untouched() -> None:
    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock()
    orch = _orch(OSError("store unreadable"), old_client=old)
    rec = _Recorder()

    with pytest.raises(OSError):
        await _run(orch, rec)

    old.close.assert_not_awaited()
    assert orch._socket_client is old
    assert orch._app_token == "xapp-stale"
    assert orch._slack_enabled is False
    assert rec.calls == []
    # Nothing was recorded either: the badge still describes the live state.
    assert orch.dashboard_state.slack_connect_error == "invalid_auth"


@pytest.mark.asyncio
async def test_missing_tokens_disable_slack_without_a_handshake() -> None:
    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock()
    orch = _orch({CRED_OWNER_ID: "U0NEWOWNER"}, old_client=old)
    rec = _Recorder()

    result = await _run(orch, rec)

    old.close.assert_awaited_once()  # a cleared token must not keep the old socket alive
    assert orch._socket_client is None
    assert orch._slack_enabled is False
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert rec.calls == []
    assert result == {"connected": False, "connect_error": "tokens_missing"}
    assert orch.dashboard_state.slack_socket_connected is False
    assert orch.dashboard_state.slack_connect_error == "tokens_missing"


@pytest.mark.asyncio
async def test_missing_owner_is_named_for_the_badge() -> None:
    creds = {k: v for k, v in NEW_CREDS.items() if k != CRED_OWNER_ID}
    orch = _orch(creds)
    rec = _Recorder()

    result = await _run(orch, rec)

    assert len(rec.calls) == 1  # init_socket_mode ran (the flag was recomputed True)
    assert orch._socket_client is None
    assert result == {"connected": False, "connect_error": "owner_id_missing"}


@pytest.mark.asyncio
async def test_connect_failure_reason_is_surfaced() -> None:
    orch = _orch()
    rec = _Recorder()

    async def _connect(self: Any) -> bool:
        self._slack_connect_error = "invalid_auth"
        return False

    result = await _run(orch, rec, connect=_connect)

    assert result == {"connected": False, "connect_error": "invalid_auth"}
    assert orch.dashboard_state.slack_socket_connected is False
    assert orch.dashboard_state.slack_connect_error == "invalid_auth"


def _bind_former_owner() -> None:
    """Put the handler module in the state a booted gateway leaves it in."""
    from kiro_crew.slack import handler

    handler.set_allowed_users({FORMER_OWNER})
    handler.set_owner_id(FORMER_OWNER)
    assert handler.is_allowed_user(FORMER_OWNER)


def _unbind_handler() -> None:
    from kiro_crew.slack import handler

    handler.set_allowed_users(set())
    handler.set_owner_id("")


@pytest.mark.asyncio
async def test_old_client_close_failure_aborts_and_revokes_the_former_owner() -> None:
    """GPT F1: a close that fails leaves a listener up; the reconnect must not
    then hoist new credentials around it (tokens now missing -> no handshake ->
    the old listener keeps the former owner). Abort, keep the client referenced,
    clear the authorization subject, name the outcome."""
    from kiro_crew.slack import handler

    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock(side_effect=RuntimeError("websocket would not close"))
    orch = _orch({CRED_OWNER_ID: "U0NEWOWNER"}, old_client=old)  # tokens cleared on disk
    rec = _Recorder()
    _bind_former_owner()
    try:
        result = await _run(orch, rec)

        assert result == {"connected": False, "connect_error": "previous_client_close_failed"}
        assert orch._socket_client is old  # still referenced: retry / shutdown close it again
        assert rec.calls == []  # no handshake attempted around a live listener
        # Nothing from the store was hoisted.
        assert orch._app_token == "xapp-stale"
        assert orch._owner_id == ""
        assert orch._slack_enabled is False
        # The surviving listener authorizes nobody.
        assert handler.is_allowed_user(FORMER_OWNER) is False
        assert handler.is_owner(FORMER_OWNER) is False
        # The badge reads the failure; the mirror stays empty.
        assert orch.dashboard_state.slack_socket_connected is False
        assert orch.dashboard_state.slack_connect_error == "previous_client_close_failed"
        assert orch.dashboard_state.slack_client is None
    finally:
        _unbind_handler()


@pytest.mark.asyncio
async def test_close_timeout_is_a_failed_close() -> None:
    """The bounded close's timeout is a failed close too (it is an Exception)."""
    old = MagicMock(name="old-socket-client")  # close() is a plain MagicMock: wait_for is patched
    orch = _orch(old_client=old)
    rec = _Recorder()

    with patch("kiro_crew.slack.gateway.asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result = await _run(orch, rec)

    assert result["connect_error"] == "previous_client_close_failed"
    assert orch._socket_client is old
    assert rec.calls == []


@pytest.mark.asyncio
async def test_tokens_missing_rebinds_the_handler_subject_to_the_saved_owner() -> None:
    """The path that never reaches init_socket_mode must still move the
    handler module off the former owner (init_socket_mode is the only other
    writer of those globals)."""
    from kiro_crew.slack import handler

    old = MagicMock(name="old-socket-client")
    old.close = AsyncMock()
    orch = _orch({CRED_OWNER_ID: "U0NEWOWNER"}, old_client=old)
    rec = _Recorder()
    _bind_former_owner()
    try:
        result = await _run(orch, rec)

        assert result["connect_error"] == "tokens_missing"
        assert rec.calls == []
        assert handler.is_allowed_user(FORMER_OWNER) is False
        assert handler.is_owner("U0NEWOWNER") is True
    finally:
        _unbind_handler()


@pytest.mark.asyncio
async def test_cleared_owner_leaves_no_handler_subject() -> None:
    from kiro_crew.slack import handler

    creds = {k: v for k, v in NEW_CREDS.items() if k != CRED_OWNER_ID}
    orch = _orch(creds)
    rec = _Recorder()
    _bind_former_owner()
    try:
        result = await _run(orch, rec)

        assert result["connect_error"] == "owner_id_missing"
        assert handler.is_allowed_user(FORMER_OWNER) is False
        assert handler._owner_id == ""
        assert handler._allowed_users == set()
    finally:
        _unbind_handler()


@pytest.mark.asyncio
async def test_rejected_workspace_leaves_no_client_in_the_dashboard() -> None:
    """The enterprise gate's decline must not leave a sendable client behind.

    ``init_socket_mode`` clears ``orch.slack`` on that path but never touched
    the dashboard mirror, so a reconnect that published the mirror before the
    handshake would hand every dashboard Slack sender a client on a workspace
    the gate rejected.
    """
    old_web = MagicMock(name="old-web-client")
    orch = _orch()
    orch.slack = old_web
    orch.dashboard_state.slack_client = old_web
    seen: list[Any] = []

    class _Rejecting(_Recorder):
        async def __call__(self, orch: Any, seen_cache: Any) -> None:
            # What the dashboard held while the handshake ran.
            seen.append(orch.dashboard_state.slack_client)
            orch._slack_enabled = False
            orch.slack = None  # the real early return does this too

    result = await _run(orch, _Rejecting())

    assert seen == [None]  # the old client was gone BEFORE validation
    assert result == {"connected": False, "connect_error": "enterprise_validation_failed"}
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_socket_connected is False


@pytest.mark.asyncio
async def test_failed_handshake_publishes_no_client() -> None:
    """A client whose socket did not connect is not offered to the dashboard."""
    orch = _orch()

    async def _connect(self: Any) -> bool:
        self._slack_connect_error = "invalid_auth"
        return False

    await _run(orch, _Recorder(), connect=_connect)

    assert orch.slack is not None  # the orchestrator keeps its own handle, as at boot
    assert orch.dashboard_state.slack_client is None


@pytest.mark.asyncio
async def test_owner_change_moves_the_dashboard_owner() -> None:
    """``DashboardState.owner_id`` is the owner-only handlers' subject.

    It is set once from the boot-time owner; a reconnect that hoists a new
    owner without moving it would leave the former owner authorised.
    """
    orch = _orch()
    assert orch.dashboard_state.owner_id == "U0FORMEROWNER"

    await _run(orch, _Recorder())

    assert orch.dashboard_state.owner_id == NEW_CREDS[CRED_OWNER_ID]


@pytest.mark.asyncio
async def test_cleared_owner_clears_the_dashboard_owner() -> None:
    creds = {k: v for k, v in NEW_CREDS.items() if k != CRED_OWNER_ID}
    orch = _orch(creds)

    await _run(orch, _Recorder())

    assert orch.dashboard_state.owner_id == ""
    assert orch.dashboard_state.slack_client is None


@pytest.mark.asyncio
async def test_connected_reconnect_runs_the_tracked_channel_probe() -> None:
    """Boot warns about a tracked private channel the install cannot read; a
    reconnect that connects is the same moment and must warn the same way."""
    orch = _orch()
    orch._tracking_channels = {"C0TRACKED"}
    probe = AsyncMock()

    with patch("kiro_crew.slack.gateway.warn_unreadable_tracked_channels", probe):
        await _run(orch, _Recorder())
        await asyncio.sleep(0)  # let the fire-and-forget task start

    probe.assert_awaited_once()
    args, kwargs = probe.await_args
    assert args[0] is orch.slack
    assert args[1] == {"C0TRACKED"}
    assert kwargs["notify"] is orch.dashboard_state.notify


@pytest.mark.asyncio
async def test_failed_reconnect_skips_the_tracked_channel_probe() -> None:
    orch = _orch()
    orch._tracking_channels = {"C0TRACKED"}
    probe = AsyncMock()

    async def _connect(self: Any) -> bool:
        self._slack_connect_error = "invalid_auth"
        return False

    with patch("kiro_crew.slack.gateway.warn_unreadable_tracked_channels", probe):
        await _run(orch, _Recorder(), connect=_connect)
        await asyncio.sleep(0)

    probe.assert_not_awaited()


# ── workspace switch: persisted Slack destinations ───────────────────────────


def _team(team_id: str):
    """Pin the workspace the handshake 'validated' (what ``auth.test`` named)."""
    return patch(
        "kiro_crew.slack.gateway.GatewayOrchestrator._slack_validated_team_id",
        staticmethod(lambda: team_id),
    )


def _recorded_team(orch: Any) -> str | None:
    """What the workspace record beside the session map says, None when absent."""
    path = orch._slack_workspace_state_path
    return json.loads(path.read_text())["team_id"] if path.exists() else None


@pytest.mark.asyncio
async def test_workspace_switch_sweeps_persisted_links_before_publishing_the_client() -> None:
    """Credentials for ANOTHER workspace: every persisted Slack thread /
    channel link named a channel in the former workspace, so all of them go
    -- and are ON DISK -- before the dashboard is handed the new client: never
    a moment where a dashboard turn could combine the new client with an old
    destination, and never a crash window after publishing that a restart
    would fill with the swept rows restored under the new client."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    order: list[str] = []

    def _sweep() -> list[str]:
        order.append(f"sweep client={orch.dashboard_state.slack_client!r}")
        return ["dashboard:one", "slack:171.2"]

    async def _flush() -> None:
        order.append(
            f"flush client={orch.dashboard_state.slack_client!r} "
            f"recorded={_recorded_team(orch)!r}"
        )

    orch.sessions.clear_all_slack_links.side_effect = _sweep
    orch.sessions.aflush.side_effect = _flush

    with _team("T0NEW"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    # Sweep, then the awaited flush, both while the mirror is still empty and
    # the record still names the former workspace; the identity moves last.
    assert order == ["sweep client=None", "flush client=None recorded=None"]
    assert orch.dashboard_state.slack_client is orch.slack  # published afterwards
    assert orch._slack_links_team_id == "T0NEW"
    assert _recorded_team(orch) == "T0NEW"


@pytest.mark.asyncio
async def test_switch_flush_failure_keeps_the_former_identity_and_publishes_nothing() -> None:
    """The sweep is durable or the switch did not happen: a flush that raises
    leaves the record on the former workspace, so the next connect sweeps
    again instead of adopting the new workspace over unflushed rows. And the
    socket the handshake built does NOT stay live behind the failure: the
    attempt is refused like an unwritable record, the socket retired, the
    dashboard mirror left empty -- not an exception that skips the retirement
    and answers 500 while the listener keeps running.

    And the rows the sweep removed from MEMORY are put back before the raise
    leaves: the map is already dirty with the deletion, so its next deferred
    write (or ``aclose`` at shutdown) would land it under an identity that was
    never adopted -- and if the operator then reverts to the former workspace's
    tokens, the next connect sees no switch and never restores. The restore is
    in memory only (this disk just refused a write); the map's deferred flush
    lands it."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    swept = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    orch.sessions.snapshot_slack_links.return_value = swept
    orch.sessions.clear_all_slack_links.return_value = ["dashboard:one"]
    orch.sessions.restore_slack_links.return_value = ["dashboard:one"]
    orch.sessions.aflush.side_effect = OSError("disk full")
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unrecorded"}
    assert orch._slack_links_team_id == "T0FORMER"
    orch.sessions.restore_slack_links.assert_called_once_with(swept)
    orch.sessions.aflush.assert_awaited_once()  # the sweep's; the restore is not re-flushed here
    assert _recorded_team(orch) is None
    recorder.client.close.assert_awaited_once()
    assert orch._socket_client is None
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_socket_connected is False
    assert orch.dashboard_state.slack_connect_error == "workspace_identity_unrecorded"


@pytest.mark.asyncio
async def test_first_recorded_identity_keeps_existing_links() -> None:
    """Nothing recorded yet but Slack destinations persist: every install that
    predates the record is in this state on its first boot after upgrading.
    The links are KEPT and the workspace only recorded -- sweeping here would
    end every live mirror on installs whose workspace never changed. The
    record then protects every switch from this boot on."""
    orch = _orch()

    with _team("T0NEW"):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    orch.sessions.clear_all_slack_links.assert_not_called()
    orch.sessions.aflush.assert_not_awaited()
    assert orch.dashboard_state.slack_client is orch.slack
    assert orch._slack_links_team_id == "T0NEW"
    assert _recorded_team(orch) == "T0NEW"


@pytest.mark.asyncio
async def test_first_recorded_identity_on_a_fresh_install_is_quiet() -> None:
    """No record (a fresh install, or one upgrading onto the record): nothing
    is logged as cleared -- nothing was -- and the workspace is simply
    recorded so later connects have something to compare against."""
    orch = _orch()

    with _team("T0NEW"), patch("kiro_crew.slack.gateway.logger") as log:
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    assert orch._slack_links_team_id == "T0NEW"
    assert _recorded_team(orch) == "T0NEW"
    assert not [
        c for c in log.warning.call_args_list if "cleared" in str(c.args[0])
    ], "nothing was cleared, so nothing should say so"


@pytest.mark.asyncio
async def test_same_workspace_keeps_persisted_links() -> None:
    """A token rotation inside one workspace is not a switch: the links still
    name reachable channels and stripping them would silently end every mirror."""
    orch = _orch()
    orch._slack_links_team_id = "T0SAME"

    with _team("T0SAME"):
        await _run(orch, _Recorder())

    orch.sessions.clear_all_slack_links.assert_not_called()
    assert orch.dashboard_state.slack_client is orch.slack
    assert orch._slack_links_team_id == "T0SAME"
    assert _recorded_team(orch) is None  # unchanged identity, nothing rewritten


@pytest.mark.asyncio
async def test_unknown_identity_with_nothing_recorded_is_accepted() -> None:
    """No workspace was ever recorded and the handshake names none either:
    there are no former-workspace destinations to protect, so the connect
    stands (the pre-reconnect behaviour) and nothing is recorded."""
    orch = _orch()

    with _team(""):
        result = await _run(orch, _Recorder())

    assert result["connected"] is True
    orch.sessions.clear_all_slack_links.assert_not_called()
    assert orch.dashboard_state.slack_client is orch.slack
    assert orch._slack_links_team_id == ""
    assert _recorded_team(orch) is None


@pytest.mark.asyncio
async def test_unverified_workspace_with_recorded_destinations_refuses_to_publish() -> None:
    """Destinations recorded under a KNOWN workspace, and the handshake could
    not say which workspace the new tokens reach (``auth.test`` failed; the
    default gate passes that open): a switch would slip through unswept, so
    the socket is torn down again, nothing is published, nothing is swept, the
    record stays with the former workspace, and the badge names the reason."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team(""):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unverified"}
    recorder.client.close.assert_awaited_once()
    assert orch._socket_client is None
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_socket_connected is False
    assert orch.dashboard_state.slack_connect_error == "workspace_identity_unverified"
    orch.sessions.clear_all_slack_links.assert_not_called()
    orch.sessions.aflush.assert_not_awaited()
    assert orch._slack_links_team_id == "T0FORMER"
    assert _recorded_team(orch) is None


@pytest.mark.asyncio
async def test_unverified_socket_that_will_not_close_is_dropped_anyway() -> None:
    """The refused client is discarded either way: a close that raises is
    logged, not propagated, and the attempt still ends unpublished."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    recorder = _Recorder()
    recorder.client.close = AsyncMock(side_effect=RuntimeError("websocket gone"))

    with _team(""):
        result = await _run(orch, recorder)

    assert result["connected"] is False
    assert result["connect_error"] == "workspace_identity_unverified"
    assert orch._socket_client is None
    assert orch.slack is None


@pytest.mark.asyncio
async def test_boot_sweeps_when_the_record_names_another_workspace() -> None:
    """Boot binds through the same step as reconnect: credentials replaced
    while the gateway was down name another workspace than the record beside
    the session map, so the rows are swept and flushed and the record moves."""
    orch = _orch()
    orch._slack_workspace_state_path.write_text(json.dumps({"team_id": "T0FORMER"}))
    from kiro_crew.slack.gateway import _load_slack_links_team_id

    orch._slack_links_team_id = _load_slack_links_team_id(orch._slack_workspace_state_path)
    assert orch._slack_links_team_id == "T0FORMER"

    with _team("T0NEW"):
        await orch._adopt_slack_workspace(source="boot")

    orch.sessions.clear_all_slack_links.assert_called_once_with()
    orch.sessions.aflush.assert_awaited_once()
    assert orch._slack_links_team_id == "T0NEW"
    assert _recorded_team(orch) == "T0NEW"


def test_missing_record_reads_as_nothing_recorded(tmp_path: Any) -> None:
    from kiro_crew.slack.gateway import _load_slack_links_team_id, _store_slack_links_team_id

    path = tmp_path / "slack_workspace.json"
    assert _load_slack_links_team_id(path) == ""
    _store_slack_links_team_id(path, "T0NEW")
    assert _load_slack_links_team_id(path) == "T0NEW"


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"team_id": 7}', '{"other": "x"}'])
def test_damaged_record_reads_as_damaged_not_absent(tmp_path: Any, content: str) -> None:
    """A record that exists but does not parse is None, never "": taken for
    absent, the next bind would record whatever workspace the handshake names
    as the first and skip the switch check it exists for."""
    from kiro_crew.slack.gateway import _load_slack_links_team_id

    path = tmp_path / "slack_workspace.json"
    path.write_text(content)
    assert _load_slack_links_team_id(path) is None


def test_unreadable_record_reads_as_damaged(tmp_path: Any) -> None:
    from kiro_crew.slack.gateway import _load_slack_links_team_id

    path = tmp_path / "slack_workspace.json"
    path.mkdir()  # exists, but read_text raises IsADirectoryError (an OSError)
    assert _load_slack_links_team_id(path) is None


@pytest.mark.asyncio
async def test_damaged_record_refuses_to_publish_and_sweeps_nothing() -> None:
    """Boot found a record it could not read: the bind re-reads it, still
    cannot, and refuses -- socket retired, nothing published, nothing swept,
    nothing recorded over the damaged file. A damaged record is not an absent
    one: adopting the validated workspace as the first would skip the switch
    check for good, on the one boot where it may matter most."""
    orch = _orch()
    orch._slack_workspace_state_path.write_text("not json")
    orch._slack_workspace_record_damaged = True
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"):
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_record_unreadable"}
    orch.sessions.clear_all_slack_links.assert_not_called()
    assert orch._slack_workspace_state_path.read_text() == "not json"
    assert orch._slack_links_team_id == ""
    assert orch._slack_workspace_record_damaged is True
    recorder.client.close.assert_awaited_once()
    assert orch._socket_client is None
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_connect_error == "workspace_record_unreadable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repair", "expect_sweep", "recorded_after"),
    [
        ("remove", False, "T0NEW"),  # removed: nothing recorded, first record, links kept
        ("same", False, "T0SAME"),  # repaired to the same workspace: no switch
        ("former", True, "T0NEW"),  # repaired to the former one: the switch is caught
    ],
)
async def test_repaired_record_recovers_on_the_next_reconnect_without_a_restart(
    repair: str, expect_sweep: bool, recorded_after: str
) -> None:
    """The damaged reading is not cached: once the operator repaired or removed
    the file, the next Reconnect re-reads it and binds through the ordinary
    path -- and what it then does is decided by what the repaired record says."""
    orch = _orch()
    path = orch._slack_workspace_state_path
    path.write_text("not json")
    orch._slack_workspace_record_damaged = True
    validated = "T0SAME" if repair == "same" else "T0NEW"

    with _team(validated):
        first = await _run(orch, _Recorder())
        if repair == "remove":
            path.unlink()
        else:
            path.write_text(json.dumps({"team_id": "T0SAME" if repair == "same" else "T0FORMER"}))
        second = await _run(orch, _Recorder())

    assert first["connect_error"] == "workspace_record_unreadable"
    assert second["connected"] is True
    assert orch._slack_workspace_record_damaged is False
    assert orch._slack_links_team_id == recorded_after
    assert _recorded_team(orch) == recorded_after
    assert orch.sessions.clear_all_slack_links.called is expect_sweep
    assert orch.dashboard_state.slack_client is orch.slack


def test_orchestrator_boot_keeps_the_damaged_reading() -> None:
    """Source pin: construction records the loader's None as ``damaged`` (and
    "" as the identity) instead of collapsing both into "nothing recorded"."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator.__init__)
    assert "recorded_team = _load_slack_links_team_id(self._slack_workspace_state_path)" in src
    assert 'self._slack_links_team_id: str = recorded_team or ""' in src
    assert "self._slack_workspace_record_damaged: bool = recorded_team is None" in src


@pytest.mark.asyncio
async def test_failed_handshake_neither_sweeps_nor_moves_the_identity() -> None:
    """Nothing is published on a failed connect, so nothing can misroute; the
    links stay for the workspace that still owns them, and the recorded
    identity stays with them."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"

    async def _connect(self: Any) -> bool:
        self._slack_connect_error = "invalid_auth"
        return False

    with _team("T0NEW"):
        await _run(orch, _Recorder(), connect=_connect)

    orch.sessions.clear_all_slack_links.assert_not_called()
    assert orch._slack_links_team_id == "T0FORMER"
    assert orch.dashboard_state.slack_client is None


def test_validated_team_id_reads_the_enterprise_cache() -> None:
    from kiro_crew.slack import enterprise
    from kiro_crew.slack.gateway import GatewayOrchestrator

    with patch.object(enterprise, "_validated_team_id", "T0CACHED"):
        assert enterprise.validated_team_id() == "T0CACHED"
        assert GatewayOrchestrator._slack_validated_team_id() == "T0CACHED"


def test_stale_link_generation_is_refused(tmp_path: Any) -> None:
    """The in-flight-turn fence: a Slack turn that captured the generation
    before a workspace switch cannot re-persist its former-workspace thread
    after the sweep. A fresh capture writes; an unfenced write (no
    generation) is the dashboard's and always writes."""
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("slack:171.1", "sid-1")
        smap.set_slack_link("slack:171.1", "171.1", "C0FORMER")
        at_receipt = smap.slack_links_generation()

        # The switch lands while the turn is suspended.
        assert smap.clear_all_slack_links() == ["slack:171.1"]
        assert smap.slack_links_generation() == at_receipt + 1

        # The resumed turn presents its receipt generation: refused, nothing
        # written, nothing indexed.
        smap.set_slack_link("slack:171.1", "171.1", "C0FORMER", generation=at_receipt)
        assert smap.get_slack_link("slack:171.1") == (None, None)
        assert smap.get_session_for_thread("171.1") is None
        assert "slack_link_nonce" not in smap._data["slack:171.1"]

        # A turn received AFTER the switch writes; so does an unfenced writer.
        smap.set_slack_link(
            "slack:171.2", "171.2", "C0NEW", generation=smap.slack_links_generation()
        )
        assert smap.get_slack_link("slack:171.2") == ("171.2", "C0NEW")
        smap.set_slack_link("dashboard:one", "171.3", "C0NEW")
        assert smap.get_slack_link("dashboard:one") == ("171.3", "C0NEW")

        # The sweep bumps even with nothing to clear: the fence is about the
        # workspace having changed, not about how many rows named it.
        before = smap.slack_links_generation()
        smap.clear_all_slack_links()
        assert smap.slack_links_generation() == before + 1
        smap.set_slack_link("slack:171.2", "171.2", "C0NEW", generation=before)
        assert smap.get_slack_link("slack:171.2") == (None, None)


@pytest.mark.asyncio
async def test_manager_threads_the_generation_to_every_link_writer() -> None:
    """``SessionManager.set_slack_link`` / ``set_channel`` hand the generation
    to the map, and ``slack_links_generation`` reads it from there."""
    from kiro_crew.session import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._session_map = MagicMock()
    mgr._session_map.slack_links_generation.return_value = 4
    mgr._session_map.get_slack_link.return_value = ("171.1", None)

    assert mgr.slack_links_generation() == 4
    mgr.set_slack_link("slack:171.1", "171.1", "C1", generation=3)
    mgr._session_map.set_slack_link.assert_called_with("slack:171.1", "171.1", "C1", generation=3)
    await mgr.set_channel("slack:171.1", "C1", generation=3)
    mgr._session_map.set_slack_link.assert_called_with("slack:171.1", "171.1", "C1", generation=3)
    mgr.set_slack_link("dashboard:one", "171.2", "C1")
    mgr._session_map.set_slack_link.assert_called_with(
        "dashboard:one", "171.2", "C1", generation=None
    )


def test_clear_all_slack_links_sweeps_only_slack_destinations(tmp_path: Any) -> None:
    """The map sweep: every row that names a Slack thread goes (fields,
    reverse index, mute marker), a non-Slack mirror and a legacy namespaced
    ``slack_channel_id`` with no thread -- ``set_channel`` bookkeeping, not a
    destination -- are left alone, and the result is on disk."""
    from kiro_crew.messaging.link import ChannelLink
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("dashboard:one", "sid-1")
        smap.set_slack_link("dashboard:one", "171.1", "C0FORMER")
        smap.set_slack_paused("dashboard:one", True)
        smap.set("slack:171.2", "sid-2")
        smap.set_slack_link("slack:171.2", "171.2", "C0FORMER")
        smap.set("dashboard:tg", "sid-3")
        smap.set_mirror_link("dashboard:tg", ChannelLink("telegram", channel_id="99"))
        smap.set("discord:55", "sid-4")
        smap._data["discord:55"]["slack_channel_id"] = "discord:55"  # legacy bucket, no thread

        cleared = smap.clear_all_slack_links()

        assert sorted(cleared) == ["dashboard:one", "slack:171.2"]
        assert smap.get_slack_link("dashboard:one") == (None, None)
        assert smap.get_slack_link("slack:171.2") == (None, None)
        assert smap.get_session_for_thread("171.1") is None
        assert smap.get_session_for_thread("171.2") is None
        assert smap.is_slack_paused("dashboard:one") is False
        assert smap.get_mirror_link("dashboard:tg") == ChannelLink("telegram", channel_id="99")
        assert smap._data["discord:55"]["slack_channel_id"] == "discord:55"
        assert smap._data["dashboard:one"]["sid"] == "sid-1"  # the sessions themselves survive

        reloaded = SessionMap()
        assert reloaded.get_slack_link("dashboard:one") == (None, None)
        assert reloaded.get_session_for_thread("171.2") is None
        assert reloaded.get_mirror_link("dashboard:tg") == ChannelLink("telegram", channel_id="99")


def test_snapshot_then_restore_undoes_the_sweep_on_disk(tmp_path: Any) -> None:
    """The undo of a sweep whose workspace switch did not complete: the copy
    holds exactly the rows the sweep removes (thread, channel, nonce, mute --
    not the non-Slack mirror, not the legacy bucket), and restoring it brings
    the fields, the reverse index and the mute back, on disk, without moving
    the link generation back."""
    from kiro_crew.messaging.link import ChannelLink
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("dashboard:one", "sid-1")
        smap.set_slack_link("dashboard:one", "171.1", "C0FORMER")
        smap.set_slack_paused("dashboard:one", True)
        smap.set("slack:171.2", "sid-2")
        smap.set_slack_link("slack:171.2", "171.2", "C0FORMER")
        smap.set("dashboard:tg", "sid-3")
        smap.set_mirror_link("dashboard:tg", ChannelLink("telegram", channel_id="99"))
        smap.set("discord:55", "sid-4")
        smap._data["discord:55"]["slack_channel_id"] = "discord:55"
        nonce = smap.slack_link_nonce("dashboard:one")
        generation = smap.slack_links_generation()

        rows = smap.snapshot_slack_links()
        assert sorted(r["key"] for r in rows) == ["dashboard:one", "slack:171.2"]
        assert sorted(smap.clear_all_slack_links()) == ["dashboard:one", "slack:171.2"]
        assert smap.get_slack_link("dashboard:one") == (None, None)

        restored = smap.restore_slack_links(rows)

        assert sorted(restored) == ["dashboard:one", "slack:171.2"]
        assert smap.get_slack_link("dashboard:one") == ("171.1", "C0FORMER")
        assert smap.get_slack_link("slack:171.2") == ("171.2", "C0FORMER")
        assert smap.get_session_for_thread("171.1") == "dashboard:one"
        assert smap.get_session_for_thread("171.2") == "slack:171.2"
        assert smap.is_slack_paused("dashboard:one") is True
        assert smap.slack_link_nonce("dashboard:one") == nonce
        assert smap.slack_links_generation() == generation + 1  # the fence is not rewound
        assert smap._data["discord:55"]["slack_channel_id"] == "discord:55"

        reloaded = SessionMap()
        assert reloaded.get_slack_link("dashboard:one") == ("171.1", "C0FORMER")
        assert reloaded.get_session_for_thread("171.2") == "slack:171.2"
        assert reloaded.is_slack_paused("dashboard:one") is True


def test_restore_announces_each_binding_to_the_bind_listener(tmp_path: Any) -> None:
    """A restored row is a binding COMMITTED again after the sweep removed it,
    so the class recorder hears one bind per restored key -- after the save,
    and none for rows the restore skipped. Without it the sweep's unbind would
    be the last thing on record for a link that is live."""
    from kiro_crew import session_map as session_map_module
    from kiro_crew.session_map import SessionMap

    heard: list[str] = []
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        smap.set("dashboard:one", "sid-1")
        smap.set_slack_link("dashboard:one", "171.1", "C0FORMER")
        smap.set("dashboard:gone", "sid-2")
        smap.set_slack_link("dashboard:gone", "171.9", "C0FORMER")
        rows = smap.snapshot_slack_links()
        smap.clear_all_slack_links()
        smap.delete("dashboard:gone")

        def listener(key: str) -> None:
            heard.append(key)
            # Announced after the commit: the binding is already readable.
            assert smap.get_slack_link(key) == ("171.1", "C0FORMER")

        session_map_module.set_bind_listener(listener)
        try:
            assert smap.restore_slack_links(rows) == ["dashboard:one"]
        finally:
            session_map_module.set_bind_listener(None)

    assert heard == ["dashboard:one"]


def test_restore_skips_gone_sessions_and_keeps_newer_links(tmp_path: Any) -> None:
    """Between the sweep's flush and the restore the loop ran: a session the
    copy names may have been deleted (stays gone) or linked to another thread
    (keeps the newer link). Neither row is restored; the rest are."""
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        for key, ts in (("a", "1.1"), ("b", "1.2"), ("c", "1.3")):
            smap.set(f"dashboard:{key}", f"sid-{key}")
            smap.set_slack_link(f"dashboard:{key}", ts, "C0FORMER")
        rows = smap.snapshot_slack_links()
        smap.clear_all_slack_links()
        smap.delete("dashboard:a")
        smap.set_slack_link("dashboard:b", "9.9", "C0FORMER")

        restored = smap.restore_slack_links(rows)

        assert restored == ["dashboard:c"]
        assert smap.get("dashboard:a") is None
        assert smap.get_slack_link("dashboard:b") == ("9.9", "C0FORMER")
        assert smap.get_session_for_thread("1.2") is None
        assert smap.get_slack_link("dashboard:c") == ("1.3", "C0FORMER")


def test_restore_of_nothing_writes_nothing(tmp_path: Any) -> None:
    from kiro_crew.session_map import SessionMap

    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        smap = SessionMap()
        with patch.object(smap, "_save") as save:
            assert smap.restore_slack_links([]) == []
            assert smap.restore_slack_links([{"key": "dashboard:x", "slack_thread_ts": ""}]) == []
        save.assert_not_called()


def test_manager_passes_snapshot_and_restore_through() -> None:
    from kiro_crew.session import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr._session_map = MagicMock()
    mgr._session_map.snapshot_slack_links.return_value = [{"key": "k"}]
    mgr._session_map.restore_slack_links.return_value = ["k"]

    assert mgr.snapshot_slack_links() == [{"key": "k"}]
    assert mgr.restore_slack_links([{"key": "k"}]) == ["k"]
    mgr._session_map.restore_slack_links.assert_called_once_with([{"key": "k"}])


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_handshake() -> None:
    """A double-click must not race two Socket Mode handshakes."""
    orch = _orch()
    gate = asyncio.Event()
    rec = _Recorder(hold=gate)
    p_init, p_client, p_connect = _patches(rec)
    with p_init, p_client, p_connect:
        first = asyncio.create_task(orch.reconnect_slack())
        await asyncio.sleep(0)  # first attempt is now parked inside init_socket_mode
        second = asyncio.create_task(orch.reconnect_slack())
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(first, second)

    assert len(rec.calls) == 1
    assert results[0] == results[1] == {"connected": True, "connect_error": ""}
    assert orch._cfg.load_credentials.call_count == 1
    # The slot is released, so a later click starts a fresh attempt.
    assert orch._slack_reconnect_task is None


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_the_shared_attempt() -> None:
    orch = _orch()
    gate = asyncio.Event()
    rec = _Recorder(hold=gate)
    p_init, p_client, p_connect = _patches(rec)
    with p_init, p_client, p_connect:
        first = asyncio.create_task(orch.reconnect_slack())
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        gate.set()
        second = await orch.reconnect_slack()

    assert len(rec.calls) == 1  # the in-flight attempt finished and was reused
    assert second["connected"] is True


def test_gateway_wires_the_callback_onto_dashboard_state() -> None:
    """Source pin: the dashboard init publishes reconnect_slack for the route."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator._init_dashboard)
    assert "self.dashboard_state._slack_reconnect = self.reconnect_slack" in src


# ── route ────────────────────────────────────────────────────────────────────


def _request(state: Any) -> web.Request:
    app = web.Application()
    app["state"] = state
    return make_mocked_request("POST", "/api/slack/reconnect", app=app)


@pytest.mark.asyncio
async def test_route_answers_the_config_get_shape() -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    state = MagicMock()
    state._slack_reconnect = AsyncMock(
        return_value={"connected": False, "connect_error": "invalid_auth"}
    )
    sel = MagicMock()
    with (
        patch.object(mod, "is_direct_local_request", lambda req: True),
        patch.object(mod, "_sel", lambda: sel),
    ):
        resp = await mod.api_slack_reconnect(_request(state))

    assert resp.status == 200
    assert resp.text is not None
    import json

    assert json.loads(resp.text) == {"connected": False, "connect_error": "invalid_auth"}
    state._slack_reconnect.assert_awaited_once_with()
    kw = sel.log_api_access.call_args.kwargs
    assert kw["operation"] == "slack.reconnect"
    assert kw["outcome"] == "failed"
    assert kw["error"] == "invalid_auth"


@pytest.mark.asyncio
async def test_route_denies_remote_sessions_like_the_put() -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    state = MagicMock()
    state._slack_reconnect = AsyncMock()
    with (
        patch.object(mod, "is_direct_local_request", lambda req: False),
        patch.object(mod, "_sel", lambda: MagicMock()),
    ):
        resp = await mod.api_slack_reconnect(_request(state))

    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "remote_read_only"
    state._slack_reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_is_503_when_no_gateway_owns_a_socket() -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    state = MagicMock()
    state._slack_reconnect = None
    with (
        patch.object(mod, "is_direct_local_request", lambda req: True),
        patch.object(mod, "_sel", lambda: MagicMock()),
    ):
        resp = await mod.api_slack_reconnect(_request(state))

    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "slack_reconnect_unavailable"


@pytest.mark.asyncio
async def test_route_is_500_when_the_store_cannot_be_read() -> None:
    import kiro_crew.dashboard.handlers.messaging as mod

    state = MagicMock()
    state._slack_reconnect = AsyncMock(side_effect=OSError("store unreadable"))
    sel = MagicMock()
    with (
        patch.object(mod, "is_direct_local_request", lambda req: True),
        patch.object(mod, "_sel", lambda: sel),
    ):
        resp = await mod.api_slack_reconnect(_request(state))

    assert resp.status == 500
    assert json.loads(resp.body)["code"] == "credential_store_unreadable"
    assert sel.log_api_access.call_args.kwargs["outcome"] == "denied"


@pytest.mark.asyncio
async def test_two_concurrent_requests_share_one_attempt_through_the_route() -> None:
    """The route must not serialize concurrent clicks into two full attempts.

    A lock held by the HTTP handler would make the second click wait for the
    first attempt to finish and then run a fresh handshake, tearing down the
    socket the first had just established. Both requests have to reach the
    orchestrator while the first attempt is still running, so its coalescing
    (``test_concurrent_callers_share_one_handshake``) can fold them.
    """
    import kiro_crew.dashboard.handlers.messaging as mod

    in_flight = 0
    peak = 0
    release = asyncio.Event()

    async def slow_reconnect() -> dict[str, object]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await release.wait()
            return {"connected": True, "connect_error": ""}
        finally:
            in_flight -= 1

    state = MagicMock()
    state._slack_reconnect = slow_reconnect
    with (
        patch.object(mod, "is_direct_local_request", lambda req: True),
        patch.object(mod, "_sel", lambda: MagicMock()),
    ):
        first = asyncio.create_task(mod.api_slack_reconnect(_request(state)))
        second = asyncio.create_task(mod.api_slack_reconnect(_request(state)))
        for _ in range(5):
            await asyncio.sleep(0)
        assert peak == 2  # both reached the orchestrator before either finished
        release.set()
        r1, r2 = await asyncio.wait_for(asyncio.gather(first, second), 5)

    assert r1.status == r2.status == 200


@pytest.mark.asyncio
async def test_attempt_waits_for_the_config_lock_the_save_holds() -> None:
    """Reconnect must not read credentials while a save is writing them.

    Outside ``_get_config_lock()`` a Reconnect that lands mid-save snapshots
    the credentials the operator is replacing and hoists them AFTER the save
    commits: the former owner stays authorized on the live socket. The shared
    attempt takes the lock the PUT holds, so the read sees only a completed
    save.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    orch = _orch()
    rec = _Recorder()
    p_init, p_client, p_connect = _patches(rec)
    with p_init, p_client, p_connect:
        async with _get_config_lock():  # a save in flight
            attempt = asyncio.create_task(orch.reconnect_slack())
            for _ in range(5):
                await asyncio.sleep(0)
            orch._cfg.load_credentials.assert_not_called()  # blocked behind the save
        result = await asyncio.wait_for(attempt, 5)

    assert result == {"connected": True, "connect_error": ""}
    orch._cfg.load_credentials.assert_called_once_with()


@pytest.mark.asyncio
async def test_shared_attempt_keeps_the_lock_until_it_ends() -> None:
    """A caller that gives up mid-handshake must not release the lock early.

    The attempt is shielded from the caller's cancel and keeps running; if the
    lock followed the caller, a save could commit under that still-running
    read -- the same window.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    orch = _orch()
    gate = asyncio.Event()
    rec = _Recorder(hold=gate)
    p_init, p_client, p_connect = _patches(rec)
    with p_init, p_client, p_connect:
        caller = asyncio.create_task(orch.reconnect_slack())
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(rec.calls) == 1  # parked inside init_socket_mode
        caller.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert _get_config_lock().locked()  # still held while the attempt runs
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 5)
        shared = orch._slack_reconnect_task
        if shared is not None:
            await asyncio.wait_for(shared, 5)

    assert not _get_config_lock().locked()


def test_route_is_registered_as_post_beside_the_put() -> None:
    from kiro_crew.dashboard import handlers
    from kiro_crew.dashboard.routes import messaging as routes

    app = web.Application()
    routes.register(app)
    reconnect = [
        r
        for r in app.router.routes()
        if r.resource is not None and r.resource.canonical == "/api/slack/reconnect"
    ]
    assert [r.method for r in reconnect] == ["POST"]
    assert reconnect[0].handler is handlers.api_slack_reconnect


# ── Queued turns keep the client that received them across a reconnect ──


def _queue_orch(live_client: Any) -> MagicMock:
    """Orchestrator double for ``_dispatch_queued`` / ``_route_message``.

    ``orch.slack`` is whatever a reconnect made it; the test decides what the
    queue entry remembers. Mirrors test_message_queue's harness: transport off
    so the native ``handle_message`` patch is the one that runs.
    """
    from kiro_crew.config.loader import ACTIVATION_ALWAYS, KiroCrewConfig, MessagingConfig

    orch = MagicMock()
    orch._cfg = KiroCrewConfig(
        slack_channels={},
        slack_dm_activation=ACTIVATION_ALWAYS,
        messaging=MessagingConfig(use_transport=False),
    )
    orch.channel_history = MagicMock()
    orch.slack = live_client
    orch.sessions = MagicMock()
    orch.sessions.is_busy.return_value = False
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.cancel_queued = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.clear_queue = MagicMock()
    orch.sessions.has_session = MagicMock(return_value=False)
    orch.ctx_builder = None
    orch.cron_svc = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch.task_runner = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


@pytest.mark.asyncio
async def test_queued_turn_answers_through_the_client_that_received_it() -> None:
    """A reconnect to workspace B between enqueue and drain must not carry a
    workspace-A turn onto B's client: the reaction removal and the turn itself
    both use the client the queue entry bound at enqueue."""
    from kiro_crew.slack import events

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _queue_orch(workspace_b)  # the reconnect already happened
    kwargs = {"channel": "C_A", "thread_ts": "1.0", "slack_client": workspace_a}

    with patch.object(events, "handle_message", new_callable=AsyncMock) as hm:
        await events._dispatch_queued(orch, "1.0", "2.0", "follow up", kwargs)

    workspace_a.remove_reaction.assert_awaited_once_with("C_A", "2.0", "hourglass_flowing_sand")
    workspace_b.remove_reaction.assert_not_awaited()
    assert hm.await_args.args[0] is workspace_a


@pytest.mark.asyncio
async def test_queue_entry_without_a_bound_client_uses_the_live_one() -> None:
    """Entries queued before the key existed keep working."""
    from kiro_crew.slack import events

    live = AsyncMock(name="live")
    orch = _queue_orch(live)

    with patch.object(events, "handle_message", new_callable=AsyncMock) as hm:
        await events._dispatch_queued(orch, "1.0", "2.0", "follow up", {"channel": "C1"})

    live.remove_reaction.assert_awaited_once()
    assert hm.await_args.args[0] is live


@pytest.mark.asyncio
async def test_every_enqueue_site_binds_the_receiving_client() -> None:
    """All three queue writers in ``_route_message`` record ``orch.slack`` as
    it was when the message arrived: the session queue (busy task), the
    pre-session pending queue, and the semaphore-locked session queue."""
    from kiro_crew.slack.events import SeenCache, _route_message

    received_by = AsyncMock(name="received_by")
    patches = [
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock),
    ]
    for p in patches:
        p.start()
    try:
        # 1. Busy task, session object exists -> sessions.enqueue.
        orch = _queue_orch(received_by)
        orch._session_tasks["ts1"] = MagicMock()
        orch.sessions.enqueue.return_value = True
        event = {
            "user": "U1",
            "text": "q",
            "ts": "ts1",
            "channel": "D1",
            "channel_type": "im",
            "team": "T1",
        }
        await _route_message(orch, event, SeenCache(), is_mention=True)
        assert orch.sessions.enqueue.call_args.kwargs["slack_client"] is received_by

        # 2. Busy task, no session object yet -> orch._pending_queue.
        orch = _queue_orch(received_by)
        orch._session_tasks["thr"] = MagicMock()
        orch.sessions.enqueue.return_value = False
        event = {
            "user": "U1",
            "text": "q",
            "ts": "ts2",
            "thread_ts": "thr",
            "channel": "C1",
            "channel_type": "channel",
            "team": "T1",
        }
        await _route_message(orch, event, SeenCache(), is_mention=True)
        _ts, _text, kw = orch._pending_queue["thr"][0]
        assert kw["slack_client"] is received_by

        # 3. No task, but the session semaphore is locked -> sessions.enqueue.
        orch = _queue_orch(received_by)
        orch.sessions.enqueue.return_value = True
        event = {
            "user": "U1",
            "text": "q",
            "ts": "ts3",
            "channel": "D1",
            "channel_type": "im",
            "team": "T1",
        }
        await _route_message(orch, event, SeenCache(), is_mention=True)
        assert orch.sessions.enqueue.call_args.kwargs["slack_client"] is received_by
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_queue_binds_the_socket_client_even_when_routing_suspends() -> None:
    """``_route_message`` suspends before it enqueues (governance gate,
    ``users.info``, file downloads); a Reconnect in that gap swaps
    ``orch.slack`` to another workspace. The queue entry must carry the client
    of the socket that received the event, which the listener passes in --
    not whatever ``orch.slack`` is when the enqueue line runs."""
    from kiro_crew.slack.events import SeenCache, _route_message

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _queue_orch(workspace_a)
    orch._session_tasks["ts1"] = MagicMock()
    orch.sessions.enqueue.return_value = True

    async def _gate_that_reconnects(_channel: str) -> bool:
        orch.slack = workspace_b  # POST /api/slack/reconnect landed mid-route
        return True

    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.channel_inbound_permitted", _gate_that_reconnects),
        patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock),
    ):
        event = {
            "user": "U1",
            "text": "q",
            "ts": "ts1",
            "channel": "D1",
            "channel_type": "im",
            "team": "T1",
        }
        await _route_message(orch, event, SeenCache(), is_mention=True, slack_client=workspace_a)

    assert orch.slack is workspace_b  # the swap did happen mid-route
    assert orch.sessions.enqueue.call_args.kwargs["slack_client"] is workspace_a
    # The hourglass the drain removes through workspace_a was added through it.
    workspace_a.add_reaction.assert_awaited_once_with("D1", "ts1", "hourglass_flowing_sand")
    workspace_b.add_reaction.assert_not_awaited()


def test_listener_passes_its_own_client_to_route_message() -> None:
    """``init_socket_mode`` captures ``orch.slack`` once, beside the socket it
    builds, and hands it to every ``_route_message`` call."""
    import inspect

    from kiro_crew.slack import events

    src = inspect.getsource(events.init_socket_mode)
    assert "received_by = orch.slack" in src
    assert "slack_client=received_by," in src


def _swap_event() -> dict:
    return {
        "user": "U1",
        "text": "q",
        "ts": "ts1",
        "channel": "D1",
        "channel_type": "im",
        "team": "T1",
    }


@pytest.mark.asyncio
async def test_immediate_native_dispatch_uses_the_client_that_received_the_event() -> None:
    """The idle-session path dispatches ``handle_message`` straight away. A
    Reconnect that lands while routing is suspended must not make that turn
    answer through the new workspace: the handler gets the receiving socket's
    client, not ``orch.slack`` as it is at dispatch time."""
    from kiro_crew.slack.events import SeenCache, _route_message

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _queue_orch(workspace_a)

    async def _gate_that_reconnects(_channel: str) -> bool:
        orch.slack = workspace_b  # POST /api/slack/reconnect landed mid-route
        return True

    handled = AsyncMock()
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.channel_inbound_permitted", _gate_that_reconnects),
        patch("kiro_crew.slack.events.handle_message", handled),
    ):
        await _route_message(
            orch, _swap_event(), SeenCache(), is_mention=True, slack_client=workspace_a
        )
        await asyncio.gather(*orch._handler_tasks)

    assert orch.slack is workspace_b
    handled.assert_awaited_once()
    assert handled.await_args.args[0] is workspace_a


@pytest.mark.asyncio
async def test_immediate_transport_dispatch_uses_the_client_that_received_the_event() -> None:
    """Same invariant on the transport path (``handle_message_transport``)."""
    from kiro_crew.config.loader import MessagingConfig
    from kiro_crew.slack.events import SeenCache, _route_message

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _queue_orch(workspace_a)
    orch._cfg.messaging = MessagingConfig(use_transport=True)

    async def _gate_that_reconnects(_channel: str) -> bool:
        orch.slack = workspace_b
        return True

    handled = AsyncMock()
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.channel_inbound_permitted", _gate_that_reconnects),
        patch("kiro_crew.slack.events.handle_message_transport", handled),
    ):
        await _route_message(
            orch, _swap_event(), SeenCache(), is_mention=True, slack_client=workspace_a
        )
        await asyncio.gather(*orch._handler_tasks)

    assert orch.slack is workspace_b
    handled.assert_awaited_once()
    assert handled.await_args.args[0] is workspace_a


def test_route_message_never_reads_the_live_client_after_binding() -> None:
    """Enumeration guard: after ``received_by`` is bound on entry, no code line
    in ``_route_message`` reads ``orch.slack`` -- every event-scoped Web API
    call (user lookup, ephemeral denials, file download, stop/queue replies,
    both immediate dispatch branches, all enqueue sites) goes through the
    client of the socket that received the event."""
    import inspect

    from kiro_crew.slack import events

    lines = inspect.getsource(events._route_message).splitlines()
    bind = next(i for i, line in enumerate(lines) if "received_by = slack_client" in line)
    offenders = [
        line.strip()
        for line in lines[bind + 1 :]
        if "orch.slack" in line
        and not line.strip().startswith("#")
        and "orch.slack_command" not in line
    ]
    assert offenders == []


# ── in-flight turns: link generation captured at receipt ─────────────────────


def _generation_orch(client: Any, generation: int) -> MagicMock:
    orch = _queue_orch(client)
    orch.sessions.slack_links_generation = MagicMock(return_value=generation)
    return orch


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [False, True])
async def test_immediate_dispatch_carries_the_generation_captured_at_receipt(
    transport: bool,
) -> None:
    """Both dispatch branches hand the handler the link generation as of the
    event's RECEIPT, not as of dispatch: a workspace switch that sweeps and
    bumps while routing is suspended must leave this turn holding the OLD
    value, which is what ``set_slack_link`` refuses later."""
    from kiro_crew.config.loader import MessagingConfig
    from kiro_crew.slack.events import SeenCache, _route_message

    client = AsyncMock(name="workspace_a")
    orch = _generation_orch(client, 7)
    if transport:
        orch._cfg.messaging = MessagingConfig(use_transport=True)

    async def _gate_that_switches(_channel: str) -> bool:
        orch.sessions.slack_links_generation.return_value = 8  # the sweep landed
        return True

    handled = AsyncMock()
    target = "handle_message_transport" if transport else "handle_message"
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.channel_inbound_permitted", _gate_that_switches),
        patch(f"kiro_crew.slack.events.{target}", handled),
    ):
        await _route_message(orch, _swap_event(), SeenCache(), is_mention=True, slack_client=client)
        await asyncio.gather(*orch._handler_tasks)

    handled.assert_awaited_once()
    assert handled.await_args.kwargs["links_generation"] == 7


@pytest.mark.asyncio
async def test_every_enqueue_site_carries_the_generation_captured_at_receipt() -> None:
    """All three queue writers record the receipt generation beside the
    receiving client, and ``_dispatch_queued`` hands it on to the handler."""
    from kiro_crew.slack import events
    from kiro_crew.slack.events import SeenCache, _route_message

    client = AsyncMock(name="received_by")
    patches = [
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock),
    ]
    for p in patches:
        p.start()
    try:
        orch = _generation_orch(client, 3)
        orch._session_tasks["ts1"] = MagicMock()
        orch.sessions.enqueue.return_value = True
        await _route_message(orch, dict(_swap_event(), ts="ts1"), SeenCache(), is_mention=True)
        assert orch.sessions.enqueue.call_args.kwargs["links_generation"] == 3

        orch = _generation_orch(client, 3)
        orch._session_tasks["thr"] = MagicMock()
        orch.sessions.enqueue.return_value = False
        event = dict(_swap_event(), ts="ts2", thread_ts="thr", channel="C1", channel_type="channel")
        await _route_message(orch, event, SeenCache(), is_mention=True)
        _ts, _text, kw = orch._pending_queue["thr"][0]
        assert kw["links_generation"] == 3

        orch = _generation_orch(client, 3)
        orch.sessions.enqueue.return_value = True
        await _route_message(orch, dict(_swap_event(), ts="ts3"), SeenCache(), is_mention=True)
        assert orch.sessions.enqueue.call_args.kwargs["links_generation"] == 3
    finally:
        for p in patches:
            p.stop()

    orch = _queue_orch(client)
    with patch.object(events, "handle_message", new_callable=AsyncMock) as hm:
        await events._dispatch_queued(
            orch, "1.0", "2.0", "follow up", {"channel": "C_A", "links_generation": 3}
        )
    assert hm.await_args.kwargs["links_generation"] == 3


@pytest.mark.asyncio
async def test_sessions_double_without_a_generation_means_an_unfenced_turn() -> None:
    """A ``sessions`` that does not model the generation (older doubles, a
    stand-in) yields ``None`` -- an unfenced write -- never a bogus value the
    map would refuse."""
    from kiro_crew.slack.events import SeenCache, _route_message

    client = AsyncMock(name="received_by")
    orch = _queue_orch(client)
    orch.sessions.slack_links_generation = MagicMock(return_value=MagicMock())  # not an int
    handled = AsyncMock()
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.handle_message", handled),
    ):
        await _route_message(orch, _swap_event(), SeenCache(), is_mention=True, slack_client=client)
        await asyncio.gather(*orch._handler_tasks)

    assert handled.await_args.kwargs["links_generation"] is None


def test_every_slack_turn_link_write_is_fenced() -> None:
    """Enumeration guard over the three Slack-turn link writers: every
    ``set_slack_link`` / ``set_channel`` call in ``handler.py`` and
    ``transport_dispatch.py`` presents ``generation=links_generation``."""
    import inspect
    import re

    from kiro_crew.slack import handler, transport_dispatch

    offenders: list[str] = []
    for module in (handler, transport_dispatch):
        for line in inspect.getsource(module).splitlines():
            if re.search(r"sessions\.set_(slack_link|channel)\(", line) and (
                "generation=links_generation" not in line
            ):
                offenders.append(f"{module.__name__}: {line.strip()}")
    assert offenders == []


# ── workspace record: persisted before it is adopted ─────────────────────────


def _store_fails():
    """Make the workspace record unwritable (a full or read-only data dir)."""
    return patch(
        "kiro_crew.slack.gateway._store_slack_links_team_id",
        side_effect=OSError(28, "No space left on device"),
    )


@pytest.mark.asyncio
async def test_unwritable_record_on_a_switch_refuses_and_keeps_the_former_identity() -> None:
    """The identity is PERSISTED before it is adopted: a record that cannot be
    written leaves ``_slack_links_team_id`` on the former workspace and the
    attempt fails closed (socket torn down, nothing published). Adopting it in
    memory first would let the next boot re-read the former record, take the
    new workspace for a switch, and sweep every link it wrote since."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    swept = [{"key": "dashboard:one", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    orch.sessions.snapshot_slack_links.return_value = swept
    orch.sessions.clear_all_slack_links.return_value = ["dashboard:one"]
    orch.sessions.restore_slack_links.return_value = ["dashboard:one"]
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"), _store_fails():
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unrecorded"}
    # The switch did not happen, so its sweep is undone: the rows still name
    # the workspace this install remains bound to, and they are back on disk.
    orch.sessions.clear_all_slack_links.assert_called_once_with()
    orch.sessions.restore_slack_links.assert_called_once_with(swept)
    assert orch.sessions.aflush.await_count == 2  # the sweep's flush, then the restore's
    # Nothing adopted, nothing published, the new socket closed again.
    assert orch._slack_links_team_id == "T0FORMER"
    assert _recorded_team(orch) is None
    recorder.client.close.assert_awaited_once()
    assert orch._socket_client is None
    assert orch.slack is None
    assert orch.dashboard_state.slack_client is None
    assert orch.dashboard_state.slack_socket_connected is False
    assert orch.dashboard_state.slack_connect_error == "workspace_identity_unrecorded"


@pytest.mark.asyncio
async def test_failed_record_restores_the_swept_links_before_refusing() -> None:
    """Order pin for the undo: snapshot before the sweep (the copy is of the
    rows the sweep removes), and on the failed record write the restore and
    ITS flush both complete before the attempt is refused -- a refusal that
    returned first would leave the rows deleted for a switch that never took
    effect, which is the loss an unwritable crew home would otherwise cause."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    rows = [{"key": "slack:171.1", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}]
    order: list[str] = []
    orch.sessions.snapshot_slack_links.side_effect = lambda: order.append("snapshot") or rows
    orch.sessions.clear_all_slack_links.side_effect = lambda: order.append("sweep") or [
        "slack:171.1"
    ]
    orch.sessions.restore_slack_links.side_effect = lambda got: order.append(
        f"restore {got is rows}"
    ) or ["slack:171.1"]

    async def _flush() -> None:
        order.append("flush")

    orch.sessions.aflush.side_effect = _flush

    with _team("T0NEW"), _store_fails():
        result = await _run(orch, _Recorder())

    assert result["connect_error"] == "workspace_identity_unrecorded"
    assert order == ["snapshot", "sweep", "flush", "restore True", "flush"]
    assert orch._slack_links_team_id == "T0FORMER"


@pytest.mark.asyncio
async def test_restore_flush_failure_still_refuses_with_the_former_identity() -> None:
    """The restore's flush hits the same disk the record write failed on and
    may fail too. That is the same refusal (socket retired, nothing published,
    former identity kept) -- not an exception past the retirement -- with the
    rows restored in memory for the map's own deferred write to land."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    orch.sessions.snapshot_slack_links.return_value = [
        {"key": "slack:171.1", "slack_thread_ts": "171.1", "slack_channel_id": "C0OLD"}
    ]
    orch.sessions.aflush.side_effect = [None, OSError("disk full")]
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"), _store_fails():
        result = await _run(orch, recorder)

    assert result == {"connected": False, "connect_error": "workspace_identity_unrecorded"}
    orch.sessions.restore_slack_links.assert_called_once()
    assert orch._slack_links_team_id == "T0FORMER"
    assert _recorded_team(orch) is None
    recorder.client.close.assert_awaited_once()
    assert orch.slack is None


@pytest.mark.asyncio
async def test_nothing_swept_means_nothing_to_restore() -> None:
    """A first record (no former workspace) or an empty sweep has no rows to
    put back: the failed write refuses without a restore or a second flush."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"

    with _team("T0NEW"), _store_fails():
        result = await _run(orch, _Recorder())

    assert result["connect_error"] == "workspace_identity_unrecorded"
    orch.sessions.restore_slack_links.assert_not_called()
    orch.sessions.aflush.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_record_that_cannot_be_written_refuses_too() -> None:
    """Same on the very first record: without it the next boot cannot tell a
    switch from a rotation, so an unrecorded identity is not adopted."""
    orch = _orch()
    recorder = _Recorder()
    recorder.client.close = AsyncMock(name="close")

    with _team("T0NEW"), _store_fails():
        result = await _run(orch, recorder)

    assert result["connect_error"] == "workspace_identity_unrecorded"
    assert orch._slack_links_team_id == ""
    assert orch.slack is None
    recorder.client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_recorded_identity_is_on_disk_before_it_is_adopted_in_memory() -> None:
    """Order pin: at the moment the record is written the in-memory identity
    still names the former workspace; it moves only after the write returned."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"
    seen: list[str] = []

    def _store(path: Path, team_id: str) -> None:
        seen.append(f"store {team_id} while memory={orch._slack_links_team_id}")
        path.write_text(json.dumps({"team_id": team_id}))

    with _team("T0NEW"), patch("kiro_crew.slack.gateway._store_slack_links_team_id", _store):
        await _run(orch, _Recorder())

    assert seen == ["store T0NEW while memory=T0FORMER"]
    assert orch._slack_links_team_id == "T0NEW"


@pytest.mark.asyncio
async def test_retry_after_a_failed_record_does_not_sweep_the_new_workspace() -> None:
    """Because the identity stayed with the former workspace, the retry sees
    the same switch: it re-runs the (now empty) sweep and records the identity
    -- rather than, had the identity moved in memory, taking the new workspace
    for the recorded one and skipping the record for good."""
    orch = _orch()
    orch._slack_links_team_id = "T0FORMER"

    with _team("T0NEW"):
        with _store_fails():
            first = await _run(orch, _Recorder())
        second = await _run(orch, _Recorder())

    assert first["connect_error"] == "workspace_identity_unrecorded"
    assert second["connected"] is True
    assert orch._slack_links_team_id == "T0NEW"
    assert _recorded_team(orch) == "T0NEW"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous", "current", "code"),
    [
        ("T0FORMER", "", "workspace_identity_unverified"),
        ("T0FORMER", "T0NEW", "workspace_identity_unrecorded"),
        ("", "T0NEW", "workspace_identity_unrecorded"),
    ],
)
async def test_boot_binding_refuses_through_the_same_step(
    previous: str, current: str, code: str
) -> None:
    """Boot and reconnect share ``_bind_slack_workspace``: every refusal the
    reconnect step knows tears the boot socket down again, drops the live
    client and records the code where the settings badge reads it."""
    orch = _orch()
    orch._slack_links_team_id = previous
    socket = MagicMock(name="boot-socket")
    socket.close = AsyncMock(name="close")
    orch._socket_client = socket
    orch.slack = MagicMock(name="boot-web-client")

    with _team(current), _store_fails():
        result = await orch._bind_slack_workspace(source="boot")

    assert result == code
    socket.close.assert_awaited_once()
    assert orch._socket_client is None
    assert orch.slack is None
    assert orch._slack_connect_error == code
    assert orch._slack_links_team_id == previous
    assert _recorded_team(orch) is None


@pytest.mark.asyncio
async def test_boot_binding_that_succeeds_publishes_nothing_and_refuses_nothing() -> None:
    orch = _orch()
    socket = MagicMock(name="boot-socket")
    socket.close = AsyncMock(name="close")
    orch._socket_client = socket
    web = MagicMock(name="boot-web-client")
    orch.slack = web

    with _team("T0NEW"):
        assert await orch._bind_slack_workspace(source="boot") == ""

    socket.close.assert_not_awaited()
    assert orch._socket_client is socket
    assert orch.slack is web
    assert orch._slack_links_team_id == "T0NEW"


def test_boot_binds_through_the_refusing_step() -> None:
    """Source pin: ``run()`` binds through ``_bind_slack_workspace`` (which
    refuses) and withdraws the dashboard mirror on a refusal; a bare
    ``_adopt_slack_workspace`` call there would publish past a refusal."""
    import inspect

    from kiro_crew.slack.gateway import GatewayOrchestrator

    src = inspect.getsource(GatewayOrchestrator.run)
    assert 'await self._bind_slack_workspace(source="boot")' in src
    assert '_adopt_slack_workspace(source="boot")' not in src
    after = src.split('_bind_slack_workspace(source="boot")', 1)[1]
    assert "self.dashboard_state.slack_client = None" in after.split("slack_socket_connected", 1)[0]


# ── client affinity: work in flight keeps the client that received it ────────


def _affinity_orch(live: Any) -> Any:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.slack = live
    return orch


def test_slack_client_is_a_property_on_the_orchestrator() -> None:
    """Enumeration guard: every ``orch.slack`` read in the Slack package --
    ``interactions.py`` alone has dozens -- goes through ONE accessor, so the
    binding below covers all of them without touching each site."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    assert isinstance(GatewayOrchestrator.__dict__["slack"], property)


def test_bound_context_wins_over_the_live_client() -> None:
    """Inside a binding ``orch.slack`` is the bound client, however the live
    one changes; outside it is the live one. ``None`` is a real binding."""
    from kiro_crew.slack import affinity

    workspace_a = MagicMock(name="workspace_a")
    workspace_b = MagicMock(name="workspace_b")
    orch = _affinity_orch(workspace_a)

    assert orch.slack is workspace_a
    with affinity.client_scope(workspace_a):
        orch.slack = workspace_b  # POST /api/slack/reconnect landed
        assert orch.slack is workspace_a
        with affinity.client_scope(None):
            assert orch.slack is None
        assert orch.slack is workspace_a
    assert orch.slack is workspace_b
    assert affinity.bound_client() is affinity.UNBOUND


@pytest.mark.asyncio
async def test_tasks_spawned_inside_the_binding_keep_it_across_a_reconnect() -> None:
    """The agent turn an envelope starts runs in tasks of its own; they inherit
    the binding, so their final post after a reconnect still goes through the
    client that received the envelope."""
    from kiro_crew.slack import affinity

    workspace_a = MagicMock(name="workspace_a")
    workspace_b = MagicMock(name="workspace_b")
    orch = _affinity_orch(workspace_a)
    reconnected = asyncio.Event()

    async def _turn() -> Any:
        await reconnected.wait()  # the agent is thinking; the operator clicks Reconnect
        return orch.slack

    with affinity.client_scope(workspace_a):
        task = asyncio.create_task(_turn())
    await asyncio.sleep(0)
    orch.slack = workspace_b
    reconnected.set()

    assert await task is workspace_a
    assert orch.slack is workspace_b


def _envelope(req_type: str, payload: dict | None = None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(type=req_type, payload=payload or {}, envelope_id="env-1")


async def _install_listener(orch: Any) -> Any:
    """Run the real ``init_socket_mode`` against a mocked Socket Mode client
    and return the listener it installed."""
    from kiro_crew.slack import events

    client_cls = MagicMock(name="WSSocketModeClient")
    client_cls.return_value.socket_mode_request_listeners = []
    ctx = MagicMock()
    ctx.return_value.slack_gate.validate_enterprise.return_value = True
    with (
        patch("kiro_crew.slack.events.WSSocketModeClient", client_cls),
        patch("kiro_crew.slack.events.AsyncWebClient", MagicMock()),
        patch("kiro_crew.slack.events.current_context", ctx),
        patch("kiro_crew.slack.events.set_allowed_users"),
        patch("kiro_crew.slack.events.set_tracking_channels"),
        patch("kiro_crew.slack.events.set_open_channels"),
        patch("kiro_crew.slack.events.set_owner_id"),
        patch("kiro_crew.slack.events.set_orch_cfg"),
        patch("kiro_crew.slack.events.set_dashboard_state"),
        patch("kiro_crew.slack.events.set_yolo_mode"),
    ):
        await events.init_socket_mode(orch, events.SeenCache())
    return orch._socket_client.socket_mode_request_listeners[0]


def _listener_orch(live_client: Any) -> MagicMock:
    orch = _queue_orch(live_client)
    orch._slack_enabled = True
    orch._bot_token = "xoxb-not-a-real-value"
    orch._app_token = "xapp-not-a-real-value"
    orch._owner_id = "U_OWNER"
    orch._allowed_users = {"U_OWNER"}
    orch._tracking_channels = set()
    orch._open_channels = set()
    orch._approval_mode = ""
    orch.slack_command = "kirocrew"
    orch._socket_client = None
    orch.sessions.is_paused_for_update = MagicMock(return_value=False)
    return orch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("req_type", "payload", "target"),
    [
        ("interactive", {"type": "block_actions"}, "dispatch_interactive"),
        ("slash_commands", {"command": "/kirocrew", "user_id": "U_OWNER"}, "_handle_slash"),
    ],
)
async def test_listener_binds_the_receiving_client_to_every_envelope(
    req_type: str, payload: dict, target: str
) -> None:
    """Interactions and slash commands answer through ``orch.slack`` reads
    scattered over ``interactions.py``; the listener binds the client that
    received the envelope to the envelope's task, so those reads resolve to
    it even after a reconnect swapped the live client mid-handler."""
    from kiro_crew.slack import affinity

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _listener_orch(workspace_a)
    seen: list[Any] = []
    reconnected = asyncio.Event()

    async def _handler(*_a: Any, **_k: Any) -> None:
        await reconnected.wait()
        seen.append(affinity.bound_client())

    socket = MagicMock(name="socket")
    socket.send_socket_mode_response = AsyncMock()
    on_event = await _install_listener(orch)
    with (
        patch("kiro_crew.slack.events.admit_inbound_callback", AsyncMock(return_value=True)),
        patch(f"kiro_crew.slack.events.{target}", _handler),
    ):
        await on_event(socket, _envelope(req_type, payload))
        await asyncio.sleep(0)
        orch.slack = workspace_b  # POST /api/slack/reconnect landed mid-handler
        reconnected.set()
        await asyncio.gather(*orch._handler_tasks)

    assert seen == [workspace_a]
    assert affinity.bound_client() is affinity.UNBOUND  # nothing leaked out of the envelope


@pytest.mark.asyncio
async def test_listener_binds_the_receiving_client_to_message_events() -> None:
    from kiro_crew.slack import affinity

    workspace_a = AsyncMock(name="workspace_a")
    orch = _listener_orch(workspace_a)
    seen: list[Any] = []

    async def _route(*_a: Any, **_k: Any) -> None:
        seen.append(affinity.bound_client())

    socket = MagicMock(name="socket")
    socket.send_socket_mode_response = AsyncMock()
    on_event = await _install_listener(orch)
    with (
        patch("kiro_crew.slack.events.admit_inbound_callback", AsyncMock(return_value=True)),
        patch("kiro_crew.slack.events._route_message", _route),
    ):
        await on_event(
            socket,
            _envelope("events_api", {"event": {"type": "message", **_swap_event()}}),
        )

    assert seen == [workspace_a]


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep_during", ["admission", "ack"])
async def test_listener_reads_the_generation_before_its_first_await(sweep_during: str) -> None:
    """The admission gate and the ACK both suspend before a message reaches
    ``_route_message``; a workspace switch that sweeps in either gap bumps
    the generation. The listener reads it at envelope ENTRY, so the turn
    carries the pre-sweep value and ``set_slack_link`` refuses its
    former-workspace thread -- a read after those awaits would be the current
    value and pass the row through."""
    workspace_a = AsyncMock(name="workspace_a")
    orch = _listener_orch(workspace_a)
    orch.sessions.slack_links_generation = MagicMock(return_value=5)
    carried: list[Any] = []

    async def _route(*_a: Any, **kw: Any) -> None:
        carried.append(kw.get("links_generation"))

    async def _admit(*_a: Any, **_k: Any) -> bool:
        if sweep_during == "admission":
            orch.sessions.slack_links_generation.return_value = 6
        return True

    async def _ack(*_a: Any, **_k: Any) -> None:
        if sweep_during == "ack":
            orch.sessions.slack_links_generation.return_value = 6

    socket = MagicMock(name="socket")
    socket.send_socket_mode_response = _ack
    on_event = await _install_listener(orch)
    with (
        patch("kiro_crew.slack.events.admit_inbound_callback", _admit),
        patch("kiro_crew.slack.events._route_message", _route),
    ):
        await on_event(
            socket,
            _envelope("events_api", {"event": {"type": "message", **_swap_event()}}),
        )

    assert carried == [5]
    assert orch.sessions.slack_links_generation.return_value == 6  # the sweep did land


@pytest.mark.asyncio
async def test_route_message_keeps_the_generation_it_was_handed() -> None:
    """Given a receipt value, ``_route_message`` does not read a fresh one:
    what the listener captured is what reaches the handler."""
    from kiro_crew.slack.events import SeenCache, _route_message

    client = AsyncMock(name="workspace_a")
    orch = _generation_orch(client, 6)  # the live value has already moved on
    handled = AsyncMock()
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
        patch("kiro_crew.slack.events.channel_inbound_permitted", AsyncMock(return_value=True)),
        patch("kiro_crew.slack.events.handle_message", handled),
    ):
        await _route_message(
            orch,
            _swap_event(),
            SeenCache(),
            is_mention=True,
            slack_client=client,
            links_generation=5,
        )
        await asyncio.gather(*orch._handler_tasks)

    assert handled.await_args.kwargs["links_generation"] == 5
    orch.sessions.slack_links_generation.assert_not_called()


def test_listener_reads_the_generation_before_the_binding_scope() -> None:
    """Source pin: in ``_on_envelope`` the generation read precedes the
    affinity scope and the ``_on_event`` await, and ``_on_event`` passes it
    down to ``_route_message`` instead of reading its own."""
    import inspect

    from kiro_crew.slack import events

    src = inspect.getsource(events.init_socket_mode)
    envelope = src.split("async def _on_envelope(", 1)[1].split("async def _on_event(", 1)[0]
    read_at = envelope.index("_links_generation_at_receipt(orch)")
    assert read_at < envelope.index("client_scope(")
    assert read_at < envelope.index("await ")
    on_event = src.split("async def _on_event(", 1)[1]
    assert "_links_generation_at_receipt" not in on_event
    assert "links_generation=links_generation" in on_event


@pytest.mark.asyncio
async def test_queued_dispatch_binds_the_entry_client_for_the_whole_turn() -> None:
    """The drain runs in its own task, so the listener's binding does not
    reach it: ``_dispatch_queued`` binds the queue entry's client itself, and
    the handler (and whatever it spawns) reads that client back."""
    from kiro_crew.slack import affinity, events

    workspace_a = AsyncMock(name="workspace_a")
    workspace_b = AsyncMock(name="workspace_b")
    orch = _queue_orch(workspace_b)  # the reconnect already happened
    seen: list[Any] = []

    async def _handler(*_a: Any, **_k: Any) -> None:
        seen.append(affinity.bound_client())

    with patch.object(events, "handle_message", _handler):
        await events._dispatch_queued(
            orch, "1.0", "2.0", "follow up", {"channel": "C_A", "slack_client": workspace_a}
        )

    assert seen == [workspace_a]
    assert affinity.bound_client() is affinity.UNBOUND


def test_every_envelope_kind_is_dispatched_inside_the_binding() -> None:
    """Source pin: the listener binds BEFORE any dispatch -- the ack, the
    interactive task, the slash task and the message routing all live in the
    bound body (``_on_event``), none in the registered wrapper
    (``_on_envelope``); and the wrapper is what the socket gets."""
    import inspect

    from kiro_crew.slack import events

    src = inspect.getsource(events.init_socket_mode)
    outer = src.split("async def _on_envelope(", 1)[1].split("async def _on_event(", 1)[0]
    assert "with slack_affinity.client_scope(received_by):" in outer
    assert "await _on_event(client, req, links_generation=links_generation)" in outer
    assert "create_task" not in outer and "_route_message" not in outer
    assert "socket_mode_request_listeners.append(_on_envelope)" in src
    # The reserve-before-ack contract (test_update_check_install_aware) reads
    # ``_on_event``; the wrapper must add no suspension ahead of it.
    assert outer.count("await") == 1


# ── workspace record: rides in the config snapshot beside the map ─────────────


def test_workspace_record_is_a_config_component_file_beside_the_session_map() -> None:
    """A config snapshot restored to a replacement host whose credentials name
    another workspace must carry the workspace record WITH the session map:
    without it the restored rows have no identity beside them, the boot's
    switch detection treats "no record" as a first boot, and every
    workspace-A destination is kept under workspace B. Same component, so a
    selective restore cannot separate the two; and JSON-object validated, so a
    misshapen restore is refused rather than read as a damaged record that
    takes Slack down."""
    from kiro_crew import snapshot_components as sc
    from kiro_crew.slack.gateway import SLACK_WORKSPACE_STATE_FILENAME

    config_files = sc.CORE_FILES["config"]
    assert SLACK_WORKSPACE_STATE_FILENAME in config_files
    assert "session_map.json" in config_files
    assert SLACK_WORKSPACE_STATE_FILENAME in sc.COMPONENT_JSON_OBJECTS
    assert SLACK_WORKSPACE_STATE_FILENAME in sc.CORE_FILES_FLAT
    assert SLACK_WORKSPACE_STATE_FILENAME in sc.COMPONENT_HELP["config"]


# ── interactive callbacks present the receipt generation ─────────────────────


def _resume_orch(generation_reads: list[int]) -> MagicMock:
    """An interactions orchestrator whose map reports *generation_reads* in
    turn (the last value repeats), with a resumable session and a dashboard."""
    orch = MagicMock(name="orch")
    orch.slack = AsyncMock(name="workspace_a")
    orch.slack.post_message = AsyncMock(return_value="ts1")
    orch.slack.open_dm = AsyncMock(return_value="D1")
    orch.sessions = MagicMock(name="sessions")
    orch.sessions.get_slack_link = MagicMock(return_value=("", ""))
    orch.sessions.set_slack_link = MagicMock()
    reads = iter(generation_reads)
    last = generation_reads[-1]

    def _gen() -> int:
        nonlocal last
        last = next(reads, last)
        return last

    orch.sessions.slack_links_generation = MagicMock(side_effect=_gen)
    orch.dashboard_state = MagicMock(name="dashboard_state")
    return orch


_RESUME_KEY = "dashboard_r16"


def _resume_action(key: str = _RESUME_KEY) -> dict:
    return {"action_id": "mc_resume_thread_x", "value": json.dumps({"key": key, "title": "T"})}


@pytest.fixture
def _own_resume_lock() -> Any:
    """Drop this module's entry from ``interactions._resume_locks`` afterwards:
    the map is module-global and a sibling test counts it."""
    from kiro_crew.slack import interactions as ix

    yield
    ix._resume_locks.pop(_RESUME_KEY, None)


def _resume_payload() -> dict:
    return {"user": {"id": "U_OWNER"}, "channel": {"id": "C1"}, "message": {"ts": "m1"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["thread", "dm"])
async def test_resume_choice_refuses_both_link_writes_when_the_workspace_switched(
    mode: str, _own_resume_lock: Any
) -> None:
    """The resume posted its header through several awaits; a workspace-switching
    Reconnect inside them swept every persisted destination and bumped the
    generation. Presenting the receipt value, the callback skips the map write
    AND the dashboard link together -- half a link is the two-owner state the
    batched save exists to prevent -- and records the refusal."""
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([7])  # receipt was 5; the map now reads 7

    async def _post(*_a: Any, **_k: Any) -> str:
        return "ts1"

    orch.slack.post_message = AsyncMock(side_effect=_post)
    sel = MagicMock()
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_owner", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, "sel", lambda: sel),
    ):
        await ix._handle_resume_choice(
            _resume_payload(),
            _resume_action(),
            "C1",
            "m1",
            "U_OWNER",
            mode=mode,
            links_generation=5,
        )

    orch.sessions.set_slack_link.assert_not_called()
    orch.dashboard_state.link_slack.assert_not_called()
    refused = [
        c.kwargs for c in sel.log_api_access.call_args_list if c.kwargs.get("outcome") == "refused"
    ]
    assert refused and refused[0]["operation"] == "slack.session_resume"


@pytest.mark.asyncio
async def test_resume_choice_links_with_the_receipt_generation_when_unchanged(
    _own_resume_lock: Any,
) -> None:
    """Same workspace throughout: both writes land, and the map write presents
    the receipt generation so the map's own fence can judge it too."""
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([5])
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_owner", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, "sel", lambda: MagicMock()),
    ):
        await ix._handle_resume_choice(
            _resume_payload(),
            _resume_action(),
            "C1",
            "m1",
            "U_OWNER",
            mode="thread",
            links_generation=5,
        )

    orch.sessions.set_slack_link.assert_called_once_with(_RESUME_KEY, "ts1", "C1", generation=5)
    orch.dashboard_state.link_slack.assert_called_once_with(_RESUME_KEY, "ts1", "C1")


@pytest.mark.asyncio
async def test_resume_choice_without_a_receipt_generation_is_unfenced(
    _own_resume_lock: Any,
) -> None:
    """``None`` means no generation was captured; the fence is opt-in per turn,
    as it is for message turns, so the write goes through (with ``None``)."""
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([9])
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_owner", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, "sel", lambda: MagicMock()),
    ):
        await ix._handle_resume_choice(
            _resume_payload(), _resume_action(), "C1", "m1", "U_OWNER", mode="thread"
        )

    orch.sessions.set_slack_link.assert_called_once_with(_RESUME_KEY, "ts1", "C1", generation=None)
    orch.dashboard_state.link_slack.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_id", ["mc_resume_thread_x", "mc_resume_dm_x"])
async def test_dispatch_threads_the_generation_to_the_resume_choice(action_id: str) -> None:
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([5])
    handled = AsyncMock()
    payload = {**_resume_payload(), "actions": [{"action_id": action_id, "value": "{}"}]}
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_allowed_user", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, "_handle_resume_choice", handled),
    ):
        await ix.dispatch(payload, links_generation=5)

    assert handled.await_args.kwargs["links_generation"] == 5


@pytest.mark.asyncio
async def test_dispatch_threads_the_generation_to_the_link_dashboard_import() -> None:
    from kiro_crew.slack import interactions as ix

    orch = _resume_orch([5])
    orch.dashboard_state.get_or_create_slot = MagicMock()
    importer = AsyncMock(return_value=None)
    payload = {
        **_resume_payload(),
        "message": {"ts": "m1", "thread_ts": "200.0"},
        "actions": [{"action_id": ix.LINK_DASHBOARD_ACTION, "value": ""}],
    }
    with (
        patch.object(ix, "_orch", orch),
        patch.object(ix, "is_allowed_user", lambda uid: True),
        patch.object(ix, "channel_inbound_permitted", AsyncMock(return_value=True)),
        patch.object(ix, "sel", lambda: MagicMock()),
        patch.object(ix, "_import_thread_to_slot", importer),
    ):
        await ix.dispatch(payload, links_generation=5)

    assert importer.await_args.kwargs["links_generation"] == 5


@pytest.mark.asyncio
async def test_thread_import_refuses_the_link_when_the_workspace_switched_mid_fetch() -> None:
    """The import's await is ``fetch_thread_replies``; a switch inside it bumps
    the generation. Decided after the fetch and before the slot exists, so a
    refused import leaves nothing behind: no slot, no dashboard link."""
    from kiro_crew.slack import interactions as ix

    ds = MagicMock(name="dashboard_state")
    ds.get_linked_slot = MagicMock(return_value=None)
    ds.sessions.slack_links_generation = MagicMock(return_value=5)
    slack = MagicMock(name="workspace_a")

    async def _fetch(*_a: Any, **_k: Any) -> list[dict]:
        ds.sessions.slack_links_generation.return_value = 6  # the sweep landed here
        return [{"user": "U1", "text": "hello"}]

    slack.fetch_thread_replies = AsyncMock(side_effect=_fetch)

    result = await ix._import_thread_to_slot(slack, ds, "C1", "100.0", links_generation=5)

    assert result is None
    ds.get_or_create_slot.assert_not_called()
    ds.link_slack.assert_not_called()


@pytest.mark.asyncio
async def test_thread_import_links_when_the_generation_is_unchanged() -> None:
    from kiro_crew.slack import interactions as ix

    ds = MagicMock(name="dashboard_state")
    ds.get_linked_slot = MagicMock(return_value=None)
    ds.sessions.slack_links_generation = MagicMock(return_value=5)
    ds._self_bot_id = "B1"
    slot = MagicMock(name="slot")
    slot.key = "s1"
    ds.get_or_create_slot = MagicMock(return_value=slot)
    slack = MagicMock(name="workspace_a")
    slack.fetch_thread_replies = AsyncMock(return_value=[{"user": "U1", "text": "hello"}])

    with patch("kiro_crew.dashboard.chat_persistence.save_slot_off_loop", AsyncMock()):
        result = await ix._import_thread_to_slot(slack, ds, "C1", "100.0", links_generation=5)

    assert result is slot
    ds.link_slack.assert_called_once_with("s1", "100.0", "C1")


@pytest.mark.asyncio
async def test_listener_hands_the_receipt_generation_to_interactive_dispatch() -> None:
    """The interactive envelope kind carries the same receipt value the message
    kind does; a resume or link-to-dashboard callback is one Slack turn."""
    workspace_a = AsyncMock(name="workspace_a")
    orch = _listener_orch(workspace_a)
    orch.sessions.slack_links_generation = MagicMock(return_value=5)
    carried: list[Any] = []

    async def _dispatch(*_a: Any, **kw: Any) -> None:
        carried.append(kw.get("links_generation"))

    socket = MagicMock(name="socket")
    socket.send_socket_mode_response = AsyncMock()
    on_event = await _install_listener(orch)
    with (
        patch("kiro_crew.slack.events.admit_inbound_callback", AsyncMock(return_value=True)),
        patch("kiro_crew.slack.events.dispatch_interactive", _dispatch),
    ):
        await on_event(socket, _envelope("interactive", {"type": "block_actions"}))
        await asyncio.gather(*orch._handler_tasks)

    assert carried == [5]


def test_slash_command_threads_the_generation_to_the_thread_import() -> None:
    """Source pin over ``handler.py``: every ``_handle_slash_command`` call in
    ``handle_message`` forwards ``links_generation``, and the one command that
    writes a Slack link (``!link-to-dashboard``) presents it to the import."""
    import inspect
    import re

    from kiro_crew.slack import handler

    body = inspect.getsource(handler.handle_message)
    calls = re.findall(r"_handle_slash_command\((?:[^()]|\([^()]*\))*\)", body, re.S)
    assert len(calls) == 2, calls
    assert all("links_generation=links_generation" in c for c in calls)
    slash = inspect.getsource(handler._handle_slash_command)
    imports = re.findall(r"_import_thread_to_slot\((?:[^()]|\([^()]*\))*\)", slash, re.S)
    assert imports and all("links_generation=links_generation" in c for c in imports)


def test_every_interactive_link_write_is_fenced() -> None:
    """Enumeration guard over ``interactions.py``, the module the message-path
    guard (``test_every_slack_turn_link_write_is_fenced``) does not cover.
    Every ``sessions.set_slack_link`` there presents ``generation=``, and every
    function that links through ``dashboard_state.link_slack`` -- which has no
    generation to present -- gates on ``_slack_links_stale`` first.

    Enumerated and deliberately NOT fenced: ``gateway.py``'s cron post
    (``self.sessions.set_channel(session_key, channel)``) records the job's own
    configured channel after posting to it. It is not a Slack turn -- no envelope,
    no receipt generation -- and the destination it writes comes from the job
    record, not from a swept row, so a switch cannot make it republish one."""
    import inspect
    import re

    from kiro_crew.slack import interactions

    src = inspect.getsource(interactions)
    offenders = [
        line.strip()
        for line in src.splitlines()
        if re.search(r"sessions\.set_slack_link\(", line) and "generation=" not in line
        # A multi-line call carries the kwarg on a later line; pin those by the
        # call's opening line ending in "(".
        and not line.rstrip().endswith("(")
    ]
    assert offenders == []
    linkers = [
        obj
        for name, obj in vars(interactions).items()
        if inspect.isfunction(obj)
        and obj.__module__ == interactions.__name__
        and ".link_slack(" in inspect.getsource(obj)
    ]
    assert {f.__name__ for f in linkers} == {"_import_thread_to_slot", "_handle_resume_choice"}
    for fn in linkers:
        body = inspect.getsource(fn)
        assert body.index("_slack_links_stale(") < body.index(".link_slack("), fn.__name__
