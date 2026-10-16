"""What could break: the callers a change can affect, split by certainty.

Starting from the changed symbols, walk CALLS edges backwards (callers of
callers, up to `depth` hops) and sort every reached function into:

- will break: every edge on its path names the changed code explicitly
  or by a unique name (EXTRACTED / INFERRED);
- might break: some call on its path is AMBIGUOUS. Instead of guessing,
  each entry lists what that call could mean: "X or Y" (and, when the
  receiver's type is unknown, "or a method outside this repository");
- tests: test functions that reach the change (they are reported, not
  walked further: nothing depends on a test).

Deterministic, read-only, no LLM.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from graphora.blast import members_of
from graphora.parser import AMBIGUOUS, extract_symbols_from_diff
from graphora.store import GraphStore

DEFAULT_DEPTH = 2
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Alternative:
    qualname: str
    path: str
    line: int
    is_target: bool  # the definition being changed


@dataclass(frozen=True)
class Breakage:
    caller: dict  # symbol record of the function that may break
    calls: str  # qualified name of the symbol it calls on the path
    call_line: int  # line of that call inside `caller`
    depth: int  # 1 = calls the changed code directly
    via: tuple[str, ...]  # call chain from `calls` down to the changed symbol
    certain: bool
    alternatives: tuple[Alternative, ...] = ()  # for the uncertain call on the path
    may_be_external: bool = False  # receiver type unknown: could be a method outside the repo

    def to_dict(self) -> dict:
        return {
            "name": self.caller["qualname"],
            "kind": self.caller["kind"],
            "path": self.caller["path"],
            "line": self.caller["line"],
            "calls": self.calls,
            "call_line": self.call_line,
            "depth": self.depth,
            "via": list(self.via),
            "certain": self.certain,
            "alternatives": [a.__dict__ for a in self.alternatives],
            "may_be_external": self.may_be_external,
        }


@dataclass(frozen=True)
class WhatCouldBreak:
    targets: list[dict]
    will_break: list[Breakage] = field(default_factory=list)
    might_break: list[Breakage] = field(default_factory=list)
    tests: list[Breakage] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    depth: int = DEFAULT_DEPTH

    def to_dict(self) -> dict:
        return {
            "targets": [_brief(t) for t in self.targets],
            "will_break": [b.to_dict() for b in self.will_break],
            "might_break": [b.to_dict() for b in self.might_break],
            "tests": [b.to_dict() for b in self.tests],
            "unresolved": self.unresolved,
            "depth": self.depth,
        }

    def to_text(self) -> str:
        if not self.targets:
            missing = ", ".join(f"`{n}`" for n in self.unresolved) or "nothing"
            return f"No definition found for {missing} in the code graph."
        lines = [f"What could break if you change {_target_list(self.targets)}"]
        names = [t["name"] for t in self.targets]
        repeated = sorted({n for n in names if names.count(n) > 1})
        if repeated:
            lines.append(
                f"(several definitions are named {', '.join(f'`{n}`' for n in repeated)}; all are included."
                " Pass a qualified name such as `Class.method` to pick one.)"
            )
        lines.append("")
        lines.append(f"Will break ({len(self.will_break)}):")
        lines.extend(_line(b) for b in self.will_break)
        if not self.will_break:
            lines.append("  nothing found")
        lines.append("")
        lines.append(f"Might break ({len(self.might_break)}): the call could mean something else")
        for b in self.might_break:
            lines.append(_line(b))
            options = [f"`{a.qualname}` ({a.path}:{a.line}){'  <- the one you are changing' if a.is_target else ''}"
                       for a in b.alternatives]
            if b.may_be_external:
                options.append("a method outside this repository")
            if options:
                called = b.calls.rsplit(".", 1)[-1]
                lines.append(f"      this `{called}(...)` call is either " + "\n      or ".join(options))
        if not self.might_break:
            lines.append("  nothing found")
        lines.append("")
        lines.append(f"Tests that reach it ({len(self.tests)}):")
        lines.extend(_line(b) for b in self.tests)
        if not self.tests:
            lines.append("  none found: consider adding one")
        if self.unresolved:
            lines.append("")
            lines.append("Not in the graph: " + ", ".join(f"`{n}`" for n in self.unresolved))
        return "\n".join(lines)


def what_could_break(store: GraphStore, names: list[str], depth: int = DEFAULT_DEPTH) -> WhatCouldBreak:
    """What could break if the named symbols change (plain or qualified names)."""
    targets: list[dict] = []
    unresolved: list[str] = []
    for name in names:
        found = store.find_symbols(name)
        if found:
            targets.extend(found)
        else:
            unresolved.append(name)
    return _walk(store, _unique(targets), unresolved, depth)


def what_could_break_for_diff(store: GraphStore, diff: str, depth: int = DEFAULT_DEPTH) -> WhatCouldBreak:
    """What could break for a unified diff: every symbol whose body a hunk touches."""
    targets, unresolved = changed_symbols(store, diff)
    return _walk(store, targets, unresolved, depth)


def changed_symbols(store: GraphStore, diff: str) -> tuple[list[dict], list[str]]:
    """Innermost graph symbol enclosing each changed line of a unified diff.

    Line numbers are taken on the new side of the diff, so the graph should
    be indexed from the changed code. Definitions removed by the diff are
    looked up by name in the same file.
    """
    targets: list[dict] = []
    unresolved: list[str] = []
    for path, lines, removed_text in _diff_changes(diff):
        symbols = store.symbols_in_file(path)
        for line in sorted(lines):
            enclosing = [s for s in symbols if s["line"] <= line <= s["end_line"]]
            if enclosing:
                targets.append(max(enclosing, key=lambda s: (s["line"], -s["end_line"])))
        for name in extract_symbols_from_diff(removed_text):
            found = [s for s in store.find_symbols(name) if s["path"] == path]
            if found:
                targets.extend(found)
            elif not any(s["name"] == name for s in symbols):
                unresolved.append(name)
    return _unique(targets), list(dict.fromkeys(unresolved))


def _walk(store: GraphStore, targets: list[dict], unresolved: list[str], depth: int) -> WhatCouldBreak:
    target_keys = {member["key"] for t in targets for member in members_of(store, t)}
    best: dict[str, Breakage] = {}
    queue: deque[tuple[str, int, tuple[str, ...], bool]] = deque()
    for key in sorted(target_keys):
        record = store.get_symbol(key)
        queue.append((key, 0, (record["qualname"] if record else key,), True))
    expanded: set[tuple[str, bool]] = set()
    while queue:
        key, level, chain, certain = queue.popleft()
        if level >= depth or (key, certain) in expanded:
            continue
        expanded.add((key, certain))
        callee = store.get_symbol(key)
        for edge in store.incoming_calls(key):
            caller = edge["caller"]
            if caller["key"] in target_keys:
                continue
            ambiguous = edge["confidence"] == AMBIGUOUS
            still_certain = certain and not ambiguous
            alternatives: tuple[Alternative, ...] = ()
            if ambiguous:
                called_name = callee["name"] if callee else ""
                alternatives = tuple(
                    Alternative(t["qualname"], t["path"], t["line"], t["key"] in target_keys)
                    for t in store.call_site_targets(caller["key"], edge["line"])
                    if not called_name or t["name"] == called_name  # other calls may share the line
                )
            entry = Breakage(
                caller=caller,
                calls=callee["qualname"] if callee else key,
                call_line=edge["line"],
                depth=level + 1,
                via=chain,
                certain=still_certain,
                alternatives=alternatives,
                may_be_external=ambiguous and edge["reason"] in {"receiver-unknown", "self-inherited"},
            )
            previous = best.get(caller["key"])
            if previous is None or (entry.certain, -entry.depth) > (previous.certain, -previous.depth):
                best[caller["key"]] = entry
            if not caller["is_test"]:
                queue.append((caller["key"], level + 1, (caller["qualname"], *chain), still_certain))
    entries = sorted(best.values(), key=lambda b: (b.depth, b.caller["path"], b.caller["line"]))
    return WhatCouldBreak(
        targets=targets,
        will_break=[b for b in entries if b.certain and not b.caller["is_test"]],
        might_break=[b for b in entries if not b.certain and not b.caller["is_test"]],
        tests=[b for b in entries if b.caller["is_test"]],
        unresolved=unresolved,
        depth=depth,
    )


def _diff_changes(diff: str) -> list[tuple[str, set[int], str]]:
    """(path, changed new-side lines, removed-line text) per file of a unified diff."""
    files: list[tuple[str, set[int], str]] = []
    path = ""
    lines: set[int] = set()
    removed: list[str] = []
    new_line = 0

    def flush() -> None:
        if path:
            files.append((path, set(lines), "".join(removed)))

    header = ""
    for raw in diff.splitlines():
        if raw.startswith("--- "):
            header = raw
            continue
        if raw.startswith("+++ "):
            flush()
            target = raw[4:].strip()
            path = "" if target == "/dev/null" else target.removeprefix("b/")
            lines, removed = set(), []
            if not path:  # deleted file: attribute its removals to the old path
                path = header[4:].strip().removeprefix("a/")
            continue
        match = _HUNK.match(raw)
        if match:
            new_line = int(match.group(3))
            continue
        if not path:
            continue
        if raw.startswith("+"):
            lines.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            lines.add(max(new_line, 1))  # the deletion sits just before this new-side line
            removed.append(f"+++ b/{path}\n{raw}\n")
        elif raw.startswith(" ") or raw == "":
            new_line += 1
    flush()
    return files


def _unique(records: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for record in records:
        seen.setdefault(record["key"], record)
    return sorted(seen.values(), key=lambda r: (r["path"], r["line"], r["key"]))


def _brief(record: dict) -> dict:
    return {
        "name": record["qualname"],
        "kind": record["kind"],
        "path": record["path"],
        "line": record["line"],
        "signature": record["signature"],
    }


def _target_list(targets: list[dict]) -> str:
    shown = [f"`{t['qualname']}` ({t['path']}:{t['line']})" for t in targets[:5]]
    if len(targets) > 5:
        shown.append(f"and {len(targets) - 5} more")
    return ", ".join(shown)


def _line(b: Breakage) -> str:
    where = f"{b.caller['path']}:{b.caller['line']}"
    via = f" ({' -> '.join((b.caller['qualname'], *b.via))})" if b.depth > 1 else ""
    verb = "calls" if b.certain else "may call"
    return f"  - `{b.caller['qualname']}` ({where}) {verb} `{b.calls}` at line {b.call_line}{via}"
