Copied from tariff-core. Do not edit here; edit in tariff-core and re-copy.

# Tariff plan schema (v1)

This document is the contract shared by three repositories:

- `tariff-core`: the Python library that defines, validates and evaluates plans
- `tariff-catalogue`: the data pipeline that harvests, curates and publishes plans
- `ha-energy-tariff`: the Home Assistant custom integration that consumes plans

`tariff-core` owns this file. The other repos must not redefine the schema; they depend on the published `tariff-core` package and link here.

> Project and package names are working names and may change before first release.

## Scope

The schema covers three commodities:

| Commodity | Typical structure | Catalogue sources | Release |
|---|---|---|---|
| `electricity` | Fixed + time-of-use or flat usage, blocks, demand, export (feed-in or two-way), controlled loads | AU CDR, US URDB, UK Octopus, community | v1 |
| `gas` | Fixed + usage in blocks, often seasonal; billed in energy units, metered in volume | AU CDR, community | v1 |
| `water` | Fixed access charges + usage in blocks; sewerage often derived from water usage | Community and user-built only | After the plan builder |

## Core concepts

| Concept | Meaning | Mutable? |
|---|---|---|
| **Plan** | A product a supplier offers, e.g. Origin "Go Variable Ongoing" on Ergon | Identity only |
| **PlanVersion** | The plan's full pricing as of one effective date | **Never.** A price change creates a new version |
| **NetworkTariffVersion** | An electricity distribution-network tariff for one regulatory year | **Never** |
| **Contract** | What one household is actually on, for one commodity | Yes, but only by appending segments |
| **ContractSegment** | `[from, to)` → a frozen copy of a PlanVersion, plus optional overrides | Never after creation |

Billing an interval means: find the contract segment active at that instant, then evaluate that segment's frozen plan. History is never rewritten.

## Identifiers

Plan IDs are namespaced by source so IDs from different sources cannot collide:

| Source | Pattern | Example |
|---|---|---|
| Official feed | `{country}:{feed}:{native_id}` | `au:cdr:ORI1161031MRE3@EME` |
| Network tariff | `{country}:network:{dnsp}:{zone}:{code}` | `au:network:ergon:east:RTOUE` |
| Community | `{country}:community:{slug}` | `gb:community:octopus-go` |
| Formula template | `{country}:formula:{slug}` | `nl:formula:tibber-dynamic` |
| User-built | `user:{uuid4}` | `user:7c1e…` |

A version ID is `{plan_id}@{effective_from:YYYY-MM-DD}#{content_hash[:8]}`. `content_hash` is the SHA-256 of the canonical JSON (sorted keys, no whitespace, all monetary and quantity values as decimal strings) of the version **excluding** metadata fields (`id`, `retrieved_at`, `source`, `confidence`). Two harvests with identical pricing produce identical hashes; that is how duplicate versions are suppressed.

## Quantities and units

Every usage component names the unit it is priced in. Units belong to a **dimension**; the engine converts within a dimension and never across one without an explicit contract conversion.

| Dimension | Units | Notes |
|---|---|---|
| energy | `Wh`, `kWh`, `MWh`, `MJ`, `GJ`, `therm` | 1 kWh = 3.6 MJ; 1 therm = 105.5056 MJ |
| volume | `L`, `kL`, `m3`, `ft3`, `CCF`, `gal` | 1 kL = 1 m³; 1 CCF = 100 ft³; `gal` = US gallon (3.785411784 L) |
| power | `kW`, `kVA` | demand charges only |

- `gal` always means the US gallon. An imperial gallon, if ever needed, is a separate unit `gal_imp`.
- Conversion constants live in `tariff-core` as exact `Decimal` values and are covered by tests.
- Gas **volume → energy** conversion is never a constant. It depends on heating value and correction factors that vary by network and over time, so it lives on the contract (see `conversions`), not in the plan.

## Registers

