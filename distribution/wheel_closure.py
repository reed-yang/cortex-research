"""Version, marker, tag, and closure primitives for proving a shipped closure.

Standard library only: this module travels inside a bundle's `tools/` and is
imported by the verifier, so it may not depend on anything the bundle does not
carry.

Every parser here refuses what it cannot fully decide rather than guessing. A
guess in a version comparison or a marker evaluation would silently drop a
required dependency or admit an unrelated one, and the closure proof would then
pass on a set that does not match what the product will actually import.
"""

from __future__ import annotations

import email.parser
import email.policy
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

TARGET_ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.14.6",
    "os_name": "posix",
    "platform_machine": "arm64",
    "platform_python_implementation": "CPython",
    "platform_system": "Darwin",
    "python_full_version": "3.14.6",
    "python_version": "3.14",
    "sys_platform": "darwin",
}
# Knowable at build time and version-valued, respectively. `extra` is a variable
# whose value is deliberately unset, not a member of the environment.
_MARKER_VARIABLES = frozenset(TARGET_ENVIRONMENT) | {"extra"}
_VERSION_VARIABLES = frozenset(
    {"implementation_version", "python_full_version", "python_version"}
)
_REFUSED_VARIABLES = frozenset({"platform_release", "platform_version"})

MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_WHEEL_MEMBERS = 50_000
MAX_REQUIREMENTS_BYTES = 1024 * 1024
MAX_MARKER_BYTES = 4096
MAX_MARKER_TOKENS = 192
MAX_MARKER_DEPTH = 24


class WheelClosureError(RuntimeError):
    """A wheel, marker, version, or closure could not be decided."""


# ---------------------------------------------------------------------------
# Distribution names
# ---------------------------------------------------------------------------

_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def normalize_name(value: str) -> str:
    """Return the PEP 503 normalized form of a distribution name."""

    text = str(value).strip()
    if not _NAME.fullmatch(text):
        raise WheelClosureError(f"distribution name is unsupported: {text!r}")
    return re.sub(r"[-_.]+", "-", text).lower()


# ---------------------------------------------------------------------------
# PEP 440 versions
# ---------------------------------------------------------------------------


class _Boundary:
    """A comparable sentinel, so a version key never mixes types."""

    def __init__(self, name: str, greater: bool) -> None:
        self._name = name
        self._greater = greater

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return self._name

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return hash((self._name, self._greater))

    def __lt__(self, other: object) -> bool:
        return not self._greater and self is not other

    def __le__(self, other: object) -> bool:
        return self is other or self.__lt__(other)

    def __gt__(self, other: object) -> bool:
        return self._greater and self is not other

    def __ge__(self, other: object) -> bool:
        return self is other or self.__gt__(other)


_ABOVE_ALL = _Boundary("above-all", True)
_BELOW_ALL = _Boundary("below-all", False)

_VERSION = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_label>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_number>[0-9]+)?)?
    (?:-(?P<post_implicit>[0-9]+)
       |[-_.]?(?P<post_label>post|rev|r)[-_.]?(?P<post_number>[0-9]+)?)?
    (?:(?P<dev_segment>[-_.]?dev[-_.]?(?P<dev_number>[0-9]+)?))?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)
_PRE_LABELS = {"alpha": "a", "beta": "b", "c": "rc", "pre": "rc", "preview": "rc"}


@dataclass(frozen=True)
class Version:
    """A parsed PEP 440 version and its total-ordering key."""

    text: str
    epoch: int
    release: tuple[int, ...]
    pre: tuple[str, int] | None
    post: int | None
    dev: int | None
    local: tuple[object, ...] | None

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    def key(self, *, with_local: bool = True) -> tuple[object, ...]:
        trimmed = list(self.release)
        while trimmed and trimmed[-1] == 0:
            trimmed.pop()
        if self.pre is None and self.post is None and self.dev is not None:
            pre: object = _BELOW_ALL
        elif self.pre is None:
            pre = _ABOVE_ALL
        else:
            pre = self.pre
        post: object = _BELOW_ALL if self.post is None else self.post
        dev: object = _ABOVE_ALL if self.dev is None else self.dev
        if not with_local:
            return (self.epoch, tuple(trimmed), pre, post, dev)
        if self.local is None:
            local: object = _BELOW_ALL
        else:
            local = self.local
        return (self.epoch, tuple(trimmed), pre, post, dev, local)


