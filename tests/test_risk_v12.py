from dataclasses import dataclass
from nova_api.risk import RiskEngine


@dataclass
class Cap:
    id: str
    risk_level: str = "low"
    reversible: bool = True
    requires_confirmation: bool = False
    category: str = "computer"


def test_desktop_mutation_is_contextually_elevated_without_confirmation_spam():
    result = RiskEngine().assess(Cap("computer.ui.invoke"), {"element_ref": "opaque"})
    assert result.level == "medium"
    assert result.requires_confirmation is False
    assert "desktop_state_mutation" in result.reasons


def test_secret_like_ui_input_requires_confirmation():
    result = RiskEngine().assess(Cap("computer.ui.set_value"), {"element_ref": "opaque", "value": "api_key=sk-abcdefghijklmnop"})
    assert result.level == "high"
    assert result.requires_confirmation is True


def test_sensitive_file_target_is_high_risk():
    cap = Cap("filesystem.write", risk_level="medium", reversible=True, requires_confirmation=True, category="filesystem")
    result = RiskEngine().assess(cap, {"path": ".env", "content": "X=1"})
    assert result.level == "high"
    assert result.requires_confirmation is True


def test_observation_stays_low_risk():
    result = RiskEngine().assess(Cap("computer.ui.inspect"), {"window_ref": "opaque"})
    assert result.level == "low"
    assert result.requires_confirmation is False


def test_sensitive_application_context_requires_confirmation():
    result = RiskEngine().assess(
        Cap("computer.ui.invoke"), {"element_ref": "opaque"},
        context={"application": "powershell.exe", "window_title": "Administrator: PowerShell", "target_name": "Run"},
    )
    assert result.level == "high"
    assert result.requires_confirmation is True
    assert "sensitive_application" in result.reasons


def test_high_impact_target_name_is_high_risk_even_in_generic_app():
    result = RiskEngine().assess(
        Cap("computer.ui.invoke"), {"element_ref": "opaque"},
        context={"application": "browser.exe", "window_title": "Checkout", "target_name": "Pay now", "target_role": "button"},
    )
    assert result.level == "high"
    assert result.requires_confirmation is True
    assert "high_impact_target" in result.reasons


def test_ambiguous_target_fails_safe_to_high_risk():
    result = RiskEngine().assess(
        Cap("computer.ui.invoke"), {"element_ref": "stale"}, context={"ambiguous": True},
    )
    assert result.level == "high"
    assert result.requires_confirmation is True

def test_risk_v2_escalates_irreversible_external_side_effect():
    cap = Cap("computer.ui.invoke", risk_level="medium", reversible=False)
    result = RiskEngine().assess(cap, {"element_ref": "opaque"}, context={"external_side_effect": True})
    assert result.level == "critical" and result.requires_confirmation

def test_risk_v2_combines_high_risk_and_high_uncertainty():
    cap = Cap("computer.ui.invoke", risk_level="high", reversible=False)
    result = RiskEngine().assess(cap, {"element_ref": "opaque"}, context={"uncertainty_level": "high"})
    assert result.level == "critical" and "high_risk_under_uncertainty" in result.reasons
