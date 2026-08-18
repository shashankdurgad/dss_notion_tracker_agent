import { useState } from "react";
import type { ToolActivity } from "../types";

const VERB: Record<string, string> = {
  "notion-search": "Searched Notion",
  "notion-fetch": "Read a Notion page",
  "notion-get-comments": "Read comments",
  "notion-get-self": "Checked workspace",
  "notion-create-pages": "Created page(s)",
  "notion-update-page": "Updated page",
  "notion-create-comment": "Posted comment",
};

export default function ToolChip({ activity }: { activity: ToolActivity }) {
  const [open, setOpen] = useState(false);
  const label = VERB[activity.tool] ?? activity.tool;

  const query =
    (activity.arguments?.query as string | undefined) ??
    (activity.arguments?.id as string | undefined) ??
    "";

  return (
    <div className={`chip chip-${activity.status}`}>
      <button
        className="chip-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="chip-dot" aria-hidden="true" />
        <span className="chip-label">
          {label}
          {query && <span className="chip-query"> · {query}</span>}
        </span>
        <span className="chip-toggle">{open ? "hide" : "details"}</span>
      </button>

      {open && (
        <div className="chip-body">
          <div className="chip-section">Arguments</div>
          <pre>{JSON.stringify(activity.arguments ?? {}, null, 2)}</pre>
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
