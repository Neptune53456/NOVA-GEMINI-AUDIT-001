"""Client OpenAI-compatible minimal pour l'instance OmniRoute locale.

Le client ne conserve jamais de credentials et accepte un transport ``httpx``
injectable afin que la decouverte et le failover restent testables hors reseau.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Any, Iterable, Mapping

import httpx

from .provider_manager import supports_structured_output_with_tools


DEFAULT_OMNIROUTE_BASE_URL = "http://127.0.0.1:20128/v1"


@dataclass(frozen=True)
class OmniRouteStatus:
    reachable: bool
    base_url: str
    latency_ms: int | None
    models_count: int
    error_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "base_url": self.base_url,
            "latency_ms": self.latency_ms,
            "models_count": self.models_count,
            "error_kind": self.error_kind,
        }


class OmniRouteProvider:
    """Acces borne a ``/models`` et ``/chat/completions`` sans log secret."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 5.0,
        client: Any | None = None,
    ) -> None:
        configured = base_url or os.environ.get("OMNIROUTE_BASE_URL") or os.environ.get("OMNIROUTE_API_BASE")
        self.base_url = str(configured or DEFAULT_OMNIROUTE_BASE_URL).rstrip("/")
        self._api_key = api_key if api_key is not None else os.environ.get("OMNIROUTE_API_KEY", "")
        self.timeout_seconds = max(0.1, min(float(timeout_seconds), 120.0))
        self._client = client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        request = self._client.request if self._client is not None else httpx.request
        response = request(
            method,
            self.base_url + path,
            headers=self._headers(),
            timeout=self.timeout_seconds,
            **kwargs,
        )
        response.raise_for_status()
        return response

    def list_models(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/models").json()
        raw_models = payload.get("data", []) if isinstance(payload, Mapping) else []
        return [dict(item) for item in raw_models if isinstance(item, Mapping) and item.get("id")]

    def chat(
        self,
        *,
        model: str,
        messages: Iterable[Mapping[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": list(messages), "stream": False}
        if tools is not None:
            payload["tools"] = tools
        if response_format is not None and (
            not tools or supports_structured_output_with_tools("omniroute")
        ):
            payload["response_format"] = response_format
        if options and "temperature" in options:
            payload["temperature"] = options["temperature"]
        data = self._request("POST", "/chat/completions", json=payload).json()
        if not isinstance(data, Mapping):
            raise ValueError("omniroute_invalid_response")
        return dict(data)

    def status(self) -> OmniRouteStatus:
        started = time.perf_counter()
        try:
            models = self.list_models()
            return OmniRouteStatus(True, self.base_url, int((time.perf_counter() - started) * 1000), len(models))
        except httpx.TimeoutException:
            kind = "TIMEOUT"
        except httpx.RequestError:
            kind = "NETWORK_ERROR"
        except httpx.HTTPStatusError as exc:
            kind = f"HTTP_{exc.response.status_code}"
        except Exception:
            kind = "RUNTIME_ERROR"
        return OmniRouteStatus(False, self.base_url, int((time.perf_counter() - started) * 1000), 0, kind)
