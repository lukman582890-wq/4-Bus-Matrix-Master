"""
Real-time 4-Bus Digital Matrix Mixer Engine (WASAPI Loopback + Multi-Output Routing).
When the user selects "4-Bus Matrix Master (4-Bus Matrix Audio)" in Windows Sound Output
(Win + Ctrl + V), Windows audio (Spotify, Browser, DAW) enters the 4 mixer buses:
  - OUT 1 & OUT 2 -> e.g. Stereo Out / Top -> routed to Main Out 1/2 (Studio 26c)
  - OUT 3 & OUT 4 -> e.g. Sub Only (LPF)   -> routed to Line Out 3/4 (Studio 26c) (or Main Out 1/2)
All 4 outputs can be controlled independently (Mode/Crossover, Gain, Pan, Phase, Mute, Solo)
and routed to any physical soundcard output pair simultaneously.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf
from scipy import signal


FILTER_MODES = ("Full Range", "Stereo Top (HPF)", "Sub Only (LPF)", "Bandpass")
SOURCE_MODES = ("Soundcard", "Audio File", "Test Tone")
TAP_MODES = ("L (Ch 1)", "R (Ch 2)", "L+R (Stereo)", "L+R (Mono)")
TONE_TYPES = ("Sub 60Hz", "Sine 440Hz", "Pink Noise")


def db_to_linear(db: float) -> float:
    """Convert decibels to linear amplitude; <= -59.5 dB is treated as silence."""
    if db <= -59.5:
        return 0.0
    return float(10.0 ** (db / 20.0))


def linear_to_db(amp: float, floor_db: float = -60.0) -> float:
    """Convert linear amplitude to decibels with a floor."""
    if amp <= 1e-7:
        return floor_db
    return max(floor_db, float(20.0 * math.log10(amp)))


def constant_power_pan(pan: float) -> Tuple[float, float]:
    """Constant-power pan law (-3 dB center) for pan in [-1.0, 1.0]."""
    p = max(-1.0, min(1.0, float(pan)))
    angle = (p + 1.0) * (math.pi / 4.0)
    return math.cos(angle), math.sin(angle)


class LufsMeter:
    """Real-time ITU-R BS.1770-4 K-weighted Short-Term (3s) & Integrated LUFS meter."""

    def __init__(self, sample_rate: int = 48000) -> None:
        self.sample_rate = sample_rate
        self._sos: Optional[np.ndarray] = None
        self._zi_l: Optional[np.ndarray] = None
        self._zi_r: Optional[np.ndarray] = None
        self._st_window: Deque[Tuple[float, float]] = deque()
        self._st_dur_sum: float = 0.0
        self._st_e_sum: float = 0.0

        # 400 ms gating blocks for Integrated LUFS
        self._gate_accum_e: float = 0.0
        self._gate_accum_dur: float = 0.0
        self._gate_blocks: List[float] = []

        self.short_term_lufs: float = -70.0
        self.integrated_lufs: float = -70.0
        self._build_k_filter(sample_rate)

    def _build_k_filter(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        # Stage 1: ITU-R BS.1770-4 high-shelf pre-filter (+4 dB)
        # Stage 2: RLB high-pass (~38 Hz)
        if sample_rate == 48000:
            s1 = [1.53512485958697, -2.69169618940638, 1.19839281085285, 1.0, -1.69065929318241, 0.73248077421585]
            s2 = [1.0, -2.0, 1.0, 1.0, -1.99004745483398, 0.99007225036621]
        else:
            # Analog-prototype bilinear transform for arbitrary sample rates (e.g. 44.1 kHz, 96 kHz)
            f0 = 1681.974450955533
            G = 3.999843853973347
            Q = 0.7071752369554196
            K = math.tan(math.pi * f0 / sample_rate)
            Vh = 10.0 ** (G / 20.0)
            Vb = Vh ** 0.4996667741545416
            a0 = 1.0 + K / Q + K * K
            s1 = [
                (Vh + Vb * K / Q + K * K) / a0,
                2.0 * (K * K - Vh) / a0,
                (Vh - Vb * K / Q + K * K) / a0,
                1.0,
                2.0 * (K * K - 1.0) / a0,
                (1.0 - K / Q + K * K) / a0,
            ]
            f0_hp = 38.13547087602444
            Q_hp = 0.5003270373238773
            K_hp = math.tan(math.pi * f0_hp / sample_rate)
            a0_hp = 1.0 + K_hp / Q_hp + K_hp * K_hp
            s2 = [
                1.0 / a0_hp,
                -2.0 / a0_hp,
                1.0 / a0_hp,
                1.0,
                2.0 * (K_hp * K_hp - 1.0) / a0_hp,
                (1.0 - K_hp / Q_hp + K_hp * K_hp) / a0_hp,
            ]
        self._sos = np.array([s1, s2], dtype=np.float64)
        zi = signal.sosfilt_zi(self._sos) * 0.0
        self._zi_l = zi.copy()
        self._zi_r = zi.copy()

    def reset(self, sample_rate: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self.sample_rate:
            self._build_k_filter(sample_rate)
        elif self._sos is not None:
            zi = signal.sosfilt_zi(self._sos) * 0.0
            self._zi_l = zi.copy()
            self._zi_r = zi.copy()
        self._st_window.clear()
        self._st_dur_sum = 0.0
        self._st_e_sum = 0.0
        self._gate_accum_e = 0.0
        self._gate_accum_dur = 0.0
        self._gate_blocks.clear()
        self.short_term_lufs = -70.0
        self.integrated_lufs = -70.0

    @staticmethod
    def _energy_to_lufs(z: float) -> float:
        if z <= 1e-10:
            return -70.0
        return max(-70.0, float(-0.691 + 10.0 * math.log10(z)))

    def process(self, left: np.ndarray, right: np.ndarray, sample_rate: int) -> None:
        if sample_rate != self.sample_rate:
            self.reset(sample_rate)
        if len(left) == 0 or self._sos is None or self._zi_l is None or self._zi_r is None:
            return

        kl, self._zi_l = signal.sosfilt(self._sos, left.astype(np.float64, copy=False), zi=self._zi_l)
        kr, self._zi_r = signal.sosfilt(self._sos, right.astype(np.float64, copy=False), zi=self._zi_r)
        ms_sum = float(np.mean(kl * kl) + np.mean(kr * kr))
        dur = len(left) / float(sample_rate)

        # Short-term 3.0s sliding window
        self._st_window.append((dur, ms_sum))
        self._st_dur_sum += dur
        self._st_e_sum += ms_sum * dur
        while self._st_dur_sum > 3.0 and len(self._st_window) > 1:
            d0, e0 = self._st_window.popleft()
            self._st_dur_sum -= d0
            self._st_e_sum -= e0 * d0
        if self._st_dur_sum > 0.0:
            self.short_term_lufs = self._energy_to_lufs(self._st_e_sum / self._st_dur_sum)

        # Integrated 400 ms gated blocks (ITU-R BS.1770-4)
        self._gate_accum_e += ms_sum * dur
        self._gate_accum_dur += dur
        if self._gate_accum_dur >= 0.4:
            z_blk = self._gate_accum_e / self._gate_accum_dur
            self._gate_accum_e = 0.0
            self._gate_accum_dur = 0.0
            if self._energy_to_lufs(z_blk) > -70.0:
                self._gate_blocks.append(z_blk)
                if len(self._gate_blocks) > 9000:  # Cap at ~1 hour of 400ms blocks
                    self._gate_blocks.pop(0)
                ungated = sum(self._gate_blocks) / len(self._gate_blocks)
                rel_gate = self._energy_to_lufs(ungated) - 10.0
                gated = [b for b in self._gate_blocks if self._energy_to_lufs(b) > rel_gate]
                if gated:
                    self.integrated_lufs = self._energy_to_lufs(sum(gated) / len(gated))


class StereoRingBuffer:
    """Thread-safe stereo float32 ring buffer with fan-out read support."""

    def __init__(self, capacity_frames: int = 32768) -> None:
        self.capacity = capacity_frames
        self.buf = np.zeros((capacity_frames, 2), dtype=np.float32)
        self.write_pos = 0
        self.lock = threading.Lock()
        self.read_cursors: Dict[int, int] = {}

    def register_reader(self, reader_id: int) -> None:
        with self.lock:
            self.read_cursors[reader_id] = self.write_pos

    def write(self, frames_lr: np.ndarray) -> None:
        n = len(frames_lr)
        if n == 0:
            return
        if n >= self.capacity:
            frames_lr = frames_lr[-self.capacity :]
            n = self.capacity
        with self.lock:
            end = self.write_pos + n
            if end <= self.capacity:
                self.buf[self.write_pos : end] = frames_lr
            else:
                first = self.capacity - self.write_pos
                self.buf[self.write_pos :] = frames_lr[:first]
                self.buf[: n - first] = frames_lr[first:]
            self.write_pos = end % self.capacity

    def read(self, reader_id: int, frames: int) -> np.ndarray:
        with self.lock:
            rpos = self.read_cursors.get(reader_id, self.write_pos)
            avail = (self.write_pos - rpos) % self.capacity

            max_lag = max(3072, frames * 4)
            if avail > max_lag:
                rpos = (self.write_pos - frames * 2) % self.capacity
                avail = frames * 2

            if avail == 0:
                self.read_cursors[reader_id] = rpos
                return np.zeros((frames, 2), dtype=np.float32)

            to_read = min(frames, avail)
            end = rpos + to_read
            if end <= self.capacity:
                chunk = self.buf[rpos:end].copy()
            else:
                first = self.capacity - rpos
                chunk = np.vstack((self.buf[rpos:], self.buf[: to_read - first]))
            self.read_cursors[reader_id] = end % self.capacity

            if to_read < frames:
                pad = np.zeros((frames - to_read, 2), dtype=np.float32)
                return np.vstack((chunk, pad))
            return chunk


@dataclass
class ChannelState:
    """Configuration and DSP state for one of the 4 mixer output buses."""

    index: int
    name: str
    source_mode: str = "Soundcard"
    capture_device_id: Optional[int] = None
    target_output_id: Optional[int] = None  # Physical soundcard output for this bus (None = Main Out)
    tap_mode: str = "L (Ch 1)"
    tone_type: str = "Sine 440Hz"
    file_path: str = ""
    filter_mode: str = "Full Range"
    hpf_hz: float = 90.0
    lpf_hz: float = 90.0
    gain_db: float = 0.0
    pan: float = 0.0
    phase_invert: bool = False
    mute: bool = False
    solo: bool = False

    file_data: Optional[np.ndarray] = field(default=None, repr=False)
    file_pos: int = 0
    file_loop: bool = True

    tone_phase: float = 0.0
    pink_b: np.ndarray = field(default_factory=lambda: np.zeros(7, dtype=np.float64), repr=False)

    _sos: Optional[np.ndarray] = field(default=None, repr=False)
    _zi_l: Optional[np.ndarray] = field(default=None, repr=False)
    _zi_r: Optional[np.ndarray] = field(default=None, repr=False)
    _filter_sig: Tuple = field(default=(), repr=False)

    peak_linear: float = 0.0

    def ensure_filter(self, sample_rate: int) -> None:
        sig = (self.filter_mode, round(self.hpf_hz, 1), round(self.lpf_hz, 1), sample_rate)
        if sig == self._filter_sig:
            return
        self._filter_sig = sig

        nyq = sample_rate * 0.5
        if self.filter_mode == "Full Range":
            self._sos = None
            self._zi_l = None
            self._zi_r = None
            return

        if self.filter_mode == "Stereo Top (HPF)":
            fc = max(20.0, min(nyq * 0.9, self.hpf_hz))
            self._sos = signal.butter(4, fc / nyq, btype="highpass", output="sos")
        elif self.filter_mode == "Sub Only (LPF)":
            fc = max(25.0, min(nyq * 0.9, self.lpf_hz))
            self._sos = signal.butter(4, fc / nyq, btype="lowpass", output="sos")
        elif self.filter_mode == "Bandpass":
            low = max(20.0, min(nyq * 0.85, self.hpf_hz))
            high = max(low + 20.0, min(nyq * 0.95, self.lpf_hz))
            self._sos = signal.butter(3, [low / nyq, high / nyq], btype="bandpass", output="sos")
        else:
            self._sos = None

        if self._sos is not None:
            zi_zero = signal.sosfilt_zi(self._sos) * 0.0
            self._zi_l = zi_zero.copy()
            self._zi_r = zi_zero.copy()
        else:
            self._zi_l = None
            self._zi_r = None


class CaptureEndpoint:
    """Manages one PyAudioWPatch WASAPI capture stream (Loopback or Input) feeding a ring buffer."""

    def __init__(self, pa: pyaudio.PyAudio, device_id: int, target_sr: int, blocksize: int) -> None:
        self.device_id = device_id
        self.target_sr = target_sr
        info = pa.get_device_info_by_index(device_id)
        self.cap_sr = int(info["defaultSampleRate"])
        self.cap_ch = max(1, min(2, int(info["maxInputChannels"])))
        self.ring = StereoRingBuffer(capacity_frames=max(32768, target_sr))

        def _cb(in_data, frame_count, time_info, status):
            arr = np.frombuffer(in_data, dtype=np.float32)
            if self.cap_ch == 1:
                lr = np.column_stack((arr, arr))
            else:
                lr = arr.reshape(-1, self.cap_ch)[:, :2]
            if self.cap_sr != self.target_sr and len(lr) > 0:
                g = math.gcd(self.target_sr, self.cap_sr)
                up = self.target_sr // g
                down = self.cap_sr // g
                l_r = signal.resample_poly(lr[:, 0], up, down)
                r_r = signal.resample_poly(lr[:, 1], up, down)
                lr = np.column_stack((l_r, r_r)).astype(np.float32)
            self.ring.write(lr)
            return (None, pyaudio.paContinue)

        self.stream = pa.open(
            format=pyaudio.paFloat32,
            channels=self.cap_ch,
            rate=self.cap_sr,
            frames_per_buffer=blocksize,
            input=True,
            input_device_index=device_id,
            stream_callback=_cb,
        )
        self.stream.start_stream()

    def close(self) -> None:
        try:
            if self.stream.is_active():
                self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass


class SecondaryOutputEndpoint:
    """Manages an additional physical WASAPI output device (e.g., Line Out 3/4 on Studio 26c)."""

    def __init__(self, pa: pyaudio.PyAudio, device_id: int, engine_sr: int, blocksize: int) -> None:
        self.device_id = device_id
        self.engine_sr = engine_sr
        info = pa.get_device_info_by_index(device_id)
        self.out_sr = int(info["defaultSampleRate"])
        self.ring = StereoRingBuffer(capacity_frames=max(32768, self.out_sr))
        self.ring.register_reader(0)

        def _out_cb(in_data, frame_count, time_info, status):
            blk = self.ring.read(0, frame_count)
            return (blk.tobytes(), pyaudio.paContinue)

        self.stream = pa.open(
            format=pyaudio.paFloat32,
            channels=2,
            rate=self.out_sr,
            frames_per_buffer=blocksize,
            output=True,
            output_device_index=device_id,
            stream_callback=_out_cb,
        )
        self.stream.start_stream()

    def write(self, frames_lr: np.ndarray) -> None:
        if self.out_sr != self.engine_sr and len(frames_lr) > 0:
            g = math.gcd(self.out_sr, self.engine_sr)
            up = self.out_sr // g
            down = self.engine_sr // g
            l_r = signal.resample_poly(frames_lr[:, 0], up, down)
            r_r = signal.resample_poly(frames_lr[:, 1], up, down)
            frames_lr = np.column_stack((l_r, r_r)).astype(np.float32)
        self.ring.write(frames_lr)

    def close(self) -> None:
        try:
            if self.stream.is_active():
                self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass


class MatrixMixerEngine:
    """
    Real-time 4-bus digital matrix mixer.
    Takes audio from 4-Bus Matrix Master (or individual sources) and routes/sums
    across 4 configurable buses (OUT 1..4) to physical soundcard outputs (e.g., Main Out 1/2 + Line Out 3/4).
    """

    def __init__(self, sample_rate: int = 48000, blocksize: int = 512) -> None:
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self._lock = threading.RLock()

        self.channels: List[ChannelState] = [
            ChannelState(
                index=0,
                name="OUT 1 (Stereo L)",
                source_mode="Soundcard",
                tap_mode="L (Ch 1)",
                tone_type="Sine 440Hz",
                filter_mode="Full Range",
                hpf_hz=90.0,
                lpf_hz=120.0,
                gain_db=0.0,
                pan=-1.0,
            ),
            ChannelState(
                index=1,
                name="OUT 2 (Stereo R)",
                source_mode="Soundcard",
                tap_mode="R (Ch 2)",
                tone_type="Sine 440Hz",
                filter_mode="Full Range",
                hpf_hz=90.0,
                lpf_hz=120.0,
                gain_db=0.0,
                pan=1.0,
            ),
            ChannelState(
                index=2,
                name="OUT 3 (Sub 1)",
                source_mode="Soundcard",
                tap_mode="L+R (Mono)",
                tone_type="Sub 60Hz",
                filter_mode="Sub Only (LPF)",
                hpf_hz=30.0,
                lpf_hz=90.0,
                gain_db=0.0,
                pan=-1.0,
            ),
            ChannelState(
                index=3,
                name="OUT 4 (Sub 2)",
                source_mode="Soundcard",
                tap_mode="L+R (Mono)",
                tone_type="Sub 60Hz",
                filter_mode="Sub Only (LPF)",
                hpf_hz=30.0,
                lpf_hz=90.0,
                gain_db=0.0,
                pan=1.0,
            ),
        ]

        # Master controls
        self.master_gain_db: float = 0.0
        self.master_mute: bool = False
        self.master_mono: bool = False
        self.master_dim: bool = False
        self.limiter_enabled: bool = True
        self.also_sum_sub_to_main: bool = False  # Keep Out 1/2 & Out 3/4 isolated by default (Cubase Control Room style)
        self.master_peak_l: float = 0.0
        self.master_peak_r: float = 0.0
        self.master_clipped: bool = False
        self.lufs_meter = LufsMeter(sample_rate)

        # PyAudioWPatch runtime
        self._pa: Optional[pyaudio.PyAudio] = None
        self._captures: Dict[int, CaptureEndpoint] = {}
        self._sec_outputs: Dict[int, SecondaryOutputEndpoint] = {}
        self._out_stream = None
        self.output_device_id: Optional[int] = None
        self.running: bool = False

        # Master WAV recording
        self._rec_file: Optional[sf.SoundFile] = None
        self.recording: bool = False

    @staticmethod
    def list_devices() -> Tuple[List[Tuple[Optional[int], str]], List[Tuple[int, str]], Optional[int]]:
        """
        Return (capture_devices, output_devices, default_output_id) using Windows WASAPI.
        """
        pa = pyaudio.PyAudio()
        loopbacks: List[Tuple[Optional[int], str]] = []
        hw_inputs: List[Tuple[Optional[int], str]] = []
        outputs: List[Tuple[int, str]] = []
        default_out_id: Optional[int] = None

        try:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            wasapi_idx = wasapi["index"]
            default_out_id = wasapi.get("defaultOutputDevice")

            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if d["hostApi"] != wasapi_idx:
                    continue
                is_loop = bool(d.get("isLoopbackDevice", False))
                name = d["name"]
                if is_loop:
                    loopbacks.append((i, f"[OUT TAP] {name}"))
                elif d["maxInputChannels"] > 0:
                    hw_inputs.append((i, f"[IN] {name}"))
                if d["maxOutputChannels"] > 0 and not is_loop:
                    outputs.append((i, f"{name} ({int(d['defaultSampleRate'])} Hz)"))
        finally:
            pa.terminate()

        # Put 4-Bus Matrix Master [Loopback] at the very top of capture list
        loopbacks.sort(key=lambda x: (0 if "4-Bus Matrix Master" in x[1] else 1, x[1]))
        captures: List[Tuple[Optional[int], str]] = [(None, "None (Muted)")] + loopbacks + hw_inputs
        return captures, outputs, default_out_id

    def load_channel_file(self, ch_idx: int, path: str) -> str:
        data, file_sr = sf.read(path, dtype="float32", always_2d=True)
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        elif data.shape[1] > 2:
            data = data[:, :2]

        if file_sr != self.sample_rate:
            g = math.gcd(int(self.sample_rate), int(file_sr))
            up = int(self.sample_rate) // g
            down = int(file_sr) // g
            l_res = signal.resample_poly(data[:, 0], up, down)
            r_res = signal.resample_poly(data[:, 1], up, down)
            data = np.column_stack((l_res, r_res)).astype(np.float32)

        with self._lock:
            ch = self.channels[ch_idx]
            ch.file_path = path
            ch.file_data = np.ascontiguousarray(data, dtype=np.float32)
            ch.file_pos = 0
            ch.source_mode = "Audio File"
        return f"Loaded {path} ({len(data) / self.sample_rate:.1f}s)"

    def apply_preset(self, preset_name: str) -> None:
        with self._lock:
            if preset_name in (
                "Speaker Mixing (1/2 Full) + Sub (3/4 LPF)",
                "1&2 Stereo Out + 3&4 Sub Only (Full Top)",
            ):
                specs = [
                    ("L (Ch 1)", "Full Range", 90.0, 120.0, -1.0, 0.0),
                    ("R (Ch 2)", "Full Range", 90.0, 120.0, 1.0, 0.0),
                    ("L+R (Mono)", "Sub Only (LPF)", 30.0, 90.0, -1.0, 0.0),
                    ("L+R (Mono)", "Sub Only (LPF)", 30.0, 90.0, 1.0, 0.0),
                ]
            elif preset_name in (
                "Crossover Split: Tops (1/2 HPF) + Sub (3/4 LPF)",
                "1&2 Stereo Top + 3&4 Sub Only",
            ):
                specs = [
                    ("L (Ch 1)", "Stereo Top (HPF)", 90.0, 120.0, -1.0, 0.0),
                    ("R (Ch 2)", "Stereo Top (HPF)", 90.0, 120.0, 1.0, 0.0),
                    ("L+R (Mono)", "Sub Only (LPF)", 30.0, 90.0, -1.0, 0.0),
                    ("L+R (Mono)", "Sub Only (LPF)", 30.0, 90.0, 1.0, 0.0),
                ]
            elif preset_name in (
                "Dual Monitor: Mixing (1/2 Full) + Monitor 2 (3/4 Full)",
                "4-Ch Flat Stereo Sum",
            ):
                specs = [
                    ("L (Ch 1)", "Full Range", 90.0, 120.0, -1.0, 0.0),
                    ("R (Ch 2)", "Full Range", 90.0, 120.0, 1.0, 0.0),
                    ("L (Ch 1)", "Full Range", 90.0, 120.0, -1.0, 0.0),
                    ("R (Ch 2)", "Full Range", 90.0, 120.0, 1.0, 0.0),
                ]
            elif preset_name == "L/R Split + Dual Sub (80Hz/60Hz)":
                specs = [
                    ("L (Ch 1)", "Stereo Top (HPF)", 80.0, 120.0, -1.0, 0.0),
                    ("R (Ch 2)", "Stereo Top (HPF)", 80.0, 120.0, 1.0, 0.0),
                    ("L+R (Mono)", "Sub Only (LPF)", 30.0, 80.0, -1.0, 0.0),
                    ("L+R (Mono)", "Sub Only (LPF)", 25.0, 60.0, 1.0, 0.0),
                ]
            else:
                return

            for ch, (tap, fmode, hpf, lpf, pan, gain) in zip(self.channels, specs):
                ch.tap_mode = tap
                ch.filter_mode = fmode
                ch.hpf_hz = hpf
                ch.lpf_hz = lpf
                ch.pan = pan
                ch.gain_db = gain
                ch.mute = False
                ch.solo = False

    def sync_streams(self) -> None:
        """Open/close WASAPI capture and secondary output streams dynamically."""
        if not self.running or self._pa is None:
            return
        with self._lock:
            # 1. Sync Capture streams
            needed_caps: Dict[int, List[int]] = {}
            for ch in self.channels:
                if ch.source_mode == "Soundcard" and ch.capture_device_id is not None:
                    needed_caps.setdefault(ch.capture_device_id, []).append(ch.index)

            for dev_id in list(self._captures.keys()):
                if dev_id not in needed_caps:
                    self._captures[dev_id].close()
                    del self._captures[dev_id]

            for dev_id, ch_indices in needed_caps.items():
                if dev_id not in self._captures:
                    self._captures[dev_id] = CaptureEndpoint(
                        self._pa, dev_id, self.sample_rate, self.blocksize
                    )
                cap = self._captures[dev_id]
                for idx in ch_indices:
                    cap.ring.register_reader(idx)

            # 2. Sync Secondary Output streams (for channels targeting another output like Line Out 3/4)
            needed_outs = {
                ch.target_output_id
                for ch in self.channels
                if ch.target_output_id is not None and ch.target_output_id != self.output_device_id
            }

            for dev_id in list(self._sec_outputs.keys()):
                if dev_id not in needed_outs:
                    self._sec_outputs[dev_id].close()
                    del self._sec_outputs[dev_id]

            for dev_id in needed_outs:
                if dev_id not in self._sec_outputs:
                    try:
                        self._sec_outputs[dev_id] = SecondaryOutputEndpoint(
                            self._pa, dev_id, self.sample_rate, self.blocksize
                        )
                    except Exception:
                        pass

    # Keep alias for backwards compatibility
    def sync_captures(self) -> None:
        self.sync_streams()

    def start(self, output_device_id: int) -> None:
        """Start the Primary Hardware Output stream, Secondary Output streams, and Capture streams."""
        self.stop()
        self._pa = pyaudio.PyAudio()
        out_info = self._pa.get_device_info_by_index(output_device_id)
        self.sample_rate = int(out_info["defaultSampleRate"])
        self.output_device_id = output_device_id
        self.running = True

        self.sync_streams()

        def _out_cb(in_data, frame_count, time_info, status):
            blk = self.process_block(frame_count)
            return (blk.tobytes(), pyaudio.paContinue)

        self._out_stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=2,
            rate=self.sample_rate,
            frames_per_buffer=self.blocksize,
            output=True,
            output_device_index=output_device_id,
            stream_callback=_out_cb,
        )
        self._out_stream.start_stream()

    def stop(self) -> None:
        self.running = False
        if self._out_stream is not None:
            try:
                if self._out_stream.is_active():
                    self._out_stream.stop_stream()
                self._out_stream.close()
            except Exception:
                pass
            self._out_stream = None

        for sec in self._sec_outputs.values():
            sec.close()
        self._sec_outputs.clear()

        for cap in self._captures.values():
            cap.close()
        self._captures.clear()

        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

        for ch in self.channels:
            ch.peak_linear = 0.0
        self.master_peak_l = 0.0
        self.master_peak_r = 0.0

    def reset_lufs(self) -> None:
        with self._lock:
            self.lufs_meter.reset(self.sample_rate)

    def start_recording(self, wav_path: str) -> None:
        with self._lock:
            if self._rec_file is not None:
                self._rec_file.close()
            self._rec_file = sf.SoundFile(
                wav_path, mode="w", samplerate=self.sample_rate, channels=2, subtype="PCM_24"
            )
            self.recording = True

    def stop_recording(self) -> None:
        with self._lock:
            self.recording = False
            if self._rec_file is not None:
                self._rec_file.close()
                self._rec_file = None

    def _generate_source(self, ch: ChannelState, frames: int) -> Tuple[np.ndarray, np.ndarray]:
        if ch.source_mode == "Soundcard":
            cap = self._captures.get(ch.capture_device_id) if ch.capture_device_id is not None else None
            if cap is None:
                z = np.zeros(frames, dtype=np.float32)
                return z, z.copy()
            lr = cap.ring.read(ch.index, frames)
            if ch.tap_mode == "L (Ch 1)":
                m = lr[:, 0].copy()
                return m, m.copy()
            if ch.tap_mode == "R (Ch 2)":
                m = lr[:, 1].copy()
                return m, m.copy()
            if ch.tap_mode == "L+R (Mono)":
                m = (0.5 * (lr[:, 0] + lr[:, 1])).astype(np.float32)
                return m, m.copy()
            return lr[:, 0].copy(), lr[:, 1].copy()

        if ch.source_mode == "Audio File" and ch.file_data is not None and len(ch.file_data) > 0:
            data = ch.file_data
            total = len(data)
            pos = ch.file_pos
            if pos + frames <= total:
                chunk = data[pos : pos + frames]
                ch.file_pos = pos + frames
            else:
                part1 = data[pos:]
                rem = frames - len(part1)
                if ch.file_loop:
                    part2 = data[:rem] if rem <= total else np.resize(data, (rem, 2))
                    chunk = np.vstack((part1, part2))
                    ch.file_pos = rem % total
                else:
                    chunk = np.vstack((part1, np.zeros((rem, 2), dtype=np.float32)))
                    ch.file_pos = total
            return chunk[:, 0].copy(), chunk[:, 1].copy()

        if ch.source_mode == "Test Tone":
            if ch.tone_type == "Pink Noise":
                white = np.random.uniform(-1.0, 1.0, frames).astype(np.float64)
                b = ch.pink_b
                out = np.empty(frames, dtype=np.float32)
                for i in range(frames):
                    w = white[i]
                    b[0] = 0.99886 * b[0] + w * 0.0555179
                    b[1] = 0.99332 * b[1] + w * 0.0750759
                    b[2] = 0.96900 * b[2] + w * 0.1538520
                    b[3] = 0.86650 * b[3] + w * 0.3104856
                    b[4] = 0.55000 * b[4] + w * 0.5329522
                    b[5] = -0.7616 * b[5] - w * 0.0168980
                    out[i] = float((b[0] + b[1] + b[2] + b[3] + b[4] + b[5] + b[6] + w * 0.5362) * 0.08)
                    b[6] = w * 0.115926
                return out, out.copy()

            freq = 60.0 if ch.tone_type == "Sub 60Hz" else 440.0
            phase_inc = 2.0 * math.pi * freq / self.sample_rate
            phases = ch.tone_phase + phase_inc * np.arange(frames, dtype=np.float64)
            ch.tone_phase = float((phases[-1] + phase_inc) % (2.0 * math.pi))
            wave = (0.35 * np.sin(phases)).astype(np.float32)
            return wave, wave.copy()

        z = np.zeros(frames, dtype=np.float32)
        return z, z.copy()

    def process_block(self, frames: int) -> np.ndarray:
        """
        Process all 4 buses.
        - Sums buses assigned to Primary Output (and optionally all 4 buses when also_sum_sub_to_main=True)
        - Sends buses assigned to Secondary Outputs (e.g., Line Out 3/4) to their hardware streams!
        """
        main_l = np.zeros(frames, dtype=np.float32)
        main_r = np.zeros(frames, dtype=np.float32)
        sec_buffers: Dict[int, Tuple[np.ndarray, np.ndarray]] = {
            dev_id: (np.zeros(frames, dtype=np.float32), np.zeros(frames, dtype=np.float32))
            for dev_id in self._sec_outputs
        }

        with self._lock:
            any_solo = any(ch.solo for ch in self.channels)
            eff_master_db = self.master_gain_db - (15.0 if self.master_dim else 0.0)
            m_gain = 0.0 if self.master_mute else db_to_linear(eff_master_db)

            for ch in self.channels:
                l_sig, r_sig = self._generate_source(ch, frames)

                ch.ensure_filter(self.sample_rate)
                if ch._sos is not None and ch._zi_l is not None and ch._zi_r is not None:
                    l_sig, ch._zi_l = signal.sosfilt(ch._sos, l_sig, zi=ch._zi_l)
                    r_sig, ch._zi_r = signal.sosfilt(ch._sos, r_sig, zi=ch._zi_r)
                    l_sig = l_sig.astype(np.float32, copy=False)
                    r_sig = r_sig.astype(np.float32, copy=False)

                if ch.filter_mode == "Sub Only (LPF)":
                    mono_sub = 0.5 * (l_sig + r_sig)
                    l_sig = mono_sub
                    r_sig = mono_sub.copy()

                active = (not ch.mute) and (not any_solo or ch.solo)
                gain = db_to_linear(ch.gain_db) if active else 0.0
                if ch.phase_invert:
                    gain = -gain

                pan_l, pan_r = constant_power_pan(ch.pan)
                out_l = l_sig * (gain * pan_l)
                out_r = r_sig * (gain * pan_r)

                blk_peak = float(max(np.max(np.abs(out_l)), np.max(np.abs(out_r))))
                ch.peak_linear = max(blk_peak, ch.peak_linear * 0.85)

                tgt = ch.target_output_id
                if tgt is not None and tgt != self.output_device_id and tgt in sec_buffers:
                    sl, sr = sec_buffers[tgt]
                    sl += out_l
                    sr += out_r
                    if self.also_sum_sub_to_main:
                        main_l += out_l
                        main_r += out_r
                else:
                    main_l += out_l
                    main_r += out_r

            # Apply Master Gain, Mono Check & Soft Limiter to Secondary Outputs (e.g., Line Out 3/4)
            for dev_id, (sl, sr) in sec_buffers.items():
                sl *= m_gain
                sr *= m_gain
                if self.master_mono:
                    m_sec = 0.5 * (sl + sr)
                    sl = m_sec
                    sr = m_sec.copy()
                if self.limiter_enabled:
                    sl = np.tanh(sl * 1.05) * 0.966
                    sr = np.tanh(sr * 1.05) * 0.966
                sec_lr = np.column_stack((sl, sr)).astype(np.float32, copy=False)
                self._sec_outputs[dev_id].write(sec_lr)

            # Apply Master Gain, Mono Check & Soft Limiter to Primary Output (Main Out 1/2)
            main_l *= m_gain
            main_r *= m_gain
            if self.master_mono:
                m_pri = 0.5 * (main_l + main_r)
                main_l = m_pri
                main_r = m_pri.copy()

            if self.limiter_enabled:
                main_l = np.tanh(main_l * 1.05) * 0.966
                main_r = np.tanh(main_r * 1.05) * 0.966

            peak_l = float(np.max(np.abs(main_l)))
            peak_r = float(np.max(np.abs(main_r)))
            self.master_peak_l = max(peak_l, self.master_peak_l * 0.85)
            self.master_peak_r = max(peak_r, self.master_peak_r * 0.85)
            self.master_clipped = (peak_l >= 0.999) or (peak_r >= 0.999)
            self.lufs_meter.process(main_l, main_r, self.sample_rate)

            stereo_out = np.column_stack((main_l, main_r)).astype(np.float32, copy=False)
            if self.recording and self._rec_file is not None:
                self._rec_file.write(stereo_out)
            return stereo_out
