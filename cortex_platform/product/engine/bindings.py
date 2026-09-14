"""D3: the total binding of every ambient input the research engine can read.

The engine's own defaults resolve under `Path.home()` and `~/.local/state`, so
running it inside a daemon that carries a real environment would make every
variable this table missed a silent write to live data. The effect child
therefore receives a **fully replacing** environment built here: one row per
ambient input, one disposition, and nothing inherited.

The table is recomputed against the supported surface, not against the whole
legacy engine: the rows the excluded modules justified were removed with them,
and `RETAINED_WITHOUT_A_READER` names -- with a reason each -- the rows that
outlive their reader on purpose.

Three dispositions, and under a replacing environment two of them behave alike:

* `BOUND`   - the product supplies the value (a `PathRegistry` root, a pinned
              literal, or a `SecretResolver` reveal).
* `DENIED`  - deliberately absent so the engine fails closed on that path.
* `INERT`   - absent, and provably unread by the operations this slice runs.

`DENIED` and `INERT` are both "not in the dict". They differ in what a reader is
being told: a denial is a decision that would still hold if the path became
reachable, an inert row is an observation about the operations P4.2 runs. The
distinction is documentary on purpose -- `bindings_by_disposition()` reports
both, and the conformance test refuses a name that carries neither.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cortex_platform.product.paths import PathRegistry
from cortex_platform.product.secrets import SecretValue

BOUND = "BOUND"
DENIED = "DENIED"
INERT = "INERT"
_DISPOSITIONS = frozenset({BOUND, DENIED, INERT})

# The product's own marker, read by the effect child (never by the engine) so a
# `setsid` grandchild that outlived its parent can still be attributed to the
# effect that spawned it. V3 rules out `killpg`, which cannot reach one.
EFFECT_MARKER_VARIABLE = "CORTEX_EFFECT_MARKER"
PRODUCT_CHILD_VARIABLES: frozenset[str] = frozenset({EFFECT_MARKER_VARIABLE})

# Measured, not assumed: `env -i python -I -c 'print(sorted(os.environ))'` on
# darwin answers with these. libSystem injects them into every process it
# starts, so "fully replacing" is exact for everything the product controls and
# these two arrive underneath it. Named here so a conformance assertion can say
# so rather than quietly widening to "anything unexpected is fine".
PLATFORM_INJECTED_VARIABLES: frozenset[str] = frozenset(
    {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}
)


@dataclass(frozen=True)
class Binding:
    """One ambient input, its disposition, and why it carries that one."""

    name: str
    disposition: str
    source: str | None
    reason: str

    def __post_init__(self) -> None:
        if self.disposition not in _DISPOSITIONS:
            raise ValueError(f"{self.name}: unsupported disposition")
        if (self.disposition == BOUND) != (self.source is not None):
            raise ValueError(f"{self.name}: only a bound input names a source")
        if not self.reason:
            raise ValueError(f"{self.name}: every disposition needs a reason")

    @property
    def secret_alias(self) -> str | None:
        if self.source is None or not self.source.startswith("secret:"):
            return None
        return self.source.partition(":")[2]


@dataclass(frozen=True)
class DynamicSite:
    """One ambient read whose variable name is not a literal at the site.

    Frozen as an enumeration with a reason each, never a wildcard: the whole
    point of the table is that "we could not resolve it" is a statement about a
    named site, not a hole of unknown size.
    """

    module: str
    expression: str
    reason: str


# Every read the AST scan cannot resolve, named with the reason it stays
# unresolved. Deliberately not a wildcard: "we could not resolve it" has to be a
# statement about a listed site, not a hole of unknown size. Modules rather than
# line numbers, so moving code does not turn the assertion red for no reason.
DYNAMIC_ENVIRONMENT_SITES: tuple[DynamicSite, ...] = (
    DynamicSite(
        "cortex_research/paper_ingest.py",
        "name",
        "_env_cred(name): resolves NOVITA_API_KEY, GLM_API_ID and GLM_API_KEY, "
        "which a literal scan alone would have missed entirely.",
    ),
)


# Names reachable only through a dynamic site, so no literal read carries them.
DYNAMIC_ONLY_NAMES: frozenset[str] = frozenset()


# Names the AST scan no longer finds, kept with a stated reason. Everything the
# scan lost when the excluded modules left was removed with them: an `INERT` row
# about an operation that no longer exists is not documentation, it is drift.
# Two reasons survive scanning.
#
# * A process-level input the interpreter or a spawned tool reads rather than the
#   engine's own source (`HOME`, `PATH`, `TMPDIR`, the locale triple), plus the
#   corpus root F4 requires be nameable under both of its names.
# * A standing product decision that must outlive the code that used to read it:
#   the profile switch, every Telegram name, the OCR shim's out-of-tree tools and
#   the product's own backup state root. Removing such a row would leave the
#   decision unrecorded, not merely unenforced.
RETAINED_WITHOUT_A_READER: Mapping[str, str] = {
    "HOME": "AMD-9. `Path.home()` is consulted by code no table enumerates; unbound it answers the operator's home.",
    "PATH": "Read by every subprocess spawn, not by engine source.",
    "TMPDIR": "Read by `tempfile`, not by engine source.",
    "LANG": "Read by the C library and by spawned tools.",
    "LC_ALL": "Read by the C library and by spawned tools.",
    "LC_CTYPE": "Read by the C library; also one of the two names darwin injects underneath a replacing environment.",
    "CORTEX_PAPERS_DIR": "F4. Its last in-package reader left with the non-arXiv ingest, but the corpus must stay nameable under both names or a future reader re-splits it.",
    "HERMES_PROFILE": "F6, and the product has no profiles to switch between.",
    "HERMES_HOME": "Set only inside the worker environment; the engine child is not it.",
    "CORTEX_HERMES_HOOKS_DIR": "The product sources no secret-load.sh, so it cannot honour Hermes hook semantics.",
    "TELEGRAM_BOT_TOKEN": "F10: no engine path may send Telegram.",
    "TELEGRAM_BOT_TOKEN_RESEARCH": "F10: no engine path may send Telegram.",
    "TELEGRAM_HOME_CHANNEL": "F10: no engine path may send Telegram.",
    "CORTEX_RESEARCH_CHAT_ID": "F10: no engine path may send Telegram.",
    "CORTEX_BACKUP_STATE_ROOT": "AMD-9: the product's backup state root is not the engine's business.",
    "CORTEX_VENV_PY": "An out-of-tree interpreter is never the effect child's interpreter.",
    "OCR_GLOBAL_SLOTS": "No in-package reader; denied so a stray value cannot acquire meaning later.",
    "OCR_SLOT_DIR": "Same: the slot semaphore left with `ingest_slots`, and a bound directory nothing reads is an invitation.",
}


ENGINE_BINDINGS: Mapping[str, Binding] = {
    # -- read by the retained engine surface (the AST scan finds all of these) --
    "CORTEX_AGENT_READINGS": Binding("CORTEX_AGENT_READINGS", BOUND, "path:readings_root", "F4: one of the two independent corpus roots."),
    "CORTEX_ARXIV_ABS_BASE": Binding("CORTEX_ARXIV_ABS_BASE", BOUND, "literal:arxiv_abs_base", "The product owns the primary direct metadata endpoint."),
    "CORTEX_ARXIV_API_BASE": Binding("CORTEX_ARXIV_API_BASE", BOUND, "literal:arxiv_api_base", "P4.2 binds the engine's egress: the product owns which arxiv endpoint an effect reaches."),
    "CORTEX_ARXIV_HTML_BASE": Binding("CORTEX_ARXIV_HTML_BASE", BOUND, "literal:arxiv_html_base", "P4.2 binds the engine's egress: the product owns which arxiv endpoint an effect reaches."),
    "CORTEX_ARXIV_MIN_INTERVAL": Binding("CORTEX_ARXIV_MIN_INTERVAL", BOUND, "literal:arxiv_interval", "The engine's own throttle, pinned by the product."),
    "CORTEX_ARXIV_PDF_BASE": Binding("CORTEX_ARXIV_PDF_BASE", BOUND, "literal:arxiv_pdf_base", "P4.2 binds the engine's egress: the product owns which arxiv endpoint an effect reaches."),
    "CORTEX_INGEST_MAX_IMAGES": Binding("CORTEX_INGEST_MAX_IMAGES", BOUND, "literal:ingest_max_images", "Bounded figure download."),
    "CORTEX_OCR_ENGINES": Binding("CORTEX_OCR_ENGINES", BOUND, "literal:ocr_engines", "The product owns the OCR chain order."),
    "CORTEX_OCR_TIMEOUT": Binding("CORTEX_OCR_TIMEOUT", BOUND, "literal:ocr_timeout", "The product owns the OCR wall clock."),
    "CORTEX_PAPER_INGEST_SKILL": Binding("CORTEX_PAPER_INGEST_SKILL", DENIED, None, "The bundle does not ship ~/.claude/skills/paper-ingestion."),
    "CORTEX_RADAR_SKIP_NETWORK": Binding("CORTEX_RADAR_SKIP_NETWORK", DENIED, None, "A skip-network switch may not be settable from ambient environment."),
    "CORTEX_RESEARCH_DB": Binding("CORTEX_RESEARCH_DB", BOUND, "path:research_db", "F5: six modules re-derive this variable, so only the variable is total."),
    "CORTEX_SKIP_EMBED": Binding("CORTEX_SKIP_EMBED", BOUND, "literal:skip_embed", "Whether this installation can reach an embedding provider."),
    "CORTEX_UV_BIN": Binding("CORTEX_UV_BIN", DENIED, None, "The OCR shim's packaged default is an out-of-tree uv the bundle does not ship."),
    "GLM_API_ID": Binding("GLM_API_ID", BOUND, "secret:glm-app-id", "Resolved in cortexd from secret_refs only; absent when the installation configures no reference, which fails the engine closed."),
    "GLM_API_KEY": Binding("GLM_API_KEY", BOUND, "secret:glm", "Resolved in cortexd from secret_refs only; absent when the installation configures no reference, which fails the engine closed."),
    "NOVITA_API_KEY": Binding("NOVITA_API_KEY", BOUND, "secret:novita", "Resolved in cortexd from secret_refs only; absent when the installation configures no reference, which fails the engine closed."),
    "OPENROUTER_API_KEY": Binding("OPENROUTER_API_KEY", BOUND, "secret:openrouter", "Resolved in cortexd from secret_refs only; absent when the installation configures no reference, which fails the engine closed."),
    # -- retained without a reader, per RETAINED_WITHOUT_A_READER above --
    "HOME": Binding("HOME", BOUND, "path:home", "Layer 2: every Path.home()-relative default the table missed lands in product state."),
    "PATH": Binding("PATH", BOUND, "literal:path", "A minimal system PATH; the engine's shim spawns must not find the operator's tools."),
    "TMPDIR": Binding("TMPDIR", BOUND, "path:tmp", "Scratch stays inside product state."),
    "LANG": Binding("LANG", BOUND, "literal:locale", "Pinned; the child runs in UTF-8 mode via -X utf8, not via the locale."),
    "LC_ALL": Binding("LC_ALL", BOUND, "literal:locale", "Pinned; the child runs in UTF-8 mode via -X utf8, not via the locale."),
    "LC_CTYPE": Binding("LC_CTYPE", BOUND, "literal:locale", "Pinned; the child runs in UTF-8 mode via -X utf8, not via the locale."),
    "CORTEX_PAPERS_DIR": Binding("CORTEX_PAPERS_DIR", BOUND, "path:corpus_root", "F4: bind one corpus name and not the other and the corpus splits."),
    "HERMES_PROFILE": Binding("HERMES_PROFILE", DENIED, None, "F6: one variable picks the research DB or the investment cache; the product has no profiles."),
    "HERMES_HOME": Binding("HERMES_HOME", DENIED, None, "Set only inside S3.3's worker environment."),
    "CORTEX_HERMES_HOOKS_DIR": Binding("CORTEX_HERMES_HOOKS_DIR", DENIED, None, "The product sources no secret-load.sh, so it cannot honour Hermes hook semantics."),
    "TELEGRAM_BOT_TOKEN": Binding("TELEGRAM_BOT_TOKEN", DENIED, None, "F10: no engine path may send Telegram."),
    "TELEGRAM_BOT_TOKEN_RESEARCH": Binding("TELEGRAM_BOT_TOKEN_RESEARCH", DENIED, None, "F10: the name twenty in-package callers pass as token_env (telegram.py:167)."),
    "TELEGRAM_HOME_CHANNEL": Binding("TELEGRAM_HOME_CHANNEL", DENIED, None, "F10: no engine path may send Telegram."),
    "CORTEX_RESEARCH_CHAT_ID": Binding("CORTEX_RESEARCH_CHAT_ID", DENIED, None, "F10: no engine path may send Telegram."),
    "CORTEX_BACKUP_STATE_ROOT": Binding("CORTEX_BACKUP_STATE_ROOT", DENIED, None, "AMD-9: the product's backup state root is not the engine's business."),
    "CORTEX_VENV_PY": Binding("CORTEX_VENV_PY", DENIED, None, "An out-of-tree interpreter is never the effect child's interpreter."),
    "OCR_GLOBAL_SLOTS": Binding("OCR_GLOBAL_SLOTS", DENIED, None, "No in-package reader; denied so a stray value cannot acquire meaning later."),
    "OCR_SLOT_DIR": Binding("OCR_SLOT_DIR", DENIED, None, "The slot semaphore left with the excluded ingest_slots module; no retained reader."),
}


# One value per BOUND literal slot, each the engine's own shipped default. The
# Telegram kill switch that used to sit here is gone with the module it
# disabled: the engine has no Telegram sender to switch off any more.
_LITERAL_VALUES: Mapping[str, str] = {
    "path": os.defpath,
    "locale": "C",
    "arxiv_interval": "1.0",
    "arxiv_api_base": "https://export.arxiv.org/api/query",
    "arxiv_abs_base": "https://arxiv.org/abs",
    "arxiv_html_base": "https://arxiv.org/html",
    "arxiv_pdf_base": "https://arxiv.org/pdf",
    "ocr_engines": "deepseek-ocr,glm-ocr",
    "ocr_timeout": "1000",
    "ingest_max_images": "200",
}


class CorpusBindingError(RuntimeError):
    """The two corpus names cannot be made to answer one directory."""


@dataclass(frozen=True)
class EngineRoots:
    """The product-owned filesystem roots one effect child is bound to.

    `corpus_root` is the directory the adoption manifest enumerates, and it is
    whatever name the operator's asset root carries -- S1 registers
    `research-corpus` at `<data_dir>/research/corpus`, so the name is `corpus`.
    `CORTEX_PAPERS_DIR` names that directory directly, but
    `CORTEX_AGENT_READINGS` is one level up because
    `profiles/research/src/cortex_research/index_papers.py:16-33` appends
    `papers` to it, and appending `papers` to the corpus's parent
    answers the corpus only when the corpus is itself called `papers`. Binding
    one and not the other splits the corpus (F4), so `readings_root` is a
    product-owned directory of its own and `prepare()` makes its `papers` child
    resolve to exactly `corpus_root` -- one directory under both names, whatever
    the adopted root is named.
    """

    research_db: Path
    corpus_root: Path
    state: Path
    engine_root: Path
    readings_root: Path

    @classmethod
    def resolve(cls, paths: PathRegistry, *, corpus_root: Path) -> EngineRoots:
        engine_root = paths.data_dir / "research"
        return cls(
            research_db=engine_root / "research.db",
            corpus_root=Path(corpus_root),
            state=paths.state_dir / "research",
            engine_root=engine_root,
            readings_root=engine_root / "agent-readings",
        )

    @property
    def home(self) -> Path:
        return self.state / "home"

    @property
    def tmp(self) -> Path:
        return self.state / "tmp"

    @property
    def write_roots(self) -> tuple[Path, ...]:
        """Every root a child may legitimately write under (D3 layer 3)."""

        return (self.engine_root, self.state, self.corpus_root)

    @property
    def watch_roots(self) -> Mapping[str, Path]:
        """D4(4): the declared subset the before/after digest pair covers.

        Not a live corpus directory outside the sandbox. Digesting one would
        read gigabytes twice per effect on a thirty-second tick, and the child
        cannot reach it by construction anyway -- D4 layer 2 redirects `HOME`,
        so a legacy engine default this table missed, of the documented
        `~/gdrive/...` shape, resolves *here*, under the bound home. The key is
        named after that shape for exactly that reason. Watching this directory
        is what turns layer 2 from a claim into a checked property, and it is
        cheap precisely because the tree should stay empty.

        Roots the effect legitimately writes are deliberately absent: the child
        fails an effect whose watched trees moved, so watching the corpus would
        make every successful ingest an `outcome_unknown`.
        """

        return {"gdrive_under_bound_home": self.home / "gdrive"}

    def path_values(self) -> Mapping[str, Path]:
        """One directory per `path:` binding slot, and nothing else.

        The engine's own output trees (ideas, projects, dossiers, explorations,
        explainers, the q-journal, the radar stubs and the xhs cache) left with
        the operations that wrote them. Creating a directory no shipped code
        writes would make `prepare()` describe a program the product does not
        run.
        """

        return {
            "home": self.home,
            "tmp": self.tmp,
            "state": self.state,
            "engine_root": self.engine_root,
            "research_db": self.research_db,
            "corpus_root": self.corpus_root,
            "readings_root": self.readings_root,
        }

    def prepare(self) -> None:
        """Create the directories the child is bound to, and nothing else.

        `db.connect()` mkdirs its parent on open (F5) and `Path.home()` is
        consulted by code this table did not enumerate, so both roots have to
        exist before the child starts or the first failure is a mkdir into a
        path nobody chose.
        """

        for name, value in self.path_values().items():
            if name == "research_db":
                value.parent.mkdir(parents=True, exist_ok=True)
            else:
                value.mkdir(parents=True, exist_ok=True)
        self.bind_readings_papers()

    def bind_readings_papers(self) -> None:
        """Make `<readings_root>/papers` resolve to exactly `corpus_root`.

        This is the F4 invariant expressed as a fact on disk rather than as
        path arithmetic over a name the product does not choose: `asset_roots`
        carries replacement, identity and delete triggers, so the adopted
        root's name is immutable and the product owns the other side.
        """

        link = self.readings_root / "papers"
        if link.is_symlink():
            if Path(os.readlink(link)) == self.corpus_root:
                return
            link.unlink()
        elif link.exists():
            # Nothing legitimately creates a real directory here -- `prepare()`
            # runs before every child and the link is what the child follows --
            # so this is a corpus split already on disk. Name it rather than
            # write into it.
            raise CorpusBindingError(
                f"{link} is not a link to the adopted corpus {self.corpus_root}"
            )
        link.symlink_to(self.corpus_root, target_is_directory=True)


def bindings_by_disposition() -> Mapping[str, tuple[str, ...]]:
    """Report the table's size by class, for the slice record and `doctor`."""

    grouped: dict[str, list[str]] = {BOUND: [], DENIED: [], INERT: []}
    for name, binding in ENGINE_BINDINGS.items():
        grouped[binding.disposition].append(name)
    return {key: tuple(sorted(value)) for key, value in grouped.items()}


