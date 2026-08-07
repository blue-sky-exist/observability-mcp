# Security policy

Do not open a public issue containing credentials, internal URLs, log contents, trace IDs,
or PromQL results from a private environment.

Report vulnerabilities privately to the repository maintainer. Include the affected version,
impact, reproduction steps, and a suggested remediation when possible.

Deployments should place this MCP service behind HTTPS and authentication. Restrict
`ES_ALLOWED_INDEX_PATTERNS`, use read-only backend credentials, keep TLS verification enabled,
and never commit `.env` files.
