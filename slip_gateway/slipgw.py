#!/usr/bin/env python3
"""Userspace SLIP <-> TUN bridge.

Opens a serial port (the ESP32's USB bridge), creates a TUN interface with
the gateway address, and shuttles IPv4 packets between the two using SLIP
framing (RFC 1055). No kernel SLIP driver or slattach needed.
"""
import fcntl
import glob
import json
import os
import select
import signal
import socket
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
    "announce_name": "",  # e.g. "slip-node": answer mDNS for slip-node.local with peer_ip (empty = off)
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


def install_signal_handlers():
    """Exit cleanly (status 0) on SIGTERM/SIGINT.

    As PID 1 in the container, Python ignores SIGTERM unless a handler is
    installed. Stopping the app would then wait for the Supervisor's timeout
    and kill the process (exit status 137), which Home Assistant shows as
    "Error" instead of "Stopped".
    """
    def _exit(signum, frame):
        log("stopping")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT, _exit)


def start_announcer():
    """Optionally answer mDNS for <announce_name>.local with the peer's IP.

    The ESP32 cannot speak mDNS to the LAN, so tools that look the board up by
    name (the ESPHome dashboard's default install and status check) never find
    it. This publishes only the name -> address record, under a private
    service type, so Home Assistant's own ESPHome discovery is not triggered.
    The address is the point-to-point one, so only this host can reach it.
    """
    name = (OPTS.get("announce_name") or "").strip().rstrip(".")
    if name.endswith(".local"):
        name = name[: -len(".local")]
    if not name:
        return None
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf

        zc = Zeroconf(ip_version=IPVersion.V4Only)
        info = ServiceInfo(
            "_slipgateway._tcp.local.",
            f"{name}._slipgateway._tcp.local.",
            addresses=[socket.inet_aton(OPTS["peer_ip"])],
            port=6053,
            server=f"{name}.local.",
            properties={"peer": OPTS["peer_ip"]},
        )
        zc.register_service(info)
        log(f"mDNS: announcing {name}.local -> {OPTS['peer_ip']}")
        return zc
    except Exception as e:  # never let this optional feature stop the bridge
        log(f"mDNS announcement failed: {e}")
        return None


def resolve_device():
    """Return the serial port to use, or None.

    `device` may be a plain path or a glob, e.g.
    /dev/serial/by-id/usb-Silicon_Labs_CP2102*. A glob must match exactly one
    port: with several serial adapters attached the script refuses to guess,
    because SLIP written to the wrong port (a Zigbee stick, a meter reader)
    would break whatever uses it.
    """
    pattern = OPTS["device"]
    if any(c in pattern for c in "*?["):
        matches = sorted(glob.glob(pattern))
    else:
        matches = [pattern] if os.path.exists(pattern) else []
    if len(matches) > 1:
        log(f"device pattern {pattern!r} matches {len(matches)} ports: {matches}; not guessing")
        return None
    return matches[0] if matches else None


def list_candidates():
    log("serial ports visible to this app (use the by-id path as `device`):")
    by_id = {os.path.realpath(p): p for p in glob.glob("/dev/serial/by-id/*")}
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not ports:
        log("  (none)")
    for port in ports:
        log(f"  {by_id.get(os.path.realpath(port), port)} -> {os.path.realpath(port)}")


def open_serial(path):
    ser = serial.Serial()
    ser.port = path
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
    install_signal_handlers()
    log(f"slip gateway: {OPTS}")
    tun = open_tun(OPTS["ifname"])
    ip("addr", "replace", OPTS["local_ip"], "peer", OPTS["peer_ip"], "dev", OPTS["ifname"])
    ip("link", "set", OPTS["ifname"], "mtu", str(OPTS["mtu"]), "up")
    log(f"{OPTS['ifname']} up: {OPTS['local_ip']} <-> {OPTS['peer_ip']}")
    announcer = start_announcer()  # keep a reference so it stays alive

    list_candidates()
    last_listing = time.time()
    try:
        while True:  # reconnect if the USB device disappears
            try:
                path = resolve_device()
                if path is None:
                    if time.time() - last_listing >= 30:
                        log(f"serial device {OPTS['device']!r} not found or ambiguous")
                        list_candidates()
                        last_listing = time.time()
                    time.sleep(3)
                    continue
                ser = open_serial(path)
                log(f"serial open: {path}")
                bridge(tun, ser)
            except (serial.SerialException, OSError) as e:
                log(f"serial error: {e}; retrying in 3 s")
                time.sleep(3)
    finally:
        if announcer is not None:
            announcer.close()  # sends the mDNS goodbye so the name disappears


if __name__ == "__main__":
    sys.exit(main())