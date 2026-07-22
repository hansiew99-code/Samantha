import pytest

from samantha.brain import Brain
from samantha.governor import Governor
from samantha.replies import OwnerReply
from samantha.tools import gchat_tools
from samantha.tools.registry import (
    MUTATING_TOOLS,
    STRICT_TOOLS,
    Tool,
    ToolRegistry,
    validate_provider_tools,
)

from fakes import FakeAnthropicClient, FakeResponse, text_block


def _tool_spec(name: str, schema: dict, *, strict: bool = True) -> dict:
    spec = {
        "name": name,
        "description": "Synthetic provider-contract test tool.",
        "input_schema": schema,
    }
    if strict:
        spec["strict"] = True
    return spec


def test_only_high_impact_provider_mutations_are_marked_strict():
    read_tool = Tool(
        name="memory_search",
        description="Read memory.",
        input_schema={"type": "object", "properties": {}},
        func=lambda: "",
    ).spec()
    local_mutation = Tool(
        name="memory_save",
        description="Save memory.",
        input_schema={"type": "object", "properties": {}},
        func=lambda: "",
    ).spec()
    provider_mutation = Tool(
        name="calendar_update_event",
        description="Update a calendar event.",
        input_schema={"type": "object", "properties": {}},
        func=lambda: "",
    ).spec()

    assert "strict" not in read_tool
    assert "strict" not in local_mutation
    assert provider_mutation["strict"] is True
    assert STRICT_TOOLS <= MUTATING_TOOLS


def test_approval_gated_draft_is_runtime_validated_but_not_strict():
    draft = Tool(
        name="gmail_draft_reply",
        description="Create an approval-gated email draft.",
        input_schema={"type": "object", "properties": {}},
        func=lambda: "",
    ).spec()

    assert "strict" not in draft


def test_registry_specs_pass_provider_contract():
    registry = ToolRegistry()
    registry.register(Tool(
        name="memory_search",
        description="Read memory.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        func=lambda query: query,
    ))
    registry.register(Tool(
        name="memory_save",
        description="Save memory.",
        input_schema={
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["subject"],
        },
        func=lambda subject, note=None: subject,
    ))

    validate_provider_tools(registry.specs())


def test_gchat_limit_is_described_without_unsupported_range_keywords():
    registry = ToolRegistry()
    gchat_tools.register(registry, object())

    spec = registry.specs()[0]
    limit_schema = spec["input_schema"]["properties"]["limit"]

    assert "hard cap 25" in limit_schema["description"]
    assert "minimum" not in limit_schema
    assert "maximum" not in limit_schema
    validate_provider_tools([spec])


@pytest.mark.parametrize("keyword", ["minimum", "maxLength", "pattern"])
def test_provider_contract_rejects_nested_unsupported_constraints(keyword):
    tools = [_tool_spec(
        "bad_schema",
        {
            "type": "object",
            "properties": {
                "options": {
                    "type": "array",
                    "items": {"type": "string", keyword: 1},
                },
            },
        },
        strict=False,
    )]

    with pytest.raises(ValueError, match=rf"bad_schema.*{keyword}.*input_schema"):
        validate_provider_tools(tools)


def test_provider_contract_allows_supported_min_items_values():
    validate_provider_tools([_tool_spec(
        "small_array",
        {"type": "array", "items": {"type": "string"}, "minItems": 1},
    )])


def test_provider_contract_rejects_more_than_twenty_strict_tools():
    tools = [
        _tool_spec(f"mutation_{index}", {"type": "object", "properties": {}})
        for index in range(21)
    ]

    with pytest.raises(ValueError, match=r"21 tools \(maximum 20\)"):
        validate_provider_tools(tools)


def test_provider_contract_rejects_more_than_twenty_four_optional_parameters():
    schema = {
        "type": "object",
        "properties": {
            f"optional_{index}": {"type": "string"}
            for index in range(25)
        },
    }

    with pytest.raises(ValueError, match=r"25 parameters \(maximum 24\)"):
        validate_provider_tools([_tool_spec("too_optional", schema)])


def test_provider_contract_counts_nested_optional_parameters():
    nested = {
        "type": "object",
        "properties": {
            "group": {
                "type": "object",
                "properties": {
                    f"optional_{index}": {"type": "string"}
                    for index in range(24)
                },
            },
        },
        "required": ["group"],
    }

    validate_provider_tools([_tool_spec("at_optional_limit", nested)])

    nested["properties"]["group"]["properties"]["one_more"] = {"type": "string"}
    with pytest.raises(ValueError, match=r"25 parameters \(maximum 24\)"):
        validate_provider_tools([_tool_spec("over_optional_limit", nested)])


def test_provider_contract_rejects_more_than_sixteen_union_parameters():
    schema = {
        "type": "object",
        "properties": {
            f"union_{index}": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
            }
            for index in range(17)
        },
        "required": [f"union_{index}" for index in range(17)],
    }

    with pytest.raises(ValueError, match=r"17 parameters \(maximum 16\)"):
        validate_provider_tools([_tool_spec("too_many_unions", schema)])


def test_provider_contract_counts_type_array_unions():
    schema = {
        "type": "object",
        "properties": {
            f"union_{index}": {"type": ["string", "null"]}
            for index in range(17)
        },
        "required": [f"union_{index}" for index in range(17)],
    }

    with pytest.raises(ValueError, match=r"17 parameters \(maximum 16\)"):
        validate_provider_tools([_tool_spec("too_many_type_unions", schema)])


async def test_brain_rejects_invalid_bundle_before_provider_call(
    settings, memory, conn
):
    registry = ToolRegistry()
    registry.register(Tool(
        name="unsafe_read",
        description="Synthetic invalid read schema.",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "maximum": 25}},
        },
        func=lambda limit=10: str(limit),
    ))
    client = FakeAnthropicClient([FakeResponse(content=[text_block("unexpected")])])
    brain = Brain(
        settings,
        memory,
        registry,
        Governor(conn, settings.daily_budget_usd),
        client=client,
    )

    reply = await brain.handle_message("read it")

    assert isinstance(reply, OwnerReply)
    assert reply.status == "degraded"
    assert client.calls == []
