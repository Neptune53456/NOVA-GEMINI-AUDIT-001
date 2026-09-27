# Règles du frontend

Pour une tâche frontend isolée, le périmètre est `frontend/**` par défaut. Ne pas l'élargir sans nécessité explicite et replanification.

- Ne jamais lire `.runtime`, les caches, télémétries, rapports, fichiers ZIP, historiques, surfaces privées ou fichiers Python pour une tâche frontend isolée.
- Utiliser React et TypeScript conformément aux conventions existantes.
- Créer des composants courts, spécialisés et testables ; éviter les composants monolithiques et la duplication.
- Ne placer aucune logique métier autoritative dans le frontend.
- Le backend reste autoritaire sur les états, permissions, validations et confirmations. L’interface reflète les décisions du backend sans les contourner. Les mocks sont autorisés uniquement pour le développement et les tests, et doivent être explicitement identifiés comme fictifs.
- Utiliser `npm run build` et `npm run lint` comme validations normales d'une modification frontend.
- Ne lancer aucun `pytest` pour une tâche frontend isolée.
- Ne pas ajouter de dépendance, moteur 3D ou nouvelle infrastructure sans demande explicite.

Pour une modification documentaire seulement, ne lancer ni build ni lint. Respecter les règles de sécurité et de preuve du fichier `AGENTS.md` racine.