A register is one metered (or derived) stream that can be priced separately.

| Commodity | Registers |
|---|---|
| electricity | `general`, `controlled_load_1`, `controlled_load_2` |
| gas | `general` |
| water | `general` (potable), `recycled`; `sewerage` is normally a derived quantity, see below |

## PlanVersion

```yaml
id: au:cdr:ORI1161031MRE3@EME@2026-10-01#3fa1c2d9
plan_id: au:cdr:ORI1161031MRE3@EME
kind: retail                 # retail | network
display_name: Origin Go Variable Ongoing - New & Move Customers only
supplier: {id: origin, name: Origin Energy}
commodity: electricity       # electricity | gas | water
bundle_id: null              # set when a source sells several commodities as one plan (dual fuel); shared by each per-commodity PlanVersion
customer_type: residential   # residential | business
region:                      # generic hierarchy; deepest known level
  country: AU
  network: ergon
  zone: null                 # e.g. east | west | mount_isa for Ergon network tariffs
currency: AUD                # ISO 4217
tax:
  inclusive: true            # are the rates below tax-inclusive?
  rate: "0.10"               # informational; used to convert when needed
timezone: Australia/Brisbane # IANA
time_basis: local            # local | market_standard (e.g. NEM time, no DST)
holidays: {calendar: AU-QLD, treatment: none}  # none | as_weekend | own_schedule
effective: {from: 2026-10-01, to: null}         # half-open [from, to)
pricing_model: pass_through | bundled
network_tariff_ref: null     # electricity only; required when pricing_model = pass_through
billing: {frequencies: [P1M, P3M]}
seasons:                     # optional; omitted = one season all year
  summer: {from: "11-01", to: "04-01"}   # MM-DD, half-open, may wrap the year end
  other:  {from: "04-01", to: "11-01"}
  # shorthand also accepted: summer: {months: [11, 12, 1, 2, 3]}
schedules:                   # named reusable schedules; each is a LIST of windows
  peak:     [{days: all, time: ["16:00", "21:00"]}]
  off_peak: [{days: all, time: ["11:00", "16:00"]}]
  shoulder: [{days: all, time: ["21:00", "11:00"]}]
  # split example: [{days: weekdays, time: ["07:00","09:00"]}, {days: weekends, time: ["07:00","22:00"]}]
components: [...]            # see below
source:
  type: official_feed        # official_feed | community | formula | user | network_list
  feed: au_cdr
  url: https://cdr.energymadeeasy.gov.au/origin/cds-au/v1/energy/plans/ORI1161031MRE3@EME
  retrieved_at: 2026-10-09T08:15:00+10:00
  raw_sha256: …
confidence: high             # high | medium | low | unverified
partial: false               # true if any source feature could not be mapped
unmapped_features: []        # human-readable notes when partial = true
notes: []                    # e.g. "Uber Eats voucher incentive not modelled"
```

### Time rules (apply everywhere)

- All windows are **half-open** `[start, end)` in `HH:MM`. A window may wrap midnight (`["21:00", "11:00"]`).
- Sources that publish inclusive end minutes (CDR uses `"20:59"`) must be converted on import: `20:59` → `21:00`.
- A schedule is a list of windows; the schedule matches if any window matches. Windows within one schedule must not overlap.
- Seasons are `MM-DD` half-open date ranges and must cover the whole year exactly once (or be omitted entirely). Leap day belongs to whichever season contains `02-29` when evaluated as a date.
- `season` may be set on **any** component (`fixed`, `usage`, `demand`, `discount`). A component without `season` applies in all seasons.
- Coverage: for every `(register, direction, season)` with usage components, the schedules must cover all 168 hours of the week.
- Precedence: a component with a schedule beats a component with no schedule ("anytime"), and a component with a season beats one without. Overlaps are allowed only where precedence resolves them; any other overlap is a validation error.
- Interval data is labelled by **interval end** (NEM convention). An interval ending 16:30 belongs to the window containing 16:15, i.e. the interval's start is used for window lookup.
- Gas and water are often read daily, monthly or quarterly rather than in short intervals. A long interval is billed as a whole against the season and block period it falls in; if it spans a season or rate change it is split pro-rata by elapsed time and a warning is recorded.
- Day names: `mon … sun`. `days` may also be `weekdays`, `weekends`, `all`.

