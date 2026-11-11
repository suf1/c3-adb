# C3 AdBlock Flasher (Windows .exe)

Plug in an ESP32-C3 SuperMini, type your Wi-Fi name and password (and optionally a dashboard
password), click **FLASH**. The .exe contains everything: firmware, bootloader, partition table,
the ~253K-domain blocklist (StevenBlack + Hagezi Multi PRO), the LittleFS image tool and esptool.
No Python, PlatformIO or drivers to install.

It flashes the firmware from https://github.com/9M2PJU/9M2PJU-ESP32-C3-AdBlocker in its
single-app layout (big blocklist, no firmware OTA, blocklist OTA still works).

## 1. Get the .exe (one-time, about 10 minutes, no software to install)

The .exe is built by GitHub, so you never compile anything.

1. On github.com create a new **public** repository, e.g. `c3-flasher`.
2. Upload everything from this folder to it (drag the files in, keep the folder structure:
   `.github/`, `ci/`, `flasher/`, `tests/`, `README.md`, `.gitignore`).
3. Open the **Actions** tab, enable workflows if asked, choose **Build flasher exe**, **Run workflow**.
4. Wait for the green tick (about 10-15 minutes the first time).
5. Download `C3-AdBlock-Flasher.exe` from the repo's **Releases** page (tag `flasher-latest`),
   or from the run's Artifacts.

The workflow rebuilds every Monday, so the downloaded .exe always contains a fresh blocklist and
the latest upstream firmware.

If a step fails, the log tells you why; the checks that stop a bad build are:
placeholder `secrets.h` creation, firmware build, blocklist fit test, "Wi-Fi placeholders present
in firmware", and a self-test of the finished .exe.

## 2. Use it

1. Double-click `C3-AdBlock-Flasher.exe` (Windows SmartScreen may warn because the file is
   unsigned: **More info, Run anyway**).
2. Plug the board in with a data-capable USB cable. It appears in the COM port list.
3. Enter Wi-Fi name (2.4 GHz) and password. Optionally enter a dashboard password (8+ characters).
4. Click **FLASH**. It takes about 1-2 minutes.
5. When it finishes, the window shows the dashboard URL and logs in to check your password.
   Set your router's DNS to the device IP and give the device a DHCP reservation.

If it cannot connect: hold **BOOT**, tap **RESET**, release **BOOT**, click FLASH again.

## 3. Updating later

* **Blocklist, no cable:** dashboard > Blocklist > Upload, or Remote auto-update (see your
  earlier setup; HTTPS needs free RAM, so a smaller Bloom filter may be needed).
* **Everything (new firmware + list):** download the newest .exe and flash again.
  "Erase all flash" wipes saved settings; untick it only if you know you want to keep them.

## Customising

Edit the `env:` block at the top of `.github/workflows/build-flasher.yml`:

* `BLOCKLIST_SOURCES` : space-separated list URLs
* `BLOCKLIST_EXTRA_ARGS` : extra options for `tools/build_blocklist.py` (e.g. Bloom size; run it
  with `--help` to see the flag name)
* `UPSTREAM_REF` : pin a specific upstream tag or commit instead of `main`

## How it works (and what is assumed)

* **Wi-Fi:** the build compiles firmware with fixed-length placeholder strings in `secrets.h`.
  At flash time the tool overwrites them and recomputes the ESP image checksum and SHA-256.
  This is unit-tested against esptool's own image validator. If the firmware does not embed the
  strings as literals, the build fails at "Wi-Fi placeholders present" instead of shipping a
  broken .exe.
* **Dashboard password:** written to `/auth.cfg` in the filesystem image as plain text, which is
  an assumption about the firmware. The tool logs in after flashing to prove it worked; if it
  reports "rejected", flash again with the password box blank and use the password shown.
* **Filesystem:** built with the same `mklittlefs` PlatformIO uses, with the partition size read
  from `partitions-single.csv` at build time.

## Files

    flasher/app.py        GUI, flashing logic, self-test
    flasher/fwpatch.py    firmware placeholder patcher + checksum/SHA fix
    ci/make_secrets.py    creates the placeholder secrets.h for the CI build
    ci/collect_assets.py  gathers firmware/tools/blocklist + manifest into the .exe
    tests/test_fwpatch.py patcher test (uses esptool image_info as validator)
    .github/workflows/build-flasher.yml  the build

MIT-licensed upstream project; keep its license notice if you redistribute the firmware.
