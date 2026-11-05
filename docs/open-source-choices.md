# Open-source choices

The rule for this project: if a tool in the job description is paid, find a free or open-source way to show the same skill, say
what was substituted, and avoid features locked behind a paywall. Licences and plans change, so check before relying on a row.

| Role | Paid or proprietary option | Used here | Licence |
|---|---|---|---|
| Workflow automation | Workato (paid, no free tier) | **n8n Community Edition**, self-hosted | Sustainable Use License (free for internal use; source-available, not OSI) |
| Warehouse | Snowflake, BigQuery, Redshift | **DuckDB** | MIT |
| Alert channel | Slack, PagerDuty, Opsgenie (paid tiers) | **Mailpit** as a local SMTP sink | MIT |
| Container runtime | Docker Desktop (free for personal use and small business, paid for larger firms) | works with it; Docker Engine, Podman or Rancher Desktop also run the compose file | Docker Engine: Apache-2.0 |
| Monitoring | Datadog, New Relic | SQL rules in the queue API and the monitor workflow | n/a |
| BI and charts | Tableau, Looker | SQL and Markdown tables; Metabase or Superset if dashboards are wanted | Metabase: AGPL-3.0; Superset: Apache-2.0 |
| API testing | Postman | `curl` and FastAPI's built-in `/docs` | MIT |
| Diagrams | Lucidchart | Mermaid in the README | MIT |
| Browser screenshots | paid capture tools | Playwright with the installed Edge | Apache-2.0 |
| Data | paid market-data feeds | Bank of Canada Valet API (free, attribution) | the Bank's terms |

## n8n features that are paid, and what replaces them

| Paid feature (check the current plan) | Free replacement used here |
|---|---|
| Variables (`$vars`) | environment variables (`$env`), switched on with `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` |
| Environments: Git source control for workflows | workflow JSON in git, generated and deployed by `lab bootstrap` |
| External secrets, SSO, log streaming, advanced permissions | not needed for a single-user lab; secrets live in `.env` (git-ignored) |
| Conveniences unlocked by registering an email for a free licence key | skipped on purpose: no account or email is registered |

If a strictly OSI-approved engine is required, the same design ports to **Activepieces** (MIT), **Windmill** (AGPL-3.0),
**Node-RED** (Apache-2.0) or **Kestra** (Apache-2.0). The queue API, the validator tests, the SQL and the runbook do not depend on n8n.

## Workato concepts in this lab

Workato cannot be tried without a paid plan, so the pattern is shown in n8n. The mapping uses Workato's public terminology as the
author understands it; it is a study aid, not a claim of Workato experience.

| Workato | In this lab |
|---|---|
| Recipe | n8n workflow (`n8n/workflows/`) |
| Trigger (scheduler, webhook) | Schedule Trigger, Webhook |
| Action and connector | HTTP Request, Send Email, Code |
| Callable recipe | Execute Workflow node (not used: the alert chain is generated into each workflow) |
| Job history and repeating failed jobs | Executions tab, plus the DuckDB failure queue and `lab requeue` |
| Lookup table | DuckDB tables behind the queue API |
| Error monitoring and notifications | Retry on Fail, the failure queue, the error workflow, the monitor |
| Deployment between environments | workflow JSON in git, `lab bootstrap` |
