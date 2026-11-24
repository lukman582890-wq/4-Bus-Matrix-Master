"""
4-Bus Digital Matrix Mixer GUI (CustomTkinter + PyAudioWPatch WASAPI Loopback).
Cubase Control Room-style speaker & subwoofer manager for Windows:
  - Select "4-Bus Matrix Master (4-Bus Matrix Audio)" in Windows Sound Output (Win + Ctrl + V).
  - OUT 1 & OUT 2 (Mixing L/R)  -> Main Out 1/2 (Studio 26c)
  - OUT 3 & OUT 4 (Sub / Mon 2) -> Line Out 3/4 (Studio 26c)
Features: Pair Linking (1/2 & 3/4), BS.1770-4 LUFS & Peak dBFS, MONO check, DIM (-15 dB),
Right-click 0 dB fader reset, WAV Recorder, and automatic persistent state (mixer_config.json).
"""

from __future__ import annotations

import json
import os
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from audio_engine import (
    FILTER_MODES,
    SOURCE_MODES,
    TAP_MODES,
    TONE_TYPES,
    MatrixMixerEngine,
    linear_to_db,
)
from manage_endpoints import apply_endpoint_names

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

BG_MAIN = "#121418"
BG_STRIP = "#1B1E24"
BG_MASTER = "#181C24"
BORDER_COLOR = "#2B303A"
ACCENT_BLUE = "#3B82F6"
ACCENT_ORANGE = "#F59E0B"
ACCENT_RED = "#EF4444"
ACCENT_GREEN = "#10B981"
TEXT_MUTED = "#9CA3AF"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mixer_config.json")

PRESETS = [
    "Speaker Mixing (1/2 Full) + Sub (3/4 LPF)",
    "Crossover Split: Tops (1/2 HPF) + Sub (3/4 LPF)",
    "Dual Monitor: Mixing (1/2 Full) + Monitor 2 (3/4 Full)",
    "L/R Split + Dual Sub (80Hz/60Hz)",
]


class LevelMeter(tk.Canvas):
    """Vertical segmented LED-style audio level meter (-60..0 dBFS, red at -4..0 dBFS)."""

    def __init__(self, master, width: int = 16, height: int = 150, **kwargs) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg="#0D0F12",
            highlightthickness=1,
            highlightbackground="#2B303A",
            **kwargs,
        )
        self._meter_w = width
        self._meter_h = height
        self._level = 0.0

    def set_level(self, linear_amp: float) -> None:
        db = linear_to_db(linear_amp, floor_db=-60.0)
        norm = max(0.0, min(1.0, (db + 60.0) / 60.0))
        if abs(norm - self._level) < 0.01 and norm == 0.0:
            return
        self._level = norm
        self.delete("all")

        segments = 30  # 2.0 dB per segment across -60..0 dBFS
        seg_h = (self._meter_h - 4) / segments
        lit = int(round(norm * segments))
        for i in range(segments):
            y1 = self._meter_h - 2 - (i + 1) * seg_h + 1
            y2 = self._meter_h - 2 - i * seg_h - 1
            if i < lit:
                seg_db = -60.0 + ((i + 1) / segments) * 60.0
                if seg_db > -4.0:
                    color = ACCENT_RED      # -4.0 dB .. 0.0 dB
                elif seg_db > -14.0:
                    color = ACCENT_ORANGE   # -14.0 dB .. -4.0 dB
                else:
                    color = ACCENT_GREEN    # -60.0 dB .. -14.0 dB
            else:
                color = "#1F242D"
            self.create_rectangle(2, y1, self._meter_w - 2, y2, fill=color, outline="")


