"""Shared plumbing for the keyboard-layout-per-app helpers: the assignments file, bounded
hyprctl / xkbcli / menu calls, and the input limits both helpers enforce.

Everything external goes through plugin_safety (vendored, identical to the shared copy):

  * programs resolve to root-owned binaries in /usr/bin -- never PATH -- and run with
    PATH=/usr/bin, a deadline, an output ceiling and a whole-process-group kill. Each one is
    additionally started under /usr/bin/timeout, so it dies on its deadline even if the
    helper that started it is SIGKILLed before it can clean up;
  * a helper told to stop (SIGTERM/SIGHUP/SIGINT) kills the process group of every child it
    started before exiting (exit_cleanly_on_signals);
  * the assignments file is reached without following symlinks, must be a regular file we
    own, is read with a size cap, and is written through a random O_EXCL temporary file in
    the same directory that is renamed over the destination;
  * strings from Hyprland events, hyprctl output and the file are length- and
    character-checked before they become JSON keys, argv or menu rows, and the number of
    remembered apps is capped.
"""
import configparser
import math
import os
import re
import signal
import stat
import sys

import plugin_safety as safe

HOME = safe.home_dir()
CONFIG = os.path.join(HOME, ".config/omarchy/kb-layout-per-app.json")
LEGACY = os.path.join(HOME, ".local/state/omarchy/kb-layout-per-app.json")

CONFIG_MAX_BYTES = 128 * 1024
MAX_APPS = 500              # remembered apps; new ones are refused past this, never evicted
MAX_NAME = 256              # window class or layout display name
MAX_KEYBOARDS = 64          # devices read from one hyprctl reading
MAX_LAYOUTS = 32            # entries in kb_layout (xkb itself allows four groups)

HYPRCTL_TIMEOUT = 3
HYPRCTL_MAX_OUTPUT = 256 * 1024
XKBCLI_TIMEOUT = 10
XKBCLI_MAX_OUTPUT = 2 * 1024 * 1024
LAYOUTS_MAX_OUTPUT = 256 * 1024
MAX_XKB_LINE = 512
MENU_TIMEOUT = 600
MENU_MAX_OUTPUT = 64 * 1024
MAX_MENU_ROWS = 2000
NOTIFY_TIMEOUT = 10

SYSTEM_APP_DIRS = ("/usr/share/applications", "/usr/local/share/applications")
USER_APP_DIR = os.path.join(HOME, ".local/share/applications")
DESKTOP_MAX_BYTES = 256 * 1024
MAX_DESKTOP_FILES = 2000
MAX_CLIENTS = 1000

_last_log = [None]


def log(message):
    """stderr, without repeating the previous message: the daemon hits the same problem on
    every focus change and should say so once."""
    if message != _last_log[0]:
        _last_log[0] = message
        print(f"kb-layout-per-app: {message}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ names

def clean_name(value, limit=MAX_NAME):
    """value itself when it can be stored or shown as a window class or layout name: a
    non-empty string of at most `limit` printable characters (no tabs, newlines or other
    control characters) with no surrounding whitespace. None otherwise."""
    if isinstance(value, str) and 0 < len(value) <= limit and value.isprintable() and value == value.strip():
        return value
    return None


# Hyprland lowercases device names and turns spaces into dashes. hyprctl joins its arguments
# into one request, so a name must not contain whitespace, and must not look like a flag.
_DEVICE = re.compile(r"[^\s;-][^\s;]{0,127}")
_LAYOUT_TARGET = re.compile(r"next|prev|[0-9]{1,2}")


def device_name(value):
    if isinstance(value, str) and _DEVICE.fullmatch(value) and value.isprintable():
        return value
    return None


# ------------------------------------------------------------------ processes

def bounded(argv, *, timeout, max_output):
    """safe.run with the program started under /usr/bin/timeout, so it cannot outlive its
    deadline even if this process is killed outright. argv[0] is a tool name."""
    seconds = max(1, math.ceil(timeout))
    return safe.run(["timeout", "--kill-after=1", str(seconds), safe.tool(argv[0]), *argv[1:]],
                    timeout=seconds + 3, max_output=max_output)


def output_of(argv, *, timeout, max_output):
    """stdout of a run that exited 0 within its deadline and output ceiling; None for any
    failure, including a tool that is missing or not a trusted system binary."""
    try:
        result = bounded(argv, timeout=timeout, max_output=max_output)
    except (safe.UnsafeError, OSError) as e:
        log(f"{argv[0]}: {e}")
        return None
    return result.stdout if result.ok else None


def kill_children():
    """SIGKILL the process group of every child this process started. Children from
    safe.run/spawn lead their own session, so each group is exactly that child and whatever
    it started. Runs inside a signal handler, so it takes no locks (no threading calls)."""
    try:
        tasks = os.listdir("/proc/self/task")
    except OSError:
        tasks = [str(os.getpid())]
    for task in tasks:
        if not task.isdigit():
            continue
        try:
            blob = safe.read_system_file(f"/proc/self/task/{task}/children", 64 * 1024)
        except (safe.UnsafeError, OSError):
            continue
        for pid in (blob or b"").split():
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except (ValueError, OSError):
                pass


def exit_cleanly_on_signals():
    def handler(signum, _frame):
        kill_children()
        os._exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, handler)


