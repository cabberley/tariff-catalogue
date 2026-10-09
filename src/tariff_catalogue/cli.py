"""Command-line interface for the tariff catalogue."""

import argparse


def _not_implemented(_args: argparse.Namespace) -> None:
    print("not implemented")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tariff-catalogue")
    commands = parser.add_subparsers(dest="command", required=True)

    harvest = commands.add_parser("harvest", help="Harvest plans from a supported source.")
    harvest_commands = harvest.add_subparsers(dest="source", required=True)
    au_cdr = harvest_commands.add_parser("au-cdr", help="Harvest Australian CDR plans.")
    au_cdr.set_defaults(handler=_not_implemented)

    publish = commands.add_parser("publish", help="Build and publish catalogue files.")
    publish.set_defaults(handler=_not_implemented)

    check = commands.add_parser("check", help="Run catalogue checks.")
    check.set_defaults(handler=_not_implemented)

    community = commands.add_parser("community", help="Manage community submissions.")
    community_commands = community.add_subparsers(dest="community_command", required=True)
    intake = community_commands.add_parser("intake", help="Process community plan intake.")
    intake.set_defaults(handler=_not_implemented)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
