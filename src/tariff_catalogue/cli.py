"""Command-line interface for the tariff catalogue."""

import argparse
import os
from pathlib import Path

from tariff_catalogue.harvest.au_cdr.brands import CDRResponseError, discover_brands
from tariff_catalogue.harvest.au_cdr.listing import list_changed_plans, load_last_success
from tariff_catalogue.harvest.common.archive import LocalArchiveStore
from tariff_catalogue.harvest.common.http import PoliteClient
from tariff_catalogue.harvest.common.report import RunReport


def _not_implemented(_args: argparse.Namespace) -> None:
    print("not implemented")


def _harvest_au_cdr(args: argparse.Namespace) -> None:
    if not args.list_only:
        _not_implemented(args)
        return

    archive = LocalArchiveStore(args.archive_root)
    report = RunReport()
    with PoliteClient(report=report) as client:
        brands = discover_brands(client, report)
        for brand in brands:
            since = load_last_success(archive, brand.brand_id)
            failures_before = report.failures
            try:
                plans = list_changed_plans(
                    client, brand, since, None if args.dry_run else archive
                )
            except Exception as error:
                if isinstance(error, CDRResponseError) or report.failures == failures_before:
                    report.record_failure(error)
                print(f"{brand.brand_name} ({brand.brand_id}): error")
                continue
            print(f"{brand.brand_name} ({brand.brand_id}): {len(plans)} plans")
    if not report.write_summary():
        print(report.to_markdown(), end="")


def _archive_ls(args: argparse.Namespace) -> None:
    for path in LocalArchiveStore(args.root).list(args.prefix):
        print(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tariff-catalogue")
    commands = parser.add_subparsers(dest="command", required=True)

    harvest = commands.add_parser("harvest", help="Harvest plans from a supported source.")
    harvest_commands = harvest.add_subparsers(dest="source", required=True)
    au_cdr = harvest_commands.add_parser("au-cdr", help="Harvest Australian CDR plans.")
    au_cdr.add_argument("--list-only", action="store_true")
    au_cdr.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("DRY_RUN", "").casefold() in {"1", "true", "yes"},
    )
    au_cdr.add_argument(
        "--archive-root", type=Path, default=Path(os.getenv("ARCHIVE_ROOT", "archive"))
    )
    au_cdr.set_defaults(handler=_harvest_au_cdr)

    publish = commands.add_parser("publish", help="Build and publish catalogue files.")
    publish.set_defaults(handler=_not_implemented)

    check = commands.add_parser("check", help="Run catalogue checks.")
    check.set_defaults(handler=_not_implemented)

    community = commands.add_parser("community", help="Manage community submissions.")
    community_commands = community.add_subparsers(dest="community_command", required=True)
    intake = community_commands.add_parser("intake", help="Process community plan intake.")
    intake.set_defaults(handler=_not_implemented)

    archive_ls = commands.add_parser("archive-ls", help="List files in the local archive.")
    archive_ls.add_argument("prefix", nargs="?", default="")
    archive_ls.add_argument(
        "--root", type=Path, default=Path(os.getenv("ARCHIVE_ROOT", "archive"))
    )
    archive_ls.set_defaults(handler=_archive_ls)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
