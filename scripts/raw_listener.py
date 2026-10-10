"""Raw TCP listener for FkWeb devices that send non-standard HTTP. Logs every byte received to
fkweb_raw.log and answers with a minimal HTTP 200. No dependencies.
Usage: python scripts/raw_listener.py [port]   (default 80)"""
import asyncio
import json
import re
import sys
from datetime import datetime

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 80
LOG = "fkweb_raw.log"


def log(rec: dict):
    rec["at"] = datetime.now().isoformat(timespec="seconds")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def body_len(head: bytes) -> int:
    m = re.search(rb"(?im)^(?:content-length|body_len)\s*:\s*(\d+)", head)
    return int(m.group(1)) if m else 0


def header_val(head: bytes, name: bytes) -> bytes:
    m = re.search(rb"(?im)^" + name + rb"\s*:\s*([^\r\n]*)", head)
    return m.group(1) if m else b""


async def handle(reader, writer):
    peer = writer.get_extra_info("peername")
    buf = b""
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=10)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
            log({"peer": f"{peer[0]}:{peer[1]}", "chunk_len": len(chunk),
                 "text": chunk[:1500].decode("latin-1"), "hex": chunk[:300].hex()})
            print(datetime.now().strftime("%H:%M:%S"), peer[0], len(chunk), "bytes:", chunk[:120])
            end = buf.find(b"\r\n\r\n")
            if end == -1:
                continue
            head = buf[:end]
            if len(buf) - (end + 4) < body_len(head):
                continue  # wait for the rest of the body
            buf = buf[end + 4 + body_len(head):]
            trans = header_val(head, b"trans_id") or b"0"
            resp = (b"HTTP/1.1 200 OK\r\nresponse_code: OK\r\ntrans_id: " + trans +
                    b"\r\ncmd_id: 0\r\nContent-Length: 0\r\n\r\n")
            writer.write(resp)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    print(f"Listening on port {PORT}. Logging to {LOG}. Scan a finger on the device now. Ctrl+C to stop.")
    async with server:
        await server.serve_forever()


asyncio.run(main())
