import os
import tempfile

os.environ["FKWEB_PORT"] = "0"
os.environ["BACKUP_HOUR"] = "-1"          # no nightly backup or reminder loops during tests
os.environ["PUSH_REMINDERS"] = "0"
os.environ["LATE_EMAILS"] = "0"           # no late-arrival e-mail loop either
os.environ["WEEKLY_REPORT"] = "0"         # nor the weekly report
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
        assert [p["device_user_id"] for p in punches] == ["217228"]   # leading zeros ignored
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
        c.post("/api/leaves", json={"device_user_id": "S1", "start_date": "2025-06-04", "end_date": "2025-06-05", "type": "Sick", "override": True})
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
        assert g["S1"][0] == "WO" and g["S1"][1] == "P" and g["S1"][2] == "H" and g["S1"][3] == "AL" and g["S1"][5] == "HD"   # Adhoc leave
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


def test_leading_zero_ids_are_the_same_person():
    from app import db as appdb
    assert [appdb.canon_id(x) for x in ("00217315", "217315", " 007 ", "0", "000", "AB-01", "")] == ["217315", "217315", "7", "0", "0", "AB-01", ""]
    with TestClient(app) as c:
        c.post("/api/employees", json={"device_user_id": "00555", "name": "Zed"})
        assert [e["device_user_id"] for e in c.get("/api/employees").json() if e["name"] == "Zed"] == ["555"]
        c.post("/api/employees/0555/pin", json={"pin": "2468"})                      # any zero-padding works
        assert c.post("/api/app/login", json={"employee_id": "00000555", "pin": "2468"}).status_code == 200
        c.post("/api/punches", json={"device_user_id": "0000555", "ts": "2025-04-07 09:00:00"})
        assert [p["device_user_id"] for p in c.get("/api/punches?day=2025-04-07").json() if p["name"] == "Zed"] == ["555"]


def test_duplicate_ids_are_merged_on_startup():
    """Data created before the rule (or by the vendor import) with 217315 and 00217315 as two people."""
    from app import db as appdb
    c = appdb.conn()
    c.execute("INSERT INTO employees(device_user_id,name,department) VALUES ('00777','Twin Old','Ops')")
    c.execute("INSERT INTO employees(device_user_id,name,department,pin_hash) VALUES ('777','Twin New','Ops','h$h')")
    c.execute("INSERT INTO employees(device_user_id,name) VALUES ('00888','Solo')")
    c.execute("INSERT INTO punches(device_user_id,ts,source) VALUES ('00777','2025-05-05 09:00:00','d1'),('777','2025-05-05 09:00:00','d1'),('777','2025-05-05 17:00:00','d1')")
    c.execute("INSERT INTO leaves(device_user_id,start_date,end_date) VALUES ('00777','2025-05-06','2025-05-06')")
    c.commit()
    appdb.init()
    emps = {r["device_user_id"]: r["name"] for r in c.execute("SELECT * FROM employees WHERE device_user_id IN ('777','00777','888','00888')")}
    assert emps == {"777": "Twin New", "888": "Solo"}
    assert c.execute("SELECT COUNT(*) FROM punches WHERE device_user_id='777'").fetchone()[0] == 2   # the exact duplicate collapsed
    assert c.execute("SELECT COUNT(*) FROM punches WHERE device_user_id='00777'").fetchone()[0] == 0
    assert c.execute("SELECT device_user_id FROM leaves WHERE start_date='2025-05-06'").fetchone()[0] == "777"
    assert c.execute("SELECT pin_hash FROM employees WHERE device_user_id='777'").fetchone()[0] == "h$h"


def test_excel_exports_and_formula_injection_guard():
    import io as _io
    from openpyxl import load_workbook
    with TestClient(app) as c:
        c.post("/api/employees", json={"device_user_id": "X1", "name": "=HYPERLINK(\"http://evil\",\"x\")", "department": "+cmd"})
        for path in ("daily?day=2025-06-02", "monthly?month=2025-06", "grid?month=2025-06", "payroll?month=2025-06"):
            r = c.get(f"/api/reports/{path.split('?')[0]}.xlsx?" + path.split("?")[1])
            assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
            rows = list(load_workbook(_io.BytesIO(r.content)).active.iter_rows(values_only=True))
            assert len(rows) >= 2
            assert not any(isinstance(v, str) and v[:1] in "=+@" for row in rows for v in row)
        assert "'=HYPERLINK" in c.get("/api/reports/daily.csv?day=2025-06-02").text


def test_muster_timecard_and_grid_totals():
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "Muster 10-19", "start": "10:00", "end": "19:00"})
        sid = [x for x in c.get("/api/shifts").json() if x["name"] == "Muster 10-19"][0]["id"]
        c.post("/api/employees", json={"device_user_id": "M1", "name": "Mia", "shift_id": sid, "department": "Ops"})
        c.post("/api/punches", json={"device_user_id": "M1", "ts": "2025-08-04 10:30:00"})   # Mon, 30 min late
        c.post("/api/punches", json={"device_user_id": "M1", "ts": "2025-08-04 19:40:00"})
        row = [r for r in c.get("/api/reports/muster?month=2025-08").json() if r["Code"] == "M1"][0]
        assert row["04"] == "L" and row["03"] == "WO" and row["05"] == "A" and row["Worked Days"] == 1   # 3 Aug 2025 = Sunday
        assert row["W.Days Off"] == 5 and row["Total Salary Days"] == 1 + 5   # Aug 2025 has 5 Sundays
        tc = c.get("/api/reports/timecard?employee=M1&month=2025-08").json()
        assert len(tc) == 31 and tc[3]["in"] == "10:30:00" and tc[3]["late_min"] == 30 and tc[3]["overtime_min"] == 40
        assert tc[3]["schedule"] == "10:00-19:00" and tc[3]["day_type"] == "WORKDAY"
        assert (tc[2]["weekday"], tc[2]["day_type"], tc[2]["status"]) == ("Sun", "WEEKLY OFF", "Weekly Off")
        assert tc[4]["day_type"] == "WORKDAY" and tc[4]["status"] == "Absent"
        g = [r for r in c.get("/api/reports/grid?month=2025-08").json()["rows"] if r["device_user_id"] == "M1"][0]
        assert g["totals"]["worked"] == 1
        assert c.get("/api/reports/muster.xlsx?month=2025-08").status_code == 200
        assert c.get("/api/reports/timecard.xlsx?employee=0M1&month=2025-08").status_code == 200


def _csv_bytes(rows):
    import csv as _csv, io as _io
    b = _io.StringIO()
    _csv.writer(b).writerows(rows)
    return b.getvalue().encode()


def test_importers():
    import io as _io
    from openpyxl import Workbook
    with TestClient(app) as c:
        up = lambda kind, name, data, dry: c.post(f"/api/import/{kind}?dry_run={'true' if dry else 'false'}", files={"file": (name, data)})
        # shifts (vendor-style headers)
        sh = _csv_bytes([["Shift Name", "Shift Start", "Shift End", "Grace Min", "Weekly Off"],
                         ["Imp 10-19", "10:00", "19:00", "10", "SUN"], ["Imp Bad", "19:00", "10:00", ""], ["Imp PM", "2:00 PM", "10:00 PM", "", "SAT,SUN"]])
        r = up("shifts", "s.csv", sh, True).json()
        assert (r["created"], r["skipped"]) == (2, 1) and r["errors"][0]["row"] == 3 and "after start" in r["errors"][0]["message"]
        assert not [x for x in c.get("/api/shifts").json() if x["name"].startswith("Imp")]          # dry run wrote nothing
        assert up("shifts", "s.csv", sh, False).json()["created"] == 2
        assert {x["name"]: x["start"] for x in c.get("/api/shifts").json() if x["name"].startswith("Imp")} == {"Imp 10-19": "10:00", "Imp PM": "14:00"}
        # employees via .xlsx, zero-padded ids, unknown shift flagged, existing employee updated without losing its PIN
        c.post("/api/employees", json={"device_user_id": "9001", "name": "Old Name"})
        c.post("/api/employees/9001/pin", json={"pin": "1357"})
        wb = Workbook(); ws = wb.active
        ws.append(["Emp Code", "Employee Name", "Dept Name", "Shift Name", "Date of Joining"])
        ws.append(["00009001", "New Name", "Ops", "Imp 10-19", "01/04/2025"])
        ws.append(["00009002", "Second", "HR", "No Such Shift", ""])
        ws.append(["", "No Id", "HR", "", ""])
        buf = _io.BytesIO(); wb.save(buf)
        r = up("employees", "e.xlsx", buf.getvalue(), False).json()
        assert (r["created"], r["updated"], r["skipped"]) == (0, 1, 2) and {e["row"] for e in r["errors"]} == {3, 4}
        e = [x for x in c.get("/api/employees").json() if x["device_user_id"] == "9001"][0]
        assert (e["name"], e["department"], e["shift"], e["joined"], e["pin_hash"]) == ("New Name", "Ops", "Imp 10-19", "2025-04-01", 1)
        assert c.post("/api/app/login", json={"employee_id": "9001", "pin": "1357"}).status_code == 200
        # holidays + leaves (CL -> Informed) + duplicate leave skipped
        assert up("holidays", "h.csv", _csv_bytes([["Date", "Name"], ["15/08/2025", "Independence Day"]]), False).json()["created"] == 1
        lv = _csv_bytes([["Emp Code", "From Date", "To Date", "Leave Name", "Reason"], ["9001", "2025-08-11", "2025-08-12", "CL", "trip"], ["777777", "2025-08-11", "2025-08-11", "CL", ""]])
        r = up("leaves", "l.csv", lv, False).json()
        assert (r["created"], r["skipped"]) == (1, 1) and "no employee" in r["errors"][0]["message"]
        assert up("leaves", "l.csv", lv, False).json()["created"] == 0
        assert c.get("/api/leaves?month=2025-08").json()[0]["type"] == "Informed"
        # punches: datetime column and "date + several times in one cell"; replays are no-ops
        pc = _csv_bytes([["Card No", "Attendance DateTime"], ["00009001", "2025-08-18 09:58:20"], ["9001", "2025-08-18 18:30"]])
        assert up("punches", "p.csv", pc, False).json()["created"] == 2
        assert up("punches", "p.csv", pc, False).json()["created"] == 0
        sw = _csv_bytes([["Emp Code", "Date", "Swipe Record"], ["9001", "19/08/2025", "09:58 13:00 18:30"]])
        r = up("punches", "p2.csv", sw, True).json()
        assert r["created"] == 3
        assert not [p for p in c.get("/api/punches?day=2025-08-19").json() if p["device_user_id"] == "9001"]     # dry run
        assert up("punches", "p2.csv", sw, False).json()["created"] == 3
        assert [p["method"] for p in c.get("/api/punches?day=2025-08-18").json() if p["device_user_id"] == "9001"] == ["import", "import"]
        # guards
        assert up("employees", "x.pdf", b"x", True).status_code == 400
        assert up("employees", "x.csv", b"id,name\n", True).status_code == 400
        assert up("nonsense", "x.csv", b"id,name\n1,a", True).status_code == 400
        assert up("employees", "x.xlsx", b"not really excel", True).status_code == 400
        t = c.get("/api/import/employees/template.csv")
        assert t.status_code == 200 and t.text.startswith("id,name,department") and c.get("/api/import/zzz/template.csv").status_code == 404


def test_logins_roles_scope_and_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "correct-horse-battery")
    H = {"X-Requested-With": "fetch"}
    with TestClient(app) as admin:
        admin.headers.update(H)
        # locked down from the first request: no login, no data
        anon = TestClient(app)
        assert anon.get("/api/employees").status_code == 401 and anon.get("/api/dashboard").status_code == 401
        assert anon.get("/", follow_redirects=False).headers["location"] == "/login"
        assert anon.get("/login").status_code == 200 and anon.get("/healthz").status_code == 200
        with __import__("pytest").raises(Exception):
            anon.websocket_connect("/ws").__enter__()
        # login: wrong password, success, session cookie
        assert admin.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
        r = admin.post("/api/auth/login", json={"username": "ADMIN", "password": "correct-horse-battery"})
        assert r.status_code == 200 and r.json()["role"] == "admin" and "httponly" in r.headers["set-cookie"].lower()
        assert "samesite=strict" in r.headers["set-cookie"].lower()
        assert admin.get("/", follow_redirects=False).status_code == 200
        # CSRF guard: a state-changing request without the custom header is refused
        bare = TestClient(app, cookies=admin.cookies)
        assert bare.post("/api/employees", json={"device_user_id": "1", "name": "x"}).status_code == 403
        # employee-app tokens are not admin sessions
        fake = TestClient(app, cookies={access_cookie(): auth_token_for_employee()})
        assert fake.get("/api/employees").status_code == 401

        # create users
        mk = lambda **k: admin.post("/api/users", json=k)
        assert mk(username="hr1", password="hr-password-1", role="hr").status_code == 200
        assert mk(username="mgr1", password="mgr-password-1", role="manager", department="Ops").status_code == 200
        assert mk(username="mgr2", password="mgr-password-2", role="manager").status_code == 400        # needs a department
        assert mk(username="x", password="short", role="hr").status_code == 400
        assert mk(username="bad name", password="longenough1", role="hr").status_code == 400
        assert mk(username="nopass", role="hr").status_code == 400
        # data: Ops and Sales staff, with salaries
        for uid, nm, dp in (("A1", "Ann Ops", "Ops"), ("B1", "Bob Sales", "Sales")):
            admin.post("/api/employees", json={"device_user_id": uid, "name": nm, "department": dp, "salary": 50000})
            admin.post("/api/punches", json={"device_user_id": uid, "ts": "2025-09-01 09:00:00"})

        def login(user, pw):
            cl = TestClient(app)
            cl.headers.update(H)
            assert cl.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200
            return cl

        hr, mgr = login("hr1", "hr-password-1"), login("mgr1", "mgr-password-1")
        # hr: day-to-day work yes; users / audit / sites / devices no
        assert hr.post("/api/employees", json={"device_user_id": "C1", "name": "Cy", "department": "Ops"}).status_code == 200
        assert hr.get("/api/reports/payroll?month=2025-09").status_code == 200
        assert hr.get("/api/users").status_code == 403 and hr.get("/api/audit").status_code == 403
        assert hr.post("/api/sites", json={"name": "s", "lat": 1, "lng": 1}).status_code == 403
        assert hr.post("/api/users", json={"username": "evil", "password": "evil-password", "role": "admin"}).status_code == 403
        # manager: read-only, own department only, no salaries, no payroll
        emps = mgr.get("/api/employees").json()
        assert {e["device_user_id"] for e in emps} == {"A1", "C1"} and all(e["salary"] == 0 for e in emps)
        assert [r["device_user_id"] for r in mgr.get("/api/reports/daily?day=2025-09-01").json() if r["status"] == "Present"] == ["A1"]
        assert {p["device_user_id"] for p in mgr.get("/api/punches?day=2025-09-01").json()} == {"A1"}
        assert {r["device_user_id"] for r in mgr.get("/api/reports/monthly?month=2025-09").json()} == {"A1", "C1"}
        assert {r["Code"] for r in mgr.get("/api/reports/muster?month=2025-09").json()} == {"A1", "C1"}
        assert mgr.get("/api/reports/payroll?month=2025-09").status_code == 403
        assert mgr.post("/api/employees", json={"device_user_id": "Z", "name": "z"}).status_code == 403
        assert mgr.post("/api/punches", json={"device_user_id": "A1", "ts": "2025-09-02 09:00:00"}).status_code == 200   # own team
        assert mgr.post("/api/punches", json={"device_user_id": "B1", "ts": "2025-09-02 09:00:00"}).status_code == 403   # not their team
        assert mgr.get("/api/audit").status_code == 403 and mgr.get("/api/import/employees/template.csv").status_code == 403
        assert mgr.get("/api/dashboard").json()["total"] == 2
        with __import__("pytest").raises(Exception):
            mgr.websocket_connect("/ws").__enter__()
        # last-admin and self-delete protection
        assert admin.delete("/api/users/admin").status_code == 400
        assert mk(username="admin", role="hr", password="").status_code == 400
        assert admin.delete("/api/users/hr1").status_code == 200 and hr.get("/api/employees").status_code == 401
        # password change
        assert mgr.post("/api/auth/password", json={"old_password": "wrong", "new_password": "brand-new-pass"}).status_code == 403
        assert mgr.post("/api/auth/password", json={"old_password": "mgr-password-1", "new_password": "brand-new-pass"}).status_code == 200
        assert TestClient(app).post("/api/auth/login", json={"username": "mgr1", "password": "mgr-password-1"}).status_code == 401
        # audit: who did what, never bodies/passwords; failed logins recorded
        log = admin.get("/api/audit").json()
        acts = [(a["user"], a["action"], a["status"]) for a in log]
        assert ("admin", "POST /api/users", 200) in acts and ("hr1", "POST /api/employees", 200) in acts
        assert ("mgr1", "POST /api/employees", 403) in acts and ("admin", "LOGIN failed", 401) in acts
        assert "correct-horse" not in str(log) and "password" not in " ".join(a["action"] for a in log).replace("/api/auth/password", "")
        # login lockout
        spam = TestClient(app)
        for _ in range(5):
            spam.post("/api/auth/login", json={"username": "ghost", "password": "x"})
        assert spam.post("/api/auth/login", json={"username": "ghost", "password": "x"}).status_code == 429
        # logout kills the session
        admin.post("/api/auth/logout")
        admin.cookies.clear()
        assert admin.get("/api/employees").status_code == 401


