# Attendance System (FastAPI + SQLite)

Admin dashboard (live feed, punches with selfies/GPS, sites, employees, daily/monthly reports, CSV), an installable employee phone app (`/m/`: ID + PIN login, GPS geofence, selfie), and fingerprint-device integration (Realtime/FK-series via FkWeb, plus ZKTeco pull/ADMS).

**Hosting:** see [DEPLOY.md](DEPLOY.md).

## Run
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
ZK_DEVICES=192.168.1.201:4370 uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Open http://localhost:8000. Test without hardware: `python scripts/simulate_device.py`.

## Connecting the ZKTeco device
**Pull mode (simplest, works on any ZK device):** set `ZK_DEVICES=ip[:port],...` (default port 4370, `ZK_PASSWORD` if the device has a comm key). The server syncs users and logs every `ZK_POLL_SECONDS` (default 10s). Server must reach the device IP.

**Push mode (true real-time, ADMS):** on the device: Menu → Comm/COMM → Cloud Server Setting → Server Address = this server's IP, Port = 8000, enable Proxy off. The device POSTs to `/iclock/cdata`. Needs firmware with ADMS support.

**FkWeb mode (Realtime / FK-series devices, e.g. T304F mini = M61BH):** the device POSTs each scan to the *Webserver URL* in its Comm menu (set it to `http://<server-pc-ip>`). The app runs a built-in listener for this on port 80 (`FKWEB_PORT`, -1 disables). Allow the port in Windows Firewall. The device sends only user IDs, so unknown IDs get a placeholder name you can edit in the Employees tab. Raw requests are logged to `fkweb_raw.log`.

Use either or both (duplicates are ignored).

## Config (env vars)
`DB_PATH`, `SELFIE_DIR`, `TIMEZONE`, `GEOFENCE_MODE`, `SECRET_KEY`, `FKWEB_DEVICES`, `PUBLIC_DOMAIN`, `FKWEB_PORT` (80), `ZK_DEVICES`, `ZK_PASSWORD`, `ZK_POLL_SECONDS`, `SHIFT_START` (09:00), `GRACE_MINUTES` (10), `DEBOUNCE_SECONDS` (60), `ADMIN_PASSWORD` (enables HTTP Basic auth, user `admin`; device endpoints stay open).

## Tests
`pytest`
