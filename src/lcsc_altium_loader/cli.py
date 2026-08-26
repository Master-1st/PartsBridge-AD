"""Command-line interface for LCSC search, resolve and long-term library append."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from typing import Iterable

from . import __version__
from .client import ClientError, LCSCClient
from .convert import prepare_libraries
from .integrity import verify_output
from .jlc_api import JLCOpenApiSettings, signing_self_test
from .library_store import default_output_dir
from .workflow import read_codes, read_queries, resolve_queries, write_candidate_csv


def _client() -> LCSCClient:
    return LCSCClient()


def _cmd_search(args: argparse.Namespace) -> int:
    if not args.query.strip():
        print("query must not be empty", file=sys.stderr)
        return 3
    client = _client()
    try:
        rows = client.search(args.query, limit=args.limit, in_stock=args.in_stock)
    except ClientError as exc:
        print(json.dumps({"query": args.query, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([item.to_dict() for item in rows], ensure_ascii=False, indent=2))
    else:
        print("rank\texact\tlcsc\tmpn\tmanufacturer\tpackage\tstock\tprice\tcurrency")
        for index, item in enumerate(rows, 1):
            print("\t".join(str(value) for value in (index, str(item.exact).lower(), item.lcsc, item.mpn, item.manufacturer, item.package, item.stock, item.price, item.currency)))
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    try:
        queries = read_queries(Path(args.input_csv))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    client = _client()
    try:
        result = resolve_queries(
            client, queries, limit=args.limit, in_stock=args.in_stock
        )
        write_candidate_csv(Path(args.output), result.rows)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "output": str(Path(args.output).resolve()),
                "queries": result.query_count,
                "candidates": result.candidate_count,
                "no_matches": result.no_match_count,
                "errors": result.error_count,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    if args.input_csv:
        try:
            codes = read_codes(Path(args.input_csv))
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 3
    else:
        codes = args.codes
    if not codes:
        print("prepare requires at least one C-code or --input-csv", file=sys.stderr)
        return 3
    invalid = [code for code in codes if not re.fullmatch(r"C\d+", str(code).strip(), flags=re.IGNORECASE)]
    if invalid:
        print(f"prepare input contains non-LCSC codes: {', '.join(map(str, invalid[:5]))}", file=sys.stderr)
        return 3
    client = _client()
    try:
        output = Path(args.output).expanduser() if args.output else default_output_dir()
        manifest, status = prepare_libraries(client, codes, output, with_3d=args.with_3d)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (ClientError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"无法追加到长期总库：{exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "components": len(manifest["components"]),
                "added_count": manifest["added_count"],
                "skipped_count": manifest["skipped_count"],
                "total_components": manifest["total_components"],
                "added_codes": manifest["added_codes"],
                "skipped": manifest["skipped"],
                "failures": len(manifest["failures"]),
                "failure_details": manifest["failures"],
                "state": manifest.get("status"),
                "status": status,
                "state_directory": str(manifest["state_directory"]),
                "backup_directory": (
                    str(manifest["backup_directory"])
                    if manifest["backup_directory"] is not None
                    else None
                ),
            },
            ensure_ascii=False,
        )
    )
    return status


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})

    add(
        "python",
        sys.version_info[:2] == (3, 12),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    try:
        import tkinter

        add("tkinter", True, f"Tk {tkinter.TkVersion}")
    except ImportError as exc:
        add("tkinter", False, str(exc))
    for distribution in ("easyeda2kicad", "altium-monkey"):
        try:
            add(distribution, True, importlib.metadata.version(distribution))
        except importlib.metadata.PackageNotFoundError:
            add(distribution, False, "not installed")
    add("jlc_signing", signing_self_test(), "official documentation vector")
    settings = JLCOpenApiSettings.from_env()
    add(
        "official_api_credentials",
        settings.configured,
        "configured but live MRO method remains unverified"
        if settings.configured
        else "pending approval; missing " + ", ".join(settings.missing_variables),
        required=False,
    )
    if args.online:
        client = _client()
        try:
            detail = client.get_detail("C25804")
            add("public_lcsc_detail", bool(detail.get("productCode")), "C25804")
        except ClientError as exc:
            add("public_lcsc_detail", False, str(exc))
        try:
            component = client.get_component_data("C25804")
            add("easyeda_component", bool(component), "C25804")
        except ClientError as exc:
            add("easyeda_component", False, str(exc))
    ok = all(bool(item["ok"]) for item in checks if bool(item["required"]))
    report = {"version": __version__, "ok": ok, "checks": checks}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            requirement = "required" if item["required"] else "informational"
            state = "OK" if item["ok"] else ("INFO" if not item["required"] else "FAIL")
            print(f"{state}\t{item['name']}\t{requirement}\t{item['detail']}")
    return 0 if ok else 1


def _cmd_gui(_args: argparse.Namespace) -> int:
    from .gui import run

    run()
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        output = Path(args.output).expanduser() if args.output else default_output_dir()
        report = verify_output(output)
    except RuntimeError as exc:
        print(f"无法验证长期总库：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("OK" if report["ok"] else "FAILED")
        for check in report.get("checks", []):
            print(f"OK\t{check}")
        for error in report.get("errors", []):
            print(f"FAIL\t{error}", file=sys.stderr)
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcsc-altium",
        description="Search LCSC data and append confirmed parts to long-term native Altium libraries",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search", help="search public LCSC data")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--in-stock", action="store_true")
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=_cmd_search)
    resolve = sub.add_parser("resolve", help="resolve a CSV query list into candidates")
    resolve.add_argument("--input-csv", required=True)
    resolve.add_argument("--output", required=True)
    resolve.add_argument("--limit", type=int, default=5)
    resolve.add_argument("--in-stock", action="store_true")
    resolve.set_defaults(func=_cmd_resolve)
    prepare = sub.add_parser("prepare", help="append confirmed parts to the long-term library")
    prepare.add_argument("codes", nargs="*")
    prepare.add_argument("--input-csv")
    prepare.add_argument(
        "--output", default=None, help="long-term library directory (default: remembered directory)"
    )
    prepare.add_argument(
        "--with-3d",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="request embedded STEP data (default: enabled)",
    )
    prepare.set_defaults(func=_cmd_prepare)
    doctor = sub.add_parser("doctor", help="check runtime, signer and optional network access")
    doctor.add_argument("--online", action="store_true")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)
    verify = sub.add_parser("verify", help="verify the long-term library and its manifest")
    verify.add_argument(
        "--output", default=None, help="long-term library directory (default: remembered directory)"
    )
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=_cmd_verify)
    gui = sub.add_parser("gui", help="open the Windows desktop application")
    gui.set_defaults(func=_cmd_gui)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


__all__ = ["build_parser", "main"]
