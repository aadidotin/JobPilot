"""Config editing from Telegram.

The load-bearing test here is comment preservation: ~40% of these files are
comments recording things like the LinkedIn 'iceland' crash and the naukri
recaptcha block. A round-trip that silently deletes them would destroy the
reasoning behind the config while looking like it worked.
"""

import shutil
from pathlib import Path

import pytest
import yaml

from jobpilot import config_edit
from jobpilot.config_edit import (
    add_company,
    add_term,
    list_companies,
    list_terms,
    remove_company,
    remove_term,
    set_value,
    show_settings,
)

REAL_CONFIG = Path("config")


@pytest.fixture
def cfg(tmp_path):
    """A copy of the REAL shipped config, so these tests exercise the actual
    files the pipeline reads rather than a simplified stand-in."""
    dest = tmp_path / "config"
    shutil.copytree(REAL_CONFIG, dest)
    return dest


def read(cfg, name):
    return (cfg / name).read_text()


# ---- the guarantee that matters ----

def test_comments_survive_a_company_add(cfg):
    before = read(cfg, "companies.yaml")
    assert before.count("#") > 20
    add_company("Zerodha", "lever", "zerodha", "india", config_dir=cfg)
    after = read(cfg, "companies.yaml")
    for line in [ln for ln in before.splitlines() if ln.strip().startswith("#")]:
        assert line in after, f"lost comment: {line}"


def test_comments_survive_a_role_edit(cfg):
    before = read(cfg, "roles.yaml")
    add_term("include", "golang", config_dir=cfg)
    after = read(cfg, "roles.yaml")
    assert "Invalid country string" in after  # the LinkedIn 'iceland' note
    for line in [ln for ln in before.splitlines() if ln.strip().startswith("#")]:
        assert line in after, f"lost comment: {line}"


def test_unrelated_keys_are_untouched(cfg):
    before = yaml.safe_load(read(cfg, "roles.yaml"))
    add_term("exclude", "sap basis", config_dir=cfg)
    after = yaml.safe_load(read(cfg, "roles.yaml"))
    assert after["search_terms"] == before["search_terms"]
    assert after["jobspy"] == before["jobspy"]
    assert after["title_include"] == before["title_include"]


def test_every_write_leaves_a_backup(cfg):
    original = read(cfg, "roles.yaml")
    add_term("include", "golang", config_dir=cfg)
    assert (cfg / "roles.yaml.bak").read_text() == original


def test_result_is_still_loadable_by_the_pipeline(cfg):
    from jobpilot.filters import FilterConfig

    add_term("exclude", "golang", config_dir=cfg)
    add_company("Zerodha", "lever", "zerodha", "india", config_dir=cfg)
    set_value("freshness_days", "14", config_dir=cfg)
    loaded = FilterConfig.load(cfg)  # the real consumer
    assert "golang" in loaded.title_exclude
    assert loaded.freshness_days == 14


def test_no_temp_file_is_left_behind(cfg):
    add_term("include", "golang", config_dir=cfg)
    assert list(cfg.glob("*.tmp")) == []


# ---- companies ----

def test_add_then_list_then_remove(cfg):
    before = len(yaml.safe_load(read(cfg, "companies.yaml"))["companies"])
    assert "✅" in add_company("Zerodha", "lever", "zerodha", "india", config_dir=cfg)
    assert "Zerodha" in list_companies(cfg)
    entry = [c for c in yaml.safe_load(read(cfg, "companies.yaml"))["companies"]
             if c["name"] == "Zerodha"][0]
    assert entry == {"name": "Zerodha", "ats": "lever", "slug": "zerodha",
                     "market": "india", "aliases": []}
    assert "✅" in remove_company("zerodha", config_dir=cfg)  # case-insensitive
    assert len(yaml.safe_load(read(cfg, "companies.yaml"))["companies"]) == before


def test_duplicate_company_is_rejected(cfg):
    add_company("Zerodha", "lever", "zerodha", "india", config_dir=cfg)
    assert "already tracked" in add_company("ZERODHA", "lever", "z2", "india", config_dir=cfg)


def test_bad_ats_and_market_rejected_before_writing(cfg):
    before = read(cfg, "companies.yaml")
    assert "❌" in add_company("X", "workday", "x", "india", config_dir=cfg)
    assert "❌" in add_company("X", "lever", "x", "mars", config_dir=cfg)
    assert read(cfg, "companies.yaml") == before  # nothing written


