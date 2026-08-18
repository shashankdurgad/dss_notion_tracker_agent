import type { ChatMessage } from "../types";
import ToolChip from "./ToolChip";

/** Linkify bare URLs so Notion references are clickable. */
function renderText(text: string) {
  const parts = text.split(/(https?:\/\/[^\s)>\]]+)/g);
  return parts.map((part, i) =>
    /^https?:\/\//.test(part) ? (
      <a key={i} href={part} target="_blank" rel="noopener noreferrer">
        {part}
      </a>
    ) : (
      <span key={i}>{part}</span>
    ),
  );
}

export default function Message({
  message,
  children,
}: {
  message: ChatMessage;
  children?: React.ReactNode;
}) {
  return (
    <div className={`msg msg-${message.role}`}>
      <div className="msg-role">
        {message.role === "user" ? "You" : "Assistant"}
      </div>

      {message.tools.length > 0 && (
        <div className="msg-tools">
          {message.tools.map((tool, i) => (
            <ToolChip key={`${tool.tool}-${i}`} activity={tool} />
          ))}
        </div>
      )}

      {message.text && <div className="msg-text">{renderText(message.text)}</div>}

      {message.error && (
        <div className="msg-error" role="alert">
          {message.error}
        </div>
      )}

      {children}
    </div>
  );
}
