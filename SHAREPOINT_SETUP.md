# Connect the verifier to SharePoint (one-time, ~10 minutes)

The verifier looks up each report's Power Automate daily export on SharePoint:

```
<client site> / Shared Documents / Daily Data Exports / <job> / <YYYY-MM-DD> / <job> <YYYY-MM-DD>.csv
```

It needs a **read-only** Microsoft 365 app registration. Only a PPG Microsoft 365 admin can create it.

## 1. Create the app registration

1. Open **https://entra.microsoft.com** → **Applications** → **App registrations** → **New registration**.
2. Name: `PPG Verifier – SharePoint read` · Supported account types: **this organizational directory only** · no redirect URI → **Register**.
3. On the overview page copy the **Application (client) ID** and **Directory (tenant) ID**.
4. **Certificates & secrets** → **New client secret** → 12 or 24 months → **Add**. Copy the secret **Value** right away (it is shown once). Put a reminder on the calendar before it expires.

## 2. Give it read access — pick one

**Option A — only the client sites (recommended, least access)**

1. **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions** → `Sites.Selected` → **Add** → **Grant admin consent for PPG**.
2. Grant read on each client site. In **https://developer.microsoft.com/graph/graph-explorer** (signed in as an admin; consent to `Sites.FullControl.All` if asked):
   - `GET https://graph.microsoft.com/v1.0/sites/ppgquality.sharepoint.com:/sites/BriggsStratton_Auburn` → copy `id`.
   - `POST https://graph.microsoft.com/v1.0/sites/<that id>/permissions` with body:
     ```json
     {"roles": ["read"], "grantedToIdentities": [{"application": {"id": "<Application (client) ID>", "displayName": "PPG Verifier – SharePoint read"}}]}
     ```
   - Repeat for every site in `SHAREPOINT_SITES` (below), and for any new client site later.

**Option B — all PPG sites (simpler, broader)**

**API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions** → `Sites.Read.All` → **Add** → **Grant admin consent for PPG**. New client sites work without extra steps.

## 3. Add the three values to Vercel

Vercel → project **ppg-ci-verifier** → **Settings** → **Environment Variables**, environment **Preview** only:

| Name | Value | Type |
|---|---|---|
| `MS_TENANT_ID` | Directory (tenant) ID | Plain |
| `MS_CLIENT_ID` | Application (client) ID | Plain |
| `MS_CLIENT_SECRET` | the secret **Value** | **Sensitive** |

Never paste the secret into chat, email, Teams or the repo.

Already set: `SHAREPOINT_SITES` = `BriggsStratton_Auburn,BriggsStratton_Statesboro,Grammer_Shannon,HyundaiMobisAlabama_Montgomery,Hyundai_Montgomery,Lear_T1,PrattWhitney_Atlanta,SLAlabama_AlexanderCity`. Add new client sites there. With Option B you may leave it as is (it also makes lookups faster).

## 4. Redeploy and check

Redeploy the `preview` branch (environment variables apply to new deployments). Then `/api/health` shows `"sharepoint_configured": true`, and every uploaded report is checked against SharePoint automatically. **Check against SharePoint** in a report's detail re-runs it.

## What it can and cannot do

- Read-only: it never writes, moves or deletes anything in SharePoint.
- It reads only the daily export file for the report's job and date.
- The export and the comparison are stored with the report in the verifier's database (Supabase project `project-hub`, schema `ci_verifier`).
- To revoke: delete the client secret or the app registration in Entra; the verifier immediately falls back to "not set up".
