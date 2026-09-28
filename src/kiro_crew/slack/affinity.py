"""Client affinity for Slack deliveries that outlive a Reconnect.

``POST /api/slack/reconnect`` replaces the gateway's live Web API client
(``GatewayOrchestrator.slack``) while work started under the previous client
may still be in flight: a turn awaiting its agent, an approval card waiting
for the owner's click, a modal about to be updated. Every destination that
work holds -- channel id, thread ``ts``, message ``ts`` -- was minted by the
workspace the OLD client reached. Sent through the new client after a switch
to another workspace it is lost (``channel_not_found``) or, on a colliding
id, misrouted.

The receiving side already binds the routing of a message to the client that
received it (``_route_message``'s ``received_by``, the queue entry's client).
This module closes the rest: the client an envelope arrived through is bound
to the TASK CONTEXT of that envelope, and ``GatewayOrchestrator.slack``
resolves to the bound client whenever one is set. ``asyncio.create_task``
copies the context, so every task spawned while handling the envelope --
the interactive dispatch, the slash command, the agent turn and its final
post -- keeps answering through the client that received it, however many
reconnects happen meanwhile. Code that runs outside any envelope (the HTTP
routes, boot, cron) sees the live client as before.

Deliberately strict: a bound delivery never falls back to the live client,
even for a same-workspace token rotation, because nothing here can tell a
rotation from a switch without a network round trip, and the failure of a
post through a revoked token is visible (the Web API says so) where a post
into the wrong workspace is not.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:  # pragma: no cover
    from kiro_crew.slack.client import RealSlackClient


class _Unbound:
    """Sentinel type: no client is bound to the current context."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNBOUND"


UNBOUND: Any = _Unbound()

_bound_client: ContextVar[Any] = ContextVar("kiro_crew.slack.bound_client", default=UNBOUND)


def bound_client() -> Any:
    """The client bound to the current context, or ``UNBOUND``.

    ``None`` is a legitimate binding (an envelope received while the gateway
    held no Web API client), which is why absence is a sentinel, not ``None``.
    """
    return _bound_client.get()


@contextmanager
def client_scope(client: "RealSlackClient | Any | None") -> Iterator[None]:
    """Bind *client* for the dynamic extent of the block (and tasks it spawns).

    Nesting is honoured: the inner binding wins inside, the outer one is
    restored on exit. Re-binding the same client is a no-op in effect.
    """
    token = _bound_client.set(client)
    try:
        yield
    finally:
        _bound_client.reset(token)
