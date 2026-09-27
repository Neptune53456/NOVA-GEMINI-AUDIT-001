"""Laboratoire autonome borné de scénarios déterministes et adversariaux."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import tempfile
import threading
import time

from .benchmark_runner import execute_scenario
from .models import Scenario
from .real_bug_corpus import BUG_CORPUS_PATH, RealBugCorpus
from .red_team import (
    DeterministicRedTeamProvider, RedTeamCorpus, RedTeamError, RedTeamProvider,
    attack_signature, provider_for_mode,
)
from .scenario_loader import DATASET_PATH, load_scenarios
from .variant_generator import (
    LinguisticVariantGenerator, assign_discovery_splits, assign_family_splits,
    scenario_fingerprint,
)
from .curriculum import detect_saturation
from .reporting import write_text_atomic


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_CASES = PROJECT_ROOT / ".self_improvement_discoveries" / "public.json"
DEFAULT_PRIVATE_HOLDOUT = PROJECT_ROOT / ".self_improvement_holdout" / "scenarios.json"
DEFAULT_REPORT_DIR = Path(__file__).with_name("reports")
MAX_CAMPAIGN_SCENARIOS = 10_000


class ScenarioLabError(ValueError):
    pass


def _write_json_atomic(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, newline="\n") as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def _scenario_from_dict(payload: dict) -> Scenario:
    fields = {
        "id", "category", "split", "initial_context", "messages", "simulated_state",
        "expectations", "success_criteria", "weight", "tags", "runner",
    }
    if set(payload) != fields:
        raise ScenarioLabError("Schéma de scénario découvert invalide.")
    scenario = Scenario(**payload)
    if not scenario.id or not scenario.messages or not scenario.success_criteria:
        raise ScenarioLabError("Scénario découvert incomplet.")
    return scenario


def load_discovered(path: Path) -> list[Scenario]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != "1.0" or not isinstance(payload.get("scenarios"), list):
        raise ScenarioLabError("Corpus de découvertes invalide.")
    return [_scenario_from_dict(item) for item in payload["scenarios"]]


def _execute_bounded(executor, scenario, timeout_seconds):
    """Borne un harness injecté ; un worker bloqué reste daemon et ne retient pas le processus."""
    output = queue.Queue(maxsize=1)

    def worker():
        try:
            output.put((True, executor(scenario)))
        except BaseException as error:
            output.put((False, error))

    thread = threading.Thread(target=worker, name="scenario-lab-runner", daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise TimeoutError("Un scénario est resté bloqué au-delà du délai de campagne.")
    succeeded, value = output.get_nowait()
    if not succeeded:
        raise value
    return value


class ScenarioLab:
    def __init__(
        self, *, dataset_path=DATASET_PATH, bug_corpus_path=BUG_CORPUS_PATH,
        public_cases_path=DEFAULT_PUBLIC_CASES, private_holdout_path=DEFAULT_PRIVATE_HOLDOUT,
        report_dir=DEFAULT_REPORT_DIR, executor=execute_scenario,
        red_team_provider: RedTeamProvider | None = None, red_team_mode="deterministic",
        red_team_corpus=None, max_per_family=20, logger=print, clock=time.monotonic,
    ):
        self.dataset_path = Path(dataset_path)
        self.bug_corpus = RealBugCorpus(bug_corpus_path)
        self.public_cases_path = Path(public_cases_path)
        self.private_holdout_path = Path(private_holdout_path)
        self.report_dir = Path(report_dir)
        self.executor = executor
        self.red_team_provider = red_team_provider or DeterministicRedTeamProvider()
        self.red_team_mode = red_team_mode
        if red_team_corpus is not None:
            self.red_team_corpus = red_team_corpus
        elif self.public_cases_path == Path(DEFAULT_PUBLIC_CASES):
            self.red_team_corpus = RedTeamCorpus()
        else:
            self.red_team_corpus = RedTeamCorpus(self.public_cases_path.parent / "red_team_corpus")
        self.max_per_family = max_per_family
        self._last_attacks = {}
        self._last_red_team_stats = {}
        self.logger = logger
        self.clock = clock

    def _sources(self, *, seed=0) -> list[tuple[Scenario, list[str]]]:
        """Compatibilité V0 : expose les sources sans déclencher le Red Team."""
        del seed
        _version, existing = load_scenarios(self.dataset_path)
        sources = [
            (self.bug_corpus.to_scenario(case), list(case.variants))
            for case in self.bug_corpus.load(statuses={"validated", "integrated", "resolved"})
        ]
        sources.extend((scenario, []) for scenario in existing)
        return sources

    def generate(self, *, count: int, seed: int, max_seconds=None) -> tuple[int, list[Scenario]]:
        if not 0 <= count <= MAX_CAMPAIGN_SCENARIOS:
            raise ScenarioLabError(f"La génération doit être comprise entre 0 et {MAX_CAMPAIGN_SCENARIOS}.")
        _version, existing = load_scenarios(self.dataset_path)
        bug_sources = [
            self.bug_corpus.to_scenario(case)
            for case in self.bug_corpus.load(statuses={"validated", "integrated", "resolved"})
        ]
        sources = [*bug_sources, *existing]
        source_by_id = {item.id: item for item in sources}
        try:
            attacks = self.red_team_provider.propose(
                limit=count, seed=seed, scenarios=sources,
                max_per_family=self.max_per_family, max_seconds=max_seconds,
            )
        except TypeError as error:
            # Compatibilité avec un provider V0 injecté par un appelant externe.
            if "unexpected keyword" not in str(error):
                raise
            attacks = self.red_team_provider.propose(limit=count, seed=seed)
        generated, seen = [], set()
        self._last_attacks = {}
        invalid_base = 0
        for attack in attacks:
            source = source_by_id.get(attack.get("base_scenario_id"))
            if source is None or len(attack.get("messages", [])) != len(source.messages):
                invalid_base += 1
                continue
            signature = attack_signature(attack["messages"])
            identifier = f"red-team-{signature}"
            source_family = LinguisticVariantGenerator.family(source)
            family_digest = hashlib.sha256(
                f"{attack['family_id']}:{source_family}".encode("utf-8")
            ).hexdigest()[:12]
            discovery_family = f"{attack['family_id']}-{family_digest}"
            scenario = replace(
                source, id=identifier, messages=list(attack["messages"]), weight=min(source.weight, 0.25),
                tags=list(dict.fromkeys([
                    *source.tags, "generated", "provisional", "red-team",
                    f"discovery-family:{discovery_family}",
                    f"family:{attack['family_id']}", f"red-team-category:{attack['category']}",
                    f"red-team-source:{attack['source']}", f"red-team-seed:{attack['seed']}",
                    f"normalized-signature:{signature}",
                ])),
            )
            fingerprint = scenario_fingerprint(scenario)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            generated.append(scenario)
            self._last_attacks[identifier] = attack
        provider_stats = dict(getattr(self.red_team_provider, "last_stats", {}))
        provider_stats["invalid"] = int(provider_stats.get("invalid", 0)) + invalid_base
        provider_stats["unique"] = len(generated)
        self._last_red_team_stats = provider_stats
        return count, assign_family_splits(generated[:count], seed=seed)

    @staticmethod
    def _merge_unique(existing: list[Scenario], additions: list[Scenario]) -> list[Scenario]:
        merged = {scenario_fingerprint(item): item for item in existing}
        for item in additions:
            merged.setdefault(scenario_fingerprint(item), item)
        return list(merged.values())

    def _save_failures(self, failures: list[Scenario]):
        public = [item for item in failures if item.split != "holdout"]
        holdout = [item for item in failures if item.split == "holdout"]
        existing_public = load_discovered(self.public_cases_path)
        existing_holdout = load_discovered(self.private_holdout_path)
        merged_public = self._merge_unique(existing_public, public)
        merged_holdout = self._merge_unique(existing_holdout, holdout)
        _write_json_atomic(self.public_cases_path, {"version": "1.0", "scenarios": [asdict(item) for item in merged_public]})
        _write_json_atomic(self.private_holdout_path, {"version": "1.0", "scenarios": [asdict(item) for item in merged_holdout]})
        return len(merged_public) - len(existing_public), len(merged_holdout) - len(existing_holdout)

    def run(self, *, max_scenarios: int, seed: int, max_seconds=60.0, save=True) -> dict:
        if max_seconds <= 0:
            raise ScenarioLabError("Le timeout de campagne doit être positif.")
        started = self.clock()
        requested, generated = self.generate(count=max_scenarios, seed=seed, max_seconds=max_seconds)
        self.logger(f"[RedTeam] mode={self.red_team_mode}")
        self.logger(f"[RedTeam] {self._last_red_team_stats.get('proposed', len(generated))} attaques proposées")
        self.logger(f"[ScenarioLab] {requested} scénarios demandés, {len(generated)} générés")
        self.logger(f"[ScenarioLab] {len(generated)} uniques")
        existing_public = load_discovered(self.public_cases_path)
        existing_holdout = load_discovered(self.private_holdout_path)
        known = {scenario_fingerprint(item) for item in [*existing_public, *existing_holdout]}
        failures = []
        uncertain = 0
        known_failures = 0
        interrupted = False
        # Generation may exhaust its budget before producing any scenarios.
        # Preserve that timeout even when the execution loop would be empty.
        timed_out = bool(self._last_red_team_stats.get("timed_out")) or self.clock() - started >= max_seconds
        try:
            for scenario in generated:
                if timed_out:
                    break
                remaining = max_seconds - (self.clock() - started)
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    result = _execute_bounded(self.executor, scenario, remaining)
                except TimeoutError:
                    timed_out = True
                    break
                if result.error or not result.criteria:
                    uncertain += 1
                elif not result.passed:
                    if scenario_fingerprint(scenario) in known:
                        known_failures += 1
                    else:
                        failures.append(scenario)
        except KeyboardInterrupt:
            interrupted = True
        failures, split_policy = assign_discovery_splits(
            failures, existing_public=existing_public,
            existing_holdout=existing_holdout, seed=seed,
        )
        counts = {split: sum(item.split == split for item in failures) for split in ("train", "validation", "holdout")}
        new_public = new_holdout = 0
        if save and failures:
            new_public, new_holdout = self._save_failures(failures)
            for scenario in failures:
                attack = self._last_attacks.get(scenario.id)
                # Le corpus versionnable ne reçoit jamais le détail d'un holdout.
                if attack is not None and scenario.split != "holdout":
                    self.red_team_corpus.save(attack, status="confirmed_failure")
        report = {
            "version": "1.0", "timestamp": datetime.now(timezone.utc).isoformat(),
            "seed": seed, "requested": requested, "generated": len(generated),
            "unique": len(generated), "new_failures": len(failures),
            "known_failures": known_failures, "saved": new_public + new_holdout,
            "uncertain": uncertain,
            "splits": counts, "timed_out": timed_out, "interrupted": interrupted,
            "split_policy": split_policy,
            "duration_seconds": round(self.clock() - started, 3),
            # Le détail du holdout n'est intentionnellement jamais sérialisé.
            "visible_failure_ids": [item.id for item in failures if item.split != "holdout"],
            "red_team": {
                "mode": self.red_team_mode,
                "proposed": self._last_red_team_stats.get("proposed", len(generated)),
                "unique": len(generated),
                "duplicates": self._last_red_team_stats.get("duplicates", 0),
                "invalid": self._last_red_team_stats.get("invalid", 0),
                "quota_dropped": self._last_red_team_stats.get("quota_dropped", 0),
                "families": self._last_red_team_stats.get("families", {}),
                "categories": self._last_red_team_stats.get("categories", {}),
            },
        }
        self.logger(f"[ScenarioLab] {len(failures)} nouveaux échecs")
        self.logger(f"[RedTeam] {len(failures)} échecs confirmés")
        self.logger(f"[RedTeam] {self._last_red_team_stats.get('duplicates', 0)} doublons")
        self.logger(f"[RedTeam] {self._last_red_team_stats.get('invalid', 0)} invalides")
        categories = self._last_red_team_stats.get("categories", {})
        if categories:
            self.logger("[RedTeam] catégories : " + ", ".join(
                f"{name}={value}" for name, value in sorted(categories.items())
            ))
        self.logger(f"[ScenarioLab] {known_failures} échecs déjà connus")
        self.logger(f"[ScenarioLab] {uncertain} résultats incertains (non promus)")
        self.logger(f"[ScenarioLab] {report['saved']} nouveaux cas conservés")
        self.logger(f"[ScenarioLab] train={counts['train']} validation={counts['validation']} holdout={counts['holdout']}")
        if counts["holdout"] == 0:
            self.logger(f"[ScenarioLab] holdout vide : {split_policy['reason']}")
        if timed_out:
            self.logger(f"[ScenarioLab] timeout après {max_seconds:.1f} secondes")
        if interrupted:
            self.logger("[ScenarioLab] campagne interrompue proprement")
        return report

    def write_report(self, report: dict, *, stem="scenario_lab"):
        self.report_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.report_dir / f"{stem}.json"
        markdown_path = self.report_dir / f"{stem}.md"
        history_path = self.report_dir / "scenario_lab_campaign_history.json"
        try:
            history_payload = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {"campaigns": []}
        except (OSError, json.JSONDecodeError):
            history_payload = {"campaigns": []}
        aggregate = {
            "proposed": report.get("red_team", {}).get("proposed", report.get("generated", 0)),
            "duplicates": report.get("red_team", {}).get("duplicates", 0),
            "new_failures": report.get("new_failures", 0),
            "new_root_causes": report.get("new_root_causes", 0),
        }
        history_payload["campaigns"] = [*history_payload.get("campaigns", [])[-19:], aggregate]
        saturation = detect_saturation(history_payload["campaigns"])
        report["saturation"] = asdict(saturation)
        _write_json_atomic(history_path, history_payload)
        _write_json_atomic(json_path, report)
        markdown = (
            "# Autonomous Scenario Lab\n\n"
            f"- Seed : {report['seed']}\n"
            f"- Scénarios uniques : {report['unique']}\n"
            f"- Nouveaux échecs : {report['new_failures']}\n"
            f"- Cas conservés : {report['saved']}\n"
            f"- Résultats incertains : {report['uncertain']}\n"
            f"- Red Team : {report['red_team']['mode']} ({report['red_team']['unique']} uniques)\n"
            f"- Répartition : train={report['splits']['train']}, validation={report['splits']['validation']}, holdout={report['splits']['holdout']}\n"
            f"- Politique de split : {report['split_policy']['reason']}\n"
            f"- Saturation : {'oui' if saturation.saturated else 'non'} — {saturation.reason}\n"
            f"- État : {'INTERRUPTED' if report['interrupted'] else 'TIMEOUT' if report['timed_out'] else 'COMPLETED'}\n"
        )
        write_text_atomic(markdown_path, markdown)
        return json_path, markdown_path


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--generate", type=int, metavar="N")
    mode.add_argument("--campaign", action="store_true")
    parser.add_argument("--max-scenarios", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-seconds", type=float, default=60)
    parser.add_argument("--red-team", choices=("deterministic", "model", "hybrid"))
    parser.add_argument("--max-per-family", type=int, default=20)
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--report", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    count = args.max_scenarios if args.campaign else args.generate
    try:
        red_team_mode = args.red_team or "deterministic"
        chat_callable = None
        if red_team_mode in {"model", "hybrid"}:
            try:
                from model_router import chat
                chat_callable = chat
            except (ImportError, OSError):
                pass
        provider = provider_for_mode(red_team_mode, chat_callable=chat_callable)
        lab = ScenarioLab(red_team_provider=provider, red_team_mode=red_team_mode, max_per_family=args.max_per_family)
        report = lab.run(max_scenarios=count, seed=args.seed, max_seconds=args.max_seconds, save=not args.no_save)
        if args.report:
            lab.write_report(report)
    except (ScenarioLabError, RedTeamError, OSError, json.JSONDecodeError) as error:
        print(f"[ScenarioLab] erreur : {error}")
        return 2
    if report["interrupted"]:
        return 130
    return 124 if report["timed_out"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
