# API Specification: Project Management POC

| | |
|---|---|
| Document | `docs/api.md` |
| Version | v2 (17 Nov 2026): adds spaces (SPC-1 to SPC-5, TSK-9) and `space_id` on tasks; matches the approved `docs/database.md` v2 |
| Depends on | `docs/database.md` (D1) |
| Base URL | `/api/v1` |
| Status | Endpoints final for implementation. Items marked **A-n** are behaviour proposals awaiting mentor approval (section 13). |

This document is the contract between the backend and the frontend. The Pydantic schemas (`app/schemas/*`) and the TypeScript types (`frontend/src/types/api.ts`) are both written from it. If an endpoint changes, this file changes first.

---

## 1. Conventions

| Topic | Rule |
|---|---|
| Versioning | Every path starts with `/api/v1`. A breaking change would go to `/api/v2`; additive changes stay in v1. |
| Format | JSON request and response bodies, `Content-Type: application/json`, UTF-8 |
| Field names | `snake_case`, same as the database columns |
| Timestamps | ISO 8601 in UTC with `Z`, e.g. `"2026-11-16T07:30:00.123456Z"` (`TIMESTAMPTZ`, P-1) |
| Dates | `YYYY-MM-DD`, e.g. `"2026-12-01"` |
| Partial updates | `PATCH` changes only the fields sent. Sending `null` clears a nullable field; leaving a field out keeps it unchanged. |
| Status codes | `200` read/update, `201` created, `204` deleted or no content, `4xx` client error (section 3) |
| Identifiers in URLs | Projects use `public_id` (random, not guessable). Tasks use their per-project `number`. Teams, spaces, statuses, comments, roles and users use their numeric `id`. Every lookup is scoped by organization, project and permission, so changing a number in the URL never reveals another tenant's data. |
| Hidden fields | Never returned anywhere: `password_hash`, `token_hash`, internal `projects.id`, `next_task_number` |

---

## 2. Authentication

### 2.1 Tokens

| Token | Format | Lifetime (A-10) | Where it lives |
|---|---|---|---|
| Access token | JWT signed with HS256; claims `sub` (user id), `org` (organization id), `type: "access"`, `iat`, `exp` | 15 minutes | httpOnly cookie, held by the Next.js server (BFF) |
| Refresh token | 32 random bytes, URL-safe; only its SHA-256 hash is stored (`refresh_tokens.token_hash`) | 14 days | httpOnly cookie, held by the Next.js server (BFF) |
| Password reset token | 32 random bytes; only its SHA-256 hash is stored (`password_reset_tokens.token_hash`) | 30 minutes, single use | Inside the emailed link only |

Protected endpoints require the header:

```
Authorization: Bearer <access_token>
```

On every request the backend loads the user by `sub` (a primary-key lookup) and rejects the request if the user no longer exists or has `is_active = false`. Deactivating a user therefore takes effect immediately, without waiting for their access token to expire.

### 2.2 Refresh rotation and reuse detection (A-11)

Each call to `POST /auth/refresh` revokes the refresh token it was given and issues a new one. If an already-revoked token is presented:

- **Revoked less than 30 seconds ago:** this is probably two browser tabs refreshing at the same moment. The response is `409 token_already_rotated`. The client simply retries its original request, which now carries the new cookie. Nothing else is revoked.
- **Revoked more than 30 seconds ago:** this looks like a stolen token being replayed. **All** of the user's refresh tokens are revoked, and the response is `401 token_reused`.

---

## 3. Errors

