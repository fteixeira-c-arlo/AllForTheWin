"""Log line parsing and HTML report generation for parse_logs / parse_logs_stop."""
import html
import re
from collections import Counter
from typing import Any

# Event categories for camera behavior (matched in message/raw, case-insensitive)
# Order matters: first match wins. More specific patterns should come first.
EVENT_PATTERNS: list[tuple[str, str]] = [
    # Motion
    ("motion detected", "motion_detected"),
    ("motion event", "motion_detected"),
    ("motion trigger", "motion_detected"),
    ("motion start", "motion_detected"),
    ("motion_detected", "motion_detected"),
    ("pir trigger", "motion_detected"),
    ("pir event", "motion_detected"),
    ("motion alert", "motion_detected"),
    # Audio
    ("audio detected", "audio_detected"),
    ("sound detected", "audio_detected"),
    ("voice detected", "audio_detected"),
    ("mic level", "audio_detected"),
    ("audio event", "audio_detected"),
    ("sound event", "audio_detected"),
    # Streaming
    ("stream start", "streaming"),
    ("streaming started", "streaming"),
    ("start streaming", "streaming"),
    ("live view", "streaming"),
    ("stream up", "streaming"),
    ("rtsp.*start", "streaming"),
    ("webrtc.*start", "streaming"),
    ("stream active", "streaming"),
    ("started stream", "streaming"),
    # Stream failed
    ("stream fail", "stream_failed"),
    ("stream failed", "stream_failed"),
    ("stream error", "stream_failed"),
    ("stream timeout", "stream_failed"),
    ("streaming failed", "stream_failed"),
    ("connection failed", "stream_failed"),
    ("connection error", "stream_failed"),
    ("stream connect fail", "stream_failed"),
    ("rtsp.*fail", "stream_failed"),
    ("webrtc.*fail", "stream_failed"),
    ("stream disconnected", "stream_failed"),
    ("streaming disconnect", "stream_failed"),
    # Stream stopped
    ("stream stop", "stream_stopped"),
    ("stream stopped", "stream_stopped"),
    ("stop streaming", "stream_stopped"),
    ("stream end", "stream_stopped"),
    ("stream down", "stream_stopped"),
    ("streaming stopped", "stream_stopped"),
    ("ended stream", "stream_stopped"),
    ("stop stream", "stream_stopped"),
    # Idle / standby
    ("camera idle", "idle"),
    ("entering idle", "idle"),
    ("idle state", "idle"),
    ("standby", "idle"),
    ("going to sleep", "idle"),
    ("power save", "idle"),
    ("ready state", "idle"),
    ("idle mode", "idle"),
    ("sleep mode", "idle"),
    # Recording
    ("recording start", "recording"),
    ("start record", "recording"),
    ("recording started", "recording"),
    ("record start", "recording"),
    ("recording stop", "recording_stopped"),
    ("stop record", "recording_stopped"),
    ("recording stopped", "recording_stopped"),
    ("record stop", "recording_stopped"),
    # Connection / network (disconnected before generic "connect" patterns)
    ("disconnected", "disconnected"),
    ("connection lost", "disconnected"),
    ("offline", "disconnected"),
    ("connected", "connected"),
    ("connection established", "connected"),
    ("wifi connected", "connected"),
    ("ethernet link", "connected"),
    # Onboard / registration
    ("onboarded successfully", "onboard_success"),
    ("onboard success", "onboard_success"),
    ("claim success", "onboard_success"),
    ("claimed successfully", "onboard_success"),
    ("registration success", "onboard_success"),
    ("bs_claimed", "onboard_success"),
    ("device claimed", "onboard_success"),
    ("onboarded", "onboard_success"),
    # PoE (Power over Ethernet)
    ("power over ethernet", "poe"),
    ("poe detected", "poe"),
    ("poe power", "poe"),
    ("ethernet power", "poe"),
    ("poe", "poe"),
    # Wi‑Fi (association, setup, WLAN)
    ("wifi association", "wifi"),
    ("wifi connect", "wifi"),
    ("wlan", "wifi"),
    ("wireless connect", "wifi"),
    ("wifi setup", "wifi"),
    ("wifi configured", "wifi"),
    ("joining wifi", "wifi"),
    ("wifi", "wifi"),
    # Ethernet (link up, wired)
    ("ethernet link up", "ethernet"),
    ("ethernet connected", "ethernet"),
    ("wired connection", "ethernet"),
    ("ethernet", "ethernet"),
    # Boot / init
    ("boot", "boot"),
    ("startup", "boot"),
    ("init complete", "boot"),
    ("firmware", "boot"),
    ("reboot", "reboot"),
]
_EVENT_REGEXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), label) for p, label in EVENT_PATTERNS
]

