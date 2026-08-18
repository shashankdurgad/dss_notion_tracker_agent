import type { AgentEvent, AuthStatus, Conversation } from "./types";

export async function fetchAuthStatus(): Promise<AuthStatus> {
  const response = await fetch("/auth/status", { credentials: "include" });
  if (!response.ok) return { authenticated: false };
  return response.json();
}

export async function logout(): Promise<void> {
  await fetch("/auth/logout", { method: "POST", credentials: "include" });
}

export async function listConversations(): Promise<Conversation[]> {
  const response = await fetch("/conversations", { credentials: "include" });
  if (!response.ok) return [];
  return (await response.json()).conversations ?? [];
}

export async function loadConversation(
  id: string,
): Promise<{ role: "user" | "assistant"; text: string }[]> {
  const response = await fetch(`/conversations/${id}`, {
    credentials: "include",
  });
  if (!response.ok) return [];
  return (await response.json()).messages ?? [];
}

export async function deleteConversation(id: string): Promise<void> {
  await fetch(`/conversations/${id}`, {
    method: "DELETE",
    credentials: "include",
  });
}

export async function renameConversation(
  id: string,
  title: string,
): Promise<void> {
  await fetch(`/conversations/${id}/rename`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

/**
 * POST a JSON body and yield decoded SSE events as they arrive.
 *
 * EventSource can't be used here because it is GET-only and can't send
 * a JSON body, so we parse the `data:` frames off the fetch stream.
 */
async function* streamEvents(
  url: string,
  body: unknown,
): AsyncGenerator<AgentEvent> {
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (response.status === 401) {
    yield { type: "auth_required", message: "Your session expired." };
    return;
  }
  if (!response.ok || !response.body) {
    yield { type: "error", message: `Request failed (${response.status}).` };
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      try {
        yield JSON.parse(line.slice(6)) as AgentEvent;
      } catch {
        // Ignore malformed frames rather than killing the stream.
      }
    }
  }
}

export function sendMessage(
  message: string,
  conversationId: string | null,
): AsyncGenerator<AgentEvent> {
  // A null id asks the server to start a new conversation and tell us its id.
  return streamEvents("/chat", { message, conversation_id: conversationId });
}

export function respondToApproval(
  callId: string,
  decision: "approve" | "reject",
  conversationId: string,
): AsyncGenerator<AgentEvent> {
  return streamEvents("/approve", {
    call_id: callId,
    decision,
    conversation_id: conversationId,
  });
}
