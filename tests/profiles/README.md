# Synthetic load profiles

Fixed, non-household load profiles used for synthetic tariff bill checks are
packaged under `src/tariff_catalogue/checks/profiles` so installed harvesters can
load them. Regenerate the CSVs with `python scripts/gen_profiles.py`; the script uses
a fixed random seed and writes a year of electricity intervals plus daily gas
reads. The gas profile's `import_kwh` column contains m³ meter quantities and is
converted to MJ during billing.

Profiles cover no solar, solar, solar with battery, EV-heavy use,
controlled-load use, and a winter-weighted gas household. They are generated
data, not household usage data.
