"""Adapter fixture tests (E12). Fixtures are trimmed real payloads captured
2026-07-19 from live boards; MockTransport serves them so no test hits the
network.
"""

import json
from pathlib import Path

import httpx
import pytest

from jobpilot.adapters import ashby, greenhouse, lever

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def client_returning(payload_by_call):
    """MockTransport client. payload_by_call: list of JSON payloads, one per
    successive request (last one repeats)."""
    calls = {"n": 0, "requests": []}

    def handler(request):
        calls["requests"].append(request)
        i = min(calls["n"], len(payload_by_call) - 1)
        calls["n"] += 1
        return httpx.Response(200, json=payload_by_call[i])

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def failing_client(exc=httpx.ConnectError("boom")):
    def handler(request):
        raise exc

    return httpx.Client(transport=httpx.MockTransport(handler))


# ---- Greenhouse ----

def test_greenhouse_parses_fixture():
    client, _ = client_returning([fixture("greenhouse_board.json")])
    r = greenhouse.poll("Postman", "postman", client)
    assert r.success and len(r.jobs) == 3
    j = r.jobs[0]
    assert j.source == "greenhouse" and j.company == "Postman"
    assert j.external_id and j.title and j.url.startswith("http")


def test_greenhouse_unescapes_content():
    client, _ = client_returning([fixture("greenhouse_board.json")])
    r = greenhouse.poll("Postman", "postman", client)
    for j in r.jobs:
        assert "&lt;" not in (j.description or "") and "&quot;" not in (j.description or "")
    assert "<div" in r.jobs[0].description  # escaped markup became real markup


def test_greenhouse_timestamp_provenance():
    board = fixture("greenhouse_board.json")
    assert board["jobs"][0]["first_published"]  # fixture precondition
    client, _ = client_returning([board])
    r = greenhouse.poll("Postman", "postman", client)
    assert r.jobs[0].posted_at is not None
    # prefer first_published over updated_at
    board["jobs"][0]["updated_at"] = "2001-01-01T00:00:00-00:00"
    client, _ = client_returning([board])
    r = greenhouse.poll("Postman", "postman", client)
    assert r.jobs[0].posted_at.year != 2001


def test_greenhouse_error_payload_with_200_is_failed_poll():
    client, _ = client_returning([{"error": "Board not found"}])
    r = greenhouse.poll("Postman", "postman", client)
    assert not r.success and r.error and r.jobs == []


def test_greenhouse_network_error_is_failed_poll():
    r = greenhouse.poll("Postman", "postman", failing_client())
    assert not r.success and "ConnectError" in r.error


# ---- Lever ----

def test_lever_parses_fixture():
    client, _ = client_returning([fixture("lever_board.json")])
    r = lever.poll("CRED", "cred", client)
    assert r.success and len(r.jobs) == 3
    j = r.jobs[0]
    assert j.source == "lever" and j.posted_at is not None and j.posted_at.year >= 2020


def test_lever_paginates_full_pages():
    template = fixture("lever_board.json")[0]
    full_page = [dict(template, id=f"job-{i}") for i in range(250)]
    short_page = [dict(template, id="job-last")]
    client, calls = client_returning([full_page, short_page])
    r = lever.poll("CRED", "cred", client)
    assert r.success and len(r.jobs) == 251
    assert calls["n"] == 2
    assert "skip=250" in str(calls["requests"][1].url)


def test_lever_short_first_page_makes_one_request():
    client, calls = client_returning([fixture("lever_board.json")])
    r = lever.poll("CRED", "cred", client)
    assert r.success and calls["n"] == 1


def test_lever_failure_mid_pagination_is_failed_poll():
    template = fixture("lever_board.json")[0]
    full_page = [dict(template, id=f"job-{i}") for i in range(250)]
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=full_page)
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = lever.poll("CRED", "cred", client)
    assert not r.success  # incomplete read must never flip statuses


# ---- Ashby ----

def test_ashby_parses_fixture_and_skips_unlisted():
    board = fixture("ashby_board.json")
    assert any(not j["isListed"] for j in board["jobs"])  # fixture precondition
    client, _ = client_returning([board])
    r = ashby.poll("Supabase", "supabase", client)
    assert r.success and len(r.jobs) == 3  # 4 in fixture, 1 unlisted
    assert all(j.external_id != "hidden-1" for j in r.jobs)


def test_ashby_timestamp_and_remote_location():
    client, _ = client_returning([fixture("ashby_board.json")])
    r = ashby.poll("Supabase", "supabase", client)
    j = r.jobs[0]
    assert j.posted_at is not None and j.posted_at.tzinfo is not None
    assert "remote" in (j.location or "").lower()


def test_ashby_error_payload_is_failed_poll():
    client, _ = client_returning([{"errors": ["unknown board"]}])
    r = ashby.poll("Supabase", "supabase", client)
    assert not r.success


# ---- Contract ----

@pytest.mark.parametrize("mod", [greenhouse, lever, ashby])
def test_all_adapters_survive_non_json_response(mod):
    def handler(request):
        return httpx.Response(200, text="<html>Cloudflare 1020</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    r = mod.poll("X", "x", client)
    assert not r.success and r.error
