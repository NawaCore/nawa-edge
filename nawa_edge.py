#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NAWA EDGE — The Sovereign Edge Anomaly Sentinel
=================================================
"Zero data leaves this machine." · «لا تغادر أي بيانات هذا الجهاز»

A single-file, ZERO-dependency (pure Python 3.9+ stdlib) anomaly sentinel for
critical infrastructure — desalination plants, district cooling, solar farms,
substations, airports, data centers. Built for the UAE edge reality: 48 °C
afternoons, air-gapped OT networks, and PDPL-aligned data-sovereignty by design.

Core invention: the MIZAN-SARAB algorithm
  MIZAN  (ميزان, "the balance")  — context-zoned streaming quantile ribbons
  SARAB  (سراب, "the mirage")    — desert-noise mirage suppression gate

Sovereign Seal: every report is sealed into a local SHA-256 hash chain
("Silsila", سلسلة) with dual UTC / Gulf timestamps — cryptographic, offline,
tamper-evident integrity evidence of what was detected, when, and by which engine version.

Usage (one command):
    python nawa_edge.py                     # full demo: data + detection + dashboard
    python nawa_edge.py detect plant.csv    # run on your own CSV
    python nawa_edge.py verify out/nawa_edge_seals.jsonl
    python nawa_edge.py demo --csv-only     # just write the sample dataset

License: MIT — free forever. Built by Nawa Advanced Technologies, Masdar City,
Abu Dhabi (licence MC 14734) · nawacore.ai · "Building the Core of Intelligent
Futures" · «نبني نواة المستقبل الذكي»
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import http.server
import json
import math
import os
import platform
import random
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

EDGE_VERSION = "1.0.0"
ALGO_NAME = "MIZAN-SARAB"
ALGO_VERSION = "1.0"
BRAND = "Nawa Advanced Technologies · nawacore.ai"
GULF_TZ = timezone(timedelta(hours=4), name="GST")

# ============================================================================
# SECTION 1 — THE MIZAN-SARAB CORE
# ----------------------------------------------------------------------------
# MIZAN (Balance): every sensor gets a small book of "thermal context zones"
# (e.g. night-cool / morning / hot / extreme-heat). Inside each zone we track
# a streaming quantile RIBBON (q05, q50, q95) with Robbins–Monro updates and
# exponential forgetting — O(1) memory, O(1) CPU per reading, no training
# phase, no stored raw data (PDPL-aligned by design: only sufficient statistics live
# in memory).
#
# Young zones "borrow" their baseline from the sensor's global ribbon and
# earn independence as they mature — so the detector is useful within hours,
# not weeks (cold-start borrowing).
#
# SARAB (Mirage): the desert lies. Dust hits, thermal transients and sensor
# glitches produce single-tick spikes that naive detectors alarm on. The
# Sarab gate requires BOTH a persistent exceedance memory p (EWMA) above
# threshold AND a minimum run-length of consecutive out-of-ribbon readings —
# one-tick mirages are silently absorbed, sustained faults flash red fast.
#
# Integrity gate: while a sensor is in exceedance, its baselines learn at
# 5 % speed — the model refuses to "learn the fault as the new normal".
# ============================================================================


@dataclass
class MizanParams:
    """Tunable parameters of the MIZAN-SARAB detector (sane UAE defaults)."""
    lr: float = 0.08              # Robbins–Monro quantile step (forgetting rate)
    scale_beta: float = 0.05      # EW update rate of the robust scale estimate
    alpha: float = 0.90           # persistence memory (SARAB EWMA decay)
    e_cap: float = 5.0            # cap on instantaneous exceedance
    tau: float = 0.45             # alarm threshold on persistence score p
    min_run: int = 3              # SARAB gate: consecutive exceed ticks required
    warmup: int = 1440            # readings before alarms may fire — with
                                  # 1-minute data this is one full diurnal
                                  # cycle: day 1 is for listening, not alarming
    zone_mature: int = 120        # readings for a zone to earn full independence
    band_floor_frac: float = 0.05 # ribbon width floor, as fraction of global width
    anomaly_lr_frac: float = 0.05 # baseline learning speed while in exceedance
    zone_edges: Tuple[float, ...] = (33.0, 39.0, 45.0)  # °C context bins
    phase_hours: int = 3          # diurnal phase bucket width (hours)
    soft_ctx_margin: float = 1.5  # °C: soft-blend width across thermal edges
    soft_phase_frac: float = 0.15 # phase fraction blended with the neighbor
    young_boost: float = 3.0      # learning-rate boost while a zone is young
    conf_gate: float = 0.6        # zone confidence required before alarming
    event_gap: int = 30           # ticks: merge events separated by <= gap
    event_min_len: int = 3        # ticks: minimum event length to report


class QuantileRibbon:
    """Streaming q05/q50/q95 tracker with exponential forgetting.

    Robbins–Monro with a constant, scale-proportional step: each estimate
    drifts toward its target quantile and forgets the distant past — the
    ribbon breathes with the plant instead of fossilising.
    """

    __slots__ = ("q05", "q50", "q95", "scale", "n", "mass")

    def __init__(self) -> None:
        self.q05: float = 0.0
        self.q50: float = 0.0
        self.q95: float = 0.0
        self.scale: float = 0.0
        self.n: int = 0
        self.mass: float = 0.0    # membership-weighted evidence (maturity)

    def update(self, x: float, lr: float, beta: float, w: float = 1.0) -> None:
        """Fold one reading into the ribbon (w = membership weight)."""
        self.mass += w
        if self.n == 0:
            self.q05 = self.q50 = self.q95 = x
            self.scale = max(self.scale, 1e-9)  # keep a pre-seeded scale
            self.n = 1
            return
        self.scale = (1.0 - beta) * self.scale + beta * w * abs(x - self.q50)
        s = max(self.scale, 1e-9)
        self.q05 += lr * s * (0.05 - (1.0 if x < self.q05 else 0.0))
        self.q50 += lr * s * (0.50 - (1.0 if x < self.q50 else 0.0))
        self.q95 += lr * s * (0.95 - (1.0 if x < self.q95 else 0.0))
        # keep the ribbon ordered (numerical safety under fast drift)
        self.q50 = min(max(self.q50, self.q05), max(self.q95, self.q05))
        self.q95 = max(self.q95, self.q50)
        self.q05 = min(self.q05, self.q50)
        self.n += 1

    def width(self) -> float:
        return max(self.q95 - self.q05, 1e-9)


@dataclass
class Tick:
    """Per-reading detector output (kept for dashboards & audit)."""
    lo: float          # blended expected-band floor
    hi: float          # blended expected-band ceiling
    e: float           # instantaneous exceedance (band widths outside)
    p: float           # SARAB persistence score
    flag: bool         # anomaly state after the mirage gate
    zone: int          # thermal context zone index


