# JobPilot — Detailed Build Plan

> **⚠️ Superseded in part (2026-07-19).** The plan of record is the approved design doc
> (`~/.gstack/projects/JobPilot/atreus-unknown-design-20260719-180727.md`) with its
> CEO (C1–C11) and engineering (E1–E13) amendments. On conflict: C > E > design deltas > this file.
> Known-stale here: §2's diagram (pgvector, IMAP email tier, separate crons, no bot daemon — all
> replaced: SQLite WAL, JobSpy, one chained 30-min pipeline, persistent Telegram daemon), and the
> §7 Stage 3 embedding pre-filter (removed — Haiku scores all filter survivors). §7's other stages
> remain normative for carried-over components. See CLAUDE.md for the hard rules.
### Automated job matching + application-drafting for full-time roles (India + remote international)

**Owner:** solo developer (full-stack, Python/JS)
**Scope change from v1:** freelancing (Upwork etc.) is deferred to a separate future project. This system targets **full-time roles only**, across Indian and international-remote markets.
**Core principle:** auto-match + auto-draft + human-approve. No platform scraping, no ToS violations — every source here is a public API or an email the site sends you voluntarily.

---

## 1. Goals and Non-Goals

**Goals.** Continuously ingest full-time job posts from Indian and remote-international sources; score each against a structured profile and explicit preferences; generate a tailored cover letter **and a tailored resume** for high-scoring matches; present drafts for one-tap approval; assist form-filling on company ATS pages; track every application (sent → viewed → replied → interview → offer → won/lost) so matching and drafting improve over time.

**Non-goals for v1.** No freelance platforms, no LinkedIn Easy Apply automation (account risk), no CAPTCHA solving, no multi-user support, no web dashboard (Telegram is the UI).

**Success metric.** Interview-call rate per application sent, not applications sent.

---

## 2. System Architecture

```
                        ┌─────────────────────────────────────┐
                        │            SCHEDULER (cron)          │
                        │  ingest: */30min   score: hourly     │
                        └──────┬──────────────────┬───────────┘
                               ▼                  ▼
┌───────────────┐   ┌───────────────────┐   ┌──────────────────┐
│  SOURCES       │──▶│  INGESTION LAYER  │──▶│  FILTER ENGINE   │
│ ATS APIs:      │   │  adapters/*.py    │   │  SQL + rules     │
│  Greenhouse    │   │  normalize →      │   │  salary floor,   │
│  Lever         │   │  Job model        │   │  blocklist, geo, │
│  Ashby         │   │  market tag       │   │  visa/remote     │
│ Email (IMAP):  │   │  dedupe on        │   └───────┬──────────┘
│  LinkedIn      │   │  (source, ext_id) │           ▼
│  Naukri        │   └───────────────────┘   ┌──────────────────┐
│  Indeed        │                           │  MATCH ENGINE    │
│ HN Who's Hiring│                           │  1. embed job    │
│ Remotive       │                           │  2. cosine vs    │
│ RemoteOK       │                           │     ideal-job    │
│ Wellfound      │                           │  3. LLM score    │
└───────────────┘                            │     top-K        │
        ┌───────────────────────────────────┤                  │
        ▼                                   └───────┬──────────┘
┌───────────────┐    ┌───────────────────┐          ▼
│  POSTGRES      │◀──▶│  DRAFT ENGINE     │◀──┌──────────────────┐
│  + pgvector    │    │  cover letter +   │   │  score > 70      │
│  (all state)   │    │  tailored resume  │   └──────────────────┘
└──────┬────────┘    └─────────┬─────────┘
       │                       ▼
       │             ┌───────────────────┐    ┌──────────────────┐
       │             │  TELEGRAM BOT     │───▶│  HUMAN APPROVES  │
       │             │  card + buttons:  │    │  ✅ send ✏️ edit  │
       │             │  approve/edit/skip│    │  ❌ skip          │
       │             └─────────┬─────────┘    └──────────────────┘
       │                       ▼
       │             ┌───────────────────┐
       └────────────▶│  SUBMIT LAYER     │
                     │  v1: deep-link +  │
                     │  files ready      │
                     │  v1.5: Playwright │
                     │  ATS form-assist  │
                     │  (human clicks    │
                     │   Submit)         │
                     └───────────────────┘
```

