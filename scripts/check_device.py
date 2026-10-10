"""Probe a device: find open ports, read any banner, and try the ZK protocol on each open port.
Usage: python scripts/check_device.py 192.168.1.3 [comm_key] [extra_port ...]"""
import socket
import sys

ip = sys.argv[1]
key = int(sys.argv[2]) if len(sys.argv) > 2 else 0
extra = [int(p) for p in sys.argv[3:]]
CANDIDATES = [4370, 5005, 5010, 5000, 5001, 5002, 5003, 5004, 8000, 8080, 80, 443, 9922, 8089, 7005, 3000, 1470, 7788, 8888, 9999] + extra

open_ports = []
for port in dict.fromkeys(CANDIDATES):
    s = socket.socket(); s.settimeout(1.5)
    if s.connect_ex((ip, port)) == 0:
        open_ports.append(port)
        try:
            s.settimeout(2)
            banner = s.recv(128)
        except Exception:
            banner = b""
        print(f"port {port}: OPEN  banner={banner!r}")
    s.close()
if not open_ports:
    print("no candidate ports open")

try:
    from zk import ZK
except ImportError:
    sys.exit("pyzk not installed")

for port in open_ports:
    try:
        c = ZK(ip, port=port, timeout=8, password=key, ommit_ping=True).connect()
        print(f"ZK protocol OK on port {port}. serial:", c.get_serialnumber(), "| firmware:", c.get_firmware_version(),
              "| device:", c.get_device_name(), "| users:", len(c.get_users()), "| logs:", len(c.get_attendance()))
        c.disconnect()
        print(f"\n=> use ZK_DEVICES={ip}:{port}")
        break
    except Exception as e:
        print(f"ZK protocol on port {port}: FAILED ({e})")
