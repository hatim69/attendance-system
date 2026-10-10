import os
import tempfile

os.environ["FKWEB_PORT"] = "0"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.main import app  # noqa: E402


def test_end_to_end():
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "Day 9-18", "start": "09:00", "end": "18:00"})
        sid = [x for x in c.get("/api/shifts").json() if x["name"] == "Day 9-18"][0]["id"]
        c.post("/api/employees", json={"device_user_id": "1", "name": "Alice", "department": "Eng", "shift_id": sid})
        c.post("/api/employees", json={"device_user_id": "2", "name": "Bob", "shift_id": sid})
        assert c.get("/iclock/cdata?SN=X&options=all").text.startswith("GET OPTION")
        body = "1\t2025-03-03 08:55:00\t0\t1\n1\t2025-03-03 08:55:20\t0\t1\n1\t2025-03-03 17:30:00\t1\t1\n"
        r = c.post("/iclock/cdata?SN=X&table=ATTLOG", content=body)
        assert r.text == "OK:2"  # 2nd line debounced
        # replay is idempotent
        assert c.post("/iclock/cdata?SN=X&table=ATTLOG", content=body).text == "OK:0"
        rep = {r["name"]: r for r in c.get("/api/reports/daily?day=2025-03-03").json()}
        assert rep["Alice"]["status"] == "Present" and rep["Alice"]["hours"] == 8.58
        assert rep["Bob"]["status"] == "Absent"
        c.post("/api/punches", json={"device_user_id": "2", "ts": "2025-03-03 10:00:00"})
        assert c.get("/api/reports/daily?day=2025-03-03").json()[1]["status"] == "Late"
        assert "Alice" in c.get("/api/reports/daily.csv?day=2025-03-03").text
        assert c.get("/api/reports/monthly?month=2025-03").json()[0]["days_present"] == 1
        with c.websocket_connect("/ws") as ws:
            ws.send_text("hi")



def test_fkweb_real_device_capture(tmp_path, monkeypatch):
    """Replays requests captured verbatim from the device."""
    import socket
    from app import fkweb
    monkeypatch.setattr(config, "RAW_LOG", str(tmp_path / "raw.log"))

    def post(port, code, body, extra=""):
        raw = (f"POST http://192.168.1.28 HTTP/1.0\r\nAccept: */*\r\nContent-Type: application/octet-stream\r\n"
               f"request_code: {code}\r\ndev_id: RSS202506120926\r\n{extra}Content-Length: {len(body) + 4}\r\n"
               f"Connection: close\r\n\r\n").encode() + len(body).to_bytes(4, "little") + body
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(raw)
        out = s.recv(500).decode()
        s.close()
        return out

    with TestClient(app) as c:
        port = fkweb.bound_port
        glog = b'{"fk_bin_data_lib":"M50","user_id":"00217228","verify_mode":2,"io_mode":1,"io_time":"20261010140444"}'
        r = post(port, "realtime_glog", glog)
        assert r.startswith("HTTP/1.1 200") and "response_code: OK" in r
        post(port, "realtime_glog", glog)  # replay is ignored
        r = post(port, "receive_cmd", b'{"fk_name":"M61BH"}', "trans_id: 40\r\n")
        assert "response_code: ERROR_NO_CMD" in r and "trans_id: 40" in r
        punches = c.get("/api/punches?day=2026-10-10").json()
        assert [p["device_user_id"] for p in punches] == ["00217228"]
        assert punches[0]["ts"] == "2026-10-10 14:04:44" and punches[0]["source"] == "RSS202506120926"


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def _login(c, emp="E1", pin="1234"):
    r = c.post("/api/app/login", json={"employee_id": emp, "pin": pin})
    return r, {"Authorization": "Bearer " + r.json().get("token", "x")}


