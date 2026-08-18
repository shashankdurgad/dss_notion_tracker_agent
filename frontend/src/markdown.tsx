import type { ReactNode } from "react";

/**
 * Minimal Markdown renderer for assistant replies.
 *
 * Deliberately hand-rolled rather than pulling in react-markdown: the models
 * only ever emit a small subset (bold, italic, code, links, bullets, numbered
 * lists, headings), and this keeps the bundle small with no HTML passed
 * through — every node below is constructed as React elements, so there is no
 * dangerouslySetInnerHTML and no XSS surface from model output.
 */

type Inline = ReactNode;

// Ordered so the first match wins: code before emphasis, because `**` inside
// backticks must stay literal.
const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)]+\))|(https?:\/\/[^\s)<>\]]+)/g;

function renderInline(text: string, keyPrefix: string): Inline[] {
  const nodes: Inline[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let i = 0;

  INLINE.lastIndex = 0;
  while ((match = INLINE.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    const token = match[0];
    const key = `${keyPrefix}-i${i++}`;

    if (token.startsWith("`")) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**") || token.startsWith("__")) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("*")) {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>);
    } else if (token.startsWith("[")) {
      // [label](url)
      const split = token.indexOf("](");
      const label = token.slice(1, split);
      const href = token.slice(split + 2, -1);
      nodes.push(
        <a key={key} href={href} target="_blank" rel="noopener noreferrer">
          {label}
        </a>,
      );
    } else {
      // Bare URL. Trailing punctuation usually belongs to the sentence.
      const trimmed = token.replace(/[.,;:]+$/, "");
      const trailing = token.slice(trimmed.length);
      nodes.push(
        <a key={key} href={trimmed} target="_blank" rel="noopener noreferrer">
          {trimmed}
        </a>,
      );
      if (trailing) nodes.push(trailing);
    }
    lastIndex = match.index + token.length;
  }

  if (lastIndex < text.length) nodes.push(text.slice(lastIndex));
  return nodes;
}

const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*(\d+)[.)]\s+(.*)$/;
const HEADING = /^(#{1,4})\s+(.*)$/;

export function renderMarkdown(text: string): ReactNode[] {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];

  let listItems: string[] = [];
  let listOrdered = false;
  let paragraph: string[] = [];
  let key = 0;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const content = paragraph.join(" ");
    blocks.push(<p key={`p${key++}`}>{renderInline(content, `p${key}`)}</p>);
    paragraph = [];
  };

  const flushList = () => {
    if (!listItems.length) return;
    const items = listItems.map((item, i) => (
      <li key={i}>{renderInline(item, `l${key}-${i}`)}</li>
    ));
    blocks.push(
      listOrdered ? (
        <ol key={`l${key++}`}>{items}</ol>
      ) : (
        <ul key={`l${key++}`}>{items}</ul>
      ),
    );
    listItems = [];
  };

  for (const line of lines) {
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      // Cap at h4 — assistant replies sit inside the page, so a big h1 would
      // out-shout the page's own heading.
      const level = Math.min(heading[1].length + 2, 6);
      const Tag = `h${level}` as "h3" | "h4" | "h5" | "h6";
      blocks.push(
        <Tag key={`h${key++}`}>{renderInline(heading[2], `h${key}`)}</Tag>,
      );
      continue;
    }

    const numbered = NUMBERED.exec(line);
    if (numbered) {
      flushParagraph();
      if (!listOrdered) flushList();
      listOrdered = true;
      listItems.push(numbered[2]);
      continue;
    }

    const bullet = BULLET.exec(line);
    if (bullet) {
      flushParagraph();
      if (listOrdered) flushList();
      listOrdered = false;
      listItems.push(bullet[1]);
      continue;
    }

    flushList();
    paragraph.push(line.trim());
  }

  flushParagraph();
  flushList();
  return blocks;
}
