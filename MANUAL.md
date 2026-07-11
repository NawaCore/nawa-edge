# Nawa Edge — Engineer's Manual

*Version 1.0.0 · MIZAN-SARAB engine v1.0 · Nawa Advanced Technologies · nawacore.ai*

This manual covers everything a plant, reliability, or SCADA engineer needs to run
Nawa Edge on real equipment data — installation, data preparation, tuning,
interpreting results, and verifying the cryptographic audit trail.

---

## 1 · Requirements

- Python **3.9 or newer** (check with `python --version` or `python3 --version`)
- Any operating system: Windows, Linux, macOS
- **Nothing else.** No pip packages, no internet connection, no admin rights,
  no GPU. Nawa Edge runs on air-gapped machines by design.

If your OT workstation has Python installed for any other tool, Nawa Edge will run
on it. If not, a standard offline Python installer from python.org (transferred by
USB per your site's media policy) is sufficient.

## 2 · Installation

Copy **one file** — `nawa_edge.py` — anywhere on the target machine. That is the
entire installation. To remove it, delete the file and the output folder.

Verify:

```bash
python nawa_edge.py --help
```

## 3 · Quickstart — the built-in demo

```bash
python nawa_edge.py
```

This generates a realistic 48-hour, 1-minute-resolution demo plant (desalination
high-pressure pump vibration, district-cooling compressor power, solar inverter
output, under a 30→48 °C diurnal cycle), runs detection, writes all reports, and
opens the dashboard at `http://127.0.0.1:8742` (localhost only — nothing is exposed
to the network). Day 1 of the demo is silent learning; day 2 contains three real
faults and one single-tick dust spike that is correctly ignored.

Useful variants:

```bash
python nawa_edge.py --no-browser        # serve dashboard but don't auto-open
python nawa_edge.py --no-serve          # write files only (headless / scheduled)
python nawa_edge.py --port 9000         # different port
python nawa_edge.py demo --csv-only     # just write the sample CSV to inspect
```

## 4 · Running on your own data

### 4.1 CSV format

One tidy CSV: one row per timestamp, one column per sensor.

```csv
timestamp,ambient_c,pump_vibration_mm_s,compressor_kw,flow_m3h
2026-07-01 00:00,33.4,2.21,9.8,412.5
2026-07-01 00:01,33.4,2.19,9.9,413.1
...
```

Rules:

- **timestamp** — any format starting `YYYY-MM-DD HH:MM` or ISO-8601. Rows must be
  in time order. A fixed cadence (e.g. every minute) works best.
- **ambient / context column** — optional but strongly recommended. Any column whose
  name contains "ambient" or "temp" is auto-detected, or name it explicitly with
  `--context-col`. This is what makes the baseline climate-aware.
- **sensor columns** — every other numeric column is monitored automatically.
  Non-numeric columns are skipped. Avoid gaps; if a value is missing, repeat the
  previous reading or drop the row.
- Exports from most historians (PI, Wonderware, Ignition, DCS trend exports) can be
  shaped into this format with their standard CSV export.

### 4.2 Run detection

```bash
python nawa_edge.py detect plant.csv --context-col ambient_c --site "RO Train 2, Taweelah"
```

Options:

| Option | Default | Meaning |
|---|---|---|
| `--time-col` | `timestamp` | name of the time column |
| `--context-col` | auto-detect | ambient/thermal context column |
| `--site` | file name | plant/site name printed on all reports |
| `--sensitivity` | `medium` | `low` (fewer alarms) / `medium` / `high` (earlier alarms) |
| `--out` | `nawa_edge_out` | output folder |
| `--no-serve` | off | write reports without starting the dashboard |

### 4.3 How much data do you need?

The engine learns baselines per thermal zone × time-of-day phase. Practical guidance:

- **Minimum:** ~1 full day at minute cadence. Alarms are suppressed during the warmup
  period (up to the first diurnal cycle, capped at 25 % of the stream) — this is
  deliberate: day one is for listening.
- **Good:** 3–14 days including typical weather variation.
- The stream should start from **healthy operation**. If the fault is already present
  at the start of the file, the engine will learn it as normal — feed it a longer
  history that includes healthy running.

## 5 · Reading the results

### 5.1 Dashboard

Each sensor card shows: the raw signal (colored line), the **MIZAN ribbon** (shaded
band = the range the engine currently considers normal *for this temperature and time
of day*), red shading over detected anomaly windows, and a red dot at each event's
peak. The **Fleet MIZAN score** chart at the bottom fuses all sensors into one
plant-health line against the alarm threshold. Use the عربي / English button to
switch language; hover any chart for exact values.

### 5.2 Anomaly events (`nawa_edge_events.csv`)

| Column | Meaning |
|---|---|
| `sensor` | which signal alarmed |
| `start_time` / `end_time` | event window |
| `duration_min` | length in readings |
| `peak_score` | severity; 0.45 is the default alarm threshold, >1.0 is strong |
| `thermal_zone_at_peak` | 0 = coolest bin … 3 = hottest bin |

Rule of thumb: `peak_score` 0.45–0.8 = investigate at next round; 0.8–1.5 = investigate
today; >1.5 = act now.

### 5.3 What the engine will and won't catch

It catches **sustained departures from context-conditioned normal**: bearing-wear
ramps, efficiency losses, fouling/soiling, stuck sensors, abnormal cycling. It
deliberately does **not** alarm on: single-tick spikes (dust/EMI mirages), the first
occurrence of a never-seen operating state (learned silently), or conditions already
present throughout the training window. It is an anomaly sentinel, not a physics
model — treat alarms as prioritized attention, not diagnosis.

## 6 · Tuning

Start with `--sensitivity medium`. If operators report missed early-stage faults, use
`high`; if a noisy sensor generates nuisance events, use `low` or fix the sensor.
Engineers comfortable with Python can adjust `MizanParams` in the file — every
parameter is documented in-line (thermal bin edges default to 33/39/45 °C, tuned for
Gulf climate; change them for other regions).

## 7 · The Sovereign Seal — audit & compliance

Every run appends a seal to `nawa_edge_seals.jsonl`: SHA-256 of the canonical report,
chained to the previous seal, with dual UTC + Gulf timestamps, engine version, and a
pseudonymous host fingerprint. Verify any time, offline:

```bash
python nawa_edge.py verify nawa_edge_out/nawa_edge_seals.jsonl \
    --report nawa_edge_out/nawa_edge_report_sealed.json
```

`chain VERIFIED — untampered` means: these results existed at that time, on that
machine, produced by that engine version, and have not been edited since. Attach the
sealed PDF to work orders, insurance claims, or compliance records. Keep the
`.jsonl` chain file — it is the root of trust; back it up like a logbook.

## 8 · Scheduled / unattended operation

```bash
python nawa_edge.py detect latest_export.csv --no-serve --out /data/edge_reports
```

Exit is clean for cron / Task Scheduler use. Each run appends one seal. Point the
scheduler at your historian's daily CSV export for a daily sealed health report.

## 9 · Security notes for OT environments

- The dashboard server binds to `127.0.0.1` **only** — it is unreachable from the
  network. `--no-serve` avoids opening any socket at all.
- There are zero outbound network calls anywhere in the code (it is one readable file
  — your security team can audit it in an afternoon).
- The detector never writes raw readings anywhere new; reports contain only event
  summaries and statistics. Your CSV stays wherever you put it.

## 10 · Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `time column 'timestamp' not found` | pass `--time-col <name>` matching your header |
| `no numeric sensor columns found` | check decimal separators (use `.`), remove units from cells |
| No alarms on a known fault | fault present since start of file (feed longer healthy history), or fault too brief (<3 readings), or try `--sensitivity high` |
| Too many alarms on one sensor | sensor is genuinely erratic — `--sensitivity low`, or exclude the column |
| Dashboard doesn't open | use `--no-browser` and open `nawa_edge_out/dashboard.html` manually |
| Port in use | Nawa Edge picks a free port automatically and prints it; or set `--port` |
| Arabic text misrendered in terminal | cosmetic only; reports and dashboard are unaffected |

## 11 · Support & the commercial layer

Nawa Edge is free forever (MIT). For fleet-scale deployment, live OPC-UA/Modbus
connectors, sovereign AI agents acting on Edge signals, regulatory reporting packs,
and managed assurance — that is the **Nawa Industrial** platform:
**info@nawacore.ai** · **nawacore.ai**

---

<div dir="rtl">

## دليل البدء السريع (العربية)

**نوى إيدج** أداة مجانية لكشف الشذوذ في معدات البنية التحتية، تعمل محليًا بالكامل —
لا سحابة، لا إنترنت، لا تثبيت. انسخ الملف `nawa_edge.py` إلى أي جهاز عليه بايثون
(الإصدار ٣٫٩ أو أحدث) ثم نفّذ:

<div dir="ltr">

```bash
python nawa_edge.py
```

</div>

ستُفتح لوحة تحكم ثنائية اللغة على جهازك مباشرة مع عرض تجريبي واقعي (مضخة تحلية،
ضاغط تبريد، عاكس شمسي تحت حرارة ٤٨°). ولتشغيلها على بياناتك: جهّز ملف CSV فيه عمود
`timestamp` وعمود لدرجة الحرارة المحيطة وأعمدة رقمية للمستشعرات، ثم:

<div dir="ltr">

```bash
python nawa_edge.py detect plant.csv --context-col ambient_c
```

</div>

كل تقرير يُختم بـ**الختم السيادي** (سلسلة SHA-256 محلية) ويمكن التحقق منه دون
اتصال — دليل تدقيق وامتثال لا يغادر الجهاز. للدعم التجاري: info@nawacore.ai

</div>
