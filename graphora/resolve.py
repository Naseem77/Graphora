"""Call resolution: turn parsed call sites into CALLS edges. Deterministic, no LLM.

Both graph stores persist each file's raw call sites and import bindings,
then hand the whole symbol table to `resolve()` to (re)build every CALLS
edge. Re-resolving globally after each write keeps incremental updates
correct: when a callee's file changes, callers in other files are relinked.

A call is resolved through the strongest evidence available, in order:

1. `self`/`this` receivers -> methods of the enclosing class
2. a definition in the same file (or same Go package)
3. an import binding (`from pkg.mod import f`, `import x from "./m"`)
4. a name match across the repository (last resort)

Edge confidence says which kind of evidence produced it:

- EXTRACTED: steps 1-3. The source names the target explicitly.
- INFERRED: a bare call whose name matches exactly one definition.
- AMBIGUOUS: several definitions match, or the receiver's type is unknown
  (it may also be a method outside the repository). Every candidate is
  linked, and each edge records how many candidates the call site has, so
  consumers can show "this calls X or Y" instead of guessing.

Calls through an import that resolves outside the repository
(`os.path.join`, `fmt.Println`) produce no edge, and a call site with more
than MAX_CANDIDATES candidates is left unlinked rather than fanned out.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field

from graphora.parser import AMBIGUOUS, EXTRACTED, INFERRED, ParsedFile

MAX_CANDIDATES = 8

_FAMILY = {
    "python": "python",
    "javascript": "js",
    "typescript": "js",
    "go": "go",
    "java": "java",
    "rust": "rust",
    "c": "c",
    "cpp": "c",
    "ruby": "ruby",
    "php": "php",
}
# Receivers that mean "the enclosing class".
_SELF_RECEIVERS = {
    "python": {"self", "cls"},
    "js": {"this"},
    "java": {"this"},
    "c": {"this"},
    "rust": {"self", "Self"},
    "ruby": {"self"},
    "php": {"this", "self", "static"},
    "go": set(),
}
# Languages where a bare call `f()` inside a method may call a sibling method.
_IMPLICIT_SELF = {"java", "c", "ruby"}
_CONSTRUCTOR_NAMES = ("__init__", "constructor", "initialize", "__construct")
# Rules that pick candidates by name rather than by what the source names.
_GUESSED = {"name", "receiver-unknown", "self-inherited", "class"}
_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_C_SOURCE_EXTENSIONS = (".c", ".cc", ".cpp", ".cxx")


@dataclass(frozen=True)
class SymbolRecord:
    key: str
    name: str
    qualname: str
    kind: str  # "Function" or "Class"
    path: str
    line: int
    end_line: int
    signature: str
    parent: str
    parent_kind: str
    is_test: bool
    confidence: str = EXTRACTED
    returns: str = ""  # declared return type name

    @property
    def is_method(self) -> bool:
        return self.kind == "Function" and self.parent_kind == "Class"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class CallSite:
    caller_key: str
    callee: str
    receiver: str
    line: int


@dataclass(frozen=True)
class FileRecord:
    path: str
    language: str
    is_test: bool
    bindings: tuple[tuple[str, str, str], ...]  # (local, module, name)
    calls: tuple[CallSite, ...]


@dataclass(frozen=True)
class CallEdge:
    a: str  # caller symbol key
    b: str  # callee symbol key
    line: int
    confidence: str
    candidates: int  # how many targets this call site resolved to
    reason: str  # which rule produced it, e.g. "import", "same-file", "name"


@dataclass
class Resolution:
    edges: list[CallEdge] = field(default_factory=list)
    file_imports: list[tuple[str, str]] = field(default_factory=list)  # (importer path, imported path)
    external: int = 0  # call sites through imports that leave the repository
    unmatched: int = 0  # call sites with no definition anywhere (stdlib, builtins, dynamic)
    too_ambiguous: int = 0  # call sites with more than MAX_CANDIDATES candidates


def symbol_key(path: str, kind: str, qualname: str, occurrence: int = 1) -> str:
    base = f"{path}:{kind}:{qualname}"
    return base if occurrence == 1 else f"{base}#{occurrence}"


def records_for(parsed: ParsedFile) -> tuple[list[SymbolRecord], FileRecord]:
    """Symbol records (with unique keys) and the raw call/import data for one file."""
    seen: dict[tuple[str, str], int] = defaultdict(int)
    symbols: list[SymbolRecord] = []
    for s in parsed.symbols:
        kind = "Function" if s.kind == "Function" else "Class"
        seen[(kind, s.qualname)] += 1
        symbols.append(
            SymbolRecord(
                key=symbol_key(parsed.path, kind, s.qualname, seen[(kind, s.qualname)]),
                name=s.name,
                qualname=s.qualname,
                kind=kind,
                path=parsed.path,
                line=s.line,
                end_line=s.end_line,
                signature=s.signature,
                parent=s.parent,
                parent_kind=s.parent_kind,
                is_test=parsed.is_test,
                confidence=s.confidence,
                returns=s.returns,
            )
        )
    functions_by_qualname: dict[str, list[SymbolRecord]] = defaultdict(list)
    for record in symbols:
        if record.kind == "Function":
            functions_by_qualname[record.qualname].append(record)
    calls = []
    for call in parsed.calls:
        owners = functions_by_qualname.get(call.caller)
        if not owners:
            continue
        owner = next((o for o in owners if o.line <= call.line <= o.end_line), owners[0])
        calls.append(CallSite(owner.key, call.callee, call.receiver, call.line))
    file_record = FileRecord(
        path=parsed.path,
        language=parsed.language,
        is_test=parsed.is_test,
        bindings=tuple((b.local, b.module, b.name) for b in parsed.bindings),
        calls=tuple(calls),
    )
    return symbols, file_record


def resolve(symbols: list[SymbolRecord], files: list[FileRecord]) -> Resolution:
    return _Resolver(symbols, files).run()


class _Resolver:
    def __init__(self, symbols: list[SymbolRecord], files: list[FileRecord]):
        self.symbols = {s.key: s for s in symbols}
        self.files = {f.path: f for f in files}
        self.paths = sorted(self.files)
        self.by_file: dict[str, list[SymbolRecord]] = defaultdict(list)
        self.by_name: dict[tuple[str, str], list[SymbolRecord]] = defaultdict(list)
        self.methods: dict[tuple[str, str], list[SymbolRecord]] = defaultdict(list)  # (class qualname, name)
        self.classes: dict[tuple[str, str], list[SymbolRecord]] = defaultdict(list)  # (family, name)
        for s in sorted(symbols, key=lambda r: (r.path, r.line, r.key)):
            family = self._family(s.path)
            self.by_file[s.path].append(s)
            self.by_name[(family, s.name)].append(s)
            if s.kind == "Class":
                self.classes[(family, s.name)].append(s)
            if s.parent_kind == "Class":
                self.methods[(s.parent, s.name)].append(s)
        self.dir_files: dict[str, list[str]] = defaultdict(list)
        for path in self.paths:
            self.dir_files[posixpath.dirname(path)].append(path)
        self.result = Resolution()

    # --- driver -------------------------------------------------------------

    def run(self) -> Resolution:
        file_imports: set[tuple[str, str]] = set()
        for path in self.paths:
            record = self.files[path]
            for _, module, _ in record.bindings:
                for imported in self._module_files(record, module):
                    if imported != path:
                        file_imports.add((path, imported))
            for call in record.calls:
                caller = self.symbols.get(call.caller_key)
                if caller is None:
                    continue
                targets, confidence, reason = self._resolve_call(record, caller, call)
                targets = [t for t in dict.fromkeys(targets) if t.key != caller.key]
                if not caller.is_test and reason in _GUESSED and any(not t.is_test for t in targets):
                    targets = [t for t in targets if not t.is_test]  # production code never calls test doubles
                if not targets:
                    continue
                if len(targets) > MAX_CANDIDATES:
                    self.result.too_ambiguous += 1
                    continue
                if len(targets) > 1:
                    confidence = AMBIGUOUS
                for target in targets:
                    self.result.edges.append(
                        CallEdge(caller.key, target.key, call.line, confidence, len(targets), reason)
                    )
        self.result.file_imports = sorted(file_imports)
        return self.result

    # --- one call -----------------------------------------------------------

    def _resolve_call(
        self, record: FileRecord, caller: SymbolRecord, call: CallSite
    ) -> tuple[list[SymbolRecord], str, str]:
        family = _FAMILY.get(record.language, record.language)
        receiver = call.receiver

        if receiver in _SELF_RECEIVERS.get(family, set()):
            owner = self._enclosing_class(caller)
            if owner:
                found = self._methods_of(owner, call.callee, record.path)
                if found:
                    return found, EXTRACTED, "self"
            return self._by_name(family, call.callee, methods=True), AMBIGUOUS, "self-inherited"

        if receiver == "":
            return self._resolve_bare(record, family, caller, call.callee)
        if "|" in receiver:  # `x: A | B`: either type's method
            owners = [c for part in receiver.split("|") for c in self._classes_named(part, record.path)]
            found = [m for owner in owners for m in self._methods_of(owner.qualname, call.callee, owner.path)]
            if found:
                return found, EXTRACTED if len(found) == 1 else AMBIGUOUS, "union"
            receiver = "<expr>"
        if receiver.endswith("()"):
            owners = self._result_classes(record, caller, receiver, depth=0)
            found = [m for owner in owners for m in self._methods_of(owner.qualname, call.callee, owner.path)]
            if found:
                return found, EXTRACTED if len(owners) == 1 else AMBIGUOUS, "returns"
            receiver = "<expr>"
        return self._resolve_attribute(record, family, call.callee, receiver)

    def _result_classes(
        self, record: FileRecord, caller: SymbolRecord, receiver: str, depth: int
    ) -> list[SymbolRecord]:
        """Classes a call result is an instance of, from its callee's declared return type.

        `receiver` is "a.b()" or "a.b().c()": resolve the last call, then read
        the return type of what it resolved to (a constructor returns its class).
        """
        if depth > 4:
            return []
        inner = receiver[:-2]
        head, separator, name = max(
            (inner.rpartition(sep) for sep in (".", "::", "->")), key=lambda parts: len(parts[0])
        )
        counters = (self.result.external, self.result.unmatched)
        call = CallSite(caller.key, name if separator else inner, head if separator else "", 0)
        targets, _, _ = self._resolve_call(record, caller, call)
        self.result.external, self.result.unmatched = counters  # a probe, not a call site
        owners: dict[str, SymbolRecord] = {}
        for target in targets[:MAX_CANDIDATES]:
            if target.kind == "Class":
                owners[target.key] = target
            elif target.parent_kind == "Class" and target.name in (*_CONSTRUCTOR_NAMES, target.parent.rsplit(".", 1)[-1]):
                owners.update((c.key, c) for c in self._classes_named(target.parent.rsplit(".", 1)[-1], target.path))
            elif target.returns:
                owners.update((c.key, c) for c in self._classes_named(target.returns, target.path))
        return list(owners.values())

    def _classes_named(self, type_name: str, from_path: str) -> list[SymbolRecord]:
        """Resolve a type name as written in `from_path`: same file, its imports, then the repo."""
        simple = re.split(r"\.|::|\\", type_name)[-1]
        local = [s for s in self.by_file[from_path] if s.kind == "Class" and s.name == simple]
        if local:
            return local
        record = self.files.get(from_path)
        if record is not None:
            for local_name, module, member in record.bindings:
                if local_name == simple:
                    files = self._module_files(record, module)
                    found = [s for s in self._top_level(files, member or simple) if s.kind == "Class"]
                    if found:
                        return found
        return self.classes.get((self._family(from_path), simple), [])

    def _resolve_bare(
        self, record: FileRecord, family: str, caller: SymbolRecord, name: str
    ) -> tuple[list[SymbolRecord], str, str]:
        local = [s for s in self.by_file[record.path] if s.name == name and not s.is_method]
        visible = self._closest_scope([s for s in local if s.kind == "Function"], caller)
        if visible:
            return visible, EXTRACTED, "same-file"
        local_classes = [s for s in local if s.kind == "Class"]
        if local_classes:
            return self._constructors(local_classes), EXTRACTED, "same-file"

        if family in _IMPLICIT_SELF:
            owner = self._enclosing_class(caller)
            if owner:
                found = self._methods_of(owner, name, record.path)
                if found:
                    return found, EXTRACTED, "self"

        for local_name, module, member in record.bindings:
            if local_name != name:
                continue
            files = self._module_files(record, module)
            if not files:
                self.result.external += 1
                return [], EXTRACTED, "external"
            if member == "default":
                found = self._default_export(files)
            else:
                found = self._top_level(files, member or name)
            if not found and member:
                # `from pkg import mod` imports a submodule, not a member.
                found = self._top_level(self._module_files(record, f"{module}.{member}"), name)
            if found:
                return self._constructors(found), EXTRACTED, "import"
            break  # imported from a repo module that re-exports it: fall back to names

        if family == "go":
            package = [
                s
                for path in self.dir_files[posixpath.dirname(record.path)]
                for s in self.by_file[path]
                if s.name == name and not s.is_method and path.endswith(".go")
            ]
            if package:
                return self._constructors(package), EXTRACTED, "package"

        wildcard_files = [
            f for local_name, module, _ in record.bindings if local_name == "*" for f in self._module_files(record, module)
        ]
        if wildcard_files:
            found = self._top_level(wildcard_files, name)
            if found:
                return self._constructors(found), EXTRACTED, "import"

        candidates = self._by_name(family, name, methods=family in _IMPLICIT_SELF, top_level=True)
        if not candidates:
            self.result.unmatched += 1
            return [], INFERRED, "unmatched"
        imported = {target for importer, target in self._imports_of(record) if importer}
        preferred = [c for c in candidates if c.path in imported]
        candidates = preferred or candidates
        return self._constructors(candidates), INFERRED if len(candidates) == 1 else AMBIGUOUS, "name"

    def _resolve_attribute(
        self, record: FileRecord, family: str, name: str, receiver: str
    ) -> tuple[list[SymbolRecord], str, str]:
        separator = "::" if "::" in receiver else ("\\" if "\\" in receiver else ".")
        parts = receiver.split(separator)

        # Receiver is an imported module, or a member of one (a submodule or a class).
        for local_name, module, member in record.bindings:
            if local_name == "*" or not (local_name == receiver or (local_name == parts[0] and len(parts) > 1)):
                continue
            rest = parts[1:] if local_name != receiver else []
            imported = member if member not in ("", "default", "*") else ""
            # `from pkg import util; util.x()` / `import a; a.b.x()`: a module path.
            module_path = ".".join([module, *([imported] if imported else []), *rest])
            files = self._module_files(record, module_path) if (imported or rest) else []
            if files:
                found = self._top_level(files, name)
                if found:
                    return self._constructors(found), EXTRACTED, "import"
                owners = [s for f in files for s in self.by_file[f] if s.kind == "Class" and s.name == parts[-1]]
            else:
                files = self._module_files(record, module)
                if not files:
                    self.result.external += 1
                    return [], EXTRACTED, "external"
                if imported and not rest:
                    # `from x import Klass; Klass.create()`: methods of the imported class.
                    owners = [s for s in self._top_level(files, imported) if s.kind == "Class"]
                else:
                    found = self._top_level(files, name)
                    if found:
                        return self._constructors(found), EXTRACTED, "import"
                    owners = [s for f in files for s in self.by_file[f] if s.kind == "Class" and s.name == parts[-1]]
            found = [m for owner in owners for m in self._methods_of(owner.qualname, name, owner.path)]
            if found:
                return found, EXTRACTED, "import"
            break

        # Receiver names a class: `Foo.create()`, `Foo::new()`.
        class_name = parts[-1]
        local_classes = [s for s in self.by_file[record.path] if s.kind == "Class" and s.name == class_name]
        classes = local_classes or self.classes.get((family, class_name), [])
        found = [m for owner in classes for m in self._methods_of(owner.qualname, name, owner.path)]
        if found:
            confidence = EXTRACTED if local_classes or len(classes) == 1 else AMBIGUOUS
            return found, confidence, "class"

        # Unknown receiver type: any method with that name could be meant,
        # including one outside the repository, so even one match is AMBIGUOUS.
        candidates = self._by_name(family, name, methods=True, members=True)
        if not candidates:
            self.result.unmatched += 1
            return [], AMBIGUOUS, "unmatched"
        return candidates, AMBIGUOUS, "receiver-unknown"

    # --- lookups ------------------------------------------------------------

    def _family(self, path: str) -> str:
        record = self.files.get(path)
        return _FAMILY.get(record.language, record.language) if record else ""

    def _enclosing_class(self, symbol: SymbolRecord) -> str:
        current: SymbolRecord | None = symbol
        hops = 0
        while current is not None and hops < 20:
            if current.parent_kind == "Class":
                return current.parent
            if current.parent_kind != "Function":
                return ""
            current = next(
                (s for s in self.by_file[current.path] if s.qualname == current.parent and s.kind == "Function"),
                None,
            )
            hops += 1
        return ""

    def _methods_of(self, class_qualname: str, name: str, near_path: str) -> list[SymbolRecord]:
        found = self.methods.get((class_qualname, name), [])
        same_file = [m for m in found if m.path == near_path]
        if same_file:
            return same_file
        if found:
            return found
        # Go/Rust methods live outside the type's body (other files, impl blocks).
        simple = class_qualname.rsplit(".", 1)[-1]
        family = self._family(near_path)
        return [
            m for m in self.by_name.get((family, name), []) if m.is_method and m.parent.rsplit(".", 1)[-1] == simple
        ]

    def _constructors(self, targets: list[SymbolRecord]) -> list[SymbolRecord]:
        """Calling a class constructs it: link to its constructor when it has one."""
        out = []
        for target in targets:
            if target.kind != "Class":
                out.append(target)
                continue
            ctor = [
                m
                for ctor_name in (*_CONSTRUCTOR_NAMES, target.name)
                for m in self.methods.get((target.qualname, ctor_name), [])
            ]
            out.extend(ctor[:1] or [target])
        return out

    def _closest_scope(self, functions: list[SymbolRecord], caller: SymbolRecord) -> list[SymbolRecord]:
        """Among same-file functions with one name, prefer the innermost visible one."""
        ancestors = []
        scope = caller.qualname
        while scope:
            ancestors.append(scope)
            scope = scope.rsplit(".", 1)[0] if "." in scope else ""
        ancestors.append("")
        for scope in ancestors:
            hits = [f for f in functions if f.parent == scope]
            if hits:
                return hits[-1:]  # a later redefinition shadows an earlier one
        return []  # only nested in other functions: not visible from here

    def _top_level(self, files: list[str], name: str, depth: int = 0) -> list[SymbolRecord]:
        """Top-level definitions of `name` in `files`, following re-exports
        (`from .impl import name` in a package __init__, `export { name } from`)."""
        found = [s for path in files for s in self.by_file[path] if s.name == name and s.parent_kind in ("", "Namespace")]
        if found or depth >= 4:
            return found
        for path in files:
            record = self.files.get(path)
            if record is None:
                continue
            for local, module, member in record.bindings:
                if local == "*" or (local == name and member not in ("", "*", "default")):
                    original = name if local == "*" else member
                    found.extend(self._top_level(self._module_files(record, module), original, depth + 1))
        return list(dict.fromkeys(found))

    def _default_export(self, files: list[str]) -> list[SymbolRecord]:
        found: list[SymbolRecord] = []
        for path in files:
            names = [name for local, _, name in self.files[path].bindings if local == "default"]
            found.extend(s for s in self.by_file[path] if s.name in names and s.parent_kind == "")
        return found

    def _by_name(
        self, family: str, name: str, methods: bool, top_level: bool = False, members: bool = False
    ) -> list[SymbolRecord]:
        out: list[SymbolRecord] = []
        for s in self.by_name.get((family, name), []):
            if s.is_method and not methods:
                continue
            if top_level and s.parent_kind == "Function":
                continue  # nested helpers are only visible in their own scope
            if not top_level and not s.is_method and not (members and s.parent_kind == "Namespace"):
                continue
            out.append(s)
        return out

    def _imports_of(self, record: FileRecord) -> list[tuple[str, str]]:
        return [
            (record.path, target) for _, module, _ in record.bindings for target in self._module_files(record, module)
        ]

    # --- module specifiers -> repository files ------------------------------

    def _module_files(self, record: FileRecord, module: str) -> list[str]:
        family = _FAMILY.get(record.language, record.language)
        resolver = {
            "python": self._python_module,
            "js": self._js_module,
            "go": self._go_module,
            "java": self._java_module,
            "rust": self._rust_module,
            "c": self._c_module,
            "ruby": self._ruby_module,
            "php": self._php_module,
        }.get(family)
        return resolver(record.path, module) if resolver and module else []

    def _suffix_match(self, *relatives: str) -> list[str]:
        """Files ending in one of `relatives`, nearest the repository root first.

        Absolute imports resolve from a source root, so `falkordb` means
        falkordb/__init__.py, not falkordb/asyncio/falkordb.py; among matches
        only those with the fewest leading directories are kept.
        """
        matches: list[tuple[int, str]] = []
        for relative in relatives:
            relative = relative.lstrip("/")
            for p in self.paths:
                if p == relative or p.endswith("/" + relative):
                    matches.append((p[: len(p) - len(relative)].count("/"), p))
        if not matches:
            return []
        nearest = min(depth for depth, _ in matches)
        return list(dict.fromkeys(p for depth, p in matches if depth == nearest))

    def _exact(self, *candidates: str) -> list[str]:
        return [c for c in dict.fromkeys(posixpath.normpath(c) for c in candidates) if c in self.files]

    def _python_module(self, path: str, module: str) -> list[str]:
        if module.startswith("."):
            dots = len(module) - len(module.lstrip("."))
            base = posixpath.dirname(path)
            for _ in range(dots - 1):
                base = posixpath.dirname(base)
            rest = module[dots:].replace(".", "/")
            target = posixpath.join(base, rest) if rest else base
            return self._exact(f"{target}.py", f"{target}/__init__.py") if rest else self._exact(f"{target}/__init__.py")
        relative = module.replace(".", "/")
        return self._suffix_match(f"{relative}.py", f"{relative}/__init__.py")

    def _js_module(self, path: str, module: str) -> list[str]:
        if module.startswith("."):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), module))
            return self._exact(
                target, *(target + ext for ext in _JS_EXTENSIONS), *(f"{target}/index{ext}" for ext in _JS_EXTENSIONS)
            )[:1]
        for alias in ("@/", "~/", "src/"):
            if module.startswith(alias):
                stem = module[len(alias) :]
                return self._suffix_match(*(stem + ext for ext in _JS_EXTENSIONS), *(f"{stem}/index{ext}" for ext in _JS_EXTENSIONS))[:1]
        return []  # a package from node_modules

    def _go_module(self, path: str, module: str) -> list[str]:
        segments = module.strip("/").split("/")
        best: list[str] = []
        best_score = 0
        for directory, files in self.dir_files.items():
            dir_segments = directory.split("/") if directory else []
            score = 0
            while score < min(len(segments), len(dir_segments)) and segments[-1 - score] == dir_segments[-1 - score]:
                score += 1
            if score and score > best_score:
                best, best_score = [directory], score
            elif score and score == best_score:
                best.append(directory)
        if not best or (best_score == 1 and len(best) > 1):
            return []
        return [f for directory in best for f in self.dir_files[directory] if f.endswith(".go")]

    def _java_module(self, path: str, module: str) -> list[str]:
        relative = module.replace(".", "/")
        exact = self._suffix_match(f"{relative}.java")
        if exact:
            return exact
        return [p for p in self.paths if posixpath.dirname(p).endswith(relative) and p.endswith(".java")]

    def _rust_module(self, path: str, module: str) -> list[str]:
        parts = [p for p in module.split("::") if p not in ("crate", "self", "super", "")]
        if not parts:
            return [path]
        relative = "/".join(parts)
        found = self._suffix_match(f"{relative}.rs", f"{relative}/mod.rs")
        if not found and len(parts) > 1:
            parent = "/".join(parts[:-1])
            found = self._suffix_match(f"{parent}.rs", f"{parent}/mod.rs")
        return found

    def _c_module(self, path: str, module: str) -> list[str]:
        local = self._exact(posixpath.join(posixpath.dirname(path), module))
        headers = local or self._suffix_match(module)
        sources = []
        for header in headers:
            stem = posixpath.splitext(header)[0]
            sources.extend(self._exact(*(stem + ext for ext in _C_SOURCE_EXTENSIONS)))
        return list(dict.fromkeys(headers + sources))

    def _ruby_module(self, path: str, module: str) -> list[str]:
        target = module if module.endswith(".rb") else f"{module}.rb"
        if module.startswith("."):
            return self._exact(posixpath.join(posixpath.dirname(path), target))
        return self._suffix_match(target)

    def _php_module(self, path: str, module: str) -> list[str]:
        parts = [p for p in module.split("\\") if p]
        for start in range(len(parts)):
            found = self._suffix_match("/".join(parts[start:]) + ".php")
            if found and (start < len(parts) - 1 or len(found) == 1):
                return found
        return []
