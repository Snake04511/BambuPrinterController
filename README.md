# PrinterDash

A self-hosted web app / PWA for a **Bambu Lab P1S** that talks to the printer directly over the LAN (MQTT, FTPS, camera). One printer, one Docker container, no other service needed.

**Stack:** FastAPI (Python 3.12) backend + React 19 / TypeScript / Tailwind 4 frontend. Dark neon UI, accent `#38FF9C`.

## Features

**Live control**
- Status, temperatures, fans, speed, light, pause / resume / stop, homing, jogging, extrusion.
- HMS errors.
- Status, light and moves go over **LAN MQTT**.
  - **Temperatures and the part fan** go over Bambu Cloud MQTT, because the P1S ignores `M104`/`M140`/`M106` sent as LAN `gcode_line`.

**Camera**
- Direct P1S camera stream (port 6000).
- Restart camera button.
- Timelapse recording of every print.
  - MP4 export with an optional info overlay.
  - Pieces are merged after an app restart.

**SD card / files**
- File list, details and plate thumbnails over the printer's FTPS.
- Print start with AMS mapping, timelapse, bed levelling and flow calibration options.
- Downloaded 3MF files are kept on the server (LRU limit).

**Multi-plate runs**
- Print plate after plate of one 3MF.
- Every next plate waits for a confirmed **plate clear**; nothing starts on its own.

**Live 3D progress**
- Outer-wall tool paths extracted from the plate G-code.
- Layer slider.
- Per-object colours.

**Skip objects**
- 2D pick image / 3D view.
- Numbered object list.

**AMS / spools**
- Own spool inventory with location and cost per kg.
- Slot configuration and RFID refresh.
- Live remaining grams during a print, run-out forecast.
- AMS humidity history chart and a drying reminder.

**Print log and statistics**
- Own print log with cost (filament + electricity) and thumbnails.
- Monthly and all-time stats.

**Maintenance**
- Counters by print hours / days with reminders.

**Notifications**
- Discord webhook and ntfy, toggled per channel and per event.
- Snapshots, filament colour swatches and a daily / weekly summary.
- Message templates are editable in Settings (the default texts are Hungarian; edit them to translate).

**Automation**
- Light on during a print, off a few minutes after the end.
- Speed change after the first layer.

**AI failure detection**
- [Obico](https://www.obico.io/) ML API integration (self-hosted).

**Backups**
- Daily SQLite backup to `/backups`.

**ESP32 panel API**
- `/api/v1/...` routes in the REST shapes an ESP32 touch panel firmware expects.
- Plus native `/api/...` endpoints (live state, commands, SD files, filament forecast), authenticated with an `X-API-Key`.

## Requirements

- A Bambu Lab P1S in **LAN mode**. You need its IP, serial number and **access code** (printer screen → Settings → WLAN).
- Any Docker host on the same network as the printer (a small Linux box, NAS or Raspberry Pi 4/5 is enough).
- Optional:
  - a Bambu Cloud account (temperature / fan commands, cloud task history);
  - an Obico ML API server;
  - a Discord webhook / ntfy topic.

## Quick start (Docker)

The image serves the built frontend, so build it first (Node.js 20+), then start the container:

```bash
git clone https://github.com/Snake04511/BambuPrinterController.git
cd BambuPrinterController
cd frontend
npm ci
npm run build
cd ..
docker compose up -d --build
```

Before the first start, set your time zone in `docker-compose.yml` (`TZ`).

Open `http://<host>:8090`. On first start you set the login password / PIN. Then go to **Settings**:

1. **Printer (LAN):** fill in the IP, serial number and access code. The app connects right away.
2. **Bambu Cloud** (optional): log in for temperature and fan commands, starting prints and cloud reprints. The access code can also be read from the cloud account here.
3. **Maintenance:** add the usual P1S items with one click, or your own.
4. **Notifications, Obico, costs, storage limits, backups.**

Spools are added on the **Spools** page and assigned to AMS trays on the **AMS** page.

Secrets (access code, API keys, tokens, webhook URL) are stored only in the SQLite database in the `/data` volume and are never sent back to the frontend.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATA_DIR` | `/data` | SQLite database, caches, timelapses, kept 3MF files |
| `BACKUP_DIR` | `/backups` | Daily backups |
| `TZ` | `Europe/Budapest` | Your time zone (logs, daily summary, timestamps) |
| `PRINTER_IP`, `PRINTER_SERIAL` | – | Initial printer values (also editable in Settings) |
| `APP_URL` | – | This app's address as seen from the Obico server (e.g. `http://<host>:8090`), used to hand it camera frames |
| `SECURE_COOKIE` | `0` | Set to `1` behind HTTPS |
| `LOG_LEVEL` | `INFO` | |

Volumes in `docker-compose.yml`:
- `printerdash_data:/data`;
- `./backups:/backups`.

The container has a health check (`/api/health`). The `autoheal` label is only used if you run an [autoheal](https://github.com/willfarrell/docker-autoheal) container; otherwise it does nothing.

### Updating

```bash
git pull
cd frontend
npm ci
npm run build
cd ..
docker compose up -d --build
```

Your data stays in the `printerdash_data` volume.

## Development

```bash
# backend
python3.12 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt pytest
cd backend
DATA_DIR=/tmp/pd-data ../.venv/bin/uvicorn app.main:app --reload --port 8091
../.venv/bin/python -m pytest tests -q

# frontend (Vite dev server, proxies /api to :8091)
cd frontend
npm ci
npm run dev
npx tsc -b
```

Frontend code style: single quotes, no semicolons, print width 140:

```bash
npx prettier --single-quote --no-semi --print-width 140 --write src
```

## Project layout

```
backend/app/
  main.py            FastAPI app, native /api routes
  compat.py          Panel API: /api/v1 routes (ESP32 panel)
  poller.py          Engine: status polling, print start/end, automation, print log
  printer_mqtt.py    LAN MQTT client (status, commands, AMS slots, skip objects)
  printer_ftp.py     FTPS client for the printer's SD card
  printer_camera.py  P1S camera stream (port 6000)
  cloud.py           Bambu Cloud login, cloud MQTT, task history
  sdcard.py          SD file list, 3MF details, kept copies, plate thumbnails
  forecast.py        AMS auto mapping + filament check before a print
  gcodepaths.py      Outer-wall paths for the 3D progress view
  timelapse.py       Recording, MP4 export, merging
  inventory.py       Spools, AMS assignments, filament booking
  printlog.py        Own print log;  stats.py: statistics
  maintenance.py     Maintenance counters
  notify.py, push.py, notify_texts.py   Discord / ntfy notifications
  obico_client.py    Obico ML failure detection
  backup.py, settings.py, auth.py, db.py
backend/tests/       pytest suite
frontend/src/        React PWA (pages/: Home, Control, AMS, Spools, Files, Stats, Settings)
docs/                ESP32 panel prompt
```

## ESP32 panel

The `/api/v1/...` routes answer in the REST shapes the firmware of an ESP32-S3 7" LVGL panel (8048S070) expects:
- status, queue / plate clear;
- inventory, archives, notifications;
- maintenance, AMS history.

Generate the panel key in **Settings → ESP32 panel** and send it as `X-API-Key`. The same key also works on the native `/api/...` routes (`/api/state`, `/api/cmd/{name}`, `/api/sd/*`). See `docs/panel-*-prompt.md`.

## Notes

- **Commands:** they reach the real printer. Moves are refused by the backend while printing.
- **The P1S idle part fan:** it does not spin while the printer is idle. This is firmware behaviour (Bambu Studio does the same).
- **Affiliation:** not affiliated with Bambu Lab or Obico.
