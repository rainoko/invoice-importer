
# Invoice Importer

This project runs a 2-step flow:

1. `onedrive_reader.py` reads PDF invoices from OneDrive month folders, creates `.md` text, and extracts structured invoice JSON (`.parsed.json`) using an LLM.
2. `simplbooks_importer.py` reads those `.parsed.json` files and creates purchase invoices in SimplBooks.

## Prerequisites

- Python 3.10+
- Tesseract OCR installed and available on `PATH`
- Playwright Chromium browser installed
- Microsoft Graph app access to the target OneDrive/Drive
- SimplBooks credentials

## Setup

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Install Playwright browser:

```bash
python -m playwright install chromium
```

4. Install OCR runtime (Ubuntu/Debian):

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr
```

5. Create `.env` from template and fill real values:

```bash
cp .env.example .env
```

## Environment Variables

### OneDrive / Graph

Required:

- `TENANT_ID`
- `CLIENT_ID`
- `FOLDER_BASE_PATH` (base path before `YYYYMM` folders)

Required in app auth mode (`--auth-mode app`, default):

- `CLIENT_SECRET`
- `DRIVE_ID`

Notes:

- `OBJECT_ID` is not used by current code.
- Target folders are previous and current month in `YYYYMM` format under `FOLDER_BASE_PATH`.

### OneDrive item permission (app mode)

Use this when your app token works but access to a specific folder/file is still denied.

1. Set variables:

```bash
export TENANT_ID="<tenant-id>"
export CLIENT_ID="<app-client-id>"
export CLIENT_SECRET="<app-client-secret>"
export DRIVE_ID="<drive-id>"
export ITEM_ID="<item-id>"   # folder or file inside the drive
```

2. Get a fresh app token:

```bash
TOKEN=$(curl -sS -X POST "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token" \
	-H "Content-Type: application/x-www-form-urlencoded" \
	--data-urlencode "client_id=$CLIENT_ID" \
	--data-urlencode "client_secret=$CLIENT_SECRET" \
	--data-urlencode "grant_type=client_credentials" \
	--data-urlencode "scope=https://graph.microsoft.com/.default" \
	| sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
```

3. Grant read or write to the application.

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

### LLM provider (`onedrive_reader.py`)

Set `LLM_PROVIDER` to one of:

- `ollama`
- `cerebras`
- `openrouter`
- `github_models` (also accepts `github-models` and `github`)

Provider-specific requirements:

- `ollama`: `OLLAMA_URL`, `OLLAMA_MODEL`
- `cerebras`: `CEREBRAS_API_KEY`, `CEREBRAS_MODEL`
- `openrouter`: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`
- `github_models`: `GITHUB_MODELS_API_KEY`, `GITHUB_MODELS_MODEL` (optional `GITHUB_MODELS_URL`)

### SimplBooks (`simplbooks_importer.py`)

Required:

- `SIMPLBOOKS_USER`
- `SIMPLBOOKS_PASSWORD`
- `SIMPLBOOKS_ACCOUNT_CODE_CAR_EXPENSE`
- `SIMPLBOOKS_ACCOUNT_CODE_EU_BUY`
- `SIMPLBOOKS_ACCOUNT_CODE_DEFAULT`
- `SIMPLBOOKS_VAT_PROFILE_CAR_EXPENSE`
- `SIMPLBOOKS_VAT_PROFILE_EU_BUY`
- `SIMPLBOOKS_VAT_PROFILE_DEFAULT`

Optional:

- `SIMPLBOOKS_BASE_URL` (defaults to `https://www.simplbooks.ee`)

Important:

- Account code and VAT profile values are now required from `.env`.
- No fallback defaults are used for those mappings. Missing values raise an exception.

## Run

### Step 1: Parse PDFs from OneDrive

```bash
python onedrive_reader.py
```

Useful options:

```bash
python onedrive_reader.py --auth-mode app
python onedrive_reader.py --auth-mode delegated
python onedrive_reader.py --base-path "root/04 arved/02 välja"
python onedrive_reader.py --reference-month 202605
```

### Step 2: Import parsed invoices to SimplBooks

Default mode reads/writes via OneDrive:

```bash
python simplbooks_importer.py
```

Useful options:

```bash
python simplbooks_importer.py --headless
python simplbooks_importer.py --test-file 260416.eurostauto.parsed.json
python simplbooks_importer.py --no-submit
python simplbooks_importer.py --reference-month 202605
```

Local mode (read/write local files instead of OneDrive):

```bash
python simplbooks_importer.py --source local --input-dir .
```

## Outputs

`onedrive_reader.py` writes into each month folder `process/`:

- `<invoice>.md`
- `<invoice>.tables.json` (when table extraction is available)
- `<invoice>.parsed.json`

`simplbooks_importer.py` writes status markers:

- `<invoice>.parsed.success.json`
- `<invoice>.parsed.failed.json`

Existing success/failed markers are respected and skipped.

## Quick Check

Run the focused test for the purchase settings env requirements:

```bash
.venv/bin/pytest -q tests/test_simplbooks_helpers.py::TestResolvePurchaseSettings
```
