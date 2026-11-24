(() => {
  "use strict";

  const BUS_META = [
    { id: 0, tag: "OUT 1", name: "Mixing L", accent: "#38d6e8", isSub: false },
    { id: 1, tag: "OUT 2", name: "Mixing R", accent: "#38d6e8", isSub: false },
    { id: 2, tag: "OUT 3", name: "Sub / Mon 2 L", accent: "#ffb020", isSub: true },
    { id: 3, tag: "OUT 4", name: "Sub / Mon 2 R", accent: "#ffb020", isSub: true },
  ];

  // Fader curve: -60 (-inf) .. +12 dB with generous resolution around 0 dB
  const FADER_MIN = -60.0;
  const FADER_MAX = 12.0;
  const SCALE_MARKS = [
    { db: 12, label: "+12" },
    { db: 6, label: "+6" },
    { db: 0, label: "0", zero: true },
    { db: -6, label: "-6" },
    { db: -12, label: "-12" },
    { db: -24, label: "-24" },
    { db: -60, label: "-inf" },
  ];

  let devices = null;
  let state = null;
  let activePopBus = null;

  // Per-bus + Master animated fader & meter state (60 FPS)
  const makeMeterState = (canvas = null) => ({
    targetDb: -60,
    displayDb: -60,
    peakHoldDb: -60,
    peakHoldUntil: 0,
    canvas,
  });
  const busMeters = [0, 1, 2, 3].map(() => makeMeterState());
  const masterMeterL = makeMeterState();
  const masterMeterR = makeMeterState();
  const faderAnim = [null, null, null, null];
  let masterFaderAnim = null;
  const stripEls = [];

  // 60-second Short-Term LUFS history buffer: [{ t: ms, st: lufs }]
  const lufsHistory = [];
  let lastGraphSampleTs = 0;

  const $ = (id) => document.getElementById(id);

  // ---------------- Math Helpers ----------------
  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  // Piecewise dB <-> normalized [0..1] (0=bottom -60dB, 1=top +12dB)
  // Top 75% covers -24..+12 dB linearly; bottom 25% covers -60..-24 dB
  function dbToNorm(db) {
    const d = clamp(db, FADER_MIN, FADER_MAX);
    if (d >= -24) return 0.25 + ((d + 24) / 36) * 0.75;
    return ((d + 60) / 36) * 0.25;
  }

  function normToDb(norm) {
    const n = clamp(norm, 0, 1);
    if (n >= 0.25) return -24 + ((n - 0.25) / 0.75) * 36;
    return -60 + (n / 0.25) * 36;
  }

  // Logarithmic knob mapping
  function hzToLogNorm(hz, minHz, maxHz) {
    const clamped = clamp(hz, minHz, maxHz);
    return Math.log(clamped / minHz) / Math.log(maxHz / minHz);
  }

  function logNormToHz(norm, minHz, maxHz) {
    const n = clamp(norm, 0, 1);
    return minHz * Math.pow(maxHz / minHz, n);
  }

  function formatDb(db) {
    if (db <= -59.5) return "-inf";
    const sign = db >= 0 ? "+" : "";
    return `${sign}${db.toFixed(1)}`;
  }

  function formatPan(pan) {
    if (Math.abs(pan) < 0.04) return "C";
    return pan < 0 ? `L${Math.round(Math.abs(pan) * 100)}` : `R${Math.round(pan * 100)}`;
  }

  function shortModeTag(mode) {
    if (mode === "Full Range") return "Full Range";
    if (mode === "Stereo Top (HPF)") return "Top · HPF";
    if (mode === "Sub Only (LPF)") return "Sub · LPF";
    return "Bandpass";
  }

  function shortDeviceName(label) {
    if (!label) return "None";
    return label
      .replace(/^\[OUT TAP\]\s*/i, "")
      .replace(/^\[IN\]\s*/i, "")
      .replace(/\s*\[Loopback\]/i, "")
      .replace(/\s*\(\d+\s*Hz\)/i, "")
      .replace(/\s*\(4-Bus Matrix Audio\)/i, "")
      .replace(/\s*\(Studio 26c\)/i, "")
      .replace("4-Bus Matrix Master", "Master")
      .replace("Main Out 1/2", "Main 1/2")
      .replace("Line Out 3/4", "Line 3/4")
      .trim();
  }

  function shortTap(tap) {
    if (tap === "L (Ch 1)") return "L";
    if (tap === "R (Ch 2)") return "R";
    if (tap === "L+R (Mono)") return "Mono";
    return "L+R";
  }

  // Throttle helper (~60 calls/sec max per key)
  const throttleTimers = new Map();
  function throttledCall(key, fn, delayMs = 16) {
    const entry = throttleTimers.get(key);
    const now = performance.now();
    if (!entry || now - entry.last >= delayMs) {
      if (entry && entry.timer) clearTimeout(entry.timer);
      throttleTimers.set(key, { last: now, timer: null });
      fn();
    } else {
      if (entry.timer) clearTimeout(entry.timer);
      entry.timer = setTimeout(() => {
        throttleTimers.set(key, { last: performance.now(), timer: null });
        fn();
      }, delayMs);
    }
  }

  // ---------------- SVG Knob Helper ----------------
  function polarToXY(cx, cy, r, deg) {
    const rad = ((deg - 90) * Math.PI) / 180;
    return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
  }

  function arcPath(cx, cy, r, startDeg, endDeg) {
    const [x1, y1] = polarToXY(cx, cy, r, startDeg);
    const [x2, y2] = polarToXY(cx, cy, r, endDeg);
    const large = Math.abs(endDeg - startDeg) > 180 ? 1 : 0;
    return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
  }

  function buildKnobSvg(accent) {
    const bgArc = arcPath(24, 24, 19, -135, 135);
    return `
      <svg class="knob-svg" viewBox="0 0 48 48" width="46" height="46">
        <path d="${bgArc}" fill="none" stroke="#252c37" stroke-width="3.2" stroke-linecap="round"/>
        <path class="knob-arc" d="${bgArc}" fill="none" stroke="${accent}" stroke-width="3.2" stroke-linecap="round"/>
        <circle cx="24" cy="24" r="13.5" fill="url(#knobGrad)" stroke="#3b4454" stroke-width="1.2"/>
        <line class="knob-ptr" x1="24" y1="24" x2="24" y2="12.5" stroke="${accent}" stroke-width="2.2" stroke-linecap="round"/>
      </svg>`;
  }

  function updateKnobVisual(knobEl, hz, minHz, maxHz) {
    const norm = hzToLogNorm(hz, minHz, maxHz);
    const angle = -135 + norm * 270;
    const arcEl = knobEl.querySelector(".knob-arc");
    const ptrEl = knobEl.querySelector(".knob-ptr");
    const valEl = knobEl.querySelector(".knob-val");
    if (norm <= 0.002) {
      arcEl.setAttribute("d", "");
    } else {
      arcEl.setAttribute("d", arcPath(24, 24, 19, -135, angle));
    }
    const [px, py] = polarToXY(24, 24, 11.5, angle);
    ptrEl.setAttribute("x2", px.toFixed(2));
    ptrEl.setAttribute("y2", py.toFixed(2));
    valEl.textContent = `${Math.round(hz)} Hz`;
  }

  // ---------------- Build 4 Bus Strips DOM ----------------
  function buildBusStrips() {
    const grid = $("busGrid");
    // Shared SVG gradient definition
    const defsSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    defsSvg.setAttribute("width", "0");
    defsSvg.setAttribute("height", "0");
    defsSvg.style.position = "absolute";
    defsSvg.innerHTML = `
      <defs>
        <linearGradient id="knobGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#363e4d"/>
          <stop offset="100%" stop-color="#191e26"/>
        </linearGradient>
      </defs>`;
    document.body.appendChild(defsSvg);

    grid.innerHTML = "";
    BUS_META.forEach((meta, idx) => {
      const strip = document.createElement("article");
      strip.className = `bus-strip ${meta.isSub ? "bus-sub" : "bus-mix"}`;
      strip.dataset.bus = String(idx);

      const scaleHtml = SCALE_MARKS.map((m) => {
        const pct = (1 - dbToNorm(m.db)) * 100;
        return `<span class="scale-mark ${m.zero ? "zero" : ""}" style="top:calc(12px + (100% - 24px) * ${pct / 100})">${m.label}</span>`;
      }).join("");

      const zeroPct = (1 - dbToNorm(0)) * 100;

      strip.innerHTML = `
        <div class="strip-head">
          <div class="strip-id-wrap">
            <div class="strip-out-num">${meta.tag}</div>
            <div class="strip-name">${meta.name}</div>
          </div>
          <span class="mode-tag">Full Range</span>
        </div>

        <button class="route-chip" type="button" title="Klik untuk atur Send To, Input Source & Crossover">
          <span class="route-chip-text">Loading...</span>
          <span class="route-chip-caret">▾</span>
        </button>

        <div class="knobs-row">
          <div class="knob-wrap knob-hpf" tabindex="0" role="slider" aria-label="${meta.tag} HPF" aria-valuemin="20" aria-valuemax="500" title="HPF (20-500 Hz) · Drag / Scroll / Double-click reset">
            <span class="knob-label">HPF</span>
            ${buildKnobSvg(meta.accent)}
            <span class="knob-val">90 Hz</span>
          </div>
          <div class="knob-wrap knob-lpf" tabindex="0" role="slider" aria-label="${meta.tag} LPF" aria-valuemin="40" aria-valuemax="250" title="LPF (40-250 Hz) · Drag / Scroll / Double-click reset">
            <span class="knob-label">LPF</span>
            ${buildKnobSvg(meta.accent)}
            <span class="knob-val">90 Hz</span>
          </div>
        </div>

        <div class="pan-block">
          <div class="pan-head">
            <span>PAN</span>
            <span class="pan-val">C</span>
          </div>
          <div class="pan-track" tabindex="0" role="slider" aria-label="${meta.tag} Pan" aria-valuemin="-100" aria-valuemax="100" title="Drag / Scroll / Double-click or Right-click reset">
            <div class="pan-center-tick"></div>
            <div class="pan-fill"></div>
            <div class="pan-thumb"></div>
          </div>
        </div>

        <div class="fader-stage">
          <div class="fader-scale">${scaleHtml}</div>
          <div class="fader-track" tabindex="0" role="slider" aria-label="${meta.tag} Gain" aria-valuemin="-60" aria-valuemax="12" title="Drag / Shift+Drag Fine / Scroll 0.1dB / Dbl-Click, Right-Click, Ctrl+Click = 0 dB">
            <div class="fader-rail"></div>
            <div class="fader-zero-line" style="top:calc(12px + (100% - 24px) * ${zeroPct / 100})"></div>
            <div class="fader-cap"></div>
          </div>
          <div class="meter-wrap">
            <canvas class="meter-canvas" width="14" height="220"></canvas>
          </div>
        </div>

        <button class="db-pill" type="button" title="Klik kanan / Double-click untuk reset 0.0 dB">+0.0</button>

        <div class="msp-row">
          <button class="btn-msp btn-m" type="button" aria-pressed="false" title="Mute">M</button>
          <button class="btn-msp btn-s" type="button" aria-pressed="false" title="Solo">S</button>
          <button class="btn-msp btn-p" type="button" aria-pressed="false" title="Polarity Invert (180°)">Ø</button>
        </div>
      `;

      grid.appendChild(strip);

      const refs = {
        el: strip,
        modeTag: strip.querySelector(".mode-tag"),
        routeChip: strip.querySelector(".route-chip"),
        routeText: strip.querySelector(".route-chip-text"),
        knobHpf: strip.querySelector(".knob-hpf"),
        knobLpf: strip.querySelector(".knob-lpf"),
        panTrack: strip.querySelector(".pan-track"),
        panFill: strip.querySelector(".pan-fill"),
        panThumb: strip.querySelector(".pan-thumb"),
        panVal: strip.querySelector(".pan-val"),
        faderTrack: strip.querySelector(".fader-track"),
        faderCap: strip.querySelector(".fader-cap"),
        dbPill: strip.querySelector(".db-pill"),
        btnM: strip.querySelector(".btn-m"),
        btnS: strip.querySelector(".btn-s"),
        btnP: strip.querySelector(".btn-p"),
        meterCanvas: strip.querySelector(".meter-canvas"),
      };
      busMeters[idx].canvas = refs.meterCanvas;
      stripEls.push(refs);

      bindStripEvents(idx, refs);
    });
  }

  // ---------------- Link Helper (Local Instant UI Mirror) ----------------
  function getLinkedIndices(bus) {
    if (!state) return [bus];
    if (bus < 2 && state.link_12) return [bus, 1 - bus];
    if (bus >= 2 && state.link_34) return [bus, 5 - bus];
    return [bus];
  }

  function applyGainVisual(bus, db) {
    const refs = stripEls[bus];
    const norm = dbToNorm(db);
    const pct = (1 - norm) * 100;
    refs.faderCap.style.top = `calc(12px + (100% - 24px) * ${pct / 100})`;
    refs.dbPill.textContent = formatDb(db);
    refs.faderTrack.setAttribute("aria-valuenow", db.toFixed(1));
  }

  function setBusGain(bus, db, animate = false) {
    const targetDb = clamp(db, FADER_MIN, FADER_MAX);
    const indices = getLinkedIndices(bus);

    if (animate) {
      indices.forEach((i) => {
        if (faderAnim[i]) cancelAnimationFrame(faderAnim[i]);
        const startDb = state ? state.channels[i].gain_db : targetDb;
        const startTs = performance.now();
        const dur = 150;
        // Update numeric dB immediately as requested
        stripEls[i].dbPill.textContent = formatDb(targetDb);
        if (state) state.channels[i].gain_db = targetDb;

        const step = (now) => {
          const t = clamp((now - startTs) / dur, 0, 1);
          const ease = 1 - Math.pow(1 - t, 3);
          const cur = startDb + (targetDb - startDb) * ease;
          const norm = dbToNorm(cur);
          stripEls[i].faderCap.style.top = `calc(12px + (100% - 24px) * ${1 - norm})`;
          if (t < 1) faderAnim[i] = requestAnimationFrame(step);
          else {
            faderAnim[i] = null;
            applyGainVisual(i, targetDb);
          }
        };
        faderAnim[i] = requestAnimationFrame(step);
      });
      window.pywebview.api.set_gain(bus, targetDb).then(syncStateLight);
    } else {
      indices.forEach((i) => {
        if (faderAnim[i]) {
          cancelAnimationFrame(faderAnim[i]);
          faderAnim[i] = null;
        }
        if (state) state.channels[i].gain_db = targetDb;
        applyGainVisual(i, targetDb);
      });
      throttledCall(`gain-${bus}`, () => {
        window.pywebview.api.set_gain(bus, targetDb);
      });
    }
  }

  function setBusFilterHz(bus, kind, hz) {
    const minHz = kind === "hpf" ? 20 : 40;
    const maxHz = kind === "hpf" ? 500 : 250;
    const val = clamp(hz, minHz, maxHz);
    const indices = getLinkedIndices(bus);
    indices.forEach((i) => {
      if (state) state.channels[i][kind === "hpf" ? "hpf_hz" : "lpf_hz"] = val;
      updateKnobVisual(kind === "hpf" ? stripEls[i].knobHpf : stripEls[i].knobLpf, val, minHz, maxHz);
    });
    throttledCall(`${kind}-${bus}`, () => {
      if (kind === "hpf") window.pywebview.api.set_hpf(bus, val);
      else window.pywebview.api.set_lpf(bus, val);
    });
  }

  function applyPanVisual(bus, pan) {
    const refs = stripEls[bus];
    const pct = ((pan + 1) / 2) * 100;
    refs.panThumb.style.left = `${pct}%`;
    if (pan >= 0) {
      refs.panFill.style.left = "50%";
      refs.panFill.style.width = `${(pan / 2) * 100}%`;
    } else {
      refs.panFill.style.left = `${pct}%`;
      refs.panFill.style.width = `${(Math.abs(pan) / 2) * 100}%`;
    }
    refs.panVal.textContent = formatPan(pan);
    refs.panTrack.setAttribute("aria-valuenow", String(Math.round(pan * 100)));
  }

  function setBusPan(bus, pan) {
    const val = clamp(pan, -1, 1);
    if (state) state.channels[bus].pan = val;
    applyPanVisual(bus, val);
    throttledCall(`pan-${bus}`, () => {
      window.pywebview.api.set_pan(bus, val);
    });
  }

  // ---------------- Bind Strip Controls ----------------
  function bindStripEvents(bus, refs) {
    // Routing popover trigger
    refs.routeChip.addEventListener("click", (e) => {
      e.stopPropagation();
      openPopover(bus, refs.routeChip);
    });

    // HPF & LPF Knobs (Logarithmic scale, drag vertical, Shift fine, scroll, dblclick reset, keyboard)
    [
      { el: refs.knobHpf, kind: "hpf", min: 20, max: 500 },
      { el: refs.knobLpf, kind: "lpf", min: 40, max: 250 },
    ].forEach(({ el, kind, min, max }) => {
      el.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        el.focus();
        const startY = e.clientY;
        const startHz = state ? state.channels[bus][kind === "hpf" ? "hpf_hz" : "lpf_hz"] : min;
        let norm = hzToLogNorm(startHz, min, max);
        let lastY = startY;

        const onMove = (ev) => {
          const dy = lastY - ev.clientY;
          lastY = ev.clientY;
          const sens = ev.shiftKey ? 0.0008 : 0.005;
          norm = clamp(norm + dy * sens, 0, 1);
          setBusFilterHz(bus, kind, logNormToHz(norm, min, max));
        };
        const onUp = () => {
          window.removeEventListener("mousemove", onMove);
          window.removeEventListener("mouseup", onUp);
        };
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onUp);
      });

      el.addEventListener("wheel", (e) => {
        e.preventDefault();
        const cur = state ? state.channels[bus][kind === "hpf" ? "hpf_hz" : "lpf_hz"] : min;
        const step = e.shiftKey ? 0.004 : 0.015;
        const dir = e.deltaY < 0 ? 1 : -1;
        const n = clamp(hzToLogNorm(cur, min, max) + dir * step, 0, 1);
        setBusFilterHz(bus, kind, logNormToHz(n, min, max));
      }, { passive: false });

      el.addEventListener("dblclick", () => {
        if (!state) return;
        const defHz = kind === "hpf" ? state.channels[bus].default_hpf_hz : state.channels[bus].default_lpf_hz;
        setBusFilterHz(bus, kind, defHz);
      });

      el.addEventListener("keydown", (e) => {
        if (!state) return;
        const cur = state.channels[bus][kind === "hpf" ? "hpf_hz" : "lpf_hz"];
        const step = e.shiftKey ? 0.005 : 0.025;
        if (e.key === "ArrowUp" || e.key === "ArrowRight") {
          e.preventDefault();
          setBusFilterHz(bus, kind, logNormToHz(hzToLogNorm(cur, min, max) + step, min, max));
        } else if (e.key === "ArrowDown" || e.key === "ArrowLeft") {
          e.preventDefault();
          setBusFilterHz(bus, kind, logNormToHz(hzToLogNorm(cur, min, max) - step, min, max));
        }
      });
    });

    // Bipolar Pan (Reset to default per strip: OUT1/OUT3 = L100, OUT2/OUT4 = R100)
    const resetPan = () => {
      if (!state) return;
      setBusPan(bus, state.channels[bus].default_pan);
    };

    refs.panTrack.addEventListener("mousedown", (e) => {
      if (e.button === 2) return;
      e.preventDefault();
      refs.panTrack.focus();
      const rect = refs.panTrack.getBoundingClientRect();
      let curPan = state ? state.channels[bus].pan : 0;
      let lastX = e.clientX;
      if (!e.shiftKey) {
        curPan = clamp(((e.clientX - rect.left) / rect.width) * 2 - 1, -1, 1);
        setBusPan(bus, curPan);
      }

      const onMove = (ev) => {
        if (ev.shiftKey) {
          const dx = ev.clientX - lastX;
          curPan = clamp(curPan + (dx / rect.width) * 0.25, -1, 1);
        } else {
          curPan = clamp(((ev.clientX - rect.left) / rect.width) * 2 - 1, -1, 1);
        }
        lastX = ev.clientX;
        setBusPan(bus, curPan);
      };
      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
    refs.panTrack.addEventListener("dblclick", resetPan);
    refs.panTrack.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      resetPan();
    });
    refs.panTrack.addEventListener("wheel", (e) => {
      e.preventDefault();
      const cur = state ? state.channels[bus].pan : 0;
      const step = e.shiftKey ? 0.01 : 0.05;
      setBusPan(bus, cur + (e.deltaY < 0 ? step : -step));
    }, { passive: false });
    refs.panTrack.addEventListener("keydown", (e) => {
      const cur = state ? state.channels[bus].pan : 0;
      const step = e.shiftKey ? 0.02 : 0.1;
      if (e.key === "ArrowRight" || e.key === "ArrowUp") {
        e.preventDefault();
        setBusPan(bus, cur + step);
      } else if (e.key === "ArrowLeft" || e.key === "ArrowDown") {
        e.preventDefault();
        setBusPan(bus, cur - step);
      }
    });

    // Vertical Fader: Drag, Shift+Drag fine (10x), Scroll 0.1 dB, Dbl-Click / Right-Click / Ctrl+Click = 0 dB (150ms anim)
    refs.faderTrack.addEventListener("mousedown", (e) => {
      if (e.button === 2) return;
      e.preventDefault();
      refs.faderTrack.focus();
      if (e.ctrlKey) {
        setBusGain(bus, 0.0, true);
        return;
      }
      refs.faderTrack.classList.add("dragging");
      const rect = refs.faderTrack.getBoundingClientRect();
      const usableH = Math.max(40, rect.height - 24);
      let lastY = e.clientY;
      let curNorm = state ? dbToNorm(state.channels[bus].gain_db) : dbToNorm(0);

      // Jump to click position if not holding Shift
      if (!e.shiftKey) {
        curNorm = clamp(1 - (e.clientY - rect.top - 12) / usableH, 0, 1);
        setBusGain(bus, normToDb(curNorm), false);
      }

      const onMove = (ev) => {
        const dy = lastY - ev.clientY;
        lastY = ev.clientY;
        if (ev.shiftKey) {
          // Fine mode: 10x finer resolution
          curNorm = clamp(curNorm + (dy / usableH) * 0.1, 0, 1);
        } else {
          curNorm = clamp(1 - (ev.clientY - rect.top - 12) / usableH, 0, 1);
        }
        setBusGain(bus, normToDb(curNorm), false);
      };
      const onUp = () => {
        refs.faderTrack.classList.remove("dragging");
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });

    refs.faderTrack.addEventListener("dblclick", () => setBusGain(bus, 0.0, true));
    refs.faderTrack.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      setBusGain(bus, 0.0, true);
    });
    refs.faderTrack.addEventListener("wheel", (e) => {
      e.preventDefault();
      const cur = state ? state.channels[bus].gain_db : 0;
      const step = 0.1;
      setBusGain(bus, cur + (e.deltaY < 0 ? step : -step), false);
    }, { passive: false });
    refs.faderTrack.addEventListener("keydown", (e) => {
      const cur = state ? state.channels[bus].gain_db : 0;
      const step = e.shiftKey ? 0.1 : 1.0;
      if (e.key === "ArrowUp" || e.key === "ArrowRight") {
        e.preventDefault();
        setBusGain(bus, cur + step, false);
      } else if (e.key === "ArrowDown" || e.key === "ArrowLeft") {
        e.preventDefault();
        setBusGain(bus, cur - step, false);
      } else if (e.key === "Home") {
        e.preventDefault();
        setBusGain(bus, 0.0, true);
      }
    });

    // dB pill reset
    refs.dbPill.addEventListener("dblclick", () => setBusGain(bus, 0.0, true));
    refs.dbPill.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      setBusGain(bus, 0.0, true);
    });

    // M / S / Phase
    refs.btnM.addEventListener("click", async () => {
      if (!state) return;
      const res = await window.pywebview.api.set_mute(bus, !state.channels[bus].mute);
      renderState(res);
    });
    refs.btnS.addEventListener("click", async () => {
      if (!state) return;
      const res = await window.pywebview.api.set_solo(bus, !state.channels[bus].solo);
      renderState(res);
    });
    refs.btnP.addEventListener("click", async () => {
      if (!state) return;
      const res = await window.pywebview.api.set_phase(bus, !state.channels[bus].phase_invert);
      renderState(res);
    });
  }

  // ---------------- Routing Popover ----------------
  function openPopover(bus, anchorEl) {
    if (!state || !devices) return;
    activePopBus = bus;
    const ch = state.channels[bus];
    const pop = $("routingPopover");
    $("popTitle").textContent = `${BUS_META[bus].tag} · ${BUS_META[bus].name} Routing`;

    fillSelect($("popSendTo"), devices.outputs.map((d) => d.label), ch.target_output_label);
    fillSelect($("popSourceMode"), devices.source_modes, ch.source_mode);
    fillSelect($("popCapDev"), devices.captures.map((d) => d.label), ch.capture_device_label);
    fillSelect($("popTapMode"), devices.tap_modes, ch.tap_mode);
    fillSelect($("popToneType"), devices.tone_types, ch.tone_type);
    fillSelect($("popFilterMode"), devices.filter_modes, ch.filter_mode);
    $("popFileName").textContent = ch.file_name || "No file loaded";

    updatePopoverSourceBoxes(ch.source_mode);

    pop.classList.remove("hidden");
    const rect = anchorEl.getBoundingClientRect();
    const left = clamp(rect.left, 12, window.innerWidth - pop.offsetWidth - 12);
    const top = clamp(rect.bottom + 6, 12, window.innerHeight - pop.offsetHeight - 12);
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
  }

  function closePopover() {
    activePopBus = null;
    $("routingPopover").classList.add("hidden");
  }

  function updatePopoverSourceBoxes(mode) {
    $("popSoundcardBox").classList.toggle("hidden", mode !== "Soundcard");
    $("popFileBox").classList.toggle("hidden", mode !== "Audio File");
    $("popToneBox").classList.toggle("hidden", mode !== "Test Tone");
  }

  function fillSelect(sel, values, current) {
    sel.innerHTML = values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
    if (values.includes(current)) sel.value = current;
  }

  function escapeHtml(str) {
    return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // ---------------- Render State ----------------
  function syncStateLight(st) {
    if (st && st.ok) state = st;
  }

  function renderState(st) {
    if (!st) return;
    if (!st.ok) {
      if (st.error) showError(st.error);
      return;
    }
    state = st;
    if (st.error) showError(st.error);
    else hideError();

    // Header subtitle
    const devShort = (st.primary_output_label || "No Device").replace(/\s*\(\d+\s*Hz\)/, "");
    $("subtitle").textContent = `${devShort} · ${st.sample_rate} Hz · WASAPI · ${st.running ? "ACTIVE" : "STOPPED"}`;

    // Primary out & Preset dropdowns
    if (devices) {
      fillSelect($("selPrimaryOut"), devices.outputs.map((d) => d.label), st.primary_output_label);
      fillSelect($("selPreset"), st.presets, st.preset);
    }
    $("btnDelPreset").disabled = !(st.custom_presets || []).includes(st.preset);

    // Links & Auto-Start
    $("btnLink12").classList.toggle("active-cyan", st.link_12);
    $("btnLink12").setAttribute("aria-pressed", String(st.link_12));
    $("btnLink34").classList.toggle("active-amber", st.link_34);
    $("btnLink34").setAttribute("aria-pressed", String(st.link_34));
    $("btnAutoStart").classList.toggle("active-green", !!st.autostart);
    $("btnAutoStart").setAttribute("aria-pressed", String(!!st.autostart));

    // Rec & Power
    const btnRec = $("btnRec");
    btnRec.classList.toggle("recording", st.recording);
    btnRec.textContent = st.recording ? "■ STOP REC" : "● REC WAV";

    const btnPow = $("btnPower");
    btnPow.classList.toggle("running", st.running);
    btnPow.textContent = st.running ? "STOP MIXER" : "START MIXER";

    // Render 4 strips
    st.channels.forEach((ch, i) => {
      const refs = stripEls[i];
      if (!refs) return;
      refs.modeTag.textContent = shortModeTag(ch.filter_mode);

      // Chip text: "Main 1/2 · In: Master L"
      const outShort = shortDeviceName(ch.target_output_label);
      let inShort = "";
      if (ch.source_mode === "Soundcard") {
        inShort = `${shortDeviceName(ch.capture_device_label)} ${shortTap(ch.tap_mode)}`;
      } else if (ch.source_mode === "Audio File") {
        inShort = ch.file_name || "No File";
      } else {
        inShort = ch.tone_type;
      }
      refs.routeText.textContent = `${outShort} · In: ${inShort}`;

      // Knobs
      updateKnobVisual(refs.knobHpf, ch.hpf_hz, 20, 500);
      updateKnobVisual(refs.knobLpf, ch.lpf_hz, 40, 250);
      refs.knobHpf.classList.toggle("inactive", ch.filter_mode === "Full Range" || ch.filter_mode === "Sub Only (LPF)");
      refs.knobLpf.classList.toggle("inactive", ch.filter_mode === "Full Range" || ch.filter_mode === "Stereo Top (HPF)");

      // Pan & Fader
      applyPanVisual(i, ch.pan);
      if (!faderAnim[i]) applyGainVisual(i, ch.gain_db);

      // M / S / Phase
      refs.btnM.classList.toggle("active-mute", ch.mute);
      refs.btnM.setAttribute("aria-pressed", String(ch.mute));
      refs.btnS.classList.toggle("active-solo", ch.solo);
      refs.btnS.setAttribute("aria-pressed", String(ch.solo));
      refs.btnP.classList.toggle("active-phase", ch.phase_invert);
      refs.btnP.setAttribute("aria-pressed", String(ch.phase_invert));
    });

    // Render MAIN OUT panel state
    $("chkLimiter").checked = !!st.limiter_enabled;
    $("btnMono").classList.toggle("active-mono", !!st.master_mono);
    $("btnMono").setAttribute("aria-pressed", String(!!st.master_mono));
    $("btnDim").classList.toggle("active-dim", !!st.master_dim);
    $("btnDim").setAttribute("aria-pressed", String(!!st.master_dim));
    $("btnMasterMute").classList.toggle("active-mute", !!st.master_mute);
    $("btnMasterMute").setAttribute("aria-pressed", String(!!st.master_mute));
    if (!masterFaderAnim) applyMasterGainVisual(st.master_gain_db);
  }

  function showError(msg) {
    $("errorText").textContent = msg;
    $("errorBanner").classList.remove("hidden");
  }
  function hideError() {
    $("errorBanner").classList.add("hidden");
  }

  // ---------------- MAIN OUT Controls ----------------
  function applyMasterGainVisual(db) {
    const norm = dbToNorm(db);
    const pct = (1 - norm) * 100;
    $("masterFaderCap").style.top = `calc(12px + (100% - 24px) * ${pct / 100})`;
    const dimTag = state && state.master_dim && db > -59.5 ? " · DIM" : "";
    $("masterDbPill").textContent = `${formatDb(db)} dB${dimTag}`;
    $("masterFaderTrack").setAttribute("aria-valuenow", db.toFixed(1));
  }

  function setMasterGain(db, animate = false) {
    const targetDb = clamp(db, FADER_MIN, FADER_MAX);
    if (animate) {
      if (masterFaderAnim) cancelAnimationFrame(masterFaderAnim);
      const startDb = state ? state.master_gain_db : targetDb;
      const startTs = performance.now();
      const dur = 150;
      if (state) state.master_gain_db = targetDb;
      const dimTag = state && state.master_dim && targetDb > -59.5 ? " · DIM" : "";
      $("masterDbPill").textContent = `${formatDb(targetDb)} dB${dimTag}`;

      const step = (now) => {
        const t = clamp((now - startTs) / dur, 0, 1);
        const ease = 1 - Math.pow(1 - t, 3);
        const cur = startDb + (targetDb - startDb) * ease;
        const norm = dbToNorm(cur);
        $("masterFaderCap").style.top = `calc(12px + (100% - 24px) * ${1 - norm})`;
        if (t < 1) masterFaderAnim = requestAnimationFrame(step);
        else {
          masterFaderAnim = null;
          applyMasterGainVisual(targetDb);
        }
      };
      masterFaderAnim = requestAnimationFrame(step);
      window.pywebview.api.set_master_gain(targetDb).then(syncStateLight);
    } else {
      if (masterFaderAnim) {
        cancelAnimationFrame(masterFaderAnim);
        masterFaderAnim = null;
      }
      if (state) state.master_gain_db = targetDb;
      applyMasterGainVisual(targetDb);
      throttledCall("master-gain", () => {
        window.pywebview.api.set_master_gain(targetDb);
      });
    }
  }

  function initMainOutPanel() {
    // Build master dB scale + zero line
    $("masterScale").innerHTML = SCALE_MARKS.map((m) => {
      const pct = (1 - dbToNorm(m.db)) * 100;
      return `<span class="scale-mark ${m.zero ? "zero" : ""}" style="top:calc(12px + (100% - 24px) * ${pct / 100})">${m.label}</span>`;
    }).join("");
    const zeroPct = (1 - dbToNorm(0)) * 100;
    $("masterZeroLine").style.top = `calc(12px + (100% - 24px) * ${zeroPct / 100})`;

    masterMeterL.canvas = $("meterMasterL");
    masterMeterR.canvas = $("meterMasterR");

    const track = $("masterFaderTrack");
    track.addEventListener("mousedown", (e) => {
      if (e.button === 2) return;
      e.preventDefault();
      track.focus();
      if (e.ctrlKey) {
        setMasterGain(0.0, true);
        return;
      }
      track.classList.add("dragging");
      const rect = track.getBoundingClientRect();
      const usableH = Math.max(40, rect.height - 24);
      let lastY = e.clientY;
      let curNorm = state ? dbToNorm(state.master_gain_db) : dbToNorm(0);

      if (!e.shiftKey) {
        curNorm = clamp(1 - (e.clientY - rect.top - 12) / usableH, 0, 1);
        setMasterGain(normToDb(curNorm), false);
      }

      const onMove = (ev) => {
        const dy = lastY - ev.clientY;
        lastY = ev.clientY;
        if (ev.shiftKey) {
          curNorm = clamp(curNorm + (dy / usableH) * 0.1, 0, 1);
        } else {
          curNorm = clamp(1 - (ev.clientY - rect.top - 12) / usableH, 0, 1);
        }
        setMasterGain(normToDb(curNorm), false);
      };
      const onUp = () => {
        track.classList.remove("dragging");
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });

    track.addEventListener("dblclick", () => setMasterGain(0.0, true));
    track.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      setMasterGain(0.0, true);
    });
    track.addEventListener("wheel", (e) => {
      e.preventDefault();
      const cur = state ? state.master_gain_db : 0;
      setMasterGain(cur + (e.deltaY < 0 ? 0.1 : -0.1), false);
    }, { passive: false });
    track.addEventListener("keydown", (e) => {
      const cur = state ? state.master_gain_db : 0;
      const step = e.shiftKey ? 0.1 : 1.0;
      if (e.key === "ArrowUp" || e.key === "ArrowRight") {
        e.preventDefault();
        setMasterGain(cur + step, false);
      } else if (e.key === "ArrowDown" || e.key === "ArrowLeft") {
        e.preventDefault();
        setMasterGain(cur - step, false);
      } else if (e.key === "Home") {
        e.preventDefault();
        setMasterGain(0.0, true);
      }
    });

    $("masterDbPill").addEventListener("dblclick", () => setMasterGain(0.0, true));
    $("masterDbPill").addEventListener("contextmenu", (e) => {
      e.preventDefault();
      setMasterGain(0.0, true);
    });

    $("chkLimiter").addEventListener("change", async (e) => {
      renderState(await window.pywebview.api.set_soft_limiter(e.target.checked));
    });
    $("btnResetLufs").addEventListener("click", async () => {
      lufsHistory.length = 0;
      renderState(await window.pywebview.api.reset_lufs());
    });
    $("btnMono").addEventListener("click", async () => {
      if (!state) return;
      renderState(await window.pywebview.api.set_mono(!state.master_mono));
    });
    $("btnDim").addEventListener("click", async () => {
      if (!state) return;
      renderState(await window.pywebview.api.set_dim(!state.master_dim));
    });
    $("btnMasterMute").addEventListener("click", async () => {
      if (!state) return;
      renderState(await window.pywebview.api.set_master_mute(!state.master_mute));
    });
  }

  // ---------------- 30 Hz Meter Push + 60 FPS Canvas Animation ----------------
  function feedMeter(mState, db, now) {
    mState.targetDb = db;
    if (db >= mState.peakHoldDb || now >= mState.peakHoldUntil) {
      mState.peakHoldDb = db;
      mState.peakHoldUntil = now + 1500; // 1.5s peak hold
    }
  }

  window.__onMeters = (m) => {
    const now = performance.now();
    if (Array.isArray(m.buses)) {
      m.buses.forEach((db, i) => feedMeter(busMeters[i], db, now));
    }
    feedMeter(masterMeterL, m.master_l, now);
    feedMeter(masterMeterR, m.master_r, now);

    // Update numeric LUFS & Peak readouts
    $("valLufsInt").textContent = m.lufs_int.toFixed(1);
    $("valLufsSt").textContent = `${m.lufs_st.toFixed(1)} LUFS`;

    const pkEl = $("valPeakDb");
    pkEl.textContent = `${m.peak.toFixed(1)} dBFS`;
    pkEl.style.color = m.peak > -4.5 ? "#ff5a5f" : m.peak > -12.5 ? "#ffb020" : "#e6e9ee";

    const badge = $("signalBadge");
    if (m.clipped) {
      badge.textContent = "CLIPPING!";
      badge.className = "signal-badge clip";
    } else {
      badge.textContent = "SIGNAL OK";
      badge.className = "signal-badge ok";
    }

    // Record 60-second Short-Term LUFS history (~5 samples/sec)
    if (now - lastGraphSampleTs >= 200) {
      lastGraphSampleTs = now;
      lufsHistory.push({ t: now, st: m.lufs_st });
      const cutoff = now - 60000;
      while (lufsHistory.length > 0 && lufsHistory[0].t < cutoff) {
        lufsHistory.shift();
      }
    }
  };

  // Exact 24-segment dB -> lit count matching:
  // Seg 1..17  (-60.0 .. -12.5 dB): Green
  // Seg 18..21 (-12.5 .. -4.5 dB):  Orange
  // Seg 22..24 (-4.5  .. 0.0 dB):   Red
  function dbToSegmentCount(db) {
    if (db <= -60.0) return 0;
    if (db <= -12.5) return Math.round(((db + 60.0) / 47.5) * 17);
    if (db <= -4.5) return 17 + Math.round(((db + 12.5) / 8.0) * 4);
    return Math.min(24, 21 + Math.round(((db + 4.5) / 4.5) * 3));
  }

  function drawSegmentedMeter(canvas, db, peakDb) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 14;
    const cssH = canvas.clientHeight || 200;
    const needW = Math.round(cssW * dpr);
    const needH = Math.round(cssH * dpr);
    if (canvas.width !== needW || canvas.height !== needH) {
      canvas.width = needW;
      canvas.height = needH;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const segments = 24;
    const gap = 2;
    const pad = 2;
    const segH = Math.max(2, (cssH - pad * 2 - gap * (segments - 1)) / segments);
    const lit = dbToSegmentCount(db);
    const peakIdx = Math.min(segments - 1, dbToSegmentCount(peakDb) - 1);

    for (let i = 0; i < segments; i++) {
      const y = cssH - pad - (i + 1) * segH - i * gap;
      let litColor = "#3ddc97";       // Seg 1..17  (i=0..16):  -60..-12.5 dB (Green)
      if (i >= 21) litColor = "#ff5a5f";      // Seg 22..24 (i=21..23): -4.5..0.0 dB (Red)
      else if (i >= 17) litColor = "#ffb020"; // Seg 18..21 (i=17..20): -12.5..-4.5 dB (Orange)

      if (i < lit) {
        ctx.fillStyle = litColor;
      } else if (i === peakIdx && peakDb > -59.0) {
        ctx.fillStyle = litColor;
      } else {
        ctx.fillStyle = "#1a202a";
      }
      ctx.fillRect(2, y, cssW - 4, segH);
    }
  }

  function drawLufsGraph(now) {
    const canvas = $("lufsGraph");
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 348;
    const cssH = canvas.clientHeight || 82;
    const needW = Math.round(cssW * dpr);
    const needH = Math.round(cssH * dpr);
    if (canvas.width !== needW || canvas.height !== needH) {
      canvas.width = needW;
      canvas.height = needH;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const padL = 26;
    const padR = 6;
    const padT = 8;
    const padB = 14;
    const w = cssW - padL - padR;
    const h = cssH - padT - padB;
    const minLufs = -50;
    const maxLufs = -4;

    const yFor = (val) => padT + (1 - clamp((val - minLufs) / (maxLufs - minLufs), 0, 1)) * h;

    // Reference lines: -14 LUFS (Streaming) & -23 LUFS (Broadcast)
    ctx.font = '9px "JetBrains Mono", monospace';
    ctx.textBaseline = "middle";
    [
      { db: -14, color: "rgba(61, 220, 151, 0.35)", txt: "-14" },
      { db: -23, color: "rgba(56, 214, 232, 0.25)", txt: "-23" },
      { db: -36, color: "rgba(139, 147, 161, 0.14)", txt: "-36" },
    ].forEach((ref) => {
      const y = yFor(ref.db);
      ctx.strokeStyle = ref.color;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + w, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#6f7887";
      ctx.fillText(ref.txt, 3, y);
    });

    // Time axis labels
    ctx.fillStyle = "#6f7887";
    ctx.textBaseline = "bottom";
    ctx.fillText("-60s", padL, cssH - 1);
    ctx.fillText("-30s", padL + w * 0.5 - 10, cssH - 1);
    ctx.fillText("0s", padL + w - 12, cssH - 1);

    if (lufsHistory.length < 2) return;

    const grad = ctx.createLinearGradient(0, padT, 0, padT + h);
    grad.addColorStop(0, "rgba(61, 220, 151, 0.38)");
    grad.addColorStop(1, "rgba(61, 220, 151, 0.02)");

    ctx.beginPath();
    lufsHistory.forEach((pt, idx) => {
      const x = padL + clamp((pt.t - (now - 60000)) / 60000, 0, 1) * w;
      const y = yFor(pt.st);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });

    // Stroke curve
    ctx.strokeStyle = "#3ddc97";
    ctx.lineWidth = 1.6;
    ctx.stroke();

    // Fill area under curve
    const lastPt = lufsHistory[lufsHistory.length - 1];
    const firstPt = lufsHistory[0];
    ctx.lineTo(padL + clamp((lastPt.t - (now - 60000)) / 60000, 0, 1) * w, padT + h);
    ctx.lineTo(padL + clamp((firstPt.t - (now - 60000)) / 60000, 0, 1) * w, padT + h);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
  }

  function stepMeterState(m, now) {
    if (m.targetDb >= m.displayDb) {
      m.displayDb = m.targetDb;
    } else {
      m.displayDb = Math.max(m.targetDb, m.displayDb - 1.35);
    }
    if (now >= m.peakHoldUntil && m.peakHoldDb > m.displayDb) {
      m.peakHoldDb = Math.max(m.displayDb, m.peakHoldDb - 0.9);
    }
    drawSegmentedMeter(m.canvas, m.displayDb, m.peakHoldDb);
  }

  function meterAnimLoop() {
    const now = performance.now();
    for (let i = 0; i < 4; i++) stepMeterState(busMeters[i], now);
    stepMeterState(masterMeterL, now);
    stepMeterState(masterMeterR, now);
    drawLufsGraph(now);
    requestAnimationFrame(meterAnimLoop);
  }

  // ---------------- Bind Global Header & Popover Events ----------------
  function bindGlobalEvents() {
    $("selPrimaryOut").addEventListener("change", async (e) => {
      renderState(await window.pywebview.api.set_primary_output(e.target.value));
    });

    $("selPreset").addEventListener("change", async (e) => {
      renderState(await window.pywebview.api.set_preset(e.target.value));
    });

    $("btnSavePreset").addEventListener("click", async () => {
      const name = window.prompt("Nama preset baru:");
      if (!name || !name.trim()) return;
      renderState(await window.pywebview.api.save_custom_preset(name.trim()));
    });

    $("btnDelPreset").addEventListener("click", async () => {
      if (!state) return;
      renderState(await window.pywebview.api.delete_custom_preset(state.preset));
    });

    $("btnLink12").addEventListener("click", async () => {
      if (!state) return;
      renderState(await window.pywebview.api.set_link("12", !state.link_12));
    });
    $("btnLink34").addEventListener("click", async () => {
      if (!state) return;
      renderState(await window.pywebview.api.set_link("34", !state.link_34));
    });
    $("btnAutoStart").addEventListener("click", async () => {
      if (!state) return;
      renderState(await window.pywebview.api.set_autostart(!state.autostart));
    });

    // Close Modal (Hide to Tray vs Full Close)
    const closeModal = () => $("closeModal").classList.add("hidden");
    window.__showCloseDialog = () => {
      closePopover();
      $("closeModal").classList.remove("hidden");
      $("btnModalHide").focus();
    };
    $("btnModalHide").addEventListener("click", async () => {
      closeModal();
      await window.pywebview.api.hide_to_tray();
    });
    $("btnModalExit").addEventListener("click", async () => {
      closeModal();
      await window.pywebview.api.full_close();
    });
    $("btnModalCancel").addEventListener("click", closeModal);
    $("closeModal").addEventListener("click", (e) => {
      if (e.target === $("closeModal")) closeModal();
    });

    $("btnRec").addEventListener("click", async () => {
      if (!state) return;
      const res = state.recording
        ? await window.pywebview.api.stop_rec()
        : await window.pywebview.api.start_rec();
      if (res && !res.cancelled) renderState(res);
    });

    $("btnPower").addEventListener("click", async () => {
      if (!state) return;
      const res = state.running
        ? await window.pywebview.api.stop_mixer()
        : await window.pywebview.api.start_mixer();
      renderState(res);
    });

    const doRefresh = async () => {
      const res = await window.pywebview.api.refresh_devices();
      if (res && res.ok) {
        devices = res.devices;
        renderState(res.state);
      } else if (res && res.error) {
        showError(res.error);
      }
    };
    $("btnRefresh").addEventListener("click", doRefresh);
    $("btnErrorRefresh").addEventListener("click", doRefresh);

    // Popover controls
    $("popClose").addEventListener("click", closePopover);
    document.addEventListener("click", (e) => {
      const pop = $("routingPopover");
      if (!pop.classList.contains("hidden") && !pop.contains(e.target)) {
        closePopover();
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closePopover();
        $("closeModal").classList.add("hidden");
      }
    });

    $("popSendTo").addEventListener("change", async (e) => {
      if (activePopBus === null) return;
      renderState(await window.pywebview.api.set_output(activePopBus, e.target.value));
    });
    $("popSourceMode").addEventListener("change", async (e) => {
      if (activePopBus === null) return;
      updatePopoverSourceBoxes(e.target.value);
      renderState(await window.pywebview.api.set_source_mode(activePopBus, e.target.value));
    });
    $("popCapDev").addEventListener("change", async (e) => {
      if (activePopBus === null) return;
      renderState(await window.pywebview.api.set_input(activePopBus, e.target.value));
    });
    $("popTapMode").addEventListener("change", async (e) => {
      if (activePopBus === null) return;
      renderState(await window.pywebview.api.set_tap(activePopBus, e.target.value));
    });
    $("popToneType").addEventListener("change", async (e) => {
      if (activePopBus === null) return;
      renderState(await window.pywebview.api.set_tone_type(activePopBus, e.target.value));
    });
    $("popLoadFile").addEventListener("click", async () => {
      if (activePopBus === null) return;
      const res = await window.pywebview.api.load_audio_file(activePopBus);
      if (res && !res.cancelled) {
        renderState(res);
        if (state) $("popFileName").textContent = state.channels[activePopBus].file_name || "Loaded";
      }
    });
    $("popFilterMode").addEventListener("change", async (e) => {
      if (activePopBus === null) return;
      renderState(await window.pywebview.api.set_mode(activePopBus, e.target.value));
    });
  }

  // ---------------- Boot ----------------
  async function init() {
    buildBusStrips();
    initMainOutPanel();
    bindGlobalEvents();
    requestAnimationFrame(meterAnimLoop);

    try {
      devices = await window.pywebview.api.get_devices();
      const st = await window.pywebview.api.get_state();
      renderState(st);
    } catch (err) {
      showError(`Gagal menghubungkan ke Python bridge: ${err}`);
    }
  }

  if (window.pywebview) init();
  else window.addEventListener("pywebviewready", init);
})();
