import pytest

from providers.base import BaseProvider, ProviderError
from providers.example_provider import ExampleProvider


def test_example_provider_is_subclass_of_base():
    """The provider must inherit from BaseProvider."""
    provider = ExampleProvider()
    assert isinstance(provider, BaseProvider)


def test_available_returns_boolean():
    """The ``available`` method should always return a boolean value.

    The exact value (True/False) may depend on the environment, but the type
    must be ``bool`` so that callers can rely on a deterministic truthy/falsy
    contract.
    """
    provider = ExampleProvider()
    result = provider.available()
    assert isinstance(result, bool)


def test_call_returns_dict_for_valid_input():
    """Calling the provider with a valid mapping should return a ``dict``.

    The concrete content of the dictionary is provider‑specific; the test only
    asserts that the return type is correct and that no exception is raised.
    """
    provider = ExampleProvider()
    # A minimal, valid payload – the provider is expected to accept any mapping.
    payload = {"key": "value"}
    result = provider.call(payload)
    assert isinstance(result, dict)


def test_call_raises_provider_error_for_invalid_input():
    """The provider must raise ``ProviderError`` when given an unsupported input.

    Supplying a non‑mapping (e.g., a string) is considered invalid for the
    ``ExampleProvider`` contract and should trigger a deterministic error.
    """
    provider = ExampleProvider()
    with pytest.raises(ProviderError):
        provider.call("invalid payload")
