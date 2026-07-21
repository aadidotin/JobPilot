"""Config editing from Telegram.

The load-bearing test here is comment preservation: ~40% of these files are
comments recording things like the LinkedIn 'iceland' crash and the naukri
recaptcha block. A round-trip that silently deletes them would destroy the
reasoning behind the config while looking like it worked.
"""

import shutil
from datetime import datetime
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


# ---- search terms (roles.yaml) ----
#
# Distinct from title_include: these are what the aggregators are ASKED for,
# not a filter on what came back.

def test_search_terms_keep_the_case_you_type(cfg):
    """They are sent verbatim as queries; the file's entries are Title Case."""
    add_term("search", "Golang Developer", config_dir=cfg)
    assert "Golang Developer" in yaml.safe_load(read(cfg, "roles.yaml"))["search_terms"]


def test_search_term_dedupe_is_still_case_insensitive(cfg):
    add_term("search", "Golang Developer", config_dir=cfg)
    assert "already in" in add_term("search", "golang developer", config_dir=cfg)


def test_include_terms_are_still_lowercased(cfg):
    add_term("include", "GoLang", config_dir=cfg)
    assert "golang" in yaml.safe_load(read(cfg, "roles.yaml"))["title_include"]


def test_adding_a_search_term_reports_sweep_cost(cfg):
    out = add_term("search", "Golang Developer", config_dir=cfg)
    assert "scrapes/sweep" in out


def test_search_terms_warn_before_the_budget_silently_drops_them(cfg):
    """Terms are walked in order, so budget exhaustion starves the LAST ones."""
    doc = yaml.safe_load(read(cfg, "roles.yaml"))
    start = len(doc["search_terms"])
    out = ""
    for i in range(config_edit.SEARCH_TERM_SOFT_CAP - start + 1):
        out = add_term("search", f"Term {i}", config_dir=cfg)
    assert "⚠️" in out and "LAST" in out


def test_search_terms_are_hard_capped(cfg):
    for i in range(config_edit.SEARCH_TERM_HARD_CAP):
        add_term("search", f"Term {i}", config_dir=cfg)
    terms = yaml.safe_load(read(cfg, "roles.yaml"))["search_terms"]
    assert len(terms) == config_edit.SEARCH_TERM_HARD_CAP
    assert "❌" in add_term("search", "One More", config_dir=cfg)


def test_cannot_remove_the_last_search_term(cfg):
    """Zero terms = the aggregator tier queries nothing, which looks exactly
    like a quiet job market."""
    doc = yaml.safe_load(read(cfg, "roles.yaml"))
    for term in doc["search_terms"][:-1]:
        remove_term("search", term, config_dir=cfg)
    last = yaml.safe_load(read(cfg, "roles.yaml"))["search_terms"]
    assert len(last) == 1
    assert "❌" in remove_term("search", last[0], config_dir=cfg)
    assert yaml.safe_load(read(cfg, "roles.yaml"))["search_terms"] == last


# ---- blocklist / salary / location (filters.yaml) ----

def test_block_and_unblock(cfg):
    assert "✅" in config_edit.block_company("Infosys", config_dir=cfg)
    assert "Infosys" in yaml.safe_load(read(cfg, "filters.yaml"))["company_blocklist"]
    assert "Infosys" in config_edit.list_blocklist(cfg)
    assert "✅" in config_edit.unblock_company("infosys", config_dir=cfg)
    assert yaml.safe_load(read(cfg, "filters.yaml"))["company_blocklist"] == []


def test_block_dedupes_on_the_normalized_name(cfg):
    """Matching is normalized, so 'Acme Inc.' and 'Acme' are one block."""
    config_edit.block_company("Acme Inc.", config_dir=cfg)
    assert "already blocked" in config_edit.block_company("acme", config_dir=cfg)


def test_blocked_company_actually_filters(cfg):
    from jobpilot.filters import FilterConfig, passes_company

    config_edit.block_company("Infosys", config_dir=cfg)
    loaded = FilterConfig.load(cfg)
    job = type("J", (), {"company": "Infosys Limited", "market": "india"})()
    assert not passes_company(job, loaded)