def access_cookie():
    from app import access
    return access.COOKIE


def auth_token_for_employee():
    from app import auth
    return auth.make_token("A1", "emp")


def test_require_login_refuses_open_hosting(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "open.db"))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "REQUIRE_LOGIN", True)
    with __import__("pytest").raises(RuntimeError, match="ADMIN_PASSWORD"):
        with TestClient(app):
            pass


def test_management_is_admin_level_and_bulk_shift_and_new_employee_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "mgmt.db"))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin-password-123")
    H = {"X-Requested-With": "fetch"}
    with TestClient(app) as admin:
        admin.headers.update(H)
        admin.post("/api/auth/login", json={"username": "admin", "password": "admin-password-123"})
        admin.post("/api/users", json={"username": "boss", "password": "boss-password-1", "role": "management"})
        admin.post("/api/users", json={"username": "lead", "password": "lead-password-1", "role": "manager", "department": "Ops"})
        admin.post("/api/shifts", json={"name": "Bulk 9-6", "start": "09:00", "end": "18:00"})
        sid = admin.get("/api/shifts").json()[0]["id"]
        for uid, dp in (("1", "Ops"), ("2", "Sales"), ("3", "Ops")):
            admin.post("/api/employees", json={"device_user_id": uid, "name": f"E{uid}", "department": dp, "salary": 40000,
                                               "designation": "Exec", "branch": "Mumbai", "mobile": "99999", "father_name": "Dad"})
        # bulk assign by id (zero-padded ids work, unknown shift and empty selection are rejected)
        assert admin.post("/api/employees/bulk-shift", json={"ids": ["001", "2", "3"], "shift_id": sid}).json() == {"updated": 3}
        assert admin.post("/api/employees/bulk-shift", json={"ids": ["1"], "shift_id": 99999}).status_code == 404
        assert admin.post("/api/employees/bulk-shift", json={"ids": [], "shift_id": sid}).status_code == 400
        e = {x["device_user_id"]: x for x in admin.get("/api/employees").json()}
        assert e["1"]["shift"] == "Bulk 9-6" and e["1"]["designation"] == "Exec" and e["1"]["father_name"] == "Dad" and e["1"]["salary"] == 40000
        assert admin.post("/api/employees/bulk-shift", json={"ids": ["2"], "shift_id": None}).json() == {"updated": 1}
        # management has admin power: sees everything including salaries, can run payroll and manage users
        boss = TestClient(app); boss.headers.update(H)
        assert boss.post("/api/auth/login", json={"username": "boss", "password": "boss-password-1"}).status_code == 200
        emps = boss.get("/api/employees").json()
        assert {x["device_user_id"] for x in emps} == {"1", "2", "3"} and all(x["salary"] == 40000 for x in emps)
        assert boss.get("/api/reports/payroll?month=2025-09").status_code == 200
        assert boss.post("/api/employees/bulk-shift", json={"ids": ["1"], "shift_id": sid}).status_code == 200
        assert boss.get("/api/dashboard").json()["total"] == 3
        assert boss.get("/api/users").status_code == 200 and boss.get("/api/audit").status_code == 200
        # team lead: own department only
        lead = TestClient(app); lead.headers.update(H)
        lead.post("/api/auth/login", json={"username": "lead", "password": "lead-password-1"})
        assert {x["device_user_id"] for x in lead.get("/api/employees").json()} == {"1", "3"}
        # muster has the statutory columns; resigned import marks inactive
        row = admin.get("/api/reports/muster?month=2025-09").json()[0]
        assert row["Father's Name"] == "Dad" and row["Designation"] == "Exec"
        csv_bytes = b"Emp Code,Empname,Dept Name,Desig Name,Branch Name,Phone.No,Father Name\n00077,Gone Gina,Ops,Clerk,Pune,12345,Papa\n"
        r = admin.post("/api/import/employees?dry_run=false&inactive=true", files={"file": ("old.csv", csv_bytes)}).json()
        assert r["created"] == 1
        gina = [x for x in admin.get("/api/employees").json() if x["device_user_id"] == "77"][0]
        assert (gina["active"], gina["designation"], gina["branch"], gina["mobile"]) == (0, "Clerk", "Pune", "12345")
        assert "77" not in {x["device_user_id"] for x in admin.get("/api/reports/monthly?month=2025-09").json()}


def test_payslips_publish_adjustments_and_self_service(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "pay.db"))
    monkeypatch.setattr(config, "COMPANY_NAME", "Test Travels Pvt Ltd")
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "Pay 9-6", "start": "09:00", "end": "18:00"})
        sid = c.get("/api/shifts").json()[0]["id"]
        c.post("/api/employees", json={"device_user_id": "P1", "name": "Priya", "shift_id": sid, "salary": 30000, "designation": "Exec"})
        c.post("/api/employees", json={"device_user_id": "P2", "name": "NoSalary", "shift_id": sid})
        for uid in ("P1", "P2"):
            c.post(f"/api/employees/{uid}/pin", json={"pin": "1234"})
        # June 2025 (30 days, 5 Sundays): present Mon 2 Jun only, rest absent -> paid = 1 + 5 Sundays = 6 days
        # (Sun 1 Jun is before the first punch in the system, but a weekly off is still paid)
        c.post("/api/punches", json={"device_user_id": "P1", "ts": "2025-06-02 09:00:00"})
        c.post("/api/punches", json={"device_user_id": "P1", "ts": "2025-06-02 18:00:00"})
        row = [r for r in c.get("/api/reports/payroll?month=2025-06").json() if r["device_user_id"] == "P1"][0]
        assert row["paid_days"] == 6 and row["net_pay"] == 6000.0
        # adjustments: bonus +500, advance -200 -> net 6300; validation
        post = lambda **k: c.post("/api/payroll/adjustments", json={"month": "2025-06", "device_user_id": "P1", **k})
        assert post(label="Bonus", amount=500).status_code == 200 and post(label="Advance", amount=-200).status_code == 200
        assert post(label="", amount=5).status_code == 400 and post(label="x", amount=0).status_code == 400
        assert c.post("/api/payroll/adjustments", json={"month": "2025-6", "device_user_id": "P1", "label": "x", "amount": 1}).status_code == 400
        assert c.post("/api/payroll/adjustments", json={"month": "2025-06", "device_user_id": "NOPE", "label": "x", "amount": 1}).status_code == 404
        d = c.get("/api/payslips/P1/2025-06.pdf")
        assert d.status_code == 200 and d.content.startswith(b"%PDF") and b"Test Travels Pvt Ltd" in d.content and b"Bonus" in d.content
        slip = __import__("app.reports", fromlist=["payslip"]).payslip("2025-06", "P1")
        assert slip["net_pay"] == 6300.0 and slip["gross"] == 30500.0 and slip["total_deductions"] == 24200.0
        # employees see nothing until HR publishes, and only a finished month can be published
        tok = c.post("/api/app/login", json={"employee_id": "P1", "pin": "1234"}).json()["token"]
        auth = {"Authorization": "Bearer " + tok}
        assert c.get("/api/app/payslips", headers=auth).json() == []
        assert c.get("/api/app/payslip/2025-06.pdf", headers=auth).status_code == 404
        from app.timeutil import now_local
        assert c.post(f"/api/payroll/{now_local().strftime('%Y-%m')}/publish").status_code == 400
        assert c.post("/api/payroll/2025-06/publish").status_code == 200
        assert c.get("/api/payroll/2025-06/status").json()["published"] == 1
        assert c.get("/api/app/payslips", headers=auth).json() == [{"month": "2025-06", "net_pay": 6300.0}]
        mine = c.get("/api/app/payslip/2025-06.pdf", headers=auth)
        assert mine.status_code == 200 and mine.content.startswith(b"%PDF") and b"Priya" in mine.content
        # a published month is frozen; an employee can never fetch someone else's slip or a month that is not published
        assert post(label="Late fix", amount=50).status_code == 400
        assert c.get("/api/app/payslip/2025-05.pdf", headers=auth).status_code == 404
        tok2 = c.post("/api/app/login", json={"employee_id": "P2", "pin": "1234"}).json()["token"]
        assert c.get("/api/app/payslips", headers={"Authorization": "Bearer " + tok2}).json() == []       # no salary set
        assert c.get("/api/app/payslip/2025-06.pdf", headers={"Authorization": "Bearer " + tok2}).status_code == 404
        assert c.get("/api/app/payslips").status_code == 401
        # unpublish hides it again and unfreezes edits
        c.post("/api/payroll/2025-06/unpublish")
        assert c.get("/api/app/payslips", headers=auth).json() == [] and post(label="Late fix", amount=50).status_code == 200


def test_team_lead_powers_manual_grants_shift_changes_and_punch_fixes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "tl.db"))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin-password-123")
    H = {"X-Requested-With": "fetch"}

    def login(u, p):
        cl = TestClient(app); cl.headers.update(H)
        assert cl.post("/api/auth/login", json={"username": u, "password": p}).status_code == 200
        return cl

    with TestClient(app) as admin:
        admin.headers.update(H)
        admin.post("/api/auth/login", json={"username": "admin", "password": "admin-password-123"})
        mk = lambda **k: admin.post("/api/users", json=k)
        assert mk(username="lead", password="lead-password-1", role="manager", department="Ops").status_code == 200
        assert mk(username="lead2", password="lead-password-2", role="manager", department="Ops", permissions="leave,employees").status_code == 200
        assert mk(username="lead3", password="lead-password-3", role="manager", department="Ops", permissions="payroll_edit").status_code == 400   # not grantable
        assert mk(username="lead4", password="lead-password-4", role="manager", department="Ops", permissions="import").status_code == 400
        assert mk(username="hrx", password="hr-password-1x", role="hr", permissions="employees,leave").status_code == 200       # HR with abilities removed
        assert mk(username="hry", password="hr-password-2y", role="hr", permissions="sites").status_code == 200                 # HR given an extra
        assert mk(username="hrz", password="hr-password-3z", role="hr", permissions="bogus").status_code == 400
        admin.post("/api/shifts", json={"name": "Early 7-4", "start": "07:00", "end": "16:00"})
        admin.post("/api/shifts", json={"name": "Late 12-9", "start": "12:00", "end": "21:00"})
        sh = {x["name"]: x["id"] for x in admin.get("/api/shifts").json()}
        for uid, dp in (("1", "Ops"), ("2", "Ops"), ("3", "Sales")):
            admin.post("/api/employees", json={"device_user_id": uid, "name": f"E{uid}", "department": dp, "shift_id": sh["Early 7-4"]})
        lead, lead2 = login("lead", "lead-password-1"), login("lead2", "lead-password-2")
        hrx, hry = login("hrx", "hr-password-1x"), login("hry", "hr-password-2y")

        # --- team lead: shift change for own team only, new shifts yes, editing shared shifts no
        r = lead.post("/api/shift-changes", json={"ids": ["1", "2"], "start_date": "2025-07-07", "end_date": "2025-07-08", "shift_id": sh["Late 12-9"]})
        assert r.status_code == 200 and r.json() == {"created": 2}
        assert lead.post("/api/shift-changes", json={"ids": ["3"], "start_date": "2025-07-07", "end_date": "2025-07-08", "shift_id": sh["Late 12-9"]}).status_code == 403
        assert lead.post("/api/shifts", json={"name": "Lead's own 10-7", "start": "10:00", "end": "19:00"}).status_code == 200
        assert lead.post("/api/shifts", json={"id": sh["Early 7-4"], "name": "Hacked", "start": "01:00", "end": "02:00"}).status_code == 403
        assert lead.delete(f"/api/shifts/{sh['Early 7-4']}").status_code == 403
        assert lead.post("/api/employees/bulk-shift", json={"ids": ["1"], "shift_id": sh["Late 12-9"]}).status_code == 403
        assert {c["name"] for c in lead.get("/api/shift-changes?month=2025-07").json()} == {"E1", "E2"}
        # the engine uses the changed shift on those dates only: 13:00 arrival is late on the early shift, fine on the late shift
        for day in ("2025-07-07", "2025-07-09"):
            lead.post("/api/punches", json={"device_user_id": "1", "ts": f"{day} 12:05:00"})
            lead.post("/api/punches", json={"device_user_id": "1", "ts": f"{day} 21:00:00"})
        st = lambda d: {r["device_user_id"]: r for r in admin.get(f"/api/reports/daily?day={d}").json()}["1"]
        assert (st("2025-07-07")["status"], st("2025-07-07")["shift"]) == ("Present", "Late 12-9")
        assert (st("2025-07-09")["status"], st("2025-07-09")["shift"]) == ("Late", "Early 7-4")
        cid = lead.get("/api/shift-changes?month=2025-07").json()[0]["id"]
        assert lead.delete(f"/api/shift-changes/{cid}").status_code == 200

        # --- team lead: fix punch-in / punch-out for own team (day entry and single-punch correction)
        r = lead.post("/api/punches/day", json={"employee_id": "2", "date": "2025-07-10", "in_time": "09:00", "out_time": "18:30", "remark": "forgot to punch"})
        assert r.status_code == 200 and r.json() == {"stored": 2}
        assert lead.post("/api/punches/day", json={"employee_id": "3", "date": "2025-07-10", "in_time": "09:00"}).status_code == 403
        assert lead.post("/api/punches/day", json={"employee_id": "2", "date": "2025-07-10", "in_time": "9am"}).status_code == 400
        assert lead.post("/api/punches/day", json={"employee_id": "2", "date": "2025-07-10"}).status_code == 400
        assert lead.post("/api/punches/day", json={"employee_id": "2", "date": "2999-01-01", "in_time": "09:00"}).status_code == 400
        assert lead.post("/api/punches/day", json={"employee_id": "2", "date": "2025-07-10", "in_time": "09:00"}).status_code == 409   # already there
        pun = [p for p in admin.get("/api/punches?day=2025-07-10").json() if p["device_user_id"] == "2"]
        assert {p["note"] for p in pun} == {"forgot to punch"} and {p["method"] for p in pun} == {"manual"}
        out = [p for p in pun if p["ts"].endswith("18:30:00")][0]
        assert lead.post(f"/api/punches/{out['id']}/correct", json={"ts": "2025-07-10 17:45", "remark": "left early"}).status_code == 200
        assert lead.post(f"/api/punches/{out['id']}/correct", json={"ts": "garbage"}).status_code == 400
        row = [r for r in admin.get("/api/reports/daily?day=2025-07-10").json() if r["device_user_id"] == "2"][0]
        assert (row["first_in"], row["last_out"]) == ("09:00:00", "17:45:00")
        after = admin.get("/api/punches?day=2025-07-10").json()
        assert [p["flag"] for p in after if p["id"] == out["id"]] == ["rejected"]            # the old punch is kept, marked rejected
        assert any(p["note"].startswith("corrected from 18:30") for p in after)
        assert lead.post("/api/punches/99999/correct", json={"ts": "2025-07-10 10:00"}).status_code == 404

        # --- manual grants: lead2 can manage leave / employees, but only for the own team, and nothing beyond the grant
        assert lead2.post("/api/leaves", json={"device_user_id": "1", "start_date": "2025-07-14", "end_date": "2025-07-14", "type": "Informed", "override": True}).status_code == 200
        assert lead2.post("/api/leaves", json={"device_user_id": "3", "start_date": "2025-07-14", "end_date": "2025-07-14", "type": "Informed", "override": True}).status_code == 403
        assert lead.post("/api/leaves", json={"device_user_id": "1", "start_date": "2025-07-15", "end_date": "2025-07-15", "type": "Informed", "override": True}).status_code == 403   # no grant
        assert lead2.post("/api/payroll/adjustments", json={"month": "2025-07", "device_user_id": "1", "label": "x", "amount": 1}).status_code == 403
        assert lead2.get("/api/reports/payroll?month=2025-07").status_code == 403           # payroll view not granted
        assert lead2.post("/api/import/employees?dry_run=true", files={"file": ("x.csv", b"id,name\n1,a")}).status_code == 403
        assert lead2.post("/api/sites", json={"name": "s", "lat": 1, "lng": 1}).status_code == 403
        # employees grant: allowed inside the team, refused outside it
        assert lead2.post("/api/employees", json={"device_user_id": "1", "name": "Renamed", "department": "Ops"}).status_code == 200
        assert lead2.post("/api/employees", json={"device_user_id": "3", "name": "Nope", "department": "Sales"}).status_code == 403
        assert lead2.post("/api/employees", json={"device_user_id": "9", "name": "Sneaky", "department": "Sales"}).status_code == 403
        assert lead2.post("/api/employees", json={"device_user_id": "1", "name": "Moved", "department": "Sales"}).status_code == 403   # cannot move out
        assert lead2.post("/api/employees/bulk-shift", json={"ids": ["1", "3"], "shift_id": sh["Late 12-9"]}).status_code == 403

        # --- HR with abilities removed / added
        assert hrx.post("/api/employees", json={"device_user_id": "7", "name": "Seven", "department": "Ops"}).status_code == 200
        assert hrx.post("/api/holidays", json={"date": "2025-08-15", "name": "x"}).status_code == 403        # holidays removed
        assert hrx.get("/api/reports/payroll?month=2025-07").status_code == 403                               # payroll removed
        assert hrx.post("/api/punches/day", json={"employee_id": "1", "date": "2025-07-10", "in_time": "09:00"}).status_code == 403   # punches removed
        assert hry.post("/api/sites", json={"name": "HQ", "lat": 1.0, "lng": 1.0}).status_code == 200        # extra granted... 
        assert hry.get("/api/reports/payroll?month=2025-07").status_code == 403                                # ...and an explicit list means only that