class MizanSarabDetector:
    """One sensor's MIZAN-SARAB state machine. O(zones) memory per sensor.

    A zone = (thermal bin × diurnal phase): "a 46 °C mid-afternoon" is a
    different normal from "a 46 °C morning ramp". Memberships are SOFT —
    readings near a zone edge belong partially to both neighbors, which
    kills boundary flicker (a classic false-alarm source in binned models).
    """

    def __init__(self, params: MizanParams) -> None:
        self.pr = params
        self.global_ribbon = QuantileRibbon()
        self.zones: Dict[Tuple[int, int], QuantileRibbon] = {}
        self.p: float = 0.0
        self.run: int = 0
        self.n: int = 0

    def _thermal_members(self, ctx: Optional[float]) -> List[Tuple[int, float]]:
        """Fuzzy membership over thermal bins (≤ 2 bins, weights sum to 1)."""
        if ctx is None or ctx != ctx:  # None or NaN → single bin
            return [(0, 1.0)]
        edges, m = self.pr.zone_edges, self.pr.soft_ctx_margin
        b = sum(1 for e in edges if ctx >= e)
        for j, e in enumerate(edges):
            if abs(ctx - e) < m:  # inside the soft band around edge j
                t = (ctx - (e - m)) / (2.0 * m)   # 0 → all lower, 1 → all upper
                return [(j, 1.0 - t), (j + 1, t)]
        return [(b, 1.0)]

    def _phase_members(self, hod: Optional[float]) -> List[Tuple[int, float]]:
        """Fuzzy membership over diurnal phases (≤ 2, weights sum to 1)."""
        if hod is None:
            return [(0, 1.0)]
        L = max(1, self.pr.phase_hours)
        n_ph = max(1, round(24 / L))
        pos = (hod % 24.0) / L
        ph = int(pos) % n_ph
        u, m = pos - int(pos), self.pr.soft_phase_frac
        if u < m:
            w = 0.5 * (1.0 - u / m)
            return [(ph, 1.0 - w), ((ph - 1) % n_ph, w)]
        if u > 1.0 - m:
            w = 0.5 * (1.0 - (1.0 - u) / m)
            return [(ph, 1.0 - w), ((ph + 1) % n_ph, w)]
        return [(ph, 1.0)]

    def step(self, x: float, ctx: Optional[float], hod: Optional[float] = None) -> Tick:
        """Score one reading, then let the baselines learn from it."""
        pr = self.pr
        g = self.global_ribbon
        members: List[Tuple[Tuple[int, int], float]] = [
            ((tb, ph), wt * wp)
            for tb, wt in self._thermal_members(ctx)
            for ph, wp in self._phase_members(hod)
        ]

        # --- MIZAN: soft-blended context band (young zones borrow global) ---
        lo = hi = conf = 0.0
        if g.n == 0:
            lo = hi = x
        else:
            for zid, wm in members:
                zr = self.zones.get(zid)
                wz = min(1.0, zr.mass / float(pr.zone_mature)) if zr and zr.n else 0.0
                zlo = wz * zr.q05 + (1.0 - wz) * g.q05 if zr and zr.n else g.q05
                zhi = wz * zr.q95 + (1.0 - wz) * g.q95 if zr and zr.n else g.q95
                lo += wm * zlo
                hi += wm * zhi
                conf += wm * wz
        floor = pr.band_floor_frac * g.width()
        width = max(hi - lo, floor, 1e-9)

        # --- instantaneous exceedance, in units of band width ---
        if x > hi:
            e = min((x - hi) / width, pr.e_cap)
        elif x < lo:
            e = min((lo - x) / width, pr.e_cap)
        else:
            e = 0.0

        # --- SARAB: persistence memory + run-length mirage gate.
        # An alarm additionally requires zone CONFIDENCE: a state of the
        # world Nawa Edge has never lived through (first-ever 48 °C afternoon)
        # is learned silently, never alarmed on — day 1 is for listening.
        self.p = pr.alpha * self.p + (1.0 - pr.alpha) * e
        self.run = self.run + 1 if e > 0.0 else 0
        flag = (
            self.n >= pr.warmup
            and conf >= pr.conf_gate
            and self.p > pr.tau
            and e > 0.0
            and self.run >= pr.min_run
        )

        # --- integrity gate: a mature detector refuses to learn the fault
        # as the new normal (baselines crawl at 5% speed during exceedance).
        gate = e > 0.0 and conf >= 0.5 and self.n >= pr.warmup
        slow = pr.anomaly_lr_frac if gate else 1.0
        g.update(x, pr.lr * slow, pr.scale_beta)
        for zid, wm in members:
            zr = self.zones.setdefault(zid, QuantileRibbon())
            if zr.n == 0:
                zr.scale = 0.5 * g.scale  # bootstrap spread from global
            boost = pr.young_boost if zr.mass < pr.zone_mature else 1.0
            # local integrity: a MATURE zone resists readings far outside its
            # own ribbon even when the blended band was diluted by a young
            # neighbor — faults cannot leak into settled baselines.
            local_slow = slow
            if zr.mass >= pr.zone_mature and not (
                zr.q05 - 0.25 * zr.width() <= x <= zr.q95 + 0.25 * zr.width()
            ):
                local_slow = pr.anomaly_lr_frac
            zr.update(x, pr.lr * wm * boost * local_slow, pr.scale_beta, w=wm)
        self.n += 1
        dom_bin = max(self._thermal_members(ctx), key=lambda bw: bw[1])[0]
        return Tick(lo=lo, hi=hi, e=e, p=self.p, flag=flag, zone=dom_bin)


@dataclass
class SensorResult:
    key: str
    ticks: List[Tick]
    events: List[Dict[str, object]] = field(default_factory=list)


def _extract_events(ticks: List[Tick], times: List[str], pr: MizanParams) -> List[Dict[str, object]]:
    """Merge flagged runs (gap-tolerant) into reportable anomaly events."""
    runs: List[List[int]] = []
    for i, t in enumerate(ticks):
        if t.flag:
            if runs and i - runs[-1][1] <= pr.event_gap:
                runs[-1][1] = i
            else:
                runs.append([i, i])
    events: List[Dict[str, object]] = []
    for s, e in runs:
        if e - s + 1 < pr.event_min_len:
            continue
        peak_i = max(range(s, e + 1), key=lambda i: ticks[i].p)
        events.append({
            "start_index": s, "end_index": e,
            "start_time": times[s], "end_time": times[e],
            "duration_ticks": e - s + 1,
            "peak_score": round(ticks[peak_i].p, 4),
            "peak_time": times[peak_i], "peak_index": peak_i,
            "zone_at_peak": ticks[peak_i].zone,
        })
    return events


def _hod_of(ts: str) -> Optional[float]:
    """Fractional hour-of-day from a timestamp string (None if unparseable)."""
    try:
        return int(ts[11:13]) + int(ts[14:16]) / 60.0
    except (ValueError, IndexError):
        try:
            d = datetime.fromisoformat(ts.strip())
            return d.hour + d.minute / 60.0
        except ValueError:
            return None


def run_detection(
    times: List[str],
    context: Optional[List[float]],
    sensors: Dict[str, List[float]],
    params: Optional[MizanParams] = None,
) -> Tuple[Dict[str, SensorResult], List[float], MizanParams]:
    """Run MIZAN-SARAB over aligned series. Returns per-sensor results and
    the fused fleet MIZAN score M_t = 1 − Π(1 − min(1, p_i / 2τ))."""
    pr = params or MizanParams()
    dets = {k: MizanSarabDetector(pr) for k in sensors}
    results = {k: SensorResult(key=k, ticks=[]) for k in sensors}
    mizan: List[float] = []
    n = len(times)
    hods = [_hod_of(ts) for ts in times]
    for i in range(n):
        ctx = context[i] if context is not None else None
        prod = 1.0
        for k, series in sensors.items():
            tick = dets[k].step(series[i], ctx, hods[i])
            results[k].ticks.append(tick)
            if i >= pr.warmup:  # during warmup the fleet listens silently
                prod *= 1.0 - min(1.0, tick.p / (2.0 * pr.tau))
        mizan.append(round(1.0 - prod, 4))
    for k in results:
        results[k].events = _extract_events(results[k].ticks, times, pr)
    return results, mizan, pr


# ============================================================================
# SECTION 2 — THE SOVEREIGN SEAL (Silsila · سلسلة hash chain)
# ----------------------------------------------------------------------------
# Every report is canonically serialised, SHA-256 hashed, and chained to the
# previous seal — a local, offline, append-only chain of custody. Anyone can
# re-verify with `python nawa_edge.py verify seals.jsonl` — no cloud, no vendor.
# ============================================================================


