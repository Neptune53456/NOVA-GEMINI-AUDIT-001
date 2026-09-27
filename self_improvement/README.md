# Auto-amélioration contrôlée

La V7 **Autonomous Brain Pool** est documentée dans `ARCHITECTURE_V7.md`.
OmniRoute devient la couche cognitive prioritaire lorsqu'il est configuré; les
providers directs V6.2 et Ollama restent des fallbacks de résilience.

Ce package mesure le comportement logiciel de l'assistant. Il ne réentraîne aucun modèle et ne modifie jamais `memory.db`.

## Software Agent autonome V5

### V5 — agent persistant, missions longues et auto-curriculum vérifié

La V5 conserve la frontière de confiance de V4 et ajoute une couche d’autonomie longue durée. Elle sépare toujours explicitement deux zones :

- le **cœur cognitif auto-modifiable** (`developer_agent`, `developer_tools`, `engineering_planner`, `repo_intelligence`, stratégies et routeur/conversation) ;
- le **Trusted Control Plane** non auto-modifiable (politique de chemins, sécurité du code, budgets/campagnes, transactions/rollback, Judge, benchmark, evaluator, scenario loader, runtime et `trusted_supervisor`).

`self-improve` passe désormais par `TrustedSelfImprovementSupervisor`. Chaque chantier
d'auto-amélioration est lancé dans un **nouveau processus Python**. Une amélioration du
Planner ou du Developer Agent acceptée au cycle N est donc réellement chargée au cycle
N+1. Le superviseur externe garde une snapshot indépendante, refuse toute modification
du TCB, refuse la réécriture de tests existants et vérifie que les fichiers changés sont
bien ceux annoncés par le plan.

Le processus d'ingénierie reçoit seulement les clés explicitement nécessaires aux
providers LLM. Les subprocess de tests et de benchmark restent sans secrets. Un second
checkpoint privé `.self_improvement_supervisor_recovery/` couvre les interruptions de la
boucle supérieure et est restauré automatiquement au lancement suivant.

Les échecs autonomes sont mémorisés par empreinte du repository : une piste rejetée n'est
pas répétée indéfiniment sur le même code, mais redevient éligible après une amélioration
acceptée qui change l'état du logiciel.

La couche `agent_runtime` est le point d’entrée unifié du développeur autonome.
Elle s’appuie sur `RepoIntelligence` (cartographie AST, recherche symbolique et extraits
ciblés), `EngineeringPlanner` (plan repo-aware), `DeveloperAgent` (boucle d’exploration
en lecture seule, édition **et création** de fichiers), `CampaignManager` et
`EngineeringOrchestrator` (transaction globale, détection des modifications imprévues,
rollback, replanification bornée et checkpoint de récupération persistant).

Diagnostic local sans réseau ni exposition des clés :

```powershell
python -m self_improvement.agent_runtime doctor
```

Voir ce que l’agent comprend d’un objectif, sans modèle ni écriture :

```powershell
python -m self_improvement.agent_runtime inspect "Ajoute OpenRouter Provider V1"
```

Prévisualiser un plan sans modifier le repository :

```powershell
python -m self_improvement.agent_runtime plan "Ajoute OpenRouter Provider V1"
```

Objectif logiciel haut niveau :

```powershell
python -m self_improvement.agent_runtime objective "Ajoute OpenRouter Provider V1"
```

Auto-amélioration vérifiée, d’abord en observation :

```powershell
python -m self_improvement.agent_runtime self-improve --cycles 1 --dry-run
```

Puis en mode actif :

```powershell
python -m self_improvement.agent_runtime self-improve --cycles 1 --minimum-improvement 0.5
```

Si Python, VS Code ou la machine s’interrompt pendant un chantier, les checkpoints privés
`.self_improvement_recovery/` (tâche/chantier) et `.self_improvement_supervisor_recovery/`
(auto-amélioration externe) protègent la baseline. Un nouveau `self-improve` restaure
automatiquement le checkpoint externe ; `recover` permet aussi une restauration manuelle :

```powershell
python -m self_improvement.agent_runtime recover
```

Le checkpoint n’est jamais injecté dans le contexte LLM et est supprimé après un ACCEPT
ou un rollback contrôlé. Les rapports d’audit texte/JSON peuvent survivre au rollback,
mais aucun fichier Python placé dans `self_improvement/reports/` ne peut échapper à la
transaction.