def _freeze_today(monkeypatch, y=2026, m=10, d=10):
    from datetime import datetime as _dt
    fixed = lambda: _dt(y, m, d, 12, 0, 0)
    for mod in ("app.app_api", "app.main", "app.reports", "app.lates"):
        monkeypatch.setattr(f"{mod}.now_local", fixed)


def test_leave_policy_informed_adhoc_credits_and_workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "leave.db"))
    _freeze_today(monkeypatch)                                   # Saturday 10 Oct 2026
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "Lv 9-6", "start": "09:00", "end": "18:00"})            # Sunday off
        sid = c.get("/api/shifts").json()[0]["id"]
        # LV1 eligible since 5 Apr 2026 (whole year credited); LV2 eligible 1 Nov 2026; LV3 eligible 15 Sep 2026
        c.post("/api/employees", json={"device_user_id": "LV1", "name": "Eli", "shift_id": sid, "joined": "2026-01-05", "department": "Ops", "salary": 31000})
        c.post("/api/employees", json={"device_user_id": "LV2", "name": "New Nina", "shift_id": sid, "joined": "2026-08-01", "department": "Ops"})
        c.post("/api/employees", json={"device_user_id": "LV3", "name": "Tara", "shift_id": sid, "joined": "2026-06-15", "department": "Ops", "salary": 31000})
        c.post("/api/holidays", json={"date": "2026-10-21", "name": "Test holiday"})                  # a Wednesday
        for uid in ("LV1", "LV2", "LV3"):
            c.post(f"/api/employees/{uid}/pin", json={"pin": "1234"})
        tok = lambda uid: {"Authorization": "Bearer " + c.post("/api/app/login", json={"employee_id": uid, "pin": "1234"}).json()["token"]}
        h1, h2, h3 = tok("LV1"), tok("LV2"), tok("LV3")
        apply = lambda h, **k: c.post("/api/app/leave", json=k, headers=h)
        preview = lambda h, **k: c.get("/api/app/leave/preview", params=k, headers=h)

        # credits: quarter's first month 1 Informed + 1 Adhoc, other months 2 Informed, carried until March
        b = c.get("/api/app/leave-balance", headers=h1).json()          # Apr..Oct = 1+2+2+1+2+2+1 Informed, 3 Adhoc
        assert b["eligible_from"] == "2026-04-05" and b["eligible"] is True and b["fy"] == "2026-27"
        assert (b["informed"]["left"], b["adhoc"]["left"], b["year"]["credited"], b["year"]["entitlement"]) == (11, 3, 14, 24)
        assert b["next_credit"] == {"date": "2026-11-01", "informed": 2, "adhoc": 0}
        b = c.get("/api/app/leave-balance", headers=h3).json()          # eligible in September: Sep 2 + Oct 1, Oct 1 Adhoc
        assert (b["informed"]["credited"], b["informed"]["left"], b["adhoc"]["left"]) == (3, 3, 1)
        # Informed without 7 days' notice counts as Adhoc
        p = preview(h3, start_date="2026-10-14", end_date="2026-10-14", type="Informed").json()
        assert (p["informed_days"], p["adhoc_days"], p["excess_days"], p["needs_consent"]) == (0, 1, 0, False)
        assert "7 days' notice" in p["warnings"][0]
        r = apply(h3, start_date="2026-10-19", end_date="2026-10-20", type="Informed", reason="trip")
        assert r.status_code == 200 and (r.json()["days"], r.json()["adhoc_days"], r.json()["excess_days"]) == (2, 0, 0)
        # Thu 22 + Fri 23 (Wed 21 is a holiday): 1 Informed left, so the 2nd day uses the Adhoc credit
        p = preview(h3, start_date="2026-10-21", end_date="2026-10-23", type="Informed").json()
        assert (p["working_days"], p["informed_days"], p["adhoc_days"]) == (2, 1, 1) and "Informed balance is not enough" in p["warnings"][0]
        assert [x["type"] for x in p["parts"]] == ["Informed", "Adhoc"]
        assert apply(h3, start_date="2026-10-21", end_date="2026-10-23", type="Casual").json()["adhoc_days"] == 1   # old name still works
        # nothing left in October: beyond the balance costs 1.5 x a day's salary (31000 / 31 = 1000) and needs a signature
        p = preview(h3, start_date="2026-10-26", end_date="2026-10-26", type="Informed").json()
        assert (p["excess_days"], p["deduction"], p["needs_consent"], p["parts"][0]["lop"]) == (1, 1500.0, True, 1.5)
        assert "1 Informed day(s) x 1.5" in p["warnings"][-1] and "Rs 1,500.00" in p["warnings"][-1] and "I agree" in p["consent_text"]
        r = apply(h3, start_date="2026-10-26", end_date="2026-10-26", type="Informed")
        assert r.status_code == 400 and "full name" in r.json()["detail"]
        assert apply(h3, start_date="2026-10-26", end_date="2026-10-26", type="Informed", consent_name="someone else").status_code == 400
        assert apply(h3, start_date="2026-10-26", end_date="2026-10-26", type="Informed", consent_name="  tara ").json()["excess_days"] == 1
        lv = {x["start_date"]: x for x in c.get("/api/leaves?month=2026-10").json() if x["device_user_id"] == "LV3"}
        assert (lv["2026-10-26"]["type"], lv["2026-10-26"]["lop"]) == ("Informed", 1.5) and lv["2026-10-26"]["consent_id"] and not lv["2026-10-19"]["consent_id"]
        k = c.get(f"/api/leave-consents/{lv['2026-10-26']['consent_id']}").json()
        assert k["typed_name"] == "tara" and "Rs 1,500.00" in k["text"] and k["device_user_id"] == "LV3"
        assert c.get(f"/api/leave-consents/{lv['2026-10-26']['consent_id']}/pdf").content.startswith(b"%PDF")
        # November brings 2 more Informed (carried over is used first); no Adhoc until January, so Adhoc in December is 2x
        assert preview(h3, start_date="2026-11-02", end_date="2026-11-03", type="Informed").json()["excess_days"] == 0
        p = preview(h3, start_date="2026-12-01", end_date="2026-12-01", type="Adhoc").json()
        assert (p["excess_days"], p["deduction"], p["parts"][0]["lop"]) == (1, 2000.0, 2.0) and "2 days' salary" in p["warnings"][-1]
        # an Adhoc beyond the balance also takes 1 Informed credit
        assert apply(h3, start_date="2026-12-01", end_date="2026-12-01", type="Adhoc", consent_name="Tara").status_code == 200
        # (December's credit, so November's 2 are still free)
        assert preview(h3, start_date="2026-11-02", end_date="2026-11-03", type="Informed").json()["informed_days"] == 2
        b = c.get("/api/app/leave-balance", headers=h3).json()
        assert (b["informed"]["left"], b["adhoc"]["left"], b["informed"]["used"], b["adhoc"]["used"]) == (0, 0, 4, 1)
        # only a Sunday: nothing to take; too old for self-service; removed types are refused
        assert "no working days" in apply(h1, start_date="2026-10-25", end_date="2026-10-25", type="Adhoc").json()["detail"]
        assert apply(h1, start_date="2026-07-06", end_date="2026-07-06", type="Adhoc").status_code == 400
        for gone in ("Festive", "Paid", "Unpaid"):
            assert apply(h1, start_date="2026-12-10", end_date="2026-12-10", type=gone).status_code == 400
        # before eligibility there is no credit: 1.5x / 2x like anyone without balance
        p = preview(h2, start_date="2026-10-20", end_date="2026-10-20", type="Informed").json()
        assert (p["excess_days"], p["parts"][0]["type"], p["parts"][0]["lop"]) == (1, "Informed", 1.5)
        assert "01 Nov 2026" in p["warnings"][0] and p["salary_known"] is False and p["needs_consent"]
        assert apply(h2, start_date="2026-11-03", end_date="2026-11-03", type="Informed").json()["excess_days"] == 0
        assert c.get("/api/app/leave-balance", headers=h2).json()["eligible"] is False
        # bad input
        assert apply(h1, start_date="2026-10-30", end_date="2026-10-29", type="Informed").status_code == 400
        assert apply(h1, start_date="x", end_date="y", type="Adhoc").status_code == 400
        assert c.get("/api/app/leaves").status_code == 401

        # my list (one application can be several records), cancelling, and ownership
        mine = c.get("/api/app/leaves", headers=h3).json()
        assert {x["status"] for x in mine} == {"pending"} and len(mine) == 5
        lid = [x["id"] for x in mine if x["start_date"] == "2026-10-19"][0]
        assert c.delete(f"/api/app/leave/{lid}", headers=h2).status_code == 404                                   # not theirs
        assert c.delete(f"/api/app/leave/{lid}", headers=h3).status_code == 200
        assert c.get("/api/app/leave-balance", headers=h3).json()["informed"]["used"] == 2                       # balance freed
        # HR approves an application as a whole (both its parts), rejects another
        pend = {x["start_date"]: x for x in c.get("/api/leaves?month=2026-10").json() if x["status"] == "pending"}
        assert c.post(f"/api/leaves/{pend['2026-10-22']['id']}/approve").status_code == 200
        assert c.post(f"/api/leaves/{pend['2026-10-23']['id']}/approve").status_code == 400                       # decided with its group
        assert c.post(f"/api/leaves/{pend['2026-10-26']['id']}/reject").status_code == 200
        assert c.delete(f"/api/app/leave/{pend['2026-10-22']['id']}", headers=h3).status_code == 400              # approved: cannot self-cancel
        # HR records a past leave (no notice, so Adhoc) with no Adhoc left: 2x pay cut, shown in reports and payroll
        r = c.post("/api/leaves", json={"device_user_id": "LV3", "start_date": "2026-10-08", "end_date": "2026-10-08", "type": "Informed"})
        assert [(x["type"], x["lop"]) for x in r.json()["parts"]] == [("Adhoc", 2.0)]
        row = {r["device_user_id"]: r for r in c.get("/api/reports/daily?day=2026-10-08").json()}["LV3"]
        assert (row["status"], row["leave_type"]) == ("Leave", "Adhoc") and "beyond balance" in row["note"]
        pay = {r["device_user_id"]: r for r in c.get("/api/reports/payroll?month=2026-10").json()}["LV3"]
        assert pay["leave_cut"] == 2000.0
        # HR can record exactly as entered; only Informed / Adhoc exist
        assert c.post("/api/leaves", json={"device_user_id": "LV2", "start_date": "2026-10-05", "end_date": "2026-10-05", "type": "Informed", "override": True}).json()["parts"][0] == \
            {"type": "Informed", "lop": 0, "half": 0, "start_date": "2026-10-05", "end_date": "2026-10-05"}
        assert c.post("/api/leaves", json={"device_user_id": "LV2", "start_date": "2026-10-07", "end_date": "2026-10-07", "type": "Paid"}).status_code == 400
        assert c.get("/api/leave-preview", params={"employee": "LV1", "type": "Informed", "start_date": "2026-11-24", "end_date": "2026-11-24"}).json()["informed_days"] == 1
        b = c.get("/api/leave-balance?employee=LV1").json()
        assert b["eligible_from"] == "2026-04-05" and c.get("/api/leave-balance?employee=NOPE").status_code == 404
        # year-end: what is left of the year's credit is encashed (LV3 earned Sep..Mar only: 2+1+2+2+1+2+2 = 12 + 2 Adhoc)
        ys = {r["device_user_id"]: r for r in c.get("/api/leave-summary?fy=2026").json()}
        y1, y3 = ys["LV1"], ys["LV3"]
        assert (y1["informed_credit"], y1["adhoc_credit"], y1["used"], y1["balance"]) == (20, 4, 0, 24)
        assert y1["encashment"] == 24000.0
        assert (y3["informed_credit"], y3["adhoc_credit"]) == (12, 2)
        assert y3["balance"] == (12 - y3["informed"]) + (2 - y3["adhoc"])
        # today (10 Oct) only the credit earned so far counts: LV3 is eligible from September (Sep 2 + Oct 1, Oct 1 Adhoc)
        assert (y3["informed_earned"], y3["adhoc_earned"], y3["year_over"]) == (3, 1, False)
        assert y3["balance_now"] == max(3 - y3["informed"], 0) + max(1 - y3["adhoc"], 0)


