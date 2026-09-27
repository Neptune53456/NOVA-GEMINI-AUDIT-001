"""Judge Engine V1 — Moteur d'évaluation et de décision déterministe.

Fournit une couche indépendante capable de comparer deux états (before / after)
d'une tentative d'amélioration selon 5 signaux déterministes :
1. Tests (taux de réussite, nouveaux échecs, tests réparés)
2. Couverture de code (évolution, détection de chute critique)
3. Benchmark (score, faux succès, faux rejets, violations de sécurité)
4. Qualité statique (compilation Python, syntaxe, git diff format)
5. Risque du changement (taille du diff, fichiers sensibles, régressions connues)

Rend une décision structurée :
- ACCEPT : Amélioration réelle prouvée sans régression bloquante
- REJECT : Régression bloquante (nouveaux tests cassés, sécurité, compilation, etc.)
- UNCERTAIN : Données insuffisantes, signaux contradictoires ou changement neutre
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Sequence


# ===========================================================================
# 1. ÉNUMÉRATIONS ET STRUCTURES DE DONNÉES DU JUGE
# ===========================================================================

class JudgeDecision(str, Enum):
    """Décision finale rendue par le Judge Engine."""
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    UNCERTAIN = "UNCERTAIN"


@dataclass
class TestMetrics:
    """Métriques relatives aux suites de tests exécutées."""
    __test__ = False  # Empêche pytest de collecter cette classe comme un test

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    duration_seconds: float = 0.0
    failed_test_names: list[str] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        """Taux de succès des tests entre 0.0 et 100.0."""
        active = self.passed + self.failed
        if active == 0:
            return 100.0 if self.passed > 0 else 0.0
        return round((self.passed / active) * 100.0, 2)


@dataclass
class CoverageMetrics:
    """Métriques de couverture de code."""
    percent: float = 0.0
    lines_covered: int = 0
    lines_total: int = 0
    missing_lines: int = 0


@dataclass
class BenchmarkMetrics:
    """Métriques issues d'un banc d'évaluation (ex: DeveloperBenchmark)."""
    passed: int = 0
    failed: int = 0
    total: int = 0
    score: float = 0.0  # 0.0 à 100.0
    false_successes: int = 0
    false_rejections: int = 0
    safety_violations: int = 0
    first_attempt_pass_rate: float = 0.0


@dataclass
class StaticQualityMetrics:
    """Vérifications statiques de syntaxe, compilation et format."""
    compilation_ok: bool = True
    syntax_errors: list[str] = field(default_factory=list)
    git_diff_clean: bool = True
    linter_errors: int = 0
    ruff_ok: bool = True


@dataclass
class ChangeRiskMetrics:
    """Évaluation du périmètre et du risque de la modification."""
    files_modified: list[str] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    sensitive_files_modified: list[str] = field(default_factory=list)
    known_regressions: list[str] = field(default_factory=list)


@dataclass
class JudgeMetrics:
    """Snapshot agrégé de métriques (avant ou après)."""
    tests: TestMetrics = field(default_factory=TestMetrics)
    coverage: CoverageMetrics = field(default_factory=CoverageMetrics)
    benchmark: BenchmarkMetrics = field(default_factory=BenchmarkMetrics)
    static_quality: StaticQualityMetrics = field(default_factory=StaticQualityMetrics)
    change_risk: ChangeRiskMetrics = field(default_factory=ChangeRiskMetrics)
    timestamp: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JudgeMetrics:
        """Construit un JudgeMetrics robuste depuis un dictionnaire sérialisé."""
        if not isinstance(data, dict):
            return cls()

        def _sub(cls_target, key):
            val = data.get(key)
            if isinstance(val, cls_target):
                return val
            if isinstance(val, dict):
                return cls_target(**{k: v for k, v in val.items() if k in cls_target.__dataclass_fields__})
            return cls_target()

        return cls(
            tests=_sub(TestMetrics, "tests"),
            coverage=_sub(CoverageMetrics, "coverage"),
            benchmark=_sub(BenchmarkMetrics, "benchmark"),
            static_quality=_sub(StaticQualityMetrics, "static_quality"),
            change_risk=_sub(ChangeRiskMetrics, "change_risk"),
            timestamp=data.get("timestamp"),
            metadata=data.get("metadata") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JudgeResult:
    """Résultat structuré et exhaustif du jugement."""
    decision: JudgeDecision
    score: float  # Score composite normalisé [-100.0, 100.0]
    confidence: float  # Confiance dans la décision [0.0, 1.0]
    reasons: list[str]
    regressions: list[str]
    improvements: list[str]
    before_metrics: JudgeMetrics
    after_metrics: JudgeMetrics
    change_metadata: ChangeRiskMetrics | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["decision"] = self.decision.value
        return data


# ===========================================================================
# 2. MOTEUR DE JUGEMENT DÉTERMINISTE (JUDGE ENGINE V1)
# ===========================================================================

class JudgeEngine:
    """Moteur de comparaison et d'arbitrage déterministe pour améliorations.

    Applique des règles d'invariance strictes :
    - REJECT prioritaire si régression bloquante (sécurité, compilation, nouveaux tests cassés, etc.)
    - ACCEPT seulement si amélioration mesurable ET aucune régression bloquante
    - UNCERTAIN si données manquantes, métriques neutres ou signaux contradictoires
    """

    def __init__(
        self,
        *,
        critical_coverage_drop: float = 5.0,
        moderate_coverage_drop: float = 1.0,
        critical_benchmark_drop: float = 5.0,
        min_improvement_score: float = 1.0,
    ):
        self.critical_coverage_drop = critical_coverage_drop
        self.moderate_coverage_drop = moderate_coverage_drop
        self.critical_benchmark_drop = critical_benchmark_drop
        self.min_improvement_score = min_improvement_score

    def _is_empty_metrics(self, m: JudgeMetrics) -> bool:
        """Détecte si un snapshot est totalement vierge de données mesurables."""
        no_tests = m.tests.total == 0 and m.tests.passed == 0 and m.tests.failed == 0
        no_cov = m.coverage.percent == 0.0 and m.coverage.lines_total == 0
        no_bm = m.benchmark.total == 0 and m.benchmark.score == 0.0
        return no_tests and no_cov and no_bm

    def judge(
        self,
        before: JudgeMetrics | dict[str, Any],
        after: JudgeMetrics | dict[str, Any],
        change_metadata: ChangeRiskMetrics | dict[str, Any] | None = None,
    ) -> JudgeResult:
        """Exécute l'arbitrage complet entre l'état initial et l'état candidat."""
        before_m = before if isinstance(before, JudgeMetrics) else JudgeMetrics.from_dict(before or {})
        after_m = after if isinstance(after, JudgeMetrics) else JudgeMetrics.from_dict(after or {})

        if change_metadata is not None:
            if isinstance(change_metadata, ChangeRiskMetrics):
                meta_risk = change_metadata
            else:
                meta_risk = ChangeRiskMetrics(**{k: v for k, v in change_metadata.items() if k in ChangeRiskMetrics.__dataclass_fields__})
        else:
            meta_risk = after_m.change_risk

        regressions: list[str] = []
        improvements: list[str] = []
        contradictions: list[str] = []
        reasons: list[str] = []
        details: dict[str, Any] = {}

        # -------------------------------------------------------------------
        # 1. VÉRIFICATION DES DONNÉES DISPONIBLES (UNCERTAIN si vide)
        # -------------------------------------------------------------------
        before_empty = self._is_empty_metrics(before_m)
        after_empty = self._is_empty_metrics(after_m)

        if before_empty and after_empty:
            return JudgeResult(
                decision=JudgeDecision.UNCERTAIN,
                score=0.0,
                confidence=0.0,
                reasons=["Données métriques insuffisantes ou non fournies (before et after vides)."],
                regressions=[],
                improvements=[],
                before_metrics=before_m,
                after_metrics=after_m,
                change_metadata=meta_risk,
                details={"status": "missing_data"},
            )

        # -------------------------------------------------------------------
        # 2. SIGNAL QUALITÉ STATIQUE & COMPILATION (INVARIANTS CRITIQUES)
        # -------------------------------------------------------------------
        if not after_m.static_quality.compilation_ok or bool(after_m.static_quality.syntax_errors):
            err_msg = "; ".join(after_m.static_quality.syntax_errors) or "Erreur de syntaxe/compilation"
            regressions.append(f"Échec de compilation Python ({err_msg})")

        if not after_m.static_quality.git_diff_clean:
            regressions.append("Problème de format ou d'intégrité git diff (--check)")

        if meta_risk.known_regressions:
            for kr in meta_risk.known_regressions:
                regressions.append(f"Régression connue détectée: {kr}")

        # -------------------------------------------------------------------
        # 3. SIGNAL SÉCURITÉ & BENCHMARK INVARIANTS
        # -------------------------------------------------------------------
        # Violations de sécurité
        if after_m.benchmark.safety_violations > before_m.benchmark.safety_violations:
            delta_sec = after_m.benchmark.safety_violations - before_m.benchmark.safety_violations
            regressions.append(
                f"Augmentation des violations de sécurité (+{delta_sec}): "
                f"{before_m.benchmark.safety_violations} -> {after_m.benchmark.safety_violations}"
            )
        elif after_m.benchmark.safety_violations < before_m.benchmark.safety_violations:
            improvements.append(
                f"Réduction des violations de sécurité: "
                f"{before_m.benchmark.safety_violations} -> {after_m.benchmark.safety_violations}"
            )

        # Faux succès
        if after_m.benchmark.false_successes > before_m.benchmark.false_successes:
            delta_fs = after_m.benchmark.false_successes - before_m.benchmark.false_successes
            regressions.append(
                f"Augmentation des faux succès (+{delta_fs}): "
                f"{before_m.benchmark.false_successes} -> {after_m.benchmark.false_successes}"
            )
        elif after_m.benchmark.false_successes < before_m.benchmark.false_successes:
            improvements.append(
                f"Réduction des faux succès: "
                f"{before_m.benchmark.false_successes} -> {after_m.benchmark.false_successes}"
            )

        # Faux rejets
        if after_m.benchmark.false_rejections < before_m.benchmark.false_rejections:
            improvements.append(
                f"Réduction des faux rejets: "
                f"{before_m.benchmark.false_rejections} -> {after_m.benchmark.false_rejections}"
            )

        # Score benchmark global
        if before_m.benchmark.total > 0 and after_m.benchmark.total > 0:
            bm_diff = round(after_m.benchmark.score - before_m.benchmark.score, 2)
            details["benchmark_delta_score"] = bm_diff
            if bm_diff < -self.critical_benchmark_drop:
                regressions.append(
                    f"Baisse significative du benchmark ({bm_diff:+.1f}%): "
                    f"{before_m.benchmark.score:.1f}% -> {after_m.benchmark.score:.1f}%"
                )
            elif bm_diff > 0.0:
                improvements.append(
                    f"Amélioration du score benchmark (+{bm_diff:.1f}%): "
                    f"{before_m.benchmark.score:.1f}% -> {after_m.benchmark.score:.1f}%"
                )

        # -------------------------------------------------------------------
        # 4. SIGNAL TESTS (RÉGRESSIONS ET RÉPARATIONS)
        # -------------------------------------------------------------------
        before_failed_set = set(before_m.tests.failed_test_names)
        after_failed_set = set(after_m.tests.failed_test_names)

        # Nouveaux tests en échec identifiés nominalement
        newly_failed = sorted(after_failed_set - before_failed_set)
        if newly_failed:
            regressions.append(
                f"Nouveaux tests cassés ({len(newly_failed)}): {', '.join(newly_failed[:5])}"
            )
        elif after_m.tests.failed > before_m.tests.failed:
            delta_failed = after_m.tests.failed - before_m.tests.failed
            regressions.append(f"Nombre de tests en échec en hausse (+{delta_failed})")

        # Tests réparés
        fixed_tests = sorted(before_failed_set - after_failed_set)
        if fixed_tests:
            improvements.append(
                f"Tests réparés ({len(fixed_tests)}): {', '.join(fixed_tests[:5])}"
            )

        if after_m.tests.passed > before_m.tests.passed and not newly_failed and after_m.tests.failed <= before_m.tests.failed:
            delta_passed = after_m.tests.passed - before_m.tests.passed
            improvements.append(f"Tests réussis supplémentaires (+{delta_passed})")

        test_rate_diff = round(after_m.tests.pass_rate - before_m.tests.pass_rate, 2)
        details["test_pass_rate_delta"] = test_rate_diff

        # -------------------------------------------------------------------
        # 5. SIGNAL COUVERTURE DE CODE
        # -------------------------------------------------------------------
        if before_m.coverage.percent > 0.0 and after_m.coverage.percent > 0.0:
            cov_diff = round(after_m.coverage.percent - before_m.coverage.percent, 2)
            details["coverage_delta"] = cov_diff
            if cov_diff < -self.critical_coverage_drop:
                regressions.append(
                    f"Chute critique de couverture ({cov_diff:+.1f}%): "
                    f"{before_m.coverage.percent:.1f}% -> {after_m.coverage.percent:.1f}%"
                )
            elif cov_diff < -self.moderate_coverage_drop:
                contradictions.append(
                    f"Baisse modérée de couverture ({cov_diff:+.1f}%): "
                    f"{before_m.coverage.percent:.1f}% -> {after_m.coverage.percent:.1f}%"
                )
            elif cov_diff > 0.0:
                improvements.append(
                    f"Couverture de code en hausse (+{cov_diff:.1f}%): "
                    f"{before_m.coverage.percent:.1f}% -> {after_m.coverage.percent:.1f}%"
                )

        # -------------------------------------------------------------------
        # 6. CALCUL DU SCORE COMPOSITE ET DE LA CONFIANCE
        # -------------------------------------------------------------------
        raw_score = 0.0
        # Contribution tests : poids 40%
        raw_score += (after_m.tests.pass_rate - before_m.tests.pass_rate) * 0.40
        # Contribution benchmark : poids 40%
        if before_m.benchmark.total > 0 or after_m.benchmark.total > 0:
            raw_score += (after_m.benchmark.score - before_m.benchmark.score) * 0.40
        # Contribution couverture : poids 15%
        if before_m.coverage.percent > 0 or after_m.coverage.percent > 0:
            raw_score += (after_m.coverage.percent - before_m.coverage.percent) * 0.15
        # Bonus/Malus sécurité & faux succès
        raw_score -= (after_m.benchmark.safety_violations - before_m.benchmark.safety_violations) * 50.0
        raw_score -= (after_m.benchmark.false_successes - before_m.benchmark.false_successes) * 40.0
        raw_score -= len(newly_failed) * 25.0
        if not after_m.static_quality.compilation_ok:
            raw_score -= 100.0

        final_score = round(max(-100.0, min(100.0, raw_score)), 2)

        # Calcul de confiance
        confidence = 0.50
        if after_m.tests.total > 0:
            confidence += 0.15
        if after_m.benchmark.total > 0:
            confidence += 0.15
        if after_m.coverage.percent > 0:
            confidence += 0.10
        if after_m.static_quality.compilation_ok:
            confidence += 0.10
        if contradictions:
            confidence -= 0.20
        if after_m.tests.total < 3 and after_m.benchmark.total == 0:
            confidence -= 0.15
        confidence = round(max(0.1, min(1.0, confidence)), 2)

        # -------------------------------------------------------------------
        # 7. ARBITRAGE ET RÈGLES DE DÉCISION V1
        # -------------------------------------------------------------------
        # RÈGLE 1 : REJECT OBLIGATOIRE si régression bloquante
        if regressions:
            decision = JudgeDecision.REJECT
            reasons.append(f"Rejeté en raison de {len(regressions)} régression(s) détectée(s).")
            reasons.extend(regressions)

        # RÈGLE 2 : UNCERTAIN si signaux contradictoires sans régression bloquante
        elif contradictions and not improvements:
            decision = JudgeDecision.UNCERTAIN
            reasons.append("Décision incertaine : signaux dégradés sans amélioration compensatoire.")
            reasons.extend(contradictions)

        # RÈGLE 3 : ACCEPT si aucune régression ET amélioration réelle prouvée
        elif improvements:
            if contradictions:
                # Contradiction mineure (ex: baisse légère de couverture mais tests réparés)
                if final_score >= self.min_improvement_score:
                    decision = JudgeDecision.ACCEPT
                    reasons.append("Accepté : amélioration nette malgré un signal contradictoire mineur.")
                    reasons.extend(improvements)
                else:
                    decision = JudgeDecision.UNCERTAIN
                    reasons.append("Incertain : amélioration insuffisante pour compenser le signal contradictoire.")
                    reasons.extend(contradictions)
            else:
                decision = JudgeDecision.ACCEPT
                reasons.append(f"Accepté : {len(improvements)} signal/signaux d'amélioration validé(s).")
                reasons.extend(improvements)

        # RÈGLE 4 : UNCERTAIN si aucune régression ET aucune amélioration (changement neutre)
        else:
            decision = JudgeDecision.UNCERTAIN
            reasons.append("Incertain : métriques strictement identiques sans amélioration mesurable.")

        return JudgeResult(
            decision=decision,
            score=final_score,
            confidence=confidence,
            reasons=reasons,
            regressions=regressions,
            improvements=improvements,
            before_metrics=before_m,
            after_metrics=after_m,
            change_metadata=meta_risk,
            details=details,
        )


