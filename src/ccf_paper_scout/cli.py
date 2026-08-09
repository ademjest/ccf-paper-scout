from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
from pathlib import Path
from importlib.resources import files
from typing import Any


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("years"), list) or not payload["years"]:
        raise ValueError("years must be a non-empty list")
    if not isinstance(payload.get("venue_keys"), list):
        raise ValueError("venue_keys must be a list")
    if int(payload.get("max_results", 20)) < 0:
        raise ValueError("max_results must be non-negative")
    return payload


def doctor(config_path: Path, network: bool = True) -> int:
    failures = 0
    try:
        config = load_config(config_path)
        print(f"config: ok ({config_path})")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"config: failed — {exc}")
        return 2
    for name in ("ZOTERO_USER_ID", "ZOTERO_API_KEY"):
        value = os.environ.get(name, "")
        print(f"{name}: {'present length=' + str(len(value)) if value else 'missing'}")
        failures += not bool(value)
    smtp = config.get("delivery", {}).get("smtp", {})
    if smtp.get("enabled"):
        for field in ("host", "port", "sender", "receiver"):
            present = bool(smtp.get(field))
            print(f"smtp.{field}: {'ok' if present else 'missing'}")
            failures += not present
        password_env = str(smtp.get("password_env", "SMTP_PASSWORD"))
        password = os.environ.get(password_env, "")
        print(f"{password_env}: {'present length=' + str(len(password)) if password else 'missing'}")
        failures += not bool(password)
        if network and smtp.get("host") and smtp.get("port"):
            try:
                with socket.create_connection((str(smtp["host"]), int(smtp["port"])), timeout=10) as sock:
                    if smtp.get("use_ssl", True):
                        with ssl.create_default_context().wrap_socket(sock, server_hostname=str(smtp["host"])):
                            pass
                print("smtp TLS: ok")
            except OSError as exc:
                print(f"smtp TLS: failed — {exc}")
                failures += 1
    state_path = Path(config.get("state_db", "state/paper_scout.sqlite3"))
    if not state_path.is_absolute():
        state_path = config_path.resolve().parent / state_path
    state_dir = state_path.parent
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        print(f"state path: writable ({state_dir})")
    except OSError as exc:
        print(f"state path: failed — {exc}")
        failures += 1
    return 0 if failures == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ccf-paper-scout")
    sub = parser.add_subparsers(dest="command")
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--config", type=Path, default=Path("config.json"))
    doctor_parser.add_argument("--no-network", action="store_true")
    run_parser = sub.add_parser("run", add_help=False)
    delivery_parser = sub.add_parser("test-delivery")
    delivery_parser.add_argument("--config", type=Path, default=Path("config.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if args.command == "run":
        args.args = list(unknown)
    elif unknown:
        parser.error("unrecognized arguments: " + " ".join(unknown))
    if args.command == "doctor":
        return doctor(args.config, not args.no_network)
    if args.command in ("run", "test-delivery"):
        import paper_scout
        if not hasattr(args, "venues"):
            args.venues = Path(str(files("ccf_paper_scout").joinpath("data/ccf_a_venues.json")))
        if args.command == "test-delivery":
            import sys
            old = sys.argv
            try:
                sys.argv = ["paper_scout.py", "--config", str(args.config), "--test-delivery"]
                return paper_scout.main()
            finally:
                sys.argv = old
        import sys
        old = sys.argv
        try:
            run_args = list(args.args)
            if "--venues" not in run_args:
                run_args.extend(["--venues", str(files("ccf_paper_scout").joinpath("data/ccf_a_venues.json"))])
            sys.argv = ["paper_scout.py", *run_args]
            return paper_scout.main()
        finally:
            sys.argv = old
    parser.print_help()
    return 0
