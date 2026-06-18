# `aviary_directory_import.py` — Code Review Recommendations

A review of potential bugs and improvements for the Aviary bulk-import script.
Items are grouped by severity. Line references are approximate and may shift as
the file evolves.

## Status legend

- ✅ **Done** — fix applied to the script.
- ⬜ **Open** — not yet addressed.

---

## Bugs / correctness risks

### ✅ 1. No `timeout` on any `AviaryClient` HTTP call (hang risk)

Every `requests.get/post/put` inside the client omitted `timeout=` (only
`fetch_marc_xml` set one). A single stalled connection — especially the large
presigned media PUT — could block the entire bulk run forever with no recovery.

**Fix applied:** added `HTTP_TIMEOUT = (30, 300)` and `UPLOAD_TIMEOUT = (30, 600)`
constants and applied them to all 11 previously unguarded request calls
(collection list/create, resource create/access-update, media POST/PUT/complete,
media GET, MARC import POST/GET, collection-resource list, index create).

### ⬜ 2. Docstring promises retry/backoff that doesn't exist

The module comment claims "a small wait plus ret/backoff keeps a bulk run from
tripping the limiter," but `_pace()` is just a flat `time.sleep`. There is **no
429 handling and no retry anywhere**. On a long batch this is exactly when the
rate limiter is hit, and the script will instead raise `RuntimeError` from
`_require_ok`.

**Recommendation:** implement a retry-with-backoff wrapper around requests, or
correct the comment.

### ⬜ 3. Media↔index positional pairing is fragile across subdirectories

`find_media_files` walks the **entire** `deliverable/` tree recursively and sorts
by **basename only**, while `find_index_files` reads only `deliverable/playlists/`
non-recursively. Two problems:

- If media live in multiple subfolders, a basename-only sort can interleave files
  from different folders in an order that doesn't match the playlist order —
  silently pairing index N with the wrong media file.
- The recursive scan can sweep up proxies/derivatives/sample clips that happen to
  be `.mp3/.mp4/.mov`, throwing off the count and alignment.

**Recommendation:** sort by full relative path, restrict the media scan to the
level the playlists imply, or pair by a shared name stem rather than position.

### ✅ 4. `_media_is_ready` can throw on a non-string `duration`

`duration = (data.get("duration") or "").strip()` raises `AttributeError` if the
API returns a non-zero numeric `duration`, aborting that resource.

**Fix applied:** coerce with `str(...)` before stripping —
`str(data.get("duration") or "").strip()`.

### ⬜ 5. `wait_for_import` terminal heuristic can fire early or never

The `non_terminal` set is a hardcoded guess-list of readable strings, and the
terminal decision is an **OR**. A readable status not in the list (e.g.
`"Processing"`, `"Running"`) is treated as terminal immediately even while
`status` is still 2; conversely an unanticipated in-progress numeric status can
loop to timeout.

**Recommendation:** drive the decision primarily off the numeric `status`, with
the readable string as a secondary signal.

### ⬜ 6. Unchecked responses on the upload completion path

The presigned `complete` GET ignores its response entirely, and the presigned PUT
only does `raise_for_status()` — a storage backend that returns 200 with an error
body won't be caught. A failed `/complete` leaves a media file that never finishes
processing, and the script then waits the full `MEDIA_READY_TIMEOUT` before giving
up.

**Recommendation:** validate both responses and fail fast on a bad `/complete`.

---

## Robustness / design

### ⬜ 7. Not idempotent — re-runs create duplicates

The collection is reused when the title matches, but resources/media/indexes are
**never deduplicated**. A second run on the same day re-creates every resource and
re-uploads every media file. `resource_user_key` is computed and sent but never
used to check for an existing resource.

**Recommendation:** check for an existing resource by `resource_user_key` before
creating. Pair with a created-id manifest (see #11) for resumability.

### ✅ 8. Whole file read into memory for upload

The upload read the entire media file into RAM (`file_data = fh.read()`) before
the PUT — potentially gigabytes for `.mp4/.mov`.

**Fix applied:** stream straight from the file handle (`data=fh`) instead.

> **Related, still open:** the PUT hardcodes `Content-Type:
> application/octet-stream`. Some presigned URLs are signed for a specific
> content type, so this can cause a signature mismatch. Consider deriving the
> type via `mimetypes` to match what AVP signed.

### ⬜ 9. Config taken from only the first directory

`organization_id` and the base URL come solely from `resource_dirs[0]`'s
`project.prop`. If the batch is heterogeneous (mixed orgs), the rest are silently
sent to the wrong org.

**Recommendation:** validate that all directories agree on `aviaryOrg`; warn or
skip mismatches.

### ⬜ 10. `find_subdirectory` picks an arbitrary match

It returns the first hit from `os.walk`, whose ordering isn't guaranteed. If a
resource dir has more than one `deliverable` or `playlists` directory, the
selection is nondeterministic.

**Recommendation:** sort candidates or assert a single match.

---

## Minor / polish

### ⬜ 11. No file logging or run manifest

Long bulk runs only `print`. A persistent log (and a manifest of created ids)
would make failures diagnosable and partially recoverable. Pairs with #7 for
resumability.

### ⬜ 12. `language="en"` is hardcoded

`create_index` always tags indexes English regardless of content. Fine as a
documented default, but worth surfacing as a CLI flag if non-English deposits are
possible.

### ⬜ 13. Multiple-new-resource MARC case picks `max(new_ids)`

Reasonable, but if a concurrent import added the other resource, `max` could grab
the wrong one. The before/after collection diff assumes single-threaded ingest
into that collection — worth stating as a precondition.

### ⬜ 14. `/complete` and the presigned PUT bypass `_pace()` / `_require_ok`

Inconsistent with the rest of the client. Routing them through the same helpers
would unify pacing and error handling.

---

## Summary

| #  | Item                                          | Severity | Status |
|----|-----------------------------------------------|----------|--------|
| 1  | Missing HTTP timeouts                         | High     | ✅ Done |
| 2  | Claimed retry/backoff doesn't exist           | High     | ⬜ Open |
| 3  | Fragile media↔index pairing                   | High     | ⬜ Open |
| 4  | `_media_is_ready` numeric-duration crash      | Medium   | ✅ Done |
| 5  | `wait_for_import` terminal heuristic           | Medium   | ⬜ Open |
| 6  | Unchecked upload-completion responses         | Medium   | ⬜ Open |
| 7  | Not idempotent (duplicate re-runs)            | Medium   | ⬜ Open |
| 8  | Whole file read into memory for upload        | Medium   | ✅ Done |
| 9  | Config read from only the first directory     | Low      | ⬜ Open |
| 10 | `find_subdirectory` arbitrary match           | Low      | ⬜ Open |
| 11 | No file logging / run manifest                | Low      | ⬜ Open |
| 12 | Hardcoded `language="en"`                     | Low      | ⬜ Open |
| 13 | Multiple-new-resource MARC case               | Low      | ⬜ Open |
| 14 | `/complete` & PUT bypass client helpers       | Low      | ⬜ Open |
