#!/usr/bin/env python3
"""
aviary_directory_import.py
==========================

Extract metadata and files from a structured deposit directory (or a parent
directory containing several deposit directories) and import them into the
Aviary platform (https://www.aviaryplatform.com) using its public REST API
(https://www.aviaryplatform.com/api/v1/documentation).

What the script does
--------------------
1. Walks the directory the user supplies and finds every "resource directory"
   (a directory that directly contains a ``project.prop`` file).
2. Reads the Aviary organization ID from the ``aviaryOrg`` field of a
   ``project.prop`` file.
3. Reuses the Aviary collection titled ``MPS Upload {today's date}`` if one
   already exists in the organization; otherwise creates it with
   ``is_public = false`` and ``is_featured = true``.
4. For each resource directory creates one Aviary resource. By default the
   resource is built from the project.prop metadata mapping:
       title       <- project.prop "title" (falls back to "shelfnum", then
                      the directory name, if "title" is NULL/blank)
       description <- project.prop "metsLabel"
       access      <- project.prop "access"
       plus additional metadata fields (Description/Agent/Date/Format/Subject/
       Identifier with per-field vocabularies) mapped from project.prop fields
       such as abstract, creator, composer, performer, subject, date, genre,
       alephID, findingAid, etc. (see RESOURCE_METADATA_MAP /
       build_resource_metadata).
   If the optional ``--importMarc`` flag is given AND project.prop has a usable
   ``alephID``, the resource is instead built from the Harvard HOLLIS MARC XML
   record:
     a. The alephID is normalized to a HOLLIS id. It may be supplied as 9, 17,
        or 18 digits. A 17- or 18-digit value is used as-is; a 9-digit value
        is expanded by prepending "99" and appending "0203941" (e.g.
        "014511460" -> "990145114600203941").
     b. The MARC XML record is fetched from the Harvard HOLLIS webservice
        (https://webservices.lib.harvard.edu/rest/marc/hollis/<HOLLIS id>).
     c. The MARC XML file is submitted to Aviary's MARC XML import API
        (POST /api/v1/imports/marc_xml, with status = 2 so the import worker
        is scheduled). This import runs asynchronously, so the script then
        polls the import job (GET /api/v1/imports/{id}) and WAITS until it
        reports a terminal status before continuing.
     d. The import job does not return the id of the resource it creates, and
        that resource appears in the collection a moment after the import
        reports complete, so the script polls the collection's resource list
        and waits for the new resource to appear, then forces the project.prop
        access mapping onto it (PUT /api/v1/resources/{id}).
   Only once the import has completed and the resource is in hand does media
   import proceed. If ``--importMarc`` is not given, or ``alephID`` is NULL, or
   the MARC fetch/import fails, the project.prop metadata mapping above is used.
5. Imports one Aviary media file for every ``.mp3`` / ``.mp4`` / ``.mov`` found
   anywhere
   under that resource's ``deliverable`` subdirectory, sorted alphanumerically
   by filename. Each media file is created with
       access = true, is_downloadable = false, is_360 = false.
6. Imports one Aviary index for every ``*playlist.xml`` file found in the
   ``deliverable/playlists`` subdirectory. Each index is linked (via
   ``resource_file_id``) to the media file whose filename (without extension)
   equals the playlist's ``<dc:identifier>`` value -- e.g. a playlist with
   ``<dc:identifier>T-529_0006_DM_01_01_{...}</dc:identifier>`` links to the
   media file ``T-529_0006_DM_01_01_{...}.mp3``. An index with no matching
   media file is skipped with a warning. Each index is created with
       is_public = true, language = en, title = filename without extension.

If the optional ``--mint-urns`` flag is given, after each resource is created
the script mints a persistent NRS URN that resolves to the resource's Aviary
URL (using the ``urn-minter`` library) and records it in the log's ``URN``
column. NRS credentials come from the environment/.env (NRS_ENDPOINT,
NRS_AGENT, NRS_APIGEE_API_KEY); the authority path defaults to ``HUL.TEST``
(override with ``--urn-authority``).

The HTTP request patterns (resource create, presigned media upload, index
create) follow AVP's own published bulk-import script
(https://github.com/WeAreAVP/aviary-api-scripts).

The API base URL does NOT need to be supplied: it is derived automatically
from the ``aviaryOrg`` value in project.prop (see ``resolve_base_url``).

Usage
-----
    export AVIARY_TOKEN="your_api_key"

    # Default: build every resource from the project.prop metadata mapping.
    python3 aviary_directory_import.py /path/to/top_level_directory

    # Opt in to building resources from Harvard HOLLIS MARC XML (when a usable
    # alephID is present), falling back to the metadata mapping on failure.
    python3 aviary_directory_import.py /path/to/dir --importMarc

    # Mint a persistent NRS URN per resource, record it in the log's URN
    # column, and add it to the resource metadata (Identifier / "URN").
    # Requires the urn-minter library and NRS credentials in a .env file.
    python3 aviary_directory_import.py /path/to/dir --mint-urns

    # Preview the planned API calls without contacting Aviary.
    python3 aviary_directory_import.py /path/to/dir --dry-run

Run ``python3 aviary_directory_import.py --help`` for all options.
"""

import argparse
import csv
import datetime
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import requests

# urn-minter (HUIT Artifactory, lts-python index) is only needed when the
# --mint-urns flag is used, so the import is optional: the script still runs
# for plain imports on a machine without it installed. main() errors out if
# --mint-urns is passed while the library is missing.
try:
    from urn_minter import (
        mint_urns, MintItem, CreateResourceDetails, Status, SequenceName,
    )
    _URN_MINTER_AVAILABLE = True
except ImportError:
    _URN_MINTER_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Configuration constants (override most of these from the command line)
# --------------------------------------------------------------------------- #

# Format used in the collection title "MPS Upload {today's date}".
DATE_FORMAT = "%Y-%m-%d"

# Central Aviary platform host. Used as the API base URL when project.prop's
# aviaryOrg is a numeric organization ID (the org is then identified by the
# organization_id sent in API payloads). See resolve_base_url().
AVIARY_PLATFORM_HOST = "https://www.aviaryplatform.com/"

# Polite delay (seconds) between API calls. AVP rate-limits the API; a small
# wait plus ret/backoff keeps a bulk run from tripping the limiter.
WAIT_SECONDS = 1.0

# (connect, read) timeouts for HTTP calls so a stalled server can't hang the
# run. UPLOAD_TIMEOUT is used for the small multipart uploads (MARC XML import,
# index file). The large media PUT to presigned storage uses NO client-side
# timeout (matching AVP's bulk-import script), since a read timeout was cutting
# off multi-GB transfers mid-upload.
HTTP_TIMEOUT = (30, 300)
UPLOAD_TIMEOUT = (30, 3600)

# Retry/backoff for media and index creation (transient network/server errors).
RETRY_ATTEMPTS = 3       # total attempts per operation
RETRY_BACKOFF = 2.0      # base seconds; delay grows as RETRY_BACKOFF * 2**(n-1)

# Aviary transcodes uploaded media asynchronously; an index cannot be attached
# until the media file finishes processing. These bound how long to wait.
MEDIA_READY_TIMEOUT = 600.0    # max seconds to wait for one media file
MEDIA_READY_INTERVAL = 5.0     # seconds between status checks

# Harvard HOLLIS MARC XML webservice (one MARC record per normalized HOLLIS id).
HOLLIS_MARC_BASE_URL = "https://webservices.lib.harvard.edu/rest/marc/hollis/"

