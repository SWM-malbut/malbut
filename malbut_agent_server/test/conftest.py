"""Shared fixtures: keep keys set on this machine out of every test."""

import pytest
from urllib.parse import urlsplit


@pytest.fixture(scope='session')
def _empty_key_root(tmp_path_factory):
    return tmp_path_factory.mktemp('malbut-keys')


@pytest.fixture(autouse=True)
def _no_web_keys(monkeypatch, _empty_key_root):
    """Keys the web set on this machine never reach a test (SWM25-235)."""
    monkeypatch.setenv('MALBUT_KEY_DIR', str(_empty_key_root / 'none'))


@pytest.fixture(autouse=True)
def _no_remote_pooled_inference(monkeypatch):
    """Real pool fixtures use loopback; all model calls must inject a transport."""
    from malbut_agent_server.providers.http_pool import HTTPConnectionPool

    post = HTTPConnectionPool.post

    def local_post(pool, url, *args, **kwargs):
        if urlsplit(url).hostname not in {'localhost', '127.0.0.1', '::1'}:
            pytest.fail('Offline test attempted a remote pooled model request; inject transport')
        return post(pool, url, *args, **kwargs)

    monkeypatch.setattr(HTTPConnectionPool, 'post', local_post)
