"""Bounded HTTP for public literature sources; parent processes enforce wall deadlines."""
from __future__ import annotations

import datetime as dt
import hashlib
from email.utils import parsedate_to_datetime
import json
import random
import time
from urllib.parse import urlsplit, urljoin

import requests


class SourceError(RuntimeError):
    def __init__(self, status: str, code: str, retry_after: float = 0):
        super().__init__(code)
        self.status, self.code, self.retry_after = status, code, retry_after


def retry_seconds(value: str | None) -> float:
    try:
        return max(0, float(value or 0))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(0, (parsed - dt.datetime.now(dt.timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class Runtime:
    def __init__(self, settings: dict, hosts: set[str], *, cache=None, save_cache=None, progress=None):
        self.settings, self.hosts = settings, hosts
        self.deadline = time.monotonic() + float(settings.get("deadline_seconds", 90))
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'ccf-paper-scout/0.3 (literature discovery)'
        self.requests = self.bytes = 0
        self.last_request: dict[str, float] = {}
        self.quota: dict[str, str] = {}
        self.cache = cache if cache is not None else {}
        self.save_cache = save_cache
        self.cache_hits = 0
        self.progress = progress

    def cache_response(self, key, body, headers):
        if 'no-store' in headers.get('Cache-Control', '').lower():
            return
        entry = dict(cache_key=key, body=body, fetched_at=time.time(),
                     etag=headers.get('ETag', ''), last_modified=headers.get('Last-Modified', ''))
        self.cache[key] = entry
        if self.save_cache:
            self.save_cache(entry)

    def close(self):
        self.session.close()

    def wait(self, seconds: float):
        if time.monotonic() + seconds >= self.deadline:
            raise SourceError('partial', 'source_deadline')
        time.sleep(max(0, seconds))

    def get(self, url: str, *, params=None, headers=None, max_bytes=2_000_000, cache_seconds=300) -> str:
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in self.hosts or parts.username or parts.password or parts.port not in (None, 443):
            raise SourceError('protocol_error', 'untrusted_endpoint')
        if time.monotonic() >= self.deadline:
            raise SourceError('partial', 'source_deadline')
        # Hash the access context; never persist API keys, headers or credential-bearing URLs.
        cache_key = hashlib.sha256(json.dumps([url, params, dict(self.session.headers), headers], sort_keys=True).encode()).hexdigest()
        cached = self.cache.get(cache_key)
        if cached and len(cached.get('body', '').encode('utf-8')) > max_bytes:
            raise SourceError('protocol_error', 'response_too_large')
        if cached and cached.get('body') is not None and time.time() < cached['fetched_at'] + cache_seconds:
            self.cache_hits += 1
            return cached['body']
        attempts = int(self.settings.get('max_attempts', 2))
        for attempt in range(attempts):
            current, current_params, current_headers = url, params, dict(headers or {})
            if cached and cached.get('body') is not None:
                if cached.get('etag'):
                    current_headers['If-None-Match'] = cached['etag']
                if cached.get('last_modified'):
                    current_headers['If-Modified-Since'] = cached['last_modified']
            for redirect in range(4):
                parts = urlsplit(current)
                if parts.scheme != 'https' or parts.hostname not in self.hosts or parts.username or parts.password or parts.port not in (None, 443):
                    raise SourceError('protocol_error', 'untrusted_endpoint')
                if self.requests >= int(self.settings.get('max_requests', 10)):
                    raise SourceError('partial', 'request_budget')
                host = parts.hostname
                self.wait(max(0, float(self.settings.get('min_interval_seconds', 1)) -
                              (time.monotonic() - self.last_request.get(host, 0))))
                self.requests += 1
                if self.progress:
                    self.progress(dict(requests=self.requests, bytes=self.bytes, cache_hits=self.cache_hits))
                self.last_request[host] = time.monotonic()
                remaining = self.deadline - time.monotonic()
                timeout = (min(float(self.settings.get('connect_timeout_seconds', 8)), remaining),
                           min(float(self.settings.get('read_timeout_seconds', 20)), remaining))
                try:
                    with self.session.get(current, params=current_params, headers=current_headers,
                                          timeout=timeout, stream=True, allow_redirects=False) as response:
                        self.quota.update({k: v for k, v in response.headers.items() if 'ratelimit' in k.lower()})
                        if response.is_redirect:
                            if redirect == 3:
                                raise SourceError('protocol_error', 'redirect_limit')
                            target = urljoin(current, response.headers.get('Location', ''))
                            if urlsplit(target).hostname != host:
                                current_headers = {}  # Never forward credentials across hosts.
                            current, current_params = target, None
                            continue
                        code = response.status_code
                        if code == 304:
                            if cached and cached.get('body') is not None:
                                self.cache_hits += 1
                                self.cache_response(cache_key, cached['body'], {
                                    'ETag': response.headers.get('ETag', cached.get('etag', '')),
                                    'Last-Modified': response.headers.get('Last-Modified', cached.get('last_modified', ''))})
                                return cached['body']
                            # An unsolicited 304 is not an empty success; the next attempt costs a request.
                            if redirect == 3:
                                raise SourceError('protocol_error', 'cache_miss_304')
                            current_headers.pop('If-None-Match', None)
                            current_headers.pop('If-Modified-Since', None)
                            continue
                        if code in (429, 500, 502, 503, 504):
                            delay = retry_seconds(response.headers.get('Retry-After')) or 2 ** (attempt + 1) + random.uniform(0, .5)
                            status = 'rate_limited' if code == 429 else 'unavailable'
                            if code == 429 or attempt + 1 == attempts or delay >= self.deadline - time.monotonic():
                                raise SourceError(status, f'http_{code}', delay)
                            self.wait(delay)
                            break
                        if code in (401, 403):
                            raise SourceError('auth_required' if code == 401 else 'blocked', f'http_{code}')
                        if code == 404:
                            raise SourceError('not_found', 'http_404')
                        if code != 200:
                            raise SourceError('protocol_error', f'http_{code}')
                        data = bytearray()
                        for chunk in response.iter_content(65536):
                            if time.monotonic() >= self.deadline:
                                raise SourceError('partial', 'source_deadline')
                            data.extend(chunk)
                            if len(data) > max_bytes:
                                raise SourceError('protocol_error', 'response_too_large')
                        self.bytes += len(data)
                        text = data.decode('utf-8', errors='replace')
                        lowered = text[:10000].lower()
                        if any(token in lowered for token in ('making sure you\'re not a bot', 'challengerequirederror', '<title>just a moment')):
                            raise SourceError('blocked', 'challenge_page')
                        self.cache_response(cache_key, text, response.headers)
                        return text
                except (requests.Timeout, requests.ConnectionError) as exc:
                    if attempt + 1 == attempts:
                        status, code = ('timeout', 'transport_timeout') if isinstance(exc, requests.Timeout) else (
                            'unavailable', 'connection_failed')
                        raise SourceError(status, code) from None
                    self.wait(2 ** (attempt + 1) + random.uniform(0, .5))
                    break
                except requests.RequestException:
                    raise SourceError('protocol_error', 'invalid_http_response') from None
        raise SourceError('unavailable', 'attempts_exhausted')

    def json(self, url, **kwargs):
        try:
            value = json.loads(self.get(url, **kwargs))
        except json.JSONDecodeError:
            raise SourceError('protocol_error', 'invalid_json') from None
        if not isinstance(value, dict):
            raise SourceError('protocol_error', 'invalid_json_object')
        return value
