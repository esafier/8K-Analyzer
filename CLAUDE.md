# 8K Analyzer — Project Instructions

## Database Compatibility
This project runs on **SQLite locally** and **PostgreSQL on Render**. The two behave differently:

- **sqlite3.Row** supports `row["key"]` bracket access but does NOT support `.get()`.
- **PostgreSQL rows** (via `_dict_rows` in database.py) are converted to real Python dicts that DO support `.get()`.

**Rule:** When writing new database query functions, always convert results to real dicts if downstream code will use `.get()`. Either:
1. Use `columns = [desc[0] for desc in cursor.description]` + `dict(zip(columns, row))` explicitly, or
2. Test with both access patterns before committing.

Never assume `_dict_rows()` returns objects with `.get()` — it doesn't on SQLite.

## Architecture Notes
- **Prompts live in** `prompts/` as `.txt` files with `{filing_text}` and `{context_block}` placeholders
- **Signal analysis** uses Chat Completions (not Responses API). Context is pre-gathered in app.py before calling the LLM.
- **Web search** is a separate optional pre-step using the Responses API with `web_search` tool (only model that needs it)
- **Departure clustering** is detected by querying the local database for same-CIK 5.02 filings, not by web search
- **Deploy:** Push to `main` triggers auto-deploy on Render
