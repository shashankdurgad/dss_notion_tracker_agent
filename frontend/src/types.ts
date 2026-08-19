export type AgentEvent =
  | { type: "conversation"; conversation_id: string }
  | { type: "conversation_title"; title: string }
  | { type: "message"; text: string }
  | { type: "tool_start"; tool: string; arguments: Record<string, unknown> }
  | { type: "tool_result"; tool: string; result: string }
  | { type: "tool_rejected"; tool: string }
  | {
      type: "approval_required";
      call_id: string;
      tool: string;
      arguments: Record<string, unknown>;
    }
  | { type: "done" }
  | { type: "error"; message: string }
  | { type: "auth_required"; message: string };

export interface ToolActivity {
  tool: string;
  arguments?: Record<string, unknown>;
  result?: string;
  status: "running" | "done" | "rejected";
}

export interface PendingApproval {
  callId: string;
  tool: string;
  arguments: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  tools: ToolActivity[];
  approval?: PendingApproval;
  error?: string;
}

export type ServiceId = "notion" | "sheets";

export interface ServiceConnection {
  connected: boolean;
  required: boolean;
  /** False when the deployment has no credentials configured for it. */
  available?: boolean;
  account?: string | null;
}

export interface AuthStatus {
  authenticated: boolean;
  /** Every required service connected — the gate for reaching the chat. */
  setup_complete: boolean;
  workspace_name?: string | null;
  connections: Record<ServiceId, ServiceConnection>;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
}
