"""ORG-1 and ORG-2: the caller's own organization (docs/api.md, section 8).

There is no id in the URL: a user belongs to exactly one organization (P-8),
so "/organization" always means theirs, and nobody can ask for another one.
"""

from fastapi import APIRouter

from app.api.deps import CurrentUserDep, SessionDep, require_org_permission
from app.core.permissions import Action, Feature
from app.schemas.common import ErrorResponse
from app.schemas.organization import OrganizationOut, UpdateOrganizationRequest
from app.services import organization as organization_service

router = APIRouter(prefix="/organization", tags=["organization"])


@router.get(
    "",
    summary="Your organization (ORG-1)",
    dependencies=[require_org_permission(Feature.ORGANIZATION, Action.VIEW)],
    responses={403: {"model": ErrorResponse}},
)
async def get_organization(session: SessionDep, user: CurrentUserDep) -> OrganizationOut:
    return await organization_service.get(session, user.organization_id)


@router.patch(
    "",
    summary="Rename or describe your organization (ORG-2)",
    dependencies=[require_org_permission(Feature.ORGANIZATION, Action.EDIT)],
    responses={
        403: {"model": ErrorResponse},
        409: {
            "model": ErrorResponse,
            "description": "stale_update: someone else saved first",
        },
    },
)
async def update_organization(
    body: UpdateOrganizationRequest, session: SessionDep, user: CurrentUserDep
) -> OrganizationOut:
    return await organization_service.update(session, user.organization_id, body)
