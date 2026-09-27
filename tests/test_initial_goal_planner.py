import json

import pytest
from fastapi.testclient import TestClient

from nova_api.app import create_app
from nova_api.autonomy import GoalRunner, GoalStore
from nova_api.capabilities import build_default_registry
from nova_api.context_builder import ContextBuilder
from nova_api.conversation_service import ConversationService
from nova_api.initial_goal_planner import (
    InitialGoalPlanner, InitialPlanInvalid, extract_step_array, is_goal_intent,
    structural_fingerprint,
)
from nova_api.journal import EventJournal
from nova_api.memory_store import MemoryStore
from nova_api.state import ApiStateStore


def _response(steps, success=None):
    payload = {"steps": steps}
    if success is not None:
        payload["success_criteria"] = success
    return {"message": {"content": json.dumps(payload)}}


def _step(capability_id, arguments, *, objective="perform bounded action", evidence="verified capability result",
          family="filesystem", risk="low", reversible=True):
    return {"expected_evidence": evidence, "capability_id": capability_id, "arguments": arguments}


def _stack(tmp_path, replies):
    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    captured = []

    def chat_fn(**kwargs):
        captured.append(kwargs)
        return replies.pop(0)

    initial = InitialGoalPlanner(registry, chat_fn=chat_fn)
    runner = GoalRunner(registry, journal, ContextBuilder(memory), memory,
                        store=GoalStore(tmp_path / "goals.sqlite3"), initial_planner=initial)
    return runner, captured


def test_goal_intent_is_conservative():
    assert is_goal_intent("Diagnose why demo does not start and fix it.")
    assert is_goal_intent("Crée un fichier temporaire puis vérifie son contenu.")
    assert not is_goal_intent("Salut Nova")
    assert not is_goal_intent("C'est quoi Git ?")


def test_structured_transport_failure_has_planner_taxonomy(tmp_path):
    from model_router import ModelRouterError

    journal = EventJournal(tmp_path / "events.sqlite3")
    registry = build_default_registry(journal, project_root=tmp_path)

    def unavailable(**_kwargs):
        raise ModelRouterError(
            "no_capable_provider", "safe",
            details={"required_capabilities": ["structured_output"], "attempt_history": []},
        )

    planner = InitialGoalPlanner(registry, chat_fn=unavailable)
    context = ContextBuilder(MemoryStore(tmp_path / "memory.sqlite3")).build("objective")
    with pytest.raises(InitialPlanInvalid, match="NO_STRUCTURED_PLANNER_PROVIDER") as captured:
        planner.plan("Create a temporary file then verify it", context)

    assert captured.value.stage == "initial_goal_planning"
    assert captured.value.transport_diagnostics == []


def test_initial_plan_uses_context_and_validates_capabilities(tmp_path):
    runner, calls = _stack(tmp_path, [_response([
        _step("project.basic_info", {}, objective="inspect project", family="project", reversible=True),
        _step("filesystem.list", {"path": "."}, objective="inspect files"),
    ], "startup cause is identified with verified observations")])
    runner.memory.remember(memory_type="PROJECT_STATE", source_type="test", provenance="DETERMINISTIC",
                           subject="demo startup", content="demo startup uses demo.conf", importance=8)
    goal = runner.create_from_objective("conversation", "Diagnose why the demo project startup does not start and fix it")
    assert goal.status == "pending" and goal.model_calls == 1
    assert goal.success_criteria == "All planned capability steps pass authoritative backend verification."
    assert len(goal.plan) == 2
    assert calls[0]["task_type"] == "initial_planning"
    assert calls[0]["required_capabilities"] == {"structured_output"}
    assert "demo startup uses demo.conf" in calls[0]["messages"][0]["content"]


def test_invalid_plan_gets_only_one_correction_attempt(tmp_path):
    invalid_step = {**_step("filesystem.read", {"path": "note.txt"}),
                    "unrecognized_mechanic": {"content": "ready"}}
    invalid = _response([invalid_step])
    valid = _response([_step("filesystem.list", {"path": "."})],
                      "the allowed workspace directory listing is observed")
    runner, calls = _stack(tmp_path, [invalid, valid])
    goal = runner.create_from_objective("conversation", "Verify the project then report")
    assert goal.model_calls == 2 and len(calls) == 2
    repair_prompt = calls[1]["messages"][0]["content"]
    assert '"reason_codes":["UNKNOWN_STEP_FIELD"]' in repair_prompt
    assert '"invalid_fields":["unrecognized_mechanic"]' in repair_prompt
    assert "complete corrected plan, not a patch" in repair_prompt
    assert calls[1]["format"] == calls[0]["format"]