# Normalized log levels for detection and styling
LEVEL_KEYWORDS = [
    ("error", "error"),
    ("err]", "error"),
    (" err ", "error"),
    ("warn", "warn"),
    ("warning", "warn"),
    ("info", "info"),
    ("debug", "debug"),
    ("trace", "trace"),
    ("crit", "fatal"),
    ("fatal", "fatal"),
    ("alert", "fatal"),
    ("emerg", "fatal"),
]
# Order by length descending so we match longer first (e.g. "warning" before "warn")
_LEVEL_PATTERNS = sorted(
    [(re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE), v) for k, v in LEVEL_KEYWORDS],
    key=lambda x: -len(x[0].pattern),
)

# Timestamp patterns: [YYYY-MM-DD HH:MM:SS], ISO-like, or leading digits (epoch)
_TIMESTAMP_BRACKET = re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\]?\s*")
_TIMESTAMP_LEADING = re.compile(r"^(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*")
_EPOCH_LEADING = re.compile(r"^(\d{10,13})\s+")


def parse_line(line: str) -> dict[str, Any]:
    """
    Parse a single log line into timestamp, level, message, and raw.
    Returns dict with keys: timestamp (str), level (str), message (str), raw (str).
    """
    raw = line.rstrip("\r\n")
    stripped = raw.strip()
    timestamp = ""
    level = "other"
    message = stripped

    if not stripped:
        return {"timestamp": "", "level": "other", "message": "", "raw": raw, "event": "other"}

    # Try to extract timestamp from start
    for pattern in (_TIMESTAMP_BRACKET, _TIMESTAMP_LEADING, _EPOCH_LEADING):
        m = pattern.search(stripped)
        if m:
            timestamp = m.group(1).strip()
            rest = stripped[m.end() :].strip()
            if rest:
                message = rest
            break

    # Detect level in the whole line (or message part)
    text_to_scan = message or stripped
    for pat, norm_level in _LEVEL_PATTERNS:
        if pat.search(text_to_scan):
            level = norm_level
            break

    # Detect event category (motion, audio, streaming, etc.)
    event = "other"
    for pat, event_label in _EVENT_REGEXES:
        if pat.search(text_to_scan):
            event = event_label
            break

    return {
        "timestamp": timestamp,
        "level": level,
        "message": message or stripped,
        "raw": raw,
        "event": event,
    }


def _escape(s: str) -> str:
    """Escape for HTML content to avoid XSS."""
    return html.escape(s, quote=True)


# Human-readable event labels for the report
EVENT_LABELS: dict[str, str] = {
    "motion_detected": "Motion detected",
    "audio_detected": "Audio / sound detected",
    "streaming": "Streaming (live)",
    "stream_failed": "Stream failed",
    "stream_stopped": "Stream stopped",
    "idle": "Idle / standby",
    "recording": "Recording started",
    "recording_stopped": "Recording stopped",
    "connected": "Connected",
    "disconnected": "Disconnected",
    "onboard_success": "Onboard / claim success",
    "poe": "PoE (Power over Ethernet)",
    "wifi": "Wi‑Fi",
    "ethernet": "Ethernet / wired",
    "boot": "Boot / startup",
    "reboot": "Reboot",
    "other": "Other",
}


