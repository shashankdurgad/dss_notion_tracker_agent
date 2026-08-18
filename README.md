# DSS Notion Tracker Agent

A chatbot over UCL Data Science Society's Notion workspace. Ask questions about
committee minutes, events, sponsors and action items in plain English — and let
the agent create or update pages, with your approval.

Built on Notion's hosted **MCP** server and **Gemini**.

---

## How it works

```
Browser (React)
   │  SSE
   ▼
FastAPI backend
   ├── /auth/*   OAuth 2.1 (discovery → DCR → PKCE → tokens)
   ├── /chat     Gemini agent loop
   └── /approve  resolves a pending write
   │
   ├── Gemini (google-genai)
   └── MCP client ──Bearer──▶ https://mcp.notion.com/mcp
```

Each member signs in with **their own Notion account**, so the agent can only
see pages that person already has access to. Tokens live encrypted on the
server and are never exposed to the browser.

### The approval gate

Automatic function calling is deliberately **disabled**. If the SDK ran tools
for us, a page edit would land before anyone could review it. Instead the loop
is driven manually:

- **Reads** (`notion-search`, `notion-fetch`, …) run immediately, in parallel.
- **Writes** (`notion-create-pages`, `notion-update-page`, `notion-create-comment`, …)
  suspend the turn. The exact payload is shown in the UI and nothing reaches
  Notion until you press **Approve**.

A rejected write is reported back to the model as declined, so it responds
sensibly instead of assuming success. Tool classification also fails safe: any
tool whose name contains `create`/`update`/`delete`/`append`/`move`/`duplicate`/`upload`
is treated as a write even if it's new and not on the explicit list.

---

## Setup

### 1. Requirements

- Python 3.11+
- Node 18+
- A Gemini API key — <https://aistudio.google.com/apikey>
- A Notion account with access to the DSS workspace

### 2. Configure

```bash
cp .env.example .env
```

Fill in `.env`. Generate the two secrets:

```bash
# SESSION_SECRET
python -c "import secrets; print(secrets.token_urlsafe(32))"

# TOKEN_ENC_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`.env` is gitignored — keep it that way.

### 3. Install and run

```bash
# Backend
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/uvicorn backend.main:app --reload --port 8000

# Frontend (second terminal)
cd frontend && npm install && npm run dev
```

Open <http://localhost:5173> and click **Connect Notion**.

No Notion integration needs creating by hand — the backend registers itself
with Notion's MCP server automatically via Dynamic Client Registration on first
boot, and caches the resulting `client_id` in `.state/`.

---

## Things worth knowing

**`notion-search` requires Notion AI.** If the DSS workspace isn't on a plan
with Notion AI, the search tool won't be offered by the MCP server. The backend
detects this at sign-in and logs a warning; set `NOTION_ROOT_PAGE_ID` in `.env`
so the agent can navigate from a known root page instead.

**Notion MCP is Beta.** Tool names and schemas may change. Tool declarations are
built dynamically from `list_tools()` at runtime, so a renamed tool won't break
the app — but the friendly labels in the UI may fall back to raw tool names.

**Sessions are in-memory.** Conversation history resets when the backend
restarts. Notion tokens survive (they're persisted encrypted under `.state/`),
so you won't have to sign in again. Swap in Redis or SQLite if that becomes
annoying.

**Token lifecycle.** Access tokens last ~8 hours and refresh automatically 5
minutes before expiry. Refresh tokens rotate on every use, and refreshes are
serialized per user with a mutex — replaying a rotated token makes Notion revoke
the entire grant. If a grant does die (`invalid_grant`), tokens are cleared and
you're prompted to sign in again; it is never retried.

---

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | **Required.** Gemini API key |
| `GEMINI_MODEL` | `gemini-3.7-flash` | Model for the agent loop |
| `SESSION_SECRET` | — | **Required.** Signs the session cookie |
| `TOKEN_ENC_KEY` | — | **Required.** Fernet key encrypting Notion tokens |
| `OAUTH_REDIRECT_URI` | `http://localhost:8000/auth/callback` | Must match exactly |
| `FRONTEND_URL` | `http://localhost:5173` | CORS origin + post-auth redirect |
| `NOTION_ROOT_PAGE_ID` | — | Fallback root page when search is unavailable |
| `STATE_DIR` | `.state` | Encrypted tokens + registered OAuth client |

---

## Verifying it works

```bash
# OAuth endpoints are live and discoverable
curl -s https://mcp.notion.com/.well-known/oauth-authorization-server | python -m json.tool
```

Then in the app:

1. **Sign in** → you land back authenticated with the workspace name in the header.
   Check DevTools → Application → Cookies: the session cookie is `HttpOnly` and
   contains no Notion token.
2. **Read** → "What's in our latest committee meeting notes?" Tool chips appear
   and the answer cites real Notion URLs.
3. **Write, rejected** → "Add an action item to the meeting notes." An approval
   card shows the exact payload. Press **Reject** → nothing changes in Notion.
4. **Write, approved** → ask again, press **Approve** → the page updates. Verify
   in Notion.

---

## Project layout

```
backend/
  main.py         app entrypoint, CORS, lifespan
  config.py       env settings
  oauth.py        discovery, DCR, PKCE, token exchange/refresh
  tokens.py       encrypted store + per-user refresh mutex
  mcp_client.py   Notion MCP session manager
  agent.py        Gemini loop + approval gate
  deps.py         app state, session cookie, PKCE stash
  routes/         auth.py, chat.py
frontend/src/
  App.tsx         chat state machine
  api.ts          SSE client
  components/     LoginGate, Message, ToolChip, ApprovalCard
```