def test_invalid_plan_is_bounded_after_repair(tmp_path):
    invalid = _response([_step("unknown.capability", {})] * 30)
    runner, calls = _stack(tmp_path, [invalid, invalid])
    try:
        runner.create_from_objective("conversation", "Verify the project then report")
    except InitialPlanInvalid as error:
        assert str(error) == "PLAN_INVALID"
    else:
        raise AssertionError("invalid plan must be rejected")
    assert len(calls) == 2


def test_final_rejection_preserves_safe_stage_and_fingerprint(tmp_path):
    invalid = {"message": {"content": "[]"}}
    runner, _calls = _stack(tmp_path, [invalid])
    runner.initial_planner.max_attempts = 1
    with pytest.raises(InitialPlanInvalid, match="PLAN_INVALID") as captured:
        runner.create_from_objective("conversation", "Inspect the workspace")
    assert captured.value.reason_code == "INVALID_STEP_COLLECTION"
    assert captured.value.stage == "planner_top_level_dto"
    assert captured.value.structural_fingerprint["top_level_type"] == "array"
    rejected = next(event for event in runner.journal.recent() if event.type == "goal.plan.rejected")
    assert rejected.structural_fingerprint["reason_code"] == "INVALID_STEP_COLLECTION"


def test_objective_only_api_and_confirmation_to_verified_completion(tmp_path):
    steps = [
        _step("filesystem.write", {"path": "nova_goal_test.txt", "content": "HELLO_NOVA_7391"},
              objective="write temporary proof", risk="medium"),
        _step("filesystem.read", {"path": "nova_goal_test.txt"}, objective="verify exact content"),
    ]
    runner, _calls = _stack(tmp_path, [_response(steps, "file contains HELLO_NOVA_7391")])
    client = TestClient(create_app(goal_runner=runner, memory_store=runner.memory,
                                   capability_registry=runner.registry, journal=runner.journal))
    created = client.post("/api/v1/goals", json={"conversation_id": "c", "objective":
                          "Create a temporary file with X and verify it."})
    assert created.status_code == 201 and created.json()["model_calls"] == 1
    waiting = client.post(f"/api/v1/goals/{created.json()['goal_id']}/resume").json()
    assert waiting["status"] == "awaiting_confirmation"
    completed = client.post(f"/api/v1/goals/{created.json()['goal_id']}/resume",
                            json={"token": waiting["plan"][0]["confirmation"]["token"],
                                  "approved": True}).json()
    assert completed["status"] == "completed_verified"
    assert (tmp_path / "nova_goal_test.txt").read_text(encoding="utf-8") == "HELLO_NOVA_7391"


def test_exact_lucas_request_reaches_confirmation_with_canonical_safe_plan(tmp_path):
    request = ("Crée dans l'espace de test temporaire un fichier nova_goal_test.txt "
               "contenant HELLO_NOVA_7391, puis vérifie que le contenu est correct.")
    steps = [
        _step("filesystem.write", {"path": ".\\nova_goal_test.txt", "content": "HELLO_NOVA_7391"},
              objective="write requested temporary file", evidence="transaction verifies final hash"),
        _step("filesystem.read", {"path": "nova_goal_test.txt"}, objective="read back exact content",
              evidence="read content equals requested text"),
    ]
    success = ("nova_goal_test.txt exists in the allowed workspace and its content "
               "equals HELLO_NOVA_7391")
    runner, calls = _stack(tmp_path, [_response(steps, "file created successfully")])
    client = TestClient(create_app(goal_runner=runner, memory_store=runner.memory,
                                   capability_registry=runner.registry, journal=runner.journal))
    conversation_id = client.post("/api/v1/conversations", json={}).json()["conversation_id"]
    streamed = client.post(f"/api/v1/conversations/{conversation_id}/messages/stream",
                           json={"content": request})
    assert "event: goal.awaiting_confirmation" in streamed.text
    assert "PLAN_INVALID" not in streamed.text
    goal = runner.store.list(conversation_id=conversation_id)[0]
    assert goal.status == "awaiting_confirmation"
    assert [step["capability_id"] for step in goal.plan] == ["filesystem.write", "filesystem.read"]
    assert goal.plan[0]["arguments"]["path"] == "nova_goal_test.txt"
    assert goal.plan[1]["verification"] == {"content": "HELLO_NOVA_7391"}
    assert {"step_id", "status", "risk", "reversible", "confirmation_token", "confirmation"} <= set(
        goal.plan[0])
    assert all(field not in steps[0] for field in
               ("step_id", "status", "risk", "reversible", "plan_version", "goal_id"))
    assert goal.success_criteria == success
    assert len(calls) == 1
    event_types = [event.type for event in reversed(runner.journal.recent(limit=30))]
    expected = ["goal.intent.detected", "goal.planning.started", "goal.started",
                "goal.plan.created", "goal.step.started"]
    positions = [event_types.index(event) for event in expected]
    assert positions == sorted(positions)