Every error has the same shape:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed.",
    "details": [
      { "field": "title", "message": "String should have at least 1 character" }
    ]
  },
  "request_id": "0f6c2a1e9b7d4c35"
}
```

- `code` is stable and machine-readable; the frontend switches on it.
- `message` is human-readable and may change.
- `details` is present only for validation errors.
- `request_id` also appears in the server logs, so a user's screenshot can be matched to the log line.

| HTTP | `code` | When |
|---|---|---|
| 400 | `bad_request` | Malformed request that isn't a validation issue (e.g. invalid JSON) |
| 401 | `not_authenticated` | Missing, expired or invalid access token; user inactive |
| 401 | `invalid_credentials` | Wrong email or password, or inactive user, at login (same message for all three) |
| 401 | `invalid_token` | Refresh or reset token unknown, expired, revoked or used |
| 401 | `token_reused` | Revoked refresh token replayed (section 2.2) |
| 403 | `permission_denied` | User can see the resource but lacks the action permission |
| 404 | `not_found` | Resource doesn't exist, belongs to another organization, or the user has no access to its project |
| 405 | `method_not_allowed` | The URL exists but not for this HTTP method, e.g. `DELETE` on a space (spaces are never deleted, S-4) |
| 409 | `conflict` | Unique rule violated (details name the field), e.g. email, slug, task prefix or space name already used |
| 409 | `stale_update` | `expected_updated_at` doesn't match: someone else saved first (section 6) |
| 409 | `project_archived` | Write attempted on an archived project |
| 409 | `status_in_use` | Deleting a status that tasks still use, without `move_to` |
| 409 | `last_status` | Deleting a project's only status |
| 409 | `role_in_use` | Deleting a role that is still assigned |
| 409 | `already_member` | User already has a role in this project |
| 409 | `token_already_rotated` | Concurrent refresh (section 2.2) |
| 409 | `self_lockout` | Action would remove your own admin access (A-13) |
| 422 | `validation_error` | Body or query parameters fail validation |
| 422 | `search_too_short` | `q` has 1 or 2 characters (minimum 3) |
| 422 | `invalid_reference` | A referenced id is not usable here (status or space from another project, assignee without project access, role from another organization) |
| 500 | `internal_error` | Unexpected server error; details only in logs |

**404 before 403.** If a user has no role at all on a project, every request under that project answers `404 not_found`, never `403`. Otherwise anyone could discover which project ids exist by probing URLs. `403` is used only when the user can view the project but lacks a specific action permission, for example a viewer trying to delete a task.

---

## 4. Lists: pagination, sorting and search

### 4.1 Pagination

| Query parameter | Default | Limits |
|---|---|---|
| `page` | 1 | ≥ 1 |
| `page_size` | 20 | 1–100 |

Response shape for every paginated list:

```json
{
  "items": [],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_items": 250,
    "total_pages": 13
  }
}
```

A `page` beyond the last page returns an empty `items` array with correct metadata, not an error.

**Why offset pagination:** users jump to page numbers and need total counts, and lists here stay in the thousands per project. A `COUNT(*)` over an indexed filter is cheap at that size. Cursor pagination would be revisited if a list grows to hundreds of thousands of rows or turns into an infinite feed.

### 4.2 Sorting

`sort=<field>&order=asc|desc`. Each endpoint lists its allowed sort fields; any other value returns `422`. The backend always adds `id` as a final tie-breaker, so rows never jump between pages when two rows share the same sort value.

### 4.3 Search (`q`)

| Rule | Detail |
|---|---|
| Minimum length | Trimmed `q` of 0 characters → ignored. 1–2 characters → `422 search_too_short`. 3–100 characters → searched. |
| Case | Case-insensitive (`ILIKE`, served by the trigram index `ix_tasks_title_trgm` for tasks, P-9) |
| Scope | The whole filtered dataset in the database, **then** paginated. `total_items` counts all matches, not just the current page. |
| Combination | Search, filters, sorting and pagination all apply together in one query |
| Task keys | On the task list, a `q` that looks like a task key (e.g. `WEB-42`) also matches that task's number exactly |

---

## 5. Permissions

### 5.1 Features and actions

`role_permissions` stores one row per role and feature, with four booleans: `view_perm`, `create_perm`, `edit_perm`, `delete_perm`.

| Feature | Scope | Controls |
|---|---|---|
| `organization` | Organization | Organization details |
| `users` | Organization | User accounts (list, create, deactivate). Added to the P-5 list (A-4). |
| `roles` | Organization | Role definitions, permissions, org-wide role assignment |
| `projects` | Both | `create` and "see all projects" need an org-wide role; view/edit/delete apply per project |
| `members` | Project | Who has which role in a project |
| `teams` | Project | Teams and team membership |
| `spaces` | Project | Spaces inside a project (A-14, S-6). Uses view/create/edit; `delete` is unused because spaces are never deleted (S-4). Moving a task between spaces is a task edit (`tasks.edit`). |
| `task_statuses` | Project | Workflow statuses |
| `tasks` | Project | Tasks |
| `comments` | Project | Task comments (with author rules, A-6) |

### 5.2 How the effective permission is resolved (A-5)

1. Org-scope features (`organization`, `users`, `roles`, `projects.create`) come **only** from the user's org-wide role (`user_roles.project_id IS NULL`).
2. Project-scope features combine the org-wide role and the project role with **OR**. If either role allows an action, it's allowed. An org admin keeps full access even if someone also gives them a "Viewer" role on one project.
3. If the user has neither an org-wide role nor a role on this project, the project doesn't exist for them: `404`.
4. The result is calculated once per request in one query (`user_roles` joined with `role_permissions`) and reused by every check in that request.

### 5.3 Default Admin role

`POST /auth/register` creates a role named `Admin` with all four actions on all ten features, and assigns it org-wide to the registering user (A-2). Every other role is created by admins.

---

## 6. Concurrency rules

| Situation | Behaviour |
|---|---|
| Two users edit the same task, project, team, space, status or comment | Edit requests send `expected_updated_at`, the `updated_at` value the client loaded. The update runs `WHERE id = … AND updated_at = :expected` and, if no row matched, returns `409 stale_update` with the current version in `error.details`, so the UI can show "This was changed by someone else" and reload (P-11). |
| Single-field task actions (status, assignee, priority) | `expected_updated_at` is optional. Without it, the last write wins. That's acceptable for one-field changes like dragging a card. |
| Two users create tasks in the same project at once | Both succeed with different numbers. The atomic `UPDATE projects SET next_task_number = next_task_number + 1 … RETURNING` serializes them on the project row, and `uq_tasks_project_id_number` is the final guard. |
| Create Task clicked three times quickly | Three requests are three legitimate tasks. The frontend prevents this by disabling the button while a request is pending. No idempotency keys in the POC (D1, section 9). |
| Add the same team member twice | `INSERT … ON CONFLICT DO NOTHING`: the second request succeeds and reports the user as `skipped`. No error, no duplicate. |
| Add the same project member twice | The second request gets `409 already_member` (unique `(user_id, project_id)`, P-6) |
| Same thing created twice where a unique rule exists (email, slug, task prefix) | The second request gets `409 conflict`. The unique constraint decides, not a check-then-insert race. |
| Delete the same thing twice | The second request gets `404` |
| Lock order inside transactions | Always project row → space rows (ascending id) → status rows (ascending id) → task rows (ascending id). Using one fixed order prevents deadlocks. |
| Deadlock or serialization failure anyway | Retried up to 3 times with jittered backoff (`app/db/transaction.py`, B44). Only for operations that are safe to repeat. |

---

## 7. Endpoint index

| ID | Method | Path | Purpose |
|---|---|---|---|
| SYS-1 | GET | `/health` | Liveness and database check |
| AUTH-1 | POST | `/auth/register` | Create organization + first admin |
| AUTH-2 | POST | `/auth/login` | Log in |
| AUTH-3 | POST | `/auth/refresh` | Rotate refresh token, get new access token |
| AUTH-4 | POST | `/auth/logout` | Revoke the current refresh token |
| AUTH-5 | POST | `/auth/password-reset/request` | Email a reset link |
| AUTH-6 | POST | `/auth/password-reset/confirm` | Set a new password with a reset token |
| ORG-1 | GET | `/organization` | Current organization |
| ORG-2 | PATCH | `/organization` | Update organization |
| USR-1 | GET | `/users/me` | Current user, org role and org permissions |
| USR-2 | PATCH | `/users/me` | Update own name |
| USR-3 | GET | `/users` | List organization users |
| USR-4 | POST | `/users` | Create a user (sends set-password link) |
| USR-5 | PATCH | `/users/{user_id}` | Update or deactivate a user |
| USR-6 | PUT | `/users/{user_id}/org-role` | Set a user's org-wide role |
| USR-7 | DELETE | `/users/{user_id}/org-role` | Remove a user's org-wide role |
| ROLE-1 | GET | `/roles` | List roles with permissions |
| ROLE-2 | POST | `/roles` | Create role with permissions |
| ROLE-3 | PATCH | `/roles/{role_id}` | Rename role and/or replace permissions |
| ROLE-4 | DELETE | `/roles/{role_id}` | Delete an unassigned role |
| PRJ-1 | POST | `/projects` | Create project (+ default statuses and "General" space) |
| PRJ-2 | GET | `/projects` | List accessible projects |
| PRJ-3 | GET | `/projects/{public_id}` | Project details, task counts, my permissions |
| PRJ-4 | PATCH | `/projects/{public_id}` | Update project |
| PRJ-5 | POST | `/projects/{public_id}/archive` | Archive project |
| PRJ-6 | POST | `/projects/{public_id}/unarchive` | Restore archived project |
| MEM-1 | GET | `/projects/{public_id}/members` | List members (also the assignee picker) |
| MEM-2 | POST | `/projects/{public_id}/members` | Give a user a role in the project |
| MEM-3 | PATCH | `/projects/{public_id}/members/{user_id}` | Change a member's role |
| MEM-4 | DELETE | `/projects/{public_id}/members/{user_id}` | Remove a member |
| TEAM-1 | GET | `/projects/{public_id}/teams` | List teams |
| TEAM-2 | POST | `/projects/{public_id}/teams` | Create team |
| TEAM-3 | GET | `/projects/{public_id}/teams/{team_id}` | Team details |
| TEAM-4 | PATCH | `/projects/{public_id}/teams/{team_id}` | Update team |
| TEAM-5 | DELETE | `/projects/{public_id}/teams/{team_id}` | Delete team |
| TEAM-6 | GET | `/projects/{public_id}/teams/{team_id}/members` | List team members |
| TEAM-7 | POST | `/projects/{public_id}/teams/{team_id}/members` | Add team members |
| TEAM-8 | DELETE | `/projects/{public_id}/teams/{team_id}/members/{user_id}` | Remove team member |
| SPC-1 | GET | `/projects/{public_id}/spaces` | List spaces in display order (paginated, with task counts) |
| SPC-2 | POST | `/projects/{public_id}/spaces` | Create space |
| SPC-3 | GET | `/projects/{public_id}/spaces/{space_id}` | Space details |
| SPC-4 | PATCH | `/projects/{public_id}/spaces/{space_id}` | Rename space or change its description |
| SPC-5 | PUT | `/projects/{public_id}/spaces/order` | Reorder spaces |
| STS-1 | GET | `/projects/{public_id}/statuses` | List statuses in board order |
| STS-2 | POST | `/projects/{public_id}/statuses` | Create status |
| STS-3 | PATCH | `/projects/{public_id}/statuses/{status_id}` | Rename, recolour or recategorize |
| STS-4 | PUT | `/projects/{public_id}/statuses/order` | Reorder statuses |
| STS-5 | DELETE | `/projects/{public_id}/statuses/{status_id}` | Delete status (optionally moving its tasks) |
| TSK-1 | GET | `/projects/{public_id}/tasks` | List, filter, search, sort, paginate tasks |
| TSK-2 | POST | `/projects/{public_id}/tasks` | Create task |
| TSK-3 | GET | `/projects/{public_id}/tasks/{number}` | Task details |
| TSK-4 | PATCH | `/projects/{public_id}/tasks/{number}` | Update task (edit form) |
| TSK-5 | PATCH | `/projects/{public_id}/tasks/{number}/status` | Change status |
| TSK-6 | PATCH | `/projects/{public_id}/tasks/{number}/assignee` | Assign or unassign |
| TSK-7 | PATCH | `/projects/{public_id}/tasks/{number}/priority` | Change priority |
| TSK-8 | DELETE | `/projects/{public_id}/tasks/{number}` | Delete task |
| TSK-9 | PATCH | `/projects/{public_id}/tasks/{number}/space` | Move task to another space |
| CMT-1 | GET | `/projects/{public_id}/tasks/{number}/comments` | List comments |
| CMT-2 | POST | `/projects/{public_id}/tasks/{number}/comments` | Add comment |
| CMT-3 | PATCH | `/projects/{public_id}/tasks/{number}/comments/{comment_id}` | Edit own comment |
| CMT-4 | DELETE | `/projects/{public_id}/tasks/{number}/comments/{comment_id}` | Delete comment |

All paths are relative to `/api/v1`. "Common errors" in the endpoint sections below means `401 not_authenticated`, `422 validation_error` and `500 internal_error`, which every protected endpoint can return.

---

## 8. System and authentication

### SYS-1 `GET /health`

| | |
|---|---|
| Purpose | Load balancer and uptime check. Runs `SELECT 1`. |
| Auth | None |
| Tables | None |
| Errors | `503` with `{"status": "unavailable"}` if the database is unreachable |

Response `200`:

```json
{ "status": "ok", "database": "ok" }
```

### AUTH-1 `POST /auth/register`

| | |
|---|---|
| Purpose | Sign-up: creates an organization, its `Admin` role with all permissions, the first user, and the org-wide role assignment, all in one transaction. Logs the user in. (P-8, A-2) |
| Auth | None |
| Tables | organizations, roles, role_permissions, users, user_roles, refresh_tokens |
| Errors | `409 conflict` (`email` or `slug` taken), `422` |

Request:

```json
{
  "organization_name": "Acme Corp",
  "organization_slug": "acme",
  "name": "Asha Rao",
  "email": "asha@acme.com",
  "password": "correct-horse-battery"
}
```

- `organization_slug` is optional. If it's left out, it's generated from the name (`"Acme Corp"` → `"acme-corp"`).
- `email` is lowercased before saving.
- `password` must be 8–128 characters (A-12).

Response `201`: same body as AUTH-2.

### AUTH-2 `POST /auth/login`

| | |
|---|---|
| Purpose | Verify email and password; issue tokens |
| Auth | None |
| Tables | users (read), refresh_tokens (insert) |
| Errors | `401 invalid_credentials`, `422` |

Request:

```json
{ "email": "asha@acme.com", "password": "correct-horse-battery" }
```

Response `200`:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 900,
  "refresh_token": "q8D3tY...",
  "refresh_expires_in": 1209600,
  "user": { "id": 1, "name": "Asha Rao", "email": "asha@acme.com" }
}
```