## Components

Every component has `kind`. Monetary values and quantities are **decimal strings**, never floats.

### `fixed`

```yaml
- kind: fixed
  label: supply           # supply | metering | subscription | water_access | sewerage_access | other
  register: general       # optional; controlled-load supply charges use their register
  unit: per_day           # per_day | per_month | per_quarter | per_year
  rate: "1.42482"
```

### `usage`

Charges per unit of a metered or derived quantity. (Called `energy` in early drafts; `usage` covers gas and water too.)

```yaml
- kind: usage
  direction: import       # import | export (export is electricity only)
  register: general
  quantity_unit: kWh      # see "Quantities and units"
  schedule: peak          # name from `schedules`, an inline schedule, or omitted = anytime
  season: summer          # optional
  period: peak            # normalised: peak | shoulder | off_peak | solar_soak | critical_peak | single
  period_label: Peak      # supplier's own wording, for display only
  rate: "0.36955"         # currency per quantity_unit
  stack: true             # optional: price alongside the selected usage component
  blocks:                 # optional stepped pricing; replaces `rate`
    period: billing_period   # day | billing_period | month | quarter | year
    prorate: true
    tiers:
      - {up_to: "1000", rate: "0.28"}   # up_to is in quantity_unit
      - {rate: "0.31"}
```

**Derived quantities.** A usage component may charge on a quantity calculated from another register instead of its own meter, e.g. sewerage billed on 85% of water used:

```yaml
- kind: usage
  register: sewerage
  quantity: {from: general, factor: "0.85"}   # register of the same commodity
  quantity_unit: kL
  rate: "1.92"
```

Derived registers have no meter of their own and are excluded from coverage checks only if every component on them is derived.

**Sign convention:** `rate` is what the customer pays per unit. Export credits (feed-in tariffs) are **negative**. Export charges (two-way tariffs) are **positive**. Engine output always reports positive amounts with a line type (`cost` or `credit`); signs never leak to Home Assistant entities.

**Dynamic rates:** `rate` may be an object instead of a string:

```yaml
rate:
  entity: sensor.amber_general_price   # resolved by the host (HA), not by tariff-core
  multiplier: "1.0"
  adder: "0.0"
  unit_scale: "1.0"                     # e.g. 0.001 if the feed is per MWh
```

`tariff-core` treats this as a `RateSource` that the caller must resolve to numbers before billing.

### `demand`

Electricity only.

```yaml
- kind: demand
  schedule: [{days: all, time: ["16:00", "21:00"]}]
  method: max_interval_avg   # max_interval_avg | max_instant | top_n_avg | rolling_12m_max
  interval_minutes: 30
  top_n: null                # required for top_n_avg
  measure: kW                # kW | kVA
  threshold: null            # charge only demand above this, e.g. "35"
  reset: monthly             # monthly | billing_period
  unit: per_kw_per_month     # per_kw_per_month | per_kw_per_day | per_kva_per_month
  rate: "12.50"
```

### `discount`

```yaml
- kind: discount
  applies_to: [usage.import]   # component selectors
  percent: "10"
  conditional: pay_on_time     # null = unconditional
```

Conditional discounts are excluded from plan comparisons by default and shown separately.

### `incentive`

Informational only (vouchers, sign-up credits). Never included in bill calculations.

## Examples

### Gas: daily supply plus two seasonal blocks, priced in MJ