# ------------------------------------------------------------------ hyprctl

def hypr_json(what):
    blob = output_of(["hyprctl", "-j", what], timeout=HYPRCTL_TIMEOUT, max_output=HYPRCTL_MAX_OUTPUT)
    if blob is None:
        return None
    try:
        return safe.loads(blob, max_depth=8, max_items=20000, max_string=64 * 1024)
    except (ValueError, UnicodeDecodeError, safe.UnsafeError):
        return None


def seat_keyboards():
    """Hyprland's keyboard list, at most MAX_KEYBOARDS entries, or None when the query
    failed -- which callers must tell apart from a seat that has no keyboards."""
    devices = hypr_json("devices")
    if not isinstance(devices, dict) or not isinstance(devices.get("keyboards"), list):
        return None
    return [k for k in devices["keyboards"][:MAX_KEYBOARDS] if isinstance(k, dict)]


def switchable_keyboards(keyboards=None):
    """Keyboards carrying more than one layout, under a name hyprctl can be handed. On a
    laptop that is often ten devices (lid switch, power button, hotkeys...), and all of
    them are switched together."""
    if keyboards is None:
        keyboards = seat_keyboards()
    return [k for k in keyboards or []
            if device_name(k.get("name")) and isinstance(k.get("layout"), str) and "," in k["layout"]]


def switch_layout(device, target):
    """hyprctl switchxkblayout for one device; target is "next", "prev" or a layout index."""
    target = str(target)
    if not device_name(device) or not _LAYOUT_TARGET.fullmatch(target):
        return False
    return output_of(["hyprctl", "switchxkblayout", device, target],
                     timeout=HYPRCTL_TIMEOUT, max_output=4096) is not None


def widget_keyboards(keyboards):
    """Only what the bar widget reads, each field checked: a field that is missing or fails
    its check is left out, which the widget already treats as "not reported"."""
    out = []
    for keyboard in keyboards[:MAX_KEYBOARDS]:
        entry = {}
        for field in ("name", "layout", "active_keymap"):
            value = clean_name(keyboard.get(field))
            if value is not None:
                entry[field] = value
        index = keyboard.get("active_layout_index")
        if type(index) is int and 0 <= index < MAX_LAYOUTS:
            entry["active_layout_index"] = index
        out.append(entry)
    return {"keyboards": out}


def running_classes():
    clients = hypr_json("clients")
    if not isinstance(clients, list):
        return set()
    return {c["class"] for c in clients[:MAX_CLIENTS] if isinstance(c, dict) and clean_name(c.get("class"))}


# ------------------------------------------------------------------ xkb layout table

