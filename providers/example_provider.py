from __future__ import annotations

from typing import Any

from .base import BaseProvider, ProviderError


class ExampleProvider(BaseProvider):
    """A deterministic example provider that does not perform real network calls.

    The provider is always reported as :pymeth:`available` and its :pymeth:`call`
    method simply validates the input and returns a shallow copy of the payload
    with an additional ``"processed": True`` entry.  Invalid payloads raise
    :class:`ProviderError`.
    """

    def available(self) -> bool:
        """Return ``True`` indicating the provider can be used.
        """
        return True

    def call(self, payload: Any) -> Any:
        """Process *payload* deterministically.

        Parameters
        ----------
        payload: Any
            Expected to be a ``dict``.  If it is not, a :class:`ProviderError` is
            raised.

        Returns
        -------
        Any
            A shallow copy of the original ``payload`` with an added key
            ``"processed"`` set to ``True``.
        """
        if not isinstance(payload, dict):
            raise ProviderError("Payload must be a dict")
        result = payload.copy()
        result["processed"] = True
        return result
