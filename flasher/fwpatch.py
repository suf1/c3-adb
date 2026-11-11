"""Patch Wi-Fi credentials into a prebuilt ESP32 app image.

The CI build compiles the firmware with fixed-length placeholder strings in
secrets.h.  At flash time we overwrite those placeholders in the .bin, then
recompute the ESP image checksum and the appended SHA-256 so the bootloader
accepts the modified image.

ESP app image layout (ESP-IDF):
  8-byte header, 16-byte extended header (byte 23 = append_digest flag),
  N segments of [load_addr u32][len u32][data],
  zero padding so that the checksum byte lands at offset == 15 (mod 16),
  1 checksum byte (XOR of all segment data, seed 0xEF),
  optional 32-byte SHA-256 of everything before the digest.
"""
import hashlib
import operator
import struct
from functools import reduce

SSID_MAX = 32
PASS_MAX = 63
SSID_SLOT = b"@@SSID_SLOT_" + b"x" * 18 + b"@@"
PASS_SLOT = b"@@PASS_SLOT_" + b"x" * 49 + b"@@"
assert len(SSID_SLOT) == SSID_MAX and len(PASS_SLOT) == PASS_MAX


def _layout(data):
    if len(data) < 32 or data[0] != 0xE9:
        raise ValueError("Not an ESP app image (bad magic byte).")
    nseg = data[1]
    append_digest = data[23]
    off = 24
    segs = []
    for _ in range(nseg):
        addr, ln = struct.unpack_from("<II", data, off)
        segs.append((off + 8, ln))
        off += 8 + ln
    cs_off = off + (15 - off % 16)
    return segs, cs_off, append_digest


def _checksum(data, segs):
    cs = 0xEF
    for start, ln in segs:
        cs = reduce(operator.xor, data[start:start + ln], cs)
    return cs


def fix_image(data):
    """Recompute checksum byte and SHA-256 digest in place (bytearray)."""
    segs, cs_off, append_digest = _layout(data)
    data[cs_off] = _checksum(data, segs)
    if append_digest:
        data[cs_off + 1:cs_off + 33] = hashlib.sha256(bytes(data[:cs_off + 1])).digest()
    return data


def check_image(data):
    """True if checksum (and digest, if present) are valid."""
    segs, cs_off, append_digest = _layout(data)
    if data[cs_off] != _checksum(data, segs):
        return False
    if append_digest:
        want = hashlib.sha256(bytes(data[:cs_off + 1])).digest()
        return bytes(data[cs_off + 1:cs_off + 33]) == want
    return True


def _replace_all(buf, slot, value):
    repl = value + b"\x00" * (len(slot) - len(value))
    count = 0
    pos = buf.find(slot)
    while pos != -1:
        buf[pos:pos + len(slot)] = repl
        count += 1
        pos = buf.find(slot, pos + len(slot))
    return count


def patch_firmware(firmware, ssid, password):
    ssid_b = ssid.encode("utf-8")
    pass_b = password.encode("utf-8")
    if not ssid_b:
        raise ValueError("SSID cannot be empty.")
    if len(ssid_b) > SSID_MAX:
        raise ValueError("SSID is longer than %d bytes." % SSID_MAX)
    if len(pass_b) > PASS_MAX:
        raise ValueError("Wi-Fi password is longer than %d bytes." % PASS_MAX)
    buf = bytearray(firmware)
    if not check_image(buf):
        raise ValueError("Bundled firmware image failed its own checksum.")
    n1 = _replace_all(buf, SSID_SLOT, ssid_b)
    n2 = _replace_all(buf, PASS_SLOT, pass_b)
    if n1 == 0 or n2 == 0:
        raise ValueError(
            "Wi-Fi placeholder not found in firmware (ssid=%d, pass=%d). "
            "The firmware was probably built without the placeholder secrets.h." % (n1, n2))
    fix_image(buf)
    if not check_image(buf):
        raise ValueError("Patched image failed verification.")
    return bytes(buf)