def test_mobile_app_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SELFIE_DIR", str(tmp_path / "selfies"))
    with TestClient(app) as c:
        c.post("/api/employees", json={"device_user_id": "E1", "name": "Eve"})
        assert c.post("/api/employees/E1/pin", json={"pin": "12"}).status_code == 400
        assert c.post("/api/employees/E1/pin", json={"pin": "1234"}).status_code == 200
        assert c.post("/api/employees/NOPE/pin", json={"pin": "1234"}).status_code == 404
        assert _login(c, pin="9999")[0].status_code == 401
        r, h = _login(c)
        assert r.status_code == 200 and c.get("/api/app/me").status_code == 401
        assert c.get("/api/app/me", headers={"Authorization": "Bearer " + r.json()["token"][:-2] + "xx"}).status_code == 401

        # no sites configured -> location recorded, never blocked
        form = {"lat": "19.0760", "lng": "72.8777", "accuracy": "20"}
        ok = c.post("/api/app/punch", data=form, files={"selfie": ("s.jpg", JPEG, "image/jpeg")}, headers=h)
        assert ok.status_code == 200 and ok.json()["type"] == "Check in"
        # immediate repeat is debounced
        assert c.post("/api/app/punch", data=form, files={"selfie": ("s.jpg", JPEG, "image/jpeg")}, headers=h).status_code == 409
        # not an image
        assert c.post("/api/app/punch", data=form, files={"selfie": ("s.txt", b"hello", "text/plain")}, headers=h).status_code == 400

        # geofence: office at Mumbai, 150 m radius
        c.post("/api/sites", json={"name": "HQ", "lat": 19.0760, "lng": 72.8777, "radius_m": 150})
        assert len(c.get("/api/sites").json()) == 1
        far = {"lat": "28.6139", "lng": "77.2090", "accuracy": "10"}
        r = c.post("/api/app/punch", data=far, files={"selfie": ("s.jpg", JPEG, "image/jpeg")}, headers=h)
        assert r.status_code == 403 and "HQ" in r.json()["detail"]
        monkeypatch.setattr(config, "GEOFENCE_MODE", "flag")
        monkeypatch.setattr(config, "DEBOUNCE_SECONDS", 0)
        import time
        time.sleep(1.1)  # punches in the same second would be exact duplicates
        r = c.post("/api/app/punch", data=far, files={"selfie": ("s.jpg", JPEG, "image/jpeg")}, headers=h)
        assert r.status_code == 200 and r.json()["flag"] == "outside_geofence"

        from app.timeutil import now_local
        day = now_local().strftime("%Y-%m-%d")
        punches = [p for p in c.get(f"/api/punches?day={day}").json() if p["device_user_id"] == "E1"]
        assert len(punches) == 2 and punches[0]["flag"] == "outside_geofence" and punches[0]["selfie"]
        # admin can fetch the selfie; path traversal is refused
        ym, fn = punches[0]["selfie"].split("/")
        assert c.get(f"/api/selfies/{ym}/{fn}").content == JPEG
        assert c.get(f"/api/selfies/{ym}/..%2F..%2Fetc%2Fpasswd").status_code == 404
        # rejecting a punch removes it from the report
        before = [x for x in c.get(f"/api/reports/daily?day={day}").json() if x["name"] == "Eve"][0]["punches"]
        c.post(f"/api/punches/{punches[0]['id']}/review", json={"flag": "rejected"})
        after = [x for x in c.get(f"/api/reports/daily?day={day}").json() if x["name"] == "Eve"][0]["punches"]
        assert (before, after) == (2, 1)
        assert len(c.get("/api/app/me", headers=h).json()["today"]) == 1


def test_login_lockout():
    with TestClient(app) as c:
        c.post("/api/employees", json={"device_user_id": "L1", "name": "Lock"})
        c.post("/api/employees/L1/pin", json={"pin": "4321"})
        for _ in range(5):
            assert _login(c, "L1", "0000")[0].status_code == 401
        assert _login(c, "L1", "4321")[0].status_code == 429


def test_port80_browser_redirect(monkeypatch):
    import socket
    from app import fkweb
    monkeypatch.setattr(config, "PUBLIC_DOMAIN", "att.example.com")
    with TestClient(app):
        s = socket.create_connection(("127.0.0.1", fkweb.bound_port), timeout=5)
        s.sendall(b"GET /m/ HTTP/1.1\r\nHost: att.example.com\r\n\r\n")
        out = s.recv(300).decode()
        s.close()
    assert out.startswith("HTTP/1.1 301") and "Location: https://att.example.com/m/" in out


