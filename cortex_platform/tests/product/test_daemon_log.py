"""⟦P5.6⟧ The daemon writes a log, and the log is bounded and secret-free.

`cortexd.log` had been 0 bytes since July: it was only ever the launcher's
stdout/stderr redirect, and the daemon configured no `logging` handler. The
sixth window's poller failed in bursts for sixteen minutes and the machine
recorded nothing.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cortex_platform.product import daemon as daemon_module
from cortex_platform.product import daemon_log
from cortex_platform.product.daemon_log import (
    BACKUP_COUNT,
    MAX_BYTES,
    PRODUCT_LOGGER,
    ROOT_LOGGER,
    configure_daemon_logging,
    release_daemon_logging,
)

FAKE_TOKEN = "000000000:FAKE-TOKEN-FOR-TESTS-ONLY-NOT-A-CREDENTIAL"


@pytest.fixture
def log_path(tmp_path: Path):
    path = tmp_path / "log" / "cortexd.log"
    handler = configure_daemon_logging(path)
    try:
        yield path
    finally:
        release_daemon_logging(handler)


def test_product_loggers_reach_the_file(log_path: Path) -> None:
    logging.getLogger("cortex_platform.product.transports.worker_rpc").warning(
        "telegram poller failure: WorkerProtocolError: worker response timed out"
    )
    text = log_path.read_text(encoding="utf-8")
    assert "worker response timed out" in text
    assert "WARNING cortex_platform.product.transports.worker_rpc" in text


def test_the_file_is_private_and_a_symlink_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "log" / "cortexd.log"
    handler = configure_daemon_logging(path)
    try:
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        release_daemon_logging(handler)

    victim = tmp_path / "victim"
    victim.write_text("do-not-append", encoding="utf-8")
    link = tmp_path / "linked" / "cortexd.log"
    link.parent.mkdir()
    link.symlink_to(victim)
    handler = configure_daemon_logging(link)
    try:
        # Fell back to stderr rather than following the link.
        assert isinstance(handler, logging.StreamHandler)
        assert not isinstance(handler, logging.FileHandler)
        logging.getLogger(ROOT_LOGGER).info("must not land in the victim")
    finally:
        release_daemon_logging(handler)
    assert victim.read_text(encoding="utf-8") == "do-not-append"


def test_a_secret_in_a_log_call_never_reaches_the_disk(log_path: Path) -> None:
    logger = logging.getLogger("cortex_platform.product.test")
    logger.info("poll failed for /bot%s/getUpdates?offset=1", FAKE_TOKEN)
    logger.error("boom", exc_info=RuntimeError(f"token={FAKE_TOKEN}"))
    text = log_path.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text
    assert "offset=1" not in text
    assert "/bot[redacted]/getUpdates" in text
    assert "[RuntimeError: token=[redacted]]" in text
    assert "Traceback" not in text


def test_rotation_is_bounded_at_five_files_of_five_mib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_log, "MAX_BYTES", 2_000)
    path = tmp_path / "log" / "cortexd.log"
    handler = configure_daemon_logging(path)
    try:
        logger = logging.getLogger("cortex_platform.product.test")
        for index in range(400):
            logger.info("line %s %s", index, "x" * 80)
    finally:
        release_daemon_logging(handler)
    files = sorted(p.name for p in path.parent.iterdir())
    assert files == ["cortexd.log"] + [f"cortexd.log.{n}" for n in range(1, BACKUP_COUNT + 1)]
    assert all((path.parent / name).stat().st_size <= 2_000 + 200 for name in files)
    assert MAX_BYTES == 5 * 1024 * 1024 and BACKUP_COUNT == 4


def test_stdout_and_stderr_follow_the_live_file_across_a_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher redirects fds 1/2 into the log; a rename must not strand them."""

    monkeypatch.setattr(daemon_log, "MAX_BYTES", 500)
    path = tmp_path / "log" / "cortexd.log"
    path.parent.mkdir(parents=True)
    launcher_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    saved = (os.dup(1), os.dup(2))
    try:
        os.dup2(launcher_fd, 1)
        os.dup2(launcher_fd, 2)
        handler = configure_daemon_logging(path)
        try:
            logger = logging.getLogger("cortex_platform.product.test")
            for index in range(40):
                logger.info("rotate me %s %s", index, "y" * 60)
            os.write(2, b"traceback-after-rotation\n")
        finally:
            release_daemon_logging(handler)
    finally:
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])
        os.close(launcher_fd)
    assert (path.parent / "cortexd.log.1").exists()
    assert "traceback-after-rotation" in path.read_text(encoding="utf-8")


def test_fds_that_do_not_point_at_the_log_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_log, "MAX_BYTES", 500)
    path = tmp_path / "log" / "cortexd.log"
    before = (os.fstat(1).st_ino, os.fstat(2).st_ino)
    handler = configure_daemon_logging(path)
    try:
        logger = logging.getLogger("cortex_platform.product.test")
        for index in range(40):
            logger.info("rotate me %s %s", index, "z" * 60)
    finally:
        release_daemon_logging(handler)
    assert (os.fstat(1).st_ino, os.fstat(2).st_ino) == before


class _ImmediateServer:
    def __init__(self, stop_requested) -> None:
        self.server_address = ("127.0.0.1", 51_234)
        self.timeout = 0.0
        self._stop_requested = stop_requested

    def handle_request(self) -> None:
        self._stop_requested.set()

    def request_stop(self) -> None:
        self._stop_requested.set()

    def server_close(self) -> None:
        return None


