# God Eyes V3 — Phase 1

God Eyes est une fondation de données locale intégrée à `nova_api`. Elle normalise les cotations,
les métadonnées de qualité et les actualités, puis les conserve dans SQLite. Le cache mémoire est
borné et chaque provider échoue isolément.

## Providers et sécurité

- Crypto : Coinbase Exchange public, puis Kraken en repli.
- Actions, indices, ETF et, quand Yahoo les expose proprement, FX/commodities : Yahoo Finance public.
- Actualités : adaptateur RSS/Atom multi-source ; les sources sont injectées par configuration.
- Réseau : HTTPS et ports standards seulement, allowlist d'hôtes, rejet des adresses non publiques,
  redirects revalidés et bornés, timeout et taille de réponse maximale. Aucun secret ni broker.

L'univers par défaut est un exemple diversifié. Il est remplaçable intégralement en injectant des
`MarketInstrument`; les symboles propres à chaque provider sont portés par la configuration de
l'instrument et non par la logique d'orchestration.

## API

- `GET /api/v1/god-eye/market`
- `GET /api/v1/god-eye/news`
- `GET /api/v1/god-eye/health`
- `POST /api/v1/god-eye/refresh`

Lancer les tests ciblés : `python -m pytest tests/test_god_eye.py test_nova_api.py -q --no-cov`.

## Limites de phase 1

Pas de scheduler, broker, paper trading, scoring probabiliste ni prévision LLM. Les tables candles,
events et forecasts préparent les phases suivantes ; leur ingestion métier n'est pas encore exposée.
La Phase 2 ajoute les candles, la configuration structurée de sources officielles, le scheduler
interne désactivé par défaut et l'Event Intelligence déterministe. Le scheduler est pilotable par
le service et observable via `GET /api/v1/god-eye/scheduler`.
