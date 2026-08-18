import type { PendingApproval } from "../types";

interface Props {
  approval: PendingApproval;
  busy: boolean;
  onDecide: (decision: "approve" | "reject") => void;
}

/** Human-friendly label for what the agent is about to do. */
const ACTION_LABEL: Record<string, string> = {
  "notion-create-pages": "Create page(s) in Notion",
  "notion-update-page": "Update an existing Notion page",
  "notion-create-comment": "Post a comment in Notion",
};

export default function ApprovalCard({ approval, busy, onDecide }: Props) {
  const label = ACTION_LABEL[approval.tool] ?? `Run ${approval.tool}`;

  return (
    <div className="approval" role="group" aria-label="Approval required">
      <div className="approval-head">
        <span className="approval-badge">Needs your approval</span>
        <strong>{label}</strong>
      </div>

      <p className="approval-hint">
        Review the exact change below. Nothing is written to Notion until you
        approve it.
      </p>

      <pre className="approval-payload">
        {JSON.stringify(approval.arguments, null, 2)}
      </pre>

      <div className="approval-actions">
        <button
          className="btn-approve"
          onClick={() => onDecide("approve")}
          disabled={busy}
        >
          {busy ? "Applying…" : "Approve"}
        </button>
        <button
          className="btn-reject"
          onClick={() => onDecide("reject")}
          disabled={busy}
        >
          Reject
        </button>
      </div>
    </div>
  );
}