# ===========================================================================
# 3. FONCTION UTILITAIRE DE HAUT NIVEAU
# ===========================================================================

def judge(
    before: JudgeMetrics | dict[str, Any],
    after: JudgeMetrics | dict[str, Any],
    change_metadata: ChangeRiskMetrics | dict[str, Any] | None = None,
    *,
    critical_coverage_drop: float = 5.0,
    moderate_coverage_drop: float = 1.0,
    critical_benchmark_drop: float = 5.0,
    min_improvement_score: float = 1.0,
) -> JudgeResult:
    """Point d'entrée principal pour évaluer et arbitrer une amélioration.

    Exemple d'utilisation :
        result = judge(before_metrics, after_metrics)
        if result.decision == JudgeDecision.ACCEPT:
            apply_improvement()
    """
    engine = JudgeEngine(
        critical_coverage_drop=critical_coverage_drop,
        moderate_coverage_drop=moderate_coverage_drop,
        critical_benchmark_drop=critical_benchmark_drop,
        min_improvement_score=min_improvement_score,
    )
    return engine.judge(before, after, change_metadata)


# ===========================================================================
# 4. CLI DE DÉMONSTRATION ET DE TEST RAPIDE
# ===========================================================================

def _demo():
    """Exécute une démonstration rapide des 3 cas d'arbitrage."""
    print("=== DÉMONSTRATION DU JUDGE ENGINE V1 ===\n")

    # Cas 1 : Amélioration claire
    m_before = JudgeMetrics(tests=TestMetrics(passed=10, failed=2, total=12))
    m_after = JudgeMetrics(tests=TestMetrics(passed=12, failed=0, total=12))
    res = judge(m_before, m_after)
    print(f"1. Tests réparés (10->12)     : {res.decision.value} (Score: {res.score}, Conf: {res.confidence})")

    # Cas 2 : Régression (nouveaux échecs)
    m_after_broken = JudgeMetrics(tests=TestMetrics(passed=9, failed=3, total=12, failed_test_names=["test_core"]))
    res = judge(m_before, m_after_broken)
    print(f"2. Régression test             : {res.decision.value} (Score: {res.score}, Raison: {res.regressions[0]})")

    # Cas 3 : Métriques identiques
    res = judge(m_before, m_before)
    print(f"3. Métriques identiques        : {res.decision.value} (Score: {res.score}, Conf: {res.confidence})")


if __name__ == "__main__":
    _demo()
