"""C3 AdBlock Flasher - plug in an ESP32-C3, fill in 3 fields, click Flash.

Everything needed (firmware, bootloader, partition table, blocklist, mklittlefs,
esptool) is bundled into the executable by the CI workflow.
"""
import base64
import io
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import serial
from serial.tools import list_ports

import fwpatch

APP_NAME = "C3 AdBlock Flasher"
ESPRESSIF_VID = 0x303A
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------- assets
def base_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def asset(name):
    return os.path.join(base_dir(), "assets", name)


def load_manifest():
    with open(asset("manifest.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def settings_path():
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(root, "C3AdBlockFlasher", "settings.json")


def load_settings():
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d):
    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


# --------------------------------------------------------------------- helpers
class LineWriter(io.TextIOBase):
    """File-like object that forwards complete lines (\\n or \\r separated)."""

    def __init__(self, cb):
        self.cb, self.buf, self._in = cb, "", False

    def writable(self):
        return True

    def write(self, s):
        if self._in:          # ignore output produced by the callback itself
            return len(s)
        self._in = True
        try:
            self.buf += s
            while True:
                m = re.search(r"[\r\n]", self.buf)
                if not m:
                    break
                line, self.buf = self.buf[:m.start()], self.buf[m.end():]
                if line.strip():
                    self.cb(line)
        finally:
            self._in = False
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def run_esptool(argv, on_line):
    import esptool
    old = (sys.stdout, sys.stderr)
    sys.stdout = sys.stderr = LineWriter(on_line)
    try:
        esptool.main(argv)
    except SystemExit as e:
        if e.code not in (0, None):
            raise RuntimeError("esptool exited with code %s" % e.code)
    finally:
        sys.stdout, sys.stderr = old


def list_ports_info():
    out = []
    for p in list_ports.comports():
        is_esp = p.vid == ESPRESSIF_VID
        out.append((0 if is_esp else 1, p.device, "%s - %s%s" % (p.device, p.description or "serial port",
                                                                  "  [ESP32-C3]" if is_esp else "")))
    out.sort()
    return [(dev, label) for _, dev, label in out]


def build_fs_image(workdir, fs_size, blocklist_path, dash_password, log):
    data = os.path.join(workdir, "data")
    os.makedirs(data, exist_ok=True)
    shutil.copy(blocklist_path, os.path.join(data, "blocklist.bin"))
    if dash_password:
        with open(os.path.join(data, "auth.cfg"), "wb") as f:
            f.write(dash_password.encode("ascii"))
    used = sum(os.path.getsize(os.path.join(data, n)) for n in os.listdir(data))
    if used > fs_size * 0.95:
        raise RuntimeError("Blocklist (%.2f MB) does not fit the %.2f MB filesystem." % (used / 1048576, fs_size / 1048576))
    out = os.path.join(workdir, "fs.bin")
    exe = asset(load_manifest().get("mklittlefs", "mklittlefs.exe"))
    cmd = [exe, "-c", data, "-p", "256", "-b", "4096", "-s", str(fs_size), out]
    log("Building filesystem image (%.2f MB of data)..." % (used / 1048576))
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("mklittlefs failed: " + (r.stderr or r.stdout))
    if os.path.getsize(out) != fs_size:
        raise RuntimeError("Filesystem image has unexpected size %d (wanted %d)." % (os.path.getsize(out), fs_size))
    return out


def capture_boot(port, seconds, on_line, stop):
    """Read the device's serial log; return (ip, generated_password)."""
    ip, pw, t0, ser, buf = None, None, time.time(), None, ""
    ip_at = None
    while time.time() - t0 < seconds and not stop.is_set():
        if ser is None:
            try:
                ser = serial.Serial()
                ser.port, ser.baudrate, ser.timeout = port, 115200, 0.5
                ser.dtr = False
                ser.rts = False
                ser.open()
            except Exception:
                ser = None
                time.sleep(0.5)
                continue
        try:
            chunk = ser.read(256)
        except Exception:
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            time.sleep(0.5)
            continue
        if chunk:
            buf += chunk.decode("utf-8", "replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if line:
                    on_line("device: " + line)
                m = re.search(r"WiFi up:\s*(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    ip, ip_at = m.group(1), time.time()
                m = re.search(r"dashboard password:\s*(\S+)", line)
                if m:
                    pw = m.group(1)
        if ip and time.time() - ip_at > 3:
            break
    if ser:
        try:
            ser.close()
        except Exception:
            pass
    return ip, pw


def check_login(host, password):
    """True/False if the dashboard accepts the password, None if unreachable."""
    token = base64.b64encode(("admin:" + password).encode()).decode()
    req = urllib.request.Request("http://%s/" % host, headers={"Authorization": "Basic " + token})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403) else None
    except Exception:
        return None


# --------------------------------------------------------------------- worker
def flash_job(cfg, q, stop):
    log = lambda s: q.put(("log", s))
    prog = lambda v: q.put(("progress", v))
    result = {"ip": None, "password": None, "login": None}
    workdir = tempfile.mkdtemp(prefix="c3flash_")
    try:
        man = load_manifest()
        prog(2)
        log("Patching Wi-Fi settings into firmware...")
        with open(asset("firmware.bin"), "rb") as f:
            fw = fwpatch.patch_firmware(f.read(), cfg["ssid"], cfg["wifi_pass"])
        fw_path = os.path.join(workdir, "firmware.bin")
        with open(fw_path, "wb") as f:
            f.write(fw)

        bl = cfg["blocklist_path"] or asset("blocklist.bin")
        fs_path = build_fs_image(workdir, man["fs_size"], bl, cfg["dash_pass"], log)
        prog(6)

        files = [(0x0, asset("bootloader.bin")), (0x8000, asset("partitions.bin")),
                 (0xE000, asset("boot_app0.bin")), (man["app_offset"], fw_path), (man["fs_offset"], fs_path)]
        sizes = [os.path.getsize(p) for _, p in files]
        total = float(sum(sizes))
        state = {"idx": -1}

        def on_es(line):
            log(line)
            if line.startswith("Compressed"):
                state["idx"] += 1
            m = re.search(r"Writing at 0x[0-9a-fA-F]+\.\.\. \((\d+) %\)", line)
            if m and 0 <= state["idx"] < len(files):
                done = sum(sizes[:state["idx"]]) + sizes[state["idx"]] * int(m.group(1)) / 100.0
                prog(12 + 80 * done / total)

        base = ["--chip", "esp32c3", "--port", cfg["port"], "--baud", str(cfg["baud"])]
        if cfg["erase"]:
            log("Erasing flash...")
            run_esptool(base + ["erase_flash"], on_es)
        prog(12)
        log("Writing firmware, partition table, blocklist...")
        args = base + ["--after", "hard_reset", "write_flash", "--flash_mode", "keep", "--flash_freq", "keep",
                       "--flash_size", "keep"]
        for off, path in files:
            args += [hex(off), path]
        run_esptool(args, on_es)
        prog(93)

        log("Flash complete. Waiting for the device to boot and join Wi-Fi (up to 60 s)...")
        ip, gen_pw = capture_boot(cfg["port"], 60, log, stop)
        if not ip:
            try:
                ip = socket.gethostbyname("c3adblock.local")
            except Exception:
                ip = None
        result["ip"], result["password"] = ip, gen_pw
        prog(97)
        pw_to_test = cfg["dash_pass"] or gen_pw
        if ip and pw_to_test:
            result["login"] = check_login(ip, pw_to_test)
        prog(100)
        q.put(("done", result))
    except Exception as e:
        log(traceback.format_exc())
        q.put(("error", str(e)))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------- self-test
def selftest():
    lines, ok = [], True

    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        lines.append("%s %s %s" % ("PASS" if cond else "FAIL", name, extra))

    try:
        man = load_manifest()
        for n in ("bootloader.bin", "partitions.bin", "boot_app0.bin", "firmware.bin", "blocklist.bin", man["mklittlefs"]):
            check("asset " + n, os.path.exists(asset(n)), "%d bytes" % (os.path.getsize(asset(n)) if os.path.exists(asset(n)) else 0))
        with open(asset("firmware.bin"), "rb") as f:
            fw = f.read()
        patched = fwpatch.patch_firmware(fw, "SelfTestNet", "selftest-pass")
        check("firmware patch + checksum", fwpatch.check_image(bytearray(patched)))
        wd = tempfile.mkdtemp(prefix="c3st_")
        img = build_fs_image(wd, man["fs_size"], asset("blocklist.bin"), "SelfTest123", lambda s: None)
        check("littlefs image", os.path.getsize(img) == man["fs_size"], "%d bytes" % os.path.getsize(img))
        shutil.rmtree(wd, ignore_errors=True)
        import esptool  # noqa: F401
        check("esptool import", True, esptool.__version__)
        check("serial ports api", isinstance(list_ports_info(), list))
    except Exception:
        ok = False
        lines.append("FAIL exception\n" + traceback.format_exc())
    out = os.environ.get("C3F_SELFTEST_OUT") or os.path.join(tempfile.gettempdir(), "c3flasher_selftest.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\nRESULT: %s\n" % ("OK" if ok else "FAILED"))
    print("\n".join(lines))
    return 0 if ok else 1


# --------------------------------------------------------------------- GUI
def main_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    man = None
    try:
        man = load_manifest()
    except Exception:
        pass

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("760x880")
    root.minsize(700, 780)
    style = ttk.Style()
    try:
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    except Exception:
        pass
    style.configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=8)
    style.configure("H.TLabel", font=("Segoe UI", 15, "bold"))

    q = queue.Queue()
    stop = threading.Event()
    busy = {"v": False, "ports": []}
    saved = load_settings()

    v_ssid = tk.StringVar(value=saved.get("ssid", ""))
    v_wpass = tk.StringVar()
    v_dpass = tk.StringVar()
    v_show = tk.BooleanVar(value=False)
    v_bl_mode = tk.StringVar(value="bundled")
    v_bl_path = tk.StringVar()
    v_port = tk.StringVar()
    v_erase = tk.BooleanVar(value=True)
    v_baud = tk.StringVar(value="460800")

    outer = ttk.Frame(root, padding=14)
    outer.pack(fill="both", expand=True)
    ttk.Label(outer, text="ESP32-C3 AdBlock Flasher", style="H.TLabel").pack(anchor="w")
    if man:
        info = "Bundled: %s domains  |  firmware %s (%s)  |  built %s" % (
            format(man["blocklist_domains"], ","), man.get("upstream_commit", "?"), man.get("env", ""), man["built_at"])
    else:
        info = "WARNING: bundled assets not found (run from the built .exe)"
    ttk.Label(outer, text=info, foreground="#555").pack(anchor="w", pady=(0, 8))

    def grid_entry(parent, label, var, row, show=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3, padx=(0, 10))
        e = ttk.Entry(parent, textvariable=var, width=38, show=show or "")
        e.grid(row=row, column=1, sticky="we", pady=3)
        return e

    f1 = ttk.LabelFrame(outer, text=" 1. Wi-Fi (2.4 GHz) ", padding=10)
    f1.pack(fill="x", pady=4)
    f1.columnconfigure(1, weight=1)
    grid_entry(f1, "Network name (SSID)", v_ssid, 0)
    e_w = grid_entry(f1, "Wi-Fi password", v_wpass, 1, show="*")

    f2 = ttk.LabelFrame(outer, text=" 2. Dashboard password ", padding=10)
    f2.pack(fill="x", pady=4)
    f2.columnconfigure(1, weight=1)
    e_d = grid_entry(f2, "Password (8+ chars)", v_dpass, 0, show="*")
    ttk.Label(f2, text="Leave blank to let the device generate one; it will be shown here after flashing.",
              foreground="#555").grid(row=1, column=0, columnspan=2, sticky="w")

    def toggle_show():
        ch = "" if v_show.get() else "*"
        e_w.config(show=ch)
        e_d.config(show=ch)
    ttk.Checkbutton(outer, text="Show passwords", variable=v_show, command=toggle_show).pack(anchor="w")

    f3 = ttk.LabelFrame(outer, text=" 3. Blocklist ", padding=10)
    f3.pack(fill="x", pady=4)
    ttk.Radiobutton(f3, text="Use bundled list (StevenBlack + Hagezi Multi PRO)", variable=v_bl_mode, value="bundled").pack(anchor="w")
    rowc = ttk.Frame(f3)
    rowc.pack(fill="x")
    ttk.Radiobutton(rowc, text="Use my own blocklist.bin:", variable=v_bl_mode, value="custom").pack(side="left")
    ttk.Entry(rowc, textvariable=v_bl_path, width=34).pack(side="left", padx=6, fill="x", expand=True)

    def browse():
        p = filedialog.askopenfilename(filetypes=[("Blocklist", "*.bin"), ("All", "*.*")])
        if p:
            v_bl_path.set(p)
            v_bl_mode.set("custom")
    ttk.Button(rowc, text="Browse...", command=browse).pack(side="left")

    f4 = ttk.LabelFrame(outer, text=" 4. Board ", padding=10)
    f4.pack(fill="x", pady=4)
    f4.columnconfigure(1, weight=1)
    ttk.Label(f4, text="COM port").grid(row=0, column=0, sticky="w", padx=(0, 10))
    cb = ttk.Combobox(f4, textvariable=v_port, state="readonly", width=46)
    cb.grid(row=0, column=1, sticky="we")
    ttk.Checkbutton(f4, text="Erase all flash first (recommended; clears old Wi-Fi/settings)", variable=v_erase).grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
    rb = ttk.Frame(f4)
    rb.grid(row=2, column=0, columnspan=2, sticky="w")
    ttk.Label(rb, text="Speed").pack(side="left")
    ttk.Combobox(rb, textvariable=v_baud, values=["115200", "230400", "460800", "921600"], width=8, state="readonly").pack(side="left", padx=6)
    ttk.Label(f4, text="Plug the board straight into a USB port with a data cable. If it is not found or fails to connect: hold BOOT, tap RESET, release BOOT.",
              foreground="#555", wraplength=640).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    btn = ttk.Button(outer, text="FLASH", style="Big.TButton")
    btn.pack(fill="x", pady=(10, 4))
    pb = ttk.Progressbar(outer, maximum=100)
    pb.pack(fill="x")
    status = tk.StringVar(value="Ready.")
    ttk.Label(outer, textvariable=status).pack(anchor="w", pady=(2, 4))

    res = ttk.LabelFrame(outer, text=" Result ", padding=8)
    res.pack(fill="x", pady=4)
    v_res = tk.StringVar(value="Dashboard details will appear here after flashing.")
    ttk.Label(res, textvariable=v_res, wraplength=680, justify="left").pack(anchor="w")
    rbtn = ttk.Frame(res)
    rbtn.pack(anchor="w", pady=(4, 0))
    b_open = ttk.Button(rbtn, text="Open dashboard", state="disabled")
    b_open.pack(side="left")
    b_copy = ttk.Button(rbtn, text="Copy password", state="disabled")
    b_copy.pack(side="left", padx=6)

    logf = ttk.Frame(outer)
    logf.pack(fill="both", expand=True, pady=(4, 0))
    logt = tk.Text(logf, height=7, wrap="word", font=("Consolas", 9), state="disabled", background="#111", foreground="#ddd")
    sb = ttk.Scrollbar(logf, command=logt.yview)
    logt.config(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    logt.pack(side="left", fill="both", expand=True)

    def log(s):
        logt.config(state="normal")
        logt.insert("end", s + "\n")
        logt.see("end")
        logt.config(state="disabled")

    def refresh_ports():
        if busy["v"]:
            return
        ports = list_ports_info()
        if ports != busy["ports"]:
            busy["ports"] = ports
            cb["values"] = [lab for _, lab in ports]
            cur = v_port.get()
            labels = [lab for _, lab in ports]
            if cur not in labels:
                v_port.set(labels[0] if labels else "")
        root.after(2000, refresh_ports)

    def selected_port():
        for dev, lab in busy["ports"]:
            if lab == v_port.get():
                return dev
        return None

    def validate():
        ssid, wp, dp = v_ssid.get().strip(), v_wpass.get(), v_dpass.get()
        if not ssid:
            return "Enter your Wi-Fi network name."
        if len(ssid.encode()) > fwpatch.SSID_MAX:
            return "SSID is too long (max 32 bytes)."
        if wp and len(wp) < 8:
            return "Wi-Fi password must be at least 8 characters (or empty for an open network)."
        if len(wp.encode()) > fwpatch.PASS_MAX:
            return "Wi-Fi password is too long (max 63 bytes)."
        if dp:
            if len(dp) < 8:
                return "Dashboard password must be at least 8 characters."
            if not re.fullmatch(r"[\x21-\x7E]+", dp):
                return "Dashboard password: use letters, digits and symbols only (no spaces, accents or emoji)."
        if v_bl_mode.get() == "custom":
            p = v_bl_path.get()
            if not os.path.isfile(p):
                return "Choose your blocklist.bin file."
            with open(p, "rb") as f:
                if f.read(4) != b"CADB":
                    return "That file is not a valid blocklist (missing CADB header)."
        if not selected_port():
            return "No board found. Plug in the ESP32-C3 (use a data cable)."
        if not man:
            return "Bundled firmware is missing. Use the .exe built by the workflow."
        return None

    def set_busy(b):
        busy["v"] = b
        btn.config(state="disabled" if b else "normal", text="Flashing... do not unplug" if b else "FLASH")
        if not b:
            refresh_ports()

    def start():
        err = validate()
        if err:
            messagebox.showwarning(APP_NAME, err)
            return
        if v_erase.get() and not messagebox.askyesno(
                APP_NAME, "This erases everything on the board and installs the ad-blocker.\n\nContinue?"):
            return
        save_settings({"ssid": v_ssid.get().strip()})
        cfg = {"ssid": v_ssid.get().strip(), "wifi_pass": v_wpass.get(), "dash_pass": v_dpass.get(),
               "port": selected_port(), "baud": int(v_baud.get()), "erase": v_erase.get(),
               "blocklist_path": v_bl_path.get() if v_bl_mode.get() == "custom" else None}
        stop.clear()
        pb["value"] = 0
        v_res.set("Working...")
        b_open.config(state="disabled")
        b_copy.config(state="disabled")
        set_busy(True)
        status.set("Flashing...")
        threading.Thread(target=flash_job, args=(cfg, q, stop), daemon=True).start()

    btn.config(command=start)

    def show_done(r):
        set_busy(False)
        status.set("Done.")
        ip, gen, login = r["ip"], r["password"], r["login"]
        lines = []
        if ip:
            url = "http://%s" % ip
            lines.append("Dashboard: %s   (also http://c3adblock.local)" % url)
            b_open.config(state="normal", command=lambda: webbrowser.open(url))
        else:
            lines.append("Flashed OK, but the device did not report an IP within 60 s. Check the Wi-Fi name/password, "
                         "then press RESET. You can also join the 'C3AdBlock-Setup' Wi-Fi and open http://192.168.4.1.")
        pw = v_dpass.get() or gen
        if v_dpass.get():
            if login is True:
                lines.append("Login check: your dashboard password was accepted.")
            elif login is False:
                lines.append("Login check: the device REJECTED your password. The firmware may not read it from auth.cfg. "
                             "Re-flash with the box blank to use a device-generated password.")
            else:
                lines.append("Login check: could not reach the dashboard to verify (try the link above).")
        elif gen:
            lines.append("Device-generated dashboard password: %s  (username: anything)" % gen)
        elif ip:
            lines.append("No password was printed by the device; leave the box blank on an erased board to see it.")
        lines.append("Next: set your router's DNS to the device IP and give it a DHCP reservation.")
        v_res.set("\n".join(lines))
        if pw:
            def cp():
                root.clipboard_clear()
                root.clipboard_append(pw)
            b_copy.config(state="normal", command=cp)

    def show_error(msg):
        set_busy(False)
        status.set("Failed.")
        hint = ""
        if re.search(r"Failed to connect|No serial data|Could not open|PermissionError|timed out", msg, re.I):
            hint = "\n\nTry: another USB cable/port, close any serial monitor, then hold BOOT, tap RESET, release BOOT and click FLASH again."
        v_res.set("Flashing failed: " + msg + hint)
        messagebox.showerror(APP_NAME, "Flashing failed:\n\n" + msg + hint)

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    log(val)
                elif kind == "progress":
                    pb["value"] = val
                elif kind == "done":
                    show_done(val)
                elif kind == "error":
                    show_error(val)
        except queue.Empty:
            pass
        root.after(100, poll)

    def on_close():
        if busy["v"] and not messagebox.askyesno(APP_NAME, "Flashing is in progress. Quit anyway?"):
            return
        stop.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_ports()
    poll()
    root.mainloop()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main_gui()
