"""Structured, sanitized trace of one Guide request (schema version 1).

The collector records what the bridge actually did - routing lookups,
provider rounds, every game tool call, timings and the final grounding
state - so a controller can show evidence without scraping logs.

It never stores tool result text, SQL, raw rows, provider payloads, the
system prompt, conversation text or model reasoning. Tool arguments are
reduced to schema-declared scalar keys, and secret-like keys are dropped.
"""

import json
import re
import time

from guide_readiness import (
    FAILED_LOOKUP_PREFIXES, NO_MATCH_ITEM, NO_MATCH_PREFIXES, readiness_key,
)
from guide_reliability import EvidenceLedger

TRACE_SCHEMA_VERSION = 1
MAX_ARG_BYTES = 128
MAX_ROUNDS = 12
MAX_TOOL_CALLS = 40
MAX_TOOL_NAME = 64

GROUNDING_STATES = (
    'verified', 'not-required', 'no-result', 'clarification', 'failed',
    'unknown',
)
TOOL_STATUSES = (
    'succeeded', 'no-result', 'failed', 'invalid-arguments', 'unknown-tool',
)
ERROR_CODES = (
    'timeout', 'provider-unavailable', 'model-unavailable', 'provider-rejected',
    'provider-incomplete', 'grounding-rejected', 'database-unavailable',
    'internal-error',
)

_SECRET_KEY = re.compile(
    r'pass|secret|token|key|sql|credential|auth|cookie|session', re.IGNORECASE)
_SAFE_TOOL_NAME = re.compile(r'[^A-Za-z0-9_.-]')
_GROUNDING_REJECTIONS = (
    'No successful factual lookup', 'unverified entity link',
    'raw game hyperlink', 'malformed entity link',
)


def truncate_utf8(value, limit=MAX_ARG_BYTES):
    """Cut a string to at most `limit` UTF-8 bytes on a character boundary."""
    data = value.encode('utf-8')
    if len(data) <= limit:
        return value
    return data[:limit].decode('utf-8', errors='ignore')


def safe_tool_name(name):
    text = name if isinstance(name, str) else ''
    return _SAFE_TOOL_NAME.sub('_', text)[:MAX_TOOL_NAME] or 'unnamed'


def summarize_arguments(properties, arguments):
    """Keep schema-declared scalar arguments only, bounded and secret-free."""
    if not isinstance(arguments, dict) or not isinstance(properties, dict):
        return {}
    summary = {}
    for key in sorted(arguments):
        if key not in properties or _SECRET_KEY.search(key):
            continue
        value = arguments[key]
        if value is None or isinstance(value, (bool, int, float)):
            summary[key] = value
        elif isinstance(value, str):
            summary[key] = truncate_utf8(value)
        elif isinstance(value, list):
            summary[key] = f'{len(value)} item(s)'
    return summary


def classify_tool_result(name, arguments, result, executor=None):
    """Status of one lookup, reusing AnswerReadiness when it judged it."""
    text = result if isinstance(result, str) else ''
    if text.startswith('Unknown tool:'):
        return 'unknown-tool'
    if text.startswith('Invalid tool arguments:'):
        return 'invalid-arguments'
    if text.startswith(FAILED_LOOKUP_PREFIXES):
        return 'failed'
    readiness = getattr(executor, 'readiness', None)
    if readiness is not None and getattr(executor, 'readiness_enabled', False):
        check = readiness.checks.get(readiness_key(name, arguments))
        if check is not None and not check[0]:
            return 'no-result'
        if check is not None:
            return 'succeeded'
    if not text.strip() or text.startswith(NO_MATCH_PREFIXES) or \
            NO_MATCH_ITEM.match(text):
        return 'no-result'
    return 'succeeded'


def classify_error(error):
    """Map a request failure to a fixed code; never keep its message."""
    if isinstance(error, TimeoutError):
        return 'timeout'
    kind = type(error).__name__
    status = getattr(error, 'status_code', None)
    if kind in {'APIConnectionError', 'APITimeoutError', 'ConnectionError'}:
        return 'provider-unavailable'
    if status == 404 or kind == 'NotFoundError':
        return 'model-unavailable'
    if isinstance(status, int):
        return 'provider-rejected'
    module = type(error).__module__ or ''
    if module.startswith('mysql'):
        return 'database-unavailable'
    if isinstance(error, ValueError):
        message = str(error)
        if any(marker in message for marker in _GROUNDING_REJECTIONS):
            return 'grounding-rejected'
        return 'provider-incomplete'
    return 'internal-error'


class GuideTraceCollector:
    """Collects one request's trace. One instance per request/worker."""

    def __init__(self, tools, clock=time.monotonic):
        self._clock = clock
        self._properties = {
            tool['name']: tool.get('input_schema', {}).get('properties', {})
            for tool in tools
        }
        self._started = clock()
        self.rounds = []
        self.provider_ms = 0.0
        self.provider_calls = 0
        self.tool_ms = 0.0
        self.tool_calls = 0
        self.dropped_tool_calls = 0

    def begin_round(self, phase):
        if len(self.rounds) >= MAX_ROUNDS:
            return
        self.rounds.append({
            'index': len(self.rounds) + 1,
            'phase': 'routing' if phase == 'routing' else 'answer',
            'tools': [],
        })

    def record_provider(self, duration_ms):
        self.provider_calls += 1
        self.provider_ms += max(0.0, float(duration_ms))

    def record_tool(self, name, arguments, result, duration_ms, executor=None):
        self.tool_calls += 1
        self.tool_ms += max(0.0, float(duration_ms))
        if not self.rounds:
            self.begin_round('answer')
        entries = sum(len(item['tools']) for item in self.rounds)
        if entries >= MAX_TOOL_CALLS or len(self.rounds) > MAX_ROUNDS:
            self.dropped_tool_calls += 1
            return
        known = name in self._properties
        text = result if isinstance(result, str) else ''
        self.rounds[-1]['tools'].append({
            'name': name if known else safe_tool_name(name),
            'status': classify_tool_result(name, arguments, text, executor),
            'duration_ms': round(max(0.0, float(duration_ms)), 1),
            'marker_count': len(set(
                match[0] for match in EvidenceLedger.MARKER.finditer(text))),
            'args': summarize_arguments(
                self._properties.get(name, {}), arguments) if known else {},
        })

    def tool_failures(self):
        return sum(1 for item in self.rounds for tool in item['tools']
                   if tool['status'] in {'failed', 'invalid-arguments',
                                         'unknown-tool'})

    def finish(self, grounding_state, evidence_markers=0, error_code=None):
        if grounding_state not in GROUNDING_STATES:
            grounding_state = 'unknown'
        if error_code is not None and error_code not in ERROR_CODES:
            error_code = 'internal-error'
        total = max(0.0, (self._clock() - self._started) * 1000.0)
        return {
            'schema_version': TRACE_SCHEMA_VERSION,
            'grounding_state': grounding_state,
            'rounds': [item for item in self.rounds if item['tools']],
            'provider_ms': round(self.provider_ms),
            'provider_calls': self.provider_calls,
            'tool_ms': round(self.tool_ms),
            'tool_calls': self.tool_calls,
            'total_ms': round(total),
            'evidence_markers': int(evidence_markers),
            'tool_failures': self.tool_failures(),
            'dropped_tool_calls': self.dropped_tool_calls,
            'error_code': error_code,
        }


def trace_json(trace):
    return json.dumps(trace, ensure_ascii=False, separators=(',', ':'),
                      sort_keys=True)
