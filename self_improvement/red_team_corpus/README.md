# Corpus Red Team

Ce corpus versionnable sépare les attaques selon leur état de revue. Un provider ne peut
jamais confirmer lui-même un bug : seul un résultat évalué par les invariants et oracles
déterministes peut entrer dans `confirmed_failure`.

- `generated/` : propositions valides conservées par une revue explicite ;
- `confirmed_failure/` : échecs déterministes confirmés et dédupliqués ;
- `rejected/` : propositions invalidées par la revue ;
- `regression/` : cas promus comme tests permanents.

Les campagnes ordinaires ne conservent pas indéfiniment les propositions qui passent.

## Migration legacy

Les anciens cas publics peuvent être audités puis migrés explicitement, sans modèle ni
Codex :

```powershell
python -m self_improvement.red_team --migrate-legacy-corpus --dry-run --report
python -m self_improvement.red_team --migrate-legacy-corpus --report
```

La migration reconstruit la famille uniquement lorsque `family_id` et le scénario source
sont encore vérifiables. Sinon, le cas reçoit `legacy-status:legacy_public` et reste
définitivement public. Aucun cas legacy n'est déplacé vers le holdout. Les originaux sont
copiés dans `backups/legacy-migration-<date>/` avant remplacement. Relancer la commande
est sans effet lorsque le corpus est déjà migré.
