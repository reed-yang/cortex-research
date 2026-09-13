from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from cortex_platform.product.control import ControlStore
from cortex_platform.product.sources import CandidateObservation


ECHO_TITLE = (
    "Echo-Infinity: Learnable Evolving Memory for Real-Time Infinite Video "
    "Generation"
)
LINGBOT_TITLE = (
    "Scaling Mixture-of-Experts Video Pretraining for Embodied Intelligence"
)
LINGBOT_URL = "https://arxiv.org/pdf/2607.07675"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def __call__(self, kind: str) -> str:
        with self._lock:
            self._counts[kind] += 1
            return f"{kind}-{self._counts[kind]}"


class GoldenResolver:
    def resolve(
        self,
        *,
        title: str | None,
        locator: str | None,
        locator_sha256: str | None = None,
    ) -> tuple[CandidateObservation, ...]:
        assert locator_sha256 is None
        assert title == ECHO_TITLE
        assert locator == LINGBOT_URL
        return (
            CandidateObservation(
                claim_kind="title",
                authority="arxiv",
                authority_id="2606.04527",
                official_title=ECHO_TITLE,
                locator="https://arxiv.org/abs/2606.04527",
            ),
            CandidateObservation(
                claim_kind="url",
                authority="arxiv",
                authority_id="2607.07675",
                official_title=LINGBOT_TITLE,
                locator="https://arxiv.org/abs/2607.07675",
            ),
        )


def make_store(tmp_path, *, clock: MutableClock | None = None) -> ControlStore:
    value = ControlStore(
        tmp_path / "control.db",
        clock=clock or MutableClock(),
        id_factory=DeterministicIds(),
    )
    value.initialize()
    return value


def make_run(store: ControlStore) -> dict:
    return make_named_run(store, "default")


def make_named_run(store: ControlStore, suffix: str) -> dict:
    workspace = store.create_workspace(
        title=f"Research {suffix}",
        actor_id=f"local-{suffix}",
        idempotency_key=f"source-workspace-{suffix}-0001",
    ).value
    thread = store.create_thread(
        workspace_id=workspace["id"],
        title="Echo / Helios",
        expected_revision=workspace["revision"],
        actor_id=f"local-{suffix}",
        idempotency_key=f"source-thread-{suffix}-000001",
    ).value
    return store.create_run(
        thread_id=thread["id"],
        expected_revision=thread["revision"],
        actor_id=f"local-{suffix}",
        idempotency_key=f"source-run-{suffix}-00000001",
    ).value


def register_echo(store: ControlStore) -> dict:
    return store.register_source(
        authority="arxiv",
        authority_id="2606.04527",
        source_kind="paper",
        official_title=ECHO_TITLE,
        engine_ref="paper:echo-existing",
        aliases=(
            {"authority": "project", "value": "Echo-Infinity"},
        ),
        actor_id="fixture",
        idempotency_key="register-echo-000001",
    ).value


def create_golden_intent(store: ControlStore, run: dict) -> dict:
    candidates = [candidate.to_record() for candidate in GoldenResolver().resolve(
        title=ECHO_TITLE, locator=LINGBOT_URL, locator_sha256=None
    )]
    return store.create_source_intent(
        run_id=run["id"],
        attempt_id=run["attempt"]["id"],
        title=ECHO_TITLE,
        locator=LINGBOT_URL,
        candidates=candidates,
        actor_id="local",
        idempotency_key="source-intent-0001",
    ).value
