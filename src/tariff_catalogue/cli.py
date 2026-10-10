"""Command-line interface for the tariff catalogue."""

import argparse
import os
from pathlib import Path

from tariff_catalogue.harvest.au_cdr.brands import CDRResponseError, discover_brands
from tariff_catalogue.harvest.au_cdr.listing import list_changed_plans, load_last_success
from tariff_catalogue.harvest.au_cdr.run import run_au_cdr
from tariff_catalogue.harvest.common.archive import ArchiveStore, LocalArchiveStore, S3ArchiveStore
from tariff_catalogue.harvest.common.http import PoliteClient
from tariff_catalogue.harvest.common.report import RunReport
from tariff_catalogue.publish.build import build
from tariff_catalogue.publish.upload import upload


def _not_implemented(_args: argparse.Namespace) -> None:
    print("not implemented")


def _archive_store(root: Path) -> ArchiveStore:
    if os.getenv("R2_BUCKET") or os.getenv("R2_BUCKET_NAME"):
        return S3ArchiveStore()
    return LocalArchiveStore(root)


def _harvest_au_cdr(args: argparse.Namespace) -> None:
    archive = _archive_store(args.archive_root)
    report = RunReport()
    with PoliteClient(report=report) as client:
        if args.list_only:
            for brand in discover_brands(client, report):
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
        else:
            brands = None
            if args.brand:
                discovered = discover_brands(client, report)
                brands = [
                    brand
                    for brand in discovered
                    if brand.brand_id.casefold() == args.brand.casefold()
                ]
                if not brands:
                    report.record_failure(f"Unknown active CDR brand: {args.brand}")
            if brands or not args.brand:
                run_au_cdr(
                    client,
                    archive,
                    brands=brands,
                    dry_run=args.dry_run,
                    full=args.full,
                )
    if not report.write_summary():
        print(report.to_markdown(), end="")
    if args.report is not None:
        args.report.write_text(report.to_json() + "\n", encoding="utf-8")


def _archive_ls(args: argparse.Namespace) -> None:
    for path in LocalArchiveStore(args.root).list(args.prefix):
        print(path)


def _publish(args: argparse.Namespace) -> None:
    report = build(_archive_store(args.archive_root), args.out)
    if not args.dry_run:
        destination = S3ArchiveStore(os.getenv("PUBLISH_BUCKET") or os.getenv("R2_BUCKET"))
        upload_report = upload(destination, args.out)
        print(
            f"Built {report.plans} plans ({report.versions} versions); "
            f"uploaded {upload_report.uploaded} files, "
            f"{upload_report.unchanged} unchanged."
        )
    else:
        print(f"Built {report.plans} plans ({report.versions} versions) in {args.out}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tariff-catalogue")
    commands = parser.add_subparsers(dest="command", required=True)

    harvest = commands.add_parser("harvest", help="Harvest plans from a supported source.")
    harvest_commands = harvest.add_subparsers(dest="source", required=True)
    au_cdr = harvest_commands.add_parser("au-cdr", help="Harvest Australian CDR plans.")
    au_cdr.add_argument("--list-only", action="store_true")
    au_cdr.add_argument("--brand")
    au_cdr.add_argument("--full", action="store_true")
    au_cdr.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("DRY_RUN", "").casefold() in {"1", "true", "yes"},
    )
    au_cdr.add_argument(
        "--archive-root", type=Path, default=Path(os.getenv("ARCHIVE_ROOT", "archive"))
    )
    au_cdr.add_argument("--report", type=Path)
    au_cdr.set_defaults(handler=_harvest_au_cdr)

    publish = commands.add_parser("publish", help="Build and publish catalogue files.")
    publish.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("DRY_RUN", "").casefold() in {"1", "true", "yes"},
    )
    publish.add_argument("--out", type=Path, default=Path("dist"))
    publish.add_argument(
        "--archive-root", type=Path, default=Path(os.getenv("ARCHIVE_ROOT", "archive"))
    )
    publish.set_defaults(handler=_publish)

    check = commands.add_parser("check", help="Run catalogue checks.")
    check.set_defaults(handler=_not_implemented)

    community = commands.add_parser("community", help="Manage community submissions.")
    community_commands = community.add_subparsers(dest="community_command", required=True)
    intake = community_commands.add_parser("intake", help="Process community plan intake.")
    intake.set_defaults(handler=_not_implemented)

    archive_ls = commands.add_parser("archive-ls", help="List files in the local archive.")
    archive_ls.add_argument("prefix", nargs="?", default="")
    archive_ls.add_argument("--root", type=Path, default=Path(os.getenv("ARCHIVE_ROOT", "archive")))
    archive_ls.set_defaults(handler=_archive_ls)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
