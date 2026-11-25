"""SPC-1 to SPC-5: the spaces of a project (docs/api.md, section 13).

There is no DELETE (S-5): FastAPI answers ``405 method_not_allowed`` for it
because ``/spaces/{space_id}`` exists with GET and PATCH only.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi import status as http_status

from app.api.deps import (
    ProjectContext,
    SessionDep,
    require_any_project_permission,
    require_project_permission,
)
from app.core.permissions import Action, Feature
from app.schemas.common import ErrorResponse, IdPath, Page
from app.schemas.space import (
    SPACES_PAGE_SIZE,
    CreateSpaceRequest,
    ReorderSpacesRequest,
    SpaceOut,
    UpdateSpaceRequest,
)
from app.services import space as space_service
from app.utils.pagination import PageParams, page_params
from app.utils.search import SearchQuery

router = APIRouter(prefix="/projects/{public_id}/spaces", tags=["spaces"])

# Browsing tasks needs the sidebar, so tasks.view is enough to read spaces.
View = Annotated[
    ProjectContext,
    require_any_project_permission((Feature.SPACES, Action.VIEW), (Feature.TASKS, Action.VIEW)),
]
Create = Annotated[ProjectContext, require_project_permission(Feature.SPACES, Action.CREATE)]
Edit = Annotated[ProjectContext, require_project_permission(Feature.SPACES, Action.EDIT)]
SpacesPage = Annotated[PageParams, Depends(page_params(SPACES_PAGE_SIZE))]

_READ_ERRORS = {403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}


@router.get(
    "",
    summary="Spaces in sidebar order, with task counts (SPC-1)",
    responses=_READ_ERRORS,
)
async def list_spaces(
    session: SessionDep, project: View, page: SpacesPage, q: SearchQuery
) -> Page[SpaceOut]:
    return await space_service.list_spaces(session, project.project, q=q, page=page)


@router.post(
    "",
    status_code=http_status.HTTP_201_CREATED,
    summary="Add a space at the end of the sidebar (SPC-2)",
    responses=_READ_ERRORS
    | {
        409: {
            "model": ErrorResponse,
            "description": "conflict (name) or project_archived",
        }
    },
)
async def create_space(body: CreateSpaceRequest, session: SessionDep, project: Create) -> SpaceOut:
    return await space_service.create_space(session, project.project, body)


# Declared before "/{space_id}" so "order" is never read as an id.
@router.put(
    "/order",
    summary="Set the whole sidebar order (SPC-5)",
    responses=_READ_ERRORS | {409: {"model": ErrorResponse, "description": "project_archived"}},
)
async def reorder_spaces(
    body: ReorderSpacesRequest, session: SessionDep, project: Edit
) -> Page[SpaceOut]:
    return await space_service.reorder_spaces(session, project.project, body)


@router.get("/{space_id}", summary="One space (SPC-3)", responses=_READ_ERRORS)
async def get_space(space_id: IdPath, session: SessionDep, project: View) -> SpaceOut:
    return await space_service.get_space(session, project.project, space_id)


@router.patch(
    "/{space_id}",
    summary="Rename or describe a space (SPC-4)",
    responses=_READ_ERRORS
    | {
        409: {
            "model": ErrorResponse,
            "description": "conflict, stale_update or project_archived",
        }
    },
)
async def update_space(
    space_id: IdPath, body: UpdateSpaceRequest, session: SessionDep, project: Edit
) -> SpaceOut:
    return await space_service.update_space(session, project.project, space_id, body)
