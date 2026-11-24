"""
JS <-> Python bridge for 4-Bus Digital Matrix Mixer (pywebview WebView2).
Wraps MatrixMixerEngine from audio_engine.py WITHOUT modifying any audio/DSP logic.
Handles thread-safe parameter updates, Pair Link (1/2 & 3/4), 30 Hz meter/LUFS push,
settings.json persistence (with auto-migration from mixer_config.json), custom presets,
and error logging to app.log.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import winreg
from typing import Any, Callable, Dict, List, Optional, Tuple

from audio_engine import (
    FILTER_MODES,
    SOURCE_MODES,
    TAP_MODES,
    TONE_TYPES,
    MatrixMixerEngine,
    linear_to_db,
)
from manage_endpoints import apply_endpoint_names


def get_app_dir() -> str:
    """Writable directory for settings.json and app.log (works in script and PyInstaller .exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_resource_path(rel_path: str) -> str:
    """Bundled read-only resource path (supports PyInstaller _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel_path)


APP_DIR = get_app_dir()
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
LEGACY_CONFIG_PATH = os.path.join(APP_DIR, "mixer_config.json")
LOG_PATH = os.path.join(APP_DIR, "app.log")

STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VAL_NAME = "4-BusMatrixMaster"


def get_startup_command() -> str:
    """Command registered in Windows HKCU Run key (launches minimized to tray on boot)."""
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}" --minimized'
    # Prefer dist\4-Bus-Matrix-Master.exe if present, else pythonw.exe main.py
    exe_candidate = os.path.join(APP_DIR, "dist", "4-Bus-Matrix-Master.exe")
    if os.path.exists(exe_candidate):
        return f'"{exe_candidate}" --minimized'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    py_exe = pyw if os.path.exists(pyw) else sys.executable
    main_py = os.path.join(APP_DIR, "main.py")
    return f'"{py_exe}" "{main_py}" --minimized'


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, STARTUP_VAL_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def set_autostart_registry(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, STARTUP_VAL_NAME, 0, winreg.REG_SZ, get_startup_command())
        else:
            try:
                winreg.DeleteValue(k, STARTUP_VAL_NAME)
            except FileNotFoundError:
                pass

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("matrix_mixer")

BUILTIN_PRESETS: List[str] = [
    "Speaker Mixing (1/2 Full) + Sub (3/4 LPF)",
    "Crossover Split: Tops (1/2 HPF) + Sub (3/4 LPF)",
    "Dual Monitor: Mixing (1/2 Full) + Monitor 2 (3/4 Full)",
    "L/R Split + Dual Sub (80Hz/60Hz)",
]

# Default HPF/LPF per built-in preset for double-click knob reset
PRESET_FILTER_DEFAULTS: Dict[str, List[Tuple[float, float]]] = {
    "Speaker Mixing (1/2 Full) + Sub (3/4 LPF)": [(90.0, 120.0), (90.0, 120.0), (30.0, 90.0), (30.0, 90.0)],
    "Crossover Split: Tops (1/2 HPF) + Sub (3/4 LPF)": [(90.0, 120.0), (90.0, 120.0), (30.0, 90.0), (30.0, 90.0)],
    "Dual Monitor: Mixing (1/2 Full) + Monitor 2 (3/4 Full)": [(90.0, 120.0), (90.0, 120.0), (90.0, 120.0), (90.0, 120.0)],
    "L/R Split + Dual Sub (80Hz/60Hz)": [(80.0, 120.0), (80.0, 120.0), (30.0, 80.0), (25.0, 60.0)],
}

DEFAULT_PAN_BY_BUS = [-1.0, 1.0, -1.0, 1.0]  # OUT1 & OUT3 = L100, OUT2 & OUT4 = R100


class Api:
    """pywebview js_api bridge exposing MatrixMixerEngine safely to HTML/JS UI."""

    def __init__(self, settings_path: str = SETTINGS_PATH) -> None:
        self.settings_path = settings_path
        self.engine = MatrixMixerEngine(sample_rate=48000, blocksize=512)
        self._lock = threading.RLock()
        self._window = None
        self._on_hide_cb: Optional[Callable[[], None]] = None
        self._on_exit_cb: Optional[Callable[[], None]] = None

        # Cached device lists
        self._captures: List[Tuple[Optional[int], str]] = []
        self._outputs: List[Tuple[int, str]] = []
        self._default_out_id: Optional[int] = None
        self._cap_by_label: Dict[str, Optional[int]] = {}
        self._cap_by_id: Dict[Optional[int], str] = {}
        self._out_by_label: Dict[str, int] = {}
        self._out_by_id: Dict[int, str] = {}

        # UI/App state persisted in settings.json
        self.primary_output_label: str = ""
        self.active_preset: str = BUILTIN_PRESETS[0]
        self.custom_presets: Dict[str, Dict[str, Any]] = {}
        self.link_12: bool = True
        self.link_34: bool = True
        self.window_size: Tuple[int, int] = (1320, 780)
        self.last_error: str = ""
        self.rec_path: str = ""

        # Debounced save timer
        self._save_timer: Optional[threading.Timer] = None

        # 30 Hz meter push thread
        self._meter_stop = threading.Event()
        self._meter_thread: Optional[threading.Thread] = None

        # Initialize endpoints, devices, and load saved settings
        self._init_devices()
        self.load_settings()

    # ------------------------------------------------------------------
    # Lifecycle & Meter Push Thread (30 Hz)
    # ------------------------------------------------------------------
    def attach_window(self, window) -> None:
        self._window = window

    def attach_lifecycle_handlers(
        self, on_hide: Callable[[], None], on_exit: Callable[[], None]
    ) -> None:
        self._on_hide_cb = on_hide
        self._on_exit_cb = on_exit

    def start_meter_push(self) -> None:
        if self._meter_thread is not None and self._meter_thread.is_alive():
            return
        self._meter_stop.clear()
        self._meter_thread = threading.Thread(target=self._meter_loop, name="MeterPush30Hz", daemon=True)
        self._meter_thread.start()

    def stop_meter_push(self) -> None:
        self._meter_stop.set()
        if self._meter_thread is not None:
            self._meter_thread.join(timeout=0.5)
            self._meter_thread = None

    def _meter_loop(self) -> None:
        interval = 1.0 / 30.0
        while not self._meter_stop.is_set():
            t0 = time.perf_counter()
            win = self._window
            if win is not None:
                try:
                    eng = self.engine
                    buses_db = [round(linear_to_db(ch.peak_linear, -60.0), 2) for ch in eng.channels]
                    ml_db = round(linear_to_db(eng.master_peak_l, -60.0), 2)
                    mr_db = round(linear_to_db(eng.master_peak_r, -60.0), 2)
                    pk_db = round(max(ml_db, mr_db), 2)
                    payload = json.dumps(
                        {
                            "buses": buses_db,
                            "master_l": ml_db,
                            "master_r": mr_db,
                            "peak": pk_db,
                            "clipped": bool(eng.master_clipped),
                            "lufs_st": round(float(eng.lufs_meter.short_term_lufs), 2),
                            "lufs_int": round(float(eng.lufs_meter.integrated_lufs), 2),
                            "running": bool(eng.running),
                            "recording": bool(eng.recording),
                            "sample_rate": int(eng.sample_rate),
                        },
                        separators=(",", ":"),
                    )
                    win.evaluate_js(f"window.__onMeters && window.__onMeters({payload})")
                except Exception:
                    # Window may be closing or not yet loaded
                    pass
            elapsed = time.perf_counter() - t0
            sleep_s = interval - elapsed
            if sleep_s > 0:
                self._meter_stop.wait(sleep_s)

    def shutdown(self) -> None:
        self.stop_meter_push()
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        self.save_settings()
        try:
            self.engine.stop_recording()
        except Exception as exc:
            logger.exception("stop_recording error on shutdown: %s", exc)
        try:
            self.engine.stop()
        except Exception as exc:
            logger.exception("engine.stop error on shutdown: %s", exc)

    # ------------------------------------------------------------------
    # Device Discovery & Smart Defaults
    # ------------------------------------------------------------------
    def _init_devices(self) -> None:
        try:
            apply_endpoint_names(restore=False)
        except Exception as exc:
            logger.warning("apply_endpoint_names warning: %s", exc)

        try:
            captures, outputs, default_out_id = MatrixMixerEngine.list_devices()
        except Exception as exc:
            logger.exception("list_devices failed: %s", exc)
            self.last_error = f"Gagal membaca device audio WASAPI: {exc}"
            captures, outputs, default_out_id = [(None, "None (Muted)")], [], None

        with self._lock:
            self._captures = captures
            self._outputs = outputs
            self._default_out_id = default_out_id
            self._cap_by_label = {lbl: idx for idx, lbl in captures}
            self._cap_by_id = {idx: lbl for idx, lbl in captures}
            self._out_by_label = {lbl: idx for idx, lbl in outputs}
            self._out_by_id = {idx: lbl for idx, lbl in outputs}

            # Default Primary Output: Main Out 1/2 (Studio 26c) or system default
            studio_main = next((lbl for _, lbl in outputs if "Main Out 1/2" in lbl), None)
            if not self.primary_output_label or self.primary_output_label not in self._out_by_label:
                self.primary_output_label = (
                    studio_main
                    or self._out_by_id.get(default_out_id, "")
                    or (outputs[0][1] if outputs else "")
                )

            # Default Capture: [OUT TAP] 4-Bus Matrix Master [Loopback]
            master_tap_id = next(
                (idx for idx, lbl in captures if idx is not None and "4-Bus Matrix Master" in lbl),
                None,
            )
            if master_tap_id is None:
                loop_ids = [idx for idx, lbl in captures if idx is not None and "[OUT TAP]" in lbl]
                master_tap_id = loop_ids[0] if loop_ids else None

            main12_id = next((idx for idx, lbl in outputs if "Main Out 1/2" in lbl), default_out_id)
            line34_id = next((idx for idx, lbl in outputs if "Line Out 3/4" in lbl), main12_id)
            default_targets = [main12_id, main12_id, line34_id, line34_id]

            for ch, tgt_id in zip(self.engine.channels, default_targets):
                if ch.capture_device_id is None or ch.capture_device_id not in self._cap_by_id:
                    ch.capture_device_id = master_tap_id
                if ch.target_output_id is None or ch.target_output_id not in self._out_by_id:
                    ch.target_output_id = tgt_id

    # ------------------------------------------------------------------
    # Settings Persistence (settings.json + migration from mixer_config.json)
    # ------------------------------------------------------------------
    def _schedule_save(self) -> None:
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(0.35, self.save_settings)
            self._save_timer.daemon = True
            self._save_timer.start()

    def _serialize_channels(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for ch in self.engine.channels:
            out.append(
                {
                    "target_output": self._out_by_id.get(ch.target_output_id, ""),
                    "source_mode": ch.source_mode,
                    "capture_device": self._cap_by_id.get(ch.capture_device_id, "None (Muted)"),
                    "tap_mode": ch.tap_mode,
                    "tone_type": ch.tone_type,
                    "file_path": ch.file_path,
                    "filter_mode": ch.filter_mode,
                    "hpf_hz": round(float(ch.hpf_hz), 1),
                    "lpf_hz": round(float(ch.lpf_hz), 1),
                    "gain_db": round(float(ch.gain_db), 2),
                    "pan": round(float(ch.pan), 3),
                    "phase_invert": bool(ch.phase_invert),
                    "mute": bool(ch.mute),
                }
            )
        return out

    def save_settings(self) -> None:
        with self._lock:
            data = {
                "primary_output": self.primary_output_label,
                "preset": self.active_preset,
                "link_12": bool(self.link_12),
                "link_34": bool(self.link_34),
                "master_gain_db": round(float(self.engine.master_gain_db), 2),
                "limiter_enabled": bool(self.engine.limiter_enabled),
                "master_mono": bool(self.engine.master_mono),
                "master_dim": bool(self.engine.master_dim),
                "window_size": [int(self.window_size[0]), int(self.window_size[1])],
                "channels": self._serialize_channels(),
                "custom_presets": self.custom_presets,
            }
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.exception("Failed saving settings.json: %s", exc)

    def load_settings(self) -> None:
        src_path = self.settings_path
        migrated = False
        if not os.path.exists(src_path) and os.path.exists(LEGACY_CONFIG_PATH):
            src_path = LEGACY_CONFIG_PATH
            migrated = True
        if not os.path.exists(src_path):
            return

        try:
            with open(src_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.exception("Failed reading %s: %s", src_path, exc)
            return

        with self._lock:
            pri_lbl = data.get("primary_output", "")
            if pri_lbl in self._out_by_label:
                self.primary_output_label = pri_lbl

            self.custom_presets = data.get("custom_presets", {}) or {}
            preset = data.get("preset", "")
            if preset in BUILTIN_PRESETS or preset in self.custom_presets:
                self.active_preset = preset

            self.link_12 = bool(data.get("link_12", True))
            self.link_34 = bool(data.get("link_34", True))

            ws = data.get("window_size")
            if isinstance(ws, list) and len(ws) == 2:
                self.window_size = (max(1100, int(ws[0])), max(700, int(ws[1])))

            self.engine.master_gain_db = max(-60.0, min(12.0, float(data.get("master_gain_db", 0.0))))
            self.engine.limiter_enabled = bool(data.get("limiter_enabled", True))
            self.engine.master_mono = bool(data.get("master_mono", False))
            self.engine.master_dim = bool(data.get("master_dim", False))

            self._apply_channel_dicts(data.get("channels", []))

        if migrated:
            logger.info("Migrated legacy mixer_config.json -> settings.json")
            self.save_settings()

    def _apply_channel_dicts(self, ch_list: List[Dict[str, Any]]) -> None:
        for ch, cdata in zip(self.engine.channels, ch_list):
            tgt_lbl = cdata.get("target_output", "")
            if tgt_lbl in self._out_by_label:
                ch.target_output_id = self._out_by_label[tgt_lbl]

            cap_lbl = cdata.get("capture_device", "")
            if cap_lbl in self._cap_by_label:
                ch.capture_device_id = self._cap_by_label[cap_lbl]

            s_mode = cdata.get("source_mode", ch.source_mode)
            if s_mode in SOURCE_MODES:
                ch.source_mode = s_mode

            f_path = cdata.get("file_path", "")
            if ch.source_mode == "Audio File" and f_path and os.path.exists(f_path):
                try:
                    self.engine.load_channel_file(ch.index, f_path)
                except Exception as exc:
                    logger.warning("Failed reloading audio file %s: %s", f_path, exc)
                    ch.source_mode = "Soundcard"

            tap = cdata.get("tap_mode", ch.tap_mode)
            if tap in TAP_MODES:
                ch.tap_mode = tap

            tone = cdata.get("tone_type", ch.tone_type)
            if tone in TONE_TYPES:
                ch.tone_type = tone

            fmode = cdata.get("filter_mode", ch.filter_mode)
            if fmode in FILTER_MODES:
                ch.filter_mode = fmode

            ch.hpf_hz = max(20.0, min(500.0, float(cdata.get("hpf_hz", ch.hpf_hz))))
            ch.lpf_hz = max(40.0, min(250.0, float(cdata.get("lpf_hz", ch.lpf_hz))))
            ch.gain_db = max(-60.0, min(12.0, float(cdata.get("gain_db", ch.gain_db))))
            ch.pan = max(-1.0, min(1.0, float(cdata.get("pan", ch.pan))))
            ch.phase_invert = bool(cdata.get("phase_invert", False))
            ch.mute = bool(cdata.get("mute", False))

    # ------------------------------------------------------------------
    # Pair Link Helper (matches legacy GUI behavior)
    # ------------------------------------------------------------------
    def _linked_indices(self, bus: int) -> List[int]:
        idx = int(bus)
        if idx < 0 or idx >= len(self.engine.channels):
            raise ValueError(f"Invalid bus index: {bus}")
        if idx in (0, 1) and self.link_12:
            return [idx, 1 - idx]
        if idx in (2, 3) and self.link_34:
            return [idx, 5 - idx]
        return [idx]

    # ------------------------------------------------------------------
    # Public JS Bridge API Methods
    # ------------------------------------------------------------------
    def get_devices(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "captures": [{"id": idx, "label": lbl} for idx, lbl in self._captures],
                "outputs": [{"id": idx, "label": lbl} for idx, lbl in self._outputs],
                "filter_modes": list(FILTER_MODES),
                "source_modes": list(SOURCE_MODES),
                "tap_modes": list(TAP_MODES),
                "tone_types": list(TONE_TYPES),
                "presets": BUILTIN_PRESETS + sorted(self.custom_presets.keys()),
                "builtin_presets": BUILTIN_PRESETS,
                "custom_presets": sorted(self.custom_presets.keys()),
            }

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            preset_defs = PRESET_FILTER_DEFAULTS.get(
                self.active_preset, [(90.0, 120.0), (90.0, 120.0), (30.0, 90.0), (30.0, 90.0)]
            )
            channels_state = []
            for i, ch in enumerate(self.engine.channels):
                def_hpf, def_lpf = preset_defs[i]
                channels_state.append(
                    {
                        "index": ch.index,
                        "name": ch.name,
                        "source_mode": ch.source_mode,
                        "capture_device_id": ch.capture_device_id,
                        "capture_device_label": self._cap_by_id.get(ch.capture_device_id, "None (Muted)"),
                        "target_output_id": ch.target_output_id,
                        "target_output_label": self._out_by_id.get(ch.target_output_id, ""),
                        "tap_mode": ch.tap_mode,
                        "tone_type": ch.tone_type,
                        "file_path": ch.file_path,
                        "file_name": os.path.basename(ch.file_path) if ch.file_path else "",
                        "filter_mode": ch.filter_mode,
                        "hpf_hz": round(float(ch.hpf_hz), 1),
                        "lpf_hz": round(float(ch.lpf_hz), 1),
                        "default_hpf_hz": def_hpf,
                        "default_lpf_hz": def_lpf,
                        "default_pan": DEFAULT_PAN_BY_BUS[i],
                        "gain_db": round(float(ch.gain_db), 2),
                        "pan": round(float(ch.pan), 3),
                        "phase_invert": bool(ch.phase_invert),
                        "mute": bool(ch.mute),
                        "solo": bool(ch.solo),
                    }
                )

            return {
                "ok": True,
                "running": bool(self.engine.running),
                "recording": bool(self.engine.recording),
                "rec_file": os.path.basename(self.rec_path) if self.rec_path else "",
                "sample_rate": int(self.engine.sample_rate),
                "primary_output_label": self.primary_output_label,
                "primary_output_id": self._out_by_label.get(self.primary_output_label),
                "preset": self.active_preset,
                "presets": BUILTIN_PRESETS + sorted(self.custom_presets.keys()),
                "custom_presets": sorted(self.custom_presets.keys()),
                "link_12": bool(self.link_12),
                "link_34": bool(self.link_34),
                "master_gain_db": round(float(self.engine.master_gain_db), 2),
                "master_mute": bool(self.engine.master_mute),
                "master_mono": bool(self.engine.master_mono),
                "master_dim": bool(self.engine.master_dim),
                "limiter_enabled": bool(self.engine.limiter_enabled),
                "autostart": is_autostart_enabled(),
                "error": self.last_error,
                "channels": channels_state,
            }

    def set_gain(self, bus: int, db: float) -> Dict[str, Any]:
        try:
            val = max(-60.0, min(12.0, float(db)))
            with self._lock:
                for idx in self._linked_indices(bus):
                    self.engine.channels[idx].gain_db = val
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_gain error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_hpf(self, bus: int, hz: float) -> Dict[str, Any]:
        try:
            val = max(20.0, min(500.0, float(hz)))
            with self._lock:
                for idx in self._linked_indices(bus):
                    self.engine.channels[idx].hpf_hz = val
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_hpf error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_lpf(self, bus: int, hz: float) -> Dict[str, Any]:
        try:
            val = max(40.0, min(250.0, float(hz)))
            with self._lock:
                for idx in self._linked_indices(bus):
                    self.engine.channels[idx].lpf_hz = val
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_lpf error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_pan(self, bus: int, value: float) -> Dict[str, Any]:
        try:
            val = max(-1.0, min(1.0, float(value)))
            with self._lock:
                self.engine.channels[int(bus)].pan = val
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_pan error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_mute(self, bus: int, enabled: bool) -> Dict[str, Any]:
        try:
            flag = bool(enabled)
            with self._lock:
                for idx in self._linked_indices(bus):
                    self.engine.channels[idx].mute = flag
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_mute error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_solo(self, bus: int, enabled: bool) -> Dict[str, Any]:
        try:
            flag = bool(enabled)
            with self._lock:
                for idx in self._linked_indices(bus):
                    self.engine.channels[idx].solo = flag
            return self.get_state()
        except Exception as exc:
            logger.exception("set_solo error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_phase(self, bus: int, enabled: bool) -> Dict[str, Any]:
        try:
            with self._lock:
                self.engine.channels[int(bus)].phase_invert = bool(enabled)
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_phase error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_mode(self, bus: int, mode: str) -> Dict[str, Any]:
        try:
            if mode not in FILTER_MODES:
                return {"ok": False, "error": f"Mode tidak dikenal: {mode}"}
            with self._lock:
                for idx in self._linked_indices(bus):
                    self.engine.channels[idx].filter_mode = mode
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_mode error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_source_mode(self, bus: int, source_mode: str) -> Dict[str, Any]:
        try:
            if source_mode not in SOURCE_MODES:
                return {"ok": False, "error": f"Source mode tidak dikenal: {source_mode}"}
            with self._lock:
                for idx in self._linked_indices(bus):
                    self.engine.channels[idx].source_mode = source_mode
                self.engine.sync_streams()
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_source_mode error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_input(self, bus: int, device_label: str) -> Dict[str, Any]:
        """Set capture soundcard device by label (or 'None (Muted)')."""
        try:
            with self._lock:
                dev_id = self._cap_by_label.get(device_label)
                for idx in self._linked_indices(bus):
                    self.engine.channels[idx].capture_device_id = dev_id
                self.engine.sync_streams()
            self.last_error = ""
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_input error: %s", exc)
            self.last_error = f"Gagal membuka capture device: {exc}"
            return {"ok": False, "error": self.last_error}

    def set_tap(self, bus: int, tap_mode: str) -> Dict[str, Any]:
        try:
            if tap_mode not in TAP_MODES:
                return {"ok": False, "error": f"Tap mode tidak dikenal: {tap_mode}"}
            with self._lock:
                self.engine.channels[int(bus)].tap_mode = tap_mode
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_tap error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_tone_type(self, bus: int, tone_type: str) -> Dict[str, Any]:
        try:
            if tone_type not in TONE_TYPES:
                return {"ok": False, "error": f"Tone type tidak dikenal: {tone_type}"}
            with self._lock:
                for idx in self._linked_indices(bus):
                    self.engine.channels[idx].tone_type = tone_type
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_tone_type error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def load_audio_file(self, bus: int, path: Optional[str] = None) -> Dict[str, Any]:
        """Open file dialog (if path omitted) and load WAV/FLAC/OGG into channel."""
        try:
            chosen = path
            if not chosen and self._window is not None:
                import webview

                result = self._window.create_file_dialog(
                    webview.FileDialog.OPEN,
                    file_types=("Audio Files (*.wav;*.flac;*.ogg)", "All files (*.*)"),
                )
                if result and len(result) > 0:
                    chosen = result[0]
            if not chosen:
                return {"ok": True, "cancelled": True, "state": self.get_state()}

            msg = self.engine.load_channel_file(int(bus), chosen)
            self._schedule_save()
            st = self.get_state()
            st["message"] = msg
            return st
        except Exception as exc:
            logger.exception("load_audio_file error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_output(self, bus: int, device_label: str) -> Dict[str, Any]:
        """Set per-bus Send To physical output device by label."""
        try:
            with self._lock:
                dev_id = self._out_by_label.get(device_label)
                for idx in self._linked_indices(bus):
                    self.engine.channels[idx].target_output_id = dev_id
                self.engine.sync_streams()
            self.last_error = ""
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_output error: %s", exc)
            self.last_error = f"Gagal mengatur output device: {exc}"
            return {"ok": False, "error": self.last_error}

    def set_primary_output(self, device_label: str) -> Dict[str, Any]:
        try:
            with self._lock:
                if device_label not in self._out_by_label:
                    return {"ok": False, "error": f"Device tidak ditemukan: {device_label}"}
                self.primary_output_label = device_label
                out_id = self._out_by_label[device_label]
                if self.engine.running:
                    self.engine.start(output_device_id=out_id)
            self.last_error = ""
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_primary_output error: %s", exc)
            self.last_error = f"Gagal pindah Primary Output: {exc}"
            return {"ok": False, "error": self.last_error}

    def set_preset(self, name: str) -> Dict[str, Any]:
        try:
            with self._lock:
                if name in BUILTIN_PRESETS:
                    self.engine.apply_preset(name)
                    self.active_preset = name
                elif name in self.custom_presets:
                    pdata = self.custom_presets[name]
                    self._apply_channel_dicts(pdata.get("channels", []))
                    self.engine.master_gain_db = float(pdata.get("master_gain_db", self.engine.master_gain_db))
                    self.engine.limiter_enabled = bool(pdata.get("limiter_enabled", self.engine.limiter_enabled))
                    self.engine.sync_streams()
                    self.active_preset = name
                else:
                    return {"ok": False, "error": f"Preset tidak ditemukan: {name}"}
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_preset error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def save_custom_preset(self, name: str) -> Dict[str, Any]:
        try:
            clean = name.strip()
            if not clean:
                return {"ok": False, "error": "Nama preset tidak boleh kosong."}
            if clean in BUILTIN_PRESETS:
                return {"ok": False, "error": "Tidak bisa menimpa preset bawaan."}
            with self._lock:
                self.custom_presets[clean] = {
                    "channels": self._serialize_channels(),
                    "master_gain_db": round(float(self.engine.master_gain_db), 2),
                    "limiter_enabled": bool(self.engine.limiter_enabled),
                }
                self.active_preset = clean
            self.save_settings()
            return self.get_state()
        except Exception as exc:
            logger.exception("save_custom_preset error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def delete_custom_preset(self, name: str) -> Dict[str, Any]:
        try:
            with self._lock:
                if name not in self.custom_presets:
                    return {"ok": False, "error": "Hanya preset custom yang bisa dihapus."}
                del self.custom_presets[name]
                if self.active_preset == name:
                    self.active_preset = BUILTIN_PRESETS[0]
            self.save_settings()
            return self.get_state()
        except Exception as exc:
            logger.exception("delete_custom_preset error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_master_gain(self, db: float) -> Dict[str, Any]:
        try:
            with self._lock:
                self.engine.master_gain_db = max(-60.0, min(12.0, float(db)))
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_master_gain error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_mono(self, enabled: bool) -> Dict[str, Any]:
        try:
            with self._lock:
                self.engine.master_mono = bool(enabled)
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_mono error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_dim(self, enabled: bool) -> Dict[str, Any]:
        try:
            with self._lock:
                self.engine.master_dim = bool(enabled)
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_dim error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_master_mute(self, enabled: bool) -> Dict[str, Any]:
        try:
            with self._lock:
                self.engine.master_mute = bool(enabled)
            return self.get_state()
        except Exception as exc:
            logger.exception("set_master_mute error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_soft_limiter(self, enabled: bool) -> Dict[str, Any]:
        try:
            with self._lock:
                self.engine.limiter_enabled = bool(enabled)
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_soft_limiter error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def set_link(self, pair: str, enabled: bool) -> Dict[str, Any]:
        try:
            with self._lock:
                if pair == "12":
                    self.link_12 = bool(enabled)
                elif pair == "34":
                    self.link_34 = bool(enabled)
                else:
                    return {"ok": False, "error": f"Unknown pair: {pair}"}
            self._schedule_save()
            return self.get_state()
        except Exception as exc:
            logger.exception("set_link error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def start_mixer(self) -> Dict[str, Any]:
        try:
            with self._lock:
                out_id = self._out_by_label.get(self.primary_output_label)
                if out_id is None:
                    self.last_error = "Device Primary Monitor Output tidak ditemukan. Klik Refresh."
                    return {"ok": False, "error": self.last_error}
                self.engine.start(output_device_id=out_id)
                self.last_error = ""
            return self.get_state()
        except Exception as exc:
            logger.exception("start_mixer error: %s", exc)
            self.last_error = f"Gagal memulai audio WASAPI: {exc}"
            return {"ok": False, "error": self.last_error}

    def stop_mixer(self) -> Dict[str, Any]:
        try:
            with self._lock:
                self.engine.stop()
            return self.get_state()
        except Exception as exc:
            logger.exception("stop_mixer error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def start_rec(self, path: Optional[str] = None) -> Dict[str, Any]:
        try:
            chosen = path
            if not chosen and self._window is not None:
                import webview

                result = self._window.create_file_dialog(
                    webview.FileDialog.SAVE,
                    save_filename="main_stereo_mix.wav",
                    file_types=("WAV Audio (*.wav)",),
                )
                if isinstance(result, (list, tuple)) and result:
                    chosen = result[0]
                elif isinstance(result, str) and result:
                    chosen = result
            if not chosen:
                return {"ok": True, "cancelled": True, "state": self.get_state()}
            if not chosen.lower().endswith(".wav"):
                chosen += ".wav"

            self.engine.start_recording(chosen)
            self.rec_path = chosen
            return self.get_state()
        except Exception as exc:
            logger.exception("start_rec error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def stop_rec(self) -> Dict[str, Any]:
        try:
            self.engine.stop_recording()
            self.rec_path = ""
            return self.get_state()
        except Exception as exc:
            logger.exception("stop_rec error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def reset_lufs(self) -> Dict[str, Any]:
        try:
            self.engine.reset_lufs()
            return self.get_state()
        except Exception as exc:
            logger.exception("reset_lufs error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def refresh_devices(self) -> Dict[str, Any]:
        try:
            was_running = self.engine.running
            if was_running:
                self.engine.stop()
            self._init_devices()
            self.last_error = ""
            if was_running:
                out_id = self._out_by_label.get(self.primary_output_label)
                if out_id is not None:
                    self.engine.start(output_device_id=out_id)
            return {"ok": True, "devices": self.get_devices(), "state": self.get_state()}
        except Exception as exc:
            logger.exception("refresh_devices error: %s", exc)
            self.last_error = f"Refresh device gagal: {exc}"
            return {"ok": False, "error": self.last_error}

    def set_window_size(self, width: int, height: int) -> Dict[str, Any]:
        try:
            self.window_size = (max(1100, int(width)), max(700, int(height)))
            self._schedule_save()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_autostart(self, enabled: bool) -> Dict[str, Any]:
        try:
            set_autostart_registry(bool(enabled))
            return self.get_state()
        except Exception as exc:
            logger.exception("set_autostart error: %s", exc)
            return {"ok": False, "error": f"Gagal mengubah Auto-Start Windows: {exc}"}

    def hide_to_tray(self) -> Dict[str, Any]:
        try:
            if self._on_hide_cb is not None:
                self._on_hide_cb()
            elif self._window is not None:
                self._window.hide()
            return {"ok": True}
        except Exception as exc:
            logger.exception("hide_to_tray error: %s", exc)
            return {"ok": False, "error": str(exc)}

    def full_close(self) -> Dict[str, Any]:
        try:
            if self._on_exit_cb is not None:
                threading.Thread(target=self._on_exit_cb, daemon=True).start()
            elif self._window is not None:
                self._window.destroy()
            return {"ok": True}
        except Exception as exc:
            logger.exception("full_close error: %s", exc)
            return {"ok": False, "error": str(exc)}
