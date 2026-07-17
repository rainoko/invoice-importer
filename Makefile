# Invoice importer tasks

.PHONY: extract import run

extract:  ## Read OneDrive PDFs with the Claude Code CLI -> *.parsed.json
	LLM_PROVIDER=claude_cli CLAUDE_MODEL=claude-haiku-4-5 .venv/bin/python onedrive_reader.py

import:  ## Import parsed invoices into SimplBooks
	.venv/bin/python simplbooks_importer.py

run: extract import  ## Extract then import
