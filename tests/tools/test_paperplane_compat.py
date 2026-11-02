"""Servers built on the community API (Plane CE, PaperPlane).

Two gaps against Plane Commercial: a work item create needs an explicit type_id,
and the dependency / custom-relation routes are absent (404). On Commercial the
behaviour must not change, so every fallback here is paired with a test that the
Commercial path still answers as it did.
"""

from __future__ import annotations

import pytest
from plane.errors.errors import HttpError
from plane.models.work_item_types import WorkItemType
from plane.models.work_items import WorkItemRelationResponse

NOT_FOUND = HttpError("HTTP 404: Not Found", 404, "<h1>Not Found</h1>")


def _type(name: str, type_id: str, is_default: bool | None = None) -> WorkItemType:
    return WorkItemType(id=type_id, name=name, is_default=is_default)


def _created_type(spy) -> str | None:
    (create,) = [call for call in spy.recorder.calls if call.method == "work_items.create"]
    return create.kwargs["data"].type_id


def _create(registered, **extra):
    return registered["workitem"].fn(action="create", project_id="p", name="x", **extra)


# -- default type on create -------------------------------------------------------


def test_create_sends_the_default_type(registered, spy):
    spy.returns["work_item_types.list"] = [_type("Bug", "bug"), _type("Feature", "feat", is_default=True)]
    _create(registered)
    assert _created_type(spy) == "feat"


def test_create_falls_back_to_task_when_no_type_is_default(registered, spy):
    """PaperPlane reports is_default as null on every type."""
    spy.returns["work_item_types.list"] = [_type("Epic", "epic"), _type("Task", "task")]
    _create(registered)
    assert _created_type(spy) == "task"


def test_an_explicit_type_is_kept_and_nothing_is_looked_up(registered, spy):
    spy.returns["work_item_types.list"] = [_type("Task", "task", is_default=True)]
    _create(registered, type_id="mine")
    assert _created_type(spy) == "mine"
    assert "work_item_types.list" not in spy.recorder.methods


@pytest.mark.parametrize(
    "listed",
    [[], [_type("Bug", "bug")], HttpError("HTTP 400", 400, {"error": "Work item types are disabled"})],
    ids=["no-types", "no-default-no-task", "listing-refused"],
)
def test_create_without_a_resolvable_type_sends_none(listed, registered, spy):
    """Today's create, unchanged: Plane picks the type itself."""
    spy.returns["work_item_types.list"] = listed
    _create(registered)
    assert _created_type(spy) is None


# -- relations: list ---------------------------------------------------------------


def _relations(**kinds) -> dict:
    return dict.fromkeys(WorkItemRelationResponse.model_fields, []) | kinds


def _relation(registered, action, **extra):
    return registered["workitem_relation"].fn(action=action, project_id="p", workitem_id="w", **extra)


def test_list_uses_the_commercial_routes_when_they_exist(registered, spy):
    _relation(registered, "list")
    assert spy.recorder.methods == ["work_items.dependencies.list", "work_items.custom_relations.list"]


def test_list_falls_back_to_v1_relations_on_404(registered, spy):
    spy.returns["work_items.dependencies.list"] = NOT_FOUND
    # The server sends work item objects, not the ids the SDK model declares.
    blocker = {"id": "b1", "name": "Blocker", "project_id": "p", "sequence_id": 7}
    spy.returns["work_items.relations._get"] = _relations(blocked_by=[blocker], relates_to=["r1"])

    result = _relation(registered, "list")

    (call,) = [c for c in spy.recorder.calls if c.method == "work_items.relations._get"]
    assert call.kwargs["endpoint"] == "acme/projects/p/work-items/w/relations"
    assert result["dependencies"]["blocked_by"] == [blocker]
    assert result["dependencies"]["blocking"] == []
    assert result["relations"]["relates_to"] == [{"id": "r1"}]
    assert result["custom"] == {}


