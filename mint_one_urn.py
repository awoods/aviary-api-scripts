#!/usr/bin/env python3
"""
mint_one_urn.py  --  throwaway example

Mint a single persistent NRS URN that resolves to one Aviary URL, using the
urn-minter library. This is a minimal standalone demo of the same call the
main importer makes in mint_resource_urn(); not part of the import pipeline.

Setup
-----
    pip install urn-minter            # from HUIT Artifactory lts-python index

    # NRS credentials (env or a .env file the library loads via pydantic):
    export NRS_ENDPOINT="https://stage.apis.huit.harvard.edu/lts-nrs-admin-api"
    export NRS_AGENT="aviary-minting"
    export NRS_APIGEE_API_KEY="your_apigee_key"

Usage
-----
    python3 mint_one_urn.py <aviary_url> <resource_id> [--authority HUL.TEST]
    python3 mint_one_urn.py https://x.aviaryplatform.com/r/123 123 --dry-run
"""

import argparse
import sys

from urn_minter import (
    mint_urns, MintItem, CreateResourceDetails, Status, SequenceName,
)


def main():
    parser = argparse.ArgumentParser(description="Mint one NRS URN for an "
                                                 "Aviary URL.")
    parser.add_argument("aviary_url", help="The Aviary resource URL the URN "
                                           "should resolve to.")
    parser.add_argument("resource_id", help="Used as the URN application_id.")
    parser.add_argument("--authority", default="HUL.TEST",
                        help="NRS authority path (default HUL.TEST).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build the payload and log it; do not call NRS.")
    args = parser.parse_args()

    # Explicit-URL form, file-sequenced resource name (same shape the importer
    # uses for Aviary resources). NRS credentials are read from the environment
    # via the library's NRSClientConfig.from_settings() fallback.
    item = MintItem(args.resource_id, CreateResourceDetails(
        authority_path=args.authority,
        status=Status.ACTIVE,
        url=args.aviary_url,
        sequence=SequenceName.FILE,
    ))

    # mint_urns returns one MintResult per item and does NOT raise on API
    # errors (auth/server/transport arrive as result.error), so just inspect it.
    result = mint_urns([item], dry_run=args.dry_run)[0]

    if args.dry_run:
        # dry_run returns an empty-URN MintResult (no urn, no error): the
        # payload was built and logged but NRS was never called.
        print(f"DRY-RUN  {args.resource_id}  {args.aviary_url}  "
              f"(payload built; NRS not called)")
        return 0
    if result.ok:
        print(f"OK   {args.resource_id}  {args.aviary_url}  ->  {result.urn}")
        return 0
    print(f"FAIL {args.resource_id}  {args.aviary_url}  "
          f"({result.error_kind}): {result.error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
