import type { PendingApproval } from "../types";

interface Props {
  approval: PendingApproval;
  busy: boolean;
  onDecide: (decision: "approve" | "reject") => void;
}

/** Human-friendly label for what the agent is about to do. */
const ACTION_LABEL: Record<string, string> = {
  "notion__notion-create-pages": "Create page(s) in Notion",
  "notion__notion-update-page": "Update an existing Notion page",
  "notion__notion-create-comment": "Post a comment in Notion",
  "sheets__update_values": "Change cells in a spreadsheet",
  "sheets__update_formulas": "Change formulas in a spreadsheet",
  "sheets__update_spreadsheet": "Modify a spreadsheet's structure",
  "sheets__insert_dimension": "Insert rows or columns into a spreadsheet",
};

/** The bits of a write worth reading before approving it. */
function targets(approval: PendingApproval): [string, string][] {
  const a = approval.arguments as Record<string, unknown>;
  const rows: [string, string][] = [];
  const push = (label: string, value: unknown) => {
    if (typeof value === "string" && value) rows.push([label, value]);
    else if (typeof value === "number") rows.push([label, String(value)]);
  };

  push("Spreadsheet", a.spreadsheetId);
  push("Range", a.range);
  push("Sheet", a.sheetId);
  push("Page", a.page_id ?? a.pageId ?? a.id);
  if (typeof a.dimension === "string") {
    const from = a.startIndex;
    const to = a.endIndex;
    rows.push([
      "Inserting",
      typeof from === "number" && typeof to === "number"
        ? `${Number(to) - Number(from)} ${String(a.dimension).toLowerCase()}`
        : String(a.dimension).toLowerCase(),
    ]);
  }
  return rows;
}

export default function ApprovalCard({ approval, busy, onDecide }: Props) {
  const label = ACTION_LABEL[approval.tool] ?? `Run ${approval.tool}`;
  const rows = targets(approval);

  return (
    <div className="approval" role="group" aria-label="Approval required">
      <div className="approval-head">
        <span className="approval-badge">Needs your approval</span>
        <strong>{label}</strong>
      </div>

      <p className="approval-hint">
        Review the change below. Nothing is written until you approve it.
      </p>

      {rows.length > 0 && (
        <dl className="approval-targets">
          {rows.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}

      <details className="approval-raw">
        <summary>Exact payload</summary>
        <pre className="approval-payload">
          {JSON.stringify(approval.arguments, null, 2)}
        </pre>
      </details>

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
