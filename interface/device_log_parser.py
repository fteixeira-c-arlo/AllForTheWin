"""Parse embedded camera log lines (mpp… format) for structured log viewer UI."""
from __future__ import annotations

import re
from typing import Any, Literal

LogLevel = Literal["I", "N", "W", "E", "unknown"]
LogKind = Literal["log", "json", "system"]


# mpp[pid]: [ts.sec][L][TL][Module/line or pid][threadId] message
_MPP_LINE = re.compile(
    r"^mpp\[(\d+)\]:\s+\[([\d.]+)\]\[([NIWE])\]\[TL\]\[([^\]]+)\]\[(\d+)\]\s*(.*)\s*$"
)

_HEX = re.compile(r"0x[0-9a-fA-F]+")
_FLOAT = re.compile(r"(?<![\w.])(-?\d+\.\d+)(?![\w.])")
_INT = re.compile(r"(?<![\w.])(-?\d+)(?![\w.])")
_QUOTED = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"')
_KEY = re.compile(r"\b([a-zA-Z_][\w]*)\s*:")


def parse_device_log_line(line: str) -> dict[str, Any]:
    """
    Parse one line into a dict suitable for the log table.

    Keys: raw, timestamp, level (I|N|W|E|unknown), module, line_number, thread_id,
    message, kind (log|json|system), json_expanded (bool, default False for json rows).
    """
    raw = line.rstrip("\r\n")
    stripped = raw.strip()
    if not stripped:
        return _fallback(raw, "")

    low = stripped.lower()
    if "connection lost" in low or (
        "device disconnected" in low and "reconnect" in low
    ):
        return {
            "raw": raw,
            "timestamp": "",
            "level": "unknown",
            "module": "",
            "line_number": "",
            "thread_id": "",
            "message": stripped,
            "kind": "system",
            "json_expanded": False,
        }

    if stripped.lower().startswith("sent:"):
        return {
            "raw": raw,
            "timestamp": "",
            "level": "unknown",
            "module": "",
            "line_number": "",
            "thread_id": "",
            "message": stripped,
            "kind": "json",
            "json_expanded": False,
        }

    m = _MPP_LINE.match(stripped)
    if not m:
        return _fallback(raw, stripped)

    _pid, ts, lev, mod_or_pid, tid, msg = m.groups()
    module = ""
    line_no = ""
    inner = mod_or_pid.strip()
    if "/" in inner:
        a, b = inner.split("/", 1)
        module = a.strip()
        if b.isdigit():
            line_no = b
        else:
            line_no = b.strip()
    elif inner.isdigit():
        module = ""
        line_no = ""
    else:
        module = inner

    return {
        "raw": raw,
        "timestamp": ts,
        "level": lev if lev in ("I", "N", "W", "E") else "unknown",
        "module": module,
        "line_number": line_no,
        "thread_id": tid,
        "message": msg,
        "kind": "log",
        "json_expanded": False,
    }


def _fallback(raw: str, stripped: str) -> dict[str, Any]:
    return {
        "raw": raw,
        "timestamp": "",
        "level": "unknown",
        "module": "",
        "line_number": "",
        "thread_id": "",
        "message": stripped,
        "kind": "log",
        "json_expanded": False,
    }


def json_display_message(entry: dict[str, Any]) -> str:
    """Collapsed one-line label for json kind rows (click row to expand)."""
    if entry.get("kind") != "json":
        return str(entry.get("message") or "")
    if entry.get("json_expanded"):
        return str(entry.get("message") or "")
    return "sent: {…}"


def tokenize_message_for_paint(message: str) -> list[tuple[str, str | None]]:
    """
    Split message into (text, color_hex_or_None) segments for optional delegate painting.
    Order: hex, quoted strings, floats, ints, keys (word:), plain remainder.
    """
    if not message:
        return []
    # Colors tuned for dark table background (aligned with embedded log palette).
    hex_c = "#e8b060"
    num_c = "#90d8a8"
    str_c = "#f687b3"
    key_c = "#a8b8d0"

    spans: list[tuple[int, int, str]] = []

    def add(pat: re.Pattern[str], color: str) -> None:
        for m in pat.finditer(message):
            spans.append((m.start(), m.end(), color))

    add(_HEX, hex_c)
    add(_QUOTED, str_c)
    add(_FLOAT, num_c)
    add(_INT, num_c)
    add(_KEY, key_c)

    if not spans:
        return [(message, None)]

    spans.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    merged: list[tuple[int, int, str]] = []
    for s, e, c in spans:
        overlap = False
        for ms, me, _ in merged:
            if not (e <= ms or s >= me):
                overlap = True
                break
        if not overlap:
            merged.append((s, e, c))
    merged.sort(key=lambda x: x[0])

    out: list[tuple[str, str | None]] = []
    pos = 0
    for s, e, c in merged:
        if s > pos:
            out.append((message[pos:s], None))
        out.append((message[s:e], c))
        pos = e
    if pos < len(message):
        out.append((message[pos:], None))
    return out


def entry_matches_level(entry: dict[str, Any], level_filter: str | None) -> bool:
    """level_filter None or 'ALL' = accept all; else I/N/W/E must match (json+system pass)."""
    if not level_filter or level_filter.upper() == "ALL":
        return True
    k = entry.get("kind")
    if k in ("json", "system"):
        return True
    lev = (entry.get("level") or "unknown").upper()
    want = level_filter.upper()
    if want == "INFO":
        want = "I"
    elif want == "NOTICE":
        want = "N"
    elif want == "WARN":
        want = "W"
    elif want == "ERROR":
        want = "E"
    return lev == want


def entry_matches_search(entry: dict[str, Any], needle: str) -> bool:
    if not needle:
        return True
    n = needle.lower()
    parts = [
        entry.get("raw") or "",
        entry.get("timestamp") or "",
        entry.get("level") or "",
        entry.get("module") or "",
        entry.get("line_number") or "",
        entry.get("thread_id") or "",
        entry.get("message") or "",
    ]
    return any(n in (p or "").lower() for p in parts)