**Design decisions:** monolith + cron (Postgres status columns ARE the queue); Postgres + pgvector as the single store; Telegram as the entire UI; adapters as plugins behind one interface; LLM calls only after cheap filters; every source is either a public JSON API or an email alert parsed over IMAP — nothing here can get an account banned.

---

## 3. Sources (the biggest v2 change)

**Tier 1 — ATS public APIs (best data, zero risk).** Greenhouse, Lever, and Ashby expose company job boards as open JSON endpoints, e.g. `https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true`, `https://api.lever.co/v0/postings/{company}?mode=json`, `https://api.ashbyhq.com/posting-api/job-board/{company}`. Full descriptions, no auth, no scraping. The work is curating `config/companies.yaml`: 100–200 companies in your niche (AI product startups, dev-tools, SaaS with India presence or remote-friendly policies), each tagged with its ATS and slug. Growing this list weekly is the single highest-leverage manual habit in the whole system.

**Tier 2 — Email alerts via IMAP.** A dedicated Gmail address subscribed to LinkedIn, Naukri, and Indeed job alerts (3–5 saved searches each). One LLM parse per alert email → job records. Caveat: these alerts truncate descriptions, so the adapter fetches the linked posting for full text where the link is public, and otherwise scores on partial text with a confidence penalty. Naukri has no public API — email alerts are the only clean channel, and they matter because Naukri dominates India-based listings.

**Tier 3 — Aggregator APIs/feeds.** HN Who's Hiring (Algolia API, monthly), Remotive and RemoteOK (clean JSON), Wellfound alert emails. These cover the remote-international side.

**Dual-market handling.** Every job gets a `market` tag at ingest ('india' | 'remote_intl'), derived from source + location text. Preferences carry per-market salary floors (INR for India, USD for international), and the weekly digest groups by market so you can see whether one side is starving.

---

## 4. Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Library coverage for IMAP, LLM SDKs, docx/pdf generation |
| DB | Postgres 16 + pgvector | Relational + vector in one store |
| ORM | SQLAlchemy 2.0 + Alembic | Migrations |
| Scheduler | cron (systemd timers) | No Airflow for a solo project |
| HTTP | httpx | ATS API polling |
| Email | imap-tools | Alert parsing |
| Embeddings | text-embedding-3-small (1536-dim) | ~$0.02/1M tokens |
| Scoring LLM | Haiku-class | Cheap structured scoring |
| Drafting LLM | Sonnet-class | Cover letters + resume tailoring are customer-facing |
| Resume output | python-docx → PDF (libreoffice headless) | ATS parsers prefer clean docx/PDF |
| Bot | python-telegram-bot v21 | Inline keyboards |
| Browser (v1.5) | Playwright, headed, persistent profile | ATS form-assist; human clicks Submit |
| Config | pydantic-settings + YAML | preferences.yaml, companies.yaml |
| Deploy | Docker Compose on ₹400–1,000/mo VPS | Hetzner/DO |
| Observability | structlog + Telegram error channel | You are the pager |

---

## 5. Repository Layout