def parse_version(value: str) -> Version:
    """Parse one PEP 440 version, refusing anything outside the grammar."""

    text = str(value).strip()
    match = _VERSION.fullmatch(text)
    if match is None:
        raise WheelClosureError(f"version is unsupported: {text!r}")
    release = tuple(int(part) for part in match.group("release").split("."))
    pre_label = match.group("pre_label")
    pre = None
    if pre_label is not None:
        label = pre_label.lower()
        pre = (_PRE_LABELS.get(label, label), int(match.group("pre_number") or 0))
    post = None
    if match.group("post_implicit") is not None:
        post = int(match.group("post_implicit"))
    elif match.group("post_label") is not None:
        post = int(match.group("post_number") or 0)
    # The dev segment is its own named group, so its presence is decided by
    # whether it actually matched — not by a substring search, which would
    # also fire on an unrelated local-version segment such as `1.0+abcdev`.
    dev = None
    if match.group("dev_segment") is not None:
        dev = int(match.group("dev_number") or 0)
    local_text = match.group("local")
    local = None
    if local_text is not None:
        local = tuple(
            (int(part), "") if part.isdigit() else (_BELOW_ALL, part.lower())
            for part in re.split(r"[-_.]", local_text)
        )
    return Version(
        text=text,
        epoch=int(match.group("epoch") or 0),
        release=release,
        pre=pre,
        post=post,
        dev=dev,
        local=local,
    )


def compare(left: str, right: str) -> int:
    """Return -1, 0, or 1 for two PEP 440 versions."""

    first = parse_version(left).key()
    second = parse_version(right).key()
    if first == second:
        return 0
    return -1 if first < second else 1


_SPECIFIER = re.compile(r"^(===|==|!=|~=|<=|>=|<|>)\s*(.+)$")


def _release_prefix_matches(candidate: Version, target: Version) -> bool:
    length = len(target.release)
    padded = candidate.release + (0,) * max(0, length - len(candidate.release))
    return candidate.epoch == target.epoch and padded[:length] == target.release


def _satisfies_one(candidate: Version, operator: str, value: str) -> bool:
    if operator == "===":
        return candidate.text.strip() == value.strip()
    wildcard = value.endswith(".*")
    if wildcard and operator not in {"==", "!="}:
        raise WheelClosureError(f"specifier is unsupported: {operator}{value}")
    target = parse_version(value.removesuffix(".*") if wildcard else value)
    if wildcard and (
        target.pre is not None
        or target.post is not None
        or target.dev is not None
        or target.local is not None
    ):
        # PEP 440 admits `.*` on a release segment only. Truncating a qualified
        # version to its release prefix would silently accept an unrelated one.
        raise WheelClosureError(f"specifier is unsupported: {operator}{value}")
    if operator in {"==", "!="}:
        if wildcard:
            matched = _release_prefix_matches(candidate, target)
        else:
            matched = candidate.key(with_local=target.local is not None) == target.key(
                with_local=target.local is not None
            )
        return matched if operator == "==" else not matched
    if operator == "~=":
        if len(target.release) < 2 or target.local is not None:
            raise WheelClosureError(f"specifier is unsupported: {operator}{value}")
        floor = target.key(with_local=False) <= candidate.key(with_local=False)
        ceiling = _release_prefix_matches(
            candidate, Version(value, target.epoch, target.release[:-1], None, None, None, None)
        )
        return floor and ceiling
    if target.local is not None:
        # PEP 440 forbids a local version here. Silently discarding it made
        # `satisfies` contradict `compare`'s own total order.
        raise WheelClosureError(f"specifier is unsupported: {operator}{value}")
    # Ordered comparisons ignore the candidate's local version.
    left = candidate.key(with_local=False)
    right = target.key(with_local=False)
    if operator == "<=":
        return left <= right
    if operator == ">=":
        return left >= right
    if operator == "<":
        # An exclusive lower bound must not admit a pre-release of the bound
        # itself unless the bound is already a pre-release.
        if (
            candidate.pre is not None
            and target.pre is None
            and candidate.release == target.release
            and candidate.epoch == target.epoch
        ):
            return False
        return left < right
    if operator == ">":
        if (
            candidate.post is not None
            and target.post is None
            and candidate.release == target.release
            and candidate.epoch == target.epoch
        ):
            return False
        return left > right
    raise WheelClosureError(f"specifier is unsupported: {operator}{value}")


