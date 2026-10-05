"""Minimal Markdown → HTML for the reports this system generates (headings, lists, tables, quotes, bold, code).
Input is escaped first; no raw HTML passes through."""

from __future__ import annotations

import html
import re


def _inline(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" rel="noreferrer">\1</a>', t)
    return t


def render(md: str) -> str:
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if m := re.match(r"^(#{1,4})\s+(.*)", line):
            level = min(len(m[1]) + 1, 4)
            out.append(f"<h{level}>{_inline(m[2])}</h{level}>")
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            body = [c for c in cells[1:] if not all(re.fullmatch(r":?-{2,}:?", x) for x in c)]
            out.append("<div class=\"table\"><table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells[0])
                       + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r)
                                                          + "</tr>" for r in body) + "</tbody></table></div>")
        elif line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i].lstrip("> "))
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(quote))}</blockquote>")
        elif re.match(r"^\s*([-*]|\d+\.)\s", line):
            ordered = bool(re.match(r"^\s*\d+\.", line))
            items = []
            while i < len(lines) and (re.match(r"^\s*([-*]|\d+\.)\s", lines[i]) or lines[i].startswith("  ")):
                if re.match(r"^\s*([-*]|\d+\.)\s", lines[i]):
                    items.append(re.sub(r"^\s*([-*]|\d+\.)\s", "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||>|\s*([-*]|\d+\.)\s)", lines[i]):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)
