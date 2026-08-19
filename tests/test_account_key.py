"""Saved chats reattach on re-login, and never cross accounts.

The account key is what makes "don't delete chats on sign-out" actually
useful: without a stable id, a returning user gets a fresh random identity
and their chats are stranded in storage.
"""

from __future__ import annotations

from backend.oauth import TokenSet, _account_key


def token_payload(**overrides):
    base = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_in": 3600,
        "user_id": "user-abc",
        "workspace_id": "ws-1",
    }
    base.update(overrides)
    return base


def test_same_account_yields_same_key():
    """Signing out and back in must land on the same id."""
    first = TokenSet.from_token_response(token_payload())
    second = TokenSet.from_token_response(token_payload(access_token="at2"))
    assert first.account_key == second.account_key
    assert first.account_key


def test_different_users_get_different_keys():
    a = TokenSet.from_token_response(token_payload(user_id="user-a"))
    b = TokenSet.from_token_response(token_payload(user_id="user-b"))
    assert a.account_key != b.account_key


def test_same_user_different_workspace_is_separate():
    """Two workspaces are separate contexts; chats must not merge."""
    a = TokenSet.from_token_response(token_payload(workspace_id="ws-1"))
    b = TokenSet.from_token_response(token_payload(workspace_id="ws-2"))
    assert a.account_key != b.account_key


def test_key_does_not_leak_raw_notion_ids():
    key = _account_key(token_payload())
    assert "user-abc" not in key and "ws-1" not in key
    assert len(key) == 32


def test_nested_owner_shape_supported():
    """Notion also returns identity nested under `owner`."""
    key = _account_key(
        {"workspace_id": "ws-1", "owner": {"user": {"id": "user-abc"}}}
    )
    assert key == _account_key(token_payload())


def test_missing_identity_falls_back_to_none():
    """No identity fields must not crash sign-in; caller uses a random id."""
    assert _account_key({"access_token": "at"}) is None
    assert TokenSet.from_token_response({"access_token": "at"}).account_key is None


def test_account_key_survives_storage_roundtrip():
    original = TokenSet.from_token_response(token_payload())
    restored = TokenSet.from_dict(original.to_dict())
    assert restored.account_key == original.account_key