def satisfies(version: str, specifier: str) -> bool:
    """Decide whether one version satisfies a comma-separated specifier set."""

    text = str(specifier).strip()
    candidate = parse_version(version)
    if not text:
        return not candidate.is_prerelease
    clauses: list[tuple[str, str]] = []
    for part in text.split(","):
        piece = part.strip()
        match = _SPECIFIER.fullmatch(piece)
        if match is None:
            raise WheelClosureError(f"specifier is unsupported: {piece!r}")
        clauses.append((match.group(1), match.group(2).strip()))
    if candidate.is_prerelease and not any(
        operator in {"==", "!=", "==="}
        or parse_version(value.removesuffix(".*")).is_prerelease
        for operator, value in clauses
    ):
        # Matching pip: an ordered specifier set does not admit a pre-release
        # unless it mentions one. Being strict here fails the build closed
        # rather than shipping a version the resolver would not have chosen.
        return False
    return all(_satisfies_one(candidate, operator, value) for operator, value in clauses)


# ---------------------------------------------------------------------------
# PEP 508 markers
# ---------------------------------------------------------------------------

_MARKER_TOKEN = re.compile(
    r"""
    \s*(?:
        (?P<open>\() | (?P<close>\)) |
        (?P<operator>===|==|!=|<=|>=|~=|<|>) |
        (?P<string>'[^']*'|"[^"]*") |
        (?P<word>[A-Za-z_][A-Za-z0-9_.]*)
    )
    """,
    re.VERBOSE,
)
_UNSET = object()


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


def _tokenize_marker(text: str) -> list[_Token]:
    if len(text.encode("utf-8")) > MAX_MARKER_BYTES:
        raise WheelClosureError("requirement marker is oversized")
    tokens: list[_Token] = []
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        match = _MARKER_TOKEN.match(text, position)
        if match is None:
            raise WheelClosureError(f"requirement marker is unsupported: {text!r}")
        position = match.end()
        for kind in ("open", "close", "operator", "string", "word"):
            value = match.group(kind)
            if value is None:
                continue
            if kind == "word" and value in {"and", "or", "in", "not"}:
                tokens.append(_Token(value, value))
            else:
                tokens.append(_Token(kind, value))
            break
        if len(tokens) > MAX_MARKER_TOKENS:
            raise WheelClosureError("requirement marker is oversized")
    return tokens


class _MarkerParser:
    def __init__(self, tokens: list[_Token], text: str) -> None:
        self._tokens = tokens
        self._text = text
        self._index = 0
        self._depth = 0

    def _peek(self) -> _Token | None:
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _next(self) -> _Token:
        token = self._peek()
        if token is None:
            raise WheelClosureError(f"requirement marker is unsupported: {self._text!r}")
        self._index += 1
        return token

    def parse(self) -> object:
        node = self._or_expression()
        if self._peek() is not None:
            raise WheelClosureError(f"requirement marker is unsupported: {self._text!r}")
        return node

    def _or_expression(self) -> object:
        node = self._and_expression()
        while (token := self._peek()) is not None and token.kind == "or":
            self._next()
            node = ("or", node, self._and_expression())
        return node

    def _and_expression(self) -> object:
        node = self._term()
        while (token := self._peek()) is not None and token.kind == "and":
            self._next()
            node = ("and", node, self._term())
        return node

    def _term(self) -> object:
        token = self._peek()
        if token is not None and token.kind == "open":
            self._depth += 1
            if self._depth > MAX_MARKER_DEPTH:
                raise WheelClosureError("requirement marker nests too deeply")
            self._next()
            node = self._or_expression()
            self._depth -= 1
            closing = self._next()
            if closing.kind != "close":
                raise WheelClosureError(f"requirement marker is unsupported: {self._text!r}")
            return node
        return self._comparison()

    def _operand(self) -> tuple[str, str]:
        token = self._next()
        if token.kind == "string":
            return ("literal", token.value[1:-1])
        if token.kind != "word":
            raise WheelClosureError(f"requirement marker is unsupported: {self._text!r}")
        name = token.value
        if name in _REFUSED_VARIABLES:
            raise WheelClosureError(f"unsupported requirement marker variable: {name}")
        if name not in _MARKER_VARIABLES:
            raise WheelClosureError(f"unsupported requirement marker variable: {name}")
        return ("variable", name)

    def _comparison(self) -> object:
        left = self._operand()
        token = self._next()
        if token.kind == "operator":
            operator = token.value
        elif token.kind == "in":
            operator = "in"
        elif token.kind == "not":
            following = self._next()
            if following.kind != "in":
                raise WheelClosureError(f"requirement marker is unsupported: {self._text!r}")
            operator = "not in"
        else:
            raise WheelClosureError(f"requirement marker is unsupported: {self._text!r}")
        right = self._operand()
        return ("compare", operator, left, right)