A wrong password, an unknown email and an inactive account all return the same `invalid_credentials` message, and take about the same time to answer (a dummy hash is checked when the email is unknown). This stops attackers from finding out which emails have accounts.

### AUTH-3 `POST /auth/refresh`

| | |
|---|---|
| Purpose | Exchange a valid refresh token for a new access token and a new refresh token (rotation, section 2.2) |
| Auth | None (the refresh token is the credential) |
| Tables | refresh_tokens (`SELECT … FOR UPDATE`, update, insert), users |
| Errors | `401 invalid_token`, `401 token_reused`, `409 token_already_rotated`, `422` |

Request:

```json
{ "refresh_token": "q8D3tY..." }
```

Response `200`: same as AUTH-2, without `user`.

### AUTH-4 `POST /auth/logout`

| | |
|---|---|
| Purpose | Revoke this session's refresh token |
| Auth | None (the refresh token identifies the session) |
| Tables | refresh_tokens |
| Errors | None. Always `204`, even for unknown tokens, so repeated logout clicks are harmless. |

Request:

```json
{ "refresh_token": "q8D3tY..." }
```

Response: `204 No Content`.

### AUTH-5 `POST /auth/password-reset/request`

| | |
|---|---|
| Purpose | If an active user has this email, invalidate their older unused reset tokens, create a new one and send the link (A-3) |
| Auth | None |
| Tables | users, password_reset_tokens |
| Errors | `422` only |

Request:

```json
{ "email": "asha@acme.com" }
```

Response `202`, identical whether or not the email exists:

```json
{ "message": "If an account exists for this email, a reset link has been sent." }
```

### AUTH-6 `POST /auth/password-reset/confirm`

| | |
|---|---|
| Purpose | Set a new password, mark the token used, and revoke **all** of the user's refresh tokens (every device is logged out) |
| Auth | None |
| Tables | password_reset_tokens (`FOR UPDATE`), users, refresh_tokens |
| Errors | `401 invalid_token` (unknown, expired or already used), `422` |

Request:

```json
{ "token": "Zx81...", "new_password": "another-long-password" }
```

Response: `204 No Content`. The token row is locked while it's used, so two simultaneous confirms with the same token can't both succeed.

---

## 9. Organization and users

### ORG-1 `GET /organization`

| | |
|---|---|
| Purpose | Details of the caller's organization |
| Auth | Required |
| Permission | `organization.view` (org-wide) |
| Tables | organizations |
| Errors | Common, `403` |

Response `200`:

```json
{
  "name": "Acme Corp",
  "slug": "acme",
  "description": "Product engineering",
  "created_at": "2026-11-16T07:30:00Z",
  "updated_at": "2026-11-16T07:30:00Z"
}
```

### ORG-2 `PATCH /organization`

| | |
|---|---|
| Purpose | Update name or description. `slug` can't be changed (A-8). |
| Auth | Required |
| Permission | `organization.edit` |
| Tables | organizations |
| Errors | Common, `403`, `409 stale_update` |

Request:

```json
{
  "name": "Acme Corporation",
  "description": null,
  "expected_updated_at": "2026-11-16T07:30:00Z"
}
```

Response `200`: same as ORG-1.

### USR-1 `GET /users/me`

| | |
|---|---|
| Purpose | Who am I: profile, organization, org-wide role and org-level permissions. The frontend uses it to build the navigation (e.g. show "New project" only if `projects.create`). |
| Auth | Required |
| Permission | None |
| Tables | users, organizations, user_roles, roles, role_permissions |
| Errors | Common |

Response `200`:

```json
{
  "id": 1,
  "name": "Asha Rao",
  "email": "asha@acme.com",
  "organization": { "name": "Acme Corp", "slug": "acme" },
  "org_role": { "id": 1, "name": "Admin" },
  "org_permissions": {
    "organization": { "view": true, "create": true, "edit": true, "delete": true },
    "users":        { "view": true, "create": true, "edit": true, "delete": true },
    "roles":        { "view": true, "create": true, "edit": true, "delete": true },
    "projects":     { "view": true, "create": true, "edit": true, "delete": true }
  }
}
```

`org_role` is `null` for users who only have project roles. Their `org_permissions` are then all `false`.

### USR-2 `PATCH /users/me`

| | |
|---|---|
| Purpose | Change own display name |
| Auth | Required |
| Permission | None |
| Tables | users |
| Errors | Common |

Request:

```json
{ "name": "Asha R." }
```

Response `200`: same as USR-1.

### USR-3 `GET /users`

