"""Authenticated stdio worker bootstrap for the v1 activation probe.

This module used to carry a second implementation of the v2 worker loop
beside the one a release ships. S3.2 moved production onto the shipped copy
(`worker_payload/cortex_worker/serve.py`) and left the product-side twin
alive only under a cross-pin test; S3.3 retires it, because the v2 loop is
about to grow a turn thread, a heartbeat and fd hygiene, and a duplicate
that no production path exercises is the shape this project has already
been bitten by. What remains here is v1: the sandboxed `handle` probe
`cli._probe` runs against a candidate before activation.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import socket
import sys
from pathlib import Path
from typing import Sequence

PROTOCOL_VERSION = 1


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=False)
    return any(resolved == root or root in resolved.parents for root in roots)


def _install_sandbox(candidate: Path, state: Path) -> None:
    roots = tuple(
        path.resolve(strict=False)
        for path in {
            candidate,
            state,
            Path(sys.prefix),
            Path(sys.base_prefix),
            Path(__file__).parent,
        }
    )

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.system"}:
            raise PermissionError("sandbox_denied")
        if event == "open" and args:
            target = args[0]
            if isinstance(target, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(target))
                if not path.is_absolute():
                    path = Path.cwd() / path
                if not _within(path, roots):
                    raise PermissionError("sandbox_denied")
                mode = args[1] if len(args) > 1 else None
                flags = args[2] if len(args) > 2 else 0
                writes = (
                    isinstance(mode, str)
                    and any(marker in mode for marker in "wax+")
                ) or (
                    isinstance(flags, int)
                    and bool(flags & (os.O_WRONLY | os.O_RDWR))
                )
                if writes and not _within(path, (state.resolve(strict=False),)):
                    raise PermissionError("sandbox_denied")
        if event in {
            "os.chmod",
            "os.chown",
            "os.remove",
            "os.rmdir",
            "os.mkdir",
            "os.rename",
            "os.symlink",
            "os.link",
        }:
            for target in args[:2]:
                if isinstance(target, (str, bytes, os.PathLike)):
                    path = Path(os.fsdecode(target))
                    if not path.is_absolute():
                        path = Path.cwd() / path
                    if not _within(path, (state.resolve(strict=False),)):
                        raise PermissionError("sandbox_denied")

    sys.addaudithook(audit)

    original_connect = socket.create_connection

    def denied_connect(*args: object, **kwargs: object) -> None:
        raise PermissionError("sandbox_denied")

    socket.create_connection = denied_connect  # type: ignore[assignment]
    del original_connect


def _safe_entrypoint(name: str) -> str:
    """Accept only what a release manifest can attest: one safe file name.

    The manifest's rule (`ReleaseManifest.from_dict`) is a single component with
    no separator. Re-check it here rather than trusting the argument, so the
    name can never resolve outside the candidate tree.
    """

    if (
        not isinstance(name, str)
        or not name
        or "\0" in name
        or "/" in name
        or os.sep in name
        or (os.altsep is not None and os.altsep in name)
        or name in {".", ".."}
    ):
        raise RuntimeError("worker_entrypoint_invalid")
    return name


def _load_handler(candidate: Path, entrypoint: str):
    module_path = candidate / _safe_entrypoint(entrypoint)
    if module_path.is_symlink() or not module_path.is_file():
        raise RuntimeError("worker_entrypoint_missing")
    spec = importlib.util.spec_from_file_location(
        "cortex_candidate_runtime", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("worker_entrypoint_invalid")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = getattr(module, "handle", None)
    if not callable(handler):
        raise RuntimeError("worker_handler_missing")
    return handler


def _response(request_id: str, *, result: object = None, error: str | None = None) -> bytes:
    value = {
        "protocol": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": error is None,
        "result": result if error is None else None,
        "error": error,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def serve(candidate: Path, state: Path, entrypoint: str) -> int:
    token = os.environ.pop("CORTEX_WORKER_TOKEN", "")
    if len(token) < 32:
        return 2
    candidate = candidate.resolve(strict=True)
    state = state.resolve(strict=True)
    _install_sandbox(candidate, state)
    try:
        handler = _load_handler(candidate, entrypoint)
    except Exception:
        return 3
    for line in sys.stdin.buffer:
        request_id = "invalid"
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict) or set(raw) != {
                "protocol", "request_id", "token", "method", "params"
            }:
                raise ValueError("invalid_request")
            request_id = raw["request_id"]
            if (
                raw["protocol"] != PROTOCOL_VERSION
                or not isinstance(request_id, str)
                or raw["token"] != token
                or not isinstance(raw["method"], str)
                or not isinstance(raw["params"], dict)
            ):
                raise ValueError("invalid_request")
            if raw["method"] == "__hello__":
                result: object = {"protocol": PROTOCOL_VERSION}
            elif raw["method"] == "__shutdown__":
                sys.stdout.buffer.write(_response(request_id, result={"stopping": True}))
                sys.stdout.buffer.flush()
                return 0
            else:
                result = handler(raw["method"], raw["params"])
            payload = _response(request_id, result=result)
        except PermissionError:
            payload = _response(request_id, error="sandbox_denied")
        except (TypeError, ValueError):
            payload = _response(request_id, error="protocol_error")
        except Exception:
            payload = _response(request_id, error="runtime_error")
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path, nargs="?")
    parser.add_argument("state", type=Path, nargs="?")
    parser.add_argument("--worker-entrypoint")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.candidate is None or args.state is None or not args.worker_entrypoint:
        return 2
    return serve(args.candidate, args.state, args.worker_entrypoint)


if __name__ == "__main__":
    raise SystemExit(main())