class ChannelStrip(ctk.CTkFrame):
    """Vertical digital mixer strip for one of the 4 output channels."""

    def __init__(
        self,
        master,
        engine: MatrixMixerEngine,
        ch_idx: int,
        status_cb: Callable[[str], None],
        link_cb: Callable[[int, str, object], None],
        change_cb: Callable[[], None],
    ) -> None:
        super().__init__(
            master,
            fg_color=BG_STRIP,
            corner_radius=10,
            border_width=1,
            border_color=BORDER_COLOR,
        )
        self.engine = engine
        self.ch_idx = ch_idx
        self.status_cb = status_cb
        self.link_cb = link_cb
        self.change_cb = change_cb
        self.ch = engine.channels[ch_idx]
        self.capture_map: Dict[str, Optional[int]] = {}
        self.capture_labels: List[str] = ["None (Muted)"]
        self.output_map: Dict[str, Optional[int]] = {}
        self.output_labels: List[str] = ["Main Out (Default)"]

        self._build_ui()
        self.sync_from_state()

    def _build_ui(self) -> None:
        strip_titles = [
            "OUT 1 • Mixing L",
            "OUT 2 • Mixing R",
            "OUT 3 • Sub / Mon 2 L",
            "OUT 4 • Sub / Mon 2 R",
        ]
        self.lbl_title = ctk.CTkLabel(
            self,
            text=strip_titles[self.ch_idx],
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#F3F4F6",
        )
        self.lbl_title.pack(pady=(8, 0), padx=10)

        self.lbl_role = ctk.CTkLabel(
            self,
            text=self.ch.filter_mode,
            font=ctk.CTkFont(size=11),
            text_color=ACCENT_BLUE,
        )
        self.lbl_role.pack(pady=(0, 4), padx=10)

        # Physical Output Target (Send To Soundcard Output)
        ctk.CTkLabel(
            self, text="Send To (Soundcard Out)", font=ctk.CTkFont(size=11), text_color=ACCENT_GREEN
        ).pack(anchor="w", padx=12)
        self.opt_target_out = ctk.CTkOptionMenu(
            self,
            values=self.output_labels,
            command=self._on_target_out,
            height=24,
            font=ctk.CTkFont(size=11),
            fg_color="#1E3A2F",
            button_color="#10B981",
        )
        self.opt_target_out.pack(fill="x", padx=10, pady=(1, 5))

        # Input Source Mode
        ctk.CTkLabel(self, text="Input Source", font=ctk.CTkFont(size=11), text_color=TEXT_MUTED).pack(
            anchor="w", padx=12
        )
        self.opt_source = ctk.CTkOptionMenu(
            self,
            values=list(SOURCE_MODES),
            command=self._on_source_mode,
            height=24,
            font=ctk.CTkFont(size=11),
        )
        self.opt_source.pack(fill="x", padx=10, pady=(1, 4))

        # Dynamic Source Sub-Frame
        self.sub_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.sub_frame.pack(fill="x", padx=10, pady=(0, 5))

        self.opt_cap_dev = ctk.CTkOptionMenu(
            self.sub_frame,
            values=self.capture_labels,
            command=self._on_cap_dev,
            height=24,
            font=ctk.CTkFont(size=11),
        )
        self.opt_tap = ctk.CTkOptionMenu(
            self.sub_frame,
            values=list(TAP_MODES),
            command=self._on_tap,
            height=23,
            font=ctk.CTkFont(size=11),
            fg_color="#252B36",
        )
        self.opt_tone = ctk.CTkOptionMenu(
            self.sub_frame,
            values=list(TONE_TYPES),
            command=self._on_tone_type,
            height=24,
            font=ctk.CTkFont(size=11),
        )
        self.btn_file = ctk.CTkButton(
            self.sub_frame,
            text="Load WAV/FLAC...",
            command=self._on_load_file,
            height=24,
            font=ctk.CTkFont(size=11),
            fg_color="#2A313D",
            hover_color="#374151",
        )

        # Crossover / Filter Mode
        ctk.CTkLabel(self, text="Crossover / Mode", font=ctk.CTkFont(size=11), text_color=TEXT_MUTED).pack(
            anchor="w", padx=12
        )
        self.opt_filter = ctk.CTkOptionMenu(
            self,
            values=list(FILTER_MODES),
            command=self._on_filter_mode,
            height=24,
            font=ctk.CTkFont(size=11),
        )
        self.opt_filter.pack(fill="x", padx=10, pady=(1, 5))

        # HPF Cutoff
        self.lbl_hpf = ctk.CTkLabel(
            self, text="HPF: 90 Hz", font=ctk.CTkFont(size=11), text_color=TEXT_MUTED
        )
        self.lbl_hpf.pack(anchor="w", padx=12)
        self.sld_hpf = ctk.CTkSlider(
            self, from_=20, to=500, number_of_steps=96, command=self._on_hpf, height=13
        )
        self.sld_hpf.pack(fill="x", padx=10, pady=(0, 3))

        # LPF Cutoff
        self.lbl_lpf = ctk.CTkLabel(
            self, text="LPF: 90 Hz", font=ctk.CTkFont(size=11), text_color=TEXT_MUTED
        )
        self.lbl_lpf.pack(anchor="w", padx=12)
        self.sld_lpf = ctk.CTkSlider(
            self, from_=40, to=250, number_of_steps=84, command=self._on_lpf, height=13
        )
        self.sld_lpf.pack(fill="x", padx=10, pady=(0, 4))

        # Pan
        self.lbl_pan = ctk.CTkLabel(
            self, text="Pan: C", font=ctk.CTkFont(size=11), text_color=TEXT_MUTED
        )
        self.lbl_pan.pack(anchor="w", padx=12)
        self.sld_pan = ctk.CTkSlider(
            self, from_=-1.0, to=1.0, number_of_steps=40, command=self._on_pan, height=13
        )
        self.sld_pan.bind("<Button-3>", lambda _e: self._reset_pan())
        self.sld_pan.pack(fill="x", padx=10, pady=(0, 4))

        # Fader + Meter area
        fader_row = ctk.CTkFrame(self, fg_color="transparent")
        fader_row.pack(fill="both", expand=True, padx=10, pady=(2, 4))

        self.sld_gain = ctk.CTkSlider(
            fader_row,
            from_=-60.0,
            to=12.0,
            orientation="vertical",
            height=145,
            command=self._on_gain,
        )
        self.sld_gain.bind("<Button-3>", lambda _e: self._reset_gain())
        self.sld_gain.pack(side="left", expand=True, padx=(14, 6))

        self.meter = LevelMeter(fader_row, width=16, height=145)
        self.meter.pack(side="right", padx=(6, 14))

        self.lbl_gain = ctk.CTkLabel(
            self, text="0.0 dB", font=ctk.CTkFont(size=12, weight="bold"), text_color="#E5E7EB"
        )
        self.lbl_gain.bind("<Button-3>", lambda _e: self._reset_gain())
        self.lbl_gain.pack(pady=(0, 4))

        # M / S / Phase buttons
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=(0, 8))
        btn_row.grid_columnconfigure((0, 1, 2), weight=1)

        self.btn_mute = ctk.CTkButton(
            btn_row, text="M", width=36, height=26, command=self._toggle_mute, font=ctk.CTkFont(size=12, weight="bold")
        )
        self.btn_mute.grid(row=0, column=0, padx=2)

        self.btn_solo = ctk.CTkButton(
            btn_row, text="S", width=36, height=26, command=self._toggle_solo, font=ctk.CTkFont(size=12, weight="bold")
        )
        self.btn_solo.grid(row=0, column=1, padx=2)

        self.btn_phase = ctk.CTkButton(
            btn_row, text="Ø", width=36, height=26, command=self._toggle_phase, font=ctk.CTkFont(size=12, weight="bold")
        )
        self.btn_phase.grid(row=0, column=2, padx=2)

    def set_devices(
        self,
        captures: List[tuple[Optional[int], str]],
        default_cap_id: Optional[int],
        outputs: List[tuple[int, str]],
        default_out_id: Optional[int],
    ) -> None:
        self.capture_map = {lbl: idx for idx, lbl in captures}
        self.capture_labels = [lbl for _, lbl in captures]
        self.opt_cap_dev.configure(values=self.capture_labels)
        if default_cap_id is not None:
            self.ch.capture_device_id = default_cap_id
        sel_cap = next(
            (lbl for lbl, idx in self.capture_map.items() if idx == self.ch.capture_device_id),
            self.capture_labels[0],
        )
        self.opt_cap_dev.set(sel_cap)

        self.output_map = {lbl: idx for idx, lbl in outputs}
        self.output_labels = [lbl for _, lbl in outputs]
        self.opt_target_out.configure(values=self.output_labels)
        if default_out_id is not None:
            self.ch.target_output_id = default_out_id
        sel_out = next(
            (lbl for lbl, idx in self.output_map.items() if idx == self.ch.target_output_id),
            self.output_labels[0] if self.output_labels else "",
        )
        if sel_out:
            self.opt_target_out.set(sel_out)

    def get_target_out_label(self) -> str:
        return self.opt_target_out.get()

    def get_capture_label(self) -> str:
        return self.opt_cap_dev.get()

    def _refresh_source_widget(self) -> None:
        self.opt_cap_dev.pack_forget()
        self.opt_tap.pack_forget()
        self.opt_tone.pack_forget()
        self.btn_file.pack_forget()

        if self.ch.source_mode == "Soundcard":
            selected = next(
                (lbl for lbl, idx in self.capture_map.items() if idx == self.ch.capture_device_id),
                self.capture_labels[0],
            )
            self.opt_cap_dev.set(selected)
            self.opt_cap_dev.pack(fill="x", pady=(0, 3))
            self.opt_tap.set(self.ch.tap_mode)
            self.opt_tap.pack(fill="x")
        elif self.ch.source_mode == "Test Tone":
            self.opt_tone.set(self.ch.tone_type)
            self.opt_tone.pack(fill="x")
        else:
            label = os.path.basename(self.ch.file_path) if self.ch.file_path else "Load WAV/FLAC..."
            self.btn_file.configure(text=label[:22])
            self.btn_file.pack(fill="x")

    def sync_from_state(self) -> None:
        self.opt_source.set(self.ch.source_mode)
        self._refresh_source_widget()
        if self.output_map:
            sel_out = next(
                (lbl for lbl, idx in self.output_map.items() if idx == self.ch.target_output_id),
                self.output_labels[0],
            )
            self.opt_target_out.set(sel_out)
        self.opt_filter.set(self.ch.filter_mode)
        self.lbl_role.configure(text=self.ch.filter_mode)
        self.sld_hpf.set(self.ch.hpf_hz)
        self.lbl_hpf.configure(text=f"HPF: {int(self.ch.hpf_hz)} Hz")
        self.sld_lpf.set(self.ch.lpf_hz)
        self.lbl_lpf.configure(text=f"LPF: {int(self.ch.lpf_hz)} Hz")
        self.sld_pan.set(self.ch.pan)
        self._update_pan_label(self.ch.pan)
        self.sld_gain.set(self.ch.gain_db)
        self._update_gain_label(self.ch.gain_db)
        self._refresh_buttons()

    def _update_pan_label(self, val: float) -> None:
        if abs(val) < 0.05:
            txt = "Pan: C"
        elif val < 0:
            txt = f"Pan: L{int(abs(val) * 100)}%"
        else:
            txt = f"Pan: R{int(val * 100)}%"
        self.lbl_pan.configure(text=txt)

    def _update_gain_label(self, db: float) -> None:
        self.lbl_gain.configure(text="-inf dB" if db <= -59.5 else f"{db:+.1f} dB")

    def _refresh_buttons(self) -> None:
        self.btn_mute.configure(
            fg_color=ACCENT_RED if self.ch.mute else "#2A313D",
            hover_color="#DC2626" if self.ch.mute else "#374151",
        )
        self.btn_solo.configure(
            fg_color=ACCENT_ORANGE if self.ch.solo else "#2A313D",
            hover_color="#D97706" if self.ch.solo else "#374151",
        )
        self.btn_phase.configure(
            fg_color=ACCENT_BLUE if self.ch.phase_invert else "#2A313D",
            hover_color="#2563EB" if self.ch.phase_invert else "#374151",
        )

    def _reset_gain(self) -> None:
        self._on_gain(0.0)
        self.sld_gain.set(0.0)

    def _reset_pan(self) -> None:
        default_pan = -1.0 if self.ch_idx in (0, 2) else 1.0
        self._on_pan(default_pan)
        self.sld_pan.set(default_pan)

    def _on_target_out(self, label: str) -> None:
        self.ch.target_output_id = self.output_map.get(label)
        self.link_cb(self.ch_idx, "target_output_id", self.ch.target_output_id)
        self.engine.sync_streams()
        self.change_cb()

    def _on_source_mode(self, val: str) -> None:
        self.ch.source_mode = val
        self._refresh_source_widget()
        self.link_cb(self.ch_idx, "source_mode", val)
        self.engine.sync_streams()
        self.change_cb()

    def _on_cap_dev(self, label: str) -> None:
        self.ch.capture_device_id = self.capture_map.get(label)
        self.link_cb(self.ch_idx, "capture_device_id", self.ch.capture_device_id)
        try:
            self.engine.sync_streams()
        except Exception as exc:
            messagebox.showerror("Capture Device Error", str(exc))
        self.change_cb()

    def _on_tap(self, val: str) -> None:
        self.ch.tap_mode = val
        self.change_cb()

    def _on_tone_type(self, val: str) -> None:
        self.ch.tone_type = val
        self.link_cb(self.ch_idx, "tone_type", val)
        self.change_cb()

    def _on_load_file(self) -> None:
        path = filedialog.askopenfilename(
            title=f"Select Audio File for OUT {self.ch_idx + 1}",
            filetypes=[("Audio Files", "*.wav *.flac *.ogg"), ("All Files", "*.*")],
        )
        if not path:
            return
        try:
            msg = self.engine.load_channel_file(self.ch_idx, path)
            self._refresh_source_widget()
            self.status_cb(msg)
            self.change_cb()
        except Exception as exc:
            messagebox.showerror("Load Error", str(exc))

    def _on_filter_mode(self, val: str) -> None:
        self.ch.filter_mode = val
        self.lbl_role.configure(text=val)
        self.link_cb(self.ch_idx, "filter_mode", val)
        self.change_cb()

    def _on_hpf(self, val: float) -> None:
        self.ch.hpf_hz = float(val)
        self.lbl_hpf.configure(text=f"HPF: {int(val)} Hz")
        self.link_cb(self.ch_idx, "hpf_hz", float(val))
        self.change_cb()

    def _on_lpf(self, val: float) -> None:
        self.ch.lpf_hz = float(val)
        self.lbl_lpf.configure(text=f"LPF: {int(val)} Hz")
        self.link_cb(self.ch_idx, "lpf_hz", float(val))
        self.change_cb()

    def _on_pan(self, val: float) -> None:
        self.ch.pan = float(val)
        self._update_pan_label(float(val))
        self.change_cb()

    def _on_gain(self, val: float) -> None:
        self.ch.gain_db = float(val)
        self._update_gain_label(float(val))
        self.link_cb(self.ch_idx, "gain_db", float(val))
        self.change_cb()

    def _toggle_mute(self) -> None:
        self.ch.mute = not self.ch.mute
        self._refresh_buttons()
        self.link_cb(self.ch_idx, "mute", self.ch.mute)
        self.change_cb()

    def _toggle_solo(self) -> None:
        self.ch.solo = not self.ch.solo
        self._refresh_buttons()
        self.link_cb(self.ch_idx, "solo", self.ch.solo)

    def _toggle_phase(self) -> None:
        self.ch.phase_invert = not self.ch.phase_invert
        self._refresh_buttons()
        self.change_cb()


