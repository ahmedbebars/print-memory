# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Print Memory"
# description = "Learns from your Klipper printers' print history: how long a slice will really take on this printer, how often this material, length and model finished before, what changed since the last good print, and why recent prints stopped."
# author = "Ahmed Bebars"
# version = "0.1.1"
# ///
"""Print Memory for OrcaSlicer.

Run "Print Memory" from the Plugins window or the Speed Dial (press Space). A panel opens beside
the 3D view. It reads the print history that Moonraker keeps for the selected printer (the print
host in the printer preset) and turns it into answers:

  * This plate: how long the slice will really take on this printer, judged from earlier prints
    sliced with the same OrcaSlicer version, when it will be done, and how often prints of this
    material, this length and this model finished before. If the model was printed before, it
    lists the settings that changed since the last print that finished.
  * This printer: how many prints finished, how the others stopped, how far off Orca's time
    estimates are for each OrcaSlicer version, and how long heating and homing take.
  * Recent stops: each stopped print in plain words, how far it got, and the error Klipper
    reported when Moonraker still has it.
  * Your printers: every printer preset with a print host, whether it is free, and how well it
    does with the material you sliced.

The plugin only reads (HTTP GET). It never sends G-code and never changes a printer.
"""

import http.client
import json
import os
import queue
import re
import time
import urllib.parse
from email.utils import parsedate_to_datetime

try:
    import orca
except ImportError:  # running the analysis outside OrcaSlicer (tests)
    orca = None

VERSION = "0.1.1"
HTTP_TIMEOUT = 8.0
FLEET_TIMEOUT = 4.0
PERMISSION_WAIT = 90.0      # seconds to keep retrying while OrcaSlicer's permission prompt is open
MAX_JOBS = 500              # newest history entries read per printer
CACHE_SECONDS = 120.0       # reuse a printer's history this long before reading it again
TIME_WINDOW = 20            # newest finished prints used for the time factor
MIN_TIME_SAMPLES = 5
MIN_RATE_SAMPLES = 5
HEAD_BYTES = 262144
TAIL_BYTES = 262144
EARLY_CANCEL_SECONDS = 600

# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def plain(value):
    """Turn a host value into plain Python data (str, int, float, bool, list)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return str(value)


def first_text(value):
    """The first entry of a host value that may be a list or a "a;b" / "a,b" string."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    text = "" if value is None else str(value).strip()
    for sep in (";", ","):
        if sep in text:
            text = text.split(sep)[0]
    return text.strip().strip('"')


def num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


def median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def pct(part, whole):
    return int(round(100.0 * part / whole)) if whole else 0


def plural(n, word, many=None):
    return "%d %s" % (n, word if n == 1 else (many or word + "s"))


def fmt_dur(seconds):
    """3725 -> '1h 02m', 300 -> '5 min', 90000 -> '1d 1h'."""
    if seconds is None:
        return "?"
    minutes = int(round(max(0, seconds) / 60.0))
    if minutes < 60:
        return "%d min" % max(1, minutes)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh %02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours)


def fmt_clock(epoch, now=None):
    """'23:50' today, 'Tue 09:10' on another day."""
    now = time.time() if now is None else now
    t, n = time.localtime(epoch), time.localtime(now)
    if (t.tm_year, t.tm_yday) == (n.tm_year, n.tm_yday):
        return time.strftime("%H:%M", t)
    return time.strftime("%a %H:%M", t)


def fmt_day(epoch, now=None):
    """'7 Nov', or '7 Nov 2025' in another year."""
    now = time.time() if now is None else now
    t = time.localtime(epoch)
    text = "%d %s" % (t.tm_mday, time.strftime("%b", t))
    if t.tm_year != time.localtime(now).tm_year:
        text += " %d" % t.tm_year
    return text


def fmt_when(epoch, now=None):
    return "%s %s" % (fmt_day(epoch, now), time.strftime("%H:%M", time.localtime(epoch)))


def short(text, size=60):
    text = str(text)
    return text if len(text) <= size else text[:size - 1] + "…"


def parse_duration(text):
    """'2h 43m 17s', '1d 2h 3m', '45m 10s', '0.882001s' -> seconds."""
    total, found = 0.0, False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", str(text or "").lower()):
        total += float(amount) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        found = True
    return total if found else None


# --------------------------------------------------------------------------------------------
# Moonraker
# --------------------------------------------------------------------------------------------

class MoonrakerError(Exception):
    pass


class Moonraker:
    """Minimal read-only Moonraker client on http.client (standard library only)."""

    def __init__(self, print_host, api_key="", timeout=HTTP_TIMEOUT):
        url = (print_host or "").strip()
        if not url:
            raise MoonrakerError("The printer preset has no print host. Set the printer's "
                                 "address in the printer settings (the Wi-Fi icon next to the "
                                 "printer name).")
        if "://" not in url:
            url = "http://" + url
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            raise MoonrakerError("Can't read the print host address %r." % print_host)
        self.scheme = (parts.scheme or "http").lower()
        self.host = parts.hostname
        self.port = parts.port
        self.prefix = parts.path.rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    @property
    def label(self):
        return self.host + (":%d" % self.port if self.port else "")

    def _request(self, path, extra_headers=None):
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=self.timeout)
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        headers.update(extra_headers or {})
        try:
            conn.request("GET", self.prefix + path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        except PermissionError:
            raise
        except OSError as error:
            raise MoonrakerError("Can't reach %s (%s). Is the printer on, and is the print "
                                 "host address in the printer preset right?" % (self.label, error))
        finally:
            conn.close()
        if resp.status in (401, 403):
            raise MoonrakerError("Moonraker on %s refused the request (HTTP %d). Add this "
                                 "computer to [authorization] trusted_clients in "
                                 "moonraker.conf, or put an API key in the printer preset."
                                 % (self.label, resp.status))
        return resp.status, resp.getheader("Date"), body

    def get(self, path, with_date=False):
        status, date, body = self._request(path)
        if status == 404 and path.startswith("/server/info"):
            raise MoonrakerError("%s answered, but it isn't Moonraker. Print Memory needs the "
                                 "Moonraker address (often port 7125, or 80 behind "
                                 "Mainsail/Fluidd)." % self.label)
        if status != 200:
            raise MoonrakerError("Moonraker on %s answered HTTP %d for %s."
                                 % (self.label, status, path.split("?")[0]))
        try:
            result = json.loads(body.decode("utf-8"))["result"]
        except (ValueError, KeyError):
            raise MoonrakerError("%s answered something that isn't Moonraker JSON." % self.label)
        if with_date:
            try:
                stamp = parsedate_to_datetime(date).timestamp() if date else None
            except (TypeError, ValueError):
                stamp = None
            return result, stamp
        return result

    def tail(self, path, size):
        """The last `size` bytes of a file, or None when the server can't send them."""
        status, _, body = self._request(path, {"Range": "bytes=-%d" % size,
                                               "Accept": "*/*"})
        if status == 206:
            return body
        if status == 200:
            return body[-size:]
        return None

    def history(self, max_jobs=MAX_JOBS):
        jobs, start = [], 0
        while len(jobs) < max_jobs:
            limit = min(200, max_jobs - len(jobs))
            page = self.get("/server/history/list?limit=%d&start=%d&order=desc" % (limit, start))
            got = page.get("jobs") or []
            jobs.extend(got)
            if len(got) < limit:
                break
            start += len(got)
        return jobs

    def fetch(self, store=True):
        """Everything Print Memory reads from one printer."""
        server = self.get("/server/info")
        data = {"server": server, "jobs": self.history(), "totals": {}, "status": {},
                "eventtime": None, "date": None, "store": [], "file_meta": {}}
        try:
            data["totals"] = self.get("/server/history/totals").get("job_totals", {})
        except MoonrakerError:
            pass
        if server.get("klippy_state") == "ready":
            try:
                query, date = self.get("/printer/objects/query?print_stats&virtual_sdcard"
                                       "&webhooks", with_date=True)
                data["status"] = query.get("status", {})
                data["eventtime"], data["date"] = query.get("eventtime"), date
            except MoonrakerError:
                pass
            ps = data["status"].get("print_stats", {})
            if ps.get("state") in ("printing", "paused") and ps.get("filename"):
                try:
                    data["file_meta"] = self.get("/server/files/metadata?filename="
                                                 + urllib.parse.quote(ps["filename"]))
                except MoonrakerError:
                    pass
        if store:
            try:
                data["store"] = self.get("/server/gcode_store?count=1000").get("gcode_store", [])
            except MoonrakerError:
                pass
        data["fetched"] = time.time()
        return data

    def gcode_tail(self, filename, size=TAIL_BYTES):
        raw = self.tail("/server/files/gcodes/" + urllib.parse.quote(filename), size)
        return raw.decode("utf-8", "replace") if raw else ""


# --------------------------------------------------------------------------------------------
# G-code files
# --------------------------------------------------------------------------------------------

def parse_config_block(text):
    """The '; key = value' lines between CONFIG_BLOCK_START and CONFIG_BLOCK_END."""
    start = text.find("; CONFIG_BLOCK_START")
    if start < 0:
        return {}
    end = text.find("; CONFIG_BLOCK_END", start)
    block = text[start:end if end > 0 else len(text)]
    config = {}
    for line in block.splitlines()[1:]:
        if line.startswith("; ") and " = " in line:
            key, value = line[2:].split(" = ", 1)
            config[key.strip()] = value.strip()
    return config


def parse_gcode(head, tail):
    """What Print Memory needs from an OrcaSlicer G-code file: its slicer, time estimate,
    filament, object names and settings. `head` and `tail` are the start and end of the file
    (the same text twice is fine for small files)."""
    info = {"slicer": None, "version": None, "estimate": None, "grams": None,
            "objects": [], "config": parse_config_block(tail) or parse_config_block(head)}
    match = re.search(r"^; generated by (\S+) (\S+)", head, re.M)
    if match:
        info["slicer"], info["version"] = match.group(1), match.group(2)
    match = (re.search(r"^; estimated printing time \(normal mode\) = (.+)$", tail, re.M)
             or re.search(r"^; estimated printing time \(normal mode\) = (.+)$", head, re.M)
             or re.search(r"^; model printing time: ([^;]+)", head, re.M))
    if match:
        info["estimate"] = parse_duration(match.group(1))
    match = re.search(r"^; total filament used \[g\] = ([\d.]+)", tail, re.M)
    if match:
        info["grams"] = float(match.group(1))
    names = []
    for name in re.findall(r"^; printing object (.+?)(?: id:\d+)?(?: copy \d+)?\s*$", head, re.M):
        if name not in names and name != "ENDGCODE":
            names.append(name)
    if not names:
        for name in re.findall(r"^EXCLUDE_OBJECT_DEFINE NAME='?([^' ]+)", head, re.M):
            name = re.sub(r"_id_\d+_copy_\d+$", "", name)
            if name not in names:
                names.append(name)
    info["objects"] = names[:20]
    config = info["config"]
    info["material"] = first_text(config.get("filament_type")) or None
    info["printer_preset"] = config.get("printer_settings_id", "").strip('"')
    info["process_preset"] = config.get("print_settings_id", "").strip('"')
    info["filament_preset"] = first_text(config.get("filament_settings_id")) or ""
    return info


def read_local_gcode(path):
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES)
        f.seek(0, 2)
        size = f.tell()
        tail = b""
        if size > len(head):
            f.seek(max(size - TAIL_BYTES, len(head)))
            tail = f.read()
    head_text = head.decode("utf-8", "replace")
    tail_text = tail.decode("utf-8", "replace") if tail else head_text
    info = parse_gcode(head_text, tail_text)
    info["path"] = path
    info["size"] = size
    return info


