"""OAuth routes: login, callback, status, logout."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..deps import SESSION_COOKIE, AppState, get_state, optional_user
from ..mcp_client import probe_workspace
from ..oauth import OAuthError, generate_pkce_pair

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login")
async def login(state: AppState = Depends(get_state)) -> RedirectResponse:
    """Kick off the OAuth authorization-code + PKCE flow."""
    try:
        verifier, challenge = generate_pkce_pair()
        oauth_state = secrets.token_urlsafe(32)
        await state.stash_pkce(oauth_state, verifier)
        url = await state.oauth.authorization_url(
            state=oauth_state, code_challenge=challenge
        )
    except OAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=307)


@router.get("/callback")
async def callback(
    request: Request,
    state: AppState = Depends(get_state),
) -> RedirectResponse:
    """Handle the redirect back from Notion and exchange the code for tokens."""
    params = request.query_params
    frontend = state.settings.frontend_url

    if error := params.get("error"):
        logger.warning("OAuth denied: %s", error)
        return RedirectResponse(f"{frontend}?auth_error={error}", status_code=307)

    returned_state = params.get("state")
    code = params.get("code")
    if not returned_state or not code:
        return RedirectResponse(f"{frontend}?auth_error=missing_params", status_code=307)

    # Single-use lookup; an unknown/replayed state is rejected outright (CSRF).
    verifier = await state.take_pkce(returned_state)
    if verifier is None:
        return RedirectResponse(f"{frontend}?auth_error=bad_state", status_code=307)

    try:
        tokens = await state.oauth.exchange_code(code=code, code_verifier=verifier)
    except OAuthError as exc:
        logger.warning("Token exchange failed: %s", exc)
        return RedirectResponse(f"{frontend}?auth_error=exchange_failed", status_code=307)

    user_id = state.new_user_id()
    await state.tokens.save(user_id, tokens)

    # Identify the workspace so the UI can show which Notion this is.
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

    response = RedirectResponse(f"{frontend}?auth=ok", status_code=307)
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


@router.get("/status")
async def status(
    user_id: str | None = Depends(optional_user),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    if not user_id:
        return JSONResponse({"authenticated": False})

    tokens = await state.tokens.peek(user_id)
    if tokens is None:
        return JSONResponse({"authenticated": False})

    return JSONResponse(
        {
            "authenticated": True,
            "workspace_name": tokens.workspace_name,
        }
    )


@router.post("/logout")
async def logout(
    user_id: str | None = Depends(optional_user),
    state: AppState = Depends(get_state),
) -> JSONResponse:
    if user_id:
        await state.mcp.disconnect(user_id)
        await state.tokens.clear(user_id)
        # Signing out clears saved chats too — they're tied to this session's
        # Notion access, and leaving them behind on a shared machine would
        # expose workspace content to the next person to sign in.
        for conversation_id in await state.conversations.clear(user_id):
            await state.agent.reset(user_id, conversation_id)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