def test_canonical_plan_roundtrip_preserves_semantics(tmp_path):
    steps = [
        _step("filesystem.write", {"path": "proof.txt", "content": "exact"}),
        _step("filesystem.read", {"path": "proof.txt"}),
    ]
    runner, _calls = _stack(tmp_path, [])
    planner = runner.initial_planner
    success, canonical = planner._validate({"success_criteria": "ignored by deterministic derivation",
                                            "steps": steps})
    serialized = [step.semantic() for step in canonical]
    goal = runner.create("conversation", "Create and verify proof", success, serialized)
    assert [{key: step[key] for key in serialized[0]} for step in goal.plan] == serialized
    assert goal.plan[1]["verification"] == {"content": "exact"}


@pytest.mark.parametrize(("content", "reason"), [
    ('{"plan":{"success_criteria":"x","steps":[]}}', "INVALID_TOP_LEVEL_SCHEMA"),
    ('{"success_criteria":"x"}', "UNKNOWN_TOP_LEVEL_FIELD"),
    ('{"success_criteria":"x","steps":[]} trailing', "INVALID_TOP_LEVEL_SCHEMA"),
    ('{"success_criteria":"x","steps":[]} {"second":true}', "INVALID_TOP_LEVEL_SCHEMA"),
])
def test_strict_json_and_top_level_schema_reject_ambiguous_payloads(tmp_path, content, reason):
    runner, calls = _stack(tmp_path, [
        {"message": {"content": content}}, {"message": {"content": content}},
    ])
    with pytest.raises(InitialPlanInvalid) as captured:
        runner.create_from_objective("conversation", "Create and verify a temporary file")
    assert captured.value.reason_code == reason
    assert len(calls) == 2


def test_whole_json_code_fence_is_accepted(tmp_path):
    payload = _response([_step("filesystem.list", {"path": "."})],
                        "the workspace listing is observed")["message"]["content"]
    runner, _calls = _stack(tmp_path, [{"message": {"content": f"```json\n{payload}\n```"}}])
    assert runner.create_from_objective("conversation", "Inspect workspace").status == "pending"


@pytest.mark.parametrize(("payload", "reason"), [
    ({"success_criteria": "specific", "steps": []}, "INVALID_STEP_COLLECTION"),
    ({"success_criteria": "specific", "steps": [_step("filesystem.list", {"path": "."})] * 9},
     "INVALID_STEP_COLLECTION"),
    ({"success_criteria": "specific", "steps": [{
        "objective": "inspect", "expected_evidence": "listing", "arguments": {},
    }]}, "MISSING_STEP_FIELD"),
    ({"success_criteria": "specific", "steps": [{
        **_step("filesystem.list", {"path": "."}), "arguments": [],
    }]}, "INVALID_ARGUMENT_OBJECT"),
])
def test_invalid_step_shapes_are_strictly_rejected(tmp_path, payload, reason):
    runner, _calls = _stack(tmp_path, [])
    with pytest.raises(InitialPlanInvalid, match=reason):
        runner.initial_planner._validate(payload)


