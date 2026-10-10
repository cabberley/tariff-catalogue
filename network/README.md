# Network tariffs

Curated, human-reviewed network tariff plans belong here, organized by country,
distributor, regulatory year, zone, and tariff code. Record the source document
URL, page or table reference, and retrieval date in each tariff file.

`sources.yaml` lists documents checked by `python -m tariff_catalogue.review.watch`.
The watcher archives changed documents and opens an issue for human review; it
does not extract rates or modify tariff files. Ergon's JavaScript-rendered source
is marked `needs_browser: true` and requires the `network-watch` extra.