def test_leave_approval_is_scoped_to_the_team(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lv2.db"))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin-password-123")
    _freeze_today(monkeypatch)
    H = {"X-Requested-With": "fetch"}
    with TestClient(app) as admin:
        admin.headers.update(H)
        assert admin.post("/api/auth/login", json={"username": "admin", "password": "admin-password-123"}).status_code == 200
        r = admin.post("/api/users", json={"username": "lead1", "password": "tl-password-12", "role": "manager", "department": "Ops", "permissions": "leave"})
        assert r.status_code == 200, r.text
        assert admin.post("/api/users", json={"username": "lead0", "password": "tl-password-00", "role": "manager", "department": "Ops"}).status_code == 200
        for uid, dp in (("1", "Ops"), ("2", "Sales")):
            admin.post("/api/employees", json={"device_user_id": uid, "name": f"E{uid}", "department": dp, "joined": "2026-01-05"})
            admin.post(f"/api/employees/{uid}/pin", json={"pin": "1234"})
            tk = admin.post("/api/app/login", json={"employee_id": uid, "pin": "1234"}).json()["token"]
            assert admin.post("/api/app/leave", json={"start_date": "2026-10-26", "end_date": "2026-10-26", "type": "Informed"}, headers={"Authorization": "Bearer " + tk}).status_code == 200
        ids = {x["device_user_id"]: x["id"] for x in admin.get("/api/leaves?month=2026-10").json()}
        tl, tl0 = TestClient(app), TestClient(app)
        for cl, u, p in ((tl, "lead1", "tl-password-12"), (tl0, "lead0", "tl-password-00")):
            cl.headers.update(H)
            assert cl.post("/api/auth/login", json={"username": u, "password": p}).status_code == 200
        assert {x["device_user_id"] for x in tl.get("/api/leaves?month=2026-10").json()} == {"1"}          # sees only own team's requests
        assert tl.post(f"/api/leaves/{ids['2']}/approve").status_code == 403                                  # other team
        assert tl0.post(f"/api/leaves/{ids['1']}/approve").status_code == 403                                 # no 'leave' ability
        assert tl.post(f"/api/leaves/{ids['1']}/approve").status_code == 200
        assert tl.get("/api/leave-balance?employee=2").status_code == 403 and tl.get("/api/leave-balance?employee=1").status_code == 200


def test_punch_log_file_safety_net(tmp_path, monkeypatch):
    import csv as _csv, socket
    from app import fkweb
    log_file = tmp_path / "punches_log.csv"
    monkeypatch.setattr(config, "PUNCH_LOG_FILE", str(log_file))
    monkeypatch.setattr(config, "RAW_LOG", str(tmp_path / "raw.log"))

    def post(port, dev, body):
        raw = (f"POST http://x HTTP/1.0\r\nrequest_code: realtime_glog\r\ndev_id: {dev}\r\nContent-Length: {len(body) + 4}\r\n\r\n").encode() \
              + len(body).to_bytes(4, "little") + body
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(raw); s.recv(300); s.close()

    with TestClient(app) as c:
        port = fkweb.bound_port
        scan = b'{"user_id":"00123456","io_mode":1,"verify_mode":2,"io_time":"20250707091500"}'
        post(port, "DEV-LOG", scan)
        post(port, "DEV-LOG", scan)                                                  # same scan again -> duplicate
        monkeypatch.setattr(config, "FKWEB_DEVICES", ["ONLY-THIS"])
        post(port, "STRANGER", b'{"user_id":"999","io_mode":1,"io_time":"20250707100000"}')
    rows = list(_csv.DictReader(open(log_file, newline="")))
    assert [(r["device"], r["id"], r["datetime"], r["stored"]) for r in rows] == [
        ("DEV-LOG", "00123456", "2025-07-07 09:15:00", "yes"),
        ("DEV-LOG", "00123456", "2025-07-07 09:15:00", "duplicate"),
        ("STRANGER", "999", "2025-07-07 10:00:00", "ignored-device")]
    assert list(rows[0].keys()) == ["received_at", "device", "id", "datetime", "io_mode", "verify_mode", "stored"]
    # the file can be loaded back through the Import tab: ids ignore leading zeros, replays are ignored
    from app import importer
    res = importer.run("punches", "punches_log.csv", log_file.read_bytes(), True)
    assert res["rows"] == 3 and res["errors"] == []
    # disabled by default and a broken path never breaks recording
    monkeypatch.setattr(config, "PUNCH_LOG_FILE", str(tmp_path / "no" / "such" / "dir" / "x.csv"))
    monkeypatch.setattr(config, "FKWEB_DEVICES", [])
    with TestClient(app):
        post(fkweb.bound_port, "DEV-LOG", b'{"user_id":"777","io_mode":1,"io_time":"20250707110000"}')
        assert [p["device_user_id"] for p in TestClient(app).get("/api/punches?day=2025-07-07").json() if p["device_user_id"] == "777"] == ["777"]


def test_documents_lists_loans_roster_breaks_wfh_and_payslip_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "hr.db"))
    monkeypatch.setattr(config, "DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setattr(config, "SELFIE_DIR", str(tmp_path / "selfies"))
    _freeze_today(monkeypatch)                                   # Saturday 10 Oct 2026
    with TestClient(app) as c:
        # shift with a (paid) lunch break 13:00-14:00
        assert c.post("/api/shifts", json={"name": "B 9-6", "start": "09:00", "end": "18:00", "break_start": "13:00", "break_end": "14:00"}).status_code == 200
        assert c.post("/api/shifts", json={"name": "Bad", "start": "09:00", "end": "18:00", "break_start": "19:00", "break_end": "20:00"}).status_code == 400
        c.post("/api/shifts", json={"name": "Eve 14-22", "start": "14:00", "end": "22:00"})
        sh = {s["name"]: s["id"] for s in c.get("/api/shifts").json()}
        c.post("/api/employees", json={"device_user_id": "H1", "name": "Hana", "shift_id": sh["B 9-6"], "salary": 31000,
                                       "department": "Accounts", "designation": "Clerk"})
        c.post("/api/employees", json={"device_user_id": "H2", "name": "Wfh Wasim", "shift_id": sh["B 9-6"], "remote_ok": True})
        # department / designation pick lists: typed values are added; rename moves people; in-use cannot be deleted
        m = c.get("/api/masters").json()
        assert {"name": "Accounts", "employees": 1} in m["departments"] and m["designations"][0]["name"] == "Clerk"
        assert c.post("/api/masters/department", json={"name": "Sales"}).status_code == 200
        assert c.delete("/api/masters/department/Accounts").status_code == 400
        assert c.post("/api/masters/department", json={"name": "Accounts", "new_name": "Finance"}).status_code == 200
        assert [e for e in c.get("/api/employees").json() if e["device_user_id"] == "H1"][0]["department"] == "Finance"
        assert c.delete("/api/masters/department/Sales").status_code == 200 and c.post("/api/masters/zzz", json={"name": "x"}).status_code == 404

        # documents: content decides the type; path tricks in the name are harmless
        up = lambda data, name="a.pdf", t="PAN card": c.post("/api/employees/H1/documents", files={"file": (name, data)}, data={"doc_type": t})
        assert up(b"%PDF-1.4 hello", "../../pan.pdf").status_code == 200
        assert up(b"MZ\x90\x00 not a doc", "x.exe").status_code == 400
        docs = c.get("/api/employees/H1/documents").json()
        assert len(docs) == 1 and docs[0]["doc_type"] == "PAN card" and "/" not in docs[0]["filename"]
        r = c.get(f"/api/documents/{docs[0]['id']}")
        assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
        assert [e for e in c.get("/api/employees").json() if e["device_user_id"] == "H1"][0]["documents"] == 1
        assert c.delete(f"/api/documents/{docs[0]['id']}").status_code == 200 and c.get("/api/employees/H1/documents").json() == []

        # attendance: the paid break is not deducted; a single punch counts as present (late in the morning, not late when it is the out punch)
        for ts in ("2026-10-05 09:00:00", "2026-10-05 18:00:00", "2026-10-06 09:30:00", "2026-10-07 17:55:00"):
            c.post("/api/punches", json={"device_user_id": "H1", "ts": ts})
        d = lambda day: {r["device_user_id"]: r for r in c.get(f"/api/reports/daily?day={day}").json()}["H1"]
        assert (d("2026-10-05")["hours"], d("2026-10-05")["status"]) == (9.0, "Present")
        assert (d("2026-10-06")["status"], d("2026-10-06")["note"]) == ("Late", "missing out punch")
        assert (d("2026-10-07")["status"], d("2026-10-07")["late_min"], d("2026-10-07")["note"]) == ("Present", 0, "missing in punch")
        assert d("2026-10-02")["status"] == ""                     # before the first punch: working days are not marked absent ...
        assert d("2026-10-04")["status"] == "Weekly Off"           # ... but Sundays are still paid

        # roster: Thu 8 Oct on the evening shift, Fri 9 Oct off, Sun 11 Oct (normally off) a working day
        for day, v in (("2026-10-08", str(sh["Eve 14-22"])), ("2026-10-09", "off"), ("2026-10-11", str(sh["B 9-6"]))):
            assert c.post("/api/roster", json={"device_user_id": "H1", "date": day, "value": v}).status_code == 200
        c.post("/api/punches", json={"device_user_id": "H1", "ts": "2026-10-08 14:20:00"})
        assert (d("2026-10-08")["shift"], d("2026-10-08")["late_min"]) == ("Eve 14-22", 20)
        assert d("2026-10-09")["status"] == "Weekly Off"
        g = c.get("/api/roster?start=2026-10-05").json()
        assert g["dates"][0] == "2026-10-05" and [r for r in g["rows"] if r["device_user_id"] == "H1"][0]["cells"]["2026-10-09"] == "off"
        assert c.post("/api/roster/copy", json={"from_start": "2026-10-05", "to_start": "2026-10-12"}).json()["copied"] == 3
        assert c.post("/api/roster", json={"device_user_id": "H1", "date": "2026-10-09", "value": "999"}).status_code == 404

        # work from home: a home site for one employee; a remote employee may punch anywhere
        assert c.post("/api/sites", json={"name": "Office", "lat": 19.0, "lng": 72.8, "radius_m": 100}).status_code == 200
        assert c.post("/api/sites", json={"name": "Hana home", "lat": 19.2, "lng": 72.9, "radius_m": 100, "kind": "home", "device_user_id": "H1"}).status_code == 200
        for uid in ("H1", "H2"):
            c.post(f"/api/employees/{uid}/pin", json={"pin": "1234"})
        tok = lambda uid: {"Authorization": "Bearer " + c.post("/api/app/login", json={"employee_id": uid, "pin": "1234"}).json()["token"]}
        jpg = b"\xff\xd8\xff\xe0" + b"0" * 100
        punch = lambda h, lat, lng: c.post("/api/app/punch", data={"lat": lat, "lng": lng, "accuracy": 5}, files={"selfie": ("s.jpg", jpg)}, headers=h)
        r = punch(tok("H1"), 19.2, 72.9)
        assert r.status_code == 200 and r.json()["site"] == "Hana home"
        r = punch(tok("H2"), 25.0, 80.0)                                     # far from everything, but H2 does field work
        assert r.status_code == 200 and r.json()["site"] == "Remote" and r.json()["flag"] == ""
        # work from home on request: a team lead / HR approves it, then that day the person may check in anywhere
        c.post("/api/employees", json={"device_user_id": "H3", "name": "Home Huda", "shift_id": sh["B 9-6"], "department": "Finance"})
        c.post("/api/employees/H3/pin", json={"pin": "1234"})
        h3 = tok("H3")
        assert punch(h3, 25.0, 80.0).status_code == 403                    # not approved yet: blocked
        r = c.post("/api/app/wfh", json={"start_date": "2026-10-10", "reason": "plumber visit"}, headers=h3)
        assert r.status_code == 200 and c.post("/api/app/wfh", json={"start_date": "2026-10-10"}, headers=h3).status_code == 400
        assert c.get("/api/dashboard").json()["pending_wfh"] == 1
        wid = c.get("/api/wfh?status=pending").json()[0]["id"]
        assert c.post(f"/api/wfh/{wid}/approve").status_code == 200 and c.post(f"/api/wfh/{wid}/approve").status_code == 400
        assert c.get("/api/app/me", headers=h3).json()["wfh_today"] is True
        r = punch(h3, 25.0, 80.0)
        assert r.status_code == 200 and r.json()["site"] == "Work from home"
        assert c.get("/api/app/wfh", headers=h3).json()[0]["status"] == "approved"
        assert [s["name"] for s in c.get("/api/app/me", headers=tok("H2")).json()["sites"]] == ["Office"]   # others' homes are private
        assert d("2026-10-10")["note"].startswith("WFH")
        c.post("/api/punches", json={"device_user_id": "H3", "ts": "2026-10-10 18:00:00"})   # today counts after the punch-out
        mon = {r["device_user_id"]: r for r in c.get("/api/reports/monthly?month=2026-10").json()}
        assert mon["H3"]["wfh_days"] == 1 and round(mon["H3"]["paid_days"] % 1, 2) == 0.8     # a WFH day is paid at 0.8
        assert mon["H2"]["wfh_days"] == 0 and mon["H2"]["paid_days"] % 1 == 0                 # field work is a full day
        # a holiday is off (the festival list is gone: an old client's "optional" flag makes a normal holiday)
        c.post("/api/holidays", json={"date": "2026-10-02", "name": "Gandhi Jayanti"})
        assert d("2026-10-02")["status"] == "Holiday"

        # loans: 50,000 over 5 months = 10,000 EMI, deducted from payroll; publishing records it in the ledger
        r = c.post("/api/loans", json={"device_user_id": "H1", "amount": 50000, "months": 5, "start_month": "2026-09", "note": "bike"})
        assert r.status_code == 200 and r.json()["emi"] == 10000
        lid = r.json()["id"]
        assert c.post("/api/loans", json={"device_user_id": "H1", "amount": 100, "months": 0, "start_month": "2026-09"}).status_code == 400
        sep = [x for x in c.get("/api/reports/payroll?month=2026-09").json() if x["device_user_id"] == "H1"][0]
        assert sep["loan_deduction"] == 10000 and sep["payslip_ready"] is True
        octo = [x for x in c.get("/api/reports/payroll?month=2026-10").json() if x["device_user_id"] == "H1"][0]
        assert octo["loan_deduction"] == 10000 and octo["payslip_ready"] is False
        assert c.post("/api/payroll/2026-09/publish").status_code == 200
        assert c.post(f"/api/loans/{lid}/repay", json={"month": "2026-09", "amount": 5000, "note": "cash"}).status_code == 200
        L = c.get("/api/loans?employee=H1").json()[0]
        assert (L["repaid"], L["outstanding"], L["status"]) == (15000, 35000, "Active")
        led = c.get("/api/loans/ledger/H1").json()
        assert [e["balance"] for e in led] == [50000, 40000, 35000]
        # edit: a bigger loan with a smaller EMI from November; never below what is already repaid
        edit = lambda **k: c.post(f"/api/loans/{lid}/edit", json={"kind": "Loan", "amount": 60000, "emi": 5000, "start_month": "2026-09", "note": "bike", **k})
        assert edit(amount=10000).status_code == 400 and edit(emi=0).status_code == 400 and edit(start_month="2026/11").status_code == 400
        assert edit().status_code == 200
        L = c.get("/api/loans?employee=H1").json()[0]
        assert (L["amount"], L["emi"], L["outstanding"], L["months_left"]) == (60000, 5000, 45000, 9)
        assert [x for x in c.get("/api/reports/payroll?month=2026-10").json() if x["device_user_id"] == "H1"][0]["loan_deduction"] == 5000
        assert edit(amount=50000, emi=10000).status_code == 200                            # back as it was
        assert c.delete(f"/api/loans/{lid}").status_code == 400 and c.post(f"/api/loans/{lid}/close").status_code == 200
        assert c.post("/api/payroll/2026-09/unpublish").status_code == 200
        assert c.get("/api/loans?employee=H1").json()[0]["repaid"] == 5000          # unpublishing removes the salary deduction

        # dated adjustment lands in that date's month
        assert c.post("/api/payroll/adjustments", json={"date": "2026-10-03", "device_user_id": "H1", "label": "Bonus", "amount": 700}).status_code == 200
        assert c.get("/api/payroll/adjustments?month=2026-10").json()[0]["date"] == "2026-10-03"

        # payslips: not mid-month, except for someone whose last working day has passed
        assert c.get("/api/payslips/H1/2026-10.pdf").status_code == 400
        c.post("/api/employees", json={"device_user_id": "H1", "name": "Hana", "shift_id": sh["B 9-6"], "salary": 31000,
                                       "department": "Finance", "active": False, "left_on": "2026-10-08"})
        assert c.get("/api/payslips/H1/2026-10.pdf").content.startswith(b"%PDF")
        assert "H1" in {r["device_user_id"] for r in c.get("/api/reports/payroll?month=2026-10").json()}   # still paid for days worked
        assert "H1" not in {r["device_user_id"] for r in c.get("/api/reports/daily?day=2026-10-10").json()}  # gone after leaving


def test_half_day_leave(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "half.db"))
    _freeze_today(monkeypatch)                                   # Saturday 10 Oct 2026
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "H 9-6", "start": "09:00", "end": "18:00"})
        sid = c.get("/api/shifts").json()[0]["id"]
        c.post("/api/employees", json={"device_user_id": "HD", "name": "Hafsa", "shift_id": sid, "joined": "2026-06-15", "salary": 31000})   # 3 Informed + 1 Adhoc by Oct
        c.post("/api/employees/HD/pin", json={"pin": "1234"})
        h = {"Authorization": "Bearer " + c.post("/api/app/login", json={"employee_id": "HD", "pin": "1234"}).json()["token"]}
        prev = lambda **k: c.get("/api/app/leave/preview", params={"type": "Informed", **k}, headers=h).json()
        apply = lambda **k: c.post("/api/app/leave", json={"type": "Informed", **k}, headers=h)
        # half day on Mon 12 Oct (no notice needed): half an Informed day, paid in full
        p = prev(start_date="2026-10-12", half=True)
        assert (p["working_days"], p["informed_days"], p["needs_consent"]) == (0.5, 1, False) and "paid in full" in p["warnings"][0]
        assert prev(start_date="2026-10-12", end_date="2026-10-13", half=True)["detail"] == "A half day is a single date"
        assert apply(start_date="2026-10-12", half=True).status_code == 200
        assert c.get("/api/app/leave-balance", headers=h).json()["informed"]["left"] == 2.5
        assert apply(start_date="2026-10-26").status_code == 200                       # full Informed: 1.5 left
        assert apply(start_date="2026-10-27").status_code == 200                       # 0.5 left
        assert apply(start_date="2026-10-13", type="Adhoc").status_code == 200         # the quarter's Adhoc
        assert apply(start_date="2026-10-14", half=True).json()["excess_days"] == 0    # the last 0.5 Informed
        # no balance left: 0.5 x 1.5 = 0.75 of a day's salary (31000 / 31 = 1000) is cut, with consent
        p = prev(start_date="2026-10-15", half=True)
        assert (p["excess_days"], p["deduction"], p["needs_consent"], p["parts"][0]["lop"]) == (1, 750.0, True, 0.75)
        assert apply(start_date="2026-10-15", half=True, consent_name="Hafsa").status_code == 200
        for x in c.get("/api/leaves?month=2026-10").json():
            if x["status"] == "pending":
                c.post(f"/api/leaves/{x['id']}/approve")
        # worked the other half of 12 Oct: a half day, no late mark, paid as a full day
        for ts in ("2026-10-12 13:40:00", "2026-10-12 18:00:00"):
            c.post("/api/punches", json={"device_user_id": "HD", "ts": ts})
        r = {x["device_user_id"]: x for x in c.get("/api/reports/daily?day=2026-10-12").json()}["HD"]
        assert (r["status"], r["late_min"], r["leave_type"]) == ("Half Day", 0, "Informed") and "half-day Informed leave" in r["note"]
        # HR records a past half day with no balance left: 0.75 day cut in payroll; the other half (no punch) is absent
        r = c.post("/api/leaves", json={"device_user_id": "HD", "start_date": "2026-10-09", "end_date": "2026-10-09", "type": "Informed", "half": True})
        assert [(x["type"], x["lop"], x["half"]) for x in r.json()["parts"]] == [("Informed", 0.75, 1)]
        r = {x["device_user_id"]: x for x in c.get("/api/reports/daily?day=2026-10-09").json()}["HD"]
        assert r["status"] == "Absent" and "0.75x pay cut" in r["note"]
        pay = {x["device_user_id"]: x for x in c.get("/api/reports/payroll?month=2026-10").json()}["HD"]
        assert pay["leave_cut"] == 750.0


