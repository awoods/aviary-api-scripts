# Aviary Directory Import

A command-line tool for bulk-importing structured deposit directories into the
[Aviary platform](https://www.aviaryplatform.com) via its
[public REST API](https://www.aviaryplatform.com/api/v1/documentation).

## What it's for

Harvard Library MPS (Media Preservation Services) deposits arrive as directory
trees, each resource directory described by a `project.prop` metadata file and
holding its media under a `deliverable/` subdirectory. This script walks such a
tree and, for each resource directory, creates the corresponding Aviary
**collection**, **resource**, **media files**, and **indexes** — turning a batch
of deposits into published Aviary records in one run.

It is intended as operational tooling for librarians/archivists running MPS
uploads, not as a reusable library.

## What it does

Given a top-level directory (a single deposit, or a parent containing several):

1. **Finds resource directories** — every directory that directly contains a
   `project.prop` file.
2. **Resolves the collection** — reuses the collection titled
   `MPS Upload <today's date>` if it already exists, otherwise creates it.
   The Aviary organization and API base URL are both derived from the
   `aviaryOrg` field in `project.prop`; no base URL needs to be supplied.
3. **Creates one resource per directory:**
   - By default the resource is built from the `project.prop` metadata mapping:
     `title` (→ `shelfnum` → directory name), `metsLabel` as description, the
     `access` code, plus a rich set of additional fields mapped to Aviary
     metadata (Agent roles such as composer/performer/creator, Date, Format,
     Subject, and Identifiers like `alephID`/`findingAid`/`shelfnum`).
   - With the `--importMarc` flag *and* a usable `alephID`, the resource is
     instead built from the Harvard HOLLIS MARC XML record (fetched from the
     HOLLIS webservice and submitted to Aviary's MARC XML import API, which runs
     asynchronously and is polled to completion). The `project.prop` access
     setting is then forced onto it. On any MARC failure it falls back to the
     metadata mapping above.
4. **Uploads media** — every `.mp3` / `.mp4` / `.mov` under `deliverable/`,
   sorted alphanumerically, via Aviary's presigned-upload flow.
5. **Creates indexes** — every `*playlist.xml` in `deliverable/playlists/`,
   each linked to the media file whose filename (without extension) matches the
   playlist's `<dc:identifier>` value. Indexes with no matching media file are
   skipped with a warning.

Media uploads and index creation are retried with exponential backoff on
transient errors, and each resource directory's outcome is appended as a row to
a CSV log (default `mps_aviary_import_log.csv`).

The HTTP request patterns mirror AVP's own published bulk-import scripts
(<https://github.com/WeAreAVP/aviary-api-scripts>).

## Requirements

- Python 3
- The [`requests`](https://pypi.org/project/requests/) library:
  ```bash
  pip install requests
  ```
- An Aviary API key/token.

## Usage

```bash
export AVIARY_TOKEN="your_api_key"

# Preview the planned API calls without contacting Aviary (recommended first):
python3 aviary_directory_import.py /path/to/top_level_directory --dry-run

# Run the import for real:
python3 aviary_directory_import.py /path/to/top_level_directory

# Build resources from Harvard HOLLIS MARC XML where a usable alephID exists:
python3 aviary_directory_import.py /path/to/top_level_directory --importMarc
```

Run `python3 aviary_directory_import.py --help` for all options.

### Common options

| Option | Purpose |
| --- | --- |
| `--dry-run` | Scan and print the planned API calls without contacting Aviary. |
| `--importMarc` | Build resources from Harvard HOLLIS MARC XML when a usable `alephID` is present (default: always use the `project.prop` metadata mapping). |
| `--token` | API token (defaults to `AVIARY_TOKEN`). |
| `--collection-title` | Override the default `MPS Upload <today>` collection title. |
| `--collection-description` | Description for a newly created collection. |
| `--base-url` | Force a specific API base URL (normally derived from `aviaryOrg`). |
| `--wait` | Seconds to pause between API calls (default `1.0`) to respect rate limits. |
| `--log-file` | CSV log file, one row per resource directory (default `mps_aviary_import_log.csv`). |
| `--retry-attempts` / `--retry-backoff` | Attempts and exponential-backoff base for media uploads and index creation. |
| `--media-ready-timeout` / `--media-ready-interval` | Bound the wait for media transcoding before attaching an index. |
| `--marc-import-timeout` / `--marc-import-interval` | Bound the wait for a MARC XML import job to finish. |
| `--resource-appear-timeout` | Bound the wait for a MARC-imported resource to appear in the collection. |

## Expected directory layout

```
top_level_directory/
└── <resource_dir>/
    ├── project.prop            # key=value metadata (aviaryOrg, alephID, title, access, …)
    └── deliverable/
        ├── *.mp3 / *.mp4 / *.mov   # media files (any depth)
        └── playlists/
            └── *playlist.xml       # AES60 (FADGI) index files, matched to media by <dc:identifier>
```

The run prints a per-resource log and a final summary of collections, resources,
media files, and indexes created, and appends a row per resource directory to the
CSV log. Errors on one resource directory are logged and the batch continues.
