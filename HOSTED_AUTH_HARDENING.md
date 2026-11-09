# Hosted authentication hardening

Hosted browser APIs require a valid application session even when the persisted
`access_required` setting is missing or false. The preview flag and Host allowlist
are deployment/routing checks, not evidence that a visitor signed in. No Vercel
header, automation bypass header, or platform sign-in substitutes for an app session.
The stored setting is not migrated or changed by this fix.

Only explicit loopback development (`CI_LOCAL_DEV=1`, no serverless environment)
retains the virtual owner fallback. API-key and MCP connectors retain their own
key authentication; health, sign-in endpoints, and static assets retain their
intended public behavior behind the host gate. Hosted admins cannot turn the
application session requirement off. The app reports that effective requirement.

## Deployment prerequisite and recovery

Do not deploy this change until an authorized operator verifies an existing,
non-disabled application admin can sign in using the intended Microsoft flow or
a valid one-time email link. Verify this against the intended database, not a
synthetic test identity. Confirm the relevant sign-in service is configured and
reachable and that required platform protection does not obstruct that flow.
Retain deployment protection as defense in depth. No live verification or account
provisioning is part of this local patch.

If no usable admin or sign-in route exists, the application deliberately remains
closed. Arrange separately authorized admin provisioning/recovery through the
existing controlled administrative process before deployment. This patch adds no
anonymous setup endpoint, new credential, automatic grant, or bootstrap bypass.
Do not disable authentication or revert to anonymous owner access as recovery.

## Verification

`tests/test_hosted_auth_boundary.py` uses the actual HTTP handler with a synthetic
in-memory fake Postgres store. It checks missing/false/true stored settings,
valid/invalid/revoked sessions, spoofed headers, unapproved hosts, anonymous admin
operations, public exceptions, and independent API/MCP key authentication.
Existing `tests/test_access.py` covers the explicit local development fallback,
member scoping, sign-in and session flows. These tests are not evidence that live
accounts or live deployment protection are configured correctly.
