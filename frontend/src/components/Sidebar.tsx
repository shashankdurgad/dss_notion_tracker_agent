import { useState } from "react";
import type { Conversation } from "../types";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  open: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}

function groupLabel(updatedAt: number): string {
  const days = (Date.now() / 1000 - updatedAt) / 86400;
  if (days < 1) return "Today";
  if (days < 2) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return "Older";
}

export default function Sidebar({
  conversations,
  activeId,
  open,
  onSelect,
  onNew,
  onDelete,
  onClose,
}: Props) {
  const [confirmId, setConfirmId] = useState<string | null>(null);

  // Conversations arrive newest-first, so grouping in order preserves that.
  const groups: { label: string; items: Conversation[] }[] = [];
  for (const conversation of conversations) {
    const label = groupLabel(conversation.updated_at);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.items.push(conversation);
    else groups.push({ label, items: [conversation] });
  }

  return (
    <>
      {open && <div className="sidebar-scrim" onClick={onClose} />}
      <aside className={`sidebar ${open ? "sidebar-open" : ""}`}>
        <button className="new-chat" onClick={onNew}>
          + New chat
        </button>

        <nav className="chat-list" aria-label="Saved chats">
          {conversations.length === 0 && (
            <p className="chat-empty">No saved chats yet.</p>
          )}

          {groups.map((group) => (
            <div key={group.label} className="chat-group">
              <div className="chat-group-label">{group.label}</div>
              {group.items.map((conversation) => (
                <div
                  key={conversation.id}
                  className={`chat-item ${
                    conversation.id === activeId ? "chat-item-active" : ""
                  }`}
                >
                  <button
                    className="chat-item-title"
                    onClick={() => onSelect(conversation.id)}
                    title={conversation.title}
                  >
                    {conversation.title}
                  </button>

                  {confirmId === conversation.id ? (
                    <span className="chat-confirm">
                      <button
                        onClick={() => {
                          onDelete(conversation.id);
                          setConfirmId(null);
                        }}
                        aria-label="Confirm delete"
                      >
                        Delete
                      </button>
                      <button
                        onClick={() => setConfirmId(null)}
                        aria-label="Cancel delete"
                      >
                        Cancel
                      </button>
                    </span>
                  ) : (
                    <button
                      className="chat-item-delete"
                      onClick={() => setConfirmId(conversation.id)}
                      aria-label={`Delete ${conversation.title}`}
                    >
                      ×
                    </button>
                  )}
                </div>
              ))}
            </div>
          ))}
        </nav>
      </aside>
    </>
  );
}
