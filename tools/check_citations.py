"""Check the ``<file>.py:<line>`` citations this product's comments carry.

⟦batchM 6⟧ Four review rounds have asked for this. The comments in this
product are its design record, and a record whose citations have drifted is
worse than no record: it sends the next reader to a line that means nothing,
and it does it with a citation's authority. Nothing catches that drift,
because a citation is a comment and a comment compiles. Two rounds later the
same arc produced the proof -- a reader-driven audit corrected two tokens and
missed two more in its own record. This catches it mechanically instead.

WHAT IT CHECKS, and deliberately nothing else:

* the cited path names exactly one file. A repo-relative path, or one
  relative to the citing file's own package, is taken as written; a path more
  than one file in the repository answers to -- the shape a bare `store.py`
  has here, where the product's control store and the eval one share a
  basename -- is itself a finding, not a guess. ⟦batchP ADJ-A1⟧ There is no
  preferred tree: a rule that picked the in-product match reported clean the
  citations most likely to be wrong, and measured every other check in a file
  the writer may never have meant;
* the cited line exists and is not blank;
* when a backticked symbol introduces the token, as in "`sweep` (mod.py:12)"
  or "`sweep` at mod.py:12", that symbol still appears within a few lines of
  the cited one. That pairing is the whole point: a token whose line merely
  shifted still resolves to a real, non-blank line, so only the symbol can
  say the citation stopped being true.

It does NOT judge prose, and it does not edit. ``--fix-report`` prints where a
moved symbol now lives; applying that is a human's decision, because from here
a shifted line number and a renamed function look identical.

    python -m tools.check_citations
    python -m tools.check_citations --fix-report
    python -m tools.check_citations cortex_platform/product/transports
    python -m tools.check_citations --exclude some/vendored/tree

``--exclude`` takes a path prefix -- repo-relative, or absolute inside the
repository -- and leaves that tree alone on both counts a reader can act on:
nothing under it is scanned, and no BARE BASENAME resolves to it, so a
vendored copy cannot make an unrelated citation ambiguous. What it does NOT
do is refuse a citation that names a file under it BY PATH, and that is
deliberate: such a citation is written in a file that IS scanned, by somebody
who can edit it, so checking it is right. The default exclusion is the
vendored Hermes worker payload, whose own citations can only be fixed by a
re-import; ``--no-default-excludes`` scans it anyway. The exclusions in force
are named on the summary line.

Exit code 0 when every citation holds, 1 when any does not, 2 on usage.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]

#: Scanned when the caller names no paths: the product tree is where the
#: design record lives and where the drift the audits found happened.
DEFAULT_PATHS = ("cortex_platform/product",)

#: Excluded unless the caller passes ``--no-default-excludes``. The Hermes
#: worker payload is vendored in byte for byte and re-imported wholesale, so a
#: citation that drifted there cannot be edited here -- only re-imported. A
#: lint that reports what nobody may fix is a lint that gets muted.
DEFAULT_EXCLUDES = ("cortex_platform/product/runtime_update/worker_payload",)

#: Never walked while resolving: caches, virtualenvs, vendored trees and the
#: agent worktrees, none of which a product comment ever cites.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".claude",
        ".cortex-dev",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "output",
        "logs",
        "wheelhouse",
    }
)

#: A citation. The path may be bare (`store.py`), partial (`api/app.py`) or
#: repo-relative; the line may be one line or a range.
_TOKEN = re.compile(
    r"(?P<path>[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*\.py)"
    r":(?P<start>\d+)(?:-(?P<end>\d+))?"
)

#: A symbol as the comments write one: single backticks, no newline inside.
_BACKTICKED = re.compile(r"`([^`\n]+)`")

#: What counts as a symbol worth pairing with a citation. Anything else --
#: a kwarg literal, a sentence in backticks, a filename -- is left to the
#: existence check rather than guessed at.
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:\(\))?$")

#: How much prose may sit between a backticked symbol and the citation that
#: belongs to it. "`sweep` (mod.py:12)" has none and "`sweep` at mod.py:12"
#: has one; "the write at", in "(mod.py:12, the write at mod.py:40)", has
#: three -- and that second token belongs to the write, not to the method
#: named first, which is exactly the pairing this bound refuses to invent.
_MAX_INTERVENING_WORDS = 1

#: How far a paired symbol may have moved from its citation before the
#: citation is stale. Three lines absorbs a decorator or a re-wrapped
#: signature; a real drift is tens or hundreds.
_DEFAULT_WINDOW = 3

_WORD = re.compile(r"[A-Za-z]+")


class CitationError(RuntimeError):
    """The scan could not be performed at all. Never a stale citation."""


@dataclass(frozen=True)
class Finding:
    """One citation that does not hold, and where it was written."""

    file: str
    line: int
    token: str
    reason: str
    suggestion: str | None = None

    def render(self, *, with_suggestion: bool) -> str:
        head = f"{self.file}:{self.line}: {self.token} -> {self.reason}"
        if with_suggestion and self.suggestion:
            return f"{head}\n    {self.suggestion}"
        return head


@dataclass(frozen=True)
class _Resolution:
    """What a cited path resolved to, or every file it could have named.

    Exactly one of the two is populated: a resolution that succeeded names one
    file and no alternatives, and one that failed names none and lists what it
    could not choose between. There is no third state in which the tool picked
    a file AND had another candidate for it -- that state was the guess this
    lint exists to refuse.
    """

    path: str | None
    alternatives: tuple[str, ...]


@dataclass(frozen=True)
class _Region:
    """A comment block or a docstring: prose, with its source line numbers."""

    text: str
    #: Character offset at which each source line starts, parallel to `lines`.
    offsets: tuple[int, ...]
    lines: tuple[int, ...]

    def line_of(self, offset: int) -> int:
        return self.lines[max(0, bisect_right(self.offsets, offset) - 1)]


def _region(lines: Sequence[tuple[int, str]]) -> _Region:
    text_parts: list[str] = []
    offsets: list[int] = []
    numbers: list[int] = []
    cursor = 0
    for number, text in lines:
        offsets.append(cursor)
        numbers.append(number)
        text_parts.append(text)
        cursor += len(text) + 1
    return _Region("\n".join(text_parts), tuple(offsets), tuple(numbers))


def annotated_regions(source: str) -> list[_Region]:
    """Every comment block and docstring in one module, in source order.

    Consecutive comment lines are ONE region because the sentences this
    product writes span them; a citation on the third line is regularly
    introduced by a symbol on the second.
    """

    lines = source.splitlines()
    regions: list[_Region] = []

    comments: list[tuple[int, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                comments.append((token.start[0], token.string))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        raise CitationError(f"cannot tokenize: {exc}") from exc

    block: list[tuple[int, str]] = []
    for number, text in comments:
        if block and number != block[-1][0] + 1:
            regions.append(_region(block))
            block = []
        block.append((number, text))
    if block:
        regions.append(_region(block))

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CitationError(f"cannot parse: {exc}") from exc
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        end = first.end_lineno or first.lineno
        regions.append(
            _region(
                [
                    (number, lines[number - 1])
                    for number in range(first.lineno, end + 1)
                    if number <= len(lines)
                ]
            )
        )
    # Comments are collected before docstrings; sorting here is what makes a
    # report read down the file instead of down the collector.
    regions.sort(key=lambda region: region.lines[0] if region.lines else 0)
    return regions


def paired_symbol(text: str, start: int) -> str | None:
    """The symbol a citation at `start` belongs to, or None if it stands alone.

    "Belongs to" is deliberately narrow: the symbol must be the last backticked
    span before the token with at most `_MAX_INTERVENING_WORDS` of prose
    between them, and no sentence may end in between. Widening it invents
    pairings -- the second token of `(store.py:6138, the write at
    store.py:6208)` belongs to the write, not to the method named first.
    """

    last = None
    for match in _BACKTICKED.finditer(text, 0, start):
        last = match
    if last is None:
        return None
    between = text[last.end() : start]
    if re.search(r"[.!?](\s|$)", between):
        return None
    if len(_WORD.findall(between)) > _MAX_INTERVENING_WORDS:
        return None
    symbol = last.group(1).strip()
    if not _SYMBOL.match(symbol) or symbol.endswith(".py"):
        return None
    symbol = symbol.removesuffix("()")
    return symbol.rsplit(".", 1)[-1] or None


def is_excluded(relative: str, excludes: Sequence[str]) -> bool:
    """Whether a repo-relative path sits under one of the excluded prefixes.

    Whole path components only: `--exclude a/b` covers `a/b` and everything
    under it and never `a/bc`, because a prefix that can cut a name in half is
    a prefix nobody can predict.
    """

    return any(
        relative == prefix or relative.startswith(prefix + "/") for prefix in excludes
    )


class _Corpus:
    """Every Python file the repository can resolve a citation to."""

    def __init__(self, root: Path, excludes: Sequence[str] = ()) -> None:
        self.root = root
        self._excludes = tuple(excludes)
        self._files: list[str] | None = None
        self._lines: dict[str, list[str]] = {}

    def _all(self) -> list[str]:
        if self._files is None:
            found: list[str] = []
            stack = [self.root]
            while stack:
                current = stack.pop()
                for entry in current.iterdir():
                    if entry.is_symlink():
                        continue
                    if entry.is_dir():
                        if entry.name in _SKIP_DIRS or entry.name.startswith(".venv"):
                            continue
                        stack.append(entry)
                    elif entry.suffix == ".py":
                        relative = entry.relative_to(self.root).as_posix()
                        if not is_excluded(relative, self._excludes):
                            found.append(relative)
            self._files = sorted(found)
        return self._files

    def lines(self, relative: str) -> list[str]:
        cached = self._lines.get(relative)
        if cached is None:
            cached = (self.root / relative).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            self._lines[relative] = cached
        return cached

    def resolve(self, cited: str, citing: str) -> _Resolution:
        """Which file a citation names, and what else it could have named.

        Repo-relative first, then relative to the citing file's own package and
        its ancestors -- `runtime/hermes.py` written from inside
        `cortex_platform/` means that package's -- and only then by basename,
        across the repository.

        A basename more than one file in the repository answers to is NOT
        resolved, and there is no tree whose copy wins: a bare `hermes.py`
        here could be the runtime adapter or the product transport, and a tool
        that picks one reports every consequence -- a bad line, a blank line, a
        moved symbol -- measured in a file the writer may never have meant.
        Naming both and making the writer qualify the path is the answer that
        cannot be wrong.
        """

        if (self.root / cited).is_file():
            return _Resolution(cited, ())
        here = (self.root / citing).parent
        while True:
            candidate = here / cited
            if candidate.is_file():
                return _Resolution(candidate.relative_to(self.root).as_posix(), ())
            if here == self.root:
                break
            here = here.parent
        suffix = "/" + cited
        everywhere = [
            path for path in self._all() if path == cited or path.endswith(suffix)
        ]
        if len(everywhere) == 1:
            return _Resolution(everywhere[0], ())
        return _Resolution(None, tuple(everywhere))


def _nearest(lines: Sequence[str], symbol: str, cited: int) -> int | None:
    """The line closest to `cited` on which `symbol` now appears, if any.

    A definition wins a tie with a mere mention at the same distance, because
    that is the line a citation of a symbol means.
    """

    best: tuple[int, int, int] | None = None
    for index, text in enumerate(lines, start=1):
        if symbol not in text:
            continue
        stripped = text.strip()
        defines = stripped.startswith(
            (
                f"def {symbol}",
                f"async def {symbol}",
                f"class {symbol}",
                f"{symbol} =",
                f"{symbol}:",
            )
        )
        rank = (abs(index - cited), 0 if defines else 1, index)
        if best is None or rank < best:
            best = rank
    return None if best is None else best[2]


def check_file(path: Path, corpus: _Corpus, *, window: int) -> list[Finding]:
    """Every citation in one file that does not hold."""

    relative = path.relative_to(corpus.root).as_posix()
    source = path.read_text(encoding="utf-8", errors="replace")
    findings: list[Finding] = []
    for region in annotated_regions(source):
        for match in _TOKEN.finditer(region.text):
            token = match.group(0)
            line = region.line_of(match.start())
            cited = match.group("path")
            found = corpus.resolve(cited, relative)
            resolved = found.path
            if resolved is None:
                others = found.alternatives
                reason = (
                    f"ambiguous: '{cited}' matches {len(others)} files "
                    f"({', '.join(others)})"
                    if others
                    else f"unresolved: no file named '{cited}'"
                )
                findings.append(Finding(relative, line, token, reason))
                continue
            # Every reason below is measured in `resolved`, and reaching here
            # means `resolved` is the only file the path names -- an ambiguous
            # one was reported above. That ordering is the point: a line number
            # judged against a file the tool merely preferred is a diagnosis of
            # the wrong file.
            target = corpus.lines(resolved)
            start = int(match.group("start"))
            end = int(match.group("end") or start)
            if end < start:
                findings.append(
                    Finding(relative, line, token, f"bad range: {end} < {start}")
                )
                continue
            outside = [n for n in (start, end) if n < 1 or n > len(target)]
            if outside:
                findings.append(
                    Finding(
                        relative,
                        line,
                        token,
                        f"out of range: {resolved} has {len(target)} lines",
                    )
                )
                continue
            blank = [n for n in (start, end) if not target[n - 1].strip()]
            if blank:
                findings.append(
                    Finding(
                        relative,
                        line,
                        token,
                        f"blank line: {resolved}:{blank[0]} is empty",
                    )
                )
                continue
            symbol = paired_symbol(region.text, match.start())
            if symbol is None:
                continue
            low = max(1, start - window)
            high = min(len(target), end + window)
            if any(symbol in target[n - 1] for n in range(low, high + 1)):
                continue
            moved = _nearest(target, symbol, start)
            suggestion = (
                f"'{symbol}' now at {resolved}:{moved}"
                if moved is not None
                else f"'{symbol}' no longer appears in {resolved}"
            )
            findings.append(
                Finding(
                    relative,
                    line,
                    token,
                    (
                        f"symbol moved: '{symbol}' is not within "
                        f"{window} lines of {resolved}:{start}"
                    ),
                    suggestion,
                )
            )
    return findings


def iter_sources(
    paths: Iterable[Path], root: Path, excludes: Sequence[str] = ()
) -> Iterator[Path]:
    """The Python files named, directories expanded, in a stable order.

    The skip list is applied to the part of the path INSIDE the repository:
    an agent worktree lives under `.cortex-dev/.claude/`, so matching on the
    absolute path would skip every file in the checkout it is scanning.

    An excluded prefix is honoured even when the caller names a file under it
    outright. "Excluded" means the tool has nothing to say about that tree,
    and a flag that a longer command line can defeat is not that.
    """

    for path in paths:
        if path.is_dir():
            for found in sorted(path.rglob("*.py")):
                relative = found.relative_to(root)
                if any(
                    part in _SKIP_DIRS or part.startswith(".venv")
                    for part in relative.parts
                ):
                    continue
                if is_excluded(relative.as_posix(), excludes):
                    continue
                yield found
        elif path.suffix == ".py":
            if not is_excluded(path.relative_to(root).as_posix(), excludes):
                yield path


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_citations",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", help="files or directories to scan")
    parser.add_argument(
        "--fix-report",
        action="store_true",
        help="for a moved symbol, print the nearest line it now sits on",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="PREFIX",
        help=(
            "a path prefix to leave alone: not scanned, and never the answer "
            "to a bare basename (repeatable; absolute is relativised)"
        ),
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help=f"scan the default exclusions too ({', '.join(DEFAULT_EXCLUDES)})",
    )
    parser.add_argument(
        "--repository",
        default=str(REPOSITORY),
        help="repository root citations resolve against",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=_DEFAULT_WINDOW,
        help="how far a paired symbol may sit from its citation",
    )
    args = parser.parse_args(argv)

    root = Path(args.repository).resolve()
    if not root.is_dir():
        print(f"check_citations: no such repository: {root}", file=sys.stderr)
        return 2
    given = [Path(p) for p in args.paths] or [Path(p) for p in DEFAULT_PATHS]
    # ⟦batchP ADJ-A2⟧ Resolved, so an argument naming the repository through a
    # symlink (/tmp for /private/tmp on macOS) is recognised as inside it, and
    # containment-checked, because everything downstream measures a path
    # relative to `root` and an outside one used to reach `iter_sources` and
    # abort on a ValueError -- a traceback and exit 1, indistinguishable from
    # "stale citations found".
    resolved_paths = [(p if p.is_absolute() else root / p).resolve() for p in given]
    for path in resolved_paths:
        if not path.is_relative_to(root):
            print(
                f"check_citations: path outside repository {root}: {path}",
                file=sys.stderr,
            )
            return 2
        if not path.exists():
            print(f"check_citations: no such path: {path}", file=sys.stderr)
            return 2
    # ⟦batchR A5⟧ An absolute prefix is relativised against the repository
    # rather than carried as written: `--exclude /abs/path/inside` used to be
    # compared against repo-relative paths, match nothing, exclude nothing --
    # and still be printed on the summary line as in force, which is the one
    # thing a scope line may never do. One outside the repository, or naming
    # the repository itself, is a usage error rather than a silent no-op or a
    # scan of nothing.
    given_excludes = (
        () if args.no_default_excludes else DEFAULT_EXCLUDES
    ) + tuple(args.exclude or ())
    excluded: list[str] = []
    for prefix in given_excludes:
        candidate = Path(prefix)
        if candidate.is_absolute():
            inside = candidate.resolve()
            if not inside.is_relative_to(root):
                print(
                    f"check_citations: exclusion outside repository {root}: "
                    f"{prefix}",
                    file=sys.stderr,
                )
                return 2
            if inside == root:
                print(
                    f"check_citations: exclusion names the whole repository: "
                    f"{prefix}",
                    file=sys.stderr,
                )
                return 2
            prefix = inside.relative_to(root).as_posix()
        excluded.append(Path(prefix).as_posix().rstrip("/"))
    excludes = tuple(excluded)
    corpus = _Corpus(root, excludes)

    findings: list[Finding] = []
    scanned = 0
    for source in iter_sources(resolved_paths, root, excludes):
        scanned += 1
        try:
            findings.extend(check_file(source, corpus, window=args.window))
        except CitationError as exc:
            findings.append(
                Finding(
                    source.relative_to(root).as_posix(), 1, source.name, str(exc)
                )
            )
    for finding in findings:
        print(finding.render(with_suggestion=args.fix_report))
    # The exclusions ride on the summary line because a clean run whose scope
    # is invisible is the shape a muted gate takes.
    scope = f", excluding {', '.join(excludes)}" if excludes else ""
    if findings:
        print(
            f"check_citations: {len(findings)} stale citation(s) "
            f"in {scanned} file(s){scope}",
            file=sys.stderr,
        )
        return 1
    # ⟦batchR A4⟧ `0 file(s) clean` and exit 0 is what a gate wired to the
    # wrong path prints, and it is indistinguishable from a tree that holds.
    # Nothing examined is a usage error, not a pass.
    if scanned == 0:
        print(
            f"check_citations: nothing scanned "
            f"(every named path is excluded){scope}",
            file=sys.stderr,
        )
        return 2
    print(f"check_citations: {scanned} file(s) clean{scope}", file=sys.stderr)
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
