# Privacy and Data Flow

CCF Paper Scout keeps interest ranking local, but it is not an offline-only application.

| Component | Data sent or stored |
|---|---|
| Zotero Web API | Numeric user ID, read-only API key, and requests for the configured personal-library items |
| DBLP | Configured venue names/keys, years and pagination/search parameters; no Zotero interest corpus |
| OpenAlex | DOI of selected enrichment candidates; no Zotero interest corpus |
| Optional LLM provider | Final candidate title and abstract plus configured explicit interest phrases; never Zotero API keys or the full Zotero library |
| Optional SMTP provider | Rendered recommendation digest and sender/receiver metadata |
| Local disk | Config, seen history, translation/analysis cache, report, logs, and optional Zotero debug listing |

## Secrets

Secrets are read from environment variables and must not be placed in `config.json`:

- `ZOTERO_API_KEY`
- `LLM_API_KEY`
- `SMTP_PASSWORD`

`.env.local`, `config.json`, `state/`, reports, logs and Zotero debug exports are excluded from Git. This is a safeguard, not a substitute for checking staged files before pushing.

## Local sensitive files

`zotero_library_debug.md` may contain private titles, abstracts, item keys and dates. Keep debug listing disabled for normal scheduled runs, restrict local permissions, and delete it when no longer needed.

To delete local state:

```bash
rm -f zotero_library_debug.md recommendations.md paper_scout.log
rm -rf state/
```

Deleting `state/seen.json` resets delivery deduplication and may cause old papers to be recommended again.
