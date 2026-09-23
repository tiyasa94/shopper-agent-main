"""Render explicitly headed Markdown tables as labeled evidence sentences."""

import html
import re
from collections.abc import Mapping, Set


def _cells(line: str) -> list[str]:
    """Split a pipe-delimited row while preserving escaped pipes in cell values."""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]


def rewrite_markdown_tables(
    text: str,
    *,
    row_headers: Set[str],
    value_labels: Mapping[str, str],
    required_headers: Set[str],
    group_label: str,
    row_label: str,
    note: str,
) -> str:
    """Render recognized rows after the original prose and a caller-supplied note.

    Header keys must be lowercase with normalized whitespace. Mapping order determines value
    order in each sentence. Repeated-cell rows supply group labels; Markdown headings supply
    section labels. Literal values, prose, and footnotes are retained without interpretation.
    Unheaded, malformed, and unrecognized table rows are omitted, not assigned guessed labels.
    """
    lines = text.splitlines()
    prose: list[str] = []
    sentences: list[str] = []
    columns: list[tuple[int, str]] = []
    width = 0
    section = ""
    group = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        row = _cells(line)
        if not (line.strip().startswith("|") and line.strip().endswith("|")):
            prose.append(line)
        if (
            len(row) >= 2
            and re.sub(r"\s+", " ", html.unescape(row[0])).strip().casefold() in row_headers
        ):
            headers = [re.sub(r"\s+", " ", html.unescape(cell)).strip().casefold() for cell in row]
            separator = _cells(lines[index + 1]) if index + 1 < len(lines) else []
            if (
                required_headers.issubset(headers)
                and len(set(headers)) == len(headers)
                and all(header in value_labels for header in headers[1:])
                and len(separator) == len(row)
                and all(re.fullmatch(r":?-+:?", cell) for cell in separator)
            ):
                width = len(row)
                columns = [
                    (headers.index(header), label)
                    for header, label in value_labels.items()
                    if header in headers
                ]
                group = ""
                index += 2
                continue
        if width and len(row) == width:
            if len(set(row)) == 1:
                group = row[0]
            elif all(row):
                values = " ".join(f"{label}: {row[position]}." for position, label in columns)
                sentences.append(
                    f"Section: {section}. {group_label}: {group}. {row_label}: {row[0]}. {values}"
                )
        else:
            width = 0
            group = ""
            heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
            if heading:
                section = heading.group(1)
        index += 1
    return "\n".join(prose) + "\n\n" + note + "\n\n" + "\n\n".join(sentences)