| | |
|---|---|
| Purpose | User administration list for the organization |
| Auth | Required |
| Permission | `users.view` |
| Query | `q` (name or email, min 3), `is_active` (`true`/`false`), `page`, `page_size`, `sort` = `name` \| `email` \| `created_at` (default `name`), `order` |
| Tables | users, user_roles, roles |
| Errors | Common, `403`, `422 search_too_short` |

Response `200` (paginated):

```json
{
  "items": [
    {
      "id": 2,
      "name": "Ravi Kumar",
      "email": "ravi@acme.com",
      "is_active": true,
      "org_role": null,
      "created_at": "2026-11-16T08:00:00Z",
      "updated_at": "2026-11-16T08:00:00Z"
    }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 1, "total_pages": 1 }
}
```

### USR-4 `POST /users`

| | |
|---|---|
| Purpose | Create a user in the caller's organization. The account gets an unusable random password, and a set-password link is sent through the password-reset flow (P-8, A-3). |
| Auth | Required |
| Permission | `users.create` |
| Tables | users, password_reset_tokens |
| Errors | Common, `403`, `409 conflict` (`email`) |

Request:

```json
{ "name": "Ravi Kumar", "email": "ravi@acme.com" }
```

Response `201`: one item as in USR-3. The set-password link expires after 30 minutes; an admin can send a new one with AUTH-5.

### USR-5 `PATCH /users/{user_id}`

| | |
|---|---|
| Purpose | Rename, deactivate or reactivate a user. Deactivating also revokes all their refresh tokens in the same transaction. |
| Auth | Required |
| Permission | `users.edit` |
| Tables | users, refresh_tokens |
| Errors | Common, `403`, `404`, `409 stale_update`, `409 self_lockout` (deactivating yourself) |

Request:

```json
{ "is_active": false, "expected_updated_at": "2026-11-16T08:00:00Z" }
```

Response `200`: one item as in USR-3.

### USR-6 `PUT /users/{user_id}/org-role`

| | |
|---|---|
| Purpose | Give or replace the user's org-wide role (`user_roles` row with `project_id = NULL`). `PUT` because it sets the one org-wide role, replacing any existing one. |
| Auth | Required |
| Permission | `roles.edit` |
| Tables | user_roles, roles, users |
| Errors | Common, `403`, `404`, `422 invalid_reference` (role from another organization), `409 self_lockout` (changing your own org role) |

Request:

```json
{ "role_id": 3 }
```

Response `200`:

```json
{ "user_id": 2, "role": { "id": 3, "name": "Manager" } }
```

### USR-7 `DELETE /users/{user_id}/org-role`

| | |
|---|---|
| Purpose | Remove the user's org-wide role. Their project roles stay. |
| Auth | Required |
| Permission | `roles.edit` |
| Tables | user_roles |
| Errors | Common, `403`, `404`, `409 self_lockout` |

Response: `204 No Content`.

---

## 10. Roles

### ROLE-1 `GET /roles`

| | |
|---|---|
| Purpose | All roles of the organization with their permissions. Not paginated: organizations have a handful of roles. |
| Auth | Required |
| Permission | `roles.view` |
| Tables | roles, role_permissions, user_roles (assignment count) |
| Errors | Common, `403` |

Response `200`:

```json
{
  "items": [
    {
      "id": 3,
      "name": "Developer",
      "assigned_count": 5,
      "permissions": [
        { "feature": "tasks", "view": true, "create": true, "edit": true, "delete": false },
        { "feature": "comments", "view": true, "create": true, "edit": true, "delete": false }
      ],
      "created_at": "2026-11-16T08:10:00Z",
      "updated_at": "2026-11-16T08:10:00Z"
    }
  ]
}
```

Features with no row are returned with all four values `false`, so every role always lists all ten features. The JSON keys `view` / `create` / `edit` / `delete` map to the columns `view_perm` / `create_perm` / `edit_perm` / `delete_perm`.

### ROLE-2 `POST /roles`

| | |
|---|---|
| Purpose | Create a role and its permission rows in one transaction |
| Auth | Required |
| Permission | `roles.create` |
| Tables | roles, role_permissions |
| Errors | Common, `403`, `422` (unknown feature, duplicate feature in the list) |

Request:

```json
{
  "name": "Developer",
  "permissions": [
    { "feature": "projects", "view": true, "create": false, "edit": false, "delete": false },
    { "feature": "tasks", "view": true, "create": true, "edit": true, "delete": false }
  ]
}
```

Response `201`: one item as in ROLE-1.

### ROLE-3 `PATCH /roles/{role_id}`

| | |
|---|---|
| Purpose | Rename and/or **replace** the role's permissions. If `permissions` is sent, it becomes the complete list; features not listed are set to all `false`. |
| Auth | Required |
| Permission | `roles.edit` |
| Tables | roles, role_permissions |
| Errors | Common, `403`, `404`, `409 stale_update`, `409 self_lockout` (removing `roles.edit` from the role you hold org-wide) |

Request:

```json
{
  "name": "Senior Developer",
  "permissions": [
    { "feature": "tasks", "view": true, "create": true, "edit": true, "delete": true }
  ],
  "expected_updated_at": "2026-11-16T08:10:00Z"
}
```

Response `200`: one item as in ROLE-1.

### ROLE-4 `DELETE /roles/{role_id}`

| | |
|---|---|
| Purpose | Delete a role nobody holds. Its permission rows go with it (CASCADE). |
| Auth | Required |
| Permission | `roles.delete` |
| Tables | roles, role_permissions, user_roles (usage check) |
| Errors | Common, `403`, `404`, `409 role_in_use` |

Response: `204 No Content`.

---

## 11. Projects and members

### PRJ-1 `POST /projects`

| | |
|---|---|
| Purpose | Create a project with a generated `public_id`, `next_task_number = 1`, three default statuses (A-1) and a default "General" space (A-15), in one transaction |
| Auth | Required |
| Permission | `projects.create` (org-wide) |
| Tables | projects, task_statuses, spaces |
| Errors | Common, `403`, `409 conflict` (`task_prefix` already used in this organization) |

Request:

```json
{
  "name": "Website Revamp",
  "task_prefix": "WEB",
  "description": "New marketing site",
  "start_date": "2026-12-01",
  "end_date": "2027-03-31"
}
```

`task_prefix` is uppercased and must match `^[A-Z][A-Z0-9]{1,9}$` (P-5). `status` starts as `active`.

Default statuses: `To Do` (`todo`, `#64748B`, position 0), `In Progress` (`in_progress`, `#3B82F6`, position 1), `Done` (`done`, `#22C55E`, position 2).

Default space: `General` (position 0, no description).

Response `201`: same as PRJ-3.

### PRJ-2 `GET /projects`

| | |
|---|---|
| Purpose | Projects the caller can see. A user with org-wide `projects.view` sees all organization projects; anyone else sees projects where their project role grants `projects.view`. |
| Auth | Required |
| Permission | Filtered by access (no 403) |
| Query | `q` (name, min 3), `status` (default: everything except `archived`; pass `archived` to see archived ones), `page`, `page_size`, `sort` = `name` \| `created_at` \| `updated_at` \| `end_date` (default `updated_at`), `order` (default `desc`) |
| Tables | projects, user_roles, role_permissions |
| Errors | Common, `422 search_too_short` |

Response `200` (paginated):

```json
{
  "items": [
    {
      "public_id": "k3x9p2m7qa1z",
      "name": "Website Revamp",
      "status": "active",
      "task_prefix": "WEB",
      "start_date": "2026-12-01",
      "end_date": "2027-03-31",
      "updated_at": "2026-11-16T09:00:00Z"
    }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 1, "total_pages": 1 }
}
```

