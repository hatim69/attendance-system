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
        # holidays + leaves (CL -> Casual) + duplicate leave skipped
        assert up("holidays", "h.csv", _csv_bytes([["Date", "Name"], ["15/08/2025", "Independence Day"]]), False).json()["created"] == 1
        lv = _csv_bytes([["Emp Code", "From Date", "To Date", "Leave Name", "Reason"], ["9001", "2025-08-11", "2025-08-12", "CL", "trip"], ["777777", "2025-08-11", "2025-08-11", "CL", ""]])
        r = up("leaves", "l.csv", lv, False).json()
        assert (r["created"], r["skipped"]) == (1, 1) and "no employee" in r["errors"][0]["message"]
        assert up("leaves", "l.csv", lv, False).json()["created"] == 0
        assert c.get("/api/leaves?month=2025-08").json()[0]["type"] == "Casual"
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
        assert mgr.post("/api/punches", json={"device_user_id": "A1", "ts": "2025-09-02 09:00:00"}).status_code == 403
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


def test_management_role_and_bulk_shift_and_new_employee_fields(tmp_path, monkeypatch):
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
        # management: everything read-only across ALL departments, never salaries or payroll
        boss = TestClient(app); boss.headers.update(H)
        assert boss.post("/api/auth/login", json={"username": "boss", "password": "boss-password-1"}).status_code == 200
        emps = boss.get("/api/employees").json()
        assert {x["device_user_id"] for x in emps} == {"1", "2", "3"} and all(x["salary"] == 0 for x in emps)
        assert boss.get("/api/reports/payroll?month=2025-09").status_code == 403
        assert boss.post("/api/employees/bulk-shift", json={"ids": ["1"], "shift_id": sid}).status_code == 403
        assert boss.get("/api/dashboard").json()["total"] == 3
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
