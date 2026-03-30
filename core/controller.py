"""
core/controller.py — OnionController

Single object that owns ALL adaptive scheduling decisions for the watcher:

  ┌─────────────────────────────────────────────┐
  │            OnionController                   │
  │  ┌──────────────┐  ┌──────────────────────┐ │
  │  │  User state  │  │  System CPU pressure │ │
  │  │  activity    │  │  EMA + 4 tiers       │ │
  │  │  presence    │  │  hysteresis          │ │
  │  └──────────────┘  └──────────────────────┘ │
  │                                             │
  │  get_mult()      → interval multiplier      │
  │  feature_enabled → which features run       │
  │  to_dict()       → status for web UI        │
  └─────────────────────────────────────────────┘

CPU pressure tiers (applied to other_cpu = sys_cpu − watcher_cpu):
  0 normal   < threshold[0]  → mult = activity-based (0.4–1.0)
  1 light    threshold[0–1]  → mult at least 2×, camera off
  2 heavy    threshold[1–2]  → mult at least 5×, vision/audio/camera/reader off
  3 critical ≥ threshold[2]  → mult at least 20×, only checkpoint/OCR bare minimum

Hysteresis: upgrade instantly, downgrade only after 3 consecutive below-threshold samples.
"""
from __future__ import annotations
import logging
import time

log = logging.getLogger("onion.controller")

# Default CPU pressure thresholds (% other_cpu = sys - watcher)
_DEFAULT_THRESHOLDS = [50, 70, 85]

# Interval multiplier per pressure tier
_PRESSURE_MULT = {0: 1.0, 1: 2.0, 2: 5.0, 3: 20.0}

# Feature gates: {feature: max_pressure_allowed}
_FEATURE_GATES = {
    "vision":  1,   # off at pressure >= 2
    "audio":   1,   # off at pressure >= 2
    "camera":  0,   # off at pressure >= 1
    "reader":  1,   # off at pressure >= 2
    "media":   1,   # off at pressure >= 2 (media_analysis_loop)
    "ocr":     2,   # off at pressure >= 3 (critical only)
}