def test_devices_registry_and_filter(tmp_path, monkeypatch):
    import socket
    from app import fkweb
    monkeypatch.setattr(config, "RAW_LOG", str(tmp_path / "raw.log"))

    def post(port, code, dev, body):
        raw = (f"POST http://x HTTP/1.0\r\nrequest_code: {code}\r\ndev_id: {dev}\r\nContent-Length: {len(body) + 4}\r\n\r\n"
               ).encode() + len(body).to_bytes(4, "little") + body
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(raw)
        s.recv(300)
        s.close()

    with TestClient(app) as c:
        port = fkweb.bound_port
        post(port, "receive_cmd", "DEV-A", b'{"fk_name":"M61BH","fk_info":{"firmware":"v3"}}')
        post(port, "realtime_glog", "DEV-A", b'{"user_id":"A1","io_time":"20250106090000"}')
        post(port, "realtime_glog", "DEV-B", b'{"user_id":"B1","io_time":"20250106090100"}')
        devs = {d["dev_id"]: d for d in c.get("/api/devices").json()}
        assert devs["DEV-A"]["model"] == "M61BH" and devs["DEV-A"]["firmware"] == "v3"
        assert devs["DEV-A"]["online"] is True and "DEV-B" in devs
        assert c.post("/api/devices/DEV-A/info", json={"name": "Mumbai Office", "location": "Mumbai"}).status_code == 200
        assert c.post("/api/devices/NOPE/info", json={"name": "x"}).status_code == 404
        # per-device filtering of punches and reports
        c.post("/api/employees", json={"device_user_id": "A1", "name": "Ann"})
        c.post("/api/employees", json={"device_user_id": "B1", "name": "Bob"})
        assert [p["device_user_id"] for p in c.get("/api/punches?day=2025-01-06&device=DEV-A").json()] == ["A1"]
        rows = {r["name"]: r["status"] for r in c.get("/api/reports/daily?day=2025-01-06&device=DEV-B").json()}
        assert rows["Bob"] != "Absent" and rows["Ann"] == "Absent"
        # stale heartbeat -> offline
        db_conn = __import__("app.db", fromlist=["conn"]).conn()
        db_conn.execute("UPDATE devices SET last_seen='2020-01-01 00:00:00' WHERE dev_id='DEV-B'")
        db_conn.commit()
        assert {d["dev_id"]: d["online"] for d in c.get("/api/devices").json()}["DEV-B"] is False
        # allow-list: unknown devices are neither registered nor allowed to punch
        monkeypatch.setattr(config, "FKWEB_DEVICES", ["DEV-A"])
        post(port, "realtime_glog", "DEV-EVIL", b'{"user_id":"E1","io_time":"20250106100000"}')
        assert "DEV-EVIL" not in {d["dev_id"] for d in c.get("/api/devices").json()}
        assert not [p for p in c.get("/api/punches?day=2025-01-06").json() if p["device_user_id"] == "E1"]


def test_dashboard_summary(monkeypatch):
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "Dash 9-18", "start": "09:00", "end": "18:00"})
        sid = [x for x in c.get("/api/shifts").json() if x["name"] == "Dash 9-18"][0]["id"]
        for uid, nm, dp in (("D1", "Dee", "HR"), ("D2", "Dan", "IT"), ("D3", "Dot", "IT")):
            c.post("/api/employees", json={"device_user_id": uid, "name": nm, "department": dp, "shift_id": sid})
        c.post("/api/punches", json={"device_user_id": "D1", "ts": "2025-02-03 08:50:00"})
        c.post("/api/punches", json={"device_user_id": "D2", "ts": "2025-02-03 10:30:00"})
        d = c.get("/api/dashboard?day=2025-02-03").json()
        assert (d["present"], d["late"]) == (2, 1)
        assert d["absent"] == d["total"] - 2
        assert {"name": "HR", "present": 1} in d["departments"] and {"name": "IT", "present": 1} in d["departments"]
        assert isinstance(d["devices"], list) and isinstance(d["gps_punches"], list)