def test_malformed_arguments_get_one_bounded_complete_plan_repair(tmp_path):
    malformed = _response([{**_step("filesystem.read", {"path": "note.txt"}),
                            "arguments": "note.txt"}])
    valid = _response([_step("filesystem.list", {"path": "."})],
                      "the allowed workspace directory listing is observed")
    runner, calls = _stack(tmp_path, [malformed, valid])
    assert runner.create_from_objective("conversation", "Inspect the workspace").model_calls == 2
    repair_prompt = calls[1]["messages"][0]["content"]
    assert '"reason_codes":["INVALID_ARGUMENT_OBJECT"]' in repair_prompt
    assert "complete corrected plan, not a patch" in repair_prompt


def test_provider_schema_and_parser_contract_are_identical(tmp_path):
    runner, calls = _stack(tmp_path, [_response([
        {"objective": "inspect workspace", "capability_id": "filesystem.list",
         "arguments": {"path": "."}},
    ], "the workspace listing is observed")])
    runner.create_from_objective("conversation", "Inspect the workspace")
    schema = calls[0]["format"]
    step_schema = schema["properties"]["steps"]["items"]
    assert set(step_schema["properties"]) == InitialGoalPlanner.MODEL_STEP_FIELDS
    assert set(step_schema["required"]) == InitialGoalPlanner.MODEL_STEP_FIELDS
    assert step_schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"steps"}
    assert schema["required"] == ["steps"]


