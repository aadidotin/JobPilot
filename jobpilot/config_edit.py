"""Edit config/*.yaml safely from Telegram.

Deliberately NOT a "send me YAML" command. Three reasons, all of which bite
in practice: a typo in a phone keyboard silently breaks the next pipeline run;
these files are ~40% comments recording things like the LinkedIn 'iceland'
crash, and a naive rewrite deletes all of it; and a half-written file can be
read by cron mid-write.

So: structured operations, round-trip YAML that preserves comments, validation
before anything touches disk, and an atomic replace. Every write keeps a .bak
of the previous version.
"""

import io
import os
import shutil
from pathlib import Path

import yaml
from ruamel.yaml import YAML

CONFIG_DIR = Path("config")
ATS_CHOICES = ("greenhouse", "lever", "ashby")
MARKET_CHOICES = ("india", "remote-intl")
TERM_KINDS = {
    "include": "title_include",
    "exclude": "title_exclude",
    "search": "search_terms",
}
# title_include/exclude are lowercased before matching, so case is noise there.
# search_terms are sent verbatim to LinkedIn/Indeed as queries, so keep them
# as typed — the file's existing entries are Title Case.
CASE_PRESERVING = {"search_terms"}

# Aggregator runtime is markets x sites x terms, and poll() walks terms in the
# OUTER loop — so when the budget runs out it is the LAST terms in the list
# that silently never run. Warn before that becomes invisible starvation.
SEARCH_TERM_SOFT_CAP = 8
SEARCH_TERM_HARD_CAP = 12

# key -> (file, kind, low, high). Bounds are guardrails, not preferences.
SETTABLE = {
    "freshness_days": ("filters.yaml", "int", 1, 90),
    "digest_max": ("schedule.yaml", "int", 1, 50),
    "silence_alert_hours": ("schedule.yaml", "int", 1, 168),
    "jobspy_budget_seconds": ("schedule.yaml", "int", 30, 1800),
    "ats_timeout_seconds": ("schedule.yaml", "int", 5, 120),
    "jobspy_hours": ("schedule.yaml", "hours", 0, 23),
    "digest_hours": ("schedule.yaml", "hours", 0, 23),
}

