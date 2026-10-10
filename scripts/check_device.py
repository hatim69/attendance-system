"""Probe a device: which ports are open and whether it speaks the ZK protocol.
Usage: python scripts/check_device.py 192.168.1.201 [comm_key]"""
import socket
import sys

ip = sys.argv[1]
key = int(sys.argv[2]) if len(sys.argv) > 2 else 0
for port in (4370, 80, 8080, 5005, 9922):
    s = socket.socket(); s.settimeout(2)
    print(f"port {port}:", "OPEN" if s.connect_ex((ip, port)) == 0 else "closed"); s.close()
try:
    from zk import ZK
    c = ZK(ip, port=4370, timeout=8, password=key, ommit_ping=True).connect()
    print("ZK protocol OK. serial:", c.get_serialnumber(), "| firmware:", c.get_firmware_version(),
          "| device:", c.get_device_name(), "| users:", len(c.get_users()), "| logs:", len(c.get_attendance()))
    c.disconnect()
except Exception as e:
    print("ZK protocol FAILED:", e)