# --------------------------------------------------------------------------------------------
# Reading the history
# --------------------------------------------------------------------------------------------

KINDS = {
    "completed": ("Finished", "ok", ""),
    "cancelled": ("Cancelled", "info",
                  "Someone pressed Cancel (in Mainsail, Fluidd, KlipperScreen or the slicer), "
                  "or a macro ran CANCEL_PRINT."),
    "klippy_shutdown": ("Klipper shut down", "error",
                        "Klipper stopped everything: an MCU or CAN error, a heater or "
                        "thermistor fault, or an emergency stop."),
    "error": ("Stopped by an error", "error",
              "Klipper refused a command and stopped the print, for example \"Move out of "
              "range\" or an error in a macro."),
    "interrupted": ("Printer computer restarted", "warn",
                    "Moonraker restarted while printing: a power cut, a reboot, or the "
                    "printer's computer was switched off."),
    "klippy_disconnect": ("Lost contact with Klipper", "error",
                          "Moonraker lost its connection to Klipper during the print, so "
                          "Klipper crashed or was restarted."),
    "server_exit": ("Moonraker stopped", "warn",
                    "Moonraker was stopped or restarted while printing."),
}


def kind_of(job):
    return KINDS.get(job.get("status"), (str(job.get("status") or "Unknown").replace("_", " ")
                                         .capitalize(), "info", ""))


def finished_jobs(jobs):
    return [j for j in jobs if j.get("status") not in ("in_progress", None)]


def meta(job):
    return job.get("metadata") or {}


def slicer_group(slicer, version):
    """'OrcaSlicer 2.5' for OrcaSlicer 2.5.0-alpha. Time estimates change between slicer
    versions, so each version is judged on its own."""
    match = re.match(r"(\d+)\.(\d+)", str(version or ""))
    name = slicer or "Unknown slicer"
    return "%s %s.%s" % (name, match.group(1), match.group(2)) if match else name


def job_group(job):
    m = meta(job)
    return slicer_group(m.get("slicer"), m.get("slicer_version"))


def time_samples(jobs):
    """(group, real/estimate ratio, warm-up seconds) for finished prints, newest first."""
    out = []
    for job in jobs:
        est = num(meta(job).get("estimated_time"))
        printed = num(job.get("print_duration"))
        if job.get("status") != "completed" or not est or est < 600 or not printed:
            continue
        ratio = printed / est
        if 0.25 <= ratio <= 4.0:
            warm = max(0.0, (num(job.get("total_duration")) or printed) - printed)
            out.append((job_group(job), ratio, warm))
    return out


def time_model(jobs, group, window=TIME_WINDOW):
    """How the newest finished prints of this slicer version compared with their estimate."""
    ratios = [r for g, r, _ in time_samples(jobs) if g == group][:window]
    if len(ratios) < MIN_TIME_SAMPLES:
        return None
    return {"group": group, "factor": median(ratios), "low": quantile(ratios, 0.25),
            "high": quantile(ratios, 0.75), "samples": len(ratios)}


def time_models(jobs):
    """One time model per slicer version found in the history, most used first."""
    counts = {}
    for g, _, _ in time_samples(jobs):
        counts[g] = counts.get(g, 0) + 1
    out = []
    for group in sorted(counts, key=lambda g: -counts[g]):
        model = time_model(jobs, group)
        if model:
            model["total"] = counts[group]
            out.append(model)
    return out


def warmup(jobs):
    warms = [w for _, _, w in time_samples(jobs)[:TIME_WINDOW] if w <= 3600]
    return median(warms) if len(warms) >= MIN_TIME_SAMPLES else None


def expected_seconds(job, factors):
    est = num(meta(job).get("estimated_time"))
    if not est:
        return num(job.get("print_duration"))
    return est * factors.get(job_group(job), 1.0)


LENGTHS = [(0, 3600, "Under 1 h"), (3600, 4 * 3600, "1 to 4 h"), (4 * 3600, 10 * 3600, "4 to 10 h"),
           (10 * 3600, float("inf"), "Over 10 h")]


def length_bucket(seconds):
    for low, high, name in LENGTHS:
        if seconds is not None and low <= seconds < high:
            return name
    return None


def rate_rows(groups):
    rows = []
    for name, (done, total) in groups:
        rows.append({"name": name, "done": done, "total": total, "pct": pct(done, total)})
    return rows


def grams_used(job):
    m = meta(job)
    used, total, weight = num(job.get("filament_used")), num(m.get("filament_total")), \
        num(m.get("filament_weight_total"))
    if used is None:
        return None
    if total and weight:
        return weight * used / total
    return used * 0.00298  # 1.75 mm filament at about 1.24 g/cm3


def progress_of(job):
    """How far a print got, 0..1, from the filament it used against the filament it needed."""
    used, total = num(job.get("filament_used")), num(meta(job).get("filament_total"))
    if used is not None and total:
        return max(0.0, min(1.0, used / total))
    return None


def store_reason(store, end_time):
    """The error Klipper printed around the moment a print stopped, from Moonraker's G-code
    console memory (it only keeps the newest lines, so old stops have none)."""
    if not end_time:
        return None
    lines = [g for g in store if isinstance(g, dict) and num(g.get("time")) is not None
             and end_time - 120 <= g["time"] <= end_time + 15]
    errors = [str(g.get("message", "")) for g in lines if g.get("type") == "response"
              and str(g.get("message", "")).startswith("!!")]
    if errors:
        return errors[-1][2:].strip().splitlines()[0]
    commands = [str(g.get("message", "")).strip() for g in lines if g.get("type") == "command"
                and re.match(r"(?i)\s*(CANCEL_PRINT|M112|SDCARD_RESET_FILE|FIRMWARE_RESTART|"
                             r"RESTART)\b", str(g.get("message", "")))]
    if commands:
        return "%s was sent" % short(commands[-1], 50)
    return None


