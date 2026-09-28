"""A user Stop during an automatic compaction neither fails it nor restarts the session.

The report behind these tests (#14841): a long-running dashboard session looked
stalled, the user pressed Stop, and the dashboard answered "Compaction didn't succeed
at 87%, so the session was restarted instead." Nothing had shown a compaction was
running, the Stop cancelled the ``/compact`` turn, and the failure arm recycled the
process. Four things pin the fix here:

1. ``stop_turn`` DECLINES a cooperative Stop while the key is compacting and does
   not record it as a Stop the turn saw; a force stop still goes through.
2. When a ``/compact`` turn IS ended by a Stop (a force stop, or the race the
   pre-check cannot close), the compaction settles as ``cancelled`` -- cooldown armed,
   provider NOT shut down, notice says so -- instead of recycling.
3. The compacting set is observable: an observer is told on enter and leave, and the
   dashboard slot payload carries ``compacting`` so the composer can show it.
4. The restart notices no longer claim the agent remembers nothing.

Fakes only: a mock provider whose ``/compact`` blocks until released or raises, no
real harness.
"""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import (
    COMPACT_OUTCOME_CANCELLED,
    COMPACT_OUTCOME_RECYCLED,
)

KEY = "dashboard:chat-14841"


class _Compact:
    """A ``/compact`` turn the test controls: it blocks until released or is failed."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.fail_with: BaseException | None = None
        self.started = asyncio.Event()

    async def stream(self, _command: str):
        self.started.set()
        await self.release.wait()
        if self.fail_with is not None:
            raise self.fail_with
        if False:  # pragma: no cover - makes this an async generator
            yield None


def _factory(compact: _Compact, order: list[str]):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.cwd = ""
        m.disown_work_dir = MagicMock()
        m.memory_mode = "persistent"
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 90.0
        m.context_usage_unknown = lambda: False
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: True
        m.runtime_info = lambda: (None, None)
        m.stream_command = MagicMock(side_effect=compact.stream)
        m.wait_for_compaction = AsyncMock(return_value={"type": "failed"})

        # A cooperative cancel is what a soft Stop does to a live turn; here it
        # ends the /compact turn the way the harness would: the stream raises.
        async def _cancel(*, wait_ack_timeout: float = 0.0):
            compact.fail_with = RuntimeError("compaction reported no result")
            compact.release.set()
            return "acked"

        m.cancel = AsyncMock(side_effect=_cancel)
        m.shutdown = AsyncMock(side_effect=lambda: order.append("shutdown"))
        return m

    return factory


async def _setup():
    order: list[str] = []
    compact = _Compact()
    mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory(compact, order))
    await mgr.get_or_create(KEY)
    key = mgr._fold_key(KEY)
    mgr.release(key)
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps,
        compact_wait_timeout_secs=lambda: 5.0,
        compact_result_wait_secs=lambda _elapsed: 0.05,
        compact_failure_cooldown_secs=123.0,
    )
    notices: list[tuple[bool, str]] = []

    async def _cb(key, pct, *, success, outcome="compacted"):
        notices.append((success, outcome))

    mgr.set_compact_callback(_cb)
    return mgr, key, compact, order, notices


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# -- 1. a cooperative Stop is declined while compacting --


@pytest.mark.asyncio
async def test_a_cooperative_stop_during_compaction_is_declined_and_not_recorded():
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    before = mgr.stop_generation(key)

    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    # The trigger paths add the key; _compact_in_place alone does not. Mirror
    # the state a real threshold trigger leaves.
    mgr._compacting.add(key)
    try:
        assert mgr.is_compacting(KEY) is True
        outcome = await mgr.stop_turn(KEY, force=False)
        assert outcome == "compacting"
        # Not recorded: a declined Stop is not a Stop the turn saw, and recording
        # it would make the compaction read its own later failure as cancelled.
        assert mgr.stop_generation(key) == before
        session.provider.cancel.assert_not_called()
        assert not task.done()
    finally:
        mgr._compacting.discard(key)
        compact.release.set()
        await asyncio.wait_for(task, timeout=5)
    # The compaction ran to its own (failed) end and recycled -- the ordinary
    # failure arm, untouched by the declined Stop.
    assert order == ["shutdown"]
    assert notices == [(True, COMPACT_OUTCOME_RECYCLED)]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_force_stop_during_compaction_is_never_declined():
    """The escape hatch stays open: ``force`` is the user's second press."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    mgr._compacting.add(key)
    try:
        outcome = await mgr.stop_turn(KEY, force=True)
    finally:
        mgr._compacting.discard(key)
        compact.fail_with = RuntimeError("compaction reported no result")
        compact.release.set()
    assert outcome == "hard"
    result = await asyncio.wait_for(task, timeout=5)
    # The force stop reset the session; the compaction that was running on it
    # settles as CANCELLED rather than recycling a provider the reset already
    # replaced and telling the user compaction "didn't succeed".
    assert result == "cancelled"
    assert notices[-1] == (False, COMPACT_OUTCOME_CANCELLED)
    await mgr.close_all()


