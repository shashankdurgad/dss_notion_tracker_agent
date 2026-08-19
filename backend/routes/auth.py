"""OAuth routes for both connected services.

Notion is the identity provider: its account key becomes the app's user id.
Google Sheets attaches as a second grant on an already-signed-in session, so
connecting it never mints a competing identity.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..config import PROVIDER_NOTION, PROVIDER_SHEETS
from ..deps import SESSION_COOKIE, AppState, current_user, get_state, optional_user
from ..mcp_client import probe_workspace
from ..oauth import OAuthError, generate_pkce_pair

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


async def _begin(state: AppState, provider: str) -> RedirectResponse:
    """Start an authorization-code + PKCE flow for one provider."""
    try:
        verifier, challenge = generate_pkce_pair()
        oauth_state = secrets.token_urlsafe(32)
        await state.stash_pkce(oauth_state, verifier, provider)
        url = await state.oauth_clients[provider].authorization_url(
            state=oauth_state, code_challenge=challenge
        )
    except OAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=307)


@router.get("/login")
async def login(state: AppState = Depends(get_state)) -> RedirectResponse:
    """Sign in with Notion. This is what establishes the user's identity."""
    return await _begin(state, PROVIDER_NOTION)


@router.get("/google/login")
async def google_login(
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> RedirectResponse:
    """Connect Google Sheets to the current session.

    Requires an existing session: Google is an add-on grant, not a way in.
    """
    if not state.settings.sheets_configured:
        raise HTTPException(
            status_code=503,
            detail="Google Sheets isn't configured on this deployment.",
        )
    return await _begin(state, PROVIDER_SHEETS)


@router.get("/callback")
async def callback(
    request: Request,
    state: AppState = Depends(get_state),
) -> RedirectResponse:
    """Notion's redirect target: exchanges the code and starts the session."""
    frontend = state.settings.frontend_url
    params = request.query_params

    if error := params.get("error"):
        logger.warning("OAuth denied: %s", error)
        return RedirectResponse(f"{frontend}?auth_error={error}", status_code=307)

    returned_state = params.get("state")
    code = params.get("code")
    if not returned_state or not code:
        return RedirectResponse(f"{frontend}?auth_error=missing_params", status_code=307)

    # Single-use lookup; an unknown/replayed state is rejected outright (CSRF).
    stashed = await state.take_pkce(returned_state)
    if stashed is None:
        return RedirectResponse(f"{frontend}?auth_error=bad_state", status_code=307)
    verifier, _provider = stashed

    try:
        tokens = await state.oauth.exchange_code(code=code, code_verifier=verifier)
    except OAuthError as exc:
        logger.warning("Token exchange failed: %s", exc)
        return RedirectResponse(f"{frontend}?auth_error=exchange_failed", status_code=307)

    # Reuse the same id when this Notion account signs in again, so saved
    # chats reattach instead of being stranded under a fresh random id.
    user_id = tokens.account_key or state.new_user_id()
    await state.tokens.save(user_id, tokens, PROVIDER_NOTION)

    try:
        info = await probe_workspace(state.mcp, user_id)
        if info.get("workspace_name"):
            await state.tokens.set_workspace_name(user_id, info["workspace_name"])
        if not info.get("search_available"):
            logger.warning(
                "notion-search unavailable for this workspace "
                "(likely no Notion AI). Falling back to fetch-based lookup."
            )
    except Exception:  # noqa: BLE001 - identity probe is best-effort
        logger.warning("Workspace probe failed", exc_info=True)

    response = RedirectResponse(f"{frontend}?connected=notion", status_code=307)
    response.set_cookie(
        SESSION_COOKIE,
        state.sign(user_id),
        httponly=True,  # token never reachable from JS
        samesite="lax",
        secure=state.settings.oauth_redirect_uri.startswith("https://"),
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    state: AppState = Depends(get_state),
) -> RedirectResponse:
    """Google's redirect target: attaches the grant to the current session."""
    frontend = state.settings.frontend_url
    params = request.query_params

    if error := params.get("error"):
        logger.warning("Google OAuth denied: %s", error)
        return RedirectResponse(
            f"{frontend}?auth_error={error}&service=sheets", status_code=307
        )

    # The session cookie must still be present — without it there's no user to
    # attach the grant to.
    user_id = optional_user(request)
    if not user_id:
        return RedirectResponse(
            f"{frontend}?auth_error=session_expired&service=sheets", status_code=307
        )

    returned_state = params.get("state")
    code = params.get("code")
    if not returned_state or not code:
        return RedirectResponse(
            f"{frontend}?auth_error=missing_params&service=sheets", status_code=307
        )

    stashed = await state.take_pkce(returned_state)
    if stashed is None:
        return RedirectResponse(
            f"{frontend}?auth_error=bad_state&service=sheets", status_code=307
        )
    verifier, _provider = stashed

    try:
        tokens = await state.oauth_clients[PROVIDER_SHEETS].exchange_code(
            code=code, code_verifier=verifier
        )
    except OAuthError as exc:
        logger.warning("Google token exchange failed: %s", exc)
        return RedirectResponse(
            f"{frontend}?auth_error=exchange_failed&service=sheets", status_code=307
        )

    if not tokens.refresh_token:
        # Without a refresh token the connection dies in about an hour. This
        # means access_type=offline/prompt=consent didn't take effect.
        logger.warning("Google returned no refresh token for %s", user_id)

    await state.tokens.save(user_id, tokens, PROVIDER_SHEETS)
    return RedirectResponse(f"{frontend}?connected=sheets", status_code=307)


@router.get("/status")
async def status(
    user_id: str | None = Depends(optional_user),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    """Per-service connection state, driving the onboarding flow."""
    sheets_available = state.settings.sheets_configured

    if not user_id:
        return JSONResponse(
            {
                "authenticated": False,
                "setup_complete": False,
                "connections": {
                    PROVIDER_NOTION: {"connected": False, "required": True},
                    PROVIDER_SHEETS: {
                        "connected": False,
                        "required": True,
                        "available": sheets_available,
                    },
                },
            }
        )

    notion_tokens = await state.tokens.peek(user_id, PROVIDER_NOTION)
    sheets_connected = await state.tokens.connected(user_id, PROVIDER_SHEETS)

    return JSONResponse(
        {
            "authenticated": notion_tokens is not None,
            # Both services are required before the chat is usable. If Sheets
            # isn't configured on this deployment, don't block on it.
            "setup_complete": bool(notion_tokens)
            and (sheets_connected or not sheets_available),
            "workspace_name": notion_tokens.workspace_name if notion_tokens else None,
            "connections": {
                PROVIDER_NOTION: {
                    "connected": notion_tokens is not None,
                    "required": True,
                    "account": notion_tokens.workspace_name if notion_tokens else None,
                },
                PROVIDER_SHEETS: {
                    "connected": sheets_connected,
                    "required": True,
                    "available": sheets_available,
                },
            },
        }
    )


@router.post("/{provider}/disconnect")
async def disconnect(
    provider: str,
    user_id: str = Depends(current_user),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    """Drop one service's grant, leaving the session and chats intact."""
    if provider not in (PROVIDER_NOTION, PROVIDER_SHEETS):
        raise HTTPException(status_code=404, detail="Unknown service.")

    await state.mcp.disconnect(user_id, provider)
    await state.tokens.clear(user_id, provider)

    if provider == PROVIDER_NOTION:
        # Notion is the identity — dropping it ends the session.
        response = JSONResponse({"ok": True, "signed_out": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response
    return JSONResponse({"ok": True, "signed_out": False})


@router.post("/logout")
async def logout(
    user_id: str | None = Depends(optional_user),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    if user_id:
        await state.mcp.disconnect(user_id)
        for provider in (PROVIDER_NOTION, PROVIDER_SHEETS):
            await state.tokens.clear(user_id, provider)
        # Saved chats are deliberately kept: signing out revokes access to
        # the connected services, and they reattach when the same account
        # signs back in. Nothing is readable meanwhile — the tokens are gone,
        # and reaching the chats requires completing OAuth as that account.
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
