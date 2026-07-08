"""WebArena login state (css/envs/webarena/auth.py) — the auto_login equivalent.

Pins the two mechanisms (header for shopping_admin, cookie jar for the rest),
the per-(stack,site) storage layout, and the multi-site merge — without any
browser (generate_state's playwright path is exercised live, not in unit tests).
"""
from __future__ import annotations

import json

from css.envs.webarena import auth


def test_header_only_for_admin():
    assert auth.extra_headers(("shopping_admin",)) == {
        auth.ADMIN_AUTO_LOGIN_HEADER: "admin:admin1234"}
    assert auth.extra_headers(("shopping", "reddit", "gitlab")) == {}
    # a multi-site task that includes admin still gets the header
    assert auth.ADMIN_AUTO_LOGIN_HEADER in auth.extra_headers(
        ("gitlab", "shopping_admin"))


def test_state_path_is_per_stack_and_site():
    p1 = auth.state_path("/a", "s1", "gitlab")
    p2 = auth.state_path("/a", "s2", "gitlab")
    assert p1 != p2, "a cookie is only valid against the container that issued it"
    assert p1.endswith("s1.gitlab_state.json")


def test_merged_state_unions_cookies(tmp_path):
    d = str(tmp_path)
    for site, ck in (("shopping", "sh"), ("gitlab", "gl")):
        with open(auth.state_path(d, "s1", site), "w") as f:
            json.dump({"cookies": [{"name": ck}], "origins": []}, f)
    merged = auth.merged_state(d, "s1", ("shopping", "gitlab"))
    names = {c["name"] for c in merged["cookies"]}
    assert names == {"sh", "gl"}


def test_merged_state_skips_header_site_and_missing(tmp_path):
    d = str(tmp_path)
    with open(auth.state_path(d, "s1", "reddit"), "w") as f:
        json.dump({"cookies": [{"name": "rd"}], "origins": []}, f)
    # admin (header) contributes nothing; gitlab file absent -> skipped
    merged = auth.merged_state(d, "s1", ("reddit", "shopping_admin", "gitlab"))
    assert [c["name"] for c in merged["cookies"]] == ["rd"]


def test_merged_state_none_when_nothing_present(tmp_path):
    assert auth.merged_state(str(tmp_path), "s1", ("shopping_admin",)) is None
    assert auth.merged_state(str(tmp_path), "s1", ("gitlab",)) is None


def test_merged_state_tolerates_corrupt_jar(tmp_path):
    d = str(tmp_path)
    with open(auth.state_path(d, "s1", "gitlab"), "w") as f:
        f.write("{ not json")
    assert auth.merged_state(d, "s1", ("gitlab",)) is None


def test_generate_state_rejects_header_site(tmp_path):
    try:
        auth.generate_state("s1", "shopping_admin", "http://x:7780/admin",
                            str(tmp_path))
    except ValueError as e:
        assert "header" in str(e)
    else:
        raise AssertionError("should refuse to cookie-login a header-auth site")


def test_refresh_login_noop_for_header_site(tmp_path):
    # must not raise, must not launch a browser
    auth.refresh_login("s1", "shopping_admin", "http://x:7780/admin",
                       str(tmp_path))


def test_accounts_cover_every_farm_site():
    assert set(auth.COOKIE_SITES) | set(auth.HEADER_SITES) == set(auth.ACCOUNTS)
