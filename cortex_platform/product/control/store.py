"""Transactional SQLite control store for Cortex product state."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from ..artifacts import (
    Artifact,
    ArtifactVersion,
    MaterializationRequest,
    MaterializedAsset,
    ParentVersionInput,
    ProducerIdentity,
    Provenance,
    Snapshot,
    SnapshotMember,
)
from ..artifacts.reader import ArtifactContentReference
from ..resources import parse_resource_uri
from ..sources import (
    CandidateObservation,
    canonicalize_arxiv_id,
    canonicalize_doi,
    canonicalize_locator,
    canonicalize_source_locator,
    validate_engine_ref,
)
from ..sources.adoption import AdoptionEntry, AdoptionManifest
from ..sources.models import normalize_source_text
from ..workflows.models import (
    ArtifactWorkflowRequest,
    ArtifactWorkflowResult,
    EffectReconciliationRequest,
    EffectReconciliationResult,
    LineageQueryRequest,
    RuntimeStageRequest,
    RuntimeStageResult,
    SourceBindingRequest,
    SourceImportRequest,
    SuccessorCreationRequest,
    WorkflowDefinition,
)
from .research_store import ResearchItemsStore
from .errors import (
    CaptureConflict,
    IdempotencyConflict,
    InvalidTransition,
    MachineRunRefused,
    NotFound,
    RevisionConflict,
    ThreadActiveRun,
    ThreadArchived,
    TransportBindingConflict,
)
from .registry import (
    TRANSPORT_WINDOW_ABORTED_EVENT,
    TRANSPORT_WINDOW_AGGREGATE,
    TRANSPORT_WINDOW_CLOSED_EVENT,
    AdoptionManifestRecord,
    RuntimeActivationRecord,
    RuntimeReleaseApprovalRecord,
    TransportActivationRecord,
    AssetRootRecord,
    BackupCopyProof,
    ConnectorRecord,
    ControlStoreIdentity,
    ControlStoreSnapshot,
    HealthSubject,
    PairedBackupProofInput,
    PairedBackupProofRecord,
    ProtectedAssetRootSnapshot,
    ProtectedLogicalSnapshot,
    ProtectedSetManifest,
    RestoreVerification,
    SystemHealthObservationInput,
    SystemHealthObservationRecord,
    canonical_protected_set_manifest,
    protected_set_digest,
)
from .schema import apply_migrations
from .transport import (
    TransportCommandReceipt,
    TransportCommandResponse,
)
from .transport_store import TransportDeliveryStore

JsonObject = dict[str, Any]
IdFactory = Callable[[str], str]
Clock = Callable[[], datetime]

_KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_ADAPTER_EVENT_RE = re.compile(r"[A-Za-z0-9_.:-]{1,500}\Z")
_SOURCE_AUTHORITY_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SOURCE_ALIAS_RE = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]{0,498}[\x21-\x7e])?\Z")
_PROJECT_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_TRANSPORT_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,49}\Z")
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_ASSET_ROOT_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_CONNECTOR_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
_CREDENTIAL_ALIAS_RE = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")
_CREDENTIAL_KEY_RE = re.compile(
    r"(^|[_-])(api[_-]?key|token|password|secret|credential)([_-]|$)",
    re.IGNORECASE,
)
_REGISTRY_RESOURCE_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,99}\Z")
_HEALTH_CATEGORY_RE = re.compile(r"[a-z][a-z0-9_.-]{0,99}\Z")
_HEALTH_METRIC_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HEALTH_STATUSES = frozenset({"ok", "degraded", "unavailable", "unknown"})
_HEALTH_SENSITIVE_TERMS = (
    "credential",
    "hash",
    "hostname",
    "path",
    "secret",
    "token",
)
_SOURCE_ALIAS_AUTHORITIES = frozenset({"arxiv", "doi", "sha256", "project"})
_RESEARCH_SCHEDULE_OPERATIONS = frozenset({"capture_drain", "legacy"})
_RESEARCH_CADENCE_SOURCES = frozenset({"product", "migrated", "unknown"})
_RESEARCH_SCHEDULE_OUTCOMES = frozenset({"ran", "skipped", "refused", "failed"})
_SOURCE_IMPORT_FAILURES = frozenset(
    {
        "adapter_unavailable",
        "invalid_source",
        "materialization_failed",
        "outcome_unknown",
    }
)
_CAPTURE_FAILURES = frozenset(
    {
        "adapter_unavailable",
        "invalid_source",
        "materialization_failed",
        "outcome_unknown",
    }
)
#: What an `uncertain` capture may say about itself. `outcome_unknown` is the
#: half-run-effect word (the default); ⟦V-R3⟧ `carrier_thread_busy` says the
#: consumer set the capture aside because somebody else's run holds its
#: thread (`ForeignCarrierRun`), and ⟦ADJ-2⟧ `carrier_thread_foreign` because
#: somebody else created the thread its carrier would use. All three are
#: reopenable, and none says the capture itself was wrong.
_CAPTURE_UNCERTAIN_CATEGORIES = frozenset(
    {"outcome_unknown", "carrier_thread_busy", "carrier_thread_foreign"}
)
#: ⟦P9-3⟧ The two of those that name something ELSE the capture is waiting on
#: -- a run still holding its carrier thread, or a foreign thread of the
#: carrier's title. `outcome_unknown` names nothing, so a capture carrying it
#: has no `blocked_by` to show. Public because the API decides from a
#: capture's CURRENT category whether to look one up at all.
CAPTURE_BLOCKING_CATEGORIES = frozenset(
    {"carrier_thread_busy", "carrier_thread_foreign"}
)
CAPTURE_STATES = frozenset(
    {
        "pending",
        "approved",
        "claimed",
        "uncertain",
        "consumed",
        "dismissed",
        "failed",
    }
)
#: ⟦BRK-7⟧ The three states no capture transition leaves: `approve` demands
#: `pending`, `dismiss` demands `_CAPTURE_DISMISSABLE`, `reopen` demands
#: `uncertain`, `claim` demands `approved` and `complete` demands `claimed`,
#: so a capture that reaches one of these is done. Public for the same reason
#: `CAPTURE_BLOCKING_CATEGORIES` is: the API decides from a capture's CURRENT
#: row whether a blocker is still worth naming, and on a done capture nobody
#: is waiting on the run or thread that fenced it.
CAPTURE_TERMINAL_STATES = frozenset({"consumed", "dismissed", "failed"})
_CAPTURE_DISMISSABLE = frozenset({"pending", "approved", "uncertain"})
_CAPTURE_PAYLOAD_MAX = 16_384
_CAPTURE_NOTE_MAX = 2_000
_CAPTURE_URL_SCHEMES = frozenset({"http", "https"})
# urlsplit removes every C0 control before parsing -- it deletes tab, CR and
# LF anywhere and strips the rest off both ends -- so a payload holding one is
# not the URL that parsing would leave behind and must not be keyed as though
# it were. Excluding them is also what makes the key derivation a pure
# in-place case fold: on what is left, urlsplit deletes nothing.
_CAPTURE_URL_DELETED = frozenset(chr(code) for code in range(0x20))
# The transport activation gate (D-P5-2). One transport, and a window bound
# of half an hour rather than migration 12's week: this gate is opened while
# the product deliberately holds a token the legacy research gateway also
# uses, under a procedure written for an operator who is present.
_ACTIVATION_TRANSPORTS = frozenset({"telegram"})
_TRANSPORT_WINDOW_MAX_SECONDS = 1_800
_ARTIFACT_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_ARTIFACT_FAILURE_RE = re.compile(r"[a-z][a-z0-9_.-]{0,99}\Z")
_ARTIFACT_ACTIVE_RUN_STATES = frozenset(
    {"queued", "starting", "running", "waiting_for_decision", "resuming", "retrying"}
)
_ARTIFACT_LIST_LIMIT = 500
_RESEARCH_PROJECTION_LIMIT = 200
_RUN_HISTORY_TIME_SQL = (
    "CASE WHEN instr(created_at, '.') = 0 "
    "THEN substr(created_at, 1, length(created_at) - 1) || '.000000Z' "
    "ELSE created_at END"
)
_TERMINAL_RUN_STATES = frozenset({"completed", "failed", "canceled"})
_WORKFLOW_FENCED_RUN_STATES = _TERMINAL_RUN_STATES | {"cancel_requested"}
_WORKFLOW_EXACT_REFERENCE_KINDS = frozenset(
    {"decision", "engine_source", "lineage_node"}
)
_WORKFLOW_CONTEXT_REFERENCE_KINDS = frozenset(
    {
        "source_resolution",
        "source",
        "source_binding",
        "artifact_version",
        "snapshot",
        "event",
        "runtime_event",
    }
)
_DECISION_OPTION_FIELDS = frozenset({"id", "label", "description"})
_SENSITIVE_DTO_FIELDS = frozenset(
    {
        "secret",
        "token",
        "path",
        "raw",
        "raw_payload",
        "reasoning",
        "hidden_reasoning",
        "chain_of_thought",
        "runtime_session_ref",
        "provider_request_id",
    }
)
_RECOVERY_OUTCOMES = frozenset(
    {"recovered", "dispatchable", "inactive", "retryable_failed", "manual_required"}
)
#: The states a run can be dispatched from: `RunOrchestrator.dispatch` refuses
#: any other, and the turn bridge and the control API hand a thread over only
#: when its active run is in one of these.
DISPATCHABLE_RUN_STATES = frozenset({"queued", "retrying", "resuming"})
#: The states in which a message arriving on the thread joins the run in
#: flight: about to be driven, or being driven. A run parked on a decision,
#: paused, resuming from a checkpoint or being cancelled is waiting on the
#: OPERATOR, and a message must not be the thing that moves it.
FOLLOWUP_RUN_STATES = frozenset({"queued", "retrying", "starting", "running"})
#: What a `cancel_requested` run whose attempt never bound a runtime is called
#: when it is converged to `canceled` without one -- the vocabulary
#: `RunOrchestrator.recover_startup` established, shared so the turn bridge and
#: the control API end such a run with the same word.
CANCELED_BEFORE_BINDING = "canceled_before_runtime_binding"

#: ⟦P8 V6-2 / V-R4⟧ What makes a thread a MACHINE thread -- one the research
#: engine's capture consumer owns, on which no run is ever an operator's
#: conversation -- decided from rows that exist for every generation's data
#: (no migration). Half one: the thread's `create_thread` receipt carries the
#: consumer's own actor, `CAPTURE_CONSUMER_ACTOR` -- who created it, not what
#: it is called, so an operator's thread in the consumer's workspace titled
#: "capture my thoughts" is an ordinary conversation. Every generation's
#: consumer created its threads through `create_thread(actor_id=MACHINE_ACTOR,
#: idempotency_key=...)` (`capture_consumer._thread`, unchanged since it was
#: written), and command receipts are never pruned, so the receipt exists for
#: every carrier thread the consumer uses: ⟦ADJ-2⟧ `_thread` adopts an
#: existing thread only when that receipt names the consumer
#: (`thread_creator`, the check `_carrier` applies to runs), sets the
#: capture aside (`ForeignCarrierThread`, `carrier_thread_foreign`) when
#: somebody else created the same-titled thread, and creates its own
#: otherwise -- never filing a receipt for a thread it did not create.
#: Half two: any run on the thread ever carried a workflow instance. Either
#: suffices; see
#: `ControlStore._MACHINE_THREAD_PREDICATE`. The workspace title and thread
#: title prefix are the consumer's discovery strings, kept here so the
#: consumer and the tests spell them once; they decide nothing in the store.
CAPTURE_CONSUMER_ACTOR = "machine:p4-capture-consumer"
CAPTURE_CONSUMER_WORKSPACE_TITLE = "Capture consumer"
CAPTURE_THREAD_TITLE_PREFIX = "capture "
#: ⟦ADJ-4 / batchK-9⟧ The capture workflow's definition id
#: (`RESEARCH_CAPTURE_WORKFLOW` in `capture_consumer` is built from it): the
#: workflow half of `_ENGINE_OWNED_RUN_PREDICATE`, the narrower of the two
#: forms `_machine_run_predicate` builds, beside the run's own `create_run`
#: receipt naming `CAPTURE_CONSUMER_ACTOR`. Only this definition, not "any
#: workflow instance": the store's exemption is this one actor, so a run
#: carrying some OTHER workflow would be left endable by the capture consumer
#: alone. The route asks the wider form, which is a different question --
#: see `_machine_run_predicate`.
CAPTURE_WORKFLOW_DEFINITION_ID = "research.capture"
#: ⟦batchK-8⟧ The ceiling on one page of `list_threads`. A workspace's thread
#: list is what the cockpit's rail is built from, so its ceiling is the
#: capture list's 500 rather than the run history's 100 -- large enough that
#: the rail is one request on any workspace an operator has today, small
#: enough that no request can be made to walk a list that grows by a carrier
#: thread per capture. `limit=None` is still the whole list, which is what
#: every caller in the tree asks for.
THREAD_PAGE_LIMIT = 500


def _drivable_thread_predicate(thread_column: str) -> str:
    """What a thread needs before a run on it can be a turn: something to answer.

    ⟦P9-2⟧ Either an operator's own message, or a transport scope bound to the
    thread -- the Telegram path, where what is to be answered arrives as a
    delivery rather than through the API. Spelled once, because two callers
    have to agree exactly: `_CONVERSATION_RUN_PREDICATE`, which decides
    whether the bridge may drive an existing run, and the API, which refuses
    to create a run that predicate could never admit.
    """

    return f"""
        EXISTS (SELECT 1 FROM messages m
                WHERE m.thread_id = {thread_column} AND m.role = 'user')
        OR EXISTS (SELECT 1 FROM transport_bindings b
                   WHERE b.thread_id = {thread_column})
    """


def _receipt_subject_id(response_column: str) -> str:
    """The id a receipt's stored response names, or NULL if the row is corrupt.

    ⟦batchO⟧ Every predicate that asks "which resource did this receipt
    create" reads `json_extract(<response_json>, '$.id')`, and `json_extract`
    RAISES `OperationalError: malformed JSON` on a row whose `response_json`
    is not JSON. One such row -- which the store's own writer cannot produce,
    but a corrupted or hand-edited database can hold -- took down every read
    that consults it: `GET /runs/{id}`, `GET /threads/{id}/runs` and
    `GET /threads` answered 503 `control_store_unavailable`, a category
    naming the store as unavailable while the rest of it answers perfectly.

    So the extraction is asked only of a row that is JSON, and a row that is
    not contributes NOTHING rather than raising: `NULL = <id>` is NULL, which
    is not true, so the corrupt row simply fails to match. The `CASE` is
    load-bearing over the shorter `json_valid(x) AND json_extract(x, ...)`,
    because AND terms in a WHERE clause are the planner's to reorder while
    `CASE` is evaluated in written order by definition.

    For a WELL-FORMED row the answer is unchanged by construction:
    `json_valid` is true exactly when `json_extract` would not raise, so the
    `CASE` reduces to the extraction it replaced. In particular the receipt
    tie-break ("the first receipt by `(created_at, actor_id)`") is untouched
    -- the guard changes which rows are candidates, never their order.

    ⟦batchQ A-4⟧ What the direction costs, said rather than left to be
    discovered. ⟦batchS ADJ-2⟧ When the unreadable row is the subject's OWN
    creating receipt, `run_creator`, `thread_creator` and
    `workspace_creator` return None for it -- unconditionally, since that
    receipt is the whole of what they read. Whether the subject also stops
    being the ENGINE'S is a narrower question: `_MACHINE_RUN_PREDICATE` and
    `_MACHINE_THREAD_PREDICATE` each OR that receipt half with a workflow
    half -- a run owning a workflow instance (control/store.py:521), a
    thread any run of which ever carried one
    (control/store.py:11315-11317). So a gen-13 carrier that owns its
    workflow still answers "the engine's" at both doors, and only its
    creator changes. "Not the engine's at every door" is the answer for a
    subject whose receipt is its ONLY machine signal: a workflow-less
    carrier, the shape a pre-V-3 consumer left between its two transactions.

    For that subject the cockpit is told a confident `engine_owned: false`
    rather than shown an error, and offers an operator the Cancel it would
    otherwise hide. ⟦batchS N3⟧ The engine reads the None creator too:
    `capture_consumer._thread` adopts a `capture <id>` thread only when
    `thread_creator` returns its own actor
    (engine/capture_consumer.py:624), so the consumer's own carrier thread
    reads as somebody else's and the capture is set aside as
    `ForeignCarrierThread` (engine/capture_consumer.py:628).

    ⟦batchS ADJ-1⟧ And the write the corruption permits is not only an
    operator's. A capture thread that stops matching
    `_MACHINE_THREAD_PREDICATE` makes a workflow-less carrier run on it
    match `_CONVERSATION_RUN_PREDICATE` (control/store.py:10929-10935) as soon
    as the thread is drivable at all -- an operator message the API stored,
    or a transport binding -- and the turn bridge's start sweep projects
    exactly those runs (transports/bridge.py:2044). So a restarted daemon
    drives a Hermes turn on the engine's own carrier when the dispatch gate
    is open. ⟦batchT ADJ-A1⟧ With the gate shut the answer splits on the
    same two conditions: on a thread nothing can deliver to, the run is
    ended typed by `_end_undriven` (transports/bridge.py:2022-2024); on a
    transport-bound thread nothing is written at all -- the bridge returns
    at `thread_has_transport_binding` (transports/bridge.py:2017-2018) and
    the carrier stays `queued` for the next submit after the gate opens. No
    operator acts in any of the three.

    That is still the accepted direction rather than an oversight -- the
    alternative is admitting a MACHINE write on the strength of a receipt
    nobody can read, and a subject somebody can act on is an outcome where
    a subject nobody can act on is not -- but it does NOT buy an operator
    write in place of a machine one: under a capture thread's unreadable
    receipt it buys both. What the daemon then does is the ordinary
    treatment of a workflow-less run on a non-machine thread; the
    corruption changes only which threads fall into that class.

    ⟦batchQ A-6⟧ The guard is not free -- `json_valid` parses each candidate
    row a second time. Measured through the real route on a 900-thread
    whole-workspace ownership read (median of 7, two independent passes):
    69.3 -> 74.2 ms and 77.7 -> 83.1 ms, about +7 %, and roughly +3 % to
    +9 % across the five request shapes the bench ran. The cost CLASS is
    what was required to be unchanged, and is -- the same scan, no extra
    table, no index and no migration -- but the constant belongs here rather
    than in the next reader's profiler.

    ⟦batchQ A-8⟧ Six SQL sites read a receipt's subject through this helper,
    and they are the guarded ones. The SEVENTH `json_extract` in this module
    -- the `run_events` payload read in `_expect_workflow_artifact_result`
    -- is deliberately left bare, so this enumeration is exhaustive over the
    module's SQL rather than merely the sites somebody noticed. It is a
    write path over rows a single writer produces (`run_events` is INSERTed
    in one place here, and that payload is one this store emitted earlier in
    the same run), and guarding it would answer a corrupt payload with
    `InvalidTransition("artifact_snapshot_identity_mismatch", ...)` -- a
    false diagnosis, since nothing about the identity is known. One write
    that fails loudly is not the harm batchO fixed, which was every reader
    of an unrelated resource getting 503 `control_store_unavailable`.

    ⟦batchS ADJ-3⟧ Over SQL, and only there. Three PYTHON readers parse a
    `response_json` column with a bare `json.loads` and carry no guard at
    all: `_receipt` (control/store.py:12781-12805) over these same
    `idempotency_receipts` rows, `_runtime_event_replay`
    (control/store.py:11728-11795) over `runtime_event_inbox`, and
    `_transport_command_row` (control/store.py:14488-14515) over
    `transport_command_receipts`. Each raises `json.JSONDecodeError` on a
    corrupt row, and ⟦batchT ADJ-A2⟧ the door that error reaches is not the
    same for all three. `_receipt` sits behind the control API's command
    routes, where `ControlAPI.handle` answers its decode error as 400
    `invalid_request` (api/app.py:308-309) -- so a replayed command whose
    own stored receipt is unreadable blames the caller for a row the caller
    did not write. `_runtime_event_replay` never reaches that door:
    `adapter_event_id` is supplied only by the daemon's runtime delivery
    path (orchestration/service.py:733) through `apply_runtime_transition`
    (control/store.py:5976-6011); every control-API caller leaves it None,
    and the reader returns at its `adapter_event_id` guard
    (control/store.py:11737-11738) before it parses anything -- so its
    corrupt row fails a daemon-side runtime event, not an operator's
    request. The third is the Telegram adapter's command replay
    (`transports/ports.py:244-255`). Left as they are: each is a single-row
    read on the exact key its caller supplied, where "contributes nothing"
    is not an answer a replay could act on, and batchO's harm was every
    reader of an UNRELATED resource going down rather than one command
    failing on its own receipt. Named here so the next reader knows the
    guard covers the predicates, not the module.
    """

    return (
        f"CASE WHEN json_valid({response_column})"
        f" THEN json_extract({response_column}, '$.id') END"
    )


def _machine_run_predicate(*, workflow_definition_id: str | None) -> str:
    """What makes a run the research engine's, as a predicate over `runs r`.

    ⟦ADJ-A / batchK-9⟧ ONE source for the two questions the daemon asks about
    a run's owner, which differ in exactly one clause and used to be two
    hand-written pairs that could drift apart without anything noticing:

    * `workflow_definition_id=None` -- `ControlStore._MACHINE_RUN_PREDICATE`,
      the ROUTE's question: may this door end the run? Any workflow instance
      counts, because a terminal run fences whichever workflow it carries
      (`_expect_workflow_run_open`), not only the capture's. This is what
      `run_is_machine` answers, what `machine_run_ids` projects as
      `engine_owned`, and what `ControlAPI._machine_run_refusal` refuses on --
      so the cockpit hides exactly the buttons the API would refuse.

      ⟦batchO ADJ-5⟧ The difference between this form and the narrower one
      below is unreachable today, and that is a fact about the code rather
      than a hope: the only workflow definition that is not the capture's is
      `GOLDEN_RESEARCH_WORKFLOW`, and the only caller that installs it is
      `GoldenWorkflowFacade.start`
      (cortex_platform/product/workflows/golden.py:143), which no daemon path
      constructs -- every `GoldenWorkflowFacade(` site is a test or
      apps/web/scripts/workflow-control-fixture.py. So the Telegram door and
      the cockpit door cannot disagree about any run that exists today; the
      divergence becomes reachable the moment a second definition gains a
      production installer, and whoever writes that installer owns this
      paragraph.

    * `workflow_definition_id=CAPTURE_WORKFLOW_DEFINITION_ID` -- the STORE's
      question, `_ENGINE_OWNED_RUN_PREDICATE`: is this run the ENGINE's, so
      that no writer at all may end it? Deliberately narrower, and the
      narrowing is load-bearing rather than an oversight: the store's
      exemption is one named actor (`CAPTURE_CONSUMER_ACTOR`), so widening
      this half to any workflow would leave a run carrying some OTHER
      workflow endable by the capture consumer alone -- that is, by nobody
      who has any business ending it. The workflow layer's own callers end
      runs carrying their workflows.

    The receipt half is shared verbatim, and reproduces `run_creator`'s
    "first receipt by (created_at, actor_id)" rather than "any receipt by the
    consumer": that is the rule the refusal applies, and a replayed command
    can file a second receipt for one run.

    Deliberately NOT the thread's ownership, in either form: a workflow-less
    run an operator opened on a `capture` thread stays theirs to end, which
    is the whole of the `ForeignCarrierRun` recovery.
    """

    workflow = (
        "EXISTS (SELECT 1 FROM workflow_instances w WHERE w.run_id = r.id)"
        if workflow_definition_id is None
        else (
            "EXISTS (SELECT 1 FROM workflow_instances w WHERE w.run_id = r.id"
            f" AND w.definition_id = '{workflow_definition_id}')"
        )
    )
    return f"""
        {workflow}
        OR (SELECT ir.actor_id FROM idempotency_receipts ir
            WHERE ir.operation =
                  'POST:/api/v1/threads/' || r.thread_id || '/runs'
              AND {_receipt_subject_id("ir.response_json")} = r.id
            ORDER BY ir.created_at, ir.actor_id LIMIT 1) = '{CAPTURE_CONSUMER_ACTOR}'
    """


_RUN_TRANSITIONS = {
    "queued": {"starting", "cancel_requested", "failed"},
    "starting": {"running", "cancel_requested", "failed"},
    "running": {
        "waiting_for_decision",
        "pause_requested",
        "cancel_requested",
        "completed",
        "failed",
    },
    "waiting_for_decision": {"resuming", "cancel_requested", "failed"},
    # ⟦P9-3⟧ `canceled` is here because a conversation turn can never reach
    # `paused`: that target demands the attempt carry a `checkpoint_uri` and a
    # Hermes turn writes none, and the runtime adapter refuses the pause
    # outright (`pause_requires_cortex_stage_boundary`). Without this edge the
    # only terminal a paused turn could reach was `failed`, so every late
    # worker event under `pause_requested` ended a turn the operator merely
    # wanted held as a failure with a Retry button.
    "pause_requested": {"paused", "canceled", "cancel_requested", "failed"},
    "paused": {"cancel_requested"},
    "resuming": {"starting", "running", "cancel_requested", "failed"},
    "cancel_requested": {"canceled", "failed"},
    "failed": set(),
    "retrying": {"starting", "cancel_requested", "failed"},
    "completed": set(),
    "canceled": set(),
}


@dataclass(frozen=True)
class CommandResult:
    value: JsonObject
    status_code: int
    replayed: bool = False


class _CursorPage(list[JsonObject]):
    """List-compatible page carrying a proven continuation cursor.

    A `list` first, so every caller that only iterates -- the Telegram thread
    list, the capture consumer's adoption scan -- is unchanged, with the
    cursor riding along for the route that pages.
    """

    def __init__(self, items: Sequence[JsonObject], *, next_cursor: str | None) -> None:
        super().__init__(items)
        self.next_cursor = next_cursor


@dataclass(frozen=True)
class _ValidatedPairedProof:
    id: str
    protected_set_manifest: ProtectedSetManifest
    manifest_json: str
    backup_set_digest: str
    primary_completed_at: datetime
    primary_snapshot_count: int
    independent_completed_at: datetime
    independent_snapshot_count: int
    restore_completed_at: datetime
    restored_database_count: int
    verified_sample_count: int


def _capture_shape(payload: str) -> tuple[str, str]:
    """Decide a capture's kind and its dedup key in one pure function.

    URL-shaped means the trimmed payload parses with an http/https scheme
    and a non-empty host. The only rewriting is lowering the scheme and the
    host: canonicalizing a locator is resolution, and resolution may not
    happen before the operator has approved the capture.
    """

    if not _CAPTURE_URL_DELETED.isdisjoint(payload):
        return "text", payload
    try:
        parts = urlsplit(payload)
    except ValueError:
        return "text", payload
    if parts.scheme not in _CAPTURE_URL_SCHEMES or not parts.hostname:
        return "text", payload
    # Lower the two spans where they sit rather than reassembling the URL:
    # urlunsplit drops an explicit empty query or fragment, which would key
    # two different submissions the same way.
    netloc_start = len(parts.scheme) + len("://")
    netloc_end = netloc_start + len(parts.netloc)
    userinfo, separator, hostinfo = parts.netloc.rpartition("@")
    if hostinfo.startswith("["):
        host, bracket, after = hostinfo[1:].partition("]")
        lowered = f"[{host.lower()}{bracket}{after}"
    else:
        host, colon, port = hostinfo.partition(":")
        lowered = f"{host.lower()}{colon}{port}"
    key = (
        payload[: len(parts.scheme)].lower()
        + payload[len(parts.scheme) : netloc_start]
        + f"{userinfo}{separator}{lowered}"
        + payload[netloc_end:]
    )
    return "url", key


class ControlStore(TransportDeliveryStore, ResearchItemsStore):
    """Own durable product metadata without calling runtime or domain stores."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (
            lambda kind: f"{kind}_{uuid.uuid4().hex}"
        )
        self._timeout = timeout
        self._migration_lock = threading.Lock()
        self._binding_key: bytes | None = None

    def initialize(self) -> None:
        self._prepare_directory()
        with self._migration_lock, self._initialization_guard():
            self._prepare_database_file()
            self._binding_key = self._load_or_create_binding_key()
            with self._connect() as conn:
                apply_migrations(conn, now=self._registry_now())
                journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
                if journal_mode.lower() != "wal":
                    conn.execute("PRAGMA journal_mode = WAL")

    def register_asset_root(
        self,
        *,
        root_id: str,
        private_path: Path,
        max_bytes: int,
        enabled: bool,
        actor_id: str,
        idempotency_key: str,
    ) -> AssetRootRecord:
        root_id, private_path, max_bytes, enabled = self._validated_asset_root(
            root_id=root_id,
            private_path=private_path,
            max_bytes=max_bytes,
            enabled=enabled,
        )
        request = {
            "root_id": root_id,
            "private_path": str(private_path),
            "max_bytes": max_bytes,
            "enabled": enabled,
        }
        operation = "INTERNAL:asset-roots/register"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )
            if replay is not None:
                return self._asset_root_receipt(replay.value)
            if conn.execute(
                "SELECT 1 FROM asset_roots WHERE root_id = ?", (root_id,)
            ).fetchone() is not None:
                raise InvalidTransition("already_registered", "registration")
            now = self._now()
            conn.execute(
                """INSERT INTO asset_roots
                   (root_id, private_path, max_bytes, enabled, revision,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?)""",
                (root_id, str(private_path), max_bytes, int(enabled), now, now),
            )
            record = self._asset_root(conn, root_id)
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._asset_root_response(record),
                201,
            )
            return record

    def update_asset_root(
        self,
        *,
        root_id: str,
        private_path: Path,
        max_bytes: int,
        enabled: bool,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> AssetRootRecord:
        root_id, private_path, max_bytes, enabled = self._validated_asset_root(
            root_id=root_id,
            private_path=private_path,
            max_bytes=max_bytes,
            enabled=enabled,
        )
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        request = {
            "root_id": root_id,
            "private_path": str(private_path),
            "max_bytes": max_bytes,
            "enabled": enabled,
            "expected_revision": expected_revision,
        }
        operation = f"INTERNAL:asset-roots/{root_id}/update"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )
            if replay is not None:
                return self._asset_root_receipt(replay.value)
            now = self._now()
            cursor = conn.execute(
                """UPDATE asset_roots
                   SET private_path = ?, max_bytes = ?, enabled = ?,
                       revision = revision + 1, updated_at = ?
                   WHERE root_id = ? AND revision = ?""",
                (
                    str(private_path),
                    max_bytes,
                    int(enabled),
                    now,
                    root_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                current = self._asset_root(conn, root_id)
                raise RevisionConflict(
                    {"id": current.root_id, "revision": current.revision}
                )
            record = self._asset_root(conn, root_id)
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._asset_root_response(record),
                200,
            )
            return record

    def commit_adoption_manifest(
        self,
        *,
        manifest: AdoptionManifest,
        corpus_root_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> AdoptionManifestRecord:
        """Resolve a whole copied corpus with one explicit operator decision.

        The single-source path exists for a paper met mid-run: it parks a live
        attempt on a human decision. A copied corpus has no run and thousands
        of sources, so this is its bulk counterpart -- one commit of one
        content-addressed manifest IS the resolution for every source it
        names. Nothing is fetched: the content is already on disk under the
        corpus root, so each entry lands `existing`, never `pending`, and no
        import action is ever created.
        """

        if type(manifest) is not AdoptionManifest:
            raise TypeError("manifest must be an AdoptionManifest")
        corpus_root_id = self._asset_root_id(corpus_root_id)
        manifest_id = manifest.manifest_id
        request = {
            "manifest_id": manifest_id,
            "corpus_root_id": corpus_root_id,
        }
        operation = "INTERNAL:adoption/commit"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )
            if replay is not None:
                return self._adoption_manifest_receipt(replay.value)
            root = self._asset_root(conn, corpus_root_id)
            if not root.enabled:
                raise InvalidTransition("corpus_root_disabled", "adoption")
            if conn.execute(
                "SELECT 1 FROM adoption_manifests WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone() is not None:
                raise InvalidTransition("already_adopted", "adoption")
            now = self._now()
            # Resolve every source first: the entry rows reference the
            # manifest, the manifest carries the already-adopted count, and
            # its immutability trigger forbids filling that in afterwards.
            adopted = 0
            registered: list[tuple[str, AdoptionEntry]] = []
            for entry in manifest.entries:
                source_id, was_adopted = self._adopt_source(conn, entry, now=now)
                adopted += int(was_adopted)
                # Every entry is recorded, including one already adopted by an
                # earlier manifest: a parity check must be able to prove which
                # corpus THIS manifest saw, not just what it added.
                registered.append((source_id, entry))
            conn.execute(
                """INSERT INTO adoption_manifests
                   (manifest_id, corpus_root_id, actor_id, entry_count,
                    adopted_count, committed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    manifest_id,
                    corpus_root_id,
                    actor_id,
                    len(manifest.entries),
                    adopted,
                    now,
                ),
            )
            for source_id, entry in registered:
                conn.execute(
                    """INSERT INTO adoption_entries
                       (id, manifest_id, source_id, paper_dir, engine_ref,
                        content_digest, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self._id_factory("adoption-entry"),
                        manifest_id,
                        source_id,
                        entry.paper_dir,
                        entry.engine_ref,
                        entry.content_digest,
                        now,
                    ),
                )
            record = self._adoption_manifest(conn, manifest_id)
            self._audit(
                conn,
                "adoption",
                manifest_id,
                "adoption.committed",
                {
                    "corpus_root_id": corpus_root_id,
                    "entry_count": record.entry_count,
                    "adopted_count": record.adopted_count,
                },
            )
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._adoption_manifest_response(record),
                201,
            )
            return record


    def create_capture(
        self,
        *,
        payload: str,
        note: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Stage one raw operator submission without deciding anything.

        A capture is not a source and not a source intent: it stores the
        submitted string and performs no resolution, because approval is the
        explicit decision the product shell requires before any research
        import may run.
        """

        # Validate the bounds without rebinding: the stored payload is the
        # operator's submission verbatim, and only the dedup key is derived
        # from the trimmed string.
        self._required_text(payload, "payload", maximum=_CAPTURE_PAYLOAD_MAX)
        note = self._capture_note(note)
        kind, capture_key = _capture_shape(payload.strip())
        request = {"payload": payload, "note": note}
        operation = "POST:/api/v1/captures"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            # Content-native idempotency, the adoption `already_adopted`
            # precedent: only an OPEN capture blocks. A consumed, dismissed
            # or failed row is history, so the same payload may be captured
            # again deliberately.
            open_row = conn.execute(
                """SELECT id FROM captures
                   WHERE capture_key = ?
                     AND state IN ('pending', 'approved', 'claimed', 'uncertain')
                   ORDER BY created_at, id LIMIT 1""",
                (capture_key,),
            ).fetchone()
            if open_row is not None:
                raise CaptureConflict(self._capture(conn, str(open_row["id"])))
            now = self._now()
            capture_id = self._id_factory("capture")
            conn.execute(
                """INSERT INTO captures
                   (id, capture_key, payload, kind, note, state, claim_epoch,
                    known_source_id, revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, 0, ?, ?)""",
                (
                    capture_id,
                    capture_key,
                    payload,
                    kind,
                    note,
                    self._known_capture_source(conn, capture_key),
                    now,
                    now,
                ),
            )
            # The payload never enters the audit trail: only the shape does.
            self._audit(
                conn, "capture", capture_id, "capture.created", {"kind": kind}
            )
            value = self._capture(conn, capture_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def capture_blocked_by(self, capture_ids: Sequence[str]) -> dict[str, str]:
        """What each capture's newest uncertain audit row said was blocking it.

        ⟦V-R3 / P9-3⟧ `complete_capture` writes the id of the run or thread
        that fenced a capture's carrier onto the capture's `control_audit` row
        and NOWHERE else -- the `captures` table has no detail column and this
        program makes no migration -- so the audit trail is the only place an
        operator can be shown it from. `effect_watch_digests` reads the same
        table the same way, for the same reason.

        One query for a whole page rather than one per row: the A-1 finding
        was exactly a per-row probe behind a list route, and a capture page is
        the same shape. `control_audit_aggregate_idx` covers
        `(aggregate_type, aggregate_id, cursor)`.

        Rows are read oldest first so the NEWEST wins, and an uncertain row
        that names nothing (`outcome_unknown`) correctly clears an older one.
        Callers are expected to name only the captures whose current
        `failure_category` says there is something to find, which is what
        stops a reopened capture from reporting the run that blocked the
        attempt before it.
        """

        ids = [str(capture_id) for capture_id in capture_ids]
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT aggregate_id, payload_json FROM control_audit
                    WHERE aggregate_type = 'capture'
                      AND aggregate_id IN ({placeholders})
                      AND type = 'capture.uncertain'
                    ORDER BY cursor""",
                tuple(ids),
            ).fetchall()
        blocked: dict[str, str] = {}
        for row in rows:
            aggregate_id = str(row["aggregate_id"])
            value = json.loads(str(row["payload_json"])).get("blocked_by")
            if isinstance(value, str) and value:
                blocked[aggregate_id] = value
            else:
                blocked.pop(aggregate_id, None)
        return blocked

    def get_capture(self, capture_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._capture(conn, capture_id)

    def list_captures(
        self,
        *,
        state: str | None = None,
        limit: int = 500,
        cursor: str | None = None,
    ) -> _CursorPage:
        """One bounded page of the inbox, oldest first.

        A terminal capture is never deleted -- the delete guard forbids it --
        so an unbounded list would grow for as long as the operator uses the
        omnibox and has no prune path. `cursor` is the id of the last row of
        the previous page; the sort key stays `(created_at, id)` so the page
        boundary is stable when two rows share a timestamp.

        ⟦batchO⟧ The continuation cursor is proven by reading one row further,
        not guessed from the page being full. `(created_at, id)` is a total
        order because `id` is unique, so the extra row answers "is there more"
        exactly; the route's old `items[-1]["id"] if len(items) == limit else
        None` was wrong on precisely the case a cursor exists to settle -- a
        last page that happens to be exactly full, where it handed back a
        cursor whose page comes back empty.
        """

        if state is not None and state not in CAPTURE_STATES:
            raise ValueError("capture state is invalid")
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            after_created_at: str | None = None
            if cursor is not None:
                row = conn.execute(
                    "SELECT created_at FROM captures WHERE id = ?", (cursor,)
                ).fetchone()
                if row is None:
                    raise ValueError("cursor is invalid")
                after_created_at = str(row["created_at"])
            rows = conn.execute(
                """SELECT id FROM captures
                   WHERE (? IS NULL OR state = ?)
                     AND (? IS NULL OR (created_at, id) > (?, ?))
                   ORDER BY created_at, id LIMIT ?""",
                (state, state, cursor, after_created_at, cursor, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            items = [self._capture(conn, str(row["id"])) for row in rows[:limit]]
            return _CursorPage(
                items,
                next_cursor=str(items[-1]["id"]) if has_more and items else None,
            )

    def approve_capture(
        self,
        *,
        capture_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Record the decision that authorizes one import, and only that."""

        request = {"expected_revision": expected_revision}
        operation = f"POST:/api/v1/captures/{capture_id}/approve"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            capture = self._capture(conn, capture_id)
            self._expect_revision(capture, expected_revision)
            if capture["state"] != "pending":
                raise InvalidTransition(str(capture["state"]), "approved")
            now = self._now()
            cursor = conn.execute(
                """UPDATE captures
                   SET state = 'approved', revision = revision + 1,
                       updated_at = ?
                   WHERE id = ? AND revision = ? AND state = 'pending'""",
                (now, capture_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(self._capture(conn, capture_id))
            self._audit(conn, "capture", capture_id, "capture.approved", {})
            value = self._capture(conn, capture_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def dismiss_capture(
        self,
        *,
        capture_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Close a capture the operator does not want imported.

        A claimed capture is refused: its lease must expire into `uncertain`
        first, because dismissing a row a consumer may still be ingesting
        would record a decision the product cannot honour.
        """

        request = {"expected_revision": expected_revision}
        operation = f"POST:/api/v1/captures/{capture_id}/dismiss"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            capture = self._capture(conn, capture_id)
            self._expect_revision(capture, expected_revision)
            if capture["state"] not in _CAPTURE_DISMISSABLE:
                raise InvalidTransition(str(capture["state"]), "dismissed")
            now = self._now()
            cursor = conn.execute(
                """UPDATE captures
                   SET state = 'dismissed', revision = revision + 1,
                       updated_at = ?
                   WHERE id = ? AND revision = ?
                     AND state IN ('pending', 'approved', 'uncertain')""",
                (now, capture_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(self._capture(conn, capture_id))
            self._audit(conn, "capture", capture_id, "capture.dismissed", {})
            value = self._capture(conn, capture_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )


    def reopen_capture(
        self,
        *,
        capture_id: str,
        expected_revision: int,
        acknowledged: bool,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Re-authorize an import whose previous outcome is unknown.

        The acknowledgement is mandatory and part of the request hash: an
        `uncertain` row may already have minted a paper_dir, so re-opening
        it is the operator accepting a possible duplicate ingest, not a
        retry the product may perform on its own.
        """

        if type(acknowledged) is not bool or not acknowledged:
            raise ValueError("acknowledged must be true")
        request = {"expected_revision": expected_revision, "acknowledged": True}
        operation = f"POST:/api/v1/captures/{capture_id}/reopen"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            capture = self._capture(conn, capture_id)
            self._expect_revision(capture, expected_revision)
            if capture["state"] != "uncertain":
                raise InvalidTransition(str(capture["state"]), "approved")
            now = self._now()
            # claim_epoch is deliberately retained: the previous consumer's
            # fence token has to stay invalid forever.
            cursor = conn.execute(
                """UPDATE captures
                   SET state = 'approved', claim_owner = NULL,
                       claim_expires_at = NULL, failure_category = NULL,
                       revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ? AND state = 'uncertain'""",
                (now, capture_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(self._capture(conn, capture_id))
            self._audit(conn, "capture", capture_id, "capture.reopened", {})
            value = self._capture(conn, capture_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def list_pending_captures(self, *, limit: int = 500) -> list[JsonObject]:
        """The consumer's work queue: approved rows, and nothing else.

        Captures are structurally invisible to run_events, so the consumer
        polls this predicate instead of tailing a stream.
        """

        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._transaction() as conn:
            self._sweep_expired_captures(conn)
            rows = conn.execute(
                """SELECT id FROM captures WHERE state = 'approved'
                   ORDER BY created_at, id LIMIT ?""",
                (limit,),
            ).fetchall()
            return [self._capture(conn, str(row["id"])) for row in rows]

    def claim_capture(
        self,
        *,
        capture_id: str,
        worker_id: str,
        lease_seconds: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        request = {"worker_id": worker_id, "lease_seconds": lease_seconds}
        operation = f"POST:/internal/v1/captures/{capture_id}/claim"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                current = self._capture(conn, capture_id)
                if (
                    current["state"] == "claimed"
                    and current["claim_owner"] == worker_id
                    and current["claim_epoch"] == replay.value.get("claim_epoch")
                ):
                    return replay
                raise InvalidTransition("stale_claim_receipt", "claimed")
            self._sweep_expired_captures(conn)
            capture = self._capture(conn, capture_id)
            if capture["state"] != "approved":
                raise InvalidTransition(str(capture["state"]), "claimed")
            now = self._utc_now()
            expires = self._format_time(now + timedelta(seconds=lease_seconds))
            # There is no takeover clause on purpose: only an `approved` row
            # is claimable, and an expired lease leaves `approved` for
            # `uncertain`, so an abandoned capture is never handed out twice.
            cursor = conn.execute(
                """UPDATE captures
                   SET state = 'claimed', claim_owner = ?, claim_expires_at = ?,
                       claim_epoch = claim_epoch + 1, revision = revision + 1,
                       updated_at = ?
                   WHERE id = ? AND state = 'approved'""",
                (worker_id, expires, self._format_time(now), capture_id),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_conflict", "claimed")
            self._audit(conn, "capture", capture_id, "capture.claimed", {})
            value = self._capture(conn, capture_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def complete_capture(
        self,
        *,
        capture_id: str,
        claim_owner: str,
        claim_epoch: int,
        outcome: str,
        consumed_source_ids: Sequence[str] | None = None,
        failure_category: str | None = None,
        detail: Mapping[str, str] | None = None,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Close a claimed capture under its own fence token.

        A consumer that cannot prove it did not perform the side effect
        reports `uncertain` rather than a failure, because a duplicate
        ingest mints a duplicate paper_dir.

        ⟦ADJ-2 / V-R3⟧ `detail` names what the category points at (the run
        or thread that blocked the carrier), on the capture's audit row:
        the `captures` table has no detail column and no migration is made,
        so the category is what the row and the cockpit carry, and the
        audit trail is where the id lives. Part of the request hash.
        """

        claim_owner = self._required_text(claim_owner, "claim_owner", maximum=200)
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        source_ids, category = self._capture_outcome(
            outcome, consumed_source_ids, failure_category
        )
        detail = {
            self._required_text(key, "detail key", maximum=100): self._required_text(
                value, "detail value", maximum=200
            )
            for key, value in dict(detail or {}).items()
        }
        request = {
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "outcome": outcome,
            "consumed_source_ids": source_ids,
            "failure_category": category,
        }
        if detail:
            # Absent rather than empty, so every receipt a completion without
            # a detail ever filed keeps its request hash.
            request["detail"] = detail
        operation = f"POST:/internal/v1/captures/{capture_id}/complete"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            self._capture(conn, capture_id)
            if source_ids:
                self._validate_capture_sources(conn, source_ids)
            now = self._now()
            # The lease is not part of the fence: an expired claim is never
            # redelivered, so nobody else can be holding this row, and
            # discarding a proven outcome would lose more than it protects.
            cursor = conn.execute(
                """UPDATE captures
                   SET state = ?, claim_owner = NULL, claim_expires_at = NULL,
                       consumed_source_ids = ?, failure_category = ?,
                       revision = revision + 1, updated_at = ?
                   WHERE id = ? AND state = 'claimed' AND claim_owner = ?
                     AND claim_epoch = ?""",
                (
                    outcome,
                    self._json(source_ids) if source_ids else None,
                    category,
                    now,
                    capture_id,
                    claim_owner,
                    claim_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", outcome)
            self._audit(
                conn,
                "capture",
                capture_id,
                f"capture.{outcome}",
                {"failure_category": category, **detail},
            )
            value = self._capture(conn, capture_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def _sweep_expired_captures(self, conn: sqlite3.Connection) -> None:
        """Turn an expired lease into `uncertain`; never redeliver the work.

        Handing the row to another consumer would be exactly the auto-retry
        the discipline forbids, so the capture records that its outcome is
        unknown and waits for an operator acknowledgement instead.
        """

        now = self._now()
        expired = [
            str(row["id"])
            for row in conn.execute(
                """SELECT id FROM captures
                   WHERE state = 'claimed' AND claim_expires_at IS NOT NULL
                     AND claim_expires_at <= ?""",
                (now,),
            ).fetchall()
        ]
        if not expired:
            return
        # The epoch is retained: the lost consumer's fence token must never
        # become valid again.
        conn.execute(
            """UPDATE captures
               SET state = 'uncertain', claim_owner = NULL,
                   claim_expires_at = NULL,
                   failure_category = 'outcome_unknown',
                   revision = revision + 1, updated_at = ?
               WHERE state = 'claimed' AND claim_expires_at IS NOT NULL
                 AND claim_expires_at <= ?""",
            (now, now),
        )
        for capture_id in expired:
            self._audit(
                conn,
                "capture",
                capture_id,
                "capture.lease_expired",
                {"failure_category": "outcome_unknown"},
            )

    def _capture_outcome(
        self,
        outcome: str,
        consumed_source_ids: Sequence[str] | None,
        failure_category: str | None,
    ) -> tuple[list[str] | None, str | None]:
        if outcome == "consumed":
            if failure_category is not None:
                raise ValueError("a consumed capture has no failure category")
            return self._capture_source_ids(consumed_source_ids), None
        if consumed_source_ids:
            raise ValueError("only a consumed capture names sources")
        if outcome == "failed":
            if failure_category not in _CAPTURE_FAILURES:
                raise ValueError("capture failure category is invalid")
            return None, failure_category
        if outcome == "uncertain":
            if failure_category is None:
                return None, "outcome_unknown"
            if failure_category not in _CAPTURE_UNCERTAIN_CATEGORIES:
                raise ValueError("capture failure category is invalid")
            return None, failure_category
        raise ValueError("capture outcome is invalid")

    def _capture_source_ids(self, value: Sequence[str] | None) -> list[str]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError("consumed_source_ids is invalid")
        source_ids = [
            self._required_text(item, "consumed_source_id", maximum=200)
            for item in value
        ]
        if not 1 <= len(source_ids) <= 100:
            raise ValueError("consumed_source_ids must name 1 to 100 sources")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("consumed_source_ids must be unique")
        return source_ids

    @staticmethod
    def _validate_capture_sources(
        conn: sqlite3.Connection, source_ids: Sequence[str]
    ) -> None:
        """Refuse a completion that names a source the corpus does not hold.

        The first entry is republished as the public `known_source_id` hint on
        the next paste of the same key, and a capture row can never be
        deleted, so an unresolvable id planted here is permanent. This is the
        discipline `_validate_artifact_sources` already applies to the equally
        internal-only artifact path.
        """

        placeholders = ",".join("?" for _ in source_ids)
        rows = conn.execute(
            f"SELECT id FROM sources WHERE id IN ({placeholders})",
            tuple(source_ids),
        ).fetchall()
        if {str(row["id"]) for row in rows} != set(source_ids):
            raise ValueError("consumed_source_ids names an unknown source")

    def _capture(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM captures WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("capture", resource_id)
        value = self._row(row)
        consumed = value["consumed_source_ids"]
        value["consumed_source_ids"] = json.loads(consumed) if consumed else None
        return value

    def _known_capture_source(
        self, conn: sqlite3.Connection, capture_key: str
    ) -> str | None:
        """Two exact-string lookups against the corpus that hint, never refuse.

        String equality is evidence, not identity. The alias lookup can only
        hit a bare-identifier paste -- alias values structurally cannot be
        URLs -- so a URL-form paste of an already adopted source stays
        undetectable here, a recorded gap.
        """

        alias = conn.execute(
            """SELECT source_id FROM source_aliases
               WHERE normalized_value = ? ORDER BY id LIMIT 1""",
            (capture_key,),
        ).fetchone()
        if alias is not None:
            return str(alias["source_id"])
        consumed = conn.execute(
            """SELECT consumed_source_ids FROM captures
               WHERE capture_key = ? AND state = 'consumed'
                 AND consumed_source_ids IS NOT NULL
               ORDER BY updated_at DESC, id DESC LIMIT 1""",
            (capture_key,),
        ).fetchone()
        if consumed is None:
            return None
        source_ids = json.loads(str(consumed["consumed_source_ids"]))
        return str(source_ids[0]) if source_ids else None

    @staticmethod
    def _capture_note(value: str) -> str:
        # The schema bounds the note at <= 2000 rather than 1..2000, so an
        # empty note is a legal submission and _required_text's non-empty
        # rule cannot stand in for this check. Like the payload, the note is
        # stored exactly as submitted.
        if not isinstance(value, str) or len(value) > _CAPTURE_NOTE_MAX:
            raise ValueError("note is invalid")
        return value

    def get_adoption_manifest(self, manifest_id: str) -> AdoptionManifestRecord:
        with self._connect() as conn:
            return self._adoption_manifest(conn, manifest_id)

    def _adopt_source(
        self, conn: sqlite3.Connection, entry: AdoptionEntry, *, now: str
    ) -> tuple[str, bool]:
        """Return the source id and whether it was already adopted."""

        existing = conn.execute(
            "SELECT id, engine_ref FROM sources WHERE canonical_id = ?",
            (entry.canonical_id,),
        ).fetchone()
        if existing is not None:
            # A delta manifest re-lists what is already adopted. That is
            # expected -- but only if it still names the same directory;
            # repointing a live source at a different corpus directory would
            # silently break every reference the engine already holds.
            if str(existing["engine_ref"]) != entry.engine_ref:
                raise InvalidTransition("engine_ref_conflict", "adoption")
            return str(existing["id"]), True
        # The mirror case: this directory is already bound to a DIFFERENT
        # canonical source, e.g. one first adopted by content digest and later
        # identified as an arXiv paper. sources.engine_ref is UNIQUE, so
        # without this the insert below fails with a raw IntegrityError --
        # exactly the opaque commit-time failure this design exists to avoid.
        if conn.execute(
            "SELECT 1 FROM sources WHERE engine_ref = ?", (entry.engine_ref,)
        ).fetchone() is not None:
            raise InvalidTransition("paper_dir_conflict", "adoption")
        source_id = self._insert_source(
            conn,
            authority=entry.authority,
            authority_id=entry.authority_id,
            source_kind="paper",
            official_title=entry.official_title,
            engine_ref=entry.engine_ref,
            import_state="existing",
            aliases=(),
            now=now,
        )
        return source_id, False

    def _adoption_manifest(
        self, conn: sqlite3.Connection, manifest_id: str
    ) -> AdoptionManifestRecord:
        row = conn.execute(
            """SELECT manifest_id, corpus_root_id, actor_id, entry_count,
                      adopted_count, committed_at
               FROM adoption_manifests WHERE manifest_id = ?""",
            (manifest_id,),
        ).fetchone()
        if row is None:
            raise NotFound("adoption manifest", manifest_id)
        return AdoptionManifestRecord(
            manifest_id=str(row["manifest_id"]),
            corpus_root_id=str(row["corpus_root_id"]),
            actor_id=str(row["actor_id"]),
            entry_count=int(row["entry_count"]),
            adopted_count=int(row["adopted_count"]),
            committed_at=self._parse_control_time(str(row["committed_at"])),
        )

    @staticmethod
    def _adoption_manifest_response(record: AdoptionManifestRecord) -> JsonObject:
        return {
            "manifest_id": record.manifest_id,
            "corpus_root_id": record.corpus_root_id,
            "actor_id": record.actor_id,
            "entry_count": record.entry_count,
            "adopted_count": record.adopted_count,
            "committed_at": ControlStore._format_time(record.committed_at),
        }

    @classmethod
    def _adoption_manifest_receipt(
        cls, value: Mapping[str, Any]
    ) -> AdoptionManifestRecord:
        expected = {
            "manifest_id",
            "corpus_root_id",
            "actor_id",
            "entry_count",
            "adopted_count",
            "committed_at",
        }
        if set(value) != expected:
            raise RuntimeError("adoption manifest receipt is invalid")
        return AdoptionManifestRecord(
            manifest_id=str(value["manifest_id"]),
            corpus_root_id=str(value["corpus_root_id"]),
            actor_id=str(value["actor_id"]),
            entry_count=int(value["entry_count"]),
            adopted_count=int(value["adopted_count"]),
            committed_at=cls._parse_control_time(str(value["committed_at"])),
        )

    # -- ⟦S3.4/D6⟧ the operator's approval of one exact release ------------
    #
    # Separate from the activation gate above on purpose. The gate answers "may
    # the managed runtime dispatch at all"; this answers "is *this* release, with
    # *these* bytes, one the operator approved". D6 keeps them two decisions, so
    # enabling dispatch can never imply approving whatever happens to be staged,
    # and approving a release can never turn dispatch on.

    def approve_runtime_release(
        self,
        *,
        release_id: str,
        manifest_sha256: str,
        actor_id: str,
        idempotency_key: str,
    ) -> RuntimeReleaseApprovalRecord:
        return self._record_runtime_release_decision(
            decision="approve",
            release_id=release_id,
            manifest_sha256=manifest_sha256,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def revoke_runtime_release(
        self,
        *,
        release_id: str,
        manifest_sha256: str,
        actor_id: str,
        idempotency_key: str,
    ) -> RuntimeReleaseApprovalRecord:
        return self._record_runtime_release_decision(
            decision="revoke",
            release_id=release_id,
            manifest_sha256=manifest_sha256,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def runtime_release_decision(
        self, release_id: str, manifest_sha256: str
    ) -> RuntimeReleaseApprovalRecord | None:
        """The decision in force for this exact release, approve or revoke."""

        release_id, manifest_sha256 = self._validated_release_identity(
            release_id, manifest_sha256
        )
        with self._connect() as conn:
            return self._standing_release_decision(
                conn, release_id, manifest_sha256
            )

    def _standing_release_decision(
        self,
        conn: sqlite3.Connection,
        release_id: str,
        manifest_sha256: str,
    ) -> RuntimeReleaseApprovalRecord | None:
        """The last decision recorded for this identity, on a caller's handle.

        Takes the connection so a writer can ask the same question inside its
        own transaction, rather than opening a second one that cannot see the
        rows it is about to depend on.
        """

        # Insertion order, not `decided_at`, for migration 12's reason: two
        # decisions can land in one clock tick, and the table is append-only
        # and trigger-guarded, so its rowid IS the order they were made in.
        row = conn.execute(
            """SELECT id, decision, release_id, manifest_sha256, actor_id,
                      decided_at
               FROM runtime_release_approvals
               WHERE release_id = ? AND manifest_sha256 = ?
               ORDER BY rowid DESC LIMIT 1""",
            (release_id, manifest_sha256),
        ).fetchone()
        if row is None:
            return None
        return self._runtime_release_row(row)

    def runtime_release_approved(self, release_id: str, manifest_sha256: str) -> bool:
        record = self.runtime_release_decision(release_id, manifest_sha256)
        return record is not None and record.decision == "approve"

    def runtime_release_approval_report(
        self, release_id: str, manifest_sha256: str
    ) -> JsonObject:
        record = self.runtime_release_decision(release_id, manifest_sha256)
        return {
            "release_id": release_id,
            "manifest_sha256": manifest_sha256,
            "approved": record is not None and record.decision == "approve",
            "decision": None if record is None else record.decision,
            "decided_at": (
                None if record is None else self._format_time(record.decided_at)
            ),
            "actor_id": None if record is None else record.actor_id,
        }

    @staticmethod
    def _validated_release_identity(
        release_id: object, manifest_sha256: object
    ) -> tuple[str, str]:
        if not isinstance(release_id, str) or not 1 <= len(release_id) <= 200:
            raise ValueError("release_id must be 1-200 characters")
        if not isinstance(manifest_sha256, str) or not _HEX_DIGEST_RE.fullmatch(
            manifest_sha256
        ):
            raise ValueError("manifest_sha256 must be a lowercase sha256 digest")
        return release_id, manifest_sha256

    def _record_runtime_release_decision(
        self,
        *,
        decision: str,
        release_id: str,
        manifest_sha256: str,
        actor_id: str,
        idempotency_key: str,
    ) -> RuntimeReleaseApprovalRecord:
        release_id, manifest_sha256 = self._validated_release_identity(
            release_id, manifest_sha256
        )
        request = {
            "decision": decision,
            "release_id": release_id,
            "manifest_sha256": manifest_sha256,
        }
        operation = f"INTERNAL:runtime-release/{decision}"
        with self._transaction() as conn:
            # The receipt is still consulted, and still refuses one key reused
            # for a different request, but it no longer gets to settle this on
            # its own. Callers key a decision by what they asked for, not by
            # when they asked — `runtime_update.cli._decision_key` is a pure
            # function of (command, release_id, manifest_sha256) — so the key an
            # honest retry carries is byte-identical to the key the operator's
            # next reversal carries. Only the decision in force tells them
            # apart, in either direction.
            replay = self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )
            standing = self._standing_release_decision(
                conn, release_id, manifest_sha256
            )
            if standing is not None and standing.decision == decision:
                # What was asked for is already what is in force: a retry. Fold
                # it — no row, no audit event — and answer with the decision
                # that is standing.
                if replay is not None:
                    return self._runtime_release_receipt(replay.value)
                self._save_receipt(
                    conn,
                    actor_id,
                    operation,
                    idempotency_key,
                    request,
                    self._runtime_release_response(standing),
                    200,
                )
                return standing
            approval_id = self._id_factory("runtime-release-approval")
            now = self._now()
            conn.execute(
                """INSERT INTO runtime_release_approvals
                   (id, decision, release_id, manifest_sha256, actor_id, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (approval_id, decision, release_id, manifest_sha256, actor_id, now),
            )
            self._audit(
                conn,
                "runtime_release_approval",
                approval_id,
                f"runtime_release.{decision}d",
                {"release_id": release_id, "manifest_sha256": manifest_sha256},
            )
            row = conn.execute(
                """SELECT id, decision, release_id, manifest_sha256, actor_id,
                          decided_at
                   FROM runtime_release_approvals WHERE id = ?""",
                (approval_id,),
            ).fetchone()
            record = self._runtime_release_row(row)
            if replay is not None:
                # This decision reversed the one the receipt described, so the
                # receipt is stale rather than authoritative. Re-point it at
                # what is now in force: the next honest retry then replays this
                # decision, and the primary key does not collide.
                conn.execute(
                    """DELETE FROM idempotency_receipts
                       WHERE actor_id = ? AND operation = ?
                         AND idempotency_key = ?""",
                    (actor_id, operation, idempotency_key),
                )
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._runtime_release_response(record),
                201,
            )
            return record

    def _runtime_release_row(self, row: sqlite3.Row) -> RuntimeReleaseApprovalRecord:
        return RuntimeReleaseApprovalRecord(
            id=str(row["id"]),
            decision=str(row["decision"]),
            release_id=str(row["release_id"]),
            manifest_sha256=str(row["manifest_sha256"]),
            actor_id=str(row["actor_id"]),
            decided_at=self._parse_control_time(str(row["decided_at"])),
        )

    @staticmethod
    def _runtime_release_response(
        record: RuntimeReleaseApprovalRecord,
    ) -> JsonObject:
        return {
            "id": record.id,
            "decision": record.decision,
            "release_id": record.release_id,
            "manifest_sha256": record.manifest_sha256,
            "actor_id": record.actor_id,
            "decided_at": ControlStore._format_time(record.decided_at),
        }

    @classmethod
    def _runtime_release_receipt(
        cls, value: Mapping[str, Any]
    ) -> RuntimeReleaseApprovalRecord:
        expected = {
            "id",
            "decision",
            "release_id",
            "manifest_sha256",
            "actor_id",
            "decided_at",
        }
        if set(value) != expected:
            raise RuntimeError("runtime release approval receipt is invalid")
        return RuntimeReleaseApprovalRecord(
            id=str(value["id"]),
            decision=str(value["decision"]),
            release_id=str(value["release_id"]),
            manifest_sha256=str(value["manifest_sha256"]),
            actor_id=str(value["actor_id"]),
            decided_at=cls._parse_control_time(str(value["decided_at"])),
        )

    def enable_runtime_activation(
        self,
        *,
        mode: str,
        window_seconds: int | None = None,
        actor_id: str,
        idempotency_key: str,
    ) -> RuntimeActivationRecord:
        """Authorize managed runtime dispatch, permanently or for a window.

        The health payload's `runtime_dispatch` flag is a report with no
        behavioral consumer, so it has never gated anything. This is the fact
        orchestration actually consults, and it is durable on purpose: a
        bounded window has to survive a restart that outlasts it, which an
        in-process timer would not.
        """

        if mode not in {"permanent", "window"}:
            raise ValueError("mode must be 'permanent' or 'window'")
        if mode == "window":
            if type(window_seconds) is not int or not 1 <= window_seconds <= 604_800:
                raise ValueError(
                    "window_seconds must be between 1 and 604800 for a window"
                )
        elif window_seconds is not None:
            raise ValueError("window_seconds is only valid for a window")
        request = {"mode": mode, "window_seconds": window_seconds}
        operation = "INTERNAL:runtime-activation/enable"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )
            if replay is not None:
                return self._runtime_activation_receipt(replay.value)
            now = self._now()
            expires_at = None
            if mode == "window":
                expires_at = self._format_time(
                    self._parse_control_time(now)
                    + timedelta(seconds=int(window_seconds))
                )
            record = self._insert_runtime_activation(
                conn,
                decision="enable",
                mode=mode,
                expires_at=expires_at,
                actor_id=actor_id,
                now=now,
            )
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._runtime_activation_response(record),
                201,
            )
            return record

    def disable_runtime_activation(
        self, *, actor_id: str, idempotency_key: str
    ) -> RuntimeActivationRecord:
        request: JsonObject = {"decision": "disable"}
        operation = "INTERNAL:runtime-activation/disable"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )
            if replay is not None:
                return self._runtime_activation_receipt(replay.value)
            record = self._insert_runtime_activation(
                conn,
                decision="disable",
                mode=None,
                expires_at=None,
                actor_id=actor_id,
                now=self._now(),
            )
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._runtime_activation_response(record),
                201,
            )
            return record

    def runtime_activation(self) -> RuntimeActivationRecord | None:
        """Return the decision in force, or None when dispatch is not enabled."""

        with self._connect() as conn:
            # Insertion order, not `decided_at`: two decisions can land in one
            # clock tick -- routinely so under a fixed test clock, and possible
            # under a real one -- and then a timestamp cannot say which came
            # last. The table is append-only and guarded against mutation, so
            # its rowid IS the order the operator made the decisions in.
            row = conn.execute(
                """SELECT id, decision, mode, expires_at, actor_id, decided_at
                   FROM runtime_activation_decisions
                   ORDER BY rowid DESC LIMIT 1"""
            ).fetchone()
        if row is None or str(row["decision"]) != "enable":
            return None
        record = self._runtime_activation_row(row)
        if record.expires_at is not None and record.expires_at <= self._clock():
            return None
        return record

    def runtime_dispatch_enabled(self) -> bool:
        return self.runtime_activation() is not None

    def runtime_activation_report(self) -> JsonObject:
        """The read-only projection health and doctor report, never a literal."""

        record = self.runtime_activation()
        return {
            "enabled": record is not None,
            "mode": None if record is None else record.mode,
            "expires_at": (
                None
                if record is None or record.expires_at is None
                else self._format_time(record.expires_at)
            ),
        }

    def _insert_runtime_activation(
        self,
        conn: sqlite3.Connection,
        *,
        decision: str,
        mode: str | None,
        expires_at: str | None,
        actor_id: str,
        now: str,
    ) -> RuntimeActivationRecord:
        activation_id = self._id_factory("runtime-activation")
        conn.execute(
            """INSERT INTO runtime_activation_decisions
               (id, decision, mode, expires_at, actor_id, decided_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (activation_id, decision, mode, expires_at, actor_id, now),
        )
        self._audit(
            conn,
            "runtime_activation",
            activation_id,
            f"runtime_activation.{decision}d",
            {"mode": mode, "expires_at": expires_at},
        )
        row = conn.execute(
            """SELECT id, decision, mode, expires_at, actor_id, decided_at
               FROM runtime_activation_decisions WHERE id = ?""",
            (activation_id,),
        ).fetchone()
        return self._runtime_activation_row(row)

    def _runtime_activation_row(self, row: sqlite3.Row) -> RuntimeActivationRecord:
        expires_at = row["expires_at"]
        return RuntimeActivationRecord(
            id=str(row["id"]),
            decision=str(row["decision"]),
            mode=None if row["mode"] is None else str(row["mode"]),
            expires_at=(
                None if expires_at is None else self._parse_control_time(str(expires_at))
            ),
            actor_id=str(row["actor_id"]),
            decided_at=self._parse_control_time(str(row["decided_at"])),
        )

    @staticmethod
    def _runtime_activation_response(record: RuntimeActivationRecord) -> JsonObject:
        return {
            "id": record.id,
            "decision": record.decision,
            "mode": record.mode,
            "expires_at": (
                None
                if record.expires_at is None
                else ControlStore._format_time(record.expires_at)
            ),
            "actor_id": record.actor_id,
            "decided_at": ControlStore._format_time(record.decided_at),
        }

    @classmethod
    def _runtime_activation_receipt(
        cls, value: Mapping[str, Any]
    ) -> RuntimeActivationRecord:
        expected = {"id", "decision", "mode", "expires_at", "actor_id", "decided_at"}
        if set(value) != expected:
            raise RuntimeError("runtime activation receipt is invalid")
        expires_at = value["expires_at"]
        return RuntimeActivationRecord(
            id=str(value["id"]),
            decision=str(value["decision"]),
            mode=None if value["mode"] is None else str(value["mode"]),
            expires_at=(
                None if expires_at is None else cls._parse_control_time(str(expires_at))
            ),
            actor_id=str(value["actor_id"]),
            decided_at=cls._parse_control_time(str(value["decided_at"])),
        )

    def enable_transport_activation(
        self,
        *,
        transport: str,
        scope: str,
        window_seconds: int | None = None,
        actor_id: str,
        idempotency_key: str,
    ) -> TransportActivationRecord:
        """Authorize one transport to send, permanently or for a window.

        The mirror of `enable_runtime_activation`, with one difference that is
        not cosmetic: the window bound here is 30 minutes, not a week. This
        gate is opened while a token the legacy research gateway also uses is
        deliberately held by the product, and the whole procedure around it is
        written for an operator who is present. A window nobody is watching is
        the state it exists to prevent.
        """

        transport = self._activation_transport(transport)
        if scope not in {"permanent", "window"}:
            raise ValueError("scope must be 'permanent' or 'window'")
        if scope == "window":
            if (
                type(window_seconds) is not int
                or not 1 <= window_seconds <= _TRANSPORT_WINDOW_MAX_SECONDS
            ):
                raise ValueError(
                    "window_seconds must be between 1 and "
                    f"{_TRANSPORT_WINDOW_MAX_SECONDS} for a window"
                )
        elif window_seconds is not None:
            raise ValueError("window_seconds is only valid for a window")
        request = {
            "transport": transport,
            "scope": scope,
            "window_seconds": window_seconds,
        }
        operation = "INTERNAL:transport-activation/enable"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay is not None:
                return self._transport_activation_receipt(replay.value)
            now = self._now()
            expires_at = None
            if scope == "window":
                expires_at = self._format_time(
                    self._parse_control_time(now)
                    + timedelta(seconds=int(window_seconds))
                )
            record = self._insert_transport_activation(
                conn,
                transport=transport,
                decision="enable",
                scope=scope,
                expires_at=expires_at,
                actor_id=actor_id,
                now=now,
            )
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._transport_activation_response(record),
                201,
            )
            return record

    def disable_transport_activation(
        self, *, transport: str, actor_id: str, idempotency_key: str
    ) -> TransportActivationRecord:
        """End every authorization for a transport, window or permanent."""

        transport = self._activation_transport(transport)
        request: JsonObject = {"transport": transport, "decision": "disable"}
        operation = "INTERNAL:transport-activation/disable"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay is not None:
                return self._transport_activation_receipt(replay.value)
            record = self._insert_transport_activation(
                conn,
                transport=transport,
                decision="disable",
                scope=None,
                expires_at=None,
                actor_id=actor_id,
                now=self._now(),
            )
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._transport_activation_response(record),
                201,
            )
            return record

    def transport_activation(
        self, transport: str
    ) -> TransportActivationRecord | None:
        """Return the decision in force for a transport, or None."""

        transport = self._activation_transport(transport)
        with self._connect() as conn:
            record = self._transport_activation_in_force(conn, transport)
        return record

    def transport_activation_decision(
        self, decision_id: str
    ) -> TransportActivationRecord | None:
        """One activation decision by id, in force or long superseded.

        ⟦P54A-4⟧ `close-window` has to know three things before it changes any
        process state: that the id names a decision at all, that the decision
        is a window rather than a `disable`, and whether ANY window is still in
        force. `record_transport_window_closed` checks the first two, but it is
        the last thing the supervisor does -- so a close naming a superseded id
        stopped the LIVE window's poller and killed its worker on the way to a
        refusal. This is the read that lets the refusal come first.
        """

        decision_id = self._required_text(decision_id, "decision_id", maximum=200)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id, transport, decision, scope, expires_at, actor_id,
                          decided_at
                   FROM transport_activation_decisions WHERE id = ?""",
                (decision_id,),
            ).fetchone()
        return None if row is None else self._transport_activation_row(row)

    def telegram_dispatch_enabled(self) -> bool:
        """The one predicate the Telegram adapter is required to consult.

        Named for the transport rather than parameterized because it is a
        fence, and a fence whose subject is a variable is one refactor away
        from being consulted for the wrong thing.
        """

        return self.transport_activation("telegram") is not None

    def record_transport_window_closed(
        self,
        *,
        window_id: str,
        poller_stopped: bool,
        proof: str,
        actor_id: str,
    ) -> None:
        """Record that a window ended with its release proven.

        `poller_stopped` is derived at the call site -- the supervisor's
        `close()` returned and the process is gone, or no socket to the
        provider remains -- so `proof` is mandatory and records which of
        those the caller actually observed. A window is closed by decision
        before it is recorded closed, never by expiry, so this refuses while
        the window still authorizes a send.
        """

        window_id = self._required_text(window_id, "window_id", maximum=200)
        if type(poller_stopped) is not bool:
            raise ValueError("poller_stopped must be a boolean")
        proof = self._required_text(proof, "proof", maximum=200)
        actor_id = self._required_text(actor_id, "actor_id", maximum=200)
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT id, transport, decision, scope, expires_at, actor_id,
                          decided_at
                   FROM transport_activation_decisions WHERE id = ?""",
                (window_id,),
            ).fetchone()
            if row is None:
                raise NotFound("transport activation decision", window_id)
            record = self._transport_activation_row(row)
            if record.scope != "window":
                raise InvalidTransition(str(record.scope), "closed")
            in_force = self._transport_activation_in_force(conn, record.transport)
            if in_force is not None and in_force.id == window_id:
                raise InvalidTransition("open", "closed")
            self._audit(
                conn,
                TRANSPORT_WINDOW_AGGREGATE,
                window_id,
                TRANSPORT_WINDOW_CLOSED_EVENT,
                {
                    "transport": record.transport,
                    "poller_stopped": poller_stopped,
                    "proof": proof,
                    "actor_id": actor_id,
                },
            )

    def abort_open_transport_windows(
        self, *, reason: str, actor_id: str
    ) -> tuple[str, ...]:
        """Account for every window this process cannot prove was released.

        A product that crashed or was restarted mid-window left no
        `transport_window_closed` row, so the next start has to say so rather
        than let the window disappear into an expiry nobody recorded. When
        such a window is still in force it is also disabled by decision: a
        gate that survives a restart the product cannot account for is open
        with nobody watching, which is the state the window bound exists to
        prevent.
        """

        reason = self._required_text(reason, "reason", maximum=200)
        actor_id = self._required_text(actor_id, "actor_id", maximum=200)
        aborted: list[str] = []
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT id, transport, decision, scope, expires_at, actor_id,
                          decided_at
                   FROM transport_activation_decisions
                   WHERE scope = 'window' AND decision = 'enable'
                     AND id NOT IN (
                         SELECT aggregate_id FROM control_audit
                         WHERE aggregate_type = ?
                     )
                   ORDER BY rowid""",
                (TRANSPORT_WINDOW_AGGREGATE,),
            ).fetchall()
            for row in rows:
                record = self._transport_activation_row(row)
                in_force = self._transport_activation_in_force(
                    conn, record.transport
                )
                if in_force is not None and in_force.id == record.id:
                    self._insert_transport_activation(
                        conn,
                        transport=record.transport,
                        decision="disable",
                        scope=None,
                        expires_at=None,
                        actor_id=actor_id,
                        now=self._now(),
                    )
                self._audit(
                    conn,
                    TRANSPORT_WINDOW_AGGREGATE,
                    record.id,
                    TRANSPORT_WINDOW_ABORTED_EVENT,
                    {
                        "transport": record.transport,
                        "reason": reason,
                        "actor_id": actor_id,
                    },
                )
                aborted.append(record.id)
        return tuple(aborted)

    @staticmethod
    def _activation_transport(value: object) -> str:
        if value not in _ACTIVATION_TRANSPORTS:
            raise ValueError("transport is invalid")
        return str(value)

    def _transport_activation_in_force(
        self, conn: sqlite3.Connection, transport: str
    ) -> TransportActivationRecord | None:
        # Insertion order, not `decided_at`, for migration 12's reason: two
        # decisions can land in one clock tick and then a timestamp cannot say
        # which came last. The table is append-only and guarded against
        # mutation, so its rowid IS the order the operator decided in.
        row = conn.execute(
            """SELECT id, transport, decision, scope, expires_at, actor_id,
                      decided_at
               FROM transport_activation_decisions
               WHERE transport = ?
               ORDER BY rowid DESC LIMIT 1""",
            (transport,),
        ).fetchone()
        if row is None or str(row["decision"]) != "enable":
            return None
        record = self._transport_activation_row(row)
        # A window ends at its stored expiry and cannot be extended: the only
        # way past it is another decision, which becomes the latest row.
        if record.expires_at is not None and record.expires_at <= self._clock():
            return None
        return record

    def _insert_transport_activation(
        self,
        conn: sqlite3.Connection,
        *,
        transport: str,
        decision: str,
        scope: str | None,
        expires_at: str | None,
        actor_id: str,
        now: str,
    ) -> TransportActivationRecord:
        activation_id = self._id_factory("transport-activation")
        conn.execute(
            """INSERT INTO transport_activation_decisions
               (id, transport, decision, scope, expires_at, actor_id, decided_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                activation_id,
                transport,
                decision,
                scope,
                expires_at,
                actor_id,
                now,
            ),
        )
        self._audit(
            conn,
            "transport_activation",
            activation_id,
            f"transport_activation.{decision}d",
            {"transport": transport, "scope": scope, "expires_at": expires_at},
        )
        row = conn.execute(
            """SELECT id, transport, decision, scope, expires_at, actor_id,
                      decided_at
               FROM transport_activation_decisions WHERE id = ?""",
            (activation_id,),
        ).fetchone()
        return self._transport_activation_row(row)

    def _transport_activation_row(
        self, row: sqlite3.Row
    ) -> TransportActivationRecord:
        expires_at = row["expires_at"]
        return TransportActivationRecord(
            id=str(row["id"]),
            transport=str(row["transport"]),
            decision=str(row["decision"]),
            scope=None if row["scope"] is None else str(row["scope"]),
            expires_at=(
                None
                if expires_at is None
                else self._parse_control_time(str(expires_at))
            ),
            actor_id=str(row["actor_id"]),
            decided_at=self._parse_control_time(str(row["decided_at"])),
        )

    @staticmethod
    def _transport_activation_response(
        record: TransportActivationRecord,
    ) -> JsonObject:
        return {
            "id": record.id,
            "transport": record.transport,
            "decision": record.decision,
            "scope": record.scope,
            "expires_at": (
                None
                if record.expires_at is None
                else ControlStore._format_time(record.expires_at)
            ),
            "actor_id": record.actor_id,
            "decided_at": ControlStore._format_time(record.decided_at),
        }

    @classmethod
    def _transport_activation_receipt(
        cls, value: Mapping[str, Any]
    ) -> TransportActivationRecord:
        expected = {
            "id",
            "transport",
            "decision",
            "scope",
            "expires_at",
            "actor_id",
            "decided_at",
        }
        if set(value) != expected:
            raise RuntimeError("transport activation receipt is invalid")
        expires_at = value["expires_at"]
        return TransportActivationRecord(
            id=str(value["id"]),
            transport=str(value["transport"]),
            decision=str(value["decision"]),
            scope=None if value["scope"] is None else str(value["scope"]),
            expires_at=(
                None if expires_at is None else cls._parse_control_time(str(expires_at))
            ),
            actor_id=str(value["actor_id"]),
            decided_at=cls._parse_control_time(str(value["decided_at"])),
        )

    def get_asset_root(self, root_id: str) -> AssetRootRecord:
        root_id = self._asset_root_id(root_id)
        with self._connect() as conn:
            return self._asset_root(conn, root_id)

    def list_asset_roots(self) -> tuple[AssetRootRecord, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT root_id FROM asset_roots ORDER BY root_id"
            ).fetchall()
            return tuple(self._asset_root(conn, str(row["root_id"])) for row in rows)

    # -- P4.3: the smallest honest research schedule -----------------------

    def register_research_schedule(
        self,
        *,
        job_key: str,
        operation: str,
        enabled: bool,
        interval_seconds: int,
        cadence_source: str,
        legacy_schedule: str | None,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Record one schedule row, or replay the one already recorded.

        Registration is the inventory: the twelve legacy jobs exist here so the
        migration is auditable, and they exist `enabled = 0` so an unknown
        cadence can never fire.
        """

        job_key = self._required_text(job_key, "job_key", maximum=100)
        if operation not in _RESEARCH_SCHEDULE_OPERATIONS:
            raise ValueError("research schedule operation is unsupported")
        if cadence_source not in _RESEARCH_CADENCE_SOURCES:
            raise ValueError("research schedule cadence source is unsupported")
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if type(interval_seconds) is not int or not 60 <= interval_seconds <= 604_800:
            raise ValueError("interval_seconds must be between 60 and 604800")
        if legacy_schedule is not None:
            legacy_schedule = self._required_text(
                legacy_schedule, "legacy_schedule", maximum=200
            )
        if enabled and operation == "legacy":
            # A job with no product implementation may be represented; it may
            # never be armed.
            raise ValueError("a legacy schedule cannot be enabled")
        request = {
            "operation": operation,
            "enabled": enabled,
            "interval_seconds": interval_seconds,
            "cadence_source": cadence_source,
            "legacy_schedule": legacy_schedule,
        }
        operation_name = f"INTERNAL:research-schedules/{job_key}"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation_name, idempotency_key, request
            )
            if replay:
                return replay
            now = self._now()
            conn.execute(
                """INSERT INTO research_schedules(
                       job_key, operation, enabled, interval_seconds,
                       next_due_at, cadence_source, legacy_schedule,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_key,
                    operation,
                    int(enabled),
                    interval_seconds,
                    now,
                    cadence_source,
                    legacy_schedule,
                    now,
                    now,
                ),
            )
            self._audit(
                conn,
                "research_schedule",
                job_key,
                "research.schedule.registered",
                {"operation": operation, "enabled": enabled},
            )
            value = self._research_schedule(conn, job_key)
            return self._save_receipt(
                conn, actor_id, operation_name, idempotency_key, request, value, 201
            )

    def set_research_schedule_enabled(
        self,
        *,
        job_key: str,
        enabled: bool,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        request = {"enabled": enabled, "expected_revision": expected_revision}
        operation_name = f"INTERNAL:research-schedules/{job_key}/enabled"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation_name, idempotency_key, request
            )
            if replay:
                return replay
            schedule = self._research_schedule(conn, job_key)
            self._expect_revision(schedule, expected_revision)
            if enabled and str(schedule["operation"]) == "legacy":
                raise InvalidTransition("legacy_schedule", "enabled")
            now = self._now()
            cursor = conn.execute(
                """UPDATE research_schedules
                   SET enabled = ?, revision = revision + 1, updated_at = ?
                   WHERE job_key = ? AND revision = ?""",
                (int(enabled), now, job_key, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(self._research_schedule(conn, job_key))
            self._audit(
                conn,
                "research_schedule",
                job_key,
                "research.schedule.enabled" if enabled else "research.schedule.disabled",
                {},
            )
            value = self._research_schedule(conn, job_key)
            return self._save_receipt(
                conn, actor_id, operation_name, idempotency_key, request, value, 200
            )

    def get_research_schedule(self, job_key: str) -> JsonObject:
        with self._connect() as conn:
            return self._research_schedule(conn, job_key)

    def list_research_schedules(self) -> list[JsonObject]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_key FROM research_schedules ORDER BY job_key"
            ).fetchall()
            return [self._research_schedule(conn, str(row["job_key"])) for row in rows]

    def list_due_research_schedules(self, *, limit: int = 20) -> list[JsonObject]:
        """Enabled rows whose next-due timestamp has arrived, oldest first."""

        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT job_key FROM research_schedules
                   WHERE enabled = 1 AND next_due_at <= ?
                   ORDER BY next_due_at, job_key LIMIT ?""",
                (self._now(), limit),
            ).fetchall()
            return [self._research_schedule(conn, str(row["job_key"])) for row in rows]

    def record_research_schedule_outcome(
        self,
        *,
        job_key: str,
        expected_revision: int,
        outcome: str,
        started_at: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Close one tick for one job and re-arm it from its own interval."""

        if outcome not in _RESEARCH_SCHEDULE_OUTCOMES:
            raise ValueError("research schedule outcome is unsupported")
        request = {
            "outcome": outcome,
            "started_at": started_at,
            "expected_revision": expected_revision,
        }
        operation_name = f"INTERNAL:research-schedules/{job_key}/outcome"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation_name, idempotency_key, request
            )
            if replay:
                return replay
            schedule = self._research_schedule(conn, job_key)
            self._expect_revision(schedule, expected_revision)
            now_value = self._utc_now()
            now = self._format_time(now_value)
            next_due = self._format_time(
                now_value + timedelta(seconds=int(schedule["interval_seconds"]))
            )
            cursor = conn.execute(
                """UPDATE research_schedules
                   SET next_due_at = ?, last_started_at = ?, last_finished_at = ?,
                       last_outcome = ?, revision = revision + 1, updated_at = ?
                   WHERE job_key = ? AND revision = ?""",
                (next_due, started_at, now, outcome, now, job_key, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(self._research_schedule(conn, job_key))
            value = self._research_schedule(conn, job_key)
            return self._save_receipt(
                conn, actor_id, operation_name, idempotency_key, request, value, 200
            )

    def _research_schedule(
        self, conn: sqlite3.Connection, job_key: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM research_schedules WHERE job_key = ?", (job_key,)
        ).fetchone()
        if row is None:
            raise NotFound("research schedule", job_key)
        value = self._row(row)
        value["enabled"] = bool(value["enabled"])
        return value

    def register_connector(
        self,
        *,
        connector_id: str,
        kind: str,
        adapter_id: str,
        display_name: str,
        credential_alias: str | None,
        enabled: bool,
        actor_id: str,
        idempotency_key: str,
    ) -> ConnectorRecord:
        connector_id, adapter_id, display_name, credential_alias, enabled = (
            self._validated_connector(
                connector_id=connector_id,
                adapter_id=adapter_id,
                display_name=display_name,
                credential_alias=credential_alias,
                enabled=enabled,
            )
        )
        kind = self._connector_machine_id(kind, "kind")
        request = {
            "connector_id": connector_id,
            "kind": kind,
            "adapter_id": adapter_id,
            "display_name": display_name,
            "credential_alias": credential_alias,
            "enabled": enabled,
        }
        operation = "INTERNAL:connectors/register"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )
            if replay is not None:
                return self._connector_receipt(replay.value)
            if conn.execute(
                "SELECT 1 FROM connectors WHERE id = ?", (connector_id,)
            ).fetchone() is not None:
                raise InvalidTransition("already_registered", "registration")
            now = self._now()
            conn.execute(
                """INSERT INTO connectors
                   (id, kind, adapter_id, display_name, credential_alias,
                    enabled, revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (
                    connector_id,
                    kind,
                    adapter_id,
                    display_name,
                    credential_alias,
                    int(enabled),
                    now,
                    now,
                ),
            )
            record = self._connector(conn, connector_id)
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._connector_response(record),
                201,
            )
            return record

    def update_connector(
        self,
        *,
        connector_id: str,
        adapter_id: str,
        display_name: str,
        credential_alias: str | None,
        enabled: bool,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> ConnectorRecord:
        connector_id, adapter_id, display_name, credential_alias, enabled = (
            self._validated_connector(
                connector_id=connector_id,
                adapter_id=adapter_id,
                display_name=display_name,
                credential_alias=credential_alias,
                enabled=enabled,
            )
        )
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        request = {
            "connector_id": connector_id,
            "adapter_id": adapter_id,
            "display_name": display_name,
            "credential_alias": credential_alias,
            "enabled": enabled,
            "expected_revision": expected_revision,
        }
        operation = f"INTERNAL:connectors/{connector_id}/update"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )
            if replay is not None:
                return self._connector_receipt(replay.value)
            now = self._now()
            cursor = conn.execute(
                """UPDATE connectors
                   SET adapter_id = ?, display_name = ?, credential_alias = ?,
                       enabled = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (
                    adapter_id,
                    display_name,
                    credential_alias,
                    int(enabled),
                    now,
                    connector_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                current = self._connector(conn, connector_id)
                raise RevisionConflict(
                    {"id": current.id, "revision": current.revision}
                )
            record = self._connector(conn, connector_id)
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._connector_response(record),
                200,
            )
            return record

    def get_connector(self, connector_id: str) -> ConnectorRecord:
        connector_id = self._connector_machine_id(connector_id, "connector_id")
        with self._connect() as conn:
            return self._connector(conn, connector_id)

    def list_connectors(self, *, kind: str | None = None) -> tuple[ConnectorRecord, ...]:
        if kind is not None:
            kind = self._connector_machine_id(kind, "kind")
        with self._connect() as conn:
            if kind is None:
                rows = conn.execute("SELECT id FROM connectors ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM connectors WHERE kind = ? ORDER BY id", (kind,)
                ).fetchall()
            return tuple(self._connector(conn, str(row["id"])) for row in rows)

    def current_control_store_identity(self) -> ControlStoreIdentity:
        with self._connect() as conn:
            return self._control_store_identity(conn)

    def record_paired_backup_proof(
        self,
        *,
        proof: PairedBackupProofInput,
        actor_id: str,
        idempotency_key: str,
    ) -> PairedBackupProofRecord:
        validated = self._validated_paired_backup_proof(proof)
        request = self._paired_backup_proof_request(validated)
        operation = "INTERNAL:backup-proofs/record"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )
            if replay is not None:
                return self._paired_backup_proof_receipt(replay.value)
            existing = conn.execute(
                "SELECT 1 FROM paired_backup_proofs WHERE id = ?", (validated.id,)
            ).fetchone()
            if existing is not None:
                record = self._paired_backup_proof(conn, validated.id)
                if self._paired_backup_proof_record_request(record) != request:
                    raise IdempotencyConflict()
                self._save_receipt(
                    conn,
                    actor_id,
                    operation,
                    idempotency_key,
                    request,
                    self._paired_backup_proof_response(record),
                    200,
                )
                return record
            self._validate_protected_set_coverage(
                conn, validated.protected_set_manifest
            )
            conn.execute(
                """INSERT INTO paired_backup_proofs
                   (id, backup_set_digest, protected_set_manifest_json,
                    primary_completed_at, primary_snapshot_count,
                    independent_completed_at, independent_snapshot_count,
                    restore_completed_at, restored_database_count,
                    verified_sample_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    validated.id,
                    validated.backup_set_digest,
                    validated.manifest_json,
                    self._format_registry_time(validated.primary_completed_at),
                    validated.primary_snapshot_count,
                    self._format_registry_time(validated.independent_completed_at),
                    validated.independent_snapshot_count,
                    self._format_registry_time(validated.restore_completed_at),
                    validated.restored_database_count,
                    validated.verified_sample_count,
                    self._now(),
                ),
            )
            record = self._paired_backup_proof(conn, validated.id)
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._paired_backup_proof_response(record),
                201,
            )
            return record

    def get_paired_backup_proof(self, proof_id: str) -> PairedBackupProofRecord:
        proof_id = self._registry_resource_id(proof_id, "proof_id")
        with self._connect() as conn:
            return self._paired_backup_proof(conn, proof_id)

    def list_paired_backup_proofs(
        self, *, limit: int = 100
    ) -> tuple[PairedBackupProofRecord, ...]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id FROM paired_backup_proofs
                   ORDER BY restore_completed_at DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return tuple(
                self._paired_backup_proof(conn, str(row["id"])) for row in rows
            )

    def record_system_health_observation(
        self,
        *,
        observation: SystemHealthObservationInput,
        actor_id: str,
        idempotency_key: str,
    ) -> SystemHealthObservationRecord:
        (
            observation_id,
            subject,
            status,
            category,
            observed_at,
            metrics,
        ) = self._validated_system_health_observation(observation)
        request = {
            "observation_id": observation_id,
            "subject": {"kind": subject.kind, "id": subject.id},
            "status": status,
            "category": category,
            "observed_at": self._format_registry_time(observed_at),
            "metrics": [[key, value] for key, value in metrics],
        }
        operation = "INTERNAL:system-health/record"
        with self._transaction() as conn:
            replay = self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )
            if replay is not None:
                return self._system_health_receipt(replay.value)
            existing = conn.execute(
                "SELECT 1 FROM system_health_observations WHERE id = ?",
                (observation_id,),
            ).fetchone()
            if existing is not None:
                record = self._system_health_observation(conn, observation_id)
                if self._system_health_request(record) != request:
                    raise IdempotencyConflict()
                self._save_receipt(
                    conn,
                    actor_id,
                    operation,
                    idempotency_key,
                    request,
                    self._system_health_response(record),
                    200,
                )
                return record
            subject_column = self._validate_health_subject_exists(conn, subject)
            subject_values = {
                "asset_root_id": None,
                "connector_id": None,
                "backup_proof_id": None,
            }
            subject_values[subject_column] = subject.id
            conn.execute(
                """INSERT INTO system_health_observations
                   (id, asset_root_id, connector_id, backup_proof_id, status,
                    category, observed_at, metrics_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation_id,
                    subject_values["asset_root_id"],
                    subject_values["connector_id"],
                    subject_values["backup_proof_id"],
                    status,
                    category,
                    self._format_registry_time(observed_at),
                    self._json(dict(metrics)),
                    self._now(),
                ),
            )
            record = self._system_health_observation(conn, observation_id)
            self._save_receipt(
                conn,
                actor_id,
                operation,
                idempotency_key,
                request,
                self._system_health_response(record),
                201,
            )
            return record

    def latest_system_health_observation(
        self, *, subject: HealthSubject
    ) -> SystemHealthObservationRecord | None:
        subject = self._validated_health_subject(subject)
        column, _, _ = self._health_subject_storage(subject)
        with self._connect() as conn:
            self._validate_health_subject_exists(conn, subject)
            row = conn.execute(
                f"""SELECT id FROM system_health_observations
                    WHERE {column} = ?
                    ORDER BY observed_at DESC, id DESC LIMIT 1""",
                (subject.id,),
            ).fetchone()
            return (
                self._system_health_observation(conn, str(row["id"]))
                if row is not None
                else None
            )

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.path,
            timeout=self._timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {max(1, int(self._timeout * 1000))}")
        conn.execute("PRAGMA synchronous = FULL")
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    def create_workspace(
        self,
        *,
        title: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        title = self._required_text(title, "title", maximum=500)
        request = {"title": title}
        operation = "POST:/api/v1/workspaces"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            now = self._now()
            workspace_id = self._id_factory("ws")
            conn.execute(
                """INSERT INTO workspaces
                   (id, title, revision, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?)""",
                (workspace_id, title, now, now),
            )
            self._audit(conn, "workspace", workspace_id, "workspace.created", {"title": title})
            value = self._workspace(conn, workspace_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def install_workflow(
        self,
        *,
        run_id: str,
        definition: WorkflowDefinition,
    ) -> JsonObject:
        """Install and irreversibly seal one exact workflow definition per run."""
        with self._transaction() as conn:
            return self._install_workflow(conn, run_id, definition)

    def _install_workflow(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        definition: WorkflowDefinition,
    ) -> JsonObject:
        """`install_workflow` inside the caller's transaction.

        ⟦P8 V-3⟧ Split out so `create_run(workflow=...)` can create the run
        and install its workflow in ONE transaction: a run that owns a
        workflow from the instant it exists is never, at any instant, a run
        the turn bridge may drive.
        """

        if not isinstance(definition, WorkflowDefinition):
            raise TypeError("definition must be a WorkflowDefinition")
        run = self._run(conn, run_id)
        existing = conn.execute(
            "SELECT id FROM workflow_instances WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing is not None:
            value = self._workflow(conn, str(existing["id"]))
            if self._workflow_definition_signature(value) != self._definition_signature(
                definition
            ):
                raise InvalidTransition("definition_drift", "workflow_replay")
            return value
        self._expect_workflow_run_open(run)

        workflow_id = self._id_factory("workflow")
        now = self._now()
        first_ready = next(
            stage.key for stage in definition.stages if not stage.dependencies
        )
        conn.execute(
            """INSERT INTO workflow_instances(
                   id, run_id, definition_id, definition_version, state,
                   current_stage_key, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)""",
            (
                workflow_id,
                run_id,
                definition.definition_id,
                definition.version,
                first_ready,
                now,
                now,
            ),
        )
        stage_ids: dict[str, str] = {}
        for position, stage in enumerate(definition.stages):
            stage_id = self._id_factory("workflow_stage")
            stage_ids[stage.key] = stage_id
            conn.execute(
                """INSERT INTO workflow_stage_instances(
                       id, workflow_id, stage_key, position, effect,
                       checkpoint_enabled, state
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    stage_id,
                    workflow_id,
                    stage.key,
                    position,
                    stage.effect,
                    int(stage.checkpoint),
                    "ready" if not stage.dependencies else "pending",
                ),
            )
            for requirement_position, effect_kind in enumerate(
                stage.required_receipts
            ):
                conn.execute(
                    """INSERT INTO workflow_stage_receipt_requirements(
                           workflow_id, stage_id, effect_kind, position
                       ) VALUES (?, ?, ?, ?)""",
                    (workflow_id, stage_id, effect_kind, requirement_position),
                )
            for requirement_position, result_kind in enumerate(
                stage.required_results
            ):
                conn.execute(
                    """INSERT INTO workflow_stage_result_requirements(
                           workflow_id, stage_id, result_kind, position
                       ) VALUES (?, ?, ?, ?)""",
                    (workflow_id, stage_id, result_kind, requirement_position),
                )
        for stage in definition.stages:
            for dependency_position, dependency_key in enumerate(stage.dependencies):
                conn.execute(
                    """INSERT INTO workflow_stage_dependencies(
                           workflow_id, stage_id, dependency_stage_id, position
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        workflow_id,
                        stage_ids[stage.key],
                        stage_ids[dependency_key],
                        dependency_position,
                    ),
                )
        conn.execute(
            """UPDATE workflow_instances SET definition_sealed = 1
               WHERE id = ? AND definition_sealed = 0""",
            (workflow_id,),
        )
        return self._workflow(conn, workflow_id)

    def get_workflow(self, workflow_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._workflow(conn, workflow_id)

    def get_workflow_for_run(self, run_id: str) -> JsonObject:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM workflow_instances WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFound("workflow for run", run_id)
            return self._workflow(conn, str(row["id"]))

    def activate_workflow_stage(
        self,
        *,
        workflow_id: str,
        stage_key: str,
        input_value: Mapping[str, Any],
        expected_workflow_revision: int,
        expected_stage_revision: int,
    ) -> JsonObject:
        if not isinstance(input_value, Mapping):
            raise TypeError("input_value must be a mapping")
        input_hash = self._request_hash(input_value)
        with self._transaction() as conn:
            workflow = self._workflow_row(conn, workflow_id)
            self._expect_revision(workflow, expected_workflow_revision)
            self._expect_workflow_run_open(
                self._run(conn, str(workflow["run_id"]))
            )
            stage = self._workflow_stage_row(conn, workflow_id, stage_key)
            self._expect_revision(stage, expected_stage_revision)
            incomplete = conn.execute(
                """SELECT 1
                   FROM workflow_stage_dependencies AS dependency
                   JOIN workflow_stage_instances AS required
                     ON required.id = dependency.dependency_stage_id
                   WHERE dependency.stage_id = ? AND required.state != 'completed'
                   LIMIT 1""",
                (stage["id"],),
            ).fetchone()
            if incomplete is not None or stage["state"] == "pending":
                raise InvalidTransition("dependencies_incomplete", "active")
            if stage["state"] != "ready":
                raise InvalidTransition(str(stage["state"]), "active")
            now = self._now()
            stage_cursor = conn.execute(
                """UPDATE workflow_stage_instances
                   SET state = 'active', input_hash = ?, attempt = attempt + 1,
                       revision = revision + 1, started_at = ?
                   WHERE id = ? AND revision = ? AND state = 'ready'""",
                (input_hash, now, stage["id"], expected_stage_revision),
            )
            if stage_cursor.rowcount != 1:
                raise RevisionConflict(
                    self._workflow_stage_row(conn, workflow_id, stage_key)
                )
            workflow_cursor = conn.execute(
                """UPDATE workflow_instances
                   SET state = 'running', current_stage_key = ?,
                       revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (stage_key, now, workflow_id, expected_workflow_revision),
            )
            if workflow_cursor.rowcount != 1:
                raise RevisionConflict(self._workflow_row(conn, workflow_id))
            return {
                "workflow": self._workflow(conn, workflow_id),
                "stage": self._workflow_stage(conn, str(stage["id"])),
            }

    def create_workflow_decision(
        self,
        *,
        workflow_id: str,
        stage_key: str,
        expected_workflow_revision: int,
        expected_stage_revision: int,
        kind: str,
        prompt: str,
        options: Sequence[Mapping[str, Any]],
    ) -> JsonObject:
        kind = self._required_text(kind, "kind", maximum=100)
        prompt = self._public_decision_text(prompt, "prompt", maximum=20_000)
        normalized_options = self._validate_decision_options(options)
        with self._transaction() as conn:
            workflow = self._workflow_row(conn, workflow_id)
            stage = self._workflow_stage_row(conn, workflow_id, stage_key)
            existing = conn.execute(
                """SELECT workflow_decision_refs.id AS decision_ref_id,
                          decisions.id AS decision_id
                   FROM workflow_decision_refs
                   JOIN decisions
                     ON decisions.id = workflow_decision_refs.decision_id
                   WHERE workflow_decision_refs.workflow_id = ?
                     AND workflow_decision_refs.stage_id = ?""",
                (workflow_id, stage["id"]),
            ).fetchone()
            if existing is not None:
                decision = self._decision(conn, str(existing["decision_id"]))
                if (
                    decision["kind"] != kind
                    or decision["prompt"] != prompt
                    or decision["options"] != normalized_options
                ):
                    raise InvalidTransition(
                        "decision_identity_drift", "decision_replay"
                    )
                return {
                    "decision": decision,
                    "decision_ref": self._workflow_decision_ref(
                        conn, str(existing["decision_ref_id"])
                    ),
                    "workflow": self._workflow(conn, workflow_id),
                    "stage": self._workflow_stage(conn, str(stage["id"])),
                }
            self._expect_revision(workflow, expected_workflow_revision)
            self._expect_workflow_run_open(
                self._run(conn, str(workflow["run_id"]))
            )
            self._expect_revision(stage, expected_stage_revision)
            if stage["effect"] != "decision" or stage["state"] != "active":
                raise InvalidTransition(str(stage["state"]), "waiting")
            run = self._run(conn, str(workflow["run_id"]))
            attempt = self._attempt(conn, str(run["active_attempt_id"]))
            now = self._now()
            decision_id = self._id_factory("decision")
            decision_ref_id = self._id_factory("workflow_decision_ref")
            conn.execute(
                """INSERT INTO decisions(
                       id, run_id, attempt_id, kind, prompt, options_json, state,
                       revision, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (
                    decision_id,
                    run["id"],
                    attempt["id"],
                    kind,
                    prompt,
                    self._json(normalized_options),
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO workflow_decision_refs(
                       id, workflow_id, stage_id, decision_id, expected_revision,
                       state, created_at
                   ) VALUES (?, ?, ?, ?, 0, 'pending', ?)""",
                (decision_ref_id, workflow_id, stage["id"], decision_id, now),
            )
            stage_cursor = conn.execute(
                """UPDATE workflow_stage_instances
                   SET state = 'waiting', revision = revision + 1
                   WHERE id = ? AND revision = ? AND state = 'active'""",
                (stage["id"], expected_stage_revision),
            )
            workflow_cursor = conn.execute(
                """UPDATE workflow_instances
                   SET state = 'waiting', revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, workflow_id, expected_workflow_revision),
            )
            if stage_cursor.rowcount != 1 or workflow_cursor.rowcount != 1:
                raise RevisionConflict(self._workflow_row(conn, workflow_id))
            conn.execute(
                """UPDATE runs SET state = 'waiting_for_decision',
                   revision = revision + 1, updated_at = ? WHERE id = ?""",
                (now, run["id"]),
            )
            conn.execute(
                "UPDATE attempts SET state = 'waiting_for_decision' WHERE id = ?",
                (attempt["id"],),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(attempt["id"]),
                event_type="decision.required",
                payload={
                    "decision_id": decision_id,
                    "kind": kind,
                    "prompt": prompt,
                    "options": normalized_options,
                },
            )
            return {
                "decision": self._decision(conn, decision_id),
                "decision_ref": self._workflow_decision_ref(
                    conn, decision_ref_id
                ),
                "workflow": self._workflow(conn, workflow_id),
                "stage": self._workflow_stage(conn, str(stage["id"])),
            }

    def create_workflow_effect(
        self,
        *,
        workflow_id: str,
        stage_key: str,
        effect_key: str,
        request: SourceBindingRequest
        | SourceImportRequest
        | LineageQueryRequest
        | SuccessorCreationRequest
        | RuntimeStageRequest
        | ArtifactWorkflowRequest,
        expected_workflow_revision: int,
        expected_stage_revision: int,
        expected_stage_input_hash: str,
    ) -> JsonObject:
        effect_key = self._required_text(effect_key, "effect_key", maximum=500)
        expected_stage_input_hash = self._required_text(
            expected_stage_input_hash, "expected_stage_input_hash", maximum=64
        )
        if not isinstance(
            request,
            (
                SourceBindingRequest,
                SourceImportRequest,
                LineageQueryRequest,
                SuccessorCreationRequest,
                RuntimeStageRequest,
                ArtifactWorkflowRequest,
            ),
        ):
            raise TypeError("request must be a typed workflow effect request")
        effect_class = "query" if isinstance(request, LineageQueryRequest) else "mutation"
        effect_kind = (
            request.effect_kind
            if isinstance(request, (RuntimeStageRequest, ArtifactWorkflowRequest))
            else request.DOMAIN
        )
        with self._transaction() as conn:
            workflow = self._workflow_row(conn, workflow_id)
            stage = self._workflow_stage_row(conn, workflow_id, stage_key)
            request_run_id = getattr(request, "run_id", None)
            if request_run_id is not None and request_run_id != workflow["run_id"]:
                raise InvalidTransition("request_run_mismatch", "effect_pending")
            expected_stage_effect = (
                "runtime"
                if isinstance(request, RuntimeStageRequest)
                else "artifact"
                if isinstance(request, ArtifactWorkflowRequest)
                else "engine_query"
                if isinstance(request, LineageQueryRequest)
                else "engine_mutation"
            )
            if stage["effect"] != expected_stage_effect:
                raise InvalidTransition("request_effect_mismatch", "effect_pending")
            requirement_table, requirement_column = (
                (
                    "workflow_stage_result_requirements",
                    "result_kind",
                )
                if effect_class == "query"
                else (
                    "workflow_stage_receipt_requirements",
                    "effect_kind",
                )
            )
            required = conn.execute(
                f"""SELECT 1 FROM {requirement_table}
                    WHERE workflow_id = ? AND stage_id = ?
                      AND {requirement_column} = ?""",
                (workflow_id, stage["id"], effect_kind),
            ).fetchone()
            if required is None:
                raise InvalidTransition("effect_not_required", "effect_pending")
            existing = conn.execute(
                """SELECT id FROM workflow_effect_commands
                   WHERE workflow_id = ? AND stage_id = ?
                     AND effect_kind = ? AND effect_key = ?""",
                (workflow_id, stage["id"], effect_kind, effect_key),
            ).fetchone()
            if existing is not None:
                value = self._workflow_effect(conn, str(existing["id"]))
                stored = value["request"]
                if (
                    value["operation_id"] != request.operation_id
                    or value["request_hash"] != request.request_hash
                    or stored.to_dict() != request.to_dict()
                ):
                    raise InvalidTransition(
                        "effect_identity_drift", "effect_replay"
                    )
                return value
            if isinstance(request, (RuntimeStageRequest, ArtifactWorkflowRequest)):
                run = self._run(conn, str(workflow["run_id"]))
                if request.attempt_id != run["active_attempt_id"]:
                    raise InvalidTransition(
                        "request_attempt_mismatch", "effect_pending"
                    )
                if request.stage_key != stage_key:
                    raise InvalidTransition("request_stage_mismatch", "effect_pending")
                if request.stage_input_hash != stage["input_hash"]:
                    raise InvalidTransition("request_input_mismatch", "effect_pending")
                dependency_rows = conn.execute(
                    """SELECT workflow_effect_commands.result_identity
                       FROM workflow_stage_dependencies
                       JOIN workflow_effect_commands
                         ON workflow_effect_commands.stage_id =
                            workflow_stage_dependencies.dependency_stage_id
                       WHERE workflow_stage_dependencies.stage_id = ?
                         AND workflow_effect_commands.state = 'completed'
                       ORDER BY workflow_effect_commands.result_identity""",
                    (stage["id"],),
                ).fetchall()
                dependency_result_ids = tuple(
                    str(row["result_identity"]) for row in dependency_rows
                )
                if request.dependency_result_ids != dependency_result_ids:
                    raise InvalidTransition(
                        "dependency_result_mismatch", "effect_pending"
                    )
            self._expect_revision(workflow, expected_workflow_revision)
            self._expect_revision(stage, expected_stage_revision)
            if stage["input_hash"] != expected_stage_input_hash:
                raise InvalidTransition("stage_input_drift", "effect_pending")
            self._expect_workflow_run_open(
                self._run(conn, str(workflow["run_id"]))
            )
            if not workflow["definition_sealed"] or stage["state"] != "active":
                raise InvalidTransition(str(stage["state"]), "effect_pending")
            operation_collision = conn.execute(
                "SELECT id FROM workflow_effect_commands WHERE operation_id = ?",
                (request.operation_id,),
            ).fetchone()
            if operation_collision is not None:
                raise InvalidTransition("effect_identity_drift", "effect_replay")
            effect_id = self._id_factory("workflow_effect")
            conn.execute(
                """INSERT INTO workflow_effect_commands(
                       id, operation_id, workflow_id, stage_id, effect_class,
                       effect_kind, effect_key, request_hash, request_json,
                       state, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    effect_id,
                    request.operation_id,
                    workflow_id,
                    stage["id"],
                    effect_class,
                    effect_kind,
                    effect_key,
                    request.request_hash,
                    self._json(request.to_dict()),
                    self._now(),
                ),
            )
            return self._workflow_effect(conn, effect_id)

    def list_dispatchable_workflow_effects(self) -> list[JsonObject]:
        with self._connect() as conn:
            now = self._now()
            rows = conn.execute(
                """SELECT workflow_effect_commands.id
                   FROM workflow_effect_commands
                   JOIN workflow_instances
                     ON workflow_instances.id = workflow_effect_commands.workflow_id
                   JOIN runs ON runs.id = workflow_instances.run_id
                   WHERE runs.state NOT IN (
                       'cancel_requested', 'canceled', 'completed', 'failed'
                   ) AND (
                       workflow_effect_commands.state = 'pending'
                       OR (
                           workflow_effect_commands.state = 'claimed'
                           AND workflow_effect_commands.effect_class = 'query'
                           AND workflow_effect_commands.claim_expires_at <= ?
                       )
                   )
                   ORDER BY workflow_effect_commands.created_at,
                            workflow_effect_commands.id""",
                (now,),
            ).fetchall()
            return [self._workflow_effect(conn, str(row["id"])) for row in rows]

    def supersede_workflow_runtime_effect(
        self,
        *,
        effect_id: str,
        replacement_attempt_id: str,
    ) -> JsonObject:
        """Fence one settled old-attempt envelope before runtime rebinding."""
        with self._connect() as conn:
            effect = self._workflow_effect(conn, effect_id)
        if not isinstance(effect["request"], RuntimeStageRequest):
            raise InvalidTransition("non_runtime_effect", "superseded")
        return self.supersede_workflow_attempt_effect(
            effect_id=effect_id,
            replacement_attempt_id=replacement_attempt_id,
        )

    def supersede_workflow_attempt_effect(
        self,
        *,
        effect_id: str,
        replacement_attempt_id: str,
    ) -> JsonObject:
        """Fence one pending runtime or artifact effect before attempt rebinding."""
        with self._transaction() as conn:
            effect = self._workflow_effect(conn, effect_id)
            request = effect["request"]
            if not isinstance(
                request, (RuntimeStageRequest, ArtifactWorkflowRequest)
            ):
                raise InvalidTransition("non_attempt_effect", "superseded")
            artifact_effect = isinstance(request, ArtifactWorkflowRequest)
            superseded_category = (
                "artifact_attempt_superseded"
                if artifact_effect
                else "runtime_attempt_superseded"
            )
            unsettled_category = (
                "artifact_effect_unsettled"
                if artifact_effect
                else "runtime_effect_unsettled"
            )
            workflow = self._workflow_row(conn, str(effect["workflow_id"]))
            run = self._run(conn, str(workflow["run_id"]))
            self._expect_workflow_run_open(run)
            if run["active_attempt_id"] != replacement_attempt_id:
                raise InvalidTransition(
                    "replacement_attempt_mismatch", "superseded"
                )
            if request.attempt_id == replacement_attempt_id:
                return effect
            replacement = self._attempt(conn, replacement_attempt_id)
            source_attempt_id = replacement.get("source_attempt_id")
            ancestors: set[str] = set()
            while source_attempt_id is not None:
                source_attempt_id = str(source_attempt_id)
                if source_attempt_id in ancestors:
                    raise InvalidTransition(
                        "attempt_lineage_cycle", "superseded"
                    )
                ancestors.add(source_attempt_id)
                source = self._attempt(conn, source_attempt_id)
                source_attempt_id = source.get("source_attempt_id")
            if request.attempt_id not in ancestors:
                raise InvalidTransition(
                    (
                        "artifact_attempt_not_ancestor"
                        if artifact_effect
                        else "runtime_attempt_not_ancestor"
                    ),
                    "superseded",
                )
            state = str(effect["state"])
            if state == "completed" or (
                state == "failed"
                and effect["failure_category"] == superseded_category
            ):
                return effect
            if state != "pending":
                raise InvalidTransition(unsettled_category, "superseded")
            now = self._now()
            cursor = conn.execute(
                """UPDATE workflow_effect_commands
                   SET state = 'failed', failure_category = ?, completed_at = ?
                   WHERE id = ? AND state = 'pending'""",
                (superseded_category, now, effect_id),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition(unsettled_category, "superseded")
            self._audit(
                conn,
                "workflow_effect",
                effect_id,
                "workflow.effect.superseded",
                {
                    "attempt_id": request.attempt_id,
                    "replacement_attempt_id": replacement_attempt_id,
                },
            )
            return self._workflow_effect(conn, effect_id)

    def record_effect_watch_digests(
        self, *, effect_id: str, digests: Mapping[str, Any]
    ) -> JsonObject:
        """⟦AMD-12⟧ Land D4(4)'s before/after content-tree digest pair.

        `SourceImportResult.FIELDS` is a closed set and its `manifest` is pinned
        to exactly three integer counts, so the pair cannot ride in the receipt
        without changing a frozen DTO. It lands instead as one `control_audit`
        row keyed by the effect command it belongs to -- the same mechanism
        `capture.lease_expired` uses -- which is the named landing surface
        ⟦AMD-1⟧ requires before a digest may be called "recorded through
        `ControlStore`".
        """

        payload = dict(digests)
        with self._transaction() as conn:
            # Refuses an id that names no effect, so the row can never be an
            # orphan pointing at nothing.
            self._workflow_effect(conn, effect_id)
            self._audit(
                conn,
                "workflow_effect",
                effect_id,
                "workflow.effect.watch_digests",
                payload,
            )
        return payload

    def effect_watch_digests(self, *, effect_id: str) -> list[JsonObject]:
        """Every watch-digest pair recorded for one effect, oldest first."""

        with self._connect() as conn:
            rows = conn.execute(
                """SELECT payload_json FROM control_audit
                   WHERE aggregate_type = 'workflow_effect' AND aggregate_id = ?
                     AND type = 'workflow.effect.watch_digests'
                   ORDER BY cursor""",
                (effect_id,),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def list_workflow_effects(
        self, *, workflow_id: str, stage_key: str | None = None
    ) -> list[JsonObject]:
        """Read durable workflow effects in stable creation order."""
        with self._connect() as conn:
            workflow = self._workflow_row(conn, workflow_id)
            parameters: list[object] = [workflow["id"]]
            stage_filter = ""
            if stage_key is not None:
                stage = self._workflow_stage_row(conn, workflow_id, stage_key)
                stage_filter = " AND stage_id = ?"
                parameters.append(stage["id"])
            rows = conn.execute(
                """SELECT id FROM workflow_effect_commands
                   WHERE workflow_id = ?"""
                + stage_filter
                + " ORDER BY created_at, id",
                parameters,
            ).fetchall()
            return [self._workflow_effect(conn, str(row["id"])) for row in rows]

    def claim_workflow_effect(
        self,
        *,
        effect_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> JsonObject:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        with self._transaction() as conn:
            effect = self._workflow_effect(conn, effect_id)
            now_value = self._utc_now()
            now = self._format_time(now_value)
            if (
                effect["state"] == "claimed"
                and str(effect["claim_expires_at"]) <= now
                and effect["effect_class"] == "mutation"
            ):
                conn.execute(
                    """UPDATE workflow_effect_commands
                       SET state = 'outcome_unknown', claim_owner = NULL,
                           claim_expires_at = NULL WHERE id = ?""",
                    (effect_id,),
                )
                return self._workflow_effect(conn, effect_id)
            workflow = self._workflow_row(conn, str(effect["workflow_id"]))
            self._expect_workflow_run_open(
                self._run(conn, str(workflow["run_id"]))
            )
            if effect["state"] == "claimed":
                if str(effect["claim_expires_at"]) > now:
                    raise InvalidTransition("claimed", "claimed")
            elif effect["state"] != "pending":
                raise InvalidTransition(str(effect["state"]), "claimed")
            expires_at = self._format_time(
                now_value + timedelta(seconds=lease_seconds)
            )
            cursor = conn.execute(
                """UPDATE workflow_effect_commands
                   SET state = 'claimed', claim_owner = ?,
                       claim_epoch = claim_epoch + 1,
                       delivery_epoch = delivery_epoch + 1,
                       claim_expires_at = ?
                   WHERE id = ? AND (
                       state = 'pending' OR (
                           state = 'claimed' AND effect_class = 'query'
                           AND claim_expires_at <= ?
                       )
                   )""",
                (worker_id, expires_at, effect_id, now),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition(str(effect["state"]), "claimed")
            if effect["effect_class"] == "mutation":
                self._audit(
                    conn,
                    "workflow_effect",
                    effect_id,
                    "workflow.effect.dispatched",
                    {
                        "claim_epoch": int(effect["claim_epoch"]) + 1,
                        "delivery_epoch": int(effect["delivery_epoch"]) + 1,
                    },
                )
            value = self._workflow_effect(conn, effect_id)
            value["request"] = replace(
                value["request"], delivery_epoch=value["delivery_epoch"]
            )
            return value

    def complete_workflow_effect(
        self,
        *,
        effect_id: str,
        worker_id: str,
        claim_epoch: int,
        delivery_epoch: int,
        result: Any,
    ) -> JsonObject:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        with self._transaction() as conn:
            effect = self._workflow_effect(conn, effect_id)
            self._expect_workflow_effect_claim(
                effect,
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                delivery_epoch=delivery_epoch,
                state="claimed",
            )
            request = replace(effect["request"], delivery_epoch=delivery_epoch)
            request.validate_result(result)
            self._expect_workflow_effect_result(
                conn, effect=effect, result=result, target="effect_completed"
            )
            now = self._now()
            cursor = conn.execute(
                """UPDATE workflow_effect_commands
                   SET state = 'completed', claim_owner = NULL,
                       claim_expires_at = NULL, result_identity = ?,
                       receipt_json = ?, completed_at = ?
                   WHERE id = ? AND state = 'claimed' AND claim_owner = ?
                     AND claim_epoch = ? AND delivery_epoch = ?
                     AND claim_expires_at > ?""",
                (
                    result.result_identity,
                    self._json(result.to_dict()),
                    now,
                    effect_id,
                    worker_id,
                    claim_epoch,
                    delivery_epoch,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("stale_claim", "completed")
            return self._workflow_effect(conn, effect_id)

    def mark_workflow_effect_outcome_unknown(
        self,
        *,
        effect_id: str,
        worker_id: str,
        claim_epoch: int,
        delivery_epoch: int,
    ) -> JsonObject:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        with self._transaction() as conn:
            effect = self._workflow_effect(conn, effect_id)
            if effect["effect_class"] != "mutation":
                raise InvalidTransition("query", "outcome_unknown")
            self._expect_workflow_effect_claim(
                effect,
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                delivery_epoch=delivery_epoch,
                state="claimed",
                require_unexpired=False,
            )
            conn.execute(
                """UPDATE workflow_effect_commands
                   SET state = 'outcome_unknown', claim_owner = NULL,
                       claim_expires_at = NULL
                   WHERE id = ? AND state = 'claimed' AND claim_owner = ?
                     AND claim_epoch = ? AND delivery_epoch = ?""",
                (effect_id, worker_id, claim_epoch, delivery_epoch),
            )
            return self._workflow_effect(conn, effect_id)

    def reject_workflow_effect(
        self,
        *,
        effect_id: str,
        worker_id: str,
        claim_epoch: int,
        delivery_epoch: int,
        failure_category: str,
        reconciliation_result: EffectReconciliationResult | None = None,
    ) -> JsonObject:
        """Claim-fence a permanent effect failure and settle owned lifecycle."""
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        failure_category = self._required_text(
            failure_category, "failure_category", maximum=100
        )
        if (
            _ARTIFACT_FAILURE_RE.fullmatch(failure_category) is None
            or failure_category == "outcome_unknown"
        ):
            raise ValueError("failure_category is invalid")
        with self._transaction() as conn:
            effect = self._workflow_effect(conn, effect_id)
            state = str(effect["state"])
            if state not in {"claimed", "reconciling"}:
                raise InvalidTransition("stale_claim", "failed")
            self._expect_workflow_effect_claim(
                effect,
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                delivery_epoch=delivery_epoch,
                state=state,
            )
            if state == "reconciling":
                request = EffectReconciliationRequest(
                    effect_request=effect["request"],
                    delivery_epoch=delivery_epoch,
                )
                request.validate_result(reconciliation_result)
                if reconciliation_result.disposition != "not_found":
                    raise InvalidTransition(
                        str(reconciliation_result.disposition), "failed"
                    )
            elif reconciliation_result is not None:
                raise ValueError(
                    "reconciliation_result requires a reconciling effect"
                )

            workflow = self._workflow_row(conn, str(effect["workflow_id"]))
            stage = self._workflow_stage(conn, str(effect["stage_id"]))
            run = self._run(conn, str(workflow["run_id"]))
            run_fenced = run["state"] in _WORKFLOW_FENCED_RUN_STATES
            if not run_fenced and stage["state"] not in {"active", "waiting"}:
                raise InvalidTransition(str(stage["state"]), "failed")
            now = self._now()
            effect_cursor = conn.execute(
                """UPDATE workflow_effect_commands
                   SET state = 'failed', claim_owner = NULL,
                       claim_expires_at = NULL, failure_category = ?,
                       completed_at = ?
                   WHERE id = ? AND state = ? AND claim_owner = ?
                     AND claim_epoch = ? AND delivery_epoch = ?
                     AND claim_expires_at > ?""",
                (
                    failure_category,
                    now,
                    effect_id,
                    state,
                    worker_id,
                    claim_epoch,
                    delivery_epoch,
                    now,
                ),
            )
            if effect_cursor.rowcount != 1:
                raise InvalidTransition("stale_claim", "failed")
            if run_fenced:
                return self._workflow_effect(conn, effect_id)
            if run["state"] == "paused":
                self._audit(
                    conn,
                    "workflow_effect",
                    effect_id,
                    "workflow.effect.failure_deferred",
                    {"failure_category": failure_category},
                )
                return self._workflow_effect(conn, effect_id)
            self._settle_workflow_failure(
                conn,
                effect_id=effect_id,
                workflow=workflow,
                stage=stage,
                run=run,
                failure_category=failure_category,
                now=now,
            )
            return self._workflow_effect(conn, effect_id)

    def settle_workflow_effect_failure(self, *, effect_id: str) -> JsonObject:
        """Apply a deferred terminal effect failure once its run is resumable."""
        with self._transaction() as conn:
            effect = self._workflow_effect(conn, effect_id)
            if effect["state"] != "failed":
                raise InvalidTransition(str(effect["state"]), "failed")
            if effect["failure_category"] in {
                "runtime_attempt_superseded",
                "artifact_attempt_superseded",
            }:
                return effect
            workflow = self._workflow_row(conn, str(effect["workflow_id"]))
            stage = self._workflow_stage(conn, str(effect["stage_id"]))
            run = self._run(conn, str(workflow["run_id"]))
            if run["state"] == "paused" or run["state"] in _WORKFLOW_FENCED_RUN_STATES:
                return effect
            if workflow["state"] == "failed" and stage["state"] == "failed":
                return effect
            if stage["state"] not in {"active", "waiting"}:
                raise InvalidTransition(str(stage["state"]), "failed")
            self._settle_workflow_failure(
                conn,
                effect_id=effect_id,
                workflow=workflow,
                stage=stage,
                run=run,
                failure_category=str(effect["failure_category"]),
                now=self._now(),
            )
            return self._workflow_effect(conn, effect_id)

    def _settle_workflow_failure(
        self,
        conn: sqlite3.Connection,
        *,
        effect_id: str,
        workflow: Mapping[str, Any],
        stage: Mapping[str, Any],
        run: Mapping[str, Any],
        failure_category: str,
        now: str,
    ) -> None:
        conn.execute(
            """UPDATE workflow_stage_instances
               SET state = 'failed', revision = revision + 1
               WHERE id = ? AND state IN ('active', 'waiting')""",
            (stage["id"],),
        )
        conn.execute(
            """UPDATE workflow_instances
               SET state = 'failed', revision = revision + 1, updated_at = ?
               WHERE id = ?
                 AND state IN ('running', 'waiting', 'manual_recovery')""",
            (now, workflow["id"]),
        )

        if "failed" in _RUN_TRANSITIONS.get(str(run["state"]), set()) and (
            run["state"] not in _WORKFLOW_FENCED_RUN_STATES
        ):
            attempt_id = run.get("active_attempt_id")
            failed_runtime_action_ids = (
                self._fail_pending_runtime_actions(
                    conn,
                    run_id=str(run["id"]),
                    attempt_id=str(attempt_id),
                    settled_at=now,
                )
                if attempt_id is not None
                else []
            )
            closed_source_work = self._close_source_work_for_run(
                conn, run_id=str(run["id"]), settled_at=now
            )
            decision_rows = conn.execute(
                """SELECT id FROM decisions
                   WHERE run_id = ? AND state = 'pending'
                   ORDER BY created_at, id""",
                (run["id"],),
            ).fetchall()
            expired_decision_ids = [str(row["id"]) for row in decision_rows]
            if expired_decision_ids:
                conn.execute(
                    """UPDATE decisions
                       SET state = 'expired', revision = revision + 1,
                           resolved_at = ?
                       WHERE run_id = ? AND state = 'pending'""",
                    (now, run["id"]),
                )
            conn.execute(
                """UPDATE runs SET state = 'failed',
                   revision = revision + 1, updated_at = ? WHERE id = ?""",
                (now, run["id"]),
            )
            if attempt_id is not None:
                conn.execute(
                    """UPDATE attempts SET state = 'failed', finished_at = ?,
                       dispatch_owner = NULL, dispatch_expires_at = NULL
                       WHERE id = ?""",
                    (now, attempt_id),
                )
            conn.execute(
                """UPDATE threads SET status = 'failed', active_run_id = NULL,
                   revision = revision + 1, updated_at = ?
                   WHERE id = ? AND active_run_id = ?""",
                (now, run["thread_id"], run["id"]),
            )
            pin_release_action_id = None
            if attempt_id is not None:
                attempt = self._attempt(conn, str(attempt_id))
                if (
                    attempt.get("runtime_identity_version") == 1
                    and attempt.get("runtime_release_id") is not None
                    and attempt.get("state_generation_id") is not None
                ):
                    pin_release_action_id = self._insert_pin_release(
                        conn,
                        run_id=str(run["id"]),
                        attempt=attempt,
                        now=now,
                    )
            event_payload: JsonObject = {
                "from": run["state"],
                "state": "failed",
                "category": failure_category,
                "workflow_effect_id": effect_id,
                "retryable": False,
            }
            if failed_runtime_action_ids:
                event_payload["failed_runtime_action_ids"] = (
                    failed_runtime_action_ids
                )
            if expired_decision_ids:
                event_payload["expired_decision_ids"] = expired_decision_ids
            if closed_source_work:
                event_payload["closed_source_work"] = closed_source_work
            if pin_release_action_id is not None:
                event_payload["pin_release_action_id"] = pin_release_action_id
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(attempt_id) if attempt_id is not None else None,
                event_type="run.failed",
                payload=event_payload,
            )

    def claim_workflow_effect_reconciliation(
        self,
        *,
        effect_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> JsonObject:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        with self._transaction() as conn:
            effect = self._workflow_effect(conn, effect_id)
            if effect["effect_class"] != "mutation":
                raise InvalidTransition("query", "reconciling")
            now_value = self._utc_now()
            now = self._format_time(now_value)
            if effect["state"] == "reconciling" and str(
                effect["claim_expires_at"]
            ) > now:
                raise InvalidTransition("reconciling", "reconciling")
            if effect["state"] not in {"outcome_unknown", "reconciling"}:
                raise InvalidTransition(str(effect["state"]), "reconciling")
            expires_at = self._format_time(
                now_value + timedelta(seconds=lease_seconds)
            )
            cursor = conn.execute(
                """UPDATE workflow_effect_commands
                   SET state = 'reconciling', claim_owner = ?,
                       claim_epoch = claim_epoch + 1,
                       delivery_epoch = delivery_epoch + 1,
                       claim_expires_at = ?
                   WHERE id = ? AND (
                       state = 'outcome_unknown' OR (
                           state = 'reconciling' AND claim_expires_at <= ?
                       )
                   )""",
                (worker_id, expires_at, effect_id, now),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition(str(effect["state"]), "reconciling")
            value = self._workflow_effect(conn, effect_id)
            value["reconciliation_request"] = EffectReconciliationRequest(
                effect_request=effect["request"],
                delivery_epoch=value["delivery_epoch"],
            )
            return value

    def complete_workflow_effect_reconciliation(
        self,
        *,
        effect_id: str,
        worker_id: str,
        claim_epoch: int,
        delivery_epoch: int,
        result: EffectReconciliationResult,
    ) -> JsonObject:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        with self._transaction() as conn:
            effect = self._workflow_effect(conn, effect_id)
            self._expect_workflow_effect_claim(
                effect,
                worker_id=worker_id,
                claim_epoch=claim_epoch,
                delivery_epoch=delivery_epoch,
                state="reconciling",
            )
            request = EffectReconciliationRequest(
                effect_request=effect["request"], delivery_epoch=delivery_epoch
            )
            request.validate_result(result)
            now = self._now()
            if result.disposition == "not_found":
                conn.execute(
                    """UPDATE workflow_effect_commands
                       SET state = 'pending', claim_owner = NULL,
                           claim_expires_at = NULL WHERE id = ?""",
                    (effect_id,),
                )
            elif result.disposition == "committed":
                assert result.result is not None
                self._expect_workflow_effect_result(
                    conn,
                    effect=effect,
                    result=result.result,
                    target="effect_reconciled",
                )
                conn.execute(
                    """UPDATE workflow_effect_commands
                       SET state = 'completed', claim_owner = NULL,
                           claim_expires_at = NULL, result_identity = ?,
                           receipt_json = ?, completed_at = ? WHERE id = ?""",
                    (
                        result.result.result_identity,
                        self._json(result.result.to_dict()),
                        now,
                        effect_id,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE workflow_effect_commands
                       SET state = 'manual_required', claim_owner = NULL,
                           claim_expires_at = NULL,
                           failure_category = 'outcome_unknown', completed_at = ?
                       WHERE id = ?""",
                    (now, effect_id),
                )
            return self._workflow_effect(conn, effect_id)

    def complete_workflow_stage(
        self,
        *,
        workflow_id: str,
        stage_key: str,
        expected_workflow_revision: int,
        expected_stage_revision: int,
        references: Sequence[Mapping[str, Any]],
        checkpoint_state: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        if isinstance(references, (str, bytes)):
            raise TypeError("references must be a sequence")
        normalized_references: list[JsonObject] = []
        for reference in references:
            if not isinstance(reference, Mapping) or set(reference) != {
                "kind",
                "id",
                "metadata",
            }:
                raise ValueError("workflow reference is invalid")
            kind = self._required_text(
                reference["kind"], "reference.kind", maximum=100
            )
            reference_id = self._required_text(
                reference["id"], "reference.id", maximum=1_000
            )
            metadata = reference["metadata"]
            if not isinstance(metadata, Mapping):
                raise TypeError("reference.metadata must be a mapping")
            normalized_references.append(
                {"kind": kind, "id": reference_id, "metadata": dict(metadata)}
            )
        if checkpoint_state is not None and not isinstance(checkpoint_state, Mapping):
            raise TypeError("checkpoint_state must be a mapping")
        with self._transaction() as conn:
            workflow = self._workflow_row(conn, workflow_id)
            self._expect_revision(workflow, expected_workflow_revision)
            run = self._run(conn, str(workflow["run_id"]))
            self._expect_workflow_run_open(run)
            stage = self._workflow_stage_row(conn, workflow_id, stage_key)
            self._expect_revision(stage, expected_stage_revision)
            if stage["state"] != "active":
                raise InvalidTransition(str(stage["state"]), "completed")
            required_rows = conn.execute(
                """SELECT effect_kind AS kind
                   FROM workflow_stage_receipt_requirements WHERE stage_id = ?
                   UNION ALL
                   SELECT result_kind AS kind
                   FROM workflow_stage_result_requirements WHERE stage_id = ?""",
                (stage["id"], stage["id"]),
            ).fetchall()
            required_kinds = {str(row["kind"]) for row in required_rows}
            effect_rows = conn.execute(
                """SELECT id FROM workflow_effect_commands WHERE stage_id = ?""",
                (stage["id"],),
            ).fetchall()
            effects = [
                self._workflow_effect(conn, str(row["id"]))
                for row in effect_rows
            ]
            attempt_effects = [
                effect
                for effect in effects
                if isinstance(
                    effect["request"],
                    (RuntimeStageRequest, ArtifactWorkflowRequest),
                )
            ]
            active_attempt_effects = [
                effect
                for effect in attempt_effects
                if effect["request"].attempt_id == run["active_attempt_id"]
            ]
            if active_attempt_effects:
                stale_attempt_effects = [
                    effect
                    for effect in attempt_effects
                    if effect["request"].attempt_id != run["active_attempt_id"]
                ]
                if any(
                    effect["state"] != "completed"
                    and not (
                        effect["state"] == "failed"
                        and effect["failure_category"]
                        == (
                            "artifact_attempt_superseded"
                            if isinstance(
                                effect["request"], ArtifactWorkflowRequest
                            )
                            else "runtime_attempt_superseded"
                        )
                    )
                    for effect in stale_attempt_effects
                ):
                    category = (
                        "artifact_effect_unsettled"
                        if any(
                            isinstance(
                                effect["request"], ArtifactWorkflowRequest
                            )
                            for effect in stale_attempt_effects
                        )
                        else "runtime_effect_unsettled"
                    )
                    raise InvalidTransition(
                        category, "completed"
                    )
                effects = active_attempt_effects
            completed_kinds = {
                str(effect["effect_kind"])
                for effect in effects
                if effect["state"] == "completed"
            }
            if required_kinds - completed_kinds or any(
                effect["state"] != "completed" for effect in effects
            ):
                raise InvalidTransition(
                    "effect_requirements_missing", "completed"
                )

            required_references: set[tuple[str, str]] = set()
            required_artifact_references: set[tuple[str, str]] = set()
            for effect in effects:
                receipt = effect["receipt"] or {}
                if effect["effect_kind"] in {"source_binding", "source_import"}:
                    engine_reference = receipt.get("engine_reference") or {}
                    if engine_reference.get("value"):
                        required_references.add(
                            ("engine_source", str(engine_reference["value"]))
                        )
                elif effect["effect_kind"] == "lineage_query":
                    for node in receipt.get("nodes", []):
                        engine_reference = node.get("engine_reference") or {}
                        if engine_reference.get("value"):
                            required_references.add(
                                ("lineage_node", str(engine_reference["value"]))
                            )
                elif effect["effect_kind"] == "successor_creation":
                    engine_reference = (receipt.get("node") or {}).get(
                        "engine_reference"
                    ) or {}
                    if engine_reference.get("value"):
                        required_references.add(
                            ("lineage_node", str(engine_reference["value"]))
                        )
                if isinstance(effect["request"], RuntimeStageRequest):
                    runtime_result = RuntimeStageResult.from_dict(receipt)
                    self._expect_workflow_effect_result(
                        conn,
                        effect=effect,
                        result=runtime_result,
                        target="stage_completed",
                    )
                    required_references.add(
                        ("runtime_event", runtime_result.runtime_event_id)
                    )
                elif isinstance(effect["request"], ArtifactWorkflowRequest):
                    artifact_result = ArtifactWorkflowResult.from_dict(receipt)
                    self._expect_workflow_effect_result(
                        conn,
                        effect=effect,
                        result=artifact_result,
                        target="stage_completed",
                    )
                    artifact_references = {
                        ("artifact_version", version_id)
                        for version_id in artifact_result.artifact_version_ids
                    }
                    if artifact_result.snapshot_id is not None:
                        artifact_references.add(
                            ("snapshot", artifact_result.snapshot_id)
                        )
                    required_artifact_references.update(artifact_references)
                    required_references.update(artifact_references)

            decision_ref = conn.execute(
                """SELECT decision_id, selected_choice
                   FROM workflow_decision_refs
                   WHERE stage_id = ? AND state = 'resolved'""",
                (stage["id"],),
            ).fetchone()
            if stage["effect"] == "decision":
                if decision_ref is None:
                    raise InvalidTransition("decision_unresolved", "completed")
                normalized_references.append(
                    {
                        "kind": "decision",
                        "id": str(decision_ref["decision_id"]),
                        "metadata": {"choice": decision_ref["selected_choice"]},
                    }
                )
                required_references.add(
                    ("decision", str(decision_ref["decision_id"]))
                )
            supplied_references = {
                (reference["kind"], reference["id"])
                for reference in normalized_references
            }
            if required_references - supplied_references:
                raise InvalidTransition("references_missing", "completed")
            supplied_exact_references = {
                identity
                for identity in supplied_references
                if identity[0] in _WORKFLOW_EXACT_REFERENCE_KINDS
            }
            supplied_context_references = supplied_references - supplied_exact_references
            required_runtime_events = {
                identity
                for identity in required_references
                if identity[0] == "runtime_event"
            }
            supplied_runtime_events = {
                identity
                for identity in supplied_references
                if identity[0] == "runtime_event"
            }
            if (
                supplied_exact_references - required_references
                or supplied_runtime_events - required_runtime_events
                or (
                    required_artifact_references
                    and supplied_references != required_references
                )
                or any(
                    kind not in _WORKFLOW_CONTEXT_REFERENCE_KINDS
                    for kind, _ in supplied_context_references
                )
            ):
                raise InvalidTransition("references_extra", "completed")
            for _, runtime_event_id in supplied_runtime_events:
                event = conn.execute(
                    """SELECT run_id, attempt_id, type FROM run_events
                       WHERE id = ?""",
                    (runtime_event_id,),
                ).fetchone()
                if event is None or event["run_id"] != run["id"]:
                    raise InvalidTransition(
                        "runtime_event_identity_mismatch", "completed"
                    )
                if (
                    run["active_attempt_id"] is None
                    or event["attempt_id"] != run["active_attempt_id"]
                ):
                    raise InvalidTransition(
                        "runtime_event_attempt_mismatch", "completed"
                    )
                if not str(event["type"]).startswith("runtime."):
                    raise InvalidTransition(
                        "runtime_event_type_mismatch", "completed"
                    )
            if bool(stage["checkpoint_enabled"]) != (checkpoint_state is not None):
                raise InvalidTransition("checkpoint_requirement_mismatch", "completed")

            now = self._now()
            inserted_references: list[JsonObject] = []
            seen_references: set[tuple[str, str]] = set()
            for reference in normalized_references:
                identity = (reference["kind"], reference["id"])
                if identity in seen_references:
                    raise ValueError("workflow references must be unique")
                seen_references.add(identity)
                reference_id = self._id_factory("workflow_reference")
                identity_hash = self._request_hash(reference)
                conn.execute(
                    """INSERT INTO workflow_stage_references(
                           id, workflow_id, stage_id, reference_kind,
                           reference_id, identity_hash, metadata_json, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        reference_id,
                        workflow_id,
                        stage["id"],
                        reference["kind"],
                        reference["id"],
                        identity_hash,
                        self._json(reference["metadata"]),
                        now,
                    ),
                )
                inserted_references.append(
                    self._workflow_reference(conn, reference_id)
                )

            stage_cursor = conn.execute(
                """UPDATE workflow_stage_instances
                   SET state = 'completed', revision = revision + 1,
                       completed_at = ?
                   WHERE id = ? AND revision = ? AND state = 'active'""",
                (now, stage["id"], expected_stage_revision),
            )
            if stage_cursor.rowcount != 1:
                raise RevisionConflict(
                    self._workflow_stage_row(conn, workflow_id, stage_key)
                )
            checkpoint: JsonObject | None = None
            if checkpoint_state is not None:
                checkpoint_id = self._id_factory("workflow_checkpoint")
                state_value = dict(checkpoint_state)
                references_hash = hashlib.sha256(self._json(
                    [
                        {
                            "kind": item["reference_kind"],
                            "id": item["reference_id"],
                            "identity_hash": item["identity_hash"],
                        }
                        for item in sorted(
                            inserted_references,
                            key=lambda item: (
                                item["reference_kind"], item["reference_id"]
                            ),
                        )
                    ]
                ).encode()).hexdigest()
                conn.execute(
                    """INSERT INTO workflow_checkpoints(
                           id, workflow_id, stage_id, workflow_revision,
                           state_hash, state_json, references_hash, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        checkpoint_id,
                        workflow_id,
                        stage["id"],
                        expected_workflow_revision + 1,
                        self._request_hash(state_value),
                        self._json(state_value),
                        references_hash,
                        now,
                    ),
                )
                checkpoint = self._workflow_checkpoint(conn, checkpoint_id)

            conn.execute(
                """UPDATE workflow_stage_instances AS candidate SET state = 'ready'
                   WHERE candidate.workflow_id = ? AND candidate.state = 'pending'
                     AND NOT EXISTS (
                         SELECT 1 FROM workflow_stage_dependencies AS edge
                         JOIN workflow_stage_instances AS dependency
                           ON dependency.id = edge.dependency_stage_id
                         WHERE edge.stage_id = candidate.id
                           AND dependency.state != 'completed'
                     )""",
                (workflow_id,),
            )
            next_row = conn.execute(
                """SELECT stage_key FROM workflow_stage_instances
                   WHERE workflow_id = ? AND state = 'ready'
                   ORDER BY position LIMIT 1""",
                (workflow_id,),
            ).fetchone()
            unfinished = conn.execute(
                """SELECT COUNT(*) FROM workflow_stage_instances
                   WHERE workflow_id = ? AND state != 'completed'""",
                (workflow_id,),
            ).fetchone()[0]
            workflow_state = "completed" if unfinished == 0 else "running"
            next_stage_key = str(next_row[0]) if next_row is not None else None
            workflow_cursor = conn.execute(
                """UPDATE workflow_instances
                   SET state = ?, current_stage_key = ?, revision = revision + 1,
                       updated_at = ? WHERE id = ? AND revision = ?""",
                (
                    workflow_state,
                    next_stage_key,
                    now,
                    workflow_id,
                    expected_workflow_revision,
                ),
            )
            if workflow_cursor.rowcount != 1:
                raise RevisionConflict(self._workflow_row(conn, workflow_id))
            return {
                "workflow": self._workflow(conn, workflow_id),
                "stage": self._workflow_stage(conn, str(stage["id"])),
                "references": inserted_references,
                "checkpoint": checkpoint,
            }

    def classify_workflow_recovery(self) -> list[JsonObject]:
        """Classify persisted effects without invoking adapters or changing state."""
        with self._connect() as conn:
            now = self._now()
            rows = conn.execute(
                """SELECT workflow_effect_commands.id, runs.state AS run_state
                   FROM workflow_effect_commands
                   JOIN workflow_instances
                     ON workflow_instances.id = workflow_effect_commands.workflow_id
                   JOIN runs ON runs.id = workflow_instances.run_id
                   ORDER BY workflow_effect_commands.created_at,
                            workflow_effect_commands.id"""
            ).fetchall()
            classifications: list[JsonObject] = []
            for row in rows:
                effect = self._workflow_effect(conn, str(row["id"]))
                state = str(effect["state"])
                run_fenced = row["run_state"] in _WORKFLOW_FENCED_RUN_STATES
                if run_fenced and state == "pending":
                    classification = "fenced"
                elif state == "pending":
                    classification = "pending"
                elif state == "claimed":
                    expired = str(effect["claim_expires_at"] or "") <= now
                    if expired and effect["effect_class"] == "mutation":
                        classification = "unknown"
                    elif expired and run_fenced:
                        classification = "fenced"
                    elif expired:
                        classification = "pending"
                    else:
                        classification = "claimed"
                elif state in {"outcome_unknown", "reconciling"}:
                    classification = "unknown"
                else:
                    classification = "terminal"
                classifications.append(
                    {
                        "effect_id": effect["id"],
                        "workflow_id": effect["workflow_id"],
                        "stage_id": effect["stage_id"],
                        "state": state,
                        "classification": classification,
                    }
                )
            return classifications

    def create_thread(
        self,
        *,
        workspace_id: str,
        title: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        title = self._required_text(title, "title", maximum=500)
        request = {"title": title, "expected_revision": expected_revision}
        operation = f"POST:/api/v1/workspaces/{workspace_id}/threads"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            workspace = self._workspace(conn, workspace_id)
            self._expect_revision(workspace, expected_revision)
            now = self._now()
            thread_id = self._id_factory("thread")
            conn.execute(
                """INSERT INTO threads
                   (id, workspace_id, title, status, revision, created_at, updated_at)
                   VALUES (?, ?, ?, 'idle', 0, ?, ?)""",
                (thread_id, workspace_id, title, now, now),
            )
            self._update_revision(conn, "workspaces", workspace_id, expected_revision, now)
            self._audit(
                conn,
                "thread",
                thread_id,
                "thread.created",
                {"workspace_id": workspace_id, "title": title},
            )
            value = self._thread(conn, thread_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def rename_workspace(
        self,
        *,
        workspace_id: str,
        title: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Give a workspace a new title, leaving everything else untouched."""

        title = self._required_text(title, "title", maximum=500)
        request = {"title": title, "expected_revision": expected_revision}
        operation = f"POST:/api/v1/workspaces/{workspace_id}/rename"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            workspace = self._workspace(conn, workspace_id)
            self._expect_revision(workspace, expected_revision)
            now = self._now()
            conn.execute(
                "UPDATE workspaces SET title = ? WHERE id = ?", (title, workspace_id)
            )
            self._update_revision(conn, "workspaces", workspace_id, expected_revision, now)
            self._audit(
                conn,
                "workspace",
                workspace_id,
                "workspace.renamed",
                {"from": workspace["title"], "to": title},
            )
            value = self._workspace(conn, workspace_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def rename_thread(
        self,
        *,
        thread_id: str,
        title: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Give a thread a new title without touching its workspace."""

        title = self._required_text(title, "title", maximum=500)
        request = {"title": title, "expected_revision": expected_revision}
        operation = f"POST:/api/v1/threads/{thread_id}/rename"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            thread = self._thread(conn, thread_id)
            self._expect_revision(thread, expected_revision)
            now = self._now()
            conn.execute("UPDATE threads SET title = ? WHERE id = ?", (title, thread_id))
            self._update_revision(conn, "threads", thread_id, expected_revision, now)
            self._audit(
                conn,
                "thread",
                thread_id,
                "thread.renamed",
                {"from": thread["title"], "to": title},
            )
            value = self._thread(conn, thread_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def archive_thread(
        self,
        *,
        thread_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        return self._set_thread_archived(
            thread_id=thread_id,
            archived=True,
            expected_revision=expected_revision,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def unarchive_thread(
        self,
        *,
        thread_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        return self._set_thread_archived(
            thread_id=thread_id,
            archived=False,
            expected_revision=expected_revision,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def _set_thread_archived(
        self,
        *,
        thread_id: str,
        archived: bool,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Hide a thread from the default list, or bring it back.

        Archiving is not deletion: the row, its messages and its runs stay
        exactly where they were, and `list_threads(include_archived=True)`
        still returns it. A thread whose run is still active is refused, so
        the archived set never contains something still moving -- and
        `append_message` and `create_run` refuse an archived thread with
        `ThreadArchived`, so it cannot start moving again either.
        """

        action = "archive" if archived else "unarchive"
        request = {"expected_revision": expected_revision}
        operation = f"POST:/api/v1/threads/{thread_id}/{action}"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            thread = self._thread(conn, thread_id)
            self._expect_revision(thread, expected_revision)
            if archived and thread.get("active_run_id") is not None:
                raise ThreadActiveRun(dict(thread))
            if (thread.get("archived_at") is not None) == archived:
                # Already in the requested state: a receipt, no revision bump,
                # no audit row.
                return self._save_receipt(
                    conn, actor_id, operation, idempotency_key, request, thread, 200
                )
            now = self._now()
            conn.execute(
                "UPDATE threads SET archived_at = ? WHERE id = ?",
                (now if archived else None, thread_id),
            )
            self._update_revision(conn, "threads", thread_id, expected_revision, now)
            self._audit(conn, "thread", thread_id, f"thread.{action}d", {})
            value = self._thread(conn, thread_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def append_message(
        self,
        *,
        thread_id: str,
        role: str,
        content: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("role is invalid")
        content = self._required_text(content, "content", maximum=1_000_000)
        request = {
            "role": role,
            "content": content,
            "expected_revision": expected_revision,
        }
        operation = f"POST:/api/v1/threads/{thread_id}/messages"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            thread = self._thread(conn, thread_id)
            self._expect_revision(thread, expected_revision)
            if thread.get("archived_at") is not None:
                raise ThreadArchived(dict(thread))
            position = int(
                conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM messages WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()[0]
            )
            now = self._now()
            message_id = self._id_factory("msg")
            conn.execute(
                """INSERT INTO messages
                   (id, thread_id, role, content, position, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (message_id, thread_id, role, content, position, now),
            )
            self._update_revision(conn, "threads", thread_id, expected_revision, now)
            self._audit(
                conn,
                "thread",
                thread_id,
                "message.created",
                {"message_id": message_id, "role": role, "position": position},
            )
            value = self._message(conn, message_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def create_run(
        self,
        *,
        thread_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        workflow: WorkflowDefinition | None = None,
    ) -> CommandResult:
        """Create a `queued` run on an idle thread.

        ⟦P8 V-3⟧ With `workflow`, the definition is installed on the new run
        in the same transaction (`_install_workflow`), so the run owns its
        workflow from the instant it exists and is a conversation run at no
        instant -- the research engine's carrier form. The definition's
        identity is part of the receipt's request, so a replay with another
        shape is a conflict rather than a silent match.
        """

        request: dict[str, Any] = {"expected_revision": expected_revision}
        if workflow is not None:
            if not isinstance(workflow, WorkflowDefinition):
                raise TypeError("workflow must be a WorkflowDefinition")
            request["workflow"] = {
                "definition_id": workflow.definition_id,
                "version": workflow.version,
            }
        operation = f"POST:/api/v1/threads/{thread_id}/runs"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            thread = self._thread(conn, thread_id)
            self._expect_revision(thread, expected_revision)
            if thread.get("archived_at") is not None:
                raise ThreadArchived(dict(thread))
            if thread["active_run_id"] is not None:
                active = self._run(conn, str(thread["active_run_id"]))
                if active["state"] not in _TERMINAL_RUN_STATES:
                    raise InvalidTransition(str(active["state"]), "queued")
            now = self._now()
            run_id = self._id_factory("run")
            attempt_id = self._id_factory("attempt")
            conn.execute(
                """INSERT INTO runs
                   (id, thread_id, state, active_attempt_id, stage,
                    latest_sequence, revision, created_at, updated_at)
                   VALUES (?, ?, 'queued', ?, NULL, 0, 0, ?, ?)""",
                (run_id, thread_id, attempt_id, now, now),
            )
            conn.execute(
                """INSERT INTO attempts
                   (id, run_id, number, state)
                   VALUES (?, ?, 1, 'queued')""",
                (attempt_id, run_id),
            )
            conn.execute(
                """UPDATE threads
                   SET active_run_id = ?, status = 'running',
                       revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (run_id, now, thread_id, expected_revision),
            )
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type="run.queued",
                payload={"state": "queued"},
            )
            if workflow is not None:
                self._install_workflow(conn, run_id, workflow)
            value = self._run(conn, run_id)
            value["attempt"] = self._attempt(conn, attempt_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def create_runtime_binding(
        self,
        *,
        thread_id: str,
        adapter_id: str,
        runtime_session_ref: str,
        generation: int,
        adapter_version: str,
        actor_id: str,
        idempotency_key: str,
        parent_ref: str | None = None,
    ) -> CommandResult:
        adapter_id = self._required_text(adapter_id, "adapter_id", maximum=100)
        runtime_session_ref = self._required_text(
            runtime_session_ref, "runtime_session_ref", maximum=1_000
        )
        adapter_version = self._required_text(
            adapter_version, "adapter_version", maximum=100
        )
        if type(generation) is not int or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        request = {
            "thread_id": thread_id,
            "adapter_id": adapter_id,
            "runtime_session_ref": runtime_session_ref,
            "generation": generation,
            "adapter_version": adapter_version,
            "parent_ref": parent_ref,
        }
        operation = f"POST:/internal/v1/threads/{thread_id}/runtime-bindings"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            self._thread(conn, thread_id)
            now = self._now()
            binding_id = self._id_factory("runtime_binding")
            conn.execute(
                """INSERT INTO runtime_bindings
                   (id, thread_id, adapter_id, runtime_session_ref, generation,
                    adapter_version, parent_ref, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    binding_id,
                    thread_id,
                    adapter_id,
                    runtime_session_ref,
                    generation,
                    adapter_version,
                    parent_ref,
                    now,
                    now,
                ),
            )
            value = self._runtime_binding(conn, binding_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def pin_attempt_runtime(
        self,
        *,
        run_id: str,
        attempt_id: str,
        runtime_binding_id: str,
        runtime_release_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        state_generation_id: str | None = None,
        dispatch_owner: str | None = None,
    ) -> CommandResult:
        runtime_release_id = self._required_text(
            runtime_release_id, "runtime_release_id", maximum=200
        )
        state_generation_id = self._required_text(
            state_generation_id, "state_generation_id", maximum=200
        )
        dispatch_owner = self._required_text(
            dispatch_owner, "dispatch_owner", maximum=200
        )
        request = {
            "attempt_id": attempt_id,
            "runtime_binding_id": runtime_binding_id,
            "runtime_release_id": runtime_release_id,
            "state_generation_id": state_generation_id,
            "expected_revision": expected_revision,
            "dispatch_owner": dispatch_owner,
        }
        operation = f"POST:/internal/v1/runs/{run_id}/pin-runtime"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            run = self._run(conn, run_id)
            self._expect_revision(run, expected_revision)
            if run["active_attempt_id"] != attempt_id:
                raise InvalidTransition("attempt_mismatch", "runtime_bound")
            attempt = self._attempt(conn, attempt_id)
            binding = self._runtime_binding(conn, runtime_binding_id)
            if binding["thread_id"] != run["thread_id"]:
                raise InvalidTransition("binding_mismatch", "runtime_bound")
            if attempt["runtime_binding_id"] is not None:
                raise InvalidTransition("already_bound", "runtime_bound")
            if (
                attempt.get("dispatch_owner") != dispatch_owner
                or attempt.get("runtime_identity_version") != 1
                or attempt.get("runtime_release_id") != runtime_release_id
                or attempt.get("state_generation_id") != state_generation_id
                or not all(
                    isinstance(attempt.get(field), str) and attempt.get(field)
                    for field in (
                        "runtime_slot_id",
                        "runtime_artifact_digest",
                        "runtime_worker_protocol",
                    )
                )
                or not isinstance(attempt.get("dispatch_expires_at"), str)
                or attempt["dispatch_expires_at"] <= self._now()
            ):
                raise InvalidTransition("dispatch_reservation_mismatch", "runtime_bound")
            now = self._now()
            cursor = conn.execute(
                """UPDATE attempts SET runtime_binding_id = ?, dispatch_owner = NULL,
                   dispatch_expires_at = NULL
                   WHERE id = ? AND runtime_binding_id IS NULL
                     AND dispatch_owner = ? AND runtime_release_id = ?
                     AND state_generation_id = ? AND dispatch_expires_at > ?""",
                (
                    runtime_binding_id,
                    attempt_id,
                    dispatch_owner,
                    runtime_release_id,
                    state_generation_id,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("dispatch_reservation_mismatch", "runtime_bound")
            source_pin_release_action_ids = self._insert_paused_source_releases(
                conn,
                run_id=run_id,
                attempt=attempt,
                now=now,
            )
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run_id, expected_revision),
            )
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type="runtime.bound",
                payload={
                    "runtime_binding_id": runtime_binding_id,
                    "runtime_release_id": runtime_release_id,
                    "state_generation_id": state_generation_id,
                    "source_pin_release_action_ids": source_pin_release_action_ids,
                },
            )
            value = self._run(conn, run_id)
            value["attempt"] = self._attempt(conn, attempt_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def reserve_attempt_dispatch(
        self,
        *,
        run_id: str,
        attempt_id: str,
        dispatch_owner: str,
        runtime_release_id: str,
        state_generation_id: str,
        runtime_slot_id: str,
        runtime_artifact_digest: str,
        runtime_worker_protocol: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        lease_seconds: int = 300,
    ) -> CommandResult:
        """Persist exact managed-runtime identity before any session side effect."""

        dispatch_owner = self._required_text(
            dispatch_owner, "dispatch_owner", maximum=200
        )
        runtime_release_id = self._required_text(
            runtime_release_id, "runtime_release_id", maximum=200
        )
        state_generation_id = self._required_text(
            state_generation_id, "state_generation_id", maximum=200
        )
        runtime_slot_id = self._required_text(
            runtime_slot_id, "runtime_slot_id", maximum=200
        )
        runtime_artifact_digest = self._required_text(
            runtime_artifact_digest, "runtime_artifact_digest", maximum=200
        )
        runtime_worker_protocol = self._required_text(
            runtime_worker_protocol, "runtime_worker_protocol", maximum=100
        )
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 1 and 300")
        request = {
            "attempt_id": attempt_id,
            "dispatch_owner": dispatch_owner,
            "runtime_release_id": runtime_release_id,
            "state_generation_id": state_generation_id,
            "runtime_slot_id": runtime_slot_id,
            "runtime_artifact_digest": runtime_artifact_digest,
            "runtime_worker_protocol": runtime_worker_protocol,
            "expected_revision": expected_revision,
            "lease_seconds": lease_seconds,
        }
        operation = f"POST:/internal/v1/runs/{run_id}/reserve-dispatch"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                attempt = self._attempt(conn, attempt_id)
                if (
                    attempt.get("runtime_binding_id") is None
                    and attempt.get("runtime_identity_version") == 1
                    and attempt.get("dispatch_owner") == dispatch_owner
                    and attempt.get("runtime_release_id") == runtime_release_id
                    and attempt.get("state_generation_id") == state_generation_id
                    and attempt.get("runtime_slot_id") == runtime_slot_id
                    and attempt.get("runtime_artifact_digest")
                    == runtime_artifact_digest
                    and attempt.get("runtime_worker_protocol")
                    == runtime_worker_protocol
                    and isinstance(attempt.get("dispatch_expires_at"), str)
                    and attempt["dispatch_expires_at"] > self._now()
                ):
                    return replay
                raise InvalidTransition("stale_dispatch_receipt", "dispatch_reserved")
            run = self._run(conn, run_id)
            self._expect_revision(run, expected_revision)
            if run["active_attempt_id"] != attempt_id:
                raise InvalidTransition("attempt_mismatch", "dispatch_reserved")
            if run["state"] not in {"queued", "retrying", "resuming"}:
                raise InvalidTransition(str(run["state"]), "dispatch_reserved")
            attempt = self._attempt(conn, attempt_id)
            if attempt["runtime_binding_id"] is not None:
                raise InvalidTransition("already_bound", "dispatch_reserved")
            current = self._utc_now()
            now = self._format_time(current)
            expires = self._format_time(current + timedelta(seconds=lease_seconds))
            cursor = conn.execute(
                """UPDATE attempts
                   SET dispatch_owner = ?, dispatch_expires_at = ?,
                       runtime_release_id = ?, state_generation_id = ?,
                       runtime_slot_id = ?, runtime_artifact_digest = ?,
                       runtime_worker_protocol = ?, runtime_identity_version = 1
                   WHERE id = ? AND runtime_binding_id IS NULL
                     AND (dispatch_owner IS NULL OR dispatch_owner = ?
                          OR dispatch_expires_at <= ?)
                     AND (runtime_release_id IS NULL OR runtime_release_id = ?)
                     AND (state_generation_id IS NULL OR state_generation_id = ?)
                     AND (runtime_slot_id IS NULL OR runtime_slot_id = ?)
                     AND (runtime_artifact_digest IS NULL
                          OR runtime_artifact_digest = ?)
                     AND (runtime_worker_protocol IS NULL
                          OR runtime_worker_protocol = ?)""",
                (
                    dispatch_owner,
                    expires,
                    runtime_release_id,
                    state_generation_id,
                    runtime_slot_id,
                    runtime_artifact_digest,
                    runtime_worker_protocol,
                    attempt_id,
                    dispatch_owner,
                    now,
                    runtime_release_id,
                    state_generation_id,
                    runtime_slot_id,
                    runtime_artifact_digest,
                    runtime_worker_protocol,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("already_reserved", "dispatch_reserved")
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run_id, expected_revision),
            )
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type="runtime.dispatch.reserved",
                payload={
                    "runtime_release_id": runtime_release_id,
                    "state_generation_id": state_generation_id,
                    "runtime_slot_id": runtime_slot_id,
                    "runtime_artifact_digest": runtime_artifact_digest,
                    "runtime_worker_protocol": runtime_worker_protocol,
                },
            )
            value = self._run(conn, run_id)
            value["attempt"] = self._attempt(conn, attempt_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def claim_attempt_dispatch(
        self,
        *,
        run_id: str,
        attempt_id: str,
        dispatch_owner: str,
        lease_seconds: int = 300,
    ) -> JsonObject:
        """Reject the pre-v3 lease API, which lacks exact managed identity."""

        del run_id, attempt_id, dispatch_owner, lease_seconds
        raise InvalidTransition("dispatch_reservation_required", "dispatch_claimed")

    @staticmethod
    def transition_operation(run_id: str, target_state: str) -> str:
        """The receipt operation `transition_run` files under."""

        return f"POST:/api/v1/runs/{run_id}/transition/{target_state}"

    @staticmethod
    def transition_request(
        *,
        target_state: str,
        expected_revision: int,
        stage: str | None = None,
        payload: Mapping[str, Any] | None = None,
        attempt_id: str | None = None,
        runtime_binding_id: str | None = None,
        runtime_release_id: str | None = None,
        state_generation_id: str | None = None,
        adapter_event_sequence: int | None = None,
        caused_by_adapter_operation_id: str | None = None,
        caused_by_delivery_epoch: int | None = None,
    ) -> dict[str, Any]:
        """The request `transition_run` hashes its receipt over.

        ⟦P8 N-1⟧ The control API replays a cancel or pause before it refuses
        one (`replay_command`), which only works if the request it asks about
        is byte-identical to the one the command filed. Spelled once, here,
        so the two cannot drift.
        """

        return {
            "target_state": target_state,
            "expected_revision": expected_revision,
            "stage": stage,
            "payload": dict(payload or {}),
            "attempt_id": attempt_id,
            "runtime_binding_id": runtime_binding_id,
            "runtime_release_id": runtime_release_id,
            "state_generation_id": state_generation_id,
            "adapter_event_sequence": adapter_event_sequence,
            "caused_by_adapter_operation_id": caused_by_adapter_operation_id,
            "caused_by_delivery_epoch": caused_by_delivery_epoch,
        }

    def transition_run(
        self,
        *,
        run_id: str,
        target_state: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        stage: str | None = None,
        payload: Mapping[str, Any] | None = None,
        attempt_id: str | None = None,
        runtime_binding_id: str | None = None,
        runtime_release_id: str | None = None,
        state_generation_id: str | None = None,
        adapter_event_id: str | None = None,
        adapter_event_sequence: int | None = None,
        caused_by_adapter_operation_id: str | None = None,
        caused_by_delivery_epoch: int | None = None,
    ) -> CommandResult:
        request = self.transition_request(
            target_state=target_state,
            expected_revision=expected_revision,
            stage=stage,
            payload=payload,
            attempt_id=attempt_id,
            runtime_binding_id=runtime_binding_id,
            runtime_release_id=runtime_release_id,
            state_generation_id=state_generation_id,
            adapter_event_sequence=adapter_event_sequence,
            caused_by_adapter_operation_id=caused_by_adapter_operation_id,
            caused_by_delivery_epoch=caused_by_delivery_epoch,
        )
        operation = self.transition_operation(run_id, target_state)
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            inbox_replay = self._runtime_event_replay(
                conn,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                adapter_event_sequence=adapter_event_sequence,
                request={"operation": operation, **request},
            )
            if inbox_replay:
                return inbox_replay
            run = self._run(conn, run_id)
            if (
                target_state in {"pause_requested", "cancel_requested"}
                and actor_id != CAPTURE_CONSUMER_ACTOR
                and self._engine_owned_run(conn, run)
            ):
                # ⟦ADJ-4⟧ Before the revision is checked: no revision will
                # ever make this allowed, and the answer an operator needs is
                # the owner, not "refresh and retry". After the receipt: a
                # command that committed is replayed, never refused.
                raise MachineRunRefused(run_id, target_state)
            self._expect_revision(run, expected_revision)
            runtime_attempt: JsonObject | None = None
            if target_state not in {"pause_requested", "cancel_requested"}:
                if not all((attempt_id, runtime_binding_id, runtime_release_id)):
                    raise ValueError("runtime identity is required for this transition")
                runtime_attempt = self._validate_runtime_identity(
                    conn,
                    run=run,
                    attempt_id=str(attempt_id),
                    runtime_binding_id=str(runtime_binding_id),
                    runtime_release_id=str(runtime_release_id),
                    state_generation_id=state_generation_id,
                )
                self._confirm_runtime_resume_from_event(
                    conn,
                    run=run,
                    attempt_id=str(attempt_id),
                    adapter_event_id=adapter_event_id,
                    caused_by_adapter_operation_id=caused_by_adapter_operation_id,
                    caused_by_delivery_epoch=caused_by_delivery_epoch,
                )
            if target_state == "paused" and not (
                runtime_attempt and runtime_attempt["checkpoint_uri"]
            ):
                raise InvalidTransition("checkpoint_missing", "paused")
            source = str(run["state"])
            if target_state not in _RUN_TRANSITIONS.get(source, set()):
                raise InvalidTransition(source, target_state)
            now = self._now()
            active_attempt_id = (
                str(run["active_attempt_id"])
                if run.get("active_attempt_id") is not None
                else None
            )
            runtime_action_id: str | None = None
            runtime_action_kind = {
                "pause_requested": "control.pause",
                "cancel_requested": "control.cancel",
            }.get(target_state)
            if target_state == "cancel_requested":
                cancellation = self._prepare_run_cancellation(
                    conn,
                    run_id=run_id,
                    attempt_id=active_attempt_id,
                    now=now,
                )
                canceled_runtime_action_ids = cancellation[
                    "failed_runtime_action_ids"
                ]
                runtime_action_id = cancellation["runtime_action_id"]
            else:
                cancellation = None
                canceled_runtime_action_ids = []
            if runtime_action_kind == "control.pause":
                runtime_action_id = self._insert_control_runtime_action(
                    conn,
                    run_id=run_id,
                    attempt_id=active_attempt_id,
                    kind=runtime_action_kind,
                    now=now,
                )
            failed_action_ids = canceled_runtime_action_ids or (
                self._fail_pending_runtime_actions(
                    conn,
                    run_id=run_id,
                    attempt_id=active_attempt_id,
                    settled_at=now,
                )
                if (
                    target_state in _TERMINAL_RUN_STATES or target_state == "paused"
                )
                and active_attempt_id
                else []
            )
            if target_state == "paused" and active_attempt_id:
                self._fence_artifact_materializations_for_attempt(
                    conn,
                    run_id=run_id,
                    attempt_id=active_attempt_id,
                    settled_at=now,
                )
            closed_source_work = cancellation["closed_source_work"] if (
                cancellation is not None
            ) else (
                self._close_source_work_for_run(
                    conn,
                    run_id=run_id,
                    settled_at=now,
                )
                if target_state == "cancel_requested"
                or target_state in _TERMINAL_RUN_STATES
                else []
            )
            finished = (
                now
                if target_state in _TERMINAL_RUN_STATES or target_state == "paused"
                else None
            )
            conn.execute(
                """UPDATE runs
                   SET state = ?, stage = COALESCE(?, stage), revision = revision + 1,
                       updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (target_state, stage, now, run_id, expected_revision),
            )
            attempt_id = run.get("active_attempt_id")
            if attempt_id:
                conn.execute(
                    """UPDATE attempts
                       SET state = ?,
                           started_at = CASE WHEN ? = 'running'
                               THEN COALESCE(started_at, ?) ELSE started_at END,
                           finished_at = COALESCE(?, finished_at)
                       WHERE id = ?""",
                    (target_state, target_state, now, finished, attempt_id),
                )
            if target_state in _TERMINAL_RUN_STATES:
                conn.execute(
                    """UPDATE threads
                       SET status = ?, active_run_id = NULL,
                           revision = revision + 1, updated_at = ?
                       WHERE id = ? AND active_run_id = ?""",
                    (target_state, now, run["thread_id"], run_id),
                )
                expired_decisions = [
                    str(row["id"])
                    for row in conn.execute(
                        """SELECT id FROM decisions
                           WHERE run_id = ? AND state = 'pending'
                           ORDER BY created_at, id""",
                        (run_id,),
                    ).fetchall()
                ]
                if expired_decisions:
                    conn.execute(
                        """UPDATE decisions
                           SET state = 'expired', revision = revision + 1,
                               resolved_at = ?
                           WHERE run_id = ? AND state = 'pending'""",
                        (now, run_id),
                    )
            else:
                expired_decisions = []
            pin_release_action_id: str | None = None
            if target_state in _TERMINAL_RUN_STATES and active_attempt_id:
                terminal_attempt = self._attempt(conn, active_attempt_id)
                if (
                    terminal_attempt["runtime_release_id"] is not None
                    and terminal_attempt.get("state_generation_id") is not None
                ):
                    pin_release_action_id = self._insert_pin_release(
                        conn,
                        run_id=run_id,
                        attempt=terminal_attempt,
                        now=now,
                    )
            event_payload = {"from": source, "state": target_state}
            event_payload.update(dict(payload or {}))
            if failed_action_ids:
                event_payload["failed_runtime_action_ids"] = failed_action_ids
            if expired_decisions:
                event_payload["expired_decision_ids"] = expired_decisions
            if closed_source_work:
                event_payload["closed_source_work"] = closed_source_work
            if runtime_action_id is not None:
                event_payload["runtime_action_id"] = runtime_action_id
            if pin_release_action_id is not None:
                event_payload["pin_release_action_id"] = pin_release_action_id
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=str(attempt_id) if attempt_id else None,
                event_type=f"run.{target_state}",
                payload=event_payload,
                causation_id=adapter_event_id,
            )
            value = self._run(conn, run_id)
            result = self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )
            self._save_runtime_event(
                conn,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                adapter_event_sequence=adapter_event_sequence,
                request={"operation": operation, **request},
                result=result,
            )
            return result

    @staticmethod
    def _fail_pending_runtime_actions(
        conn: sqlite3.Connection,
        *,
        run_id: str,
        attempt_id: str,
        settled_at: str,
    ) -> list[str]:
        rows = conn.execute(
            """SELECT id FROM runtime_actions
               WHERE run_id = ? AND attempt_id = ? AND state = 'pending'
               ORDER BY created_at, id""",
            (run_id, attempt_id),
        ).fetchall()
        action_ids = [str(row["id"]) for row in rows]
        if action_ids:
            conn.execute(
                """UPDATE runtime_actions
                   SET state = 'failed', acknowledged_at = ?,
                       claim_owner = NULL, claim_expires_at = NULL,
                       failure_category = 'attempt_terminated'
                   WHERE run_id = ? AND attempt_id = ? AND state = 'pending'""",
                (settled_at, run_id, attempt_id),
            )
        return action_ids

    def _insert_control_runtime_action(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        attempt_id: str | None,
        kind: str,
        now: str,
    ) -> str | None:
        if attempt_id is None:
            return None
        attempt = self._attempt(conn, attempt_id)
        if (
            attempt["runtime_binding_id"] is None
            or attempt["runtime_release_id"] is None
        ):
            return None
        action_id = self._id_factory("runtime_action")
        conn.execute(
            """INSERT INTO runtime_actions
               (id, run_id, attempt_id, runtime_binding_id,
                runtime_release_id, kind, payload_json, state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                action_id,
                run_id,
                attempt_id,
                attempt["runtime_binding_id"],
                attempt["runtime_release_id"],
                kind,
                self._json({}),
                now,
            ),
        )
        return action_id

    def _prepare_run_cancellation(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        attempt_id: str | None,
        now: str,
    ) -> JsonObject:
        failed_runtime_action_ids = (
            self._fail_pending_runtime_actions(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                settled_at=now,
            )
            if attempt_id is not None
            else []
        )
        runtime_action_id = self._insert_control_runtime_action(
            conn,
            run_id=run_id,
            attempt_id=attempt_id,
            kind="control.cancel",
            now=now,
        )
        closed_source_work = self._close_source_work_for_run(
            conn,
            run_id=run_id,
            settled_at=now,
        )
        return {
            "failed_runtime_action_ids": failed_runtime_action_ids,
            "runtime_action_id": runtime_action_id,
            "closed_source_work": closed_source_work,
        }

    def _close_source_work_for_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        settled_at: str,
    ) -> JsonObject:
        rows = conn.execute(
            """SELECT id, decision_id FROM source_intents
               WHERE run_id = ? AND state = 'pending'
               ORDER BY created_at, id""",
            (run_id,),
        ).fetchall()
        intent_ids = [str(row["id"]) for row in rows]
        decision_ids = [
            str(row["decision_id"])
            for row in rows
            if row["decision_id"] is not None
        ]
        if intent_ids:
            conn.execute(
                """UPDATE source_intents
                   SET state = 'canceled', revision = revision + 1,
                       updated_at = ?
                   WHERE run_id = ? AND state = 'pending'""",
                (settled_at, run_id),
            )
        if decision_ids:
            placeholders = ",".join("?" for _ in decision_ids)
            conn.execute(
                f"""UPDATE decisions
                    SET state = 'expired', revision = revision + 1,
                        resolved_at = ?
                    WHERE id IN ({placeholders}) AND state = 'pending'""",
                (settled_at, *decision_ids),
            )
        import_action_ids = self._fence_source_imports_for_run(
            conn,
            run_id=run_id,
            settled_at=settled_at,
        )
        artifact_action_ids = self._fence_artifact_materializations_for_run(
            conn,
            run_id=run_id,
            settled_at=settled_at,
        )
        result = {
            "source_intent_ids": intent_ids,
            "source_decision_ids": decision_ids,
            "source_import_ids": import_action_ids,
            "artifact_materialization_ids": artifact_action_ids,
        }
        return result if any(result.values()) else {}

    @staticmethod
    def _fence_artifact_materializations_for_run(
        conn: sqlite3.Connection,
        *,
        run_id: str,
        settled_at: str,
    ) -> list[str]:
        rows = conn.execute(
            """SELECT artifact_materialization_actions.id,
                      artifact_materialization_actions.artifact_version_id
               FROM artifact_materialization_actions
               JOIN artifact_versions
                 ON artifact_versions.id =
                    artifact_materialization_actions.artifact_version_id
               WHERE artifact_versions.run_id = ?
                 AND artifact_versions.state = 'pending_materialization'
                 AND artifact_materialization_actions.state IN ('pending', 'claimed')
               ORDER BY artifact_materialization_actions.created_at,
                        artifact_materialization_actions.id""",
            (run_id,),
        ).fetchall()
        if not rows:
            return []
        action_ids = [str(row["id"]) for row in rows]
        version_ids = [str(row["artifact_version_id"]) for row in rows]
        action_placeholders = ",".join("?" for _ in action_ids)
        version_placeholders = ",".join("?" for _ in version_ids)
        conn.execute(
            f"""UPDATE artifact_materialization_actions
                SET state = 'failed', claim_owner = NULL,
                    claim_expires_at = NULL, claim_epoch = claim_epoch + 1,
                    failure_category = 'run_terminated', completed_at = ?
                WHERE id IN ({action_placeholders})
                  AND state IN ('pending', 'claimed')""",
            (settled_at, *action_ids),
        )
        conn.execute(
            f"""UPDATE artifact_versions SET state = 'failed'
                WHERE id IN ({version_placeholders})
                  AND state = 'pending_materialization'""",
            tuple(version_ids),
        )
        return action_ids

    @staticmethod
    def _fence_artifact_materializations_for_attempt(
        conn: sqlite3.Connection,
        *,
        run_id: str,
        attempt_id: str,
        settled_at: str,
    ) -> list[str]:
        rows = conn.execute(
            """SELECT artifact_materialization_actions.id,
                      artifact_materialization_actions.artifact_version_id
               FROM artifact_materialization_actions
               JOIN artifact_versions
                 ON artifact_versions.id =
                    artifact_materialization_actions.artifact_version_id
               WHERE artifact_versions.run_id = ?
                 AND artifact_versions.attempt_id = ?
                 AND artifact_versions.state = 'pending_materialization'
                 AND artifact_materialization_actions.state IN ('pending', 'claimed')
               ORDER BY artifact_materialization_actions.created_at,
                        artifact_materialization_actions.id""",
            (run_id, attempt_id),
        ).fetchall()
        if not rows:
            return []
        action_ids = [str(row["id"]) for row in rows]
        version_ids = [str(row["artifact_version_id"]) for row in rows]
        action_placeholders = ",".join("?" for _ in action_ids)
        version_placeholders = ",".join("?" for _ in version_ids)
        conn.execute(
            f"""UPDATE artifact_materialization_actions
                SET state = 'failed', claim_owner = NULL,
                    claim_expires_at = NULL, claim_epoch = claim_epoch + 1,
                    failure_category = 'attempt_superseded', completed_at = ?
                WHERE id IN ({action_placeholders})
                  AND state IN ('pending', 'claimed')""",
            (settled_at, *action_ids),
        )
        conn.execute(
            f"""UPDATE artifact_versions SET state = 'failed'
                WHERE id IN ({version_placeholders})
                  AND state = 'pending_materialization'""",
            tuple(version_ids),
        )
        return action_ids

    def _fence_source_imports_for_run(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        settled_at: str,
    ) -> list[str]:
        rows = conn.execute(
            """SELECT action_id FROM source_import_waiters
               WHERE run_id = ? AND state = 'waiting'
               ORDER BY created_at, id""",
            (run_id,),
        ).fetchall()
        action_ids = list(dict.fromkeys(str(row["action_id"]) for row in rows))
        if not action_ids:
            return []
        conn.execute(
            """UPDATE source_import_waiters
               SET state = 'canceled', completed_at = ?
               WHERE run_id = ? AND state = 'waiting'""",
            (settled_at, run_id),
        )
        for action_id in action_ids:
            replacement = self._first_live_source_import_waiter(
                conn,
                action_id=action_id,
                excluded_run_id=run_id,
            )
            if replacement is None:
                conn.execute(
                    """UPDATE source_import_actions
                       SET state = 'canceled', claim_owner = NULL,
                           claim_expires_at = NULL, claim_epoch = claim_epoch + 1,
                           completed_at = ?
                       WHERE id = ? AND state = 'pending'""",
                    (settled_at, action_id),
                )
                continue
            conn.execute(
                """UPDATE source_import_actions
                   SET run_id = ?, resolution_id = ?, claim_owner = NULL,
                       claim_expires_at = NULL, claim_epoch = claim_epoch + 1
                   WHERE id = ? AND state = 'pending'""",
                (
                    replacement["run_id"],
                    replacement["resolution_id"],
                    action_id,
                ),
            )
        return action_ids

    @staticmethod
    def _first_live_source_import_waiter(
        conn: sqlite3.Connection,
        *,
        action_id: str,
        excluded_run_id: str | None = None,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT source_import_waiters.*
               FROM source_import_waiters
               JOIN runs ON runs.id = source_import_waiters.run_id
               JOIN threads ON threads.id = runs.thread_id
               WHERE source_import_waiters.action_id = ?
                 AND source_import_waiters.state = 'waiting'
                 AND (? IS NULL OR source_import_waiters.run_id != ?)
                 AND runs.state NOT IN (
                     'cancel_requested', 'canceled', 'completed', 'failed'
                 )
                 AND threads.active_run_id = runs.id
               ORDER BY source_import_waiters.created_at,
                        source_import_waiters.id LIMIT 1""",
            (action_id, excluded_run_id, excluded_run_id),
        ).fetchone()

    def _confirm_runtime_resume_from_event(
        self,
        conn: sqlite3.Connection,
        *,
        run: JsonObject,
        attempt_id: str,
        adapter_event_id: str | None,
        caused_by_adapter_operation_id: str | None,
        caused_by_delivery_epoch: int | None,
    ) -> None:
        """Use an exact durable event as causal proof of decision acceptance."""

        if run["state"] != "resuming" or adapter_event_id is None:
            return
        rows = conn.execute(
            """SELECT id, claim_epoch FROM runtime_actions
               WHERE run_id = ? AND attempt_id = ?
                 AND kind = 'decision.resolve' AND state = 'pending'
               ORDER BY created_at, id""",
            (run["id"], attempt_id),
        ).fetchall()
        if len(rows) != 1:
            raise InvalidTransition(
                "runtime_resume_action_missing", "runtime_event_ingest"
            )
        action_id = str(rows[0]["id"])
        claim_epoch = int(rows[0]["claim_epoch"])
        if (
            claim_epoch < 1
            or caused_by_adapter_operation_id != f"runtime-action:{action_id}"
            or caused_by_delivery_epoch != claim_epoch
        ):
            raise InvalidTransition(
                "runtime_resume_causation_unverified", "runtime_event_ingest"
            )
        now = self._now()
        cursor = conn.execute(
            """UPDATE runtime_actions
               SET state = 'acked', acknowledged_at = ?, outcome_state = 'ready',
                   claim_owner = NULL, claim_expires_at = NULL,
                   failure_category = NULL
               WHERE id = ? AND state = 'pending'""",
            (now, action_id),
        )
        if cursor.rowcount != 1:
            raise InvalidTransition(
                "runtime_resume_action_race", "runtime_event_ingest"
            )
        conn.execute(
            "UPDATE runs SET state = 'running', updated_at = ? WHERE id = ?",
            (now, run["id"]),
        )
        conn.execute(
            "UPDATE attempts SET state = 'running' WHERE id = ?",
            (attempt_id,),
        )
        self._insert_event(
            conn,
            run_id=str(run["id"]),
            attempt_id=attempt_id,
            event_type="runtime.action.acked",
            payload={
                "runtime_action_id": action_id,
                "kind": "decision.resolve",
                "confirmation": "durable_runtime_event",
            },
            causation_id=adapter_event_id,
        )
        run["state"] = "running"

    def apply_runtime_transition(
        self,
        *,
        run_id: str,
        attempt_id: str,
        runtime_binding_id: str,
        runtime_release_id: str,
        target_state: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        adapter_event_id: str | None = None,
        adapter_event_sequence: int | None = None,
        caused_by_adapter_operation_id: str | None = None,
        caused_by_delivery_epoch: int | None = None,
        stage: str | None = None,
        payload: Mapping[str, Any] | None = None,
        state_generation_id: str | None = None,
    ) -> CommandResult:
        return self.transition_run(
            run_id=run_id,
            target_state=target_state,
            expected_revision=expected_revision,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            stage=stage,
            payload=payload,
            attempt_id=attempt_id,
            runtime_binding_id=runtime_binding_id,
            runtime_release_id=runtime_release_id,
            state_generation_id=state_generation_id,
            adapter_event_id=adapter_event_id,
            adapter_event_sequence=adapter_event_sequence,
            caused_by_adapter_operation_id=caused_by_adapter_operation_id,
            caused_by_delivery_epoch=caused_by_delivery_epoch,
        )

    def record_runtime_observation(
        self,
        *,
        run_id: str,
        attempt_id: str,
        runtime_binding_id: str,
        runtime_release_id: str,
        state_generation_id: str | None,
        event_type: str,
        payload: Mapping[str, Any],
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        adapter_event_id: str | None = None,
        adapter_event_sequence: int | None = None,
        caused_by_adapter_operation_id: str | None = None,
        caused_by_delivery_epoch: int | None = None,
    ) -> CommandResult:
        allowed_fields = {
            "runtime.tool.started": ("tool_call_id", "tool_name"),
            "runtime.tool.completed": (
                "tool_call_id",
                "tool_name",
                "is_error",
                "duration_ms",
            ),
            "runtime.session_rebound": (
                "runtime_session_ref",
                "generation",
                "parent_runtime_session_ref",
            ),
            # ⟦P9 / P9-3 C1⟧ A durable worker event that arrived after the
            # operator's stop request and was not applied: recorded so the
            # attempt's event sequence stays contiguous, carrying the event's
            # TYPE and never its body. The one observation a run in either
            # converging state takes -- `cancel_requested` and
            # `pause_requested` both, because a worker mid-answer emits
            # `runtime.tool.started` and `runtime.message.completed` into the
            # window either request opens, and the guard below rejecting them
            # is what ended a merely-paused turn `failed /
            # runtime_dispatch_failed` with a Retry button.
            "runtime.event.discarded": ("event_type",),
        }
        if event_type not in allowed_fields:
            raise ValueError("runtime observation type is invalid")
        sanitized = {
            field: payload[field]
            for field in allowed_fields[event_type]
            if field in payload
        }
        for field in ("tool_call_id", "tool_name"):
            if field in sanitized:
                sanitized[field] = self._required_text(
                    str(sanitized[field]), field, maximum=500
                )
        request = {
            "attempt_id": attempt_id,
            "runtime_binding_id": runtime_binding_id,
            "runtime_release_id": runtime_release_id,
            "state_generation_id": state_generation_id,
            "event_type": event_type,
            "payload": sanitized,
            "expected_revision": expected_revision,
            "adapter_event_sequence": adapter_event_sequence,
            "caused_by_adapter_operation_id": caused_by_adapter_operation_id,
            "caused_by_delivery_epoch": caused_by_delivery_epoch,
        }
        operation = f"POST:/internal/v1/runs/{run_id}/runtime-observations"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            inbox_replay = self._runtime_event_replay(
                conn,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                adapter_event_sequence=adapter_event_sequence,
                request={"operation": operation, **request},
            )
            if inbox_replay:
                return inbox_replay
            run = self._run(conn, run_id)
            self._expect_revision(run, expected_revision)
            self._validate_runtime_identity(
                conn,
                run=run,
                attempt_id=attempt_id,
                runtime_binding_id=runtime_binding_id,
                runtime_release_id=runtime_release_id,
                state_generation_id=state_generation_id,
            )
            self._confirm_runtime_resume_from_event(
                conn,
                run=run,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                caused_by_adapter_operation_id=caused_by_adapter_operation_id,
                caused_by_delivery_epoch=caused_by_delivery_epoch,
            )
            if run["state"] != "running" and not (
                event_type == "runtime.event.discarded"
                and run["state"] in {"cancel_requested", "pause_requested"}
            ):
                raise InvalidTransition(str(run["state"]), event_type)
            if event_type == "runtime.session_rebound":
                session_ref = self._required_text(
                    str(sanitized.get("runtime_session_ref", "")),
                    "runtime_session_ref",
                    maximum=1_000,
                )
                generation = sanitized.get("generation")
                if type(generation) is not int or generation < 1:
                    raise ValueError("runtime binding generation is invalid")
                binding = self._runtime_binding(conn, runtime_binding_id)
                if generation <= int(binding["generation"]):
                    raise InvalidTransition("stale_binding_generation", "rebound")
                conn.execute(
                    """UPDATE runtime_bindings
                       SET runtime_session_ref = ?, generation = ?, parent_ref = ?,
                           updated_at = ? WHERE id = ?""",
                    (
                        session_ref,
                        generation,
                        sanitized.get("parent_runtime_session_ref"),
                        self._now(),
                        runtime_binding_id,
                    ),
                )
            now = self._now()
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run_id, expected_revision),
            )
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type=event_type,
                payload=sanitized,
                causation_id=adapter_event_id,
            )
            value = self._run(conn, run_id)
            result = self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )
            self._save_runtime_event(
                conn,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                adapter_event_sequence=adapter_event_sequence,
                request={"operation": operation, **request},
                result=result,
            )
            return result

    def append_runtime_message(
        self,
        *,
        run_id: str,
        attempt_id: str,
        runtime_binding_id: str,
        runtime_release_id: str,
        state_generation_id: str | None,
        content: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        adapter_event_id: str | None = None,
        adapter_event_sequence: int | None = None,
        caused_by_adapter_operation_id: str | None = None,
        caused_by_delivery_epoch: int | None = None,
    ) -> CommandResult:
        content = self._required_text(content, "content", maximum=1_000_000)
        request = {
            "attempt_id": attempt_id,
            "runtime_binding_id": runtime_binding_id,
            "runtime_release_id": runtime_release_id,
            "state_generation_id": state_generation_id,
            "content": content,
            "expected_revision": expected_revision,
            "adapter_event_sequence": adapter_event_sequence,
            "caused_by_adapter_operation_id": caused_by_adapter_operation_id,
            "caused_by_delivery_epoch": caused_by_delivery_epoch,
        }
        operation = f"POST:/internal/v1/runs/{run_id}/assistant-message"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            inbox_replay = self._runtime_event_replay(
                conn,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                adapter_event_sequence=adapter_event_sequence,
                request={"operation": operation, **request},
            )
            if inbox_replay:
                return inbox_replay
            run = self._run(conn, run_id)
            self._expect_revision(run, expected_revision)
            self._validate_runtime_identity(
                conn,
                run=run,
                attempt_id=attempt_id,
                runtime_binding_id=runtime_binding_id,
                runtime_release_id=runtime_release_id,
                state_generation_id=state_generation_id,
            )
            self._confirm_runtime_resume_from_event(
                conn,
                run=run,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                caused_by_adapter_operation_id=caused_by_adapter_operation_id,
                caused_by_delivery_epoch=caused_by_delivery_epoch,
            )
            if run["state"] != "running":
                raise InvalidTransition(str(run["state"]), "message_completed")
            thread = self._thread(conn, str(run["thread_id"]))
            position = int(
                conn.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM messages WHERE thread_id = ?",
                    (thread["id"],),
                ).fetchone()[0]
            )
            message_id = self._id_factory("msg")
            now = self._now()
            conn.execute(
                """INSERT INTO messages
                   (id, thread_id, role, content, position, created_at)
                   VALUES (?, ?, 'assistant', ?, ?, ?)""",
                (message_id, thread["id"], content, position, now),
            )
            conn.execute(
                """UPDATE threads SET revision = revision + 1, updated_at = ?
                   WHERE id = ?""",
                (now, thread["id"]),
            )
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run_id, expected_revision),
            )
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type="runtime.message.completed",
                payload={"message_id": message_id, "role": "assistant"},
                causation_id=adapter_event_id,
            )
            value = self._run(conn, run_id)
            value["message"] = self._message(conn, message_id)
            result = self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )
            self._save_runtime_event(
                conn,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                adapter_event_sequence=adapter_event_sequence,
                request={"operation": operation, **request},
                result=result,
            )
            return result

    def fail_unbound_run(
        self,
        *,
        run_id: str,
        attempt_id: str,
        expected_revision: int,
        category: str,
        actor_id: str,
        idempotency_key: str,
        dispatch_owner: str | None = None,
        retryable: bool = True,
    ) -> CommandResult:
        category = self._required_text(category, "category", maximum=100)
        if type(retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        request = {
            "attempt_id": attempt_id,
            "expected_revision": expected_revision,
            "category": category,
            "dispatch_owner": dispatch_owner,
        }
        operation = f"POST:/internal/v1/runs/{run_id}/fail-unbound"
        if not retryable:
            request["retryable"] = False
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            run = self._run(conn, run_id)
            self._expect_revision(run, expected_revision)
            if run["active_attempt_id"] != attempt_id:
                raise InvalidTransition("attempt_mismatch", "failed")
            attempt = self._attempt(conn, attempt_id)
            if attempt["runtime_binding_id"] is not None:
                raise InvalidTransition("runtime_already_bound", "failed")
            if dispatch_owner is not None:
                dispatch_owner = self._required_text(
                    dispatch_owner, "dispatch_owner", maximum=200
                )
                if attempt.get("dispatch_owner") != dispatch_owner:
                    raise InvalidTransition("dispatch_owner_mismatch", "failed")
            if run["state"] not in {
                "queued",
                "retrying",
                "resuming",
                "cancel_requested",
            }:
                raise InvalidTransition(str(run["state"]), "failed")
            now = self._now()
            paused_source = self._paused_source_attempt(conn, attempt)
            target_state = (
                "canceled"
                if run["state"] == "cancel_requested"
                else "paused"
                if paused_source is not None
                else "failed"
            )
            target_attempt_id = (
                str(paused_source["id"])
                if target_state == "paused" and paused_source is not None
                else attempt_id
            )
            failed_runtime_action_ids = (
                self._fail_pending_runtime_actions(
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    settled_at=now,
                )
                if target_state in _TERMINAL_RUN_STATES
                else []
            )
            closed_source_work = (
                self._close_source_work_for_run(
                    conn,
                    run_id=run_id,
                    settled_at=now,
                )
                if target_state in _TERMINAL_RUN_STATES
                else {}
            )
            if target_state == "paused":
                self._fence_artifact_materializations_for_attempt(
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    settled_at=now,
                )
            conn.execute(
                """UPDATE runs SET state = ?, active_attempt_id = ?,
                   revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (target_state, target_attempt_id, now, run_id, expected_revision),
            )
            conn.execute(
                """UPDATE attempts SET state = 'failed', finished_at = ?,
                   dispatch_owner = NULL, dispatch_expires_at = NULL
                   WHERE id = ?""",
                (now, attempt_id),
            )
            if target_state in {"failed", "canceled"}:
                conn.execute(
                    """UPDATE threads SET status = ?, active_run_id = NULL,
                       revision = revision + 1, updated_at = ?
                       WHERE id = ? AND active_run_id = ?""",
                    (target_state, now, run["thread_id"], run_id),
                )
            else:
                conn.execute(
                    """UPDATE threads SET status = 'paused',
                       revision = revision + 1, updated_at = ?
                       WHERE id = ? AND active_run_id = ?""",
                    (now, run["thread_id"], run_id),
                )
            pin_release_id = None
            if (
                attempt.get("runtime_release_id") is not None
                and attempt.get("state_generation_id") is not None
            ):
                pin_release_id = self._insert_pin_release(
                    conn,
                    run_id=run_id,
                    attempt=attempt,
                    now=now,
                )
            source_pin_release_action_ids = (
                self._insert_paused_source_releases(
                    conn,
                    run_id=run_id,
                    attempt=attempt,
                    now=now,
                )
                if target_state == "canceled"
                else []
            )
            event_payload: JsonObject = {
                "from": run["state"],
                "state": target_state,
                "category": category,
                "retryable": retryable and target_state != "canceled",
                "pin_release_action_id": pin_release_id,
            }
            if source_pin_release_action_ids:
                event_payload["source_pin_release_action_ids"] = (
                    source_pin_release_action_ids
                )
            if failed_runtime_action_ids:
                event_payload["failed_runtime_action_ids"] = (
                    failed_runtime_action_ids
                )
            if closed_source_work:
                event_payload["closed_source_work"] = closed_source_work
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type=f"run.{target_state}",
                payload=event_payload,
            )
            value = self._run(conn, run_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def _validate_runtime_identity(
        self,
        conn: sqlite3.Connection,
        *,
        run: Mapping[str, Any],
        attempt_id: str,
        runtime_binding_id: str,
        runtime_release_id: str,
        state_generation_id: str | None = None,
    ) -> JsonObject:
        state_generation_id = self._required_text(
            state_generation_id, "state_generation_id", maximum=200
        )
        if run["active_attempt_id"] != attempt_id:
            raise InvalidTransition("attempt_mismatch", "runtime_result")
        attempt = self._attempt(conn, attempt_id)
        if attempt.get("runtime_identity_version") != 1 or not all(
            isinstance(attempt.get(field), str) and attempt.get(field)
            for field in (
                "runtime_release_id",
                "state_generation_id",
                "runtime_slot_id",
                "runtime_artifact_digest",
                "runtime_worker_protocol",
            )
        ):
            raise InvalidTransition("legacy_runtime_identity", "manual_recovery")
        if (
            attempt["run_id"] != run["id"]
            or attempt["runtime_binding_id"] != runtime_binding_id
            or attempt["runtime_release_id"] != runtime_release_id
            or attempt["state_generation_id"] != state_generation_id
        ):
            raise InvalidTransition("runtime_binding_mismatch", "runtime_result")
        return attempt

    def resume_run(
        self,
        *,
        run_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        return self._restart_attempt(
            run_id=run_id,
            expected_revision=expected_revision,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            source_state="paused",
            target_state="resuming",
            operation=f"POST:/api/v1/runs/{run_id}/resume",
            reason=None,
        )

    def retry_run(
        self,
        *,
        run_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        reason: str,
    ) -> CommandResult:
        reason = self._required_text(reason, "reason", maximum=2_000)
        return self._restart_attempt(
            run_id=run_id,
            expected_revision=expected_revision,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            source_state="failed",
            target_state="retrying",
            operation=f"POST:/api/v1/runs/{run_id}/retry",
            reason=reason,
        )

    def commit_checkpoint(
        self,
        *,
        run_id: str,
        attempt_id: str,
        runtime_binding_id: str,
        runtime_release_id: str,
        checkpoint_uri: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        adapter_event_id: str | None = None,
        adapter_event_sequence: int | None = None,
        state_generation_id: str | None = None,
        caused_by_adapter_operation_id: str | None = None,
        caused_by_delivery_epoch: int | None = None,
    ) -> CommandResult:
        checkpoint_uri = self._required_text(
            checkpoint_uri, "checkpoint_uri", maximum=4_000
        )
        parsed_checkpoint = parse_resource_uri(checkpoint_uri)
        if parsed_checkpoint.root != "artifacts":
            raise ValueError("checkpoint_uri must use the artifacts resource root")
        request = {
            "attempt_id": attempt_id,
            "runtime_binding_id": runtime_binding_id,
            "runtime_release_id": runtime_release_id,
            "state_generation_id": state_generation_id,
            "checkpoint_uri": checkpoint_uri,
            "expected_revision": expected_revision,
            "adapter_event_sequence": adapter_event_sequence,
            "caused_by_adapter_operation_id": caused_by_adapter_operation_id,
            "caused_by_delivery_epoch": caused_by_delivery_epoch,
        }
        operation = f"POST:/internal/v1/runs/{run_id}/checkpoint"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            inbox_replay = self._runtime_event_replay(
                conn,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                adapter_event_sequence=adapter_event_sequence,
                request={"operation": operation, **request},
            )
            if inbox_replay:
                return inbox_replay
            run = self._run(conn, run_id)
            self._expect_revision(run, expected_revision)
            attempt = self._validate_runtime_identity(
                conn,
                run=run,
                attempt_id=attempt_id,
                runtime_binding_id=runtime_binding_id,
                runtime_release_id=runtime_release_id,
                state_generation_id=state_generation_id,
            )
            self._confirm_runtime_resume_from_event(
                conn,
                run=run,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                caused_by_adapter_operation_id=caused_by_adapter_operation_id,
                caused_by_delivery_epoch=caused_by_delivery_epoch,
            )
            if run["state"] not in {"running", "pause_requested"}:
                raise InvalidTransition(str(run["state"]), "checkpointed")
            now = self._now()
            conn.execute(
                "UPDATE attempts SET checkpoint_uri = ? WHERE id = ?",
                (checkpoint_uri, attempt_id),
            )
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run_id, expected_revision),
            )
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type="checkpoint.committed",
                payload={"checkpoint_uri": checkpoint_uri},
                causation_id=adapter_event_id,
            )
            value = self._run(conn, run_id)
            value["attempt"] = {**attempt, "checkpoint_uri": checkpoint_uri}
            result = self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )
            self._save_runtime_event(
                conn,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                adapter_event_sequence=adapter_event_sequence,
                request={"operation": operation, **request},
                result=result,
            )
            return result

    def _restart_attempt(
        self,
        *,
        run_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        source_state: str,
        target_state: str,
        operation: str,
        reason: str | None,
    ) -> CommandResult:
        request = {"expected_revision": expected_revision, "reason": reason}
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            run = self._run(conn, run_id)
            self._expect_revision(run, expected_revision)
            if run["state"] != source_state:
                raise InvalidTransition(str(run["state"]), target_state)
            thread = self._thread(conn, str(run["thread_id"]))
            active_run_id = thread["active_run_id"]
            if active_run_id not in {None, run_id}:
                raise InvalidTransition("another_run_active", target_state)
            previous_attempt = self._attempt(conn, str(run["active_attempt_id"]))
            if source_state == "paused" and not previous_attempt["checkpoint_uri"]:
                raise InvalidTransition("checkpoint_missing", target_state)
            source_checkpoint_uri = (
                previous_attempt["checkpoint_uri"]
                or previous_attempt.get("source_checkpoint_uri")
            )
            number_row = conn.execute(
                "SELECT COALESCE(MAX(number), 0) AS maximum FROM attempts WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            number = int(number_row["maximum"]) + 1
            attempt_id = self._id_factory("attempt")
            now = self._now()
            failed_action_ids = self._fail_pending_runtime_actions(
                conn,
                run_id=run_id,
                attempt_id=str(previous_attempt["id"]),
                settled_at=now,
            )
            self._fence_artifact_materializations_for_attempt(
                conn,
                run_id=run_id,
                attempt_id=str(previous_attempt["id"]),
                settled_at=now,
            )
            conn.execute(
                """INSERT INTO attempts
                   (id, run_id, number, state, source_attempt_id,
                    source_checkpoint_uri)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    attempt_id,
                    run_id,
                    number,
                    target_state,
                    previous_attempt["id"],
                    source_checkpoint_uri,
                ),
            )
            conn.execute(
                """UPDATE runs SET state = ?, active_attempt_id = ?,
                   revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (target_state, attempt_id, now, run_id, expected_revision),
            )
            conn.execute(
                """UPDATE threads SET active_run_id = ?, status = 'running',
                   revision = revision + 1, updated_at = ? WHERE id = ?""",
                (run_id, now, thread["id"]),
            )
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type=f"run.{target_state}",
                payload={
                    "from": source_state,
                    "state": target_state,
                    "source_attempt_id": previous_attempt["id"],
                    "source_checkpoint_uri": source_checkpoint_uri,
                    "reason": reason,
                    "failed_runtime_action_ids": failed_action_ids,
                },
            )
            value = self._run(conn, run_id)
            value["attempt"] = self._attempt(conn, attempt_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def create_decision(
        self,
        *,
        run_id: str,
        attempt_id: str,
        runtime_binding_id: str,
        runtime_release_id: str,
        expected_revision: int,
        kind: str,
        prompt: str,
        options: Sequence[Mapping[str, Any]],
        actor_id: str,
        idempotency_key: str,
        adapter_event_id: str | None = None,
        adapter_event_sequence: int | None = None,
        state_generation_id: str | None = None,
        caused_by_adapter_operation_id: str | None = None,
        caused_by_delivery_epoch: int | None = None,
        runtime_decision_ref: str | None = None,
        runtime_decision_revision: int | None = None,
    ) -> CommandResult:
        kind = self._required_text(kind, "kind", maximum=100)
        prompt = self._public_decision_text(prompt, "prompt", maximum=20_000)
        normalized_options = self._validate_decision_options(options)
        if runtime_decision_ref is not None:
            runtime_decision_ref = self._required_text(
                runtime_decision_ref, "runtime_decision_ref", maximum=500
            )
        if runtime_decision_revision is not None and (
            type(runtime_decision_revision) is not int
            or runtime_decision_revision < 0
        ):
            raise ValueError("runtime_decision_revision is invalid")
        request = {
            "expected_revision": expected_revision,
            "attempt_id": attempt_id,
            "runtime_binding_id": runtime_binding_id,
            "runtime_release_id": runtime_release_id,
            "state_generation_id": state_generation_id,
            "runtime_decision_ref": runtime_decision_ref,
            "runtime_decision_revision": runtime_decision_revision,
            "kind": kind,
            "prompt": prompt,
            "options": normalized_options,
            "adapter_event_sequence": adapter_event_sequence,
            "caused_by_adapter_operation_id": caused_by_adapter_operation_id,
            "caused_by_delivery_epoch": caused_by_delivery_epoch,
        }
        operation = f"POST:/internal/v1/runs/{run_id}/decisions"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            inbox_replay = self._runtime_event_replay(
                conn,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                adapter_event_sequence=adapter_event_sequence,
                request={"operation": operation, **request},
            )
            if inbox_replay:
                return inbox_replay
            run = self._run(conn, run_id)
            self._expect_revision(run, expected_revision)
            self._validate_runtime_identity(
                conn,
                run=run,
                attempt_id=attempt_id,
                runtime_binding_id=runtime_binding_id,
                runtime_release_id=runtime_release_id,
                state_generation_id=state_generation_id,
            )
            self._confirm_runtime_resume_from_event(
                conn,
                run=run,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                caused_by_adapter_operation_id=caused_by_adapter_operation_id,
                caused_by_delivery_epoch=caused_by_delivery_epoch,
            )
            if run["state"] != "running":
                raise InvalidTransition(str(run["state"]), "waiting_for_decision")
            now = self._now()
            decision_id = self._id_factory("decision")
            conn.execute(
                """INSERT INTO decisions
                   (id, run_id, attempt_id, kind, prompt, options_json, state,
                    revision, created_at, runtime_decision_ref,
                    runtime_decision_revision)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
                (
                    decision_id,
                    run_id,
                    attempt_id,
                    kind,
                    prompt,
                    self._json(normalized_options),
                    now,
                    runtime_decision_ref,
                    runtime_decision_revision,
                ),
            )
            conn.execute(
                """UPDATE runs SET state = 'waiting_for_decision',
                   revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run_id, expected_revision),
            )
            conn.execute(
                "UPDATE attempts SET state = 'waiting_for_decision' WHERE id = ?",
                (attempt_id,),
            )
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type="decision.required",
                payload={
                    "decision_id": decision_id,
                    "kind": kind,
                    "prompt": prompt,
                    "options": normalized_options,
                },
                causation_id=adapter_event_id,
            )
            value = self._decision(conn, decision_id)
            result = self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )
            self._save_runtime_event(
                conn,
                attempt_id=attempt_id,
                adapter_event_id=adapter_event_id,
                adapter_event_sequence=adapter_event_sequence,
                request={"operation": operation, **request},
                result=result,
            )
            return result

    def resolve_decision(
        self,
        *,
        decision_id: str,
        choice: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        choice = self._required_text(choice, "choice", maximum=500)
        request = {"choice": choice, "expected_revision": expected_revision}
        operation = f"POST:/api/v1/decisions/{decision_id}/resolve"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            decision = self._decision(conn, decision_id)
            self._expect_revision(decision, expected_revision)
            workflow_ref_row = conn.execute(
                "SELECT * FROM workflow_decision_refs WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if workflow_ref_row is None and decision["state"] != "pending":
                raise RevisionConflict(decision)
            source_intent = conn.execute(
                "SELECT id FROM source_intents WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if source_intent is not None:
                raise InvalidTransition(
                    "source_decision_requires_source_resolver", "resolved"
                )
            allowed = {
                str(option.get("id"))
                for option in decision["options"]
                if isinstance(option, Mapping) and option.get("id") is not None
            }
            if choice not in allowed:
                raise ValueError("choice is not one of the decision options")
            if workflow_ref_row is not None:
                workflow_ref = self._row(workflow_ref_row)
                if workflow_ref["state"] != "pending":
                    raise RevisionConflict(decision)
                if decision["state"] == "resolved":
                    resolution = decision.get("resolution") or {}
                    if resolution.get("choice") != choice:
                        raise RevisionConflict(decision)
                elif decision["state"] != "pending":
                    raise RevisionConflict(decision)
                workflow = self._workflow_row(
                    conn, str(workflow_ref["workflow_id"])
                )
                stage = self._workflow_stage_row(
                    conn,
                    str(workflow_ref["workflow_id"]),
                    str(
                        conn.execute(
                            "SELECT stage_key FROM workflow_stage_instances WHERE id = ?",
                            (workflow_ref["stage_id"],),
                        ).fetchone()[0]
                    ),
                )
                run = self._run(conn, str(decision["run_id"]))
                if run["state"] != "waiting_for_decision":
                    raise InvalidTransition(str(run["state"]), "resuming")
                if stage["state"] != "waiting":
                    raise InvalidTransition(str(stage["state"]), "active")
                now = self._now()
                resolution = {"choice": choice, "actor_id": actor_id}
                resolved_revision = expected_revision
                if decision["state"] == "pending":
                    cursor = conn.execute(
                        """UPDATE decisions
                           SET state = 'resolved', resolution_json = ?,
                               revision = revision + 1, resolved_at = ?
                           WHERE id = ? AND revision = ? AND state = 'pending'""",
                        (
                            self._json(resolution),
                            now,
                            decision_id,
                            expected_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RevisionConflict(self._decision(conn, decision_id))
                    resolved_revision += 1
                conn.execute(
                    """UPDATE workflow_decision_refs
                       SET state = 'resolved', resolved_revision = ?,
                           selected_choice = ?, resolved_at = ?
                       WHERE id = ? AND state = 'pending'""",
                    (
                        resolved_revision,
                        choice,
                        now,
                        workflow_ref["id"],
                    ),
                )
                conn.execute(
                    """UPDATE workflow_stage_instances
                       SET state = 'active', revision = revision + 1
                       WHERE id = ? AND state = 'waiting'""",
                    (stage["id"],),
                )
                conn.execute(
                    """UPDATE workflow_instances
                       SET state = 'running', revision = revision + 1, updated_at = ?
                       WHERE id = ?""",
                    (now, workflow["id"]),
                )
                conn.execute(
                    """UPDATE runs SET state = 'resuming', revision = revision + 1,
                       updated_at = ? WHERE id = ?""",
                    (now, run["id"]),
                )
                conn.execute(
                    "UPDATE attempts SET state = 'resuming' WHERE id = ?",
                    (decision["attempt_id"],),
                )
                self._insert_event(
                    conn,
                    run_id=str(run["id"]),
                    attempt_id=str(decision["attempt_id"]),
                    event_type="decision.resolved",
                    payload={"decision_id": decision_id, "choice": choice},
                )
                return self._save_receipt(
                    conn,
                    actor_id,
                    operation,
                    idempotency_key,
                    request,
                    self._decision(conn, decision_id),
                    200,
                )
            if decision["state"] != "pending":
                raise RevisionConflict(decision)
            run = self._run(conn, str(decision["run_id"]))
            if run["state"] != "waiting_for_decision":
                raise InvalidTransition(str(run["state"]), "resuming")
            now = self._now()
            resolution = {"choice": choice, "actor_id": actor_id}
            attempt = self._attempt(conn, str(decision["attempt_id"]))
            if not attempt["runtime_binding_id"] or not attempt["runtime_release_id"]:
                raise InvalidTransition("runtime_not_bound", "decision_resolved")
            action_id = self._id_factory("runtime_action")
            runtime_identity = conn.execute(
                """SELECT runtime_decision_ref, runtime_decision_revision
                   FROM decisions WHERE id = ?""",
                (decision_id,),
            ).fetchone()
            conn.execute(
                """UPDATE decisions
                   SET state = 'resolved', resolution_json = ?, revision = revision + 1,
                       resolved_at = ? WHERE id = ? AND revision = ? AND state = 'pending'""",
                (self._json(resolution), now, decision_id, expected_revision),
            )
            conn.execute(
                """UPDATE runs SET state = 'resuming', revision = revision + 1,
                   updated_at = ? WHERE id = ? AND revision = ?""",
                (now, run["id"], run["revision"]),
            )
            conn.execute(
                "UPDATE attempts SET state = 'resuming' WHERE id = ?",
                (decision["attempt_id"],),
            )
            conn.execute(
                """INSERT INTO runtime_actions
                   (id, run_id, attempt_id, runtime_binding_id,
                    runtime_release_id, kind, payload_json, state, created_at)
                   VALUES (?, ?, ?, ?, ?, 'decision.resolve', ?, 'pending', ?)""",
                (
                    action_id,
                    run["id"],
                    decision["attempt_id"],
                    attempt["runtime_binding_id"],
                    attempt["runtime_release_id"],
                    self._json(
                        {
                            "decision_id": decision_id,
                            "runtime_decision_ref": runtime_identity[0],
                            "runtime_decision_revision": runtime_identity[1],
                            "choice": choice,
                        }
                    ),
                    now,
                ),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(decision["attempt_id"]),
                event_type="decision.resolved",
                payload={
                    "decision_id": decision_id,
                    "choice": choice,
                    "runtime_action_id": action_id,
                },
            )
            value = self._decision(conn, decision_id)
            value["runtime_action_id"] = action_id
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def list_pending_runtime_actions(self) -> list[JsonObject]:
        with self._connect() as conn:
            now = self._now()
            rows = conn.execute(
                """SELECT runtime_actions.id
                   FROM runtime_actions
                   JOIN runs ON runs.id = runtime_actions.run_id
                   WHERE runtime_actions.state = 'pending'
                     AND runtime_actions.outcome_state = 'ready'
                     AND runs.active_attempt_id = runtime_actions.attempt_id
                     AND (
                         (runtime_actions.kind = 'decision.resolve'
                          AND runs.state = 'resuming')
                         OR (runtime_actions.kind = 'control.pause'
                             AND runs.state = 'pause_requested')
                         OR (runtime_actions.kind = 'control.cancel'
                             AND runs.state = 'cancel_requested')
                     )
                     AND (runtime_actions.claim_owner IS NULL
                          OR runtime_actions.claim_expires_at <= ?)
                   ORDER BY runtime_actions.created_at, runtime_actions.id"""
                ,
                (now,),
            ).fetchall()
            return [self._runtime_action(conn, str(row["id"])) for row in rows]

    def list_runtime_actions_requiring_reconciliation(
        self, *, limit: int = 500
    ) -> list[JsonObject]:
        """Return unknown outcomes and expired reconciliation leases."""

        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            now = self._now()
            rows = conn.execute(
                """SELECT id FROM runtime_actions
                   WHERE state = 'pending'
                     AND (
                         outcome_state = 'outcome_unknown'
                         OR (outcome_state = 'reconcile'
                             AND claim_expires_at <= ?)
                     )
                   ORDER BY created_at, id LIMIT ?""",
                (now, limit),
            ).fetchall()
            return [self._runtime_action(conn, str(row["id"])) for row in rows]

    def claim_runtime_action(
        self,
        *,
        action_id: str,
        worker_id: str,
        lease_seconds: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 1 and 300")
        request = {"worker_id": worker_id, "lease_seconds": lease_seconds}
        operation = f"POST:/internal/v1/runtime-actions/{action_id}/claim"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                current = self._runtime_action(conn, action_id)
                if self._claim_replay_is_current(
                    current,
                    worker_id=worker_id,
                    claim_epoch=replay.value.get("claim_epoch"),
                ):
                    return replay
                raise InvalidTransition("stale_claim_receipt", "claimed")
            action = self._runtime_action(conn, action_id)
            if action["state"] != "pending" or action["outcome_state"] != "ready":
                raise InvalidTransition(str(action["state"]), "claimed")
            run = self._run(conn, str(action["run_id"]))
            if (
                run["active_attempt_id"] != action["attempt_id"]
                or (
                    (action["kind"] == "decision.resolve" and run["state"] != "resuming")
                    or (action["kind"] == "control.pause" and run["state"] != "pause_requested")
                    or (action["kind"] == "control.cancel" and run["state"] != "cancel_requested")
                )
            ):
                raise InvalidTransition(str(run["state"]), "claimed")
            current = self._utc_now()
            now = self._format_time(current)
            expires = self._format_time(current + timedelta(seconds=lease_seconds))
            cursor = conn.execute(
                """UPDATE runtime_actions
                   SET claim_owner = ?, claim_expires_at = ?,
                       claim_epoch = claim_epoch + 1
                   WHERE id = ? AND state = 'pending' AND outcome_state = 'ready'
                     AND (claim_owner IS NULL OR claim_expires_at <= ?)""",
                (worker_id, expires, action_id, now),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("already_claimed", "claimed")
            value = self._runtime_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def acknowledge_runtime_action(
        self,
        *,
        action_id: str,
        run_id: str,
        attempt_id: str,
        runtime_binding_id: str,
        runtime_release_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        state_generation_id: str | None = None,
        claim_owner: str | None = None,
        claim_epoch: int | None = None,
    ) -> CommandResult:
        claim_owner = self._required_text(
            claim_owner, "claim_owner", maximum=200
        )
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        request = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "runtime_binding_id": runtime_binding_id,
            "runtime_release_id": runtime_release_id,
            "state_generation_id": state_generation_id,
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "expected_revision": expected_revision,
        }
        operation = f"POST:/internal/v1/runtime-actions/{action_id}/ack"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._runtime_action(conn, action_id)
            if action["state"] != "pending":
                raise InvalidTransition(str(action["state"]), "acked")
            if (
                not self._claim_fence_is_current(
                    action,
                    worker_id=claim_owner,
                    claim_epoch=claim_epoch,
                )
                or action.get("outcome_state") not in {"ready", "reconcile"}
            ):
                raise InvalidTransition("claim_fence_mismatch", "acked")
            if (
                action["run_id"] != run_id
                or action["attempt_id"] != attempt_id
                or action["runtime_binding_id"] != runtime_binding_id
                or action["runtime_release_id"] != runtime_release_id
            ):
                raise InvalidTransition("runtime_binding_mismatch", "acked")
            run = self._run(conn, run_id)
            self._expect_revision(run, expected_revision)
            self._validate_runtime_identity(
                conn,
                run=run,
                attempt_id=attempt_id,
                runtime_binding_id=runtime_binding_id,
                runtime_release_id=runtime_release_id,
                state_generation_id=state_generation_id,
            )
            now = self._now()
            cursor = conn.execute(
                """UPDATE runtime_actions SET state = 'acked', acknowledged_at = ?,
                   claim_owner = NULL, claim_expires_at = NULL,
                   outcome_state = 'ready'
                   WHERE id = ? AND state = 'pending' AND claim_owner = ?
                     AND claim_epoch = ?
                     AND outcome_state IN ('ready', 'reconcile')""",
                (now, action_id, claim_owner, claim_epoch),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", "acked")
            if action["kind"] == "decision.resolve" and run["state"] == "resuming":
                conn.execute(
                    """UPDATE runs SET state = 'running', revision = revision + 1,
                       updated_at = ? WHERE id = ? AND revision = ?""",
                    (now, run_id, expected_revision),
                )
                conn.execute(
                    "UPDATE attempts SET state = 'running' WHERE id = ?",
                    (attempt_id,),
                )
            else:
                conn.execute(
                    """UPDATE runs SET revision = revision + 1, updated_at = ?
                       WHERE id = ? AND revision = ?""",
                    (now, run_id, expected_revision),
                )
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type="runtime.action.acked",
                payload={"runtime_action_id": action_id, "kind": action["kind"]},
            )
            value = self._runtime_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def fail_runtime_action(
        self,
        *,
        action_id: str,
        claim_owner: str,
        claim_epoch: int,
        expected_revision: int,
        category: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        category = self._required_text(category, "category", maximum=100)
        claim_owner = self._required_text(
            claim_owner, "claim_owner", maximum=200
        )
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        request = {
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "expected_revision": expected_revision,
            "category": category,
        }
        operation = f"POST:/internal/v1/runtime-actions/{action_id}/fail"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._runtime_action(conn, action_id)
            if (
                action["state"] != "pending"
                or not self._claim_fence_is_current(
                    action,
                    worker_id=claim_owner,
                    claim_epoch=claim_epoch,
                )
                or action.get("outcome_state") not in {"ready", "reconcile"}
            ):
                raise InvalidTransition("claim_fence_mismatch", "failed")
            run = self._run(conn, str(action["run_id"]))
            self._expect_revision(run, expected_revision)
            now = self._now()
            cursor = conn.execute(
                """UPDATE runtime_actions
                   SET state = 'failed', failure_category = ?, acknowledged_at = ?,
                       claim_owner = NULL, claim_expires_at = NULL,
                       outcome_state = 'ready'
                   WHERE id = ? AND state = 'pending' AND claim_owner = ?
                     AND claim_epoch = ?
                     AND outcome_state IN ('ready', 'reconcile')""",
                (category, now, action_id, claim_owner, claim_epoch),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", "failed")
            closed_source_work = self._close_source_work_for_run(
                conn,
                run_id=str(run["id"]),
                settled_at=now,
            )
            conn.execute(
                """UPDATE runs SET state = 'failed', revision = revision + 1,
                   updated_at = ? WHERE id = ? AND revision = ?""",
                (now, run["id"], expected_revision),
            )
            conn.execute(
                """UPDATE attempts SET state = 'failed', finished_at = ?
                   WHERE id = ?""",
                (now, action["attempt_id"]),
            )
            conn.execute(
                """UPDATE threads SET status = 'failed', active_run_id = NULL,
                   revision = revision + 1, updated_at = ?
                   WHERE id = ? AND active_run_id = ?""",
                (now, run["thread_id"], run["id"]),
            )
            attempt = self._attempt(conn, str(action["attempt_id"]))
            if (
                attempt.get("runtime_release_id") is not None
                and attempt.get("state_generation_id") is not None
            ):
                self._insert_pin_release(
                    conn,
                    run_id=str(run["id"]),
                    attempt=attempt,
                    now=now,
                )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(action["attempt_id"]),
                event_type="runtime.action.failed",
                payload={
                    "runtime_action_id": action_id,
                    "kind": action["kind"],
                    "category": category,
                    "closed_source_work": closed_source_work,
                },
            )
            value = self._runtime_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def reject_runtime_action(
        self,
        *,
        action_id: str,
        claim_owner: str,
        claim_epoch: int,
        expected_revision: int,
        category: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Settle a known rejection without pretending execution terminated."""

        category = self._required_text(category, "category", maximum=100)
        claim_owner = self._required_text(
            claim_owner, "claim_owner", maximum=200
        )
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        request = {
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "expected_revision": expected_revision,
            "category": category,
        }
        operation = f"POST:/internal/v1/runtime-actions/{action_id}/reject"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._runtime_action(conn, action_id)
            if (
                action["state"] != "pending"
                or not self._claim_fence_is_current(
                    action,
                    worker_id=claim_owner,
                    claim_epoch=claim_epoch,
                )
                or action.get("outcome_state") not in {"ready", "reconcile"}
            ):
                raise InvalidTransition("claim_fence_mismatch", "rejected")
            run = self._run(conn, str(action["run_id"]))
            self._expect_revision(run, expected_revision)
            now = self._now()
            cursor = conn.execute(
                """UPDATE runtime_actions
                   SET state = 'failed', failure_category = ?, acknowledged_at = ?,
                       claim_owner = NULL, claim_expires_at = NULL,
                       outcome_state = 'ready'
                   WHERE id = ? AND state = 'pending' AND claim_owner = ?
                     AND claim_epoch = ?
                     AND outcome_state IN ('ready', 'reconcile')""",
                (category, now, action_id, claim_owner, claim_epoch),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", "rejected")
            next_state = str(run["state"])
            if action["kind"] == "control.pause" and next_state == "pause_requested":
                next_state = "running"
                conn.execute(
                    "UPDATE attempts SET state = 'running' WHERE id = ?",
                    (action["attempt_id"],),
                )
            conn.execute(
                """UPDATE runs SET state = ?, revision = revision + 1,
                   updated_at = ? WHERE id = ? AND revision = ?""",
                (next_state, now, run["id"], expected_revision),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(action["attempt_id"]),
                event_type="runtime.action.rejected",
                payload={
                    "runtime_action_id": action_id,
                    "kind": action["kind"],
                    "category": category,
                },
            )
            value = self._runtime_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def mark_runtime_action_outcome_unknown(
        self,
        *,
        action_id: str,
        claim_owner: str,
        claim_epoch: int,
        expected_revision: int,
        category: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Fence an uncertain side effect for inspection instead of failing its run."""

        claim_owner = self._required_text(
            claim_owner, "claim_owner", maximum=200
        )
        category = self._required_text(category, "category", maximum=100)
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        request = {
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "expected_revision": expected_revision,
            "category": category,
        }
        operation = f"POST:/internal/v1/runtime-actions/{action_id}/outcome-unknown"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._runtime_action(conn, action_id)
            if (
                action["state"] != "pending"
                or action.get("outcome_state") != "ready"
                or not self._claim_fence_is_current(
                    action,
                    worker_id=claim_owner,
                    claim_epoch=claim_epoch,
                )
            ):
                raise InvalidTransition("claim_fence_mismatch", "outcome_unknown")
            run = self._run(conn, str(action["run_id"]))
            self._expect_revision(run, expected_revision)
            now = self._now()
            cursor = conn.execute(
                """UPDATE runtime_actions
                   SET outcome_state = 'outcome_unknown', failure_category = ?,
                       claim_owner = NULL, claim_expires_at = NULL
                   WHERE id = ? AND state = 'pending' AND outcome_state = 'ready'
                     AND claim_owner = ? AND claim_epoch = ?""",
                (category, action_id, claim_owner, claim_epoch),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", "outcome_unknown")
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run["id"], expected_revision),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(action["attempt_id"]),
                event_type="runtime.action.outcome_unknown",
                payload={
                    "runtime_action_id": action_id,
                    "kind": action["kind"],
                    "category": category,
                },
            )
            value = self._runtime_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def claim_runtime_action_reconciliation(
        self,
        *,
        action_id: str,
        worker_id: str,
        lease_seconds: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 1 and 300")
        request = {"worker_id": worker_id, "lease_seconds": lease_seconds}
        operation = f"POST:/internal/v1/runtime-actions/{action_id}/reconcile"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                current = self._runtime_action(conn, action_id)
                if (
                    current.get("outcome_state") == "reconcile"
                    and self._claim_replay_is_current(
                        current,
                        worker_id=worker_id,
                        claim_epoch=replay.value.get("claim_epoch"),
                    )
                ):
                    return replay
                raise InvalidTransition("stale_claim_receipt", "reconcile")
            action = self._runtime_action(conn, action_id)
            now = self._now()
            if (
                action["state"] != "pending"
                or (
                    action.get("outcome_state") != "outcome_unknown"
                    and not (
                        action.get("outcome_state") == "reconcile"
                        and action.get("claim_expires_at") is not None
                        and str(action["claim_expires_at"]) <= now
                    )
                )
            ):
                raise InvalidTransition(str(action.get("outcome_state")), "reconcile")
            current = self._utc_now()
            expires = self._format_time(current + timedelta(seconds=lease_seconds))
            cursor = conn.execute(
                """UPDATE runtime_actions
                   SET outcome_state = 'reconcile', claim_owner = ?,
                       claim_expires_at = ?, claim_epoch = claim_epoch + 1
                   WHERE id = ? AND state = 'pending'
                     AND (outcome_state = 'outcome_unknown'
                          OR (outcome_state = 'reconcile'
                              AND claim_expires_at <= ?))""",
                (worker_id, expires, action_id, now),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("already_claimed", "reconcile")
            value = self._runtime_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def defer_runtime_action_reconciliation(
        self,
        *,
        action_id: str,
        claim_owner: str,
        claim_epoch: int,
        expected_revision: int,
        category: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Release a reconciliation lease while preserving outcome uncertainty."""

        claim_owner = self._required_text(
            claim_owner, "claim_owner", maximum=200
        )
        category = self._required_text(category, "category", maximum=100)
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        request = {
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "expected_revision": expected_revision,
            "category": category,
        }
        operation = f"POST:/internal/v1/runtime-actions/{action_id}/defer-reconcile"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._runtime_action(conn, action_id)
            if (
                action["state"] != "pending"
                or action.get("outcome_state") != "reconcile"
                or not self._claim_fence_is_current(
                    action,
                    worker_id=claim_owner,
                    claim_epoch=claim_epoch,
                )
            ):
                raise InvalidTransition("claim_fence_mismatch", "outcome_unknown")
            run = self._run(conn, str(action["run_id"]))
            self._expect_revision(run, expected_revision)
            now = self._now()
            cursor = conn.execute(
                """UPDATE runtime_actions
                   SET outcome_state = 'outcome_unknown', failure_category = ?,
                       claim_owner = NULL, claim_expires_at = NULL
                   WHERE id = ? AND state = 'pending'
                     AND outcome_state = 'reconcile' AND claim_owner = ?
                     AND claim_epoch = ?""",
                (category, action_id, claim_owner, claim_epoch),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", "outcome_unknown")
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run["id"], expected_revision),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(action["attempt_id"]),
                event_type="runtime.action.reconciliation_deferred",
                payload={
                    "runtime_action_id": action_id,
                    "kind": action["kind"],
                    "category": category,
                },
            )
            value = self._runtime_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def retry_runtime_action_delivery(
        self,
        *,
        action_id: str,
        claim_owner: str,
        claim_epoch: int,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Return a reconciled-not-applied action to the normal delivery queue."""

        claim_owner = self._required_text(
            claim_owner, "claim_owner", maximum=200
        )
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        request = {
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "expected_revision": expected_revision,
        }
        operation = f"POST:/internal/v1/runtime-actions/{action_id}/retry-delivery"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._runtime_action(conn, action_id)
            if (
                action["state"] != "pending"
                or action.get("outcome_state") != "reconcile"
                or not self._claim_fence_is_current(
                    action,
                    worker_id=claim_owner,
                    claim_epoch=claim_epoch,
                )
            ):
                raise InvalidTransition("claim_fence_mismatch", "ready")
            run = self._run(conn, str(action["run_id"]))
            self._expect_revision(run, expected_revision)
            now = self._now()
            cursor = conn.execute(
                """UPDATE runtime_actions
                   SET outcome_state = 'ready', failure_category = NULL,
                       claim_owner = NULL, claim_expires_at = NULL
                   WHERE id = ? AND state = 'pending'
                     AND outcome_state = 'reconcile' AND claim_owner = ?
                     AND claim_epoch = ?""",
                (action_id, claim_owner, claim_epoch),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", "ready")
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run["id"], expected_revision),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(action["attempt_id"]),
                event_type="runtime.action.retry_scheduled",
                payload={"runtime_action_id": action_id, "kind": action["kind"]},
            )
            value = self._runtime_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def list_pending_pin_releases(self, *, limit: int = 500) -> list[JsonObject]:
        """Return exact pins that no active attempt still owns."""

        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT runtime_pin_releases.id
                   FROM runtime_pin_releases
                   JOIN runs ON runs.id = runtime_pin_releases.run_id
                   JOIN attempts ON attempts.id = runtime_pin_releases.attempt_id
                   WHERE runtime_pin_releases.state = 'pending'
                     AND (
                         runs.state IN ('completed', 'failed', 'canceled')
                         OR (
                             runtime_pin_releases.attempt_id
                                 <> runs.active_attempt_id
                             AND attempts.state IN (
                                 'paused', 'failed', 'completed', 'canceled'
                             )
                         )
                     )
                     AND (runtime_pin_releases.claim_owner IS NULL
                          OR runtime_pin_releases.claim_expires_at <= ?)
                   ORDER BY runtime_pin_releases.created_at,
                            runtime_pin_releases.id LIMIT ?""",
                (self._now(), limit),
            ).fetchall()
            return [self._pin_release(conn, str(row["id"])) for row in rows]

    def claim_pin_release(
        self,
        *,
        release_action_id: str,
        worker_id: str,
        lease_seconds: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 1 and 300")
        request = {"worker_id": worker_id, "lease_seconds": lease_seconds}
        operation = f"POST:/internal/v1/pin-releases/{release_action_id}/claim"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                current = self._pin_release(conn, release_action_id)
                if self._claim_replay_is_current(
                    current,
                    worker_id=worker_id,
                    claim_epoch=replay.value.get("claim_epoch"),
                ):
                    return replay
                raise InvalidTransition("stale_claim_receipt", "claimed")
            action = self._pin_release(conn, release_action_id)
            if action["state"] != "pending":
                raise InvalidTransition(str(action["state"]), "claimed")
            run = self._run(conn, str(action["run_id"]))
            release_attempt = self._attempt(conn, str(action["attempt_id"]))
            superseded = (
                run.get("active_attempt_id") != action["attempt_id"]
                and release_attempt["state"]
                in {"paused", "failed", "completed", "canceled"}
            )
            if run["state"] not in _TERMINAL_RUN_STATES and not superseded:
                raise InvalidTransition(str(run["state"]), "pin_release")
            current = self._utc_now()
            now = self._format_time(current)
            expires = self._format_time(current + timedelta(seconds=lease_seconds))
            cursor = conn.execute(
                """UPDATE runtime_pin_releases
                   SET claim_owner = ?, claim_expires_at = ?,
                       claim_epoch = claim_epoch + 1
                   WHERE id = ? AND state = 'pending'
                     AND (claim_owner IS NULL OR claim_expires_at <= ?)""",
                (worker_id, expires, release_action_id, now),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("already_claimed", "claimed")
            value = self._pin_release(conn, release_action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def acknowledge_pin_release(
        self,
        *,
        release_action_id: str,
        claim_owner: str,
        claim_epoch: int,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        return self._settle_pin_release(
            release_action_id=release_action_id,
            claim_owner=claim_owner,
            claim_epoch=claim_epoch,
            expected_revision=expected_revision,
            target_state="acked",
            category=None,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def fail_pin_release(
        self,
        *,
        release_action_id: str,
        claim_owner: str,
        claim_epoch: int,
        expected_revision: int,
        category: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        return self._settle_pin_release(
            release_action_id=release_action_id,
            claim_owner=claim_owner,
            claim_epoch=claim_epoch,
            expected_revision=expected_revision,
            target_state="failed",
            category=self._required_text(category, "category", maximum=100),
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def retry_pin_release(
        self,
        *,
        release_action_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        request = {"expected_revision": expected_revision}
        operation = f"POST:/internal/v1/pin-releases/{release_action_id}/retry"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._pin_release(conn, release_action_id)
            if action["state"] != "failed":
                raise InvalidTransition(str(action["state"]), "pending")
            run = self._run(conn, str(action["run_id"]))
            self._expect_revision(run, expected_revision)
            now = self._now()
            conn.execute(
                """UPDATE runtime_pin_releases
                   SET state = 'pending', failure_category = NULL,
                       acknowledged_at = NULL, claim_owner = NULL,
                       claim_expires_at = NULL
                   WHERE id = ? AND state = 'failed'""",
                (release_action_id,),
            )
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run["id"], expected_revision),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(action["attempt_id"]),
                event_type="runtime.pin_release.retried",
                payload={"pin_release_action_id": release_action_id},
            )
            value = self._pin_release(conn, release_action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def _settle_pin_release(
        self,
        *,
        release_action_id: str,
        claim_owner: str,
        claim_epoch: int,
        expected_revision: int,
        target_state: str,
        category: str | None,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        claim_owner = self._required_text(
            claim_owner, "claim_owner", maximum=200
        )
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        request = {
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "expected_revision": expected_revision,
            "category": category,
        }
        operation = (
            f"POST:/internal/v1/pin-releases/{release_action_id}/{target_state}"
        )
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._pin_release(conn, release_action_id)
            if (
                action["state"] != "pending"
                or not self._claim_fence_is_current(
                    action,
                    worker_id=claim_owner,
                    claim_epoch=claim_epoch,
                )
            ):
                raise InvalidTransition("claim_fence_mismatch", target_state)
            run = self._run(conn, str(action["run_id"]))
            self._expect_revision(run, expected_revision)
            now = self._now()
            cursor = conn.execute(
                """UPDATE runtime_pin_releases
                   SET state = ?, failure_category = ?, acknowledged_at = ?,
                       claim_owner = NULL, claim_expires_at = NULL
                   WHERE id = ? AND state = 'pending' AND claim_owner = ?
                     AND claim_epoch = ?""",
                (
                    target_state,
                    category,
                    now,
                    release_action_id,
                    claim_owner,
                    claim_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", target_state)
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run["id"], expected_revision),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(action["attempt_id"]),
                event_type=f"runtime.pin_release.{target_state}",
                payload={
                    "pin_release_action_id": release_action_id,
                    "category": category,
                },
            )
            value = self._pin_release(conn, release_action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def schedule_runtime_recovery(
        self,
        *,
        run_id: str,
        attempt_id: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        """Commit a recovery command and replayable event before inspection."""

        request = {
            "attempt_id": attempt_id,
            "expected_revision": expected_revision,
        }
        operation = f"POST:/internal/v1/runs/{run_id}/schedule-recovery"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            run = self._run(conn, run_id)
            self._expect_revision(run, expected_revision)
            if run["state"] in _TERMINAL_RUN_STATES or run["state"] == "paused":
                raise InvalidTransition(str(run["state"]), "recovery_queued")
            if run["active_attempt_id"] != attempt_id:
                raise InvalidTransition("attempt_mismatch", "recovery_queued")
            attempt = self._attempt(conn, attempt_id)
            existing = conn.execute(
                """SELECT id FROM runtime_recovery_commands
                   WHERE attempt_id = ? AND state IN ('pending', 'manual_required')
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                value = self._recovery_command(conn, str(existing["id"]))
                return self._save_receipt(
                    conn,
                    actor_id,
                    operation,
                    idempotency_key,
                    request,
                    value,
                    200,
                )
            binding_id = attempt.get("runtime_binding_id")
            release_id = attempt.get("runtime_release_id")
            generation_id = attempt.get("state_generation_id")
            slot_id = attempt.get("runtime_slot_id")
            artifact_digest = attempt.get("runtime_artifact_digest")
            worker_protocol = attempt.get("runtime_worker_protocol")
            identity_complete = bool(
                attempt.get("runtime_identity_version") == 1
                and all(
                    isinstance(value, str) and value
                    for value in (
                        release_id,
                        generation_id,
                        slot_id,
                        artifact_digest,
                        worker_protocol,
                    )
                )
            )
            has_partial_identity = any(
                value is not None
                for value in (
                    release_id,
                    generation_id,
                    slot_id,
                    artifact_digest,
                    worker_protocol,
                )
            )
            if binding_id is not None and not identity_complete:
                kind = "manual_recovery"
                command_state = "manual_required"
            elif binding_id is not None:
                kind = "inspect_runtime"
                command_state = "pending"
            elif identity_complete:
                kind = "resume_dispatch"
                command_state = "pending"
            elif has_partial_identity:
                kind = "manual_recovery"
                command_state = "manual_required"
            else:
                kind = "dispatch"
                command_state = "pending"
            now = self._now()
            command_id = self._id_factory("recovery")
            conn.execute(
                """INSERT INTO runtime_recovery_commands
                   (id, run_id, attempt_id, runtime_binding_id,
                    runtime_release_id, state_generation_id, kind, state,
                    created_at, completed_at, runtime_slot_id,
                    runtime_artifact_digest, runtime_worker_protocol,
                    runtime_identity_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    command_id,
                    run_id,
                    attempt_id,
                    binding_id,
                    release_id,
                    generation_id,
                    kind,
                    command_state,
                    now,
                    now if command_state == "manual_required" else None,
                    slot_id,
                    artifact_digest,
                    worker_protocol,
                    1 if identity_complete else 0,
                ),
            )
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run_id, expected_revision),
            )
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type=(
                    "runtime.recovery.manual_required"
                    if command_state == "manual_required"
                    else "runtime.recovery.queued"
                ),
                payload={
                    "recovery_command_id": command_id,
                    "kind": kind,
                    "state": command_state,
                },
            )
            value = self._recovery_command(conn, command_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def list_pending_runtime_recoveries(
        self, *, limit: int = 500
    ) -> list[JsonObject]:
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id FROM runtime_recovery_commands
                   WHERE state = 'pending'
                     AND (claim_owner IS NULL OR claim_expires_at <= ?)
                   ORDER BY created_at, id LIMIT ?""",
                (self._now(), limit),
            ).fetchall()
            return [self._recovery_command(conn, str(row["id"])) for row in rows]

    def claim_runtime_recovery(
        self,
        *,
        recovery_command_id: str,
        worker_id: str,
        lease_seconds: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 1 and 300")
        request = {"worker_id": worker_id, "lease_seconds": lease_seconds}
        operation = f"POST:/internal/v1/recoveries/{recovery_command_id}/claim"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                current = self._recovery_command(conn, recovery_command_id)
                if self._claim_replay_is_current(
                    current,
                    worker_id=worker_id,
                    claim_epoch=replay.value.get("claim_epoch"),
                ):
                    return replay
                raise InvalidTransition("stale_claim_receipt", "claimed")
            command = self._recovery_command(conn, recovery_command_id)
            if command["state"] != "pending":
                raise InvalidTransition(str(command["state"]), "claimed")
            current = self._utc_now()
            now = self._format_time(current)
            expires = self._format_time(current + timedelta(seconds=lease_seconds))
            cursor = conn.execute(
                """UPDATE runtime_recovery_commands
                   SET claim_owner = ?, claim_expires_at = ?,
                       claim_epoch = claim_epoch + 1
                   WHERE id = ? AND state = 'pending'
                     AND (claim_owner IS NULL OR claim_expires_at <= ?)""",
                (worker_id, expires, recovery_command_id, now),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("already_claimed", "claimed")
            value = self._recovery_command(conn, recovery_command_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def record_runtime_recovery_outcome(
        self,
        *,
        recovery_command_id: str,
        claim_owner: str,
        claim_epoch: int,
        outcome: str,
        expected_revision: int,
        details: Mapping[str, Any] | None,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        claim_owner = self._required_text(
            claim_owner, "claim_owner", maximum=200
        )
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        if outcome not in _RECOVERY_OUTCOMES:
            raise ValueError("recovery outcome is invalid")
        result = self._validate_recovery_details(details or {})
        request = {
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "outcome": outcome,
            "expected_revision": expected_revision,
            "details": result,
        }
        operation = f"POST:/internal/v1/recoveries/{recovery_command_id}/outcome"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            command = self._recovery_command(conn, recovery_command_id)
            if (
                command["state"] != "pending"
                or not self._claim_fence_is_current(
                    command,
                    worker_id=claim_owner,
                    claim_epoch=claim_epoch,
                )
            ):
                raise InvalidTransition("claim_fence_mismatch", outcome)
            run = self._run(conn, str(command["run_id"]))
            self._expect_revision(run, expected_revision)
            target_state = {
                "manual_required": "manual_required",
                "retryable_failed": "failed",
            }.get(outcome, "completed")
            now = self._now()
            cursor = conn.execute(
                """UPDATE runtime_recovery_commands
                   SET state = ?, result_json = ?, completed_at = ?,
                       claim_owner = NULL, claim_expires_at = NULL
                   WHERE id = ? AND state = 'pending' AND claim_owner = ?
                     AND claim_epoch = ?""",
                (
                    target_state,
                    self._json(result),
                    now,
                    recovery_command_id,
                    claim_owner,
                    claim_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", outcome)
            conn.execute(
                """UPDATE runs SET revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (now, run["id"], expected_revision),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(command["attempt_id"]),
                event_type=f"runtime.recovery.{outcome}",
                payload={
                    "recovery_command_id": recovery_command_id,
                    "outcome": outcome,
                    **result,
                },
            )
            value = self._recovery_command(conn, recovery_command_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def register_source(
        self,
        *,
        authority: str,
        authority_id: str,
        source_kind: str,
        official_title: str,
        engine_ref: str,
        aliases: Sequence[Mapping[str, Any]] = (),
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        authority, authority_id, canonical_id = self._canonical_source_identity(
            authority, authority_id
        )
        source_kind = self._required_text(source_kind, "source_kind", maximum=100)
        official_title = normalize_source_text(
            official_title, "official_title", maximum=2_000
        )
        engine_ref = self._validate_engine_ref(engine_ref)
        normalized_aliases = self._normalize_source_aliases(aliases)
        request = {
            "authority": authority,
            "authority_id": authority_id,
            "source_kind": source_kind,
            "official_title": official_title,
            "engine_ref": engine_ref,
            "aliases": normalized_aliases,
        }
        operation = "POST:/internal/v1/sources/register"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            existing = conn.execute(
                "SELECT id FROM sources WHERE canonical_id = ?", (canonical_id,)
            ).fetchone()
            if existing is not None:
                value = self._source(conn, str(existing["id"]))
                expected = {
                    "authority": authority,
                    "authority_id": authority_id,
                    "source_kind": source_kind,
                    "official_title": official_title,
                    "engine_ref": engine_ref,
                }
                if any(value.get(key) != item for key, item in expected.items()):
                    raise InvalidTransition("canonical_source_collision", "registered")
                self._insert_source_aliases(
                    conn,
                    source_id=str(value["id"]),
                    aliases=normalized_aliases,
                    now=self._now(),
                )
                value = self._source(conn, str(existing["id"]))
            else:
                source_id = self._insert_source(
                    conn,
                    authority=authority,
                    authority_id=authority_id,
                    source_kind=source_kind,
                    official_title=official_title,
                    engine_ref=engine_ref,
                    import_state="existing",
                    aliases=normalized_aliases,
                    now=self._now(),
                )
                value = self._source(conn, source_id)
                self._audit(
                    conn,
                    "source",
                    source_id,
                    "source.registered",
                    {"canonical_id": canonical_id, "import_state": "existing"},
                )
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def create_source_intent(
        self,
        *,
        run_id: str,
        attempt_id: str,
        title: str | None,
        locator: str | None,
        candidates: Sequence[Mapping[str, Any]],
        actor_id: str,
        idempotency_key: str,
        locator_sha256: str | None = None,
    ) -> CommandResult:
        title = (
            normalize_source_text(title, "title", maximum=2_000)
            if title is not None
            else None
        )
        locator, locator_identity, locator_sha256 = self._normalize_locator_claim(
            locator, locator_sha256=locator_sha256
        )
        if title is None and locator is None:
            raise ValueError("title or locator is required")
        request = {
            "attempt_id": attempt_id,
            "title": title,
            "locator": locator,
            "locator_sha256": locator_sha256,
        }
        operation = f"POST:/api/v1/runs/{run_id}/source-intents"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            normalized_candidates = self._normalize_source_candidates(candidates)
            if not normalized_candidates:
                raise ValueError("resolver returned no source candidates")
            self._validate_source_observation_coverage(
                title=title,
                locator_identity=locator_identity,
                candidates=normalized_candidates,
            )
            run = self._run(conn, run_id)
            attempt = self._attempt(conn, attempt_id)
            if run.get("active_attempt_id") != attempt_id or attempt["run_id"] != run_id:
                raise InvalidTransition("attempt_not_active", "source_resolution")
            if run["state"] not in {"queued", "starting", "running"}:
                raise InvalidTransition(str(run["state"]), "waiting_for_decision")
            if attempt["state"] not in {"queued", "starting", "running"}:
                raise InvalidTransition(str(attempt["state"]), "waiting_for_decision")
            now = self._now()
            intent_id = self._id_factory("source_intent")
            conn.execute(
                """INSERT INTO source_intents
                   (id, run_id, attempt_id, title_claim, locator_claim,
                    locator_claim_kind, locator_canonical_id, locator_version,
                    locator_sha256,
                    resume_run_state, resume_attempt_state, state, revision,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
                (
                    intent_id,
                    run_id,
                    attempt_id,
                    title,
                    locator,
                    locator_identity.claim_kind if locator_identity else None,
                    locator_identity.canonical_id if locator_identity else None,
                    locator_identity.version if locator_identity else None,
                    locator_sha256,
                    run["state"],
                    attempt["state"],
                    now,
                    now,
                ),
            )
            candidate_ids: list[str] = []
            source_ids: list[str | None] = []
            canonical_ids: list[str] = []
            for candidate in normalized_candidates:
                candidate_id = self._id_factory("source_candidate")
                existing = conn.execute(
                    "SELECT id FROM sources WHERE canonical_id = ?",
                    (candidate["canonical_id"],),
                ).fetchone()
                source_id = str(existing["id"]) if existing is not None else None
                conn.execute(
                    """INSERT INTO source_candidates
                       (id, intent_id, claim_kind, authority, authority_id,
                        canonical_id, official_title, locator, evidence_json,
                        version, source_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        candidate_id,
                        intent_id,
                        candidate["claim_kind"],
                        candidate["authority"],
                        candidate["authority_id"],
                        candidate["canonical_id"],
                        candidate["official_title"],
                        candidate.get("locator"),
                        self._json(candidate.get("evidence", {})),
                        candidate.get("version"),
                        source_id,
                        now,
                    ),
                )
                candidate_ids.append(candidate_id)
                source_ids.append(source_id)
                canonical_ids.append(str(candidate["canonical_id"]))
            distinct_canonical_ids = list(dict.fromkeys(canonical_ids))
            decision_id = self._id_factory("decision")
            conflict_detected = len(distinct_canonical_ids) > 1
            if conflict_detected:
                decision_kind = "source_conflict"
                prompt = (
                    "The supplied source claims resolve to different canonical works."
                )
                options = [
                    {"id": "keep_both", "label": "Keep both sources"},
                    {
                        "id": "replace_url_with_echo",
                        "label": "Replace URL with the title source",
                    },
                    {"id": "cancel", "label": "Cancel"},
                ]
            else:
                decision_kind = "source_confirmation"
                prompt = "Confirm the authority-backed source identity."
                options = [
                    {"id": "use_source", "label": "Use this source"},
                    {"id": "cancel", "label": "Cancel"},
                ]
            conn.execute(
                """INSERT INTO decisions
                   (id, run_id, attempt_id, kind, prompt, options_json, state,
                    revision, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (
                    decision_id,
                    run_id,
                    attempt_id,
                    decision_kind,
                    prompt,
                    self._json(options),
                    now,
                ),
            )
            conn.execute(
                "UPDATE source_intents SET decision_id = ? WHERE id = ?",
                (decision_id, intent_id),
            )
            conn.execute(
                """UPDATE runs SET state = 'waiting_for_decision',
                   revision = revision + 1, updated_at = ? WHERE id = ?""",
                (now, run_id),
            )
            conn.execute(
                "UPDATE attempts SET state = 'waiting_for_decision' WHERE id = ?",
                (attempt_id,),
            )
            conn.execute(
                """UPDATE threads SET status = 'waiting_for_decision',
                   revision = revision + 1, updated_at = ?
                   WHERE id = ? AND active_run_id = ?""",
                (now, run["thread_id"], run_id),
            )
            received = self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type="source.intent_received",
                payload={
                    "source_intent_id": intent_id,
                    "canonical_ids": canonical_ids,
                    "locator_identity": (
                        {
                            "claim_kind": locator_identity.claim_kind,
                            "canonical_id": locator_identity.canonical_id,
                            "version": locator_identity.version,
                        }
                        if locator_identity
                        else None
                    ),
                },
            )
            causation_id = received["id"]
            if conflict_detected:
                conflict = self._insert_event(
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    event_type="source.conflict_detected",
                    payload={
                        "source_intent_id": intent_id,
                        "candidate_ids": candidate_ids,
                        "canonical_ids": distinct_canonical_ids,
                    },
                    causation_id=received["id"],
                )
                causation_id = conflict["id"]
            self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type="decision.required",
                payload={
                    "decision_id": decision_id,
                    "kind": decision_kind,
                    "options": options,
                },
                causation_id=causation_id,
            )
            self._audit(
                conn,
                "source_intent",
                intent_id,
                "source.intent_received",
                {"candidate_ids": candidate_ids, "source_ids": source_ids},
            )
            value = self._source_intent(conn, intent_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def replay_source_intent(
        self,
        *,
        run_id: str,
        attempt_id: str,
        title: str | None,
        locator: str | None,
        actor_id: str,
        idempotency_key: str,
        locator_sha256: str | None = None,
    ) -> CommandResult | None:
        """Return a prior user-claim receipt without invoking a resolver."""

        title = (
            normalize_source_text(title, "title", maximum=2_000)
            if title is not None
            else None
        )
        locator, _, locator_sha256 = self._normalize_locator_claim(
            locator, locator_sha256=locator_sha256
        )
        if title is None and locator is None:
            raise ValueError("title or locator is required")
        request = {
            "attempt_id": attempt_id,
            "title": title,
            "locator": locator,
            "locator_sha256": locator_sha256,
        }
        operation = f"POST:/api/v1/runs/{run_id}/source-intents"
        with self._connect() as conn:
            return self._receipt(
                conn, actor_id, operation, idempotency_key, request
            )

    def resolve_source_intent(
        self,
        *,
        intent_id: str,
        choice: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        if choice not in {
            "use_source",
            "keep_both",
            "replace_url_with_echo",
            "cancel",
        }:
            raise ValueError("source resolution choice is invalid")
        request = {"choice": choice, "expected_revision": expected_revision}
        operation = f"POST:/api/v1/source-intents/{intent_id}/resolve"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            intent = self._source_intent(conn, intent_id)
            self._expect_revision(intent, expected_revision)
            if intent["state"] != "pending":
                raise RevisionConflict(intent)
            decision = intent.get("decision")
            if not isinstance(decision, Mapping) or decision.get("state") != "pending":
                raise InvalidTransition("decision_not_pending", "source_resolution")
            run = self._run(conn, str(intent["run_id"]))
            attempt = self._attempt(conn, str(intent["attempt_id"]))
            thread = self._thread(conn, str(run["thread_id"]))
            if run["state"] != "waiting_for_decision":
                raise InvalidTransition(str(run["state"]), "source_resolution")
            if (
                run.get("active_attempt_id") != intent["attempt_id"]
                or attempt["run_id"] != intent["run_id"]
                or attempt["state"] != "waiting_for_decision"
                or thread.get("active_run_id") != intent["run_id"]
                or thread["status"] != "waiting_for_decision"
                or decision.get("run_id") != intent["run_id"]
                or decision.get("attempt_id") != intent["attempt_id"]
                or decision.get("kind")
                not in {"source_conflict", "source_confirmation"}
            ):
                raise InvalidTransition("source_intent_owner_mismatch", "source_resolution")
            candidate_rows = conn.execute(
                """SELECT * FROM source_candidates WHERE intent_id = ?
                   ORDER BY created_at, id""",
                (intent_id,),
            ).fetchall()
            allowed_choices = {
                str(option["id"])
                for option in decision["options"]
                if isinstance(option, Mapping) and "id" in option
            }
            if choice not in allowed_choices:
                raise ValueError("choice is not one of the decision options")
            if choice in {"use_source", "keep_both"}:
                selected_rows = candidate_rows
            elif choice == "replace_url_with_echo":
                selected_rows = [
                    row for row in candidate_rows if row["claim_kind"] == "title"
                ]
                if len(selected_rows) != 1:
                    raise InvalidTransition("title_identity_ambiguous", "resolved")
            else:
                selected_rows = []
            selected: list[sqlite3.Row] = []
            seen: set[str] = set()
            for row in selected_rows:
                canonical_id = str(row["canonical_id"])
                if canonical_id not in seen:
                    selected.append(row)
                    seen.add(canonical_id)
            now = self._now()
            resolution_id = self._id_factory("source_resolution")
            selected_candidate_ids = [str(row["id"]) for row in selected]
            resolution_hash = self._request_hash(
                {
                    "intent_id": intent_id,
                    "decision_id": decision["id"],
                    "choice": choice,
                    "selected_candidate_ids": selected_candidate_ids,
                }
            )
            conn.execute(
                """INSERT INTO source_resolutions
                   (id, intent_id, decision_id, choice,
                    selected_candidate_ids_json, request_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    resolution_id,
                    intent_id,
                    decision["id"],
                    choice,
                    self._json(selected_candidate_ids),
                    resolution_hash,
                    now,
                ),
            )
            conn.execute(
                """UPDATE decisions SET state = 'resolved', resolution_json = ?,
                   revision = revision + 1, resolved_at = ?
                   WHERE id = ? AND revision = ? AND state = 'pending'""",
                (
                    self._json({"choice": choice, "actor_id": actor_id}),
                    now,
                    decision["id"],
                    decision["revision"],
                ),
            )
            target_state = "canceled" if choice == "cancel" else "resolved"
            conn.execute(
                """UPDATE source_intents SET state = ?, revision = revision + 1,
                   updated_at = ? WHERE id = ? AND revision = ?""",
                (target_state, now, intent_id, expected_revision),
            )
            run_id = str(intent["run_id"])
            attempt_id = str(intent["attempt_id"])
            if choice == "cancel":
                run_state = attempt_state = "cancel_requested"
                cancellation = self._prepare_run_cancellation(
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    now=now,
                )
                failed_runtime_action_ids = cancellation[
                    "failed_runtime_action_ids"
                ]
                runtime_action_id = cancellation["runtime_action_id"]
                closed_source_work = cancellation["closed_source_work"]
            else:
                run_state = str(intent["resume_run_state"])
                attempt_state = str(intent["resume_attempt_state"])
                failed_runtime_action_ids = []
                runtime_action_id = None
                closed_source_work = {}
            conn.execute(
                """UPDATE runs SET state = ?, revision = revision + 1,
                   updated_at = ? WHERE id = ?""",
                (run_state, now, run_id),
            )
            conn.execute(
                "UPDATE attempts SET state = ?, finished_at = ? WHERE id = ?",
                (
                    attempt_state,
                    None,
                    attempt_id,
                ),
            )
            if choice == "cancel":
                conn.execute(
                    """UPDATE threads SET status = 'cancel_requested',
                       revision = revision + 1, updated_at = ? WHERE id = ?
                         AND active_run_id = ?""",
                    (now, run["thread_id"], run_id),
                )
            else:
                conn.execute(
                    """UPDATE threads SET status = ?,
                       revision = revision + 1, updated_at = ?
                       WHERE id = ? AND active_run_id = ?""",
                    (run_state, now, run["thread_id"], run_id),
                )
            resolved_event = self._insert_event(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                event_type="decision.resolved",
                payload={
                    "decision_id": decision["id"],
                    "choice": choice,
                    "source_resolution_id": resolution_id,
                },
            )
            if choice == "cancel":
                self._insert_event(
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    event_type="run.cancel_requested",
                    payload={
                        "from": "waiting_for_decision",
                        "state": "cancel_requested",
                        "runtime_action_id": runtime_action_id,
                        "failed_runtime_action_ids": failed_runtime_action_ids,
                        "closed_source_work": closed_source_work,
                    },
                    causation_id=resolved_event["id"],
                )
            for candidate in selected:
                canonical_id = str(candidate["canonical_id"])
                source_row = conn.execute(
                    "SELECT * FROM sources WHERE canonical_id = ?",
                    (canonical_id,),
                ).fetchone()
                if (
                    source_row is not None
                    and source_row["import_state"] in {"existing", "imported"}
                    and source_row["engine_ref"]
                ):
                    source_id = str(source_row["id"])
                    binding_id = self._insert_run_source_binding(
                        conn,
                        run_id=run_id,
                        source_id=source_id,
                        resolution_id=resolution_id,
                        disposition="reused",
                        now=now,
                    )
                    self._insert_event(
                        conn,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        event_type="source.reused",
                        payload={
                            "source_id": source_id,
                            "binding_id": binding_id,
                            "canonical_id": canonical_id,
                        },
                        causation_id=resolved_event["id"],
                    )
                else:
                    if source_row is None:
                        source_id = self._insert_source(
                            conn,
                            authority=str(candidate["authority"]),
                            authority_id=str(candidate["authority_id"]),
                            source_kind="paper",
                            official_title=str(candidate["official_title"]),
                            engine_ref=None,
                            import_state="pending",
                            aliases=(),
                            now=now,
                        )
                        action_id = self._insert_source_import_action(
                            conn,
                            run_id=run_id,
                            source_id=source_id,
                            resolution_id=resolution_id,
                            canonical_id=canonical_id,
                            now=now,
                        )
                        event_type = "source.import_requested"
                    else:
                        source_id = str(source_row["id"])
                        action_row = conn.execute(
                            """SELECT id, state FROM source_import_actions
                               WHERE source_id = ?""",
                            (source_id,),
                        ).fetchone()
                        if action_row is None:
                            action_id = self._insert_source_import_action(
                                conn,
                                run_id=run_id,
                                source_id=source_id,
                                resolution_id=resolution_id,
                                canonical_id=canonical_id,
                                now=now,
                            )
                        else:
                            action_id = str(action_row["id"])
                            if action_row["state"] == "canceled":
                                conn.execute(
                                    """UPDATE source_import_actions
                                       SET state = 'pending', run_id = ?,
                                           resolution_id = ?, claim_owner = NULL,
                                           claim_expires_at = NULL,
                                           failure_category = NULL,
                                           completed_at = NULL
                                       WHERE id = ? AND state = 'canceled'""",
                                    (run_id, resolution_id, action_id),
                                )
                                conn.execute(
                                    """UPDATE sources
                                       SET import_state = 'pending',
                                           revision = revision + 1,
                                           updated_at = ? WHERE id = ?""",
                                    (now, source_id),
                                )
                            elif action_row["state"] == "completed":
                                raise InvalidTransition(
                                    "source_import_inconsistent", "bound"
                                )
                        event_type = "source.import_waiting"
                    waiter_id = self._insert_source_import_waiter(
                        conn,
                        action_id=action_id,
                        run_id=run_id,
                        resolution_id=resolution_id,
                        now=now,
                    )
                    self._insert_event(
                        conn,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        event_type=event_type,
                        payload={
                            "source_id": source_id,
                            "canonical_id": canonical_id,
                            "import_action_id": action_id,
                            "import_waiter_id": waiter_id,
                        },
                        causation_id=resolved_event["id"],
                    )
            self._audit(
                conn,
                "source_intent",
                intent_id,
                "source.resolved",
                {
                    "choice": choice,
                    "resolution_id": resolution_id,
                    "selected_candidate_ids": selected_candidate_ids,
                },
            )
            value = self._source_intent(conn, intent_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def list_pending_source_imports(self, *, limit: int = 500) -> list[JsonObject]:
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT source_import_actions.id
                   FROM source_import_actions
                   WHERE source_import_actions.state = 'pending'
                     AND EXISTS (
                         SELECT 1 FROM source_import_waiters
                         JOIN runs ON runs.id = source_import_waiters.run_id
                         JOIN threads ON threads.id = runs.thread_id
                         WHERE source_import_waiters.action_id = source_import_actions.id
                           AND source_import_waiters.state = 'waiting'
                           AND runs.state NOT IN (
                               'cancel_requested', 'canceled', 'completed', 'failed'
                           )
                           AND threads.active_run_id = runs.id
                     )
                   ORDER BY created_at, id LIMIT ?""",
                (limit,),
            ).fetchall()
            return [self._source_import_action(conn, str(row["id"])) for row in rows]

    def list_all_source_imports(self) -> list[JsonObject]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id FROM source_import_actions
                   ORDER BY created_at, id"""
            ).fetchall()
            return [
                self._source_import_action(conn, str(row["id"])) for row in rows
            ]

    def list_source_import_waiters(self, action_id: str) -> list[JsonObject]:
        with self._connect() as conn:
            self._source_import_action(conn, action_id)
            rows = conn.execute(
                """SELECT * FROM source_import_waiters WHERE action_id = ?
                   ORDER BY created_at, id""",
                (action_id,),
            ).fetchall()
            return [self._row(row) for row in rows]

    def claim_source_import(
        self,
        *,
        action_id: str,
        worker_id: str,
        lease_seconds: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        request = {
            "action_id": action_id,
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
        }
        operation = f"POST:/internal/v1/source-imports/{action_id}/claim"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                current = self._source_import_action(conn, action_id)
                if self._claim_replay_is_current(
                    current,
                    worker_id=worker_id,
                    claim_epoch=replay.value.get("claim_epoch"),
                ):
                    return replay
                raise InvalidTransition("stale_claim_receipt", "claimed")
            action = self._source_import_action(conn, action_id)
            if action["state"] != "pending":
                raise InvalidTransition(str(action["state"]), "claimed")
            if self._first_live_source_import_waiter(
                conn, action_id=action_id
            ) is None:
                raise InvalidTransition("source_import_has_no_live_waiter", "claimed")
            now = self._utc_now()
            now_text = self._format_time(now)
            if action.get("claim_expires_at") and str(action["claim_expires_at"]) > now_text:
                raise InvalidTransition("already_claimed", "claimed")
            expires = self._format_time(now + timedelta(seconds=lease_seconds))
            cursor = conn.execute(
                """UPDATE source_import_actions
                   SET claim_owner = ?, claim_expires_at = ?,
                       claim_epoch = claim_epoch + 1
                   WHERE id = ? AND state = 'pending'
                     AND (claim_owner IS NULL OR claim_expires_at <= ?)""",
                (worker_id, expires, action_id, now_text),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_conflict", "claimed")
            value = self._source_import_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def complete_source_import(
        self,
        *,
        action_id: str,
        claim_owner: str,
        claim_epoch: int,
        request_hash: str,
        engine_ref: str,
        result_manifest: Mapping[str, Any],
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        claim_owner = self._required_text(
            claim_owner, "claim_owner", maximum=200
        )
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        if not isinstance(request_hash, str) or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None:
            raise ValueError("request_hash is invalid")
        engine_ref = self._validate_engine_ref(engine_ref)
        manifest = self._validate_import_manifest(result_manifest)
        request = {
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "request_hash": request_hash,
            "engine_ref": engine_ref,
            "result_manifest": manifest,
        }
        operation = f"POST:/internal/v1/source-imports/{action_id}/complete"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._source_import_action(conn, action_id)
            if action["request_hash"] != request_hash:
                raise IdempotencyConflict()
            if action["state"] != "pending" or not self._claim_fence_is_current(
                action, worker_id=claim_owner, claim_epoch=claim_epoch
            ):
                raise InvalidTransition("claim_fence_mismatch", "completed")
            if self._first_live_source_import_waiter(
                conn, action_id=action_id
            ) is None:
                raise InvalidTransition(
                    "source_import_has_no_live_waiter", "completed"
                )
            collision = conn.execute(
                "SELECT id FROM sources WHERE engine_ref = ? AND id != ?",
                (engine_ref, action["source_id"]),
            ).fetchone()
            if collision is not None:
                raise InvalidTransition("engine_ref_collision", "completed")
            now = self._now()
            cursor = conn.execute(
                """UPDATE source_import_actions
                   SET state = 'completed', claim_owner = NULL,
                       claim_expires_at = NULL, engine_ref = ?,
                       result_manifest_json = ?, completed_at = ?
                   WHERE id = ? AND state = 'pending' AND claim_owner = ?
                     AND claim_epoch = ?""",
                (
                    engine_ref,
                    self._json(manifest),
                    now,
                    action_id,
                    claim_owner,
                    claim_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", "completed")
            conn.execute(
                """UPDATE sources SET import_state = 'imported', engine_ref = ?,
                   revision = revision + 1, updated_at = ? WHERE id = ?""",
                (engine_ref, now, action["source_id"]),
            )
            binding_ids: list[str] = []
            waiters = conn.execute(
                """SELECT * FROM source_import_waiters
                   WHERE action_id = ? AND state = 'waiting'
                   ORDER BY created_at, id""",
                (action_id,),
            ).fetchall()
            for waiter in waiters:
                waiter_run = self._run(conn, str(waiter["run_id"]))
                waiter_thread = self._thread(conn, str(waiter_run["thread_id"]))
                if (
                    waiter_run["state"]
                    in {"cancel_requested", "canceled", "completed", "failed"}
                    or waiter_thread.get("active_run_id") != waiter["run_id"]
                ):
                    conn.execute(
                        """UPDATE source_import_waiters
                           SET state = 'canceled', completed_at = ? WHERE id = ?""",
                        (now, waiter["id"]),
                    )
                    continue
                binding_id = self._insert_run_source_binding(
                    conn,
                    run_id=str(waiter["run_id"]),
                    source_id=str(action["source_id"]),
                    resolution_id=str(waiter["resolution_id"]),
                    disposition="imported",
                    now=now,
                )
                binding_ids.append(binding_id)
                conn.execute(
                    """UPDATE source_import_waiters
                       SET state = 'bound', completed_at = ? WHERE id = ?""",
                    (now, waiter["id"]),
                )
                context = conn.execute(
                    """SELECT source_intents.attempt_id
                       FROM source_resolutions
                       JOIN source_intents
                         ON source_intents.id = source_resolutions.intent_id
                       WHERE source_resolutions.id = ?""",
                    (waiter["resolution_id"],),
                ).fetchone()
                self._insert_event(
                    conn,
                    run_id=str(waiter["run_id"]),
                    attempt_id=str(context["attempt_id"]),
                    event_type="source.imported",
                    payload={
                        "source_id": action["source_id"],
                        "binding_id": binding_id,
                        "canonical_id": action["canonical_id"],
                        "import_action_id": action_id,
                    },
                )
            self._audit(
                conn,
                "source",
                str(action["source_id"]),
                "source.imported",
                {"import_action_id": action_id, "binding_ids": binding_ids},
            )
            value = self._source_import_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def fail_source_import(
        self,
        *,
        action_id: str,
        claim_owner: str,
        claim_epoch: int,
        category: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        claim_owner = self._required_text(
            claim_owner, "claim_owner", maximum=200
        )
        if type(claim_epoch) is not int or claim_epoch < 1:
            raise ValueError("claim_epoch must be a positive integer")
        if category not in _SOURCE_IMPORT_FAILURES:
            raise ValueError("source import failure category is invalid")
        request = {
            "claim_owner": claim_owner,
            "claim_epoch": claim_epoch,
            "category": category,
        }
        operation = f"POST:/internal/v1/source-imports/{action_id}/fail"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._source_import_action(conn, action_id)
            if action["state"] != "pending" or not self._claim_fence_is_current(
                action, worker_id=claim_owner, claim_epoch=claim_epoch
            ):
                raise InvalidTransition("claim_fence_mismatch", "failed")
            now = self._now()
            cursor = conn.execute(
                """UPDATE source_import_actions
                   SET state = 'failed', claim_owner = NULL,
                       claim_expires_at = NULL, failure_category = ?,
                       completed_at = ?
                   WHERE id = ? AND state = 'pending' AND claim_owner = ?
                     AND claim_epoch = ?""",
                (category, now, action_id, claim_owner, claim_epoch),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition("claim_fence_mismatch", "failed")
            conn.execute(
                """UPDATE sources SET import_state = 'failed',
                   revision = revision + 1, updated_at = ? WHERE id = ?""",
                (now, action["source_id"]),
            )
            self._audit(
                conn,
                "source",
                str(action["source_id"]),
                "source.import_failed",
                {"import_action_id": action_id, "category": category},
            )
            value = self._source_import_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def retry_source_import(
        self,
        *,
        action_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        request = {"action_id": action_id}
        operation = f"POST:/internal/v1/source-imports/{action_id}/retry"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._source_import_action(conn, action_id)
            if action["state"] != "failed":
                raise InvalidTransition(str(action["state"]), "pending")
            now = self._now()
            conn.execute(
                """UPDATE source_import_actions
                   SET state = 'pending', claim_owner = NULL,
                       claim_expires_at = NULL, failure_category = NULL,
                       completed_at = NULL WHERE id = ? AND state = 'failed'""",
                (action_id,),
            )
            conn.execute(
                """UPDATE sources SET import_state = 'pending',
                   revision = revision + 1, updated_at = ? WHERE id = ?""",
                (now, action["source_id"]),
            )
            self._audit(
                conn,
                "source",
                str(action["source_id"]),
                "source.import_retried",
                {"import_action_id": action_id},
            )
            value = self._source_import_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def get_source_intent(self, intent_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._source_intent(conn, intent_id)

    def get_source(self, source_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._source(conn, source_id)

    def list_sources(self) -> list[JsonObject]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM sources ORDER BY canonical_id, id"
            ).fetchall()
            return [self._source(conn, str(row["id"])) for row in rows]

    def list_run_source_bindings(self, run_id: str) -> list[JsonObject]:
        with self._connect() as conn:
            self._run(conn, run_id)
            rows = conn.execute(
                """SELECT id FROM run_source_bindings WHERE run_id = ?
                   ORDER BY created_at, id""",
                (run_id,),
            ).fetchall()
            return [
                self._run_source_binding(conn, str(row["id"])) for row in rows
            ]

    def get_source_import_action(self, action_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._source_import_action(conn, action_id)

    def _research_context(self, conn: sqlite3.Connection, run_id: str) -> JsonObject | None:
        from ..research.context import validate_snapshot

        row = conn.execute(
            "SELECT * FROM research_contexts WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["snapshot"] = json.loads(value.pop("snapshot_json"))
        validate_snapshot(value["snapshot"], value["sha256"])
        if value["query"] != value["snapshot"]["query"]:
            raise InvalidTransition("research_context_integrity", "research_read")
        return value

    def get_research_context(self, run_id: str) -> JsonObject | None:
        with self._connect() as conn:
            self._run(conn, run_id)
            return self._research_context(conn, run_id)

    def previous_research_context(self, thread_id: str, message_id: str) -> JsonObject | None:
        with self._connect() as conn:
            message = self._message(conn, message_id)
            if message["thread_id"] != thread_id:
                raise InvalidTransition("research_message_not_owned", "research_read")
            row = conn.execute(
                """SELECT c.run_id FROM research_contexts c JOIN messages m ON m.id = c.message_id
                   WHERE c.thread_id = ? AND m.position < ? ORDER BY m.position DESC LIMIT 1""",
                (thread_id, message["position"]),
            ).fetchone()
            return self._research_context(conn, row["run_id"]) if row else None

    def get_research_result(self, run_id: str, attempt_id: str) -> JsonObject | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT v.id FROM artifact_versions v JOIN artifacts a ON a.id = v.artifact_id
                   WHERE v.run_id = ? AND v.attempt_id = ? AND a.kind = 'research-memo'
                     AND v.tool_name = 'application-research' AND v.state = 'committed'
                   ORDER BY v.created_at, v.id LIMIT 1""", (run_id, attempt_id),
            ).fetchone()
            return self._artifact_version(conn, row["id"], include_action=True) if row else None

    def research_materialization_leased(self, run_id: str, attempt_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                """SELECT 1 FROM artifact_materialization_actions a
                   JOIN artifact_versions v ON v.id = a.artifact_version_id
                   JOIN research_contexts c ON c.run_id = v.run_id
                   WHERE v.run_id = ? AND v.attempt_id = ?
                     AND v.tool_name = 'application-research'
                     AND a.state = 'claimed' AND a.claim_expires_at > ? LIMIT 1""",
                (run_id, attempt_id, self._now()),
            ).fetchone() is not None

    def _validate_research_sources(self, conn: sqlite3.Connection, snapshot: JsonObject) -> None:
        from ..sources.adoption import decode_engine_ref, encode_engine_ref

        for source in snapshot["sources"]:
            rows = conn.execute(
                """SELECT s.canonical_id, s.engine_ref, e.paper_dir
                   FROM sources s JOIN adoption_entries e
                     ON e.source_id = s.id AND e.engine_ref = s.engine_ref
                   JOIN adoption_manifests m ON m.manifest_id = e.manifest_id
                   JOIN asset_roots r ON r.root_id = m.corpus_root_id
                   WHERE s.id = ? AND s.source_kind = 'paper'
                     AND s.import_state IN ('existing', 'imported')
                     AND r.root_id = 'research-corpus' AND r.enabled = 1""",
                (source["source_id"],),
            ).fetchall()
            if not rows or any(
                row["canonical_id"] != source["canonical_id"]
                or row["engine_ref"] != source["engine_ref"]
                or decode_engine_ref(row["engine_ref"]) != row["paper_dir"]
                or encode_engine_ref(row["paper_dir"]) != row["engine_ref"]
                for row in rows
            ):
                raise InvalidTransition("research_source_not_ready", "research_selection")

    def record_research_context(
        self, *, run_id: str, thread_id: str, attempt_id: str, message_id: str,
        query: str, snapshot: JsonObject, sha256: str, expected_revision: int,
        actor_id: str, idempotency_key: str,
    ) -> CommandResult:
        """Select adopted evidence once; retries can only reload the original packet."""
        from ..research.context import ACTOR, mode_for, validate_snapshot

        if actor_id != ACTOR:
            raise InvalidTransition("research_actor_not_owned", "research_selection")
        encoded = validate_snapshot(snapshot, sha256)
        if query != snapshot["query"]:
            raise ValueError("research query does not match snapshot")
        request = {"run_id": run_id, "thread_id": thread_id, "message_id": message_id,
                   "query": query, "sha256": sha256}
        operation = f"INTERNAL:research-contexts/{run_id}"
        with self._transaction() as conn:
            run, _ = self._validate_artifact_run_attempt(
                conn, run_id=run_id, attempt_id=attempt_id, thread_id=thread_id)
            self._expect_revision(run, expected_revision)
            message = self._message(conn, message_id)
            if message["thread_id"] != thread_id or message["role"] != "user":
                raise InvalidTransition("research_message_not_owned", "research_selection")
            history = [dict(row) for row in conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? AND position <= ? ORDER BY position",
                (thread_id, message["position"]),
            )]
            authority, question = mode_for(history)
            if authority != snapshot["authority"]["message_id"] or question != query:
                raise InvalidTransition("research_authority_mismatch", "research_selection")
            self._validate_research_sources(conn, snapshot)
            existing = self._research_context(conn, run_id)
            self._validate_research_documents(
                conn, snapshot, thread_id=thread_id, admission=existing is None
            )
            if existing is not None:
                if any(existing[key] != request[key] for key in request):
                    raise InvalidTransition("research_context_immutable", "research_selection")
                return CommandResult(value=existing, status_code=200, replayed=True)
            latest = conn.execute(
                "SELECT id FROM messages WHERE thread_id = ? AND role = 'user' ORDER BY position DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            if latest is None or latest["id"] != message_id:
                raise InvalidTransition("research_message_not_current", "research_selection")
            self._receipt(conn, actor_id, operation, idempotency_key, request)
            now = self._now()
            conn.execute(
                """INSERT INTO research_contexts
                   (run_id, thread_id, message_id, attempt_id, query, snapshot_json,
                    sha256, created_at, actor_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, thread_id, message_id, attempt_id, query, encoded, sha256, now, actor_id),
            )
            conn.execute("UPDATE runs SET revision = revision + 1, updated_at = ? WHERE id = ?",
                         (now, run_id))
            self._insert_event(conn, run_id=run_id, attempt_id=attempt_id,
                               event_type="research.context_selected",
                               payload={"message_id": message_id, "sha256": sha256,
                                        "authority": snapshot["authority"]})
            self._audit(conn, "run", run_id, "research.context_selected",
                        {"message_id": message_id, "sha256": sha256, "actor_id": actor_id})
            value = self._research_context(conn, run_id)
            return self._save_receipt(conn, actor_id, operation, idempotency_key, request, value, 201)

    def research_attempt_response(self, run_id: str, attempt_id: str) -> str | None:
        """Recover only messages durably attributed to this exact producing attempt."""
        with self._connect() as conn:
            run = self._run(conn, run_id)
            if self._attempt(conn, attempt_id)["run_id"] != run_id:
                raise InvalidTransition("run_attempt_ownership", "research_response")
            rows = conn.execute(
                """SELECT m.content FROM run_events e JOIN messages m
                     ON m.id = json_extract(e.payload_json, '$.message_id')
                   WHERE e.run_id = ? AND e.attempt_id = ?
                     AND e.type = 'runtime.message.completed'
                     AND m.thread_id = ? AND m.role = 'assistant'
                   ORDER BY e.sequence""", (run_id, attempt_id, run["thread_id"]),
            ).fetchall()
            return "\n\n".join(row["content"] for row in rows) or None

    def create_artifact(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        run_id: str,
        attempt_id: str,
        kind: str,
        title: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        kind = self._artifact_identifier(kind, "kind")
        title = self._artifact_text(title, "title", maximum=2_000)
        request = {
            "workspace_id": workspace_id,
            "thread_id": thread_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "kind": kind,
            "title": title,
        }
        operation = "POST:/internal/v1/artifacts"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            thread = self._thread(conn, thread_id)
            if thread["workspace_id"] != workspace_id:
                raise InvalidTransition("thread_workspace_mismatch", "artifact_created")
            run, attempt = self._validate_artifact_run_attempt(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                thread_id=thread_id,
            )
            artifact_id = self._id_factory("artifact")
            Artifact(
                schema_version=1,
                artifact_id=artifact_id,
                workspace_id=workspace_id,
                thread_id=thread_id,
                kind=kind,
                title=title,
            )
            now = self._now()
            conn.execute(
                """INSERT INTO artifacts
                   (id, workspace_id, thread_id, kind, title,
                    head_revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (artifact_id, workspace_id, thread_id, kind, title, now, now),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(attempt["id"]),
                event_type="artifact.created",
                payload={"artifact_id": artifact_id, "kind": kind},
            )
            self._audit(
                conn,
                "artifact",
                artifact_id,
                "artifact.created",
                {"workspace_id": workspace_id, "thread_id": thread_id, "kind": kind},
            )
            value = self._artifact(conn, artifact_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def request_artifact_version(
        self,
        *,
        artifact_id: str,
        logical_version: int,
        run_id: str,
        attempt_id: str,
        source_ids: Sequence[str],
        research_engine_refs: Sequence[str],
        generator: Mapping[str, Any],
        tool: Mapping[str, Any],
        parents: Sequence[Mapping[str, Any]],
        root_id: str,
        relative_path: str,
        sha256: str,
        byte_length: int,
        media_type: str,
        advance_head: bool,
        expected_head_revision: int | None,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        if type(logical_version) is not int or logical_version < 1:
            raise ValueError("logical_version must be a positive integer")
        normalized_sources = self._artifact_identifiers(
            source_ids, "source_ids", allow_empty=True
        )
        normalized_refs = self._artifact_identifiers(
            research_engine_refs, "research_engine_refs", allow_empty=True
        )
        normalized_parents = self._artifact_parents(parents)
        producer = ProducerIdentity.from_dict(dict(generator))
        tool_identity = ProducerIdentity.from_dict(dict(tool))
        if type(advance_head) is not bool:
            raise ValueError("advance_head must be a boolean")
        if advance_head:
            if type(expected_head_revision) is not int or expected_head_revision < 0:
                raise ValueError(
                    "expected_head_revision must be a non-negative integer"
                )
        elif expected_head_revision is not None:
            raise ValueError("expected_head_revision requires advance_head")
        MaterializationRequest.from_dict(
            {
                "schema_version": 1,
                "operation_id": "artifact-materialize-validation",
                "root_id": root_id,
                "relative_path": relative_path,
                "sha256": sha256,
                "byte_length": byte_length,
                "media_type": media_type,
                "parents": [parent.to_dict() for parent in normalized_parents],
            }
        )
        request = {
            "artifact_id": artifact_id,
            "logical_version": logical_version,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "source_ids": list(normalized_sources),
            "research_engine_refs": list(normalized_refs),
            "generator": producer.to_dict(),
            "tool": tool_identity.to_dict(),
            "parents": [parent.to_dict() for parent in normalized_parents],
            "root_id": root_id,
            "relative_path": relative_path,
            "sha256": sha256,
            "byte_length": byte_length,
            "media_type": media_type,
            "advance_head": advance_head,
            "expected_head_revision": expected_head_revision,
        }
        operation = f"POST:/internal/v1/artifacts/{artifact_id}/versions"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            artifact = self._artifact(conn, artifact_id)
            run, attempt = self._validate_artifact_run_attempt(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                thread_id=str(artifact["thread_id"]),
            )
            if advance_head and artifact["head_revision"] != expected_head_revision:
                raise RevisionConflict(artifact)
            next_version = int(
                conn.execute(
                    """SELECT COALESCE(MAX(logical_version), 0) + 1
                       FROM artifact_versions WHERE artifact_id = ?""",
                    (artifact_id,),
                ).fetchone()[0]
            )
            if logical_version != next_version:
                raise InvalidTransition(
                    f"logical_version_{next_version}",
                    f"logical_version_{logical_version}",
                )
            self._validate_artifact_sources(
                conn,
                run_id=run_id,
                source_ids=normalized_sources,
                research_engine_refs=normalized_refs,
            )
            self._validate_artifact_parents(
                conn,
                artifact=artifact,
                parents=normalized_parents,
            )
            version_id = self._id_factory("artifact_version")
            self._artifact_identifier(version_id, "artifact_version_id")
            resource_uri = "cortex://artifacts/" + "/".join(
                quote(segment, safe="-._~") for segment in (artifact_id, version_id)
            )
            action_id = self._id_factory("artifact_materialization")
            operation_id = f"artifact-materialize:{version_id}"
            materialization_request = MaterializationRequest.from_dict(
                {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "root_id": root_id,
                    "relative_path": relative_path,
                    "sha256": sha256,
                    "byte_length": byte_length,
                    "media_type": media_type,
                    "parents": [parent.to_dict() for parent in normalized_parents],
                }
            )
            now = self._now()
            conn.execute(
                """INSERT INTO artifact_versions
                   (id, artifact_id, logical_version, resource_uri, sha256,
                    byte_length, media_type, run_id, attempt_id,
                    generator_name, generator_version, tool_name, tool_version,
                    state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           'pending_materialization', ?)""",
                (
                    version_id,
                    artifact_id,
                    logical_version,
                    resource_uri,
                    sha256,
                    byte_length,
                    media_type,
                    run_id,
                    attempt_id,
                    producer.name,
                    producer.version,
                    tool_identity.name,
                    tool_identity.version,
                    now,
                ),
            )
            for position, parent in enumerate(normalized_parents):
                conn.execute(
                    """INSERT INTO artifact_version_parents
                       (artifact_version_id, parent_artifact_version_id,
                        expected_sha256, position)
                       VALUES (?, ?, ?, ?)""",
                    (version_id, parent.artifact_version_id, parent.sha256, position),
                )
            for position, source_id in enumerate(normalized_sources):
                conn.execute(
                    """INSERT INTO artifact_version_sources
                       (artifact_version_id, source_id, position)
                       VALUES (?, ?, ?)""",
                    (version_id, source_id, position),
                )
            for position, engine_ref in enumerate(normalized_refs):
                conn.execute(
                    """INSERT INTO artifact_version_engine_refs
                       (artifact_version_id, engine_ref, position)
                       VALUES (?, ?, ?)""",
                    (version_id, engine_ref, position),
                )
            conn.execute(
                """UPDATE artifact_versions SET lineage_sealed = 1
                   WHERE id = ? AND lineage_sealed = 0""",
                (version_id,),
            )
            conn.execute(
                """INSERT INTO artifact_materialization_actions
                   (id, operation_id, artifact_version_id, request_hash,
                    root_id, relative_path, sha256, byte_length, media_type,
                    advance_head, expected_head_revision, state, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    action_id,
                    operation_id,
                    version_id,
                    self._request_hash(materialization_request.to_dict()),
                    root_id,
                    relative_path,
                    sha256,
                    byte_length,
                    media_type,
                    int(advance_head),
                    expected_head_revision,
                    now,
                ),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(attempt["id"]),
                event_type="artifact.materialization_requested",
                payload={
                    "artifact_id": artifact_id,
                    "artifact_version_id": version_id,
                    "logical_version": logical_version,
                },
            )
            self._audit(
                conn,
                "artifact_version",
                version_id,
                "artifact.materialization_requested",
                {
                    "artifact_id": artifact_id,
                    "logical_version": logical_version,
                    "root_id": root_id,
                },
            )
            value = self._artifact_version(conn, version_id, include_action=True)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 202
            )

    def list_pending_artifact_materializations(
        self, *, limit: int = 500
    ) -> list[JsonObject]:
        if type(limit) is not int or not 1 <= limit <= _ARTIFACT_LIST_LIMIT:
            raise ValueError("limit must be between 1 and 500")
        now = self._now()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT artifact_materialization_actions.id
                   FROM artifact_materialization_actions
                   JOIN artifact_versions
                     ON artifact_versions.id =
                        artifact_materialization_actions.artifact_version_id
                   JOIN runs ON runs.id = artifact_versions.run_id
                   JOIN threads ON threads.id = runs.thread_id
                   WHERE (
                       artifact_materialization_actions.state = 'pending'
                       OR (
                           artifact_materialization_actions.state = 'claimed'
                           AND artifact_materialization_actions.claim_expires_at <= ?
                       )
                   )
                     AND artifact_versions.state = 'pending_materialization'
                     AND runs.state IN (
                         'queued', 'starting', 'running',
                         'waiting_for_decision', 'resuming', 'retrying'
                     )
                     AND runs.active_attempt_id = artifact_versions.attempt_id
                     AND threads.active_run_id = runs.id
                   ORDER BY artifact_materialization_actions.created_at,
                            artifact_materialization_actions.id
                   LIMIT ?""",
                (now, limit),
            ).fetchall()
            return [
                self._artifact_materialization_action(conn, str(row["id"]))
                for row in rows
            ]

    def claim_artifact_materialization(
        self,
        *,
        action_id: str,
        worker_id: str,
        lease_seconds: int,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        request = {"worker_id": worker_id, "lease_seconds": lease_seconds}
        operation = f"POST:/internal/v1/artifact-materializations/{action_id}/claim"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._artifact_materialization_action(conn, action_id)
            version = self._artifact_version(
                conn, str(action["artifact_version_id"]), include_action=False
            )
            self._validate_artifact_run_attempt(
                conn,
                run_id=str(version["run_id"]),
                attempt_id=str(version["attempt_id"]),
                thread_id=str(self._artifact(conn, str(version["artifact_id"]))["thread_id"]),
            )
            now_value = self._utc_now()
            now = self._format_time(now_value)
            if action["state"] == "claimed" and str(
                action.get("claim_expires_at") or ""
            ) > now:
                raise InvalidTransition("claimed", "claimed")
            if action["state"] not in {"pending", "claimed"}:
                raise InvalidTransition(str(action["state"]), "claimed")
            if version["state"] != "pending_materialization":
                raise InvalidTransition(str(version["state"]), "claimed")
            expires_at = self._format_time(now_value + timedelta(seconds=lease_seconds))
            cursor = conn.execute(
                """UPDATE artifact_materialization_actions
                   SET state = 'claimed', claim_owner = ?,
                       claim_epoch = claim_epoch + 1, claim_expires_at = ?,
                       attempt_count = attempt_count + 1,
                       failure_category = NULL, completed_at = NULL
                   WHERE id = ? AND (
                       state = 'pending'
                       OR (state = 'claimed' AND claim_expires_at <= ?)
                   )""",
                (worker_id, expires_at, action_id, now),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition(str(action["state"]), "claimed")
            value = self._artifact_materialization_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def complete_artifact_materialization(
        self,
        *,
        action_id: str,
        worker_id: str,
        claim_epoch: int,
        materialized_result: Mapping[str, Any],
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        result = self._artifact_materialized_result(materialized_result)
        request = {
            "worker_id": worker_id,
            "claim_epoch": claim_epoch,
            "materialized_result": result.to_dict(),
        }
        operation = f"POST:/internal/v1/artifact-materializations/{action_id}/complete"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._artifact_materialization_action(conn, action_id)
            version = self._artifact_version(
                conn, str(action["artifact_version_id"]), include_action=False
            )
            if action["state"] == "completed":
                if action.get("result") != result.to_dict() or version["state"] != "committed":
                    raise InvalidTransition("completed", "conflicting_completion")
                value = self._artifact_version(
                    conn, str(version["id"]), include_action=True
                )
                return self._save_receipt(
                    conn, actor_id, operation, idempotency_key, request, value, 200
                )
            if not self._claim_fence_is_current(
                action, worker_id=worker_id, claim_epoch=claim_epoch
            ):
                raise InvalidTransition("stale_claim", "completed")
            artifact = self._artifact(conn, str(version["artifact_id"]))
            run, attempt = self._validate_artifact_run_attempt(
                conn,
                run_id=str(version["run_id"]),
                attempt_id=str(version["attempt_id"]),
                thread_id=str(artifact["thread_id"]),
            )
            expected_request = MaterializationRequest.from_dict(
                {
                    "schema_version": 1,
                    "operation_id": action["operation_id"],
                    "root_id": action["root_id"],
                    "relative_path": action["relative_path"],
                    "sha256": action["sha256"],
                    "byte_length": action["byte_length"],
                    "media_type": action["media_type"],
                    "parents": version["parents"],
                }
            )
            research_context = self._research_context(conn, str(run["id"]))
            documents = ()
            if research_context is not None:
                self._validate_research_sources(conn, research_context["snapshot"])
                documents = tuple(sorted(d["document_version_id"]
                                         for d in research_context["snapshot"].get("documents", ())))
                if documents:
                    self._validate_research_documents(
                        conn, research_context["snapshot"], thread_id=str(run["thread_id"]), admission=False
                    )
            if not version["source_ids"] and not documents:
                raise InvalidTransition("research_source_not_ready", "artifact_version")
            if (
                action["request_hash"]
                != self._request_hash(expected_request.to_dict())
                or result.operation_id != expected_request.operation_id
                or result.root_id != expected_request.root_id
                or result.relative_path != expected_request.relative_path
                or result.sha256 != expected_request.sha256
                or result.byte_length != expected_request.byte_length
                or result.media_type != expected_request.media_type
                or result.parents != expected_request.parents
            ):
                raise InvalidTransition("materialized_result_mismatch", "committed")
            now = self._now()
            provenance = Provenance(
                schema_version=2 if documents else 1,
                document_version_ids=documents,
                research_context_sha256=research_context["sha256"] if documents else None,
                run_id=str(version["run_id"]),
                attempt_id=str(version["attempt_id"]),
                source_ids=tuple(version["source_ids"]),
                research_engine_refs=tuple(version["research_engine_refs"]),
                generator=ProducerIdentity(
                    str(version["generator"]["name"]),
                    str(version["generator"]["version"]),
                ),
                tool=ProducerIdentity(
                    str(version["tool"]["name"]),
                    str(version["tool"]["version"]),
                ),
                parents=tuple(
                    ParentVersionInput.from_dict(parent)
                    for parent in version["parents"]
                ),
                media_type=str(version["media_type"]),
                byte_length=int(version["byte_length"]),
                sha256=str(version["sha256"]),
                committed_at=now,
            )
            ArtifactVersion(
                schema_version=1,
                artifact_version_id=str(version["id"]),
                artifact_id=str(version["artifact_id"]),
                logical_version=int(version["logical_version"]),
                resource_uri=str(version["resource_uri"]),
                sha256=str(version["sha256"]),
                byte_length=int(version["byte_length"]),
                media_type=str(version["media_type"]),
                parents=provenance.parents,
                provenance=provenance,
                committed_at=now,
            )
            materialized_cursor = conn.execute(
                """UPDATE artifact_materialization_actions
                   SET state = 'materialized', result_json = ?,
                       failure_category = NULL
                   WHERE id = ? AND state = 'claimed'
                     AND claim_owner = ? AND claim_epoch = ?""",
                (
                    self._json(result.to_dict()),
                    action_id,
                    worker_id,
                    claim_epoch,
                ),
            )
            if materialized_cursor.rowcount != 1:
                raise InvalidTransition("stale_claim", "completed")
            version_cursor = conn.execute(
                """UPDATE artifact_versions
                   SET state = 'committed', provenance_json = ?, committed_at = ?
                   WHERE id = ? AND state = 'pending_materialization'""",
                (self._json(provenance.to_dict()), now, version["id"]),
            )
            if version_cursor.rowcount != 1:
                raise InvalidTransition(str(version["state"]), "committed")
            head_advanced = False
            observed_head_revision: int | None = None
            if action["advance_head"]:
                head_cursor = conn.execute(
                    """UPDATE artifacts
                       SET head_artifact_version_id = ?,
                           head_revision = head_revision + 1, updated_at = ?
                       WHERE id = ? AND head_revision = ?""",
                    (
                        version["id"],
                        now,
                        artifact["id"],
                        action["expected_head_revision"],
                    ),
                )
                if head_cursor.rowcount == 1:
                    head_advanced = True
                    observed_head_revision = int(action["expected_head_revision"]) + 1
                else:
                    observed_head_revision = int(
                        self._artifact(conn, str(artifact["id"]))["head_revision"]
                    )
            action_cursor = conn.execute(
                """UPDATE artifact_materialization_actions
                   SET state = 'completed', claim_owner = NULL,
                       claim_expires_at = NULL, completed_at = ?,
                       head_advanced = ?, observed_head_revision = ?
                   WHERE id = ? AND state = 'materialized'
                     AND claim_owner = ? AND claim_epoch = ?""",
                (
                    now,
                    int(head_advanced),
                    observed_head_revision,
                    action_id,
                    worker_id,
                    claim_epoch,
                ),
            )
            if action_cursor.rowcount != 1:
                raise InvalidTransition("stale_claim", "completed")
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(attempt["id"]),
                event_type="artifact.version_committed",
                payload={
                    "artifact_id": artifact["id"],
                    "artifact_version_id": version["id"],
                    "logical_version": version["logical_version"],
                    "resource_uri": version["resource_uri"],
                },
            )
            if head_advanced:
                self._insert_event(
                    conn,
                    run_id=str(run["id"]),
                    attempt_id=str(attempt["id"]),
                    event_type="artifact.head_advanced",
                    payload={
                        "artifact_id": artifact["id"],
                        "artifact_version_id": version["id"],
                        "head_revision": int(action["expected_head_revision"]) + 1,
                    },
                )
            self._audit(
                conn,
                "artifact_version",
                str(version["id"]),
                "artifact.version_committed",
                {
                    "artifact_id": artifact["id"],
                    "head_advanced": head_advanced,
                },
            )
            value = self._artifact_version(conn, str(version["id"]), include_action=True)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def fail_artifact_materialization(
        self,
        *,
        action_id: str,
        worker_id: str,
        claim_epoch: int,
        failure_category: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        worker_id = self._required_text(worker_id, "worker_id", maximum=200)
        if (
            not isinstance(failure_category, str)
            or _ARTIFACT_FAILURE_RE.fullmatch(failure_category) is None
        ):
            raise ValueError("failure_category is invalid")
        request = {
            "worker_id": worker_id,
            "claim_epoch": claim_epoch,
            "failure_category": failure_category,
        }
        operation = f"POST:/internal/v1/artifact-materializations/{action_id}/fail"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._artifact_materialization_action(conn, action_id)
            if not self._claim_fence_is_current(
                action, worker_id=worker_id, claim_epoch=claim_epoch
            ):
                raise InvalidTransition("stale_claim", "failed")
            now = self._now()
            conn.execute(
                """UPDATE artifact_materialization_actions
                   SET state = 'failed', claim_owner = NULL,
                       claim_expires_at = NULL, failure_category = ?,
                       completed_at = ?
                   WHERE id = ? AND state = 'claimed'
                     AND claim_owner = ? AND claim_epoch = ?""",
                (failure_category, now, action_id, worker_id, claim_epoch),
            )
            conn.execute(
                """UPDATE artifact_versions SET state = 'failed'
                   WHERE id = ? AND state = 'pending_materialization'""",
                (action["artifact_version_id"],),
            )
            value = self._artifact_materialization_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def retry_artifact_materialization(
        self,
        *,
        action_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        request: JsonObject = {}
        operation = f"POST:/internal/v1/artifact-materializations/{action_id}/retry"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            action = self._artifact_materialization_action(conn, action_id)
            if action["state"] != "failed":
                raise InvalidTransition(str(action["state"]), "pending")
            version = self._artifact_version(
                conn, str(action["artifact_version_id"]), include_action=False
            )
            artifact = self._artifact(conn, str(version["artifact_id"]))
            self._validate_artifact_run_attempt(
                conn,
                run_id=str(version["run_id"]),
                attempt_id=str(version["attempt_id"]),
                thread_id=str(artifact["thread_id"]),
            )
            conn.execute(
                """UPDATE artifact_materialization_actions
                   SET state = 'pending', failure_category = NULL,
                       completed_at = NULL
                   WHERE id = ? AND state = 'failed'""",
                (action_id,),
            )
            conn.execute(
                """UPDATE artifact_versions SET state = 'pending_materialization'
                   WHERE id = ? AND state = 'failed'""",
                (version["id"],),
            )
            value = self._artifact_materialization_action(conn, action_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 200
            )

    def advance_artifact_head(
        self,
        *,
        artifact_id: str,
        artifact_version_id: str,
        expected_head_revision: int,
        run_id: str,
        attempt_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        if type(expected_head_revision) is not int or expected_head_revision < 0:
            raise ValueError("expected_head_revision must be a non-negative integer")
        request = {
            "artifact_version_id": artifact_version_id,
            "expected_head_revision": expected_head_revision,
            "run_id": run_id,
            "attempt_id": attempt_id,
        }
        operation = f"POST:/internal/v1/artifacts/{artifact_id}/head"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            artifact = self._artifact(conn, artifact_id)
            run, attempt = self._validate_artifact_run_attempt(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                thread_id=str(artifact["thread_id"]),
            )
            version = self._artifact_version(
                conn, artifact_version_id, include_action=False
            )
            if version["artifact_id"] != artifact_id or version["state"] != "committed":
                raise InvalidTransition(str(version["state"]), "artifact_head")
            now = self._now()
            cursor = conn.execute(
                """UPDATE artifacts
                   SET head_artifact_version_id = ?,
                       head_revision = head_revision + 1, updated_at = ?
                   WHERE id = ? AND head_revision = ?""",
                (artifact_version_id, now, artifact_id, expected_head_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(self._artifact(conn, artifact_id))
            current = self._artifact(conn, artifact_id)
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(attempt["id"]),
                event_type="artifact.head_advanced",
                payload={
                    "artifact_id": artifact_id,
                    "artifact_version_id": artifact_version_id,
                    "head_revision": current["head_revision"],
                },
            )
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, current, 200
            )

    def create_artifact_snapshot(
        self,
        *,
        workspace_id: str,
        run_id: str,
        attempt_id: str,
        name: str,
        artifact_version_ids: Sequence[str],
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        name = self._artifact_text(name, "name", maximum=2_000)
        version_ids = self._artifact_identifiers(
            artifact_version_ids,
            "artifact_version_ids",
            allow_empty=False,
        )
        if len(version_ids) > 500:
            raise ValueError("artifact snapshot exceeds the member limit")
        request = {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "name": name,
            "artifact_version_ids": list(version_ids),
        }
        operation = "POST:/internal/v1/artifact-snapshots"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            self._workspace(conn, workspace_id)
            run = self._run(conn, run_id)
            thread = self._thread(conn, str(run["thread_id"]))
            if thread["workspace_id"] != workspace_id:
                raise InvalidTransition("run_workspace_mismatch", "snapshot_created")
            run, attempt = self._validate_artifact_run_attempt(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                thread_id=str(thread["id"]),
            )
            members: list[SnapshotMember] = []
            for version_id in version_ids:
                version = self._artifact_version(
                    conn, version_id, include_action=False
                )
                artifact = self._artifact(conn, str(version["artifact_id"]))
                if artifact["workspace_id"] != workspace_id:
                    raise InvalidTransition(
                        "artifact_workspace_mismatch", "snapshot_member"
                    )
                if version["state"] != "committed":
                    raise InvalidTransition(str(version["state"]), "snapshot_member")
                members.append(
                    SnapshotMember(
                        artifact_id=str(artifact["id"]),
                        artifact_version_id=str(version["id"]),
                        logical_version=int(version["logical_version"]),
                        sha256=str(version["sha256"]),
                    )
                )
            members.sort(key=lambda member: (member.artifact_id, member.artifact_version_id))
            snapshot_id = self._id_factory("artifact_snapshot")
            now = self._now()
            snapshot = Snapshot(
                schema_version=1,
                snapshot_id=snapshot_id,
                workspace_id=workspace_id,
                name=name,
                members=tuple(members),
                created_at=now,
            )
            conn.execute(
                """INSERT INTO artifact_snapshots
                   (id, workspace_id, name, member_count, state, created_at)
                   VALUES (?, ?, ?, ?, 'building', ?)""",
                (snapshot_id, workspace_id, name, len(snapshot.members), now),
            )
            for position, member in enumerate(snapshot.members):
                conn.execute(
                    """INSERT INTO artifact_snapshot_members
                       (snapshot_id, artifact_id, artifact_version_id,
                        logical_version, sha256, position)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        member.artifact_id,
                        member.artifact_version_id,
                        member.logical_version,
                        member.sha256,
                        position,
                    ),
                )
            conn.execute(
                """UPDATE artifact_snapshots SET state = 'committed'
                   WHERE id = ? AND state = 'building'""",
                (snapshot_id,),
            )
            self._insert_event(
                conn,
                run_id=str(run["id"]),
                attempt_id=str(attempt["id"]),
                event_type="artifact.snapshot_created",
                payload={
                    "snapshot_id": snapshot_id,
                    "workspace_id": workspace_id,
                    "member_count": len(snapshot.members),
                },
            )
            self._audit(
                conn,
                "artifact_snapshot",
                snapshot_id,
                "artifact.snapshot_created",
                {"workspace_id": workspace_id, "member_count": len(snapshot.members)},
            )
            value = self._artifact_snapshot(conn, snapshot_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def get_artifact(self, artifact_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._artifact(conn, artifact_id)

    def list_artifacts(
        self,
        *,
        thread_id: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> _CursorPage:
        """One page of a thread's artifacts, keyset over the artifact id.

        ⟦batchO⟧ Returns the page's continuation cursor rather than leaving
        the route to guess it from the page being full: `id` is unique and the
        ordering is `id` alone, so reading one row further proves whether
        anything remains. The route used to answer `items[-1]["id"] if
        len(items) == limit else None`, which on a last page that is exactly
        full hands the client a cursor whose page comes back empty -- to a
        cockpit indistinguishable from the end of the list, which is the
        distinction a cursor exists to make.
        """

        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if after_id is not None:
            after_id = self._artifact_identifier(after_id, "after_id")
        with self._connect() as conn:
            self._thread(conn, thread_id)
            rows = conn.execute(
                """SELECT id FROM artifacts
                   WHERE thread_id = ? AND (? IS NULL OR id > ?)
                   ORDER BY id LIMIT ?""",
                (thread_id, after_id, after_id, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            items = [self._artifact(conn, str(row["id"])) for row in rows[:limit]]
            return _CursorPage(
                items,
                next_cursor=str(items[-1]["id"]) if has_more and items else None,
            )

    def get_artifact_version(self, artifact_version_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._artifact_version(conn, artifact_version_id, include_action=True)

    def read_artifact_content_reference(
        self, artifact_version_id: str
    ) -> ArtifactContentReference:
        artifact_version_id = self._artifact_identifier(
            artifact_version_id, "artifact_version_id"
        )
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                version_row = conn.execute(
                    """SELECT id, artifact_id, resource_uri, sha256,
                              byte_length, media_type, state
                       FROM artifact_versions WHERE id = ?""",
                    (artifact_version_id,),
                ).fetchone()
                if version_row is None:
                    raise NotFound("artifact version", artifact_version_id)
                parent_rows = conn.execute(
                    """SELECT parent_artifact_version_id, expected_sha256
                       FROM artifact_version_parents
                       WHERE artifact_version_id = ? ORDER BY position""",
                    (artifact_version_id,),
                ).fetchall()
                version: JsonObject = {
                    "id": str(version_row["id"]),
                    "artifact_id": str(version_row["artifact_id"]),
                    "resource_uri": str(version_row["resource_uri"]),
                    "sha256": str(version_row["sha256"]),
                    "byte_length": int(version_row["byte_length"]),
                    "media_type": str(version_row["media_type"]),
                    "state": str(version_row["state"]),
                    "parents": [
                        {
                            "artifact_version_id": str(
                                row["parent_artifact_version_id"]
                            ),
                            "sha256": str(row["expected_sha256"]),
                        }
                        for row in parent_rows
                    ],
                }
                if version["state"] != "committed":
                    raise InvalidTransition(str(version["state"]), "content_read")
                artifact = self._artifact(conn, str(version["artifact_id"]))
                parsed_uri = parse_resource_uri(str(version["resource_uri"]))
                if parsed_uri.root != "artifacts" or parsed_uri.segments != (
                    artifact["id"],
                    version["id"],
                ):
                    raise InvalidTransition("resource_uri_mismatch", "content_read")
                action_row = conn.execute(
                    """SELECT id FROM artifact_materialization_actions
                       WHERE artifact_version_id = ?""",
                    (artifact_version_id,),
                ).fetchone()
                if action_row is None:
                    raise InvalidTransition("missing_materialization", "content_read")
                action = self._artifact_materialization_action(
                    conn, str(action_row["id"])
                )
                if action["state"] != "completed" or action["result"] is None:
                    raise InvalidTransition(str(action["state"]), "content_read")
                raw_request: JsonObject = {
                    "schema_version": 1,
                    "operation_id": action["operation_id"],
                    "root_id": action["root_id"],
                    "relative_path": action["relative_path"],
                    "sha256": action["sha256"],
                    "byte_length": action["byte_length"],
                    "media_type": action["media_type"],
                    "parents": version["parents"],
                }
                request = self._validated_content_request(raw_request)
                if action["request_hash"] != self._request_hash(raw_request):
                    raise InvalidTransition("request_hash_mismatch", "content_read")
                result = action["result"]
                expected_result_fields = {
                    "schema_version",
                    "operation_id",
                    "root_id",
                    "relative_path",
                    "sha256",
                    "byte_length",
                    "media_type",
                    "parents",
                    "replayed",
                    "recovered_from",
                }
                if not isinstance(result, Mapping) or set(result) != expected_result_fields:
                    raise InvalidTransition("materialized_result_invalid", "content_read")
                raw_result_request = {
                    key: result[key] for key in MaterializationRequest.FIELDS
                }
                self._validated_content_request(raw_result_request)
                if (
                    raw_result_request != raw_request
                    or type(result["replayed"]) is not bool
                    or (
                        result["recovered_from"] is not None
                        and result["recovered_from"]
                        not in {"staged", "dual-link", "final"}
                    )
                    or version["sha256"] != raw_request["sha256"]
                    or version["byte_length"] != raw_request["byte_length"]
                    or version["media_type"] != raw_request["media_type"]
                ):
                    raise InvalidTransition("materialized_result_mismatch", "content_read")
                root = self._asset_root(conn, request.root_id)
                if not root.enabled or root.root_id != request.root_id:
                    raise InvalidTransition("asset_root_disabled", "content_read")
                reference = ArtifactContentReference(
                    artifact_version_id=str(version["id"]),
                    private_root=root.private_path,
                    root_max_bytes=root.max_bytes,
                    relative_path=request.relative_path,
                    media_type=str(raw_request["media_type"]),
                    byte_length=request.byte_length,
                    sha256=request.sha256,
                )
                conn.commit()
                return reference
            except BaseException:
                conn.rollback()
                raise

    def get_artifact_materialization(self, action_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._artifact_materialization_action(conn, action_id)

    def get_artifact_snapshot(self, snapshot_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._artifact_snapshot(conn, snapshot_id)

    def list_artifact_snapshots(
        self,
        *,
        workspace_id: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> _CursorPage:
        """One page of a workspace's snapshots, keyset over the snapshot id.

        ⟦batchO⟧ The proven cursor, for the reason argued at `list_artifacts`:
        the ordering is `id` alone and `id` is unique, so one extra row
        answers "is there more" instead of the route inferring it from a full
        page and being wrong on the last one.
        """

        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if after_id is not None:
            after_id = self._artifact_identifier(after_id, "after_id")
        with self._connect() as conn:
            self._workspace(conn, workspace_id)
            rows = conn.execute(
                """SELECT id FROM artifact_snapshots
                   WHERE workspace_id = ? AND (? IS NULL OR id > ?)
                   ORDER BY id LIMIT ?""",
                (workspace_id, after_id, after_id, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            items = [
                self._artifact_snapshot(conn, str(row["id"]))
                for row in rows[:limit]
            ]
            return _CursorPage(
                items,
                next_cursor=str(items[-1]["id"]) if has_more and items else None,
            )

    def bind_transport(
        self,
        *,
        transport: str,
        external_scope: str,
        thread_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        transport = self._required_text(transport, "transport", maximum=50)
        external_scope = self._required_text(
            external_scope, "external_scope", maximum=500
        )
        request = {"transport": transport, "thread_id": thread_id}
        digest = self._scope_digest(external_scope)
        operation = f"POST:/internal/v1/transports/{transport}/bindings/{digest}"
        with self._transaction() as conn:
            replay = self._receipt(conn, actor_id, operation, idempotency_key, request)
            if replay:
                return replay
            self._thread(conn, thread_id)
            existing = conn.execute(
                """SELECT id FROM transport_bindings
                   WHERE transport = ? AND external_scope = ?""",
                (transport, digest),
            ).fetchone()
            if existing is not None:
                value = self._binding(conn, str(existing["id"]))
                if value["thread_id"] != thread_id:
                    raise TransportBindingConflict(value)
                return self._save_receipt(
                    conn,
                    actor_id,
                    operation,
                    idempotency_key,
                    request,
                    value,
                    200,
                )
            now = self._now()
            binding_id = self._id_factory("binding")
            conn.execute(
                """INSERT INTO transport_bindings
                   (id, transport, external_scope, thread_id, revision,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?)""",
                (binding_id, transport, digest, thread_id, now, now),
            )
            self._audit(
                conn,
                "thread",
                thread_id,
                "transport.bound",
                {"binding_id": binding_id, "transport": transport},
            )
            value = self._binding(conn, binding_id)
            return self._save_receipt(
                conn, actor_id, operation, idempotency_key, request, value, 201
            )

    def thread_has_transport_binding(self, thread_id: str) -> bool:
        """Whether any transport scope is bound to this thread.

        ⟦P8⟧ The turn bridge asks this under a closed dispatch gate: a thread
        a transport can deliver to must stay write-free there (a `run.failed`
        would be delivered), a thread nothing can deliver to gets its run
        ended typed instead of left queued.
        """

        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM transport_bindings WHERE thread_id = ? LIMIT 1",
                (thread_id,),
            ).fetchone()
            return row is not None

    def resolve_transport(self, *, transport: str, external_scope: str) -> JsonObject | None:
        digest = self._scope_digest(external_scope)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id FROM transport_bindings
                   WHERE transport = ? AND external_scope = ?""",
                (transport, digest),
            ).fetchone()
            return self._binding(conn, str(row["id"])) if row else None

    def record_transport_command(
        self,
        *,
        transport: str,
        command_key: str,
        request_hash: str,
        response: TransportCommandResponse,
    ) -> TransportCommandReceipt:
        transport = self._transport_name(transport)
        self._validate_key(command_key)
        request_hash = self._transport_digest(request_hash, "request_hash")
        response = self._transport_command_response(response)
        response_json = self._json(asdict(response))
        with self._transaction() as conn:
            existing = self._transport_command_row(conn, transport, command_key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict()
                return existing
            now = self._now()
            conn.execute(
                """INSERT INTO transport_command_receipts(
                       transport, command_key, request_hash, response_json, created_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (transport, command_key, request_hash, response_json, now),
            )
            receipt = self._transport_command_row(conn, transport, command_key)
            if receipt is None:  # pragma: no cover - guarded by the transaction
                raise RuntimeError("transport command receipt was not persisted")
            return receipt

    def get_transport_command(
        self,
        *,
        transport: str,
        command_key: str,
        request_hash: str,
    ) -> TransportCommandReceipt | None:
        transport = self._transport_name(transport)
        self._validate_key(command_key)
        request_hash = self._transport_digest(request_hash, "request_hash")
        with self._connect() as conn:
            receipt = self._transport_command_row(conn, transport, command_key)
        if receipt is not None and receipt.request_hash != request_hash:
            raise IdempotencyConflict()
        return receipt

    def get_workspace(self, workspace_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._workspace(conn, workspace_id)

    def get_thread(self, thread_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._thread(conn, thread_id)

    def get_run(self, run_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._run(conn, run_id)

    def read_research_workflow_snapshot(self, run_id: str) -> JsonObject:
        """Read one run's research aggregate from a single SQLite snapshot."""

        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                run = self._run(conn, run_id)
                workflow_row = conn.execute(
                    "SELECT id FROM workflow_instances WHERE run_id = ?", (run_id,)
                ).fetchone()
                workflow = (
                    self._workflow(conn, str(workflow_row["id"]))
                    if workflow_row is not None
                    else None
                )
                effects: list[JsonObject] = []
                if workflow is not None:
                    effect_rows = conn.execute(
                        """SELECT id FROM workflow_effect_commands
                           WHERE workflow_id = ? ORDER BY created_at, id
                           LIMIT ?""",
                        (workflow["id"], _RESEARCH_PROJECTION_LIMIT + 1),
                    ).fetchall()
                    effects = [
                        self._workflow_effect(conn, str(row["id"]))
                        for row in effect_rows
                    ]

                intent_rows = conn.execute(
                    """SELECT id FROM source_intents WHERE run_id = ?
                       ORDER BY created_at, id LIMIT ?""",
                    (run_id, _RESEARCH_PROJECTION_LIMIT + 1),
                ).fetchall()
                binding_rows = conn.execute(
                    """SELECT id FROM run_source_bindings WHERE run_id = ?
                       ORDER BY created_at, id LIMIT ?""",
                    (run_id, _RESEARCH_PROJECTION_LIMIT + 1),
                ).fetchall()
                source_bindings = [
                    self._run_source_binding(conn, str(row["id"]))
                    for row in binding_rows
                ]
                context = self._research_context(conn, run_id)
                if context is not None:
                    if context["thread_id"] != run["thread_id"]:
                        raise InvalidTransition("research_context_integrity", "research_read")
                    self._validate_research_sources(conn, context["snapshot"])
                    if context["snapshot"].get("documents"):
                        self._validate_research_documents(
                            conn, context["snapshot"], thread_id=str(run["thread_id"]), admission=False
                        )
                    by_source = {item["source"]["id"]: item for item in source_bindings}
                    for selected in context["snapshot"]["sources"]:
                        source_id = selected["source_id"]
                        binding = by_source.get(source_id)
                        if binding is None:
                            identity = hashlib.sha256(
                                f"research-selection:{run_id}:{source_id}".encode("utf-8")
                            ).hexdigest()
                            binding = {
                                "id": f"research_selection_{identity}",
                                "disposition": "research_selected",
                                "created_at": context["created_at"],
                                "source": self._source(conn, source_id),
                            }
                            source_bindings.append(binding)
                        # A projection of immutable context authority, never a
                        # fabricated source resolution or approval record.
                        binding["research_selection"] = {
                            "kind": "research_context",
                            "run_id": run_id,
                            "message_id": context["message_id"],
                            "authority_message_id": context["snapshot"]["authority"]["message_id"],
                            "context_sha256": context["sha256"],
                            "label": selected["label"],
                        }
                decision_rows = conn.execute(
                    """SELECT id FROM decisions WHERE run_id = ?
                       ORDER BY created_at, id LIMIT ?""",
                    (run_id, _RESEARCH_PROJECTION_LIMIT + 1),
                ).fetchall()
                artifact_rows = conn.execute(
                    """SELECT DISTINCT artifacts.id
                       FROM artifacts
                       JOIN artifact_versions
                         ON artifact_versions.artifact_id = artifacts.id
                       WHERE artifact_versions.run_id = ?
                       ORDER BY artifacts.created_at, artifacts.id LIMIT ?""",
                    (run_id, _RESEARCH_PROJECTION_LIMIT + 1),
                ).fetchall()
                artifacts: list[JsonObject] = []
                for row in artifact_rows:
                    artifact = self._artifact(conn, str(row["id"]))
                    version_rows = conn.execute(
                        """SELECT id FROM artifact_versions
                           WHERE artifact_id = ? AND state = 'committed'
                           ORDER BY logical_version, id LIMIT ?""",
                        (artifact["id"], _RESEARCH_PROJECTION_LIMIT + 1),
                    ).fetchall()
                    artifact["versions"] = [
                        self._artifact_version(
                            conn, str(version_row["id"]), include_action=False
                        )
                        for version_row in version_rows
                    ]
                    artifacts.append(artifact)

                thread = self._thread(conn, str(run["thread_id"]))
                snapshot_rows = conn.execute(
                    """SELECT id FROM artifact_snapshots
                       WHERE workspace_id = ? AND state = 'committed'
                       ORDER BY created_at, id LIMIT ?""",
                    (thread["workspace_id"], _RESEARCH_PROJECTION_LIMIT + 1),
                ).fetchall()
                value: JsonObject = {
                    "run": run,
                    "workflow": workflow,
                    "effects": effects,
                    "source_intents": [
                        self._source_intent(conn, str(row["id"]))
                        for row in intent_rows
                    ],
                    "source_bindings": source_bindings,
                    "decisions": [
                        self._decision(conn, str(row["id"]))
                        for row in decision_rows
                    ],
                    "artifacts": artifacts,
                    "snapshots": [
                        self._artifact_snapshot(conn, str(row["id"]))
                        for row in snapshot_rows
                    ],
                }
                conn.commit()
                return value
            except BaseException:
                conn.rollback()
                raise

    def get_attempt(self, attempt_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._attempt(conn, attempt_id)

    def get_runtime_binding(self, binding_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._runtime_binding(conn, binding_id)

    def latest_runtime_binding(
        self, *, thread_id: str, adapter_id: str
    ) -> JsonObject | None:
        with self._connect() as conn:
            self._thread(conn, thread_id)
            row = conn.execute(
                """SELECT id FROM runtime_bindings
                   WHERE thread_id = ? AND adapter_id = ?
                   ORDER BY generation DESC, created_at DESC, id DESC LIMIT 1""",
                (thread_id, adapter_id),
            ).fetchone()
            return (
                self._runtime_binding(conn, str(row["id"]))
                if row is not None
                else None
            )

    #: ⟦P8 V6-2 / V-R4⟧ A machine thread, as a predicate over `threads t`: the
    #: thread's `create_thread` receipt names the capture consumer's actor
    #: (who created it -- the same receipt mechanism `run_creator` reads), or
    #: a thread any run of which ever carried a workflow instance. Rows every
    #: generation wrote, so a carrier a pre-V-3 consumer left between its two
    #: transactions -- no workflow row, on a thread an operator may since have
    #: typed into -- is covered as well as one gen 13 creates; and a thread an
    #: operator opened in the consumer's workspace is not.
    _MACHINE_THREAD_PREDICATE = f"""
        EXISTS (SELECT 1 FROM idempotency_receipts ir
                WHERE ir.actor_id = '{CAPTURE_CONSUMER_ACTOR}'
                  AND ir.operation = 'POST:/api/v1/workspaces/' || t.workspace_id || '/threads'
                  AND {_receipt_subject_id("ir.response_json")} = t.id)
        OR EXISTS (SELECT 1 FROM runs r2
                   JOIN workflow_instances w2 ON w2.run_id = r2.id
                   WHERE r2.thread_id = t.id)
    """

    #: ⟦P8⟧ What makes a run a CONVERSATION run -- one the turn bridge may
    #: drive -- as a predicate over `runs r`. Durable, never sampled, and true
    #: of rows every generation wrote. Two exclusions: the run owns a
    #: workflow instance (⟦V-3⟧ `create_run(workflow=...)` installs it in
    #: the transaction that creates a gen-13 carrier, so there is no instant
    #: at which such a carrier lacks one), or ⟦V6-2⟧ its thread is a machine
    #: thread (`_MACHINE_THREAD_PREDICATE`) -- which is what excludes a
    #: carrier a pre-V-3 consumer left without its workflow, and a run an
    #: operator once opened on a capture thread. Then what a run with no
    #: workflow still needs to be a conversation at all: a thread an operator
    #: wrote into, or a transport scope is bound to. The thread's messages are
    #: NOT what excludes a carrier: the API stores an operator's message on a
    #: `capture` thread (and declines to submit it), so a carrier thread may
    #: well hold one.
    _CONVERSATION_RUN_PREDICATE = f"""
        NOT EXISTS (SELECT 1 FROM workflow_instances w WHERE w.run_id = r.id)
        AND NOT EXISTS (SELECT 1 FROM threads t
                        WHERE t.id = r.thread_id
                          AND ({_MACHINE_THREAD_PREDICATE}))
        AND ({_drivable_thread_predicate("r.thread_id")})
    """

    def thread_is_machine(self, thread_id: str) -> bool:
        """Whether this thread belongs to the research engine.

        ⟦V6-2 / V6-3⟧ `_MACHINE_THREAD_PREDICATE`, asked by the API before it
        creates or drives a run on a thread (a machine thread stays a machine
        thread: typed 409), by the bridge before it would create one, and
        folded into `run_is_conversation` so a workflow-less run on such a
        thread is never a conversation run whatever the thread's messages say.
        """

        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT 1 FROM threads t
                    WHERE t.id = ? AND ({self._MACHINE_THREAD_PREDICATE})""",
                (thread_id,),
            ).fetchone()
            return row is not None

    def machine_thread_ids(
        self, workspace_id: str, *, thread_ids: Sequence[str] | None = None
    ) -> set[str]:
        """Which of these threads belong to the engine, in ONE query.

        ⟦A-1⟧ `thread_is_machine` asked per row is the same answer at a cost
        that is linear in the size of the list and is nearly all connection
        setup -- `_connect` runs three PRAGMAs, `synchronous = FULL` among
        them, on every call. The route that needs this needs it for EVERY
        thread it returns, so it is asked once rather than once per row.

        ⟦batchK-8⟧ `thread_ids` scopes the answer to the PAGE the caller is
        about to project, exactly as `run_ids` does for `machine_run_ids`, and
        for the same reason: `_MACHINE_THREAD_PREDICATE`'s first half is a
        scan of `idempotency_receipts` per thread it is asked about -- there
        is no index on `json_extract(response_json, '$.id')` and adding one
        would be a migration -- so asking for a whole workspace to project
        one page multiplies a growing list by a growing table. Without it the
        whole workspace is asked for, which is what a caller with no page
        wants.

        The same `_MACHINE_THREAD_PREDICATE` in the same place: the id set is
        identical to filtering `thread_is_machine` over those threads by
        construction, not by coincidence.
        """

        with self._connect() as conn:
            self._workspace(conn, workspace_id)
            if thread_ids is None:
                rows = conn.execute(
                    f"""SELECT t.id FROM threads t
                        WHERE t.workspace_id = ?
                          AND ({self._MACHINE_THREAD_PREDICATE})""",
                    (workspace_id,),
                ).fetchall()
                return {str(row["id"]) for row in rows}
            identifiers = [str(thread_id) for thread_id in thread_ids]
            if not identifiers:
                return set()
            placeholders = ",".join("?" for _ in identifiers)
            rows = conn.execute(
                f"""SELECT t.id FROM threads t
                    WHERE t.workspace_id = ? AND t.id IN ({placeholders})
                      AND ({self._MACHINE_THREAD_PREDICATE})""",
                (workspace_id, *identifiers),
            ).fetchall()
            return {str(row["id"]) for row in rows}

    #: ⟦P9-2⟧ A machine workspace, as a predicate over `workspaces w`: the
    #: workspace's `create_workspace` receipt names the capture consumer's
    #: actor. The receipt-actor half of `_MACHINE_THREAD_PREDICATE`, over the
    #: operation `create_workspace` files -- a constant, since a workspace has
    #: no parent in its path. There is deliberately no second half: a
    #: workspace holding a machine thread is not thereby the engine's, because
    #: the consumer will happily carry a capture on a thread it created inside
    #: a workspace an operator made first (`capture_consumer._workspace` adopts
    #: by title, and files no receipt when it adopts). Whose the workspace is
    #: is who made it, and nothing else.
    _MACHINE_WORKSPACE_PREDICATE = f"""
        EXISTS (SELECT 1 FROM idempotency_receipts ir
                WHERE ir.actor_id = '{CAPTURE_CONSUMER_ACTOR}'
                  AND ir.operation = 'POST:/api/v1/workspaces'
                  AND {_receipt_subject_id("ir.response_json")} = w.id)
    """

    def workspace_is_machine(self, workspace_id: str) -> bool:
        """Whether the research engine created this workspace.

        ⟦P9-2⟧ `thread_is_machine`'s twin, read on every route that returns a
        workspace so the cockpit can keep the engine's own `Capture consumer`
        workspace out of the operator's picker. Computed from receipts on
        read, never stored, and no migration: the rows already exist for every
        workspace the consumer ever created.
        """

        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT 1 FROM workspaces w
                    WHERE w.id = ? AND ({self._MACHINE_WORKSPACE_PREDICATE})""",
                (workspace_id,),
            ).fetchone()
            return row is not None

    def machine_workspace_ids(self) -> frozenset[str]:
        """Every engine-created workspace id, in ONE query on ONE connection.

        ⟦A-1⟧ `workspace_is_machine` opens a fresh connection per call, and
        `_connect` runs `PRAGMA foreign_keys`, `PRAGMA busy_timeout` and
        `PRAGMA synchronous = FULL` on every one of them -- so asking it once
        per row turns a list route into N connection setups, which the review
        measured at ~75x on 100 threads for the thread equivalent. The list
        route asks this once and projects from the answer; the per-id form
        stays for the single GET and the create response, where it is one call
        either way.
        """

        with self._connect() as conn:
            return frozenset(
                str(row["id"])
                for row in conn.execute(
                    f"""SELECT w.id FROM workspaces w
                        WHERE ({self._MACHINE_WORKSPACE_PREDICATE})"""
                )
            )

    def workspace_creator(self, workspace_id: str) -> str | None:
        """The actor whose `create_workspace` receipt names it, or None.

        ⟦P9-2⟧ `thread_creator`'s twin. `None` for a workspace no receipt
        names -- a row written directly, or one from before the receipt
        existed -- which reads as "not the engine's", the same way an
        unnamed thread does.
        """

        with self._connect() as conn:
            self._workspace(conn, workspace_id)
            row = conn.execute(
                f"""SELECT actor_id FROM idempotency_receipts
                   WHERE operation = 'POST:/api/v1/workspaces'
                     AND {_receipt_subject_id("response_json")} = ?
                   ORDER BY created_at, actor_id LIMIT 1""",
                (workspace_id,),
            ).fetchone()
            return None if row is None else str(row["actor_id"])

    def thread_can_carry_a_turn(self, thread_id: str) -> bool:
        """Whether a run created on this thread would have anything to answer.

        ⟦P9-2 / V-2⟧ `_drivable_thread_predicate`, the same rule
        `run_is_conversation` applies to an existing run, asked BEFORE one is
        created. "Start research run" on a thread nobody has written to used
        to commit a `queued` run the bridge would never pick up -- not a
        conversation run, so never driven -- which then sat on the thread as
        its active run until a message or a cancel cleared it.
        """

        with self._connect() as conn:
            # ⟦ADJ-C⟧ Existence and drivability in ONE query, and they are
            # different answers: `WHERE id = ? AND (<drivable>)` says False for
            # a thread that is not there, which turned a 404 into a 409 saying
            # the thread had nothing to answer. A missing thread is `NotFound`,
            # the same answer `create_run` would have given.
            row = conn.execute(
                f"""SELECT ({_drivable_thread_predicate("t.id")}) AS drivable
                    FROM threads t WHERE t.id = ?""",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise NotFound("thread", thread_id)
            return bool(row["drivable"])

    def run_creator(self, run_id: str) -> str | None:
        """The actor whose `create_run` receipt names this run, or None.

        ⟦V6-3⟧ Receipts are the one durable record of who created a run
        (`runs` has no actor column, and no migration is allowed), and a
        command receipt is never pruned. The capture consumer repairs only its
        OWN workflow-less carrier -- a run whose receipt carries its machine
        actor -- and never a run somebody else opened on its thread.
        """

        with self._connect() as conn:
            run = self._run(conn, run_id)
            row = conn.execute(
                f"""SELECT actor_id FROM idempotency_receipts
                   WHERE operation = ?
                     AND {_receipt_subject_id("response_json")} = ?
                   ORDER BY created_at, actor_id LIMIT 1""",
                (f"POST:/api/v1/threads/{run['thread_id']}/runs", run_id),
            ).fetchone()
            return None if row is None else str(row["actor_id"])

    def thread_creator(self, thread_id: str) -> str | None:
        """The actor whose `create_thread` receipt names this thread, or None.

        ⟦ADJ-2⟧ `run_creator`'s twin for threads, over the same receipt rows
        `_MACHINE_THREAD_PREDICATE`'s first half reads: the capture consumer
        adopts an existing `capture <id>` thread only when this names its
        own actor, so the thread it carries a capture on is always one it
        created. A thread that does not exist is `NotFound`.
        """

        with self._connect() as conn:
            thread = self._thread(conn, thread_id)
            row = conn.execute(
                f"""SELECT actor_id FROM idempotency_receipts
                   WHERE operation = ?
                     AND {_receipt_subject_id("response_json")} = ?
                   ORDER BY created_at, actor_id LIMIT 1""",
                (
                    f"POST:/api/v1/workspaces/{thread['workspace_id']}/threads",
                    thread_id,
                ),
            ).fetchone()
            return None if row is None else str(row["actor_id"])

    def run_owns_workflow(self, run_id: str) -> bool:
        """Whether a workflow instance is installed on this run.

        ⟦P8 N-1⟧ The research engine's carrier owns one from the transaction
        that creates it (⟦V-3⟧), and a terminal carrier fences that workflow
        for ever (`_expect_workflow_run_open`): the capture could never be
        claimed again. So a run that owns a workflow is the engine's to end,
        and the control API asks this before a cancel or pause is written.
        A run that does not exist is `NotFound`, as the command would say.
        """

        with self._connect() as conn:
            self._run(conn, run_id)
            row = conn.execute(
                "SELECT 1 FROM workflow_instances WHERE run_id = ?", (run_id,)
            ).fetchone()
            return row is not None

    #: ⟦ADJ-A⟧ A machine RUN, as a predicate over `runs r` -- exactly what
    #: `ControlAPI._machine_run_refusal` asks (through `run_is_machine`)
    #: before it refuses a cancel or a pause: the run owns a workflow
    #: instance, OR its `create_run` receipt names the capture consumer.
    #: Spelled as SQL so a whole list can be asked at once, and asked by the
    #: refusal itself so the cockpit hides exactly the buttons the API would
    #: refuse -- no more. See `_machine_run_predicate` for why the store's own
    #: guard reads the narrower `_ENGINE_OWNED_RUN_PREDICATE` beside it.
    _MACHINE_RUN_PREDICATE = _machine_run_predicate(workflow_definition_id=None)

    #: ⟦ADJ-4 / batchK-9⟧ The same predicate with its workflow half narrowed
    #: to the capture workflow: what `transition_run` refuses a cancel or a
    #: pause on for any actor but the consumer's own, at every door rather
    #: than only the route's. Built from the one source above so the two
    #: cannot drift; the difference between them is one clause, argued there,
    #: and pinned by test.
    _ENGINE_OWNED_RUN_PREDICATE = _machine_run_predicate(
        workflow_definition_id=CAPTURE_WORKFLOW_DEFINITION_ID
    )

    def run_is_machine(self, run_id: str) -> bool:
        """Whether this run is the research engine's to end.

        ⟦ADJ-A⟧ `_MACHINE_RUN_PREDICATE` for one run: what the cockpit needs
        to know before it offers Cancel, and the same answer the API gives when
        it is pressed. A run that does not exist is `NotFound`, as the command
        would say.
        """

        with self._connect() as conn:
            self._run(conn, run_id)
            row = conn.execute(
                f"""SELECT 1 FROM runs r
                    WHERE r.id = ? AND ({self._MACHINE_RUN_PREDICATE})""",
                (run_id,),
            ).fetchone()
            return row is not None

    def machine_run_ids(
        self, thread_id: str, *, run_ids: Sequence[str] | None = None
    ) -> set[str]:
        """Which of these runs are the engine's, in ONE query.

        ⟦ADJ-A + A-1⟧ The run-history route needs ownership for every run it
        returns, so it asks once rather than twice per row --
        `run_owns_workflow` and `run_creator` are a fresh connection each, and
        `_connect` runs three PRAGMAs every time. Same predicate, same answer,
        by construction.

        ⟦ADJ-G-4⟧ `run_ids` scopes the answer to the PAGE the caller is about
        to project. The route is paginated (`after_id`, `limit`) but the thread
        is not: a capture carrier thread accumulates runs for ever, so asking
        for the whole thread to project a hundred rows would reintroduce, in
        the predicate, the unbounded scan the batching removed. Without it the
        whole thread is asked for, which is what a caller with no page wants.
        """

        with self._connect() as conn:
            self._thread(conn, thread_id)
            if run_ids is None:
                rows = conn.execute(
                    f"""SELECT r.id FROM runs r
                        WHERE r.thread_id = ? AND ({self._MACHINE_RUN_PREDICATE})""",
                    (thread_id,),
                ).fetchall()
                return {str(row["id"]) for row in rows}
            identifiers = [str(run_id) for run_id in run_ids]
            if not identifiers:
                return set()
            placeholders = ",".join("?" for _ in identifiers)
            rows = conn.execute(
                f"""SELECT r.id FROM runs r
                    WHERE r.thread_id = ? AND r.id IN ({placeholders})
                      AND ({self._MACHINE_RUN_PREDICATE})""",
                (thread_id, *identifiers),
            ).fetchall()
            return {str(row["id"]) for row in rows}

    @classmethod
    def _engine_owned_run(
        cls, conn: sqlite3.Connection, run: Mapping[str, Any]
    ) -> bool:
        """Whether this run is the research engine's to end, and nobody else's.

        ⟦ADJ-4 / batchK-9⟧ `_ENGINE_OWNED_RUN_PREDICATE`, asked inside the
        transaction that is about to write, on the connection that holds it.
        It used to be a second hand-written copy of the same two facts, in
        Python, with a receipt half that read "any receipt by the consumer"
        where the route's read "the first by (created_at, actor_id)" -- two
        spellings of one rule, free to drift, and drifted. Both are now
        generated from `_machine_run_predicate`, which names the single
        clause that still differs and why it does.

        Never the thread: a workflow-less run an operator opened on a
        `capture` thread is theirs, and its cancel is the `ForeignCarrierRun`
        recovery. The engine's own actor is exempt at the call site, which is
        how `capture_consumer._close_run` still ends its carrier.
        """

        row = conn.execute(
            f"""SELECT 1 FROM runs r
                WHERE r.id = ? AND ({cls._ENGINE_OWNED_RUN_PREDICATE})""",
            (run["id"],),
        ).fetchone()
        return row is not None

    def run_is_conversation(self, run_id: str) -> bool:
        """Whether this run is one the turn bridge may drive at all.

        ⟦P8⟧ One predicate for every entry -- the API's follow-up submit, the
        bridge's drive path, its gate-closed ending and its sweep -- so a run
        the engine owns is never driven or ended by the bridge from any of
        them. See `_CONVERSATION_RUN_PREDICATE`.
        """

        with self._connect() as conn:
            row = conn.execute(
                f"""SELECT 1 FROM runs r
                    WHERE r.id = ? AND {self._CONVERSATION_RUN_PREDICATE}""",
                (run_id,),
            ).fetchone()
            return row is not None

    def list_recoverable_runs(
        self, *, conversations_only: bool = False
    ) -> list[JsonObject]:
        """Runs a restarted daemon has to converge, and only those.

        ⟦P5.4d⟧ `waiting_for_decision` is excluded. A parked run is waiting for
        the OPERATOR, not for a recovery: it has already committed
        `decision.required`, the transport has already rendered the card, and
        converging it at the next daemon start would answer away the very
        question the operator was asked. It became reachable the moment the
        approval callback did.

        ⟦P8⟧ `conversations_only` is the turn bridge's form: only runs it may
        drive (`run_is_conversation`), decided in the query so a run the
        engine owns is never even projected for it. `recover_startup` keeps
        the full set.
        """

        predicate = (
            f"AND {self._CONVERSATION_RUN_PREDICATE}" if conversations_only else ""
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT r.id FROM runs r
                    WHERE r.state NOT IN (
                        'completed', 'failed', 'canceled', 'paused',
                        'waiting_for_decision'
                    )
                    {predicate}
                    ORDER BY r.created_at, r.id"""
            ).fetchall()
            return [self._run(conn, str(row["id"])) for row in rows]

    def get_decision(self, decision_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._decision(conn, decision_id)

    def get_runtime_action(self, action_id: str) -> JsonObject:
        with self._connect() as conn:
            return self._runtime_action(conn, action_id)

    def list_workspaces(self) -> list[JsonObject]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM workspaces ORDER BY created_at, id"
            ).fetchall()
            return [self._workspace(conn, str(row["id"])) for row in rows]

    def list_threads(
        self,
        *,
        workspace_id: str,
        after_id: str | None = None,
        limit: int | None = None,
        include_archived: bool = False,
    ) -> _CursorPage:
        """This workspace's threads, oldest first, optionally one page at a time.

        ⟦batchK-8⟧ The list a workspace's rail is built from grows by one
        carrier thread per capture for ever and was never pruned, paged or
        bounded: `GET /threads` read every row of it and hard-coded
        `next_cursor` to null, so the only way to see a workspace at all was
        to read all of it. `limit` bounds a page and `after_id` continues one,
        keyset over `(created_at, id)` -- the SAME order the unpaged list has
        always returned, and a total one because `id` is unique, so no row is
        skipped or repeated when a thread is created between two pages.

        Archived threads are hidden unless `include_archived` asks for them;
        they keep their place in the same `(created_at, id)` order, so a page
        that includes them pages exactly like one that does not.

        `limit=None` is the historical whole-list answer, unchanged and still
        the default: the cockpit's `listThreads` sends no `limit`, the
        Telegram thread list and the capture consumer's adoption scan iterate
        the result as the plain list it still is. A caller that pages gets a
        `next_cursor` naming the last thread of the page, and null on the last
        page -- proven by reading one row further, never guessed from the page
        being full.

        The cursor is the thread's own id, as on every other paged route here
        (`list_thread_runs`, `list_artifacts`, `list_captures`): opaque to the
        client, which may only hand it back, and resolved against this
        workspace -- a cursor naming another workspace's thread is a
        `ValueError`, not a silently empty page.
        """

        if limit is not None and (
            type(limit) is not int or not 1 <= limit <= THREAD_PAGE_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {THREAD_PAGE_LIMIT}")
        with self._connect() as conn:
            self._workspace(conn, workspace_id)
            after_created_at: str | None = None
            if after_id is not None:
                after = self._thread(conn, after_id)
                if after["workspace_id"] != workspace_id:
                    raise ValueError("after_id does not belong to workspace")
                after_created_at = str(after["created_at"])
            rows = conn.execute(
                """SELECT id FROM threads
                   WHERE workspace_id = ?
                     AND (? = 1 OR archived_at IS NULL)
                     AND (
                       ? IS NULL
                       OR created_at > ?
                       OR (created_at = ? AND id > ?)
                     )
                   ORDER BY created_at, id LIMIT ?""",
                (
                    workspace_id,
                    1 if include_archived else 0,
                    after_created_at,
                    after_created_at,
                    after_created_at,
                    after_id,
                    -1 if limit is None else limit + 1,
                ),
            ).fetchall()
            has_more = limit is not None and len(rows) > limit
            page = rows if limit is None else rows[:limit]
            items = [self._thread(conn, str(row["id"])) for row in page]
            return _CursorPage(
                items,
                next_cursor=str(items[-1]["id"]) if has_more and items else None,
            )

    def list_thread_runs(
        self,
        *,
        thread_id: str,
        after_id: str | None = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as conn:
            self._thread(conn, thread_id)
            after_created_at: str | None = None
            if after_id is not None:
                after = self._run(conn, after_id)
                if after["thread_id"] != thread_id:
                    raise ValueError("after_id does not belong to thread")
                after_created_at = self._parse_control_time(
                    str(after["created_at"])
                ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            rows = conn.execute(
                f"""SELECT id FROM runs
                   WHERE thread_id = ?
                     AND (
                       ? IS NULL
                       OR {_RUN_HISTORY_TIME_SQL} < ?
                       OR ({_RUN_HISTORY_TIME_SQL} = ? AND id < ?)
                     )
                   ORDER BY {_RUN_HISTORY_TIME_SQL} DESC, id DESC LIMIT ?""",
                (
                    thread_id,
                    after_created_at,
                    after_created_at,
                    after_created_at,
                    after_id,
                    limit + 1,
                ),
            ).fetchall()
            has_more = len(rows) > limit
            items = [self._run(conn, str(row["id"])) for row in rows[:limit]]
            return _CursorPage(
                items,
                next_cursor=str(items[-1]["id"]) if has_more and items else None,
            )

    def list_decisions(self, *, state: str = "pending") -> list[JsonObject]:
        if state not in {"pending", "resolved", "expired"}:
            raise ValueError("decision state is invalid")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id FROM decisions WHERE state = ?
                   ORDER BY created_at, id""",
                (state,),
            ).fetchall()
            return [self._decision(conn, str(row["id"])) for row in rows]

    def list_messages(self, thread_id: str) -> list[JsonObject]:
        with self._connect() as conn:
            self._thread(conn, thread_id)
            rows = conn.execute(
                "SELECT id FROM messages WHERE thread_id = ? ORDER BY position",
                (thread_id,),
            ).fetchall()
            return [self._message(conn, str(row["id"])) for row in rows]

    def list_run_events(self, run_id: str, *, after_sequence: int = 0) -> list[JsonObject]:
        with self._connect() as conn:
            self._run(conn, run_id)
            rows = conn.execute(
                """SELECT * FROM run_events
                   WHERE run_id = ? AND sequence > ? ORDER BY sequence""",
                (run_id, after_sequence),
            ).fetchall()
            return [self._event(row) for row in rows]

    def latest_event_cursor(self) -> int:
        """The head of the global event stream, without reading any event.

        A daemon that starts mid-history must not treat every notification event
        ever recorded as undelivered work: the transport ledger would dedupe the
        sends, but only after deciding to make them. Starting from the head is
        what makes "deliver what happens from now on" expressible.
        """

        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(global_cursor) AS head FROM run_events"
            ).fetchone()
        return 0 if row is None or row["head"] is None else int(row["head"])

    def get_run_event(self, event_id: str) -> JsonObject:
        """One durable event by id, for a caller that has only the id.

        The delivery ledger keys on `event_id` and nothing else, so resuming an
        interrupted delivery after a restart means finding the event again.
        """

        event_id = self._required_text(event_id, "event_id", maximum=500)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise NotFound("run event", event_id)
            return self._event(row)

    def list_events(self, *, after_cursor: int = 0, limit: int = 500) -> list[JsonObject]:
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM run_events WHERE global_cursor > ?
                   ORDER BY global_cursor LIMIT ?""",
                (after_cursor, limit),
            ).fetchall()
            return [self._event(row) for row in rows]

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        attempt_id: str | None,
        event_type: str,
        payload: Mapping[str, Any],
        causation_id: str | None = None,
    ) -> JsonObject:
        run = self._run(conn, run_id)
        sequence = int(run["latest_sequence"]) + 1
        now = self._now()
        event_id = self._id_factory("event")
        cursor = conn.execute(
            """INSERT INTO run_events
               (id, schema_version, run_id, attempt_id, sequence, type,
                occurred_at, causation_id, durability, payload_json)
               VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'durable', ?)""",
            (
                event_id,
                run_id,
                attempt_id,
                sequence,
                event_type,
                now,
                causation_id,
                self._json(dict(payload)),
            ),
        ).lastrowid
        conn.execute(
            "UPDATE runs SET latest_sequence = ? WHERE id = ?",
            (sequence, run_id),
        )
        return {
            "cursor": int(cursor),
            "schema_version": 1,
            "id": event_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "sequence": sequence,
            "type": event_type,
            "occurred_at": now,
            "causation_id": causation_id,
            "durability": "durable",
            "payload": dict(payload),
        }

    def _prepare_directory(self) -> None:
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = parent.lstat()
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) & 0o077
        ):
            raise PermissionError("control store directory must be private and user-owned")

    @contextlib.contextmanager
    def _initialization_guard(self) -> Iterator[None]:
        lock_path = self.path.with_name(f".{self.path.name}.initialize.lock")
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            lock_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.geteuid()
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
                or lock_stat.st_nlink != 1
            ):
                raise PermissionError(
                    "control store initialization lock must be private and user-owned"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _prepare_database_file(self) -> None:
        try:
            file_stat = self.path.lstat()
        except FileNotFoundError:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
            file_stat = self.path.lstat()
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
        ):
            raise PermissionError("control store file must be private and user-owned")

    def _load_or_create_binding_key(self) -> bytes:
        path = self.path.with_name(f".{self.path.name}.transport.key")
        database_has_state = self.path.stat().st_size > 0
        try:
            path.lstat()
        except FileNotFoundError:
            if database_has_state:
                raise PermissionError(
                    "transport binding key is missing for an existing control store"
                )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            descriptor = -1
        if descriptor >= 0:
            try:
                key = secrets.token_bytes(32)
                os.write(descriptor, key)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            directory_descriptor = os.open(self.path.parent, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        read_flags = os.O_RDONLY
        read_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, read_flags)
        try:
            key_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(key_stat.st_mode)
                or key_stat.st_uid != os.geteuid()
                or stat.S_IMODE(key_stat.st_mode) != 0o600
                or key_stat.st_nlink != 1
            ):
                raise PermissionError(
                    "transport binding key must be private and user-owned"
                )
            key = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if len(key) != 32:
            raise PermissionError("transport binding key is invalid")
        return key

    def cursor_signing_key(self) -> bytes:
        """Derive a stable, domain-separated key for opaque event cursors."""

        if self._binding_key is None:
            raise RuntimeError("control store must be initialized before use")
        return hmac.new(
            self._binding_key,
            b"cortex-event-cursor-v1",
            hashlib.sha256,
        ).digest()

    def transport_derived_key(self, purpose: str) -> bytes:
        """A domain-separated key for a transport, derived from the store's own.

        The Telegram adapter needs a signing key and an identity key. Neither
        may come from configuration -- they are secrets, and D-P5-4 keeps
        secrets out of every file the product writes -- and neither should be a
        second key file to lose. Deriving them from the binding key that
        already lives beside the database, domain-separated per purpose, gives
        one thing to back up rather than three.
        """

        if self._binding_key is None:
            raise RuntimeError("control store must be initialized before use")
        if not purpose or len(purpose) > 100:
            raise ValueError("purpose is invalid")
        return hmac.new(
            self._binding_key,
            f"cortex-transport-{purpose}-v1".encode("utf-8"),
            hashlib.sha256,
        ).digest()

    def _scope_digest(self, external_scope: str) -> str:
        if self._binding_key is None:
            raise RuntimeError("control store must be initialized before use")
        digest = hmac.new(
            self._binding_key,
            external_scope.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

    def _runtime_event_replay(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str | None,
        adapter_event_id: str | None,
        adapter_event_sequence: int | None,
        request: Mapping[str, Any],
    ) -> CommandResult | None:
        if adapter_event_id is None:
            return None
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("attempt_id is required for a runtime event")
        self._validate_adapter_event_id(adapter_event_id)
        if adapter_event_sequence is None:
            raise ValueError("adapter_event_sequence is required for a runtime event")
        if (
            type(adapter_event_sequence) is not int
            or adapter_event_sequence < 0
        ):
            raise ValueError("adapter_event_sequence must be non-negative")
        row = conn.execute(
            """SELECT adapter_event_sequence, payload_hash, status_code,
                      response_json
               FROM runtime_event_inbox
               WHERE attempt_id = ? AND adapter_event_id = ?""",
            (attempt_id, adapter_event_id),
        ).fetchone()
        if row is not None:
            logical_request = {
                key: value
                for key, value in request.items()
                if key != "expected_revision"
            }
            if (
                row["adapter_event_sequence"] != adapter_event_sequence
                or row["payload_hash"] != self._request_hash(logical_request)
            ):
                raise IdempotencyConflict()
            return CommandResult(
                value=json.loads(row["response_json"]),
                status_code=int(row["status_code"]),
                replayed=True,
            )
        if adapter_event_sequence is not None:
            collision = conn.execute(
                """SELECT adapter_event_id FROM runtime_event_inbox
                   WHERE attempt_id = ? AND adapter_event_sequence = ?""",
                (attempt_id, adapter_event_sequence),
            ).fetchone()
            if collision is not None:
                raise IdempotencyConflict()
            latest = int(
                conn.execute(
                    """SELECT COALESCE(MAX(adapter_event_sequence), -1)
                       FROM runtime_event_inbox WHERE attempt_id = ?""",
                    (attempt_id,),
                ).fetchone()[0]
            )
            expected = latest + 1
            if adapter_event_sequence != expected:
                source = (
                    "runtime_event_sequence_gap"
                    if adapter_event_sequence > expected
                    else "stale_runtime_event_sequence"
                )
                raise InvalidTransition(source, "runtime_event_ingest")
        return None

    def _save_runtime_event(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str | None,
        adapter_event_id: str | None,
        adapter_event_sequence: int | None,
        request: Mapping[str, Any],
        result: CommandResult,
    ) -> None:
        if adapter_event_id is None:
            return
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("attempt_id is required for a runtime event")
        self._validate_adapter_event_id(adapter_event_id)
        logical_request = {
            key: value for key, value in request.items() if key != "expected_revision"
        }
        conn.execute(
            """INSERT INTO runtime_event_inbox
               (attempt_id, adapter_event_id, payload_hash, status_code,
                response_json, received_at, adapter_event_sequence)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt_id,
                adapter_event_id,
                self._request_hash(logical_request),
                result.status_code,
                self._json(result.value),
                self._now(),
                adapter_event_sequence,
            ),
        )

    def _claim_replay_is_current(
        self,
        resource: Mapping[str, Any],
        *,
        worker_id: str,
        claim_epoch: Any,
    ) -> bool:
        return bool(
            resource.get("state") == "pending"
            and self._claim_fence_is_current(
                resource,
                worker_id=worker_id,
                claim_epoch=claim_epoch,
            )
        )

    def _claim_fence_is_current(
        self,
        resource: Mapping[str, Any],
        *,
        worker_id: str,
        claim_epoch: Any,
    ) -> bool:
        expires_at = resource.get("claim_expires_at")
        return bool(
            resource.get("claim_owner") == worker_id
            and type(claim_epoch) is int
            and resource.get("claim_epoch") == claim_epoch
            and isinstance(expires_at, str)
            and expires_at > self._now()
        )

    @classmethod
    def _validated_asset_root(
        cls,
        *,
        root_id: object,
        private_path: object,
        max_bytes: object,
        enabled: object,
    ) -> tuple[str, Path, int, bool]:
        validated_id = cls._asset_root_id(root_id)
        if not isinstance(private_path, Path):
            raise TypeError("private_path must be an absolute Path")
        path_text = str(private_path)
        if (
            not private_path.is_absolute()
            or path_text == private_path.anchor
            or "\0" in path_text
        ):
            raise ValueError("private_path must be an absolute non-root path")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        return validated_id, private_path, max_bytes, enabled

    @staticmethod
    def _asset_root_id(value: object) -> str:
        if type(value) is not str or _ASSET_ROOT_ID_RE.fullmatch(value) is None:
            raise ValueError("root_id is invalid")
        return value

    def _asset_root(
        self, conn: sqlite3.Connection, root_id: str
    ) -> AssetRootRecord:
        row = conn.execute(
            """SELECT root_id, private_path, max_bytes, enabled, revision,
                      created_at, updated_at
               FROM asset_roots WHERE root_id = ?""",
            (root_id,),
        ).fetchone()
        if row is None:
            raise NotFound("asset root", root_id)
        return AssetRootRecord(
            root_id=str(row["root_id"]),
            private_path=Path(str(row["private_path"])),
            max_bytes=int(row["max_bytes"]),
            enabled=bool(row["enabled"]),
            revision=int(row["revision"]),
            created_at=self._parse_control_time(str(row["created_at"])),
            updated_at=self._parse_control_time(str(row["updated_at"])),
        )

    @staticmethod
    def _asset_root_response(record: AssetRootRecord) -> JsonObject:
        return {
            "root_id": record.root_id,
            "private_path": str(record.private_path),
            "max_bytes": record.max_bytes,
            "enabled": record.enabled,
            "revision": record.revision,
            "created_at": ControlStore._format_time(record.created_at),
            "updated_at": ControlStore._format_time(record.updated_at),
        }

    @classmethod
    def _asset_root_receipt(cls, value: Mapping[str, Any]) -> AssetRootRecord:
        expected = {
            "root_id",
            "private_path",
            "max_bytes",
            "enabled",
            "revision",
            "created_at",
            "updated_at",
        }
        if set(value) != expected:
            raise RuntimeError("asset root receipt is invalid")
        root_id, private_path, max_bytes, enabled = cls._validated_asset_root(
            root_id=value["root_id"],
            private_path=Path(value["private_path"]),
            max_bytes=value["max_bytes"],
            enabled=value["enabled"],
        )
        revision = value["revision"]
        if type(revision) is not int or revision < 0:
            raise RuntimeError("asset root receipt is invalid")
        try:
            created_at = cls._parse_control_time(value["created_at"])
            updated_at = cls._parse_control_time(value["updated_at"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("asset root receipt is invalid") from exc
        return AssetRootRecord(
            root_id=root_id,
            private_path=private_path,
            max_bytes=max_bytes,
            enabled=enabled,
            revision=revision,
            created_at=created_at,
            updated_at=updated_at,
        )

    @classmethod
    def _validated_connector(
        cls,
        *,
        connector_id: object,
        adapter_id: object,
        display_name: object,
        credential_alias: object,
        enabled: object,
    ) -> tuple[str, str, str, str | None, bool]:
        validated_id = cls._connector_machine_id(connector_id, "connector_id")
        validated_adapter = cls._connector_machine_id(adapter_id, "adapter_id")
        if type(display_name) is not str:
            raise TypeError("display_name is invalid")
        if any(
            unicodedata.category(character).startswith("C")
            for character in display_name
        ):
            raise ValueError("display_name is invalid")
        validated_name = display_name.strip()
        if not validated_name or len(validated_name) > 200:
            raise ValueError("display_name is invalid")
        if credential_alias is None:
            validated_alias = None
        elif (
            type(credential_alias) is not str
            or _CREDENTIAL_ALIAS_RE.fullmatch(credential_alias) is None
            or _CREDENTIAL_KEY_RE.search(credential_alias) is not None
        ):
            raise ValueError("credential_alias is invalid")
        else:
            validated_alias = credential_alias
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        return (
            validated_id,
            validated_adapter,
            validated_name,
            validated_alias,
            enabled,
        )

    @staticmethod
    def _connector_machine_id(value: object, name: str) -> str:
        if type(value) is not str or _CONNECTOR_ID_RE.fullmatch(value) is None:
            raise ValueError(f"{name} is invalid")
        return value

    def _connector(
        self, conn: sqlite3.Connection, connector_id: str
    ) -> ConnectorRecord:
        row = conn.execute(
            """SELECT id, kind, adapter_id, display_name, credential_alias,
                      enabled, revision, created_at, updated_at
               FROM connectors WHERE id = ?""",
            (connector_id,),
        ).fetchone()
        if row is None:
            raise NotFound("connector", connector_id)
        return ConnectorRecord(
            id=str(row["id"]),
            kind=str(row["kind"]),
            adapter_id=str(row["adapter_id"]),
            display_name=str(row["display_name"]),
            credential_alias=(
                str(row["credential_alias"])
                if row["credential_alias"] is not None
                else None
            ),
            enabled=bool(row["enabled"]),
            revision=int(row["revision"]),
            created_at=self._parse_control_time(str(row["created_at"])),
            updated_at=self._parse_control_time(str(row["updated_at"])),
        )

    @staticmethod
    def _connector_response(record: ConnectorRecord) -> JsonObject:
        return {
            "id": record.id,
            "kind": record.kind,
            "adapter_id": record.adapter_id,
            "display_name": record.display_name,
            "credential_alias": record.credential_alias,
            "enabled": record.enabled,
            "revision": record.revision,
            "created_at": ControlStore._format_time(record.created_at),
            "updated_at": ControlStore._format_time(record.updated_at),
        }

    @classmethod
    def _connector_receipt(cls, value: Mapping[str, Any]) -> ConnectorRecord:
        expected = {
            "id",
            "kind",
            "adapter_id",
            "display_name",
            "credential_alias",
            "enabled",
            "revision",
            "created_at",
            "updated_at",
        }
        if set(value) != expected:
            raise RuntimeError("connector receipt is invalid")
        connector_id, adapter_id, display_name, credential_alias, enabled = (
            cls._validated_connector(
                connector_id=value["id"],
                adapter_id=value["adapter_id"],
                display_name=value["display_name"],
                credential_alias=value["credential_alias"],
                enabled=value["enabled"],
            )
        )
        kind = cls._connector_machine_id(value["kind"], "kind")
        revision = value["revision"]
        if type(revision) is not int or revision < 0:
            raise RuntimeError("connector receipt is invalid")
        try:
            created_at = cls._parse_control_time(value["created_at"])
            updated_at = cls._parse_control_time(value["updated_at"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("connector receipt is invalid") from exc
        return ConnectorRecord(
            id=connector_id,
            kind=kind,
            adapter_id=adapter_id,
            display_name=display_name,
            credential_alias=credential_alias,
            enabled=enabled,
            revision=revision,
            created_at=created_at,
            updated_at=updated_at,
        )

    def _control_store_identity(
        self, conn: sqlite3.Connection
    ) -> ControlStoreIdentity:
        if self._binding_key is None:
            raise RuntimeError("control store is not initialized")
        version_row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        if version_row is None or version_row[0] is None:
            raise RuntimeError("control schema identity is unavailable")
        schema_rows = [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute(
                """SELECT type, name, COALESCE(sql, '') FROM sqlite_schema
                   ORDER BY type, name"""
            ).fetchall()
        ]
        schema_json = json.dumps(
            schema_rows, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return ControlStoreIdentity(
            schema_version=int(version_row[0]),
            schema_fingerprint_sha256=hashlib.sha256(schema_json).hexdigest(),
            identity_companion_sha256=hashlib.sha256(self._binding_key).hexdigest(),
            identity_companion_byte_length=len(self._binding_key),
        )

    def _validated_paired_backup_proof(
        self, proof: object
    ) -> _ValidatedPairedProof:
        if type(proof) is not PairedBackupProofInput:
            raise TypeError("proof must be a PairedBackupProofInput")
        proof_id = self._registry_resource_id(proof.proof_id, "proof_id")
        manifest_bytes = canonical_protected_set_manifest(
            proof.protected_set_manifest
        )
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        now = self._utc_now()
        primary_time, primary_count = self._validated_backup_copy(
            proof.primary,
            name="primary",
            expected_digest=manifest_digest,
            now=now,
        )
        independent_time, independent_count = self._validated_backup_copy(
            proof.independent,
            name="independent",
            expected_digest=manifest_digest,
            now=now,
        )
        restore_time, database_count, sample_count = self._validated_restore(
            proof.restore,
            expected_digest=manifest_digest,
            now=now,
        )
        if restore_time < primary_time or restore_time < independent_time:
            raise ValueError("restore evidence predates a backup copy")
        return _ValidatedPairedProof(
            id=proof_id,
            protected_set_manifest=proof.protected_set_manifest,
            manifest_json=manifest_bytes.decode("utf-8"),
            backup_set_digest=manifest_digest,
            primary_completed_at=primary_time,
            primary_snapshot_count=primary_count,
            independent_completed_at=independent_time,
            independent_snapshot_count=independent_count,
            restore_completed_at=restore_time,
            restored_database_count=database_count,
            verified_sample_count=sample_count,
        )

    def _validated_backup_copy(
        self,
        value: object,
        *,
        name: str,
        expected_digest: str,
        now: datetime,
    ) -> tuple[datetime, int]:
        if type(value) is not BackupCopyProof:
            raise TypeError(f"{name} must be a BackupCopyProof")
        if value.backup_set_digest != expected_digest:
            raise ValueError(f"{name} evidence digest does not match the manifest")
        if value.verified is not True:
            raise ValueError(f"{name} evidence must be verified")
        completed_at = self._validated_evidence_time(
            value.completed_at, name=f"{name}.completed_at", now=now
        )
        snapshot_count = self._registry_count(
            value.snapshot_count, name=f"{name}.snapshot_count", minimum=1
        )
        return completed_at, snapshot_count

    def _validated_restore(
        self,
        value: object,
        *,
        expected_digest: str,
        now: datetime,
    ) -> tuple[datetime, int, int]:
        if type(value) is not RestoreVerification:
            raise TypeError("restore must be a RestoreVerification")
        if value.backup_set_digest != expected_digest:
            raise ValueError("restore evidence digest does not match the manifest")
        if value.verified is not True:
            raise ValueError("restore evidence must be verified")
        completed_at = self._validated_evidence_time(
            value.completed_at, name="restore.completed_at", now=now
        )
        database_count = self._registry_count(
            value.database_count, name="restore.database_count", minimum=1
        )
        sample_count = self._registry_count(
            value.verified_sample_count,
            name="restore.verified_sample_count",
            minimum=0,
        )
        return completed_at, database_count, sample_count

    @staticmethod
    def _validated_evidence_time(
        value: object, *, name: str, now: datetime
    ) -> datetime:
        if type(value) is not datetime or value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")
        normalized = value.astimezone(UTC)
        if normalized > now:
            raise ValueError(f"{name} must not be in the future")
        return normalized

    @staticmethod
    def _registry_count(value: object, *, name: str, minimum: int) -> int:
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} is invalid")
        return value

    @staticmethod
    def _registry_resource_id(value: object, name: str) -> str:
        if type(value) is not str or _REGISTRY_RESOURCE_ID_RE.fullmatch(value) is None:
            raise ValueError(f"{name} is invalid")
        return value

    def _validate_protected_set_coverage(
        self, conn: sqlite3.Connection, manifest: ProtectedSetManifest
    ) -> None:
        identity = self._control_store_identity(conn)
        snapshot = manifest.control_store
        expected_identity = (
            identity.schema_version,
            identity.schema_fingerprint_sha256,
            identity.identity_companion_sha256,
            identity.identity_companion_byte_length,
        )
        supplied_identity = (
            snapshot.schema_version,
            snapshot.schema_fingerprint_sha256,
            snapshot.identity_companion_sha256,
            snapshot.identity_companion_byte_length,
        )
        enabled_roots = tuple(
            (str(row["root_id"]), int(row["revision"]))
            for row in conn.execute(
                """SELECT root_id, revision FROM asset_roots
                   WHERE enabled = 1 ORDER BY root_id"""
            ).fetchall()
        )
        supplied_roots = tuple(
            (root.root_id, root.revision) for root in manifest.asset_roots
        )
        if supplied_identity != expected_identity or supplied_roots != enabled_roots:
            raise ValueError("protected set coverage does not match current Control state")

    @staticmethod
    def _paired_backup_proof_request(value: _ValidatedPairedProof) -> JsonObject:
        return {
            "proof_id": value.id,
            "protected_set_manifest": json.loads(value.manifest_json),
            "primary": {
                "backup_set_digest": value.backup_set_digest,
                "completed_at": ControlStore._format_registry_time(
                    value.primary_completed_at
                ),
                "snapshot_count": value.primary_snapshot_count,
                "verified": True,
            },
            "independent": {
                "backup_set_digest": value.backup_set_digest,
                "completed_at": ControlStore._format_registry_time(
                    value.independent_completed_at
                ),
                "snapshot_count": value.independent_snapshot_count,
                "verified": True,
            },
            "restore": {
                "backup_set_digest": value.backup_set_digest,
                "completed_at": ControlStore._format_registry_time(
                    value.restore_completed_at
                ),
                "database_count": value.restored_database_count,
                "verified_sample_count": value.verified_sample_count,
                "verified": True,
            },
        }

    def _paired_backup_proof(
        self, conn: sqlite3.Connection, proof_id: str
    ) -> PairedBackupProofRecord:
        row = conn.execute(
            """SELECT id, backup_set_digest, protected_set_manifest_json,
                      primary_completed_at, primary_snapshot_count,
                      independent_completed_at, independent_snapshot_count,
                      restore_completed_at, restored_database_count,
                      verified_sample_count, created_at
               FROM paired_backup_proofs WHERE id = ?""",
            (proof_id,),
        ).fetchone()
        if row is None:
            raise NotFound("paired backup proof", proof_id)
        return self._paired_backup_proof_record(
            {
                "id": row["id"],
                "backup_set_digest": row["backup_set_digest"],
                "protected_set_manifest": json.loads(
                    str(row["protected_set_manifest_json"])
                ),
                "primary_completed_at": row["primary_completed_at"],
                "primary_snapshot_count": row["primary_snapshot_count"],
                "independent_completed_at": row["independent_completed_at"],
                "independent_snapshot_count": row["independent_snapshot_count"],
                "restore_completed_at": row["restore_completed_at"],
                "restored_database_count": row["restored_database_count"],
                "verified_sample_count": row["verified_sample_count"],
                "created_at": row["created_at"],
            }
        )

    def _paired_backup_proof_record(
        self, value: Mapping[str, Any]
    ) -> PairedBackupProofRecord:
        expected = {
            "id",
            "backup_set_digest",
            "protected_set_manifest",
            "primary_completed_at",
            "primary_snapshot_count",
            "independent_completed_at",
            "independent_snapshot_count",
            "restore_completed_at",
            "restored_database_count",
            "verified_sample_count",
            "created_at",
        }
        if set(value) != expected:
            raise RuntimeError("paired backup proof record is invalid")
        proof_id = self._registry_resource_id(value["id"], "proof_id")
        manifest = self._protected_set_manifest(value["protected_set_manifest"])
        digest = value["backup_set_digest"]
        if type(digest) is not str or digest != protected_set_digest(manifest):
            raise RuntimeError("paired backup proof digest is invalid")
        primary_time = self._registry_stored_time(value["primary_completed_at"])
        independent_time = self._registry_stored_time(
            value["independent_completed_at"]
        )
        restore_time = self._registry_stored_time(value["restore_completed_at"])
        if restore_time < primary_time or restore_time < independent_time:
            raise RuntimeError("paired backup proof chronology is invalid")
        return PairedBackupProofRecord(
            id=proof_id,
            backup_set_digest=digest,
            protected_set_manifest=manifest,
            primary_completed_at=primary_time,
            primary_snapshot_count=self._registry_count(
                value["primary_snapshot_count"],
                name="primary_snapshot_count",
                minimum=1,
            ),
            independent_completed_at=independent_time,
            independent_snapshot_count=self._registry_count(
                value["independent_snapshot_count"],
                name="independent_snapshot_count",
                minimum=1,
            ),
            restore_completed_at=restore_time,
            restored_database_count=self._registry_count(
                value["restored_database_count"],
                name="restored_database_count",
                minimum=1,
            ),
            verified_sample_count=self._registry_count(
                value["verified_sample_count"],
                name="verified_sample_count",
                minimum=0,
            ),
            created_at=self._registry_stored_time(value["created_at"]),
        )

    @staticmethod
    def _paired_backup_proof_response(
        record: PairedBackupProofRecord,
    ) -> JsonObject:
        return {
            "id": record.id,
            "backup_set_digest": record.backup_set_digest,
            "protected_set_manifest": json.loads(
                canonical_protected_set_manifest(
                    record.protected_set_manifest
                ).decode("utf-8")
            ),
            "primary_completed_at": ControlStore._format_registry_time(
                record.primary_completed_at
            ),
            "primary_snapshot_count": record.primary_snapshot_count,
            "independent_completed_at": ControlStore._format_registry_time(
                record.independent_completed_at
            ),
            "independent_snapshot_count": record.independent_snapshot_count,
            "restore_completed_at": ControlStore._format_registry_time(
                record.restore_completed_at
            ),
            "restored_database_count": record.restored_database_count,
            "verified_sample_count": record.verified_sample_count,
            "created_at": ControlStore._format_time(record.created_at),
        }

    def _paired_backup_proof_receipt(
        self, value: Mapping[str, Any]
    ) -> PairedBackupProofRecord:
        try:
            return self._paired_backup_proof_record(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("paired backup proof receipt is invalid") from exc

    @staticmethod
    def _paired_backup_proof_record_request(
        record: PairedBackupProofRecord,
    ) -> JsonObject:
        return ControlStore._paired_backup_proof_request(
            _ValidatedPairedProof(
                id=record.id,
                protected_set_manifest=record.protected_set_manifest,
                manifest_json=canonical_protected_set_manifest(
                    record.protected_set_manifest
                ).decode("utf-8"),
                backup_set_digest=record.backup_set_digest,
                primary_completed_at=record.primary_completed_at,
                primary_snapshot_count=record.primary_snapshot_count,
                independent_completed_at=record.independent_completed_at,
                independent_snapshot_count=record.independent_snapshot_count,
                restore_completed_at=record.restore_completed_at,
                restored_database_count=record.restored_database_count,
                verified_sample_count=record.verified_sample_count,
            )
        )

    @classmethod
    def _protected_set_manifest(cls, value: object) -> ProtectedSetManifest:
        manifest = cls._registry_mapping(
            value,
            name="protected_set_manifest",
            expected={
                "schema_version",
                "control_store",
                "logical_snapshots",
                "asset_roots",
            },
        )
        control = cls._registry_mapping(
            manifest["control_store"],
            name="control_store",
            expected={
                "schema_version",
                "schema_fingerprint_sha256",
                "database_sha256",
                "database_byte_length",
                "identity_companion_sha256",
                "identity_companion_byte_length",
            },
        )
        logical_values = manifest["logical_snapshots"]
        root_values = manifest["asset_roots"]
        if type(logical_values) is not list or type(root_values) is not list:
            raise ValueError("protected set members are invalid")
        logical_snapshots = tuple(
            cls._protected_logical_snapshot(item) for item in logical_values
        )
        asset_roots = tuple(cls._protected_asset_root(item) for item in root_values)
        result = ProtectedSetManifest(
            schema_version=manifest["schema_version"],
            control_store=ControlStoreSnapshot(
                schema_version=control["schema_version"],
                schema_fingerprint_sha256=control["schema_fingerprint_sha256"],
                database_sha256=control["database_sha256"],
                database_byte_length=control["database_byte_length"],
                identity_companion_sha256=control["identity_companion_sha256"],
                identity_companion_byte_length=control[
                    "identity_companion_byte_length"
                ],
            ),
            logical_snapshots=logical_snapshots,
            asset_roots=asset_roots,
        )
        canonical_protected_set_manifest(result)
        return result

    @classmethod
    def _protected_logical_snapshot(
        cls, value: object
    ) -> ProtectedLogicalSnapshot:
        item = cls._registry_mapping(
            value,
            name="logical_snapshot",
            expected={"logical_id", "content_sha256", "byte_length"},
        )
        return ProtectedLogicalSnapshot(
            logical_id=item["logical_id"],
            content_sha256=item["content_sha256"],
            byte_length=item["byte_length"],
        )

    @classmethod
    def _protected_asset_root(cls, value: object) -> ProtectedAssetRootSnapshot:
        item = cls._registry_mapping(
            value,
            name="asset_root",
            expected={
                "root_id",
                "revision",
                "content_manifest_sha256",
                "file_count",
                "byte_length",
            },
        )
        return ProtectedAssetRootSnapshot(
            root_id=item["root_id"],
            revision=item["revision"],
            content_manifest_sha256=item["content_manifest_sha256"],
            file_count=item["file_count"],
            byte_length=item["byte_length"],
        )

    @staticmethod
    def _registry_mapping(
        value: object, *, name: str, expected: set[str]
    ) -> Mapping[str, Any]:
        if type(value) is not dict or set(value) != expected:
            raise ValueError(f"{name} is invalid")
        return value

    @staticmethod
    def _registry_stored_time(value: object) -> datetime:
        if type(value) is not str:
            raise ValueError("stored registry time is invalid")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("stored registry time is invalid")
        return parsed.astimezone(UTC)

    @staticmethod
    def _format_registry_time(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )

    def _validated_system_health_observation(
        self, observation: object
    ) -> tuple[
        str,
        HealthSubject,
        str,
        str,
        datetime,
        tuple[tuple[str, bool | int], ...],
    ]:
        if type(observation) is not SystemHealthObservationInput:
            raise TypeError("observation must be a SystemHealthObservationInput")
        observation_id = self._registry_resource_id(
            observation.observation_id, "observation_id"
        )
        subject = self._validated_health_subject(observation.subject)
        status = observation.status
        if type(status) is not str or status not in _HEALTH_STATUSES:
            raise ValueError("status is invalid")
        category = observation.category
        if type(category) is not str or _HEALTH_CATEGORY_RE.fullmatch(category) is None:
            raise ValueError("category is invalid")
        observed_at = self._validated_evidence_time(
            observation.observed_at,
            name="observed_at",
            now=self._utc_now(),
        )
        metrics = self._validated_health_metrics(observation.metrics)
        return observation_id, subject, status, category, observed_at, metrics

    @classmethod
    def _validated_health_subject(cls, subject: object) -> HealthSubject:
        if type(subject) is not HealthSubject:
            raise TypeError("subject must be a HealthSubject")
        if subject.kind == "asset_root":
            subject_id = cls._asset_root_id(subject.id)
        elif subject.kind == "connector":
            subject_id = cls._connector_machine_id(subject.id, "subject.id")
        elif subject.kind == "backup_proof":
            subject_id = cls._registry_resource_id(subject.id, "subject.id")
        else:
            raise ValueError("subject kind is invalid")
        return HealthSubject(subject.kind, subject_id)

    @staticmethod
    def _validated_health_metrics(
        metrics: object,
    ) -> tuple[tuple[str, bool | int], ...]:
        if type(metrics) is not tuple or len(metrics) > 32:
            raise ValueError("metrics must be a tuple of at most 32 pairs")
        validated: list[tuple[str, bool | int]] = []
        keys: list[str] = []
        for pair in metrics:
            if type(pair) is not tuple or len(pair) != 2:
                raise TypeError("metric entries must be pairs")
            key, value = pair
            if (
                type(key) is not str
                or _HEALTH_METRIC_KEY_RE.fullmatch(key) is None
                or any(term in key for term in _HEALTH_SENSITIVE_TERMS)
            ):
                raise ValueError("metric key is invalid")
            if type(value) is bool:
                validated_value: bool | int = value
            elif type(value) is int and value >= 0:
                validated_value = value
            else:
                raise TypeError("metric value must be a boolean or non-negative integer")
            keys.append(key)
            validated.append((key, validated_value))
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("metric keys must be unique and sorted")
        return tuple(validated)

    @staticmethod
    def _health_subject_storage(
        subject: HealthSubject,
    ) -> tuple[str, str, str]:
        if subject.kind == "asset_root":
            return "asset_root_id", "asset_roots", "root_id"
        if subject.kind == "connector":
            return "connector_id", "connectors", "id"
        return "backup_proof_id", "paired_backup_proofs", "id"

    def _validate_health_subject_exists(
        self, conn: sqlite3.Connection, subject: HealthSubject
    ) -> str:
        column, table, identity_column = self._health_subject_storage(subject)
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE {identity_column} = ?", (subject.id,)
        ).fetchone()
        if row is None:
            raise NotFound(subject.kind.replace("_", " "), subject.id)
        return column

    def _system_health_observation(
        self, conn: sqlite3.Connection, observation_id: str
    ) -> SystemHealthObservationRecord:
        row = conn.execute(
            """SELECT id, asset_root_id, connector_id, backup_proof_id, status,
                      category, observed_at, metrics_json, created_at
               FROM system_health_observations WHERE id = ?""",
            (observation_id,),
        ).fetchone()
        if row is None:
            raise NotFound("system health observation", observation_id)
        subjects = tuple(
            (kind, str(row[column]))
            for kind, column in (
                ("asset_root", "asset_root_id"),
                ("connector", "connector_id"),
                ("backup_proof", "backup_proof_id"),
            )
            if row[column] is not None
        )
        if len(subjects) != 1:
            raise RuntimeError("system health subject is invalid")
        metrics_value = json.loads(str(row["metrics_json"]))
        if type(metrics_value) is not dict:
            raise RuntimeError("system health metrics are invalid")
        metrics = self._validated_health_metrics(tuple(sorted(metrics_value.items())))
        return SystemHealthObservationRecord(
            id=self._registry_resource_id(row["id"], "observation_id"),
            subject=HealthSubject(subjects[0][0], subjects[0][1]),
            status=str(row["status"]),
            category=str(row["category"]),
            observed_at=self._registry_stored_time(row["observed_at"]),
            metrics=metrics,
            created_at=self._registry_stored_time(row["created_at"]),
        )

    @staticmethod
    def _system_health_request(record: SystemHealthObservationRecord) -> JsonObject:
        return {
            "observation_id": record.id,
            "subject": {"kind": record.subject.kind, "id": record.subject.id},
            "status": record.status,
            "category": record.category,
            "observed_at": ControlStore._format_registry_time(record.observed_at),
            "metrics": [[key, value] for key, value in record.metrics],
        }

    @staticmethod
    def _system_health_response(record: SystemHealthObservationRecord) -> JsonObject:
        return {
            **ControlStore._system_health_request(record),
            "created_at": ControlStore._format_time(record.created_at),
        }

    def _system_health_receipt(
        self, value: Mapping[str, Any]
    ) -> SystemHealthObservationRecord:
        expected = {
            "observation_id",
            "subject",
            "status",
            "category",
            "observed_at",
            "metrics",
            "created_at",
        }
        if set(value) != expected:
            raise RuntimeError("system health receipt is invalid")
        subject_value = self._registry_mapping(
            value["subject"],
            name="subject",
            expected={"kind", "id"},
        )
        metrics_value = value["metrics"]
        if type(metrics_value) is not list or any(
            type(pair) is not list or len(pair) != 2 for pair in metrics_value
        ):
            raise RuntimeError("system health receipt is invalid")
        observation = SystemHealthObservationInput(
            observation_id=value["observation_id"],
            subject=HealthSubject(subject_value["kind"], subject_value["id"]),
            status=value["status"],
            category=value["category"],
            observed_at=self._registry_stored_time(value["observed_at"]),
            metrics=tuple((pair[0], pair[1]) for pair in metrics_value),
        )
        (
            observation_id,
            subject,
            status,
            category,
            observed_at,
            metrics,
        ) = self._validated_system_health_observation(observation)
        return SystemHealthObservationRecord(
            id=observation_id,
            subject=subject,
            status=status,
            category=category,
            observed_at=observed_at,
            metrics=metrics,
            created_at=self._registry_stored_time(value["created_at"]),
        )

    def replay_command(
        self,
        *,
        actor_id: str,
        operation: str,
        idempotency_key: str,
        request: Mapping[str, Any],
    ) -> CommandResult | None:
        """The receipt a command already saved under this key, or None.

        ⟦P8⟧ The control API decides some refusals BEFORE it runs a command --
        a run it could not drive is refused rather than written -- and a
        refusal must not be given about a command that committed and whose
        response was lost. The same lookup every command performs first, on
        its own, so the API can replay before it refuses. Raises
        `IdempotencyConflict` exactly as the command would.
        """

        with self._connect() as conn:
            return self._receipt(conn, actor_id, operation, idempotency_key, request)

    def _receipt(
        self,
        conn: sqlite3.Connection,
        actor_id: str,
        operation: str,
        key: str,
        request: Mapping[str, Any],
    ) -> CommandResult | None:
        self._validate_key(key)
        request_hash = self._request_hash(request)
        row = conn.execute(
            """SELECT request_hash, status_code, response_json
               FROM idempotency_receipts
               WHERE actor_id = ? AND operation = ? AND idempotency_key = ?""",
            (actor_id, operation, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict()
        return CommandResult(
            value=json.loads(row["response_json"]),
            status_code=int(row["status_code"]),
            replayed=True,
        )

    def _save_receipt(
        self,
        conn: sqlite3.Connection,
        actor_id: str,
        operation: str,
        key: str,
        request: Mapping[str, Any],
        response: JsonObject,
        status_code: int,
    ) -> CommandResult:
        conn.execute(
            """INSERT INTO idempotency_receipts
               (actor_id, operation, idempotency_key, request_hash,
                status_code, response_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                actor_id,
                operation,
                key,
                self._request_hash(request),
                status_code,
                self._json(response),
                self._now(),
            ),
        )
        return CommandResult(value=response, status_code=status_code)

    def _audit(
        self,
        conn: sqlite3.Connection,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """INSERT INTO control_audit
               (id, aggregate_type, aggregate_id, type, occurred_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                self._id_factory("audit"),
                aggregate_type,
                aggregate_id,
                event_type,
                self._now(),
                self._json(dict(payload)),
            ),
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> JsonObject:
        return {key: row[key] for key in row.keys()}

    def _workflow_row(
        self, conn: sqlite3.Connection, workflow_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise NotFound("workflow", workflow_id)
        return self._row(row)

    def _workflow_stage_row(
        self, conn: sqlite3.Connection, workflow_id: str, stage_key: str
    ) -> JsonObject:
        row = conn.execute(
            """SELECT * FROM workflow_stage_instances
               WHERE workflow_id = ? AND stage_key = ?""",
            (workflow_id, stage_key),
        ).fetchone()
        if row is None:
            raise NotFound("workflow stage", stage_key)
        return self._row(row)

    def _workflow_stage(
        self, conn: sqlite3.Connection, stage_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM workflow_stage_instances WHERE id = ?", (stage_id,)
        ).fetchone()
        if row is None:
            raise NotFound("workflow stage", stage_id)
        value = self._row(row)
        value["checkpoint_enabled"] = bool(value["checkpoint_enabled"])
        dependencies = conn.execute(
            """SELECT dependency.stage_key
               FROM workflow_stage_dependencies AS edge
               JOIN workflow_stage_instances AS dependency
                 ON dependency.id = edge.dependency_stage_id
               WHERE edge.stage_id = ? ORDER BY edge.position""",
            (stage_id,),
        ).fetchall()
        receipts = conn.execute(
            """SELECT effect_kind FROM workflow_stage_receipt_requirements
               WHERE stage_id = ? ORDER BY position""",
            (stage_id,),
        ).fetchall()
        results = conn.execute(
            """SELECT result_kind FROM workflow_stage_result_requirements
               WHERE stage_id = ? ORDER BY position""",
            (stage_id,),
        ).fetchall()
        value["dependencies"] = [str(item[0]) for item in dependencies]
        value["required_receipts"] = [str(item[0]) for item in receipts]
        value["required_results"] = [str(item[0]) for item in results]
        return value

    def _workflow(self, conn: sqlite3.Connection, workflow_id: str) -> JsonObject:
        value = self._workflow_row(conn, workflow_id)
        value["definition_sealed"] = bool(value["definition_sealed"])
        rows = conn.execute(
            """SELECT id FROM workflow_stage_instances
               WHERE workflow_id = ? ORDER BY position""",
            (workflow_id,),
        ).fetchall()
        value["stages"] = [
            self._workflow_stage(conn, str(row["id"])) for row in rows
        ]
        return value

    def _workflow_decision_ref(
        self, conn: sqlite3.Connection, decision_ref_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM workflow_decision_refs WHERE id = ?", (decision_ref_id,)
        ).fetchone()
        if row is None:
            raise NotFound("workflow decision reference", decision_ref_id)
        return self._row(row)

    def _workflow_effect(
        self, conn: sqlite3.Connection, effect_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM workflow_effect_commands WHERE id = ?", (effect_id,)
        ).fetchone()
        if row is None:
            raise NotFound("workflow effect", effect_id)
        value = self._row(row)
        raw_request = json.loads(value.pop("request_json"))
        request_domain = raw_request.get("domain")
        parser = {
            "artifact_workflow": ArtifactWorkflowRequest.from_dict,
            "source_binding": SourceBindingRequest.from_dict,
            "source_import": SourceImportRequest.from_dict,
            "lineage_query": LineageQueryRequest.from_dict,
            "successor_creation": SuccessorCreationRequest.from_dict,
            "runtime_stage": RuntimeStageRequest.from_dict,
        }.get(request_domain)
        if parser is None:
            raise InvalidTransition("unsupported_effect_kind", "loaded")
        request = parser(raw_request)
        expected_effect_kind = (
            request.effect_kind
            if isinstance(request, (RuntimeStageRequest, ArtifactWorkflowRequest))
            else request.DOMAIN
        )
        if value["effect_kind"] != expected_effect_kind:
            raise InvalidTransition("effect_kind_mismatch", "loaded")
        value["request"] = request
        receipt_json = value.pop("receipt_json")
        value["receipt"] = json.loads(receipt_json) if receipt_json else None
        value["dispatch_attempt_count"] = int(
            conn.execute(
                """SELECT COUNT(*) FROM control_audit
                   WHERE aggregate_type = 'workflow_effect'
                     AND aggregate_id = ?
                     AND type = 'workflow.effect.dispatched'""",
                (effect_id,),
            ).fetchone()[0]
        )
        return value

    def _workflow_reference(
        self, conn: sqlite3.Connection, reference_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM workflow_stage_references WHERE id = ?",
            (reference_id,),
        ).fetchone()
        if row is None:
            raise NotFound("workflow reference", reference_id)
        value = self._row(row)
        value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    def _workflow_checkpoint(
        self, conn: sqlite3.Connection, checkpoint_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM workflow_checkpoints WHERE id = ?", (checkpoint_id,)
        ).fetchone()
        if row is None:
            raise NotFound("workflow checkpoint", checkpoint_id)
        value = self._row(row)
        value["state"] = json.loads(value.pop("state_json"))
        return value

    def _expect_workflow_effect_claim(
        self,
        effect: Mapping[str, Any],
        *,
        worker_id: str,
        claim_epoch: int,
        delivery_epoch: int,
        state: str,
        require_unexpired: bool = True,
    ) -> None:
        if (
            type(claim_epoch) is not int
            or type(delivery_epoch) is not int
            or effect.get("state") != state
            or effect.get("claim_owner") != worker_id
            or effect.get("claim_epoch") != claim_epoch
            or effect.get("delivery_epoch") != delivery_epoch
            or (
                require_unexpired
                and str(effect.get("claim_expires_at") or "") <= self._now()
            )
        ):
            raise InvalidTransition("stale_claim", "completed")

    def _expect_workflow_effect_result(
        self,
        conn: sqlite3.Connection,
        *,
        effect: Mapping[str, Any],
        result: Any,
        target: str,
    ) -> None:
        if isinstance(result, ArtifactWorkflowResult):
            self._expect_workflow_artifact_result(
                conn, effect=effect, result=result, target=target
            )
            return
        if not isinstance(result, RuntimeStageResult):
            return
        request = effect["request"]
        if not isinstance(request, RuntimeStageRequest):
            raise InvalidTransition("runtime_request_mismatch", target)
        workflow = self._workflow_row(conn, str(effect["workflow_id"]))
        run = self._run(conn, str(workflow["run_id"]))
        if result.attempt_id != request.attempt_id or (
            target == "stage_completed"
            and run["active_attempt_id"] != result.attempt_id
        ):
            category = (
                "runtime_event_attempt_mismatch"
                if target == "stage_completed"
                else "runtime_attempt_mismatch"
            )
            raise InvalidTransition(category, target)
        event = conn.execute(
            """SELECT run_id, attempt_id, type, causation_id FROM run_events
               WHERE id = ?""",
            (result.runtime_event_id,),
        ).fetchone()
        if event is None:
            raise InvalidTransition("runtime_event_missing", target)
        if (
            event["run_id"] != run["id"]
            or event["attempt_id"] != result.attempt_id
        ):
            raise InvalidTransition("runtime_event_identity_mismatch", target)
        if (
            event["causation_id"] != effect["operation_id"]
            or result.operation_id != effect["operation_id"]
        ):
            raise InvalidTransition("runtime_event_causation_mismatch", target)
        inbox = conn.execute(
            """SELECT 1 FROM runtime_event_inbox
               WHERE attempt_id = ? AND adapter_event_id = ?""",
            (result.attempt_id, effect["operation_id"]),
        ).fetchone()
        if inbox is None:
            raise InvalidTransition("runtime_event_inbox_missing", target)
        if not str(event["type"]).startswith("runtime."):
            raise InvalidTransition("runtime_event_type_mismatch", target)

    def _expect_workflow_artifact_result(
        self,
        conn: sqlite3.Connection,
        *,
        effect: Mapping[str, Any],
        result: ArtifactWorkflowResult,
        target: str,
    ) -> None:
        request = effect["request"]
        if not isinstance(request, ArtifactWorkflowRequest):
            raise InvalidTransition("artifact_request_mismatch", target)
        workflow = self._workflow_row(conn, str(effect["workflow_id"]))
        run = self._run(conn, str(workflow["run_id"]))
        if result.attempt_id != request.attempt_id or (
            target == "stage_completed"
            and run["active_attempt_id"] != result.attempt_id
        ):
            raise InvalidTransition("artifact_attempt_mismatch", target)
        if (
            request.effect_kind == "artifact_snapshot"
            and result.artifact_version_ids
            != request.snapshot_member_version_ids
        ):
            raise InvalidTransition("artifact_snapshot_members_mismatch", target)
        for version_id in result.artifact_version_ids:
            version = conn.execute(
                """SELECT run_id, attempt_id, state FROM artifact_versions
                   WHERE id = ?""",
                (version_id,),
            ).fetchone()
            if version is None:
                raise InvalidTransition("artifact_version_missing", target)
            if (
                version["run_id"] != run["id"]
                or version["attempt_id"] != result.attempt_id
                or version["state"] != "committed"
            ):
                raise InvalidTransition("artifact_version_identity_mismatch", target)
        if result.snapshot_id is None:
            return
        snapshot = conn.execute(
            "SELECT workspace_id, state FROM artifact_snapshots WHERE id = ?",
            (result.snapshot_id,),
        ).fetchone()
        if snapshot is None:
            raise InvalidTransition("artifact_snapshot_missing", target)
        thread = self._thread(conn, str(run["thread_id"]))
        if snapshot["workspace_id"] != thread["workspace_id"] or snapshot[
            "state"
        ] != "committed":
            raise InvalidTransition("artifact_snapshot_identity_mismatch", target)
        owner_event = conn.execute(
            """SELECT 1 FROM run_events
               WHERE run_id = ? AND attempt_id = ?
                 AND type = 'artifact.snapshot_created'
                 AND json_extract(payload_json, '$.snapshot_id') = ?""",
            (run["id"], result.attempt_id, result.snapshot_id),
        ).fetchone()
        if owner_event is None:
            raise InvalidTransition("artifact_snapshot_identity_mismatch", target)
        members = conn.execute(
            """SELECT artifact_version_id FROM artifact_snapshot_members
               WHERE snapshot_id = ? ORDER BY artifact_version_id""",
            (result.snapshot_id,),
        ).fetchall()
        member_ids = tuple(str(member["artifact_version_id"]) for member in members)
        if member_ids != result.artifact_version_ids:
            raise InvalidTransition("artifact_snapshot_members_mismatch", target)

    @staticmethod
    def _expect_workflow_run_open(run: Mapping[str, Any]) -> None:
        state = str(run["state"])
        if state in _WORKFLOW_FENCED_RUN_STATES:
            raise InvalidTransition(state, "workflow_active")

    @staticmethod
    def _definition_signature(definition: WorkflowDefinition) -> tuple[Any, ...]:
        return (
            definition.definition_id,
            definition.version,
            tuple(
                (
                    stage.key,
                    stage.effect,
                    tuple(stage.dependencies),
                    tuple(stage.required_receipts),
                    tuple(stage.required_results),
                    stage.checkpoint,
                )
                for stage in definition.stages
            ),
        )

    @staticmethod
    def _workflow_definition_signature(workflow: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            workflow["definition_id"],
            workflow["definition_version"],
            tuple(
                (
                    stage["stage_key"],
                    stage["effect"],
                    tuple(stage["dependencies"]),
                    tuple(stage["required_receipts"]),
                    tuple(stage["required_results"]),
                    stage["checkpoint_enabled"],
                )
                for stage in workflow["stages"]
            ),
        )

    def _workspace(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (resource_id,)).fetchone()
        if row is None:
            raise NotFound("workspace", resource_id)
        return self._row(row)

    def _thread(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute("SELECT * FROM threads WHERE id = ?", (resource_id,)).fetchone()
        if row is None:
            raise NotFound("thread", resource_id)
        return self._row(row)

    def _message(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (resource_id,)).fetchone()
        if row is None:
            raise NotFound("message", resource_id)
        return self._row(row)

    def _run(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (resource_id,)).fetchone()
        if row is None:
            raise NotFound("run", resource_id)
        return self._row(row)

    def _attempt(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute("SELECT * FROM attempts WHERE id = ?", (resource_id,)).fetchone()
        if row is None:
            raise NotFound("attempt", resource_id)
        return self._row(row)

    def _runtime_binding(
        self, conn: sqlite3.Connection, resource_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM runtime_bindings WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("runtime binding", resource_id)
        return self._row(row)

    def _runtime_action(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM runtime_actions WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("runtime action", resource_id)
        value = self._row(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    def _paused_source_attempt(
        self,
        conn: sqlite3.Connection,
        attempt: Mapping[str, Any],
    ) -> JsonObject | None:
        source_attempt_id = attempt.get("source_attempt_id")
        visited: set[str] = set()
        while source_attempt_id is not None:
            source_attempt_id = str(source_attempt_id)
            if source_attempt_id in visited:
                raise InvalidTransition("attempt_lineage_cycle", "source_attempt")
            visited.add(source_attempt_id)
            source_attempt = self._attempt(conn, source_attempt_id)
            if (
                source_attempt["state"] == "paused"
                and source_attempt.get("checkpoint_uri")
            ):
                return source_attempt
            source_attempt_id = source_attempt.get("source_attempt_id")
        return None

    def _insert_paused_source_releases(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        attempt: Mapping[str, Any],
        now: str,
    ) -> list[str]:
        paused_source = self._paused_source_attempt(conn, attempt)
        if paused_source is None:
            return []
        return [
            self._insert_pin_release(
                conn,
                run_id=run_id,
                attempt=paused_source,
                now=now,
            )
        ]

    def _insert_pin_release(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        attempt: Mapping[str, Any],
        now: str,
    ) -> str:
        runtime_release_id = self._required_text(
            attempt.get("runtime_release_id"),
            "runtime_release_id",
            maximum=200,
        )
        state_generation_id = self._required_text(
            attempt.get("state_generation_id"),
            "state_generation_id",
            maximum=200,
        )
        if attempt.get("runtime_identity_version") != 1:
            raise InvalidTransition("legacy_runtime_identity", "pin_release")
        runtime_slot_id = self._required_text(
            attempt.get("runtime_slot_id"), "runtime_slot_id", maximum=200
        )
        runtime_artifact_digest = self._required_text(
            attempt.get("runtime_artifact_digest"),
            "runtime_artifact_digest",
            maximum=200,
        )
        runtime_worker_protocol = self._required_text(
            attempt.get("runtime_worker_protocol"),
            "runtime_worker_protocol",
            maximum=100,
        )
        existing = conn.execute(
            """SELECT id FROM runtime_pin_releases
               WHERE attempt_id = ? AND runtime_release_id = ?
                 AND state_generation_id = ?""",
            (attempt["id"], runtime_release_id, state_generation_id),
        ).fetchone()
        if existing is not None:
            release = self._pin_release(conn, str(existing["id"]))
            if (
                release.get("runtime_identity_version") != 1
                or release.get("runtime_slot_id") != runtime_slot_id
                or release.get("runtime_artifact_digest")
                != runtime_artifact_digest
                or release.get("runtime_worker_protocol")
                != runtime_worker_protocol
            ):
                raise InvalidTransition("runtime_identity_mismatch", "pin_release")
            return str(existing["id"])
        release_action_id = self._id_factory("pin_release")
        conn.execute(
            """INSERT INTO runtime_pin_releases
               (id, run_id, attempt_id, runtime_release_id,
                state_generation_id, state, created_at, runtime_slot_id,
                runtime_artifact_digest, runtime_worker_protocol,
                runtime_identity_version)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, 1)""",
            (
                release_action_id,
                run_id,
                attempt["id"],
                runtime_release_id,
                state_generation_id,
                now,
                runtime_slot_id,
                runtime_artifact_digest,
                runtime_worker_protocol,
            ),
        )
        return release_action_id

    def _pin_release(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM runtime_pin_releases WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("pin release", resource_id)
        return self._row(row)

    def _recovery_command(
        self, conn: sqlite3.Connection, resource_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM runtime_recovery_commands WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("runtime recovery command", resource_id)
        value = self._row(row)
        result_json = value.pop("result_json")
        value["result"] = json.loads(result_json) if result_json else None
        return value

    def _decision(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (resource_id,)).fetchone()
        if row is None:
            raise NotFound("decision", resource_id)
        value = self._row(row)
        value.pop("runtime_decision_ref", None)
        value.pop("runtime_decision_revision", None)
        value["options"] = json.loads(value.pop("options_json"))
        resolution_json = value.pop("resolution_json")
        value["resolution"] = json.loads(resolution_json) if resolution_json else None
        return value

    def _source_intent(
        self, conn: sqlite3.Connection, resource_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM source_intents WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("source intent", resource_id)
        value = self._row(row)
        candidates = conn.execute(
            """SELECT id FROM source_candidates WHERE intent_id = ?
               ORDER BY created_at, id""",
            (resource_id,),
        ).fetchall()
        value["title"] = value.pop("title_claim")
        value["locator"] = value.pop("locator_claim")
        locator_kind = value.pop("locator_claim_kind")
        locator_canonical_id = value.pop("locator_canonical_id")
        locator_version = value.pop("locator_version")
        value.pop("locator_sha256", None)
        value["locator_identity"] = (
            {
                "claim_kind": locator_kind,
                "canonical_id": locator_canonical_id,
                "version": locator_version,
            }
            if locator_kind is not None
            else None
        )
        value["candidates"] = [
            self._source_candidate(conn, str(candidate["id"]))
            for candidate in candidates
        ]
        decision_id = value.pop("decision_id")
        value["decision"] = (
            self._decision(conn, str(decision_id)) if decision_id else None
        )
        value["bindings"] = self._source_bindings_for_intent(conn, resource_id)
        return value

    def _source_candidate(
        self, conn: sqlite3.Connection, resource_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM source_candidates WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("source candidate", resource_id)
        value = self._row(row)
        value.pop("evidence_json", None)
        return value

    def _source(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("source", resource_id)
        value = self._row(row)
        aliases = conn.execute(
            """SELECT id, authority, display_value AS value, created_at
               FROM source_aliases WHERE source_id = ?
               ORDER BY authority, normalized_value, id""",
            (resource_id,),
        ).fetchall()
        value["aliases"] = [self._row(alias) for alias in aliases]
        return value

    def _source_import_action(
        self, conn: sqlite3.Connection, resource_id: str
    ) -> JsonObject:
        row = conn.execute(
            """SELECT source_import_actions.*, sources.canonical_id
               FROM source_import_actions
               JOIN sources ON sources.id = source_import_actions.source_id
               WHERE source_import_actions.id = ?""",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise NotFound("source import action", resource_id)
        value = self._row(row)
        manifest = value.pop("result_manifest_json")
        value["result_manifest"] = json.loads(manifest) if manifest else None
        return value

    def _run_source_binding(
        self, conn: sqlite3.Connection, resource_id: str
    ) -> JsonObject:
        row = conn.execute(
            "SELECT * FROM run_source_bindings WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise NotFound("run source binding", resource_id)
        value = self._row(row)
        value["source"] = self._source(conn, str(value["source_id"]))
        return value

    def _source_bindings_for_intent(
        self, conn: sqlite3.Connection, intent_id: str
    ) -> list[JsonObject]:
        rows = conn.execute(
            """SELECT run_source_bindings.id
               FROM run_source_bindings
               JOIN source_resolutions
                 ON source_resolutions.id = run_source_bindings.resolution_id
               WHERE source_resolutions.intent_id = ?
               ORDER BY run_source_bindings.created_at, run_source_bindings.id""",
            (intent_id,),
        ).fetchall()
        return [
            self._run_source_binding(conn, str(row["id"])) for row in rows
        ]

    def _artifact(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute(
            """SELECT id, workspace_id, thread_id, kind, title,
                      head_artifact_version_id, head_revision,
                      created_at, updated_at
               FROM artifacts WHERE id = ?""",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise NotFound("artifact", resource_id)
        value = self._row(row)
        Artifact(
            schema_version=1,
            artifact_id=str(value["id"]),
            workspace_id=str(value["workspace_id"]),
            thread_id=str(value["thread_id"]),
            kind=str(value["kind"]),
            title=str(value["title"]),
        )
        value["head_revision"] = int(value["head_revision"])
        return value

    def _artifact_version(
        self,
        conn: sqlite3.Connection,
        resource_id: str,
        *,
        include_action: bool,
    ) -> JsonObject:
        row = conn.execute(
            """SELECT id, artifact_id, logical_version, resource_uri, sha256,
                      byte_length, media_type, run_id, attempt_id,
                      generator_name, generator_version, tool_name, tool_version,
                      state, provenance_json, created_at, committed_at
               FROM artifact_versions WHERE id = ?""",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise NotFound("artifact version", resource_id)
        parent_rows = conn.execute(
            """SELECT parent_artifact_version_id, expected_sha256
               FROM artifact_version_parents
               WHERE artifact_version_id = ? ORDER BY position""",
            (resource_id,),
        ).fetchall()
        source_rows = conn.execute(
            """SELECT source_id FROM artifact_version_sources
               WHERE artifact_version_id = ? ORDER BY position""",
            (resource_id,),
        ).fetchall()
        engine_rows = conn.execute(
            """SELECT engine_ref FROM artifact_version_engine_refs
               WHERE artifact_version_id = ? ORDER BY position""",
            (resource_id,),
        ).fetchall()
        parents = [
            {
                "artifact_version_id": str(item["parent_artifact_version_id"]),
                "sha256": str(item["expected_sha256"]),
            }
            for item in parent_rows
        ]
        value: JsonObject = {
            "id": str(row["id"]),
            "artifact_id": str(row["artifact_id"]),
            "logical_version": int(row["logical_version"]),
            "resource_uri": str(row["resource_uri"]),
            "sha256": str(row["sha256"]),
            "byte_length": int(row["byte_length"]),
            "media_type": str(row["media_type"]),
            "run_id": str(row["run_id"]),
            "attempt_id": str(row["attempt_id"]),
            "generator": {
                "name": str(row["generator_name"]),
                "version": str(row["generator_version"]),
            },
            "tool": {
                "name": str(row["tool_name"]),
                "version": str(row["tool_version"]),
            },
            "parents": parents,
            "source_ids": [str(item["source_id"]) for item in source_rows],
            "research_engine_refs": [
                str(item["engine_ref"]) for item in engine_rows
            ],
            "state": str(row["state"]),
            "provenance": (
                json.loads(str(row["provenance_json"]))
                if row["provenance_json"] is not None
                else None
            ),
            "created_at": str(row["created_at"]),
            "committed_at": (
                str(row["committed_at"])
                if row["committed_at"] is not None
                else None
            ),
        }
        if value["state"] == "committed":
            ArtifactVersion.from_dict(
                {
                    "schema_version": 1,
                    "artifact_version_id": value["id"],
                    "artifact_id": value["artifact_id"],
                    "logical_version": value["logical_version"],
                    "resource_uri": value["resource_uri"],
                    "sha256": value["sha256"],
                    "byte_length": value["byte_length"],
                    "media_type": value["media_type"],
                    "parents": value["parents"],
                    "provenance": value["provenance"],
                    "committed_at": value["committed_at"],
                }
            )
        if include_action:
            action = conn.execute(
                """SELECT id FROM artifact_materialization_actions
                   WHERE artifact_version_id = ?""",
                (resource_id,),
            ).fetchone()
            if action is not None:
                value["materialization_action"] = (
                    self._artifact_materialization_action(conn, str(action["id"]))
                )
        return value

    def _artifact_materialization_action(
        self, conn: sqlite3.Connection, resource_id: str
    ) -> JsonObject:
        row = conn.execute(
            """SELECT id, operation_id, artifact_version_id, request_hash,
                      root_id, relative_path, sha256, byte_length, media_type,
                      advance_head, expected_head_revision, state, claim_owner,
                      claim_epoch, claim_expires_at, attempt_count, result_json,
                      head_advanced, observed_head_revision, failure_category,
                      created_at, completed_at
               FROM artifact_materialization_actions WHERE id = ?""",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise NotFound("artifact materialization", resource_id)
        value = self._row(row)
        value["advance_head"] = bool(value["advance_head"])
        if value["head_advanced"] is not None:
            value["head_advanced"] = bool(value["head_advanced"])
        value["claim_epoch"] = int(value["claim_epoch"])
        value["attempt_count"] = int(value["attempt_count"])
        if value["expected_head_revision"] is not None:
            value["expected_head_revision"] = int(value["expected_head_revision"])
        if value["observed_head_revision"] is not None:
            value["observed_head_revision"] = int(value["observed_head_revision"])
        raw_result = value.pop("result_json")
        value["result"] = json.loads(str(raw_result)) if raw_result is not None else None
        return value

    def _artifact_snapshot(
        self, conn: sqlite3.Connection, resource_id: str
    ) -> JsonObject:
        row = conn.execute(
            """SELECT id, workspace_id, name, member_count, state, created_at
               FROM artifact_snapshots WHERE id = ? AND state = 'committed'""",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise NotFound("artifact snapshot", resource_id)
        member_rows = conn.execute(
            """SELECT artifact_id, artifact_version_id, logical_version, sha256
               FROM artifact_snapshot_members
               WHERE snapshot_id = ? ORDER BY position""",
            (resource_id,),
        ).fetchall()
        members = tuple(
            SnapshotMember(
                artifact_id=str(item["artifact_id"]),
                artifact_version_id=str(item["artifact_version_id"]),
                logical_version=int(item["logical_version"]),
                sha256=str(item["sha256"]),
            )
            for item in member_rows
        )
        snapshot = Snapshot(
            schema_version=1,
            snapshot_id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            name=str(row["name"]),
            members=members,
            created_at=str(row["created_at"]),
        )
        return {
            "id": snapshot.snapshot_id,
            "workspace_id": snapshot.workspace_id,
            "name": snapshot.name,
            "members": [member.to_dict() for member in snapshot.members],
            "created_at": snapshot.created_at,
        }

    def _validate_artifact_run_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        attempt_id: str,
        thread_id: str,
    ) -> tuple[JsonObject, JsonObject]:
        run = self._run(conn, run_id)
        attempt = self._attempt(conn, attempt_id)
        thread = self._thread(conn, thread_id)
        if run["thread_id"] != thread_id or attempt["run_id"] != run_id:
            raise InvalidTransition("run_attempt_ownership", "artifact_operation")
        if run["active_attempt_id"] != attempt_id:
            raise InvalidTransition("attempt_not_active", "artifact_operation")
        if run["state"] not in _ARTIFACT_ACTIVE_RUN_STATES:
            raise InvalidTransition(str(run["state"]), "artifact_operation")
        if attempt["state"] not in _ARTIFACT_ACTIVE_RUN_STATES:
            raise InvalidTransition(str(attempt["state"]), "artifact_operation")
        if thread["active_run_id"] != run_id:
            raise InvalidTransition("run_not_active", "artifact_operation")
        return run, attempt

    def _validate_artifact_sources(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        source_ids: tuple[str, ...],
        research_engine_refs: tuple[str, ...],
    ) -> None:
        context = self._research_context(conn, run_id)
        documents = context["snapshot"].get("documents", ()) if context is not None else ()
        if documents:
            self._validate_research_documents(
                conn, context["snapshot"], thread_id=str(self._run(conn, run_id)["thread_id"]), admission=False
            )
        if not source_ids and not documents:
            raise ValueError("source_ids must not be empty without retained document evidence")
        placeholders = ",".join("?" for _ in source_ids)
        rows = conn.execute(
            f"""SELECT sources.id, sources.engine_ref, sources.import_state
                FROM sources
                JOIN run_source_bindings
                  ON run_source_bindings.source_id = sources.id
                WHERE run_source_bindings.run_id = ?
                  AND sources.id IN ({placeholders})""",
            (run_id, *source_ids),
        ).fetchall()
        missing = set(source_ids) - {str(row["id"]) for row in rows}
        if missing:
            context = self._research_context(conn, run_id)
            run = self._run(conn, run_id)
            if context is not None and context["thread_id"] == run["thread_id"]:
                self._validate_research_sources(conn, context["snapshot"])
                selected = {s["source_id"] for s in context["snapshot"]["sources"]}
                if missing <= selected:
                    rows = list(rows) + list(conn.execute(
                        f"SELECT id, engine_ref, import_state FROM sources WHERE id IN ({','.join('?' for _ in missing)})",
                        tuple(sorted(missing)),
                    ).fetchall())
        if {str(row["id"]) for row in rows} != set(source_ids):
            raise InvalidTransition("source_not_bound_to_run", "artifact_version")
        if any(
            row["import_state"] not in {"existing", "imported"}
            or row["engine_ref"] is None
            for row in rows
        ):
            raise InvalidTransition("source_not_ready", "artifact_version")
        owned_refs = {str(row["engine_ref"]) for row in rows}
        if not set(research_engine_refs) <= owned_refs:
            raise InvalidTransition("engine_ref_not_owned", "artifact_version")

    def _validate_artifact_parents(
        self,
        conn: sqlite3.Connection,
        *,
        artifact: Mapping[str, Any],
        parents: tuple[ParentVersionInput, ...],
    ) -> None:
        for parent in parents:
            version = self._artifact_version(
                conn, parent.artifact_version_id, include_action=False
            )
            parent_artifact = self._artifact(conn, str(version["artifact_id"]))
            if (
                parent_artifact["workspace_id"] != artifact["workspace_id"]
                or parent_artifact["thread_id"] != artifact["thread_id"]
            ):
                raise InvalidTransition("parent_not_owned", "artifact_version")
            if version["state"] != "committed":
                raise InvalidTransition(str(version["state"]), "artifact_parent")
            if version["sha256"] != parent.sha256:
                raise InvalidTransition("parent_hash_mismatch", "artifact_version")

    @staticmethod
    def _artifact_identifier(value: Any, name: str) -> str:
        if (
            not isinstance(value, str)
            or _ARTIFACT_IDENTIFIER_RE.fullmatch(value) is None
        ):
            raise ValueError(f"{name} is invalid")
        return value

    @staticmethod
    def _validated_content_request(
        value: Mapping[str, Any],
    ) -> MaterializationRequest:
        media_type = value.get("media_type")
        normalized = dict(value)
        if isinstance(media_type, str) and ";" in media_type:
            parts = [part.strip() for part in media_type.split(";")]
            if (
                len(parts) != 2
                or parts[0] not in {"text/markdown", "text/plain"}
                or parts[1].lower() != "charset=utf-8"
            ):
                raise ValueError("artifact media type is invalid")
            normalized["media_type"] = parts[0]
        return MaterializationRequest.from_dict(normalized)

    @classmethod
    def _artifact_identifiers(
        cls,
        values: Sequence[str],
        name: str,
        *,
        allow_empty: bool,
    ) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or len(values) > 500:
            raise ValueError(f"{name} is invalid")
        result = tuple(cls._artifact_identifier(value, name) for value in values)
        if not allow_empty and not result:
            raise ValueError(f"{name} must not be empty")
        if len(result) != len(set(result)):
            raise ValueError(f"{name} must contain unique values")
        if list(result) != sorted(result):
            raise ValueError(f"{name} must use canonical order")
        return result

    @staticmethod
    def _artifact_text(value: Any, name: str, *, maximum: int) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > maximum
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError(f"{name} is invalid")
        return value

    @staticmethod
    def _artifact_parents(
        parents: Sequence[Mapping[str, Any]],
    ) -> tuple[ParentVersionInput, ...]:
        if isinstance(parents, (str, bytes)) or len(parents) > 256:
            raise ValueError("parents are invalid")
        values = tuple(ParentVersionInput.from_dict(dict(parent)) for parent in parents)
        identities = [parent.artifact_version_id for parent in values]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("parents must use unique canonical order")
        return values

    @classmethod
    def _artifact_materialized_result(
        cls, value: Mapping[str, Any]
    ) -> MaterializedAsset:
        expected = {
            "schema_version",
            "operation_id",
            "root_id",
            "relative_path",
            "sha256",
            "byte_length",
            "media_type",
            "parents",
            "replayed",
            "recovered_from",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("materialized_result is invalid")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("materialized_result schema is invalid")
        if type(value["replayed"]) is not bool:
            raise ValueError("materialized_result replayed is invalid")
        if value["recovered_from"] not in {None, "staged", "dual-link", "final"}:
            raise ValueError("materialized_result recovery state is invalid")
        request = MaterializationRequest.from_dict(
            {
                "schema_version": 1,
                "operation_id": value["operation_id"],
                "root_id": value["root_id"],
                "relative_path": value["relative_path"],
                "sha256": value["sha256"],
                "byte_length": value["byte_length"],
                "media_type": value["media_type"],
                "parents": value["parents"],
            }
        )
        return MaterializedAsset(
            operation_id=request.operation_id,
            root_id=request.root_id,
            relative_path=request.relative_path,
            sha256=request.sha256,
            byte_length=request.byte_length,
            media_type=request.media_type,
            parents=request.parents,
            replayed=bool(value["replayed"]),
            recovered_from=value["recovered_from"],
        )

    def _insert_source(
        self,
        conn: sqlite3.Connection,
        *,
        authority: str,
        authority_id: str,
        source_kind: str,
        official_title: str,
        engine_ref: str | None,
        import_state: str,
        aliases: Sequence[Mapping[str, str]],
        now: str,
    ) -> str:
        authority, authority_id, canonical_id = self._canonical_source_identity(
            authority, authority_id
        )
        source_id = self._id_factory("source")
        conn.execute(
            """INSERT INTO sources
               (id, authority, authority_id, canonical_id, source_kind,
                official_title, engine_ref, import_state, revision,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (
                source_id,
                authority,
                authority_id,
                canonical_id,
                source_kind,
                official_title,
                engine_ref,
                import_state,
                now,
                now,
            ),
        )
        canonical_alias = {
            "authority": authority,
            "value": authority_id,
            "normalized_value": authority_id.lower(),
        }
        self._insert_source_aliases(
            conn,
            source_id=source_id,
            aliases=[canonical_alias, *aliases],
            now=now,
        )
        return source_id

    def _insert_source_aliases(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: str,
        aliases: Sequence[Mapping[str, str]],
        now: str,
    ) -> None:
        for alias in aliases:
            authority = str(alias["authority"])
            display_value = str(alias["value"])
            normalized_value = str(alias["normalized_value"])
            existing = conn.execute(
                """SELECT source_id FROM source_aliases
                   WHERE authority = ? AND normalized_value = ?""",
                (authority, normalized_value),
            ).fetchone()
            if existing is not None:
                if existing["source_id"] != source_id:
                    raise InvalidTransition("source_alias_collision", "registered")
                continue
            conn.execute(
                """INSERT INTO source_aliases
                   (id, source_id, authority, normalized_value,
                    display_value, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    self._id_factory("source_alias"),
                    source_id,
                    authority,
                    normalized_value,
                    display_value,
                    now,
                ),
            )

    def _insert_run_source_binding(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        source_id: str,
        resolution_id: str,
        disposition: str,
        now: str,
    ) -> str:
        existing = conn.execute(
            """SELECT id, disposition, resolution_id FROM run_source_bindings
               WHERE run_id = ? AND source_id = ?""",
            (run_id, source_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["disposition"] != disposition
                or existing["resolution_id"] != resolution_id
            ):
                raise InvalidTransition("source_binding_collision", "bound")
            return str(existing["id"])
        binding_id = self._id_factory("source_binding")
        conn.execute(
            """INSERT INTO run_source_bindings
               (id, run_id, source_id, resolution_id, disposition, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (binding_id, run_id, source_id, resolution_id, disposition, now),
        )
        return binding_id

    def _insert_source_import_action(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        source_id: str,
        resolution_id: str,
        canonical_id: str,
        now: str,
    ) -> str:
        action_id = self._id_factory("source_import")
        operation_id = f"source-import:{action_id}"
        request_hash = self._request_hash(
            {
                "operation_id": operation_id,
                "source_id": source_id,
                "canonical_id": canonical_id,
            }
        )
        conn.execute(
            """INSERT INTO source_import_actions
               (id, operation_id, run_id, source_id, resolution_id,
                request_hash, state, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (
                action_id,
                operation_id,
                run_id,
                source_id,
                resolution_id,
                request_hash,
                now,
            ),
        )
        return action_id

    def _insert_source_import_waiter(
        self,
        conn: sqlite3.Connection,
        *,
        action_id: str,
        run_id: str,
        resolution_id: str,
        now: str,
    ) -> str:
        existing = conn.execute(
            """SELECT id, resolution_id, state FROM source_import_waiters
               WHERE action_id = ? AND run_id = ?""",
            (action_id, run_id),
        ).fetchone()
        if existing is not None:
            if existing["resolution_id"] != resolution_id:
                raise InvalidTransition("source_waiter_collision", "waiting")
            if existing["state"] != "waiting":
                raise InvalidTransition(str(existing["state"]), "waiting")
            return str(existing["id"])
        waiter_id = self._id_factory("source_waiter")
        conn.execute(
            """INSERT INTO source_import_waiters
               (id, action_id, run_id, resolution_id, state, created_at)
               VALUES (?, ?, ?, ?, 'waiting', ?)""",
            (waiter_id, action_id, run_id, resolution_id, now),
        )
        return waiter_id

    def _normalize_source_candidates(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> list[JsonObject]:
        if isinstance(candidates, (str, bytes)) or not 1 <= len(candidates) <= 20:
            raise ValueError("candidates must contain between 1 and 20 entries")
        normalized: list[JsonObject] = []
        for raw in candidates:
            if not isinstance(raw, Mapping):
                raise ValueError("source candidate is invalid")
            observation = CandidateObservation(
                claim_kind=raw.get("claim_kind"),
                authority=raw.get("authority"),
                authority_id=raw.get("authority_id"),
                official_title=raw.get("official_title"),
                version=raw.get("version"),
                locator=raw.get("locator"),
                evidence=raw.get("evidence", {}),
            )
            value = observation.to_record()
            if observation.locator is not None and observation.authority == "arxiv":
                locator_identity = canonicalize_locator(observation.locator)
                if locator_identity.canonical_id != observation.canonical_id:
                    raise ValueError(
                        "candidate locator does not match its authority identity"
                    )
                if locator_identity.version != observation.version:
                    raise ValueError(
                        "candidate locator version does not match its observation"
                    )
            elif observation.locator is not None and observation.authority == "doi":
                locator_identity = canonicalize_doi(observation.locator)
                if locator_identity.canonical_id != observation.canonical_id:
                    raise ValueError(
                        "candidate locator does not match its authority identity"
                    )
            elif observation.locator is not None and observation.authority == "sha256":
                locator_identity = canonicalize_source_locator(
                    observation.locator,
                    local_sha256=observation.authority_id,
                )
                if locator_identity.canonical_id != observation.canonical_id:
                    raise ValueError(
                        "candidate locator does not match its authority identity"
                    )
            supplied = raw.get("canonical_id")
            if supplied is not None and supplied != value["canonical_id"]:
                raise ValueError("candidate canonical_id is inconsistent")
            normalized.append(value)
        return normalized

    def _normalize_source_aliases(
        self, aliases: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, str]]:
        if isinstance(aliases, (str, bytes)) or len(aliases) > 100:
            raise ValueError("source aliases are invalid")
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for alias in aliases:
            if not isinstance(alias, Mapping) or set(alias) != {"authority", "value"}:
                raise ValueError("source alias is invalid")
            authority = self._normalize_source_authority(alias["authority"])
            if authority not in _SOURCE_ALIAS_AUTHORITIES:
                raise ValueError("source alias authority is unsupported")
            value = alias["value"]
            if not isinstance(value, str):
                raise ValueError("source alias is invalid")
            try:
                value.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("source alias must contain ASCII only") from exc
            value = value.strip()
            if _SOURCE_ALIAS_RE.fullmatch(value) is None:
                raise ValueError("source alias is invalid")
            if authority == "project":
                if _PROJECT_ALIAS_RE.fullmatch(value) is None:
                    raise ValueError("source alias value is invalid")
                normalized_value = value.casefold()
            elif authority == "arxiv":
                if "://" in value:
                    raise ValueError("source alias value is invalid")
                try:
                    normalized_value = canonicalize_arxiv_id(value).authority_id
                except ValueError as exc:
                    raise ValueError("source alias value is invalid") from exc
            elif authority == "doi":
                if "://" in value:
                    raise ValueError("source alias value is invalid")
                try:
                    normalized_value = canonicalize_doi(value).authority_id
                except ValueError as exc:
                    raise ValueError("source alias value is invalid") from exc
            else:
                if re.fullmatch(r"[0-9a-f]{64}", value, re.IGNORECASE) is None:
                    raise ValueError("source alias value is invalid")
                normalized_value = value.lower()
            key = (authority, normalized_value)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "authority": authority,
                    "value": value,
                    "normalized_value": normalized_value,
                }
            )
        return normalized

    @classmethod
    def _canonical_source_identity(
        cls, authority: Any, authority_id: Any
    ) -> tuple[str, str, str]:
        normalized_authority = cls._normalize_source_authority(authority)
        if not isinstance(authority_id, str):
            raise ValueError("source authority_id is invalid")
        try:
            authority_id.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("source authority_id must contain ASCII only") from exc
        authority_id = authority_id.strip()
        if _SOURCE_ALIAS_RE.fullmatch(authority_id) is None:
            raise ValueError("source authority_id is invalid")
        if normalized_authority == "arxiv":
            normalized_id = canonicalize_arxiv_id(authority_id).authority_id
        elif normalized_authority == "doi":
            normalized_id = canonicalize_doi(authority_id).authority_id
        elif normalized_authority == "sha256":
            if re.fullmatch(r"[0-9a-f]{64}", authority_id, re.IGNORECASE) is None:
                raise ValueError("SHA-256 authority_id is invalid")
            normalized_id = authority_id.lower()
        else:
            raise ValueError("source authority is not supported")
        return (
            normalized_authority,
            normalized_id,
            f"{normalized_authority}:{normalized_id}",
        )

    @staticmethod
    def _normalize_source_authority(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("source authority is invalid")
        value = value.strip().lower()
        if _SOURCE_AUTHORITY_RE.fullmatch(value) is None:
            raise ValueError("source authority is invalid")
        return value

    @staticmethod
    def _normalize_locator_claim(
        locator: str | None, *, locator_sha256: str | None
    ) -> tuple[str | None, Any | None, str | None]:
        if locator is None:
            if locator_sha256 is not None:
                raise ValueError("locator_sha256 requires a locator")
            return None, None, None
        identity = canonicalize_source_locator(
            locator, local_sha256=locator_sha256
        )
        digest = identity.authority_id if identity.authority == "sha256" else None
        return identity.normalized_locator, identity, digest

    @staticmethod
    def _validate_source_observation_coverage(
        *,
        title: str | None,
        locator_identity: Any | None,
        candidates: Sequence[Mapping[str, Any]],
    ) -> None:
        if title is not None and not any(
            candidate.get("claim_kind") == "title" for candidate in candidates
        ):
            raise ValueError("resolver returned no title observation")
        if locator_identity is None:
            return
        matches = [
            candidate
            for candidate in candidates
            if candidate.get("claim_kind") == locator_identity.claim_kind
            and candidate.get("canonical_id") == locator_identity.canonical_id
            and candidate.get("version") == locator_identity.version
        ]
        if not matches:
            raise ValueError("resolver returned no exact locator observation")
        if locator_identity.claim_kind in {"url", "local_file"} and not any(
            candidate.get("locator") for candidate in matches
        ):
            raise ValueError("resolver locator observation is missing its locator")

    @staticmethod
    def _validate_engine_ref(value: Any) -> str:
        return validate_engine_ref(value)

    @staticmethod
    def _validate_import_manifest(value: Mapping[str, Any]) -> JsonObject:
        expected = {"source_rows", "chunks", "directories"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("result_manifest is invalid")
        result: JsonObject = {}
        for key in sorted(expected):
            item = value[key]
            if type(item) is not int or item < 0 or item > 1_000_000:
                raise ValueError("result_manifest is invalid")
            result[key] = item
        return result

    def _binding(self, conn: sqlite3.Connection, resource_id: str) -> JsonObject:
        row = conn.execute(
            """SELECT id, transport, external_scope, thread_id, revision,
                      created_at, updated_at
               FROM transport_bindings WHERE id = ?""",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise NotFound("transport binding", resource_id)
        return self._row(row)

    @staticmethod
    def _event(row: sqlite3.Row) -> JsonObject:
        return {
            "cursor": int(row["global_cursor"]),
            "schema_version": int(row["schema_version"]),
            "id": row["id"],
            "run_id": row["run_id"],
            "attempt_id": row["attempt_id"],
            "sequence": int(row["sequence"]),
            "type": row["type"],
            "occurred_at": row["occurred_at"],
            "causation_id": row["causation_id"],
            "durability": row["durability"],
            "payload": json.loads(row["payload_json"]),
        }

    def _update_revision(
        self,
        conn: sqlite3.Connection,
        table: str,
        resource_id: str,
        expected_revision: int,
        now: str,
    ) -> None:
        if table not in {"workspaces", "threads"}:
            raise ValueError("unsupported revision table")
        cursor = conn.execute(
            f"UPDATE {table} SET revision = revision + 1, updated_at = ? "
            "WHERE id = ? AND revision = ?",
            (now, resource_id, expected_revision),
        )
        if cursor.rowcount != 1:
            getter = self._workspace if table == "workspaces" else self._thread
            raise RevisionConflict(getter(conn, resource_id))

    @staticmethod
    def _expect_revision(resource: Mapping[str, Any], expected: int) -> None:
        if type(expected) is not int or expected < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if resource.get("revision") != expected:
            raise RevisionConflict(dict(resource))

    @staticmethod
    def _required_text(value: str, name: str, *, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ValueError(f"{name} is invalid")
        return value.strip()

    def _public_decision_text(self, value: str, name: str, *, maximum: int) -> str:
        text = self._required_text(value, name, maximum=maximum)
        if any(ord(character) < 32 and character not in "\n\t" for character in text):
            raise ValueError(f"{name} contains invalid control characters")
        return text

    def _validate_decision_options(
        self, options: Sequence[Mapping[str, Any]]
    ) -> list[JsonObject]:
        if isinstance(options, (str, bytes)) or not 1 <= len(options) <= 20:
            raise ValueError("options must contain between 1 and 20 entries")
        normalized: list[JsonObject] = []
        option_ids: set[str] = set()
        for option in options:
            if not isinstance(option, Mapping):
                raise ValueError("decision option is invalid")
            keys = {str(key) for key in option}
            sensitive = {key.lower() for key in keys} & _SENSITIVE_DTO_FIELDS
            if sensitive or not keys <= _DECISION_OPTION_FIELDS:
                raise ValueError("decision option contains private fields")
            option_id = self._public_decision_text(
                option.get("id"), "option.id", maximum=200
            )
            if option_id in option_ids:
                raise ValueError("decision option ids must be unique")
            option_ids.add(option_id)
            value: JsonObject = {"id": option_id}
            if "label" in option:
                value["label"] = self._public_decision_text(
                    option.get("label"), "option.label", maximum=500
                )
            if "description" in option:
                value["description"] = self._public_decision_text(
                    option.get("description"),
                    "option.description",
                    maximum=2_000,
                )
            normalized.append(value)
        return normalized

    def _validate_recovery_details(
        self, details: Mapping[str, Any]
    ) -> JsonObject:
        allowed = frozenset({"category", "status", "retryable", "message"})
        keys = {str(key) for key in details}
        if ({key.lower() for key in keys} & _SENSITIVE_DTO_FIELDS) or not keys <= allowed:
            raise ValueError("recovery details contain private fields")
        value: JsonObject = {}
        for field in ("category", "status", "message"):
            if field in details:
                maximum = 2_000 if field == "message" else 200
                value[field] = self._public_decision_text(
                    details[field], field, maximum=maximum
                )
        if "retryable" in details:
            if type(details["retryable"]) is not bool:
                raise ValueError("retryable must be a boolean")
            value["retryable"] = details["retryable"]
        return value

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
            raise ValueError("idempotency key must be 16-128 URL-safe characters")

    @staticmethod
    def _transport_name(value: Any) -> str:
        if not isinstance(value, str) or _TRANSPORT_NAME_RE.fullmatch(value) is None:
            raise ValueError("transport is invalid")
        return value

    @staticmethod
    def _transport_digest(value: Any, name: str) -> str:
        if not isinstance(value, str) or _HEX_DIGEST_RE.fullmatch(value) is None:
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        return value

    @classmethod
    def _transport_command_response(
        cls, value: Any
    ) -> TransportCommandResponse:
        if not isinstance(value, TransportCommandResponse):
            raise TypeError("response must be a TransportCommandResponse")
        if any(
            type(item) is not bool
            for item in (value.ok, value.mutated, value.replayed, value.retryable)
        ):
            raise ValueError("transport command response booleans are invalid")
        category = cls._required_text(value.category, "category", maximum=100)
        action = cls._required_text(value.action, "action", maximum=100)
        response_text = cls._required_text(
            value.response_text, "response_text", maximum=4_000
        )
        if any(
            ord(character) < 32 and character not in "\n\t"
            for character in response_text
        ):
            raise ValueError("response_text contains invalid control characters")
        if value.retry_after_ms is not None and (
            type(value.retry_after_ms) is not int or value.retry_after_ms < 1
        ):
            raise ValueError("retry_after_ms must be a positive integer")
        state = (
            cls._required_text(value.state, "state", maximum=200)
            if value.state is not None
            else None
        )
        if value.revision is not None and (
            type(value.revision) is not int or value.revision < 0
        ):
            raise ValueError("revision must be a non-negative integer")
        return TransportCommandResponse(
            ok=value.ok,
            category=category,
            action=action,
            response_text=response_text,
            mutated=value.mutated,
            replayed=value.replayed,
            retryable=value.retryable,
            retry_after_ms=value.retry_after_ms,
            state=state,
            revision=value.revision,
        )

    @staticmethod
    def _parse_control_time(value: str) -> datetime:
        return datetime.fromisoformat(value).astimezone(UTC)

    def _transport_command_row(
        self,
        conn: sqlite3.Connection,
        transport: str,
        command_key: str,
    ) -> TransportCommandReceipt | None:
        row = conn.execute(
            """SELECT transport, command_key, request_hash, response_json, created_at
               FROM transport_command_receipts
               WHERE transport = ? AND command_key = ?""",
            (transport, command_key),
        ).fetchone()
        if row is None:
            return None
        response = json.loads(str(row["response_json"]))
        if not isinstance(response, dict):
            raise RuntimeError("transport command response is invalid")
        try:
            typed_response = TransportCommandResponse(**response)
        except TypeError as exc:
            raise RuntimeError("transport command response is invalid") from exc
        return TransportCommandReceipt(
            transport=str(row["transport"]),
            command_key=str(row["command_key"]),
            request_hash=str(row["request_hash"]),
            response=self._transport_command_response(typed_response),
            created_at=self._parse_control_time(str(row["created_at"])),
        )

    @staticmethod
    def _validate_adapter_event_id(adapter_event_id: str) -> None:
        if (
            not isinstance(adapter_event_id, str)
            or _ADAPTER_EVENT_RE.fullmatch(adapter_event_id) is None
        ):
            raise ValueError("adapter_event_id is invalid")

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _format_time(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _now(self) -> str:
        return self._format_time(self._utc_now())

    def _registry_now(self) -> str:
        return self._format_registry_time(self._utc_now())

    @staticmethod
    def _format_registry_time(value: datetime) -> str:
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @classmethod
    def _request_hash(cls, request: Mapping[str, Any]) -> str:
        return hashlib.sha256(cls._json(dict(request)).encode()).hexdigest()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