def _canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def host_fingerprint() -> str:
    """Pseudonymous machine fingerprint — proves locality, reveals nothing."""
    raw = f"{platform.node()}|{platform.system()}|{os.environ.get('USER', os.environ.get('USERNAME', ''))}"
    return _sha256(raw.encode("utf-8"))[:16]


def make_seal(payload: Dict[str, object], prev_seal: str) -> Dict[str, str]:
    """Seal a report payload into the Silsila chain."""
    now_utc = datetime.now(timezone.utc)
    body: Dict[str, str] = {
        "nawa_edge_version": EDGE_VERSION,
        "algorithm": f"{ALGO_NAME} v{ALGO_VERSION}",
        "created_utc": now_utc.isoformat(timespec="seconds"),
        "created_gulf": now_utc.astimezone(GULF_TZ).isoformat(timespec="seconds"),
        "host_fingerprint": host_fingerprint(),
        "payload_sha256": _sha256(_canonical(payload)),
        "prev_seal": prev_seal,
    }
    body["seal_sha256"] = _sha256(_canonical(body))
    return body


def append_seal(chain_path: str, seal: Dict[str, str]) -> None:
    with open(chain_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(seal, ensure_ascii=False) + "\n")


def verify_chain(chain_path: str, report_path: Optional[str] = None) -> Tuple[bool, List[str]]:
    """Re-verify every link of a Silsila chain (and optionally one report)."""
    msgs: List[str] = []
    ok = True
    prev = "GENESIS"
    with open(chain_path, "r", encoding="utf-8") as f:
        seals = [json.loads(line) for line in f if line.strip()]
    for i, seal in enumerate(seals):
        body = {k: v for k, v in seal.items() if k != "seal_sha256"}
        recomputed = _sha256(_canonical(body))
        if recomputed != seal.get("seal_sha256"):
            ok = False
            msgs.append(f"link {i}: seal hash MISMATCH — chain has been altered")
        elif seal.get("prev_seal") != prev:
            ok = False
            msgs.append(f"link {i}: broken back-link (expected {prev[:12]}…)")
        else:
            msgs.append(f"link {i}: seal {seal['seal_sha256'][:16]}… OK ({seal['created_gulf']})")
        prev = seal.get("seal_sha256", "")
    if report_path:
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        payload = report.get("payload", {})
        want = report.get("seal", {}).get("payload_sha256", "")
        got = _sha256(_canonical(payload))
        if want == got:
            msgs.append(f"report payload hash OK ({got[:16]}…)")
        else:
            ok = False
            msgs.append("report payload hash MISMATCH — report has been altered")
    return ok, msgs


# ============================================================================
# SECTION 3 — TinyPDF: a dependency-free PDF writer (Base-14 fonts)
# ----------------------------------------------------------------------------
# ~100 lines that write a valid PDF 1.4 by hand, so even the sealed PDF
# export needs no third-party library on an air-gapped machine.
# ============================================================================


class TinyPDF:
    """Minimal multi-page PDF writer (Helvetica / Helvetica-Bold / Courier)."""

    W, H = 595.28, 841.89  # A4 portrait, points

    def __init__(self) -> None:
        self.pages: List[List[bytes]] = []

    def add_page(self) -> None:
        self.pages.append([])

    _TRANSLIT = str.maketrans({"—": "-", "–": "-", "·": "-", "→": ">",
                               "’": "'", "‘": "'", "“": '"', "”": '"', "…": "..."})

    @classmethod
    def _esc(cls, text: str) -> str:
        text = text.translate(cls._TRANSLIT).encode("latin-1", "replace").decode("latin-1")
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    def text(self, x: float, y: float, s: str, size: float = 10,
             font: str = "F1", rgb: Tuple[float, float, float] = (0, 0, 0)) -> None:
        r, g, b = rgb
        op = f"BT /{font} {size:.1f} Tf {r:.3f} {g:.3f} {b:.3f} rg 1 0 0 1 {x:.2f} {y:.2f} Tm ({self._esc(s)}) Tj ET"
        self.pages[-1].append(op.encode("latin-1"))

    def rect(self, x: float, y: float, w: float, h: float,
             rgb: Tuple[float, float, float]) -> None:
        r, g, b = rgb
        self.pages[-1].append(f"{r:.3f} {g:.3f} {b:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f".encode("latin-1"))

    def hline(self, x1: float, x2: float, y: float,
              rgb: Tuple[float, float, float], width: float = 0.7) -> None:
        r, g, b = rgb
        self.pages[-1].append(f"{r:.3f} {g:.3f} {b:.3f} RG {width:.2f} w {x1:.2f} {y:.2f} m {x2:.2f} {y:.2f} l S".encode("latin-1"))

    def build(self) -> bytes:
        n_pages = len(self.pages)
        fonts = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Courier"}
        objs: List[bytes] = []
        kids = " ".join(f"{6 + 2 * i} 0 R" for i in range(n_pages))
        objs.append(f"<< /Type /Catalog /Pages 2 0 R >>".encode())
        objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
        for name, base in fonts.items():  # objects 3, 4, 5
            objs.append(f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} >>".encode())
        res = "<< /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >>"
        for i, ops in enumerate(self.pages):
            page_id, content_id = 6 + 2 * i, 7 + 2 * i
            objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.W} {self.H}] "
                         f"/Resources {res} /Contents {content_id} 0 R >>").encode())
            stream = b"\n".join(ops)
            objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        out = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for i, body in enumerate(objs, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref_at = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref_at}\n%%EOF").encode()
        return bytes(out)


# ============================================================================
# SECTION 4 — SYNTHETIC UAE DEMO PLANT (48 h @ 1-minute resolution)
# ----------------------------------------------------------------------------
# Day 1: clean operation under a brutal 30→48 °C diurnal cycle (Nawa Edge learns).
# Day 2: three real faults + one single-tick dust "mirage" (Nawa Edge detects,
#        and the mirage is silently suppressed by the SARAB gate).
# ============================================================================

DEMO_SENSORS = ["pump_vibration_mm_s", "hvac_compressor_kw", "solar_inverter_kw"]

DEMO_LABELS: Dict[str, Dict[str, str]] = {
    "ambient_c": {"en": "Ambient Temperature (°C)",
                  "ar": "درجة الحرارة المحيطة (°م)"},
    "pump_vibration_mm_s": {"en": "Desalination HP Pump — Vibration (mm/s)",
                            "ar": "مضخة التحلية عالية الضغط — الاهتزاز (مم/ث)"},
    "hvac_compressor_kw": {"en": "District Cooling HVAC — Compressor Power (kW)",
                           "ar": "تبريد المناطق — قدرة الضاغط (كيلوواط)"},
    "solar_inverter_kw": {"en": "Solar PV Inverter — Output Power (kW)",
                          "ar": "عاكس الطاقة الشمسية — القدرة الخارجة (كيلوواط)"},
}

DEMO_TRUTH = {  # ground-truth fault windows, in hours from start (for the README)
    "pump_vibration_mm_s": (36.0, 40.0),   # bearing degradation, day 2 noon
    "hvac_compressor_kw": (44.0, 47.0),    # refrigerant loss, day 2 evening
    "solar_inverter_kw": (35.0, 37.5),     # soiling / partial shading, day 2
}


