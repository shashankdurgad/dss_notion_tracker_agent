"""Two MCP servers behind one agent: routing, isolation and write gating."""

from __future__ import annotations

import pytest

from backend.agent import _split_tool_name
from backend.config import PROVIDER_NOTION, PROVIDER_SHEETS
from backend.oauth import GoogleOAuthClient, NotionOAuthClient
from tests.conftest import (
    FakeMCP,
    ScriptedProvider,
    call_response,
    make_agent,
    make_settings,
    text_response,
)


# ---------------------------------------------------------------- routing


def test_split_tool_name():
    assert _split_tool_name("sheets__get_values") == ("sheets", "get_values")
    assert _split_tool_name("notion__notion-search") == ("notion", "notion-search")
    # Unprefixed names fall back to Notion so writes parked before prefixing
    # existed remain resolvable.
    assert _split_tool_name("notion-search") == ("notion", "notion-search")
    # An unknown prefix is not a provider — treat the whole thing as a name.
    assert _split_tool_name("other__thing") == ("notion", "other__thing")


@pytest.mark.asyncio
async def test_tools_from_both_servers_are_namespaced():
    mcp = FakeMCP(providers=["notion", "sheets"])
    agent, _ = make_agent(ScriptedProvider([text_response("hi")]), mcp=mcp)

    names = {spec.name for spec in await agent._tools("u1")}

    assert "notion__notion-search" in names
    assert "sheets__get_values" in names
    # No bare names reach the model, so two servers can never collide.
    assert all("__" in n for n in names)


@pytest.mark.asyncio
async def test_calls_route_to_the_owning_server():
    mcp = FakeMCP(providers=["notion", "sheets"])
    agent, _ = make_agent(
        ScriptedProvider(
            [
                call_response("sheets__get_values", {"spreadsheetId": "s1"}),
                text_response("Done."),
            ]
        ),
        mcp=mcp,
    )

    [e async for e in agent.send("u1", "c1", "read the tracker")]

    assert mcp.routed == [("sheets", "get_values", {"spreadsheetId": "s1"})]


@pytest.mark.asyncio
async def test_one_server_failing_does_not_hide_the_other():
    """A broken Sheets connection must not blank out Notion's tools."""

    class HalfBroken(FakeMCP):
        async def list_tools(self, user_id, provider="notion"):
            if provider == "sheets":
                raise RuntimeError("sheets is down")
            return await super().list_tools(user_id, provider)

    mcp = HalfBroken(providers=["notion", "sheets"])
    agent, _ = make_agent(ScriptedProvider([text_response("hi")]), mcp=mcp)

    names = {spec.name for spec in await agent._tools("u1")}

    assert any(n.startswith("notion__") for n in names)
    assert not any(n.startswith("sheets__") for n in names)


@pytest.mark.asyncio
async def test_only_connected_providers_contribute_tools():
    mcp = FakeMCP(providers=["notion"])
    agent, _ = make_agent(ScriptedProvider([text_response("hi")]), mcp=mcp)

    names = {spec.name for spec in await agent._tools("u1")}

    assert names and not any(n.startswith("sheets__") for n in names)


# ------------------------------------------------------------ write gating


@pytest.mark.asyncio
async def test_sheets_write_pauses_for_approval():
    """The whole point: no unapproved edit reaches a real tracker."""
    mcp = FakeMCP(providers=["notion", "sheets"])
    agent, _ = make_agent(
        ScriptedProvider(
            [
                call_response(
                    "sheets__update_values",
                    {"spreadsheetId": "s1", "range": "A2:B2", "values": [["x"]]},
                )
            ]
        ),
        mcp=mcp,
    )

    events = [e async for e in agent.send("u1", "c1", "mark Acme confirmed")]

    assert any(e["type"] == "approval_required" for e in events)
    assert mcp.routed == []  # nothing executed


@pytest.mark.asyncio
async def test_insert_dimension_pauses_for_approval():
    """insert_dimension adds rows and must not slip through as a read."""
    mcp = FakeMCP(providers=["sheets"])
    agent, _ = make_agent(
        ScriptedProvider(
            [
                call_response(
                    "sheets__insert_dimension",
                    {"spreadsheetId": "s1", "sheetId": 0, "dimension": "ROWS",
                     "startIndex": 1, "endIndex": 2},
                )
            ]
        ),
        mcp=mcp,
    )

    events = [e async for e in agent.send("u1", "c1", "add a row")]

    assert any(e["type"] == "approval_required" for e in events)
    assert mcp.routed == []


@pytest.mark.asyncio
async def test_approved_sheets_write_routes_correctly():
    mcp = FakeMCP(providers=["sheets"])
    agent, _ = make_agent(
        ScriptedProvider(
            [
                call_response("sheets__update_values", {"spreadsheetId": "s1"}),
                text_response("Updated."),
            ]
        ),
        mcp=mcp,
    )
    events = [e async for e in agent.send("u1", "c1", "update it")]
    call_id = next(e["call_id"] for e in events if e["type"] == "approval_required")

    [e async for e in agent.resume("u1", "c1", call_id, "approve")]

    assert mcp.routed == [("sheets", "update_values", {"spreadsheetId": "s1"})]


@pytest.mark.asyncio
async def test_sheets_reads_run_without_approval():
    mcp = FakeMCP(providers=["sheets"])
    agent, _ = make_agent(
        ScriptedProvider(
            [
                call_response("sheets__get_values", {"spreadsheetId": "s1"}),
                text_response("Here you go."),
            ]
        ),
        mcp=mcp,
    )

    events = [e async for e in agent.send("u1", "c1", "what's in the tracker")]

    assert not any(e["type"] == "approval_required" for e in events)
    assert mcp.routed == [("sheets", "get_values", {"spreadsheetId": "s1"})]


# ------------------------------------------------------------ oauth clients


def test_google_client_config():
    settings = make_settings()
    settings.google_client_id = "gid"
    settings.google_client_secret = "gsec"
    client = GoogleOAuthClient(settings)

    assert client.provider == PROVIDER_SHEETS
    # Google's protected-resource metadata is path-scoped, not at the root.
    assert client.resource_metadata_path.endswith("/mcp/v1")
    # Without these Google returns no refresh token.
    assert client.extra_authorize_params() == {
        "access_type": "offline",
        "prompt": "consent",
    }
    # drive.file keeps the agent to sheets the user explicitly picks.
    assert "drive.file" in client.scope_for(None)  # type: ignore[arg-type]
    assert "drive.readonly" not in client.scope_for(None)  # type: ignore[arg-type]


def test_notion_client_owns_identity():
    assert NotionOAuthClient.provider == PROVIDER_NOTION
    payload = {"user_id": "u", "workspace_id": "w"}
    assert NotionOAuthClient.identity_from(payload)
    # Google must not mint a competing identity.
    assert GoogleOAuthClient.identity_from(payload) is None


@pytest.mark.asyncio
async def test_google_client_without_credentials_explains_itself():
    from backend.oauth import OAuthError

    client = GoogleOAuthClient(make_settings())
    with pytest.raises(OAuthError, match="GOOGLE_CLIENT_ID"):
        await client.ensure_registered()


def test_sheets_configured_flag():
    settings = make_settings()
    assert not settings.sheets_configured
    settings.google_client_id = "gid"
    settings.google_client_secret = "gsec"
    assert settings.sheets_configured
