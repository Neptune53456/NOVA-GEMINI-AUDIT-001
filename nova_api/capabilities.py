"""Deterministic capabilities and their authoritative execution registry."""
from __future__ import annotations

import hashlib, json, subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Literal
from uuid import uuid4

from .computer import ComputerController, ComputerError
from .journal import EventJournal
from .visual import VisualController
from .application_memory import ApplicationMemory
from .transactions import TransactionError, TransactionStore
from .workspace import Workspace, WorkspacePathError

RiskLevel = Literal["low", "medium", "high"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_READ_BYTES, MAX_LIST_ENTRIES = 1_000_000, 500
ANTI_SPIN_THRESHOLD, ANTI_SPIN_WINDOW = 3, 50


@dataclass(frozen=True)
class Capability:
    id: str; description: str; risk_level: RiskLevel; reversible: bool
    requires_confirmation: bool; expected_effect: str; category: str
    execute: Callable[..., dict[str, Any]]; verify: Callable[[dict[str, Any]], bool]
    argument_schema: dict[str, Any] | None = None
    verification_profile: Literal["native", "exact_readback", "observation"] = "native"

    def public_metadata(self) -> dict[str, Any]:
        value = {key: getattr(self, key) for key in ("id", "description", "risk_level", "reversible", "requires_confirmation", "expected_effect", "category")}
        value["argument_schema"] = self.argument_schema or {"type": "object", "properties": {}, "additionalProperties": False}
        return value


@dataclass(frozen=True)
class CapabilityResult:
    action_id: str; capability_id: str; status: Literal["success", "error"]
    verified: bool; duration_ms: int; result: dict[str, Any] | None = None
    error_category: str | None = None


class CapabilityNotFound(KeyError): pass
class ConfirmationRequired(PermissionError): pass


class CapabilityRegistry:
    def __init__(self, journal: EventJournal, *, transactions: TransactionStore | None = None,
                 risk_context_resolver: Callable[[str, dict[str, Any]], dict[str, object]] | None = None,
                 application_memory: ApplicationMemory | None = None) -> None:
        self._journal, self.transactions = journal, transactions
        self._risk_context_resolver = risk_context_resolver
        self.application_memory = application_memory
        self._capabilities: dict[str, Capability] = {}
        self._recent: deque[tuple[str, str]] = deque(maxlen=ANTI_SPIN_WINDOW)

    def register(self, capability: Capability) -> None:
        if capability.id in self._capabilities: raise ValueError(f"duplicate capability: {capability.id}")
        self._capabilities[capability.id] = capability

    def lookup(self, capability_id: str) -> Capability:
        try: return self._capabilities[capability_id]
        except KeyError as error: raise CapabilityNotFound(capability_id) from error

    def available(self) -> list[dict[str, Any]]:
        return [self._capabilities[key].public_metadata() for key in sorted(self._capabilities)]

    def risk_context(self, capability_id: str, arguments: dict[str, Any] | None = None) -> dict[str, object]:
        if self._risk_context_resolver is None:
            return {}
        try:
            return dict(self._risk_context_resolver(capability_id, arguments or {}))
        except Exception:
            # Risk metadata is advisory-only. Failure must never lower the static policy.
            return {"ambiguous": True} if capability_id.startswith("computer.") else {}

    def validate_arguments(self, capability_id: str, arguments: dict[str, Any]) -> None:
        """Validate model-produced arguments without producing side effects."""
        self.lookup(capability_id)
        allowed = {"git.status": set(), "project.basic_info": set(), "filesystem.list": {"path"},
                   "filesystem.read": {"path"}, "filesystem.write": {"path", "content"},
                   "computer.observe": set(), "computer.windows": set(), "computer.active_window": set(),
                   "computer.window.focus": {"window_ref"}, "computer.window.minimize": {"window_ref"},
                   "computer.window.restore": {"window_ref"},
                   "computer.ui.inspect": {"window_ref", "depth", "max_elements"},
                   "computer.ui.elements": {"window_ref", "depth", "max_elements"},
                   "computer.ui.invoke": {"element_ref"}, "computer.ui.focus": {"element_ref"},
                   "computer.ui.toggle": {"element_ref"}, "computer.ui.select": {"element_ref"},
                   "computer.ui.set_value": {"element_ref", "value"},
                   "computer.visual.displays": set(),
                   "computer.visual.capture": {"target_type", "display_ref", "window_ref"},
                   "computer.visual.inspect": {"image_ref"},
                   "computer.visual.ocr": {"image_ref"},
                   "computer.visual.analyze": {"image_ref", "prompt"},
                   "computer.perception.ground": {"window_ref", "query", "image_ref", "max_elements"}}.get(capability_id, set())
        if set(arguments) - allowed: raise ValueError("invalid_arguments")
        if capability_id.startswith("computer.window.") and not isinstance(arguments.get("window_ref"), str):
            raise ValueError("invalid_arguments")
        if capability_id in {"computer.ui.inspect", "computer.ui.elements"}:
            if not isinstance(arguments.get("window_ref"), str): raise ValueError("invalid_arguments")
        if capability_id.startswith("computer.ui.") and capability_id not in {"computer.ui.inspect", "computer.ui.elements"}:
            if not isinstance(arguments.get("element_ref"), str): raise ValueError("invalid_arguments")
        if capability_id == "computer.ui.set_value" and not isinstance(arguments.get("value"), str):
            raise ValueError("invalid_arguments")
        if capability_id == "computer.visual.capture":
            target_type = arguments.get("target_type")
            expected = {
                "window": {"target_type", "window_ref"},
                "display": {"target_type", "display_ref"},
                "primary_display": {"target_type"},
            }.get(target_type)
            if expected is None or set(arguments) != expected or any(
                    not isinstance(arguments[key], str) for key in expected - {"target_type"}):
                raise ValueError("invalid_arguments")
        if capability_id in {"computer.visual.inspect", "computer.visual.ocr", "computer.visual.analyze"}:
            if not isinstance(arguments.get("image_ref"), str): raise ValueError("invalid_arguments")
        if capability_id == "computer.visual.analyze" and "prompt" in arguments and not isinstance(arguments["prompt"], str):
            raise ValueError("invalid_arguments")
        if capability_id == "computer.perception.ground":
            if not isinstance(arguments.get("window_ref"), str) or not isinstance(arguments.get("query"), str):
                raise ValueError("invalid_arguments")
            if "image_ref" in arguments and not isinstance(arguments.get("image_ref"), str):
                raise ValueError("invalid_arguments")
            if "max_elements" in arguments and (not isinstance(arguments.get("max_elements"), int) or isinstance(arguments.get("max_elements"), bool)):
                raise ValueError("invalid_arguments")
        if capability_id in {"filesystem.read", "filesystem.write"} and not isinstance(arguments.get("path"), str):
            raise ValueError("invalid_arguments")
        if capability_id == "filesystem.write":
            content = arguments.get("content")
            if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_READ_BYTES: raise ValueError("invalid_arguments")
            if self.transactions is None: raise ValueError("transactions_unavailable")
            path = self.transactions.workspace.resolve(arguments["path"])
            if path.exists() and not path.is_file(): raise ValueError("not_a_file")

    def execute(self, capability_id: str, arguments: dict[str, Any] | None = None, *, confirmed: bool = False) -> CapabilityResult:
        capability, args = self.lookup(capability_id), arguments or {}
        self.validate_arguments(capability_id, args)
        if capability.requires_confirmation and not confirmed: raise ConfirmationRequired(capability_id)
        fingerprint = hashlib.sha256(json.dumps([capability_id, args], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        if sum(fp == fingerprint and outcome != "success" for fp, outcome in self._recent) >= ANTI_SPIN_THRESHOLD:
            return self._error_result(capability_id, "repeated_action", fingerprint)
        action_id, started = uuid4().hex, perf_counter()
        computer = capability.category == "computer"
        self._journal.append("computer.action.started" if computer else "action.started", action_id=action_id,
                             capability_id=capability_id, status="running")
        try:
            observed = capability.execute(args) if args else capability.execute()
            verified, duration_ms = capability.verify(observed), int((perf_counter() - started) * 1000)
            unverifiable = observed.get("verification_status") == "unverifiable"
            if not verified and not unverifiable:
                self._recent.append((fingerprint, "verification_failed"))
                category = str(observed.get("error_category") or "verification_failed")
                self._journal.append("computer.action.verification_failed" if computer else "action.error",
                                     action_id=action_id, capability_id=capability_id, status="error",
                                     duration_ms=duration_ms, error_category=category)
                return CapabilityResult(action_id, capability_id, "error", False, duration_ms,
                                        result=observed, error_category=category)
            self._recent.append((fingerprint, "success"))
            event_type = "visual.observed" if capability_id.startswith("computer.visual.") else ("computer.observed" if capability_id in {
                "computer.observe", "computer.windows", "computer.active_window"
            } else (
                "computer.action.completed" if computer else "action.completed"))
            structural = None
            if capability_id == "computer.perception.ground":
                structural = {
                    "provenance": str(observed.get("provenance") or "")[:40],
                    "source_types": [str(item)[:24] for item in (observed.get("source_types") or [])[:5]],
                    "confidence": observed.get("confidence"),
                    "ambiguity_score": observed.get("ambiguity_score"),
                    "vision_used": bool(observed.get("vision_used")),
                    "ocr_used": bool(observed.get("ocr_used")),
                }
            elif capability_id == "computer.visual.ocr":
                structural = {
                    "provenance": "LOCAL_OCR",
                    "available": bool(observed.get("available")),
                    "span_count": int(observed.get("span_count") or 0),
                    "backend": str(observed.get("backend") or "")[:40],
                }
            self._journal.append(event_type, action_id=action_id, transaction_id=observed.get("transaction_id"),
                                 capability_id=capability_id, status="success", duration_ms=duration_ms,
                                 structural_fingerprint=structural)
            return CapabilityResult(action_id, capability_id, "success", not unverifiable, duration_ms, result=observed)
        except (OSError, UnicodeError, WorkspacePathError, TransactionError, RuntimeError) as error:
            allowed = {"invalid_path", "path_outside_workspace", "not_a_file", "not_a_directory", "file_too_large", "verification_failed"}
            category = str(error) if isinstance(error, ComputerError) or str(error) in allowed else "execution_failed"
            duration_ms = int((perf_counter() - started) * 1000)
            self._recent.append((fingerprint, category))
            self._journal.append("computer.action.failed" if computer else "action.error", action_id=action_id,
                                 capability_id=capability_id, status="error", duration_ms=duration_ms,
                                 error_category=category)
            return CapabilityResult(action_id, capability_id, "error", False, duration_ms, error_category=category)

    def _error_result(self, capability_id: str, category: str, fingerprint: str) -> CapabilityResult:
        action_id = uuid4().hex
        self._recent.append((fingerprint, category))
        event_type = "computer.action.failed" if self.lookup(capability_id).category == "computer" else "action.error"
        self._journal.append(event_type, action_id=action_id, capability_id=capability_id,
                             status="error", duration_ms=0, error_category=category)
        return CapabilityResult(action_id, capability_id, "error", False, 0, error_category=category)


def _git_status(root: Path) -> dict[str, Any]:
    completed = subprocess.run(["git", "status", "--porcelain=v1", "--branch"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2.0, check=False, shell=False)
    if completed.returncode != 0: raise RuntimeError("git_status_failed")
    lines = completed.stdout.splitlines(); branch = lines[0][3:].split("...", 1)[0].strip() if lines and lines[0].startswith("## ") else None
    entries = lines[1:] if branch is not None else lines
    return {"return_code": 0, "branch": branch, "clean": not entries, "modified_count": sum(not line.startswith("??") for line in entries), "untracked_count": sum(line.startswith("??") for line in entries)}


def _project_basic_info(root: Path) -> dict[str, Any]:
    return {"name": root.name, "is_directory": root.is_dir(), "git_repository": (root / ".git").is_dir(), "python_project": (root / "pyproject.toml").is_file(), "node_project": (root / "package.json").is_file() or (root / "frontend" / "package.json").is_file()}


def build_default_registry(journal: EventJournal, *, project_root: Path = PROJECT_ROOT,
                           computer: ComputerController | None = None,
                           visual: VisualController | None = None) -> CapabilityRegistry:
    workspace = Workspace(project_root); transactions = TransactionStore(workspace, journal)
    desktop = computer or ComputerController()
    if visual is None:
        app_memory = ApplicationMemory(project_root / ".runtime" / "nova_application_memory.sqlite3")
        visual = VisualController(desktop, application_memory=app_memory)
    else:
        app_memory = visual.application_memory
    registry = CapabilityRegistry(journal, transactions=transactions,
                                  risk_context_resolver=desktop.risk_context,
                                  application_memory=app_memory)
    empty_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    list_schema = {"type": "object", "properties": {"path": {"type": "string"}},
                   "additionalProperties": False}
    path_schema = {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"], "additionalProperties": False}
    write_schema = {"type": "object", "properties": {
        "path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"], "additionalProperties": False}
    registry.register(Capability("git.status", "Inspecte l'état Git synthétique du projet.", "low", True, False, "Retourne un état Git parsé sans modifier le dépôt.", "project", lambda: _git_status(workspace.root), lambda value: value.get("return_code") == 0 and isinstance(value.get("clean"), bool), empty_schema, "observation"))
    registry.register(Capability("project.basic_info", "Collecte les caractéristiques publiques de base du projet.", "low", True, False, "Retourne des indicateurs réels sans chemin local absolu.", "project", lambda: _project_basic_info(workspace.root), lambda value: value.get("is_directory") is True and isinstance(value.get("name"), str), empty_schema))

    def list_files(args: dict[str, Any]) -> dict[str, Any]:
        path = workspace.resolve(str(args.get("path", ".")))
        if not path.is_dir(): raise RuntimeError("not_a_directory")
        entries = sorted(path.iterdir(), key=lambda item: item.name.casefold())
        return {"path": workspace.relative_name(path), "entries": [{"name": item.name, "type": "directory" if item.is_dir() else "file"} for item in entries[:MAX_LIST_ENTRIES]], "truncated": len(entries) > MAX_LIST_ENTRIES}

    def read_file(args: dict[str, Any]) -> dict[str, Any]:
        path = workspace.resolve(str(args.get("path", "")))
        if not path.is_file(): raise RuntimeError("not_a_file")
        data = path.read_bytes()
        if len(data) > MAX_READ_BYTES: raise RuntimeError("file_too_large")
        return {"path": workspace.relative_name(path), "content": data.decode("utf-8"), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    registry.register(Capability("filesystem.list", "Liste un dossier du workspace autorisé.", "low", True, False, "Retourne des entrées bornées avec chemins relatifs.", "filesystem", list_files, lambda value: isinstance(value.get("entries"), list) and isinstance(value.get("truncated"), bool), list_schema))
    registry.register(Capability("filesystem.read", "Lit un fichier texte borné du workspace autorisé.", "low", True, False, "Retourne le contenu UTF-8 et son hash réel.", "filesystem", read_file, lambda value: hashlib.sha256(value.get("content", "").encode()).hexdigest() == value.get("sha256"), path_schema, "exact_readback"))
    registry.register(Capability("filesystem.write", "Écrit un fichier texte dans une transaction réversible.", "medium", True, True, "Écrit, vérifie le hash final et crée une transaction avec diff.", "filesystem", lambda args: transactions.write(str(args.get("path", "")), str(args.get("content", ""))).public(), lambda value: value.get("status") == "pending" and isinstance(value.get("after_hash"), str), write_schema))
    window_schema = {"type": "object", "properties": {"window_ref": {"type": "string"}},
                     "required": ["window_ref"], "additionalProperties": False}
    registry.register(Capability("computer.observe", "Observe l'état sémantique et assaini du bureau Windows.",
        "low", True, False, "Retourne fenêtres, application active et affichage sans contenu caché.", "computer",
        desktop.observe, lambda value: isinstance(value.get("observation_id"), str), empty_schema))
    registry.register(Capability("computer.windows", "Découvre les fenêtres visibles et fournit leur window_ref opaque pour cibler une fenêtre nommée.",
        "low", True, False, "Retourne des références opaques, l'état et le statut de premier plan.", "computer",
        desktop.windows, lambda value: isinstance(value.get("windows"), list), empty_schema))
    registry.register(Capability("computer.active_window", "Observe la fenêtre active; à utiliser seulement lorsque le premier plan lui-même importe.",
        "low", True, False, "Retourne la fenêtre active avec une référence opaque.", "computer",
        desktop.active_window, lambda value: isinstance(value.get("active_window"), dict), empty_schema))
    for action in ("focus", "minimize", "restore"):
        registry.register(Capability(f"computer.window.{action}", f"{action.title()} une fenêtre visible observée.",
            "low", True, False, f"Exécute {action} puis vérifie l'état réel de la fenêtre.", "computer",
            lambda args, action=action: desktop.act(action, args.get("window_ref")),
            lambda value: value.get("verification_status") == "verified", window_schema))
    inspect_schema = {"type": "object", "properties": {"window_ref": {"type": "string"},
        "depth": {"type": "integer", "minimum": 0, "maximum": 8},
        "max_elements": {"type": "integer", "minimum": 1, "maximum": 200}},
        "required": ["window_ref"], "additionalProperties": False}
    for name in ("inspect", "elements"):
        registry.register(Capability(f"computer.ui.{name}", "Inspecte les éléments UI bornés et expose editable, read_only et supported_actions.",
            "low", True, False, "Retourne des éléments assainis avec références opaques et actions supportées.", "computer",
            lambda args: desktop.inspect_ui(args.get("window_ref"), depth=args.get("depth", 4),
                                            max_elements=args.get("max_elements", 80)),
            lambda value: isinstance(value.get("elements"), list), inspect_schema))
    element_schema = {"type": "object", "properties": {"element_ref": {"type": "string"}},
                      "required": ["element_ref"], "additionalProperties": False}
    for action in ("invoke", "focus", "toggle", "select"):
        registry.register(Capability(f"computer.ui.{action}", f"Exécute l'action UI sémantique {action}.",
            "low", True, False, "Exécute via UI Automation puis réobserve l'élément.", "computer",
            lambda args, action=action: desktop.ui_action(action, args.get("element_ref")),
            lambda value: value.get("verification_status") == "verified", element_schema))
    value_schema = {"type": "object", "properties": {"element_ref": {"type": "string"},
        "value": {"type": "string", "maxLength": 16000}}, "required": ["element_ref", "value"],
        "additionalProperties": False}
    registry.register(Capability("computer.ui.set_value", "Définit la valeur d'un contrôle éditable via UI Automation; aucun focus de fenêtre préalable n'est requis.",
        "low", True, False, "Écrit du texte directement comme donnée puis relit la valeur lorsque permis.", "computer",
        lambda args: desktop.ui_action("set_value", args.get("element_ref"), value=args.get("value")),
        lambda value: value.get("verification_status") == "verified", value_schema))
    vision = visual or VisualController(desktop)
    registry.register(Capability("computer.visual.displays", "Découvre les moniteurs et leurs display_ref seulement lorsqu'un moniteur ou sa topologie doit être choisi.",
        "low", True, False, "Observe les ecrans sans capturer leurs pixels.", "computer", vision.displays,
        lambda value: isinstance(value.get("displays"), list), empty_schema))
    capture_schema = {"type": "object", "oneOf": [
        {"type": "object", "properties": {"target_type": {"const": "window"}, "window_ref": {"type": "string"}},
         "required": ["target_type", "window_ref"], "additionalProperties": False},
        {"type": "object", "properties": {"target_type": {"const": "display"}, "display_ref": {"type": "string"}},
         "required": ["target_type", "display_ref"], "additionalProperties": False},
        {"type": "object", "properties": {"target_type": {"const": "primary_display"}},
         "required": ["target_type"], "additionalProperties": False},
    ]}
    registry.register(Capability("computer.visual.capture", "Capture une cible explicite: window avec un window_ref observé, display avec un display_ref, ou primary_display sans découverte préalable.",
        "low", True, False, "Produit une image temporaire bornee, sans chemin de fichier.", "computer",
        lambda args: vision.capture(target_type=args.get("target_type"), display_ref=args.get("display_ref"),
                                    window_ref=args.get("window_ref")),
        lambda value: isinstance(value.get("image_ref"), str), capture_schema))
    image_schema = {"type": "object", "properties": {"image_ref": {"type": "string"}},
                    "required": ["image_ref"], "additionalProperties": False}
    registry.register(Capability("computer.visual.inspect", "Inspecte uniquement les metadonnees d'une image_ref temporaire.",
        "low", True, False, "Retourne dimensions, portee, provenance et age, jamais les pixels.", "computer",
        lambda args: vision.inspect(args.get("image_ref")), lambda value: value.get("stale") is False, image_schema))
    registry.register(Capability("computer.visual.ocr", "Extrait localement le texte visible d'une image_ref quand un OCR local est disponible.",
        "low", True, False, "Retourne un texte OCR borne et ses zones; n'effectue aucune action.", "computer",
        lambda args: vision.ocr(args.get("image_ref")),
        lambda value: value.get("provenance") == "LOCAL_OCR" and isinstance(value.get("spans"), list), image_schema, "observation"))
    analyze_schema = {"type": "object", "properties": {"image_ref": {"type": "string"},
        "prompt": {"type": "string", "maxLength": 2000}}, "required": ["image_ref"], "additionalProperties": False}
    registry.register(Capability("computer.visual.analyze", "Analyse une image_ref avec un modele explicitement compatible vision.",
        "low", True, False, "Retourne une inference compacte identifiee VISUAL_MODEL.", "computer",
        lambda args: vision.analyze(args.get("image_ref"), args.get("prompt", "Decris ce qui est affiche.")),
        lambda value: value.get("provenance") == "VISUAL_MODEL" and isinstance(value.get("summary"), str), analyze_schema))
    ground_schema = {"type": "object", "properties": {
        "window_ref": {"type": "string"}, "query": {"type": "string", "maxLength": 500},
        "image_ref": {"type": "string"}, "max_elements": {"type": "integer", "minimum": 1, "maximum": 200}},
        "required": ["window_ref", "query"], "additionalProperties": False}
    registry.register(Capability("computer.perception.ground",
        "Résout une cible UI de façon hybride: UIA d'abord, vision seulement si le ciblage sémantique est insuffisant.",
        "low", True, False, "Retourne un element_ref existant sans effectuer d'action.", "computer",
        lambda args: vision.ground(window_ref=args.get("window_ref"), query=args.get("query"),
                                   image_ref=args.get("image_ref"), max_elements=args.get("max_elements", 80)),
        lambda value: isinstance(value.get("element_ref"), str) and value.get("provenance") in {"SEMANTIC_UIA", "FUSED_UIA_OCR", "FUSED_UIA_VISION"},
        ground_schema, "observation"))
    return registry
