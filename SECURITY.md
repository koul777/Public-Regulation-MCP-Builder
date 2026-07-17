# Security Policy

## Supported Scope

This project is designed for local or internal-network preprocessing and MCP access to approved regulation data. Public deployments, external MCP exposure, and cloud AI integrations require separate deployment review.

## Reporting a Vulnerability

Do not open public issues with exploit details, secrets, private documents, or institution data. Report suspected vulnerabilities privately to the project maintainer or repository owner.

Include:

- affected commit or version
- reproduction steps using synthetic data
- expected and actual behavior
- impact on approval gates, tenant isolation, MCP tool visibility, audit logging, or local path/secret exposure

## Security Expectations

- Do not commit uploaded source documents, private runtime data, vector stores, logs, or generated reports unless explicitly sanitized for public release.
- MCP tools must expose approved regulation chunks only.
- Local file paths, credentials, API tokens, and unapproved preprocessing outputs must not appear in API, MCP, log, report, or test outputs.
- Shared deployments should use authentication, tenant storage isolation, network access controls, and audited MCP server startup parameters.
- Do not run generated MCP setup scripts with placeholder values such as `<strong-token>`, `<runtime-api-key>`, or `<tunnel_id>`.
- ChatGPT or Claude remote MCP connections can transmit returned tool data to an external AI service. Use only public or separately approved data for those paths.
- Prefer local `stdio` or approved internal-network MCP for nonpublic regulation data. If ChatGPT must reach an internal MCP server, use an approved tunnel or gateway rather than unauthenticated public HTTP.

## Public Samples

Use synthetic samples or publicly redistributable documents only. If a sample document comes from a public source, record the source and confirm redistribution is allowed before committing it.