REASONS = [
    (r"webhooks request", "Someone pressed the emergency stop (in Mainsail, Fluidd or "
                          "KlipperScreen)."),
    (r"M112", "An M112 emergency stop was sent."),
    (r"Timer too close", "A board or the printer's computer fell behind (\"Timer too close\"): "
                         "often an overloaded host, a slow CAN bus or a USB problem."),
    (r"Lost communication with MCU|Unable to connect|MCU '.*' shutdown: Rescheduled timer",
     "Klipper lost contact with a board: check its cable, power and the CAN or USB connection."),
    (r"not heating at expected rate|Heater .* not heating",
     "A heater warmed up too slowly, so Klipper's heater protection stopped the print: check "
     "the heater, its wiring and the fan blowing on it."),
    (r"ADC out of range", "A temperature sensor read an impossible value: a loose, broken or "
                          "shorted thermistor."),
    (r"Move out of range", "A move went outside the printer's travel: the model, the bed size "
                           "in the printer preset, or a macro moved too far."),
    (r"Must home axis first", "A move was sent before the printer was homed."),
    (r"Probe triggered prior to movement|No trigger on probe",
     "The probe misbehaved during homing or probing."),
    (r"Unknown command", "The G-code used a command Klipper doesn't know (a missing macro)."),
    (r"Extrude below minimum temp", "The printer tried to extrude while the nozzle was too cold."),
    (r"Extrude only move too long", "An extrude-only move was longer than max_extrude_only_distance."),
    (r"[Tt]hermocouple|MAX31865|MAX6675", "The thermocouple amplifier reported a fault."),
]


def explain_reason(text):
    for pattern, words in REASONS:
        if re.search(pattern, text or ""):
            return words
    return ""


def stop_hint(job):
    status = job.get("status")
    if status in ("klippy_shutdown", "klippy_disconnect"):
        return ("klippy.log has the reason: look for \"Transition to shutdown state\" near "
                "%s." % fmt_when(job.get("end_time") or job.get("start_time") or 0))
    if status == "error":
        return ("klippy.log has the reason: look for the error just before \"Exiting SD card "
                "print\" near %s." % fmt_when(job.get("end_time") or job.get("start_time") or 0))
    return ""


def printer_state(data, factors, now):
    """'Printing 45% · about 1h 10m left', 'Free', and the seconds left (or None)."""
    status = data.get("status") or {}
    server = data.get("server") or {}
    if server.get("klippy_state") and server.get("klippy_state") != "ready":
        return "Klipper: %s" % server.get("klippy_state"), None, "bad"
    ps = status.get("print_stats") or {}
    state = ps.get("state") or ""
    if state in ("printing", "paused"):
        progress = num((status.get("virtual_sdcard") or {}).get("progress")) or 0.0
        printed = num(ps.get("print_duration")) or 0.0
        est = num((data.get("file_meta") or {}).get("estimated_time"))
        group = slicer_group((data.get("file_meta") or {}).get("slicer"),
                             (data.get("file_meta") or {}).get("slicer_version"))
        left = None
        if est:
            left = max(0.0, est * factors.get(group, 1.0) - printed)
        elif progress > 0.02:
            left = max(0.0, printed / progress - printed)
        text = "%s %d%%" % ("Paused" if state == "paused" else "Printing", int(progress * 100))
        if left is not None:
            text += " · about %s left" % fmt_dur(left)
        return text, (left if left is not None else 0.0), ("wait" if state == "paused" else "busy")
    if state == "error":
        return "Stopped with an error", None, "bad"
    if state == "complete":
        return "Idle · last print finished", None, "ok"
    if state == "cancelled":
        return "Idle · last print cancelled", None, "ok"
    return "Idle", None, "ok"