def build_html(entries: list[dict[str, Any]], title: str = "Log parse report") -> str:
    """
    Build a self-contained HTML document with event summary, key events, level summary, and full log table.
    entries: list of dicts from parse_line (timestamp, level, message, raw, event).
    """
    total = len(entries)
    by_level = Counter(e.get("level") or "other" for e in entries)
    by_event = Counter(e.get("event") or "other" for e in entries)
    key_entries = [e for e in entries if (e.get("event") or "other") != "other"]

    level_order = ("error", "fatal", "warn", "info", "debug", "trace", "other")
    event_order = (
        "motion_detected", "audio_detected", "streaming", "stream_failed", "stream_stopped",
        "idle", "recording", "recording_stopped", "connected", "disconnected",
        "onboard_success", "poe", "wifi", "ethernet", "boot", "reboot", "other",
    )
    seen_lev = set()
    summary_rows = []
    for lev in level_order:
        count = by_level.get(lev, 0)
        if count > 0 and lev not in seen_lev:
            summary_rows.append(f"<tr><td>{_escape(lev)}</td><td>{count}</td></tr>")
            seen_lev.add(lev)
    for lev, count in sorted(by_level.items(), key=lambda x: -x[1]):
        if lev not in seen_lev:
            summary_rows.append(f"<tr><td>{_escape(lev)}</td><td>{count}</td></tr>")
            seen_lev.add(lev)
    level_table = "\n".join(summary_rows) if summary_rows else "<tr><td colspan=\"2\">No entries</td></tr>"

    seen_ev = set()
    event_summary_rows = []
    for ev in event_order:
        count = by_event.get(ev, 0)
        if count > 0 and ev not in seen_ev:
            label = EVENT_LABELS.get(ev, ev.replace("_", " ").title())
            event_summary_rows.append(f"<tr><td>{_escape(label)}</td><td>{count}</td></tr>")
            seen_ev.add(ev)
    for ev, count in sorted(by_event.items(), key=lambda x: -x[1]):
        if ev not in seen_ev:
            label = EVENT_LABELS.get(ev, ev.replace("_", " ").title())
            event_summary_rows.append(f"<tr><td>{_escape(label)}</td><td>{count}</td></tr>")
            seen_ev.add(ev)
    event_table = "\n".join(event_summary_rows) if event_summary_rows else "<tr><td colspan=\"2\">No key events</td></tr>"

    def row_class(level: str) -> str:
        return f"level-{level}" if level else "level-other"

    def event_class(event: str) -> str:
        return f"event-{event}" if event and event != "other" else ""

    # Key events section
    key_event_rows = []
    for e in key_entries:
        ts = _escape((e.get("timestamp") or "").strip())
        ev = (e.get("event") or "other").strip()
        label = _escape(EVENT_LABELS.get(ev, ev.replace("_", " ").title()))
        msg = _escape((e.get("message") or e.get("raw") or "").strip())
        key_event_rows.append(
            f"<tr class=\"event-row {event_class(ev)}\"><td class=\"ts\">{ts}</td>"
            f"<td class=\"event-tag\">{label}</td><td class=\"msg\">{msg}</td></tr>"
        )
    key_events_body = "\n".join(key_event_rows) if key_event_rows else "<tr><td colspan=\"3\">No key events detected</td></tr>"

    # Full log table with Event column
    log_rows = []
    for e in entries:
        ts = _escape((e.get("timestamp") or "").strip())
        lv = (e.get("level") or "other").strip()
        ev = (e.get("event") or "other").strip()
        ev_label = EVENT_LABELS.get(ev, ev.replace("_", " ").title()) if ev != "other" else "—"
        msg = _escape((e.get("message") or e.get("raw") or "").strip())
        raw = _escape((e.get("raw") or "").strip())
        cls = row_class(lv)
        log_rows.append(
            f"<tr class=\"{cls}\"><td class=\"ts\">{ts}</td><td class=\"level\">{_escape(lv)}</td>"
            f"<td class=\"event-tag\">{_escape(ev_label)}</td><td class=\"msg\">{msg}</td><td class=\"raw\">{raw}</td></tr>"
        )
    log_table_body = "\n".join(log_rows) if log_rows else "<tr><td colspan=\"5\">No lines</td></tr>"

    css = """
    body { font-family: system-ui, sans-serif; margin: 1rem; background: #1a1a1a; color: #e0e0e0; }
    h1 { font-size: 1.25rem; margin-bottom: 0.5rem; }
    h2 { font-size: 1.1rem; margin-top: 1.25rem; margin-bottom: 0.4rem; color: #b0b0b0; }
    .summary { margin-bottom: 1rem; }
    .summary-section { margin-bottom: 1.25rem; }
    .summary-section table { margin-top: 0.25rem; }
    table { border-collapse: collapse; width: 100%; font-size: 0.875rem; }
    th, td { border: 1px solid #444; padding: 0.35rem 0.5rem; text-align: left; }
    th { background: #333; }
    .ts { white-space: nowrap; color: #888; }
    .level { white-space: nowrap; font-weight: 600; }
    .event-tag { white-space: nowrap; font-weight: 500; }
    .msg { max-width: 35%; word-break: break-word; }
    .raw { max-width: 35%; word-break: break-all; color: #aaa; }
    .level-error, .level-fatal { background: #3d1f1f; color: #f88; }
    .level-warn { background: #3d3d1f; color: #dd8; }
    .level-info { background: #1f2d3d; color: #8cf; }
    .level-debug { background: #1f2d2d; color: #8fa; }
    .level-trace { background: #252525; color: #999; }
    .level-other { background: #252525; }
    .event-motion_detected { border-left: 3px solid #6a9; }
    .event-audio_detected { border-left: 3px solid #9a6; }
    .event-streaming { border-left: 3px solid #69c; }
    .event-stream_failed { border-left: 3px solid #c66; }
    .event-stream_stopped { border-left: 3px solid #c96; }
    .event-idle { border-left: 3px solid #888; }
    .event-recording { border-left: 3px solid #9c6; }
    .event-recording_stopped { border-left: 3px solid #696; }
    .event-connected { border-left: 3px solid #6c6; }
    .event-disconnected { border-left: 3px solid #c66; }
    .event-onboard_success { border-left: 3px solid #6c9; }
    .event-poe { border-left: 3px solid #96c; }
    .event-wifi { border-left: 3px solid #c96; }
    .event-ethernet { border-left: 3px solid #69c; }
    .event-boot, .event-reboot { border-left: 3px solid #aa8; }
    """

    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        f"<title>{_escape(title)}</title>\n"
        f"<style>{css}</style>\n"
        "</head>\n<body>\n"
        f"<h1>{_escape(title)}</h1>\n"
        "<div class=\"summary-section\">\n"
        f"<p><strong>Total lines:</strong> {total}</p>\n"
        "<h2>Log level summary</h2>\n"
        "<table class=\"summary\"><thead><tr><th>Level</th><th>Count</th></tr></thead>\n<tbody>\n"
        f"{level_table}\n"
        "</tbody></table>\n</div>\n"
        "<div class=\"summary-section\">\n"
        "<h2>Event summary (motion, audio, streaming, idle, etc.)</h2>\n"
        "<table class=\"summary\"><thead><tr><th>Event</th><th>Count</th></tr></thead>\n<tbody>\n"
        f"{event_table}\n"
        "</tbody></table>\n</div>\n"
        "<div class=\"summary-section\">\n"
        "<h2>Key events (chronological)</h2>\n"
        "<p>Lines that matched motion, audio, streaming, stream failed/stopped, idle, recording, connection, or boot.</p>\n"
        "<table class=\"log-table\"><thead><tr><th>Time</th><th>Event</th><th>Message</th></tr></thead>\n<tbody>\n"
        f"{key_events_body}\n"
        "</tbody></table>\n</div>\n"
        "<div class=\"summary-section\">\n"
        "<h2>Full log (all lines)</h2>\n"
        "<table class=\"log-table\">\n<thead><tr><th>Time</th><th>Level</th><th>Event</th><th>Message</th><th>Raw</th></tr></thead>\n<tbody>\n"
        f"{log_table_body}\n"
        "</tbody></table>\n</div>\n</body>\n</html>"
    )


def write_html(entries: list[dict[str, Any]], output_path: str, title: str | None = None) -> None:
    """Write HTML report to output_path. Uses default title if not provided."""
    if title is None:
        title = "Log parse report"
    html_content = build_html(entries, title=title)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
