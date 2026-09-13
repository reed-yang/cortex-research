"""⟦P7⟧ The supervisor learns `[web]` from the installed generation, not from us."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from distribution.web_settings import (
    EPHEMERAL,
    InstalledWebSettings,
    InstalledWebSettingsError,
    resolve_installed_web_settings,
    validate_web_settings,
)

REPOSITORY = Path(__file__).resolve().parents[2]
PUBLIC_ORIGIN = "https://cortex.example.test"
ACCESS_ISSUER = "https://example-team.cloudflareaccess.com"
ACCESS_AUDIENCE = "ab" * 32


def _runtime_python(tmp_path: Path, body: str) -> Path:
    """A `runtime/bin/python` stand-in that runs `body` for any `-c` request."""

    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    python = runtime / "bin" / "python"
    python.write_text(f"#!{sys.executable}\nimport sys\n{body}\n")
    python.chmod(0o700)
    return runtime


def _real_runtime(tmp_path: Path) -> Path:
    """A stand-in that runs the real interpreter with this checkout importable."""

    return _runtime_python(
        tmp_path,
        "import os\n"
        "arguments = sys.argv[1:]\n"
        "index = arguments.index('-c') + 1\n"
        "arguments[index] = "
        + repr(f"import sys;sys.path.insert(0,{str(REPOSITORY)!r});")
        + " + arguments[index]\n"
        "os.execv(sys.executable, [sys.executable, *arguments])\n",
    )


def test_the_installed_generation_reads_its_own_web_section(tmp_path: Path) -> None:
    runtime = _real_runtime(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(
        "config_version = 1\n\n[web]\n"
        f'"access_audience" = "{ACCESS_AUDIENCE}"\n'
        f'"access_issuer" = "{ACCESS_ISSUER}"\n'
        "\"port\" = 8787\n"
        f'"public_origin" = "{PUBLIC_ORIGIN}"\n'
    )
    settings = resolve_installed_web_settings(
        runtime, config_file=config, home=tmp_path / "home"
    )
    assert settings == InstalledWebSettings(
        port=8787,
        public_origin=PUBLIC_ORIGIN,
        access_issuer=ACCESS_ISSUER,
        access_audience=ACCESS_AUDIENCE,
    )
    assert settings.public_door is True


def test_a_missing_or_sectionless_config_means_the_ephemeral_front_door(
    tmp_path: Path,
) -> None:
    runtime = _real_runtime(tmp_path)
    missing = resolve_installed_web_settings(
        runtime, config_file=tmp_path / "absent.toml", home=tmp_path / "home"
    )
    assert missing == EPHEMERAL
    config = tmp_path / "config.toml"
    config.write_text('config_version = 1\n\n[runtime]\n"model" = "m"\n')
    assert (
        resolve_installed_web_settings(
            runtime, config_file=config, home=tmp_path / "home"
        )
        == EPHEMERAL
    )


def test_a_generation_that_predates_the_section_answers_unsupported(
    tmp_path: Path,
) -> None:
    """The rollback direction: an older generation has no `web_settings`.

    Its interpreter raises ImportError inside the request, which the script
    turns into `supported: false` -- the same front door it always had.
    """

    runtime = _runtime_python(
        tmp_path,
        "import json\n"
        "print(json.dumps({'supported': False}))\n",
    )
    assert (
        resolve_installed_web_settings(
            runtime, config_file=tmp_path / "config.toml", home=tmp_path / "home"
        )
        == EPHEMERAL
    )


def test_a_config_the_installed_generation_refuses_is_a_typed_failure(
    tmp_path: Path,
) -> None:
    runtime = _real_runtime(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text(
        'config_version = 1\n\n[web]\n"public_origin" = "https://cortex.example.test"\n'
    )
    with pytest.raises(
        InstalledWebSettingsError,
        match="configuration refused: web.public_origin requires both",
    ):
        resolve_installed_web_settings(
            runtime, config_file=config, home=tmp_path / "home"
        )


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        json.dumps({"supported": True}),
        json.dumps({"supported": False, "port": 1}),
        json.dumps({"supported": "yes"}),
        json.dumps(
            {
                "supported": True,
                "port": 80,
                "public_origin": None,
                "access_issuer": None,
                "access_audience": None,
            }
        ),
        json.dumps(
            {
                "supported": True,
                "port": None,
                "public_origin": PUBLIC_ORIGIN,
                "access_issuer": None,
                "access_audience": ACCESS_AUDIENCE,
            }
        ),
        json.dumps(
            {
                "supported": True,
                "port": None,
                "public_origin": "http://cortex.example.test",
                "access_issuer": ACCESS_ISSUER,
                "access_audience": ACCESS_AUDIENCE,
            }
        ),
        json.dumps(
            {
                "supported": True,
                "port": None,
                "public_origin": PUBLIC_ORIGIN,
                "access_issuer": "https://evil.example/cloudflareaccess.com",
                "access_audience": ACCESS_AUDIENCE,
            }
        ),
    ],
)
def test_an_installed_answer_that_is_not_the_contract_is_refused(
    tmp_path: Path, answer: str
) -> None:
    runtime = _runtime_python(tmp_path, f"print({answer!r})\n")
    with pytest.raises(InstalledWebSettingsError):
        resolve_installed_web_settings(
            runtime, config_file=tmp_path / "config.toml", home=tmp_path / "home"
        )


def test_the_distribution_revalidates_the_four_public_identifiers() -> None:
    valid = {
        "port": 8787,
        "public_origin": PUBLIC_ORIGIN,
        "access_issuer": "http://127.0.0.1:8123",
        "access_audience": ACCESS_AUDIENCE,
    }
    assert validate_web_settings(valid).access_issuer == "http://127.0.0.1:8123"
    for name, bad in (
        ("port", 65_536),
        ("port", "8787"),
        ("port", True),
        ("access_audience", "AB" * 32),
        ("public_origin", "https://cortex.example.test/"),
        ("public_origin", "https://localhost"),
        ("access_issuer", "https://example-team.cloudflareaccess.com/"),
    ):
        with pytest.raises(InstalledWebSettingsError):
            validate_web_settings({**valid, name: bad})
    with pytest.raises(InstalledWebSettingsError):
        validate_web_settings({**valid, "extra": 1})
