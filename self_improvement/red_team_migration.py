"""Migration explicite du corpus Red Team V1 legacy, sans modèle ni Codex."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .real_bug_corpus import BUG_CORPUS_PATH, RealBugCorpus
from .red_team import DEFAULT_CORPUS_DIR, RedTeamError, validate_attack
from .scenario_lab import DEFAULT_PUBLIC_CASES, load_discovered
from .scenario_loader import DATASET_PATH, load_scenarios
from .variant_generator import LinguisticVariantGenerator


MIGRATION_VERSION = "legacy-family-v1"


class LegacyMigrationError(RedTeamError):
    pass


def _tag_value(tags, prefix):
    return next((tag.split(":", 1)[1] for tag in tags if tag.startswith(prefix)), None)


def _atomic_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


class LegacyCorpusMigrator:
    def __init__(
        self, *, corpus_root=DEFAULT_CORPUS_DIR, public_cases_path=DEFAULT_PUBLIC_CASES,
        dataset_path=DATASET_PATH, bug_corpus_path=BUG_CORPUS_PATH, backup_root=None,
        clock=None, replace_func=os.replace,
    ):
        self.corpus_root = Path(corpus_root)
        self.confirmed_dir = self.corpus_root / "confirmed_failure"
        self.public_cases_path = Path(public_cases_path)
        self.dataset_path = Path(dataset_path)
        self.bug_corpus_path = Path(bug_corpus_path)
        self.backup_root = Path(backup_root or self.corpus_root / "backups")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.replace_func = replace_func

    def _sources(self):
        _version, scenarios = load_scenarios(self.dataset_path)
        bugs = [
            RealBugCorpus(self.bug_corpus_path).to_scenario(case)
            for case in RealBugCorpus(self.bug_corpus_path).load(
                statuses={"validated", "integrated", "resolved"}
            )
        ]
        return {item.id: item for item in [*bugs, *scenarios]}

    def plan(self) -> dict:
        public = load_discovered(self.public_cases_path)
        legacy_public = [
            item for item in public
            if "red-team" in item.tags
            and not any(tag.startswith("discovery-family:") for tag in item.tags)
            and not any(tag.startswith("legacy-status:") for tag in item.tags)
        ]
        public_by_signature = {
            _tag_value(item.tags, "normalized-signature:"): item
            for item in legacy_public
            if _tag_value(item.tags, "normalized-signature:")
        }
        sources = self._sources()
        file_updates = {}
        signature_info = {}
        errors = []
        legacy_files_found = 0
        for path in sorted(self.confirmed_dir.glob("*.json")):
            try:
                wrapper = json.loads(path.read_text(encoding="utf-8"))
                attack = validate_attack(wrapper.get("attack"))
            except (OSError, json.JSONDecodeError, RedTeamError, AttributeError) as error:
                errors.append(type(error).__name__)
                continue
            signature = attack["normalized_signature"]
            scenario = public_by_signature.get(signature)
            if scenario is None:
                continue
            legacy_files_found += 1
            source = sources.get(attack["base_scenario_id"])
            if source is not None:
                source_family = LinguisticVariantGenerator.family(source)
                digest = hashlib.sha256(
                    f"{attack['family_id']}:{source_family}".encode("utf-8")
                ).hexdigest()[:12]
                discovery_family = f"{attack['family_id']}-{digest}"
                status = "family_reconstructed"
            else:
                discovery_family = f"legacy-public-{signature[:12]}"
                status = "legacy_public"
            migrated = {
                "version": "1.1",
                "attack": attack,
                "migration": {
                    "version": MIGRATION_VERSION,
                    "status": status,
                    "discovery_family": discovery_family,
                    "exposure": "public",
                    "previous_version": str(wrapper.get("version", "unknown")),
                },
            }
            file_updates[path] = migrated
            signature_info[signature] = (status, discovery_family)

        scenario_updates = {}
        reconstructed = legacy_public_only = 0
        for scenario in legacy_public:
            signature = _tag_value(scenario.tags, "normalized-signature:")
            info = signature_info.get(signature)
            if info is None:
                status = "legacy_public"
                stable = signature or hashlib.sha256(scenario.id.encode("utf-8")).hexdigest()[:24]
                discovery_family = f"legacy-public-{stable[:12]}"
            else:
                status, discovery_family = info
            if status == "family_reconstructed":
                reconstructed += 1
            else:
                legacy_public_only += 1
            scenario_updates[scenario.id] = replace(
                scenario,
                split="train" if scenario.split == "holdout" else scenario.split,
                tags=list(dict.fromkeys([
                    *scenario.tags, f"discovery-family:{discovery_family}",
                    f"legacy-status:{status}", "legacy-exposure:public",
                ])),
            )

        migrated_public = [scenario_updates.get(item.id, item) for item in public]
        return {
            "legacy_files_found": legacy_files_found,
            "legacy_public_scenarios_found": len(legacy_public),
            "family_reconstructed": reconstructed,
            "legacy_public": legacy_public_only,
            "errors": len(errors),
            "error_types": sorted(set(errors)),
            "file_updates": file_updates,
            "public_payload": {"version": "1.0", "scenarios": [asdict(item) for item in migrated_public]},
        }

    @staticmethod
    def public_report(plan: dict, *, dry_run: bool, backup_path=None) -> dict:
        return {
            "version": "1.0", "migration": MIGRATION_VERSION, "dry_run": dry_run,
            "legacy_files_found": plan["legacy_files_found"],
            "legacy_public_scenarios_found": plan["legacy_public_scenarios_found"],
            "family_reconstructed": plan["family_reconstructed"],
            "legacy_public": plan["legacy_public"], "errors": plan["errors"],
            "error_types": plan["error_types"],
            "backup_created": str(backup_path) if backup_path else None,
            # Aucun nom de fichier, ID, message ou famille n'est exposé.
        }

    def migrate(self, *, dry_run=True) -> dict:
        plan = self.plan()
        if dry_run or not plan["legacy_public_scenarios_found"]:
            return self.public_report(plan, dry_run=dry_run)
        if plan["errors"]:
            raise LegacyMigrationError(
                "Migration annulée : au moins un fichier du corpus est invalide."
            )
        stamp = self.clock().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = self.backup_root / f"legacy-migration-{stamp}"
        backup_confirmed = backup / "confirmed_failure"
        backup_confirmed.mkdir(parents=True, exist_ok=False)
        for target in plan["file_updates"]:
            shutil.copy2(target, backup_confirmed / target.name)
        backup_public = backup / "public.json"
        backup_public.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.public_cases_path, backup_public)

        targets = [*plan["file_updates"], self.public_cases_path]
        try:
            with tempfile.TemporaryDirectory(prefix="legacy_migration_", dir=self.corpus_root) as staging_name:
                staging = Path(staging_name)
                staged = []
                for index, (target, payload) in enumerate(plan["file_updates"].items()):
                    path = staging / f"{index:04d}.json"
                    _atomic_json(path, payload)
                    staged.append((path, target))
                public_stage = staging / "public.json"
                _atomic_json(public_stage, plan["public_payload"])
                staged.append((public_stage, self.public_cases_path))
                for source, target in staged:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self.replace_func(source, target)
        except BaseException:
            for target in targets:
                original = backup_public if target == self.public_cases_path else backup_confirmed / target.name
                if original.exists():
                    shutil.copy2(original, target)
            raise
        return self.public_report(plan, dry_run=False, backup_path=backup)


def write_migration_report(report: dict, directory: Path):
    directory = Path(directory)
    json_path = directory / "red_team_legacy_migration.json"
    markdown_path = directory / "red_team_legacy_migration.md"
    _atomic_json(json_path, report)
    markdown = (
        "# Migration legacy Red Team\n\n"
        f"- Dry-run : {report['dry_run']}\n"
        f"- Fichiers legacy : {report['legacy_files_found']}\n"
        f"- Scénarios publics legacy : {report['legacy_public_scenarios_found']}\n"
        f"- Famille reconstruite : {report['family_reconstructed']}\n"
        f"- Conservés legacy_public : {report['legacy_public']}\n"
        f"- Erreurs : {report['errors']}\n"
    )
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
    return json_path, markdown_path
