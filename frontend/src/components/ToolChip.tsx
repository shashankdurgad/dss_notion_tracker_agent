import { useState } from "react";
import type { ToolActivity } from "../types";

/** Tool names reach the UI prefixed (`sheets__get_values`). */
const VERB: Record<string, string> = {
  "notion__notion-search": "Searched Notion",
  "notion__notion-fetch": "Read a Notion page",
  "notion__notion-get-comments": "Read comments",
  "notion__notion-get-self": "Checked workspace",
  "notion__notion-create-pages": "Created page(s)",
  "notion__notion-update-page": "Updated page",
  "notion__notion-create-comment": "Posted comment",
  "sheets__get_values": "Read spreadsheet cells",
  "sheets__get_spreadsheet": "Opened a spreadsheet",
  "sheets__update_values": "Updated cells",
  "sheets__update_formulas": "Updated formulas",
  "sheets__update_spreadsheet": "Changed spreadsheet",
  "sheets__insert_dimension": "Inserted rows/columns",
};

const ICON: Record<string, string> = { notion: "📄", sheets: "📊" };

function describe(tool: string): { icon: string; label: string } {
  const [prefix, ...rest] = tool.split("__");
  const bare = rest.join("__") || tool;
  return {
    icon: ICON[prefix] ?? "🔧",
    // Fall back to the bare tool name so a newly added server-side tool
    // reads sensibly instead of leaking the prefix.
    label: VERB[tool] ?? bare.replace(/[-_]/g, " "),
  };
}

export default function ToolChip({ activity }: { activity: ToolActivity }) {
  const [open, setOpen] = useState(false);
  const { icon, label } = describe(activity.tool);

  const args = activity.arguments ?? {};
  // Show the most identifying argument inline — the search term, the page,
  // or which part of which spreadsheet.
  const detail =
    (args.query as string | undefined) ??
    (args.range as string | undefined) ??
    (args.id as string | undefined) ??
    (args.spreadsheetId as string | undefined) ??
    "";

  return (
    <div className={`chip chip-${activity.status}`}>
      <button
        className="chip-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="chip-dot" aria-hidden="true" />
        <span className="chip-icon" aria-hidden="true">
          {icon}
        </span>
        <span className="chip-label">
          {label}
          {detail && <span className="chip-query"> · {detail}</span>}
        </span>
        <span className="chip-toggle">{open ? "hide" : "details"}</span>
      </button>

      {open && (
        <div className="chip-body">
          <div className="chip-section">Arguments</div>
          <pre>{JSON.stringify(args, null, 2)}</pre>
          {activity.result && (
            <>
              <div className="chip-section">Result</div>
              <pre>{activity.result.slice(0, 4000)}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}
