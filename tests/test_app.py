import os
import tempfile

os.environ["FKWEB_PORT"] = "0"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_end_to_end():
    with TestClient(app) as c:
        c.post("/api/employees", json={"device_user_id": "1", "name": "Alice", "department": "Eng"})
        c.post("/api/employees", json={"device_user_id": "2", "name": "Bob"})
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
    monkeypatch.setattr(fkweb, "RAW_LOG", str(tmp_path / "raw.log"))

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
