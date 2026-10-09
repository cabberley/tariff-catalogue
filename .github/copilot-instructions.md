# Copilot instructions: tariff-catalogue

## What this repo is

`tariff-catalogue` collects tariff plans from official sources and community submissions, archives every version, validates them, and publishes a static catalogue that Home Assistant installs download.

It is a **data pipeline plus a curated dataset**, run by GitHub Actions. It has no always-on server and serves no user-specific data.

- Plan format and validation rules: `tariff-core` `docs/schema.md` (do not redefine them here).
- Parsing, normalising, hashing and validation: import from the `tariff-core` package. If a transformation is pure and reusable, it belongs in `tariff-core` (e.g. `tariff_core.adapters.cdr`), not here.
- This repo owns: fetching, scheduling, archiving, diffing, publishing, community intake and review tooling.

## Hard constraints

- **Be a polite client.** One harvest per source per day at most, with `updated-since` style incremental fetches where the source supports them. Respect `Retry-After`; back off exponentially on 429/5xx; cap concurrency (default 4). Identify with a descriptive `User-Agent` that includes the repo URL.
- **Never fetch on behalf of users.** Home Assistant installs only read the published static files; they never trigger harvesting.
- **Immutable history.** Raw responses and normalised versions are append-only. Never rewrite or delete an archived version; mark withdrawn plans instead.
- **No personal data.** Community submissions must contain plan data only. CI rejects files containing anything that looks like a meter identifier (NMI, MPAN, MPRN), address, email or phone number.
- **No secrets in the repo.** API keys (e.g. OpenEI) come from GitHub Actions secrets.
- **Deterministic output.** Publishing the same inputs twice must produce byte-identical files (sorted keys, canonical JSON from `tariff_core.dump_plan`).

## Repo layout

```
harvest/
  common/            # http client (retries, rate limit, UA), raw archive writer, run reports
  au_cdr/            # Australian CDR: brand discovery + plan list + plan detail
  us_urdb/           # OpenEI Utility Rate Database (later)
  gb_octopus/        # Octopus public products API (later)
network/
  au/{dnsp}/{year}/{zone}/{code}.yaml   # curated network tariffs, human-reviewed
community/
  {country}/{supplier}/{slug}.yaml      # community-submitted plans (via PR)
formula/
  {country}/{slug}.yaml                 # dynamic-price templates (spot + adders + tax)
archive/                                # NOT in git: object storage (R2/S3), see below
publish/
  build.py           # builds dist/ from current versions
  shards.py          # region sharding, index generation
review/
  extract_pdf.py     # maintainer tool: LLM-assisted draft from a price list PDF -> PR
  diff_report.py     # human-readable version diffs for PR bodies
scripts/
tests/
.github/workflows/
```

## Australian CDR harvester (first priority)

- Discover brands from the CDR Register: `GET https://api.cdr.gov.au/cdr-register/v1/energy/data-holders/brands/summary` (`x-v` header required). Use each brand's `productBaseUri`; do not hardcode the AER host, because a few retailers self-host.
- List plans: `GET {base}/cds-au/v1/energy/plans?type=ALL&fuelType=ALL&effective=CURRENT&updated-since={last_run}&page-size=1000`, header `x-v: 1`. Follow `meta.totalPages`.
- Detail: `GET {base}/cds-au/v1/energy/plans/{planId}`, header `x-v: 3` (fall back to 2, then 1). Only fetch details for plans listed as changed.
- Archive every raw detail response (gzip, keyed by `planId` + `lastUpdated` + sha256) before normalising.
- Harvest **both electricity and gas** (`fuelType=ALL`). CDR gas plans use `gasContract` and price usage in MJ; the adapter sets `commodity: gas` and `quantity_unit: MJ`. Dual-fuel plans are split into one PlanVersion per commodity sharing a `bundle_id`.
- Water has no official feed; water plans enter only through `community/` (and later formula templates where a utility publishes structured data).
- Normalise with `tariff_core.adapters.cdr`. If the adapter flags `partial: true`, still archive and publish, but the version is excluded from comparisons until fixed.
- Only current plans are exposed by the AER, so archiving from day one is the only way to build plan history.
- Known data quirks to handle (with tests): inclusive `HH:59` end times; the same product published many times under broker-specific names (dedupe groups by pricing content hash and expose `equivalent_plans`); GST convention stated in free text per field.