def test_removing_an_unknown_company_is_graceful(cfg):
    before = read(cfg, "companies.yaml")
    assert "❌" in remove_company("Nonesuch", config_dir=cfg)
    assert read(cfg, "companies.yaml") == before


# ---- role terms ----

def test_terms_are_normalised_and_deduped(cfg):
    add_term("exclude", "  GoLang  ", config_dir=cfg)
    doc = yaml.safe_load(read(cfg, "roles.yaml"))
    assert "golang" in doc["title_exclude"]
    assert "already in" in add_term("exclude", "GOLANG", config_dir=cfg)


def test_remove_term_and_unknown_term(cfg):
    assert "✅" in remove_term("exclude", "android", config_dir=cfg)
    assert "android" not in yaml.safe_load(read(cfg, "roles.yaml"))["title_exclude"]
    assert "❌" in remove_term("exclude", "android", config_dir=cfg)


def test_bad_kind_rejected(cfg):
    assert "❌" in add_term("banana", "x", config_dir=cfg)
    assert "❌" in remove_term("banana", "x", config_dir=cfg)


def test_list_terms_shows_both_lists(cfg):
    out = list_terms(cfg)
    assert "title_include" in out and "title_exclude" in out


# ---- scalars ----

def test_set_value_changes_the_right_file(cfg):
    assert "✅" in set_value("digest_max", "40", config_dir=cfg)
    assert yaml.safe_load(read(cfg, "schedule.yaml"))["digest_max"] == 40
    assert "✅" in set_value("freshness_days", "14", config_dir=cfg)
    assert yaml.safe_load(read(cfg, "filters.yaml"))["freshness_days"] == 14


def test_out_of_range_and_non_numeric_rejected(cfg):
    before = read(cfg, "schedule.yaml")
    assert "❌" in set_value("digest_max", "5000", config_dir=cfg)
    assert "❌" in set_value("digest_max", "lots", config_dir=cfg)
    assert "❌" in set_value("digest_max", "0", config_dir=cfg)
    assert read(cfg, "schedule.yaml") == before


def test_unknown_key_lists_the_valid_ones(cfg):
    out = set_value("colour", "blue", config_dir=cfg)
    assert "❌" in out and "freshness_days" in out


def test_show_settings_reads_current_values(cfg):
    set_value("digest_max", "12", config_dir=cfg)
    assert "digest_max = 12" in show_settings(cfg)


# ---- board verification ----

def test_verify_board_rejects_unknown_ats():
    ok, detail = config_edit.verify_board("workday", "acme")
    assert not ok and "unknown ats" in detail


def test_verify_board_rejects_an_empty_board(monkeypatch):
    from jobpilot.adapters import PollResult

    class Stub:
        @staticmethod
        def poll(name, slug, client):
            return PollResult(name, [])

    monkeypatch.setattr("jobpilot.adapters.lever", Stub)
    ok, detail = config_edit.verify_board("lever", "ghost")
    assert not ok and "0 postings" in detail


def test_verify_board_reports_failed_poll(monkeypatch):
    from jobpilot.adapters import PollResult

    class Stub:
        @staticmethod
        def poll(name, slug, client):
            return PollResult(name, [], success=False, error="HTTP 404")

    monkeypatch.setattr("jobpilot.adapters.greenhouse", Stub)
    ok, detail = config_edit.verify_board("greenhouse", "nope")
    assert not ok and "404" in detail


# ---- formatting stability ----

@pytest.mark.parametrize("name", ["roles.yaml", "companies.yaml", "filters.yaml", "schedule.yaml"])
def test_round_trip_is_byte_identical(cfg, name):
    """A write must touch only what changed. Without this ruamel re-indents
    every sequence in the file and a one-word edit lands as a 60-line diff."""
    before = read(cfg, name)
    doc = config_edit.load_doc(cfg / name)
    config_edit.save_doc(cfg / name, doc)
    assert read(cfg, name) == before


def test_a_single_edit_touches_only_that_line(cfg):
    before = read(cfg, "roles.yaml").splitlines()
    add_term("exclude", "wordpress", config_dir=cfg)
    after = read(cfg, "roles.yaml").splitlines()
    added = set(after) - set(before)
    assert added == {"  - wordpress"}
    assert len(after) == len(before) + 1


def test_explicit_nulls_are_preserved(cfg):
    """`location: null` carries a comment saying it is deliberate; a bare
    `location:` would read as an unfilled field."""
    add_term("include", "golang", config_dir=cfg)
    assert "location: null" in read(cfg, "roles.yaml")
    set_value("freshness_days", "9", config_dir=cfg)
    assert "india: null" in read(cfg, "filters.yaml")