```
jobpilot/
├── pyproject.toml
├── docker-compose.yml
├── config/
│   ├── preferences.yaml        # per-market salary floors, blocklists, geo
│   ├── companies.yaml          # curated ATS company list (the moat)
│   └── profile.yaml            # structured skills/domains
├── migrations/
├── jobpilot/
│   ├── models.py
│   ├── settings.py
│   ├── ingest/
│   │   ├── base.py             # SourceAdapter ABC
│   │   ├── greenhouse.py
│   │   ├── lever.py
│   │   ├── ashby.py
│   │   ├── hn_hiring.py
│   │   ├── remotive.py
│   │   ├── remoteok.py
│   │   └── email_alerts.py     # LinkedIn/Naukri/Indeed/Wellfound mails
│   ├── filters/rules.py
│   ├── matching/
│   │   ├── embedder.py
│   │   └── scorer.py
│   ├── drafting/
│   │   ├── coverletter.py
│   │   ├── resume_tailor.py    # reorders/rephrases base resume per job
│   │   └── templates/
│   ├── bot/telegram.py
│   ├── submit/
│   │   ├── deeplink.py         # v1
│   │   └── ats_assist.py       # v1.5: Playwright form-fill
│   └── pipeline.py
├── prompts/                    # versioned: score_v1.txt, coverletter_v1.txt, resume_v1.txt
└── tests/
    ├── test_filters.py
    └── fixtures/jobs/*.json    # 50-job golden set
```

---

## 6. Data Model

Same DDL as v1 with these deltas:

```sql
-- jobs table additions
market        text NOT NULL,             -- 'india' | 'remote_intl'
salary_min    numeric, salary_max numeric,
currency      text,                      -- 'INR'|'USD'|'EUR'
skip_reason   text,                      -- filter-stage rejections live here

-- applications table changes
cover_letter  text NOT NULL,
resume_path   text,                      -- generated tailored resume file
final_cover   text,
edited        boolean DEFAULT false,
-- connects_cost dropped (no Upwork)

-- matches: skipped_by only 'cosine'|'llm'; profile_version retained
```

Salary comparisons in filters normalize to one currency (pick INR) with a hardcoded monthly-refreshed rate in preferences — don't build live FX for this. The `edited` flag remains the gold mine: diffs of your edits against drafts show exactly what the prompts get wrong.

---

## 7. Pipeline Stages

**Stage 0 — Profile bootstrap.** Resume → structured `profile.yaml` via one LLM call, hand-reviewed. Write `ideal_job_text` (150–250 words, in the voice of a job post you wish existed). Consider whether India-market and intl-market ideal posts differ enough to warrant two profile rows — the schema supports it via `profile_version`; start with one, split only if retro data shows the markets need different positioning.

**Stage 1 — Ingest (every 30 min).** ATS adapters iterate companies.yaml (one HTTP call per company; ~200 companies is still trivial load). Email adapter polls IMAP. Dedupe on `(source, external_id)`; `raw` payload immutable. Health-check alert if any tier ingests nothing for 12h.

