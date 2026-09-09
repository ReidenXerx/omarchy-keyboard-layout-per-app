# Keyboard layout per app

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

## Credits

`KeyboardLayoutModel.js` and the widget's layout-querying logic come from Omarchy's built-in
`omarchy.keyboard-layout` widget by David Heinemeier Hansson, MIT licensed. This plugin adds
the per-application memory, the picker, and the daemon.

## License

MIT — see [LICENSE](LICENSE).
