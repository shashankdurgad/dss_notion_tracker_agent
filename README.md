# DSS Tracker Agent

A chatbot over UCL Data Science Society's internal records — the **Notion**
workspace (committee minutes, events, sponsors, action items) and **Google
Sheets** trackers. Ask in plain English, and let the agent update them with
your approval.

Built on Notion's and Google's hosted **MCP** servers, powered by **Gemini** or
any OpenAI-compatible model.

---

## How it works

```
Browser (React)
   │  SSE
   ▼
FastAPI backend
   ├── /auth/*   OAuth 2.1 + PKCE, one grant per service
   ├── /chat     agent loop
   └── /approve  resolves a pending write
   │
   ├── Model (Gemini or OpenRouter)
   ├── MCP ──Bearer──▶ https://mcp.notion.com/mcp
   └── MCP ──Bearer──▶ https://sheetsmcp.googleapis.com/mcp/v1
```

Each member connects **their own accounts**, so the agent only ever sees what
that person can already see. Tokens are encrypted on the server and never
reach the browser.

Tools are namespaced per service (`notion__notion-search`,
`sheets__get_values`), which is what routes each call back to the right server
— including for a write parked awaiting approval across a restart.

**Notion is the identity.** Signing in with Notion establishes the session;
Google Sheets attaches to it as a second grant. Both are required before the
chat is usable.

### The approval gate

Automatic function calling is deliberately **disabled**. If the SDK ran tools
for us, an edit would land before anyone could review it. Instead the loop is
driven manually:

- **Reads** (`notion-search`, `notion-fetch`, `get_values`, …) run immediately,
  in parallel.
- **Writes** (`notion-update-page`, `update_values`, `insert_dimension`, …)
  suspend the turn. The target — page, or spreadsheet and cell range — is shown
  in the UI, and nothing is written until you press **Approve**.

Classification fails safe: alongside an explicit list, any tool whose name
contains `create`/`update`/`delete`/`insert`/`set`/`write`/`clear`/`batch`/
`append`/`move`/`duplicate`/`upload`/`remove`/`add` is treated as a write. That
matters because a tool like `insert_dimension` doesn't read as destructive from
its name alone, but adds rows to a live tracker.

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

Open <http://localhost:5173> and follow the setup steps: **Connect Notion**,
then **Connect Google Sheets**.

