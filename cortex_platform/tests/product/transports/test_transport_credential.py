"""D-P5-4: the bot token is scoped to the window, not to the process.

The credential never enters `control.db`, the web environment, doctor or a
log. It is resolved in the parent from a `secret_refs` alias and revealed into
exactly one allowlisted worker environment key -- and only while the transport
gate says `enable`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cortex_platform.product.config import validate_config, ConfigError
from cortex_platform.product.control import ControlStore
from cortex_platform.product.runtime_update.supervisor import (
    CREDENTIAL_KEYS,
    TRANSPORT_CREDENTIAL_KEYS,
    WorkerEnvironmentError,
    worker_environment,
)
from cortex_platform.product.secrets import SecretResolver
from cortex_platform.product.transports.worker_rpc import (
    TELEGRAM_CREDENTIAL_KEY,
    TELEGRAM_SECRET_ALIAS,
    transport_credential_bindings,
)

#: Obviously fake. No real credential exists anywhere in this repository.
FAKE_TOKEN = "000000000:FAKE-TOKEN-FOR-TESTS-ONLY-NOT-A-CREDENTIAL"
_NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path):
    clock = {"now": _NOW}
    value = ControlStore(tmp_path / "control.db", clock=lambda: clock["now"])
    value.initialize()
    value.clock_box = clock  # type: ignore[attr-defined]
    return value


def _environment(tmp_path: Path, store: ControlStore) -> dict[str, str]:
    return worker_environment(
        state_dir=tmp_path / "state",
        token="t" * 40,
        secret_refs={TELEGRAM_SECRET_ALIAS: f"env://{TELEGRAM_CREDENTIAL_KEY}"},
        credential_bindings=transport_credential_bindings(store),
        resolver=SecretResolver(environment={TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN}),
    )


def test_a_closed_gate_builds_a_worker_environment_without_the_token(
    tmp_path: Path, store: ControlStore
) -> None:
    environment = _environment(tmp_path, store)
    assert TELEGRAM_CREDENTIAL_KEY not in environment
    assert FAKE_TOKEN not in environment.values()


def test_an_open_window_reveals_the_token_into_exactly_one_key(
    tmp_path: Path, store: ControlStore
) -> None:
    store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )
    environment = _environment(tmp_path, store)
    assert environment[TELEGRAM_CREDENTIAL_KEY] == FAKE_TOKEN
    assert [
        key for key, value in environment.items() if value == FAKE_TOKEN
    ] == [TELEGRAM_CREDENTIAL_KEY]


def test_a_relaunch_after_the_window_closes_does_not_carry_the_token(
    tmp_path: Path, store: ControlStore
) -> None:
    """The whole point of ⟦AMD-4⟧: the key is window-scoped, not process-scoped."""

    store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=60,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )
    assert TELEGRAM_CREDENTIAL_KEY in _environment(tmp_path, store)

    store.disable_transport_activation(
        transport="telegram",
        actor_id="operator",
        idempotency_key="tg-disable-000000001",
    )
    assert TELEGRAM_CREDENTIAL_KEY not in _environment(tmp_path, store)


def test_an_expired_window_does_not_carry_the_token_either(
    tmp_path: Path, store: ControlStore
) -> None:
    store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=60,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )
    store.clock_box["now"] = _NOW + timedelta(seconds=61)  # type: ignore[attr-defined]
    assert TELEGRAM_CREDENTIAL_KEY not in _environment(tmp_path, store)


def test_the_transport_key_is_not_in_the_forks_credential_allowlist() -> None:
    """Two allowlists, because they answer different questions.

    `CREDENTIAL_KEYS` is pinned to names the fork's `auth.py` actually reads.
    The bot token is read by `cortex_worker.telegram`, which is product code,
    so putting it in that tuple would have made the fork-surface assertion a
    lie.
    """

    assert TELEGRAM_CREDENTIAL_KEY not in CREDENTIAL_KEYS
    assert TELEGRAM_CREDENTIAL_KEY in TRANSPORT_CREDENTIAL_KEYS


def test_an_unallowlisted_key_is_still_refused(tmp_path: Path) -> None:
    with pytest.raises(WorkerEnvironmentError, match="credential key"):
        worker_environment(
            state_dir=tmp_path / "state",
            token="t" * 40,
            secret_refs={"a": "env://X"},
            credential_bindings={"a": "TELEGRAM_BOT_TOKEN"},
            resolver=SecretResolver(environment={"X": "v"}),
        )


def test_the_config_can_name_the_alias_but_never_a_raw_token() -> None:
    """`research_bot` validates today; a token-shaped alias never will."""

    assert validate_config(
        {
            "config_version": 1,
            "secret_refs": {
                TELEGRAM_SECRET_ALIAS: f"env://{TELEGRAM_CREDENTIAL_KEY}"
            },
        }
    )["secret_refs"] == {TELEGRAM_SECRET_ALIAS: f"env://{TELEGRAM_CREDENTIAL_KEY}"}

    for alias in ("telegram_bot_token", "bot_token", "api_key", "research_secret"):
        with pytest.raises(ConfigError, match="logical alias"):
            validate_config(
                {"config_version": 1, "secret_refs": {alias: "env://X"}}
            )


def test_the_token_never_reaches_control_db_or_a_report(
    tmp_path: Path, store: ControlStore
) -> None:
    """The P2b proof, extended to this key.

    The gate records that a window was opened; it never records what was
    revealed into the worker. A grep of the whole database file is the blunt
    version of that claim and the one that would catch an accidental audit
    payload.
    """

    store.enable_transport_activation(
        transport="telegram",
        scope="window",
        window_seconds=600,
        actor_id="operator",
        idempotency_key="tg-window-000000001",
    )
    environment = _environment(tmp_path, store)
    assert environment[TELEGRAM_CREDENTIAL_KEY] == FAKE_TOKEN

    assert FAKE_TOKEN.encode() not in Path(store.path).read_bytes()
    assert TELEGRAM_CREDENTIAL_KEY.encode() not in Path(store.path).read_bytes()

    from cortex_platform.product.diagnostics import doctor
    from cortex_platform.product.paths import resolve_paths

    paths = resolve_paths(
        cli_overrides={
            "config_dir": str(tmp_path / "config"),
            "data_dir": str(tmp_path / "data"),
            "state_dir": str(tmp_path / "dstate"),
            "cache_dir": str(tmp_path / "cache"),
            "log_dir": str(tmp_path / "log"),
        }
    )
    report = repr(doctor(paths, environ={TELEGRAM_CREDENTIAL_KEY: FAKE_TOKEN}))
    assert FAKE_TOKEN not in report
    assert TELEGRAM_CREDENTIAL_KEY not in report