def test_play_store_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "ps.db"))
    monkeypatch.setattr(config, "COMPANY_NAME", "Madarwala <Tours>")
    monkeypatch.setattr(config, "PRIVACY_CONTACT", "hr@example.com")
    with TestClient(app) as c:
        assert c.get("/.well-known/assetlinks.json").json() == []          # nothing until the fingerprint is configured
        monkeypatch.setattr(config, "ANDROID_CERT_SHA256", ["AA:BB"])
        a = c.get("/.well-known/assetlinks.json").json()[0]
        assert a["target"]["package_name"] == "com.madarwala.attendance" and a["target"]["sha256_cert_fingerprints"] == ["AA:BB"]
        p = c.get("/privacy")
        assert p.status_code == 200 and "hr@example.com" in p.text and "Madarwala &lt;Tours&gt;" in p.text and "{{" not in p.text
        m = c.get("/m/manifest.webmanifest").json()
        assert m["id"] == "/m/" and any(i["purpose"] == "maskable" for i in m["icons"])
        assert all(c.get("/m/" + s["src"]).status_code == 200 for s in m["screenshots"] + m["icons"])


def test_push_reminders(tmp_path, monkeypatch):
    from datetime import datetime as dt
    from app import push
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "push.db"))
    _freeze_today(monkeypatch)                                   # Saturday 10 Oct 2026
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "P 9:30-18:30", "start": "09:30", "end": "18:30", "weekly_off": "SUN"})
        sid = c.get("/api/shifts").json()[0]["id"]
        for uid in ("R1", "R2", "R3"):
            c.post("/api/employees", json={"device_user_id": uid, "name": uid, "shift_id": sid})
            c.post(f"/api/employees/{uid}/pin", json={"pin": "1234"})
        tok = lambda uid: {"Authorization": "Bearer " + c.post("/api/app/login", json={"employee_id": uid, "pin": "1234"}).json()["token"]}
        key = c.get("/api/app/push/key", headers=tok("R1")).json()["key"]
        assert len(key) > 80 and c.get("/api/app/push/key", headers=tok("R1")).json()["key"] == key      # stable
        sub = lambda uid, n: c.post("/api/app/push/subscribe", headers=tok(uid),
                                    json={"endpoint": f"https://push.example/{n}", "keys": {"p256dh": "p", "auth": "a"}})
        assert sub("R1", 1).status_code == 200 and sub("R2", 2).status_code == 200
        assert c.post("/api/app/push/subscribe", headers=tok("R3"), json={"endpoint": "http://x", "keys": {"p256dh": "p", "auth": "a"}}).status_code == 400
        assert {e["device_user_id"]: e["reminders"] for e in c.get("/api/employees").json()} == {"R1": 1, "R2": 1, "R3": 0}
        c.post("/api/leaves", json={"device_user_id": "R2", "start_date": "2026-10-10", "end_date": "2026-10-10", "type": "Informed", "override": True})
        at = lambda h, m: dt(2026, 10, 10, h, m)
        assert push.due(at(10, 29)) == []                                        # less than an hour after 09:30
        d = push.due(at(10, 30))
        assert [(x["uid"], x["kind"]) for x in d] == [("R1", "in")] and "09:30" in d[0]["body"]   # R2 is on leave, R3 has no phone
        sent = []
        monkeypatch.setattr(push, "webpush", lambda *a, **k: sent.append(a[0]["endpoint"]))
        assert push.run_once(at(10, 31)) == 1 and sent == ["https://push.example/1"]
        assert push.run_once(at(11, 0)) == 0                                     # once a day
        c.post("/api/punches", json={"device_user_id": "R1", "ts": "2026-10-10 11:05:00"})
        assert push.due(at(18, 14)) == []
        assert [(x["uid"], x["kind"]) for x in push.due(at(18, 15))] == [("R1", "out")]
        c.post("/api/punches", json={"device_user_id": "R1", "ts": "2026-10-10 18:16:00"})
        assert push.due(at(18, 17)) == []                                        # already checked out
        assert push.due(dt(2026, 10, 11, 11, 0)) == []                           # Sunday: weekly off
        # a phone that uninstalled the app answers 410: its subscription is removed
        from pywebpush import WebPushException
        class Gone:
            status_code = 410
        def boom(*a, **k):
            raise WebPushException("gone", response=Gone())
        monkeypatch.setattr(push, "webpush", boom)
        assert push.send("R1", "t", "b") == 0
        assert {e["device_user_id"]: e["reminders"] for e in c.get("/api/employees").json()}["R1"] == 0


def test_nightly_backup(tmp_path, monkeypatch):
    import pyzipper
    from datetime import datetime as dt
    from app import backup
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bk.db"))
    monkeypatch.setattr(config, "DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(config, "BACKUP_PASSWORD", "s3cret-pass")
    monkeypatch.setattr(config, "BACKUP_EMAIL_TO", "hr@example.com")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_USER", "server@example.com")
    sent = []
    class FakeSMTP:
        def __init__(self, host, port, timeout=None): sent.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): sent.append(("tls",))
        def login(self, u, p): sent.append(("login", u))
        def send_message(self, m): sent.append(("msg", m["To"], m["Subject"], [a.get_filename() for a in m.iter_attachments()]))
    monkeypatch.setattr(backup.smtplib, "SMTP", FakeSMTP)
    with TestClient(app) as c:
        c.post("/api/employees", json={"device_user_id": "B1", "name": "Bilal"})
        c.post("/api/employees/B1/documents", files={"file": ("pan.pdf", b"%PDF-1.4 x")}, data={"doc_type": "PAN card"})
        os.makedirs(config.BACKUP_DIR, exist_ok=True)
        open(os.path.join(config.BACKUP_DIR, "attendance-backup-2026-08-01.zip"), "wb").write(b"old")     # older than 30 days
        st = backup.run(dt(2026, 10, 10, 2, 0))
        assert st["error"] == "" and st["emailed_to"] == "hr@example.com" and st["encrypted"] is True
        assert ("tls",) in sent and ("login", "server@example.com") in sent
        assert sent[-1] == ("msg", "hr@example.com", "Attendance backup 10 Oct 2026", ["attendance-backup-2026-10-10.zip"])
        files = [f["file"] for f in c.get("/api/backups").json()["files"]]
        assert files == ["attendance-backup-2026-10-10.zip"]                     # the old one was pruned
        z = pyzipper.AESZipFile(os.path.join(config.BACKUP_DIR, files[0]))
        assert sorted(n for n in z.namelist() if not n.startswith("documents/")) == ["README.txt", "attendance.db"]
        assert any(n.startswith("documents/B1/") for n in z.namelist())
        try:
            z.read("attendance.db")
            raise AssertionError("readable without the password")
        except RuntimeError:
            pass
        z.setpassword(b"s3cret-pass")
        import sqlite3
        snap = tmp_path / "restored.db"
        snap.write_bytes(z.read("attendance.db"))
        assert sqlite3.connect(snap).execute("SELECT name FROM employees").fetchone()[0] == "Bilal"
        assert c.get("/api/backups/attendance-backup-2026-10-10.zip").content.startswith(b"PK")
        assert c.get("/api/backups/..%2Fbk.db").status_code == 404
        assert c.get("/api/backups").json()["last"]["file"] == "attendance-backup-2026-10-10.zip"
        # a failing mail server is reported, the local backup still exists
        def down(*a, **k): raise OSError("connection refused")
        monkeypatch.setattr(backup.smtplib, "SMTP", down)
        st = backup.run(dt(2026, 10, 11, 2, 0))
        assert "connection refused" in st["error"] and os.path.isfile(os.path.join(config.BACKUP_DIR, "attendance-backup-2026-10-11.zip"))


def test_audit_dates_and_past_leave_years(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "au.db"))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin-password-123")
    with TestClient(app) as c:
        c.headers.update({"X-Requested-With": "fetch"})
        assert c.post("/api/auth/login", json={"username": "admin", "password": "admin-password-123"}).status_code == 200
        from app import db as _db
        cx = _db.conn()
        for ts, act in (("2024-05-02 10:00:00", "POST /api/leaves"), ("2025-01-15 11:00:00", "POST /api/payroll/x"),
                        ("2025-01-31 23:59:00", "DELETE /api/holidays/x")):
            cx.execute("INSERT INTO audit(ts,user,action,status) VALUES (?,?,?,200)", (ts, "priya", act))
        cx.commit()
        r = c.get("/api/audit", params={"date_from": "2025-01-01", "date_to": "2025-01-31"}).json()
        assert [x["action"] for x in r] == ["DELETE /api/holidays/x", "POST /api/payroll/x"]          # end date inclusive
        assert [x["ts"][:4] for x in c.get("/api/audit", params={"q": "leaves"}).json()] == ["2024"]
        assert c.get("/api/audit", params={"date_from": "nope"}).status_code == 400
        # past financial years: someone who left during FY 2024-25 is still in that year's table, not in later ones
        c.post("/api/employees", json={"device_user_id": "Y1", "name": "Old Omar", "joined": "2023-01-01", "active": False, "left_on": "2024-09-30"})
        c.post("/api/employees", json={"device_user_id": "Y2", "name": "Now Nadia", "joined": "2023-01-01"})
        c.post("/api/leaves", json={"device_user_id": "Y1", "start_date": "2024-06-03", "end_date": "2024-06-03", "type": "Informed", "override": True})
        fy24 = {x["device_user_id"]: x for x in c.get("/api/leave-summary?fy=2024").json()}
        assert set(fy24) == {"Y1", "Y2"} and fy24["Y1"]["informed"] == 1 and fy24["Y1"]["balance"] == 23
        assert set(x["device_user_id"] for x in c.get("/api/leave-summary?fy=2025").json()) == {"Y2"}


