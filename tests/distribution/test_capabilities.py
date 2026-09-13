from __future__ import annotations

from pathlib import Path

from distribution.capabilities import artifact_capabilities, host_capabilities


def test_native_and_ocr_capabilities_are_explicit(tmp_path: Path) -> None:
    wheels = [
        tmp_path / "sqlite_vec-0.1.9-py3-none-macosx_11_0_arm64.whl",
        tmp_path / "cortex_research-1.0.0-py3-none-any.whl",
    ]
    for wheel in wheels:
        wheel.write_bytes(b"fixture")

    capabilities = artifact_capabilities(wheels, system="Darwin", machine="arm64")
    assert capabilities["sqlite_vec"] is True
    assert capabilities["ocr"] is False
    assert capabilities["hermes_slot"] is False

    mismatched = artifact_capabilities(wheels, system="Linux", machine="x86_64")
    assert mismatched["sqlite_vec"] is False


def test_host_capability_detection_is_read_only_and_fail_closed(
    monkeypatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...]) -> tuple[int, str]:
        calls.append(command)
        return 1, ""

    report = host_capabilities(system="Darwin", bundle=None, run=fake_run)
    assert report["codesigned"] is False
    assert report["notarized"] is False
    assert report["quarantined"] is False
    assert report["ga_ready"] is False
    assert all("--remove" not in part and "--delete" not in part for call in calls for part in call)
