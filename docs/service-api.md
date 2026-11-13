# Ledger service interface

Disabled by default. Nothing is connected to OpenBot until the supervised test install.

Private endpoints:
- `POST /api/v1/call/`: JSON `{ "operation": "list_customers", "arguments": { "q": "Sample", "limit": 20 } }`.
- `POST /mcp`: stateless Streamable HTTP with JSON responses, compatibility protocol versions
  2025-11-25, 2025-06-18 and 2025-03-26. A modern client gets an unsupported-version response
  listing the supported compatibility versions and can reconnect with `initialize`.

Every request requires `Authorization: Bearer <token>`. Cookies never authenticate these endpoints.
No bearer token belongs in a URL. Public Caddy routing must reject `/api*`, `/mcp*` and `/admin*`.
Only the explicitly configured OpenBot source IP/network is allowed; `X-Forwarded-For` is ignored.
Origins are denied unless present in the explicit allowlist. OpenBot service calls normally send no Origin.
Django `ALLOWED_HOSTS` must include the selected private service DNS name (for example `cc-billing`).

At install, create a token using `python manage.py create_ledger_token --output /private/path/ledger-token`.
The file must not exist; it is created mode 600. The command never prints the raw token. Transfer it
through the Vault/secure server configuration into the MCP Authorization header, then remove the
staging file. Only a SHA-256 hash and identifying prefix stay in the database. Never commit the file.
Use `--read-only` for a token that cannot create or edit drafts. Revoke tokens through the owner service.

Enable `CCB_PRIVATE_API_ENABLED=1` and set `CCB_SERVICE_NETWORKS` to the actual OpenBot source IP/CIDR
only after verifying the Docker network. The loopback-only default is for local development.
Do not add broad public networks or the Caddy source address. No live token was generated in this build.

`tools/list` is the authoritative schema list. Operations include customer search/contact edits,
products and tax locations, proposals (never confirmation), documents and overdue/due-soon lists,
draft creation/editing/email text, previews, accepted-quote conversion, late-fee drafts, history,
payments, receipt status, upcoming renewals and stored PDF downloads. Lists are bounded to 100 rows
and return `next_offset`. PDF downloads are base64 in JSON or embedded PDF resources in MCP.
All amounts are integer cents; rates and tax/numbering decisions remain in the service layer.

Ledger cannot approve, send, mark sent, void, record payments, resend receipts, confirm tax,
change settings or delete drafts. Both forbidden operations and protected-field attempts are logged.
Imported history is read-only. Mutating MCP calls are not declared idempotent; retries may create
another unnumbered draft, so a client should read the result before repeating an uncertain draft call.

Protocol reference: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