The list deliberately leaves out `description` and counts. They aren't shown on the list screen, so fetching them would only slow the query down.

### PRJ-3 `GET /projects/{public_id}`

| | |
|---|---|
| Purpose | Project header data, task counts per status category (one `GROUP BY` query), and the caller's effective permissions in this project, so one request serves the whole project page |
| Auth | Required |
| Permission | `projects.view` (project) |
| Tables | projects, tasks, task_statuses, user_roles, role_permissions |
| Errors | Common, `404` |

Response `200`:

```json
{
  "public_id": "k3x9p2m7qa1z",
  "name": "Website Revamp",
  "status": "active",
  "description": "New marketing site",
  "task_prefix": "WEB",
  "start_date": "2026-12-01",
  "end_date": "2027-03-31",
  "created_at": "2026-11-16T09:00:00Z",
  "updated_at": "2026-11-16T09:00:00Z",
  "task_counts": { "todo": 12, "in_progress": 4, "done": 30, "total": 46 },
  "my_permissions": {
    "projects":      { "view": true, "create": false, "edit": true, "delete": false },
    "members":       { "view": true, "create": false, "edit": false, "delete": false },
    "teams":         { "view": true, "create": true, "edit": true, "delete": false },
    "spaces":        { "view": true, "create": false, "edit": false, "delete": false },
    "task_statuses": { "view": true, "create": false, "edit": false, "delete": false },
    "tasks":         { "view": true, "create": true, "edit": true, "delete": false },
    "comments":      { "view": true, "create": true, "edit": true, "delete": false }
  }
}
```

### PRJ-4 `PATCH /projects/{public_id}`

| | |
|---|---|
| Purpose | Update name, description, status (`active` / `on_hold` / `completed`) and dates. `task_prefix` can't be changed, because every task key would change (A-8). `archived` is set only through PRJ-5. |
| Auth | Required |
| Permission | `projects.edit` (project) |
| Tables | projects |
| Errors | Common, `403`, `404`, `409 stale_update`, `409 project_archived`, `422` (end date before start date) |

Request:

```json
{
  "status": "on_hold",
  "end_date": "2027-04-30",
  "expected_updated_at": "2026-11-16T09:00:00Z"
}
```

Response `200`: same as PRJ-3.

### PRJ-5 `POST /projects/{public_id}/archive` and PRJ-6 `POST /projects/{public_id}/unarchive`

| | |
|---|---|
| Purpose | Archive sets `status = 'archived'`; the project becomes read-only (A-9). Unarchive sets it back to `active`. Projects are never hard-deleted through the API (P-4). |
| Auth | Required |
| Permission | `projects.delete` (project) |
| Tables | projects |
| Errors | Common, `403`, `404`. Archiving an archived project, or unarchiving an active one, returns `200` with no change, so a repeated click is harmless. |

Request: no body. Response `200`: same as PRJ-3.

**Archived projects:** every write under an archived project (tasks, comments, statuses, teams, members, PRJ-4) returns `409 project_archived`. Reads still work.

### MEM-1 `GET /projects/{public_id}/members`

| | |
|---|---|
| Purpose | Who can access this project: users with a role on this project, plus users with an org-wide role. It also feeds the assignee and team-member pickers. |
| Auth | Required |
| Permission | `members.view` or `tasks.view` (project). Task editors need the picker even without member-management rights. |
| Query | `q` (name, min 3), `page`, `page_size` (pickers use 20), `sort` = `name` (default) |
| Tables | user_roles, users, roles |
| Errors | Common, `403`, `404`, `422 search_too_short` |

Response `200` (paginated):

```json
{
  "items": [
    { "user": { "id": 1, "name": "Asha Rao" }, "role": { "id": 1, "name": "Admin" }, "scope": "organization" },
    { "user": { "id": 2, "name": "Ravi Kumar" }, "role": { "id": 3, "name": "Developer" }, "scope": "project" }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 2, "total_pages": 1 }
}
```

Emails aren't included. Pickers only need names, and emails are listed only for users with `users.view` (USR-3). Inactive users are excluded.

### MEM-2 `POST /projects/{public_id}/members`

| | |
|---|---|
| Purpose | Give an organization user a role in this project |
| Auth | Required |
| Permission | `members.create` (project) |
| Tables | user_roles, users, roles |
| Errors | Common, `403`, `404`, `409 already_member`, `409 project_archived`, `422 invalid_reference` (user or role from another organization, inactive user) |

Request:

```json
{ "user_id": 2, "role_id": 3 }
```

Response `201`: one item as in MEM-1.

### MEM-3 `PATCH /projects/{public_id}/members/{user_id}`

| | |
|---|---|
| Purpose | Change a member's project role. Only project-scope rows can be changed here; org-wide roles are managed with USR-6. |
| Auth | Required |
| Permission | `members.edit` (project) |
| Tables | user_roles, roles |
| Errors | Common, `403`, `404` (no project role for this user), `409 project_archived`, `422 invalid_reference` |

Request:

```json
{ "role_id": 4 }
```

Response `200`: one item as in MEM-1.

### MEM-4 `DELETE /projects/{public_id}/members/{user_id}`

| | |
|---|---|
| Purpose | Remove the user's project role. In the same transaction, their tasks in this project become unassigned and they're removed from this project's teams (A-7). |
| Auth | Required |
| Permission | `members.delete` (project) |
| Tables | user_roles, tasks, team_members, teams |
| Errors | Common, `403`, `404`, `409 project_archived` |

Response: `204 No Content`.

---

## 12. Teams, spaces, statuses, tasks and comments

### TEAM-1 `GET /projects/{public_id}/teams`

| | |
|---|---|
| Purpose | Teams of the project with a member count (one aggregate query, no N+1) |
| Auth | Required |
| Permission | `teams.view` |
| Query | `q` (name, min 3), `page`, `page_size`, `sort` = `name` (default) \| `created_at` |
| Tables | teams, team_members |
| Errors | Common, `403`, `404`, `422 search_too_short` |

Response `200` (paginated):

```json
{
  "items": [
    {
      "id": 7,
      "name": "Frontend",
      "description": "UI squad",
      "member_count": 4,
      "created_at": "2026-11-16T09:30:00Z",
      "updated_at": "2026-11-16T09:30:00Z"
    }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 1, "total_pages": 1 }
}
```

### TEAM-2 `POST /projects/{public_id}/teams`

| | |
|---|---|
| Permission | `teams.create` |
| Tables | teams |
| Errors | Common, `403`, `404`, `409 project_archived` |

Request:

```json
{ "name": "Frontend", "description": "UI squad" }
```

Response `201`: one item as in TEAM-1.

### TEAM-3 `GET /projects/{public_id}/teams/{team_id}`

| | |
|---|---|
| Permission | `teams.view` |
| Tables | teams, team_members |
| Errors | Common, `403`, `404` |

Response `200`: one item as in TEAM-1.

### TEAM-4 `PATCH /projects/{public_id}/teams/{team_id}`

| | |
|---|---|
| Permission | `teams.edit` |
| Tables | teams |
| Errors | Common, `403`, `404`, `409 stale_update`, `409 project_archived` |

Request:

```json
{ "name": "Web UI", "expected_updated_at": "2026-11-16T09:30:00Z" }
```

Response `200`: one item as in TEAM-1.

### TEAM-5 `DELETE /projects/{public_id}/teams/{team_id}`

| | |
|---|---|
| Purpose | Delete the team; memberships go with it (CASCADE). Users and tasks are not affected. |
| Permission | `teams.delete` |
| Tables | teams, team_members |
| Errors | Common, `403`, `404`, `409 project_archived` |

Response: `204 No Content`.

### TEAM-6 `GET /projects/{public_id}/teams/{team_id}/members`