def test_shift_engine_holidays_leave_payroll():
    """Different shifts per person; late/half-day/overtime; leave, holiday, weekly off; payroll."""
    with TestClient(app) as c:
        mk = lambda n, st, en, **k: c.post("/api/shifts", json={"name": n, "start": st, "end": en, **k}).status_code
        assert mk("Early", "07:00", "15:00") == 200 and mk("Late", "12:00", "20:00", weekly_off="SAT,SUN") == 200
        assert mk("Bad", "25:00", "15:00") == 400 and mk("Bad", "10:00", "09:00") == 400
        assert mk("Bad", "10:00", "11:00", weekly_off="FUNDAY") == 400
        sid = {x["name"]: x["id"] for x in c.get("/api/shifts").json()}
        for uid, nm, sh, sal in (("S1", "Early Bird", "Early", 31000), ("S2", "Night Owl", "Late", 31000), ("S3", "Flex", None, 0)):
            c.post("/api/employees", json={"device_user_id": uid, "name": nm, "shift_id": sid.get(sh), "salary": sal})
        c.post("/api/holidays", json={"date": "2025-06-03", "name": "Test Holiday"})   # Tuesday
        assert c.post("/api/holidays", json={"date": "2025-13-45", "name": "x"}).status_code == 400
        c.post("/api/leaves", json={"device_user_id": "S1", "start_date": "2025-06-04", "end_date": "2025-06-05", "type": "Sick"})
        assert c.post("/api/leaves", json={"device_user_id": "NOPE", "start_date": "2025-06-04", "end_date": "2025-06-04"}).status_code == 404
        P = lambda uid, ts: c.post("/api/punches", json={"device_user_id": uid, "ts": ts})
        # Mon 2 Jun: S1 on time, leaves late (overtime 1h); S2 (shift 12-20) arrives 12:05 = on time (grace); S3 flexible at 15:00 never late
        P("S1", "2025-06-02 07:05:00"); P("S1", "2025-06-02 16:00:00")
        P("S2", "2025-06-02 12:05:00"); P("S2", "2025-06-02 20:00:00")
        P("S3", "2025-06-02 15:00:00"); P("S3", "2025-06-02 19:00:00")
        # Fri 6 Jun: S1 arrives 08:00 (60 min late), leaves 10:00 (2 h = half day)
        P("S1", "2025-06-06 08:00:00"); P("S1", "2025-06-06 10:00:00")
        d = {r["device_user_id"]: r for r in c.get("/api/reports/daily?day=2025-06-02").json()}
        assert d["S1"]["status"] == "Present" and d["S1"]["ot_min"] == 60 and d["S1"]["early_min"] == 0
        assert d["S2"]["status"] == "Present" and d["S3"]["status"] == "Present" and d["S3"]["late_min"] == 0
        d = {r["device_user_id"]: r for r in c.get("/api/reports/daily?day=2025-06-06").json()}
        assert d["S1"]["status"] == "Half Day" and d["S1"]["late_min"] == 60 and d["S1"]["early_min"] == 300
        assert d["S2"]["status"] == "Absent" and d["S3"]["status"] == "Absent"
        st = lambda day: {r["device_user_id"]: r["status"] for r in c.get("/api/reports/daily?day=" + day).json()}
        assert st("2025-06-03")["S1"] == "Holiday" and st("2025-06-04")["S1"] == "Leave" and st("2025-06-01")["S1"] == "Weekly Off"
        assert st("2025-06-07")["S2"] == "Weekly Off" and st("2025-06-07")["S1"] == "Absent"   # Saturday: S2 off, S1 not
        # monthly + grid + payroll (June 2025 has 30 days)
        m = {r["device_user_id"]: r for r in c.get("/api/reports/monthly?month=2025-06").json()}
        s1 = m["S1"]
        assert (s1["days_present"], s1["half_days"], s1["leave"], s1["holidays"], s1["overtime_hours"]) == (2, 1, 2, 1, 1.0)
        g = {r["device_user_id"]: r["codes"] for r in c.get("/api/reports/grid?month=2025-06").json()["rows"]}
        assert g["S1"][0] == "WO" and g["S1"][1] == "P" and g["S1"][2] == "H" and g["S1"][3] == "LV" and g["S1"][5] == "HD"
        pay = {r["device_user_id"]: r for r in c.get("/api/reports/payroll?month=2025-06").json()}
        # S1: 1 full + 0.5 half + 2 paid leave + 1 holiday + 4 Sundays (1,8,15,22,29 = 5) = 9.5 paid days
        assert pay["S1"]["paid_days"] == 1 + 0.5 + 2 + 1 + 5 and pay["S1"]["net_pay"] == round(31000 / 30 * 9.5, 2)
        assert "net_pay" in c.get("/api/reports/payroll.csv?month=2025-06").text
        # deleting a shift leaves the employee flexible instead of breaking
        c.delete(f"/api/shifts/{sid['Early']}")
        assert [e for e in c.get("/api/employees").json() if e["device_user_id"] == "S1"][0]["shift_id"] is None


def test_joining_date_excludes_earlier_days():
    with TestClient(app) as c:
        assert c.post("/api/employees", json={"device_user_id": "J1", "name": "Joiner", "joined": "2025-09-15"}).status_code == 200
        assert c.post("/api/employees", json={"device_user_id": "J2", "name": "Bad", "joined": "soon"}).status_code == 400
        before = {r["device_user_id"]: r["status"] for r in c.get("/api/reports/daily?day=2025-09-10").json()}
        after = {r["device_user_id"]: r["status"] for r in c.get("/api/reports/daily?day=2025-09-16").json()}
        assert before["J1"] == "" and after["J1"] == "Absent"
        assert [e for e in c.get("/api/employees").json() if e["device_user_id"] == "J1"][0]["joined"] == "2025-09-15"