def test_personal_and_bank_details(tmp_path, monkeypatch):
    import io as _io
    from datetime import datetime as _dt
    from openpyxl import Workbook
    from app import access
    with TestClient(app) as c:
        c.post("/api/employees", json={"device_user_id": "8801", "name": "Asha", "mobile": "9000000001"})
        c.post("/api/employees", json={"device_user_id": "8802", "name": "Ravi"})
        # the form: cleaned and validated
        r = c.post("/api/employees", json={"device_user_id": "8801", "name": "Asha", "mobile": "9000000001", "pan": " abcde1234f",
                                           "bank_ifsc": "sbin0001234", "bank_account": "1234 5678 9012", "dob": "21/06/1995",
                                           "blood_group": "b positive", "bank_type": "saving"})
        assert r.status_code == 200
        e = [x for x in c.get("/api/employees").json() if x["device_user_id"] == "8801"][0]
        assert (e["pan"], e["bank_ifsc"], e["bank_account"], e["dob"], e["blood_group"], e["bank_type"]) == \
            ("ABCDE1234F", "SBIN0001234", "123456789012", "1995-06-21", "B+", "Savings")
        for bad, msg in (({"pan": "ABC"}, "PAN"), ({"bank_ifsc": "IDBI"}, "IFSC"), ({"bank_account": "12ab"}, "account"),
                         ({"email": "nope"}, "e-mail"), ({"dob": "31/02/1990"}, "date")):
            r = c.post("/api/employees", json={"device_user_id": "8801", "name": "Asha", **bad})
            assert r.status_code == 400 and msg in r.json()["detail"], bad
        # leaving the fields out (an older client, or a team lead's view) keeps them
        c.post("/api/employees", json={"device_user_id": "8801", "name": "Asha", "mobile": "9000000001", "department": "Ops"})
        e = [x for x in c.get("/api/employees").json() if x["device_user_id"] == "8801"][0]
        assert e["pan"] == "ABCDE1234F" and e["department"] == "Ops"
        # team leads (below HR) never receive personal or bank details
        token = access.salary_visible.set(False)
        try:
            from app.main import list_employees
            assert "bank_account" not in list_employees()[0] and "pan" not in list_employees()[0]
        finally:
            access.salary_visible.reset(token)
        # Google Form sheet: messy headers and values, numeric ids, HR fields untouched
        wb = Workbook(); ws = wb.active
        ws.append(["Timestamp", "Full Name", "Employee ID", "  Date of Birth  ", "  Blood Group  ", "Gender ",
                   "  House/Hostel  Address  \n(If staying in hostel add both)", "  Mobile Number  ", "Department and Designation ",
                   "Branch ", "   Joining Date  ", "Current Salary", "  PAN number  ", "   Bank Name  ", "   IFSC Code  ",
                   "  Bank Account Number  ", "Account holders Name", "Account type", "  Nationality   "])
        ws.append([_dt(2026, 9, 1), "Asha K", 8801.0, _dt(1995, 6, 21), "O - ve", "Female", "12 MG Road", 9111111111.0, "accounts , biller",
                   "Tirmulgherry", _dt(2020, 1, 2), 99999, "None", "IDBI BANK", "ibkl0000297", 297104000191371.0, "ASHA KHAN", "Saving", "INDIA"])
        ws.append([_dt(2026, 9, 1), "Ravi", 8802.0, "05/01/1985", "0+", "Male", "Flat 2", 9222222222.0, "HR", "Mumbai", _dt(2019, 5, 6),
                   50000, "nil", "nil", "nil", "nil", "nil", "", "Indian"])
        ws.append([_dt(2026, 9, 1), "Ghost", 9999999.0, "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""])
        buf = _io.BytesIO(); wb.save(buf)
        up = lambda dry: c.post(f"/api/import/profiles?dry_run={'true' if dry else 'false'}", files={"file": ("form.xlsx", buf.getvalue())}).json()
        r = up(True)
        assert (r["updated"], r["skipped"]) == (2, 1) and "no employee with id 9999999" in r["errors"][0]["message"]
        assert [x for x in c.get("/api/employees").json() if x["device_user_id"] == "8802"][0]["blood_group"] == ""   # dry run
        assert up(False)["updated"] == 2
        emps = {x["device_user_id"]: x for x in c.get("/api/employees").json()}
        a, b = emps["8801"], emps["8802"]
        assert (a["blood_group"], a["bank_name"], a["bank_ifsc"], a["bank_account"], a["bank_holder"], a["bank_type"], a["nationality"]) == \
            ("O-", "IDBI BANK", "IBKL0000297", "297104000191371", "Asha Khan", "Savings", "Indian")
        assert a["pan"] == "ABCDE1234F"                          # "None" in the sheet does not wipe it
        assert (a["mobile"], a["department"], a["salary"], a["branch"]) == ("9000000001", "Ops", 0, "")   # HR fields kept
        assert a["joined"] == "2020-01-02"                       # was empty, so filled in
        assert (b["mobile"], b["dob"], b["blood_group"], b["bank_account"], b["address"]) == ("9222222222", "1985-01-05", "O+", "", "Flat 2")


def test_several_shifts_on_one_day(tmp_path, monkeypatch):
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "Split AM 10-2", "start": "10:00", "end": "14:00"})
        c.post("/api/shifts", json={"name": "Split PM 7-9", "start": "19:00", "end": "21:00"})
        ids = {x["name"]: x["id"] for x in c.get("/api/shifts").json()}
        am, pm = ids["Split AM 10-2"], ids["Split PM 7-9"]
        for uid, name in (("7701", "Split One"), ("7702", "Split Two"), ("7703", "Split Three")):
            c.post("/api/employees", json={"device_user_id": uid, "name": name})
            # evening shift first: stored earliest-first anyway
            assert c.post("/api/roster", json={"device_user_id": uid, "date": "2025-09-03", "value": f"{pm},{am}"}).status_code == 200
        cell = [r for r in c.get("/api/roster?start=2025-09-01").json()["rows"] if r["device_user_id"] == "7701"][0]["cells"]["2025-09-03"]
        assert cell == f"{am},{pm}"
        assert c.post("/api/roster", json={"device_user_id": "7701", "date": "2025-09-04", "value": f"{am},99999"}).status_code == 404
        assert c.post("/api/roster", json={"device_user_id": "7701", "date": "2025-09-04", "value": "1,2,3,4,5"}).status_code == 400
        punch = lambda uid, ts: c.post("/api/punches", json={"device_user_id": uid, "ts": ts})
        punch("7701", "2025-09-03 10:05:00"); punch("7701", "2025-09-03 21:00:00")   # both shifts: 10:05-21:00 minus the 5 h gap
        punch("7702", "2025-09-03 10:30:00"); punch("7702", "2025-09-03 21:30:00")
        punch("7703", "2025-09-03 10:00:00"); punch("7703", "2025-09-03 12:00:00")   # 2 h of the 6 scheduled
        rep = {r["device_user_id"]: r for r in c.get("/api/reports/daily?day=2025-09-03").json()}
        one, two, three = rep["7701"], rep["7702"], rep["7703"]
        assert one["shift"] == "Split AM 10-2 + Split PM 7-9"
        assert (one["status"], one["hours"], one["late_min"], one["early_min"]) == ("Present", 5.92, 0, 0)
        assert (two["status"], two["late_min"], two["ot_min"], two["hours"]) == ("Late", 30, 30, 6.0)
        assert (three["status"], three["hours"], three["early_min"]) == ("Half Day", 2.0, 540)
        card = c.get("/api/reports/timecard?month=2025-09&employee=7701").json()
        assert [d for d in card if d["date"] == "2025-09-03"][0]["schedule"] == "10:00-14:00, 19:00-21:00"
        # back to one shift, then normal
        c.post("/api/roster", json={"device_user_id": "7701", "date": "2025-09-03", "value": str(am)})
        assert {r["device_user_id"]: r["shift"] for r in c.get("/api/reports/daily?day=2025-09-03").json()}["7701"] == "Split AM 10-2"


def test_all_employees_time_card_and_workbook(tmp_path, monkeypatch):
    import io as _io
    from openpyxl import load_workbook
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "tc.db"))
    _freeze_today(monkeypatch)                                   # Saturday 10 Oct 2026
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "TC 10-7", "start": "10:00", "end": "19:00"})              # Sunday off
        sid = c.get("/api/shifts").json()[0]["id"]
        for uid, nm, dp, jn in (("T1", "Zara", "Ops", "2026-01-01"), ("T2", "Aman", "Ops", "2026-01-01"),
                                ("T3", "Aman", "Sales", "2026-10-07"), ("T4", "Bina", "Sales", "2026-01-01")):
            c.post("/api/employees", json={"device_user_id": uid, "name": nm, "department": dp, "shift_id": sid, "joined": jn})
        P = lambda uid, ts: c.post("/api/punches", json={"device_user_id": uid, "ts": ts})
        P("T1", "2026-10-05 09:45:00"); P("T1", "2026-10-05 18:30:00")       # early login 15, early logout 30
        P("T2", "2026-10-05 10:25:00"); P("T2", "2026-10-05 19:00:00")       # 25 min late
        P("T2", "2026-10-06 10:00:00")                                        # missing out punch: nothing invented
        c.post("/api/leaves", json={"device_user_id": "T4", "start_date": "2026-10-06", "end_date": "2026-10-06", "type": "Adhoc", "override": True})
        c.post("/api/leaves", json={"device_user_id": "T4", "start_date": "2026-10-08", "end_date": "2026-10-08", "type": "Informed", "override": True})
        c.post("/api/roster", json={"device_user_id": "T1", "date": "2026-10-04", "value": str(sid)})   # Sunday, rostered
        P("T1", "2026-10-04 10:00:00"); P("T1", "2026-10-04 19:00:00")
        P("T2", "2026-10-04 10:00:00"); P("T2", "2026-10-04 13:00:00")        # Sunday, not rostered: no blue
        q = {"start": "2026-10-04", "end": "2026-10-10"}
        rows = c.get("/api/reports/timecards", params=q).json()
        assert [(r["date"], r["name"]) for r in rows[:4]] == [("2026-10-04", "Aman"), ("2026-10-04", "Bina"), ("2026-10-04", "Zara"), ("2026-10-05", "Aman")]
        assert {r["device_user_id"] for r in rows} == {"T1", "T2", "T3", "T4"}
        assert len(rows) == 7 * 3 + 4                      # T3 joined on the 7th: only 7-10 Oct
        assert len({(r["device_user_id"], r["date"]) for r in rows}) == len(rows)       # no duplicates
        by = {(r["device_user_id"], r["date"]): r for r in rows}
        t1 = by[("T1", "2026-10-05")]
        assert (t1["scheduled_in"], t1["in"], t1["scheduled_out"], t1["out"], t1["early_in_min"], t1["early_min"], t1["late_min"]) == \
            ("10:00", "09:45", "19:00", "18:30", 15, 30, 0)
        assert (by[("T2", "2026-10-05")]["late_min"], by[("T2", "2026-10-05")]["status"]) == (25, "Late")
        miss = by[("T2", "2026-10-06")]
        assert (miss["in"], miss["out"], miss["hours"]) == ("10:00", "", 0.0) and "missing out punch" in miss["note"]
        assert (by[("T4", "2026-10-06")]["leave_type"], by[("T4", "2026-10-08")]["leave_type"]) == ("Adhoc", "Informed")
        assert by[("T1", "2026-10-04")]["week_off_duty"] is True and by[("T2", "2026-10-04")]["week_off_duty"] is False
        # filters
        one = c.get("/api/reports/timecards", params={**q, "employee": "T4"}).json()
        assert {r["device_user_id"] for r in one} == {"T4"} and len(one) == 7
        assert {r["device_user_id"] for r in c.get("/api/reports/timecards", params={**q, "department": "Sales"}).json()} == {"T3", "T4"}
        assert [r["date"] for r in c.get("/api/reports/timecards", params={**q, "status": "Adhoc"}).json()] == ["2026-10-06"]
        assert [r["device_user_id"] for r in c.get("/api/reports/timecards", params={**q, "status": "Week-off duty"}).json()] == ["T1"]
        assert c.get("/api/reports/timecards", params={**q, "status": "Nope"}).status_code == 400
        assert c.get("/api/reports/timecards", params={"start": "2026-10-10", "end": "2026-10-01"}).status_code == 400
        assert c.get("/api/reports/timecards", params={"start": "2024-01-01", "end": "2026-01-01"}).status_code == 400
        # workbook: consolidated sheet + one sheet per employee (same names kept apart by ID), colours and dates
        wb = load_workbook(_io.BytesIO(c.get("/api/reports/timecards.xlsx", params=q).content))
        assert wb.sheetnames == ["In-Out", "All Employees", "Aman (T2)", "Aman (T3)", "Bina (T4)", "Zara (T1)"]
        # In-Out grid: one row per employee, In and Out under each date
        g = wb["In-Out"]
        assert (g["A1"].value, g["C1"].value, g["E1"].value, g["A2"].value, g["C2"].value, g["D2"].value) == \
            ("Date →", "Sun 04/10", "Mon 05/10", "Employee", "In", "Out")
        grid = {r[1]: r for r in g.iter_rows(min_row=3, values_only=True) if r[1] in ("T1", "T2", "T3", "T4")}
        assert [grid[k][0] for k in ("T2", "T3", "T4", "T1")] == ["Aman", "Aman", "Bina", "Zara"]
        assert grid["T1"][4:6] == ("09:45", "18:30") and grid["T2"][4:6] == ("10:25", "19:00")   # 5 Oct
        assert grid["T2"][6:8] == ("10:00", None)                                                # missing out: left empty
        assert grid["T4"][6] == "AL" and grid["T4"][10] == "IL"                                   # 6 and 8 Oct leave
        assert grid["T3"][6] is None and grid["T3"][8] == "A"            # nothing before joining on 7 Oct, then absent
        row_t2 = next(r for r in g.iter_rows(min_row=3) if r[1].value == "T2")
        assert row_t2[4].font.color.rgb.endswith("B91C1C")                                         # late in red
        row_t4 = next(r for r in g.iter_rows(min_row=3) if r[1].value == "T4")
        assert row_t4[6].fill.fgColor.rgb.endswith("FFC7CE") and row_t4[10].fill.fgColor.rgb.endswith("FFEB9C")
        row_t1 = next(r for r in g.iter_rows(min_row=3) if r[1].value == "T1")
        assert row_t1[2].fill.fgColor.rgb.endswith("DDEBF7")                                       # rostered Sunday
        allws = wb["All Employees"]
        head = [x.value for x in allws[1]]
        assert head[:4] == ["Employee Name", "Employee ID", "Department", "Date"] and "Early Login (min)" in head and "Leave Type" in head
        data = [r for r in allws.iter_rows(min_row=2, values_only=True) if r[1]]
        assert len([r for r in data if r[1] in ("T1", "T2", "T3", "T4")]) == len(rows)
        cell = next(r for r in allws.iter_rows(min_row=2) if r[1].value == "T4" and r[3].value and r[3].value.day == 6)
        assert cell[0].fill.fgColor.rgb.endswith("FFC7CE") and cell[3].number_format == "DD-MM-YYYY"     # Adhoc row red
        cell = next(r for r in allws.iter_rows(min_row=2) if r[1].value == "T4" and r[3].value and r[3].value.day == 8)
        assert cell[0].fill.fgColor.rgb.endswith("FFEB9C")                                                 # Informed row yellow
        cell = next(r for r in allws.iter_rows(min_row=2) if r[1].value == "T1" and r[3].value and r[3].value.day == 4)
        assert cell[0].fill.fgColor.rgb.endswith("DDEBF7")                                                 # rostered week-off duty
        ws = wb["Bina (T4)"]
        assert ws["A1"].value == "Employee" and ws["B1"].value == "Bina" and ws["B2"].value == "T4"
        own = [r for r in ws.iter_rows(min_row=7, values_only=True) if r[1] in ("T1", "T2", "T3", "T4")]
        assert len(own) == 7 and {r[1] for r in own} == {"T4"}
        assert any("Yellow = Informed" in str(r[0]) for r in ws.iter_rows(values_only=True))           # legend
        single = load_workbook(_io.BytesIO(c.get("/api/reports/timecards.xlsx", params={**q, "employee": "T1"}).content))
        assert single.sheetnames == ["In-Out", "All Employees", "Zara (T1)"]