def test_salary_floor_set_show_and_clear(cfg):
    assert "✅" in config_edit.set_salary_floor("india", "inr", "1500000", config_dir=cfg)
    floor = yaml.safe_load(read(cfg, "filters.yaml"))["salary_floor"]["india"]
    assert floor == {"currency": "INR", "amount": 1500000}
    assert "INR 1,500,000" in config_edit.show_salary(cfg)
    assert "✅" in config_edit.clear_salary_floor("india", config_dir=cfg)
    assert yaml.safe_load(read(cfg, "filters.yaml"))["salary_floor"]["india"] is None


def test_salary_floor_accepts_commas_and_rejects_junk(cfg):
    assert "✅" in config_edit.set_salary_floor("india", "INR", "15,00,000", config_dir=cfg)
    assert "❌" in config_edit.set_salary_floor("india", "RUPEES", "100", config_dir=cfg)
    assert "❌" in config_edit.set_salary_floor("india", "INR", "lots", config_dir=cfg)
    assert "❌" in config_edit.set_salary_floor("india", "INR", "-5", config_dir=cfg)
    assert "❌" in config_edit.set_salary_floor("mars", "INR", "100", config_dir=cfg)


def test_salary_reply_states_the_unstated_salary_rule(cfg):
    """The counterintuitive part: a floor only rejects postings that STATE a
    figure, so raising it cuts volume in a way that is easy to misread."""
    out = config_edit.set_salary_floor("india", "INR", "1500000", config_dir=cfg)
    assert "omit salary still pass" in out


def test_salary_floor_is_honoured_by_the_real_filter(cfg):
    from jobpilot.filters import FilterConfig, passes_salary

    config_edit.set_salary_floor("india", "INR", "1500000", config_dir=cfg)
    loaded = FilterConfig.load(cfg)
    low = type("J", (), {"market": "india", "salary_min": 500000, "salary_max": 900000,
                         "salary_currency": "INR"})()
    unstated = type("J", (), {"market": "india", "salary_min": None, "salary_max": None,
                              "salary_currency": None})()
    assert not passes_salary(low, loaded)
    assert passes_salary(unstated, loaded)  # no stated salary always passes


def test_location_add_remove_and_show(cfg):
    assert "✅" in config_edit.edit_location("india", "include", "add", "kolkata", config_dir=cfg)
    rules = yaml.safe_load(read(cfg, "filters.yaml"))["location"]["india"]
    assert "kolkata" in rules["include_any"]
    assert "kolkata" in config_edit.show_location(cfg)
    assert "✅" in config_edit.edit_location("india", "include", "rm", "kolkata", config_dir=cfg)


def test_emptying_include_any_warns_that_the_gate_turns_off(cfg):
    """An empty include_any does not match nothing — it disables the check."""
    terms = list(yaml.safe_load(read(cfg, "filters.yaml"))["location"]["india"]["include_any"])
    out = ""
    for term in terms:
        out = config_edit.edit_location("india", "include", "rm", term, config_dir=cfg)
    assert "⚠️" in out and "every" in out


# ---- sweeps (roles.yaml) ----

def test_show_sweeps_lists_both_markets(cfg):
    out = config_edit.show_sweeps(cfg)
    assert "india" in out and "remote-intl" in out and "scrapes" in out


def test_sweep_sites_set(cfg):
    assert "✅" in config_edit.set_sweep("india", "sites", "indeed", config_dir=cfg)
    sweeps = yaml.safe_load(read(cfg, "roles.yaml"))["jobspy"]
    assert [s for s in sweeps if s["market"] == "india"][0]["sites"] == ["indeed"]


def test_naukri_is_refused_with_the_reason(cfg):
    """It fails silently — zero rows, no exception — so a warning is not enough."""
    before = read(cfg, "roles.yaml")
    out = config_edit.set_sweep("india", "sites", "indeed,naukri", config_dir=cfg)
    assert "❌" in out and "recaptcha" in out
    assert read(cfg, "roles.yaml") == before


def test_unknown_site_rejected(cfg):
    assert "❌" in config_edit.set_sweep("india", "sites", "monster", config_dir=cfg)


def test_linkedin_location_must_end_in_a_country(cfg):
    """The 'iceland' crash: LinkedIn reads the last token as a country."""
    before = read(cfg, "roles.yaml")
    out = config_edit.set_sweep("remote-intl", "location", "Remote", config_dir=cfg)
    assert "❌" in out and "iceland" in out
    assert read(cfg, "roles.yaml") == before