def parse_marker(text: str) -> object:
    """Parse one PEP 508 marker into an evaluable tree, refusing the rest."""

    stripped = str(text).strip()
    if not stripped:
        raise WheelClosureError("requirement marker is empty")
    return _MarkerParser(_tokenize_marker(stripped), stripped).parse()


def _resolve(operand: tuple[str, str], environment: Mapping[str, str]) -> object:
    kind, value = operand
    if kind == "literal":
        return value
    if value == "extra":
        # Unset unless the walk has bound it. The base pass leaves it unset, so
        # `extra == "x"` is false and only unconditional dependencies apply; a
        # pass entered through `B[x]` binds it, so B's `extra == "x"` clauses
        # become true. Both are needed: the same distribution is traversed once
        # per requested extra plus once for its base requirements.
        return environment.get("extra", _UNSET)
    if value not in environment:
        raise WheelClosureError(f"unsupported requirement marker variable: {value}")
    return environment[value]


def _compare_operands(
    operator: str,
    left: object,
    right: object,
    *,
    versioned: bool,
) -> bool:
    if left is _UNSET or right is _UNSET:
        # `extra` is unset, so an equality against it is false and an inequality
        # is true. Any other operator cannot be decided and is refused.
        if operator == "==":
            return False
        if operator == "!=":
            return True
        raise WheelClosureError(f"requirement marker cannot decide {operator!r} against extra")
    if operator == "in":
        return str(left) in str(right)
    if operator == "not in":
        return str(left) not in str(right)
    if versioned:
        # No fallback: swallowing this refusal would replace a decision the
        # module declined to make with an unprincipled string ordering.
        return _satisfies_one(parse_version(str(left)), operator, str(right))
    if operator == "===":
        return str(left) == str(right)
    if operator == "==":
        return str(left) == str(right)
    if operator == "!=":
        return str(left) != str(right)
    if operator == "<":
        return str(left) < str(right)
    if operator == "<=":
        return str(left) <= str(right)
    if operator == ">":
        return str(left) > str(right)
    if operator == ">=":
        return str(left) >= str(right)
    raise WheelClosureError(f"requirement marker operator is unsupported: {operator}")


def _evaluate(node: object, environment: Mapping[str, str]) -> bool:
    if not isinstance(node, tuple):
        raise WheelClosureError("requirement marker is unsupported")
    if node[0] == "and":
        return _evaluate(node[1], environment) and _evaluate(node[2], environment)
    if node[0] == "or":
        return _evaluate(node[1], environment) or _evaluate(node[2], environment)
    _kind, operator, left, right = node
    versioned = (left[0] == "variable" and left[1] in _VERSION_VARIABLES) or (
        right[0] == "variable" and right[1] in _VERSION_VARIABLES
    )
    return _compare_operands(
        operator,
        _resolve(left, environment),
        _resolve(right, environment),
        versioned=versioned and operator not in {"in", "not in"},
    )


def evaluate_marker(text: str, environment: Mapping[str, str] | None = None) -> bool:
    """Evaluate one marker against the frozen target environment."""

    return _evaluate(parse_marker(text), environment or TARGET_ENVIRONMENT)


# ---------------------------------------------------------------------------
# Wheel metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WheelMetadata:
    name: str
    version: str
    requires_python: str
    requires_dist: tuple[str, ...]


