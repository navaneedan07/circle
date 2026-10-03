"""The API must never serve private archives to an unauthenticated caller.

Circle holds one person's entire message history and exposes an endpoint that
permanently deletes records. On localhost that is fine, because only the user's
own processes can reach the port. These tests pin the boundary so it cannot
regress silently if the app is ever exposed (tunnel, shared host, LAN).
"""
from __future__ import annotations

import importlib

import pytest
from starlette.requests import Request

from circle.api import auth


def _req(path: str, key: str | None = None, host: str = "203.0.113.9",
         method: str = "GET") -> Request:
    headers = [(auth.KEY_HEADER.encode(), key.encode())] if key else []
    return Request({
        "type": "http", "method": method, "path": path, "headers": headers,
        "client": (host, 5000), "query_string": b"", "scheme": "http",
        "server": ("test", 80),
    })


@pytest.fixture()
def clear_settings_cache():
    """Settings are lru_cached; env changes need an explicit clear."""
    from circle.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def reload_auth(clear_settings_cache):
    """Re-import the auth module so it re-reads the cached settings."""
    importlib.reload(auth)
    yield auth
    importlib.reload(auth)


class TestRemoteRequiresKey:
    def test_remote_without_key_is_refused(self):
        """The critical property: a tunnel with no key configured must not
        serve the archive to anyone who finds the URL."""
        allowed, reason = auth.check(_req("/api/people"))
        assert allowed is False
        assert "ACCESS_KEY" in reason

    def test_localhost_needs_no_key(self):
        """The everyday single-user case must not change."""
        assert auth.check(_req("/api/people", host="127.0.0.1"))[0] is True
        assert auth.check(_req("/api/people", host="::1"))[0] is True

    def test_ipv6_loopback_written_with_brackets(self):
        assert auth.check(_req("/api/people", host="::ffff:127.0.0.1"))[0] is True

    def test_in_process_testclient_counts_as_local(self):
        """Regression: the middleware 401'd every SPA route (/imports,
        /settings) under the test client, which reports a host of
        'testclient' because it has no real socket."""
        assert auth.check(_req("/imports", host="testclient"))[0] is True


class TestWithAccessKey:
    def test_correct_key_is_accepted(self, monkeypatch, reload_auth):
        monkeypatch.setenv("ACCESS_KEY", "friend-key-123")
        monkeypatch.setenv("ALLOW_REMOTE", "true")
        assert reload_auth.key_configured() is True
        assert reload_auth.check(_req("/api/people", "friend-key-123"))[0] is True
        # missing and wrong are both refused
        assert reload_auth.check(_req("/api/people"))[0] is False
        assert reload_auth.check(_req("/api/people", "wrong"))[0] is False
        # a prefix of the real key must not pass
        assert reload_auth.check(_req("/api/people", "friend-key"))[0] is False

    def test_loopback_still_needs_the_key_once_set(self, monkeypatch,
                                                   reload_auth):
        """Once a key exists, a loopback browser tab must present it too,
        otherwise any page the user visits could read their messages."""
        monkeypatch.setenv("ACCESS_KEY", "k123")
        assert reload_auth.check(_req("/api/people", host="127.0.0.1"))[0] is False
        assert reload_auth.check(
            _req("/api/people", "k123", host="127.0.0.1"))[0] is True


class TestAlwaysReachable:
    """The UI cannot show a useful error if it cannot reach these at all."""

    @pytest.mark.parametrize("path", [
        "/api/health", "/", "/favicon.ico", "/assets/index-abc123.js",
    ])
    def test_public_paths_need_no_key(self, path, monkeypatch, reload_auth):
        monkeypatch.setenv("ACCESS_KEY", "k123")
        assert reload_auth.check(_req(path))[0] is True, path

    def test_auth_status_is_reachable_but_not_assumed_valid(
            self, monkeypatch, reload_auth):
        """It must stay REACHABLE (the UI loads it to learn what to do), while
        its answer must reflect the caller's actual key."""
        monkeypatch.setenv("ACCESS_KEY", "k123")
        assert reload_auth.check(_req("/api/auth/status"))[0] is False

    def test_merge_endpoint_is_marked_sensitive(self):
        """The one endpoint that destroys data."""
        assert auth.is_sensitive("/api/identity/merge") is True
        assert auth.is_sensitive("/api/people") is False

    @pytest.mark.parametrize("path", [
        "/", "/imports", "/settings", "/person/hitesh-103334639098",
        "/assets/index-abc123.js", "/favicon.ico",
    ])
    def test_spa_shell_stays_reachable_with_a_key(self, path, monkeypatch,
                                                  reload_auth):
        """Regression: with ACCESS_KEY set, every non-/api path was 401'd, so
        the browser received a JSON error instead of index.html. A deep link
        or a refresh could then never reach the screen that asks for the key.
        The shell holds no private data; all data is behind /api/."""
        monkeypatch.setenv("ACCESS_KEY", "k123")
        monkeypatch.setenv("ALLOW_REMOTE", "true")
        assert reload_auth.check(_req(path))[0] is True, path

    def test_api_is_still_protected_alongside_the_spa(self, monkeypatch,
                                                      reload_auth):
        """Making the shell public must not open the data endpoints."""
        monkeypatch.setenv("ACCESS_KEY", "k123")
        monkeypatch.setenv("ALLOW_REMOTE", "true")
        assert reload_auth.check(_req("/api/people"))[0] is False
        assert reload_auth.check(_req("/api/events"))[0] is False
        assert reload_auth.check(_req("/api/identity/merge"))[0] is False


class TestCorsOrigins:
    def test_localhost_origins_always_present(self):
        from circle.config import Settings
        origins = Settings(cors_origins="").cors_origin_list()
        assert "http://localhost:5173" in origins

    def test_extra_hosted_origin_is_added(self):
        from circle.config import Settings
        origins = Settings(
            cors_origins="https://circle.onrender.com").cors_origin_list()
        assert "https://circle.onrender.com" in origins
        # and never a wildcard: the API serves private content
        assert "*" not in origins

    def test_trailing_slash_and_blanks_are_tolerated(self):
        from circle.config import Settings
        origins = Settings(
            cors_origins="https://a.example/ , ,https://b.example"
        ).cors_origin_list()
        assert "https://a.example" in origins
        assert "https://b.example" in origins
        assert "" not in origins

class TestAuthStatusIsHonest:
    """/api/auth/status must report whether THIS caller's key was accepted.

    Regression: it is a public path, so check() short-circuited to True and the
    endpoint reported key_accepted=true for every caller, including one with no
    key at all. The UI then believed it was connected and waited forever.
    """

    def test_public_path_does_not_impersonate_a_valid_key(
            self, monkeypatch, reload_auth):
        monkeypatch.setenv("ACCESS_KEY", "right-key")
        assert reload_auth.check(_req("/api/auth/status"))[0] is False
        assert reload_auth.check(
            _req("/api/auth/status", "right-key"))[0] is True

    def test_health_stays_public(self, monkeypatch, reload_auth):
        """Only auth/status needs honesty; health must never require a key,
        or the UI cannot tell 'down' from 'locked out'."""
        monkeypatch.setenv("ACCESS_KEY", "right-key")
        assert reload_auth.check(_req("/api/health"))[0] is True
