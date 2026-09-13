"""The daemon's own log file: bounded, rotating, redacted.

⟦P5.6⟧ `~/Library/Logs/Cortex/cortexd.log` had been 0 bytes since July. It
was never the daemon's log: both launchers (`product/lifecycle.py` and
`distribution/lifecycle.py`) open that path and hand it to `Popen` as the
child's stdout/stderr, so it only ever received an uncaught traceback -- and
`cortexd` configured no `logging` handler at all and silences its HTTP
handler's `log_message`. During the sixth window the inbound poller failed in
bursts for sixteen minutes and the machine recorded nothing.

This module gives the daemon a real handler on the same path:

* rotating at `MAX_BYTES` with `BACKUP_COUNT` predecessors, so the whole log
  is bounded at five files of five MiB;
* opened `O_NOFOLLOW` with mode 0600 and owner-checked, like everything else
  the product writes under its own roots;
* every record THIS HANDLER formats passes through `redaction.redact`, so a
  secret that reaches a log call is bounded and stripped before it reaches
  the disk -- the handler sits on the root logger, so a foreign library's
  record is formatted by it too, rather than falling through `lastResort`
  to fd 2 (which the launcher pointed at the same file, unredacted);
* the process's own stdout/stderr are re-pointed at the live file across a
  rotation -- but only when they already point at the log, which is the
  launcher's case and never a test's;
* every line is stamped in UTC with an explicit `Z` (`2026-09-04T03:27:08Z`),
  whatever the process's zone -- a local stamp without a zone (`22:27:08` on
  the mini meant 03:27Z) made ceremony correlation error-prone.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .redaction import redact

#: Five files of five MiB: `cortexd.log` plus four predecessors.
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 4
#: The handler is attached to the ROOT logger, so every record in the process
#: is formatted (redacted) by it. The product's own tree logs at INFO; the
#: root stays at WARNING so a foreign library's chatter cannot spend the
#: five-file budget.
ROOT_LOGGER = ""
PRODUCT_LOGGER = "cortex_platform"
ROOT_LEVEL = logging.WARNING

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
#: ISO-8601 in UTC with the `Z` designator; `RedactingFormatter.converter`
#: below is what makes the fields UTC, the suffix is what says so.
_DATE = "%Y-%m-%dT%H:%M:%SZ"


class RedactingFormatter(logging.Formatter):
    """The message is redacted AFTER interpolation and BEFORE the disk."""

    # `Formatter.formatTime` feeds `record.created` through this; the default
    # is `time.localtime`, which stamped the mini's zone without naming it.
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        # `getMessage` interpolates `args`. The record is shared with every
        # other handler and is formatted more than once by the rotating
        # handler itself (once to measure, once to emit), so a COPY carries
        # the redacted text and the original is never touched.
        message = redact(record.getMessage(), limit=2000)
        if record.exc_info:
            # A traceback carries file paths and values; keep the type and the
            # message (redacted) and drop the frames.
            exc = record.exc_info[1]
            message += f" [{type(exc).__name__}: {redact(str(exc))}]"
        copy = logging.makeLogRecord(record.__dict__)
        copy.msg = message
        copy.args = ()
        copy.exc_info = None
        copy.exc_text = None
        return super().format(copy)


class _SecureRotatingFileHandler(RotatingFileHandler):
    """`RotatingFileHandler` whose every open refuses symlinks and strangers."""

    def _open(self):  # type: ignore[override]
        path = Path(self.baseFilename)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("daemon log path is not a regular file this user owns")
        except BaseException:
            os.close(descriptor)
            raise
        stream = os.fdopen(descriptor, "a", encoding="utf-8")
        _repoint_standard_streams(path, stream.fileno())
        return stream


def _same_file(fd: int, path: Path) -> bool:
    try:
        left = os.fstat(fd)
        right = os.stat(path)
    except OSError:
        return False
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _repoint_standard_streams(path: Path, target_fd: int) -> None:
    """Keep fds 1 and 2 on the LIVE log across a rotation.

    The launcher opened `cortexd.log` and gave it to the child as stdout and
    stderr; after `doRollover` renames it, those descriptors still name the
    rotated inode and an uncaught traceback would land in `cortexd.log.1`.
    Only descriptors that already point at the log (or at its rotated
    predecessor) are touched: under a test harness they point elsewhere and
    must stay there.
    """

    rotated = path.with_name(path.name + ".1")
    for fd in (1, 2):
        if _same_file(fd, path) or _same_file(fd, rotated):
            try:
                os.dup2(target_fd, fd)
            except OSError:
                continue


def configure_daemon_logging(path: Path) -> logging.Handler:
    """Install the rotating file handler; return it so `main` can remove it.

    Falls back to a stderr handler if the file cannot be opened -- the
    launcher already redirected stderr into the same path, so nothing is lost
    on that route, and a daemon that cannot log must still serve.
    """

    root = logging.getLogger(ROOT_LOGGER)
    product = logging.getLogger(PRODUCT_LOGGER)
    previous = (root.level, product.level)
    root.setLevel(ROOT_LEVEL)
    product.setLevel(logging.INFO)
    formatter = RedactingFormatter(_FORMAT, _DATE)
    handler: logging.Handler
    try:
        handler = _SecureRotatingFileHandler(
            str(path), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        handler.cortex_previous_levels = previous  # type: ignore[attr-defined]
        root.addHandler(handler)
        product.warning(
            "daemon log file unavailable, logging to stderr: %s", type(exc).__name__
        )
        return handler
    handler.setFormatter(formatter)
    handler.cortex_previous_levels = previous  # type: ignore[attr-defined]
    root.addHandler(handler)
    return handler


def release_daemon_logging(handler: logging.Handler) -> None:
    root = logging.getLogger(ROOT_LOGGER)
    root.removeHandler(handler)
    previous = getattr(handler, "cortex_previous_levels", None)
    if previous is not None:
        root.setLevel(previous[0])
        logging.getLogger(PRODUCT_LOGGER).setLevel(previous[1])
    try:
        handler.close()
    except OSError:
        pass
