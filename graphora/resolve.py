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
import sys
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
# Rules where the source itself names the target (an import, the same file, self).
_EXPLICIT = {"import", "same-file", "self"}
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
    attributes: str = ""  # classes: "field=Type;..." for fields whose type is stated

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
    bindings: tuple[tuple[str, str, str, str], ...]  # (local, module, name, scope)
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


_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))
# Methods of each language's built-in containers, strings, and promises/iterators.
_BUILTIN_METHODS: dict[str, frozenset[str]] = {
    "python": frozenset(
        "get items keys values append extend pop popitem update setdefault copy clear insert remove index "
        "count sort reverse add discard union intersection difference join split rsplit splitlines strip "
        "lstrip rstrip replace format startswith endswith lower upper title encode decode read readline "
        "readlines write close find rfind isdigit isalpha".split()
    ),
    "js": frozenset(
        "push pop shift unshift map filter reduce forEach find findIndex some every includes indexOf slice "
        "splice concat join split trim replace replaceAll toString toLowerCase toUpperCase startsWith "
        "endsWith match then catch finally keys values entries get set has delete add clear json text "
        "padStart padEnd at flat flatMap sort reverse fill".split()
    ),
    "java": frozenset(
        "get put add addAll remove contains containsKey size isEmpty equals hashCode toString length append "
        "stream map filter collect forEach iterator next hasNext close getClass valueOf charAt substring "
        "trim split replace startsWith endsWith toLowerCase toUpperCase".split()
    ),
    "rust": frozenset(
        "unwrap expect map clone to_string to_owned as_str iter iter_mut into_iter collect push len is_empty "
        "get get_mut insert remove contains ok err unwrap_or unwrap_or_else and_then as_ref as_mut is_some "
        "is_none trim split join format".split()
    ),
    "ruby": frozenset(
        "each map select reject to_s to_i to_a to_h length size empty? nil? include? push join split strip "
        "keys values first last".split()
    ),
    "go": frozenset("Error String Len".split()),
    "c": frozenset("size push_back emplace_back begin end find insert erase clear empty at c_str".split()),
    "php": frozenset(),
}
# CamelCase: a type, not a variable or an ALL_CAPS constant.
_TYPE_LIKE = re.compile(r"^[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*$")


