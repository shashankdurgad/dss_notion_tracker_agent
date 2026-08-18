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

## Deploying to Vercel

Free on Vercel Hobby + Upstash Redis free tier.

### Why Redis is needed

Vercel runs the backend as **stateless functions**: the filesystem is wiped
between requests, and two requests from the same person can land on different
instances. Four things must therefore live outside the process — Notion tokens,
the registered OAuth client, PKCE state (login starts on one instance and the
callback lands on another), and chat history.

"Shared" means shared **between server instances, never between users**. Every
record is keyed by user (`session:<id>`, `chat:<id>`), and a user's signed
cookie can only address their own keys. Notion tokens are encrypted with
`TOKEN_ENC_KEY` before they're written, so the Redis host can't read them —
and Notion's own permissions still apply underneath.

Leave `REDIS_URL`/`REDIS_TOKEN` blank locally and it uses a file instead. Same
code either way.

### 1. Create a Redis database

At [console.upstash.com](https://console.upstash.com), create a Redis database
and copy the **REST URL** and **REST token** (not the `redis://` connection
string — the REST API is used because serverless can't hold a connection pool
open).

### 2. Deploy

```bash
npm i -g vercel
vercel            # first deploy, note the URL it gives you
```

### 3. Set environment variables

In the Vercel dashboard → Settings → Environment Variables:

| Variable | Value |
|---|---|
| `GEMINI_API_KEY` | your Gemini key |
| `SESSION_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `TOKEN_ENC_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `REDIS_URL` | Upstash REST URL |
| `REDIS_TOKEN` | Upstash REST token |
| `OAUTH_REDIRECT_URI` | `https://<your-app>.vercel.app/auth/callback` |
| `FRONTEND_URL` | `https://<your-app>.vercel.app` |

Generate **fresh** secrets for production — don't reuse your local ones.

### 4. Redeploy

```bash
vercel --prod
```

Environment variables are only picked up on a new build, so this step is
required after step 3.

> **The redirect URI must match exactly**, including `https://` and no trailing
> slash. A mismatch is the most common cause of a failed sign-in, and it
> surfaces as `auth_error=exchange_failed`. Changing `OAUTH_REDIRECT_URI` later
> re-registers the OAuth client automatically.

### Preview deployments

Each Vercel preview gets its own URL, which won't match `OAUTH_REDIRECT_URI`,
so **sign-in only works on the production domain** unless you set the variables
per-environment. That's usually what you want.

---

## Switching model provider

Gemini's free tier rate-limits under real use. To fall back to OpenRouter,
set three variables (locally in `.env`, or in Vercel's dashboard):

```
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=nvidia/nemotron-3-super-120b-a12b:free
```

Nothing else changes — the agent loop is provider-agnostic, and stored chat
history is provider-neutral, so switching mid-conversation is safe.

**Stick to `nvidia/nemotron-3-super-120b-a12b:free`.** It's the default, and
the model this app is tuned against. Free models vary wildly at tool calling,
and the agent is nothing but tool calls — a model that can't call tools can't
read Notion at all.

Tested against this app's real system prompt on 2026-08-18:

| Model | Result |
|---|---|
| **`nvidia/nemotron-3-super-120b-a12b:free`** | ✅ **default** — tools, facts and URLs correct |
| `nvidia/nemotron-3.5-lightning:free` | ✅ also works, if the default is delisted |
| `cohere/north-mini-code:free` | ⚠️ calls tools but ignored the tool result |
| `google/gemma-4-31b-it:free` | ❌ 429 from the upstream provider |
| `google/gemma-4-26b-a4b-it:free` | ❌ 429 from the upstream provider |

Only change `OPENROUTER_MODEL` if the default gets delisted — OpenRouter's
free list turns over regularly. To find current tool-capable free models:

```bash
curl -s https://openrouter.ai/api/v1/models | python3 -c \
"import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data'] \
if m.get('pricing',{}).get('prompt') in ('0','0.0') \
and 'tools' in (m.get('supported_parameters') or [])]"
```

> ⚠️ **Free OpenRouter accounts get 50 requests/day, 20/minute.** One question
> costs several requests (search → fetch → answer), so that's roughly 12–20
> questions per day across the whole committee — potentially *tighter* than
> Gemini's limit. A one-time $10 credit purchase raises it to 1000/day
> permanently. Free models also occasionally emit malformed citations
> instead of Notion URLs; observed intermittently, not on every reply.

---

## Things worth knowing

**`notion-search` requires Notion AI.** If the DSS workspace isn't on a plan
with Notion AI, the search tool won't be offered by the MCP server. The backend
detects this at sign-in and logs a warning; set `NOTION_ROOT_PAGE_ID` in `.env`
so the agent can navigate from a known root page instead.

**Notion MCP is Beta.** Tool names and schemas may change. Tool declarations are
built dynamically from `list_tools()` at runtime, so a renamed tool won't break
the app — but the friendly labels in the UI may fall back to raw tool names.

**Chats are saved and listed in the sidebar.** Each is stored separately and
keyed by user, so switching between them keeps their histories apart. Titles
are generated by the model after the first reply — one extra request per new
chat, which matters on a capped free tier — falling back to the opening
message if that fails or looks wrong.

**Saved chats and sign-ins both expire after 30 days of inactivity**, matching
Notion's own refresh-token expiry. Up to 50 chats are kept per user; beyond
that the oldest is dropped. Signing out deletes saved chats, so a shared
machine doesn't leak workspace content to the next person.

**Refresh races across instances.** The per-user refresh mutex only serializes
within one process, so on serverless two instances could in principle refresh
at once. The window is tiny — a refresh happens once per ~8 hours, 5 minutes
before expiry — and the loser simply re-reads the winner's token, so it
self-heals. Worth knowing if you ever see an unexpected re-login.

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
| `REDIS_URL` / `REDIS_TOKEN` | — | Upstash REST credentials. Set both for serverless; blank uses a local file |
| `NOTION_ROOT_PAGE_ID` | — | Fallback root page when search is unavailable |
| `STATE_DIR` | `.state` | Local-only state directory when Redis isn't configured |

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
vercel.json       function config (maxDuration)
pyproject.toml    deps + [tool.vercel] entrypoint and frontend build script
backend/
  main.py         app entrypoint, CORS, lifespan
  config.py       env settings
  storage.py      Redis / local-file key-value backends
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
