"""Authenticated HTTP management boundary for server-owned story memory.

History approval snapshots stay on the server; clients receive single-use
opaque handles. Output pages preserve complete evidence spans. The current
service still loads an owner's full story list before this boundary pages it;
database-level pagination is a future storage optimization.
"""

from collections import OrderedDict
from http import HTTPStatus
import json
import math
import secrets
import threading
import time

from .story_memory import StoryConflictError, StoryMemoryError, _id
from .story_memory_service import StoryServiceError, StoryUnavailableError


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_HISTORY_BYTES = 1024 * 1024
MAX_HISTORY_TURNS = 4096
MAX_PREVIEWS = 8
PREVIEW_TTL_SECONDS = 300


class StoryHTTPError(Exception):
    """A content-free error safe to expose to an authenticated client."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def _invalid():
    raise StoryHTTPError(HTTPStatus.BAD_REQUEST, 'invalid_story_request')


def _schema(body, allowed, required=()):
    if type(body) is not dict or set(body) - set(allowed) or set(required) - set(body):
        _invalid()


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid()
    return value


def _boolean(value):
    if type(value) is not bool:
        _invalid()
    return value


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


class StoryHTTPBoundary:
    """Share one bounded approval cache across an HTTP server's handlers."""

    def __init__(self, runtime, clock=time.monotonic):
        self.runtime = runtime
        self._clock = clock
        self._lock = threading.Lock()
        self._previews = OrderedDict()

    @staticmethod
    def _policy(service, user):
        raw = service.policy(user)
        result = {key: raw[key] for key in (
            'enabled', 'external_consent', 'revision', 'data_revision',
            'available', 'queued', 'running', 'failed', 'pending',
        ) if key in raw}
        result['error'] = 'processing_failed' if raw.get('error') else None
        return result

    def _fact_revision(self, user):
        return self.runtime.memory_store.policy_state(user)['revision']

    def _fence(self, service, user, before, fact_revision):
        after = service.policy(user)
        if (any(before[key] != after[key] for key in ('revision', 'data_revision'))
                or self._fact_revision(user) != fact_revision):
            raise StoryHTTPError(HTTPStatus.CONFLICT, 'story_changed')

    def _prune(self):
        now = self._clock()
        for token, entry in list(self._previews.items()):
            if entry['expires_at'] <= now:
                del self._previews[token]

    @staticmethod
    def _page(items, body, field):
        limit = _integer(body.get('limit', 20), 1, 50)
        offset = _integer(body.get('offset', 0), 0, 100000)
        selected, size = [], 4096
        for item in items[offset:offset + limit]:
            item_size = len(_encoded(item)) + 1
            if size + item_size > MAX_RESPONSE_BYTES:
                if not selected:
                    raise StoryHTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                         'story_item_too_large')
                break
            selected.append(item)
            size += item_size
        next_offset = offset + len(selected)
        return {field: selected, 'total': len(items),
                'next_offset': next_offset if next_offset < len(items) else None}

    @staticmethod
    def _listed(story):
        return {
            'story_id': story['story_id'], 'title': story['title'],
            'aliases': story.get('aliases', []), 'version': story['version'],
            'updated_at': story['updated_at'],
            'current': [{key: entry[key] for key in ('text', 'kind', 'actor', 'status')}
                        for entry in story['current']],
            'untrusted': True, 'execution_authorized': False,
        }

    def handle(self, path, body, user):
        """Handle only an authenticated, server-bound user supplied by HTTP."""
        service = getattr(self.runtime, 'story_memory', None)
        if service is None:
            raise StoryHTTPError(HTTPStatus.SERVICE_UNAVAILABLE, 'story_unavailable')
        try:
            # Serialize token use and mutations. A bounded sync wait must not
            # delay a concurrent consent withdrawal.
            if path.rsplit('/', 1)[-1] in {'enable', 'disable', 'delete', 'history-preview'}:
                with self._lock:
                    return self._handle(service, path, body, user)
            return self._handle(service, path, body, user)
        except StoryHTTPError:
            raise
        except StoryConflictError:
            raise StoryHTTPError(HTTPStatus.CONFLICT, 'story_changed') from None
        except StoryUnavailableError:
            raise StoryHTTPError(HTTPStatus.SERVICE_UNAVAILABLE, 'story_unavailable') from None
        except StoryServiceError as error:
            code = getattr(error, 'code', 'story_error')
            if code not in {'story_not_settled', 'history_review_required'}:
                code = 'story_unavailable'
            status = HTTPStatus.CONFLICT if code != 'story_unavailable' else HTTPStatus.SERVICE_UNAVAILABLE
            raise StoryHTTPError(status, code) from None
        except StoryMemoryError:
            raise StoryHTTPError(HTTPStatus.BAD_REQUEST, 'invalid_story_request') from None

    def _handle(self, service, path, body, user):
        action = path.removeprefix('/v1/stories/')
        if action == 'status':
            _schema(body, ())
            return HTTPStatus.OK, {'policy': self._policy(service, user), 'consent': {
                'separate_from_fact_memory': True, 'external_processing': True,
                'input_scope': ['new_completed_turns', 'selected_story_summaries',
                                'exact_quoted_evidence'],
                'history_requires_preview': True, 'retention': 'until_deleted',
                'off_preserves_saved_stories': True,
            }}
        if action in {'list', 'evidence'}:
            allowed = {'limit', 'offset'} | ({'story_id'} if action == 'evidence' else set())
            _schema(body, allowed, ('story_id',) if action == 'evidence' else ())
            _integer(body.get('limit', 20), 1, 50)
            _integer(body.get('offset', 0), 0, 100000)
            before = self._policy(service, user)
            fact_revision = self._fact_revision(user)
            if action == 'list':
                items = [self._listed(story) for story in service.list_stories(user)]
                result = self._page(items, body, 'stories')
            else:
                story_id = _id(body['story_id'], 'story_id')
                items = service.evidence(user, story_id)
                if not items:
                    raise StoryHTTPError(HTTPStatus.NOT_FOUND, 'story_not_found')
                result = self._page(items, body, 'sources')
                result['story_id'] = story_id
            self._fence(service, user, before, fact_revision)
            result.update(revision=before['revision'], data_revision=before['data_revision'])
            return HTTPStatus.OK, result
        if action == 'history-preview':
            _schema(body, ())
            before = self._policy(service, user)
            fact_revision = self._fact_revision(user)
            scope = service.history_preview(user)
            self._fence(service, user, before, fact_revision)
            if len(scope['turns']) > MAX_HISTORY_TURNS or len(_encoded(scope)) > MAX_HISTORY_BYTES:
                raise StoryHTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 'history_scope_too_large')
            self._prune()
            while len(self._previews) >= MAX_PREVIEWS:
                self._previews.popitem(last=False)
            token = secrets.token_urlsafe(32)
            self._previews[token] = {'user': user, 'revision': before['revision'],
                                     'scope': scope, 'expires_at': self._clock() + PREVIEW_TTL_SECONDS}
            return HTTPStatus.OK, {'preview_token': token, 'revision': before['revision'],
                                   'expires_in_seconds': PREVIEW_TTL_SECONDS,
                                   'scope': {key: scope[key] for key in ('turn_count', 'first_at', 'last_at')}}
        if action == 'enable':
            _schema(body, {'consent', 'external_consent', 'expected_revision',
                           'include_history', 'preview_token'},
                    ('consent', 'external_consent', 'expected_revision'))
            if not _boolean(body['consent']) or not _boolean(body['external_consent']):
                _invalid()
            revision = _integer(body['expected_revision'], 0, 2 ** 63 - 1)
            include_history = _boolean(body.get('include_history', False))
            token, scope = body.get('preview_token'), None
            if include_history:
                if type(token) is not str or not 32 <= len(token) <= 128 or not token.isascii():
                    _invalid()
                self._prune()
                entry = self._previews.get(token)
                if entry is None or entry['user'] != user or entry['revision'] != revision:
                    raise StoryHTTPError(HTTPStatus.CONFLICT, 'history_preview_expired')
                scope = entry['scope']
            elif 'preview_token' in body:
                _invalid()
            service.enable(user, include_history=include_history, history_scope=scope,
                           expected_revision=revision)
            # Any policy change makes previously issued approval handles stale.
            self._previews.clear()
            return HTTPStatus.OK, {'policy': self._policy(service, user)}
        if action == 'disable':
            _schema(body, {'expected_revision'}, ('expected_revision',))
            revision = _integer(body['expected_revision'], 0, 2 ** 63 - 1)
            service.disable(user, expected_revision=revision)
            self._previews.clear()
            return HTTPStatus.OK, {'policy': self._policy(service, user)}
        if action == 'delete':
            _schema(body, {'story_id'}, ('story_id',))
            result = service.forget(user, _id(body['story_id'], 'story_id'), timeout=0)
            if not result.get('deleted'):
                raise StoryHTTPError(HTTPStatus.NOT_FOUND, 'story_not_found')
            self._previews.clear()
            return HTTPStatus.OK, result
        if action == 'sync':
            _schema(body, {'timeout_seconds', 'retry_failed'})
            timeout = body.get('timeout_seconds', 0)
            if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                    or not 0 <= timeout <= 5):
                _invalid()
            retry = _boolean(body.get('retry_failed', False))
            settled = service.flush(user, timeout=timeout, retry_failed=retry)
            policy = self._policy(service, user)
            status = HTTPStatus.OK if settled else HTTPStatus.ACCEPTED
            if not settled and policy.get('failed'):
                raise StoryHTTPError(HTTPStatus.SERVICE_UNAVAILABLE, 'story_processing_failed')
            return status, {'settled': settled, 'policy': policy}
        raise StoryHTTPError(HTTPStatus.NOT_FOUND, 'not_found')
