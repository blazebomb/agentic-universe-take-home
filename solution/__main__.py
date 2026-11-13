import argparse
import csv
import sqlite3
from pathlib import Path

from .ledger import ingest
from .reports import report


def main():
    parser = argparse.ArgumentParser(description="Build and report on the interaction ledger.")
    parser.add_argument("--db", type=Path, default=Path("ledger.db"))
    commands = parser.add_subparsers(dest="command", required=True)
    ingest_parser = commands.add_parser("ingest")
    ingest_parser.add_argument("drops", type=Path, nargs="+", help="Drop paths in delivery order.")
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--out", type=Path, default=Path("reports"))

    args = parser.parse_args()
    try:
        if args.command == "ingest":
            ingest(args.db, args.drops)
        else:
            report(args.db, args.out)
    except KeyError as error:
        if error.args == ("LEDGER_KEY",):
            parser.exit(1, "Set LEDGER_KEY to a Fernet key before ingesting.\n")
        raise
    except (OSError, ValueError, csv.Error, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