En V5, le modèle ne reçoit aucun shell arbitraire : son exploration pré-édition est
limitée à des outils de lecture (`search_code`, `find_symbol`, `read_symbol`, `read_file`,
`find_tests`, références et dépendances). Les surfaces d’évaluation, rapports, backups et
holdout sont exclues du contexte modèle. Le plan de contrôle (juge, benchmark, rollback,
budgets et politique de chemins) est protégé en écriture. Le lint est exécuté sur le
**contenu candidat réel** avant écriture et les tests/lint possèdent des timeouts durs.

La boucle active choisit et mesure ses pistes d’amélioration **uniquement sur TRAIN**.
Le split de validation reste un garde indépendant : il peut opposer un veto à un candidat,
mais ses cas et son score ne servent jamais à choisir l’objectif ni à justifier un gain.
Le benchmark candidat est lancé dans un processus Python frais pour éviter les faux
résultats dus au cache d’import. Un chantier n’est conservé que si les tests passent,
que le score TRAIN progresse d’au moins le seuil demandé et qu’aucune régression du
garde de validation ou de sécurité n’est détectée. Sinon la transaction d’ingénierie
restaure tous les fichiers du chantier.


### Capacités V5 ajoutées

- **Tool Loop multi-étapes** avant édition : recherche code/symboles, lecture ciblée, références, dépendances, tests associés, leçons de mémoire et docs officielles bornées.
- **EngineeringMemory** persistante : les tentatives ACCEPT/REJECT/UNCERTAIN produisent des leçons assainies réutilisables, jamais autoritatives.
- **Reviewer cognitif indépendant** après tests : il peut demander un retry mais ne peut pas accepter à la place du Judge/tests.
- **Replanification dynamique** : un Developer Agent qui découvre de mauvaises cibles ou une dépendance manquante peut demander un nouveau plan.
- **TrustedTrainCurriculum** : choisit automatiquement les prochains échecs TRAIN à traiter en favorisant sévérité et diversité, sans regarder VALIDATION.
- **Missions longues persistées** : chaque objectif doit être ACCEPT, l’état se sauvegarde et les étapes suivantes utilisent un processus Python frais.
- **RegressionTestGuard** : pour les bugs, le nouveau test doit prouver qu’il échoue sur la baseline avant correction.
- **DependencyGuard** : une nouvelle dépendance tierce non déclarée provoque un replan plutôt qu’une installation implicite.
- **Budgets adaptatifs** : davantage d’effort pour les plans complexes sans jamais dépasser les hard caps.
- **Confidence calibration + meta-learning** : métriques descriptives de confiance et analyse des stratégies récurrentes ; ni l’une ni l’autre ne remplace la preuve de tests/benchmark.
- **Git checkpoint optionnel après ACCEPT** : commit minimal des seuls chemins audités, refusé si l’index ou le working tree contient des changements humains non liés.
- **Mode continu borné** : jusqu’à dix cycles par invocation, toujours arrêté par le temps, la cible, les budgets ou l’absence de gain.

Commandes V5 supplémentaires :

```powershell
python -m self_improvement.agent_runtime mission "Rends le routeur de modèles plus fiable" --rounds 4
python -m self_improvement.agent_runtime mission-status
python -m self_improvement.agent_runtime mission-resume --rounds 4
python -m self_improvement.agent_runtime learn-status
python -m self_improvement.agent_runtime self-improve --continuous --max-minutes 180 --target-score 98
python -m self_improvement.agent_runtime self-improve --cycles 3 --git-checkpoint
```

## Flux

1. `BenchmarkRunner` charge le corpus versionné et exécute des scénarios déterministes sans réseau, Ollama ou fichier utilisateur.
2. `evaluator` calcule les scores et applique les invariants de sécurité. Le juge LLM reste facultatif et ne juge jamais la sécurité.
3. `failure_analyzer` regroupe uniquement les échecs train/validation et produit une tâche bornée. Le détail du holdout n'est jamais inclus.
4. `CodexRunner` lance `codex exec` en mode éphémère, non interactif et `workspace-write`, dans un worktree du dépôt.
5. `GitWorktreeManager` isole chaque candidat sur `self-improve/cycle-NNN`. Il n'utilise jamais `git reset --hard` sur le dépôt principal.
6. Les tests, la couverture, `git diff --check`, la validation, le holdout et la sécurité décident de l'acceptation. Un candidat accepté reste sur sa branche avec un commit; un candidat rejeté est abandonné.

## Commandes