class MixerApp(ctk.CTk):
    """Main window for the 4-Bus Digital Matrix Mixer."""

    def __init__(self, auto_start: bool = True, config_path: str = CONFIG_PATH) -> None:
        super().__init__()
        self.title("4-Bus Digital Matrix Mixer -> 4-Bus Matrix Master (WASAPI)")
        self.geometry("1280x790")
        self.minsize(1140, 720)
        self.configure(fg_color=BG_MAIN)

        self.config_path = config_path
        self.engine = MatrixMixerEngine(sample_rate=48000, blocksize=512)
        self.captures_list: List[tuple[Optional[int], str]] = []
        self.outputs_list: List[tuple[int, str]] = []
        self.output_map: Dict[str, int] = {}
        self.strips: List[ChannelStrip] = []
        self.link_12: bool = True
        self.link_34: bool = True
        self._syncing_link: bool = False
        self._loading_cfg: bool = False
        self._save_job: Optional[str] = None
        self._meter_job: Optional[str] = None

        self._build_topbar()
        self._build_mixer_rack()
        self._build_statusbar()
        self._populate_devices()
        self.load_config()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_meters()

        if auto_start:
            self.after(200, self._auto_start_if_ready)

    def _build_topbar(self) -> None:
        top = ctk.CTkFrame(self, fg_color=BG_STRIP, corner_radius=10, border_width=1, border_color=BORDER_COLOR)
        top.pack(fill="x", padx=12, pady=(12, 8))

        r1 = ctk.CTkFrame(top, fg_color="transparent")
        r1.pack(fill="x", padx=12, pady=(10, 4))

        ctk.CTkLabel(
            r1,
            text="4-BUS MATRIX MASTER  →  CONTROL ROOM (1/2 MIXING + 3/4 SUB/MON 2)",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#F9FAFB",
        ).pack(side="left")

        self.btn_engine = ctk.CTkButton(
            r1,
            text="▶ START MIXER",
            width=140,
            height=32,
            fg_color=ACCENT_GREEN,
            hover_color="#059669",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._toggle_engine,
        )
        self.btn_engine.pack(side="right", padx=(8, 0))

        self.btn_record = ctk.CTkButton(
            r1,
            text="● REC WAV",
            width=105,
            height=32,
            fg_color="#2A313D",
            hover_color="#374151",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._toggle_record,
        )
        self.btn_record.pack(side="right", padx=(8, 0))

        self.opt_preset = ctk.CTkOptionMenu(
            r1,
            values=PRESETS,
            width=265,
            height=30,
            command=self._on_preset,
            font=ctk.CTkFont(size=12),
        )
        self.opt_preset.set(PRESETS[0])
        self.opt_preset.pack(side="right", padx=(8, 0))

        ctk.CTkLabel(r1, text="Preset:", font=ctk.CTkFont(size=12), text_color=TEXT_MUTED).pack(
            side="right", padx=(0, 6)
        )

        # Row 2: Primary Monitor Output + Pair Link switches + Refresh
        r2 = ctk.CTkFrame(top, fg_color="transparent")
        r2.pack(fill="x", padx=12, pady=(4, 10))
        r2.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            r2,
            text="Primary Monitor Output (Main 1/2):",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=ACCENT_BLUE,
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.opt_output = ctk.CTkOptionMenu(
            r2, values=["Loading..."], height=28, font=ctk.CTkFont(size=12), command=self._on_primary_out_change
        )
        self.opt_output.grid(row=0, column=1, sticky="ew", padx=(0, 12))

        self.sw_link12 = ctk.CTkSwitch(
            r2, text="Link 1/2", font=ctk.CTkFont(size=11, weight="bold"), width=85, command=self._on_link_toggle
        )
        self.sw_link12.select()
        self.sw_link12.grid(row=0, column=2, padx=(0, 10))

        self.sw_link34 = ctk.CTkSwitch(
            r2, text="Link 3/4", font=ctk.CTkFont(size=11, weight="bold"), width=85, command=self._on_link_toggle
        )
        self.sw_link34.select()
        self.sw_link34.grid(row=0, column=3, padx=(0, 10))

        ctk.CTkButton(
            r2,
            text="↻ Refresh",
            width=95,
            height=28,
            fg_color="#2A313D",
            hover_color="#374151",
            command=self._populate_devices,
        ).grid(row=0, column=4)

    def _build_mixer_rack(self) -> None:
        rack = ctk.CTkFrame(self, fg_color="transparent")
        rack.pack(fill="both", expand=True, padx=12, pady=4)
        rack.grid_rowconfigure(0, weight=1)
        for i in range(4):
            rack.grid_columnconfigure(i, weight=1, uniform="ch")
        rack.grid_columnconfigure(4, weight=0, minsize=210)

        for i in range(4):
            strip = ChannelStrip(
                rack, self.engine, i, self._set_status, self._propagate_link, self.schedule_save
            )
            strip.grid(row=0, column=i, sticky="nsew", padx=(0, 8))
            self.strips.append(strip)

        # Master Control Room Strip
        master = ctk.CTkFrame(
            rack,
            fg_color=BG_MASTER,
            corner_radius=10,
            border_width=2,
            border_color=ACCENT_BLUE,
        )
        master.grid(row=0, column=4, sticky="nsew")

        ctk.CTkLabel(
            master,
            text="MAIN OUT",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#F9FAFB",
        ).pack(pady=(10, 0))
        ctk.CTkLabel(
            master,
            text="Control Room Master",
            font=ctk.CTkFont(size=11),
            text_color=ACCENT_BLUE,
        ).pack(pady=(0, 8))

        self.sw_limiter = ctk.CTkSwitch(
            master,
            text="Soft Limiter",
            font=ctk.CTkFont(size=11),
            command=self._on_limiter,
        )
        self.sw_limiter.select()
        self.sw_limiter.pack(pady=(2, 6), padx=14, anchor="w")

        self.lbl_clip = ctk.CTkLabel(
            master,
            text="SIGNAL OK",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=ACCENT_GREEN,
        )
        self.lbl_clip.pack(pady=(0, 4))

        # Real-time Peak dBFS + ITU-R BS.1770 LUFS Readout
        lufs_box = ctk.CTkFrame(master, fg_color="#11141A", corner_radius=6, border_width=1, border_color=BORDER_COLOR)
        lufs_box.pack(fill="x", padx=12, pady=(0, 6))

        self.lbl_peak_db = ctk.CTkLabel(
            lufs_box,
            text="Peak: -60.0 dBFS",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#E5E7EB",
        )
        self.lbl_peak_db.pack(pady=(4, 1))

        self.lbl_lufs_st = ctk.CTkLabel(
            lufs_box,
            text="ST: -70.0 LUFS",
            font=ctk.CTkFont(size=11),
            text_color=ACCENT_BLUE,
        )
        self.lbl_lufs_st.pack(pady=1)

        self.lbl_lufs_int = ctk.CTkLabel(
            lufs_box,
            text="INT: -70.0 LUFS",
            font=ctk.CTkFont(size=11),
            text_color=TEXT_MUTED,
        )
        self.lbl_lufs_int.pack(pady=(1, 4))

        ctk.CTkButton(
            lufs_box,
            text="Reset LUFS",
            height=20,
            font=ctk.CTkFont(size=10),
            fg_color="#252B36",
            hover_color="#374151",
            command=self._on_reset_lufs,
        ).pack(fill="x", padx=8, pady=(0, 5))

        m_row = ctk.CTkFrame(master, fg_color="transparent")
        m_row.pack(fill="both", expand=True, padx=14, pady=4)

        self.sld_master = ctk.CTkSlider(
            m_row,
            from_=-60.0,
            to=12.0,
            orientation="vertical",
            height=195,
            command=self._on_master_gain,
        )
        self.sld_master.set(0.0)
        self.sld_master.bind("<Button-3>", lambda _e: self._reset_master_gain())
        self.sld_master.pack(side="left", expand=True, padx=(10, 8))

        meters_box = ctk.CTkFrame(m_row, fg_color="transparent")
        meters_box.pack(side="right", padx=(4, 10))

        self.meter_l = LevelMeter(meters_box, width=16, height=195)
        self.meter_l.pack(side="left", padx=2)
        self.meter_r = LevelMeter(meters_box, width=16, height=195)
        self.meter_r.pack(side="left", padx=2)

        self.lbl_master_db = ctk.CTkLabel(
            master, text="0.0 dB", font=ctk.CTkFont(size=12, weight="bold"), text_color="#F9FAFB"
        )
        self.lbl_master_db.bind("<Button-3>", lambda _e: self._reset_master_gain())
        self.lbl_master_db.pack(pady=(2, 6))

        # Control Room MONO & DIM (-15 dB) buttons
        cr_row = ctk.CTkFrame(master, fg_color="transparent")
        cr_row.pack(fill="x", padx=12, pady=(0, 6))
        cr_row.grid_columnconfigure((0, 1), weight=1)

        self.btn_mono = ctk.CTkButton(
            cr_row,
            text="MONO",
            height=28,
            fg_color="#2A313D",
            hover_color="#374151",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._toggle_mono,
        )
        self.btn_mono.grid(row=0, column=0, sticky="ew", padx=(0, 3))

        self.btn_dim = ctk.CTkButton(
            cr_row,
            text="DIM -15dB",
            height=28,
            fg_color="#2A313D",
            hover_color="#374151",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._toggle_dim,
        )
        self.btn_dim.grid(row=0, column=1, sticky="ew", padx=(3, 0))

        self.btn_master_mute = ctk.CTkButton(
            master,
            text="MASTER MUTE",
            height=32,
            fg_color="#2A313D",
            hover_color="#374151",
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._toggle_master_mute,
        )
        self.btn_master_mute.pack(fill="x", padx=12, pady=(0, 10))

    def _build_statusbar(self) -> None:
        self.lbl_status = ctk.CTkLabel(
            self,
            text="Klik kanan fader untuk reset 0.0 dB | Pilih '4-Bus Matrix Master' di Sound Output Windows (Win + Ctrl + V).",
            font=ctk.CTkFont(size=11),
            text_color=TEXT_MUTED,
            anchor="w",
        )
        self.lbl_status.pack(fill="x", padx=16, pady=(2, 8))

    def _set_status(self, msg: str) -> None:
        self.lbl_status.configure(text=msg)

    def _propagate_link(self, src_idx: int, attr: str, value: object) -> None:
        """Mirror linked controls between OUT 1<->2 or OUT 3<->4 when Pair Link is enabled."""
        if self._syncing_link or self._loading_cfg:
            return
        if src_idx in (0, 1) and self.link_12:
            partner_idx = 1 - src_idx
        elif src_idx in (2, 3) and self.link_34:
            partner_idx = 5 - src_idx
        else:
            return

        self._syncing_link = True
        try:
            partner_strip = self.strips[partner_idx]
            setattr(partner_strip.ch, attr, value)
            partner_strip.sync_from_state()
        finally:
            self._syncing_link = False

    def _on_link_toggle(self) -> None:
        self.link_12 = bool(self.sw_link12.get())
        self.link_34 = bool(self.sw_link34.get())
        self.schedule_save()

    def _populate_devices(self) -> None:
        try:
            apply_endpoint_names(restore=False)
        except Exception:
            pass
        captures, outputs, default_out_id = MatrixMixerEngine.list_devices()
        self.captures_list = captures
        self.outputs_list = outputs
        self.output_map = {lbl: idx for idx, lbl in outputs}
        out_labels = [lbl for _, lbl in outputs]

        self.opt_output.configure(values=out_labels if out_labels else ["No WASAPI Output"])
        studio_main_lbl = next((lbl for _, lbl in outputs if "Main Out 1/2" in lbl), None)
        default_lbl = studio_main_lbl or next(
            (lbl for idx, lbl in outputs if idx == default_out_id),
            out_labels[0] if out_labels else "",
        )
        if default_lbl:
            self.opt_output.set(default_lbl)

        master_tap_id = next(
            (idx for idx, lbl in captures if idx is not None and "4-Bus Matrix Master" in lbl),
            None,
        )
        if master_tap_id is None:
            loop_ids = [idx for idx, lbl in captures if idx is not None and "[OUT TAP]" in lbl]
            master_tap_id = loop_ids[0] if loop_ids else None

        main12_out_id = next((idx for idx, lbl in outputs if "Main Out 1/2" in lbl), default_out_id)
        line34_out_id = next((idx for idx, lbl in outputs if "Line Out 3/4" in lbl), main12_out_id)

        target_outs = [main12_out_id, main12_out_id, line34_out_id, line34_out_id]
        for strip, tgt_out_id in zip(self.strips, target_outs):
            strip.set_devices(captures, master_tap_id, outputs, tgt_out_id)

    def schedule_save(self) -> None:
        if self._loading_cfg:
            return
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.after(400, self.save_config)

    def save_config(self) -> None:
        self._save_job = None
        data = {
            "primary_output": self.opt_output.get(),
            "preset": self.opt_preset.get(),
            "link_12": self.link_12,
            "link_34": self.link_34,
            "master_gain_db": round(self.engine.master_gain_db, 2),
            "limiter_enabled": self.engine.limiter_enabled,
            "master_mono": self.engine.master_mono,
            "master_dim": self.engine.master_dim,
            "channels": [
                {
                    "target_output": strip.get_target_out_label(),
                    "source_mode": strip.ch.source_mode,
                    "capture_device": strip.get_capture_label(),
                    "tap_mode": strip.ch.tap_mode,
                    "tone_type": strip.ch.tone_type,
                    "file_path": strip.ch.file_path,
                    "filter_mode": strip.ch.filter_mode,
                    "hpf_hz": round(strip.ch.hpf_hz, 1),
                    "lpf_hz": round(strip.ch.lpf_hz, 1),
                    "gain_db": round(strip.ch.gain_db, 2),
                    "pan": round(strip.ch.pan, 2),
                    "phase_invert": strip.ch.phase_invert,
                    "mute": strip.ch.mute,
                }
                for strip in self.strips
            ],
        }
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def load_config(self) -> None:
        if not os.path.exists(self.config_path):
            return
        self._loading_cfg = True
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            pri_lbl = data.get("primary_output", "")
            if pri_lbl in self.output_map:
                self.opt_output.set(pri_lbl)

            preset = data.get("preset", "")
            if preset in PRESETS:
                self.opt_preset.set(preset)

            self.link_12 = bool(data.get("link_12", True))
            self.link_34 = bool(data.get("link_34", True))
            if self.link_12:
                self.sw_link12.select()
            else:
                self.sw_link12.deselect()
            if self.link_34:
                self.sw_link34.select()
            else:
                self.sw_link34.deselect()

            self.engine.master_gain_db = float(data.get("master_gain_db", 0.0))
            self.sld_master.set(self.engine.master_gain_db)

            self.engine.limiter_enabled = bool(data.get("limiter_enabled", True))
            if self.engine.limiter_enabled:
                self.sw_limiter.select()
            else:
                self.sw_limiter.deselect()

            self.engine.master_mono = bool(data.get("master_mono", False))
            self.engine.master_dim = bool(data.get("master_dim", False))
            self._refresh_master_buttons()
            self._refresh_master_db_label()

            ch_list = data.get("channels", [])
            for strip, cdata in zip(self.strips, ch_list):
                tgt_lbl = cdata.get("target_output", "")
                if tgt_lbl in strip.output_map:
                    strip.ch.target_output_id = strip.output_map[tgt_lbl]

                cap_lbl = cdata.get("capture_device", "")
                if cap_lbl in strip.capture_map:
                    strip.ch.capture_device_id = strip.capture_map[cap_lbl]

                s_mode = cdata.get("source_mode", "Soundcard")
                if s_mode in SOURCE_MODES:
                    strip.ch.source_mode = s_mode

                f_path = cdata.get("file_path", "")
                if s_mode == "Audio File" and f_path and os.path.exists(f_path):
                    try:
                        self.engine.load_channel_file(strip.ch_idx, f_path)
                    except Exception:
                        strip.ch.source_mode = "Soundcard"

                tap = cdata.get("tap_mode", strip.ch.tap_mode)
                if tap in TAP_MODES:
                    strip.ch.tap_mode = tap

                tone = cdata.get("tone_type", strip.ch.tone_type)
                if tone in TONE_TYPES:
                    strip.ch.tone_type = tone

                fmode = cdata.get("filter_mode", strip.ch.filter_mode)
                if fmode in FILTER_MODES:
                    strip.ch.filter_mode = fmode

                strip.ch.hpf_hz = float(cdata.get("hpf_hz", strip.ch.hpf_hz))
                strip.ch.lpf_hz = float(cdata.get("lpf_hz", strip.ch.lpf_hz))
                strip.ch.gain_db = float(cdata.get("gain_db", strip.ch.gain_db))
                strip.ch.pan = float(cdata.get("pan", strip.ch.pan))
                strip.ch.phase_invert = bool(cdata.get("phase_invert", False))
                strip.ch.mute = bool(cdata.get("mute", False))
                strip.sync_from_state()
        except Exception:
            pass
        finally:
            self._loading_cfg = False

    def _auto_start_if_ready(self) -> None:
        if not self.engine.running and self.output_map:
            out_label = self.opt_output.get()
            out_id = self.output_map.get(out_label)
            if out_id is not None:
                try:
                    self.engine.start(output_device_id=out_id)
                    self.btn_engine.configure(text="■ STOP MIXER", fg_color=ACCENT_RED, hover_color="#DC2626")
                    self._set_status(
                        f"ACTIVE @ {self.engine.sample_rate} Hz | Klik kanan fader = 0 dB | Pilih '4-Bus Matrix Master' di Win+Ctrl+V"
                    )
                except Exception:
                    pass

    def _on_primary_out_change(self, out_label: str) -> None:
        self.schedule_save()
        if self.engine.running:
            out_id = self.output_map.get(out_label)
            if out_id is not None:
                try:
                    self.engine.start(output_device_id=out_id)
                    self._set_status(f"Switched Primary Output -> {out_label}")
                except Exception as exc:
                    messagebox.showerror("Audio Stream Error", str(exc))

    def _toggle_engine(self) -> None:
        if self.engine.running:
            self.engine.stop()
            self.btn_engine.configure(text="▶ START MIXER", fg_color=ACCENT_GREEN, hover_color="#059669")
            self._set_status("Mixer stopped.")
            return

        out_label = self.opt_output.get()
        out_id = self.output_map.get(out_label)
        if out_id is None:
            messagebox.showerror("Device Error", "Pilih Primary Monitor Output terlebih dahulu.")
            return

        try:
            self.engine.start(output_device_id=out_id)
            self.btn_engine.configure(text="■ STOP MIXER", fg_color=ACCENT_RED, hover_color="#DC2626")
            self._set_status(
                f"ACTIVE @ {self.engine.sample_rate} Hz | Klik kanan fader = 0 dB | Pilih '4-Bus Matrix Master' di Win+Ctrl+V"
            )
        except Exception as exc:
            messagebox.showerror("Audio Stream Error", f"Gagal memulai stream WASAPI:\n{exc}")

    def _toggle_record(self) -> None:
        if self.engine.recording:
            self.engine.stop_recording()
            self.btn_record.configure(text="● REC WAV", fg_color="#2A313D")
            self._set_status("Master WAV recording saved.")
            return

        path = filedialog.asksaveasfilename(
            title="Save Combined Main Stereo Out As",
            defaultextension=".wav",
            filetypes=[("WAV Audio", "*.wav")],
            initialfile="main_stereo_mix.wav",
        )
        if not path:
            return
        try:
            self.engine.start_recording(path)
            self.btn_record.configure(text="■ STOP REC", fg_color=ACCENT_RED)
            self._set_status(f"Recording Main Out -> {os.path.basename(path)}")
        except Exception as exc:
            messagebox.showerror("Record Error", str(exc))

    def _on_preset(self, name: str) -> None:
        self.engine.apply_preset(name)
        for strip in self.strips:
            strip.sync_from_state()
        self.schedule_save()
        self._set_status(f"Applied preset: {name}")

    def _on_limiter(self) -> None:
        self.engine.limiter_enabled = bool(self.sw_limiter.get())
        self.schedule_save()

    def _on_reset_lufs(self) -> None:
        self.engine.reset_lufs()
        self._set_status("LUFS meter reset.")

    def _reset_master_gain(self) -> None:
        self.sld_master.set(0.0)
        self._on_master_gain(0.0)

    def _refresh_master_db_label(self) -> None:
        db = self.engine.master_gain_db
        base = "-inf dB" if db <= -59.5 else f"{db:+.1f} dB"
        if self.engine.master_dim and db > -59.5:
            base += " (DIM)"
        self.lbl_master_db.configure(text=base)

    def _refresh_master_buttons(self) -> None:
        self.btn_mono.configure(
            fg_color=ACCENT_BLUE if self.engine.master_mono else "#2A313D",
            hover_color="#2563EB" if self.engine.master_mono else "#374151",
        )
        self.btn_dim.configure(
            fg_color=ACCENT_ORANGE if self.engine.master_dim else "#2A313D",
            hover_color="#D97706" if self.engine.master_dim else "#374151",
        )
        self.btn_master_mute.configure(
            fg_color=ACCENT_RED if self.engine.master_mute else "#2A313D",
            hover_color="#DC2626" if self.engine.master_mute else "#374151",
        )

    def _on_master_gain(self, val: float) -> None:
        self.engine.master_gain_db = float(val)
        self._refresh_master_db_label()
        self.schedule_save()

    def _toggle_mono(self) -> None:
        self.engine.master_mono = not self.engine.master_mono
        self._refresh_master_buttons()
        self.schedule_save()

    def _toggle_dim(self) -> None:
        self.engine.master_dim = not self.engine.master_dim
        self._refresh_master_buttons()
        self._refresh_master_db_label()
        self.schedule_save()

    def _toggle_master_mute(self) -> None:
        self.engine.master_mute = not self.engine.master_mute
        self._refresh_master_buttons()

    def _update_meters(self) -> None:
        for strip in self.strips:
            strip.meter.set_level(strip.ch.peak_linear)
        self.meter_l.set_level(self.engine.master_peak_l)
        self.meter_r.set_level(self.engine.master_peak_r)

        peak_amp = max(self.engine.master_peak_l, self.engine.master_peak_r)
        peak_db = linear_to_db(peak_amp, floor_db=-60.0)
        peak_color = ACCENT_RED if peak_db > -4.0 else (ACCENT_ORANGE if peak_db > -14.0 else "#E5E7EB")
        self.lbl_peak_db.configure(text=f"Peak: {peak_db:+.1f} dBFS", text_color=peak_color)

        st = self.engine.lufs_meter.short_term_lufs
        intl = self.engine.lufs_meter.integrated_lufs
        self.lbl_lufs_st.configure(text=f"ST: {st:+.1f} LUFS")
        self.lbl_lufs_int.configure(text=f"INT: {intl:+.1f} LUFS")

        if self.engine.master_clipped:
            self.lbl_clip.configure(text="CLIPPING!", text_color=ACCENT_RED)
        else:
            self.lbl_clip.configure(text="SIGNAL OK", text_color=ACCENT_GREEN)

        self._meter_job = self.after(33, self._update_meters)

    def _on_close(self) -> None:
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
            self._save_job = None
        self.save_config()
        if self._meter_job is not None:
            try:
                self.after_cancel(self._meter_job)
            except Exception:
                pass
            self._meter_job = None
        self.engine.stop_recording()
        self.engine.stop()
        self.destroy()


if __name__ == "__main__":
    app = MixerApp()
    app.mainloop()
