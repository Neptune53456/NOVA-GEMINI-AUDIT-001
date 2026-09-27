# Interface locale Nova

L’API et l’interface s’exécutent dans deux terminaux distincts, liés uniquement à l’adresse locale.

## Terminal 1 — API avec rechargement automatique

```powershell
python -m uvicorn nova_api.app:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

## Terminal 2 — interface avec HMR

```powershell
cd frontend
npm.cmd run dev
```

- Interface : http://localhost:5173
- Cockpit : accessible depuis la navigation principale
- Santé de l’API : http://127.0.0.1:8000/api/v1/health
- État de Nova : http://127.0.0.1:8000/api/v1/state
- Instantané du cockpit : http://127.0.0.1:8000/api/v1/cockpit

En développement, Vite redirige les requêtes relatives `/api` vers l’API locale sur `127.0.0.1:8000`. Les modifications React et CSS apparaissent automatiquement grâce au HMR de Vite. Les modifications Python redémarrent automatiquement l’API grâce à l’option `--reload`.

Les deux terminaux doivent rester ouverts. Si `Ctrl+C` ne parvient pas à arrêter un processus, fermer le terminal arrête aussi ce processus. Tous les services restent liés à l’interface locale ; ne pas remplacer `127.0.0.1` par `0.0.0.0`.
