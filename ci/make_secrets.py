"""Create src/secrets.h from secrets.example.h with fixed-length placeholders.

usage: python ci/make_secrets.py <secrets.example.h> <secrets.h>
Handles both styles:  #define WIFI_SSID "x"   and   const char* WIFI_SSID = "x";
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "flasher"))
import fwpatch


def set_value(text, name, value):
    rx_define = re.compile(r'(?m)^([ \t]*#define[ \t]+%s[ \t]+).*$' % name)
    rx_assign = re.compile(r'(?m)^([ \t]*[A-Za-z_][\w \t\*:<>]*?\b%s[ \t]*=[ \t]*)"[^"\r\n]*"([ \t]*;.*)$' % name)
    if rx_define.search(text):
        return rx_define.sub(lambda m: m.group(1) + '"' + value + '"', text, count=1)
    if rx_assign.search(text):
        return rx_assign.sub(lambda m: m.group(1) + '"' + value + '"' + m.group(2), text, count=1)
    return None


def main(src, dst):
    text = open(src, encoding="utf-8").read()
    for name, slot in (("WIFI_SSID", fwpatch.SSID_SLOT), ("WIFI_PASS", fwpatch.PASS_SLOT)):
        new = set_value(text, name, slot.decode())
        if new is None:
            print("ERROR: could not find %s in %s. File content follows:\n" % (name, src))
            print(text)
            sys.exit(1)
        text = new
    open(dst, "w", encoding="utf-8", newline="").write(text)
    print("wrote", dst)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
