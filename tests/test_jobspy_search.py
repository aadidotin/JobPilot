"""JobSpy adapter tests. scrape_jobs is patched at its source module, so these
run offline; the row shapes below are copied from real live responses
(Indeed's "KA, IN" locations and NaN-filled salary columns included).
"""

import pandas as pd
import pytest

from jobpilot.adapters import jobspy_search
from jobpilot.adapters.jobspy_search import SweepSpec, poll

NAN = float("nan")


def row(**over):
    base = {
        "id": "in-abc123",
        "site": "indeed",
        "job_url": "https://in.indeed.com/viewjob?jk=abc123",
        "title": "Python Developer",
        "company": "Acme",
        "location": "KA, IN",
        "date_posted": "2026-07-20",
        "min_amount": NAN,
        "max_amount": NAN,
        "currency": NAN,
        "is_remote": False,
        "description": "Build things.",
    }
    return {**base, **over}


@pytest.fixture
def spec():
    return SweepSpec(market="india", sites=["indeed"], location="India", search_terms=["Python Developer"])


def patch_scrape(monkeypatch, handler):
    monkeypatch.setattr("jobspy.scrape_jobs", handler, raising=False)


def test_normalizes_a_live_shaped_row(monkeypatch, spec):
    patch_scrape(monkeypatch, lambda **kw: pd.DataFrame([row()]))
    (result,) = poll(spec)
    assert result.success and len(result.jobs) == 1
    job = result.jobs[0]
    assert job.source == "indeed" and job.external_id == "in-abc123"
    assert job.company == "Acme" and job.title == "Python Developer"
    assert job.location == "KA, India"
    assert job.salary_min is None and job.salary_currency is None
    assert job.posted_at is None  # always 'observed' provenance


def test_country_abbreviation_expanded(monkeypatch, spec):
    patch_scrape(monkeypatch, lambda **kw: pd.DataFrame([row(location="Pune, MH, IN")]))
    (result,) = poll(spec)
    assert result.jobs[0].location == "Pune, MH, India"


def test_remote_flag_tags_location(monkeypatch, spec):
    patch_scrape(monkeypatch, lambda **kw: pd.DataFrame([row(is_remote=True, location="KA, IN")]))
    (result,) = poll(spec)
    assert result.jobs[0].location == "Remote (KA, India)"


def test_salary_parsed_when_present(monkeypatch, spec):
    patch_scrape(monkeypatch, lambda **kw: pd.DataFrame([row(min_amount=1200000.0, max_amount=1800000.0, currency="INR")]))
    (result,) = poll(spec)
    job = result.jobs[0]
    assert (job.salary_min, job.salary_max, job.salary_currency) == (1200000, 1800000, "INR")


def test_missing_description_marks_partial(monkeypatch, spec):
    patch_scrape(monkeypatch, lambda **kw: pd.DataFrame([row(description=NAN)]))
    (result,) = poll(spec)
    assert result.jobs[0].description is None
    assert result.jobs[0].description_partial is True


def test_rows_without_company_are_dropped(monkeypatch, spec):
    patch_scrape(monkeypatch, lambda **kw: pd.DataFrame([row(company=NAN), row(id="in-ok")]))
    (result,) = poll(spec)
    assert [j.external_id for j in result.jobs] == ["in-ok"]


def test_same_posting_from_two_terms_appears_once(monkeypatch, spec):
    spec.search_terms = ["Python Developer", "Backend Developer"]
    patch_scrape(monkeypatch, lambda **kw: pd.DataFrame([row()]))
    (result,) = poll(spec)
    assert len(result.jobs) == 1


def test_empty_dataframe_is_a_successful_empty_poll(monkeypatch, spec):
    patch_scrape(monkeypatch, lambda **kw: pd.DataFrame())
    (result,) = poll(spec)
    assert result.success and result.jobs == []


def test_one_site_failing_does_not_lose_the_other(monkeypatch, spec):
    spec.sites = ["indeed", "linkedin"]

    def handler(**kw):
        if kw["site_name"] == ["linkedin"]:
            raise RuntimeError("429 rate limited")
        return pd.DataFrame([row()])

    patch_scrape(monkeypatch, handler)
    indeed, linkedin = poll(spec)
    assert indeed.success and len(indeed.jobs) == 1
    assert not linkedin.success and "429" in linkedin.error
    assert linkedin.jobs == []


def test_failed_site_is_not_retried_per_term(monkeypatch, spec):
    spec.search_terms = ["A", "B", "C"]
    calls = []

    def handler(**kw):
        calls.append(kw["search_term"])
        raise RuntimeError("down")

    patch_scrape(monkeypatch, handler)
    (result,) = poll(spec)
    assert not result.success
    assert calls == ["A"]  # gave up after the first failure


def test_sweep_params_reach_scrape_jobs(monkeypatch, spec):
    spec.hours_old = 24
    spec.results_wanted = 40
    captured = {}

    def handler(**kw):
        captured.update(kw)
        return pd.DataFrame()

    patch_scrape(monkeypatch, handler)
    poll(spec)
    assert captured["hours_old"] == 24
    assert captured["results_wanted"] == 40
    assert captured["location"] == "India"
    assert captured["enforce_annual_salary"] is True


def test_poll_result_labels_the_sweep(monkeypatch, spec):
    patch_scrape(monkeypatch, lambda **kw: pd.DataFrame([row()]))
    (result,) = poll(spec)
    assert result.company == "india sweep"


def test_clean_handles_nan_and_blank():
    assert jobspy_search._clean(NAN) is None
    assert jobspy_search._clean("  ") is None
    assert jobspy_search._clean("  x ") == "x"
    assert jobspy_search._int_or_none("not a number") is None