def generate_demo(minutes: int = 2880, seed: int = 42) -> Tuple[List[str], List[float], Dict[str, List[float]]]:
    """48 hours of UAE-realistic plant telemetry with injected day-2 faults."""
    rng = random.Random(seed)
    start = (datetime.now(GULF_TZ) - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    times: List[str] = []
    ambient: List[float] = []
    pump: List[float] = []
    hvac: List[float] = []
    solar: List[float] = []
    drift = 0.0
    for t in range(minutes):
        h = t / 60.0                       # hours since start
        hod = h % 24.0                     # hour of day
        times.append((start + timedelta(minutes=t)).strftime("%Y-%m-%d %H:%M"))

        # --- ambient: 30 °C pre-dawn → 48 °C mid-afternoon ---
        drift = 0.98 * drift + rng.gauss(0.0, 0.06)
        amb = 39.0 + 9.0 * math.sin(2.0 * math.pi * (hod - 9.0) / 24.0) + drift + rng.gauss(0.0, 0.25)
        ambient.append(round(amb, 2))

        # --- desalination HP pump vibration (mm/s RMS) ---
        v = 2.2 + 0.020 * max(0.0, amb - 35.0) + rng.gauss(0.0, 0.10)
        if 36.0 <= h <= 40.0:              # FAULT: bearing wear ramps up
            v += 0.9 * min(1.0, (h - 36.0) / 2.0) + rng.gauss(0.0, 0.12)
        if t == 1800:                      # MIRAGE: single-tick dust/EMI spike
            v += 3.0
        pump.append(round(max(v, 0.0), 3))

        # --- district cooling compressor power (kW) ---
        k = 8.0 + 0.35 * max(0.0, amb - 30.0) + rng.gauss(0.0, 0.30)
        if 44.0 <= h <= 47.0:              # FAULT: refrigerant loss → overwork
            k *= 1.28 + 0.04 * math.sin((h - 44.0) * 2.0)
        hvac.append(round(max(k, 0.0), 3))

        # --- solar PV inverter output (kW) ---
        sun = math.sin(math.pi * (hod - 6.25) / 13.0) if 6.25 <= hod <= 19.25 else 0.0
        derate = 1.0 - 0.0035 * max(0.0, amb - 25.0)   # heat derating
        p = 96.0 * max(sun, 0.0) ** 1.2 * derate + rng.gauss(0.0, 0.8)
        if 35.0 <= h <= 37.5:              # FAULT: soiling / partial shading
            p *= 0.68
        solar.append(round(max(p, 0.0), 3))

    return times, ambient, {"pump_vibration_mm_s": pump, "hvac_compressor_kw": hvac, "solar_inverter_kw": solar}


def write_demo_csv(path: str, times: List[str], ambient: List[float], sensors: Dict[str, List[float]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "ambient_c"] + list(sensors.keys()))
        for i, ts in enumerate(times):
            w.writerow([ts, ambient[i]] + [sensors[k][i] for k in sensors])


def read_csv(path: str, time_col: str, context_col: Optional[str]) -> Tuple[List[str], Optional[List[float]], Dict[str, List[float]]]:
    """Load any tidy CSV: one time column, optional context column, numeric sensors."""
    with open(path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"nawa-edge: {path} is empty")
    cols = list(rows[0].keys())
    if time_col not in cols:
        raise SystemExit(f"nawa-edge: time column '{time_col}' not found (columns: {cols})")
    if context_col is None:
        context_col = next((c for c in cols if "ambient" in c.lower() or "temp" in c.lower()), None)
    times = [r[time_col] for r in rows]
    context = None
    if context_col and context_col in cols:
        context = [float(r[context_col] or "nan") for r in rows]
    sensors: Dict[str, List[float]] = {}
    for c in cols:
        if c in (time_col, context_col):
            continue
        try:
            sensors[c] = [float(r[c]) for r in rows]
        except (TypeError, ValueError):
            continue  # non-numeric column — skip
    if not sensors:
        raise SystemExit("nawa-edge: no numeric sensor columns found")
    return times, context, sensors


# ============================================================================
# SECTION 5 — REPORTS & EXPORTS (CSV · sealed JSON · sealed PDF · audit HTML)
# ============================================================================


def build_report_payload(site: str, times: List[str], results: Dict[str, SensorResult],
                         mizan: List[float], pr: MizanParams) -> Dict[str, object]:
    return {
        "site": site,
        "algorithm": f"{ALGO_NAME} v{ALGO_VERSION}",
        "nawa_edge_version": EDGE_VERSION,
        "period": {"from": times[0], "to": times[-1], "readings": len(times)},
        "parameters": {"tau": pr.tau, "alpha": pr.alpha, "lr": pr.lr,
                       "min_run": pr.min_run, "warmup": pr.warmup,
                       "zone_edges_c": list(pr.zone_edges)},
        "fleet_peak_mizan_score": max(mizan) if mizan else 0.0,
        "sensors": {
            k: {"readings": len(r.ticks),
                "anomaly_events": len(r.events),
                "peak_score": max((t.p for t in r.ticks), default=0.0),
                "events": r.events}
            for k, r in results.items()
        },
    }


def export_events_csv(path: str, results: Dict[str, SensorResult]) -> int:
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sensor", "start_time", "end_time", "duration_min",
                    "peak_score", "peak_time", "thermal_zone_at_peak"])
        for k, r in results.items():
            for ev in r.events:
                w.writerow([k, ev["start_time"], ev["end_time"], ev["duration_ticks"],
                            ev["peak_score"], ev["peak_time"], ev["zone_at_peak"]])
                n += 1
    return n


NAVY = (0.01176, 0.08235, 0.15686)   # #031528 — official Nawa brand navy
GOLD = (0.788, 0.635, 0.153)   # #C9A227
INK = (0.10, 0.10, 0.10)
GRAY = (0.45, 0.45, 0.45)


def export_sealed_pdf(path: str, payload: Dict[str, object], seal: Dict[str, str],
                      labels: Dict[str, Dict[str, str]]) -> None:
    """Sealed anomaly & audit report as a genuine, dependency-free PDF."""
    pdf = TinyPDF()
    W, H = TinyPDF.W, TinyPDF.H
    M = 48.0

    def header() -> float:
        pdf.add_page()
        pdf.rect(0, H - 78, W, 78, NAVY)
        pdf.text(M, H - 40, "NAWA EDGE", 22, "F2", (1, 1, 1))
        pdf.text(M + 148, H - 40, "Sovereign Anomaly & Audit Report", 12, "F1", (0.92, 0.89, 0.80))
        pdf.text(M, H - 60, f"{ALGO_NAME} v{ALGO_VERSION}  ·  100% local — zero data left this machine",
                 8.5, "F1", (0.78, 0.80, 0.85))
        pdf.hline(0, W, H - 80.5, GOLD, 2.0)
        return H - 110

    y = header()
    period = payload["period"]  # type: ignore[index]
    meta = [
        ("Site", str(payload["site"])),
        ("Period", f"{period['from']}  ->  {period['to']}  ({period['readings']} readings)"),  # type: ignore[index]
        ("Generated (Gulf)", seal["created_gulf"]),
        ("Generated (UTC)", seal["created_utc"]),
        ("Engine", f"Nawa Edge v{EDGE_VERSION} - {ALGO_NAME} v{ALGO_VERSION}"),
    ]
    for k, v in meta:
        pdf.text(M, y, k.upper(), 7.5, "F2", GRAY)
        pdf.text(M + 110, y, v, 9.5, "F1", INK)
        y -= 16
    y -= 8
    pdf.text(M, y, "SENSOR SUMMARY", 10, "F2", NAVY)
    pdf.hline(M, W - M, y - 4, GOLD, 1.0)
    y -= 22
    cols = (M, M + 250, M + 340, M + 430)
    for label, xx in zip(("Sensor", "Events", "Peak score", "Status"), cols):
        pdf.text(xx, y, label, 8, "F2", GRAY)
    y -= 14
    sensors: Dict[str, Dict[str, object]] = payload["sensors"]  # type: ignore[assignment]
    for k, s in sensors.items():
        name = labels.get(k, {}).get("en", k)
        n_ev = int(s["anomaly_events"])  # type: ignore[arg-type]
        status = "ATTENTION" if n_ev else "NORMAL"
        pdf.text(cols[0], y, name[:52], 9, "F1", INK)
        pdf.text(cols[1], y, str(n_ev), 9, "F1", INK)
        pdf.text(cols[2], y, f"{float(s['peak_score']):.2f}", 9, "F1", INK)  # type: ignore[arg-type]
        pdf.text(cols[3], y, status, 9, "F2", (0.72, 0.23, 0.23) if n_ev else (0.15, 0.45, 0.20))
        y -= 15
    y -= 12
    pdf.text(M, y, "ANOMALY EVENTS", 10, "F2", NAVY)
    pdf.hline(M, W - M, y - 4, GOLD, 1.0)
    y -= 22
    any_events = False
    for k, s in sensors.items():
        for ev in s["events"]:  # type: ignore[union-attr]
            any_events = True
            if y < 130:
                y = header()
            name = labels.get(k, {}).get("en", k)
            pdf.text(M, y, f"{ev['start_time']} -> {ev['end_time']}", 9, "F3", INK)
            pdf.text(M + 235, y, name[:40], 9, "F1", INK)
            pdf.text(M + 470, y, f"score {float(ev['peak_score']):.2f}", 9, "F2", (0.72, 0.23, 0.23))
            y -= 14
    if not any_events:
        pdf.text(M, y, "No anomaly events in this period.", 9.5, "F1", GRAY)
        y -= 14
    y -= 18
    if y < 210:
        y = header()
    pdf.text(M, y, "SOVEREIGN SEAL (Silsila chain of custody)", 10, "F2", NAVY)
    pdf.hline(M, W - M, y - 4, GOLD, 1.0)
    y -= 20
    pdf.rect(M, y - 96, W - 2 * M, 104, (0.965, 0.955, 0.93))
    yy = y - 6
    for k2 in ("seal_sha256", "payload_sha256", "prev_seal", "host_fingerprint",
               "created_utc", "created_gulf"):
        pdf.text(M + 10, yy, k2, 7.5, "F2", GRAY)
        pdf.text(M + 120, yy, str(seal[k2]), 8, "F3", INK)
        yy -= 15
    pdf.text(M + 10, yy, "verify", 7.5, "F2", GRAY)
    pdf.text(M + 120, yy, "python nawa_edge.py verify nawa_edge_seals.jsonl", 8, "F3", NAVY)
    pdf.text(M, 58, "This report was generated and sealed entirely on the local machine. "
                    "No data left the device.", 8, "F1", GRAY)
    pdf.text(M, 44, f"{BRAND} - Masdar City, Abu Dhabi - MIT open source", 8, "F1", GRAY)
    pdf.hline(M, W - M, 70, GOLD, 0.8)
    with open(path, "wb") as f:
        f.write(pdf.build())