def test_a_daemon_start_and_stop_leave_lines_in_the_log_and_no_handler_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _server(**kwargs: object) -> _ImmediateServer:
        return _ImmediateServer(kwargs["stop_requested"])

    monkeypatch.setattr(daemon_module, "create_server", _server)
    arguments = [
        "--instance-id", "log-instance",
        "--config-dir", str(tmp_path / "config"),
        "--data-dir", str(tmp_path / "data"),
        "--state-dir", str(tmp_path / "state"),
        "--cache-dir", str(tmp_path / "cache"),
        "--log-dir", str(tmp_path / "log"),
    ]
    handlers_before = list(logging.getLogger(ROOT_LOGGER).handlers)
    assert daemon_module.main(arguments) == 0
    assert daemon_module.main(arguments) == 0
    assert logging.getLogger(ROOT_LOGGER).handlers == handlers_before
    text = (tmp_path / "log" / "cortexd.log").read_text(encoding="utf-8")
    assert text.count("cortexd starting instance=log-instance") == 2
    assert text.count("cortexd stopped instance=log-instance") == 2
    assert "cortexd serving port=51234" in text


def test_the_daemon_logs_under_the_collected_tree_even_when_run_as_main() -> None:
    """Found by the P5.6 acceptance: under `python -m ...daemon` the module's
    `__name__` is `__main__`, and a `getLogger(__name__)` there sits outside
    the `cortex_platform` handler -- the window's lines arrived, the daemon's
    own did not. The name is a literal, and the literal is below the root."""

    assert daemon_module.DAEMON_LOGGER == "cortex_platform.product.daemon"
    assert daemon_module._log.name == daemon_module.DAEMON_LOGGER
    assert daemon_module._log.name.startswith(PRODUCT_LOGGER + ".")
    source = Path(daemon_module.__file__).read_text(encoding="utf-8")
    assert "getLogger(__name__)" not in source



def test_a_foreign_logger_is_redacted_by_the_same_handler_and_kept_to_warnings(
    tmp_path: Path,
) -> None:
    """⟦Batch F P56-OBS-2⟧ The handler was bound to `cortex_platform` only, so
    a library logger fell through `lastResort` to fd 2 -- the launcher's
    unredacted route into the same file. On the root logger now: a foreign
    WARNING lands redacted, a foreign INFO does not spend the budget, and the
    product's own INFO still lands."""

    path = tmp_path / "logs" / "cortexd.log"
    foreign = logging.getLogger("urllib3.connectionpool")
    root_before = logging.getLogger(ROOT_LOGGER).level
    product_before = logging.getLogger(PRODUCT_LOGGER).level
    handler = configure_daemon_logging(path)
    try:
        assert ROOT_LOGGER == ""
        assert handler in logging.getLogger().handlers
        foreign.warning("retrying https://api.telegram.org/bot12345:%s/getMe", "A" * 30)
        foreign.info("foreign chatter that must not reach the disk")
        logging.getLogger("cortex_platform.product.test").info("product info line")
    finally:
        release_daemon_logging(handler)
    text = path.read_text(encoding="utf-8")
    assert "urllib3.connectionpool: retrying https://api.telegram.org/bot[redacted]/getMe" in text
    assert "A" * 30 not in text
    assert "foreign chatter" not in text
    assert "product info line" in text
    assert handler not in logging.getLogger().handlers
    assert logging.getLogger(ROOT_LOGGER).level == root_before
    assert logging.getLogger(PRODUCT_LOGGER).level == product_before


def test_timestamps_are_utc_and_say_so(tmp_path: Path) -> None:
    """⟦P5.6-nit⟧ The stamp was local time without a zone: `2026-09-03T22:27:08`
    on the mini meant 03:27Z, and correlating a ceremony against the log was
    guesswork. Every handler the module installs -- the rotating file handler
    and the stderr fallback -- stamps the record's UTC time with an explicit
    `Z`, whatever zone the process runs in."""

    instant = datetime(2026, 9, 4, 3, 27, 8, tzinfo=timezone.utc)
    record = logging.LogRecord(
        "cortex_platform.product.test", logging.INFO, __file__, 0, "stamped", (), None
    )
    record.created = instant.timestamp()
    expected = "2026-09-04T03:27:08Z INFO cortex_platform.product.test: stamped"

    previous_zone = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"  # UTC+8 without DST: local never equals UTC
    time.tzset()
    try:
        assert time.strftime("%H", time.localtime(record.created)) == "11"
        formatter = daemon_log.RedactingFormatter(daemon_log._FORMAT, daemon_log._DATE)
        assert formatter.format(record) == expected

        path = tmp_path / "log" / "cortexd.log"
        handler = configure_daemon_logging(path)
        try:
            assert isinstance(handler, logging.FileHandler)
            logging.getLogger(record.name).handle(record)
        finally:
            release_daemon_logging(handler)
        assert path.read_text(encoding="utf-8").splitlines() == [expected]

        victim = tmp_path / "victim"
        victim.write_text("", encoding="utf-8")
        link = tmp_path / "linked" / "cortexd.log"
        link.parent.mkdir()
        link.symlink_to(victim)
        fallback = configure_daemon_logging(link)
        try:
            assert not isinstance(fallback, logging.FileHandler)
            assert fallback.formatter is not None
            assert fallback.formatter.format(record) == expected
        finally:
            release_daemon_logging(fallback)
    finally:
        if previous_zone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_zone
        time.tzset()

    stamp = expected.split(" ", 1)[0]
    assert stamp.endswith("Z")
    assert datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) == instant
