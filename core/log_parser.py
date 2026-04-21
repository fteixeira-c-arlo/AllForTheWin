"""Log line parsing and HTML report generation for parse_logs / parse_logs_stop."""
import html
import re
from collections import Counter
from typing import Any

# Event categories for camera behavior (matched in message/raw, case-insensitive)
# Order matters: first match wins. More specific patterns should come first.
EVENT_PATTERNS: list[tuple[str, str]] = [
    # Arlo firmware-specific patterns (function names / constants in actual log output)
    # These must appear BEFORE the generic human-readable patterns below.
    ("video_motion_alert", "motion_detected"),
    ("motion_trigger_input", "motion_detected"),
    ("motion_start.*1", "motion_detected"),
    ("alert_event_video", "motion_detected"),
    ("push_notification", "push_notification"),
    ("send_push", "push_notification"),
    ("pn_send", "push_notification"),
    ("xagent.*push", "push_notification"),
    ("alert_manager_state_active", "motion_detected"),
    ("alert_mgr_active_alert", "motion_detected"),
    ("arlo_handle.*alert", "motion_detected"),
    ("tl_visual_detec", "motion_detected"),
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
    "push_notification": "Push notification (PN)",
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
    from datetime import datetime

    total = len(entries)
    by_level = Counter(e.get("level") or "other" for e in entries)
    by_event = Counter(e.get("event") or "other" for e in entries)
    key_entries = [e for e in entries if (e.get("event") or "other") != "other"]

    level_order = ("error", "fatal", "warn", "info", "debug", "trace", "other")
    event_order = (
        "motion_detected", "push_notification", "audio_detected", "streaming", "stream_failed", "stream_stopped",
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

    event_chip_colors: dict[str, tuple[str, str]] = {
        "motion_detected": ("#1c3a2e", "#3fb950"),
        "audio_detected": ("#1a2a3a", "#79c0ff"),
        "streaming": ("#1a2d45", "#58a6ff"),
        "stream_failed": ("#3a1a1a", "#f85149"),
        "stream_stopped": ("#2e2a1a", "#d29922"),
        "idle": ("#1e2228", "#8b949e"),
        "recording": ("#0a2a28", "#00c4a7"),
        "recording_stopped": ("#0a2220", "#1faa90"),
        "connected": ("#1c3a2e", "#56d364"),
        "disconnected": ("#3a2a1a", "#ffa657"),
        "onboard_success": ("#1c3a2e", "#39d353"),
        "poe": ("#261a35", "#d2a8ff"),
        "wifi": ("#1e2228", "#8b949e"),
        "ethernet": ("#1a2a45", "#4f94d4"),
        "boot": ("#261a35", "#d2a8ff"),
        "reboot": ("#2e2a1a", "#e3b341"),
        "other": ("#21262d", "#6e7681"),
    }

    event_badge_css = "\n".join(
        f".evt-{ev} {{ background: {bg}; color: {fg}; }}"
        for ev, (bg, fg) in event_chip_colors.items()
    )

    err_fatal = by_level.get("error", 0) + by_level.get("fatal", 0)
    warn_count = by_level.get("warn", 0)
    key_event_count = len(key_entries)

    level_bar_colors: dict[str, str] = {
        "error": "#f85149",
        "fatal": "#ff7b72",
        "warn": "#d29922",
        "info": "#58a6ff",
        "debug": "#3fb950",
        "trace": "#6e7681",
        "other": "#6e7681",
    }

    level_bars_html_parts: list[str] = []
    for lev in level_order:
        cnt = by_level.get(lev, 0)
        if cnt <= 0:
            continue
        pct = (100.0 * cnt / total) if total else 0.0
        bar_color = level_bar_colors.get(lev, "#6e7681")
        level_bars_html_parts.append(
            f"<div class=\"lvl-bar-row\">"
            f"<span class=\"lvl-bar-label mono\">{_escape(lev)}</span>"
            f"<div class=\"lvl-bar-track\"><div class=\"lvl-bar-fill\" style=\"width:{pct:.1f}%;background:{bar_color}\"></div></div>"
            f"<span class=\"lvl-bar-count mono\">{cnt}</span>"
            f"</div>"
        )
    level_bars_html = "\n".join(level_bars_html_parts) if level_bars_html_parts else (
        "<p class=\"muted\">No level data</p>"
    )

    event_chips_html_parts: list[str] = []
    for ev in event_order:
        cnt = by_event.get(ev, 0)
        if cnt <= 0:
            continue
        label = EVENT_LABELS.get(ev, ev.replace("_", " ").title())
        safe_ev = ev if ev in event_chip_colors else "other"
        event_chips_html_parts.append(
            f"<span class=\"event-chip evt-{safe_ev} mono\">{_escape(label)} ×{cnt}</span>"
        )
    for ev, cnt in sorted(by_event.items(), key=lambda x: -x[1]):
        if ev in event_order or cnt <= 0:
            continue
        label = EVENT_LABELS.get(ev, ev.replace("_", " ").title())
        safe_ev = ev if ev in event_chip_colors else "other"
        event_chips_html_parts.append(
            f"<span class=\"event-chip evt-{safe_ev} mono\">{_escape(label)} ×{cnt}</span>"
        )
    event_chips_html = "\n".join(event_chips_html_parts) if event_chips_html_parts else (
        "<span class=\"muted\">No event counts</span>"
    )

    # Key events section
    key_event_rows = []
    for e in key_entries:
        ts = _escape((e.get("timestamp") or "").strip())
        ev = (e.get("event") or "other").strip()
        lv = (e.get("level") or "other").strip()
        label_plain = EVENT_LABELS.get(ev, ev.replace("_", " ").title())
        safe_ev = ev if ev in event_chip_colors else "other"
        badge = f"<span class=\"badge evt-{safe_ev} mono\">{_escape(label_plain)}</span>"
        msg = _escape((e.get("message") or e.get("raw") or "").strip())
        row_tint = ""
        if lv in ("error", "fatal"):
            row_tint = " key-row-err"
        elif lv == "warn":
            row_tint = " key-row-warn"
        lvl_cls = f" lvl-txt-{lv}" if lv in ("error", "fatal", "warn") else ""
        key_event_rows.append(
            f"<tr class=\"key-row{row_tint}\"><td class=\"ts mono\">{ts}</td><td>{badge}</td>"
            f"<td class=\"mono{lvl_cls}\">{_escape(lv)}</td><td class=\"msg\">{msg}</td></tr>"
        )
    key_events_body = (
        "\n".join(key_event_rows)
        if key_event_rows
        else "<tr><td colspan=\"4\" class=\"muted\">No key events detected</td></tr>"
    )

    # Full log table with Event column
    log_rows = []
    for e in entries:
        ts = _escape((e.get("timestamp") or "").strip())
        lv = (e.get("level") or "other").strip()
        ev = (e.get("event") or "other").strip()
        ev_label = EVENT_LABELS.get(ev, ev.replace("_", " ").title()) if ev != "other" else "—"
        safe_ev = ev if ev in event_chip_colors else "other"
        ev_badge = (
            f"<span class=\"badge evt-{safe_ev} mono\">{_escape(ev_label)}</span>"
            if ev != "other"
            else f"<span class=\"badge badge-dash mono\">{_escape(ev_label)}</span>"
        )
        msg = _escape((e.get("message") or e.get("raw") or "").strip())
        raw = _escape((e.get("raw") or "").strip())
        cls = row_class(lv)
        log_rows.append(
            f"<tr class=\"full-row {cls}\" data-event=\"{_escape(ev)}\"><td class=\"ts mono\">{ts}</td>"
            f"<td class=\"mono lvl-cell-{lv}\">{_escape(lv)}</td>"
            f"<td>{ev_badge}</td><td class=\"msg\">{msg}</td><td class=\"raw mono\">{raw}</td></tr>"
        )
    log_table_body = "\n".join(log_rows) if log_rows else "<tr><td colspan=\"5\" class=\"muted\">No lines</td></tr>"

    parsed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subtitle = _escape(f"Parsed {parsed_at} · {total} lines")

    css = f"""
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: system-ui, -apple-system, sans-serif;
      background: #0d1117;
      color: #c9d1d9;
      font-size: 14px;
      line-height: 1.45;
    }}
    .page {{
      max-width: 1200px;
      margin: 0 auto;
      border-bottom: 1px solid #21262d;
    }}
    .header-bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 1rem;
      padding: 1rem 1.25rem;
      background: #161b22;
      border-bottom: 1px solid #21262d;
    }}
    .header-left {{ display: flex; align-items: center; gap: 0.85rem; min-width: 0; }}
    .icon-square {{
      width: 40px; height: 40px; flex-shrink: 0;
      background: #161b22;
      border: 1px solid #21262d;
      display: flex; align-items: center; justify-content: center;
    }}
    .icon-square svg {{ display: block; }}
    .header-titles h1 {{
      margin: 0;
      font-size: 1.15rem;
      font-weight: 600;
      color: #f0f6fc;
    }}
    .header-titles .subtitle {{
      margin: 0.2rem 0 0;
      font-size: 0.8rem;
      color: #8b949e;
    }}
    .pill-complete {{
      flex-shrink: 0;
      padding: 0.35rem 0.65rem;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
      color: #3fb950;
      background: #1c3a2e;
      border: 1px solid #238636;
    }}
    .stat-cards {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      background: #161b22;
      border-bottom: 1px solid #21262d;
    }}
    .stat-card {{
      padding: 1rem 1rem;
      text-align: center;
      border-right: 1px solid #21262d;
    }}
    .stat-card:last-child {{ border-right: none; }}
    .stat-card .stat-value {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 0.25rem; }}
    .stat-card .stat-label {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.04em; color: #8b949e; }}
    .stat-total .stat-value {{ color: #f0f6fc; }}
    .stat-err .stat-value {{ color: #f85149; }}
    .stat-warn .stat-value {{ color: #d29922; }}
    .stat-key .stat-value {{ color: #79c0ff; }}
    .two-col {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0;
      background: #161b22;
      border-bottom: 1px solid #21262d;
    }}
    .panel {{
      padding: 1rem 1.25rem;
      border-right: 1px solid #21262d;
    }}
    .panel:last-child {{ border-right: none; }}
    .panel h2 {{
      margin: 0 0 0.75rem;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #8b949e;
      font-weight: 600;
    }}
    .lvl-bar-row {{
      display: grid;
      grid-template-columns: 4.5rem 1fr 3rem;
      align-items: center;
      gap: 0.5rem;
      margin-bottom: 0.5rem;
    }}
    .lvl-bar-row:last-child {{ margin-bottom: 0; }}
    .lvl-bar-label {{ text-align: left; color: #c9d1d9; }}
    .lvl-bar-count {{ text-align: right; color: #8b949e; }}
    .lvl-bar-track {{
      height: 8px;
      background: #21262d;
      border-radius: 2px;
      overflow: hidden;
    }}
    .lvl-bar-fill {{ height: 100%; border-radius: 2px; min-width: 2px; }}
    .chips-wrap {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
    }}
    .event-chip {{
      display: inline-block;
      padding: 0.25rem 0.5rem;
      border-radius: 999px;
      font-size: 0.72rem;
      font-weight: 600;
    }}
    .mono {{ font-family: 'Courier New', Courier, monospace; }}
    .section-title {{
      margin: 0;
      padding: 0.75rem 1.25rem;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #8b949e;
      font-weight: 600;
      background: #0d1117;
      border-bottom: 1px solid #21262d;
    }}
    .data-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.8125rem;
    }}
    .data-table th, .data-table td {{
      border-bottom: 1px solid #21262d;
      padding: 0.45rem 0.65rem;
      text-align: left;
      vertical-align: top;
    }}
    .data-table th {{
      background: #161b22;
      color: #8b949e;
      font-weight: 600;
      font-size: 0.7rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .data-table .ts {{ color: #8b949e; white-space: nowrap; }}
    .data-table .msg {{ word-break: break-word; color: #c9d1d9; }}
    .data-table .raw {{ word-break: break-all; color: #8b949e; max-width: 280px; }}
    .badge {{
      display: inline-block;
      padding: 0.15rem 0.45rem;
      border-radius: 4px;
      font-size: 0.7rem;
      font-weight: 600;
      white-space: nowrap;
    }}
    .badge-dash {{ background: #21262d; color: #6e7681; }}
    .key-row-err {{ background: #2d1414; }}
    .key-row-warn {{ background: #2d1f0e; }}
    .lvl-txt-error, .lvl-txt-fatal {{ color: #f85149; font-weight: 600; }}
    .lvl-txt-warn {{ color: #d29922; font-weight: 600; }}
    .full-log-section {{ background: #0d1117; border-bottom: 1px solid #21262d; }}
    .full-log-toolbar {{
      display: flex;
      gap: 0.5rem;
      padding: 0.65rem 1.25rem;
      background: #161b22;
      border-bottom: 1px solid #21262d;
      align-items: center;
    }}
    .mock-search, .mock-filter {{
      padding: 0.35rem 0.6rem;
      border-radius: 6px;
      border: 1px solid #30363d;
      background: #0d1117;
      color: #c9d1d9;
      font-size: 0.75rem;
      font-family: 'Courier New', Courier, monospace;
      outline: none;
    }}
    .mock-search {{ flex: 1; max-width: 260px; }}
    .mock-search:focus, .mock-filter:focus {{ border-color: #58a6ff; }}
    #full-log-toggle {{
      display: block;
      width: 100%;
      text-align: left;
      padding: 0.65rem 1.25rem;
      margin: 0;
      border: none;
      border-bottom: 1px solid #21262d;
      background: #161b22;
      color: #58a6ff;
      font: inherit;
      cursor: pointer;
    }}
    #full-log-toggle:hover {{ background: #21262d; }}
    #full-log-panel {{ padding: 0 0 1rem; }}
    #full-log-panel .table-wrap {{ padding: 0 1.25rem; overflow-x: auto; }}
    .muted {{ color: #8b949e; }}
    .footer-bar {{
      display: flex;
      justify-content: space-between;
      padding: 0.5rem 1.25rem;
      font-size: 0.7rem;
      color: #6e7681;
      background: #161b22;
      border-bottom: 1px solid #21262d;
    }}
    .full-row.level-error .lvl-cell-error,
    .full-row.level-fatal .lvl-cell-fatal {{ color: #f85149; font-weight: 600; }}
    .full-row.level-warn .lvl-cell-warn {{ color: #d29922; font-weight: 600; }}
    {event_badge_css}
    """

    full_log_script = r"""<script>
(function () {
  var panel = document.getElementById('full-log-panel');
  var btn   = document.getElementById('full-log-toggle');
  var total = document.querySelectorAll('#full-log-table .full-row').length;

  window.toggleLog = function () {
    var open = panel.style.display !== 'none';
    panel.style.display = open ? 'none' : 'block';
    btn.textContent = open
      ? '\u25bc Show full log (' + total + ' lines)'
      : '\u25b2 Hide full log';
  };

  var _timer;
  window.filterLog = function () {
    clearTimeout(_timer);
    _timer = setTimeout(function () {
      var term  = document.getElementById('log-search').value.toLowerCase();
      var level = document.getElementById('log-level').value.toLowerCase();
      var event = document.getElementById('log-event').value.toLowerCase();
      var rows  = document.querySelectorAll('#full-log-table .full-row');
      var shown = 0;
      rows.forEach(function (row) {
        var text     = row.textContent.toLowerCase();
        var levelCell = row.querySelector('td:nth-child(2)');
        var rowLevel  = levelCell ? levelCell.textContent.trim().toLowerCase() : '';
        var rowEvent  = (row.getAttribute('data-event') || '').toLowerCase();
        var matchText  = !term  || text.indexOf(term) !== -1;
        var matchLevel = !level || rowLevel === level;
        var matchEvent = !event || rowEvent === event;
        var visible = matchText && matchLevel && matchEvent;
        row.style.display = visible ? '' : 'none';
        if (visible) shown++;
      });
      var counter = document.getElementById('log-match-count');
      if (counter) {
        counter.textContent = (term || level || event)
          ? shown + ' match' + (shown !== 1 ? 'es' : '')
          : '';
      }
    }, 200);
  };
})();
</script>"""

    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_escape(title)}</title>\n"
        f"<style>{css}</style>\n"
        "</head>\n<body>\n"
        "<div class=\"page\">\n"
        "<header class=\"header-bar\">\n"
        "<div class=\"header-left\">\n"
        "<div class=\"icon-square\" aria-hidden=\"true\">"
        "<svg width=\"22\" height=\"22\" viewBox=\"0 0 22 22\" xmlns=\"http://www.w3.org/2000/svg\">"
        "<polygon points=\"7,4 18,11 7,18\" fill=\"#00c4a7\"/></svg></div>\n"
        "<div class=\"header-titles\">\n"
        f"<h1>{_escape(title)}</h1>\n"
        f"<p class=\"subtitle\">{subtitle}</p>\n"
        "</div>\n</div>\n"
        "<div class=\"pill-complete\" role=\"status\">✓ complete</div>\n"
        "</header>\n"
        "<div class=\"stat-cards\">\n"
        "<div class=\"stat-card stat-total\"><div class=\"stat-value\">"
        f"{total}</div><div class=\"stat-label\">Total lines</div></div>\n"
        "<div class=\"stat-card stat-err\"><div class=\"stat-value\">"
        f"{err_fatal}</div><div class=\"stat-label\">Errors + fatals</div></div>\n"
        "<div class=\"stat-card stat-warn\"><div class=\"stat-value\">"
        f"{warn_count}</div><div class=\"stat-label\">Warnings</div></div>\n"
        "<div class=\"stat-card stat-key\"><div class=\"stat-value\">"
        f"{key_event_count}</div><div class=\"stat-label\">Key events</div></div>\n"
        "</div>\n"
        "<div class=\"two-col\">\n"
        "<div class=\"panel\"><h2>Level distribution</h2>\n"
        f"{level_bars_html}\n</div>\n"
        "<div class=\"panel\"><h2>Detected events</h2>\n"
        f"<div class=\"chips-wrap\">{event_chips_html}</div>\n</div>\n"
        "</div>\n"
        "<p class=\"section-title\">Key events</p>\n"
        "<div class=\"table-wrap\" style=\"padding:0 1.25rem 1rem;background:#161b22;\">\n"
        "<table class=\"data-table\">\n<thead><tr>"
        "<th>Time</th><th>Event</th><th>Lvl</th><th>Message</th></tr></thead>\n<tbody>\n"
        f"{key_events_body}\n"
        "</tbody></table>\n</div>\n"
        "<div class=\"full-log-section\">\n"
        f"<button type=\"button\" id=\"full-log-toggle\" onclick=\"toggleLog()\">"
        f"▼ Show full log ({total} lines)</button>\n"
        "<div id=\"full-log-panel\" style=\"display:none\">\n"
        "<div class=\"full-log-toolbar\">\n"
        "<input id=\"log-search\" class=\"mock-search\" type=\"text\" placeholder=\"Search messages...\" oninput=\"filterLog()\">\n"
        "<select id=\"log-level\" class=\"mock-filter\" onchange=\"filterLog()\">\n"
        "<option value=\"\">All levels</option>\n"
        "<option value=\"error\">error</option>\n"
        "<option value=\"fatal\">fatal</option>\n"
        "<option value=\"warn\">warn</option>\n"
        "<option value=\"info\">info</option>\n"
        "<option value=\"debug\">debug</option>\n"
        "<option value=\"trace\">trace</option>\n"
        "<option value=\"other\">other</option>\n"
        "</select>\n"
        "<select id=\"log-event\" class=\"mock-filter\" onchange=\"filterLog()\">\n"
        "<option value=\"\">All events</option>\n"
        "<option value=\"motion_detected\">Motion detected</option>\n"
        "<option value=\"push_notification\">Push notification</option>\n"
        "<option value=\"audio_detected\">Audio detected</option>\n"
        "<option value=\"streaming\">Streaming</option>\n"
        "<option value=\"stream_failed\">Stream failed</option>\n"
        "<option value=\"connected\">Connected</option>\n"
        "<option value=\"disconnected\">Disconnected</option>\n"
        "<option value=\"recording\">Recording</option>\n"
        "<option value=\"idle\">Idle / standby</option>\n"
        "<option value=\"boot\">Boot</option>\n"
        "<option value=\"reboot\">Reboot</option>\n"
        "<option value=\"wifi\">Wi-Fi</option>\n"
        "<option value=\"other\">Other</option>\n"
        "</select>\n"
        "<span id=\"log-match-count\" style=\"font-size:0.72rem;color:#8b949e;margin-left:auto;\"></span>\n"
        "</div>\n"
        "<div class=\"table-wrap\">\n"
        "<table class=\"data-table\" id=\"full-log-table\">\n<thead><tr>"
        "<th>Time</th><th>Lvl</th><th>Event</th><th>Message</th><th>Raw</th></tr></thead>\n<tbody>\n"
        f"{log_table_body}\n"
        "</tbody></table>\n</div>\n</div>\n</div>\n"
        "<footer class=\"footer-bar\">\n"
        "<span>log_parser.py · build_html()</span>\n"
        "<span>self-contained HTML · open in browser</span>\n"
        "</footer>\n</div>\n"
        f"{full_log_script}\n"
        "</body>\n</html>"
    )


def write_html(entries: list[dict[str, Any]], output_path: str, title: str | None = None) -> None:
    """Write HTML report to output_path. Uses default title if not provided."""
    if title is None:
        title = "Log parse report"
    html_content = build_html(entries, title=title)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