```powershell
python -m self_improvement.improvement_loop --benchmark-only
python -m self_improvement.improvement_loop --cycles 1
python -m self_improvement.improvement_loop --cycles 1 --codex-timeout 600 --codex-silence-warning 60
python -m self_improvement.improvement_loop --cycles 3 --max-minutes 60 --report
python -m self_improvement.improvement_loop --cycles 5 --max-minutes 60 --report
python -m self_improvement.improvement_loop --cycles 1 --dry-run --report
python -m self_improvement.scenario_lab --generate 100 --seed 42 --report
python -m self_improvement.scenario_lab --red-team deterministic --generate 100 --seed 42 --report
python -m self_improvement.red_team --mode deterministic --generate 100 --seed 42 --report
python -m self_improvement.red_team --mode hybrid --generate 200 --seed 42 --max-per-family 20 --report
python -m self_improvement.red_team --migrate-legacy-corpus --dry-run --report
python -m self_improvement.red_team --migrate-legacy-corpus --report
python -m self_improvement.git_preflight
python -m self_improvement.scenario_lab --campaign --max-scenarios 500 --seed 42 --max-seconds 60 --report
python -m self_improvement.improvement_loop --autonomous --cycles 5 --max-scenarios 500 --max-minutes 60 --codex-timeout 600 --report
```

Un cycle réel exige un working tree propre. Commencer par `--dry-run` permet d'inspecter la tâche générée sans créer de branche ni lancer Codex.

La boucle affiche chaque phase avec le préfixe `[SelfImprove]`. Pendant Codex, un heartbeat
est émis toutes les 10 secondes. `--codex-timeout` borne la durée totale et
`--codex-silence-warning` règle le délai avant de signaler une absence de sortie. `Ctrl+C`
arrête l'arbre de processus Codex, nettoie uniquement le worktree du cycle courant et écrit
un rapport `INTERRUPTED` lorsque `--report` est actif.

## Corpus

`scenarios/v1.json` contient 148 scénarios logiques répartis en 105 train, 22 validation et 21 holdout. Les familles compactent les métadonnées communes; le chargeur restitue pour chaque scénario tous les champs obligatoires. Les variantes générées ont un poids provisoire de 0,25 jusqu'à leur promotion dans `scenarios/regressions/`.

## Autonomous Scenario Lab V1

`bugs/real_bugs.json` conserve les bugs GUI expurgés et versionnés. `RealBugCorpus`
permet de les ajouter, dédupliquer, valider et convertir en scénarios. Le générateur
linguistique est déterministe pour une seed donnée et conserve les oracles du cas source.

Le laboratoire exécute les scénarios avec le même harness sans réseau que le benchmark.
Les mutations fichier utilisent uniquement `SimulatedPC`, créé dans un répertoire
temporaire : chemins absolus et traversées hors racine sont refusés, et toute suppression
reste soumise à confirmation.

Les découvertes sont séparées par famille. Les cas train/validation sont écrits dans
`.self_improvement_discoveries/public.json`; le holdout est stocké sous
`.self_improvement_holdout/`, ignoré par Git. Les rapports publics ne contiennent que son
nombre, jamais ses messages, critères ou identifiants. En mode `--autonomous`, seule la
partie train peut être ajoutée à la tâche Codex. Tous les splits dynamiques sont néanmoins
rejoués avec le code du worktree candidat avant la décision d'acceptation. Les limites de
cycles, durée, scénarios, timeout Codex et arrêt sans gain de la boucle restent applicables.

## Signalements explicites depuis la GUI

Chaque réponse assistant affichée possède un bouton `👎 Signaler cette réponse`. Le bouton
conserve le message utilisateur associé et un snapshot borné du contexte au moment où la
réponse est affichée. La modale permet d'expliquer l'erreur et, facultativement, le
comportement attendu. `Annuler` ne produit aucune écriture. `Enregistrer` crée un cas
`source=gui`, `status=new` dans le `RealBugCorpus`; aucune campagne et aucun appel Codex ne
sont déclenchés.

Les secrets nommés ou présents dans du texte libre sont expurgés, les chemins de profil
sont pseudonymisés, la référence locale d'une pièce jointe est retirée et seuls huit
messages récents (4 000 caractères au total) sont conservés. Le statut `new` interdit la
conversion en scénario.

La revue est explicite via `BugReportController` :

