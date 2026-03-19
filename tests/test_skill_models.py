"""Tests for skill data models."""

from creel.skills.models import Param, SkillMeta, ToolSpec


def test_param_defaults():
    p = Param(name="query")
    assert p.name == "query"
    assert p.type == "string"
    assert p.description == ""
    assert p.required is False


def test_param_custom():
    p = Param(name="count", type="integer", description="Max results", required=True)
    assert p.type == "integer"
    assert p.required is True


def test_toolspec_defaults():
    ts = ToolSpec(name="check_weather", description="Get weather")
    assert ts.params == ()
    assert ts.fixed_args == {}


def test_toolspec_with_params():
    ts = ToolSpec(
        name="fetch",
        description="Fetch URL",
        params=(Param(name="url", required=True),),
        fixed_args={"action": "get"},
    )
    assert len(ts.params) == 1
    assert ts.fixed_args == {"action": "get"}


def test_skillmeta_defaults():
    meta = SkillMeta(
        id="weather",
        label="Weather",
        tools=(ToolSpec(name="check_weather", description="Get weather"),),
    )
    assert meta.needs_network is False
    assert meta.needs_bridge is False
    assert meta.bridge_scope is None
    assert meta.platform is None


def test_skillmeta_with_bridge():
    meta = SkillMeta(
        id="apple_notes",
        label="Apple Notes",
        tools=(ToolSpec(name="list_notes", description="List notes"),),
        needs_bridge=True,
        bridge_scope="NOTES",
        platform="darwin",
    )
    assert meta.needs_bridge is True
    assert meta.bridge_scope == "NOTES"
    assert meta.platform == "darwin"


def test_skillmeta_frozen():
    meta = SkillMeta(id="test", label="Test", tools=())
    try:
        meta.id = "other"  # type: ignore[misc]
        assert False, "Should raise FrozenInstanceError"
    except AttributeError:
        pass
