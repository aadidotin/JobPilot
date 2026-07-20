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
TERM_KINDS = {"include": "title_include", "exclude": "title_exclude"}

# key -> (file, type, low, high). Bounds are guardrails, not preferences.
SETTABLE = {
    "freshness_days": ("filters.yaml", int, 1, 90),
    "digest_max": ("schedule.yaml", int, 1, 50),
    "silence_alert_hours": ("schedule.yaml", int, 1, 168),
    "jobspy_budget_seconds": ("schedule.yaml", int, 30, 1800),
    "ats_timeout_seconds": ("schedule.yaml", int, 5, 120),
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

    entries.append({"name": name, "ats": ats, "slug": slug, "market": market,
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
    return (f"🎯 title_include ({len(inc)}):\n  " + ", ".join(inc)
            + f"\n\n🚫 title_exclude ({len(exc)}):\n  " + ", ".join(exc))


def add_term(kind: str, term: str, config_dir: Path = CONFIG_DIR) -> str:
    key = TERM_KINDS.get(kind.lower())
    if key is None:
        return "❌ kind must be 'include' or 'exclude'."
    term = term.strip().lower()
    if not term:
        return "❌ empty term."
    path = config_dir / "roles.yaml"
    doc = load_doc(path)
    terms = doc.setdefault(key, [])
    if term in [t.lower() for t in terms]:
        return f"❌ {term!r} is already in {key}."
    terms.append(term)
    save_doc(path, doc)
    return f"✅ Added {term!r} to {key} ({len(terms)} terms). Applies from the next run."


def remove_term(kind: str, term: str, config_dir: Path = CONFIG_DIR) -> str:
    key = TERM_KINDS.get(kind.lower())
    if key is None:
        return "❌ kind must be 'include' or 'exclude'."
    term = term.strip().lower()
    path = config_dir / "roles.yaml"
    doc = load_doc(path)
    terms = doc.get(key) or []
    for i, existing in enumerate(terms):
        if str(existing).lower() == term:
            terms.pop(i)
            save_doc(path, doc)
            return f"✅ Removed {term!r} from {key} ({len(terms)} left)."
    return f"❌ {term!r} is not in {key}."


# ---- scalars ----

def set_value(key: str, raw: str, config_dir: Path = CONFIG_DIR) -> str:
    spec = SETTABLE.get(key)
    if spec is None:
        return "❌ Settable: " + ", ".join(sorted(SETTABLE))
    filename, caster, low, high = spec
    try:
        value = caster(raw)
    except (TypeError, ValueError):
        return f"❌ {key} must be {caster.__name__}, got {raw!r}"
    if not low <= value <= high:
        return f"❌ {key} must be between {low} and {high}."
    path = config_dir / filename
    doc = load_doc(path)
    old = doc.get(key)
    doc[key] = value
    save_doc(path, doc)
    return f"✅ {key}: {old} → {value} (in {filename}). Applies from the next run."


def show_settings(config_dir: Path = CONFIG_DIR) -> str:
    lines = ["⚙️ Settings:"]
    for key, (filename, _, low, high) in sorted(SETTABLE.items()):
        value = load_doc(config_dir / filename).get(key)
        lines.append(f"  {key} = {value}   ({low}–{high})")
    return "\n".join(lines)
