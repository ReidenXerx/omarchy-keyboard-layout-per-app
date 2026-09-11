# Keyboard layout per app

![Keyboard layout per app](preview.png)

An [Omarchy](https://omarchy.org) bar widget that remembers the **keyboard layout per
application** and restores it when you focus that app.

Type Ukrainian in Telegram and English in your terminal, and stop switching by hand. KDE and
Windows both behave this way; Hyprland has a single global layout, so this supplies the
memory.

![bar widget](https://img.shields.io/badge/omarchy-bar--widget-blue)

## Install

```bash
omarchy plugin add https://github.com/ReidenXerx/omarchy-keyboard-layout-per-app.git --enable
```

Requires more than one layout in `kb_layout` — the widget hides itself otherwise:

```lua
-- ~/.config/hypr/input.lua
hl.config({ input = {
  kb_layout = "us,ua,ru",
  kb_options = "compose:caps,shift:both_capslock_cancel",
}})
```

## Use

| action | result |
|---|---|
| **left-click** the widget | cycle layout |
| **right-click** the widget | pick an app, then a layout |
| focus an app | its remembered layout is restored |
| switch layout while an app is focused | that becomes the app's layout |
| unassigned apps | first layout in `kb_layout` (index 0) |

Assignments live in `~/.config/omarchy/kb-layout-per-app.json` — plain JSON, hand-editable,
re-read on every focus, so edits apply with no restart.

```bash
bin/kb-layout-assign            # the picker (same as right-click)
bin/kb-layout-assign list
bin/kb-layout-assign set foot Ukrainian
bin/kb-layout-assign clear foot
```

The app picker lists running windows *and* installed applications, so an app can be assigned
before it is ever opened — including tray-only ones.

## How it works, and why it looks like this

`bin/kb-layout-daemon` tails Hyprland's event socket, applying a layout on `activewindow`
and recording one on `activelayout`. The widget starts it via `Process { running: true }`
rather than an autostart entry: a plugin cannot edit someone's hypr config, and tying the
daemon to the shell means it stops cleanly too.

Three details that are easy to get wrong, each of which broke a build during development:

- **Layout is per input DEVICE.** A laptop can expose ten keyboard devices (lid switch,
  power button, hotkeys...). Switching only the one Hyprland flags `main` leaves the keyboard
  you actually type on behind, and `main` moves between devices, so it cannot be hardcoded.
  Every multi-layout device is switched together.
- **Applying a layout emits `activelayout` events of its own.** Suppressing them on a timer
  does not work: the apply shells out to `hyprctl` once per device, so the echo can outlast
  any fixed window and get recorded as if the user had chosen it — silently overwriting real
  assignments. Echoes are matched by *value* instead, which is exact regardless of timing.
- **Unassigned apps are set by layout INDEX 0**, not by the name "English (US)". Hyprland
  reports display names that vary with locale; the index is stable.

Keyed by window **class**, so a second window of the same app inherits the choice.

## Security

The plugin runs as you, next to other processes that also run as you, so it does not trust
what it reads or the paths it writes:

- **No shell, no PATH.** The widget starts only its own helpers, as
  `/usr/bin/python3 <plugin>/bin/…`. The helpers run `hyprctl`, `xkbcli`,
  `omarchy-menu-select` and `omarchy-notification-send` by absolute path, only if they are
  root-owned and not writable by others (`bin/plugin_safety.py`, shared by the ReidenXerx
  plugins). Children get `PATH=/usr/bin`, a deadline, an output ceiling (hyprctl 256 KB,
  xkbcli 2 MB, menus 64 KB) and a whole-process-group kill, and run under
  `/usr/bin/timeout` so they die even if their helper is killed.
- **Bounded output to QML.** `kb-layout-assign devices` prints at most 64 keyboards and only
  the four fields the widget reads; `layouts` prints only the lines the label table uses. The
  widget refuses readings over 512 KB. A stuck helper gets SIGTERM from the watchdog (it then
  kills everything it started) and SIGKILL 1.5 s later.
- **Bounded events.** The daemon reads Hyprland's event socket, opened without following
  symlinks, through a 4 KB-per-event, 64 KB-total buffer, dropping oversized events. Window
  classes and layout names over 256 characters or with control characters are never stored.
  At most 500 apps are remembered; past that a new app is refused and logged, never evicted.
  The daemon exits with the shell that started it.
- **Safe file handling.** The assignments file is read with a 128 KB cap and only if it is
  a regular file you own, reached without symlinks. Writes go to a random `O_EXCL`
  temporary file in the same directory, are fsynced and renamed over the destination. If the
  file is unreadable or not valid JSON, the plugin leaves it alone rather than overwrite it.
  The menu installer follows the same rules and never writes a side `.bak` file.

Tests: `python3 tests/helpers_test.py` and `node tests/model-test.js`.

## Credits

`KeyboardLayoutModel.js` and the widget's layout-querying logic come from Omarchy's built-in
`omarchy.keyboard-layout` widget by David Heinemeier Hansson, MIT licensed. This plugin adds
the per-application memory, the picker, and the daemon.

## Menu entries

Optional Omarchy menu routes (assign, list, clear):

```bash
bin/kb-layout-menu-install          # add them
bin/kb-layout-menu-install remove   # take them out
```

It writes only between its own marker comments in
`~/.config/omarchy/extensions/omarchy-menu.jsonc` and rolls back rather than leaving that
file unparseable.

## Remove

```bash
bin/kb-layout-menu-install remove
omarchy plugin remove reidenxerx.keyboard-layout-per-app
```

Assignments stay in `~/.config/omarchy/kb-layout-per-app.json`; delete it to forget them.
The layout daemon stops with the shell, so nothing is left running.

## License

MIT — see [LICENSE](LICENSE).
