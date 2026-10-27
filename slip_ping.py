#!/usr/bin/env python3
"""Talk SLIP to the ESP32 directly (no slattach / kernel SLIP needed).

Sends ICMP echo requests as SLIP frames and prints every frame that comes back,
including the firmware's SLIPDIAG status lines.

    pip install pyserial
    python3 slip_ping.py /dev/ttyUSB0          # stop slattach/picocom first
"""
import socket, struct, sys, time
import serial

END, ESC, ESC_END, ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD
SRC, DST = "192.168.254.1", "192.168.254.2"


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def build_ping(seq: int) -> bytes:
    payload = b"slip-test-%02d" % seq
    icmp = struct.pack("!BBHHH", 8, 0, 0, 0x1234, seq) + payload
    icmp = icmp[:2] + struct.pack("!H", checksum(icmp)) + icmp[4:]
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(icmp), seq, 0, 64, 1, 0,
                      socket.inet_aton(SRC), socket.inet_aton(DST))
    hdr = hdr[:10] + struct.pack("!H", checksum(hdr)) + hdr[12:]
    return hdr + icmp


def slip_encode(pkt: bytes) -> bytes:
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


def slip_decode(frame: bytes) -> bytes:
    out, esc = bytearray(), False
    for b in frame:
        if esc:
            out.append(END if b == ESC_END else ESC if b == ESC_ESC else b)
            esc = False
        elif b == ESC:
            esc = True
        else:
            out.append(b)
    return bytes(out)


def describe(frame: bytes) -> str:
    if len(frame) >= 20 and frame[0] >> 4 == 4:
        proto = frame[9]
        src, dst = socket.inet_ntoa(frame[12:16]), socket.inet_ntoa(frame[16:20])
        ihl = (frame[0] & 0xF) * 4
        extra = ""
        if proto == 1 and len(frame) > ihl:
            extra = " ICMP type %d%s" % (frame[ihl], "  <-- ECHO REPLY" if frame[ihl] == 0 else "")
        return "IP %s -> %s proto %d, %d bytes%s" % (src, dst, proto, len(frame), extra)
    return "TEXT/other: " + frame.decode("ascii", "replace").strip()


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, 115200, 0.1
    ser.dtr = ser.rts = False
    ser.open()
    print("Opened %s; waiting 4 s for the ESP32 to boot..." % port)
    time.sleep(4)
    ser.reset_input_buffer()

    buf, seq, last_tx, end = bytearray(), 0, 0.0, time.time() + 15
    while time.time() < end:
        if time.time() - last_tx > 2 and seq < 5:
            seq += 1
            ser.write(slip_encode(build_ping(seq)))
            print(">> sent ICMP echo request #%d" % seq)
            last_tx = time.time()
        for b in ser.read(256):
            if b == END:
                if buf:
                    print("<< " + describe(slip_decode(bytes(buf))))
                buf.clear()
            else:
                buf.append(b)


if __name__ == "__main__":
    main()