def test_linkedin_location_accepts_a_real_country(cfg):
    assert "✅" in config_edit.set_sweep("india", "location", "Bengaluru, India", config_dir=cfg)


def test_location_can_be_cleared_to_none(cfg):
    assert "✅" in config_edit.set_sweep("india", "location", "none", config_dir=cfg)
    sweeps = yaml.safe_load(read(cfg, "roles.yaml"))["jobspy"]
    assert [s for s in sweeps if s["market"] == "india"][0]["location"] is None


def test_adding_linkedin_rechecks_the_existing_location(cfg):
    """The crash is a combination, so the guard must fire from either side."""
    config_edit.set_sweep("india", "sites", "indeed", config_dir=cfg)
    config_edit.set_sweep("india", "location", "Anywhere", config_dir=cfg)
    out = config_edit.set_sweep("india", "sites", "indeed,linkedin", config_dir=cfg)
    assert "❌" in out and "iceland" in out


def test_sweep_numeric_bounds_and_flags(cfg):
    assert "✅" in config_edit.set_sweep("india", "hours_old", "24", config_dir=cfg)
    assert "❌" in config_edit.set_sweep("india", "hours_old", "9999", config_dir=cfg)
    assert "❌" in config_edit.set_sweep("india", "results_wanted", "nope", config_dir=cfg)
    assert "✅" in config_edit.set_sweep("india", "is_remote", "true", config_dir=cfg)
    sweeps = yaml.safe_load(read(cfg, "roles.yaml"))["jobspy"]
    assert [s for s in sweeps if s["market"] == "india"][0]["is_remote"] is True


def test_empty_site_list_refused(cfg):
    assert "❌" in config_edit.set_sweep("india", "sites", "  ", config_dir=cfg)


def test_unknown_market_and_key(cfg):
    assert "❌" in config_edit.set_sweep("mars", "sites", "indeed", config_dir=cfg)
    assert "❌" in config_edit.set_sweep("india", "colour", "blue", config_dir=cfg)


def test_sweep_edits_survive_the_real_loader(cfg):
    """The sweep block is consumed by pipeline.run_jobspy via SweepSpec."""
    from jobpilot.adapters.jobspy_search import SweepSpec

    config_edit.set_sweep("india", "hours_old", "48", config_dir=cfg)
    raw = [s for s in yaml.safe_load(read(cfg, "roles.yaml"))["jobspy"]
           if s["market"] == "india"][0]
    spec = SweepSpec(market=raw["market"], sites=raw["sites"], location=raw.get("location"),
                     hours_old=raw.get("hours_old", 72))
    assert spec.hours_old == 48


# ---- aliases (companies.yaml) ----

def test_alias_add_and_remove(cfg):
    assert "✅" in config_edit.edit_alias("CRED", "add", "Kuvera", config_dir=cfg)
    entry = [c for c in yaml.safe_load(read(cfg, "companies.yaml"))["companies"]
             if c["name"] == "CRED"][0]
    assert "Kuvera" in entry["aliases"]
    assert "already has" in config_edit.edit_alias("cred", "add", "kuvera", config_dir=cfg)
    assert "✅" in config_edit.edit_alias("CRED", "rm", "Kuvera", config_dir=cfg)


def test_alias_that_only_adds_a_corporate_suffix_is_refused(cfg):
    """norm_company already strips Inc/Ltd/Pvt, so such an alias is a no-op
    that looks like protection. Four of the seeded ones were exactly this."""
    out = config_edit.edit_alias("GitLab", "add", "GitLab Inc.", config_dir=cfg)
    assert "❌" in out and "would do nothing" in out


def test_alias_on_unknown_company(cfg):
    assert "❌" in config_edit.edit_alias("Nonesuch", "add", "X", config_dir=cfg)


def test_alias_feeds_the_real_dedupe_lookup(cfg):
    from jobpilot.dedupe import build_alias_lookup

    config_edit.edit_alias("Supabase", "add", "Biobase", config_dir=cfg)
    companies = yaml.safe_load(read(cfg, "companies.yaml"))["companies"]
    assert build_alias_lookup(companies)["biobase"] == "supabase"