def export_sealed_json(path: str, payload: Dict[str, object], seal: Dict[str, str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"payload": payload, "seal": seal}, f, ensure_ascii=False, indent=2)


# ============================================================================
# SECTION 6 — BILINGUAL DASHBOARD & AUDIT REPORT (self-contained HTML,
#             zero CDNs, zero telemetry — works with the cable unplugged)
# ============================================================================

I18N: Dict[str, Dict[str, str]] = {
    "en": {
        "dir": "ltr", "other": "عربي",
        "title": "NAWA EDGE", "subtitle": "Sovereign Edge Anomaly Sentinel",
        "pledge": "100% local · zero data leaves this machine · PDPL-aligned by design",
        "kpi_readings": "Readings analyzed", "kpi_events": "Anomaly events",
        "kpi_sensors": "Sensors watched", "kpi_seal": "Sovereign seal",
        "sealed": "sealed & verifiable", "mizan": "Fleet MIZAN score",
        "threshold": "alarm threshold", "expected": "Expected band (MIZAN ribbon)",
        "value": "Value", "score": "Score", "anomaly": "Anomaly",
        "ambient": "Thermal context", "dl_csv": "Events CSV",
        "dl_json": "Sealed JSON", "dl_pdf": "Sealed PDF", "dl_audit": "Audit report",
        "downloads": "Exports", "footer": "Built by Nawa Advanced Technologies · Masdar City, Abu Dhabi · nawacore.ai · MIT open source",
        "made": "Building the Core of Intelligent Futures",
    },
    "ar": {
        "dir": "rtl", "other": "English",
        "title": "NAWA EDGE", "subtitle": "حارس كشف الشذوذ السيادي على الحافة",
        "pledge": "محلي ١٠٠٪ · لا تغادر أي بيانات هذا الجهاز · متوافق مع قانون حماية البيانات",
        "kpi_readings": "قراءات محلّلة", "kpi_events": "أحداث شاذة",
        "kpi_sensors": "مستشعرات مراقبة", "kpi_seal": "الختم السيادي",
        "sealed": "مختوم وقابل للتحقق", "mizan": "درجة ميزان للمنشأة",
        "threshold": "عتبة الإنذار", "expected": "النطاق المتوقع (شريط ميزان)",
        "value": "القيمة", "score": "الدرجة", "anomaly": "شذوذ",
        "ambient": "السياق الحراري", "dl_csv": "جدول الأحداث CSV",
        "dl_json": "JSON مختوم", "dl_pdf": "PDF مختوم", "dl_audit": "تقرير التدقيق",
        "downloads": "التصدير", "footer": "من تطوير نوى للتقنيات المتقدمة · مدينة مصدر، أبوظبي · nawacore.ai · مفتوح المصدر MIT",
        "made": "نبني نواة المستقبل الذكي",
    },
}

DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nawa Edge — Sovereign Edge Anomaly Sentinel</title>
<style>
  :root{
    --navy:#031528; --navy-2:#0A2138; --card:#0A2138; --ivory:#F5F1E6;
    --ink:#F5F1E6; --ink-2:#B9C2D0; --muted:#7C8798; --gold:#C9A227;
    --gold-soft:#B8892A; --grid:rgba(245,241,230,.07); --line:rgba(245,241,230,.12);
    --s-pump:#3F86D6; --s-hvac:#1E9E74; --s-solar:#B8892A; --alarm:#D4544A;
  }
  *{box-sizing:border-box; margin:0; padding:0}
  body{background:var(--navy); color:var(--ink);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
       padding:0 0 48px}
  header{display:flex; align-items:center; gap:18px; flex-wrap:wrap;
         padding:22px 32px; border-bottom:1px solid var(--line);
         background:linear-gradient(180deg,#020C18,var(--navy))}
  .mark{width:44px;height:44px;border-radius:12px;background:var(--gold);
        display:grid;place-items:center;color:#020C18;font-weight:800;font-size:20px}
  h1{font-size:24px;letter-spacing:.04em}
  h1 .ar{color:var(--gold);margin-inline-start:10px;font-weight:700}
  .sub{color:var(--ink-2);font-size:13px}
  .pledge{margin-inline-start:auto;display:flex;align-items:center;gap:10px}
  .pill{border:1px solid var(--gold);color:var(--gold);border-radius:999px;
        padding:5px 14px;font-size:12px;letter-spacing:.02em}
  button.lang{background:transparent;border:1px solid var(--line);color:var(--ink);
        border-radius:999px;padding:6px 16px;font-size:13px;cursor:pointer}
  button.lang:hover{border-color:var(--gold);color:var(--gold)}
  main{max-width:1200px;margin:0 auto;padding:26px 32px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
  .kpi .v{font-size:30px;font-weight:750;margin-top:2px}
  .kpi .v.gold{color:var(--gold);font-size:17px;font-family:ui-monospace,Menlo,Consolas,monospace}
  .kpi .l{color:var(--muted);font-size:12px;letter-spacing:.05em;text-transform:uppercase}
  .kpi .s{color:var(--ink-2);font-size:12px;margin-top:4px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
        padding:18px 18px 8px;margin-top:16px;position:relative}
  .card h2{font-size:14.5px;font-weight:650;display:flex;align-items:center;gap:9px}
  .dot{width:9px;height:9px;border-radius:50%}
  .badge{margin-inline-start:auto;font-size:11.5px;color:var(--alarm);
         border:1px solid var(--alarm);border-radius:999px;padding:2px 10px}
  .badge.ok{color:#4CAF82;border-color:#2E6B52}
  .legend{color:var(--muted);font-size:11.5px;margin:4px 0 0 0}
  svg{display:block;width:100%;height:190px;margin-top:6px;direction:ltr}
  svg text{font:10.5px system-ui,sans-serif;fill:var(--muted)}
  .tip{position:absolute;pointer-events:none;background:#020C18;border:1px solid var(--gold);
       border-radius:10px;padding:8px 12px;font-size:12px;color:var(--ink);
       display:none;z-index:9;min-width:190px;box-shadow:0 6px 24px rgba(0,0,0,.5)}
  .tip b{color:var(--gold)}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:22px}
  .row .lbl{color:var(--muted);font-size:12px;letter-spacing:.06em;text-transform:uppercase}
  a.dl{border:1px solid var(--gold);color:var(--gold);text-decoration:none;
       border-radius:10px;padding:8px 16px;font-size:13px}
  a.dl:hover{background:var(--gold);color:#020C18}
  footer{max-width:1200px;margin:34px auto 0;padding:18px 32px;color:var(--muted);
         font-size:12.5px;border-top:1px solid var(--line);display:flex;gap:8px;flex-wrap:wrap}
  footer .gold{color:var(--gold)}
  [dir="rtl"] body{font-family:system-ui,"Segoe UI",Tahoma,sans-serif}
</style>
</head>
<body>
<header>
  <div class="mark">N</div>
  <div>
    <h1><span data-t="title">NAWA EDGE</span></h1>
    <div class="sub" data-t="subtitle">Sovereign Edge Anomaly Sentinel</div>
  </div>
  <div class="pledge">
    <span class="pill" data-t="pledge">100% local · zero data leaves this machine</span>
    <button class="lang" id="langBtn" onclick="flip()">عربي</button>
  </div>
</header>
<main>
  <div class="kpis" id="kpis"></div>
  <div id="charts"></div>
  <div class="row">
    <span class="lbl" data-t="downloads">Exports</span>
    <a class="dl" href="nawa_edge_events.csv" download data-t="dl_csv">Events CSV</a>
    <a class="dl" href="nawa_edge_report_sealed.json" download data-t="dl_json">Sealed JSON</a>
    <a class="dl" href="nawa_edge_report_sealed.pdf" download data-t="dl_pdf">Sealed PDF</a>
    <a class="dl" href="nawa_edge_audit_report.html" data-t="dl_audit">Audit report</a>
  </div>
</main>
<footer>
  <span data-t="footer"></span> · <span class="gold" data-t="made"></span>
</footer>
<script>
const DATA = __DATA_JSON__;
const I18N = __I18N_JSON__;
let LANG = "en";
const $ = s => document.querySelector(s);
const T = k => I18N[LANG][k] || k;

function flip(){ LANG = LANG === "en" ? "ar" : "en"; render(); }

function fmt(x){ return Math.abs(x) >= 100 ? x.toFixed(0) : Math.abs(x) >= 10 ? x.toFixed(1) : x.toFixed(2); }

function kpis(){
  const tot = DATA.sensors.reduce((a,s)=>a+s.events.length,0);
  $("#kpis").innerHTML = [
    {l:T("kpi_readings"), v:DATA.time.length.toLocaleString("en"), s:DATA.time[0]+" → "+DATA.time[DATA.time.length-1]},
    {l:T("kpi_events"), v:tot, s:tot? "⚠ " + T("anomaly") : "—"},
    {l:T("kpi_sensors"), v:DATA.sensors.length, s:DATA.meta.site},
    {l:T("kpi_seal"), v:DATA.meta.seal_short, s:"✓ "+T("sealed"), gold:true},
  ].map(k=>`<div class="kpi"><div class="l">${k.l}</div><div class="v${k.gold?" gold":""}">${k.v}</div><div class="s">${k.s}</div></div>`).join("");
}

function chart(card, s){
  const Wp = card.clientWidth - 36, Hp = 190, padL = 46, padR = 12, padT = 12, padB = 24;
  const n = s.values.length;
  const xs = i => padL + (Wp - padL - padR) * i / (n - 1);
  let lo = Math.min(...s.values, ...(s.lo||[])), hi = Math.max(...s.values, ...(s.hi||[]));
  if (s.max1) { lo = 0; hi = Math.max(hi, 1); }
  const min0 = lo, span = (hi - lo) || 1; lo -= 0.06*span; hi += 0.06*span;
  if (min0 >= 0) lo = Math.max(lo, 0);
  if (s.max1) hi = Math.min(hi, 1.05);
  const ys = v => padT + (Hp - padT - padB) * (1 - (v - lo) / (hi - lo));
  let g = "";
  // gridlines + y labels
  for (let i = 0; i <= 3; i++){
    const v = lo + (hi - lo) * i / 3, y = ys(v);
    g += `<line x1="${padL}" x2="${Wp-padR}" y1="${y}" y2="${y}" stroke="var(--grid)"/>`
       + `<text x="${padL-6}" y="${y+3.5}" text-anchor="end">${fmt(v)}</text>`;
  }
  // x labels every ~6h
  const step = Math.max(1, Math.round(n / 8));
  for (let i = 0; i < n; i += step){
    g += `<text x="${xs(i)}" y="${Hp-6}" text-anchor="middle">${DATA.time[i].slice(11)}</text>`;
  }
  // anomaly shading
  for (const ev of s.events){
    g += `<rect x="${xs(ev.s)}" y="${padT}" width="${Math.max(xs(ev.e)-xs(ev.s),2)}" height="${Hp-padT-padB}" fill="var(--alarm)" opacity="0.16"/>`;
  }
  // MIZAN ribbon (expected band)
  if (s.lo){
    let band = "M" + s.lo.map((v,i)=>`${xs(i).toFixed(1)},${ys(v).toFixed(1)}`).join("L");
    band += "L" + [...s.hi.keys()].reverse().map(i=>`${xs(i).toFixed(1)},${ys(s.hi[i]).toFixed(1)}`).join("L") + "Z";
    g += `<path d="${band}" fill="${s.color}" opacity="0.14"/>`;
  }
  // threshold line for score charts
  if (s.thr !== undefined){
    g += `<line x1="${padL}" x2="${Wp-padR}" y1="${ys(s.thr)}" y2="${ys(s.thr)}" stroke="var(--alarm)" stroke-dasharray="5 4" stroke-width="1.4"/>`
       + `<text x="${Wp-padR}" y="${ys(s.thr)-5}" text-anchor="end" fill="var(--alarm)">${T("threshold")} ${s.thr}</text>`;
  }
  // value line
  const line = "M" + s.values.map((v,i)=>`${xs(i).toFixed(1)},${ys(v).toFixed(1)}`).join("L");
  g += `<path d="${line}" fill="none" stroke="${s.color}" stroke-width="2"/>`;
  // peak markers
  for (const ev of s.events){
    g += `<circle cx="${xs(ev.pk)}" cy="${ys(s.values[ev.pk])}" r="4.5" fill="var(--alarm)" stroke="var(--navy)" stroke-width="2"/>`;
  }
  const svg = card.querySelector("svg");
  svg.setAttribute("viewBox", `0 0 ${Wp} ${Hp}`);
  svg.innerHTML = g;
  // crosshair tooltip
  const tip = card.querySelector(".tip");
  svg.onmousemove = e => {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(n-1, Math.round((e.clientX - r.left - padL) / (Wp - padL - padR) * (n-1))));
    let rows = `<b>${DATA.time[i]}</b><br>${T("value")}: <b>${fmt(s.values[i])}</b>`;
    if (s.lo) rows += `<br>${T("expected")}: ${fmt(s.lo[i])} – ${fmt(s.hi[i])}`;
    if (s.p)  rows += `<br>${T("score")}: ${s.p[i].toFixed(2)}`;
    tip.innerHTML = rows;
    tip.style.display = "block";
    const tx = Math.min(e.clientX - r.left + 18, Wp - 210);
    tip.style.left = tx + "px"; tip.style.top = (e.clientY - r.top - 10) + "px";
    hover.setAttribute("x1", xs(i)); hover.setAttribute("x2", xs(i));
    hover.style.display = "block";
  };
  svg.onmouseleave = () => { tip.style.display = "none"; hover.style.display = "none"; };
  const hover = document.createElementNS("http://www.w3.org/2000/svg","line");
  hover.setAttribute("y1", padT); hover.setAttribute("y2", Hp - padB);
  hover.setAttribute("stroke", "var(--gold)"); hover.setAttribute("stroke-width", "1");
  hover.style.display = "none";
  svg.appendChild(hover);
}

function charts(){
  const host = $("#charts");
  host.innerHTML = "";
  const series = [];
  if (DATA.context) series.push({key:"__ctx", label:T("ambient")+" — "+(DATA.context.label[LANG]||DATA.context.key),
    values:DATA.context.values, color:"#8FA1BC", events:[], thin:true});
  for (const s of DATA.sensors) series.push({...s, label:s.label[LANG]||s.key});
  series.push({key:"__mizan", label:T("mizan"), values:DATA.mizan, color:"var(--gold-soft)",
    events:[], thr:DATA.tau, max1:true, mizan:true});
  for (const s of series){
    const card = document.createElement("div");
    card.className = "card";
    const bad = s.events && s.events.length;
    card.innerHTML = `<h2><span class="dot" style="background:${s.color}"></span>${s.label}` +
      (s.key.startsWith("__") ? "" :
        `<span class="badge${bad?"":" ok"}">${bad ? bad+" × "+T("anomaly") : "✓"}</span>`) +
      `</h2>` + (s.lo ? `<div class="legend">▧ ${T("expected")}</div>` : "") +
      `<svg preserveAspectRatio="none"></svg><div class="tip"></div>`;
    host.appendChild(card);
    chart(card, s);
  }
}

function render(){
  const t = I18N[LANG];
  document.documentElement.lang = LANG;
  document.documentElement.dir = t.dir;
  document.querySelectorAll("[data-t]").forEach(el => { el.textContent = t[el.dataset.t] || el.textContent; });
  $("#langBtn").textContent = t.other;
  kpis(); charts();
}
render();
addEventListener("resize", () => charts());
</script>
</body>
</html>
"""


def build_dashboard(path: str, site: str, times: List[str], context: Optional[List[float]],
                    context_key: Optional[str], results: Dict[str, SensorResult],
                    mizan: List[float], pr: MizanParams, seal: Dict[str, str],
                    labels: Dict[str, Dict[str, str]]) -> None:
    colors = ["#3F86D6", "#1E9E74", "#B8892A", "#3F86D6", "#1E9E74", "#B8892A"]
    sensors_js = []
    for i, (k, r) in enumerate(results.items()):
        lab = labels.get(k, {"en": k, "ar": k})
        vals = _sensor_values(r)
        nonneg = bool(vals) and min(vals) >= 0.0  # physical floor for display
        sensors_js.append({
            "key": k, "label": lab, "color": colors[i % len(colors)],
            "values": [round(v, 4) for v in vals],
            "lo": [round(max(t.lo, 0.0) if nonneg else t.lo, 4) for t in r.ticks],
            "hi": [round(t.hi, 4) for t in r.ticks],
            "p": [round(t.p, 3) for t in r.ticks],
            "events": [{"s": ev["start_index"], "e": ev["end_index"],
                        "pk": ev["peak_index"]} for ev in r.events],
        })
    data = {
        "meta": {"site": site, "seal_short": seal["seal_sha256"][:16] + "…"},
        "time": times,
        "context": ({"key": context_key,
                     "label": labels.get(context_key or "", {"en": context_key, "ar": context_key}),
                     "values": context} if context is not None else None),
        "sensors": sensors_js,
        "mizan": mizan,
        "tau": pr.tau,
    }
    html = (DASHBOARD_TEMPLATE
            .replace("__DATA_JSON__", json.dumps(data, ensure_ascii=False))
            .replace("__I18N_JSON__", json.dumps(I18N, ensure_ascii=False)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def _sensor_values(r: SensorResult) -> List[float]:
    # values are reconstructed from band + exceedance? No — we keep raw values
    # alongside; this hook exists so run_all can inject them (set below).
    return getattr(r, "_values", [])  # type: ignore[return-value]


AUDIT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Nawa Edge — Sealed Audit Report · تقرير تدقيق مختوم</title>
<style>
 body{font:15px/1.6 Georgia,'Times New Roman',serif;color:#1B2430;background:#FAF7F0;
      max-width:860px;margin:0 auto;padding:48px 32px}
 .band{background:#031528;color:#F5F1E6;padding:26px 30px;border-radius:14px 14px 0 0;
       border-bottom:3px solid #C9A227}
 .band h1{margin:0;font-size:26px} .band .ar{float:right;font-size:22px;color:#C9A227}
 .band p{margin:6px 0 0;color:#B9C2D0;font-size:13px}
 section{background:#fff;border:1px solid #E4DCC8;border-top:none;padding:26px 30px}
 h2{font-size:15px;letter-spacing:.08em;text-transform:uppercase;color:#031528;
    border-bottom:2px solid #C9A227;padding-bottom:6px}
 h2 .ar{float:right;text-transform:none;letter-spacing:0}
 table{width:100%;border-collapse:collapse;font-size:13.5px}
 th{color:#7C8798;text-align:left;font-weight:600;padding:6px 8px;border-bottom:1px solid #E4DCC8}
 td{padding:7px 8px;border-bottom:1px solid #F0EADA}
 .alarm{color:#B03A30;font-weight:700} .ok{color:#2E6B52;font-weight:700}
 .seal{background:#F7F3E8;border:1px dashed #C9A227;border-radius:10px;padding:16px 20px;
       font:12.5px ui-monospace,Menlo,Consolas,monospace;word-break:break-all}
 .seal b{display:inline-block;min-width:150px;color:#7C8798;font-family:Georgia,serif}
 footer{color:#7C8798;font-size:12.5px;padding:22px 30px;text-align:center}
 [dir=rtl] th{text-align:right}
 @media print{body{background:#fff;padding:0}}
</style></head>
<body>
<div class="band"><span class="ar">تقرير تدقيق مختوم</span>
 <h1>NAWA EDGE — Sealed Audit Report</h1>
 <p>__SITE__ · __PERIOD__ · MIZAN-SARAB v__ALGOV__ · 100% local — zero data left this machine
 · محلي ١٠٠٪ — لا تغادر أي بيانات هذا الجهاز</p></div>
<section><h2>Anomaly events <span class="ar">الأحداث الشاذة</span></h2>
__EVENTS_TABLE__
</section>
<section><h2>Sovereign seal <span class="ar">الختم السيادي</span></h2>
 <p>This report is sealed into a local SHA-256 hash chain (<i>Silsila</i>). Verify offline:
 <code>python nawa_edge.py verify nawa_edge_seals.jsonl</code> —
 هذا التقرير مختوم بسلسلة تجزئة SHA-256 محلية ويمكن التحقق منه دون اتصال.</p>
 <div class="seal">__SEAL_ROWS__</div>
</section>
<footer>Built by Nawa Advanced Technologies · Masdar City, Abu Dhabi · nawacore.ai · MIT open source<br>
نبني نواة المستقبل الذكي · Building the Core of Intelligent Futures</footer>
</body></html>
"""


def export_audit_html(path: str, payload: Dict[str, object], seal: Dict[str, str],
                      labels: Dict[str, Dict[str, str]]) -> None:
    rows = ["<table><tr><th>Sensor · المستشعر</th><th>Start · البداية</th>"
            "<th>End · النهاية</th><th>Peak score · الذروة</th></tr>"]
    sensors: Dict[str, Dict[str, object]] = payload["sensors"]  # type: ignore[assignment]
    n_ev = 0
    for k, s in sensors.items():
        lab = labels.get(k, {})
        name = f"{lab.get('en', k)}<br><span dir='rtl'>{lab.get('ar', '')}</span>"
        for ev in s["events"]:  # type: ignore[union-attr]
            n_ev += 1
            rows.append(f"<tr><td>{name}</td><td>{ev['start_time']}</td>"
                        f"<td>{ev['end_time']}</td><td class='alarm'>{ev['peak_score']}</td></tr>")
    if not n_ev:
        rows.append("<tr><td colspan=4 class='ok'>No anomalies · لا يوجد شذوذ</td></tr>")
    rows.append("</table>")
    seal_rows = "".join(f"<b>{k}</b> {v}<br>" for k, v in seal.items())
    period = payload["period"]  # type: ignore[index]
    html = (AUDIT_TEMPLATE
            .replace("__SITE__", str(payload["site"]))
            .replace("__PERIOD__", f"{period['from']} → {period['to']}")  # type: ignore[index]
            .replace("__ALGOV__", ALGO_VERSION)
            .replace("__EVENTS_TABLE__", "".join(rows))
            .replace("__SEAL_ROWS__", seal_rows))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


# ============================================================================
# SECTION 7 — ORCHESTRATION, LOCAL SERVER & CLI
# ============================================================================


def run_all(times: List[str], context: Optional[List[float]], context_key: Optional[str],
            sensors: Dict[str, List[float]], site: str, outdir: str,
            labels: Optional[Dict[str, Dict[str, str]]] = None,
            params: Optional[MizanParams] = None) -> Dict[str, str]:
    """Detect → seal → export everything. Returns paths of artifacts."""
    os.makedirs(outdir, exist_ok=True)
    labels = labels or {k: {"en": k, "ar": k} for k in sensors}
    results, mizan, pr = run_detection(times, context, sensors, params)
    for k, r in results.items():
        r._values = sensors[k]  # type: ignore[attr-defined]

    payload = build_report_payload(site, times, results, mizan, pr)
    chain = os.path.join(outdir, "nawa_edge_seals.jsonl")
    prev = "GENESIS"
    if os.path.exists(chain):
        with open(chain, "r", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        if lines:
            prev = json.loads(lines[-1])["seal_sha256"]
    seal = make_seal(payload, prev)
    append_seal(chain, seal)

    paths = {
        "csv": os.path.join(outdir, "nawa_edge_events.csv"),
        "json": os.path.join(outdir, "nawa_edge_report_sealed.json"),
        "pdf": os.path.join(outdir, "nawa_edge_report_sealed.pdf"),
        "audit": os.path.join(outdir, "nawa_edge_audit_report.html"),
        "dashboard": os.path.join(outdir, "dashboard.html"),
        "chain": chain,
    }
    export_events_csv(paths["csv"], results)
    export_sealed_json(paths["json"], payload, seal)
    export_sealed_pdf(paths["pdf"], payload, seal, labels)
    export_audit_html(paths["audit"], payload, seal, labels)
    build_dashboard(paths["dashboard"], site, times, context, context_key,
                    results, mizan, pr, seal, labels)

    total_events = sum(len(r.events) for r in results.values())
    print(f"nawa-edge: analyzed {len(times)} readings × {len(sensors)} sensors "
          f"→ {total_events} anomaly event(s)")
    for k, r in results.items():
        for ev in r.events:
            print(f"  ⚠ {k}: {ev['start_time']} → {ev['end_time']} "
                  f"(peak score {ev['peak_score']})")
    print(f"nawa-edge: sovereign seal {seal['seal_sha256'][:20]}… appended to {chain}")
    return paths


def serve(outdir: str, port: int, open_browser: bool) -> None:
    """Serve the output folder on localhost only (sovereign by default)."""
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=outdir, **kw)
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/dashboard.html"
    print(f"nawa-edge: dashboard live at {url}  (Ctrl+C to stop — localhost only)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nnawa-edge: sentinel stopped. Ma'a salama.")


def main(argv: Optional[Sequence[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        prog="nawa-edge",
        description="Nawa Edge — sovereign, air-gapped anomaly sentinel "
                    "(MIZAN-SARAB engine). MIT, by Nawa · nawacore.ai",
        epilog="examples:  python nawa_edge.py            (full demo)\n"
               "           python nawa_edge.py detect plant.csv --context-col ambient_c\n"
               "           python nawa_edge.py verify nawa_edge_out/nawa_edge_seals.jsonl",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("demo", help="run the 48-hour UAE demo plant")
    d.add_argument("--csv-only", action="store_true", help="only write the sample CSV")
    dt = sub.add_parser("detect", help="run MIZAN-SARAB on your own CSV")
    dt.add_argument("csv_path")
    dt.add_argument("--time-col", default="timestamp")
    dt.add_argument("--context-col", default=None,
                    help="thermal/ambient context column (auto-detected if omitted)")
    dt.add_argument("--site", default=None, help="site name for the report")
    dt.add_argument("--sensitivity", choices=["low", "medium", "high"], default="medium")
    v = sub.add_parser("verify", help="verify a Silsila seal chain")
    v.add_argument("chain_path")
    v.add_argument("--report", default=None, help="also verify a sealed JSON report")
    for p in (ap, d, dt):
        p.add_argument("--out", default="nawa_edge_out", help="output folder")
        p.add_argument("--port", type=int, default=8742)
        p.add_argument("--no-browser", action="store_true")
        p.add_argument("--no-serve", action="store_true", help="write files, don't serve")
    args = ap.parse_args(argv)

    if args.cmd == "verify":
        ok, msgs = verify_chain(args.chain_path, args.report)
        for m in msgs:
            print(("  ✓ " if "OK" in m else "  ✗ ") + m)
        print("nawa-edge: chain VERIFIED — untampered." if ok else "nawa-edge: chain FAILED verification.")
        sys.exit(0 if ok else 1)

    if args.cmd == "detect":
        times, context, sensors = read_csv(args.csv_path, args.time_col, args.context_col)
        tau = {"low": 0.65, "medium": 0.45, "high": 0.30}[args.sensitivity]
        # warmup: at most one diurnal cycle at 1-min cadence, at least 120
        # readings, and never more than a quarter of the stream — so hourly
        # or sparse CSVs still get alarms.
        warmup = max(120, min(1440, len(times) // 4))
        params = MizanParams(tau=tau, warmup=warmup)
        site = args.site or os.path.basename(args.csv_path)
        ctx_key = args.context_col or "ambient"
        paths = run_all(times, context, ctx_key, sensors, site, args.out, params=params)
    else:  # demo (default)
        times, ambient, sensors = generate_demo()
        os.makedirs(args.out, exist_ok=True)
        sample = os.path.join(args.out, "sample_uae_plant.csv")
        write_demo_csv(sample, times, ambient, sensors)
        print(f"nawa-edge: 48 h UAE demo plant written to {sample}")
        if args.cmd == "demo" and args.csv_only:
            return
        paths = run_all(times, ambient, "ambient_c", sensors,
                        "Demo Plant — Masdar City, Abu Dhabi", args.out, labels=DEMO_LABELS)

    if not args.no_serve:
        serve(args.out, args.port, open_browser=not args.no_browser)
    else:
        print(f"nawa-edge: open {paths['dashboard']} in any browser.")


if __name__ == "__main__":
    main()