@pytest.mark.asyncio
async def test_stop_turn_is_unchanged_when_nothing_is_compacting():
    mgr, key, compact, order, notices = await _setup()
    assert mgr.is_compacting(KEY) is False
    # No compaction and a mock provider that acks: the ordinary soft path.
    assert await mgr.stop_turn(KEY, force=False) == "soft"
    assert mgr.stop_generation(key) == 1
    await mgr.close_all()


# -- 2. a Stop that ends the /compact turn settles as cancelled, not recycled --


@pytest.mark.asyncio
async def test_a_stop_that_ends_the_compact_turn_does_not_recycle():
    """The race the pre-check cannot close: the Stop lands on the /compact turn.

    Driven by noting the Stop directly, which is what every channel stop path
    that cancels the provider itself does, and then failing the turn the way the
    harness reports a cancelled prompt.
    """
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)

    assert mgr.note_stop(KEY) is True
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()

    assert await asyncio.wait_for(task, timeout=5) == "cancelled"
    # No recycle: the provider is still the session's provider and was not shut down.
    assert order == []
    assert mgr._sessions[key] is session
    assert notices == [(False, COMPACT_OUTCOME_CANCELLED)]
    # The cooldown is armed so the next threshold reading retries later rather
    # than immediately re-entering the compaction the user just stopped.
    assert key in mgr._compact_cooldown_until
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_stop_before_the_semaphore_is_held_is_not_this_compactions_cancel():
    """A Stop that ended the PREVIOUS turn must not be read as cancelling this compaction."""
    mgr, key, compact, order, notices = await _setup()
    session = mgr._sessions[key]
    # The Stop happened earlier, on some other turn.
    assert mgr.note_stop(KEY) is True
    task = asyncio.ensure_future(mgr._compact_in_place(key, session, 90.0))
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()
    # Genuine failure: the ordinary recycle arm.
    assert await asyncio.wait_for(task, timeout=5) == "recycled"
    assert order == ["shutdown"]
    assert notices == [(True, COMPACT_OUTCOME_RECYCLED)]
    await mgr.close_all()


# -- 3. the compacting set is observable --


@pytest.mark.asyncio
async def test_the_compacting_observer_sees_enter_and_leave_once_each():
    mgr, key, compact, order, notices = await _setup()
    seen: list[tuple[str, bool]] = []
    mgr.set_compacting_callback(lambda k, on: seen.append((k, on)))
    # A failing observer must not fail the compaction.
    session = mgr._sessions[key]
    provider = session.provider
    decline = mgr._compaction._trigger_compaction(key, "test", 90.0, provider)
    assert decline is None, decline
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    assert seen == [(key, True)]
    assert mgr.is_compacting(KEY) is True
    compact.fail_with = RuntimeError("compaction reported no result")
    compact.release.set()
    for _ in range(200):
        if not mgr.is_compacting(KEY):
            break
        await asyncio.sleep(0.01)
    assert seen == [(key, True), (key, False)]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_raising_observer_does_not_fail_the_compaction():
    mgr, key, compact, order, notices = await _setup()

    def _boom(_k, _on):
        raise RuntimeError("observer broke")

    mgr.set_compacting_callback(_boom)
    session = mgr._sessions[key]
    decline = mgr._compaction._trigger_compaction(key, "test", 90.0, session.provider)
    assert decline is None
    await asyncio.wait_for(compact.started.wait(), timeout=2)
    assert mgr.is_compacting(KEY) is True
    compact.release.set()  # completes with no status -> failed -> recycled
    compact.fail_with = RuntimeError("compaction reported no result")
    for _ in range(200):
        if not mgr.is_compacting(KEY):
            break
        await asyncio.sleep(0.01)
    assert mgr.is_compacting(KEY) is False
    assert notices == [(True, COMPACT_OUTCOME_RECYCLED)]
    await mgr.close_all()