def read_wheel_metadata(path: Path) -> WheelMetadata:
    """Read exactly one `*.dist-info/METADATA` from a wheel, bounded."""

    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_WHEEL_MEMBERS:
                raise WheelClosureError("wheel member count is unsafe")
            candidates = [
                member
                for member in members
                if not member.is_dir()
                and member.filename.count("/") == 1
                and member.filename.endswith(".dist-info/METADATA")
            ]
            if len(candidates) != 1:
                raise WheelClosureError("wheel does not carry exactly one METADATA")
            member = candidates[0]
            if member.file_size > MAX_METADATA_BYTES:
                raise WheelClosureError("wheel METADATA is oversized")
            payload = archive.read(member)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise WheelClosureError("wheel metadata is unreadable") from exc
    try:
        message = email.parser.BytesParser(policy=email.policy.compat32).parsebytes(payload)
    except Exception as exc:  # noqa: BLE001 - email raises a wide surface
        raise WheelClosureError("wheel metadata is unreadable") from exc
    name = message.get("Name")
    version = message.get("Version")
    if name is None or version is None:
        raise WheelClosureError("wheel metadata is missing its name or version")
    return WheelMetadata(
        name=normalize_name(str(name)),
        version=str(version).strip(),
        requires_python=str(message.get("Requires-Python", "")).strip(),
        requires_dist=tuple(str(value).strip() for value in message.get_all("Requires-Dist", [])),
    )


_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<extras>\[[^\]]*\])?\s*(?P<rest>.*)$"
)
# An extras list is a handful of names in every real distribution; the bound
# exists for the same reason the marker grammar has one — an allowlist that
# admits an unbounded structure has not bounded anything.
MAX_REQUIREMENT_EXTRAS = 32


def split_requirement(value: str) -> tuple[str, tuple[str, ...], str, str]:
    """Split one `Requires-Dist` into name, extras, specifier, and marker.

    Extras are returned rather than refused. They change which of the target's
    own dependencies apply — `B[client]` pulls in B's requirements gated on
    `extra == "client"` — so a closure proof that dropped them would be proving
    something other than what pip installs. Refusing them instead was equally
    wrong in practice: the real closure contains `fastmcp-slim[client,server]`,
    so the refusal made every real bundle unverifiable.
    """

    text = str(value).strip()
    body, _, marker = text.partition(";")
    match = _REQUIREMENT.fullmatch(body.strip())
    if match is None:
        raise WheelClosureError(f"requirement is unsupported: {text!r}")
    extras: tuple[str, ...] = ()
    group = match.group("extras")
    if group:
        requested = [item.strip() for item in group[1:-1].split(",") if item.strip()]
        if len(requested) > MAX_REQUIREMENT_EXTRAS:
            raise WheelClosureError(f"requirement declares too many extras: {text!r}")
        # PEP 685: an extra is normalized exactly as a distribution name is, so
        # `Extra-Name` and `extra_name` must not select different dependencies.
        extras = tuple(sorted({normalize_name(item) for item in requested}))
    specifier = match.group("rest").strip()
    # PEP 508's grammar is `versionspec = ('(' version_many ')') | version_many`,
    # and the parenthesised form is what Metadata 2.1 emitted. Five distributions
    # in the real closure still ship it — `pexpect: ptyprocess (>=0.5)`,
    # `rich: pygments (>=2.13.0,<3.0.0)` — so rejecting it rejected them.
    if specifier.startswith("("):
        if not specifier.endswith(")"):
            raise WheelClosureError(f"requirement is unsupported: {text!r}")
        specifier = specifier[1:-1].strip()
    if "(" in specifier or ")" in specifier:
        raise WheelClosureError(f"requirement is unsupported: {text!r}")
    return (
        normalize_name(match.group("name")),
        extras,
        specifier,
        marker.strip(),
    )


# ---------------------------------------------------------------------------
# Wheel tags
# ---------------------------------------------------------------------------

