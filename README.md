# Attendance System (FastAPI + SQLite + ZKTeco)

Live dashboard, employee management, daily/monthly reports, CSV export, and two ways to connect ZKTeco devices.

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

**FkWeb mode (Realtime / FK-series devices, e.g. T304F mini):** the device POSTs to the *Webserver URL* set in its Comm menu. Run the app on the TCP port set in the device (e.g. `--port 5005`) and set the URL to `http://<server-pc-ip>` (write down the old value first). Raw requests are logged to `fkweb_raw.log`; punch parsing is best-effort until verified with real traffic.

Use either or both (duplicates are ignored).

## Config (env vars)
`DB_PATH`, `ZK_DEVICES`, `ZK_PASSWORD`, `ZK_POLL_SECONDS`, `SHIFT_START` (09:00), `GRACE_MINUTES` (10), `DEBOUNCE_SECONDS` (60), `ADMIN_PASSWORD` (enables HTTP Basic auth, user `admin`; device endpoints stay open).

## Tests
`pytest`
