#!/usr/bin/python3
"""python3 tests/memory_test.py -- the daemon's event framing and its layout memory.

The memory runs against a simulated seat: every keyboard on a layout index of its own, the
virtual keyboard an input method copies layouts onto listed last, and one announcement per
keyboard that changed. Nothing here talks to Hyprland or touches the real assignments file."""
import contextlib
import importlib.machinery
import importlib.util
import io
import pathlib
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
import kb_layout as kb  # noqa: E402


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(BIN / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


daemon = load_script("kb-layout-daemon")

US, UA, RU = "English (US)", "Ukrainian", "Russian"
LAPTOP = "at-translated-set-2-keyboard"
VK = "hl-virtual-keyboard-fcitx5"


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
                         ("layout", "at-translated-set-2-keyboard"))
        self.assertEqual(p(b"configreloaded>>"), ("reload", ""))
        self.assertIsNone(p(b"activelayout>>no-comma"))
        self.assertIsNone(p(b"activewindowv2>>0x1234"))
        self.assertIsNone(p(b"workspace>>2"))
        self.assertEqual(p(b"activewindow>>\xff,x"), ("window", "�"))
        self.assertEqual(daemon.parse_events([b"workspace>>2", b"configreloaded>>"]), [("reload", "")])


# ------------------------------------------------------------------ the simulated seat

class SimSeat:
    """Hyprland's keyboards as the daemon reads them. `switch` is switchxkblayout all
    <index>; `set` is one keyboard changing, which announces it (activelayout)."""

    def __init__(self):
        self.layout, self.names = "us,ua,ru", [US, UA, RU]
        self.devices = ["video-bus", "power-button", "intel-hid-events", LAPTOP, VK]
        self.index = dict.fromkeys(self.devices, 0)
        self.address = {d: f"0x{n}" for n, d in enumerate(self.devices, 1)}
        self.announced, self.switches = [], []
        self.active, self.fail = None, False

    def keyboards(self):
        if self.fail:
            return None
        return [{"name": d, "address": self.address[d], "rules": "", "model": "",
                 "layout": self.layout, "variant": "", "options": "",
                 "active_layout_index": self.index[d], "active_keymap": self.names[self.index[d]]}
                for d in self.devices]

    def switch(self, index):
        self.switches.append(index)
        for d in self.devices:
            self.set(d, index)

    def active_class(self):
        return self.active

    def table(self, rmlvo):
        return list(self.names)

    def set(self, device, index):
        index %= len(self.names)
        if self.index[device] != index:
            self.index[device] = index
            self.announced.append(("layout", device))

    def next_all(self):
        """hyprctl switchxkblayout all next: the ALT+Shift binding, or a widget click."""
        for d in self.devices:
            self.set(d, self.index[d] + 1)

    def next_one_at_a_time(self):
        """The old kb-layout-next: a request per keyboard, and fcitx5 copying the first
        keyboard's new layout onto its virtual keyboard before the loop gets there."""
        for n, d in enumerate(self.devices):
            self.set(d, self.index[d] + 1)
            if n == 0:
                self.set(VK, self.index[d])

    def plug(self, device):
        self.devices.insert(-1, device)
        self.index[device] = 0
        self.address[device] = f"0x{100 + len(self.address)}"
        self.announced.append(("layout", device))

    def take(self):
        events, self.announced = self.announced, []
        return events

    def on(self, index):
        return all(i == index for i in self.index.values())


class Store:
    def __init__(self, assignments=None, writable=True):
        self.assignments, self.writable, self.saved = dict(assignments or {}), writable, []

    def load(self):
        return dict(self.assignments), self.writable

    def save(self, assignments):
        self.saved.append(dict(assignments))
        self.assignments = dict(assignments)


class MemoryCase(unittest.TestCase):
    def setUp(self):
        kb._last_log[0] = None
        self.err = contextlib.redirect_stderr(io.StringIO())
        self.err.__enter__()

    def tearDown(self):
        self.err.__exit__(None, None, None)

    def memory(self, seat, store):
        m = daemon.LayoutMemory(seat, store.load, store.save)
        m.start()
        return m

    def deliver(self, m, seat, *after):
        """Everything the seat announced, then `after`, as one batch."""
        m.handle(seat.take() + list(after))


# ------------------------------------------------------------------ layout memory

