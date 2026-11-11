"""Collect firmware, blocklist and tools into flasher/assets and write manifest.json.

usage: python ci/collect_assets.py <fw_dir> <env_name> <blocklist.bin> <assets_dir>
"""
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import time


def find_one(patterns):
    for pat in patterns:
        hits = glob.glob(os.path.expanduser(pat), recursive=True)
        if hits:
            return hits[0]
    raise SystemExit("not found: " + " | ".join(patterns))


def parse_partitions(path):
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        cols = [c.strip() for c in line.split(",")]
        if len(cols) >= 5:
            rows.append(cols)
    return rows


def main(fw_dir, env, blocklist, assets):
    os.makedirs(assets, exist_ok=True)
    build = os.path.join(fw_dir, ".pio", "build", env)
    for name in ("bootloader.bin", "partitions.bin", "firmware.bin"):
        shutil.copy(os.path.join(build, name), os.path.join(assets, name))

    boot_app0 = find_one(["~/.platformio/packages/framework-arduinoespressif32*/tools/partitions/boot_app0.bin"])
    shutil.copy(boot_app0, os.path.join(assets, "boot_app0.bin"))
    mk = find_one(["~/.platformio/packages/tool-mklittlefs*/mklittlefs.exe",
                   "~/.platformio/packages/tool-mklittlefs*/mklittlefs"])
    shutil.copy(mk, os.path.join(assets, os.path.basename(mk)))
    shutil.copy(blocklist, os.path.join(assets, "blocklist.bin"))

    rows = parse_partitions(os.path.join(fw_dir, "partitions-single.csv"))
    app = next(r for r in rows if r[1] == "app")
    fs = next(r for r in rows if r[1] == "data" and r[2] in ("spiffs", "littlefs"))
    app_off, fs_off, fs_size = int(app[3], 0), int(fs[3], 0), int(fs[4], 0)
    if app_off != 0x10000:
        raise SystemExit("unexpected app offset 0x%x (expected 0x10000)" % app_off)

    with open(blocklist, "rb") as f:
        head = f.read(20)
    if head[:4] != b"CADB":
        raise SystemExit("blocklist.bin has no CADB header")
    domains = struct.unpack_from("<I", head, 6)[0]
    bl_size = os.path.getsize(blocklist)

    commit = subprocess.run(["git", "-C", fw_dir, "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    manifest = {
        "built_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "upstream": os.environ.get("UPSTREAM_REPO", ""),
        "upstream_ref": os.environ.get("UPSTREAM_REF", ""),
        "upstream_commit": commit,
        "env": env,
        "app_offset": app_off,
        "fs_offset": fs_off,
        "fs_size": fs_size,
        "blocklist_domains": domains,
        "blocklist_bytes": bl_size,
        "mklittlefs": os.path.basename(mk),
        "sources": os.environ.get("BLOCKLIST_SOURCES", ""),
    }
    json.dump(manifest, open(os.path.join(assets, "manifest.json"), "w"), indent=2)
    print(json.dumps(manifest, indent=2))
    if bl_size > fs_size * 0.95:
        raise SystemExit("blocklist (%d bytes) does not fit the filesystem (%d bytes)" % (bl_size, fs_size))


if __name__ == "__main__":
    main(*sys.argv[1:5])
