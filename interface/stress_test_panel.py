"""Persistent FW stress-test panel: dual-firmware cycle tracking (main window dock)."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.build_info import (
    BUILD_INFO_SHELL,
    parse_build_info,
    parse_onboarded_from_device_info_text,
)
from core.fw_setup_service import build_camera_fota_url_for_folder
from core.local_server import check_server_status, stop_http_server

ShellAsyncFn = Callable[[str, list[str], Callable[[bool, str], None]], None]

_ACCENT = "#00897B"
_INFO_BG = "#1a237e"
_MUTED = "#9e9e9e"
_OK = "#4caf7d"
_ERR = "#e05555"
_WARN = "#c9a227"
_NEUTRAL = "#252525"


def _norm_ver(s: str) -> str:
    t = (s or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    return t


def versions_match(expected: str, reported: str) -> bool:
    """Best-effort match between UpdateRules label and build_info fw string."""
    e = _norm_ver(expected)
    g = _norm_ver(reported)
    if not e or e == "—":
        return False
    if not g:
        return False
    if e == g:
        return True
    if e in g or g in e:
        return True
    me = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", e)
    mg = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", g)
    if me and mg and me.group(1) == mg.group(1):
        return True
    return False


@dataclass
class StressTestConfig:
    fw_root: str
    folder_a: str
    folder_b: str
    version_label_a: str
    version_label_b: str


@dataclass
class _CycleHistoryEntry:
    cycle: int
    folder: str
    version: str
    result: str
    duration_sec: float


@dataclass
class _StepState:
    label: str
    done: bool = False
    ts: str | None = None


class StressTestPanel(QWidget):
    """Tracks alternating A/B firmware cycles; uses shell_async for camera I/O."""

    test_ended = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shell_async: ShellAsyncFn | None = None
        self._cfg: StressTestConfig | None = None
        self._active = False
        self._gui_connected = False
        self._profile_ok = False
        self._poll_busy = False

        self._cycle_num = 0
        self._use_a_next = True
        self._cycle_started_at: datetime | None = None
        self._passes = 0
        self._fails = 0
        self._history: list[_CycleHistoryEntry] = []

        self._steps: list[_StepState] = []
        self._verify_done = False
        self._saw_disconnect_since_url = False
        self._pending_reconnect = False
        self._saw_disconnect_after_verify = False
        self._polled_onboarded: bool | None = None
        self._polled_fw: str | None = None

        self._server_running = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        title = QLabel("FW stress test")
        title.setStyleSheet(f"color: {_ACCENT}; font-size: 13px; font-weight: bold;")
        outer.addWidget(title)

        self._banner_server = QLabel("")
        self._banner_server.setWordWrap(True)
        self._banner_server.setStyleSheet(f"color: {_WARN}; font-size: 11px;")
        outer.addWidget(self._banner_server)

        cards = QHBoxLayout()
        self._card_next = QFrame()
        self._card_next.setStyleSheet(
            f"QFrame {{ background-color: {_INFO_BG}; border: 1px solid #3949ab; border-radius: 8px; }}"
        )
        nl = QVBoxLayout(self._card_next)
        nl.setContentsMargins(8, 8, 8, 8)
        nl.addWidget(QLabel("Next up"))
        self._lbl_next_folder = QLabel("—")
        self._lbl_next_folder.setStyleSheet("color: #e8eaf6; font-weight: bold;")
        self._lbl_next_ver = QLabel("—")
        self._lbl_next_ver.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        nl.addWidget(self._lbl_next_folder)
        nl.addWidget(self._lbl_next_ver)

        self._card_after = QFrame()
        self._card_after.setStyleSheet(
            f"QFrame {{ background-color: {_NEUTRAL}; border: 1px solid #2a313a; border-radius: 8px; }}"
        )
        al = QVBoxLayout(self._card_after)
        al.setContentsMargins(8, 8, 8, 8)
        al.addWidget(QLabel("After this"))
        self._lbl_after_folder = QLabel("—")
        self._lbl_after_folder.setStyleSheet("color: #e8eef4; font-weight: bold;")
        self._lbl_after_ver = QLabel("—")
        self._lbl_after_ver.setStyleSheet(f"color: {_MUTED}; font-size: 11px;")
        al.addWidget(self._lbl_after_folder)
        al.addWidget(self._lbl_after_ver)

        cards.addWidget(self._card_next, 1)
        cards.addWidget(self._card_after, 1)
        outer.addLayout(cards)

        stats = QHBoxLayout()
        self._lbl_cycle = QLabel("Cycle: —")
        self._lbl_pass = QLabel("Pass: 0")
        self._lbl_fail = QLabel("Fail: 0")
        for lb in (self._lbl_cycle, self._lbl_pass, self._lbl_fail):
            lb.setStyleSheet(f"color: #c5ced9; font-size: 11px;")
        stats.addWidget(self._lbl_cycle)
        stats.addWidget(self._lbl_pass)
        stats.addWidget(self._lbl_fail)
        stats.addStretch(1)
        stats.addWidget(QLabel("Target cycles (0=∞)"))
        self._spin_target = QSpinBox()
        self._spin_target.setRange(0, 9999)
        self._spin_target.setValue(0)
        self._spin_target.setFixedWidth(72)
        stats.addWidget(self._spin_target)
        outer.addLayout(stats)

        self._checklist_host = QVBoxLayout()
        self._checklist_host.setSpacing(4)
        checklist_wrap = QWidget()
        checklist_wrap.setLayout(self._checklist_host)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(140)
        scroll.setWidget(checklist_wrap)
        outer.addWidget(scroll)

        btn_row = QHBoxLayout()
        self._btn_pass = QPushButton("Pass (verify)")
        self._btn_fail = QPushButton("Fail (verify)")
        self._btn_pass.setStyleSheet(
            f"QPushButton {{ background-color: #2e7d32; color: white; padding: 6px 14px; }}"
        )
        self._btn_fail.setStyleSheet(
            f"QPushButton {{ background-color: #c62828; color: white; padding: 6px 14px; }}"
        )
        self._btn_pass.clicked.connect(lambda: self._on_verify_result(True))
        self._btn_fail.clicked.connect(lambda: self._on_verify_result(False))
        btn_row.addWidget(self._btn_pass)
        btn_row.addWidget(self._btn_fail)
        outer.addLayout(btn_row)

        self._btn_next_cycle = QPushButton("Start next cycle")
        self._btn_next_cycle.setStyleSheet(
            f"QPushButton {{ background-color: {_ACCENT}; color: white; padding: 10px 16px; font-weight: bold; }}"
        )
        self._btn_next_cycle.clicked.connect(self._on_start_next_cycle)
        outer.addWidget(self._btn_next_cycle)

        self._history_toggle = QPushButton("▼ History")
        self._history_toggle.setCheckable(True)
        self._history_toggle.setChecked(False)
        self._history_toggle.clicked.connect(self._toggle_history)
        outer.addWidget(self._history_toggle)

        self._history_text = QPlainTextEdit()
        self._history_text.setReadOnly(True)
        self._history_text.setMaximumHeight(120)
        self._history_text.hide()
        outer.addWidget(self._history_text)

        hist_btns = QHBoxLayout()
        self._btn_export = QPushButton("Export…")
        self._btn_export.clicked.connect(self._export_history)
        hist_btns.addWidget(self._btn_export)
        hist_btns.addStretch(1)
        self._history_btn_row = QWidget()
        self._history_btn_row.setLayout(hist_btns)
        self._history_btn_row.hide()
        outer.addWidget(self._history_btn_row)

        bot = QHBoxLayout()
        self._btn_stop = QPushButton("Stop server")
        self._btn_stop.clicked.connect(self._on_stop_server)
        self._btn_end = QPushButton("End test")
        self._btn_end.clicked.connect(self._on_end_test)
        bot.addWidget(self._btn_stop)
        bot.addWidget(self._btn_end)
        bot.addStretch(1)
        outer.addLayout(bot)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(3500)
        self._poll_timer.timeout.connect(self._poll_tick)

        self._clear_ui_idle()
        self._tick_server_banner()

    def set_shell_async(self, fn: ShellAsyncFn) -> None:
        self._shell_async = fn

    def begin_test(
        self,
        cfg: StressTestConfig,
        *,
        from_wizard_initial: bool,
        gui_connected_now: bool = False,
    ) -> None:
        self._cfg = cfg
        self._active = True
        self._cycle_num = 1
        self._use_a_next = True
        self._passes = 0
        self._fails = 0
        self._history.clear()
        self._verify_done = False
        self._polled_onboarded = None
        self._polled_fw = None
        self._cycle_started_at = datetime.now()
        self._init_steps_for_new_cycle(from_wizard_initial=from_wizard_initial)
        if from_wizard_initial and gui_connected_now and len(self._steps) > 1:
            self._steps[1].done = True
            self._steps[1].ts = _ts()
            self._pending_reconnect = False
        self._update_firmware_cards()
        self._rebuild_checklist()
        self._refresh_stats()
        self._poll_timer.start()
        self._apply_enabled_state()
        self._tick_server_banner()

    def _soft_reset_after_end(self) -> None:
        self._poll_timer.stop()
        self._active = False
        self._cfg = None
        self._history.clear()
        self._clear_ui_idle()
        self.test_ended.emit()

    def _clear_ui_idle(self) -> None:
        self._banner_server.setText(
            "No stress test active. In the FW Wizard, use Choose mode → Stress test, then finish the flow to begin."
        )
        self._lbl_next_folder.setText("—")
        self._lbl_next_ver.setText("—")
        self._lbl_after_folder.setText("—")
        self._lbl_after_ver.setText("—")
        self._lbl_cycle.setText("Cycle: —")
        while self._checklist_host.count():
            item = self._checklist_host.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for b in (
            self._btn_pass,
            self._btn_fail,
            self._btn_next_cycle,
            self._btn_stop,
            self._btn_end,
            self._btn_export,
        ):
            b.setEnabled(False)
        self._spin_target.setEnabled(False)

    def apply_state(self, info: dict[str, Any]) -> None:
        was = self._gui_connected
        self._gui_connected = bool(info.get("connected"))
        if "command_profile" in info:
            self._profile_ok = (info.get("command_profile") or "") == "e3_wired"
        if not self._active:
            return

        if was and not self._gui_connected:
            self._saw_disconnect_since_url = True
            if self._verify_done:
                self._saw_disconnect_after_verify = True

        self._apply_enabled_state()
        self._maybe_advance_reconnect_step()

    def _current_folders(self) -> tuple[str, str, str, str]:
        """Returns (next_folder, next_ver, after_folder, after_ver)."""
        assert self._cfg is not None
        if self._use_a_next:
            return (
                self._cfg.folder_a,
                self._cfg.version_label_a,
                self._cfg.folder_b,
                self._cfg.version_label_b,
            )
        return (
            self._cfg.folder_b,
            self._cfg.version_label_b,
            self._cfg.folder_a,
            self._cfg.version_label_a,
        )

    def _update_firmware_cards(self) -> None:
        if not self._cfg:
            return
        nf, nv, af, av = self._current_folders()
        self._lbl_next_folder.setText(nf)
        self._lbl_next_ver.setText(nv)
        self._lbl_after_folder.setText(af)
        self._lbl_after_ver.setText(av)

    def _init_steps_for_new_cycle(self, *, from_wizard_initial: bool) -> None:
        self._steps = [
            _StepState("Set update URL + reboot"),
            _StepState("Camera reconnected"),
            _StepState("Onboard camera (claimed)"),
            _StepState("Camera updated to target FW"),
            _StepState("Verify"),
            _StepState("Deregister / factory reset (unclaimed + reconnect)"),
        ]
        self._verify_done = False
        self._saw_disconnect_since_url = False
        self._saw_disconnect_after_verify = False
        self._pending_reconnect = not from_wizard_initial
        if from_wizard_initial:
            self._steps[0].done = True
            self._steps[0].ts = _ts()
            self._pending_reconnect = True
            self._saw_disconnect_since_url = False

    def _rebuild_checklist(self) -> None:
        while self._checklist_host.count():
            item = self._checklist_host.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, st in enumerate(self._steps):
            row = QHBoxLayout()
            ic = QLabel("○")
            if st.done:
                ic.setText("✓")
                ic.setStyleSheet(f"color: {_OK};")
            elif self._is_step_current(i):
                ic.setText("◉")
                ic.setStyleSheet(f"color: {_ACCENT};")
            else:
                ic.setStyleSheet(f"color: {_MUTED};")
            row.addWidget(ic)
            lab = QLabel(st.label)
            lab.setStyleSheet("color: #c5ced9; font-size: 11px;")
            row.addWidget(lab, 1)
            if st.ts:
                ts = QLabel(st.ts)
                ts.setStyleSheet(f"color: {_MUTED}; font-size: 10px;")
                row.addWidget(ts)
            w = QWidget()
            w.setLayout(row)
            self._checklist_host.addWidget(w)

    def _is_step_current(self, idx: int) -> bool:
        if not self._steps:
            return False
        if self._steps[idx].done:
            return False
        for j in range(idx):
            if not self._steps[j].done:
                return False
        return True

    def _refresh_stats(self) -> None:
        self._lbl_cycle.setText(f"Cycle: {self._cycle_num}" if self._active else "Cycle: —")
        self._lbl_pass.setText(f"Pass: {self._passes}")
        self._lbl_fail.setText(f"Fail: {self._fails}")

    def _apply_enabled_state(self) -> None:
        if not self._active or not self._cfg:
            return
        running, _ = check_server_status()
        self._server_running = running
        ok = running and self._gui_connected and self._profile_ok and bool(self._shell_async)
        self._spin_target.setEnabled(ok)
        self._btn_stop.setEnabled(running)
        self._btn_end.setEnabled(True)
        target = int(self._spin_target.value())
        cycle_complete = self._is_cycle_complete()
        at_target = target > 0 and self._cycle_num >= target and cycle_complete
        can_next = (
            ok
            and cycle_complete
            and not (target > 0 and self._cycle_num >= target)
            and self._steps
            and all(s.done for s in self._steps)
        )
        self._btn_next_cycle.setEnabled(can_next)
        if at_target:
            self._btn_next_cycle.setText("Target reached")
        else:
            self._btn_next_cycle.setText("Start next cycle")

        verify_ready = ok and self._steps and self._steps[3].done and not self._verify_done
        self._btn_pass.setEnabled(verify_ready)
        self._btn_fail.setEnabled(verify_ready)
        self._btn_export.setEnabled(self._active and bool(self._history))

    def _is_cycle_complete(self) -> bool:
        return bool(self._steps) and all(s.done for s in self._steps)

    def _maybe_advance_reconnect_step(self) -> None:
        if not self._active or not self._steps:
            return
        if not self._steps[0].done:
            return
        if self._steps[1].done:
            return
        if self._pending_reconnect and self._gui_connected and self._saw_disconnect_since_url:
            self._steps[1].done = True
            self._steps[1].ts = _ts()
            self._pending_reconnect = False
            self._rebuild_checklist()
            self._apply_enabled_state()

    def _poll_tick(self) -> None:
        if not self._active or not self._cfg or not self._shell_async:
            return
        if not self._gui_connected or not self._profile_ok:
            return
        if self._poll_busy:
            return
        self._poll_busy = True
        self._poll_device_info_chain("")

    def _poll_device_info_chain(self, acc: str) -> None:
        if not self._shell_async:
            self._poll_busy = False
            return

        def after_di(ok: bool, text: str) -> None:
            part = text or ""
            blob = (acc + "\n" + part) if acc else part
            self._shell_async("arlocmd bs_info", [], lambda ok2, t2: after_bs(blob, ok2, t2))

        def after_bs(blob: str, ok2: bool, t2: str) -> None:
            part2 = t2 or ""
            blob2 = (blob + "\n" + part2) if blob else part2
            ob = parse_onboarded_from_device_info_text(blob2)
            if ob is not None:
                self._polled_onboarded = ob
            self._shell_async(BUILD_INFO_SHELL, [], lambda ok3, t3: after_bi(ok3, t3))

        def after_bi(ok3: bool, t3: str) -> None:
            self._poll_busy = False
            if ok3 and t3:
                parsed = parse_build_info(t3)
                fv = parsed.get("fw_version")
                if fv:
                    self._polled_fw = str(fv).strip()
            self._apply_poll_derived_steps()

        self._shell_async("arlocmd device_info", [], lambda ok, t: after_di(ok, t))

    def _apply_poll_derived_steps(self) -> None:
        if not self._active or not self._steps:
            return
        changed = False
        nf, nv, _af, _av = self._current_folders()

        if self._is_step_current(2) and self._polled_onboarded is True:
            self._steps[2].done = True
            self._steps[2].ts = _ts()
            changed = True

        if self._is_step_current(3) and self._polled_fw and versions_match(nv, self._polled_fw):
            self._steps[3].done = True
            self._steps[3].ts = _ts()
            changed = True

        if (
            self._verify_done
            and self._is_step_current(5)
            and self._polled_onboarded is False
            and self._gui_connected
            and self._saw_disconnect_after_verify
        ):
            self._steps[5].done = True
            self._steps[5].ts = _ts()
            changed = True

        if changed:
            self._rebuild_checklist()
        self._apply_enabled_state()

    @Slot()
    def _on_verify_result(self, passed: bool) -> None:
        if not self._active or not self._cfg or not self._steps:
            return
        if not self._steps[3].done:
            QMessageBox.information(
                self,
                "Stress test",
                "Wait until the panel detects the target firmware version (or confirm the device updated), "
                "then mark Pass or Fail.",
            )
            return
        self._steps[4].done = True
        self._steps[4].ts = _ts()
        self._verify_done = True
        if passed:
            self._passes += 1
        else:
            self._fails += 1
        dur = 0.0
        if self._cycle_started_at:
            dur = (datetime.now() - self._cycle_started_at).total_seconds()
        nf, nv, _x, _y = self._current_folders()
        self._history.append(
            _CycleHistoryEntry(
                cycle=self._cycle_num,
                folder=nf,
                version=nv,
                result="pass" if passed else "fail",
                duration_sec=dur,
            )
        )
        self._refresh_stats()
        self._append_history_view()
        self._rebuild_checklist()
        self._saw_disconnect_after_verify = False
        self._apply_enabled_state()

    @Slot()
    def _on_start_next_cycle(self) -> None:
        if not self._active or not self._cfg or not self._shell_async:
            return
        if not self._btn_next_cycle.isEnabled():
            return
        self._use_a_next = not self._use_a_next
        self._cycle_num += 1
        self._cycle_started_at = datetime.now()
        self._verify_done = False
        self._polled_onboarded = None
        self._polled_fw = None
        self._init_steps_for_new_cycle(from_wizard_initial=False)
        self._update_firmware_cards()
        self._rebuild_checklist()
        self._refresh_stats()

        nf, _nv, _af, _av = self._current_folders()
        ok_b, err_b, url = build_camera_fota_url_for_folder(self._cfg.fw_root, nf)
        if not ok_b:
            QMessageBox.warning(self, "Stress test", err_b or "Cannot build URL.")
            return

        def after_reboot(ok_r: bool, msg_r: str) -> None:
            if not ok_r:
                QMessageBox.warning(self, "Stress test", msg_r or "Reboot failed.")
            self._steps[0].done = True
            self._steps[0].ts = _ts()
            self._pending_reconnect = True
            self._saw_disconnect_since_url = False
            self._rebuild_checklist()
            self._apply_enabled_state()

        def after_url(ok: bool, msg: str) -> None:
            if not ok:
                QMessageBox.warning(self, "Stress test", msg or "update_url failed.")
                self._apply_enabled_state()
                return
            self._shell_async("arlocmd reboot", [], after_reboot)

        self._shell_async("arlocmd update_url", [url], after_url)

    @Slot()
    def _toggle_history(self) -> None:
        on = self._history_toggle.isChecked()
        self._history_text.setVisible(on)
        self._history_btn_row.setVisible(on)
        self._history_toggle.setText("▼ History" if not on else "▲ History")

    def _append_history_view(self) -> None:
        lines = []
        for e in self._history:
            lines.append(
                f"Cycle {e.cycle} | {e.folder} | {e.version} | {e.result} | {e.duration_sec:.1f}s"
            )
        self._history_text.setPlainText("\n".join(lines))

    @Slot()
    def _export_history(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export stress test history", "", "CSV (*.csv);;Text (*.txt)"
        )
        if not path:
            return
        try:
            if path.lower().endswith(".csv"):
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["cycle", "folder", "version", "result", "duration_sec"])
                    for e in self._history:
                        w.writerow([e.cycle, e.folder, e.version, e.result, f"{e.duration_sec:.3f}"])
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._history_text.toPlainText())
        except OSError as ex:
            QMessageBox.warning(self, "Stress test", str(ex))

    @Slot()
    def _on_stop_server(self) -> None:
        ok, msg = stop_http_server()
        if not ok:
            QMessageBox.warning(self, "Stress test", msg or "Could not stop server.")
        self._tick_server_banner()
        self._apply_enabled_state()

    def _tick_server_banner(self) -> None:
        running, _ = check_server_status()
        if not self._active:
            return
        if not running:
            self._banner_server.setText("Server stopped — start the firmware HTTP server again (FW Wizard or server commands) to continue.")
        else:
            self._banner_server.setText("")

    @Slot()
    def _on_end_test(self) -> None:
        r = QMessageBox.question(
            self,
            "End stress test",
            "Stop the test and close this panel? Export history first if you need it.\n\n"
            "This stops the local firmware HTTP server in this window.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        stop_http_server()
        self._soft_reset_after_end()
        w = self.window()
        d = w.findChild(QDockWidget, "StressTestDock")
        if d is not None:
            d.hide()

    def blocks_quick_switch(self) -> bool:
        return self._active


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")
