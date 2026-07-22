"""Add /shortcut/fire to CursorBridge.py -- trigger any of Resolve's ~622 named
commands by name, via its keyboard shortcut.

WHY THIS EXISTS
Resolve's scripting API exposes no way to open a panel, switch a tab, or invoke
a menu action. Measured 2026-07-22, three independent mechanisms:

    PostMessage to the window   -> did nothing. Qt reads real modifier state via
                                   GetKeyState() at message-processing time, so a
                                   posted "Ctrl is down" is not believed.
    UI Automation (Invoke)      -> WORKS but steals foreground anyway, and is
                                   slow (806 ms vs 42 ms), and Resolve's submenu
                                   tree is not usefully exposed (1 child).
    SendInput + foreground      -> WORKS. 42-173 ms. This is what we use.

So: real hardware-level input, delivered to Resolve after deliberately bringing
it to the foreground. THE FOREGROUND STEAL IS NOT OPTIONAL -- it is how Windows
routes keyboard input to any window, for real keyboards too. A keyboard hook or
virtual driver would not avoid it. Restoring focus afterwards was attempted and
does NOT work (confirmed twice: before=Claude, after=Resolve), because the
borrowed-foreground privilege does not survive the target becoming foreground.

CONSEQUENCE FOR CALLERS: this verb is VISIBLE. Resolve will pop to the front and
stay there. Every other verb in this bridge is silent; this one is not. It is
also real input -- if the wrong window is frontmost at the wrong moment, the
keystroke lands there instead. It refuses to fire unless it has confirmed
Resolve is actually foreground first.
"""
import io, sys

P = r"F:\AMBIGUITY\TOOLS\davinci-bridge\src\CursorBridge.py"
src = io.open(P, "r", encoding="utf-8").read()


def once(needle, label):
    n = src.count(needle)
    if n != 1:
        sys.exit("ANCHOR FAIL [%s]: %d occurrences, expected 1" % (label, n))
    print("anchor ok [%s]" % label)


