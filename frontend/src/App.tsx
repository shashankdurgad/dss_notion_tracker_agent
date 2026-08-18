import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchAuthStatus,
  logout,
  resetConversation,
  respondToApproval,
  sendMessage,
} from "./api";
import ApprovalCard from "./components/ApprovalCard";
import LoginGate from "./components/LoginGate";
import Message from "./components/Message";
import type { AgentEvent, AuthStatus, ChatMessage } from "./types";

const SUGGESTIONS = [
  "What did we decide in the last committee meeting?",
  "Which action items are still open?",
  "Summarise our sponsorship conversations",
];

let idCounter = 0;
const nextId = () => `m${++idCounter}`;

export default function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const authError = new URLSearchParams(window.location.search).get(
    "auth_error",
  );

  useEffect(() => {
    fetchAuthStatus().then(setAuth);
    // Strip the ?auth=ok / ?auth_error= noise once read.
    if (window.location.search) {
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  /** Fold a stream of agent events into the trailing assistant message. */
  const consume = useCallback(
    async (stream: AsyncGenerator<AgentEvent>, targetId: string) => {
      const patch = (fn: (m: ChatMessage) => ChatMessage) =>
        setMessages((prev) =>
          prev.map((m) => (m.id === targetId ? fn(m) : m)),
        );

      for await (const event of stream) {
        switch (event.type) {
          case "message":
            patch((m) => ({
              ...m,
              text: m.text ? `${m.text}\n\n${event.text}` : event.text,
            }));
            break;

          case "tool_start":
            patch((m) => ({
              ...m,
              tools: [
                ...m.tools,
                {
                  tool: event.tool,
                  arguments: event.arguments,
                  status: "running",
                },
              ],
            }));
            break;

          case "tool_result":
            patch((m) => {
              const tools = [...m.tools];
              // Complete the most recent running entry for this tool.
              for (let i = tools.length - 1; i >= 0; i--) {
                if (tools[i].tool === event.tool && tools[i].status === "running") {
                  tools[i] = { ...tools[i], result: event.result, status: "done" };
                  break;
                }
              }
              return { ...m, tools };
            });
            break;

          case "tool_rejected":
            patch((m) => ({
              ...m,
              tools: [...m.tools, { tool: event.tool, status: "rejected" }],
            }));
            break;

          case "approval_required":
            patch((m) => ({
              ...m,
              approval: {
                callId: event.call_id,
                tool: event.tool,
                arguments: event.arguments,
              },
            }));
            break;

          case "error":
            patch((m) => ({ ...m, error: event.message }));
            break;

          case "auth_required":
            setAuth({ authenticated: false });
            break;

          case "done":
            break;
        }
      }
    },
    [],
  );

  const submit = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || busy) return;

      const assistantId = nextId();
      setMessages((prev) => [
        ...prev,
        { id: nextId(), role: "user", text: trimmed, tools: [] },
        { id: assistantId, role: "assistant", text: "", tools: [] },
      ]);
      setInput("");
      setBusy(true);
      try {
        await consume(sendMessage(trimmed), assistantId);
      } finally {
        setBusy(false);
      }
    },
    [busy, consume],
  );

  const decide = useCallback(
    async (messageId: string, callId: string, decision: "approve" | "reject") => {
      setBusy(true);
      // Clear the card immediately so it can't be double-submitted.
      setMessages((prev) =>
        prev.map((m) => (m.id === messageId ? { ...m, approval: undefined } : m)),
      );
      try {
        await consume(respondToApproval(callId, decision), messageId);
      } finally {
        setBusy(false);
      }
    },
    [consume],
  );

  const startOver = async () => {
    await resetConversation();
    setMessages([]);
  };

  const signOut = async () => {
    await logout();
    setAuth({ authenticated: false });
    setMessages([]);
  };

  if (auth === null) {
    return <div className="loading">Loading…</div>;
  }
  if (!auth.authenticated) {
    return <LoginGate authError={authError} />;
  }

  return (
    <div className="app">
      <header className="header">
        <div>
          <h1>DSS Notion Assistant</h1>
          {auth.workspace_name && (
            <span className="workspace">{auth.workspace_name}</span>
          )}
        </div>
        <div className="header-actions">
          <button onClick={startOver} disabled={busy}>
            New chat
          </button>
          <button onClick={signOut}>Sign out</button>
        </div>
      </header>

      <main className="thread">
        {messages.length === 0 && (
          <div className="empty">
            <p>Ask anything about the society's Notion workspace.</p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => submit(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message) => (
          <Message key={message.id} message={message}>
            {message.approval && (
              <ApprovalCard
                approval={message.approval}
                busy={busy}
                onDecide={(decision) =>
                  decide(message.id, message.approval!.callId, decision)
                }
              />
            )}
          </Message>
        ))}

        {busy && <div className="thinking">Working…</div>}
        <div ref={bottomRef} />
      </main>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit(input);
        }}
      >
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit(input);
            }
          }}
          placeholder="Ask about meetings, events, sponsors, action items…"
          rows={1}
          disabled={busy}
          aria-label="Message"
        />
        <button type="submit" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
