"""Safe Windows launcher for Nova's loopback API and Vite interface."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
FRONTEND = ROOT / "frontend"
API_URL = "http://127.0.0.1:8000/api/v1/health"
UI_URL = "http://localhost:5173"
START_TIMEOUT_SECONDS = 30.0


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def url_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def build_commands() -> tuple[list[str], list[str]]:
    api = [str(PYTHON), "-m", "uvicorn", "nova_api.app:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000"]
    frontend = ["npm.cmd", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173", "--strictPort"]
    return api, frontend


def main() -> int:
    if not PYTHON.is_file():
        print("Environnement virtuel manquant : .venv\\Scripts\\python.exe")
        return 1
    if not (FRONTEND / "package.json").is_file() or not (FRONTEND / "node_modules").is_dir():
        print("Frontend incomplet : package.json ou node_modules manquant.")
        return 1
    api_ready, ui_ready = url_ready(API_URL), url_ready(UI_URL)
    if api_ready and ui_ready:
        print("Nova est déjà démarrée.")
        webbrowser.open(UI_URL)
        return 0
    if port_open(8000) or port_open(5173):
        print("Un port requis est occupé par un autre service (8000 ou 5173).")
        return 1
    api_command, frontend_command = build_commands()
    print("Démarrage de l’API Nova et de l’interface locale…")
    processes = [
        subprocess.Popen(api_command, cwd=ROOT, shell=False),
        subprocess.Popen(frontend_command, cwd=FRONTEND, shell=False),
    ]
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("Un composant Nova s’est arrêté pendant le démarrage.")
            if url_ready(API_URL) and url_ready(UI_URL):
                print(f"Nova est prête : {UI_URL}")
                webbrowser.open(UI_URL)
                print("Gardez cette fenêtre ouverte. Utilisez Ctrl+C pour arrêter Nova.")
                while all(process.poll() is None for process in processes):
                    time.sleep(0.25)
                raise RuntimeError("Un composant Nova s'est arrêté.")
            time.sleep(0.25)
        raise RuntimeError("Délai de démarrage dépassé.")
    except KeyboardInterrupt:
        print("Arrêt de Nova…")
        return_code = 0
    except RuntimeError as error:
        print(error)
        return_code = 1
    else:
        return_code = 0
    finally:
        for process in processes:
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill()
    return return_code


if __name__ == "__main__":
    sys.exit(main())