SWEEP_SITES = ("indeed", "linkedin", "glassdoor", "zip_recruiter")
# naukri is scraper-blocked (406 recaptcha) and fails SILENTLY — zero rows, no
# exception, indistinguishable from a healthy empty poll — while burning ~90s
# of sweep budget. Refused rather than merely warned about.
BLOCKED_SITES = {
    "naukri": "naukri is recaptcha-blocked (406) and fails silently — zero rows, "
              "no error, ~90s of budget burned per run. Re-add only if JobSpy ships a fix.",
}
# LinkedIn parses the LAST comma-separated token of `location` as a country, so
# "Remote" is read as the country 'iceland' and the scrape crashes. Any location
# handed to a LinkedIn sweep must therefore end in something it can resolve.
LINKEDIN_COUNTRIES = {
    "india", "united states", "usa", "united kingdom", "uk", "canada", "australia",
    "germany", "france", "netherlands", "singapore", "ireland", "spain", "poland",
}


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # never reflow a long line into something unreadable
    # Match the hand-written style of these files. Without it ruamel re-indents
    # every sequence in the file ("  - x" -> "- x"), so a one-word change
    # arrives as a 60-line diff and the real edit is impossible to review.
    y.indent(mapping=2, sequence=4, offset=2)
    # Write "key: null", not "key:". These files use explicit nulls for
    # deliberate absence (the remote-intl sweep's location, the salary floors),
    # and a bare key reads like something was left unfilled by accident.
    y.representer.add_representer(
        type(None), lambda dumper, _: dumper.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return y


def append_item(seq, value) -> None:
    """Append, keeping the blank line that trails a block where it belongs.

    ruamel hangs a trailing blank line off the LAST element of a sequence, so a
    plain .append() lands the new entry AFTER the gap — visually adopting it
    into the next block ("- Golang Developer" appearing under title_include).
    It still parses correctly, which is exactly what makes it worth fixing: the
    file would silently start disagreeing with how it reads.

    Only for scalar lists. In a list of maps (companies.yaml) the blank lines
    separate entries rather than close the block, and moving one welds two
    entries together. Those get a plain append — the new entry lands without a
    leading blank line, which is cosmetic, unlike the scalar case.
    """
    if seq and isinstance(seq[-1], (dict, list)):
        seq.append(value)
        return
    previous_last = len(seq) - 1
    seq.append(value)
    comments = getattr(seq, "ca", None)
    if comments is not None and previous_last in comments.items:
        comments.items[len(seq) - 1] = comments.items.pop(previous_last)


def load_doc(path: Path):
    with open(path) as fh:
        return _yaml().load(fh)


def dump_str(doc) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def save_doc(path: Path, doc) -> None:
    """Validate, back up, then replace atomically.

    The pipeline may read this file at any moment (cron, every 30 min), so a
    partially written file must never be observable: os.replace is atomic
    within a filesystem.
    """
    text = dump_str(doc)
    yaml.safe_load(text)  # must still parse with the loader the pipeline uses
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# ---- companies ----

def list_companies(config_dir: Path = CONFIG_DIR) -> str:
    doc = load_doc(config_dir / "companies.yaml")
    entries = doc.get("companies") or []
    if not entries:
        return "companies.yaml is empty."
    by_market: dict[str, list[str]] = {}
    for c in entries:
        by_market.setdefault(c["market"], []).append(f"{c['name']} ({c['ats']}:{c['slug']})")
    lines = [f"🏢 {len(entries)} companies:"]
    for market in sorted(by_market):
        lines.append(f"\n{market}:")
        lines += [f"  • {n}" for n in sorted(by_market[market])]
    return "\n".join(lines)


def add_company(name: str, ats: str, slug: str, market: str, aliases: list[str] | None = None,
                config_dir: Path = CONFIG_DIR) -> str:
    ats, market = ats.lower(), market.lower()
    if ats not in ATS_CHOICES:
        return f"❌ ats must be one of {', '.join(ATS_CHOICES)} — got {ats!r}"
    if market not in MARKET_CHOICES:
        return f"❌ market must be one of {', '.join(MARKET_CHOICES)} — got {market!r}"

    path = config_dir / "companies.yaml"
    doc = load_doc(path)
    entries = doc.setdefault("companies", [])
    for existing in entries:
        if existing["name"].lower() == name.lower():
            return f"❌ {existing['name']} is already tracked ({existing['ats']}:{existing['slug']})."

    append_item(entries, {"name": name, "ats": ats, "slug": slug, "market": market,
                          "aliases": aliases or []})
    save_doc(path, doc)
    return f"✅ Added {name} ({ats}:{slug}, {market}). Now tracking {len(entries)} companies."


def remove_company(name: str, config_dir: Path = CONFIG_DIR) -> str:
    path = config_dir / "companies.yaml"
    doc = load_doc(path)
    entries = doc.get("companies") or []
    for i, existing in enumerate(entries):
        if existing["name"].lower() == name.lower():
            entries.pop(i)
            save_doc(path, doc)
            return f"✅ Removed {existing['name']}. Now tracking {len(entries)}."
    return f"❌ No company named {name!r}. /company list to see them."


def verify_board(ats: str, slug: str, name: str = "check") -> tuple[bool, str]:
    """Poll the board before trusting a slug — a typo would otherwise sit in
    config failing silently every 30 minutes."""
    import httpx

    from jobpilot.adapters import ashby, greenhouse, lever

    adapters = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
    adapter = adapters.get(ats.lower())
    if adapter is None:
        return False, f"unknown ats {ats!r}"
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (JobPilot)"}) as client:
        result = adapter.poll(name, slug, client)
    if not result.success:
        return False, (result.error or "poll failed")[:150]
    if not result.jobs:
        return False, "board is reachable but lists 0 postings"
    return True, f"{len(result.jobs)} postings"


# ---- role terms ----

def list_terms(config_dir: Path = CONFIG_DIR) -> str:
    doc = load_doc(config_dir / "roles.yaml")
    inc, exc = doc.get("title_include") or [], doc.get("title_exclude") or []
    search = doc.get("search_terms") or []
    return (f"🎯 title_include ({len(inc)}):\n  " + ", ".join(inc)
            + f"\n\n🚫 title_exclude ({len(exc)}):\n  " + ", ".join(exc)
            + f"\n\n🔍 search_terms ({len(search)}) — aggregator queries:\n  "
            + ", ".join(search))


def _kind_error() -> str:
    return "❌ kind must be 'include', 'exclude' or 'search'."


def add_term(kind: str, term: str, config_dir: Path = CONFIG_DIR) -> str:
    key = TERM_KINDS.get(kind.lower())
    if key is None:
        return _kind_error()
    term = term.strip()
    if not term:
        return "❌ empty term."
    if key not in CASE_PRESERVING:
        term = term.lower()
    path = config_dir / "roles.yaml"
    doc = load_doc(path)
    terms = doc.setdefault(key, [])
    if term.lower() in [str(t).lower() for t in terms]:
        return f"❌ {term!r} is already in {key}."
    if key == "search_terms" and len(terms) >= SEARCH_TERM_HARD_CAP:
        return (f"❌ {SEARCH_TERM_HARD_CAP} search terms is the cap. Each term is a "
                f"separate sweep per site, and terms past the budget are dropped "
                f"silently. Remove one first: /role search rm <term>")
    append_item(terms, term)
    save_doc(path, doc)
    msg = f"✅ Added {term!r} to {key} ({len(terms)} terms). Applies from the next run."
    if key == "search_terms":
        msg += "\n" + _sweep_cost_note(len(terms), config_dir)
    return msg


def _sweep_cost_note(term_count: int, config_dir: Path = CONFIG_DIR) -> str:
    """Search terms are not free, and the way they fail is invisible."""
    doc = load_doc(config_dir / "roles.yaml")
    sweeps = doc.get("jobspy") or []
    scrapes = sum(len(s.get("sites") or []) for s in sweeps) * term_count
    note = f"   ~{scrapes} scrapes/sweep across {len(sweeps)} markets."
    if term_count >= SEARCH_TERM_SOFT_CAP:
        note += ("\n   ⚠️ At this many terms the sweep can hit jobspy_budget_seconds. "
                 "Terms are walked in order, so it is the LAST ones that get dropped — "
                 "silently. Raise the budget with /set jobspy_budget_seconds, or trim.")
    return note


def remove_term(kind: str, term: str, config_dir: Path = CONFIG_DIR) -> str:
    key = TERM_KINDS.get(kind.lower())
    if key is None:
        return _kind_error()
    term = term.strip().lower()
    path = config_dir / "roles.yaml"
    doc = load_doc(path)
    terms = doc.get(key) or []
    for i, existing in enumerate(terms):
        if str(existing).lower() == term:
            removed = terms.pop(i)
            if key == "search_terms" and not terms:
                terms.append(removed)
                return ("❌ That is the last search term. With none, the aggregator "
                        "sweeps query nothing and the tier goes quiet — which looks "
                        "identical to 'no new jobs'. Add a replacement first.")
            save_doc(path, doc)
            return f"✅ Removed {removed!r} from {key} ({len(terms)} left)."
    return f"❌ {term!r} is not in {key}."


# ---- filters.yaml: blocklist / salary floors / location ----

def list_blocklist(config_dir: Path = CONFIG_DIR) -> str:
    entries = load_doc(config_dir / "filters.yaml").get("company_blocklist") or []
    if not entries:
        return "🚫 company_blocklist is empty — no company is suppressed."
    return f"🚫 company_blocklist ({len(entries)}):\n  " + "\n  ".join(f"• {c}" for c in entries)


def block_company(name: str, config_dir: Path = CONFIG_DIR) -> str:
    name = name.strip()
    if not name:
        return "❌ empty company name."
    path = config_dir / "filters.yaml"
    doc = load_doc(path)
    entries = doc.setdefault("company_blocklist", [])
    # Matching is on the normalized form, so "Acme Inc." and "acme" are the
    # same block — say so, or a duplicate-looking entry reads like a bug.
    from jobpilot.dedupe import norm_company

    if norm_company(name) in {norm_company(str(c)) for c in entries}:
        return f"❌ {name!r} is already blocked (matched on its normalized form)."
    append_item(entries, name)
    save_doc(path, doc)
    return (f"✅ Blocked {name!r} ({len(entries)} blocked). Matches on the normalized "
            f"name, so aliases and suffixes are covered. Hides it from future digests; "
            f"rows already sent stay sent.")


def unblock_company(name: str, config_dir: Path = CONFIG_DIR) -> str:
    from jobpilot.dedupe import norm_company

    path = config_dir / "filters.yaml"
    doc = load_doc(path)
    entries = doc.get("company_blocklist") or []
    for i, existing in enumerate(entries):
        if norm_company(str(existing)) == norm_company(name):
            removed = entries.pop(i)
            save_doc(path, doc)
            return f"✅ Unblocked {removed!r} ({len(entries)} still blocked)."
    return f"❌ {name!r} is not blocked. /filter block list to see them."


def show_salary(config_dir: Path = CONFIG_DIR) -> str:
    floors = load_doc(config_dir / "filters.yaml").get("salary_floor") or {}
    lines = ["💰 salary_floor (annual):"]
    for market in MARKET_CHOICES:
        floor = floors.get(market)
        lines.append(f"  {market}: " + (f"{floor['currency']} {floor['amount']:,}"
                                        if floor else "off"))
    lines.append("  Postings with NO stated salary always pass — the floor only")
    lines.append("  rejects postings that state one below it.")
    return "\n".join(lines)


def set_salary_floor(market: str, currency: str, raw_amount: str,
                     config_dir: Path = CONFIG_DIR) -> str:
    market = market.lower()
    if market not in MARKET_CHOICES:
        return f"❌ market must be one of {', '.join(MARKET_CHOICES)} — got {market!r}"
    currency = currency.upper()
    if not (len(currency) == 3 and currency.isalpha()):
        return f"❌ currency must be a 3-letter code (INR, USD) — got {currency!r}"
    try:
        amount = int(raw_amount.replace(",", "").replace("_", ""))
    except (TypeError, ValueError):
        return f"❌ amount must be a whole number — got {raw_amount!r}"
    if amount <= 0:
        return "❌ amount must be positive. To disable: /filter salary <market> off"

    path = config_dir / "filters.yaml"
    doc = load_doc(path)
    floors = doc.setdefault("salary_floor", {})
    floors[market] = {"currency": currency, "amount": amount}
    save_doc(path, doc)
    return (f"✅ {market} salary floor: {currency} {amount:,}/year.\n"
            f"   Only postings that STATE a lower salary are dropped — the many that "
            f"omit salary still pass, unbadged. A floor that is too high therefore "
            f"cuts volume quietly. /filter salary {market} off to disable.")


def clear_salary_floor(market: str, config_dir: Path = CONFIG_DIR) -> str:
    market = market.lower()
    if market not in MARKET_CHOICES:
        return f"❌ market must be one of {', '.join(MARKET_CHOICES)} — got {market!r}"
    path = config_dir / "filters.yaml"
    doc = load_doc(path)
    floors = doc.setdefault("salary_floor", {})
    floors[market] = None
    save_doc(path, doc)
    return f"✅ {market} salary floor disabled — every posting passes the salary gate."


def show_location(config_dir: Path = CONFIG_DIR) -> str:
    rules = load_doc(config_dir / "filters.yaml").get("location") or {}
    lines = ["📍 location rules:"]
    for market in MARKET_CHOICES:
        r = rules.get(market) or {}
        inc, exc = r.get("include_any") or [], r.get("exclude") or []
        lines.append(f"\n{market}:")
        lines.append("  include_any: " + (", ".join(inc) if inc
                                          else "(none — every location passes)"))
        lines.append("  exclude:     " + (", ".join(exc) if exc else "(none)"))
    return "\n".join(lines)


def edit_location(market: str, kind: str, op: str, term: str,
                  config_dir: Path = CONFIG_DIR) -> str:
    market, kind, op = market.lower(), kind.lower(), op.lower()
    if market not in MARKET_CHOICES:
        return f"❌ market must be one of {', '.join(MARKET_CHOICES)} — got {market!r}"
    key = {"include": "include_any", "exclude": "exclude"}.get(kind)
    if key is None:
        return "❌ kind must be 'include' or 'exclude'."
    term = term.strip().lower()
    if not term:
        return "❌ empty term."

    path = config_dir / "filters.yaml"
    doc = load_doc(path)
    rules = doc.setdefault("location", {}).setdefault(market, {})
    terms = rules.setdefault(key, [])
    if op == "add":
        if term in [str(t).lower() for t in terms]:
            return f"❌ {term!r} is already in {market}.{key}."
        append_item(terms, term)
        save_doc(path, doc)
        return f"✅ Added {term!r} to {market} {key} ({len(terms)} terms)."
    if op in ("rm", "remove"):
        for i, existing in enumerate(terms):
            if str(existing).lower() == term:
                terms.pop(i)
                # An empty include_any is not "match nothing" — passes_location
                # skips the check entirely, so the market silently stops being
                # location-filtered. That is a big change to make by subtraction.
                warn = ""
                if key == "include_any" and not terms:
                    warn = ("\n   ⚠️ That was the last include term. An empty include_any "
                            "disables the location gate for this market entirely — every "
                            "location now passes.")
                save_doc(path, doc)
                return f"✅ Removed {term!r} from {market} {key} ({len(terms)} left)." + warn
        return f"❌ {term!r} is not in {market}.{key}."
    return "❌ op must be 'add' or 'rm'."


# ---- roles.yaml: the jobspy sweep block ----
#
# The riskiest surface here. Two of these fields have known configurations that
# fail in ways you cannot see from Telegram — a silent scraper block and a hard
# crash — so the validators encode those rules rather than trusting the value.

def show_sweeps(config_dir: Path = CONFIG_DIR) -> str:
    sweeps = load_doc(config_dir / "roles.yaml").get("jobspy") or []
    terms = len(load_doc(config_dir / "roles.yaml").get("search_terms") or [])
    lines = ["🛰 aggregator sweeps:"]
    for s in sweeps:
        sites = list(s.get("sites") or [])
        lines.append(f"\n{s.get('market')}:")
        lines.append(f"  sites:          {', '.join(sites)}  ({len(sites) * terms} scrapes)")
        lines.append(f"  location:       {s.get('location') if s.get('location') else 'none'}")
        lines.append(f"  is_remote:      {bool(s.get('is_remote'))}")
        lines.append(f"  results_wanted: {s.get('results_wanted', 25)}")
        lines.append(f"  hours_old:      {s.get('hours_old', 72)}")
    return "\n".join(lines)


def _find_sweep(doc, market: str):
    for s in doc.get("jobspy") or []:
        if str(s.get("market", "")).lower() == market:
            return s
    return None


def _check_linkedin_location(sites, location) -> str | None:
    """LinkedIn reads the last comma-separated token as a country name."""
    if "linkedin" not in [str(s).lower() for s in sites] or not location:
        return None
    tail = str(location).split(",")[-1].strip().lower()
    if tail in LINKEDIN_COUNTRIES:
        return None
    return (f"❌ LinkedIn parses the last part of a location as a COUNTRY, and "
            f"{tail!r} is not one it resolves — this is the config that crashed the "
            f"sweep with \"Invalid country string: 'iceland'\". Use a real country "
            f"(\"Bengaluru, India\") or set location to none and use is_remote true.")


def set_sweep(market: str, key: str, raw: str, config_dir: Path = CONFIG_DIR) -> str:
    market, key = market.lower(), key.lower()
    path = config_dir / "roles.yaml"
    doc = load_doc(path)
    sweep = _find_sweep(doc, market)
    if sweep is None:
        return f"❌ no sweep for market {market!r}. /sweep to see them."

    if key == "sites":
        wanted = [s.strip().lower() for s in raw.replace(",", " ").split() if s.strip()]
        if not wanted:
            return ("❌ at least one site. An empty site list makes the sweep a no-op "
                    "that still reports success.")
        for site in wanted:
            if site in BLOCKED_SITES:
                return f"❌ {BLOCKED_SITES[site]}"
            if site not in SWEEP_SITES:
                return f"❌ unknown site {site!r}. Supported: {', '.join(SWEEP_SITES)}"
        problem = _check_linkedin_location(wanted, sweep.get("location"))
        if problem:
            return problem
        sweep["sites"] = wanted
        save_doc(path, doc)
        terms = len(doc.get("search_terms") or [])
        return (f"✅ {market} sites: {', '.join(wanted)} "
                f"({len(wanted) * terms} scrapes/sweep).")

    if key == "location":
        value = None if raw.strip().lower() in ("none", "null", "off", "-") else raw.strip()
        problem = _check_linkedin_location(sweep.get("sites") or [], value)
        if problem:
            return problem
        sweep["location"] = value
        save_doc(path, doc)
        return f"✅ {market} location: {value if value else 'none (relies on is_remote)'}."

    if key == "is_remote":
        truthy = raw.strip().lower() in ("true", "yes", "on", "1")
        sweep["is_remote"] = truthy
        save_doc(path, doc)
        return f"✅ {market} is_remote: {truthy}."

    bounds = {"results_wanted": (5, 100), "hours_old": (1, 720)}
    if key in bounds:
        low, high = bounds[key]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return f"❌ {key} must be a whole number — got {raw!r}"
        if not low <= value <= high:
            return f"❌ {key} must be between {low} and {high}."
        sweep[key] = value
        save_doc(path, doc)
        extra = ""
        if key == "hours_old":
            extra = ("\n   Aggregator rows are 'observed' provenance, so this query bound "
                     "is what actually keeps them fresh.")
        if key == "results_wanted":
            extra = "\n   Higher means slower — watch jobspy_budget_seconds."
        return f"✅ {market} {key}: {value}.{extra}"

    return ("❌ settable per sweep: sites, location, is_remote, results_wanted, hours_old")


# ---- companies.yaml: aliases ----

def edit_alias(name: str, op: str, alias: str, config_dir: Path = CONFIG_DIR) -> str:
    """Aliases feed cross-source dedupe (E9) — they are how a Greenhouse row and
    a LinkedIn row for the same job get recognized as one."""
    path = config_dir / "companies.yaml"
    doc = load_doc(path)
    for company in doc.get("companies") or []:
        if str(company["name"]).lower() != name.lower():
            continue
        aliases = company.setdefault("aliases", [])
        alias = alias.strip()
        from jobpilot.dedupe import norm_company

        if op == "add":
            if not alias:
                return "❌ empty alias."
            if alias.lower() in [str(a).lower() for a in aliases]:
                return f"❌ {company['name']} already has alias {alias!r}."
            # Dedupe normalizes before matching, and that already strips
            # Inc/Ltd/Pvt/Limited/Corp. An alias that differs only by a suffix
            # is a no-op that looks like protection.
            if norm_company(alias) == norm_company(str(company["name"])):
                return (f"❌ {alias!r} already normalizes to "
                        f"{norm_company(alias)!r}, same as {company['name']} — dedupe "
                        f"strips Inc/Ltd/Pvt/Limited/Corp before matching, so this "
                        f"alias would do nothing. Aliases are for genuinely different "
                        f"names, e.g. CRED → Dreamplug Technologies.")
            # The existing entries are quoted; an unquoted addition beside them
            # renders as ["Dreamplug Technologies", Kuvera], which reads like a
            # different kind of value.
            from ruamel.yaml.scalarstring import DoubleQuotedScalarString

            append_item(aliases, DoubleQuotedScalarString(alias))
            save_doc(path, doc)
            return (f"✅ {company['name']} aliases: {', '.join(aliases)}. "
                    f"Used to merge duplicate postings across sources.")
        if op in ("rm", "remove"):
            for i, existing in enumerate(aliases):
                if str(existing).lower() == alias.lower():
                    aliases.pop(i)
                    save_doc(path, doc)
                    return f"✅ Removed alias {alias!r} from {company['name']}."
            return f"❌ {company['name']} has no alias {alias!r}."
        return "❌ op must be 'add' or 'rm'."
    return f"❌ No company named {name!r}. /company list to see them."


# ---- scalars ----

def _parse_hours(raw: str, low: int, high: int) -> list[int] | str:
    """'9,13,19' or '9 13 19' -> [9, 13, 19]. Returns an error string on bad input."""
    parts = [p for p in raw.replace(",", " ").split() if p]
    if not parts:
        return "❌ give at least one hour, e.g. 9,13,19"
    hours = []
    for part in parts:
        try:
            hour = int(part)
        except ValueError:
            return f"❌ {part!r} is not an hour. Use 24h numbers: 9,13,19"
        if not low <= hour <= high:
            return f"❌ hour {hour} is out of range ({low}–{high})."
        hours.append(hour)
    return sorted(set(hours))


def set_value(key: str, raw: str, config_dir: Path = CONFIG_DIR) -> str:
    spec = SETTABLE.get(key)
    if spec is None:
        return "❌ Settable: " + ", ".join(sorted(SETTABLE))
    filename, kind, low, high = spec

    if kind == "hours":
        value = _parse_hours(raw, low, high)
        if isinstance(value, str):
            return value
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return f"❌ {key} must be a whole number, got {raw!r}"
        if not low <= value <= high:
            return f"❌ {key} must be between {low} and {high}."

    path = config_dir / filename
    doc = load_doc(path)
    old = doc.get(key)
    if kind == "hours":
        # Assign in place so ruamel keeps the original flow style ([9, 13, 19]).
        existing = doc.get(key)
        if existing is not None:
            existing.clear()
            existing.extend(value)
        else:
            doc[key] = value
    else:
        doc[key] = value
    save_doc(path, doc)
    note = ""
    if key == "jobspy_hours":
        note = f"\n   {len(value)} aggregator sweeps/day (local time)."
    if key == "digest_hours":
        note = "\n   Overflow past digest_max waits for the next one — or /more."
    return f"✅ {key}: {list(old) if kind == 'hours' else old} → {value} (in {filename})." + note


def show_settings(config_dir: Path = CONFIG_DIR) -> str:
    lines = ["⚙️ Settings:"]
    for key, (filename, kind, low, high) in sorted(SETTABLE.items()):
        value = load_doc(config_dir / filename).get(key)
        shown = ",".join(str(v) for v in value) if kind == "hours" else value
        lines.append(f"  {key} = {shown}   ({low}–{high})")
    lines.append("\nAlso editable: /company /role /filter /sweep")
    return "\n".join(lines)
