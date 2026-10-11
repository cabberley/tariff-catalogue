# Formula templates

Dynamic-price plan templates belong here as `formula/{country}/{slug}.yaml`.
Each file contains a `PlanVersion` with a `{price_entity}` rate placeholder and
a `template` section with user-facing hints, expected units, and notes. The
template metadata is published separately from the plan version.

Validate all templates locally with:

```sh
PYTHONPATH=src python -m tariff_catalogue.review.formula_validation
```
