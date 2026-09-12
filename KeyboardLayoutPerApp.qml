import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Io
import qs.Ui
import qs.Commons
import "KeyboardLayoutModel.js" as KeyboardLayoutModel

BarWidget {
  id: root
  moduleName: "reidenxerx.keyboard-layout-per-app"


  property string layoutFull: ""
  // The keyboard the last reading spoke for, which is the one a click switches,
  // and separately the one activelayout named as being typed on. A reading
  // confirms the first is really there, so the click has a keyboard to reach
  // from the first reading onwards rather than only after a switch, and stops
  // naming one that has been unplugged.
  property string keyboardName: ""
  property string typedKeyboardName: ""
  // Keyboards on the seat, buttons and virtual ones excluded, and whether the
  // last reading left that shape in doubt.
  property int keyboardCount: 0
  property bool keyboardUnresolved: false
  // Nothing to read or switch on the single-layout install most people run, so
  // the widget ships on the bar and stays out of the way until there are two.
  // An older Hyprland that doesn't report the list keeps showing the label.
  property bool multipleLayouts: true
  // Short language code per layout description ("English (US)": "en"), read from
  // xkb's own table rather than maintained by hand.
  property var layoutBriefs: ({})
  readonly property string layoutLabel: KeyboardLayoutModel.shortLabel(layoutFull, layoutBriefs)

  // A query already in flight was started before this event, so it may read the
  // layout the switch replaced. Remember the request and re-run once it lands
  // rather than dropping it; nothing else would correct the label afterwards.
  property bool refreshPending: false

  // Clicks that arrived while a switch was still running, replayed one at a
  // time once it lands so each click still advances a layout.
  property int cyclesQueued: 0

  // Every process this widget starts is a helper shipped in bin/, run by the
  // system interpreter by absolute path -- never hyprctl, xkbcli or a shell
  // looked up on PATH. The helpers give what they run a deadline, an output
  // ceiling and a process-group kill, and print only bounded, checked fields.
  // Qt.resolvedUrl() resolves relative to this QML file; Process wants a plain
  // path, so the file:// scheme is stripped.
  readonly property string pluginBin: decodeURIComponent(String(Qt.resolvedUrl("bin/")).replace(/^file:\/\//, ""))
  readonly property string python: "/usr/bin/python3"
  readonly property string helper: pluginBin + "kb-layout-assign"
  // Far above anything a helper prints (a seat's keyboards, the layout table),
  // so a reading past it is refused rather than parsed.
  readonly property int maxHelperOutput: 512 * 1024
  // How long a helper that was asked to stop gets before it is SIGKILLed.
  readonly property int killGraceMs: 1500

  function refresh() {
    if (queryProc.running) {
      refreshPending = true
      return
    }

    refreshPending = false
    queryProc.running = true
  }

  // Stopping a Process sends SIGTERM and leaves it running until it exits. The
  // helpers answer SIGTERM by killing the process group of everything they
  // started, so that is normally the end of it; one still there after the grace
  // period is SIGKILLed, and whatever it started dies on its own deadline.
  function stopProcess(proc, killTimer) {
    if (!proc.running) return
    proc.running = false
    killTimer.restart()
  }

  // Keyboards someone can actually type on, which is not everything Hyprland
  // calls a keyboard.
  function typedKeyboards(keyboards) {
    return keyboards.filter(k => KeyboardLayoutModel.isTypedKeyboard(k.name))
  }

  // The main flag names no keyboard for long: fcitx5 takes it with the virtual
  // keyboard it binds to inject, which leaves no typed keyboard holding it and
  // nothing to read at all, and once that unbinds it lands on whichever device
  // Hyprland saw last, a power button included. Go by layout progress instead,
  // and by the keyboard activelayout named.
  function selectKeyboard(typed) {
    return KeyboardLayoutModel.selectKeyboard(typed, root.typedKeyboardName)
  }

  // switchxkblayout is a hyprctl command rather than a dispatcher, so the helper
  // sends it to Hyprland's socket. It moves every keyboard in one request.
  // Advancing only the keyboard the label reads left the rest behind: the
  // buttons, and the virtual keyboard an input method like fcitx5 types
  // through, whose layout is what text actually comes out in. The next key
  // from any of them flipped the layout back, and the daemon recorded the
  // wrong one. With every keyboard on one layout the label reads the same
  // whichever keyboard it settles on.
  function cycleLayout() {
    if (!root.bar) return
    if (cycleProc.running) {
      root.cyclesQueued = Math.min(root.cyclesQueued + 1, 8)
      return
    }
    cycleProc.command = [root.python, root.helper, "cycle"]
    cycleProc.running = true
    refreshTimer.restart()
  }

  Component.onCompleted: {
    briefsProc.running = true
    refresh()
  }

  Connections {
    target: Hyprland
    function onRawEvent(event) {
      if (!event || !event.name) return
      var name = String(event.name)
      // The event names the keyboard that switched ahead of the layout it moved
      // to, and that is the keyboard being typed on whatever holds the main flag.
      if (name === "activelayout") {
        const named = KeyboardLayoutModel.eventKeyboardName(event)
        if (named) root.typedKeyboardName = named
      }

      // A reload that adds a layout to kb_layout decides whether the widget
      // shows at all, and leaves every keyboard on the layout it was already
      // reading, so it raises no activelayout to notice it by.
      if (name.indexOf("activelayout") !== -1 || name === "configreloaded") root.refresh()
    }
  }

  // The seat's keyboards, as `hyprctl -j devices` reports them, reduced by the
  // helper to the fields read here.
  Process {
    id: queryProc
    command: [root.python, root.helper, "devices"]
    onRunningChanged: {
      if (running) {
        stallTimer.restart()
        return
      }

      stallTimer.stop()
      queryKillTimer.stop()
      if (root.refreshPending) root.refresh()
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (text.length > root.maxHelperOutput) return

        let listed
        try {
          listed = JSON.parse(text || "{}").keyboards
        } catch (e) {
          return
        }

        // A query the watchdog killed reports nothing at all, and an empty
        // string parses into the same shape a seat with no keyboards would.
        // Tell them apart by the list itself, so only a reading that reached
        // hyprctl gets to speak for the seat.
        if (!Array.isArray(listed)) return

        const typed = root.typedKeyboards(listed)
        const kb = root.selectKeyboard(typed)
        if (!kb || !kb.active_keymap) {
          // Either the last keyboard has been unplugged, which the label has to
          // stop describing and the click has to stop naming, or keyboards are
          // there and none of them reports a keymap. Both leave the shape in
          // doubt, so keep asking rather than letting a count from before it
          // changed settle the poll.
          root.keyboardUnresolved = true
          if (typed.length === 0) {
            root.layoutFull = ""
            root.keyboardName = ""
          }
          return
        }

        root.keyboardUnresolved = false
        root.keyboardCount = typed.length
        root.keyboardName = String(kb.name || "")
        root.multipleLayouts = kb.layout === undefined || String(kb.layout).indexOf(",") !== -1
        root.layoutFull = kb.active_keymap
      }
    }
  }

  // The table only changes when xkb data is upgraded, so read it at startup and
  // leave it alone. The bar is built per monitor, so this runs once per widget.
  // The exotic rulesets cover layouts like trans (IPA) that ship in the same xkb
  // package and set just as well, so load them or those labels lose their code.
  // The helper passes on only the lines KeyboardLayoutModel.layoutBriefs() reads.
  Process {
    id: briefsProc
    command: [root.python, root.helper, "layouts"]
    onRunningChanged: {
      if (running) {
        briefsStallTimer.restart()
        return
      }

      briefsStallTimer.stop()
      briefsKillTimer.stop()
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (text.length > root.maxHelperOutput) return
        root.layoutBriefs = KeyboardLayoutModel.layoutBriefs(text)
      }
    }
  }

  Process {
    id: cycleProc
    onRunningChanged: {
      if (running) {
        cycleStallTimer.restart()
        return
      }

      cycleStallTimer.stop()
      cycleKillTimer.stop()
      if (root.cyclesQueued > 0) {
        root.cyclesQueued -= 1
        root.cycleLayout()
      }
    }
  }

  Timer {
    id: refreshTimer
    interval: 600
    onTriggered: root.refresh()
  }

  // A query that never returns would freeze the label until the shell restarts,
  // since a Process that is already running can't be re-run. Give up on one that
  // overstays so the next refresh gets through, and ask again: the reading it
  // never delivered may have been the only one due on a settled seat, and
  // nothing else would come back for it.
  Timer {
    id: stallTimer
    interval: 5000
    onTriggered: {
      root.stopProcess(queryProc, queryKillTimer)
      refreshTimer.restart()
    }
  }

  Timer {
    id: queryKillTimer
    interval: root.killGraceMs
    onTriggered: if (queryProc.running) queryProc.signal(9)
  }

  // xkbcli gets ten seconds inside the helper; this only catches a helper that
  // outlives its own deadline.
  Timer {
    id: briefsStallTimer
    interval: 20000
    onTriggered: root.stopProcess(briefsProc, briefsKillTimer)
  }

  Timer {
    id: briefsKillTimer
    interval: root.killGraceMs
    onTriggered: if (briefsProc.running) briefsProc.signal(9)
  }

  Timer {
    id: cycleStallTimer
    interval: 5000
    onTriggered: root.stopProcess(cycleProc, cycleKillTimer)
  }

  Timer {
    id: cycleKillTimer
    interval: root.killGraceMs
    onTriggered: if (cycleProc.running) cycleProc.signal(9)
  }

  // Which keyboard on a crowded seat the label is describing can change without
  // Hyprland announcing it, since a device arriving or leaving raises no event
  // of its own, and that can only be learned by asking. Poll while there is that
  // ambiguity, until a first reading lands so a query that failed at login still
  // recovers, and while a reading has left the seat's shape in doubt. The
  // one-keyboard install has none of those, and is left alone rather than
  // spawning a query forever for an answer that cannot change.
  Timer {
    interval: 10000
    running: !root.keyboardName || root.keyboardUnresolved || root.keyboardCount > 1
    repeat: true
    onTriggered: root.refresh()
  }

  // The picker. Interactive, so no watchdog: its menus carry their own deadline
  // inside the helper.
  Process {
    id: assignProc
    command: [root.python, root.helper]
  }

  // The daemon that restores each app's layout on focus. Started by the widget rather than
  // by an autostart entry: a plugin cannot edit the user's hypr config, and tying its life
  // to the shell means it stops cleanly when the shell does (it also asks the kernel to
  // signal it if the shell dies).
  Process {
    id: daemonProc
    command: [root.python, root.pluginBin + "kb-layout-daemon"]
    running: true
    // Nothing else brings it back, and without it every app keeps whatever layout
    // it last had: Hyprland drops an event reader that falls behind, for one.
    onRunningChanged: if (!running) daemonRestartTimer.restart()
  }

  Timer {
    id: daemonRestartTimer
    interval: 3000
    onTriggered: daemonProc.running = true
  }

  visible: layoutLabel !== "" && multipleLayouts
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.layoutLabel
    fontSize: Style.font.caption
    horizontalMargin: 6
    tooltipText: root.layoutFull + "  \u2022  right-click: set layout for app"
    // WidgetButton emits pressed(int button) and already accepts right/middle, so
    // the button can be discriminated without touching its MouseArea.
    onPressed: function(button) {
      if (button === Qt.RightButton) {
        if (!assignProc.running) assignProc.running = true
      } else {
        root.cycleLayout()
      }
    }
  }
}