# NRS authority path under which persistent URNs are minted (--mint-urns). The
# authority must already exist in NRS and the configured agent must have
# permission for it. Override with --urn-authority.
URN_AUTHORITY_PATH = "HUL.TEST"

# A MARC XML import runs asynchronously and can take a while; bound the wait.
MARC_IMPORT_TIMEOUT = 900.0    # max seconds to wait for one MARC import job
MARC_IMPORT_INTERVAL = 5.0     # seconds between import-status checks

# After a MARC import completes, the created resource becomes visible in the
# collection listing a little later; bound how long to wait for it to appear.
RESOURCE_APPEAR_TIMEOUT = 120.0  # max seconds to wait for the new resource

# Files matched as media (anywhere under the "deliverable" directory).
MEDIA_EXTENSIONS = (".mp3", ".mp4", ".mov")

# Index files: XML files in deliverable/playlists whose name ends with this.
# This intentionally matches "...-playlist.xml" but NOT "...-playlist-1.xml"
# or the ".properties" sidecars, so indexes pair 1:1 with media files.
INDEX_FILENAME_SUFFIX = "playlist.xml"

# Names of the sub-directories to look for inside each resource directory.
DELIVERABLE_DIR_NAME = "deliverable"
PLAYLISTS_DIR_NAME = "playlists"

# Aviary resource Access Status values used here: public / private / internal
# ("internal" requires an Aviary API that accepts it). project.prop "access"
# uses short codes:
#   "PublicS" or "P" -> public
#   "R"              -> private
#   "N"              -> internal
ACCESS_STATUS_MAP = {
    "publics": "public",
    "p": "public",
    "r": "private",
    "n": "internal",
}
ACCESS_DEFAULT = "private"     # safe default if the value is unrecognized


# --------------------------------------------------------------------------- #
# project.prop parsing
# --------------------------------------------------------------------------- #

def parse_prop_file(path):
    """Parse a key=value .prop file into a dict.

    Handles quoted values (``key="value"``), bare values (``key=719``) and
    empty values (``key=``). Lines without '=' are ignored.
    """
    props = {}
    with open(path, "rt", encoding="utf-8-sig", errors="ignore") as fh:
        for line in fh:
            line = line.rstrip("\n").rstrip("\r")
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            props[key] = value
    return props


def is_null_value(value):
    """True if a prop value is missing/blank or the literal token ``NULL``.

    Export formats frequently write an absent field as the bare string
    ``NULL`` rather than leaving it empty, so both are treated as "no value".
    """
    return value is None or value.strip() == "" or value.strip().upper() == "NULL"


def normalize_aleph_id(value):
    """Normalize a project.prop alephID into a HOLLIS id.

    * 17 or 18 digits -> used as-is (HOLLIS ids can be either length).
    * 9 digits        -> prefixed with "99" and suffixed with "0203941"
                         (e.g. "014511460" -> "990145114600203941").
    * NULL/blank or any other length -> None (caller falls back to the old
      project.prop metadata mapping).
    """
    if is_null_value(value):
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) in (17, 18):
        return digits
    if len(digits) == 9:
        return "99" + digits + "0203941"
    return None


