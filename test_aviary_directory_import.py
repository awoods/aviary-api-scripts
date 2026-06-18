"""Tests for the pure logic in aviary_directory_import.

These cover the trickiest no-network functions: alephID -> HOLLIS id
normalization, base-URL resolution, media display-name trimming, and the
media-readiness duration check (which must tolerate a numeric ``duration``).

The script imports ``requests`` at module top, so we stub it in sys.modules
before importing — the functions under test never touch the network.
"""

import sys
import types

# Provide a dummy ``requests`` so importing the script does not require the
# real dependency; none of the functions tested here make HTTP calls.
sys.modules.setdefault("requests", types.ModuleType("requests"))

import aviary_directory_import as adi  # noqa: E402


# --- alephID -> HOLLIS id normalization ----------------------------------- #

def test_normalize_aleph_id_expands_9_digits():
    # 9 digits get "99" prepended and "0203941" appended.
    assert adi.normalize_aleph_id("014511460") == "990145114600203941"


def test_normalize_aleph_id_keeps_17_and_18_digits():
    assert adi.normalize_aleph_id("99014511460020394") == "99014511460020394"
    assert adi.normalize_aleph_id("990145114600203941") == "990145114600203941"


def test_normalize_aleph_id_strips_non_digits_before_counting():
    # Embedded punctuation is removed, leaving 9 digits to expand.
    assert adi.normalize_aleph_id("014-511-460") == "990145114600203941"


def test_normalize_aleph_id_rejects_null_and_bad_lengths():
    assert adi.normalize_aleph_id("NULL") is None
    assert adi.normalize_aleph_id("") is None
    assert adi.normalize_aleph_id(None) is None
    assert adi.normalize_aleph_id("12345") is None  # wrong length


# --- base-URL resolution -------------------------------------------------- #

def test_resolve_base_url_numeric_org_uses_central_host():
    assert adi.resolve_base_url("719") == adi.AVIARY_PLATFORM_HOST


def test_resolve_base_url_hostname_and_subdomain():
    assert adi.resolve_base_url("hfa.av.lib.harvard.edu") == \
        "https://hfa.av.lib.harvard.edu/"
    assert adi.resolve_base_url("myorg") == "https://myorg.aviaryplatform.com/"


def test_resolve_base_url_override_wins_and_normalizes_slash():
    assert adi.resolve_base_url("719", override="https://x.example.com") == \
        "https://x.example.com/"


# --- media display-name trimming ------------------------------------------ #

def test_media_display_name_trims_at_underscore_before_brace():
    name = "T-529_0006_DM_01_01_{7B18028D-91CF-1234}.mp3"
    assert adi.media_display_name(name) == "T-529_0006_DM_01_01"


def test_media_display_name_without_brace_uses_stem():
    assert adi.media_display_name("/a/b/plain_name.mp4") == "plain_name"


# --- media readiness: the #4 duration-coercion fix ------------------------ #

def test_media_is_ready_true_when_processing_false():
    assert adi.AviaryClient._media_is_ready({"processing": False}) is True


def test_media_is_ready_false_while_processing():
    assert adi.AviaryClient._media_is_ready({"processing": True}) is False


def test_media_is_ready_tolerates_numeric_duration():
    # Regression guard for #4: a numeric duration must not raise AttributeError.
    assert adi.AviaryClient._media_is_ready({"duration": 123}) is True
    assert adi.AviaryClient._media_is_ready({"duration": 0}) is False


# --- is_null_value -------------------------------------------------------- #

def test_is_null_value_treats_blank_and_null_token_as_empty():
    assert adi.is_null_value(None) is True
    assert adi.is_null_value("") is True
    assert adi.is_null_value("   ") is True
    assert adi.is_null_value("NULL") is True
    assert adi.is_null_value("null") is True  # case-insensitive


def test_is_null_value_keeps_real_values():
    assert adi.is_null_value("719") is False
    assert adi.is_null_value("a title") is False


# --- map_access_status ---------------------------------------------------- #

def test_map_access_status_known_codes_case_insensitive():
    assert adi.map_access_status("PublicS") == "public"
    assert adi.map_access_status("P") == "public"
    assert adi.map_access_status("r") == "private"
    assert adi.map_access_status("N") == "internal"


def test_map_access_status_unknown_and_blank_fall_back_to_default():
    assert adi.map_access_status("zzz") == adi.ACCESS_DEFAULT
    assert adi.map_access_status("") == adi.ACCESS_DEFAULT
    assert adi.map_access_status(None) == adi.ACCESS_DEFAULT


# --- parse_prop_file ------------------------------------------------------ #

def test_parse_prop_file_handles_quotes_bare_and_empty(tmp_path):
    prop = tmp_path / "project.prop"
    # Includes a BOM, quoted/bare/empty values, and a junk line with no '='.
    prop.write_text(
        '﻿'
        'title="A Quoted Title"\n'
        'aviaryOrg=719\n'
        'metsLabel=\n'
        "shelfnum='HUC 1234'\n"
        "junk line without equals\n",
        encoding="utf-8",
    )
    props = adi.parse_prop_file(str(prop))
    assert props["title"] == "A Quoted Title"   # surrounding quotes stripped
    assert props["aviaryOrg"] == "719"          # bare value kept
    assert props["metsLabel"] == ""             # empty value kept as ""
    assert props["shelfnum"] == "HUC 1234"      # single quotes stripped
    assert "junk line without equals" not in props
