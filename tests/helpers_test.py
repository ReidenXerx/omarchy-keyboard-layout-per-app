#!/usr/bin/python3
"""python3 tests/helpers_test.py -- the helpers' limits, file handling, process handling and
event logic.

Files live in a sandbox under $XDG_RUNTIME_DIR with the module's path constants pointed at
it. hyprctl, menus and notifications are replaced by a recorder: no test switches a real
layout, opens a menu or touches the real assignments file. The only real programs run are
sleep/yes (process bounds), node (model equivalence) and read-only `xkbcli list`."""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import plugin_safety as safe  # noqa: E402
import kb_layout as kb  # noqa: E402

PLUGIN_SAFETY_SHA256 = "bf8ffb9ff874caa1958526c1d0a9bd239a55842c979d41cd916675075cf8ec87"
NODE = shutil.which("node")


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


daemon = load_script("kb-layout-daemon")
assign_cli = load_script("kb-layout-assign")


class Recorder:
    """Stands in for kb.output_of: records argv, runs nothing, answers from a table."""

    def __init__(self):
        self.calls = []
        self.responses = {}

    def __call__(self, argv, *, timeout, max_output):
        self.calls.append((list(argv), timeout, max_output))
        return self.responses.get(tuple(argv[:3]), b"")


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="kb-layout-test-", dir=safe.runtime_dir()))
        os.chmod(self.root, 0o700)
        self.saved = {name: getattr(kb, name) for name in
                      ("CONFIG", "LEGACY", "USER_APP_DIR", "SYSTEM_APP_DIRS", "output_of", "notify",
                       "menu", "running_classes", "installed_classes")}
        self.saved_layout_names = assign_cli.layout_names
        self.saved_signals = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
        kb.CONFIG = str(self.root / "config" / "kb-layout-per-app.json")
        kb.LEGACY = str(self.root / "state" / "kb-layout-per-app.json")
        kb.USER_APP_DIR = str(self.root / "applications")
        kb.SYSTEM_APP_DIRS = ()
        self.hypr = Recorder()
        kb.output_of = self.hypr
        self.notes = []
        kb.notify = self.notes.append
        kb._last_log[0] = None

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(kb, name, value)
        assign_cli.layout_names = self.saved_layout_names
        for sig, handler in self.saved_signals.items():
            signal.signal(sig, handler)
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, path, text):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(text)
        return path

    def quiet(self, fn, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(*args)
        return rc, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ event framing

class Framing(unittest.TestCase):
    def test_lines_split_across_chunks(self):
        r = daemon.LineReader()
        self.assertEqual(r.feed(b"activewindow>>fo"), [])
        self.assertEqual(r.feed(b"ot,title\nactivelayout>>kb,US\n"),
                         [b"activewindow>>foot,title", b"activelayout>>kb,US"])

    def test_oversized_frame_dropped_next_kept(self):
        r = daemon.LineReader(max_line=16, max_buffer=64)
        self.assertEqual(r.feed(b"x" * 40 + b"\nok\n"), [b"ok"])
        self.assertEqual(r.dropped, 1)

    def test_oversized_frame_across_chunks_dropped_whole(self):
        r = daemon.LineReader(max_line=16, max_buffer=64)
        for chunk in (b"y" * 10, b"y" * 10, b"y" * 30):
            self.assertEqual(r.feed(chunk), [])
            self.assertLessEqual(len(r.buf), 16)
        self.assertEqual(r.feed(b"tail-of-it\nnext\n"), [b"next"])
        self.assertEqual(r.dropped, 1)

    def test_memory_stays_bounded_without_newlines(self):
        r = daemon.LineReader()
        for _ in range(2000):
            r.feed(b"z" * daemon.RECV_SIZE)
            self.assertLessEqual(len(r.buf), daemon.MAX_EVENT_LINE)
        self.assertEqual(r.feed(b"\nactivewindow>>foot,\n"), [b"activewindow>>foot,"])

    def test_buffer_overflow_resets(self):
        r = daemon.LineReader(max_line=16, max_buffer=64)
        self.assertEqual(r.feed(b"a\n" * 40), [])
        self.assertEqual(r.dropped, 1)
        self.assertEqual(r.feed(b"rest\nok\n"), [b"ok"])

    def test_parse_events(self):
        p = daemon.parse_event
        self.assertEqual(p(b"activewindow>>org.telegram.desktop,Chat, with comma"), ("window", "org.telegram.desktop"))
        self.assertEqual(p(b"activewindow>>,"), ("window", ""))
        self.assertEqual(p(b"activelayout>>at-translated-set-2-keyboard,English (US, intl., with dead keys)"),
                         ("layout", "English (US, intl., with dead keys)"))
        self.assertIsNone(p(b"activelayout>>no-comma"))
        self.assertIsNone(p(b"activewindowv2>>0x1234"))
        self.assertIsNone(p(b"workspace>>2"))
        self.assertEqual(p(b"activewindow>>\xff,x"), ("window", "�"))


# ------------------------------------------------------------------ layout memory

class FakeSeat:
    def __init__(self, assignments=None, writable=True):
        self.assignments = dict(assignments or {})
        self.writable = writable
        self.layout = "English (US)"
        self.saved, self.applied = [], []

    def load(self):
        return dict(self.assignments), self.writable

    def save(self, assignments):
        self.saved.append(dict(assignments))
        self.assignments = dict(assignments)

    def current(self):
        return self.layout

    def apply_index(self, index):
        self.applied.append(("index", index))
        self.layout = "English (US)"

    def apply_name(self, name):
        self.applied.append(("name", name))
        self.layout = name

    def memory(self):
        return daemon.LayoutMemory(self.load, self.save, self.current, self.apply_index, self.apply_name)


class Memory(unittest.TestCase):
    def setUp(self):
        kb._last_log[0] = None
        self.err = contextlib.redirect_stderr(io.StringIO())
        self.err.__enter__()

    def tearDown(self):
        self.err.__exit__(None, None, None)

    def test_restores_assignment_and_ignores_its_echo(self):
        seat = FakeSeat({"telegram": "Ukrainian"})
        m = seat.memory()
        m.window("telegram")
        self.assertEqual(seat.applied, [("name", "Ukrainian")])
        for _ in range(10):                 # one echo per keyboard device
            m.layout("Ukrainian")
        self.assertEqual(seat.saved, [])

    def test_records_user_choice_once(self):
        seat = FakeSeat()
        m = seat.memory()
        m.window("foot")
        self.assertEqual(seat.applied, [("index", 0)])
        m.layout("English (US)")            # echo of the default
        seat.layout = "Ukrainian"
        m.layout("Ukrainian")
        m.layout("Ukrainian")
        self.assertEqual(seat.saved, [{"foot": "Ukrainian"}])

    def test_refocusing_the_same_class_does_nothing(self):
        seat = FakeSeat()
        m = seat.memory()
        m.window("foot")
        m.window("foot")
        m.window("")
        self.assertEqual(seat.applied, [("index", 0)])
        self.assertEqual(m.cls, "foot")

    def test_unusable_class_gets_default_and_records_nothing(self):
        for bad in ("x" * (kb.MAX_NAME + 1), "bad\tclass", "esc\x1b[31m"):
            seat = FakeSeat({"telegram": "Ukrainian"})
            m = seat.memory()
            m.window("telegram")
            m.window(bad)
            self.assertIsNone(m.cls)
            self.assertEqual(seat.applied[-1], ("index", 0))
            seat.layout = "Russian"
            m.layout("Russian")
            self.assertEqual(seat.saved, [], bad)

    def test_unusable_layout_is_not_recorded(self):
        seat = FakeSeat()
        m = seat.memory()
        m.window("foot")
        m.layout("L" * (kb.MAX_NAME + 1))
        m.layout("Ukr\x1b[31m")
        self.assertEqual(seat.saved, [])

    def test_cap_refuses_new_apps_but_updates_existing(self):
        full = {f"app{i}": "Ukrainian" for i in range(kb.MAX_APPS)}
        seat = FakeSeat(full)
        m = seat.memory()
        m.window("newapp")
        seat.layout = "Russian"
        m.layout("Russian")
        self.assertEqual(seat.saved, [])
        m.window("app1")
        seat.layout = "Russian"
        m.layout("Russian")
        self.assertEqual(len(seat.saved), 1)
        self.assertEqual(seat.saved[0]["app1"], "Russian")
        self.assertEqual(len(seat.saved[0]), kb.MAX_APPS)

    def test_unusable_file_is_never_written(self):
        seat = FakeSeat(writable=False)
        m = seat.memory()
        m.window("foot")
        seat.layout = "Ukrainian"
        m.layout("Ukrainian")
        self.assertEqual(seat.saved, [])

    def test_assign_results(self):
        d = {"a": "x"}
        self.assertEqual(kb.assign(d, "a", "x"), "unchanged")
        self.assertEqual(kb.assign(d, "a", "y"), "changed")
        full = {str(i): "x" for i in range(kb.MAX_APPS)}
        self.assertEqual(kb.assign(full, "new", "x"), "full")
        self.assertNotIn("new", full)


# ------------------------------------------------------------------ assignments file

class Config(Sandbox):
    def load(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return kb.load_config()

    def test_roundtrip_is_atomic_and_private(self):
        kb.save_config({"foot": "Ukrainian", "Alacritty": "English (US)"})
        self.assertEqual(self.load(), ({"foot": "Ukrainian", "Alacritty": "English (US)"}, True))
        text = pathlib.Path(kb.CONFIG).read_text()
        self.assertEqual(json.loads(text), {"Alacritty": "English (US)", "foot": "Ukrainian"})
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(os.stat(kb.CONFIG).st_mode & 0o777, 0o600)
        kb.save_config({"foot": "Russian"})
        self.assertEqual(sorted(os.listdir(pathlib.Path(kb.CONFIG).parent)), ["kb-layout-per-app.json"])

    def test_predictable_tmp_name_is_not_used(self):
        victim = self.write(self.root / "victim", "keep")
        tmp = pathlib.Path(kb.CONFIG).with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.symlink_to(victim)
        kb.save_config({"foot": "Ukrainian"})
        self.assertEqual(victim.read_text(), "keep")

    def test_symlinked_config_is_neither_read_nor_replaced(self):
        victim = self.write(self.root / "victim.json", '{"a": "b"}')
        pathlib.Path(kb.CONFIG).parent.mkdir(parents=True)
        os.symlink(victim, kb.CONFIG)
        self.assertEqual(self.load(), ({}, False))
        with self.assertRaises(safe.UnsafeError):
            kb.save_config({"foot": "Ukrainian"})
        m = daemon.LayoutMemory(current=lambda: "English (US)", apply_index=lambda i: None, apply_name=lambda n: None)
        with contextlib.redirect_stderr(io.StringIO()):
            m.window("foot")
            m.layout("Ukrainian")
        self.assertEqual(victim.read_text(), '{"a": "b"}')
        self.assertTrue(os.path.islink(kb.CONFIG))

    def test_symlinked_parent_directory_refused(self):
        real = self.root / "real"
        real.mkdir()
        os.symlink(real, self.root / "config")
        self.assertEqual(self.load(), ({}, False))
        with self.assertRaises(safe.UnsafeError):
            kb.save_config({"foot": "Ukrainian"})
        self.assertEqual(os.listdir(real), [])

    def test_oversized_config_refused_and_left_alone(self):
        big = json.dumps({f"app{i}": "x" * 200 for i in range(1000)})
        self.assertGreater(len(big), kb.CONFIG_MAX_BYTES)
        self.write(kb.CONFIG, big)
        self.assertEqual(self.load(), ({}, False))

    def test_fifo_does_not_hang(self):
        pathlib.Path(kb.CONFIG).parent.mkdir(parents=True)
        os.mkfifo(kb.CONFIG)
        start = time.monotonic()
        self.assertEqual(self.load(), ({}, False))
        self.assertLess(time.monotonic() - start, 1)

    def test_broken_hand_edit_is_left_for_its_author(self):
        self.write(kb.CONFIG, '{"foot": ')
        self.assertEqual(self.load(), ({}, False))
        m = daemon.LayoutMemory(current=lambda: "English (US)", apply_index=lambda i: None, apply_name=lambda n: None)
        with contextlib.redirect_stderr(io.StringIO()):
            m.window("foot")
            m.layout("Ukrainian")
        self.assertEqual(pathlib.Path(kb.CONFIG).read_text(), '{"foot": ')

    def test_empty_file_is_an_empty_table(self):
        self.write(kb.CONFIG, "  \n")
        self.assertEqual(self.load(), ({}, True))

    def test_non_object_refused(self):
        self.write(kb.CONFIG, '["foot", "Ukrainian"]')
        self.assertEqual(self.load(), ({}, False))

    def test_too_many_entries_refused(self):
        self.write(kb.CONFIG, json.dumps({str(i): "x" for i in range(4 * kb.MAX_APPS + 1)}))
        self.assertEqual(self.load(), ({}, False))

    def test_malformed_entries_skipped(self):
        self.write(kb.CONFIG, json.dumps({"ok": "Ukrainian", "bad\n": "x", "n": 5, "long": "L" * 300, "": "x"}))
        self.assertEqual(self.load(), ({"ok": "Ukrainian"}, True))

    def test_legacy_file_migrates_once(self):
        self.write(kb.LEGACY, '{"foot": "Ukrainian"}')
        self.assertEqual(self.load(), ({"foot": "Ukrainian"}, True))
        self.assertFalse(os.path.exists(kb.LEGACY))
        self.assertEqual(json.loads(pathlib.Path(kb.CONFIG).read_text()), {"foot": "Ukrainian"})

    def test_legacy_ignored_once_config_exists(self):
        self.write(kb.CONFIG, "{}")
        self.write(kb.LEGACY, '{"foot": "Ukrainian"}')
        self.assertEqual(self.load(), ({}, True))
        self.assertTrue(os.path.exists(kb.LEGACY))

    def test_symlinked_legacy_not_followed(self):
        victim = self.write(self.root / "victim.json", '{"foot": "Ukrainian"}')
        pathlib.Path(kb.LEGACY).parent.mkdir(parents=True)
        os.symlink(victim, kb.LEGACY)
        self.assertEqual(self.load(), ({}, True))
        self.assertTrue(victim.exists())
        self.assertFalse(os.path.exists(kb.CONFIG))


# ------------------------------------------------------------------ the assign CLI

class AssignCli(Sandbox):
    def cli(self, *args):
        return self.quiet(assign_cli.main, ["kb-layout-assign", *args])

    def test_set_list_clear(self):
        self.assertEqual(self.cli("set", "foot", "English", "(US)")[0], 0)
        self.assertEqual(json.loads(pathlib.Path(kb.CONFIG).read_text()), {"foot": "English (US)"})
        rc, out, _ = self.cli("list")
        self.assertEqual(rc, 0)
        self.assertIn("foot", out)
        self.assertEqual(self.cli("clear", "foot")[0], 0)
        self.assertEqual(json.loads(pathlib.Path(kb.CONFIG).read_text()), {})
        self.assertIn("had no assignment", self.cli("clear", "foot")[1])

    def test_bad_arguments_do_not_fall_into_the_menu(self):
        for args in (("set", "foot"), ("set", "a\tb", "US"), ("set", "x" * 300, "US"), ("bogus",), ("cycle",), ("cycle", "-j")):
            self.assertEqual(self.cli(*args)[0], 2, args)
        self.assertEqual(self.hypr.calls, [])

    def test_set_refuses_past_the_cap(self):
        kb.save_config({f"app{i}": "x" for i in range(kb.MAX_APPS)})
        rc, _, err = self.cli("set", "newapp", "Ukrainian")
        self.assertEqual(rc, 1)
        self.assertIn(str(kb.MAX_APPS), err)
        self.assertEqual(self.cli("set", "app3", "Ukrainian")[0], 0)

    def test_set_refuses_to_overwrite_a_broken_file(self):
        self.write(kb.CONFIG, "{oops")
        self.assertEqual(self.cli("set", "foot", "Ukrainian")[0], 1)
        self.assertEqual(pathlib.Path(kb.CONFIG).read_text(), "{oops")

    def test_devices_prints_nothing_when_hyprctl_fails(self):
        self.hypr.responses[("hyprctl", "-j", "devices")] = None
        rc, out, _ = self.quiet(assign_cli.cmd_devices, [])
        self.assertEqual((rc, out), (1, ""))
        self.hypr.responses[("hyprctl", "-j", "devices")] = b'{"mice": []}'
        self.assertEqual(self.quiet(assign_cli.cmd_devices, [])[:2], (1, ""))

    def test_devices_is_bounded_and_reduced(self):
        good = {"address": "0x1", "name": "at-translated-set-2-keyboard", "rules": "", "model": "",
                "layout": "us,ua,ru", "variant": "", "options": "grp:alt_shift_toggle",
                "active_layout_index": 1, "active_keymap": "Ukrainian", "capsLock": False, "main": True}
        odd = {"name": "evil\tname", "layout": 5, "active_layout_index": True, "active_keymap": "x" * 300}
        seat = {"keyboards": [good, odd] + [dict(good, name=f"kbd-{i}") for i in range(100)]}
        self.hypr.responses[("hyprctl", "-j", "devices")] = json.dumps(seat).encode()
        rc, out, _ = self.quiet(assign_cli.cmd_devices, [])
        self.assertEqual(rc, 0)
        listed = json.loads(out)["keyboards"]
        self.assertEqual(len(listed), kb.MAX_KEYBOARDS)
        self.assertEqual(listed[0], {"name": "at-translated-set-2-keyboard", "layout": "us,ua,ru",
                                     "active_keymap": "Ukrainian", "active_layout_index": 1})
        self.assertEqual(listed[1], {})
        self.assertLess(len(out), 64 * 1024)

    def test_devices_empty_seat_is_reported(self):
        self.hypr.responses[("hyprctl", "-j", "devices")] = b'{"keyboards": []}'
        self.assertEqual(self.quiet(assign_cli.cmd_devices, [])[:2], (0, '{"keyboards": []}\n'))

    def test_cycle_switches_only_the_named_keyboard(self):
        self.hypr.responses[("hyprctl", "switchxkblayout", "at-translated-set-2-keyboard")] = b"ok"
        self.assertEqual(assign_cli.cmd_cycle(["at-translated-set-2-keyboard"]), 0)
        self.assertEqual(self.hypr.calls[0][0], ["hyprctl", "switchxkblayout", "at-translated-set-2-keyboard", "next"])
        self.assertLessEqual(self.hypr.calls[0][2], 4096)

    def test_layouts_prints_the_filtered_listing(self):
        listing = "layouts:\n- layout: 'us'\n  variant: ''\n  brief: 'en'\n  description: English (US)\n"
        self.hypr.responses[("xkbcli", "list", "--load-exotic")] = listing.encode()
        rc, out, _ = self.quiet(assign_cli.cmd_layouts, [])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "- \n  brief: 'en'\n  description: English (US)\n")
        self.assertEqual(self.hypr.calls[0][1:], (kb.XKBCLI_TIMEOUT, kb.XKBCLI_MAX_OUTPUT))

    def fake_menus(self, *picks):
        prompts, queue = [], list(picks)

        def menu(prompt, rows):
            prompts.append((prompt, list(rows)))
            return queue.pop(0)
        kb.menu = menu
        return prompts

    def test_picker_assigns_then_resets_to_default(self):
        kb.running_classes = lambda: {"foot"}
        kb.installed_classes = lambda: {"org.telegram.desktop": "Telegram"}
        assign_cli.layout_names = lambda: ["English (US)", "Ukrainian"]
        prompts = self.fake_menus("foot\trunning", "Ukrainian")
        self.assertEqual(self.cli()[0], 0)
        self.assertEqual(json.loads(pathlib.Path(kb.CONFIG).read_text()), {"foot": "Ukrainian"})
        self.assertEqual(prompts[0][1], ["󰖯\tfoot\trunning", "󰀻\torg.telegram.desktop\tTelegram"])
        self.assertEqual(prompts[1], ("Layout for foot", ["English (US)", "Ukrainian", "Default (US)"]))

        prompts = self.fake_menus("foot\tUkrainian", "Default (US)")
        self.assertEqual(self.cli("ui")[0], 0)
        self.assertEqual(prompts[0][1][0], "󰌌\tfoot\tUkrainian")
        self.assertEqual(json.loads(pathlib.Path(kb.CONFIG).read_text()), {})

    def test_picker_ignores_a_choice_it_did_not_offer(self):
        kb.running_classes = lambda: {"foot"}
        kb.installed_classes = lambda: {}
        assign_cli.layout_names = lambda: ["English (US)", "Ukrainian"]
        self.fake_menus("foot\trunning", "Klingon")
        self.assertEqual(self.cli()[0], 0)
        self.assertFalse(os.path.exists(kb.CONFIG))

    def test_clear_ui(self):
        kb.save_config({"foot": "Ukrainian", "mpv": "Russian"})
        prompts = self.fake_menus("foot\tUkrainian")
        self.assertEqual(self.cli("clear-ui")[0], 0)
        self.assertEqual(prompts[0], ("Clear layout for app", ["󰅖\tfoot\tUkrainian", "󰅖\tmpv\tRussian"]))
        self.assertEqual(json.loads(pathlib.Path(kb.CONFIG).read_text()), {"mpv": "Russian"})
        self.assertEqual(self.notes, ["foot back to the default"])
        kb.save_config({})
        self.assertEqual(self.cli("clear-ui")[0], 0)
        self.assertEqual(self.notes[-1], "No assignments to clear")

    def test_layout_names_cycles_the_first_keyboard_and_returns_to_zero(self):
        seat = {"keyboards": [{"name": "power-button", "layout": "us"},
                              {"name": "kbd", "layout": "us,ua,ru", "active_keymap": "English (US)"}]}
        self.hypr.responses[("hyprctl", "-j", "devices")] = json.dumps(seat).encode()
        self.hypr.responses[("hyprctl", "switchxkblayout", "kbd")] = b"ok"
        self.assertEqual(self.saved_layout_names(), ["English (US)"] * 3)
        switches = [c[0][2:] for c in self.hypr.calls if c[0][1] == "switchxkblayout"]
        self.assertEqual(switches, [["kbd", "0"], ["kbd", "1"], ["kbd", "2"], ["kbd", "0"]])

    def test_menu_rows_and_limits(self):
        self.hypr.responses[("omarchy-menu-select", "Pick", "a")] = b"a\n"
        self.assertEqual(kb.menu("Pick", ["a", "--", "b"]), "a")
        argv, timeout, cap = self.hypr.calls[0]
        self.assertEqual(argv, ["omarchy-menu-select", "Pick", "a", "b"])
        self.assertEqual((timeout, cap), (600, 64 * 1024))
        kb.menu("Pick", [f"row{i}" for i in range(kb.MAX_MENU_ROWS + 50)])
        self.assertEqual(len(self.hypr.calls[1][0]), 2 + kb.MAX_MENU_ROWS)


# ------------------------------------------------------------------ names and argv

class Names(Sandbox):
    def test_clean_name(self):
        for ok in ("English (US)", "org.telegram.desktop", "Українська", "x" * kb.MAX_NAME):
            self.assertEqual(kb.clean_name(ok), ok)
        for bad in ("", " x", "x ", "a\nb", "a\tb", "\x1b[2J", "x" * (kb.MAX_NAME + 1), 5, None, ["x"]):
            self.assertIsNone(kb.clean_name(bad), repr(bad))

    def test_device_name(self):
        for ok in ("at-translated-set-2-keyboard", "logitech-usb-receiver-1", "hl-virtual-keyboard"):
            self.assertEqual(kb.device_name(ok), ok)
        for bad in ("-j", "--batch", "a b", "a;b", "", "x" * 129, 5, None, "a b", "kb\x00"):
            self.assertIsNone(kb.device_name(bad), repr(bad))

    def test_switch_layout_refuses_bad_input_without_running_anything(self):
        for device, target in (("-r", "next"), ("kb", "1; reload"), ("kb", "-1"), ("kb", "999"), ("kb", "next next")):
            self.assertFalse(kb.switch_layout(device, target))
        self.assertEqual(self.hypr.calls, [])
        kb.switch_layout("kb", 0)
        self.assertEqual(self.hypr.calls[0][0], ["hyprctl", "switchxkblayout", "kb", "0"])

    def test_switchable_keyboards_skips_unaddressable_devices(self):
        seat = [{"name": "kb", "layout": "us,ua"}, {"name": "-x", "layout": "us,ua"},
                {"name": "single", "layout": "us"}, {"name": "nolayout"}]
        self.assertEqual([k["name"] for k in kb.switchable_keyboards(seat)], ["kb"])

    def test_hyprctl_output_limits(self):
        self.hypr.responses[("hyprctl", "-j", "devices")] = b"[" * 100 + b"]" * 100
        self.assertIsNone(kb.seat_keyboards())
        argv, timeout, cap = self.hypr.calls[0]
        self.assertEqual((timeout, cap), (kb.HYPRCTL_TIMEOUT, 256 * 1024))


# ------------------------------------------------------------------ xkb listing

XKB_FIXTURE = "\n".join([
    "models:", "- name: pc105", "  vendor: Generic", "  description: Generic 105-key PC",
    "layouts:",
    "- layout: 'us'", "  variant: ''", "  brief: 'en'", "  description: English (US)", "  iso639: ['eng']",
    "- layout: 'us'", "  variant: 'intl'", "  brief: 'en'", "  description: English (US, intl., with dead keys)",
    "- layout: 'mm'", "  variant: 'zawgyi'", "  brief: 'my-zwg'", "  description: Burmese (Zawgyi)",
    "- layout: 'us'", "  brief: 'en'", "- layout: 'gr'", "  description: Greek",
    "option_groups:", "- name: 'grp'", "  description: Switching to another layout", "  options:",
    "  - name: 'grp:switch'", "    brief: ''", "    description: 'Right Alt (while pressed)'",
])


def js_briefs(*texts):
    code = ("const m = require(process.argv[1]); const fs = require('fs');"
            "const texts = JSON.parse(fs.readFileSync(0, 'utf8'));"
            "process.stdout.write(JSON.stringify(texts.map(t => m.layoutBriefs(t))))")
    out = subprocess.run([NODE, "-e", code, str(ROOT / "KeyboardLayoutModel.js")],
                         input=json.dumps(texts), capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


class Listing(unittest.TestCase):
    def test_keeps_only_what_the_model_reads(self):
        out = kb.layout_listing(XKB_FIXTURE)
        for line in out.split("\n"):
            self.assertTrue(line == "- " or re.fullmatch(r"  (brief|description): .*", line), line)
        self.assertNotIn("- \n- ", out)
        self.assertLess(len(out), len(XKB_FIXTURE))

    @unittest.skipUnless(NODE, "node not installed")
    def test_model_reads_the_same_table(self):
        raw, filtered = js_briefs(XKB_FIXTURE, kb.layout_listing(XKB_FIXTURE))
        self.assertEqual(raw, filtered)
        self.assertEqual(raw["English (US)"], "en")
        self.assertNotIn("Greek", raw)

    @unittest.skipUnless(NODE and os.path.exists("/usr/bin/xkbcli"), "node or xkbcli missing")
    def test_model_reads_the_same_table_from_the_real_listing(self):
        listing = subprocess.run(["/usr/bin/xkbcli", "list", "--load-exotic"], capture_output=True,
                                 text=True, timeout=30).stdout
        raw, filtered = js_briefs(listing, kb.layout_listing(listing))
        self.assertGreater(len(raw), 100)
        self.assertEqual(raw, filtered)

    @unittest.skipUnless(NODE, "node not installed")
    def test_dropped_lines_never_move_a_brief_to_another_block(self):
        text = "\n".join([
            "- layout: 'a'", "  brief: 'aa'", "  description: " + "A" * 600,
            "- layout: 'b'", "  description: Bee",
            "- layout: 'c'", "  brief: 'c\x07'", "  description: Cee",
        ])
        (briefs,) = js_briefs(kb.layout_listing(text))
        self.assertEqual(briefs, {})

    def test_output_is_capped(self):
        block = "- layout: 'x'\n  brief: 'xx'\n  description: Layout {}\n"
        huge = "".join(block.format(i) for i in range(20000))
        out = kb.layout_listing(huge)
        self.assertLessEqual(len(out.encode()), kb.LAYOUTS_MAX_OUTPUT)


# ------------------------------------------------------------------ processes (real, bounded)

def pids_matching(pattern):
    r = subprocess.run(["/usr/bin/pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
    return r.stdout.split()


class Processes(unittest.TestCase):
    def test_deadline(self):
        start = time.monotonic()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(kb.output_of(["sleep", "30"], timeout=1, max_output=100))
        self.assertLess(time.monotonic() - start, 6)

    def test_output_ceiling(self):
        self.assertIsNone(kb.output_of(["yes"], timeout=5, max_output=4096))

    def test_untrusted_or_missing_tool(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(kb.output_of(["definitely-not-a-tool-xyz"], timeout=1, max_output=10))
            self.assertIsNone(kb.output_of(["../bin/sh"], timeout=1, max_output=10))

    def spawn_helper(self, marker, timeout):
        code = (f"import sys; sys.dont_write_bytecode = True; sys.path.insert(0, {str(BIN)!r});"
                "import kb_layout as kb; kb.exit_cleanly_on_signals(); print('ready', flush=True);"
                f"kb.output_of(['sleep', '{marker}'], timeout={timeout}, max_output=100)")
        proc = subprocess.Popen(["/usr/bin/python3", "-c", code], stdout=subprocess.PIPE)
        self.assertEqual(proc.stdout.readline().strip(), b"ready")
        deadline = time.monotonic() + 5
        while len(pids_matching(f"sleep {marker}$")) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(pids_matching(f"sleep {marker}$")), 2, "timeout + sleep should be running")
        return proc

    def wait_gone(self, marker, within):
        deadline = time.monotonic() + within
        while pids_matching(f"sleep {marker}$") and time.monotonic() < deadline:
            time.sleep(0.05)
        return pids_matching(f"sleep {marker}$")

    def test_sigterm_kills_what_the_helper_started(self):
        marker = f"{100 + random.random():.6f}"
        proc = self.spawn_helper(marker, timeout=60)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=5), 128 + signal.SIGTERM)
        proc.stdout.close()
        self.assertEqual(self.wait_gone(marker, 2), [])

    def test_sigkilled_helper_still_leaves_nothing_past_the_deadline(self):
        marker = f"{200 + random.random():.6f}"
        proc = self.spawn_helper(marker, timeout=1)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)
        proc.stdout.close()
        self.assertEqual(self.wait_gone(marker, 5), [])


# ------------------------------------------------------------------ installed applications

class Desktop(Sandbox):
    def test_user_entries_are_read_safely(self):
        apps = self.root / "applications"
        self.write(apps / "app.desktop", "[Desktop Entry]\nType=Application\nName=Foo App\nStartupWMClass=Foo\n")
        self.write(apps / "plain.desktop", "[Desktop Entry]\nName=Plain\n")
        self.write(apps / "hidden.desktop", "[Desktop Entry]\nNoDisplay=true\nStartupWMClass=Hidden\n")
        self.write(apps / "chromium.desktop", "[Desktop Entry]\nStartupWMClass=@@startup_wm_class\n")
        self.write(apps / "tab.desktop", "[Desktop Entry]\nStartupWMClass=bad\tclass\n")
        self.write(apps / "big.desktop", "[Desktop Entry]\nStartupWMClass=Big\nX=" + "x" * kb.DESKTOP_MAX_BYTES + "\n")
        outside = self.write(self.root / "evil.desktop", "[Desktop Entry]\nStartupWMClass=Evil\n")
        os.symlink(outside, apps / "link.desktop")
        os.mkfifo(apps / "fifo.desktop")
        self.assertEqual(kb.installed_classes(), {"Foo": "Foo App", "plain": "Plain"})

    def test_package_reads_refuse_user_files(self):
        own = self.write(self.root / "x.desktop", "[Desktop Entry]\n")
        self.assertIsNone(kb.read_package_file(own, 1024))
        self.assertIsNone(kb.read_package_file("/etc/passwd", 1 << 20))

    @unittest.skipUnless(os.path.isdir("/usr/share/applications"), "no system applications")
    def test_system_entries_parse(self):
        kb.SYSTEM_APP_DIRS = ("/usr/share/applications",)
        found = kb.installed_classes()
        self.assertTrue(all(kb.clean_name(c) and kb.clean_name(n) for c, n in found.items()))


# ------------------------------------------------------------------ the event socket

class EventSocket(Sandbox):
    def setUp(self):
        super().setUp()
        self.saved_runtime = safe.runtime_dir
        self.saved_env = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        root = str(self.root)
        safe.runtime_dir = lambda: root
        self.server = None

    def tearDown(self):
        safe.runtime_dir = self.saved_runtime
        if self.saved_env is None:
            os.environ.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        else:
            os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = self.saved_env
        if self.server:
            self.server.close()
        super().tearDown()

    def listen(self, signature):
        directory = self.root / "hypr" / signature
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(self.root / "hypr", 0o700)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(directory / ".socket2.sock"))
        self.server.listen(1)
        return directory

    def test_connects_and_reads(self):
        self.listen("abc_123")
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = "abc_123"
        client = daemon.connect_events()
        conn, _ = self.server.accept()
        conn.sendall(b"activewindow>>foot,x\n")
        self.assertEqual(daemon.LineReader().feed(client.recv(4096)), [b"activewindow>>foot,x"])
        conn.close()
        client.close()

    def test_signature_fallback_picks_a_real_socket(self):
        self.listen("real")
        (self.root / "hypr" / "empty").mkdir()
        os.environ.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        self.assertEqual(daemon.find_signature(), "real")

    def test_refuses_symlinks_and_bad_signatures(self):
        directory = self.listen("real")
        (self.root / "hypr" / "linked").mkdir()
        os.symlink(directory / ".socket2.sock", self.root / "hypr" / "linked" / ".socket2.sock")
        os.symlink(directory, self.root / "hypr" / "aliased")
        with self.assertRaises(safe.UnsafeError):
            daemon.event_socket_fd("linked")
        with self.assertRaises((safe.UnsafeError, OSError)):
            daemon.event_socket_fd("aliased")
        for bad in ("..", "../real", "a/b", ""):
            with self.assertRaises(safe.UnsafeError):
                daemon.event_socket_fd(bad)

    def test_refuses_loose_directories(self):
        self.listen("real")
        os.chmod(self.root / "hypr", 0o777)
        with self.assertRaises(safe.UnsafeError):
            daemon.event_socket_fd("real")


