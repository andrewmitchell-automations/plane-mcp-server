"""Relations between work items, and the workspace definitions that type them.

Two systems behind one tool: built-in dependencies (six fixed directional types)
and custom relations (workspace-defined, each with an outward and inward label).
`create` routes between them by which arguments are supplied.

Servers without the Commercial dependency routes (Plane CE, PaperPlane) answer them
with 404. There, list/create/delete fall back to the v1 `relations/` routes, which
carry the dependency types plus relates_to and duplicate. A 404 from a write can
also mean "no such relation", so the route is probed with a read before a write
falls back.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from fastmcp import FastMCP
from plane.errors.errors import HttpError
from plane.models.enums import WorkItemRelationTypeEnum
from plane.models.work_item_relation_definitions import (
    CreateWorkItemRelationDefinition,
    PaginatedWorkItemRelationDefinitionResponse,
    UpdateWorkItemRelationDefinition,
    WorkItemRelationDefinition,
)
from plane.models.work_items import (
    CreateWorkItemCustomRelation,
    CreateWorkItemDependency,
    CreateWorkItemRelation,
    DependencyTypeEnum,
    RemoveWorkItemRelation,
)

from plane_mcp.client import get_plane_client_context
from plane_mcp.toolkit import (
    Action,
    build_annotations,
    build_description,
    coerce_list,
    missing,
    needs,
    one_of,
    opt,
)

NAME = "workitem_relation"
TITLE = "Work item relations"

DEPENDENCY_TYPES: tuple[str, ...] = get_args(DependencyTypeEnum)
# The v1 relations API: the dependency types plus relates_to and duplicate.
RELATION_TYPES: tuple[str, ...] = get_args(WorkItemRelationTypeEnum)

_OTHER_RELATIONS = (
    "For any other relationship pass relation_definition_id and "
    "relation_definition_label from the list_definitions action."
)

ACTIONS = (
    Action("list", ("project_id", "workitem_id"), read=True),
    Action(
        "create",
        ("project_id", "workitem_id", "workitem_ids"),
        ("relation_type", "relation_definition_id", "relation_definition_label"),
        note="pass relation_type for a dependency, or definition id + label for a custom relation",
    ),
    Action(
        "delete",
        ("project_id", "workitem_id", "related_workitem_id"),
        ("is_dependency",),
        note="removes one relation; dependencies and custom relations are independent, so "
        "is_dependency must match the kind that was created (default false)",
        destructive=True,
    ),
    Action("list_definitions", optional=("is_default", "is_active"), read=True),
    Action("create_definition", ("name",), ("outward", "inward", "is_active", "color")),
    Action("update_definition", ("definition_id",), ("name", "outward", "inward", "is_active", "color")),
    Action("delete_definition", ("definition_id",), destructive=True),
)

FOOTER = (
    "Call list_definitions first and match the user's wording to an entry. A "
    f"built_in_dependencies value ({', '.join(DEPENDENCY_TYPES)}) goes in relation_type; a "
    "custom definition needs its id in relation_definition_id and the matched outward or "
    "inward label in relation_definition_label, which sets direction. On a server without "
    "dependency routes (Plane CE, PaperPlane) relations use the v1 API instead, where "
    "relation_type also accepts relates_to and duplicate, and custom relations are unavailable."
)

LEGACY = {
    "list_work_item_relations": "list",
    "create_work_item_relation": "create",
    "remove_work_item_relation": "delete",
    "list_work_item_relation_definitions": "list_definitions",
    "create_work_item_relation_definition": "create_definition",
    "update_work_item_relation_definition": "update_definition",
    "delete_work_item_relation_definition": "delete_definition",
}


def _all_definitions(client, workspace_slug: str, is_default, is_active) -> list[WorkItemRelationDefinition]:
    """Definitions are a small set an agent must see whole, so page through them."""
    results: list[WorkItemRelationDefinition] = []
    cursor: str | None = None
    while True:
        page: PaginatedWorkItemRelationDefinitionResponse = client.work_item_relation_definitions.list(
            workspace_slug=workspace_slug,
            is_default=is_default,
            is_active=is_active,
            per_page=100,
            cursor=cursor,
        )
        results.extend(page.results)
        cursor = page.next_cursor
        if not page.next_page_results or not cursor:
            return results


def _is_missing_route(exc: HttpError) -> bool:
    return exc.status_code == 404


def _lacks_dependency_routes(client, workspace_slug: str, project_id: str, workitem_id: str) -> bool:
    """True when the server has no Commercial dependency routes, judged by a read.

    A write's 404 can mean a missing relation as well as a missing route; this read
    answers 200 wherever the routes exist, so production never reaches the fallback.
    """
    try:
        client.work_items.dependencies.list(
            workspace_slug=workspace_slug, project_id=project_id, work_item_id=workitem_id
        )
    except HttpError as exc:
        return _is_missing_route(exc)
    return False


def _v1_list(client, workspace_slug: str, project_id: str, workitem_id: str) -> dict[str, Any]:
    """The v1 relations, in the shape of the Commercial list; there are no custom relations.

    Read raw: the SDK's WorkItemRelationResponse types each entry as an id, but the
    server returns work item objects, so validating against it fails on any relation.
    """
    relations = client.work_items.relations._get(
        f"{workspace_slug}/projects/{project_id}/work-items/{workitem_id}/relations"
    )

    def entry(item: Any) -> dict[str, Any]:
        # Entries carry the related item as issue_id; expose it as id, like the Commercial list.
        if not isinstance(item, dict):
            return {"id": item}
        return {"id": item.get("issue_id") or item.get("id"), **{k: v for k, v in item.items() if k != "id"}}

    def entries(kind: str) -> list[dict[str, Any]]:
        return [entry(item) for item in (relations or {}).get(kind) or []]

    return {
        "dependencies": {kind: entries(kind) for kind in DEPENDENCY_TYPES},
        "relations": {kind: entries(kind) for kind in RELATION_TYPES if kind not in DEPENDENCY_TYPES},
        "custom": {},
    }


def _v1_create(client, workspace_slug: str, project_id: str, workitem_id: str, relation_type: str, targets) -> Any:
    result = client.work_items.relations.create(
        workspace_slug=workspace_slug,
        project_id=project_id,
        work_item_id=workitem_id,
        data=CreateWorkItemRelation(relation_type=relation_type, issues=targets),  # type: ignore[arg-type]
    )
    if result is not None:
        return result
    return {"relation_type": relation_type, "workitem_ids": targets}


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description(
            "Relations between work items, and the definitions that type them.", ACTIONS, FOOTER
        ),
        annotations=build_annotations(TITLE, ACTIONS),
    )
    def workitem_relation(
        action: Literal[
            "list",
            "create",
            "delete",
            "list_definitions",
            "create_definition",
            "update_definition",
            "delete_definition",
        ],
        project_id: str = "",
        workitem_id: str = "",
        workitem_ids: list[str] | None = None,
        related_workitem_id: str = "",
        relation_type: str = "",
        relation_definition_id: str = "",
        relation_definition_label: str = "",
        definition_id: str = "",
        name: str = "",
        outward: str = "",
        inward: str = "",
        color: str = "",
        # Tri-state: False is a real filter value, distinct from "no filter".
        is_default: bool | None = None,
        is_active: bool | None = None,
        is_dependency: bool = False,
    ) -> Any:
        client, workspace_slug = get_plane_client_context()

        if action == "list_definitions":
            return {
                "built_in_dependencies": list(DEPENDENCY_TYPES),
                "custom_definitions": [
                    d.model_dump() for d in _all_definitions(client, workspace_slug, is_default, is_active)
                ],
            }

        if action == "create_definition":
            if not name:
                return missing(action, "name")
            return client.work_item_relation_definitions.create(
                workspace_slug=workspace_slug,
                data=CreateWorkItemRelationDefinition(
                    name=name,
                    outward=opt(outward),
                    inward=opt(inward),
                    is_active=is_active,
                    color=opt(color),
                ),
            )

        if action in ("update_definition", "delete_definition"):
            if not definition_id:
                return missing(action, "definition_id")
            if action == "update_definition":
                return client.work_item_relation_definitions.update(
                    workspace_slug=workspace_slug,
                    definition_id=definition_id,
                    data=UpdateWorkItemRelationDefinition(
                        name=opt(name),
                        outward=opt(outward),
                        inward=opt(inward),
                        is_active=is_active,
                        color=opt(color),
                    ),
                )
            client.work_item_relation_definitions.delete(workspace_slug=workspace_slug, definition_id=definition_id)
            return None

        if error := needs(action, project_id=project_id, workitem_id=workitem_id):
            return error

        if action == "list":
            try:
                dependencies = client.work_items.dependencies.list(
                    workspace_slug=workspace_slug, project_id=project_id, work_item_id=workitem_id
                )
                custom = client.work_items.custom_relations.list(
                    workspace_slug=workspace_slug, project_id=project_id, work_item_id=workitem_id
                )
            except HttpError as exc:
                if not _is_missing_route(exc):
                    raise
                try:
                    return _v1_list(client, workspace_slug, project_id, workitem_id)
                except HttpError as fallback:
                    if _is_missing_route(fallback):
                        raise exc from None
                    raise
            return {
                "dependencies": dependencies.model_dump(),
                "custom": {label: [item.model_dump() for item in items] for label, items in custom.items()},
            }

        if action == "create":
            targets = coerce_list(workitem_ids)
            if not targets:
                return missing(action, "workitem_ids")
            if relation_type:
                if relation_type in RELATION_TYPES and relation_type not in DEPENDENCY_TYPES:
                    # relates_to / duplicate exist only in the v1 API.
                    if _lacks_dependency_routes(client, workspace_slug, project_id, workitem_id):
                        return _v1_create(client, workspace_slug, project_id, workitem_id, relation_type, targets)
                if error := one_of("relation_type", relation_type, DEPENDENCY_TYPES, _OTHER_RELATIONS):
                    return error
                try:
                    return client.work_items.dependencies.create(
                        workspace_slug=workspace_slug,
                        project_id=project_id,
                        work_item_id=workitem_id,
                        data=CreateWorkItemDependency(
                            relation_type=relation_type,  # type: ignore[arg-type]
                            work_item_ids=targets,
                        ),
                    )
                except HttpError as exc:
                    if not (
                        _is_missing_route(exc)
                        and _lacks_dependency_routes(client, workspace_slug, project_id, workitem_id)
                    ):
                        raise
                    return _v1_create(client, workspace_slug, project_id, workitem_id, relation_type, targets)
            if relation_definition_id and relation_definition_label:
                return client.work_items.custom_relations.create(
                    workspace_slug=workspace_slug,
                    project_id=project_id,
                    work_item_id=workitem_id,
                    data=CreateWorkItemCustomRelation(
                        relation_definition_id=relation_definition_id,
                        relation_definition_type=relation_definition_label,
                        work_item_ids=targets,
                    ),
                )
            return (
                "Error: provide relation_type for a built-in dependency, or both "
                "relation_definition_id and relation_definition_label for a custom relation. "
                "Call the list_definitions action to find one."
            )

        if not related_workitem_id:
            return missing(action, "related_workitem_id")
        remove = client.work_items.dependencies.remove if is_dependency else client.work_items.custom_relations.remove
        try:
            remove(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=workitem_id,
                related_work_item_id=related_workitem_id,
            )
        except HttpError as exc:
            if not (
                _is_missing_route(exc) and _lacks_dependency_routes(client, workspace_slug, project_id, workitem_id)
            ):
                raise
            # v1 keeps one relation per pair, whatever its kind, so is_dependency does not matter here.
            client.work_items.relations.delete(
                workspace_slug=workspace_slug,
                project_id=project_id,
                work_item_id=workitem_id,
                data=RemoveWorkItemRelation(related_issue=related_workitem_id),
            )
        return None