def summarize_printer(data, now=None):
    """The 'This printer' and 'Recent stops' parts of the panel."""
    now = time.time() if now is None else now
    jobs = finished_jobs(data.get("jobs") or [])
    models = time_models(jobs)
    factors = {m["group"]: m["factor"] for m in models}
    state, left, state_cls = printer_state(data, factors, now)
    out = {"jobs": len(jobs), "state": state, "state_cls": state_cls, "left": left,
           "insights": [], "stops": [], "materials": [], "lengths": [], "times": [],
           "recent_stops": [], "warmup": None, "totals": "", "headline": ""}
    totals = data.get("totals") or {}
    if totals.get("total_jobs"):
        out["totals"] = "%d jobs in all · %s printing · %.2f km filament" % (
            int(totals["total_jobs"]), fmt_dur(num(totals.get("total_print_time")) or 0),
            (num(totals.get("total_filament_used")) or 0) / 1e6)
    if not jobs:
        out["headline"] = "No prints in Moonraker's history yet."
        return out
    done = [j for j in jobs if j.get("status") == "completed"]
    first = min(num(j.get("start_time")) or now for j in jobs)
    out["finished"] = {"done": len(done), "total": len(jobs), "pct": pct(len(done), len(jobs))}
    out["headline"] = "%s remembered since %s · %d%% finished" % (
        plural(len(jobs), "print"), fmt_day(first, now), out["finished"]["pct"])

    counts = {}
    for job in jobs:
        if job.get("status") != "completed":
            label = kind_of(job)[0]
            counts[label] = counts.get(label, 0) + 1
    out["stops"] = [{"label": k, "n": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]

    mats = {}
    for job in jobs:
        name = first_text(meta(job).get("filament_type")) or "Unknown"
        d, t = mats.get(name, (0, 0))
        mats[name] = (d + (job.get("status") == "completed"), t + 1)
    out["materials"] = rate_rows(sorted(mats.items(), key=lambda kv: -kv[1][1]))

    lengths = {}
    for job in jobs:
        bucket = length_bucket(expected_seconds(job, factors))
        if bucket:
            d, t = lengths.get(bucket, (0, 0))
            lengths[bucket] = (d + (job.get("status") == "completed"), t + 1)
    out["lengths"] = rate_rows([(name, lengths[name]) for _, _, name in LENGTHS if name in lengths])

    for model in models:
        out["times"].append({"name": model["group"], "text": factor_text(model),
                             "n": model["samples"]})
    out["warmup"] = warmup(jobs)

    # Things worth saying out loud.
    cancels = [j for j in jobs if j.get("status") == "cancelled"]
    early = [j for j in cancels if (num(j.get("print_duration")) or 0) < EARLY_CANCEL_SECONDS]
    if len(early) >= 3 and len(early) >= len(cancels) / 2:
        out["insights"].append({"level": "info", "text": (
            "%d of the %d cancelled prints were stopped in the first 10 minutes. That is "
            "usually the first layer: bed level, Z offset or a dirty plate."
            % (len(early), len(cancels)))})
    faults = [j for j in jobs if j.get("status") in ("klippy_shutdown", "klippy_disconnect",
                                                      "error")]
    before = [j for j in faults if (num(j.get("print_duration")) or 0) < 60]
    if len(before) >= 3 and len(before) >= len(faults) / 2:
        out["insights"].append({"level": "warn", "text": (
            "%d of the %d prints that Klipper stopped never got to the first layer: the stop "
            "came while heating, homing, probing or running the start macro. Start there."
            % (len(before), len(faults)))})
    faults = [j for j in faults if j.get("status") != "error"]
    if len(faults) >= 3 and not (len(before) >= 3 and len(before) >= len(faults) / 2):
        out["insights"].append({"level": "warn", "text": (
            "Klipper shut down during %s. If they repeat, the reason in klippy.log (\"Transition "
            "to shutdown state\") is worth checking: loose CAN or USB cables, a thermistor or "
            "an overloaded host are the usual causes." % plural(len(faults), "print"))})
    power = [j for j in jobs if j.get("status") == "interrupted"]
    if len(power) >= 2:
        out["insights"].append({"level": "warn", "text": (
            "%s stopped because the printer's computer restarted (power cut or reboot). A UPS "
            "helps if power cuts are common."
            % plural(len(power), "print"))})
    if len(models) >= 2 and abs(models[0]["factor"] - models[1]["factor"]) > 0.2:
        out["insights"].append({"level": "info", "text": (
            "Orca's time estimates changed a lot between %s and %s on this printer, so Print "
            "Memory judges each version on its own." % (models[1]["group"], models[0]["group"]))})

    store = data.get("store") or []
    stopped = [j for j in jobs if j.get("status") != "completed"]
    repeats = {}
    for job in stopped[:20]:
        name = os.path.basename(str(job.get("filename") or ""))
        repeats[name] = repeats.get(name, 0) + 1
    for job in stopped[:6]:
        label, level, detail = kind_of(job)
        prog = progress_of(job)
        printed = num(job.get("print_duration")) or 0
        grams = grams_used(job)
        how_far = []
        if job.get("status") == "interrupted" and printed <= 0:
            how_far = ["how far it got wasn't saved, because the computer stopped first"]
        else:
            if prog is not None:
                how_far.append("stopped at %d%%" % int(prog * 100))
            if printed >= 60:
                how_far.append("after %s of printing" % fmt_dur(printed))
            elif prog is not None and prog < 0.01:
                how_far = ["stopped before the first layer"]
            if grams and grams >= 1:
                how_far.append("%d g of filament used" % round(grams))
        end = num(job.get("end_time"))
        reason = store_reason(store, end)
        name = os.path.basename(str(job.get("filename") or ""))
        out["recent_stops"].append({
            "when": fmt_when(end, now) if end else "started %s" % fmt_when(
                num(job.get("start_time")) or now, now),
            "file": name, "kind": label, "level": level,
            "detail": (explain_reason(reason) if reason else "") or detail,
            "progress": ", ".join(how_far), "reason": reason,
            "hint": "" if reason else stop_hint(job),
            "repeat": ("This file stopped %d times lately." % repeats[name])
            if repeats.get(name, 0) >= 3 else "",
        })
    return out


def factor_text(model, past=False):
    """'12% longer than Orca says' / '40% shorter than Orca said' / 'as long as Orca says'."""
    f, says = model["factor"], "said" if past else "says"
    if abs(f - 1.0) < 0.03:
        return "as long as Orca %s" % says
    if f > 1.0:
        return "%d%% longer than Orca %s" % (round((f - 1.0) * 100), says)
    return "%d%% shorter than Orca %s" % (round((1.0 - f) * 100), says)


# --------------------------------------------------------------------------------------------
# The plate that was just sliced
# --------------------------------------------------------------------------------------------

def name_key(text):
    """A file or object name, lower case, without folders, extension or '+' for spaces."""
    text = os.path.basename(str(text or "")).lower().replace("+", " ")
    text = re.sub(r"\.(gcode|bgcode|stl|3mf|step|stp|obj|amf|svg|oltp)$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def model_keys(project, objects):
    keys = []
    for name in [project] + list(objects or []):
        key = name_key(name)
        if len(key) >= 2 and key not in keys:
            keys.append(key)
    return keys[:12]


def same_model(jobs, keys):
    """History entries whose file name starts with the project or an object name (OrcaSlicer
    names uploads '{input_filename_base}_...' by default)."""
    out = []
    for job in jobs:
        fname = name_key(job.get("filename"))
        for key in keys:
            if fname == key or (fname.startswith(key) and fname[len(key):len(key) + 1] in "_. -"):
                out.append(job)
                break
    return out


IGNORED_KEYS = re.compile(
    r"(_settings_id$|^inherits|^compatible_|^print_host|^printhost|^thumbnails|^filename_format"
    r"|^post_process|_notes$|^different_settings_to_system|^print_compatible_printers"
    r"|^upward_compatible_machine|^wipe_tower_x$|^wipe_tower_y$|^printer_model$"
    r"|^printer_variant$|^default_print_profile$|^default_filament_profile$|^host_type$"
    r"|^bbl_|^plate_name$|^filament_vendor$|^filament_ids$|^setting_id$|^name$)")

PRIORITY_KEYS = [
    "layer_height", "initial_layer_print_height", "wall_loops", "top_shell_layers",
    "bottom_shell_layers", "sparse_infill_density", "sparse_infill_pattern",
    "nozzle_temperature", "nozzle_temperature_initial_layer", "curr_bed_type",
    "textured_plate_temp", "hot_plate_temp", "cool_plate_temp", "eng_plate_temp",
    "textured_plate_temp_initial_layer", "hot_plate_temp_initial_layer",
    "outer_wall_speed", "inner_wall_speed", "sparse_infill_speed", "internal_solid_infill_speed",
    "top_surface_speed", "travel_speed", "initial_layer_speed", "default_acceleration",
    "outer_wall_acceleration", "filament_type", "filament_flow_ratio", "pressure_advance",
    "filament_max_volumetric_speed", "retraction_length", "retraction_speed", "z_hop",
    "fan_max_speed", "fan_min_speed", "close_fan_the_first_x_layers", "enable_support",
    "support_type", "brim_type", "brim_width", "skirt_loops", "seam_position", "line_width",
    "outer_wall_line_width", "ironing_type", "print_sequence", "nozzle_diameter",
]


# Settings that rarely matter for a single-tool print: shown after everything else.
DEMOTED_KEYS = re.compile(
    r"(multitool|variant|_list$|multi_bed|wipe_tower|prime_tower|flush|^ams|purge|ramming"
    r"|single_extruder|nozzle_volume|chamber|air_filtration|timelapse|time_lapse|bed_custom"
    r"|extruder_printable|scan_first_layer|^enable_filament_ramming|filament_map"
    r"|_preheat|^long_retractions_when|^cooling_tube|^extra_loading|^filament_loading"
    r"|^filament_unloading|^filament_cooling|^filament_minimal_purge|^filament_toolchange)")

PLATE_TEMP_KEYS = {
    "cool plate": "cool_plate_temp", "engineering plate": "eng_plate_temp",
    "high temp plate": "hot_plate_temp", "textured pei plate": "textured_plate_temp",
    "textured cool plate": "textured_cool_plate_temp", "supertack plate": "supertack_plate_temp",
}


def unused_plate_keys(then, now):
    """Bed temperature keys of plate types neither G-code was sliced for."""
    used = set()
    for config in (then, now):
        key = PLATE_TEMP_KEYS.get(first_text(config.get("curr_bed_type")).lower())
        if key:
            used.update((key, key + "_initial_layer"))
    if not used:
        return set()
    every = set(PLATE_TEMP_KEYS.values())
    every |= {k + "_initial_layer" for k in every}
    return every - used


def same_value(a, b):
    norm = {"0": "0", "false": "0", "disabled": "0", "1": "1", "true": "1", "enabled": "1"}
    a, b = str(a).strip().strip('"'), str(b).strip().strip('"')
    return a == b or norm.get(a.lower(), a) == norm.get(b.lower(), b)


LABELS = {
    "curr_bed_type": "Plate type", "default_bed_type": "Default plate type",
    "sparse_infill_density": "Infill density", "sparse_infill_pattern": "Infill pattern",
    "sparse_infill_speed": "Infill speed", "wall_loops": "Walls", "top_shell_layers": "Top layers",
    "bottom_shell_layers": "Bottom layers", "initial_layer_print_height": "First layer height",
    "nozzle_temperature_initial_layer": "Nozzle temperature, first layer",
    "filament_max_volumetric_speed": "Max volumetric speed", "enable_support": "Supports",
    "support_type": "Support type", "z_hop": "Z hop", "printable_area": "Bed area",
    "printable_height": "Build height", "bed_shape": "Bed shape",
    "first_layer_bed_temperature": "Bed temperature, first layer",
    "first_layer_temperature": "Nozzle temperature, first layer (all filaments)",
    "fan_max_speed": "Fan max speed", "fan_min_speed": "Fan min speed",
    "close_fan_the_first_x_layers": "No fan for the first layers",
}


def key_label(key):
    if key in LABELS:
        return LABELS[key]
    text = key.replace("_", " ").strip()
    text = re.sub(r"initial layer", "first layer", text)
    return text[:1].upper() + text[1:]


def show_value(value):
    value = str(value).strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return short(value.replace("\\n", " ↵ "), 48) if value else "(empty)"


def settings_diff(then, now):
    """Settings that differ between an earlier G-code's CONFIG_BLOCK and the current one,
    most important first. Keys that only one of the files has (a newer or older OrcaSlicer)
    are left out."""
    rows = []
    unused = unused_plate_keys(then, now)
    for key in set(then) & set(now):
        if IGNORED_KEYS.search(key) or key in unused:
            continue
        a, b = then.get(key), now.get(key)
        if same_value(a, b):
            continue
        row = {"key": key, "label": key_label(key)}
        if key.endswith("_gcode") or key.endswith("gcode_template"):
            row["then"], row["now"] = "", "(text changed)"
        else:
            row["then"], row["now"] = show_value(a), show_value(b)
        rows.append(row)
    order = {k: i for i, k in enumerate(PRIORITY_KEYS)}
    rows.sort(key=lambda r: (order.get(r["key"], len(order)),
                             bool(DEMOTED_KEYS.search(r["key"])), r["key"]))
    return rows


def preset_changes(then, now):
    out = []
    for key, label in (("printer_settings_id", "Printer preset"),
                       ("print_settings_id", "Process preset"),
                       ("filament_settings_id", "Filament preset")):
        a, b = first_text(then.get(key)), first_text(now.get(key))
        if a and b and a != b:
            out.append({"key": key, "label": label, "then": short(a, 48), "now": short(b, 48)})
    return out


def rate_line(level_ok, title, done, total):
    return {"level": "ok" if level_ok else "warn", "title": title,
            "text": "%d of %d finished (%d%%)" % (done, total, pct(done, total))}


def analyze_slice(slice_info, data, now=None, keys=None, earlier=None):
    """The 'This plate' part: forecast, success odds and what changed. `earlier` is the parsed
    tail of the last finished print of the same model (or None)."""
    now = time.time() if now is None else now
    jobs = finished_jobs(data.get("jobs") or [])
    models = time_models(jobs)
    factors = {m["group"]: m["factor"] for m in models}
    group = slicer_group(slice_info.get("slicer"), slice_info.get("version"))
    est = slice_info.get("estimate")
    model = time_model(jobs, group)
    warm = warmup(jobs)
    state, left, _ = printer_state(data, factors, now)
    out = {"title": "", "forecast": None, "range": "", "sub": "", "finish": "", "basis": "",
           "items": [], "diff": None, "material": slice_info.get("material")}
    names = slice_info.get("objects") or []
    out["title"] = short(", ".join(names[:3]) + (" +%d more" % (len(names) - 3) if len(names) > 3
                                                  else ""), 70) or "The sliced plate"

    if est:
        factor = model["factor"] if model else 1.0
        real = est * factor
        total = real + (warm or 0)
        out["forecast"] = fmt_dur(total)
        bits = ["Orca says %s" % fmt_dur(est)]
        if warm:
            bits.append("plus about %s to heat up and home" % fmt_dur(warm))
        out["sub"] = " · ".join(bits)
        if model:
            low, high = est * model["low"] + (warm or 0), est * model["high"] + (warm or 0)
            if fmt_dur(low) != fmt_dur(high):
                out["range"] = "usually %s to %s" % (fmt_dur(low), fmt_dur(high))
            out["basis"] = ("The last %s sliced with %s on this printer ran %s." % (
                plural(model["samples"], "finished print"), group, factor_text(model, True)))
        else:
            others = "; ".join("%s ran %s" % (m["group"], factor_text(m, True))
                               for m in models[:2])
            out["basis"] = ("No finished prints sliced with %s on this printer yet, so this is "
                            "Orca's own estimate." % group) + (
                (" Other versions here: %s." % others) if others else "")
        start = now + (left or 0)
        if left:
            out["finish"] = "The printer is busy (%s), so this would be done around %s." % (
                state.lower(), fmt_clock(start + total, now))
        else:
            out["finish"] = "Done around %s if you start now." % fmt_clock(now + total, now)

    done_all = [j for j in jobs if j.get("status") == "completed"]
    base_rate = pct(len(done_all), len(jobs)) if jobs else None

    material = slice_info.get("material")
    if material:
        same = [j for j in jobs if first_text(meta(j).get("filament_type")) == material]
        if len(same) >= MIN_RATE_SAMPLES:
            ok = sum(1 for j in same if j.get("status") == "completed")
            good = pct(ok, len(same)) >= (base_rate or 0) - 10 and pct(ok, len(same)) >= 60
            out["items"].append(rate_line(good, "%s on this printer" % material, ok, len(same)))
        elif jobs:
            out["items"].append({"level": "info", "title": "%s on this printer" % material,
                                 "text": "Only %s with %s so far." % (
                                     plural(len(same), "print"), material)})

    if est:
        bucket = length_bucket(est * (model["factor"] if model else 1.0))
        same_len = [j for j in jobs if length_bucket(expected_seconds(j, factors)) == bucket]
        if bucket and len(same_len) >= MIN_RATE_SAMPLES:
            ok = sum(1 for j in same_len if j.get("status") == "completed")
            good = pct(ok, len(same_len)) >= (base_rate or 0) - 10 and pct(ok, len(same_len)) >= 60
            out["items"].append(rate_line(good, "%s prints here" % bucket, ok, len(same_len)))

    matches = same_model(jobs, keys or model_keys("", names))
    if matches:
        ok = [j for j in matches if j.get("status") == "completed"]
        last = matches[0]
        label, level, _ = kind_of(last)
        text = "%s here, %d finished. Last time (%s): %s" % (
            plural(len(matches), "print"), len(ok), fmt_day(num(last.get("start_time")) or now,
                                                              now), label.lower())
        if last.get("status") == "completed":
            text += " in %s" % fmt_dur(num(last.get("total_duration")))
            level = "ok"
        else:
            prog = progress_of(last)
            if prog is not None:
                text += " at %d%%" % int(prog * 100)
            level = "warn" if level != "info" else "info"
        out["items"].append({"level": level, "title": "This model before", "text": text + "."})
        if ok and earlier is not None:
            then = earlier.get("config") or {}
            nowcfg = slice_info.get("config") or {}
            if then and nowcfg:
                rows = preset_changes(then, nowcfg) + settings_diff(then, nowcfg)
                out["diff"] = {
                    "title": "Changed since the last print that finished (%s)" % fmt_day(
                        num(earlier.get("start_time")) or now, now),
                    "rows": rows[:80], "more": max(0, len(rows) - 80),
                    "file": os.path.basename(earlier.get("filename") or ""),
                }
            elif not then:
                out["diff"] = {"title": "The last good print's file has no settings block, so "
                                        "there is nothing to compare.", "rows": [], "more": 0}
        elif ok:
            out["diff"] = {"title": "The G-code of the last good print is gone from the printer, "
                                    "so the settings can't be compared.", "rows": [], "more": 0}
    elif keys:
        out["items"].append({"level": "info", "title": "This model before",
                             "text": "Not printed on this printer before (by file name)."})
    return out


def last_good_file(jobs, keys):
    for job in same_model(finished_jobs(jobs), keys):
        if job.get("status") == "completed" and job.get("exists"):
            return job
    return None


def fleet_row(name, data, slice_info=None, now=None):
    """One printer in 'Your printers'."""
    now = time.time() if now is None else now
    jobs = finished_jobs(data.get("jobs") or [])
    models = time_models(jobs)
    factors = {m["group"]: m["factor"] for m in models}
    state, left, cls = printer_state(data, factors, now)
    row = {"name": name, "state": state, "cls": cls, "rate": "", "material": "", "free_at": ""}
    if jobs:
        done = sum(1 for j in jobs if j.get("status") == "completed")
        row["rate"] = "%d%% of %d finished" % (pct(done, len(jobs)), len(jobs))
    material = (slice_info or {}).get("material")
    if material:
        same = [j for j in jobs if first_text(meta(j).get("filament_type")) == material]
        if same:
            ok = sum(1 for j in same if j.get("status") == "completed")
            row["material"] = "%s: %d%% of %d" % (material, pct(ok, len(same)), len(same))
        else:
            row["material"] = "no %s prints yet" % material
    if left:
        row["free_at"] = "free around %s" % fmt_clock(now + left, now)
    return row


# --------------------------------------------------------------------------------------------
# Other addresses (shared idea with Klipper Sync)
# --------------------------------------------------------------------------------------------

def storage_folder():
    storage = getattr(getattr(getattr(orca, "host", None), "plugin", None), "storage", None)
    folder = None
    if callable(storage):
        try:
            folder = storage()
        except Exception:
            folder = None
    if not isinstance(folder, (str, os.PathLike)) or not os.path.isdir(folder):
        folder = os.path.dirname(os.path.abspath(__file__))
    return str(folder)


def address_file(storage=None):
    return os.path.join(storage or storage_folder(), "addresses.json")


# Klipper Sync's id on OrcaCloud. OrcaSlicer gives a plugin installed by hand the storage folder
# plugin_data/<module name>, and one installed from OrcaCloud
# plugin_data/_subscribed/<user id>/<plugin id>.
KLIPPER_SYNC_CLOUD_ID = "5621380b-f627-487c-ad1c-96d42da0822c"


def klipper_sync_address_files(storage=None):
    """Where Klipper Sync may keep its addresses, whichever way either plugin was installed."""
    storage = os.path.abspath(storage or storage_folder())
    root, user = storage, None
    for _ in range(4):
        if os.path.basename(root).lower() == "plugin_data":
            break
        if os.path.basename(os.path.dirname(root)) == "_subscribed":
            user = os.path.basename(root)
        root = os.path.dirname(root)
    else:
        # No plugin_data folder above us: we run from the plugin's own folder.
        return [os.path.join(os.path.dirname(storage), "klipper_sync", "addresses.json")]
    paths = [os.path.join(root, "klipper_sync", "addresses.json")]
    subscribed = os.path.join(root, "_subscribed")
    if user:
        users = [user]
    else:
        try:
            users = sorted(os.listdir(subscribed))
        except OSError:
            users = []
    paths += [os.path.join(subscribed, u, KLIPPER_SYNC_CLOUD_ID, "addresses.json") for u in users]
    return paths


def load_addresses(path=None, fallback=True, storage=None):
    """{printer preset name: address}. Falls back to the addresses typed into Klipper Sync,
    which keeps the same kind of file in its own storage folder."""
    paths = [path or address_file(storage)]
    if fallback and path is None:
        paths += klipper_sync_address_files(storage)
    merged = {}
    for p in reversed(paths):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            merged.update({str(k): str(v) for k, v in data.items() if v})
    return merged


def save_addresses(addresses, path=None):
    path = path or address_file()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in addresses.items() if v}, f, indent=2, sort_keys=True)