def _submodule(module: str, *parts: str) -> str:
    """Join a module path and member names: "." + "mod" is ".mod", not "..mod"."""
    if not parts:
        return module
    return module + ".".join(parts) if module.endswith(".") else ".".join([module, *parts])


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
                is_test=parsed.is_test or s.is_test,
                confidence=s.confidence,
                returns=s.returns,
                attributes=s.attributes,
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
        bindings=tuple((b.local, b.module, b.name, b.scope) for b in parsed.bindings),
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
        # (directory, module path) of every go.mod: imports under it are local.
        self.go_modules = sorted(
            (
                (posixpath.dirname(f.path), module)
                for f in files
                if f.language == "gomod"
                for local, module, _, _ in f.bindings
                if local == "<module>"
            ),
            key=lambda item: -len(item[1]),
        )
        # (directory, crate name) of every Cargo.toml.
        self.rust_crates = sorted(
            (
                (posixpath.dirname(f.path), module)
                for f in files
                if f.language == "cargo"
                for local, module, _, _ in f.bindings
                if local == "<crate>"
            ),
            key=lambda item: -len(item[0]),
        )
        self.result = Resolution()

    # --- driver -------------------------------------------------------------

    def run(self) -> Resolution:
        file_imports: set[tuple[str, str]] = set()
        for path in self.paths:
            record = self.files[path]
            for _, module, _, _ in record.bindings:
                for imported in self._module_files(record, module):
                    if imported != path:
                        file_imports.add((path, imported))
            for call in record.calls:
                caller = self.symbols.get(call.caller_key)
                if caller is None:
                    continue
                targets, confidence, reason = self._resolve_call(record, caller, call)
                targets = [t for t in dict.fromkeys(targets) if t.key != caller.key]
                if not caller.is_test and reason not in _EXPLICIT:
                    # Production code never calls into another file's test code unless it imports it.
                    targets = [t for t in targets if not t.is_test or t.path == caller.path]
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
        if "#" in receiver:  # `state.evidence.get()` with `state: AgentState`
            owners = self._field_classes(record, caller, receiver)
            found = [m for owner in owners for m in self._methods_of(owner.qualname, call.callee, owner.path)]
            if found:
                return found, EXTRACTED if len(owners) == 1 else AMBIGUOUS, "field"
            receiver = "<expr>"
        if receiver.endswith("()"):
            owners = self._result_classes(record, caller, receiver, depth=0)
            found = [m for owner in owners for m in self._methods_of(owner.qualname, call.callee, owner.path)]
            if found:
                return found, EXTRACTED if len(owners) == 1 else AMBIGUOUS, "returns"
            receiver = "<expr>"
        return self._resolve_attribute(record, family, caller, call.callee, receiver)

    def _bindings_for(
        self, record: FileRecord, caller: SymbolRecord, local_name: str
    ) -> list[tuple[str, str, str, str]]:
        """Bindings of `local_name` visible from `caller`. The innermost scope wins
        (a function-local import shadows a module one); several bindings in that
        scope (try/except fallbacks) are all returned."""
        visible = [
            b
            for b in record.bindings
            if b[0] == local_name
            and (not b[3] or caller.qualname == b[3] or caller.qualname.startswith(b[3] + "."))
        ]
        if not visible:
            return []
        innermost = max(len(b[3]) for b in visible)
        return [b for b in visible if len(b[3]) == innermost]

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

    def _field_classes(self, record: FileRecord, caller: SymbolRecord, receiver: str) -> list[SymbolRecord]:
        """Classes of `Type#field.subfield` (or `make()#field`): follow each field's declared type."""
        type_name, chain = receiver.split("#", 1)
        if type_name.endswith("()"):
            owners = self._result_classes(record, caller, type_name, depth=0)
        else:
            owners = self._classes_named(type_name, record.path)
        for attribute in chain.split("."):
            following: dict[str, SymbolRecord] = {}
            for owner in owners:
                fields = dict(item.split("=", 1) for item in owner.attributes.split(";") if "=" in item)
                if attribute in fields:
                    following.update((c.key, c) for c in self._classes_named(fields[attribute], owner.path))
            owners = list(following.values())
            if not owners:
                return []
        return owners

    def _classes_named(self, type_name: str, from_path: str) -> list[SymbolRecord]:
        """Resolve a type name as written in `from_path`: same file, its imports, then the repo."""
        simple = re.split(r"\.|::|\\", type_name)[-1]
        local = [s for s in self.by_file[from_path] if s.kind == "Class" and s.name == simple]
        if local:
            return local
        record = self.files.get(from_path)
        if record is not None:
            for local_name, module, member, _ in record.bindings:
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

        bindings = self._bindings_for(record, caller, name)
        if bindings:
            found_all: list[SymbolRecord] = []
            in_repo = False
            for _, module, member, _ in bindings:
                files = self._module_files(record, module)
                if not files:
                    continue
                in_repo = True
                if member == "default":
                    found = self._default_export(files)
                else:
                    found = self._top_level(files, member or name)
                if not found and member:
                    # `from pkg import mod` imports a submodule, not a member.
                    found = self._top_level(self._module_files(record, _submodule(module, member)), name)
                found_all.extend(found)
            if found_all:
                # Several bindings in one scope (try/except fallbacks): each is possible.
                return self._constructors(found_all), EXTRACTED, "import"
            if not in_repo:
                self.result.external += 1
                return [], EXTRACTED, "external"
            # Imported from a repo module that does not define it (dynamic or broken
            # re-export): a same-named function elsewhere is not what the import names.
            self.result.unmatched += 1
            return [], EXTRACTED, "unmatched"

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
            f
            for local_name, module, _, _ in record.bindings
            if local_name == "*"
            for f in self._module_files(record, module)
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
        self, record: FileRecord, family: str, caller: SymbolRecord, name: str, receiver: str
    ) -> tuple[list[SymbolRecord], str, str]:
        separator = "::" if "::" in receiver else ("\\" if "\\" in receiver else ".")
        parts = receiver.split(separator)

        # Receiver is an imported module, or a member of one (a submodule or a class).
        receiver_bindings = self._bindings_for(record, caller, receiver) or (
            self._bindings_for(record, caller, parts[0]) if len(parts) > 1 else []
        )
        for local_name, module, member, _ in receiver_bindings:
            if local_name == "*":
                continue
            rest = parts[1:] if local_name != receiver else []
            imported = member if member not in ("", "default", "*") else ""
            # `from pkg import util; util.x()` / `import a; a.b.x()`: a module path.
            module_path = _submodule(module, *([imported] if imported else []), *rest)
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

        # Receiver names a namespace or object literal: `geo::total()`, `inner::f()`, `api.load()`.
        namespace = ".".join(parts)
        members = [
            s
            for s in self.by_name.get((family, name), [])
            if s.parent_kind == "Namespace" and (s.parent == namespace or s.parent.endswith("." + namespace))
        ]
        if members:
            same_file = [s for s in members if s.path == record.path]
            if same_file:
                members = same_file
            elif family in {"python", "js"}:
                # An object literal in another file is only visible through an import.
                visible = {target for _, target in self._imports_of(record)}
                members = [s for s in members if s.path in visible]
            if members:
                return members, EXTRACTED if len(members) == 1 else AMBIGUOUS, "namespace"

        # A type name that is not defined anywhere in the repository (`String::new()`,
        # `Math.max()`, `Path.home()`): a library type, so not your code.
        if _TYPE_LIKE.match(class_name) and not classes:
            self.result.external += 1
            return [], EXTRACTED, "external"

        # `x.get()`, `items.append()` on an unknown receiver are almost always the
        # language's own containers/strings, not repo methods that share the name.
        if name in _BUILTIN_METHODS.get(family, ()):
            self.result.external += 1
            return [], AMBIGUOUS, "builtin-name"

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
            for local, module, member, _ in record.bindings:
                if local == "*" or (local == name and member not in ("", "*", "default")):
                    original = name if local == "*" else member
                    found.extend(self._top_level(self._module_files(record, module), original, depth + 1))
        return list(dict.fromkeys(found))

    def _default_export(self, files: list[str]) -> list[SymbolRecord]:
        found: list[SymbolRecord] = []
        for path in files:
            names = [name for local, _, name, _ in self.files[path].bindings if local == "default"]
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
            (record.path, target) for _, module, _, _ in record.bindings for target in self._module_files(record, module)
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
        matches = [(prefix.count("/"), p) for prefix, p in self._suffix_candidates(*relatives)]
        if not matches:
            return []
        nearest = min(depth for depth, _ in matches)
        return list(dict.fromkeys(p for depth, p in matches if depth == nearest))

    def _suffix_candidates(self, *relatives: str) -> list[tuple[str, str]]:
        """(leading directories, path) for every file ending in one of `relatives`."""
        out = []
        for relative in relatives:
            relative = relative.lstrip("/")
            for p in self.paths:
                if p == relative or p.endswith("/" + relative):
                    out.append((p[: len(p) - len(relative)], p))
        return out

    def _exact(self, *candidates: str) -> list[str]:
        return [c for c in dict.fromkeys(posixpath.normpath(c) for c in candidates) if c in self.files]

    def _python_module(self, path: str, module: str) -> list[str]:
        if module.startswith("."):
            dots = len(module) - len(module.lstrip("."))
            base = posixpath.dirname(path)
            for _ in range(dots - 1):
                if not base:
                    return []  # `from ....x import y` climbs above the repository
                base = posixpath.dirname(base)
            rest = module[dots:].replace(".", "/")
            target = posixpath.join(base, rest) if rest else base
            init = posixpath.join(target, "__init__.py") if target else "__init__.py"
            return self._exact(f"{target}.py", init) if rest else self._exact(init)
        relative = module.replace(".", "/")
        top = module.split(".")[0]
        matches = []
        for prefix, candidate in self._suffix_candidates(f"{relative}.py", f"{relative}/__init__.py"):
            if prefix and f"{prefix}__init__.py" in self.files:
                continue  # app/handlers/logging.py is app.handlers.logging, not `logging`
            if prefix not in ("", "src/") and top in _STDLIB:
                continue  # `import logging` is the standard library
            matches.append((prefix.count("/"), candidate))
        if not matches:
            return []
        nearest = min(depth for depth, _ in matches)
        return [candidate for depth, candidate in matches if depth == nearest]

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
        if self.go_modules:
            # go.mod says which import paths are local; everything else is external.
            for root, declared in self.go_modules:
                if module == declared or module.startswith(declared + "/"):
                    directory = posixpath.normpath(posixpath.join(root, module[len(declared) :].lstrip("/")))
                    directory = "" if directory == "." else directory
                    return [f for f in self.dir_files.get(directory, []) if f.endswith(".go")]
            return []
        segments = module.strip("/").split("/")
        best: list[str] = []
        best_score = 0
        for directory in self.dir_files:
            dir_segments = directory.split("/") if directory else []
            score = 0
            while score < min(len(segments), len(dir_segments)) and segments[-1 - score] == dir_segments[-1 - score]:
                score += 1
            if score > best_score:
                best, best_score = [directory], score
            elif score and score == best_score:
                best.append(directory)
        # Without go.mod, one matching segment ("encoding/json" vs internal/json) is too weak.
        if best_score < 2:
            return []
        return [f for directory in best for f in self.dir_files[directory] if f.endswith(".go")]

    def _java_module(self, path: str, module: str) -> list[str]:
        relative = module.replace(".", "/")
        exact = self._suffix_match(f"{relative}.java")
        if exact:
            return exact
        return [p for p in self.paths if posixpath.dirname(p).endswith(relative) and p.endswith(".java")]

    def _rust_module(self, path: str, module: str) -> list[str]:
        if self.rust_crates:
            return self._rust_crate_module(path, module)
        parts = [p for p in module.split("::") if p not in ("crate", "self", "super", "")]
        if not parts:
            return [path]
        relative = "/".join(parts)
        found = self._suffix_match(f"{relative}.rs", f"{relative}/mod.rs")
        if not found and len(parts) > 1:
            parent = "/".join(parts[:-1])
            found = self._suffix_match(f"{parent}.rs", f"{parent}/mod.rs")
        return found

    def _rust_crate_module(self, path: str, module: str) -> list[str]:
        """Resolve `crate::a::b`, `my_crate::a::b`, `self::`/`super::` against Cargo crates;
        any other first segment (std, tokio, ...) is an external crate."""
        parts = [p for p in module.split("::") if p]
        if not parts:
            return []
        own = next((root for root, _ in self.rust_crates if not root or path.startswith(root + "/")), None)
        first, rest = parts[0], parts[1:]
        if first == "crate" and own is not None:
            base = posixpath.join(own, "src") if own else "src"
        elif first in {"self", "super"}:
            # Modules live in a directory: `super` of a mod.rs/lib.rs climbs one level,
            # `super` of a plain file module (a.rs) is its own directory.
            base = posixpath.dirname(path)
            if first == "super" and posixpath.basename(path) in {"mod.rs", "lib.rs", "main.rs"}:
                base = posixpath.dirname(base)
        else:
            root = next((r for r, crate in self.rust_crates if crate == first), None)
            if root is None:
                return []  # external crate
            base = posixpath.join(root, "src") if root else "src"
        if not rest:
            return self._exact(posixpath.join(base, "lib.rs"), posixpath.join(base, "main.rs"), path)[:1]
        for depth in (len(rest), len(rest) - 1):
            if depth < 1:
                break
            relative = posixpath.join(base, *rest[:depth])
            found = self._exact(f"{relative}.rs", f"{relative}/mod.rs")
            if found:
                return found
        return self._exact(posixpath.join(base, "lib.rs"), posixpath.join(base, "main.rs"))[:1]

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
