"""Base classes for provider implementations.

This module defines the minimal abstract interface that concrete provider
implementations must follow.  It is deliberately lightweight and has no
runtime dependencies on external services, making it suitable for unit
testing without network access.
"""

from __future__ import annotations

import abc
from typing import Any


class ProviderError(Exception):
    """Base exception type for all provider‑related errors.

    Concrete providers should raise subclasses of this exception (or the
    exception itself) to signal failures such as mis‑configuration, lack of
    required resources, or runtime problems.
    """

    pass


class BaseProvider(abc.ABC):
    """Abstract base class that all providers must inherit from.

    A provider is a thin wrapper around an external service (e.g. an LLM
    endpoint, a database client, etc.).  The :meth:`available` method should
    return ``True`` when the underlying service can be reached or when the
    provider is otherwise ready for use.  The :meth:`call` method performs the
    actual operation and must be implemented by subclasses.
    """

    @abc.abstractmethod
    def available(self) -> bool:
        """Return ``True`` if the provider can be used.

        Implementations should perform any lightweight health‑check required
        to determine readiness.  The method must be side‑effect free and fast
        because it may be called repeatedly by higher‑level orchestration code.
        """

    @abc.abstractmethod
    def call(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the provider's primary operation.

        Sub‑classes receive arbitrary positional and keyword arguments and
        should return the result of the operation.  Any error conditions must
        raise :class:`ProviderError` (or a subclass) so that callers can handle
        them uniformly.
        """

__all__ = ["BaseProvider", "ProviderError"]