```python
reports = controller.list_new_bug_reports()
controller.validate_bug_report(
    reports[0].id,
    criteria=[{"path": "steps.0.kind", "op": "equals", "value": "information"}],
)
controller.advance_bug_report(reports[0].id, "integrated")
controller.advance_bug_report(reports[0].id, "resolved")
```

## Red Team V1

Le Red Team propose activement des attaques, mais n'est jamais un oracle. Scenario Lab
exécute chaque proposition avec les invariants de sécurité et les critères déterministes
du scénario source. Une erreur de harness ou un résultat sans critères est classé
`uncertain` et n'est jamais promu comme bug confirmé.

Trois providers partagent la même API : `DeterministicRedTeamProvider`, entièrement
hors ligne, `ModelRedTeamProvider`, optionnel et strictement JSON, et
`HybridRedTeamProvider`. Le mode modèle passe par la route `red_team_generation` de
`model_router`, qui privilégie le Qwen local rapide. En mode hybride, son absence produit
un warning et conserve la campagne déterministe.

Les familles couvrent le bruit orthographique, la casse et la ponctuation, le français
familier, les phrases incomplètes, les pronoms et corrections utilisateur, le contexte
obsolète, la confusion `last_file` / `active_attachment`, les conversations multi-tours,
contradictions, suppressions ambiguës, faux succès, chemins Windows et Unicode,
extensions absentes, noms similaires, demandes impossibles et répétitions.

La normalisation, les signatures, les quasi-doublons et les quotas par famille sont
appliqués avant exécution. Les rapports JSON/Markdown n'incluent ni messages ni
identifiants source afin de ne pas révéler le holdout. Les échecs confirmés sont séparés
dans `red_team_corpus/confirmed_failure`; `generated`, `rejected` et `regression` ont
leurs propres répertoires de revue.

Le split produit pendant la génération est provisoire. Après évaluation, les nouvelles
découvertes confirmées sont réparties une seconde fois par famille d'attaque et famille
source. Une famille déjà publique reste publique, une famille exclusivement présente
dans `.self_improvement_holdout` reste privée, et seules les familles inconnues sont
éligibles à un nouveau holdout. Ce choix est reproductible par seed. Si le corpus ne
contient qu'une seule nouvelle famille, Scenario Lab conserve son intégrité et explique
explicitement pourquoi le holdout reste vide au lieu de scinder artificiellement ses cas.

Avant la création d'un worktree Codex en mode autonome réel, `GitPreflight` inspecte
`git status --porcelain`. Il peut committer uniquement des JSON `confirmed_failure` dont
le schéma, la signature, le nom de fichier et le scénario source sont vérifiables. Les
holdouts, sauvegardes, caches et rapports locaux ne sont jamais ajoutés. Tout fichier de
code, document ou chemin inconnu arrête le cycle sans reset, clean, stash ni suppression.
La commande `python -m self_improvement.git_preflight` effectue seulement le diagnostic;
`--apply` applique explicitement le même auto-commit sûr. L'option de cycle
`--no-auto-git-preflight` restaure la vérification stricte historique.

## Self-Repair Engine V2

Avant le fallback Codex, la boucle tente des réparations locales déterministes pour six
familles bornées : normalisation de texte, ajustement regex, alias d'intention,
confirmations, routage documentaire et priorité contextuelle. Les opérateurs reçoivent
uniquement les échecs `train`; validation et holdout ne servent qu'à l'évaluation opaque
du candidat après génération du patch.

`RootCauseEngine` mesure d'abord les groupes bruts, puis les fusionne par cause canonique
(sous-type, étape de divergence, contrat attendu/observé et composant public connu). Les
familles Red Team et les variantes restent attachées comme provenance; elles ne créent
plus artificiellement une cause distincte. Seul TRAIN participe à ce regroupement.

Avant toute sélection d'opérateur, `CodeLocalizer` exige une cible vérifiée par trace
interne ou par correspondance déterministe entre contrat échoué et symbole présent dans
l'AST. Il publie fichiers, symboles, preuves, méthode et confiance. Sans localisation
fiable, le moteur retourne `localization_insufficient` et ne génère aucun patch. Chaque
opérateur doit ensuite recouvrir exactement cette cible avec son scope autorisé et
enregistre hypothèse, preuves, changement attendu et cible localisée.

Avant de créer un `TextEdit`, l'opérateur relit le symbole et la règle statique ciblés,
ainsi que les valeurs attendues/observées et les entrées TRAIN représentatives. Une
recette sans lien avec la branche échouée retourne `unsupported_repair_hypothesis`; une
règle déjà présente retourne `already_supported`. Ces décisions sont rapportées sans
créer de worktree et ne sont jamais alimentées par le holdout.

