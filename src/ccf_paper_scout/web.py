"""Loopback-only search UI. No delivery credentials or actions are exposed."""
from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import secrets
import threading
from urllib.parse import unquote

from state_store import StateStore
from . import search, search_history, search_evaluation


class SearchServer(ThreadingHTTPServer):
    def __init__(self, config, config_path, port):
        super().__init__(('127.0.0.1', port), Handler)
        self.config = config
        self.database = search.database(config, config_path)
        store = StateStore(self.database)
        store.close()
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.job = None
        self.worker = None
        self.cancel = threading.Event()

    def start_search(self, arguments):
        # Validate synchronously before creating a worker or spending any requests.
        search.settings(self.config, arguments.get('query'), arguments.get('sources'), arguments.get('years'))
        if arguments.get('publication_scope', 'published') not in search.SCOPES:
            raise ValueError('invalid publication_scope')
        if arguments.get('ranking', 'hybrid') not in search.RANKINGS:
            raise ValueError('invalid search ranking')
        with self.lock:
            if self.worker and self.worker.is_alive():
                return None
            self.cancel = threading.Event()
            identifier = secrets.token_hex(12)
            self.job = dict(id=identifier, state='starting', results=[], discovery={})

            def update(value):
                with self.lock:
                    self.job = dict(copy.deepcopy(value), id=identifier)

            def execute():
                try:
                    result = search.run(self.config, self.database, arguments['query'], enabled=arguments.get('sources'),
                        years=arguments.get('years'), scope=arguments.get('publication_scope', 'published'),
                        cancel=self.cancel, update=update, search_id=identifier, split=arguments.get('split', 'dev'),
                        cached_only=arguments.get('cached_only', False), require_abstract=arguments.get('require_abstract', False),
                        ranking=arguments.get('ranking', 'hybrid'))
                    update(result)
                except Exception as exc:
                    # Do not expose tracebacks or credential-bearing provider URLs in the UI.
                    with self.lock:
                        self.job.update(state='failed', error=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)

            self.worker = threading.Thread(target=execute, name='scout-search')
            self.worker.start()
            return identifier

    def server_close(self):
        if hasattr(self, 'cancel'):
            self.cancel.set()
        if getattr(self, 'worker', None):
            self.worker.join(timeout=10)
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Search terms and paper identities stay out of server access logs.

    def respond(self, status, payload, content_type='application/json; charset=utf-8'):
        body = payload.encode('utf-8') if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'nonce-" + self.server.token + "'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, write=False):
        expected = '127.0.0.1:' + str(self.server.server_port)
        origin = self.headers.get('Origin')
        valid = self.headers.get('Host') == expected and (not origin or origin == 'http://' + expected)
        if write:
            valid = valid and secrets.compare_digest(self.headers.get('X-Scout-Token', ''), self.server.token)
        if not valid:
            self.respond(403, {'error': 'local_origin_required'})
        return valid

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == '/':
            html = files('ccf_paper_scout').joinpath('data/search.html').read_text(encoding='utf-8')
            self.respond(200, html.replace('__SCOUT_TOKEN__', self.server.token), 'text/html; charset=utf-8')
        elif self.path in ('/searches', '/benchmark', '/annotation-tasks') or self.path.startswith('/searches/'):
            store = StateStore(self.server.database)
            try:
                if self.path == '/searches':
                    self.respond(200, search_history.recent(store))
                elif self.path in ('/benchmark', '/annotation-tasks'):
                    snapshot = search_evaluation.export(store)
                    self.respond(200, search_evaluation.ai_packet(snapshot) if self.path == '/annotation-tasks' else snapshot)
                else:
                    identifier = self.path[len('/searches/'):]
                    result = search_history.get(store, identifier)
                    with self.server.lock:
                        job = copy.deepcopy(self.server.job)
                        live = bool(job and job['id'] == identifier and self.server.worker and self.server.worker.is_alive())
                    if not result and job and job['id'] == identifier:
                        result = job
                    self.respond(200 if result else 404, dict(result, live=live) if result else {'error':'search_not_found'})
            finally:
                store.close()
        else:
            self.respond(404, {'error': 'not_found'})

    def do_POST(self):
        if not self.allowed(write=True):
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192 or self.headers.get_content_type() != 'application/json':
                raise ValueError('small JSON body required')
            self.connection.settimeout(5)
            arguments = json.loads(self.rfile.read(length))
            if not isinstance(arguments, dict):
                raise ValueError('JSON object required')
            if self.path == '/searches':
                if (type(arguments.get('cached_only', False)) is not bool or type(arguments.get('require_abstract', False)) is not bool
                        or arguments.get('split', 'dev') not in ('dev', 'test')):
                    raise ValueError('invalid search options')
                identifier = self.server.start_search(arguments)
                self.respond(202 if identifier else 409, {'id': identifier} if identifier else {'error': 'search_in_progress'})
            elif self.path.startswith('/searches/') and self.path.endswith('/feedback'):
                identifier = self.path[len('/searches/'):-len('/feedback')]
                if set(arguments) - {'paper_id', 'grade', 'reason', 'snapshot_hash'} or 'grade' not in arguments:
                    raise ValueError('invalid query judgment')
                store = StateStore(self.server.database)
                try:
                    with store.connection:
                        value = search_history.label(store, identifier, arguments.get('paper_id'), arguments['grade'],
                            reason=arguments.get('reason', ''), snapshot_hash=arguments.get('snapshot_hash'))
                    self.respond(200, value)
                finally:
                    store.close()
            elif self.path.startswith('/papers/') and self.path.endswith('/feedback'):
                paper_id = unquote(self.path[len('/papers/'):-len('/feedback')])
                if not arguments or set(arguments) - {'read', 'saved'}:
                    raise ValueError('invalid feedback fields')
                if any(type(arguments[k]) is not bool for k in ('read', 'saved') if k in arguments):
                    raise ValueError('read and saved must be booleans')
                store = StateStore(self.server.database)
                try:
                    if not store.connection.execute('SELECT 1 FROM candidate_papers WHERE paper_id=?', (paper_id,)).fetchone():
                        self.respond(404, {'error': 'paper_not_found'})
                        return
                    row = store.connection.execute('SELECT payload_json FROM paper_feedback WHERE paper_id=?', (paper_id,)).fetchone()
                    value = {**(json.loads(row[0]) if row else {}), **arguments}
                    store.save_feedback(paper_id, value)
                    self.respond(200, value)
                finally:
                    store.close()
            else:
                self.respond(404, {'error': 'not_found'})
        except (ValueError, UnicodeError):
            self.respond(400, {'error': 'invalid_request'})
        except (OSError, TimeoutError):
            self.respond(408, {'error': 'request_timeout'})

    def do_DELETE(self):
        if not self.allowed(write=True):
            return
        with self.server.lock:
            job = self.server.job
            exists = job and self.path == '/searches/' + job['id']
            if exists:
                self.server.cancel.set()
        self.respond(202 if exists else 404, {'state': 'cancelling'} if exists else {'error': 'search_not_found'})


def serve(config, config_path, port=8765):
    if not 0 <= port <= 65535:
        raise ValueError('port must be 0..65535')
    with SearchServer(config, config_path, port) as server:
        print(f'Paper search: http://127.0.0.1:{server.server_port} (Ctrl+C to stop)', flush=True)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass
    return 0
