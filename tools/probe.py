#!/usr/bin/env python3
"""Probe: open a second AAP channel to the AirPods and dump head-tracking packets."""
import socket, sys, time, struct
MAC = sys.argv[1]
s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
s.settimeout(5)
s.connect((MAC, 0x1001))
print("connected")
H = bytes.fromhex("04000400")
s.send(bytes.fromhex("00000400010002000000000000000000"))  # handshake
time.sleep(0.2)
s.send(H + bytes.fromhex("4d00d700000000000000"))  # feature flags
s.send(H + bytes.fromhex("0f00ffffffff"))          # notifications
time.sleep(0.3)
start = {"a": "1700000010000f000873420b081010021a0501409c0000",
         "n": "170000001000100008a102420b080e10021a0501409c0000"}[sys.argv[2] if len(sys.argv) > 2 else "a"]
s.send(H + bytes.fromhex(start))
t0 = time.time(); n = 0
s.settimeout(1)
while time.time() - t0 < 6:
    try: d = s.recv(1024)
    except socket.timeout: continue
    if d[:5] == bytes.fromhex("0400040017") and len(d) > 60:
        n += 1
        o1, o2, o3 = struct.unpack_from("<hhh", d, 43)
        h, v = struct.unpack_from("<hh", d, 51)
        if n % 10 == 1 or n < 3 or len(d)!=81 and n%3==0: print(len(d), d.hex())
    else:
        print("other", d[:12].hex())
print(f"{n} HT packets in 6s -> {n/6:.1f} Hz")
s.send(H + bytes.fromhex("1700000010000f000875420b081010021a050100000000"))
s.close()