# --------------------------------------------------------------------------------------------
# The panel
# --------------------------------------------------------------------------------------------

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--bg:var(--orca-bg,#fff);--fg:var(--orca-fg,#1d2227);--muted:var(--orca-muted,#6b737b);
--line:var(--orca-border,rgba(127,127,127,.28));--accent:var(--orca-accent,#009688);
--soft:rgba(127,127,127,.09);--err:#d64545;--warn:#d98a00;--info:#3a7bd5;--ok:#2e9e5b}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;padding:12px;font:13px/1.45 var(--orca-font,system-ui,-apple-system,"Segoe UI",sans-serif);
color:var(--fg);background:var(--bg)}
h1{font-size:15px;margin:0}
h2{font-size:13px;margin:14px 0 6px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.sub{color:var(--muted);font-size:12px;margin:2px 0 10px;overflow-wrap:anywhere}
.conn{display:flex;gap:8px;align-items:flex-start;padding:8px 10px;border:1px solid var(--line);
border-radius:8px;margin-bottom:10px;font-size:12px}
.conn .dot{width:9px;height:9px;border-radius:50%;margin-top:4px;flex:none;background:var(--muted)}
.conn.ok .dot{background:var(--ok)}.conn.bad .dot{background:var(--err)}.conn.wait .dot{background:var(--warn)}
.conn.busy .dot{background:var(--info)}
.actions{display:flex;gap:6px;margin-bottom:6px}
button{font:inherit;color:inherit;background:transparent;border:1px solid var(--line);border-radius:6px;
padding:5px 12px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button:disabled{opacity:.45;cursor:default}
.card{border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:8px}
.big{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.2}
.muted{color:var(--muted);font-size:12px}
.item{border-left:3px solid var(--muted);padding:2px 0 2px 8px;margin:7px 0;font-size:12px}
.item.error{border-left-color:var(--err)}.item.warn{border-left-color:var(--warn)}
.item.info{border-left-color:var(--info)}.item.ok{border-left-color:var(--ok)}
.item .t{font-weight:600;font-size:13px}
.bars{display:grid;grid-template-columns:auto 1fr auto;gap:4px 8px;align-items:center;font-size:12px;margin:4px 0}
.bar{height:7px;border-radius:4px;background:var(--soft);overflow:hidden}
.bar i{display:block;height:100%;background:var(--ok)}
.bar i.low{background:var(--warn)}
.num{font-variant-numeric:tabular-nums;color:var(--muted);text-align:right}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0;font-size:12px}
.chip{padding:2px 9px;border-radius:20px;border:1px solid var(--line)}
.chip b{font-weight:600}
.row{display:flex;justify-content:space-between;gap:8px;padding:3px 0;font-size:12px;border-bottom:1px solid var(--soft)}
.row span:last-child{color:var(--muted);text-align:right;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:4px}
td{padding:3px 4px;border-bottom:1px solid var(--soft);vertical-align:top;overflow-wrap:anywhere}
td.k{color:var(--muted);width:38%}
td.then{text-decoration:line-through;color:var(--muted)}
details summary{cursor:pointer;color:var(--muted);font-size:12px;margin-top:6px}
.note{font-size:12px;padding:8px 10px;border-radius:7px;background:var(--soft);margin-bottom:8px}
.note.err{color:var(--err)}
.stop{border:1px solid var(--line);border-left:4px solid var(--muted);border-radius:7px;padding:7px 9px;margin-bottom:6px;font-size:12px}
.stop.error{border-left-color:var(--err)}.stop.warn{border-left-color:var(--warn)}.stop.info{border-left-color:var(--info)}
.stop .t{font-weight:600;font-size:13px}
.stop .f{overflow-wrap:anywhere;color:var(--muted)}
.stop .r{margin-top:3px}
.fleet{display:grid;grid-template-columns:auto 1fr;gap:2px 8px;font-size:12px;padding:6px 0;border-bottom:1px solid var(--soft)}
.fleet .dot{width:9px;height:9px;border-radius:50%;margin-top:4px;background:var(--muted)}
.fleet .dot.ok{background:var(--ok)}.fleet .dot.busy{background:var(--info)}.fleet .dot.bad{background:var(--err)}
.fleet .dot.wait{background:var(--warn)}
.fleet b{font-weight:600}
.foot{color:var(--muted);font-size:11px;margin-top:12px}
.addr{border:1px dashed var(--line);border-radius:8px;padding:8px 10px;margin-bottom:10px;font-size:12px}
.addr p{margin:0 0 6px;color:var(--muted)}
.addrrow{display:flex;gap:6px}
.addrrow input{flex:1;min-width:0;font:inherit;color:inherit;background:transparent;
border:1px solid var(--line);border-radius:6px;padding:4px 8px}
button.link{border:0;padding:0;margin-top:6px;color:var(--accent);text-decoration:underline;font-size:12px}
</style></head><body>
<h1>Print Memory</h1>
<div class="sub" id="sub">Reading the selected printer…</div>
<div class="conn wait" id="conn"><div class="dot"></div><div id="conntext">Not read yet</div></div>
<div class="addr" id="addr" hidden>
  <p id="addrnote">Can't reach it from here? Enter another address for this printer, for example its
  Tailscale or VPN address. The printer preset stays as it is.</p>
  <div class="addrrow"><input id="addrin" placeholder="e.g. 100.64.0.12 or printer.local:7125">
  <button id="addruse">Use</button></div>
  <button class="link" id="addrreset" hidden>Use the preset's address again</button>
</div>
<div class="actions">
  <button class="primary" id="refresh">Refresh</button>
  <button id="fleetbtn">Compare printers</button>
</div>
<div id="note" class="note" hidden></div>

<h2>This plate</h2>
<div class="card" id="slice"><div class="muted">Slice the plate and Print Memory tells you how long it
will really take on this printer and how prints like it went before.</div></div>

<h2 id="fleeth" hidden>Your printers</h2>
<div class="card" id="fleet" hidden></div>

<h2 id="printerh" hidden>This printer</h2>
<div class="card" id="printer" hidden></div>

<h2 id="stopsh" hidden>Recent stops</h2>
<div id="stops"></div>

<div class="foot">Print Memory only reads Moonraker's print history. It never sends G-code or changes the printer.</div>
<script>
(function(){
var $=function(id){return document.getElementById(id)};
var busy=false;
function esc(x){return String(x==null?"":x).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function setBusy(b){busy=b;$("refresh").disabled=b;$("fleetbtn").disabled=b}
function note(text,isErr){var n=$("note");if(!text){n.hidden=true;return}
  n.hidden=false;n.textContent=text;n.className="note"+(isErr?" err":"")}
function conn(cls,text){$("conn").className="conn "+cls;$("conntext").textContent=text}
function refresh(force){if(busy)return;note("");setBusy(true);conn("wait","Reading the print history…");
  orca.postMessage({command:"refresh",force:force===true})}
function bars(rows){return '<div class="bars">'+rows.map(function(r){
  return "<span>"+esc(r.name)+'</span><div class="bar"><i class="'+(r.pct<70?"low":"")+'" style="width:'+
    r.pct+'%"></i></div><span class="num">'+r.done+"/"+r.total+" · "+r.pct+"%</span>"}).join("")+"</div>"}
function item(i){return '<div class="item '+esc(i.level)+'"><div class="t">'+esc(i.title)+"</div>"+esc(i.text)+"</div>"}
function renderPrinter(p){
  $("sub").textContent="Printer preset: "+p.printer;
  conn(p.state_cls||"ok",p.host+" · "+p.state+(p.other_address?" (other address)":""));
  showAddr(false,p.other_address);
  var h='<div class="muted">'+esc(p.headline)+"</div>";
  if(p.stops.length)h+='<div class="chips">'+p.stops.map(function(s){return '<span class="chip"><b>'+
    s.n+"</b> "+esc(s.label.replace(/^(?!Klipper|Moonraker)./,function(c){return c.toLowerCase()}))+
    "</span>"}).join("")+"</div>";
  h+=p.insights.map(function(i){return item({level:i.level,title:"",text:i.text})}).join("");
  if(p.times.length)h+='<div class="t" style="margin-top:8px;font-weight:600">Real time against Orca\'s estimate</div>'+
    p.times.map(function(t){return '<div class="row"><span>'+esc(t.name)+"</span><span>"+esc(t.text)+
    " (last "+t.n+")</span></div>"}).join("");
  if(p.warmup)h+='<div class="row"><span>Heating, homing and mesh</span><span>about '+esc(p.warmup)+"</span></div>";
  if(p.materials.length)h+='<div style="margin-top:8px;font-weight:600">Finished by material</div>'+bars(p.materials);
  if(p.lengths.length)h+='<div style="margin-top:8px;font-weight:600">Finished by length</div>'+bars(p.lengths);
  if(p.totals)h+='<div class="muted" style="margin-top:6px">'+esc(p.totals)+"</div>";
  $("printer").innerHTML=h;$("printer").hidden=false;$("printerh").hidden=false;
  $("stops").innerHTML=p.recent_stops.map(function(s){
    return '<div class="stop '+esc(s.level)+'"><div class="t">'+esc(s.kind)+' <span class="muted">· '+esc(s.when)+
      '</span></div><div class="f">'+esc(s.file)+"</div>"+(s.progress?"<div>"+esc(s.progress)+"</div>":"")+
      (s.reason?'<div class="r"><b>Klipper said:</b> '+esc(s.reason)+"</div>":"")+
      (s.detail?'<div class="r muted">'+esc(s.detail)+"</div>":"")+
      (s.hint?'<div class="r muted">'+esc(s.hint)+"</div>":"")+
      (s.repeat?'<div class="r"><b>'+esc(s.repeat)+"</b></div>":"")+"</div>"}).join("");
  $("stopsh").hidden=!p.recent_stops.length;
}
function renderSlice(s){
  var h='<div class="muted">'+esc(s.title)+(s.material?" · "+esc(s.material):"")+"</div>";
  if(s.forecast){h+='<div class="big">About '+esc(s.forecast)+'</div><div class="muted">'+esc(s.sub)+
    (s.range?" · "+esc(s.range):"")+"</div><div>"+esc(s.finish)+'</div><div class="muted" style="margin-top:4px">'+
    esc(s.basis)+"</div>"}
  else h+='<div class="muted">The G-code has no time estimate.</div>';
  h+=s.items.map(item).join("");
  if(s.diff){
    if(s.diff.rows.length){var tr=function(r){return '<tr><td class="k">'+esc(r.label)+'</td><td class="then">'+
      esc(r.then)+"</td><td>"+esc(r.now)+"</td></tr>"};var n=s.diff.rows.length+s.diff.more,top=s.diff.rows.slice(0,10),
      rest=s.diff.rows.slice(10);
      h+='<div style="margin-top:8px;font-weight:600">'+esc(s.diff.title)+'</div><div class="muted">'+
      (n===1?"1 setting":n+" settings")+" changed. Struck through: then. Plain: now.</div><table>"+top.map(tr).join("")+
      "</table>";if(rest.length)h+="<details><summary>"+(rest.length+s.diff.more)+" more</summary><table>"+
      rest.map(tr).join("")+"</table>"+(s.diff.more?'<div class="muted">and '+s.diff.more+" more</div>":"")+
      "</details>"}
    else h+='<div class="muted" style="margin-top:6px">'+esc(s.diff.title)+"</div>";
  }
  $("slice").innerHTML=h;
}
function fleetRow(r){return '<div class="fleet"><div class="dot '+esc(r.cls)+'"></div><div><b>'+esc(r.name)+
  "</b> · "+esc(r.state)+(r.free_at?" · "+esc(r.free_at):"")+'<div class="muted">'+
  esc([r.rate,r.material].filter(Boolean).join(" · "))+"</div></div></div>"}
orca.onMessage(function(m){
  if(!m)return;
  if(m.command==="printer"){renderPrinter(m.report);if(!m.more)setBusy(false)}
  else if(m.command==="slice"){renderSlice(m.report)}
  else if(m.command==="noslice"){$("slice").innerHTML='<div class="muted">'+esc(m.text)+"</div>"}
  else if(m.command==="status"){conn(m.cls||"wait",m.text)}
  else if(m.command==="note"){note(m.text,!!m.error)}
  else if(m.command==="done"){setBusy(false)}
  else if(m.command==="error"){if(m.printer)$("sub").textContent="Printer preset: "+m.printer;
    conn("bad",m.text);$("printer").hidden=true;$("printerh").hidden=true;$("stops").innerHTML="";
    $("stopsh").hidden=true;if(m.printer!==undefined)showAddr(true,m.other_address||"");setBusy(false)}
  else if(m.command==="fleet_start"){$("fleet").innerHTML='<div class="muted">'+esc(m.text)+"</div>";
    $("fleet").hidden=false;$("fleeth").hidden=false}
  else if(m.command==="fleet_wait"){var f=$("fleet"),w=$("fleetwait");if(!w){
    f.insertAdjacentHTML("beforeend",'<div class="muted" id="fleetwait"></div>');w=$("fleetwait")}
    w.textContent=m.text}
  else if(m.command==="fleet_row"){var f=$("fleet"),w=$("fleetwait");if(w)w.remove();
    if(f.querySelector(".muted:only-child"))f.innerHTML="";
    f.insertAdjacentHTML("beforeend",fleetRow(m.row))}
  else if(m.command==="autocheck"){clearTimeout(autoTimer);autoTimer=setTimeout(auto,800)}
});
var autoTimer=null;
function auto(){if(busy){autoTimer=setTimeout(auto,800);return}refresh(false)}
function showAddr(failed,other){
  $("addr").hidden=!failed&&!other;
  $("addrnote").hidden=!failed;$("addrin").parentNode.hidden=!failed;
  if(failed)$("addrin").value=other||"";
  $("addrreset").hidden=!other;
  $("addrreset").textContent=other?"Using "+other+". Use the preset's address again":"";
}
function setAddress(a){if(busy)return;note("");setBusy(true);conn("wait","Reading the print history…");
  orca.postMessage({command:"set_address",address:a})}
$("addruse").addEventListener("click",function(){var a=$("addrin").value.trim();if(a)setAddress(a)});
$("addrin").addEventListener("keydown",function(e){if(e.key==="Enter"){var a=this.value.trim();if(a)setAddress(a)}});
$("addrreset").addEventListener("click",function(){setAddress("")});
$("refresh").addEventListener("click",function(){refresh(true)});
$("fleetbtn").addEventListener("click",function(){if(busy)return;setBusy(true);orca.postMessage({command:"fleet"})});
refresh(false);
})();
</script></body></html>"""


def snapshot_orca(with_fleet=False):
    """Read the selected printer, the project and, for the fleet, every printer preset with a
    print host. Must run on the UI thread."""
    bundle = orca.host.preset_bundle()
    printer = bundle.current_printer_preset()

    def value(key):
        try:
            return plain(bundle.full_config_value(key))
        except Exception:
            return None

    snap = {"printer": printer.name if printer is not None else "",
            "host": first_text(value("print_host")), "api_key": first_text(value("printhost_apikey")),
            "project": "", "objects": []}
    try:
        snap["project"] = str(orca.host.plater().project_path() or "")
    except Exception:
        pass
    try:
        snap["objects"] = [str(o.name) for o in orca.host.model().objects()][:20]
    except Exception:
        pass
    if with_fleet:
        fleet = []
        try:
            coll = bundle.printers
            for i in range(coll.size()):
                preset = coll.preset(i)
                if not getattr(preset, "is_visible", True):
                    continue
                try:
                    host = first_text(plain(preset.config_value("print_host")))
                except Exception:
                    host = ""
                if not host:
                    continue
                try:
                    key = first_text(plain(preset.config_value("printhost_apikey")))
                except Exception:
                    key = ""
                fleet.append({"name": preset.name, "host": host, "api_key": key})
        except Exception:
            pass
        snap["fleet"] = fleet
    return snap


def temp_roots():
    roots = []
    for var in ("TMP", "TEMP", "TMPDIR"):
        folder = os.environ.get(var)
        if folder and os.path.isabs(folder):
            root = os.path.join(folder, "orcaslicer_model")
            if root not in roots:
                roots.append(root)
    if not roots and os.name != "nt":
        roots.append("/tmp/orcaslicer_model")
    return roots


if orca is not None:

    class PrintMemory(orca.script.ScriptPluginCapabilityBase):
        panel = None
        _jobs = None
        _cancelled = False
        _cache = {}              # address -> data from Moonraker
        _gcode_path = None       # newest exported G-code (set by the lifecycle event)
        _slice = None            # parsed newest G-code
        _earlier = {}            # (address, filename) -> parsed tail of an earlier print

        def get_name(self):
            return "Print Memory"

        def execute(self):
            if self.panel is not None and self.panel.is_open():
                self.panel.show()
                return orca.ExecutionResult.success("Print Memory is already open.")
            jobs = queue.Queue()
            PrintMemory._jobs = jobs
            PrintMemory._cancelled = False
            try:
                PrintMemory.panel = orca.host.ui.create_dock_panel(
                    html=PAGE, title="Print Memory", width=380, height=720,
                    on_message=self._on_message, on_close=self._on_close, dock="right")
                while True:
                    job = jobs.get()
                    if self._cancelled or job.get("command") == "close":
                        return orca.ExecutionResult.success("Print Memory closed.")
                    try:
                        if job["command"] == "refresh":
                            self._refresh(job)
                        elif job["command"] == "fleet":
                            self._fleet(job)
                        elif job["command"] == "set_address":
                            self._set_address(job)
                    except Exception as error:  # never let one job kill the panel
                        self._post({"command": "error", "text": "Print Memory hit an error: %s"
                                    % error})
            except Exception as error:
                return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError,
                                                    "Print Memory failed: %s" % error)
            finally:
                if PrintMemory._jobs is jobs:
                    PrintMemory._jobs = None
                PrintMemory.panel = None

        # UI thread: read presets and the project here, then hand the job to the worker.
        def _on_message(self, message):
            message = dict(message) if isinstance(message, dict) else {}
            command = message.get("command")
            if command not in ("refresh", "fleet", "set_address"):
                return
            try:
                snap = snapshot_orca(with_fleet=(command == "fleet"))
            except Exception as error:
                self._post({"command": "error", "text": "Couldn't read the presets: %s" % error})
                return
            job = {"command": command, "snap": snap, "force": bool(message.get("force"))}
            if command == "set_address":
                job["address"] = str(message.get("address") or "")[:200]
            self._enqueue(job)

        def _on_close(self):
            self._enqueue({"command": "close"})

        def on_cancelled(self):
            PrintMemory._cancelled = True
            self._enqueue({"command": "close"})

        # Must return at once. A finished G-code export only records the file and nudges the
        # page, which then asks for a normal refresh (so presets are read on the UI thread).
        def on_lifecycle_event(self, event, ctx):
            events = getattr(orca, "LifecycleEvent", None)
            if events is None:
                return
            if event == getattr(events, "GCodeExportFinished", None):
                code = getattr(ctx, "code", None)
                ok = getattr(getattr(orca, "LifecycleEvtCode", None), "Ok", None)
                path = str(getattr(ctx, "msg", "") or "")
                if (ok is None or code == ok) and path and "\n" not in path:
                    PrintMemory._gcode_path = path
                    self._post({"command": "autocheck"})
            elif event in (getattr(events, "PresetSelected", None),
                           getattr(events, "ProjectOpened", None),
                           getattr(events, "NewProject", None)):
                if event != getattr(events, "PresetSelected", None):
                    PrintMemory._gcode_path, PrintMemory._slice = None, None
                self._post({"command": "autocheck"})

        def _enqueue(self, job):
            jobs = self._jobs
            if jobs is not None:
                jobs.put(job)

        def _post(self, message):
            panel = self.panel
            if panel is not None and panel.is_open():
                panel.post(message)

        def _wait_permission(self, action, what, notify=None):
            """Run `action`, waiting while OrcaSlicer asks the user for permission."""
            deadline = time.monotonic() + PERMISSION_WAIT
            asked = False
            while True:
                try:
                    return action()
                except PermissionError:
                    if self._cancelled or time.monotonic() > deadline:
                        raise MoonrakerError(
                            "OrcaSlicer didn't allow Print Memory to %s. Allow it in the prompt "
                            "(or in Plugins → Print Memory → Permissions) and press Refresh."
                            % what)
                    if not asked:
                        asked = True
                        text = ("OrcaSlicer is asking whether Print Memory may %s. Choose Yes "
                                "and it goes on by itself." % what)
                        if notify:
                            notify(text)
                        else:
                            self._post({"command": "status", "cls": "wait", "text": text})
                    time.sleep(1.5)

        def _read(self, address, api_key, force=False, timeout=HTTP_TIMEOUT, notify=None):
            cached = PrintMemory._cache.get(address)
            if cached and not force and time.time() - cached["fetched"] < CACHE_SECONDS:
                return cached, Moonraker(address, api_key, timeout)
            moon = Moonraker(address, api_key, timeout)
            data = self._wait_permission(moon.fetch, "connect to %s" % moon.label, notify)
            PrintMemory._cache[address] = data
            return data, moon

        def _set_address(self, job):
            addresses = load_addresses(fallback=False)
            address = (job.get("address") or "").strip()
            name = job["snap"]["printer"]
            if address:
                addresses[name] = address
            else:
                addresses.pop(name, None)
            try:
                save_addresses(addresses)
            except OSError as error:
                self._post({"command": "error", "printer": name,
                            "text": "Couldn't save the address: %s" % error})
                return
            job["force"] = True
            self._refresh(job)

        def _refresh(self, job):
            snap = job["snap"]
            other = load_addresses().get(snap["printer"], "")
            address = other or snap["host"]
            where = {"printer": snap["printer"], "other_address": other}
            try:
                data, moon = self._read(address, snap["api_key"], force=job.get("force"))
            except MoonrakerError as error:
                self._post(dict(where, command="error", text=str(error)))
                return
            report = summarize_printer(data)
            report.update({"printer": snap["printer"], "other_address": other,
                           "host": moon.label, "warmup": fmt_dur(report["warmup"])
                           if report["warmup"] else None})
            self._post({"command": "printer", "report": report, "more": True})
            try:
                self._slice_report(snap, data, moon)
            finally:
                self._post({"command": "done"})

        def _slice_report(self, snap, data, moon):
            path = PrintMemory._gcode_path
            if path and (PrintMemory._slice is None or PrintMemory._slice.get("path") != path):
                try:
                    PrintMemory._slice = self._wait_permission(
                        lambda: read_local_gcode(path), "read the sliced G-code")
                except (OSError, MoonrakerError) as error:
                    PrintMemory._slice = None
                    self._post({"command": "noslice", "text": "Couldn't read the sliced G-code: "
                                "%s" % error})
                    return
            info = PrintMemory._slice
            if info is None:
                return
            if info.get("printer_preset") and info["printer_preset"] != snap["printer"]:
                self._post({"command": "noslice", "text": (
                    "The last slice was made for \"%s\". Slice again for this printer to see "
                    "its forecast." % info["printer_preset"])})
                return
            keys = model_keys(snap.get("project"), (info.get("objects") or [])
                              + (snap.get("objects") or []))
            earlier = None
            good = last_good_file(data.get("jobs") or [], keys)
            if good is not None:
                key = (moon.label, good.get("filename"))
                earlier = PrintMemory._earlier.get(key)
                if earlier is None:
                    try:
                        tail = moon.gcode_tail(good["filename"])
                    except MoonrakerError:
                        tail = ""
                    earlier = {"config": parse_config_block(tail),
                               "filename": good.get("filename"),
                               "start_time": good.get("start_time")}
                    PrintMemory._earlier[key] = earlier
            self._post({"command": "slice",
                        "report": analyze_slice(info, data, keys=keys, earlier=earlier)})

        def _fleet(self, job):
            snap = job["snap"]
            others = load_addresses()
            seen, printers = {}, []
            for p in snap.get("fleet") or []:
                address = others.get(p["name"]) or p["host"]
                key = address.lower().rstrip("/")
                if key in seen:
                    seen[key]["names"].append(p["name"])
                else:
                    seen[key] = {"address": address, "api_key": p["api_key"], "names": [p["name"]]}
                    printers.append(seen[key])
            if not printers:
                self._post({"command": "fleet_start", "text": "No printer preset has a print "
                                                              "host set."})
                self._post({"command": "done"})
                return
            self._post({"command": "fleet_start", "text": "Asking %s…" % plural(
                len(printers), "printer")})
            for p in printers:
                if self._cancelled:
                    return
                name = p["names"][0] + (" (+%d)" % (len(p["names"]) - 1) if len(p["names"]) > 1
                                        else "")
                try:
                    data, _ = self._read(
                        p["address"], p["api_key"], timeout=FLEET_TIMEOUT,
                        notify=lambda text: self._post({"command": "fleet_wait", "text": text}))
                    row = fleet_row(name, data, PrintMemory._slice)
                except MoonrakerError as error:
                    text = str(error)
                    row = {"name": name, "cls": "bad", "rate": "", "material": "", "free_at": "",
                           "state": ("not a Moonraker printer" if "isn't Moonraker" in text
                                     else "can't reach %s" % p["address"]
                                     if "Can't reach" in text else short(text, 90))}
                self._post({"command": "fleet_row", "row": row})
            self._post({"command": "done"})

    @orca.plugin
    class PrintMemoryPlugin(orca.base):
        def register_capabilities(self):
            request = getattr(orca, "request_permissions", None)
            roots = temp_roots()
            if callable(request) and roots:
                try:
                    request(fs_read=[r + os.sep + "*" for r in roots])
                except Exception:
                    pass
            orca.register_capability(PrintMemory)
