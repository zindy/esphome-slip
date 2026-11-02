#!/usr/bin/env python3
"""Userspace SLIP <-> TUN bridge.

Opens a serial port (the ESP32's USB bridge), creates a TUN interface with
the gateway address, and shuttles IPv4 packets between the two using SLIP
framing (RFC 1055). No kernel SLIP driver or slattach needed.
"""
import fcntl
import json
import os
import select
import struct
import subprocess
import sys
import time

import serial

END, ESC, ESC_END, ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD
TUNSETIFF = 0x400454CA
IFF_TUN, IFF_NO_PI = 0x0001, 0x1000

# Defaults, overridden by /data/options.json (add-on options)
OPTS = {
    "device": "/dev/ttyUSB0",
    "baud": 115200,
    "ifname": "slip0",
    "local_ip": "192.168.254.1",
    "peer_ip": "192.168.254.2",
    "mtu": 1500,
    "trace_port": 0,  # >0: log one line per TCP packet to/from this port (0 = off)
}


def load_options():
    try:
        with open("/data/options.json") as f:
            OPTS.update(json.load(f))
    except FileNotFoundError:
        pass  # running outside HA, use defaults / env


def log(msg):
    print(msg, flush=True)


def slip_encode(pkt):
    out = bytearray([END])
    for b in pkt:
        if b == END:
            out += bytes([ESC, ESC_END])
        elif b == ESC:
            out += bytes([ESC, ESC_ESC])
        else:
            out.append(b)
    out.append(END)
    return bytes(out)


class SlipDecoder:
    def __init__(self):
        self.buf = bytearray()
        self.esc = False

    def feed(self, data):
        for b in data:
            if b == END:
                if self.buf:
                    yield bytes(self.buf)
                self.buf = bytearray()
                self.esc = False
            elif self.esc:
                self.esc = False
                if b == ESC_END:
                    self.buf.append(END)
                elif b == ESC_ESC:
                    self.buf.append(ESC)
                else:
                    self.buf.append(b)
            elif b == ESC:
                self.esc = True
            else:
                self.buf.append(b)


def looks_like_ipv4(pkt):
    """Drop boot-ROM garbage and SLIPDIAG text frames."""
    return (
        len(pkt) >= 20
        and pkt[0] >> 4 == 4
        and int.from_bytes(pkt[2:4], "big") == len(pkt)
    )


def open_tun(name):
    fd = os.open("/dev/net/tun", os.O_RDWR)
    fcntl.ioctl(fd, TUNSETIFF, struct.pack("16sH", name.encode(), IFF_TUN | IFF_NO_PI))
    return fd


def ip(*args):
    subprocess.run(["ip", *args], check=True)


def open_serial():
    ser = serial.Serial()
    ser.port = OPTS["device"]
    ser.baudrate = int(OPTS["baud"])
    ser.timeout = 0
    # Keep DTR/RTS low so opening the port is less likely to reset the board
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def describe_tcp(pkt, direction):
    """Return (sport, dport, text) for an IPv4/TCP packet, else None."""
    ihl = (pkt[0] & 0x0F) * 4
    if pkt[9] != 6 or len(pkt) < ihl + 20:
        return None
    sport, dport, seq, ack = struct.unpack("!HHII", pkt[ihl:ihl + 12])
    off = (pkt[ihl + 12] >> 4) * 4
    flags = pkt[ihl + 13]
    win = struct.unpack("!H", pkt[ihl + 14:ihl + 16])[0]
    names = "".join(n for bit, n in ((0x02, "S"), (0x10, "A"), (0x08, "P"), (0x01, "F"), (0x04, "R")) if flags & bit)
    text = f"{direction} {names or '.'} seq={seq} ack={ack} len={len(pkt) - ihl - off} win={win}"
    return sport, dport, text


def trace(pkt, direction):
    port = int(OPTS.get("trace_port") or 0)
    if not port:
        return
    info = describe_tcp(pkt, direction)
    if info and port in (info[0], info[1]):
        t = time.time()
        log(f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int(t * 1000) % 1000:03d} {info[2]}")


STATS = {
    "to_esp_pkts": 0, "to_esp_bytes": 0, "to_esp_max": 0,
    "from_esp_pkts": 0, "from_esp_bytes": 0, "from_esp_max": 0,
    "discarded_frames": 0, "discarded_bytes": 0,
}


def bridge(tun, ser):
    dec = SlipDecoder()
    last_log = time.time()
    last_snapshot = None
    while True:
        r, _, _ = select.select([tun, ser.fileno()], [], [], 1.0)
        if tun in r:
            pkt = os.read(tun, 4096)
            trace(pkt, "to_esp  ")
            ser.write(slip_encode(pkt))
            STATS["to_esp_pkts"] += 1
            STATS["to_esp_bytes"] += len(pkt)
            STATS["to_esp_max"] = max(STATS["to_esp_max"], len(pkt))
        if ser.fileno() in r:
            data = ser.read(ser.in_waiting or 1)
            for pkt in dec.feed(data):
                if looks_like_ipv4(pkt):
                    trace(pkt, "from_esp")
                    os.write(tun, pkt)
                    STATS["from_esp_pkts"] += 1
                    STATS["from_esp_bytes"] += len(pkt)
                    STATS["from_esp_max"] = max(STATS["from_esp_max"], len(pkt))
                else:
                    STATS["discarded_frames"] += 1
                    STATS["discarded_bytes"] += len(pkt)
        # Every 10 s, log the counters if anything changed.
        if time.time() - last_log >= 10:
            last_log = time.time()
            snapshot = tuple(STATS.values())
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                log("stats: " + " ".join(f"{k}={v}" for k, v in STATS.items()))


def main():
    load_options()
    log(f"slip gateway: {OPTS}")
    tun = open_tun(OPTS["ifname"])
    ip("addr", "replace", OPTS["local_ip"], "peer", OPTS["peer_ip"], "dev", OPTS["ifname"])
    ip("link", "set", OPTS["ifname"], "mtu", str(OPTS["mtu"]), "up")
    log(f"{OPTS['ifname']} up: {OPTS['local_ip']} <-> {OPTS['peer_ip']}")

    while True:  # reconnect if the USB device disappears
        try:
            ser = open_serial()
            log(f"serial open: {OPTS['device']}")
            bridge(tun, ser)
        except (serial.SerialException, OSError) as e:
            log(f"serial error: {e}; retrying in 3 s")
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