| | |
|---|---|
| Permission | `teams.view` |
| Query | `page`, `page_size`, `sort` = `name` (default) \| `joined_at` |
| Tables | team_members, users |
| Errors | Common, `403`, `404` |

Response `200` (paginated):

```json
{
  "items": [
    { "user": { "id": 2, "name": "Ravi Kumar" }, "joined_at": "2026-11-16T09:35:00Z" }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 1, "total_pages": 1 }
}
```

`joined_at` is `team_members.created_at`.

### TEAM-7 `POST /projects/{public_id}/teams/{team_id}/members`

| | |
|---|---|
| Purpose | Add one or more users. Each must have access to the project (MEM-1). Users already in the team are skipped, not errors (`ON CONFLICT DO NOTHING`), so retries are safe. |
| Permission | `teams.edit` |
| Tables | team_members, user_roles, users |
| Errors | Common, `403`, `404`, `409 project_archived`, `422 invalid_reference` (user has no project access) |

Request:

```json
{ "user_ids": [2, 5, 9] }
```

At most 50 ids per request.

Response `200`:

```json
{ "added": [2, 9], "skipped": [5] }
```

### TEAM-8 `DELETE /projects/{public_id}/teams/{team_id}/members/{user_id}`

| | |
|---|---|
| Permission | `teams.edit` |
| Tables | team_members |
| Errors | Common, `403`, `404`, `409 project_archived` |

Response: `204 No Content`.

### SPC-1 `GET /projects/{public_id}/spaces`

| | |
|---|---|
| Purpose | The project's spaces in display order (`position`, then `id`), each with its task count (one `GROUP BY` query). Used for the space tabs/sidebar and the space picker in the task form. |
| Permission | `spaces.view` or `tasks.view` (the task form and board need the spaces) |
| Query | `page`, `page_size` (section 4.1). The order is fixed (`position`, then `id`), so there is no `sort` parameter. |
| Tables | spaces, tasks |
| Errors | Common, `403`, `404` |

Response `200` (paginated):

```json
{
  "items": [
    {
      "id": 21,
      "name": "General",
      "description": null,
      "position": 0,
      "task_count": 8,
      "created_at": "2026-11-16T09:00:00Z",
      "updated_at": "2026-11-16T09:00:00Z"
    },
    {
      "id": 22,
      "name": "Backend",
      "description": "API and database work",
      "position": 1,
      "task_count": 15,
      "created_at": "2026-11-16T09:30:00Z",
      "updated_at": "2026-11-16T09:30:00Z"
    }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 2, "total_pages": 1 }
}
```

### SPC-2 `POST /projects/{public_id}/spaces`

| | |
|---|---|
| Purpose | Add a space at the end of the list (position = current max + 1) |
| Permission | `spaces.create` |
| Tables | spaces |
| Errors | Common, `403`, `404`, `409 conflict` (name already used in this project, S-3), `409 project_archived` |

Request:

```json
{ "name": "Backend", "description": "API and database work" }
```

`name` is 1–100 characters after trimming and must be unique within the project (`uq_spaces_project_id_name`); `description` is optional.

Response `201`: same as SPC-3.

### SPC-3 `GET /projects/{public_id}/spaces/{space_id}`

| | |
|---|---|
| Purpose | One space with its task count, for the space header and the edit form |
| Permission | `spaces.view` or `tasks.view` |
| Tables | spaces, tasks |
| Errors | Common, `403`, `404` (unknown id, or a space of another project) |

Response `200`:

```json
{
  "id": 22,
  "name": "Backend",
  "description": "API and database work",
  "position": 1,
  "task_count": 15,
  "created_at": "2026-11-16T09:30:00Z",
  "updated_at": "2026-11-16T09:30:00Z"
}
```

### SPC-4 `PATCH /projects/{public_id}/spaces/{space_id}`

| | |
|---|---|
| Purpose | Rename a space and/or change its description. Tasks keep pointing at the same `space_id`, so they follow the rename automatically. Spaces can't be deleted (S-4); an unused space can be renamed or left empty. |
| Permission | `spaces.edit` |
| Tables | spaces |
| Errors | Common, `403`, `404`, `409 conflict` (new name already used in this project), `409 stale_update`, `409 project_archived` |

Request:

```json
{ "name": "Backend API", "expected_updated_at": "2026-11-16T09:30:00Z" }
```

Response `200`: same as SPC-3.

### SPC-5 `PUT /projects/{public_id}/spaces/order`

| | |
|---|---|
| Purpose | Set the full space order in one request. `space_ids` must contain every space of the project exactly once, regardless of SPC-1 pagination. Positions become 0, 1, 2, … in one transaction, with the space rows locked in ascending id order (S-5). |
| Permission | `spaces.edit` |
| Tables | spaces |
| Errors | Common, `403`, `404`, `409 project_archived`, `422 invalid_reference` (missing, extra or duplicate ids) |

Request:

```json
{ "space_ids": [22, 21, 23] }
```

Response `200`: the first page of SPC-1 in the new order. Sending the same order twice gives the same result, so a retry is safe.

### STS-1 `GET /projects/{public_id}/statuses`

| | |
|---|---|
| Purpose | All statuses in board order (`position`, then `id`), with task counts. Not paginated: a board has a handful of columns. |
| Permission | `task_statuses.view` or `tasks.view` (the board needs the columns) |
| Tables | task_statuses, tasks |
| Errors | Common, `403`, `404` |

Response `200`:

```json
{
  "items": [
    {
      "id": 11,
      "name": "To Do",
      "color": "#64748B",
      "category": "todo",
      "position": 0,
      "task_count": 12,
      "updated_at": "2026-11-16T09:00:00Z"
    }
  ]
}
```

### STS-2 `POST /projects/{public_id}/statuses`

| | |
|---|---|
| Purpose | Add a status at the end of the board (position = current max + 1) |
| Permission | `task_statuses.create` |
| Tables | task_statuses |
| Errors | Common, `403`, `404`, `409 project_archived` |

Request:

```json
{ "name": "In Review", "color": "#A855F7", "category": "in_progress" }
```

Response `201`: one item as in STS-1.

### STS-3 `PATCH /projects/{public_id}/statuses/{status_id}`

| | |
|---|---|
| Purpose | Rename, recolour and/or change category. Tasks keep pointing at the same `status_id`, so they follow the rename automatically. |
| Permission | `task_statuses.edit` |
| Tables | task_statuses |
| Errors | Common, `403`, `404`, `409 stale_update`, `409 project_archived` |

Request:

```json
{ "name": "Code Review", "color": "#9333EA", "expected_updated_at": "2026-11-16T09:00:00Z" }
```

Response `200`: one item as in STS-1.

### STS-4 `PUT /projects/{public_id}/statuses/order`

| | |
|---|---|
| Purpose | Set the full board order in one request. `status_ids` must contain every status of the project exactly once. Positions become 0, 1, 2, … in one transaction, with the status rows locked in ascending id order. |
| Permission | `task_statuses.edit` |
| Tables | task_statuses |
| Errors | Common, `403`, `404`, `409 project_archived`, `422 invalid_reference` (missing, extra or duplicate ids) |

Request:

```json
{ "status_ids": [11, 14, 12, 13] }
```

Response `200`: same as STS-1. Sending the same order twice gives the same result, so a retry is safe.

### STS-5 `DELETE /projects/{public_id}/statuses/{status_id}`

| | |
|---|---|
| Purpose | Delete a status. If tasks use it, `move_to` must name another status of the same project; those tasks are moved and the status deleted in one transaction. |
| Permission | `task_statuses.delete` (moving the tasks needs no extra permission) |
| Query | `move_to` (status id, optional) |
| Tables | task_statuses, tasks |
| Errors | Common, `403`, `404`, `409 status_in_use` (tasks exist and no `move_to`; `details` include the task count), `409 last_status`, `409 project_archived`, `422 invalid_reference` (`move_to` is the same status or from another project) |