# KeyboardLayoutModel.layoutBriefs() reads a line matching /^\s*- / as the start of a block
# and /^  (brief|description): (.*)$/ as a field. These mirror JavaScript exactly: its \s is
# this character set, and its "." stops at these four line terminators.
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_XKB_BLOCK = re.compile(f"[{_JS_SPACE}]*- ")
_XKB_FIELD = re.compile("  (?:brief|description): [^\n\r\u2028\u2029]*")


def layout_listing(text):
    """The lines of `xkbcli list` that layoutBriefs() reads, and nothing else, so the widget
    parses exactly what it did before from a fraction of the output.

    A block start only resets the pending brief, so each is written as a bare "- " and runs
    of them collapse to one. A field line that is too long or carries control characters is
    dropped; that can only cost its block a brief, never pair a brief with another block's
    description, because the block starts are always kept. Output stops at
    LAYOUTS_MAX_OUTPUT bytes."""
    out, size, after_block = [], 0, False
    for line in text.split("\n"):
        if _XKB_BLOCK.match(line):
            if after_block:
                continue
            piece, after_block = "- ", True
        elif len(line) <= MAX_XKB_LINE and _XKB_FIELD.fullmatch(line) and line.isprintable():
            piece, after_block = line, False
        else:
            continue
        size += len(piece.encode("utf-8")) + 1
        if size > LAYOUTS_MAX_OUTPUT:
            break
        out.append(piece)
    return "\n".join(out)


# ------------------------------------------------------------------ assignments file

def _parse_assignments(blob, path):
    """(assignments, ok). Entries that are not clean class -> layout strings are skipped."""
    if not blob.strip():
        return {}, True
    try:
        data = safe.loads(blob, max_depth=4, max_items=4 * MAX_APPS, max_string=CONFIG_MAX_BYTES)
    except (ValueError, UnicodeDecodeError, safe.UnsafeError) as e:
        log(f"{path} is not usable JSON ({e}); leaving it untouched until it is fixed")
        return {}, False
    if not isinstance(data, dict):
        log(f"{path} is not a JSON object; leaving it untouched until it is fixed")
        return {}, False
    clean = {k: v for k, v in data.items() if clean_name(k) and clean_name(v)}
    if len(clean) != len(data):
        log(f"ignoring {len(data) - len(clean)} malformed entries in {path}; the next save drops them")
    return clean, True


def load_config(migrate=True):
    """(assignments, writable).

    writable is False when the file exists but cannot be trusted or parsed (a symlink, not
    ours, over CONFIG_MAX_BYTES, broken JSON). Callers must then not write: a half-finished
    hand edit is left for its author, and a planted file is never replaced."""
    try:
        blob = safe.read_file(CONFIG, CONFIG_MAX_BYTES)
    except (safe.UnsafeError, OSError) as e:
        log(f"not using {CONFIG}: {e}")
        return {}, False
    if blob is None:
        return (migrate_legacy() if migrate else {}), True
    return _parse_assignments(blob, CONFIG)


def save_config(assignments):
    """Atomic, no-follow replace. Raises safe.UnsafeError / OSError."""
    safe.write_json(CONFIG, assignments, indent=2, sort_keys=True)


def assign(assignments, cls, layout):
    """Set cls -> layout in place: "changed", "unchanged", or "full" when cls is a new app
    and MAX_APPS are already remembered (nothing is changed then)."""
    if assignments.get(cls) == layout:
        return "unchanged"
    if cls not in assignments and len(assignments) >= MAX_APPS:
        return "full"
    assignments[cls] = layout
    return "changed"


def migrate_legacy():
    """One-time move from the old state path, only while the config does not exist yet."""
    try:
        blob = safe.read_file(LEGACY, CONFIG_MAX_BYTES)
    except (safe.UnsafeError, OSError) as e:
        log(f"not migrating {LEGACY}: {e}")
        return {}
    if blob is None:
        return {}
    data, ok = _parse_assignments(blob, LEGACY)
    if not ok:
        return {}
    data = dict(sorted(data.items())[:MAX_APPS])
    try:
        save_config(data)
        safe.remove_file(LEGACY)
    except (safe.UnsafeError, OSError) as e:
        log(f"migrating {LEGACY} failed: {e}")
    return data


