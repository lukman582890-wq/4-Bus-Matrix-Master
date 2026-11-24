<div align="center">

# 4-Bus Matrix Master

**Cubase Control Room-style 4-Bus Digital Matrix Mixer & Subwoofer/Monitor Manager for Windows (WASAPI Loopback)**

Built with **Python 3.14**, **pywebview (Edge WebView2)**, **PyAudioWPatch**, **SciPy**, and **pystray**.

</div>

---

## Overview

**4-Bus Matrix Master** captures system audio from a single Windows Sound Output endpoint (`4-Bus Matrix Master`) via **Windows WASAPI Loopback** and splits/routes it across **4 independently controlled output buses** into your multi-channel audio interface (e.g., PreSonus Studio 26c):

- **OUT 1 & OUT 2 (`Mixing L / R`)** → `Main Out 1/2` (Studio Monitor / Mixing Speakers)
- **OUT 3 & OUT 4 (`Sub / Mon 2 L / R`)** → `Line Out 3/4` (Subwoofer / Secondary Monitor Pair)

## Key Features

- **4 Independent Output Buses (`OUT 1`–`OUT 4`)**
  - **Routing Popover:** Per-bus **Send To** (any WASAPI output device), **Input Source** (`Soundcard` Loopback/Input + Channel Tap `L`/`R`/`L+R Stereo`/`L+R Mono`, `Audio File` WAV/FLAC/OGG, or `Test Tone` Sub 60Hz / Sine 440Hz / Pink Noise), and **Crossover Mode** (`Full Range`, `Stereo Top (HPF)`, `Sub Only (LPF)`, `Bandpass`).
  - **Logarithmic SVG Knobs:** `HPF` (`20–500 Hz`) & `LPF` (`40–250 Hz`) with 270° arc, vertical drag (`Shift` fine), scroll wheel, keyboard arrows, and double-click reset to preset default.
  - **Bipolar Pan:** Smooth horizontal bar with center tick; double-click / right-click resets to strip default (`OUT 1 & 3 = L100`, `OUT 2 & 4 = R100`).
  - **Pro Faders (`-60..+12 dB`):** Metallic fader cap, `Shift+Drag` 10× fine mode, `0.1 dB` scroll wheel, and **Double-Click / Right-Click / `Ctrl+Click`** smooth 150 ms animated reset to `0.0 dB`.
  - **24-Segment LED Meters (All 4 Buses + Master L/R):**
    - Green: `-60.0 .. -12.5 dB` (seg 1–17)
    - Orange: `-12.5 .. -4.5 dB` (seg 18–21)
    - Red: `-4.5 .. 0.0 dB` (seg 22–24)
    - 1.5s peak hold + 60 FPS smooth decay via `requestAnimationFrame` (pushed at 30 Hz from Python).
  - **Pair Linking (`Link 1/2` & `Link 3/4`):** Synchronizes Gain, HPF/LPF, Crossover Mode, Mute, Solo, Input/Output routing, and fader resets across stereo pairs.
- **Control Room `MAIN OUT` Panel**
  - **ITU-R BS.1770-4 Loudness Metering:** Large Integrated LUFS readout, Short-Term (3s) LUFS, True Peak dBFS, `Reset LUFS`, and a **60-second Loudness History Graph**.
  - **Soft Limiter (`-0.3 dBFS`):** Smooth `tanh` saturation safety limiter + `SIGNAL OK` / `CLIPPING!` indicator.
  - **`MONO`** (instant L+R phase-cancellation check), **`DIM -15 dB`**, and **`MASTER MUTE`**.
  - **Master WAV Recorder (`● REC WAV`):** Record the main stereo mix directly to 24-bit WAV.
- **Windows Integration**
  - **System Tray + Close Choice:** Clicking `X` prompts **Hide to Tray** (keeps audio running in background) or **Full Close**.
  - **Windows Auto-Start (`Auto-Start`):** Registers `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` with `--minimized` to launch straight into the System Tray on boot.
  - **Persistent State (`settings.json`) & Custom Presets:** Automatically saves faders, crossovers, devices, window size, and user-created presets.

---

## Signal Flow

```text
[ Spotify / Browser / DAW ]
            │  (Selected in Windows Sound Output: Win + Ctrl + V)
            ▼
[ 4-Bus Matrix Master (4-Bus Matrix Audio) ]
            │
            │  WASAPI Loopback (1 Stream -> StereoRingBuffer fan-out)
            ├───────────────┬───────────────┬───────────────┐
            ▼               ▼               ▼               ▼
        [ OUT 1 ]       [ OUT 2 ]       [ OUT 3 ]       [ OUT 4 ]
        Mixing L        Mixing R        Sub / Mon 2 L   Sub / Mon 2 R
        Full / HPF      Full / HPF      Sub Only LPF    Sub Only LPF
            │               │               │               │
            └───────┬───────┘               └───────┬───────┘
                    ▼                               ▼
          Main Out 1/2 (Studio 26c)       Line Out 3/4 (Studio 26c)
```

---

## Installation & Usage

### Option 1: Standalone `.exe` (Recommended)
Download `4-Bus-Matrix-Master.exe` from the **Releases** tab and run it directly.  
`settings.json` and `app.log` are created automatically alongside the executable.

### Option 2: Run from Source (Python 3.12+ / 3.14)
```powershell
git clone https://github.com/lukman582890-wq/4-Bus-Matrix-Master.git
cd 4-Bus-Matrix-Master
pip install -r requirements.txt
python main.py
```

In Windows Sound Output (`Win + Ctrl + V`), select **`4-Bus Matrix Master (4-Bus Matrix Audio)`**.

### Build Standalone `.exe` (PyInstaller)
```powershell
pyinstaller --noconfirm --onefile --windowed --name "4-Bus-Matrix-Master" --add-data "ui;ui" --hidden-import "pyaudiowpatch" --hidden-import "soundfile" --hidden-import "scipy.signal" --hidden-import "pystray._win32" --collect-submodules "webview" --collect-data "webview" main.py
```

---

## Project Structure

```text
├── audio_engine.py        # Real-time WASAPI Loopback DSP, crossover, limiter, BS.1770 LUFS, WAV rec
├── bridge.py              # pywebview JS <-> Python Api bridge, 30 Hz meter push, settings.json
├── main.py                # WebView2 + pystray System Tray + Auto-Start entry point
├── manage_endpoints.py    # Windows MMDevices endpoint friendly-name manager
├── gui_legacy.py          # Legacy CustomTkinter GUI backup
├── ui/
│   ├── index.html         # Studio mixer UI layout & routing/close modals
│   ├── style.css          # Dark studio theme, faders, knobs, responsive layout
│   ├── app.js             # 60 FPS Canvas meters/graph, SVG knobs, fader physics
│   └── fonts/             # Local offline Inter & JetBrains Mono woff2 fonts
├── virtual_audio_driver/  # Experimental WDK SysVAD virtual audio driver source & scripts
├── requirements.txt
└── Start_Mixer.bat
```