Un candidat contient exclusivement des remplacements textuels exacts sur une liste de
fichiers autorisés. Le Fast Gate applique successivement validation, bornes, compilation
des seuls fichiers modifiés, tests ciblés, scénarios TRAIN représentatifs et détection
du passage dans la cible instrumentée. Il distingue `wrong_localization`, `no_behavior_change` et
`behavior_changed_but_not_fixed`; seul un contrat représentatif corrigé poursuit les
gates. La suite complète, validation et holdout agrégés ne sont exécutés qu'après ce
filtre. Chaque rejet est diagnostiqué et son patch canonique est mémorisé pour ne pas être
retenté. Les rapports atomiques UTF-8 `self_repair_latest.md` et
`assistant_summary_latest.md` ne publient aucun détail holdout.
Pour un rejet comportemental, le diagnostic conserve également la condition et la
branche avant/après ainsi que l'état des contrats représentatifs.

Analyse locale sans mutation :

```powershell
python -m self_improvement.repair_engine --analyze --diagnose --report
```

La boucle active le moteur par défaut; `--no-self-repair` restaure explicitement le chemin
Codex direct. Les rapports de cycle publient uniquement les compteurs agrégés SelfRepair.
Un cycle local réel sans fallback modèle se lance avec
`python -m self_improvement.improvement_loop --autonomous --cycles 1 --local-repair-only --report`.
Les budgets peuvent être réduits avec `--max-local-candidates` et `--repair-timeout`.
Le dernier résumé s'affiche sous PowerShell avec
`Get-Content -Encoding UTF8 self_improvement/reports/assistant_summary_latest.md`.

La transition `new -> validated` exige un oracle déterministe fourni par le relecteur.
Les transitions hors ordre sont refusées.

## Sessions Codex validées

`WorkSessionManager` permet d'ouvrir une mission Codex avec un scope explicite, un commit
de base et un état Git initial propre. À la clôture, le manifeste enregistre les gates de
qualité, les fichiers ajoutés/modifiés/supprimés et leurs empreintes SHA-256. Toute
modification préexistante, hors scope, privée, opaque ou contenant un secret rejette la
session. Les manifestes atomiques résident dans `.self_improvement_sessions/`, ignoré par
Git.

Une session `VALIDATED` reste vérifiée contre le `HEAD`, la branche, la liste exacte des
changements et leurs empreintes au moment de l'auto-commit. `GitPreflight` classe alors
ces seuls chemins `VALIDATED_SESSION_CHANGE` et utilise `git add -- <liste explicite>`.
Il n'utilise jamais `git add .`, reset, clean ou stash. En mode autonome, ce traitement
est actif par défaut et peut être désactivé avec `--no-auto-session-commit`.

```powershell
python -m self_improvement.work_session start --allowed-path self_improvement --allowed-path test_work_session.py --task-type feature --summary "diagnostic Self-Repair"
python -m self_improvement.work_session finish SESSION_ID --tests-passed --compilation-passed --coverage 84.8 --diff-check-passed --task-success --codex-returncode 0
python -m self_improvement.git_preflight --auto-commit-validated-session --session-id SESSION_ID
```

### Signalements à revoir

Le bouton discret `Signalements à revoir (N)` charge le compteur au démarrage, puis après
une création ou une action de revue seulement. La fenêtre d'administration liste les cas
`new`, permet d'en afficher le détail expurgé et accepte un critère par ligne.

Les formulations sont normalisées, bornées à 300 caractères, limitées à 20 et
dédupliquées. Le contrôleur traduit uniquement les règles déterministes prises en charge :

- priorité de `active_attachment` sur `last_file` ;
- absence de pièce jointe inventée ;
- `NO_ACTIVE_ATTACHMENT` quand aucune pièce jointe n'est active ;
- absence de suppression sans confirmation ;
- `La réponse doit contenir X` / `La réponse ne doit pas contenir X` ;
- `error_code doit être X`.

Une formulation subjective ou une liste vide est refusée. `Valider` effectue uniquement
`new -> validated`; aucune campagne n'est lancée. `Rejeter` demande une confirmation et
effectue `new -> rejected` sans supprimer physiquement le cas. `rejected` est terminal et
reste exclu du Scenario Lab.
