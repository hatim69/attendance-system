import os
import tempfile

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