class Memory(MemoryCase):
    def test_focus_restores_the_assignment_in_one_request_and_ignores_its_echo(self):
        seat, store = SimSeat(), Store({"telegram": UA})
        m = self.memory(seat, store)
        m.handle([("window", "telegram")])
        self.assertEqual(seat.switches, [1])
        self.assertTrue(seat.on(1))
        self.deliver(m, seat)
        self.assertEqual((seat.switches, store.saved), ([1], []))

    def test_a_switch_is_recorded_once_for_the_app_it_was_made_in(self):
        seat, store = SimSeat(), Store()
        m = self.memory(seat, store)
        m.handle([("window", "foot")])
        seat.next_all()
        self.deliver(m, seat)
        self.assertEqual(store.saved, [{"foot": UA}])
        self.assertEqual(seat.switches, [0])        # the focus; the switch itself needed no help
        self.deliver(m, seat)
        self.assertEqual(len(store.saved), 1)

    def test_one_keyboard_at_a_time_records_the_layout_typed_and_rejoins_the_input_method(self):
        # What the old ALT+Shift script did to this seat: fcitx5's keyboard -- the one text comes
        # out of -- on Russian while every real keyboard and the label said Ukrainian, and
        # Russian got recorded.
        seat, store = SimSeat(), Store()
        m = self.memory(seat, store)
        m.handle([("window", "foot")])
        seat.next_one_at_a_time()
        self.assertEqual((seat.index[LAPTOP], seat.index[VK]), (1, 2))
        self.deliver(m, seat)
        self.assertEqual(store.saved, [{"foot": UA}])
        self.assertTrue(seat.on(1))
        self.deliver(m, seat)
        self.assertEqual(store.saved, [{"foot": UA}])

    def test_virtual_keyboards_are_never_a_choice(self):
        seat, store = SimSeat(), Store({"foot": US})
        m = self.memory(seat, store)
        m.handle([("window", "foot")])
        seat.set(VK, 2)
        self.deliver(m, seat, ("layout", "hl-virtual-keyboard-wtype"))
        self.assertEqual((store.saved, seat.switches), ([], [0]))

    def test_quick_focus_changes_apply_only_the_last_window(self):
        seat, store = SimSeat(), Store({"telegram": UA, "mpv": RU})
        m = self.memory(seat, store)
        m.handle([("window", "telegram"), ("window", "mpv")])
        self.assertEqual(seat.switches, [2])

    def test_late_echoes_of_an_earlier_focus_record_nothing(self):
        seat, store = SimSeat(), Store({"telegram": UA, "mpv": RU})
        m = self.memory(seat, store)
        m.handle([("window", "telegram")])
        echo = seat.take()
        m.handle([("window", "mpv")])
        m.handle(echo + seat.take())
        self.assertEqual(store.saved, [])
        self.assertTrue(seat.on(2))

    def test_a_switch_then_a_focus_change_in_one_batch_is_recorded_for_the_first_app(self):
        seat, store = SimSeat(), Store()
        m = self.memory(seat, store)
        m.handle([("window", "foot")])
        seat.next_all()
        self.deliver(m, seat, ("window", "telegram"))
        self.assertEqual(store.saved, [{"foot": UA}])
        self.assertTrue(seat.on(0))

    def test_a_keyboard_plugged_in_joins_the_layout_instead_of_being_recorded(self):
        seat, store = SimSeat(), Store({"telegram": UA})
        m = self.memory(seat, store)
        m.handle([("window", "telegram")])
        self.deliver(m, seat)
        seat.plug("usb-keyboard")
        self.deliver(m, seat)
        self.assertTrue(seat.on(1))
        seat.address[LAPTOP] = "0xfeed"             # back from suspend: same name, new device
        seat.set(LAPTOP, 0)
        self.deliver(m, seat)
        self.assertEqual(store.saved, [])
        self.assertTrue(seat.on(1))

    def test_a_reload_that_resets_keymaps_restores_instead_of_recording(self):
        seat, store = SimSeat(), Store({"telegram": UA})
        m = self.memory(seat, store)
        m.handle([("window", "telegram")])
        self.deliver(m, seat)
        for d in seat.devices:
            seat.set(d, 0)
        self.deliver(m, seat, ("reload", ""))
        self.assertEqual(store.saved, [])
        self.assertTrue(seat.on(1))

    def test_a_plain_reload_changes_nothing(self):
        seat, store = SimSeat(), Store({"telegram": UA})
        m = self.memory(seat, store)
        m.handle([("window", "telegram")])
        self.deliver(m, seat)
        m.handle([("reload", "")])
        self.assertEqual((seat.switches, store.saved), ([1], []))

    def test_a_new_kb_layout_restores_the_app_by_name(self):
        seat, store = SimSeat(), Store({"telegram": RU})
        m = self.memory(seat, store)
        m.handle([("window", "telegram")])
        self.deliver(m, seat)
        seat.layout, seat.names = "us,ru", [US, RU]  # applied without a reload event
        for d in seat.devices:
            seat.set(d, 0)
        self.deliver(m, seat)
        self.assertEqual(store.saved, [])
        self.assertTrue(seat.on(1))

    def test_leaving_every_window_records_nothing_and_coming_back_restores(self):
        seat, store = SimSeat(), Store({"telegram": UA})
        m = self.memory(seat, store)
        m.handle([("window", "telegram")])
        self.deliver(m, seat, ("window", ""))
        seat.next_all()
        self.deliver(m, seat)
        self.assertEqual(store.saved, [])
        m.handle([("window", "telegram")])
        self.assertTrue(seat.on(1))

    def test_unusable_class_gets_the_default_and_records_nothing(self):
        for bad in ("x" * (kb.MAX_NAME + 1), "bad\tclass", "esc\x1b[31m"):
            seat, store = SimSeat(), Store({"telegram": UA})
            m = self.memory(seat, store)
            m.handle([("window", "telegram")])
            self.deliver(m, seat, ("window", bad))
            self.assertTrue(seat.on(0), bad)
            seat.next_all()
            self.deliver(m, seat)
            self.assertEqual(store.saved, [], bad)

    def test_an_unusable_file_is_never_written(self):
        seat, store = SimSeat(), Store(writable=False)
        m = self.memory(seat, store)
        m.handle([("window", "foot")])
        seat.next_all()
        self.deliver(m, seat)
        self.assertEqual(store.saved, [])

    def test_the_cap_refuses_new_apps_but_updates_remembered_ones(self):
        seat, store = SimSeat(), Store({f"app{i}": UA for i in range(kb.MAX_APPS)})
        m = self.memory(seat, store)
        m.handle([("window", "newapp")])
        seat.next_all()
        self.deliver(m, seat)
        self.assertEqual(store.saved, [])
        m.handle([("window", "app1")])
        seat.next_all()
        self.deliver(m, seat)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(store.saved[0]["app1"], RU)
        self.assertEqual(len(store.saved[0]), kb.MAX_APPS)

    def test_start_adopts_the_seat_and_rejoins_keyboards_left_behind(self):
        seat, store = SimSeat(), Store({"foot": US})
        seat.active = "foot"
        seat.index = dict.fromkeys(seat.devices, 1)
        seat.index[VK], seat.index["power-button"] = 2, 0
        m = self.memory(seat, store)
        self.assertEqual((m.cls, seat.switches, store.saved), ("foot", [1], []))
        self.assertTrue(seat.on(1))
        m.handle([("window", "foot")])              # a title change: the same app
        self.assertEqual(seat.switches, [1])

    def test_an_assignment_kb_layout_lacks_gets_the_first_layout(self):
        seat, store = SimSeat(), Store({"telegram": "Klingon"})
        seat.index = dict.fromkeys(seat.devices, 2)
        m = self.memory(seat, store)
        m.handle([("window", "telegram")])
        self.assertTrue(seat.on(0))

    def test_an_unreadable_seat_changes_nothing(self):
        seat, store = SimSeat(), Store({"telegram": UA})
        seat.fail = True
        m = self.memory(seat, store)
        m.handle([("window", "telegram"), ("layout", LAPTOP), ("reload", "")])
        self.assertEqual((seat.switches, store.saved), ([], []))

    def test_assign_results(self):
        d = {"a": "x"}
        self.assertEqual(kb.assign(d, "a", "x"), "unchanged")
        self.assertEqual(kb.assign(d, "a", "y"), "changed")
        full = {str(i): "x" for i in range(kb.MAX_APPS)}
        self.assertEqual(kb.assign(full, "new", "x"), "full")
        self.assertNotIn("new", full)


class SeatTables(unittest.TestCase):
    def test_compiled_once_per_keymap_and_bounded(self):
        calls, saved = [], kb.layout_table
        kb.layout_table = lambda rmlvo: calls.append(rmlvo) or [US]
        try:
            seat = daemon.Seat()
            for _ in range(3):
                self.assertEqual(seat.table(("", "", "us,ua", "", "")), [US])
            self.assertEqual(len(calls), 1)
            for i in range(daemon.MAX_TABLES + 5):
                seat.table(("", "", f"us,x{i}", "", ""))
            self.assertLessEqual(len(seat.tables), daemon.MAX_TABLES)
        finally:
            kb.layout_table = saved


if __name__ == "__main__":
    unittest.main(verbosity=1)
