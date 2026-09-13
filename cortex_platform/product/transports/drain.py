"""The outbound half: what turns a committed event into a Telegram message.

P5.4a wired the daemon's inbound loop and found the other end open --
`TelegramAdapter.deliver_event` had no production call site at all, so `cortexd`
could receive and could not send. This is that call site.

Two things it is deliberately not. It is **not** a queue: Control's delivery
ledger already holds the truth (a frozen projection, per-chunk claims, permits
carrying the window id, write-once receipts), and re-entering `deliver_event`
for the same event is idempotent by construction -- which is what makes a daemon
restart mid-delivery resume rather than duplicate. And it is **not** a retry
engine: ⟦P5-01⟧ says only a *provable* pre-socket refusal may be re-sent, so a
chunk that reached `sending_unknown` takes its parent to `manual_required` and
this loop never looks at it again. `urllib` cannot tell the caller whether bytes
were written, and one wrong guess re-sends a message the operator already read.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ..control import ControlStore, TransportDeliveryKey
from ..control.errors import NotFound
from ..redaction import redact
from .models import COMMAND_REPLY_PREFIX, TelegramDestination, TelegramScope

_log = logging.getLogger(__name__)

TRANSPORT = "telegram"

#: The events `TelegramAdapter.deliver_event` will project. Anything else it
#: answers `ignored`, so filtering here is an optimisation and not a fence.
NOTIFICATION_TYPES = frozenset(
    {
        "decision.required",
        "run.blocked",
        "run.completed",
        "run.failed",
        "run.milestone",
    }
)

#: How many new events one pass reads. A pass runs once per reconcile tick.
EVENT_BATCH = 200

#: ⟦F-B2⟧ How many events a pass may hold back for the next one. The cursor is
#: a high-water mark and moves forward whatever happens, so an event whose
#: delivery was refused in a way ⟦P5-01⟧ says may be re-sent is remembered HERE
#: rather than by refusing to advance -- holding the cursor would stall every
#: newer event behind one bad row. Bounded because it is memory in a daemon: a
#: retry set that could grow without limit is a queue, and this is not one.
RETRY_LIMIT = 200

#: ⟦F-B4⟧ How long one pass may hold the reconcile lock. The drain runs on the
#: same single thread as the window watch, and `close_window` needs that lock:
#: the refuter measured a 49.5 s pass blocking `stop_poller()` for 49.2 s. A
#: pass that runs out of time stops where it is and the next tick continues --
#: the cursor and the ledger are both durable, so there is nothing to lose.
#: The bound is checked BETWEEN events only: one in-flight send still runs to
#: its own end, which since P5.5 is up to `TRANSPORT_SEND_WAIT_SECONDS` on the
#: shared line plus the worker RPC timeout. The true ceiling for one pass is
#: therefore `MAX_PASS_SECONDS` + that send, not 5 s flat.
MAX_PASS_SECONDS = 5.0

#: ⟦F-B7⟧ How many pending ledger rows one pass reads. The store orders by
#: `created_at, operation_id` -- the OLDEST -- and a permanently unroutable row
#: keeps its slot for ever, so at the default 100 a hundred stuck deliveries
#: starve every newer resumable one. The store's own ceiling is 1000.
PENDING_LIMIT = 500

#: How many distinct unroutable pairs are remembered. A count, not a queue.
UNROUTABLE_LIMIT = 1_000


@dataclass(frozen=True)
class Bindings:
    """One pass's answer to both routing questions, resolved once.

    Held for the length of a drain pass and thrown away: a binding revoked
    mid-pass is honoured on the next tick, which is a second later.
    """

    by_digest: dict[str, tuple[TelegramDestination, str]]
    by_thread: dict[str, TelegramDestination]


class DestinationDirectory:
    """Recover a destination from a binding digest, storing no chat id.

    `transport_bindings.external_scope` and `transport_deliveries.
    destination_digest` are HMACs of the routing scope, on purpose: Control has
    never held a raw chat id and this slice does not make it start. So the
    daemon does the only thing a digest allows -- it proposes candidates and
    lets Control confirm one. A candidate comes from the operator's own
    `telegram_allowed_user_ids` (a private chat's id IS the user's) or from an
    update the poller has already seen in this process, and `resolve_transport`
    answers whether that exact scope is bound. A wrong candidate simply does not
    match; nothing is guessed into the ledger.
    """

    def __init__(
        self,
        *,
        store: ControlStore,
        bot_identity: str,
        allowed_user_ids: Iterable[int] = (),
    ) -> None:
        self._store = store
        self._bot_identity = bot_identity
        self._lock = threading.Lock()
        self._candidates: dict[tuple[int, int | None], TelegramDestination] = {}
        for user_id in sorted(set(allowed_user_ids)):
            self.learn(TelegramDestination(chat_id=int(user_id), topic_id=None))

    def learn(self, destination: TelegramDestination) -> None:
        with self._lock:
            self._candidates[(destination.chat_id, destination.topic_id)] = destination

    def learn_from_update(self, update: Mapping[str, Any]) -> None:
        """Take the routing scope of an inbound update as a candidate."""

        message = update.get("message") or update.get("callback_query") or {}
        if not isinstance(message, Mapping):
            return
        chat = message.get("chat")
        if not isinstance(chat, Mapping):
            inner = message.get("message")
            chat = inner.get("chat") if isinstance(inner, Mapping) else None
        if not isinstance(chat, Mapping):
            return
        chat_id = chat.get("id")
        topic_id = message.get("message_thread_id")
        if type(chat_id) is not int:
            return
        self.learn(
            TelegramDestination(
                chat_id=chat_id,
                topic_id=topic_id if type(topic_id) is int else None,
            )
        )

    def candidates(self) -> tuple[TelegramDestination, ...]:
        with self._lock:
            return tuple(self._candidates.values())

    def _binding(self, destination: TelegramDestination) -> Mapping[str, Any] | None:
        scope = TelegramScope(
            bot_identity=self._bot_identity,
            chat_id=destination.chat_id,
            topic_id=destination.topic_id,
        )
        return self._store.resolve_transport(
            transport=TRANSPORT, external_scope=scope.canonical()
        )

    def resolved(self) -> Bindings:
        """Ask Control once per candidate, and answer both questions from that.

        ⟦F-B4⟧ `for_digest` and `for_thread` each walked every candidate and
        issued a `resolve_transport` per step, so a pass over 200 events cost
        200xN indexed queries on the thread that also owns `close_window`. One
        pass builds one index; the queries become N.
        """

        by_digest: dict[str, tuple[TelegramDestination, str]] = {}
        by_thread: dict[str, TelegramDestination] = {}
        for destination in self.candidates():
            binding = self._binding(destination)
            if binding is None:
                continue
            by_digest[str(binding["external_scope"])] = (
                destination,
                str(binding["thread_id"]),
            )
            by_thread.setdefault(str(binding["thread_id"]), destination)
        return Bindings(by_digest=by_digest, by_thread=by_thread)

    def for_digest(self, digest: str) -> tuple[TelegramDestination, str] | None:
        """The destination whose binding Control recorded under this digest."""

        return self.resolved().by_digest.get(digest)

    def for_thread(self, thread_id: str) -> TelegramDestination | None:
        return self.resolved().by_thread.get(thread_id)


def learning_update_handler(
    directory: DestinationDirectory, handle: Any
) -> Any:
    """Learn a routing scope only from an update the adapter accepted.

    ⟦F-B4⟧ The daemon used to call `learn_from_update` BEFORE
    `TelegramAdapter.handle_update`, whose `_authorize` is the fence. Telegram
    delivers an update from anyone who can name the bot, so any sender who
    found it put an entry in the candidate dict -- and every entry costs one
    Control query per drain pass, on the single thread that also owns
    `close_window`. Growth is now bounded to the chats the operator writes
    from, and an allowlisted sender in a group still teaches the group scope,
    which is what makes a bound topic routable at all.

    Deliberately not an LRU cap: eviction can drop a legitimately bound
    group/topic scope, which silently reintroduces the unroutable state.
    """

    def handle_update(update: Mapping[str, Any]) -> Any:
        result = handle(update)
        if getattr(result, "ok", False):
            directory.learn_from_update(update)
        return result

    return handle_update


class TransportDeliveryDrain:
    """One single-threaded pass over the work Control says is outstanding."""

    def __init__(
        self,
        *,
        store: ControlStore,
        adapter: Any,
        directory: DestinationDirectory,
        cursor: int | None = None,
        batch: int = EVENT_BATCH,
        serializer: Any | None = None,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._directory = directory
        # ⟦P5.5⟧ The poller decides how long to park `getUpdates` for, and the
        # only honest input to that decision is whether this loop still owes
        # the operator a message. Reported after every pass; `None` in the unit
        # tests that construct a drain with no transport line at all.
        self._serializer = serializer
        # The head at construction, so a daemon start does not treat every
        # notification event ever recorded as undelivered work. An interrupted
        # delivery is found through the ledger instead, which is where the
        # evidence that it was ever started actually lives.
        self._cursor = store.latest_event_cursor() if cursor is None else int(cursor)
        self._batch = int(batch)
        self.counts: Counter[str] = Counter()
        self.delivered = 0
        #: ⟦0.1.18 B⟧ Answers to the operator's own commands, counted apart
        #: from run notifications. "Deliveries happened" and "the thing I typed
        #: got an answer" were the same number, and only one of them is what an
        #: operator checks after typing `/open`.
        self.replies_delivered = 0
        self.last_outcome: dict[str, object] | None = None
        # ⟦F-B7⟧ Distinct `(destination_digest, event_id)` pairs, not a
        # per-pass occurrence tally. A pass runs once a second, so one stuck
        # row used to increment this ~3600 times an hour: the number an
        # operator read during a window was a tick count wearing a delivery
        # count's name.
        self._unroutable: set[tuple[str, str]] = set()
        # ⟦F-B10⟧ `status()` is called from the API thread while the reconcile
        # thread is inside a pass, and `self.counts[category] += 1` can change
        # the Counter's size mid-iteration. `ControlAPI._transport_window_status`
        # catches that and returns None, so a transient read error looked
        # exactly like "the daemon lost its window" -- likeliest precisely when
        # the drain first records a category the operator cares about.
        self._status_lock = threading.Lock()
        # ⟦F-B2⟧ Events a pass could not finish and the next one may. Ordered so
        # a bounded set drops the OLDEST rather than an arbitrary member.
        # ⟦P5.6⟧ The value is the monotonic instant the event becomes
        # actionable again: 0.0 for "now", or the end of a deferral Control
        # answered with `retry_after_ms` (batch D D-3). Work the drain cannot
        # act on yet is not outbound work the poller must stay short for.
        self._retry: dict[str, float] = {}
        self.retried = 0
        self.retries_dropped = 0
        #: ⟦P5.6⟧ The last `deliver_raised`, with its message: the category
        #: alone said an exception happened and nothing said which.
        self.failures = 0
        self.last_failure: dict[str, object] | None = None
        # ⟦F-B2⟧ A committed notification event whose run is bound to no
        # Telegram scope. Most runs in a product are not, so this is the normal
        # case and not an error -- but it was a `continue` with no number
        # anywhere, which is also what a genuinely lost binding looks like.
        # Distinct events, not occurrences: the same one is re-read only once.
        self._undeliverable: set[str] = set()
        #: ⟦F-B4⟧ Passes that ran out of time and left work for the next tick.
        self.deadline_stops = 0

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def unroutable(self) -> int:
        return len(self._unroutable)

    def drain(self) -> list[dict[str, object]]:
        """Resume what was interrupted, then deliver what is new.

        ⟦F-B4⟧ One index and one deadline, both for the length of this pass.
        The pass holds the reconcile lock that `close_window` needs, so it is
        bounded in wall time rather than in rows: 200 events that each send
        fast is fine, and one event whose send takes 12 seconds is not.
        """

        try:
            bindings = self._directory.resolved()
            deadline = time.monotonic() + MAX_PASS_SECONDS
            resumed = self._resume(bindings, deadline)
            # An event can be in both work sources at once -- the ledger still
            # has a pending row for it AND the last pass held it back.
            # `deliver_event` is idempotent, so the second entry would be
            # harmless; not making it is cheaper and keeps the outcome list
            # honest about what happened.
            outcomes = resumed + self._advance(
                bindings,
                deadline,
                skip={str(item["event_id"]) for item in resumed},
            )
        except BaseException:
            # ⟦P5.6⟧ A pass that died before its report left the line's last
            # answer standing (batch D D-3). Nothing this pass learned is
            # trustworthy, so the honest report is "nothing actionable" -- the
            # next pass re-derives it from the ledger a second later.
            if self._serializer is not None:
                self._serializer.outbound_pending(False)
            raise
        if self._serializer is not None:
            # Generous by one tick on purpose: a pass that delivered keeps the
            # line short-polling until a pass finds nothing, which is the safe
            # direction. Claiming "idle" one tick early puts a 30 s poll back
            # in front of the next chunk. ⟦P5.6⟧ Except for a deferral: a
            # chunk Control is holding for a future `retry_not_before` is not
            # work this pass, or the next, can act on (batch D D-3), and
            # reporting it kept the poller at 1 s for a whole flood-wait.
            self._serializer.outbound_pending(self._actionable(outcomes))
        return outcomes

    def _actionable(self, outcomes: list[dict[str, object]]) -> bool:
        """Is there outbound work the NEXT pass can act on?"""

        now = time.monotonic()
        if any(not_before <= now for not_before in self._retry.values()):
            return True
        return any(not _deferred(outcome) for outcome in outcomes)

    # -- the two work sources ---------------------------------------------

    def _resume(
        self, bindings: Bindings, deadline: float
    ) -> list[dict[str, object]]:
        outcomes: list[dict[str, object]] = []
        for row in self._store.pending_transport_deliveries(
            transport=TRANSPORT, limit=PENDING_LIMIT
        ):
            if time.monotonic() >= deadline:
                self.deadline_stops += 1
                break
            digest = str(row["destination_digest"])
            event_id = str(row["event_id"])
            resolved = bindings.by_digest.get(digest)
            if resolved is None:
                # The binding exists but this process cannot name the scope
                # behind its digest -- no candidate matched. Counted rather
                # than retried in a tight loop, and never guessed. Re-checking
                # it next pass now costs one dict lookup against the index this
                # pass already built.
                self._count_unroutable(digest, event_id)
                continue
            if event_id.startswith(COMMAND_REPLY_PREFIX):
                # ⟦0.1.18 B⟧ A command reply. It answers an inbound update, not
                # a run, so there is no `run_events` row to look up and asking
                # for one would count the operator's own answer unroutable for
                # ever. The frozen projection is the whole of it: the adapter
                # reads it back off the claim rather than re-deriving text or
                # minting a second link.
                outcomes.append(
                    self._deliver_command_reply(
                        digest=digest,
                        event_id=event_id,
                        projection_version=int(row["projection_version"]),
                        destination=resolved[0],
                    )
                )
                continue
            try:
                event = self._store.get_run_event(event_id)
            except NotFound:
                self._count_unroutable(digest, event_id)
                continue
            outcomes.append(self._deliver(event, resolved[0], source="resumed"))
        return outcomes

    def _advance(
        self,
        bindings: Bindings,
        deadline: float,
        *,
        skip: set[str] = frozenset(),
    ) -> list[dict[str, object]]:
        """What is new since the cursor, and what the last pass held back.

        ⟦F-B2⟧ The cursor used to move past an event whose delivery never
        reached the ledger: `_deliver` could raise (the whole pass dies inside
        `_drain_locked`'s `except`) or come back `retryable`, and either way the
        event was already behind the high-water mark and never looked at again.
        Held back in a bounded set instead, which is the only shape that does
        not stall every newer event behind one bad row.
        """

        outcomes: list[dict[str, object]] = []
        retries, self._retry = list(self._retry.items()), {}
        now = time.monotonic()
        for index, (event_id, not_before) in enumerate(retries):
            if event_id in skip:
                continue
            if not_before > now:
                # ⟦Batch F P56-OBS-7⟧ Deferred by Control (a flood-wait): the
                # deferral governs the ATTEMPT, not only the poll length. Held
                # with the same not-before. Without this every pass re-ran the
                # claim against control.db for the whole wait, ahead of the
                # ledger scan, and could spend the pass deadline on chunks it
                # was told not to send.
                self._remember(event_id, not_before=not_before)
                continue
            if time.monotonic() >= deadline:
                # Put back what this pass never looked at, so running out of
                # time is not a second way to lose the row.
                self.deadline_stops += 1
                for pending, pending_not_before in retries[index:]:
                    self._remember(pending, not_before=pending_not_before)
                return outcomes
            try:
                event = self._store.get_run_event(event_id)
            except NotFound:
                continue
            outcome = self._consider(event, bindings, source="retried")
            if outcome is not None:
                self.retried += 1
                outcomes.append(outcome)
        for event in self._store.list_events(
            after_cursor=self._cursor, limit=self._batch
        ):
            if time.monotonic() >= deadline:
                self.deadline_stops += 1
                break
            self._cursor = int(event["cursor"])
            outcome = self._consider(event, bindings, source="new")
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _consider(
        self, event: Mapping[str, Any], bindings: Bindings, *, source: str
    ) -> dict[str, object] | None:
        """One event, from either work source, with the cursor already past it."""

        if event.get("type") not in NOTIFICATION_TYPES:
            return None
        try:
            run = self._store.get_run(str(event["run_id"]))
        except NotFound:
            return None
        destination = bindings.by_thread.get(str(run["thread_id"]))
        if destination is None:
            # No Telegram binding for this run's thread. Not an error: most
            # runs in a product are not bound to a chat at all -- which is
            # exactly why holding the cursor here would stall the drain
            # permanently behind the first one.
            self._undeliverable.add(str(event.get("id")))
            return None
        try:
            outcome = self._deliver(event, destination, source=source)
        except Exception as exc:  # noqa: BLE001 - a pass that dies must not lose the row
            failure = {
                "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "error": type(exc).__name__,
                "detail": redact(str(exc)),
                "event_id": str(event.get("id")),
            }
            with self._status_lock:
                self.counts["deliver_raised"] += 1
                self.failures += 1
                self.last_failure = failure
            _log.warning(
                "transport delivery raised for event %s: %s: %s",
                failure["event_id"],
                failure["error"],
                failure["detail"],
            )
            self._remember(str(event.get("id")))
            return None
        if outcome["retryable"] and not outcome["delivered"]:
            # ⟦P5-01⟧ `retryable` is the adapter's own word for a refusal it
            # can prove happened before any socket. Nothing else is re-entered.
            self._remember(str(event.get("id")), not_before=_not_before(outcome))
        return outcome

    def _count_unroutable(self, digest: str, event_id: str) -> None:
        if len(self._unroutable) < UNROUTABLE_LIMIT:
            self._unroutable.add((digest, event_id))

    def _remember(self, event_id: str, *, not_before: float = 0.0) -> None:
        if event_id in self._retry:
            return
        while len(self._retry) >= RETRY_LIMIT:
            self._retry.pop(next(iter(self._retry)))
            self.retries_dropped += 1
        self._retry[event_id] = float(not_before)

    def _deliver(
        self, event: Mapping[str, Any], destination: TelegramDestination, *, source: str
    ) -> dict[str, object]:
        result = self._adapter.deliver_event(event=event, destination=destination)
        return self._record(str(event.get("id")), result, source=source)

    def _deliver_command_reply(
        self,
        *,
        digest: str,
        event_id: str,
        projection_version: int,
        destination: TelegramDestination,
    ) -> dict[str, object]:
        """Send one frozen reply the adapter owes an operator's own command."""

        result = self._adapter.deliver_command_reply(
            delivery_key=TransportDeliveryKey(
                transport=TRANSPORT,
                destination_digest=digest,
                event_id=event_id,
                projection_version=projection_version,
            ),
            destination=destination,
        )
        if result.delivered:
            with self._status_lock:
                self.replies_delivered += 1
        return self._record(event_id, result, source="command_reply")

    def _record(
        self, event_id: str, result: Any, *, source: str
    ) -> dict[str, object]:
        with self._status_lock:
            self.counts[result.category] += 1
            if result.delivered:
                self.delivered += 1
        outcome = {
            # Identity and category only. The chunk text is the message body and
            # has no business on an operator status surface.
            "event_id": event_id,
            "source": source,
            "category": result.category,
            "delivered": bool(result.delivered),
            "duplicate": bool(result.duplicate),
            "retryable": bool(result.retryable),
            "chunks": int(result.chunks),
            # ⟦P5.6⟧ How long Control (or the provider) asked this loop to
            # wait. A duration, not a payload; it is what decides whether the
            # event counts as outbound work right now.
            "retry_after_ms": (
                None if result.retry_after_ms is None else int(result.retry_after_ms)
            ),
        }
        self.last_outcome = outcome
        return outcome

    def status(self) -> dict[str, object]:
        with self._status_lock:
            categories = dict(sorted(self.counts.items()))
        return {
            "cursor": self._cursor,
            "delivered": self.delivered,
            "replies_delivered": self.replies_delivered,
            "unroutable": self.unroutable,
            # ⟦F-B2⟧ Committed notification events this daemon could not route
            # to a chat, and events held back for the next pass. Both were
            # silent `continue`s; both are how an operator watching a window
            # tells "answered but never sent" from "nothing to send".
            "undeliverable": len(self._undeliverable),
            "retrying": len(self._retry),
            "retried": self.retried,
            "retries_dropped": self.retries_dropped,
            "deadline_stops": self.deadline_stops,
            "categories": categories,
            "last_outcome": self.last_outcome,
            "failures": self.failures,
            "last_failure": (
                None if self.last_failure is None else dict(self.last_failure)
            ),
        }


def _deferred(outcome: Mapping[str, object]) -> bool:
    """A retryable outcome held back by a delay the drain must respect."""

    if outcome.get("delivered") or not outcome.get("retryable"):
        return False
    delay = outcome.get("retry_after_ms")
    return isinstance(delay, int) and delay > 0


def _not_before(outcome: Mapping[str, object]) -> float:
    delay = outcome.get("retry_after_ms")
    if not isinstance(delay, int) or delay <= 0:
        return 0.0
    return time.monotonic() + delay / 1000.0