A1 = "def action_timeline_place(body):"
B1 = r'''# ---------------------------------------------------------------------------
# Shortcut firing: name -> keyboard binding -> real input.  See module notes.
# ---------------------------------------------------------------------------
KEYBOARD_PRESET = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "Blackmagic Design",
    "DaVinci Resolve", "Preferences", "keyboard.preset.xml")

# Qt modifier bits, as stored in the preset's 4-byte key field.
_QT_SHIFT, _QT_CTRL, _QT_ALT = 0x02000000, 0x04000000, 0x08000000
_QT_META, _QT_KEYPAD = 0x10000000, 0x20000000

# Windows virtual-key codes for the modifiers we can press.
_VK_SHIFT, _VK_CTRL, _VK_ALT, _VK_LWIN = 0x10, 0x11, 0x12, 0x5B

# Qt Key_* values that do NOT coincide with a Windows VK code. Letters and
# digits do coincide (Qt::Key_A == 'A' == VK_A == 0x41), so they need no entry.
_QT_TO_VK = {
    0x01000000: 0x1B,  # Escape
    0x01000001: 0x09,  # Tab
    0x01000003: 0x08,  # Backspace
    0x01000004: 0x0D,  # Return
    0x01000005: 0x0D,  # Enter (keypad) -> same VK
    0x01000006: 0x2D,  # Insert
    0x01000007: 0x2E,  # Delete
    0x01000010: 0x24,  # Home
    0x01000011: 0x23,  # End
    0x01000012: 0x25,  # Left
    0x01000013: 0x26,  # Up
    0x01000014: 0x27,  # Right
    0x01000015: 0x28,  # Down
    0x01000016: 0x21,  # PageUp
    0x01000017: 0x22,  # PageDown
    0x20: 0x20,        # Space
}
for _i in range(24):                      # F1..F24
    _QT_TO_VK[0x01000030 + _i] = 0x70 + _i


def _keyboard_bindings(path=None):
    """command name -> raw 4-byte key field, parsed from the preset file.

    The blob is a Qt hash-map serialization: big-endian length-prefixed UTF-16BE
    strings, each followed by two constant fields then the key field. Bucket
    ORDER changes on every save, so records are located by re-syncing on each
    length prefix -- never by absolute offset.
    """
    import re as _re
    import struct as _struct
    path = path or KEYBOARD_PRESET
    try:
        txt = io.open(path, "r", encoding="utf-8").read()
    except Exception as e:
        return None, {"error": "Could not read keyboard preset: %s" % e}
    m = _re.search(r"<PresetListBA>([0-9a-fA-F]+)</PresetListBA>", txt)
    if not m:
        return None, {"error": "keyboard.preset.xml has no PresetListBA blob"}
    b = bytes.fromhex(m.group(1))

    out, i, n = {}, 0, len(b)
    while i + 4 <= n:
        L = _struct.unpack_from(">I", b, i)[0]
        if 4 <= L <= 400 and L % 2 == 0 and i + 4 + L <= n:
            try:
                s = b[i + 4:i + 4 + L].decode("utf-16be", errors="strict")
            except UnicodeDecodeError:
                s = None
            if s and s.strip() and all(32 <= ord(c) < 0x2500 for c in s):
                fo = i + 4 + L + 8
                if fo + 4 <= n and s not in out:
                    out[s] = _struct.unpack_from(">I", b, fo)[0]
                i += 4 + L
                continue
        i += 1
    if not out:
        return None, {"error": "Parsed no commands out of the keyboard preset"}
    return out, None


def _decode_binding(raw):
    """4-byte key field -> (vk, [modifier vks], human label), or (None, ..., why)."""
    if not raw:
        return None, [], "unbound"
    mods, labels = [], []
    if raw & _QT_CTRL:
        mods.append(_VK_CTRL); labels.append("Ctrl")
    if raw & _QT_ALT:
        mods.append(_VK_ALT); labels.append("Alt")
    if raw & _QT_SHIFT:
        mods.append(_VK_SHIFT); labels.append("Shift")
    if raw & _QT_META:
        mods.append(_VK_LWIN); labels.append("Meta")

    base = raw & 0x00FFFFFF
    if raw & _QT_KEYPAD and 0x30 <= base <= 0x39:
        vk = 0x60 + (base - 0x30)          # VK_NUMPAD0..9
        labels.append("Numpad%c" % base)
    elif base in _QT_TO_VK:
        vk = _QT_TO_VK[base]
        labels.append("0x%x" % base)
    elif 0x30 <= base <= 0x39 or 0x41 <= base <= 0x5A:
        vk = base                          # digits and letters map 1:1
        labels.append(chr(base))
    else:
        return None, [], "unmappable key value 0x%08x" % raw
    return vk, mods, "+".join(labels)


def _resolve_hwnd():
    """Find Resolve's real main window. Never hardcode -- the handle changes
    every launch, and a stale handle would send real keystrokes nowhere (or,
    worse, to whatever inherited the number)."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    found = []

    def proc_name(pid):
        h = kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        return buf.value.split("\\")[-1].lower() if ok else ""

    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            ln = user32.GetWindowTextLengthW(hwnd)
            if ln > 0:
                buf = ctypes.create_unicode_buffer(ln + 1)
                user32.GetWindowTextW(hwnd, buf, ln + 1)
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                # Match the PROCESS, not the title: a title match would also hit
                # the bridge's own console window ("DaVinci Resolve MCP Bridge").
                if proc_name(pid.value) == "resolve.exe":
                    found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(CB(cb), 0)
    if not found:
        return None, None
    # Prefer the real editing window over any transient splash/dialog.
    for hwnd, title in found:
        if "davinci resolve" in title.lower():
            return hwnd, title
    return found[0][0], found[0][1]


def _send_keystroke(hwnd, vk, mod_vks):
    """Foreground Resolve, then send REAL input. Returns (ok, detail)."""
    import ctypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    PUL = ctypes.POINTER(ctypes.c_ulong)

    class _KB(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", PUL)]

    class _MI(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]

    class _HI(ctypes.Structure):
        _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_short),
                    ("wParamH", ctypes.c_ushort)]

    class _II(ctypes.Union):
        _fields_ = [("ki", _KB), ("mi", _MI), ("hi", _HI)]

    class _INPUT(ctypes.Structure):
        # All three variants must be declared: sizeof() must match the native
        # INPUT struct (40 bytes on x64) or SendInput rejects every event on the
        # size check. Declaring only the keyboard variant returned 0/4 accepted.
        _fields_ = [("type", ctypes.c_ulong), ("ii", _II)]

    def ev(v, up):
        return _INPUT(1, _II(ki=_KB(v, 0, 0x0002 if up else 0, 0, None)))

    fg_before = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg_before, None)
    my_tid = kernel32.GetCurrentThreadId()
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)

    user32.AttachThreadInput(my_tid, fg_tid, True)
    user32.AttachThreadInput(my_tid, tgt_tid, True)
    user32.ShowWindow(hwnd, 9)                       # SW_RESTORE if minimized
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(my_tid, fg_tid, False)
    user32.AttachThreadInput(my_tid, tgt_tid, False)
    time.sleep(0.05)

    if user32.GetForegroundWindow() != hwnd:
        # Refuse rather than fire blind: this is real input, and it would land
        # in whatever window IS frontmost.
        return False, {"error": "Could not bring Resolve to the foreground; "
                                "refused to send input so it cannot land in "
                                "the wrong window."}

    seq = [ev(m, False) for m in mod_vks] + [ev(vk, False), ev(vk, True)] \
        + [ev(m, True) for m in reversed(mod_vks)]
    arr = (_INPUT * len(seq))(*seq)
    sent = user32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT))
    if sent != len(seq):
        return False, {"error": "SendInput accepted only %d of %d events"
                                % (sent, len(seq))}
    return True, {"events": sent}


def action_shortcut_fire(body):
    """Trigger a Resolve command by name, using its keyboard shortcut.

    body: command  (e.g. "viewActiveWindowSelectionEffects"), or
          key      (raw 4-byte Qt binding, for testing)
          [list=true] to return matching command names instead of firing.

    VISIBLE SIDE EFFECT: brings Resolve to the foreground and leaves it there.
    Unlike every other verb here, this one interrupts what the user is doing.
    """
    bindings, err = _keyboard_bindings()
    if err:
        return err

    cmd = body.get("command", "")
    if body.get("list"):
        q = cmd.lower()
        hits = sorted(k for k in bindings if q in k.lower())
        return {"query": cmd, "count": len(hits), "commands": hits[:200]}

    if "key" in body:
        raw, cmd = int(body["key"]), cmd or "(raw key)"
    else:
        if not cmd:
            return {"error": "command is required (or pass list=true to search)"}
        if cmd not in bindings:
            near = sorted(k for k in bindings if cmd.lower() in k.lower())[:10]
            return {"error": "No command named '%s'" % cmd, "didYouMean": near}
        raw = bindings[cmd]

    vk, mods, label = _decode_binding(raw)
    if vk is None:
        return {"error": "Command '%s' is %s -- nothing to fire. Assign it a "
                         "shortcut in Resolve first." % (cmd, label),
                "command": cmd, "binding": label}

    hwnd, title = _resolve_hwnd()
    if not hwnd:
        return {"error": "Could not find a visible DaVinci Resolve window"}

    t0 = time.time()
    ok, detail = _send_keystroke(hwnd, vk, mods)
    ms = (time.time() - t0) * 1000.0
    if not ok:
        detail.update({"command": cmd, "binding": label})
        return detail
    return {
        "success": True, "command": cmd, "binding": label,
        "window": title, "ms": round(ms), "events": detail.get("events"),
        "note": "Resolve is now in the foreground and will stay there; "
                "focus is not restorable (Windows limitation, tested).",
    }


def action_timeline_place(body):'''
once(A1, "verb insertion point")
src = src.replace(A1, B1)

A2 = '    "/timeline/read":               action_timeline_read,'
B2 = ('    "/timeline/read":               action_timeline_read,\n'
      '    "/shortcut/fire":               action_shortcut_fire,')
once(A2, "route table")
src = src.replace(A2, B2)

io.open(P, "w", encoding="utf-8").write(src)
print("WROTE", P)