def test_known_legacy_backend_fields_are_stripped_but_unknown_fields_are_rejected(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    legacy = {**_step("filesystem.list", {"path": "."}), "verification": {"mode": "legacy"}}
    _success, steps = runner.initial_planner._validate({
        "success_criteria": "the workspace listing is observed", "steps": [legacy],
    })
    assert steps[0].verification == {}
    assert steps[0].expected_evidence == "capability result passes native validation"
    with pytest.raises(InitialPlanInvalid, match="UNKNOWN_STEP_FIELD"):
        runner.initial_planner._validate({
            "success_criteria": "the workspace listing is observed",
            "steps": [{**legacy, "unbounded_alias": "x"}],
        })


def test_presentation_metadata_is_ignored_only_beside_complete_semantics(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    decorated = {**_step("filesystem.list", {"path": "."}),
                 "reason": "presentation only", "notes": "presentation only"}
    success, steps = runner.initial_planner._validate({"steps": [decorated]})
    assert success == "All planned capability steps pass authoritative backend verification."
    assert steps[0].capability_id == "filesystem.list"
    with pytest.raises(InitialPlanInvalid, match="UNKNOWN_STEP_FIELD"):
        runner.initial_planner._validate({"steps": [{"reason": "ambiguous"}]})


@pytest.mark.parametrize("field", [
    "tool", "command", "params", "path", "destructive", "shell_command",
])
def test_unknown_operational_fields_fail_closed(tmp_path, field):
    runner, _calls = _stack(tmp_path, [])
    with pytest.raises(InitialPlanInvalid, match="UNKNOWN_STEP_FIELD"):
        runner.initial_planner._validate({
            "steps": [{**_step("filesystem.list", {"path": "."}), field: "unsafe alias"}],
        })


def test_real_provider_action_shape_canonicalizes_exact_registered_capabilities(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    success, steps = runner.initial_planner._validate({"steps": [
        {"action": "filesystem.write",
         "arguments": {"path": "nova_goal_test.txt", "content": "HELLO_NOVA_7391"}},
        {"action": "filesystem.read", "arguments": {"path": "nova_goal_test.txt"}},
    ]})
    assert [step.capability_id for step in steps] == ["filesystem.write", "filesystem.read"]
    assert steps[1].verification == {"content": "HELLO_NOVA_7391"}
    assert success == ("nova_goal_test.txt exists in the allowed workspace and its content "
                       "equals HELLO_NOVA_7391")


def test_top_level_steps_and_plan_alias_are_the_only_accepted_plan_fields(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    canonical_step = {"capability_id": "filesystem.list", "arguments": {"path": "."}}

    for payload in ({"steps": [canonical_step]}, {"plan": [canonical_step]}):
        _success, steps = runner.initial_planner._validate(payload)
        assert [step.capability_id for step in steps] == ["filesystem.list"]

    for invalid_plan in ({}, "x"):
        with pytest.raises(InitialPlanInvalid, match="INVALID_TOP_LEVEL_SCHEMA"):
            runner.initial_planner._validate({"plan": invalid_plan})

    with pytest.raises(InitialPlanInvalid, match="AMBIGUOUS_TOP_LEVEL_PLAN_FIELD") as conflict:
        runner.initial_planner._validate({"steps": [canonical_step], "plan": [canonical_step]})
    assert conflict.value.reason_code == "AMBIGUOUS_TOP_LEVEL_PLAN_FIELD"

    with pytest.raises(InitialPlanInvalid, match="UNKNOWN_TOP_LEVEL_FIELD"):
        runner.initial_planner._validate({"actions": [canonical_step]})


def test_extract_step_array_accepts_only_the_three_supported_root_forms():
    canonical = {"capability_id": "filesystem.list", "arguments": {"path": "."}}
    assert extract_step_array({"steps": [canonical]}) == [canonical]
    assert extract_step_array({"plan": [canonical]}) == [canonical]
    assert extract_step_array([canonical]) == [canonical]


def test_root_array_canonical_steps_use_strict_step_canonicalization(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    _success, steps = runner.initial_planner._validate([
        {"capability_id": "filesystem.list", "arguments": {"path": "."}},
    ])
    assert [step.capability_id for step in steps] == ["filesystem.list"]


def test_root_array_exact_action_aliases_use_strict_step_canonicalization(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    _success, steps = runner.initial_planner._validate([
        {"action": "filesystem.list", "arguments": {"path": "."}},
    ])
    assert [step.capability_id for step in steps] == ["filesystem.list"]


def test_root_array_invalid_item_type_is_rejected(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    with pytest.raises(InitialPlanInvalid, match="INVALID_STEP_DTO_TYPE"):
        runner.initial_planner._validate(["filesystem.list"])


def test_root_array_unknown_operational_field_is_rejected(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    with pytest.raises(InitialPlanInvalid, match="UNKNOWN_STEP_FIELD"):
        runner.initial_planner._validate([
            {"capability_id": "filesystem.list", "arguments": {"path": "."},
             "command": "unsafe"},
        ])


def test_unknown_wrapper_is_rejected(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    with pytest.raises(InitialPlanInvalid, match="UNKNOWN_TOP_LEVEL_FIELD"):
        runner.initial_planner._validate({"workflow": []})


def test_real_provider_plan_and_action_aliases_compile_authoritatively(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    success, steps = runner.initial_planner._validate({"plan": [
        {"action": "filesystem.write",
         "arguments": {"path": "nova_goal_test.txt", "content": "HELLO_NOVA_7391"}},
        {"action": "filesystem.read", "arguments": {"path": "nova_goal_test.txt"}},
    ]})
    assert [step.capability_id for step in steps] == ["filesystem.write", "filesystem.read"]
    assert steps[1].verification == {"content": "HELLO_NOVA_7391"}
    assert success == ("nova_goal_test.txt exists in the allowed workspace and its content "
                       "equals HELLO_NOVA_7391")


def test_top_level_plan_alias_structural_diagnostics_are_boolean_and_value_free(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    payload = {"plan": [{"action": "filesystem.read", "arguments": {"path": "SECRET"}}]}
    fingerprint = structural_fingerprint(
        payload, failure_stage="accepted", reason_code="VALID_PLAN",
        required_step_fields=InitialGoalPlanner.MODEL_STEP_FIELDS,
        registered_capability_ids=runner.initial_planner.canonicalizer._registered_capability_ids(),
    )
    assert fingerprint["plan_alias_present"] is True
    assert fingerprint["steps_present"] is False
    assert fingerprint["top_level_plan_conflict"] is False
    assert fingerprint["plan_alias_is_array"] is True
    assert fingerprint["steps"][0]["action_matches_registered_capability"] is True
    assert "SECRET" not in json.dumps(fingerprint)


def test_action_compatibility_is_exact_and_conflicts_fail_closed(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    _success, canonical = runner.initial_planner._validate({"steps": [
        {"capability_id": "filesystem.list", "action": "filesystem.list",
         "arguments": {"path": "."}},
    ]})
    assert canonical[0].capability_id == "filesystem.list"

    with pytest.raises(InitialPlanInvalid, match="ACTION_NOT_REGISTERED_CAPABILITY"):
        runner.initial_planner._validate({"steps": [
            {"action": "write_file", "arguments": {"path": "x", "content": "x"}},
        ]})
    with pytest.raises(InitialPlanInvalid, match="AMBIGUOUS_CAPABILITY_FIELD"):
        runner.initial_planner._validate({"steps": [
            {"action": "filesystem.read", "capability_id": "filesystem.list",
             "arguments": {"path": "."}},
        ]})


def test_action_structural_diagnostics_are_boolean_and_value_free(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    payload = {"steps": [{"action": "filesystem.read", "arguments": {"path": "SECRET"}}]}
    fingerprint = structural_fingerprint(
        payload, failure_stage="accepted", reason_code="VALID_PLAN",
        required_step_fields=InitialGoalPlanner.MODEL_STEP_FIELDS,
        registered_capability_ids=runner.initial_planner.canonicalizer._registered_capability_ids(),
    )
    step = fingerprint["steps"][0]
    assert step["action_present"] is True
    assert step["capability_id_present"] is False
    assert step["action_matches_registered_capability"] is True
    assert step["capability_conflict"] is False
    assert "SECRET" not in json.dumps(fingerprint)


def test_all_backend_owned_fields_are_discarded_and_recompiled(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    step = {**_step("filesystem.list", {"path": "."}),
            "risk": "high", "reversible": False, "capability_family": "model-owned",
            "step_id": "model-step", "status": "completed"}
    _success, compiled = runner.initial_planner._validate({"steps": [step]})
    assert compiled[0].expected_evidence == "capability result passes native validation"
    goal = runner.create("c", "inspect", _success, [item.semantic() for item in compiled])
    assert goal.plan[0]["risk"] == "low"
    assert goal.plan[0]["reversible"] is True
    assert goal.plan[0]["step_id"] == "step-1"


def test_rejection_journals_sanitized_structural_fingerprint(tmp_path):
    invalid = _response([{**_step("filesystem.list", {"path": "SECRET_PATH"}),
                          "command": "SECRET_COMMAND"}])
    runner, _calls = _stack(tmp_path, [invalid, invalid])
    with pytest.raises(InitialPlanInvalid):
        runner.create_from_objective("c", "Inspect the workspace")
    event = next(item for item in runner.journal.recent() if item.type == "goal.plan.rejected")
    serialized = json.dumps(event.structural_fingerprint)
    assert "SECRET" not in serialized
    assert event.structural_fingerprint["reason_code"] == "UNKNOWN_STEP_FIELD"
    assert event.structural_fingerprint["step_index"] == 0
    assert event.structural_fingerprint["steps"][0]["unknown_fields"] == ["command"]


def test_structural_fingerprint_never_contains_values():
    payload = {"success_criteria": "SECRET", "steps": [{
        "objective": "SECRET", "capability_id": "filesystem.write",
        "arguments": {"path": "SECRET", "content": "SECRET"},
        "verification": {"content": "SECRET"},
    }]}
    fingerprint = structural_fingerprint(
        payload, failure_stage="planner_step_dto", reason_code="UNKNOWN_STEP_FIELD",
        required_step_fields=InitialGoalPlanner.MODEL_STEP_FIELDS,
    )
    serialized = json.dumps(fingerprint)
    assert "SECRET" not in serialized
    assert fingerprint["steps"][0]["unknown_fields"] == ["objective", "verification"]


@pytest.mark.parametrize("steps", [
    [{"objective": "inspect project", "capability_id": "project.basic_info", "arguments": {}}],
    [{"objective": "write", "capability_id": "filesystem.write",
      "arguments": {"path": "proof.txt", "content": "ok"}},
     {"objective": "verify", "capability_id": "filesystem.read",
      "arguments": {"path": "proof.txt"}}],
    [{"objective": "observe desktop", "capability_id": "computer.observe", "arguments": {}}],
    [{"objective": "discover windows", "capability_id": "computer.windows", "arguments": {}},
     {"objective": "observe desktop", "capability_id": "computer.observe", "arguments": {}}],
])
def test_minimal_model_step_contract_generalizes_by_capability_metadata(tmp_path, steps):
    runner, _calls = _stack(tmp_path, [])
    success, compiled = runner.initial_planner._validate({
        "success_criteria": "the requested observable state is recorded", "steps": steps,
    })
    assert success
    assert len(compiled) == len(steps)
    assert all(step.expected_evidence for step in compiled)


def test_model_success_criterion_is_ignored_and_compiled_by_backend(tmp_path):
    criterion = "note.txt exists and content equals ready"
    runner, _calls = _stack(tmp_path, [_response([
        _step("filesystem.read", {"path": "note.txt"}),
    ], criterion)])
    goal = runner.create_from_objective("conversation", "Verify note.txt contains ready")
    assert goal.success_criteria == "All planned capability steps pass authoritative backend verification."
    assert runner.initial_planner.last_structural_fingerprint["ignored_fields"] == [
        "expected_evidence", "success_criteria",
    ]


@pytest.mark.parametrize(("steps", "reason"), [
    ([_step("unknown.capability", {})], "INVALID_CAPABILITY"),
    ([_step("filesystem.write", {"path": "C:/outside/nova_goal_test.txt", "content": "x"})],
     "UNSAFE_PATH"),
    ([_step("filesystem.delete", {"path": "nova_goal_test.txt"})], "INVALID_CAPABILITY"),
])
def test_hard_invalid_plans_remain_rejected_after_one_repair(tmp_path, steps, reason):
    runner, calls = _stack(tmp_path, [_response(steps), _response(steps)])
    with pytest.raises(InitialPlanInvalid, match="PLAN_INVALID") as captured:
        runner.create_from_objective("conversation", "Create and verify a temporary file")
    assert captured.value.reason_code == reason
    assert len(calls) == 2
    rejected = next(event for event in runner.journal.recent() if event.type == "goal.plan.rejected")
    assert rejected.error_category == reason


def test_resume_is_unambiguous_and_never_selects_terminal_goal(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    pending = runner.create("c", "Inspect", "listing verified",
                            [{"capability_id": "filesystem.list", "arguments": {"path": "."}}])
    terminal = runner.create("c", "Inspect twice", "listing verified",
                             [{"capability_id": "filesystem.list", "arguments": {"path": "."}}])
    assert runner.run(terminal.goal_id).status == "completed_verified"
    assert [goal.goal_id for goal in runner.resumable("c")] == [pending.goal_id]
    assert [goal.goal_id for goal in runner.resumable("new-after-restart")] == [pending.goal_id]


def test_conversation_goal_stream_preserves_confirmation_and_completes(tmp_path):
    steps = [
        _step("filesystem.write", {"path": "conversation-proof.txt", "content": "verified"},
              objective="create proof", risk="medium"),
        _step("filesystem.read", {"path": "conversation-proof.txt"}, objective="verify proof"),
    ]
    runner, _calls = _stack(tmp_path, [_response(steps)])
    client = TestClient(create_app(goal_runner=runner, memory_store=runner.memory,
                                   capability_registry=runner.registry, journal=runner.journal))
    conversation_id = client.post("/api/v1/conversations", json={}).json()["conversation_id"]
    streamed = client.post(f"/api/v1/conversations/{conversation_id}/messages/stream",
                           json={"content": "Create a temporary file and verify it."})
    assert "event: goal.created" in streamed.text
    assert "event: goal.awaiting_confirmation" in streamed.text
    payloads = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith("data: ")]
    token = payloads[-1]["confirmation"]["token"]
    approved = client.post(f"/api/v1/conversations/{conversation_id}/confirmations",
                           json={"token": token, "approved": True})
    assert approved.status_code == 200
    assert "terminé et vérifié" in approved.json()["assistant_message"]["content"]
    assert (tmp_path / "conversation-proof.txt").read_text(encoding="utf-8") == "verified"


def test_ambiguous_natural_language_resume_requests_clarification(tmp_path):
    runner, _calls = _stack(tmp_path, [])
    for objective in ("Inspect one", "Inspect two"):
        runner.create("c", objective, "listing verified",
                      [{"capability_id": "filesystem.list", "arguments": {"path": "."}}])
    class Engine:
        def respond(self, *_args):
            raise AssertionError("ordinary chat must not run for resume")

    service = ConversationService(Engine(), ApiStateStore(), goal_runner=runner, id_factory=lambda: "c")
    client = TestClient(create_app(conversation_service=service, goal_runner=runner,
                                   memory_store=runner.memory, capability_registry=runner.registry,
                                   journal=runner.journal))
    assert client.post("/api/v1/conversations", json={}).json()["conversation_id"] == "c"
    streamed = client.post("/api/v1/conversations/c/messages/stream",
                           json={"content": "Continue ce qu'on faisait."})
    assert "event: goal.clarification_required" in streamed.text
    assert "Plusieurs missions" in streamed.text