def engine_secret_aliases() -> Mapping[str, str]:
    """Map each configured logical alias onto the variable it lands in."""

    return {
        binding.secret_alias: name
        for name, binding in ENGINE_BINDINGS.items()
        if binding.secret_alias is not None
    }


def research_effect_environment(
    *,
    roots: EngineRoots,
    secrets: Mapping[str, SecretValue] | None = None,
    effect_marker: str,
    skip_embed: bool = False,
    literal_overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the fully replacing environment for one engine effect child.

    Nothing is inherited: a name absent from the result is refused by default,
    which is what makes `DENIED` and `INERT` real rather than aspirational. The
    resolved secrets arrive already revealed from cortexd -- `SecretValue`
    refuses to serialize itself (`secrets.py:71`), so the reveal happens exactly
    here, at the boundary that hands them to the child.
    """

    if not effect_marker or not effect_marker.isascii() or not effect_marker.isalnum():
        raise ValueError("effect_marker must be a non-empty alphanumeric token")
    resolved = dict(secrets or {})
    aliases = engine_secret_aliases()
    unknown = set(resolved) - set(aliases)
    if unknown:
        raise ValueError(f"secrets carry unbound aliases: {sorted(unknown)}")

    paths = roots.path_values()
    literals = dict(_LITERAL_VALUES)
    literals["skip_embed"] = "1" if skip_embed else "0"
    for slot, value in (literal_overrides or {}).items():
        if slot not in literals:
            # An override may only move a slot the table already declares, or
            # the child environment stops being describable by the table.
            raise ValueError(f"unknown literal binding slot: {slot}")
        literals[slot] = value

    environment: dict[str, str] = {EFFECT_MARKER_VARIABLE: effect_marker}
    for name, binding in ENGINE_BINDINGS.items():
        if binding.disposition != BOUND:
            continue
        kind, _, slot = binding.source.partition(":")
        if kind == "path":
            environment[name] = str(paths[slot])
        elif kind == "literal":
            environment[name] = literals[slot]
        elif kind == "secret":
            secret = resolved.get(slot)
            if secret is not None:
                environment[name] = secret.reveal()
        else:  # pragma: no cover - Binding validates the source grammar
            raise ValueError(f"{name}: unsupported binding source")

    # HOME is BOUND and may never be DENIED (AMD-9): without it `Path.home()`
    # falls through to `pwd.getpwuid`, which answers with the operator's real
    # home and reopens the leak Layer 2 exists to close.
    if not environment.get("HOME"):
        raise ValueError("HOME must be bound in every engine effect environment")
    refused = {
        name
        for name, binding in ENGINE_BINDINGS.items()
        if binding.disposition != BOUND and name in environment
    }
    if refused:
        raise ValueError(f"refused inputs leaked into the child: {sorted(refused)}")
    return environment