```yaml
commodity: gas
seasons:
  peak:     {from: "06-01", to: "10-01"}
  off_peak: {from: "10-01", to: "06-01"}
components:
  - {kind: fixed, label: supply, unit: per_day, rate: "0.9100"}
  - kind: usage
    direction: import
    quantity_unit: MJ
    season: peak
    blocks: {period: day, prorate: false, tiers: [{up_to: "100", rate: "0.0420"}, {rate: "0.0360"}]}
  - kind: usage
    direction: import
    quantity_unit: MJ
    season: off_peak
    blocks: {period: day, prorate: false, tiers: [{up_to: "100", rate: "0.0390"}, {rate: "0.0340"}]}
```

### Water: access charges, inclining blocks, state bulk charge, derived sewerage

```yaml
commodity: water
billing: {frequencies: [P3M]}
components:
  - {kind: fixed, label: water_access,    unit: per_quarter, rate: "75.20"}
  - {kind: fixed, label: sewerage_access, unit: per_quarter, rate: "165.40"}
  - kind: usage
    direction: import
    quantity_unit: kL
    blocks: {period: quarter, prorate: true, tiers: [{up_to: "60", rate: "1.62"}, {rate: "2.10"}]}
  - kind: usage
   direction: import
   quantity_unit: kL
   label: bulk_water
   rate: "3.29"
   stack: true
  - {kind: usage, register: sewerage, quantity: {from: general, factor: "0.85"}, quantity_unit: kL, rate: "1.92"}
```

Rates in the examples are illustrative, not real tariffs.

## Contract

```yaml
contract:
  id: 1f3c…                    # uuid
  commodity: gas
  meters:                      # how each register is measured at this household
    general: {unit: m3}        # unit the household's meter or HA sensor reports
  conversions:                 # dated series; required when meter and billed units differ in dimension
    - from: 2026-07-01
      register: general
      meter_unit: m3
      billed_unit: MJ
      heating_value: "38.62"       # MJ per m³, as printed on the bill
      correction_factor: "0.9823"  # pressure/temperature correction, as printed on the bill
  segments:
    - from: 2026-10-01
      to: null
      plan: {…full frozen PlanVersion…}
      catalogue_ref: au:cdr:ORI…@EME@2026-10-01#…   # null for user plans
      overrides: []            # JSON-pointer patches applied on top of `plan`
  billing:
    cycle_anchor_day: 14
    frequency: P3M
```

- A segment stores the **full** plan, not just a reference, so the contract keeps working when the catalogue is unreachable or the plan has been withdrawn.
- Conversions are household data (they come from the bill), so they live on the contract, never in the plan. Billed quantity = `meter_quantity × heating_value × correction_factor`.
- Same-dimension differences (e.g. a meter in L, a plan in kL) are converted automatically and need no `conversions` entry.

## Validation

`tariff-core` publishes a JSON Schema generated from its types. Beyond structural validation, `validate_plan()` must check:

1. Schedule coverage is exactly 168 h per `(register, direction, season)` with usage components, after applying precedence; seasons cover the year exactly once.
2. `pass_through` plans have `network_tariff_ref`; only electricity plans may use it.
3. Currency, timezone and holiday calendar codes are valid.
4. No float values anywhere in monetary or quantity fields.
5. Version ID hash matches content.
6. Every `quantity_unit` is valid, and `export` usage and `demand` components appear only on electricity plans.
7. Derived quantities reference an existing register of the same commodity and do not form cycles.

`validate_contract()` must additionally check that every register's meter unit can be converted to every billed unit used by its plan, either within a dimension or through a `conversions` entry covering the whole segment.

## Changing this schema

- Additive, optional fields: minor version bump of `tariff-core`.
- Anything that changes meaning or removes fields: new schema major version (`v2`), with a migration function in `tariff-core` and parallel publishing of both versions by `tariff-catalogue` for at least one HA release cycle.
- Nothing has been released yet, so pre-release renames (`fuel` → `commodity`, `energy` → `usage`) are applied directly with no migration.
