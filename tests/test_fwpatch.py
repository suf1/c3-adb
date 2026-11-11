import hashlib, operator, os, struct, subprocess, sys, tempfile
from functools import reduce

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "flasher"))
import fwpatch


def make_image():
    """Build a synthetic ESP32-C3 app image with the placeholders embedded."""
    app_desc = struct.pack("<II", 0xABCD5432, 0) + b"\x00" * 248
    drom = bytearray(app_desc + b"\x11" * 64 + fwpatch.SSID_SLOT + b"\x00\x22" + fwpatch.PASS_SLOT + b"\x00" * 3)
    drom += b"\x33" * (-len(drom) % 4)
    iram = bytearray(b"\x44" * 100)
    hdr = struct.pack("<BBBBI", 0xE9, 2, 2, 0x10, 0x40380000)
    ext = struct.pack("<BBBBHBHHIB", 0xEE, 0, 0, 0, 5, 0, 0, 0xFFFF, 0, 1)[:16]
    ext = bytearray(16)
    ext[0] = 0xEE
    struct.pack_into("<H", ext, 4, 5)          # chip id = ESP32-C3
    struct.pack_into("<H", ext, 8, 0xFFFF)     # max chip rev
    ext[15] = 1                                # append digest
    body = bytearray(hdr + ext)
    cs = 0xEF
    for addr, seg in ((0x3C010020, drom), (0x40380000, iram)):
        body += struct.pack("<II", addr, len(seg)) + seg
        cs = reduce(operator.xor, seg, cs)
    body += b"\x00" * (15 - len(body) % 16)
    body += bytes([cs])
    body += hashlib.sha256(bytes(body)).digest()
    return bytes(body)


def esptool_info(path):
    r = subprocess.run([sys.executable, "-m", "esptool", "image_info", "--version", "2", path],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def main():
    img = make_image()
    assert fwpatch.check_image(bytearray(img)), "synthetic image invalid"
    out = fwpatch.patch_firmware(img, "My Home WiFi", "p@ss w0rd!$%")
    assert out != img and len(out) == len(img)
    assert fwpatch.SSID_SLOT not in out and fwpatch.PASS_SLOT not in out
    assert b"My Home WiFi\x00" in out and b"p@ss w0rd!$%\x00" in out
    assert fwpatch.check_image(bytearray(out))
    with tempfile.TemporaryDirectory() as d:
        a, b = os.path.join(d, "orig.bin"), os.path.join(d, "patched.bin")
        open(a, "wb").write(img); open(b, "wb").write(out)
        for name, p in (("orig", a), ("patched", b)):
            rc, txt = esptool_info(p)
            print("---- esptool image_info:", name, "rc=", rc)
            print("\n".join(l for l in txt.splitlines() if any(k in l for k in ("hecksum", "alidation", "rror", "hash"))))
            assert rc == 0 and "invalid" not in txt.lower(), txt
    # empty password (open network) and error paths
    fwpatch.patch_firmware(img, "Open", "")
    for bad in (("", "x"), ("a" * 33, "x"), ("a", "b" * 64)):
        try:
            fwpatch.patch_firmware(img, *bad); raise SystemExit("should have failed: %r" % (bad,))
        except ValueError:
            pass
    try:
        fwpatch.patch_firmware(out, "x", "y"); raise SystemExit("placeholder reuse should fail")
    except ValueError:
        pass
    print("ALL PATCH TESTS PASSED")


if __name__ == "__main__":
    main()