def test_late_arrival_counter_policy_and_emails(tmp_path, monkeypatch):
    from datetime import datetime as _dt
    from app import lates, mailer
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "late.db"))
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "HR_EMAIL", "hr@madarwala.com")
    _freeze_today(monkeypatch)                                   # Saturday 10 Oct 2026 12:00
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, attachment=None: sent.append((to, subject, body)))
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin-password-123")
    with TestClient(app) as c:
        c.headers.update({"X-Requested-With": "fetch"})
        assert c.post("/api/auth/login", json={"username": "admin", "password": "admin-password-123"}).status_code == 200
        c.post("/api/shifts", json={"name": "LT 10-7", "start": "10:00", "end": "19:00", "weekly_off": ""})   # grace 10
        sid = c.get("/api/shifts").json()[0]["id"]
        c.post("/api/employees", json={"device_user_id": "LA", "name": "Lata", "department": "Ops", "shift_id": sid,
                                       "joined": "2026-01-01", "work_email": "lata@madarwala.com"})
        c.post("/api/employees", json={"device_user_id": "LB", "name": "Bilal", "department": "Sales", "shift_id": sid, "joined": "2026-01-01"})
        assert c.post("/api/users", json={"username": "opslead", "password": "lead-password-1", "role": "manager",
                                          "department": "Ops", "email": "Ops.Lead@madarwala.com"}).status_code == 200
        assert c.post("/api/users", json={"username": "bad", "password": "lead-password-1", "role": "manager",
                                          "department": "Ops", "email": "nope"}).status_code == 400
        P = lambda uid, ts: c.post("/api/punches", json={"device_user_id": uid, "ts": ts})
        P("LA", "2026-09-29 10:30:00")                    # September: not in October's count
        P("LA", "2026-10-01 10:09:00")                    # within the 10-minute grace: not late
        P("LA", "2026-10-02 10:11:00"); P("LA", "2026-10-05 10:40:00")
        P("LB", "2026-10-06 10:20:00")
        pol = c.get("/api/late-policy").json()
        assert (pol["period"], pol["limit"]) == ("month", 3) and "rolling30" in pol["periods"]
        cnt = {x["device_user_id"]: x for x in c.get("/api/lates").json()}
        assert (cnt["LA"]["count"], cnt["LA"]["state"], [x["date"] for x in cnt["LA"]["lates"]]) == (2, "ok", ["2026-10-02", "2026-10-05"])
        assert cnt["LA"]["lates"][0] == {"date": "2026-10-02", "late_min": 11, "scheduled_in": "10:00", "in": "10:11"}
        # HR changes the tracking period: last 30 days also counts 29 Sep
        assert c.post("/api/late-policy", json={"period": "rolling30", "limit": 3}).json()["period"] == "rolling30"
        assert {x["device_user_id"]: x["count"] for x in c.get("/api/lates").json()}["LA"] == 3
        assert c.post("/api/late-policy", json={"period": "weekly", "limit": 3}).status_code == 400
        assert lates.bounds(_dt(2026, 11, 15).date(), "quarter")[0].isoformat() == "2026-10-01"
        assert lates.bounds(_dt(2027, 2, 15).date(), "fy")[0].isoformat() == "2026-04-01"
        c.post("/api/late-policy", json={"period": "month", "limit": 3})

        # today (10 Oct) Lata checks in late: one e-mail to her, her team lead and HR; the 3rd this month = limit
        P("LA", "2026-10-10 10:35:00")
        assert lates.run_once(_dt(2026, 10, 10, 10, 36)) == 1
        to, subject, body = sent[-1]
        assert to == ["lata@madarwala.com", "ops.lead@madarwala.com", "hr@madarwala.com"]
        assert "3rd late arrival, limit of 3 reached" in subject and "Lata (LA)" in subject
        for text in ("Scheduled login: 10:00", "Actual login: 10:35", "Minutes late: 35", "3 of 3 allowed", "No salary is deducted"):
            assert text in body
        # no duplicates: another check, a re-punch, or recalculation the same day sends nothing
        P("LA", "2026-10-10 11:00:00")
        assert lates.run_once(_dt(2026, 10, 10, 11, 1)) == 0 and len(sent) == 1
        st = {x["device_user_id"]: x for x in c.get("/api/lates").json()}["LA"]
        assert (st["count"], st["state"]) == (3, "limit")
        notes = c.get("/api/late-notices?month=2026-10").json()
        assert len(notes) == 1 and notes[0]["count"] == 3 and notes[0]["sent_at"]
        # a correction of a past day updates the count (no new e-mail): the 2 Oct punch was really 10:05
        pid = [p["id"] for p in c.get("/api/punches?day=2026-10-02").json() if p["device_user_id"] == "LA"][0]
        assert c.post(f"/api/punches/{pid}/correct", json={"ts": "2026-10-02 10:05", "reason": "device clock"}).status_code == 200
        st = {x["device_user_id"]: x for x in c.get("/api/lates").json()}["LA"]
        assert (st["count"], st["state"]) == (2, "ok") and len(sent) == 1
        # the employee sees the same count in the app
        c.post("/api/employees/LA/pin", json={"pin": "1234"})
        tk = c.post("/api/app/login", json={"employee_id": "LA", "pin": "1234"}).json()["token"]
        mine = c.get("/api/app/lates", headers={"Authorization": "Bearer " + tk}).json()
        assert (mine["count"], mine["limit"], mine["period_label"]) == (2, 3, "Calendar month")
        # over the limit is flagged for HR review; no SMTP set up -> nothing is sent or recorded
        assert c.post("/api/late-policy", json={"period": "month", "limit": 1}).status_code == 200
        assert {x["device_user_id"]: x["state"] for x in c.get("/api/lates").json()}["LA"] == "over"
        assert lates.message({"name": "B", "device_user_id": "LB"}, {"date": "2026-10-06", "late_min": 20, "scheduled_in": "10:00", "in": "10:20"},
                             2, lates.policy())[0].endswith("over the limit: for HR review")
        monkeypatch.setattr(config, "SMTP_HOST", "")
        P("LB", "2026-10-10 10:50:00")
        assert lates.run_once(_dt(2026, 10, 10, 10, 51)) == 0 and len(c.get("/api/late-notices?month=2026-10").json()) == 1
        # nothing here changes pay
        pay = {r["device_user_id"]: r for r in c.get("/api/reports/payroll?month=2026-10").json()}
        assert pay == {} or all(r["leave_cut"] == 0 for r in pay.values())


def test_weekly_report_and_test_email(tmp_path, monkeypatch):
    import io as _io
    from datetime import date as _d, datetime as _dt
    from openpyxl import load_workbook
    from app import mailer, weekly
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "wk.db"))
    _freeze_today(monkeypatch)                                   # Saturday 10 Oct 2026
    monkeypatch.setattr("app.lates.now_local", lambda: _dt(2026, 10, 12, 9, 0))
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, attachment=None: sent.append((to, subject, body, attachment)))
    # the period: month so far up to last Sunday; the first Monday of a month closes the previous month
    assert weekly.period(_d(2026, 10, 12)) == (_d(2026, 10, 1), _d(2026, 10, 11))
    assert weekly.period(_d(2026, 10, 5)) == (_d(2026, 9, 1), _d(2026, 9, 30))
    assert weekly.period(_d(2026, 11, 2)) == (_d(2026, 10, 1), _d(2026, 10, 31))
    assert weekly.period(_d(2026, 11, 9)) == (_d(2026, 11, 1), _d(2026, 11, 8))
    assert weekly.period(_d(2026, 10, 14)) == (_d(2026, 10, 1), _d(2026, 10, 11))   # a manual run mid-week
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "WK 10-7", "start": "10:00", "end": "19:00"})
        sid = c.get("/api/shifts").json()[0]["id"]
        c.post("/api/employees", json={"device_user_id": "W1", "name": "Wasim", "shift_id": sid, "joined": "2026-01-01"})
        c.post("/api/punches", json={"device_user_id": "W1", "ts": "2026-10-05 10:30:00"})
        c.post("/api/punches", json={"device_user_id": "W1", "ts": "2026-10-05 19:00:00"})
        c.post("/api/leaves", json={"device_user_id": "W1", "start_date": "2026-10-06", "end_date": "2026-10-06", "type": "Adhoc", "override": True})
        # not set up: clear message, nothing sent
        monkeypatch.setattr(config, "SMTP_HOST", "")
        assert "not set up" in c.post("/api/mail/test").json()["detail"]
        assert "not set up" in weekly.run(_dt(2026, 10, 12, 9, 0))["error"] and not sent
        monkeypatch.setattr(config, "SMTP_HOST", "smtp.gmail.com")
        r = c.post("/api/mail/test").json()
        assert r["ok"] and sent[-1][0] == [config.HR_EMAIL] and "test" in sent[-1][1]
        st = weekly.run(_dt(2026, 10, 12, 9, 0))
        assert st["error"] == "" and (st["from"], st["to"]) == ("2026-10-01", "2026-10-11")
        to, subject, body, (name, data) = sent[-1]
        assert to == [config.HR_EMAIL] and name.endswith(".xlsx") and "Adhoc leave (red): 1 days" in body and "Late arrivals: 1" in body
        wb = load_workbook(_io.BytesIO(data))
        assert wb.sheetnames == ["Summary", "Late arrivals", "Leave", "Time Card"]
        summ = [r for r in wb["Summary"].iter_rows(values_only=True) if r[1] == "W1"][0]
        assert summ[0] == "Wasim"
        leave = [r for r in wb["Leave"].iter_rows(min_row=2) if r[1].value == "W1"][0]
        assert leave[4].value == "Adhoc" and leave[0].fill.fgColor.rgb.endswith("FFC7CE")
        assert weekly.status()["sent_to"] == config.HR_EMAIL
        assert not weekly.due(_dt(2026, 10, 12, 10, 0))          # already sent this Monday
        assert not weekly.due(_dt(2026, 10, 13, 10, 0))          # not Monday
        assert weekly.due(_dt(2026, 10, 19, 9, 0)) and not weekly.due(_dt(2026, 10, 19, 7, 0))
        assert c.get("/api/mail").json()["configured"] is True
        assert c.get("/api/weekly-report.xlsx").content[:2] == b"PK"


def test_leave_balance_adjustments(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "adj.db"))
    _freeze_today(monkeypatch)                                   # Saturday 10 Oct 2026
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "AJ 9-6", "start": "09:00", "end": "18:00"})
        sid = c.get("/api/shifts").json()[0]["id"]
        c.post("/api/employees", json={"device_user_id": "AJ", "name": "Arif", "shift_id": sid, "joined": "2026-06-15", "salary": 31000})
        bal = lambda: c.get("/api/leave-balance?employee=AJ").json()
        assert (bal()["informed"]["left"], bal()["adhoc"]["left"]) == (3, 1)          # Sep 2 + Oct 1 Informed, Oct 1 Adhoc
        add = lambda **k: c.post("/api/leave-adjustments", json={"device_user_id": "AJ", "fy": 2026, **k})
        assert add(informed=4, adhoc=1, reason="carried over from Realtime").status_code == 200
        b = bal()
        assert (b["informed"]["left"], b["adhoc"]["left"], b["adjusted"]) == (7, 2, {"informed": 4, "adhoc": 1})
        assert add(informed=-1.5, reason="correction").status_code == 200
        assert bal()["informed"]["left"] == 5.5
        # the extra days are really usable: 5 Informed days next month need no pay cut
        p = c.get("/api/leave-preview", params={"employee": "AJ", "type": "Informed", "start_date": "2026-11-02", "end_date": "2026-11-06"}).json()
        assert (p["informed_days"], p["excess_days"]) == (5, 0)
        # refused input
        assert add(informed=2, reason="  ").status_code == 400
        assert add(reason="nothing").status_code == 400
        assert add(informed=0.3, reason="x").status_code == 400
        assert c.post("/api/leave-adjustments", json={"device_user_id": "NOPE", "fy": 2026, "informed": 1, "reason": "x"}).status_code == 404
        # listed with who did it, counted in the year-end encashment, removable
        rows = c.get("/api/leave-adjustments?fy=2026").json()
        assert [(r["informed"], r["adhoc"], r["name"]) for r in rows] == [(-1.5, 0, "Arif"), (4, 1, "Arif")]
        y = {r["device_user_id"]: r for r in c.get("/api/leave-summary?fy=2026").json()}["AJ"]
        assert (y["informed_credit"], y["adhoc_credit"], y["adjusted_informed"], y["balance"]) == (12 + 2.5, 2 + 1, 2.5, 14.5 + 3)
        assert c.get("/api/leave-adjustments?fy=2025").json() == []
        assert c.delete(f"/api/leave-adjustments/{rows[0]['id']}").status_code == 200
        assert bal()["informed"]["left"] == 7
        assert c.delete("/api/leave-adjustments/99999").status_code == 404