def _dashboard_state(tmp_path):
    import sys

    sys.path.insert(0, "test")
    from chat_test_helpers import _make_state

    return _make_state(tmp_path)


def test_the_slot_payload_carries_compacting_beside_running(tmp_path):
    """The composer reads ``compacting`` off the slot, separate from ``running``."""
    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    assert slot.to_dict()["compacting"] is False
    slot._compacting = True
    payload = slot.to_dict()
    assert payload["compacting"] is True
    assert payload["running"] is False, "a compaction is not a dashboard turn"


def test_the_dashboard_stop_is_declined_while_the_session_compacts(tmp_path, monkeypatch):
    """The route-level pre-check: no cancel is sent and the press still leaves a card."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    state.sessions.is_compacting = MagicMock(return_value=True)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    reply = asyncio.run(stop_slot_turn(state, slot))

    assert reply == {"ok": True, "info": "compacting", "compacting": True}
    state.sessions.stop_turn.assert_not_awaited()
    assert slot._stop_state == "idle", "nothing was stopped, so nothing is pending"
    cards = [m for m in slot.messages if '"kind": "stop_event"' in (m.get("cls") or "")]
    assert len(cards) == 1
    assert '"state": "stop_declined_compacting"' in cards[0]["cls"]


def test_a_mock_shaped_manager_does_not_decline_every_stop(tmp_path, monkeypatch):
    """The probe is ``is True``: a truthy Mock answer must read as not compacting."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    state.sessions.stop_turn = AsyncMock(return_value="soft")
    assert callable(state.sessions.is_compacting)  # a bare MagicMock attribute
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    asyncio.run(stop_slot_turn(state, slot))

    state.sessions.stop_turn.assert_awaited_once()


def test_the_race_outcome_settles_the_card_and_undoes_the_soft_stop(tmp_path, monkeypatch):
    """``stop_turn`` answering ``compacting`` after the pre-check passed."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.chat_handlers import stop_slot_turn

    state = _dashboard_state(tmp_path)
    slot = state.get_or_create_slot("chat-14841")
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    state.sessions.is_compacting = MagicMock(return_value=False)
    state.sessions.stop_turn = AsyncMock(return_value="compacting")
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())

    reply = asyncio.run(stop_slot_turn(state, slot))

    assert reply["compacting"] is True
    assert slot._stop_state == "idle"
    assert slot._stop_event_id is None
    cards = [m for m in slot.messages if '"kind": "stop_event"' in (m.get("cls") or "")]
    assert len(cards) == 1
    assert '"state": "stop_declined_compacting"' in cards[0]["cls"]


# -- 4. the notices --


def test_the_cancelled_notice_names_a_stop_and_no_restart():
    from kiro_crew.dashboard.chat_compaction_notice import notice_text
    from kiro_crew.dashboard.state import (
        _AUTO_COMPACT_CANCELLED_NOTICE,
        _AUTO_COMPACT_FAILED_NOTICE,
        _AUTO_RECYCLE_NOTICE,
    )

    dashboard = _AUTO_COMPACT_CANCELLED_NOTICE.format(pct=87)
    assert "Stop" in dashboard
    assert "not restarted" in dashboard
    assert dashboard != _AUTO_COMPACT_FAILED_NOTICE.format(pct=87)
    assert dashboard != _AUTO_RECYCLE_NOTICE.format(pct=87)

    channel = notice_text("slack", 87.0, success=False, outcome=COMPACT_OUTCOME_CANCELLED)
    assert "stop" in channel.lower()
    assert "not restarted" in channel
    assert "`!compact`" in channel
    assert channel != notice_text("slack", 87.0, success=False, outcome="compacted")