# ------------------------------------------------------------------ static rules

class Static(unittest.TestCase):
    SCRIPTS = ("kb-layout-daemon", "kb-layout-assign", "kb-layout-menu-install")

    def test_shebangs_are_absolute(self):
        for name in self.SCRIPTS:
            with open(BIN / name, encoding="utf-8") as f:
                self.assertEqual(f.readline(), "#!/usr/bin/python3\n", name)
            self.assertTrue(os.access(BIN / name, os.X_OK), name)

    def test_vendored_library_is_identical(self):
        with open(BIN / "plugin_safety.py", "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), PLUGIN_SAFETY_SHA256)

    def test_no_raw_process_or_file_calls(self):
        for name in (*self.SCRIPTS, "kb_layout.py"):
            with open(BIN / name, encoding="utf-8") as f:
                text = f.read()
            for forbidden in ("import subprocess", "shell=True", "os.system", "os.popen", "shutil",
                              "write_text(", "read_text(", "write_bytes(", ".tmp\"", ".bak\""):
                self.assertNotIn(forbidden, text, f"{name}: {forbidden}")
            self.assertIsNone(re.search(r"(?<![.\w])open\(", text), f"{name}: builtin open()")

    def test_qml_runs_only_its_helpers(self):
        with open(ROOT / "KeyboardLayoutPerApp.qml", encoding="utf-8") as f:
            qml = f.read()
        for forbidden in ('"hyprctl"', '"xkbcli"', "bar.run(", "execDetached", "bash"):
            self.assertNotIn(forbidden, qml)
        self.assertIn('readonly property string python: "/usr/bin/python3"', qml)
        commands = re.findall(r"command(?::| =) \[([^\]]*)\]", qml)
        self.assertEqual(len(commands), 5)
        for command in commands:
            self.assertTrue(command.startswith("root.python, "), command)
        self.assertEqual(qml.count("Process {"), 5)
        self.assertEqual(len(re.findall(r"\.signal\(9\)", qml)), 3)


if __name__ == "__main__":
    unittest.main(verbosity=1)