_CPYTHON_TAG = re.compile(r"^cp3(?P<minor>[0-9]+)$")
# The architecture segment is deliberately not restricted to a fixed
# alternation here: which architectures are acceptable is a property of the
# *target* being verified against (see `target` below), not of the tag
# grammar. An architecture token that is well-formed but wrong for the target
# (e.g. `x86_64` when the target is `arm64`) is a `False` decision, not a
# parse failure — only a genuinely malformed tag element is refused, and that
# refusal already happens in `wheel_tags_compatible` before this is reached.
_MACOS_TAG = re.compile(r"^macosx_(?P<major>[0-9]+)_(?P<minor>[0-9]+)_(?P<arch>[A-Za-z0-9]+)$")
MAX_SUPPORTED_MACOS_MAJOR = 26


def _python_tag_compatible(python_tag: str, abi_tag: str, *, major: int, minor: int) -> bool:
    exact = f"cp{major}{minor}"
    if abi_tag == "none":
        return python_tag in {"py3", exact}
    if abi_tag == "abi3":
        match = _CPYTHON_TAG.fullmatch(python_tag)
        return match is not None and int(match.group("minor")) <= minor
    if abi_tag == exact:
        return python_tag == exact
    return False


def _platform_tag_compatible(platform_tag: str, *, machine: str) -> bool:
    if platform_tag == "any":
        return True
    match = _MACOS_TAG.fullmatch(platform_tag)
    if match is None or int(match.group("major")) > MAX_SUPPORTED_MACOS_MAJOR:
        return False
    return match.group("arch") in {machine, "universal2"}


def wheel_tags_compatible(
    python_tag: str,
    abi_tag: str,
    platform_tag: str,
    *,
    major: int,
    minor: int,
    target: Mapping[str, str] | None = None,
) -> bool:
    """Decide whether any tag combination fits the embedded interpreter.

    An `abi_tag` of `none` does not imply a platform-independent wheel — the
    real closure ships a 74 MB `py3-none-macosx_11_0_arm64` wheel — so the
    platform tag is always decided separately.

    `target` supplies the machine architecture a `macosx_*` platform tag must
    match (`target["platform_machine"]`, alongside the always-accepted
    `universal2`); it defaults to `TARGET_ENVIRONMENT`, which is what every
    caller in this codebase currently resolves against, so omitting it is
    exactly today's arm64-only behaviour.
    """

    python_tags = str(python_tag).split(".")
    abi_tags = str(abi_tag).split(".")
    platform_tags = str(platform_tag).split(".")
    if not all(re.fullmatch(r"[A-Za-z0-9_]+", item) for item in python_tags + abi_tags):
        raise WheelClosureError("artifact tag is unsupported")
    if not all(re.fullmatch(r"[A-Za-z0-9_]+", item) for item in platform_tags):
        raise WheelClosureError("artifact tag is unsupported")
    machine = str((target or TARGET_ENVIRONMENT).get("platform_machine", "arm64"))
    # A freethreaded ABI tag such as `cp314t` parses perfectly well; it is
    # simply not this interpreter's ABI, so it is decided false below rather
    # than treated as an unreadable tag.
    for candidate_platform in platform_tags:
        if not _platform_tag_compatible(candidate_platform, machine=machine):
            continue
        for candidate_abi in abi_tags:
            for candidate_python in python_tags:
                if _python_tag_compatible(
                    candidate_python, candidate_abi, major=major, minor=minor
                ):
                    return True
    return False


# ---------------------------------------------------------------------------
# Closure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureEntry:
    name: str
    version: str
    requires_python: str
    requires_dist: tuple[str, ...]


@dataclass(frozen=True)
class ClosureProof:
    reachable: frozenset[str]
    unreachable: tuple[str, ...]
    missing: tuple[tuple[str, str], ...]
    unsatisfied: tuple[tuple[str, str, str], ...]
    forbidden: tuple[str, ...] = ()