# ---- hour lists (schedule.yaml) ----

def test_hours_parse_both_separators(cfg):
    assert "✅" in set_value("jobspy_hours", "8,12,18", config_dir=cfg)
    assert yaml.safe_load(read(cfg, "schedule.yaml"))["jobspy_hours"] == [8, 12, 18]
    assert "✅" in set_value("digest_hours", "21", config_dir=cfg)
    assert yaml.safe_load(read(cfg, "schedule.yaml"))["digest_hours"] == [21]


def test_hours_are_sorted_and_deduped(cfg):
    set_value("jobspy_hours", "19,9,9,13", config_dir=cfg)
    assert yaml.safe_load(read(cfg, "schedule.yaml"))["jobspy_hours"] == [9, 13, 19]


def test_bad_hours_rejected_without_writing(cfg):
    before = read(cfg, "schedule.yaml")
    assert "❌" in set_value("jobspy_hours", "9,25", config_dir=cfg)
    assert "❌" in set_value("jobspy_hours", "morning", config_dir=cfg)
    assert "❌" in set_value("jobspy_hours", "", config_dir=cfg)
    assert read(cfg, "schedule.yaml") == before


def test_hours_survive_the_pipeline_window_logic(cfg):
    from jobpilot.pipeline import window_due

    set_value("jobspy_hours", "6", config_dir=cfg)
    schedule = yaml.safe_load(read(cfg, "schedule.yaml"))
    assert schedule["jobspy_hours"] == [6]
    assert window_due("jobspy", schedule["jobspy_hours"],
                      datetime(2026, 7, 21, 7, 0), {}) is not None


def test_show_settings_renders_hour_lists(cfg):
    set_value("jobspy_hours", "9,13,19", config_dir=cfg)
    assert "jobspy_hours = 9,13,19" in show_settings(cfg)


# ---- append placement ----

def test_appended_term_stays_inside_its_own_block(cfg):
    """ruamel hangs the trailing blank line off the last element, so a naive
    append puts the new term under the NEXT key while still parsing as this
    one — a file that disagrees with how it reads."""
    add_term("search", "Golang Developer", config_dir=cfg)
    lines = read(cfg, "roles.yaml").splitlines()
    i = lines.index("  - Golang Developer")
    assert lines[i - 1].strip() == "- Backend Developer"   # last search term
    assert lines[i + 1].strip() == ""                      # gap still closes the block


def test_appending_a_company_does_not_weld_two_entries_together(cfg):
    before = read(cfg, "companies.yaml")
    add_company("Zerodha", "lever", "zerodha", "india", config_dir=cfg)
    after = read(cfg, "companies.yaml")
    assert "    aliases: []\n  - name: Linear" not in after
    assert set(before.splitlines()) - set(after.splitlines()) == set()  # nothing lost


@pytest.mark.parametrize("call", [
    lambda c: add_term("include", "golang", config_dir=c),
    lambda c: add_term("exclude", "wordpress", config_dir=c),
    lambda c: add_term("search", "Golang Developer", config_dir=c),
    lambda c: config_edit.block_company("Infosys", config_dir=c),
    lambda c: config_edit.edit_location("india", "include", "add", "kolkata", config_dir=c),
    lambda c: add_company("Zerodha", "lever", "zerodha", "india", config_dir=c),
    lambda c: config_edit.edit_alias("CRED", "add", "Kuvera", config_dir=c),
])
def test_one_edit_never_removes_an_existing_line(cfg, call):
    """The whole point of round-tripping: an edit adds, it does not rewrite."""
    before = {name: read(cfg, name) for name in
              ["roles.yaml", "companies.yaml", "filters.yaml", "schedule.yaml"]}
    call(cfg)
    for name, text in before.items():
        lost = set(text.splitlines()) - set(read(cfg, name).splitlines())
        # A list line legitimately changes when the list itself gains a member
        # ("aliases: []" expands; a flow list grows an entry). Anything else
        # disappearing means the write rewrote content it should not have.
        now = read(cfg, name).splitlines()
        lost = {ln for ln in lost
                if not any(n.split(":")[0] == ln.split(":")[0] for n in now)}
        assert not lost, f"{name} lost lines: {lost}"