class OnionController:
    """
    Stateful controller — instantiate once in WatcherDaemon, call update() every 10s.
    """

    def __init__(self, thresholds: list[int] | None = None):
        self._thresholds: list[int] = list(thresholds or _DEFAULT_THRESHOLDS)

        # ── User activity / presence ──────────────────────────────────────────
        self._activity_score: float = 0.0
        self._user_present: bool = True
        self._static_capture_count: int = 0

        # ── System CPU pressure ───────────────────────────────────────────────
        self._sys_cpu_ema: float = 0.0       # smoothed other_cpu (EMA α=0.25)
        self._other_cpu: float = 0.0         # last raw other_cpu sample
        self._cpu_pressure: int = 0          # 0–3
        self._down_counter: int = 0          # consecutive below-threshold samples

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def activity_score(self) -> float:
        return self._activity_score

    @property
    def user_present(self) -> bool:
        return self._user_present

    @property
    def cpu_pressure(self) -> int:
        return self._cpu_pressure

    @property
    def sys_cpu_ema(self) -> float:
        return self._sys_cpu_ema

    # ── Core update (called every 10s by _cpu_adjust_loop) ───────────────────

    def update(self, sys_cpu: float, watcher_cpu: float) -> None:
        """Feed latest CPU samples. Updates EMA, pressure tier, decays activity."""
        # Compute "other" CPU (what other apps are consuming)
        self._other_cpu = max(0.0, sys_cpu - watcher_cpu)

        # Exponential moving average (α=0.25 → ~40s to reach new steady state)
        self._sys_cpu_ema = 0.25 * self._other_cpu + 0.75 * self._sys_cpu_ema

        # Determine target pressure tier from EMA
        ema = self._sys_cpu_ema
        t = self._thresholds   # [light, heavy, critical] thresholds
        if ema >= t[2]:
            target = 3
        elif ema >= t[1]:
            target = 2
        elif ema >= t[0]:
            target = 1
        else:
            target = 0

        # Hysteresis: upgrade immediately, downgrade only after 3 consecutive samples
        if target > self._cpu_pressure:
            if self._cpu_pressure != target:
                log.info("CPU pressure: %d → %d (other_cpu EMA=%.1f%%)",
                         self._cpu_pressure, target, ema)
            self._cpu_pressure = target
            self._down_counter = 0
        elif target < self._cpu_pressure:
            self._down_counter += 1
            if self._down_counter >= 3:
                log.info("CPU pressure: %d → %d (EMA=%.1f%%, stable for 3 samples)",
                         self._cpu_pressure, target, ema)
                self._cpu_pressure = target
                self._down_counter = 0
        else:
            self._down_counter = 0

        # Decay activity score (×0.965 per 10s ≈ ×0.7 per 30s)
        self._activity_score *= 0.965

    # ── Activity / presence ───────────────────────────────────────────────────

    def bump_activity(self, amount: float) -> None:
        """Raise activity score (capped at 1.0). Significant bumps restore presence."""
        self._activity_score = min(1.0, self._activity_score + amount)
        if amount >= 0.2:
            self._user_present = True
            self._static_capture_count = 0

    def update_presence(self, idle_s: float, screen_score: float) -> None:
        """Update user-present state from HID idle time + screen change score.
        Call after each OCR capture."""
        if idle_s < 30 or screen_score > 0.05:
            self._user_present = True
            self._static_capture_count = 0
        else:
            self._static_capture_count += 1
            if self._static_capture_count >= 3 and self._user_present:
                log.info("User absent: idle=%.0fs, static captures=%d",
                         idle_s, self._static_capture_count)
                self._user_present = False

    # ── Scheduling outputs ────────────────────────────────────────────────────

    def get_mult(self, idle_s: float, presence_confirmed: bool = True) -> float:
        """Return interval multiplier combining user idle/activity and CPU pressure.

        idle (HID>120s or camera absent)  → at least 10×
        cpu_pressure                       → {0:1×, 1:2×, 2:5×, 3:20×}
        activity score                     → 0.4× (busy) – 1.0× (quiet)
        Final = max(all three contributors)
        """
        # Idle / absent override
        if idle_s > 120 or not presence_confirmed:
            idle_mult = 10.0
        else:
            idle_mult = max(0.4, 1.0 - self._activity_score * 0.6)

        cpu_mult = _PRESSURE_MULT[self._cpu_pressure]
        return max(idle_mult, cpu_mult)

    def feature_enabled(self, feature: str) -> bool:
        """Return whether a feature should run given current CPU pressure.

        feature ∈ 'vision' | 'audio' | 'camera' | 'reader' | 'media' | 'ocr'
        """
        max_pressure = _FEATURE_GATES.get(feature, 3)
        return self._cpu_pressure <= max_pressure

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_dict(self, pid: int = 0, watcher_cpu: float = 0.0,
                rss_kb: int = 0) -> dict:
        """Serialise full controller state for resource_status.json / web UI."""
        _PRESSURE_LABELS = {0: "normal", 1: "light", 2: "heavy", 3: "critical"}
        return {
            "pid": pid,
            "cpu_pct": round(watcher_cpu, 1),
            "rss_kb": rss_kb,
            "activity_score": round(self._activity_score, 3),
            "mult": round(self.get_mult(0.0), 2),   # approx (no idle_s context)
            "sys_cpu_ema": round(self._sys_cpu_ema, 1),
            "other_cpu": round(self._other_cpu, 1),
            "cpu_pressure": self._cpu_pressure,
            "cpu_pressure_label": _PRESSURE_LABELS[self._cpu_pressure],
            "user_present": self._user_present,
            "features": {f: self.feature_enabled(f) for f in _FEATURE_GATES},
        }