def prove_closure(
    entries: Iterable[ClosureEntry],
    roots: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    forbidden: Iterable[str] = (),
) -> ClosureProof:
    """Prove the entry set is exactly the transitive closure of `roots`.

    Reachability is computed forwards from the roots, so a distribution that is
    only referenced by another unreachable distribution stays unreachable — a
    subgraph of smuggled wheels cannot vouch for itself.

    `forbidden` names a set of distribution names (any spelling; normalized
    internally) that must not appear in the entry set at all, whether or not
    they are reachable — a forbidden distribution that is also unreachable
    would otherwise be reported only as `unreachable`, which does not name the
    license/policy reason it must never ship. Checking the whole catalogue
    rather than only the reachable set also catches a forbidden distribution
    that the roots genuinely require, which `unreachable` cannot see at all.
    """

    catalogue: dict[str, ClosureEntry] = {}
    for entry in entries:
        name = normalize_name(entry.name)
        if name in catalogue:
            raise WheelClosureError(f"duplicate distribution in the closure: {name}")
        catalogue[name] = entry
    normalized_roots = [normalize_name(root) for root in roots]
    for root in normalized_roots:
        if root not in catalogue:
            raise WheelClosureError(f"closure root is missing: {root}")
    forbidden_names = {normalize_name(name) for name in forbidden}
    base = dict(environment or TARGET_ENVIRONMENT)
    reachable: set[str] = set()
    missing: set[tuple[str, str]] = set()
    unsatisfied: set[tuple[str, str, str]] = set()
    # The frontier walks (distribution, extra) pairs, not bare names. A
    # distribution reached through `B[client]` exposes a different dependency
    # set than the same distribution reached plainly, so visiting it once by
    # name would silently drop whichever set was seen second.
    visited: set[tuple[str, str]] = set()
    frontier: list[tuple[str, str]] = [(root, "") for root in normalized_roots]
    while frontier:
        current, extra = frontier.pop()
        if (current, extra) in visited:
            continue
        visited.add((current, extra))
        reachable.add(current)
        entry = catalogue[current]
        environment_for_pass = base if not extra else {**base, "extra": extra}
        for requirement in entry.requires_dist:
            name, extras, specifier, marker = split_requirement(requirement)
            if marker and not evaluate_marker(marker, environment_for_pass):
                continue
            target = catalogue.get(name)
            if target is None:
                missing.add((current, name))
                continue
            if specifier and not satisfies(target.version, specifier):
                unsatisfied.add((current, name, specifier))
            frontier.append((name, ""))
            frontier.extend((name, requested) for requested in extras)
    return ClosureProof(
        reachable=frozenset(reachable),
        unreachable=tuple(sorted(set(catalogue) - reachable)),
        missing=tuple(sorted(missing)),
        unsatisfied=tuple(sorted(unsatisfied)),
        forbidden=tuple(sorted(name for name in catalogue if name in forbidden_names)),
    )


def parse_requirements_hashes(path: Path) -> dict[str, set[str]]:
    """Map each exported requirement's distribution to its pinned digests."""

    if path.stat().st_size > MAX_REQUIREMENTS_BYTES:
        raise WheelClosureError("requirements file is oversized")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WheelClosureError("requirements file is unreadable") from exc
    logical: list[str] = []
    buffer = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            buffer += stripped[:-1].strip() + " "
            continue
        logical.append((buffer + stripped).strip())
        buffer = ""
    if buffer.strip():
        logical.append(buffer.strip())
    hashes: dict[str, set[str]] = {}
    for entry in logical:
        body, *rest = entry.split("--hash=")
        requirement = body.split(";")[0].strip()
        match = re.fullmatch(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s]+)", requirement)
        if match is None:
            raise WheelClosureError(f"requirement line is unsupported: {entry[:80]!r}")
        name = normalize_name(match.group("name"))
        digests = set()
        for value in rest:
            algorithm, _, digest = value.strip().partition(":")
            if algorithm != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", digest.strip()):
                raise WheelClosureError(f"requirement hash is unsupported: {value.strip()[:80]!r}")
            digests.add(digest.strip())
        if not digests:
            raise WheelClosureError(f"requirement line carries no hash: {name}")
        if name in hashes:
            # A shipped requirements.txt is expected to carry exactly one line
            # per surviving distribution (the marker-driven filter, e.g. the
            # two `scipy` lines under different `python_full_version` markers,
            # collapses to one before the file is ever bundled). Merging two
            # lines' hash sets here would silently accept a wheel matching
            # *either* line's digests, weakening the binding §4.4 clause 6
            # depends on, so a second line for the same name is refused
            # rather than unioned.
            raise WheelClosureError(f"requirements file lists a distribution twice: {name}")
        hashes[name] = digests
    return hashes