Notion needs no manual setup — the backend registers itself via Dynamic Client
Registration on first boot. Google does: see
[Connecting Google Sheets](#connecting-google-sheets). Leave
`GOOGLE_CLIENT_ID` blank and the Sheets step is skipped entirely, so the app
still runs Notion-only.

---

## Connecting Google Sheets

Notion registers its own OAuth client automatically. **Google doesn't** — it
has no dynamic client registration, so you create credentials by hand once.

### 1. Create an OAuth client

1. [Google Cloud Console](https://console.cloud.google.com) → create or pick a project
2. **APIs & Services → Library** → enable **Google Sheets API** and **Google Drive API**
3. **OAuth consent screen** → External → fill in app name and support email
4. Add these scopes:
   - `https://www.googleapis.com/auth/drive.file`
   - `https://www.googleapis.com/auth/spreadsheets`
5. Add committee members under **Test users** (see the 100-user cap below)
6. **Credentials → Create credentials → OAuth client ID → Web application**
7. Under **Authorised redirect URIs** add, matching `GOOGLE_REDIRECT_URI` exactly:
   - `http://localhost:8000/auth/google/callback` for local dev
   - `https://<your-app>.vercel.app/auth/google/callback` for production

Copy the client ID and secret into `.env` (and Vercel's env vars):

```
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=https://<your-app>.vercel.app/auth/google/callback
```

> A mismatch between `GOOGLE_REDIRECT_URI` and the URI registered in Console
> is the most common failure, and shows up as `redirect_uri_mismatch` on
> Google's own error page.

### 2. What members will see

Google shows an **"app isn't verified"** warning for these scopes. That's
expected for an internal tool — the onboarding screen says so before sending
people there, and they continue via **Advanced → Go to DSS Assistant**.

Two consequences worth knowing:

- **100-user cap** while unverified. Fine for a committee; verification is
  needed only if DSS opens this up more widely.
- **`drive.file` is deliberately narrow.** The agent can only see spreadsheets
  the user explicitly opens or creates through the app — it **cannot search
  Drive by name**. Paste a spreadsheet link or ID the first time; broader
  access would mean full-Drive scopes and a heavier verification path.

### Which tools the agent gets

`get_values`, `get_spreadsheet` (reads, run automatically) and
`update_values`, `update_formulas`, `update_spreadsheet`, `insert_dimension`
(writes, all gated behind approval).

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
| `GOOGLE_CLIENT_ID` | from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | from Google Cloud Console |
| `GOOGLE_REDIRECT_URI` | `https://<your-app>.vercel.app/auth/google/callback` |

Generate **fresh** secrets for production — don't reuse your local ones. Add
the production `GOOGLE_REDIRECT_URI` to the credential's authorised redirect
URIs in Google Cloud Console, or Sheets consent fails there while working
locally.

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

**Signing out keeps your chats.** They reattach when the same Notion account
signs back in, because the session id is derived from a hash of Notion's
workspace + user id rather than being random per login. Nothing is readable
while signed out — the tokens are deleted, and reaching the chats at all
requires completing OAuth as that same account. Use **Clear all chats** in
the sidebar to delete them deliberately.

**Saved chats and sign-ins both expire after 30 days of inactivity**, matching
Notion's own refresh-token expiry. Up to 50 chats are kept per user; beyond
that the oldest is dropped.

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
| `TOKEN_ENC_KEY` | — | **Required.** Fernet key encrypting stored tokens |
| `OAUTH_REDIRECT_URI` | `http://localhost:8000/auth/callback` | Notion. Must match exactly |
| `FRONTEND_URL` | `http://localhost:5173` | CORS origin + post-auth redirect |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | — | Google OAuth client. Blank hides the Sheets connection |
| `GOOGLE_REDIRECT_URI` | `http://localhost:8000/auth/google/callback` | Must be registered in Google Cloud Console verbatim |
| `REDIS_URL` / `REDIS_TOKEN` | — | Upstash REST credentials. Set both for serverless; blank uses a local file |
| `NOTION_ROOT_PAGE_ID` | — | Fallback root page when search is unavailable |
| `STATE_DIR` | `.state` | Local-only state directory when Redis isn't configured |

---

## Verifying it works

```bash
# Both MCP servers are live and discoverable
curl -s https://mcp.notion.com/.well-known/oauth-authorization-server | python -m json.tool
curl -s https://sheetsmcp.googleapis.com/.well-known/oauth-protected-resource/mcp/v1 | python -m json.tool
```

Then in the app:

1. **Onboarding** → connect Notion, then Google Sheets. Refresh mid-flow: it
   resumes at the step you stopped on. In DevTools → Application → Cookies, the
   session cookie is `HttpOnly` and contains no service token.
2. **Read Notion** → "What's in our latest committee meeting notes?" Tool chips
   appear and the answer cites real page URLs.
3. **Read Sheets** → "What's in the sponsorship tracker?" (paste the sheet link
   the first time — `drive.file` can't search Drive by name).
4. **Write, rejected** → "Mark Acme as confirmed." The approval card names the
   spreadsheet and range. Press **Reject** → **check the sheet: nothing changed.**
5. **Write, approved** → ask again, press **Approve** → the cell updates.
6. **Row insert** → "Add a row to the tracker" must *also* prompt for approval,
   not run straight away.
7. **Cross-service** → "Cross-check the tracker against the sponsor page in
   Notion" → chips from both services in one turn.
8. **Disconnect Sheets** in the sidebar → Sheets tools disappear and onboarding
   asks for it again; Notion stays connected.

---

## Project layout

```
vercel.json       function config (maxDuration)
pyproject.toml    deps + [tool.vercel] entrypoint and frontend build script
backend/
  main.py         app entrypoint, CORS, lifespan
  config.py       env settings
  storage.py      Redis / local-file key-value backends
  oauth.py        BaseOAuthClient + Notion (DCR) and Google (static creds)
  tokens.py       encrypted store, keyed per (user, service)
  mcp_client.py   MCP sessions, one per (user, service)
  agent.py        model loop + approval gate + tool routing
  conversations.py saved-chat index
  llm.py          Gemini / OpenRouter behind one interface
  deps.py         app state, session cookie, PKCE stash
  routes/         auth.py, chat.py
frontend/src/
  App.tsx         chat state machine
  api.ts          SSE client
  services.ts     per-service metadata (names, blurbs, connect paths)
  components/     Onboarding, Connections, Message, ToolChip, ApprovalCard, Sidebar
```