Reference volumes (October 2026): Origin alone publishes about 3,700 current plans; 518 of them have a Queensland distributor. Expect the full harvest to be tens of thousands of plan IDs but only hundreds of detail fetches on a normal day.

## Network tariffs (semi-automated)

- A scheduled workflow (daily May–July, weekly otherwise) fetches each distributor's known price-list URLs and AER pricing pages and hashes the documents. Some sites (e.g. Ergon) render document links with JavaScript; record those as `needs_browser: true` and handle them with a Playwright step.
- When a document changes, `review/extract_pdf.py` produces draft YAML and `review/diff_report.py` produces a rate-by-rate diff. The workflow opens a PR. **A human must approve every network tariff PR.**
- Each network tariff YAML records its source document URL, page or table reference, and retrieval date.
- Coordinate with the Open Energy Collective tariff service (MIT-licensed, 12 DNSPs). Prefer importing or mapping their curated data with attribution over duplicating effort.

## Community submissions

- Intake is a GitHub issue form or PR created from the Home Assistant integration's "Share this plan" link. Convert issues to PRs with a bot.
- CI on every PR: `tariff_core.validate_plan`, personal-data scan, synthetic bill checks (below), duplicate detection against existing plans.
- New community plans start at `confidence: unverified`. Confidence rises from anonymous bill-reconciliation reports (handled by a small Cloudflare Worker outside this repo; this repo only reads its exported counts).

## Synthetic bill checks

Run every new or changed version against fixed load profiles in `tests/profiles/` (no solar; solar only; solar + battery; EV-heavy; controlled load). Flag for review when:

- any energy rate is above 1.00 or below -1.00 per kWh in the plan's currency
- a retail plan has no supply/fixed component
- the annual bill on any profile moves more than 30% from the previous version
- schedule coverage validation fails

## Published output

```
dist/v1/
  index.json                            # countries, feeds, build time, schema version
  {country}/index.json                  # regions and counts
  {country}/{region}/index.json         # plan summaries for that region (id, name, supplier,
                                        #   customer_type, fuel, pricing_model, latest_version, hash)
  plans/{url_safe_plan_id}/index.json   # all versions of one plan (ids + effective ranges)
  plans/{url_safe_plan_id}/{version_hash}.json   # full PlanVersion
```

- Published to Cloudflare R2 (or GitHub Pages) behind a CDN with long cache lifetimes on version files (immutable) and short ones on index files.
- Include an `ETag`-friendly layout so Home Assistant can poll index files cheaply.
- `schema_version` is in every index. When `tariff-core` ships schema v2, publish `dist/v2/` alongside `dist/v1/` for a transition period.

## Archive storage

Raw responses and full version history are too large for git. Store them in object storage under `archive/{feed}/{yyyy}/{mm}/{dd}/…`. Git holds curated YAML (network, community, formula) and the code; the archive holds machine-harvested data.

## Testing and tooling

- Python, `ruff`, `mypy`, `pytest`. Record HTTP fixtures with `respx` or `vcrpy`; tests never hit live endpoints.
- Every adapter quirk found in production gets a fixture and a test.
- Workflows must be runnable locally with `make harvest-au-cdr DRY_RUN=1`.

## Do not

- Put parsing or schema logic here that belongs in `tariff-core`.
- Hand-edit files under `dist/`.
- Auto-merge network tariff or community PRs.
- Publish versions that fail validation (archive them; do not publish).
- Store anything about individual households.