def test_list_keeps_the_original_404_when_v1_is_missing_too(registered, spy):
    spy.returns["work_items.dependencies.list"] = NOT_FOUND
    spy.returns["work_items.relations._get"] = HttpError("HTTP 404", 404, "v1")
    with pytest.raises(HttpError) as raised:
        _relation(registered, "list")
    assert raised.value is NOT_FOUND


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_list_does_not_fall_back_on_other_errors(status, registered, spy):
    spy.returns["work_items.dependencies.list"] = HttpError(f"HTTP {status}", status)
    with pytest.raises(HttpError):
        _relation(registered, "list")
    assert "work_items.relations._get" not in spy.recorder.methods


# -- relations: create -------------------------------------------------------------


def test_create_dependency_falls_back_to_v1_on_404(registered, spy):
    spy.returns["work_items.dependencies.create"] = NOT_FOUND
    spy.returns["work_items.dependencies.list"] = NOT_FOUND
    spy.returns["work_items.relations.create"] = None  # PaperPlane answers 201 with no body

    result = _relation(registered, "create", workitem_ids=["t"], relation_type="blocked_by")

    (call,) = [c for c in spy.recorder.calls if c.method == "work_items.relations.create"]
    assert call.kwargs["data"].relation_type == "blocked_by"
    assert call.kwargs["data"].issues == ["t"]
    assert result == {"relation_type": "blocked_by", "workitem_ids": ["t"]}


def test_create_dependency_404_on_commercial_is_raised(registered, spy):
    """The route exists (the probe answers), so the 404 is about the data: no fallback."""
    spy.returns["work_items.dependencies.create"] = NOT_FOUND
    with pytest.raises(HttpError):
        _relation(registered, "create", workitem_ids=["t"], relation_type="blocked_by")
    assert "work_items.relations.create" not in spy.recorder.methods


def test_relates_to_is_created_through_v1_where_dependencies_are_missing(registered, spy):
    spy.returns["work_items.dependencies.list"] = NOT_FOUND
    _relation(registered, "create", workitem_ids=["t"], relation_type="relates_to")
    (call,) = [c for c in spy.recorder.calls if c.method == "work_items.relations.create"]
    assert call.kwargs["data"].relation_type == "relates_to"


def test_relates_to_is_still_refused_on_commercial(registered, spy):
    result = _relation(registered, "create", workitem_ids=["t"], relation_type="relates_to")
    assert isinstance(result, str) and result.startswith("Error:")
    assert not any(c.method.endswith(".create") for c in spy.recorder.calls)


# -- relations: delete -------------------------------------------------------------


@pytest.mark.parametrize("is_dependency", [True, False])
def test_delete_falls_back_to_v1_on_404(is_dependency, registered, spy):
    remove = "dependencies" if is_dependency else "custom_relations"
    spy.returns[f"work_items.{remove}.remove"] = NOT_FOUND
    spy.returns["work_items.dependencies.list"] = NOT_FOUND

    _relation(registered, "delete", related_workitem_id="t", is_dependency=is_dependency)

    (call,) = [c for c in spy.recorder.calls if c.method == "work_items.relations.delete"]
    assert call.kwargs["data"].related_issue == "t"


def test_delete_404_on_commercial_never_reaches_v1(registered, spy):
    """v1 removes whatever relation the pair has; on Commercial that could be the wrong kind."""
    spy.returns["work_items.custom_relations.remove"] = NOT_FOUND
    with pytest.raises(HttpError):
        _relation(registered, "delete", related_workitem_id="t")
    assert "work_items.relations.delete" not in spy.recorder.methods


def test_delete_other_errors_are_raised_without_probing(registered, spy):
    spy.returns["work_items.custom_relations.remove"] = HttpError("HTTP 403", 403)
    with pytest.raises(HttpError):
        _relation(registered, "delete", related_workitem_id="t")
    assert spy.recorder.methods == ["work_items.custom_relations.remove"]
