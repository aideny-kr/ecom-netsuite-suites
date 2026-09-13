# Run Suite Studio for your company — local evaluation

This first installation slice supports one customer organization, its users,
connections, custom workspaces, and the existing Scheduled Jobs platform. It keeps
tenant IDs, permissions, approval checks and job budgets. It introduces no trial
expiry or commercial plan quotas for the bootstrapped company.

This is a **local evaluation profile**, not a production hosting release. It binds
only to localhost, uses a separate database/Redis/workspace, and does not load your
existing `.env`. The Postgres superuser matches the existing local-development
profile: RLS policies are retained but are not enforced against that superuser.
Production release still needs the runtime database-role/access review, TLS,
tested backups/restoration, monitoring and the required independent release gate.
Do not expose these ports publicly or put production customer data in this profile.

Requirements: Docker with Compose v2, Python 3, Git, and an interactive terminal.

```sh
scripts/install/local.sh init
scripts/install/local.sh bootstrap
scripts/install/local.sh up
```

`init` exclusively creates `.env.single-company` with mode 600 and generated local
secrets; rerunning does not rotate or print them. Keep the file private and backed
up with the database. `bootstrap` builds the backend, starts Postgres/Redis, runs
all migrations once, then prompts for company and administrator details. Enter
the password directly in your terminal, not in a coding-agent conversation.
The password is never accepted as a command-line argument. No AI-provider key is
required to install or sign in.

Bootstrap refuses an existing hosted/multiple-company database. Rerunning with
the same company slug and administrator is a no-op; it does not reset passwords,
change company/user names, feature flags or reactivate a company. Matching uses
the company slug and active administrator email; use Settings for later edits.
The built-in system metric-catalog
tenant is preserved. No `soul.md` file is created or overwritten.

Open [Suite Studio](http://localhost:13002/login). The API is at
[local API](http://localhost:18000/api/v1/health). To choose different ports, set
`FRONTEND_PORT` and `BACKEND_PORT` in `.env.single-company` before building.
Restart with the same profile; frontend public values are build-time settings.

After signing in:

1. Configure your AI provider/key in Settings using the existing encrypted-key
   flow. AI usage is billed by that provider; removing plan quotas does not remove
   job or provider budgets.
2. Connect the services needed by your use case. NetSuite REST and MCP use the
   customer's own integration records and their respective existing OAuth flow.
   Google Drive/BigQuery also need their own connection configuration; Google
   Sign-In for app login is optional and is disabled in this local profile.
3. Use the existing Scheduled Jobs page to describe the work and cadence. Review
   the compiled plan, approve it, run once, inspect the result, and then let the
   schedule run. No customer workflow is created or approved by this installer.
4. Invite teammates using the existing Team controls. Public organization signup
   and platform-superadmin operations are disabled. The console email provider
   is for local evaluation; real invitation delivery needs an email provider in
   the eventual hosted installation.

The current scheduled step registry supports BigQuery SQL, report composition,
PDF, Excel, Google Drive delivery, and reconciliation runs. For example, use a
supported report playbook for a weekly report delivered to Drive after configuring
the required connections. Unsupported instructions should request clarification;
this release does not promise arbitrary unattended agents or ERP writes.

Developers can extend `backend/app/services/jobs/registry.py` with a typed
`StepSpec`, parameter schema and executor. This is the existing extension seam,
not a new generic agent runtime. Add meaningful validation/execution tests and
preserve permissions, spend limits, idempotency and explicit approval of delivery
or other external actions. New financial write steps need their own safety design
and required review before being made schedulable. Keep customer-specific
configuration and credentials outside source control.

Vendor billing, web crawling, learning and benchmark schedules are disabled in
single-company mode. The customer Scheduled Jobs sweep, connection maintenance,
and existing opt-in product schedules remain. Reconciliation autonomy flags are
off; enabling a feature does not approve a financial action.

```sh
scripts/install/local.sh status
scripts/install/local.sh down
```

`down` preserves the database and workspace volumes. Do not use `down -v` unless
you deliberately intend to delete this evaluation installation's data. After code
changes, stop this local profile, rerun `bootstrap` to rebuild and migrate, then
`up`; the matching bootstrap leaves the existing administrator unchanged. This
is not a production migration or rollback procedure.

If startup reports that the company is missing, complete `bootstrap`. If it reports
another company or an unconverted hosted database, stop and verify the target;
never delete tenants to make the check pass. If a workflow cannot compile, check
the AI key, connected data sources and supported step types. Refer to job run
history for budget/error/approval outcomes.

The implementation does not change the repository's license, publish code,
migrate Framework, or deploy customer hosting. Those remain separate work.
