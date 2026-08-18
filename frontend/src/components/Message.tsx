import { renderMarkdown } from "../markdown";
import type { ChatMessage } from "../types";
import ToolChip from "./ToolChip";

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

      {message.text && (
        <div className="msg-text">
          {/* Only assistant replies are Markdown. A user's own text is shown
              verbatim so typed asterisks or hashes aren't reinterpreted. */}
          {message.role === "assistant"
            ? renderMarkdown(message.text)
            : message.text}
        </div>
      )}

      {message.error && (
        <div className="msg-error" role="alert">
          {message.error}
        </div>
      )}

      {children}
    </div>
  );
}