**Stage 2 — Hard filters.** Per-market salary floors, keyword blocklist/requirelist, location rules (for intl remote: reject "US only"/"EU timezone required" unless overlap works for you; for India: city constraints if any), max age 7 days (full-time posts live longer than gig posts — don't copy the 48h gig rule), visa-sponsorship red-flags for relocated roles. Rejections set `jobs.skip_reason`; weekly funnel report per market.

**Stage 3 — Embedding + cosine.** Post-to-post comparison against `ideal_job_text` (not your resume — different text genres). Threshold ~0.72, tuned weekly.

**Stage 4 — LLM scoring.** Structured output {score, reason, missing_requirements, red_flags}, harsh rubric, prompt version recorded, golden-set gate (≥85% agreement) before any prompt change ships. ≥70 → draft; 40–69 → daily borderline digest.

**Stage 5 — Drafting (the v2 heart).** Two artifacts per match:
*Cover letter* — under 250 words, opens with a specific hook from the posting, maps 2–3 of your strongest relevant results to their stated needs, honestly addresses one gap from `missing_requirements` if significant, no "I am excited to apply" openers.
*Tailored resume* — starts from a hand-written base resume in structured YAML; the tailor reorders bullet points, rewrites the summary line to mirror the role's language, and surfaces matching keywords **only where they're truthful** (ATS keyword screening is real; fabrication is both unethical and interview-suicide). Output docx + PDF. Hard rule in the prompt: never add skills, employers, dates, or metrics absent from the base YAML.

**Stage 6 — Telegram approval.** Card: score, market, company, salary, why/gaps, cover-letter preview, resume-diff summary ("moved geolocation project to top; summary now mentions real-time systems"), buttons approve/edit/skip/open. Skip asks one-tap reason → labeled tuning data.

**Stage 7 — Submission.** v1: deep link + both files delivered in Telegram; you attach and submit (~2 min). v1.5: Playwright opens the ATS form in a headed browser, auto-fills fields and uploads files from a saved answer bank (work authorization, notice period, salary expectation per market); **you review and click Submit**. Greenhouse/Lever/Ashby forms are predictable enough that one filler per ATS covers most of the curated list. LinkedIn Easy Apply stays manual — automating it risks the account.

**Stage 8 — Feedback loop.** Outcome logging via bot commands; monthly retro: interview-rate by score bucket, by market, by source. If one market's replies lag badly, split the profile or rebalance sources before writing more code.

---

## 8. Deployment & Operations

Docker Compose (postgres + app) on a small VPS; systemd timers per stage; nightly pg_dump to object storage; secrets in .env; structlog with ERROR-level mirrored to a private Telegram channel; 12h ingestion-silence alert per source tier (adapter rot is the #1 failure mode — an ATS changes a slug, an alert email changes format, and the system goes quietly blind).

---

## 9. Phased Roadmap

**Phase 1 — Signal check (weekend 1, ~10h).** Schema, Greenhouse + Lever adapters with a starter companies.yaml of ~50 companies, hard filters, plain Telegram digest. *Exit criterion: ≥8 appealing roles/week across both markets. If not, the fix is companies.yaml and saved searches, not more code.*

**Phase 2 — Intelligence (weekend 2, ~10h).** Ashby adapter, embeddings + cosine, LLM scorer, golden set, scored digest.

**Phase 3 — Drafting + approval (weekend 3, ~14h).** Base-resume YAML, cover-letter and resume-tailor engines, docx/PDF generation, Telegram approval cards, deep-link submission. The resume tailor is the hardest single component in the project — budget accordingly.

**Phase 4 — Coverage + feedback (weekend 4, ~10h).** Email-alert adapter (LinkedIn/Naukri/Indeed), HN + Remotive/RemoteOK, outcome logging, per-market funnel report.

**Phase 5 — v1.5 hardening (ongoing).** Playwright ATS assist, answer bank, prompt iteration from edit-diffs, companies.yaml growth habit.

Solo: ~44–50h. Pairing with Claude on the code: roughly **16–20h of your time across 2 weekends**, plus the non-compressible week of digest-watching after Phase 1 and ~30 days of sent applications before the retro queries mean anything.

---

## 10. Costs

| Item | Monthly |
|---|---|
| VPS | ₹400–1,000 ($5–12) |
| Embeddings | <$1 |
| Scoring (~40 jobs/day) | $2–3 |
| Drafting (~8/day, two artifacts each) | $6–9 |
| **Total** | **~$14–25 (₹1,200–2,100)** |

No per-application platform fees exist for full-time roles — the entire Upwork Connects line item from v1 is gone. Costs are now purely infra + tokens.

---

## 11. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Adapter rot (ATS slug/format changes) | 12h silence alert per tier; adapters isolated; companies.yaml validated weekly |
| Resume tailor fabricates | Base-YAML-only hard rule; resume-diff shown in approval card; human review |
| LinkedIn account risk | Easy Apply never automated; LinkedIn touched only via its own alert emails |
| Truncated alert-mail descriptions | Fetch linked posting where public; else score with confidence penalty |
| ATS keyword over-optimization reads as spam | 250-word cover cap; tailoring limited to reorder/rephrase of true content |
| Two markets starve each other | Per-market funnel report; per-market salary floors; optional split profiles |
| Score drift after prompt edits | Golden-set gate ≥85% before shipping any prompt version |
| You stop logging outcomes | Weekly digest with one-tap outcome buttons |

---

## 12. Portfolio Angle

Unchanged and stronger: LLM orchestration, retrieval, document generation, browser automation, and a measurable feedback loop — with a README metric ("interview rate by score bucket") that proves the system works rather than claims it. The freelance version becomes a natural sequel project sharing the same core.