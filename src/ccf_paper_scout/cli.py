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
    from .discovery import resolve_years
    resolve_years(payload)
    if payload.get('sources', {}).get('enabled'):
        from .sources import validate
        validate(payload['sources'])
    if not isinstance(payload.get("venue_keys"), list):
        raise ValueError("venue_keys must be a list")
    max_results = int(payload.get("max_results", 20))
    if not 1 <= max_results <= 20:
        raise ValueError("max_results must be 1..20")
    mode = payload.get("semantic", {}).get("mode", "shadow")
    if mode not in ("shadow", "production"):
        raise ValueError("semantic.mode must be shadow or production")
    if payload.get("relation_policy", {}).get("mode", "shadow") not in ("shadow", "production"):
        raise ValueError("relation_policy.mode must be shadow or production")
    for section in ("selection", "relation_policy"):
        calibration_file = payload.get(section, {}).get("calibration_file")
        if calibration_file:
            calibration_path = Path(calibration_file)
            if not calibration_path.is_absolute():
                calibration_path = path.resolve().parent / calibration_path
            if calibration_path.exists():
                payload[section]["calibration"] = json.loads(calibration_path.read_text(encoding="utf-8"))
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
    doctor_parser.add_argument("--sources", action="store_true", help="diagnose literature sources without Zotero/SMTP")
    doctor_parser.add_argument("--network", action="store_true", help="make bounded public-source probes")
    search_parser = sub.add_parser('search')
    search_parser.add_argument('--config', type=Path, default=Path('config.example.json'))
    search_parser.add_argument('--query', required=True)
    search_parser.add_argument('--sources', nargs='+')
    search_parser.add_argument('--years', nargs='+', type=int)
    search_parser.add_argument('--scope', choices=('verified_main', 'published', 'all_with_preprints'), default='all_with_preprints')
    search_parser.add_argument('--limit', type=int, default=20)
    search_parser.add_argument('--cached-only', action='store_true')
    search_parser.add_argument('--output', type=Path)
    search_parser.add_argument('--split', choices=('dev', 'test'), default='dev')
    history_parser = sub.add_parser('search-history')
    history_parser.add_argument('--config', type=Path, default=Path('config.example.json'))
    export_parser = sub.add_parser('search-export')
    export_parser.add_argument('--config', type=Path, default=Path('config.example.json'))
    export_parser.add_argument('--output', type=Path, required=True)
    export_parser.add_argument('--search-ids', nargs='+')
    export_parser.add_argument('--ai-tasks', action='store_true')
    import_parser = sub.add_parser('search-import-ai')
    import_parser.add_argument('--config', type=Path, default=Path('config.example.json'))
    import_parser.add_argument('--input', type=Path, required=True)
    evaluation_parser = sub.add_parser('search-evaluate')
    evaluation_parser.add_argument('--input', type=Path, required=True)
    evaluation_parser.add_argument('--output', type=Path, required=True)
    evaluation_parser.add_argument('--ranking', choices=('stored', 'lexical'), default='stored')
    evaluation_parser.add_argument('--rankings', type=Path, help='optional comparison rankings for the same frozen pool')
    evaluation_parser.add_argument('--k', type=int, default=5)
    serve_parser = sub.add_parser('serve')
    serve_parser.add_argument('--config', type=Path, default=Path('config.example.json'))
    serve_parser.add_argument('--port', type=int, default=8765)
    sub.add_parser("run", add_help=False)
    sub.add_parser("discover", add_help=False)
    sub.add_parser("evaluate", add_help=False)
    delivery_parser = sub.add_parser("test-delivery")
    delivery_parser.add_argument("--config", type=Path, default=Path("config.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if args.command in ("run", "discover", "evaluate"):
        args.args = list(unknown)
    elif unknown:
        parser.error("unrecognized arguments: " + " ".join(unknown))
    if args.command == "doctor":
        if args.sources:
            from .search import doctor as source_doctor
            return source_doctor(load_config(args.config), args.network and not args.no_network)
        return doctor(args.config, not args.no_network)
    if args.command == 'search':
        from . import search
        config = load_config(args.config)
        result = search.run(config, search.database(config, args.config), args.query, enabled=args.sources,
            years=args.years, scope=args.scope, limit=args.limit, cached_only=args.cached_only,
            update=lambda value: print(json.dumps(value, ensure_ascii=False)), split=args.split)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command in ('search-history', 'search-export', 'search-import-ai', 'search-evaluate'):
        from . import search, search_history, search_evaluation
        from state_store import StateStore
        if args.command == 'search-evaluate':
            snapshot = json.loads(args.input.read_text(encoding='utf-8-sig'))
            overrides = json.loads(args.rankings.read_text(encoding='utf-8-sig')) if args.rankings else None
            value = search_evaluation.evaluate(snapshot, k=args.k, ranking=args.ranking, overrides=overrides)
        else:
            config = load_config(args.config)
            store = StateStore(search.database(config, args.config))
            try:
                if args.command == 'search-history':
                    value = search_history.recent(store)
                elif args.command == 'search-export':
                    value = search_evaluation.export(store, args.search_ids)
                    if args.ai_tasks:
                        value = search_evaluation.ai_packet(value)
                else:
                    value = {'imported_ai_suggestions': search_evaluation.import_ai(store,
                        json.loads(args.input.read_text(encoding='utf-8-sig')))}
            finally:
                store.close()
        if getattr(args, 'output', None):
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(f'Saved {args.output}')
        else:
            print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if args.command == 'serve':
        from .web import serve
        return serve(load_config(args.config), args.config, args.port)
    if args.command in ("run", "discover", "evaluate", "test-delivery"):
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
            if args.command == "discover":
                run_args.append("--discover-only")
            elif args.command == "evaluate":
                run_args.append("--evaluate-only")
            if "--venues" not in run_args:
                run_args.extend(["--venues", str(files("ccf_paper_scout").joinpath("data/ccf_a_venues.json"))])
            sys.argv = ["paper_scout.py", *run_args]
            return paper_scout.main()
        finally:
            sys.argv = old
    parser.print_help()
    return 0
