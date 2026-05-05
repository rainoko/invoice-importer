
Grant app permission to a OneDrive item (folder/file)

Important:
- Do not paste long bearer tokens manually (can cause invalid signature due to truncation/whitespace/expiry).
- Do not commit client secrets or access tokens to source control.
- If a secret was exposed, rotate it in Entra ID.

1) Set variables

```bash
export TENANT_ID="<tenant-id>"
export CLIENT_ID="<app-client-id>"
export CLIENT_SECRET="<app-client-secret>"
export DRIVE_ID="<drive-id>"
export ITEM_ID="<item-id>"   # folder or file inside the drive
```

2) Get fresh app token

```bash
TOKEN=$(curl -sS -X POST "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "client_id=$CLIENT_ID" \
  --data-urlencode "client_secret=$CLIENT_SECRET" \
  --data-urlencode "grant_type=client_credentials" \
  --data-urlencode "scope=https://graph.microsoft.com/.default" \
  | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
```

3) Grant read or write to the application

Read:

```bash
curl -X POST "https://graph.microsoft.com/beta/drives/$DRIVE_ID/items/$ITEM_ID/permissions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "roles": ["read"],
    "grantedTo": {
      "application": {
        "id": "<app-client-id>",
        "displayName": "<app-name>"
      }
    }
  }'
```

Write:

```bash
curl -X POST "https://graph.microsoft.com/beta/drives/$DRIVE_ID/items/$ITEM_ID/permissions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "roles": ["write"],
    "grantedTo": {
      "application": {
        "id": "<app-client-id>",
        "displayName": "<app-name>"
      }
    }
  }'
```

Notes:
- The endpoint must be item-level: `/drives/{drive-id}/items/{item-id}/permissions`.
- `grantedTo` is required for this call (not `grantedToIdentities` in this scenario).
- Ensure the app has required Graph application permissions and admin consent.

## LLM provider configuration

The invoice parser supports these providers via `LLM_PROVIDER`:

- `ollama`
- `cerebras`
- `openrouter`
- `github_models` (also accepts `github-models` and `github`)

### GitHub Models

Set these values in `.env`:

```bash
LLM_PROVIDER=github_models
GITHUB_MODELS_API_KEY=<github-token-with-models-access>
GITHUB_MODELS_MODEL=openai/gpt-4.1-mini
# Optional override (default shown)
GITHUB_MODELS_URL=https://models.github.ai/inference/chat/completions
```