def test_absence_without_approved_leave_counts_as_adhoc(tmp_path, monkeypatch):
    from app import db as appdb
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "abs.db"))
    _freeze_today(monkeypatch)                                   # Saturday 10 Oct 2026
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "AB 9-6", "start": "09:00", "end": "18:00"})            # Sunday off
        sid = c.get("/api/shifts").json()[0]["id"]
        # eligible since 15 Sep: Sep 2 + Oct 1 Informed, Oct 1 Adhoc; 31000 / 31 = 1000 a day
        c.post("/api/employees", json={"device_user_id": "AB", "name": "Abid", "shift_id": sid, "joined": "2026-06-15", "salary": 31000})
        P = lambda d: [c.post("/api/punches", json={"device_user_id": "AB", "ts": f"{d} {t}"}) for t in ("09:00:00", "18:00:00")]
        for d in ("2026-09-29", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-09"):
            P(d)
        assert c.get("/api/absence-rule").json()["start"] >= "2026-10-17"          # set on install: not retroactive
        assert c.post("/api/absence-rule", json={"start": "2026-10-01"}).json()["start"] == "2026-10-01"
        assert c.post("/api/absence-rule", json={"start": "nope"}).status_code == 400
        # 6 Oct: pending leave for a day that has passed; 7 Oct: rejected; 8 Oct: approved Informed
        c.post("/api/employees/AB/pin", json={"pin": "1234"})
        h = {"Authorization": "Bearer " + c.post("/api/app/login", json={"employee_id": "AB", "pin": "1234"}).json()["token"]}
        assert c.post("/api/app/leave", json={"start_date": "2026-10-06", "end_date": "2026-10-06", "type": "Adhoc", "consent_name": "Abid"}, headers=h).status_code == 200
        assert c.post("/api/app/leave", json={"start_date": "2026-10-07", "end_date": "2026-10-07", "type": "Adhoc", "consent_name": "Abid"}, headers=h).status_code == 200
        lv = {x["start_date"]: x["id"] for x in c.get("/api/leaves?month=2026-10").json()}
        c.post(f"/api/leaves/{lv['2026-10-07']}/reject")
        c.post("/api/leaves", json={"device_user_id": "AB", "start_date": "2026-10-08", "end_date": "2026-10-08", "type": "Informed", "override": True})
        day = lambda d: {r["device_user_id"]: r for r in c.get(f"/api/reports/daily?day={d}").json()}["AB"]
        # 5 Oct (no leave): the Adhoc credit pays for it
        r = day("2026-10-05")
        assert (r["status"], r.get("absent_adhoc"), r.get("lop")) == ("Absent", True, None) and "paid from the Adhoc balance" in r["note"]
        # 6 Oct (pending) and 7 Oct (rejected): no Adhoc left, 2x cut
        for d in ("2026-10-06", "2026-10-07"):
            r = day(d)
            assert (r["status"], r.get("absent_adhoc"), r.get("lop")) == ("Absent", True, 2.0) and "beyond balance" in r["note"]
        assert day("2026-10-08")["status"] == "Leave"                              # approved leave is leave
        r = day("2026-10-10")
        assert r["status"] == "Absent" and not r.get("absent_adhoc")               # today is not counted yet
        r = day("2026-09-30")
        assert r["status"] == "Absent" and not r.get("absent_adhoc")               # before the rule's start date
        # balance: Adhoc used by the 5th; the two beyond-balance days each used 1 Informed, plus the 8th's leave
        b = c.get("/api/leave-balance?employee=AB").json()
        assert (b["adhoc"]["used"], b["adhoc"]["left"], b["informed"]["used"], b["informed"]["left"]) == (1, 0, 3, 0)
        m = {x["device_user_id"]: x for x in c.get("/api/reports/monthly?month=2026-10").json()}["AB"]
        assert (m["absent"], m["absent_as_adhoc"], m["leave_cut_days"]) == (4, 3, 4.0)
        pay = {x["device_user_id"]: x for x in c.get("/api/reports/payroll?month=2026-10").json()}["AB"]
        assert pay["leave_cut"] == 4000.0
        # approving the pending leave later turns the 6th into leave and frees the count
        c.post(f"/api/leaves/{lv['2026-10-06']}/approve")
        assert day("2026-10-06")["status"] == "Leave"
        # in the time card grid the counted absences are flagged
        rows = {r["date"]: r for r in c.get("/api/reports/timecards", params={"start": "2026-10-05", "end": "2026-10-07", "employee": "AB"}).json()}
        assert rows["2026-10-05"]["absent_adhoc"] and rows["2026-10-07"]["absent_adhoc"] and not rows["2026-10-06"]["absent_adhoc"]
        # pay never goes below zero however many days are cut
        appdb.conn().execute("UPDATE settings SET value='2026-04-01' WHERE key='absent_as_adhoc_from'"); appdb.conn().commit()
        assert all(x["net_pay"] >= 0 for x in c.get("/api/reports/payroll?month=2026-10").json())


def test_editing_employee_details_and_changing_the_id():
    from app import db as appdb
    with TestClient(app) as c:
        c.post("/api/employees", json={"device_user_id": "ED1", "name": "Edna", "department": "Ops", "salary": 40000, "father_name": "Ed"})
        # an untidy value kept from an import (lenient) must not block other edits
        appdb.conn().execute("UPDATE employees SET pan='abc 123', dob='sometime 1990' WHERE device_user_id='ED1'"); appdb.conn().commit()
        emp = lambda: {e["device_user_id"]: e for e in c.get("/api/employees").json()}["ED1"]
        form = {**emp(), "name": "Edna Shah", "mobile": "9876543210"}
        form.pop("shift", None)
        assert c.post("/api/employees", json=form).status_code == 200
        e = emp()
        assert (e["name"], e["mobile"], e["pan"], e["dob"]) == ("Edna Shah", "9876543210", "abc 123", "sometime 1990")
        # a value being changed is still checked
        r = c.post("/api/employees", json={**form, "pan": "WRONG"})
        assert r.status_code == 400 and "PAN" in r.json()["detail"]
        assert c.post("/api/employees", json={**form, "pan": "abcde1234f", "dob": "15/03/1990"}).status_code == 200
        assert (emp()["pan"], emp()["dob"]) == ("ABCDE1234F", "1990-03-15")
        # salary and father's name left out (a team lead's form) stay as they were
        assert c.post("/api/employees", json={"device_user_id": "ED1", "name": "Edna Shah", "department": "Ops"}).status_code == 200
        assert (emp()["salary"], emp()["father_name"]) == (40000, "Ed")
        # changing the ID moves punches and leave with the person
        c.post("/api/punches", json={"device_user_id": "ED1", "ts": "2025-09-01 09:00:00"})
        c.post("/api/leaves", json={"device_user_id": "ED1", "start_date": "2025-09-02", "end_date": "2025-09-02", "type": "Informed", "override": True})
        c.post("/api/employees", json={"device_user_id": "ED2", "name": "Other", "department": "Ops"})
        r = c.post("/api/employees/ED1/change-id", json={"new_id": "ED2"})
        assert r.status_code == 409 and "Other" in r.json()["detail"]
        assert c.post("/api/employees/ED1/change-id", json={"new_id": "bad id!"}).status_code == 400
        assert c.post("/api/employees/ED1/change-id", json={"new_id": "0091827"}).json()["device_user_id"] == "91827"
        ids = {e["device_user_id"]: e["name"] for e in c.get("/api/employees").json()}
        assert ids.get("91827") == "Edna Shah" and "ED1" not in ids
        con = appdb.conn()
        assert con.execute("SELECT COUNT(*) FROM punches WHERE device_user_id='91827'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM leaves WHERE device_user_id='91827'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM punches WHERE device_user_id='ED1'").fetchone()[0] == 0


def test_performance_report_workbook(monkeypatch):
    import io
    from openpyxl import load_workbook
    _freeze_today(monkeypatch, 2026, 10, 15)
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "Perf 10-19", "start": "10:00", "end": "19:00", "grace_min": 10,
                                    "half_day_hours": 4, "ot_after_min": 0, "weekly_off": "SUN"})
        sid = [x for x in c.get("/api/shifts").json() if x["name"] == "Perf 10-19"][0]["id"]
        for uid, nm, br in (("PF1", "Perf One", "Mumbai"), ("PF2", "Perf Two", "Qutbi Arcade")):
            c.post("/api/employees", json={"device_user_id": uid, "name": nm, "department": "Perf", "designation": "Exec",
                                           "branch": br, "shift_id": sid, "joined": "2026-01-01"})
        for ts in ("2026-10-01 10:30:00", "2026-10-01 19:30:00", "2026-10-02 09:55:00", "2026-10-02 19:00:00"):
            c.post("/api/punches", json={"device_user_id": "PF1", "ts": ts})
        c.post("/api/leaves", json={"device_user_id": "PF1", "start_date": "2026-10-03", "end_date": "2026-10-03", "type": "Informed", "override": True})
        r = c.get("/api/reports/performance.xlsx", params={"start": "2026-10-01", "end": "2026-10-14", "department": "Perf"})
        assert r.status_code == 200
        wb = load_workbook(io.BytesIO(r.content))
        assert wb.sheetnames == ["Mumbai", "Qutbi Arcade"]
        ws = wb["Mumbai"]
        cells = {(row, col): ws.cell(row, col).value for row in range(1, ws.max_row + 1) for col in range(1, 33)}
        find = lambda v: next(k for k, x in cells.items() if x == v)
        assert "Monthly Performance Report" in cells.values()
        er, _ = find("PF1")
        assert cells[(er, 2)] == "Perf One" and cells[(er, 12)] == 2 and cells[(er, 18)] == 9 and cells[(er, 20)] == 1   # present, absent, leave
        status = find("Status")[0]
        assert [cells[(status, c)] for c in (2, 3, 4, 5)] == ["P-LT", "P", "IL", "WO"]   # 1 Oct late, 3 Oct leave, 4 Oct Sunday
        assert ws.cell(status, 4).fill.fgColor.rgb.endswith("FFEB9C")                     # Informed leave is yellow
        assert cells[(find("Arrived Time")[0], 2)] == "10:30" and cells[(find("Dept. Time")[0], 2)] == "19:30"
        assert c.get("/api/reports/performance.xlsx", params={"start": "2026-09-01", "end": "2026-10-14"}).status_code == 400


def test_delete_employee_for_good(tmp_path, monkeypatch):
    from app import db as appdb
    monkeypatch.setattr(config, "DOCS_DIR", str(tmp_path / "docs"))
    with TestClient(app) as c:
        c.post("/api/employees", json={"device_user_id": "DEL1", "name": "Gone Soon", "department": "Ops"})
        c.post("/api/employees", json={"device_user_id": "DEL2", "name": "Stays", "department": "Ops"})
        for uid in ("DEL1", "DEL2"):
            c.post("/api/punches", json={"device_user_id": uid, "ts": "2025-09-01 09:00:00"})
        c.post("/api/leaves", json={"device_user_id": "DEL1", "start_date": "2025-09-02", "end_date": "2025-09-02", "type": "Informed", "override": True})
        assert c.post("/api/employees/DEL1/documents", files={"file": ("id.pdf", b"%PDF-1.4 x")}, data={"doc_type": "Other"}).status_code == 200
        stored = appdb.conn().execute("SELECT stored FROM employee_documents WHERE device_user_id='DEL1'").fetchone()["stored"]
        assert (tmp_path / "docs" / stored).exists()
        assert c.post("/api/employees/DEL1/erase").json() == {"ok": True}
        assert "DEL1" not in {e["device_user_id"] for e in c.get("/api/employees").json()}
        con = appdb.conn()
        for t in ("punches", "leaves", "employee_documents", "employees"):
            assert con.execute(f"SELECT COUNT(*) FROM {t} WHERE device_user_id='DEL1'").fetchone()[0] == 0
        assert not (tmp_path / "docs" / stored).exists()
        assert con.execute("SELECT COUNT(*) FROM punches WHERE device_user_id='DEL2'").fetchone()[0] == 1   # others untouched
        assert c.post("/api/employees/DEL1/erase").status_code == 404


def test_new_joiner_has_no_leave_today_but_year_end_credit(monkeypatch):
    _freeze_today(monkeypatch, 2026, 10, 20)
    with TestClient(app) as c:
        c.post("/api/employees", json={"device_user_id": "NJ1", "name": "New Joiner", "department": "Ops", "joined": "2026-09-19"})
        y = {r["device_user_id"]: r for r in c.get("/api/leave-summary?fy=2026").json()}["NJ1"]
        assert (y["informed_earned"], y["adhoc_earned"], y["balance_now"]) == (0, 0, 0)          # nothing until 19 Dec
        assert (y["informed_credit"], y["adhoc_credit"]) == (7, 1)                               # Dec 2, Jan 1+1, Feb 2, Mar 2
        b = c.get("/api/leave-balance?employee=NJ1").json()
        assert (b["informed"]["left"], b["adhoc"]["left"], b["eligible_from"]) == (0, 0, "2026-12-19")


def test_today_counts_only_after_the_punch_out(monkeypatch):
    import io
    from openpyxl import load_workbook
    _freeze_today(monkeypatch, 2026, 10, 20)
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "Today 10-19", "start": "10:00", "end": "19:00", "weekly_off": "SUN"})
        sid = [x for x in c.get("/api/shifts").json() if x["name"] == "Today 10-19"][0]["id"]
        c.post("/api/employees", json={"device_user_id": "TD1", "name": "Today Person", "department": "TodayDept",
                                       "shift_id": sid, "joined": "2026-01-01"})
        for ts in ("2026-10-19 10:00:00", "2026-10-19 19:00:00", "2026-10-20 10:11:00"):
            c.post("/api/punches", json={"device_user_id": "TD1", "ts": ts})
        c.post("/api/punches", json={"device_user_id": "TD1", "ts": "2026-10-10 10:32:00"})   # a past day without punch-out
        month = lambda: {r["device_user_id"]: r for r in c.get("/api/reports/monthly?month=2026-10").json()}["TD1"]
        before = month()
        today = {r["device_user_id"]: r for r in c.get("/api/reports/daily?day=2026-10-20").json()}["TD1"]
        assert today["status"] == "Late" and today["in_progress"] and "no punch-out yet" in today["note"]   # still on the dashboard
        assert {r["device_user_id"]: r for r in c.get("/api/reports/daily?day=2026-10-10").json()}["TD1"]["status"] == "Late"
        grid = {r["device_user_id"]: r for r in c.get("/api/reports/grid?month=2026-10").json()["rows"]}["TD1"]
        assert grid["codes"][19] == "IN" and grid["totals"]["paid_days"] == before["paid_days"]
        wb = load_workbook(io.BytesIO(c.get("/api/reports/performance.xlsx", params={"start": "2026-10-20", "end": "2026-10-20",
                                                                                 "department": "TodayDept"}).content))
        ws = wb.active
        status = next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value == "Status")
        assert ws.cell(status, 2).value == "IN"
        c.post("/api/punches", json={"device_user_id": "TD1", "ts": "2026-10-20 19:05:00"})
        after = month()
        assert after["paid_days"] == before["paid_days"] + 1 and after["days_present"] == before["days_present"] + 1


def test_phone_app_month_attendance(monkeypatch):
    _freeze_today(monkeypatch, 2026, 10, 20)
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "App 10-19", "start": "10:00", "end": "19:00", "weekly_off": "SUN"})
        sid = [x for x in c.get("/api/shifts").json() if x["name"] == "App 10-19"][0]["id"]
        c.post("/api/employees", json={"device_user_id": "AP1", "name": "App User", "department": "Ops", "shift_id": sid, "joined": "2026-01-01"})
        c.post("/api/employees/AP1/pin", json={"pin": "4321"})
        tok = {"Authorization": "Bearer " + c.post("/api/app/login", json={"employee_id": "AP1", "pin": "4321"}).json()["token"]}
        for ts in ("2026-10-01 10:30:00", "2026-10-01 19:15:00", "2026-10-20 09:55:00"):
            c.post("/api/punches", json={"device_user_id": "AP1", "ts": ts})
        rows = {r["date"]: r for r in c.get("/api/app/attendance?month=2026-10", headers=tok).json()}
        assert len(rows) == 31
        assert (rows["2026-10-01"]["in"], rows["2026-10-01"]["out"], rows["2026-10-01"]["hours"], rows["2026-10-01"]["code"]) == ("10:30", "19:15", "8:45", "P-LT")
        assert rows["2026-10-04"]["code"] == "WO" and rows["2026-10-20"]["code"] == "IN" and rows["2026-10-25"]["code"] == ""
        assert c.get("/api/app/attendance?month=2026-13", headers=tok).status_code == 400
        assert c.get("/api/app/attendance").status_code == 401


def test_roster_whole_month_for_one_person():
    with TestClient(app) as c:
        c.post("/api/shifts", json={"name": "Roster 9:30-2:30", "start": "09:30", "end": "14:30", "weekly_off": "SUN"})
        sid = [x for x in c.get("/api/shifts").json() if x["name"] == "Roster 9:30-2:30"][0]["id"]
        c.post("/api/employees", json={"device_user_id": "RM1", "name": "Roster Month", "department": "HR", "shift_id": sid})
        cells = lambda: [r for r in c.get("/api/roster?start=2026-11-01&days=30").json()["rows"] if r["device_user_id"] == "RM1"][0]["cells"]
        r = c.post("/api/roster/range", json={"device_user_id": "RM1", "start": "2026-11-01", "end": "2026-11-30", "value": "off"})
        assert r.json()["days"] == 25                       # 30 days minus 5 Sundays (weekly offs untouched)
        got = cells()
        assert "2026-11-01" not in got and got["2026-11-02"] == "off" and len(got) == 25
        r = c.post("/api/roster/range", json={"device_user_id": "RM1", "start": "2026-11-01", "end": "2026-11-30", "value": ""})
        assert r.json()["days"] == 30 and cells() == {}       # Normal clears every day
        assert c.post("/api/roster/range", json={"device_user_id": "RM1", "start": "2026-11-01", "end": "2027-03-01", "value": "off"}).status_code == 400