def fetch_marc_xml(hollis_id, dest_path, timeout=30):
    """Download MARC XML for a HOLLIS id and write it to dest_path.

    Raises RuntimeError on a non-200 response or content that does not look
    like MARC XML, so the caller can fall back to the metadata mapping.
    """
    url = HOLLIS_MARC_BASE_URL + hollis_id
    response = requests.get(url, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(
            f"HOLLIS fetch for {hollis_id} returned HTTP {response.status_code}")
    text = response.text or ""
    if "<" not in text or "record" not in text.lower():
        raise RuntimeError(
            f"HOLLIS response for {hollis_id} does not look like MARC XML")
    with open(dest_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return dest_path


def map_access_status(raw_value):
    """Map a project.prop 'access' code onto Aviary's access vocabulary.

    "PublicS"/"P" -> public, "R" -> private, "N" -> internal. Unrecognized or
    missing values fall back to ACCESS_DEFAULT.
    """
    if not raw_value:
        return ACCESS_DEFAULT
    return ACCESS_STATUS_MAP.get(raw_value.strip().lower(), ACCESS_DEFAULT)


# project.prop field -> (Aviary metadata field, vocabulary) for the metadata
# mapping used when creating a resource. Order is preserved in the output.
# "metsLabel" is handled separately (Description with no vocabulary).
RESOURCE_METADATA_MAP = [
    ("abstract", "Description", "Abstract"),
    ("actor", "Agent", "Actor"),
    ("adapter", "Agent", "Adapter"),
    ("arranger", "Agent", "Arranger"),
    ("commentator", "Agent", "Commentator"),
    ("composer", "Agent", "Composer"),
    ("creator", "Agent", "Creator"),
    ("date", "Date", ""),
    ("director", "Agent", "Director"),
    ("genre", "Format", ""),
    ("instrumentalist", "Agent", "Instrumentalist"),
    ("interviewee", "Agent", "Interviewee"),
    ("interviewer", "Agent", "Interviewer"),
    ("librettist", "Agent", "Librettist"),
    ("lyricist", "Agent", "Lyricist"),
    ("moderator", "Agent", "Moderator"),
    ("musicaldirector", "Agent", "Musical Director"),
    ("musician", "Agent", "Musician"),
    ("narrator", "Agent", "Narrator"),
    ("performer", "Agent", "Performer"),
    ("publisher", "Agent", "Publisher"),
    ("singer", "Agent", "Singer"),
    ("speaker", "Agent", "Speaker"),
    ("storyteller", "Agent", "Storyteller"),
    ("subject", "Subject", ""),
    ("vocalist", "Agent", "Vocalist"),
    ("alephID", "Identifier", "alephID"),
    ("findingAid", "Identifier", "findingAid"),
    ("shelfnum", "Identifier", "shelfnum"),
    ("depositnumber", "Identifier", "depositnumber"),
    ("collectionname", "Relation", "is part of"),
]


def build_resource_metadata(props):
    """Build the Aviary resource metadata dict from project.prop fields.

    Returns a dict mapping each Aviary metadata field (Description, Agent,
    Date, Format, Subject, Identifier) to a list of {vocabulary, value}
    entries. Fields whose project.prop value is NULL/blank are skipped.
    """
    metadata = {}

    def add(field, vocabulary, value):
        if not is_null_value(value):
            metadata.setdefault(field, []).append(
                {"vocabulary": vocabulary, "value": value.strip()})

    # metsLabel maps to Description with no vocabulary (the original mapping).
    add("Description", "", props.get("metsLabel"))
    for prop_key, field, vocabulary in RESOURCE_METADATA_MAP:
        add(field, vocabulary, props.get(prop_key))
    return metadata


def resolve_base_url(aviary_org, override=None):
    """Derive the Aviary API base URL from the project.prop ``aviaryOrg`` value.

    The user does not need to supply a base URL separately. Resolution order:

    * An explicit ``override`` (e.g. an org's custom domain) always wins.
    * If ``aviaryOrg`` is already a URL (``http(s)://...``) it is used as-is.
    * If ``aviaryOrg`` looks like a hostname (contains a dot, e.g.
      ``hfa.av.lib.harvard.edu``) it is used as the host.
    * If ``aviaryOrg`` is a bare subdomain label it becomes
      ``https://<label>.aviaryplatform.com/``.
    * If ``aviaryOrg`` is a numeric organization ID (as in the example
      project.prop, ``aviaryOrg=719``) there is no algorithmic mapping from the
      number to a subdomain, so the central Aviary platform host is used; the
      organization is identified by the ``organization_id`` sent in each API
      payload. Aviary custom/subdomain URLs are CNAME aliases of this same
      backend, so the central host serves the API for any organization.

    Returns a base URL guaranteed to end in a single trailing slash.
    """
    if override:
        return override.rstrip("/") + "/"

    value = ("" if aviary_org is None else str(aviary_org)).strip()
    if not value:
        return AVIARY_PLATFORM_HOST
    if value.startswith(("http://", "https://")):
        return value.rstrip("/") + "/"
    if "." in value:
        return "https://" + value.strip("/") + "/"
    if value.isdigit():
        return AVIARY_PLATFORM_HOST
    return f"https://{value}.aviaryplatform.com/"


# --------------------------------------------------------------------------- #
# Directory discovery
# --------------------------------------------------------------------------- #

def find_resource_directories(top_level):
    """Return sorted list of directories that directly contain project.prop.

    The supplied path itself counts if it contains a project.prop; otherwise
    every descendant directory is searched.
    """
    resource_dirs = []
    if os.path.isfile(os.path.join(top_level, "project.prop")):
        resource_dirs.append(top_level)
    for root, _dirs, files in os.walk(top_level):
        if root == top_level:
            continue
        if "project.prop" in files:
            resource_dirs.append(root)
    return sorted(set(resource_dirs))


def media_display_name(file_path):
    """Derive a media file's display name from its filename.

    The name is trimmed at the last ``_`` that occurs before the first ``{``.
    For example::

        T-529_0006_DM_01_01_{7B18028D-91CF-...}.mp3  ->  T-529_0006_DM_01_01

    If the filename has no ``{`` (or no ``_`` before it), the basename without
    its extension is used.
    """
    base = os.path.basename(file_path)
    brace_index = base.find("{")
    if brace_index == -1:
        return os.path.splitext(base)[0]
    prefix = base[:brace_index]
    underscore_index = prefix.rfind("_")
    if underscore_index == -1:
        return os.path.splitext(base)[0]
    return prefix[:underscore_index]


def find_subdirectory(start_dir, target_name):
    """Find the first directory named ``target_name`` (case-insensitive)."""
    target = target_name.lower()
    if os.path.basename(start_dir.rstrip(os.sep)).lower() == target:
        return start_dir
    for root, dirs, _files in os.walk(start_dir):
        for d in dirs:
            if d.lower() == target:
                return os.path.join(root, d)
    return None


def find_media_files(deliverable_dir):
    """All media files under the deliverable dir, sorted alphanumerically."""
    matches = []
    for root, _dirs, files in os.walk(deliverable_dir):
        for name in files:
            if name.lower().endswith(MEDIA_EXTENSIONS):
                matches.append(os.path.join(root, name))
    return sorted(matches, key=lambda p: os.path.basename(p).lower())


def find_index_files(deliverable_dir):
    """All *playlist.xml index files in deliverable/playlists, sorted."""
    playlists_dir = find_subdirectory(deliverable_dir, PLAYLISTS_DIR_NAME)
    if not playlists_dir:
        return []
    matches = []
    for name in os.listdir(playlists_dir):
        full = os.path.join(playlists_dir, name)
        if os.path.isfile(full) and name.lower().endswith(INDEX_FILENAME_SUFFIX):
            matches.append(full)
    return sorted(matches, key=lambda p: os.path.basename(p).lower())


def parse_playlist_identifiers(xml_path):
    """Return the <dc:identifier> values from a playlist XML file.

    Namespace-agnostic: matches any element whose local tag name is
    "identifier" (e.g. dc:identifier). Returns a list of stripped strings; an
    unparseable file yields an empty list.
    """
    identifiers = []
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError):
        return identifiers
    for elem in tree.iter():
        tag = elem.tag.rsplit("}", 1)[-1] if isinstance(elem.tag, str) else ""
        if tag == "identifier" and elem.text and elem.text.strip():
            identifiers.append(elem.text.strip())
    return identifiers


def media_match_key(media_path):
    """The key used to match a media file to a playlist's dc:identifier:
    the filename without its extension."""
    return os.path.splitext(os.path.basename(media_path))[0]


def parse_dmart_email_success(resource_dir):
    """Return the <emailSuccess> text from a dmart.conf in the resource dir.

    Looks for dmart.conf at the resource directory root, then anywhere beneath
    it. Returns "" if not found or unparseable.
    """
    conf_path = os.path.join(resource_dir, "dmart.conf")
    if not os.path.isfile(conf_path):
        conf_path = None
        for root, _dirs, files in os.walk(resource_dir):
            if "dmart.conf" in files:
                conf_path = os.path.join(root, "dmart.conf")
                break
    if not conf_path:
        return ""
    try:
        tree = ET.parse(conf_path)
    except (ET.ParseError, OSError):
        return ""
    for elem in tree.iter():
        tag = elem.tag.rsplit("}", 1)[-1] if isinstance(elem.tag, str) else ""
        if tag == "emailSuccess" and elem.text and elem.text.strip():
            return elem.text.strip()
    return ""


# --------------------------------------------------------------------------- #
# Aviary API client
# --------------------------------------------------------------------------- #

class AviaryClient:
    def __init__(self, base_url, token, organization_id, wait=WAIT_SECONDS,
                 dry_run=False, retry_attempts=RETRY_ATTEMPTS,
                 retry_backoff=RETRY_BACKOFF):
        # Guarantee exactly one trailing slash on the base URL.
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        # The Aviary API requires the organization id as a header on every
        # endpoint used here (resources, media_files, indexes, collections).
        self.organization_id = str(organization_id)
        self.wait = wait
        self.dry_run = dry_run
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_backoff = retry_backoff

    # -- helpers ----------------------------------------------------------- #

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.token}",
            "organization-id": self.organization_id,
        }

    def _url(self, path):
        return self.base_url + path.lstrip("/")

    def _pace(self):
        if self.wait:
            time.sleep(self.wait)

    def _with_retries(self, what, func):
        """Call func() with retries and exponential backoff.

        Retries transient network errors (timeouts, connection drops) and
        server-side failures up to retry_attempts times, then re-raises the
        last exception so the caller's per-item handler can log it.
        """
        last_exc = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return func()
            except (requests.exceptions.RequestException, RuntimeError) as exc:
                last_exc = exc
                if attempt >= self.retry_attempts:
                    break
                delay = self.retry_backoff * (2 ** (attempt - 1))
                print(f"      {what} attempt {attempt} failed ({exc}); "
                      f"retrying in {delay:.0f}s")
                time.sleep(delay)
        raise last_exc

    @staticmethod
    def _extract_id(payload):
        """Pull the new record's id out of an Aviary create response.

        Aviary nests the id differently per endpoint (resources return it under
        data.update.id, media under data.id), so try the known shapes.
        """
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", payload)
        if isinstance(data, dict):
            for key in ("update", "resource", "media_file", "index"):
                nested = data.get(key)
                if isinstance(nested, dict) and "id" in nested:
                    return nested["id"]
            if "id" in data:
                return data["id"]
        return payload.get("id")

    @staticmethod
    def _has_error(payload):
        return isinstance(payload, dict) and payload.get("error")

    def _require_ok(self, response, what):
        """Validate an Aviary create/update response or raise RuntimeError.

        Catches every failure shape seen from the API: an ``error`` or
        ``errors`` field, an explicit ``success: false``, or a non-2xx HTTP
        status. A successful Aviary response is ``{"data": {...},
        "success": true}`` (or simply carries a record id).
        """
        payload = self._safe_json(response)
        if isinstance(payload, dict):
            if payload.get("error"):
                raise RuntimeError(f"{what} failed: {payload['error']}")
            if payload.get("errors"):
                raise RuntimeError(f"{what} failed: {payload['errors']}")
            if payload.get("success") is False:
                raise RuntimeError(f"{what} failed: {payload}")
        if not (200 <= response.status_code < 300):
            raise RuntimeError(
                f"{what} failed: HTTP {response.status_code}: "
                f"{str(payload)[:300]}")
        return payload

    # -- collections ------------------------------------------------------- #

    def find_collection_by_title(self, title, page_size=100, max_pages=200):
        """Return the id of an existing collection whose title matches exactly.

        Pages through GET /api/v1/collections (which requires the
        organization-id header) and returns the first collection whose ``title``
        equals ``title``. Returns ``None`` if no match exists.
        """
        if self.dry_run:
            print(f"    [dry-run] GET {self._url('api/v1/collections')}  "
                  f"(would search for an existing collection titled {title!r})")
            return None

        url = self._url("api/v1/collections")
        for page_number in range(1, max_pages + 1):
            params = {"page_size": page_size, "page_number": page_number}
            response = requests.get(url, headers=self._headers(), params=params,
                                    timeout=HTTP_TIMEOUT)
            self._pace()
            payload = self._safe_json(response)
            if self._has_error(payload):
                raise RuntimeError(
                    f"Collection list failed: {payload['error']}")
            items = payload.get("data") if isinstance(payload, dict) else None
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("title") == title:
                    # Collection objects use "id"; tolerate alternate keys.
                    for key in ("id", "collection_id", "resource_id"):
                        if key in item:
                            return item[key]
            if len(items) < page_size:
                break  # last page reached
        return None

    def create_collection(self, title, description="", is_public=False,
                          is_featured=True):
        """Create a collection and return its id.

        Per the Aviary OpenAPI spec, POST /api/v1/collections takes the
        organization id as the ``organization-id`` header and a
        multipart/form-data body. The resource "description" maps to the
        collection ``about`` field. The new id is returned at ``data.id``.
        """
        url = self._url("api/v1/collections")
        data = {
            "title": title,
            "about": description,
            "is_public": "true" if is_public else "false",
            "is_featured": "true" if is_featured else "false",
        }
        if self.dry_run:
            print(f"    [dry-run] POST {url}  "
                  f"(multipart) body={data} header organization-id="
                  f"{self.organization_id}")
            return "DRY-RUN-COLLECTION-ID"
        # files={} forces requests to send a multipart/form-data body.
        response = requests.post(url, headers=self._headers(), data=data,
                                 files={}, timeout=HTTP_TIMEOUT)
        self._pace()
        payload = self._require_ok(response, "Collection create")
        collection_id = self._extract_id(payload)
        if collection_id is None:
            raise RuntimeError(
                f"Could not find collection id in response: {payload}"
            )
        return collection_id

    # -- resources --------------------------------------------------------- #

    def create_resource(self, collection_id, resource_user_key, title,
                         metadata, access):
        """Create a resource and return its id.

        metadata is the Aviary metadata dict (field -> list of
        {vocabulary, value}), built from project.prop by
        build_resource_metadata().
        """
        url = self._url("api/v1/resources")
        data = {
            "resource_user_key": resource_user_key,
            "collection_id": collection_id,
            "title": title,
            "access": access,
            "metadata": metadata,
        }
        if self.dry_run:
            fields = ", ".join(f"{k}({len(v)})" for k, v in metadata.items())
            print(f"    [dry-run] POST {url}  "
                  f"title={title!r} access={access!r} metadata=[{fields}]")
            return "DRY-RUN-RESOURCE-ID"
        response = requests.post(url, headers=self._headers(), json=data,
                                 timeout=HTTP_TIMEOUT)
        self._pace()
        payload = self._require_ok(response, "Resource create")
        resource_id = self._extract_id(payload)
        if resource_id is None:
            raise RuntimeError(f"Could not find resource id in response: {payload}")
        return resource_id

    def update_resource_access(self, resource_id, access):
        """Set a resource's Access Status via PUT /api/v1/resources/{id}.

        Used to force the project.prop access mapping onto a resource created
        by the MARC XML import (the import sets its own default access).
        """
        url = self._url(f"api/v1/resources/{resource_id}")
        data = {"access": access}
        if self.dry_run:
            print(f"      [dry-run] PUT {url}  access={access!r}")
            return
        response = requests.put(url, headers=self._headers(), json=data,
                                timeout=HTTP_TIMEOUT)
        self._pace()
        self._require_ok(response, "Resource access update")

    def get_resource_direct_url(self, resource_id):
        """Return a resource's direct_url via GET /api/v1/resources/{id}.

        Returns "" if it can't be determined. The field appears on the resource
        object (and under data.update in some responses).
        """
        if self.dry_run:
            return f"https://example.aviaryplatform.com/r/{resource_id}"
        url = self._url(f"api/v1/resources/{resource_id}")
        try:
            response = requests.get(url, headers=self._headers(),
                                    timeout=HTTP_TIMEOUT)
            self._pace()
            payload = self._safe_json(response)
        except requests.exceptions.RequestException:
            return ""
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            data = data[0] if data else None
        for candidate in (data, (data or {}).get("update") if isinstance(data, dict) else None):
            if isinstance(candidate, dict) and candidate.get("direct_url"):
                return candidate["direct_url"]
        return ""

    def _get_resource_metadata_list(self, resource_id):
        """Return a resource's metadata in the GET list form, or [] on error:
        [{"label": <field>, "data": [{"value": .., "vocabulary": ..}]}, ...]."""
        url = self._url(f"api/v1/resources/{resource_id}")
        try:
            response = requests.get(url, headers=self._headers(),
                                    timeout=HTTP_TIMEOUT)
            self._pace()
            payload = self._safe_json(response)
        except requests.exceptions.RequestException:
            return []
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            data = data[0] if data else None
        for candidate in (data, (data or {}).get("update")
                          if isinstance(data, dict) else None):
            if isinstance(candidate, dict) and isinstance(
                    candidate.get("metadata"), list):
                return candidate["metadata"]
        return []

    def add_resource_urn_metadata(self, resource_id, urn):
        """Add an Identifier metadata element (vocabulary "URN") to a resource.

        PUT /api/v1/resources/{id} using the SAME metadata shape as resource
        creation: {FieldLabel: [{"vocabulary": .., "value": ..}]}, where value
        is a plain string. (The {tag, data:[{value,..}]} shape from the API's
        location example makes the server treat each value as a hash and raises
        "no implicit conversion of Symbol into Integer" for text values.) The
        resource's existing Identifier entries (alephID, findingAid, ...) are
        read first and re-sent with the URN appended so they are preserved;
        the update merges at the field level, leaving other fields intact.
        """
        url = self._url(f"api/v1/resources/{resource_id}")
        if self.dry_run:
            print(f"      [dry-run] PUT {url}  metadata.Identifier += "
                  f"{{vocabulary: 'URN', value: {urn!r}}}")
            return
        identifiers = []
        for entry in self._get_resource_metadata_list(resource_id):
            if isinstance(entry, dict) and entry.get("label") == "Identifier":
                for d in entry.get("data", []):
                    if isinstance(d, dict):
                        identifiers.append(
                            {"vocabulary": d.get("vocabulary", ""),
                             "value": d.get("value", "")})
                break
        identifiers.append({"vocabulary": "URN", "value": urn})
        body = {"metadata": {"Identifier": identifiers}}
        response = requests.put(url, headers=self._headers(), json=body,
                                timeout=HTTP_TIMEOUT)
        self._pace()
        self._require_ok(response, "Resource URN metadata update")

    # -- media files ------------------------------------------------------- #

    def upload_media_file(self, file_path, resource_id, sort_order):
        """Upload a local media file via the presigned-URL flow.

        Returns the new media file id. Mirrors AVP's upload_from_path():
          1. POST media_files with media_file='presigned' -> presigned_url + id
          2. PUT the file bytes to the presigned URL
          3. GET media_files/{id}/complete
        Per the task, every media file is access=true, is_downloadable=false,
        is_360=false.
        """
        url = self._url("api/v1/media_files")
        filename = os.path.basename(file_path)
        display_name = media_display_name(file_path)
        params = {
            "collection_resource_id": resource_id,
            "access": "true",
            "is_downloadable": "false",
            "is_360": "false",
            "media_file": "presigned",
            "display_name": display_name,
            "filename": filename,
            "sort_order": sort_order,
            "thumbnail_path": "",
        }
        if self.dry_run:
            print(f"    [dry-run] POST {url}  "
                  f"file={filename!r} display_name={display_name!r} "
                  f"sort_order={sort_order} "
                  f"access=true is_downloadable=false is_360=false")
            return f"DRY-RUN-MEDIA-ID-{sort_order}"

        # Step 1: request the presigned upload slot.
        files = {"media_file": "presigned"}
        response = requests.post(url, headers=self._headers(),
                                 params=params, files=files,
                                 timeout=HTTP_TIMEOUT)
        self._pace()
        payload = self._require_ok(response, "Media create")
        try:
            presigned_url = payload["data"]["presigned_url"]
            media_id = payload["data"]["id"]
        except (KeyError, TypeError):
            raise RuntimeError(f"Unexpected media create response: {payload}")

        # Step 2: PUT the bytes to the presigned (storage) URL using the same
        # method as AVP's bulk-import script: the whole file in memory, the
        # bearer auth header with Content-Type "text/plain", and NO client-side
        # timeout. A read timeout was cutting off large (multi-GB) uploads
        # mid-transfer; the official script omits the timeout and succeeds.
        with open(os.path.abspath(file_path), "rb") as fh:
            file_data = fh.read()
        size_mb = len(file_data) / (1024 * 1024)
        print(f"        uploading {filename} ({size_mb:.1f} MB)...")
        upload_headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "text/plain",
        }
        put_resp = requests.put(presigned_url, headers=upload_headers,
                                data=file_data)
        put_resp.raise_for_status()

        # Step 3: tell Aviary the upload is complete.
        complete_url = self._url(f"api/v1/media_files/{media_id}/complete")
        requests.get(complete_url, headers=self._headers(), timeout=HTTP_TIMEOUT)
        self._pace()
        return media_id

    def get_media_file(self, media_id):
        """GET a media file record (used to check processing status)."""
        url = self._url(f"api/v1/media_files/{media_id}")
        response = requests.get(url, headers=self._headers(),
                                timeout=HTTP_TIMEOUT)
        self._pace()
        return self._safe_json(response)

    @staticmethod
    def _media_is_ready(data):
        """Heuristic readiness check for a media file GET payload.

        Aviary transcodes uploads asynchronously: right after /complete the
        file reports processing=true. We treat it as ready when processing has
        cleared, or (if that field is absent) when a transcode_url or real
        duration has been populated.
        """
        if not isinstance(data, dict):
            return False
        processing = data.get("processing")
        if processing is True:
            return False
        if processing is False:
            return True
        # processing not reported: fall back to other positive signals.
        if data.get("transcode_url"):
            return True
        # Coerce to str first: the API may return a numeric duration, and
        # calling .strip() on a number would raise AttributeError.
        duration = str(data.get("duration") or "").strip()
        if duration and duration not in ("00:00:00", "00:00:00.000", "0"):
            return True
        return False

    def wait_for_media_ready(self, media_id, timeout, interval):
        """Poll until a media file finishes processing, or timeout.

        Returns True if the file became ready, False on timeout. An index
        cannot be attached to a media file that is still processing, so this is
        called before creating the linked index.
        """
        if self.dry_run:
            return True
        deadline = time.time() + max(0, timeout)
        while True:
            payload = self.get_media_file(media_id)
            data = payload.get("data") if isinstance(payload, dict) else None
            if self._media_is_ready(data):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(max(1, interval))

    # -- MARC XML imports -------------------------------------------------- #

    def create_marc_import(self, collection_id, title, marc_xml_path):
        """Submit a MARC XML import job; returns the import job id.

        POST /api/v1/imports/marc_xml (multipart/form-data) with the
        organization-id header. Required fields: collection_id, title,
        marc_xml_file. status is set to 2 ("In Progress"): the import worker is
        only scheduled when status is In Progress. send_success_notification is
        false so Aviary sends no completion/failure email per import. All form
        values are sent as strings, since requests can mis-encode non-string
        multipart fields.
        """
        url = self._url("api/v1/imports/marc_xml")
        data = {
            "collection_id": str(collection_id),
            "title": title,
            "status": "2",  # In Progress -> schedules the import worker
            "send_success_notification": "false",  # no per-import email
        }
        if self.dry_run:
            print(f"      [dry-run] POST {url}  (multipart) "
                  f"collection_id={collection_id} title={title!r} status=2 "
                  f"send_success_notification=false "
                  f"marc_xml_file={os.path.basename(marc_xml_path)!r}")
            return "DRY-RUN-IMPORT-ID"
        with open(marc_xml_path, "rb") as fh:
            files = {"marc_xml_file":
                     (os.path.basename(marc_xml_path), fh, "application/xml")}
            response = requests.post(url, headers=self._headers(),
                                     data=data, files=files,
                                     timeout=UPLOAD_TIMEOUT)
        self._pace()
        payload = self._require_ok(response, "MARC import create")
        import_id = self._extract_id(payload)
        if import_id is None:
            raise RuntimeError(f"MARC import returned no id: {payload}")
        return import_id

    def get_import(self, import_id):
        """GET an import job record."""
        url = self._url(f"api/v1/imports/{import_id}")
        response = requests.get(url, headers=self._headers(),
                                timeout=HTTP_TIMEOUT)
        self._pace()
        return self._safe_json(response)

    @staticmethod
    def _first_record(payload):
        """Import GETs may return data as a one-element list or a dict."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            return data[0] if data else {}
        if isinstance(data, dict):
            return data
        return {}

    def wait_for_import(self, import_id, timeout, interval):
        """Poll an import job until it reaches a terminal status, or timeout.

        Returns the final import record (dict) once it is no longer "Not
        Started"/"In Progress", or None on timeout. The caller inspects
        status_readable to tell success from failure.
        """
        if self.dry_run:
            return {"status": 3, "status_readable": "Completed"}
        non_terminal = ("", "not started", "in progress", "queued",
                        "pending", "scheduled")
        deadline = time.time() + max(0, timeout)
        while True:
            record = self._first_record(self.get_import(import_id))
            status = record.get("status") if isinstance(record, dict) else None
            readable = ((record.get("status_readable") or "").strip().lower()
                        if isinstance(record, dict) else "")
            terminal = (status not in (1, 2, None)) or \
                       (readable not in non_terminal)
            if terminal:
                return record
            if time.time() >= deadline:
                return None
            time.sleep(max(1, interval))

    def list_collection_resource_ids(self, collection_id, page_size=100,
                                     max_pages=200):
        """Return the set of resource ids currently in a collection.

        Used to identify the resource a MARC import creates, by diffing the
        collection before and after the import.
        """
        if self.dry_run:
            return set()
        url = self._url(f"api/v1/collections/{collection_id}/resources")
        ids = set()
        for page_number in range(1, max_pages + 1):
            params = {"page_size": page_size, "page_number": page_number}
            response = requests.get(url, headers=self._headers(), params=params,
                                    timeout=HTTP_TIMEOUT)
            self._pace()
            payload = self._safe_json(response)
            items = payload.get("data") if isinstance(payload, dict) else None
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                # A resource object's id field is "resource_id"; tolerate "id".
                rid = item.get("resource_id", item.get("id"))
                if rid is not None:
                    ids.add(rid)
            if len(items) < page_size:
                break
        return ids

    def wait_for_new_collection_resource(self, collection_id, before_ids,
                                         timeout, interval):
        """Poll a collection until a resource id not in before_ids appears.

        A MARC import's status flips to Complete before the created resource
        becomes visible in the (eventually-consistent) collection listing, so
        the before/after diff must be retried rather than checked once.
        Returns the set of new resource ids (empty on timeout).
        """
        if self.dry_run:
            return set()
        start = time.time()
        deadline = start + max(0, timeout)
        attempt = 0
        while True:
            attempt += 1
            new_ids = (self.list_collection_resource_ids(collection_id)
                       - before_ids)
            if new_ids:
                return new_ids
            if time.time() >= deadline:
                return new_ids
            print(f"        waiting for new resource to appear "
                  f"(attempt {attempt}, {int(time.time() - start)}s elapsed)")
            time.sleep(max(1, interval))

    # -- indexes ----------------------------------------------------------- #

    def create_index(self, index_file, media_id):
        """Create an index from a playlist XML file linked to a media file.

        Per the task: is_public=true, language=en, title = filename without
        extension, resource_file_id = the linked media file id.

        The indexes endpoint requires language, title, is_public,
        associated_file and resource_file_id. The playlist XML files are AES60
        (FADGI) XML, so is_aes60_xml is set true; otherwise Aviary tries to
        parse them as OHMS XML and fails with an internal nil error.
        """
        url = self._url("api/v1/indexes")
        title = os.path.splitext(os.path.basename(index_file))[0]
        data = {
            "language": "en",
            "title": title,
            "is_public": "true",
            "resource_file_id": media_id,
            "is_aes60_xml": "true",
        }
        if self.dry_run:
            print(f"    [dry-run] POST {url}  "
                  f"title={title!r} resource_file_id={media_id} "
                  f"is_public=true language=en is_aes60_xml=true "
                  f"file={os.path.basename(index_file)!r}")
            return f"DRY-RUN-INDEX-{title}"

        file_type = mimetypes.guess_type(index_file)[0] or "application/xml"
        with open(index_file, "rb") as fh:
            files = {"associated_file": (os.path.basename(index_file), fh, file_type)}
            response = requests.post(url, headers=self._headers(),
                                     data=data, files=files,
                                     timeout=UPLOAD_TIMEOUT)
        self._pace()
        payload = self._require_ok(response, "Index create")
        index_id = self._extract_id(payload)
        if index_id is None:
            raise RuntimeError(f"Index create returned no id: {payload}")
        return index_id

    # -- misc -------------------------------------------------------------- #

    @staticmethod
    def _safe_json(response):
        try:
            return response.json()
        except ValueError:
            return {"error": f"Non-JSON response (HTTP {response.status_code}): "
                             f"{response.text[:300]}"}


# --------------------------------------------------------------------------- #
# Per-resource processing
# --------------------------------------------------------------------------- #

def build_resource_user_key(props, resource_dir):
    """A stable, unique-ish key for the resource within the collection."""
    deposit = props.get("depositnumber", "").strip()
    shelf = props.get("shelfnum", "").strip()
    if deposit and shelf:
        return f"{deposit}_{shelf}"
    if deposit:
        return deposit
    return os.path.basename(resource_dir.rstrip(os.sep))


def create_resource_via_marc(client, collection_id, hollis_id, job_title,
                             work_dir, timeout, interval, resource_appear_timeout):
    """Fetch MARC XML for a HOLLIS id and import it as an Aviary resource.

    Returns the new resource id, or None if anything fails (the caller then
    falls back to the project.prop metadata mapping). The created resource is
    identified by diffing the collection's resource ids around the import,
    since the import job response does not include the resulting resource id.
    """
    marc_path = os.path.join(work_dir, f"{hollis_id}.xml")
    try:
        if client.dry_run:
            print(f"      [dry-run] GET {HOLLIS_MARC_BASE_URL}{hollis_id}")
        else:
            fetch_marc_xml(hollis_id, marc_path)
            print(f"      fetched MARC XML for HOLLIS {hollis_id}")
    except Exception as exc:
        print(f"      ERROR fetching MARC XML for {hollis_id}: {exc}")
        return None

    resources_before = client.list_collection_resource_ids(collection_id)

    try:
        import_id = client.create_marc_import(collection_id, job_title, marc_path)
    except Exception as exc:
        print(f"      ERROR submitting MARC import: {exc}")
        return None

    print(f"      MARC import job id {import_id}; waiting for it to finish...")
    record = client.wait_for_import(import_id, timeout, interval)
    if record is None:
        print(f"      WARNING: MARC import {import_id} did not finish within "
              f"{timeout}s.")
        return None
    readable = (record.get("status_readable") or "").strip()
    if "fail" in readable.lower() or "error" in readable.lower():
        print(f"      ERROR: MARC import {import_id} failed (status "
              f"{readable!r}); logs: {record.get('logs')}")
        return None
    print(f"      MARC import complete (status {readable or 'unknown'}).")

    if client.dry_run:
        return "DRY-RUN-MARC-RESOURCE-ID"

    # The resource appears in the collection listing slightly after the import
    # reports Complete, so poll for it rather than checking once.
    new_ids = client.wait_for_new_collection_resource(
        collection_id, resources_before, resource_appear_timeout, interval)
    if len(new_ids) == 1:
        return next(iter(new_ids))
    if not new_ids:
        print(f"      WARNING: MARC import finished but no new resource appeared "
              f"in the collection within {resource_appear_timeout}s.")
        return None
    chosen = max(new_ids)
    print(f"      WARNING: MARC import produced multiple new resources "
          f"{sorted(new_ids)}; using {chosen}.")
    return chosen


def mint_resource_urn(application_id, aviary_url, authority_path, dry_run):
    """Mint a persistent NRS URN that resolves to a created Aviary URL.

    Wraps urn_minter.mint_urns for the single-resource case: builds one
    MintItem (explicit-URL form, file-sequenced resource name) and returns the
    minted URN string, or "" on failure. NRS credentials come from the
    environment (NRS_ENDPOINT / NRS_AGENT / NRS_APIGEE_API_KEY) via the
    library's NRSClientConfig.from_settings() fallback, so no config is passed.

    mint_urns does not raise on API errors (auth/server/transport become a
    MintResult with an error message), so callers just check the returned "".
    """
    item = MintItem(application_id, CreateResourceDetails(
        authority_path=authority_path,
        status=Status.ACTIVE,
        url=aviary_url,
        sequence=SequenceName.FILE,
    ))
    result = mint_urns([item], dry_run=dry_run)[0]
    if dry_run:
        # dry_run returns an empty-URN MintResult (no urn, no error): the
        # payload was built and logged but NRS was never called.
        return ""
    if result.ok:
        return result.urn
    print(f"    WARNING: URN mint failed for {aviary_url}: {result.error}")
    return ""


def process_resource_directory(client, collection_id, resource_dir, args,
                               work_dir):
    """Create one resource plus its media files and indexes."""
    media_ready_timeout = args.media_ready_timeout
    media_ready_interval = args.media_ready_interval
    name = os.path.basename(resource_dir.rstrip(os.sep))
    print(f"\n=== Resource directory: {name} ===")

    props = parse_prop_file(os.path.join(resource_dir, "project.prop"))
    # Title comes from project.prop "title"; if that is NULL/blank, fall back
    # to the "shelfnum" field, then finally to the directory name.
    if not is_null_value(props.get("title")):
        title = props["title"].strip()
    elif not is_null_value(props.get("shelfnum")):
        title = props["shelfnum"].strip()
    else:
        title = name
    description = props.get("metsLabel", "").strip()
    access = map_access_status(props.get("access", ""))
    resource_user_key = build_resource_user_key(props, resource_dir)

    print(f"    title       : {title}")
    print(f"    access      : {props.get('access', '')!r} -> {access}")
    print(f"    description : {description[:80]}{'...' if len(description) > 80 else ''}")

    counts = {"resource": 0, "media": 0, "indexes": 0}

    # Build the CSV log row for this resource (main() fills #media/#indexes
    # from counts and writes it). Success/URL are updated as we go.
    counts["log_row"] = {
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "title": title,
        "aviaryOrg": props.get("aviaryOrg", "").strip(),
        "ownercode": props.get("ownercode", "").strip(),
        "depositnumber": props.get("depositnumber", "").strip(),
        "shelfnum": props.get("shelfnum", "").strip(),
        "Success": "Failure",
        "#media": 0,
        "#indexes": 0,
        "URL": "",
        "URN": "",
        "emailSuccess": parse_dmart_email_success(resource_dir),
    }
    log_row = counts["log_row"]

    # Resource creation. By default the resource is built from the project.prop
    # metadata mapping. Only when --importMarc is passed AND project.prop has a
    # usable alephID is the resource built from the Harvard HOLLIS MARC XML
    # record (falling back to the metadata mapping on any MARC failure).
    resource_id = None
    if args.import_marc:
        aleph_raw = props.get("alephID")
        hollis_id = normalize_aleph_id(aleph_raw)
        if hollis_id:
            print(f"    alephID     : {aleph_raw!r} -> HOLLIS {hollis_id}")
            resource_id = create_resource_via_marc(
                client, collection_id, hollis_id,
                job_title=f"MARC import {hollis_id} ({name})",
                work_dir=work_dir,
                timeout=args.marc_import_timeout,
                interval=args.marc_import_interval,
                resource_appear_timeout=args.resource_appear_timeout,
            )
            if resource_id is None:
                print("    Falling back to project.prop metadata mapping.")
            else:
                # The MARC import sets its own default access, so force the
                # project.prop access mapping onto the new resource.
                try:
                    client.update_resource_access(resource_id, access)
                    print(f"      forced access on MARC resource -> {access}")
                except Exception as exc:
                    print(f"      WARNING: could not set access on resource "
                          f"{resource_id}: {exc}")
        elif not is_null_value(aleph_raw):
            print(f"    alephID     : {aleph_raw!r} (could not normalize to a "
                  f"9-, 17-, or 18-digit id; using metadata mapping)")
        else:
            print("    alephID     : NULL; using metadata mapping")

    if resource_id is None:
        try:
            resource_id = client.create_resource(
                collection_id=collection_id,
                resource_user_key=resource_user_key,
                title=title,
                metadata=build_resource_metadata(props),
                access=access,
            )
        except Exception as exc:
            print(f"    ERROR creating resource: {exc}")
            return counts
    counts["resource"] = 1
    aviary_url = client.get_resource_direct_url(resource_id)
    log_row["URL"] = aviary_url
    print(f"    -> resource id: {resource_id}")

    # Mint a persistent NRS URN that resolves to the Aviary URL (opt-in).
    if args.mint_urns and aviary_url:
        urn = mint_resource_urn(resource_user_key, aviary_url,
                                args.urn_authority, args.dry_run)
        log_row["URN"] = urn
        if urn:
            print(f"    -> URN: {urn}")
        # Record the URN as a resource metadata element (Identifier / "URN").
        urn_for_metadata = urn or ("DRY-RUN-URN" if args.dry_run else "")
        if urn_for_metadata:
            try:
                client.add_resource_urn_metadata(resource_id, urn_for_metadata)
                if urn:
                    print("    -> recorded URN in resource metadata")
            except Exception as exc:
                print(f"    WARNING: could not record URN metadata on resource "
                      f"{resource_id}: {exc}")

    # Locate the deliverable directory for this resource.
    deliverable_dir = find_subdirectory(resource_dir, DELIVERABLE_DIR_NAME)
    if not deliverable_dir:
        print("    (no 'deliverable' subdirectory found; no media/indexes)")
        return counts

    media_files = find_media_files(deliverable_dir)
    index_files = find_index_files(deliverable_dir)
    print(f"    media files : {len(media_files)} | index files: {len(index_files)}")

    # Upload media files in alphanumeric order (this sets their Aviary
    # sort_order). Map each uploaded media's filename stem -> its id so indexes
    # can be matched to media by the playlist's <dc:identifier>.
    media_id_by_key = {}
    for sort_order, media_path in enumerate(media_files, start=1):
        print(f"      media[{sort_order}] {os.path.basename(media_path)}")
        try:
            media_id = client._with_retries(
                f"media upload '{os.path.basename(media_path)}'",
                lambda mp=media_path, so=sort_order: client.upload_media_file(
                    mp, resource_id, so))
            media_id_by_key[media_match_key(media_path)] = media_id
            counts["media"] += 1
        except Exception as exc:
            print(f"      ERROR uploading media "
                  f"'{os.path.basename(media_path)}': {exc}")

    # Match each index to a media file by the playlist's <dc:identifier>, which
    # equals the media filename without its extension.
    for index_path in index_files:
        index_name = os.path.basename(index_path)
        identifiers = parse_playlist_identifiers(index_path)
        media_id = None
        matched_key = None
        for ident in identifiers:
            if ident in media_id_by_key:
                media_id = media_id_by_key[ident]
                matched_key = ident
                break
        if media_id is None:
            detail = (f"dc:identifier {identifiers}" if identifiers
                      else "no <dc:identifier> found")
            print(f"      WARNING: index '{index_name}' has no matching media "
                  f"file ({detail}); skipping.")
            continue
        print(f"      index '{index_name}' -> media '{matched_key}' "
              f"(id {media_id})")
        # An index can't attach while the media file is still transcoding, so
        # wait for it to finish processing first.
        if not client.wait_for_media_ready(media_id, media_ready_timeout,
                                           media_ready_interval):
            print(f"      WARNING: media id {media_id} still processing after "
                  f"{media_ready_timeout}s; attempting index anyway.")
        else:
            print(f"      media id {media_id} ready; creating index")
        try:
            client._with_retries(
                f"index create '{index_name}'",
                lambda ip=index_path, mid=media_id: client.create_index(ip, mid))
            counts["indexes"] += 1
        except Exception as exc:
            print(f"      ERROR creating index '{index_name}': {exc}")

    return counts


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Import a deposit directory tree into Aviary via its API.")
    parser.add_argument(
        "input_dir",
        help="Path to the top-level directory (a single deposit directory, or "
             "a parent containing several deposit directories).")
    parser.add_argument(
        "--base-url", default=os.environ.get("AVIARY_BASE_URL"),
        help="Optional override for the API base URL. Normally NOT needed: the "
             "base URL is derived automatically from the aviaryOrg value in "
             "project.prop. Use this only to force a specific custom domain "
             "(or set AVIARY_BASE_URL).")
    parser.add_argument(
        "--token", default=os.environ.get("AVIARY_TOKEN"),
        help="Aviary API key/token (or set AVIARY_TOKEN).")
    parser.add_argument(
        "--collection-title", default=None,
        help="Override the collection title (default: 'MPS Upload <today>').")
    parser.add_argument(
        "--collection-description", default="",
        help="Optional description for the new collection.")
    parser.add_argument(
        "--wait", type=float, default=WAIT_SECONDS,
        help=f"Seconds to wait between API calls (default {WAIT_SECONDS}).")
    parser.add_argument(
        "--media-ready-timeout", type=float, default=MEDIA_READY_TIMEOUT,
        help="Max seconds to wait for a media file to finish processing before "
             f"creating its index (default {MEDIA_READY_TIMEOUT}).")
    parser.add_argument(
        "--media-ready-interval", type=float, default=MEDIA_READY_INTERVAL,
        help="Seconds between media-processing status checks "
             f"(default {MEDIA_READY_INTERVAL}).")
    parser.add_argument(
        "--marc-import-timeout", type=float, default=MARC_IMPORT_TIMEOUT,
        help="Max seconds to wait for a MARC XML import job to finish "
             f"(default {MARC_IMPORT_TIMEOUT}).")
    parser.add_argument(
        "--marc-import-interval", type=float, default=MARC_IMPORT_INTERVAL,
        help="Seconds between MARC import-status checks "
             f"(default {MARC_IMPORT_INTERVAL}).")
    parser.add_argument(
        "--resource-appear-timeout", type=float, default=RESOURCE_APPEAR_TIMEOUT,
        help="Max seconds to wait for a MARC-imported resource to appear in "
             f"the collection (default {RESOURCE_APPEAR_TIMEOUT}).")
    parser.add_argument(
        "--retry-attempts", type=int, default=RETRY_ATTEMPTS,
        help="Total attempts for each media upload / index create "
             f"(default {RETRY_ATTEMPTS}).")
    parser.add_argument(
        "--retry-backoff", type=float, default=RETRY_BACKOFF,
        help="Base seconds for exponential backoff between retries "
             f"(default {RETRY_BACKOFF}).")
    parser.add_argument(
        "--importMarc", dest="import_marc", action="store_true",
        help="Build each resource from its Harvard HOLLIS MARC XML record when "
             "project.prop has a usable alephID (falling back to the metadata "
             "mapping on failure). Without this flag, resources are always "
             "built from the project.prop metadata mapping.")
    parser.add_argument(
        "--mint-urns", dest="mint_urns", action="store_true",
        help="After creating each resource, mint a persistent NRS URN that "
             "resolves to its Aviary URL (recorded in the log's URN column). "
             "Requires the urn-minter library and NRS credentials in the "
             "environment/.env (NRS_ENDPOINT, NRS_AGENT, NRS_APIGEE_API_KEY).")
    parser.add_argument(
        "--urn-authority", default=os.environ.get(
            "DEFAULT_AUTHORITY_PATH", URN_AUTHORITY_PATH),
        help="NRS authority path under which URNs are minted with --mint-urns "
             f"(default {URN_AUTHORITY_PATH}; or set DEFAULT_AUTHORITY_PATH).")
    parser.add_argument(
        "--log-file", default="mps_aviary_import_log.csv",
        help="CSV log file; one row is appended per resource directory "
             "(default mps_aviary_import_log.csv in the current directory).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan and print the planned API calls without contacting Aviary.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    input_dir = os.path.abspath(args.input_dir)
    if not os.path.isdir(input_dir):
        sys.exit(f"ERROR: not a directory: {input_dir}")

    if not args.dry_run and not args.token:
        sys.exit("ERROR: --token (or AVIARY_TOKEN) is required.")

    if args.mint_urns and not _URN_MINTER_AVAILABLE:
        sys.exit("ERROR: --mint-urns requires the 'urn-minter' library "
                 "(install urn-minter>=1.0.1 from the HUIT Artifactory "
                 "lts-python index).")

    resource_dirs = find_resource_directories(input_dir)
    if not resource_dirs:
        sys.exit(f"ERROR: no directories containing project.prop found under "
                 f"{input_dir}")

    # Organization ID comes from a project.prop (org is the same for the batch).
    first_props = parse_prop_file(os.path.join(resource_dirs[0], "project.prop"))
    organization_id = first_props.get("aviaryOrg", "").strip()
    if not organization_id:
        sys.exit("ERROR: 'aviaryOrg' not found in project.prop.")

    today = datetime.date.today().strftime(DATE_FORMAT)
    collection_title = args.collection_title or f"MPS Upload {today}"

    # Base URL is derived from aviaryOrg automatically (no separate input).
    base_url = resolve_base_url(organization_id, override=args.base_url)

    print(f"Input directory     : {input_dir}")
    print(f"Resource directories: {len(resource_dirs)}")
    print(f"Organization ID     : {organization_id}")
    print(f"API base URL        : {base_url}")
    print(f"Collection title    : {collection_title}")
    print(f"Mode                : {'DRY RUN' if args.dry_run else 'LIVE'}")

    client = AviaryClient(
        base_url=base_url,
        token=args.token or "",
        organization_id=organization_id,
        wait=args.wait,
        dry_run=args.dry_run,
        retry_attempts=args.retry_attempts,
        retry_backoff=args.retry_backoff,
    )

    print("\n--- Resolving collection ---")
    collection_id = client.find_collection_by_title(collection_title)
    if collection_id is not None:
        print(f"    Found existing collection titled {collection_title!r}; "
              f"reusing it (id {collection_id}).")
    else:
        print(f"    No existing collection titled {collection_title!r}; "
              f"creating a new one.")
        collection_id = client.create_collection(
            title=collection_title,
            description=args.collection_description,
        )
        print(f"    -> collection id: {collection_id}")

    log_fields = ["date", "title", "aviaryOrg", "ownercode", "depositnumber",
                  "shelfnum", "Success", "#media", "#indexes", "URL", "URN",
                  "emailSuccess"]
    log_path = os.path.abspath(args.log_file)
    write_header = not os.path.exists(log_path) or os.path.getsize(log_path) == 0

    work_dir = tempfile.mkdtemp(prefix="aviary_marc_")
    totals = {"resource": 0, "media": 0, "indexes": 0}
    try:
        with open(log_path, "a", newline="", encoding="utf-8") as log_fh:
            log_writer = csv.DictWriter(log_fh, fieldnames=log_fields)
            if write_header:
                log_writer.writeheader()
            for resource_dir in resource_dirs:
                try:
                    counts = process_resource_directory(
                        client, collection_id, resource_dir, args, work_dir)
                    for k in totals:
                        totals[k] += counts.get(k, 0)
                    log_row = counts.get("log_row")
                    if log_row is not None:
                        log_row["#media"] = counts.get("media", 0)
                        log_row["#indexes"] = counts.get("indexes", 0)
                        # Success only if the resource was created AND at least
                        # one media file was imported successfully.
                        log_row["Success"] = (
                            "Success" if counts.get("resource") and
                            counts.get("media", 0) > 0 else "Failure")
                        log_writer.writerow(log_row)
                        log_fh.flush()
                except Exception as exc:  # keep going through the rest of the batch
                    print(f"    ERROR processing {resource_dir}: {exc}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"\n    Log written to: {log_path}")

    print("\n=== Summary ===")
    print(f"    Collection : {collection_title} (id {collection_id})")
    print(f"    Resources  : {totals['resource']}")
    print(f"    Media files: {totals['media']}")
    print(f"    Indexes    : {totals['indexes']}")


if __name__ == "__main__":
    main()