Response: `204 No Content`.

### TSK-1 `GET /projects/{public_id}/tasks`

| | |
|---|---|
| Purpose | The task list and board: filter, search, sort and paginate in **one** query. Space, status and assignee are joined in the same query, so there are no per-task lookups (no N+1). Leave out `space_id` for the project-wide view across all spaces. |
| Permission | `tasks.view` |
| Query | `q` (title, or task key like `WEB-42`; min 3), `space_id` (one space's board or list), `status_id`, `category` (`todo`/`in_progress`/`done`), `assignee_id`, `unassigned` (`true`), `priority` (repeatable: `priority=high&priority=urgent`), `page`, `page_size`, `sort` = `created_at` \| `updated_at` \| `number` \| `priority` \| `end_date` \| `title` (default `created_at`), `order` (default `desc`) |
| Tables | tasks, spaces, task_statuses, users |
| Errors | Common, `403`, `404`, `422 search_too_short`, `422 invalid_reference` (`space_id` from another project) |

Response `200` (paginated):

```json
{
  "items": [
    {
      "number": 42,
      "key": "WEB-42",
      "title": "Fix login redirect",
      "priority": "high",
      "space": { "id": 22, "name": "Backend" },
      "status": { "id": 12, "name": "In Progress", "color": "#3B82F6", "category": "in_progress" },
      "assignee": { "id": 2, "name": "Ravi Kumar" },
      "start_date": null,
      "end_date": "2026-11-30",
      "created_at": "2026-11-16T10:00:00Z",
      "updated_at": "2026-11-16T10:05:00Z"
    }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 1, "total_pages": 1 }
}
```

`description` isn't returned in lists; TSK-3 returns it. `priority` sorts by severity (urgent > high > medium > low), not alphabetically.

### TSK-2 `POST /projects/{public_id}/tasks`

| | |
|---|---|
| Purpose | Create a task. The number comes from the atomic `next_task_number` update (section 6). If `status_id` is left out, the first `todo`-category status by position is used (or the first status, if there is no `todo` status). If `space_id` is left out, the task goes into the project's "General" space, the default space created with the project (S-2). |
| Permission | `tasks.create` |
| Tables | projects (`UPDATE … RETURNING`), tasks, spaces, task_statuses, user_roles (assignee check) |
| Errors | Common, `403`, `404`, `409 project_archived`, `422 invalid_reference` (space or status from another project; assignee without project access) |

Request:

```json
{
  "title": "Fix login redirect",
  "description": "After login, users land on /undefined.",
  "space_id": 22,
  "status_id": 11,
  "assignee_id": 2,
  "priority": "high",
  "start_date": null,
  "end_date": "2026-11-30"
}
```

Only `title` (1–255 characters) is required. `priority` defaults to `medium`.

Response `201`: same as TSK-3.

### TSK-3 `GET /projects/{public_id}/tasks/{number}`

| | |
|---|---|
| Purpose | Full task details for the task page |
| Permission | `tasks.view` |
| Tables | tasks, spaces, task_statuses, users, task_comments (count) |
| Errors | Common, `403`, `404` |

Response `200`:

```json
{
  "number": 42,
  "key": "WEB-42",
  "title": "Fix login redirect",
  "description": "After login, users land on /undefined.",
  "priority": "high",
  "space": { "id": 22, "name": "Backend" },
  "status": { "id": 12, "name": "In Progress", "color": "#3B82F6", "category": "in_progress" },
  "assignee": { "id": 2, "name": "Ravi Kumar" },
  "created_by": { "id": 1, "name": "Asha Rao" },
  "start_date": null,
  "end_date": "2026-11-30",
  "comment_count": 3,
  "created_at": "2026-11-16T10:00:00Z",
  "updated_at": "2026-11-16T10:05:00Z"
}
```

### TSK-4 `PATCH /projects/{public_id}/tasks/{number}`

| | |
|---|---|
| Purpose | Save the edit form. `expected_updated_at` is **required** (section 6). |
| Permission | `tasks.edit` |
| Tables | tasks, spaces, task_statuses, user_roles |
| Errors | Common, `403`, `404`, `409 stale_update`, `409 project_archived`, `422 invalid_reference` |

Request (any subset of TSK-2 fields, plus the version check):

```json
{
  "title": "Fix login redirect on Safari",
  "end_date": "2026-12-05",
  "expected_updated_at": "2026-11-16T10:05:00Z"
}
```

Response `200`: same as TSK-3.

### TSK-5 / TSK-6 / TSK-7 / TSK-9 Quick actions

| ID | Path suffix | Request | Rule |
|---|---|---|---|
| TSK-5 | `/status` | `{ "status_id": 13 }` | Status must belong to this project |
| TSK-6 | `/assignee` | `{ "assignee_id": 5 }` or `{ "assignee_id": null }` | Assignee must have project access; `null` unassigns |
| TSK-7 | `/priority` | `{ "priority": "urgent" }` | One of `low`, `medium`, `high`, `urgent` |
| TSK-9 | `/space` | `{ "space_id": 23 }` | Space must belong to this project. The task keeps its number and status. |

| | |
|---|---|
| Purpose | One-field changes from the board, list or task page (drag a card, pick an assignee, change priority, move to another space) |
| Method | `PATCH /projects/{public_id}/tasks/{number}/<suffix>` |
| Permission | `tasks.edit` |
| Concurrency | `expected_updated_at` is optional. If sent, it's checked; if not, the last write wins (section 6). |
| Tables | tasks (+ task_statuses, user_roles or spaces for the reference check) |
| Errors | Common, `403`, `404`, `409 stale_update` (only if `expected_updated_at` was sent), `409 project_archived`, `422 invalid_reference` |

Response `200`: same as TSK-3.

### TSK-8 `DELETE /projects/{public_id}/tasks/{number}`

| | |
|---|---|
| Purpose | Hard-delete the task; its comments go with it (CASCADE). The number is **not** reused, because `next_task_number` never goes down, so `WEB-42` always means the same task. |
| Permission | `tasks.delete` |
| Tables | tasks, task_comments |
| Errors | Common, `403`, `404`, `409 project_archived` |

Response: `204 No Content`.

### CMT-1 `GET /projects/{public_id}/tasks/{number}/comments`

| | |
|---|---|
| Purpose | A task's comments, oldest first (served by `ix_task_comments_task_id_created_at`), with author names joined in |
| Permission | `comments.view` |
| Query | `page`, `page_size`, `order` (`asc` default, `desc`) |
| Tables | task_comments, users |
| Errors | Common, `403`, `404` |

Response `200` (paginated):

```json
{
  "items": [
    {
      "id": 301,
      "body": "Reproduced on Safari 18.",
      "author": { "id": 2, "name": "Ravi Kumar" },
      "is_edited": false,
      "created_at": "2026-11-16T10:10:00Z",
      "updated_at": "2026-11-16T10:10:00Z"
    }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 1, "total_pages": 1 }
}
```

`is_edited` is calculated as `updated_at > created_at` (PDF #5); it isn't a column.

### CMT-2 `POST /projects/{public_id}/tasks/{number}/comments`

| | |
|---|---|
| Purpose | Add a comment as the current user (`author_id` comes from the token, never from the body) |
| Permission | `comments.create` |
| Tables | task_comments |
| Errors | Common, `403`, `404`, `409 project_archived` |

Request:

```json
{ "body": "Reproduced on Safari 18." }
```

`body` must be 1–10,000 characters after trimming.

Response `201`: one item as in CMT-1.

### CMT-3 `PATCH /projects/{public_id}/tasks/{number}/comments/{comment_id}`

| | |
|---|---|
| Purpose | Edit a comment. **Only its author** can edit, and the author's role needs `comments.edit` (A-6). |
| Permission | Author + `comments.edit` |
| Tables | task_comments |
| Errors | Common, `403` (not the author or no `comments.edit`), `404`, `409 stale_update`, `409 project_archived` |

Request:

```json
{ "body": "Reproduced on Safari 18 and iOS.", "expected_updated_at": "2026-11-16T10:10:00Z" }
```

Response `200`: one item as in CMT-1.

### CMT-4 `DELETE /projects/{public_id}/tasks/{number}/comments/{comment_id}`

| | |
|---|---|
| Purpose | Delete a comment: the author can delete their own (needs `comments.edit`); anyone with `comments.delete` can delete any comment (moderation) (A-6) |
| Permission | (Author + `comments.edit`) or `comments.delete` |
| Tables | task_comments |
| Errors | Common, `403`, `404`, `409 project_archived` |

Response: `204 No Content`.

---

## 13. Behaviour proposals awaiting mentor approval

These are product rules the ER diagram and PDF don't decide. Each is the recommended choice; the services (B26–B50) implement them unless changed.

| ID | Proposal | Why |
|---|---|---|
| A-1 | New projects get three default statuses: To Do, In Progress, Done | `tasks.status_id` is required, so a project without statuses couldn't hold a single task |
| A-2 | Registration creates the organization, an `Admin` role with all permissions, and assigns it org-wide to the first user | Someone has to be able to manage the new organization |
| A-3 | Emails (reset and set-password links) go through an `EmailSender` interface. In development it writes the link to the server log; a real provider (SMTP or a service) is configured later. | No email service is specified yet. **Question: which email provider will the company use?** |
| A-4 | Add `users` to the feature list (P-5), for user administration | User management isn't the same as org settings or project members |
| A-5 | Effective project permission = org-wide role **OR** project role | An org admin should never lose access because of an extra project role |
| A-6 | Comments: only the author edits (needs `comments.edit`); the author or a `comments.delete` holder deletes | Nobody can put words in someone else's mouth; moderators can still remove content |
| A-7 | Removing a project member unassigns their tasks in that project and removes them from that project's teams | Otherwise tasks would stay assigned to someone with no access |
| A-8 | `organizations.slug` and `projects.task_prefix` can't be changed after creation | Changing them would break saved URLs and every existing task key |
| A-9 | Archived projects are read-only until unarchived | Archive means "finished, keep for reference" |
| A-10 | Token lifetimes: access 15 minutes, refresh 14 days, reset or set-password link 30 minutes | Short-lived access tokens limit the damage if one leaks; refresh keeps users logged in for two weeks |
| A-11 | Refresh rotation with reuse detection and a 30-second grace window (section 2.2) | Detects stolen refresh tokens without logging out users who have two tabs open |
| A-12 | Passwords: 8–128 characters, no composition rules | Length matters more than symbols; the 128 cap stops someone sending a huge string to slow down hashing |
| A-13 | Self-lockout protection: you can't deactivate yourself, change or remove your own org-wide role, or remove `roles.edit` from the role you hold | Prevents an organization from losing its last admin by accident |
| A-14 | Add `spaces` to the feature list (P-5), so managing spaces can be granted separately from editing the project. Moving a task between spaces needs only `tasks.edit`. **Approved** as database.md S-6. | Team leads can organise spaces without being able to rename or archive the project |
| A-15 | New projects get one default space, "General" (PRJ-1). Spaces are never deleted, so it always exists, and tasks created without `space_id` go into it. **Approved** as database.md S-2 and S-4. | `tasks.space_id` is required (S-2), so a project without spaces couldn't hold a single task, and small projects never have to think about spaces |

**Not included; needs a decision:** a login rate limit (brute-force protection). A proper one needs shared storage such as Redis across server instances. Options: (a) add Redis; (b) a simple per-process limit in the POC; (c) rely on the reverse proxy (Nginx) rate limiting.

---

## 14. How the frontend consumes this API

### 14.1 Request path

```
Browser ──fetch /api/v1/...──▶ Next.js route handler (BFF) ──Bearer token──▶ FastAPI /api/v1/...
          (cookies only)        app/api/v1/[...path]/route.ts
```

- **The browser never sees a token.** Login (`app/api/auth/[action]/route.ts`) calls AUTH-2 and stores both tokens in `httpOnly`, `Secure`, `SameSite=Lax` cookies. Page scripts can't read httpOnly cookies, so an XSS bug can't steal the session.
- **The BFF proxy** forwards each `/api/v1/*` request to FastAPI with `Authorization: Bearer <access cookie>`. On `401 not_authenticated` it calls AUTH-3 once, updates the cookies, and retries the original request once. On `409 token_already_rotated` it retries with the newer cookie.
- **Server components** (first page load) call FastAPI directly through `lib/api-client.ts`, reading the access cookie on the server. The page arrives already filled with data, with no loading spinner on first paint.
- **Mutations** use `POST`/`PATCH`/`PUT`/`DELETE` through the proxy. `SameSite=Lax` cookies plus JSON-only bodies protect against cross-site request forgery (CSRF).

### 14.2 Data fetching and caching (TanStack Query)

| Concern | Approach |
|---|---|
| Cache keys | `["projects", params]`, `["project", publicId]`, `["tasks", publicId, params]`, `["task", publicId, number]`, `["comments", publicId, number, page]`, `["statuses", publicId]`, `["spaces", publicId, params]`, `["space", publicId, spaceId]`, `["teams", publicId, params]`, `["members", publicId, params]`, `["roles"]`, `["me"]` |
| After a mutation | Invalidate exactly the affected keys. For example, creating a task invalidates `["tasks", publicId]` and `["project", publicId]` (task counts); changing a status invalidates `["tasks", publicId]`, `["task", publicId, number]` and `["statuses", publicId]`; moving a task to another space (TSK-9) invalidates `["tasks", publicId]`, `["task", publicId, number]` and `["spaces", publicId]` (task counts). |
| Board drag-and-drop | Optimistic update: move the card immediately, roll it back if TSK-5 fails |
| Duplicate submissions | Buttons are disabled while a mutation is pending. The backend still enforces every rule on its own. |
| `409 stale_update` | Show "Someone else changed this task", reload it, and keep the user's unsaved input visible so it isn't lost |

### 14.3 Search with pagination

| Step | Behaviour |
|---|---|
| Typing | Debounced by 300 ms. Nothing is sent until the trimmed text has at least 3 characters; clearing the box restores the unfiltered list. |
| Outdated requests | Each new search aborts the previous request (`AbortController`, built into TanStack Query), so a slow old response can't overwrite a newer one |
| Page reset | A new search term or filter resets to `page=1` |
| URL state | `q`, filters, `sort`, `order` and `page` live in the URL (`?q=login&page=2`), so refresh, back button and shared links keep the same view |
| Display | "Showing 21–40 of 137 results for "login"", built from `pagination` |

### 14.4 Permissions in the UI

- **Navigation and org-level actions:** `org_permissions` from USR-1, fetched once and cached.
- **Inside a project:** `my_permissions` from PRJ-3.
- **Hiding actions:** buttons the user can't use are hidden, e.g. no Delete button without `tasks.delete`.
- **Errors:** `403` shows "You don't have permission"; `404` shows a not-found page.
- Hiding buttons is only for user experience. **The backend checks every permission on every request regardless.**

### 14.5 Loading, empty and error states

Every list and detail screen handles four states: loading (skeleton matching the final layout), empty ("No tasks yet — create the first one", or "No results for "xyz"" when searching), error (message plus Retry, with `request_id` shown in small text for support), and data.