# ------------------------------------------------------------------ menus and notifications

def menu(prompt, options):
    """The picked row from omarchy-menu-select, or "" when nothing was picked. "--" would be
    read as the end of the options, so it is never passed as one."""
    rows = [o for o in options if o != "--"][:MAX_MENU_ROWS]
    blob = output_of(["omarchy-menu-select", prompt, *rows], timeout=MENU_TIMEOUT, max_output=MENU_MAX_OUTPUT)
    return "" if blob is None else blob.decode("utf-8", "replace").strip()


def notify(message):
    try:
        safe.spawn(["omarchy-notification-send", "Keyboard layout", message], timeout=NOTIFY_TIMEOUT)
    except (safe.UnsafeError, OSError) as e:
        log(f"notification failed: {e}")


# ------------------------------------------------------------------ installed applications

def read_package_file(path, max_bytes):
    """Bytes of a desktop entry shipped by a package, or None. Such entries are often
    symlinks into /usr/lib (LibreOffice's are), so the link is resolved -- but the real file
    must sit under /usr, be a root-owned regular file that is not group/other-writable, and
    fit in max_bytes. Opened no-follow at its real path."""
    real = os.path.realpath(path)
    if not real.startswith("/usr/"):
        return None
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022 or st.st_size > max_bytes:
            return None
        data = os.read(fd, max_bytes + 1)
        return data if len(data) <= max_bytes else None
    except OSError:
        return None
    finally:
        os.close(fd)


def desktop_entry(blob, stem):
    """(window class, display name) for a launchable application entry, else None.

    StartupWMClass is the app's own declaration of the class it will use, which is exactly
    what the daemon keys on. Where it is absent, the desktop-file id is the best guess.
    Hidden/NoDisplay entries are skipped: they are not apps the user launches."""
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(blob.decode("utf-8"))
        entry = parser["Desktop Entry"]
    except (configparser.Error, KeyError, ValueError, UnicodeDecodeError):
        return None
    if entry.get("NoDisplay", "").lower() == "true" or entry.get("Hidden", "").lower() == "true":
        return None
    if entry.get("Type", "Application") != "Application":
        return None
    cls = (entry.get("StartupWMClass") or stem).strip()
    # Some desktop files ship an unsubstituted placeholder (Chromium's
    # "@@startup_wm_class"); others are wrappers whose stem is not a window class.
    if not cls or cls.startswith("@") or "/" in cls or not clean_name(cls):
        return None
    return cls, clean_name((entry.get("Name") or "").strip()) or cls


def _system_desktop_files(directory):
    try:
        with os.scandir(directory) as it:
            names = []
            for item in it:
                if len(names) >= MAX_DESKTOP_FILES:
                    break
                if item.name.endswith(".desktop"):
                    names.append(item.name)
    except OSError:
        return
    for name in sorted(names):
        yield name, read_package_file(os.path.join(directory, name), DESKTOP_MAX_BYTES)


def _user_desktop_files(directory):
    try:
        entries = safe.list_dir(directory, MAX_DESKTOP_FILES)
    except (safe.UnsafeError, OSError):
        return
    for name, st in entries:
        if not name.endswith(".desktop") or not stat.S_ISREG(st.st_mode):
            continue
        try:
            yield name, safe.read_file(os.path.join(directory, name), DESKTOP_MAX_BYTES)
        except (safe.UnsafeError, OSError):
            continue


def installed_classes():
    """{window class: app name} for installed apps, so an app can be assigned before it is
    opened -- including tray-only apps that never show a window."""
    out = {}
    sources = [_system_desktop_files(d) for d in SYSTEM_APP_DIRS] + [_user_desktop_files(USER_APP_DIR)]
    for source in sources:
        for name, blob in source:
            found = desktop_entry(blob, name[:-len(".desktop")]) if blob else None
            if found:
                out.setdefault(*found)
    return out
