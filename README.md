# Aviary Directory Import

A command-line tool for bulk-importing structured deposit directories into the
[Aviary platform](https://www.aviaryplatform.com) via its
[public REST API](https://www.aviaryplatform.com/api/v1/documentation).

## What it's for

Harvard Library MPS (Media Preservation Services) deposits arrive as directory
trees, each resource directory described by a `project.prop` metadata file and
holding its media under a `deliverable/` subdirectory. This script walks such a
tree and, for each resource directory, creates the corresponding Aviary
**collection**, **resource**, **media files**, **indexes**, and **captions** —
turning a batch of deposits into published Aviary records in one run.

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
     metadata (Agent roles such as composer/performer/creator, Date, Type,
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
6. **Creates captions** — every `.vtt` in the resource's `captions/`
   subdirectory, each attached to the media file whose filename (without
   extension) equals the caption's base identifier (its stem minus a trailing
   `_captions`) or begins with that base + `_`. Each media file receives at most
   one caption (`is_caption`, `is_public`, `language=en`); captions with no
   matching media file are skipped with a warning.

Media uploads, index creation, and caption creation are retried with exponential backoff on
transient errors, and each resource directory's outcome is appended as a row to
a CSV log (default `mps_aviary_import_log.csv`).

With the optional `--mint-urns` flag, after each resource is created the script
mints a persistent [NRS](https://nrs.harvard.edu) URN that resolves to the
resource's Aviary URL (via the `urn-minter` library), records it in the log's
`URN` column, and adds it to the resource's metadata as an Identifier (vocabulary
`URN`). See [Minting persistent URNs](#minting-persistent-urns-optional).

The HTTP request patterns mirror AVP's own published bulk-import scripts
(<https://github.com/WeAreAVP/aviary-api-scripts>).

## Requirements

- Python ≥ 3.13
- [`uv`](https://docs.astral.sh/uv/) for dependency management
  (`brew install uv`, or see the uv install docs).
- An Aviary API key/token.
- For `--mint-urns` only: NRS/Apigee credentials (see
  [Minting persistent URNs](#minting-persistent-urns-optional)).

## Setup

Dependencies are declared in `pyproject.toml` (`requests` and, for URN minting,
`urn-minter`). Install them into a project virtualenv with:

```bash
uv sync
```

`urn-minter` is pulled from HUIT Artifactory's `lts-python` index (configured in
`pyproject.toml`). Read access to that index is restricted to the Harvard VPN,
so connect to the VPN before running `uv sync`.

Run the script through uv so it uses that environment:

```bash
uv run aviary_directory_import.py ...
```

## Usage

```bash
export AVIARY_TOKEN="your_api_key"

# Preview the planned API calls without contacting Aviary (recommended first):
uv run aviary_directory_import.py /path/to/top_level_directory --dry-run

# Run the import for real:
uv run aviary_directory_import.py /path/to/top_level_directory

# Build resources from Harvard HOLLIS MARC XML where a usable alephID exists:
uv run aviary_directory_import.py /path/to/top_level_directory --importMarc

# Mint a persistent NRS URN for each resource (requires .env; see below):
uv run aviary_directory_import.py /path/to/top_level_directory --mint-urns
```

Run `uv run aviary_directory_import.py --help` for all options.

### Common options

| Option | Purpose |
| --- | --- |
| `--dry-run` | Scan and print the planned API calls without contacting Aviary. |
| `--importMarc` | Build resources from Harvard HOLLIS MARC XML when a usable `alephID` is present (default: always use the `project.prop` metadata mapping). |
| `--mint-urns` | Mint a persistent NRS URN per resource, record it in the log's `URN` column, and add it to the resource metadata as an Identifier (requires `urn-minter` + NRS credentials; see below). |
| `--urn-authority` | NRS authority path for `--mint-urns` (default `HUL.TEST`; or set `DEFAULT_AUTHORITY_PATH`). |
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

## Minting persistent URNs (optional)

With `--mint-urns`, each created resource also gets a persistent NRS URN that
resolves to its Aviary URL, recorded in the log's `URN` column and added to the
resource's metadata as an Identifier (vocabulary `URN`). This uses the
`urn-minter` library (installed via `uv sync`) and requires NRS credentials in a
`.env` file in the working directory, which `pydantic-settings` loads
automatically.

Copy the example and fill in the values:

```bash
cp .env.example .env
```

`.env` variables:

| Variable | Purpose |
| --- | --- |
| `NRS_ENDPOINT` | Apigee proxy URL for the target environment (dev / qa / prod). |
| `NRS_AGENT` | Request agent name; must have permission for the authority path in NRS. |
| `NRS_APIGEE_API_KEY` | Apigee-issued API key for the **same** environment as `NRS_ENDPOINT`. |
| `NRS_TIMEOUT` | Optional HTTP timeout in seconds (default `30`). |
| `DEFAULT_AUTHORITY_PATH` | Optional default for `--urn-authority` (otherwise `HUL.TEST`). |

`.env` holds a secret and is gitignored — never commit it. Apigee keys are
per-environment, so use the key issued for whichever endpoint you point at.
Verify the wiring without touching NRS by combining `--mint-urns --dry-run`.

A single URL can be minted directly with the throwaway helper:

```bash
uv run mint_one_urn.py --authority HUL.TEST https://<aviary-url> <resource_id>
```

## Expected directory layout

```
top_level_directory/
└── <resource_dir>/
    ├── project.prop            # key=value metadata (aviaryOrg, alephID, title, access, …)
    └── deliverable/
        ├── *.mp3 / *.mp4 / *.mov   # media files (any depth)
        └── playlists/
            └── *playlist.xml       # AES60 (FADGI) index files, matched to media by <dc:identifier>
    └── captions/
        └── *.vtt                   # caption transcripts, matched to media by filename base
```

The run prints a per-resource log and a final summary of collections, resources,
media files, indexes, and captions created, and appends a row per resource
directory to the CSV log. Errors on one resource directory are logged and the batch continues.
