"""
forza_wheel_leds.py
--------------------
Bridges Forza telemetry (UDP Data Out) to the Logitech G29/G920/G923 RPM LEDs.

Supported games : Forza Horizon 5, Forza Horizon 6, Forza Motorsport (2023)
Supported wheels: Logitech G29, G920, G923 (direct USB HID — no G HUB required)

Requirements:
- Python 3.8+  (not needed if using the .exe release)
- hidapi.dll bundled in the .exe release (Windows inbox-independent)

In-game setup (all supported Forza titles):
Settings > HUD and Gameplay  (or Gameplay & HUD)
Data Out             : ON
Data Out IP Address  : 127.0.0.1
Data Out IP Port     : 5607
"""

import configparser
import ctypes
import ctypes.util
import msvcrt
import os
import socket
import struct
import sys
import time

# ---------------------------------------------------------------------------
# DEFAULT CONFIGURATION  (overridden by config.ini if present)
# ---------------------------------------------------------------------------

UDP_PORT           = 5607   # Must match the port set in-game
UDP_IP             = "0.0.0.0"
LED_MIN_RPM_RATIO  = 0.65   # First LED lights at this fraction of redline
BLINK_OFFSET_LOW_GEAR_RPM = 750 # Blink offset for gears 1-3
BLINK_OFFSET_HIGH_GEAR_RPM = 500 # Blink offset for gears 4+
USE_AUTO_REDLINE   = True   # Automatically detect actual rev limiter
BLINK_HZ           = 10.0   # Blink frequency in Hz

CONFIG_FILENAME = "config.ini"


def _config_path() -> str:
    """Return the path to config.ini, next to the .exe or the script."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CONFIG_FILENAME)


def load_config(path: str) -> dict:
    """
    Read config.ini and return a dict of validated settings.
    Missing keys fall back to the module-level defaults.
    Also parses [car_XXXX] sections.
    """
    cfg = configparser.ConfigParser()
    cfg.read(path)
    s = cfg["settings"] if "settings" in cfg else {}

    def _float(key, default):
        try:
            return float(s.get(key, str(default)))
        except ValueError:
            return float(default)

    def _int(key, default):
        try:
            return int(s.get(key, str(default)))
        except ValueError:
            return int(default)

    def _bool(key, default):
        raw = s.get(key, str(default)).lower()
        return raw in ("true", "1", "yes", "on")

    # [forward] section: targets = ip:port, ip:port, ...
    forward_targets = []
    if "forward" in cfg:
        raw = cfg["forward"].get("targets", "").strip()
        if raw:
            for entry in raw.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    host, port_str = entry.rsplit(":", 1)
                    forward_targets.append((host.strip(), int(port_str.strip())))
                except ValueError:
                    pass  # ignore malformed entries

    # Parse [car_XXXX] sections
    cars = {}
    for section in cfg.sections():
        if section.startswith("car_"):
            try:
                ordinal = int(section.split("_")[1])
                sec = cfg[section]
                cars[ordinal] = {
                    "redline": float(sec.get("redline", "0.0")),
                    "nominal_max_rpm": float(sec.get("nominal_max_rpm", "0.0")),
                    "led_min_rpm_ratio": float(sec.get("led_min_rpm_ratio", str(LED_MIN_RPM_RATIO))),
                    "blink_offset_low_gear_rpm": int(sec.get("blink_offset_low_gear_rpm", str(BLINK_OFFSET_LOW_GEAR_RPM))),
                    "blink_offset_high_gear_rpm": int(sec.get("blink_offset_high_gear_rpm", str(BLINK_OFFSET_HIGH_GEAR_RPM))),
                    "blink_hz": float(sec.get("blink_hz", str(BLINK_HZ))),
                }
            except (ValueError, IndexError):
                pass

    return {
        "udp_port":          _int  ("udp_port",          UDP_PORT),
        "led_min_rpm_ratio": _float("led_min_rpm_ratio", LED_MIN_RPM_RATIO),
        "blink_offset_low_gear_rpm":  _int  ("blink_offset_low_gear_rpm",  BLINK_OFFSET_LOW_GEAR_RPM),
        "blink_offset_high_gear_rpm":  _int  ("blink_offset_high_gear_rpm",  BLINK_OFFSET_HIGH_GEAR_RPM),
        "use_auto_redline":  _bool ("use_auto_redline",  USE_AUTO_REDLINE),
        "blink_hz":          _float("blink_hz",          BLINK_HZ),
        "forward_targets":   forward_targets,
        "cars":              cars,
    }


def save_config(path: str, settings: dict) -> None:
    """Write current settings back to config.ini, including [car_XXXX] sections."""
    cfg = configparser.ConfigParser()
    cfg.read(path)
    if "settings" not in cfg:
        cfg["settings"] = {}
    
    s = cfg["settings"]
    s["udp_port"]          = str(settings["udp_port"])
    s["led_min_rpm_ratio"] = f"{settings['led_min_rpm_ratio']:.2f}"
    s["blink_offset_low_gear_rpm"]  = str(settings["blink_offset_low_gear_rpm"])
    s["blink_offset_high_gear_rpm"]  = str(settings["blink_offset_high_gear_rpm"])
    s["use_auto_redline"]  = "true" if settings["use_auto_redline"] else "false"
    s["blink_hz"]          = str(int(settings["blink_hz"]))

    # Write car sections
    for ordinal, cdata in settings.get("cars", {}).items():
        section = f"car_{ordinal}"
        if section not in cfg:
            cfg[section] = {}
        sec = cfg[section]
        sec["redline"] = f"{cdata['redline']:.1f}"
        sec["nominal_max_rpm"] = f"{cdata.get('nominal_max_rpm', 0.0):.1f}"
        sec["led_min_rpm_ratio"] = f"{cdata['led_min_rpm_ratio']:.2f}"
        sec["blink_offset_low_gear_rpm"] = str(cdata['blink_offset_low_gear_rpm'])
        sec["blink_offset_high_gear_rpm"] = str(cdata['blink_offset_high_gear_rpm'])
        sec["blink_hz"] = str(int(cdata['blink_hz']))

    try:
        with open(path, "w") as f:
            cfg.write(f)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# LOGITECH G29 / G920 / G923  —  DIRECT USB HID via hidapi.dll (ctypes)
# ---------------------------------------------------------------------------

LOGITECH_VID = 0x046D
WHEEL_PIDS = [
    0xC24F,  # G29 (PC / PS3 mode)
    0xC262,  # G920
    0xC266,  # G923 (PS4 / PC)
    0xC26D,  # G923 (Xbox / PC)
    0xC26E,  # G923 (Xbox / PC - compatibility mode)
]

NUM_LEDS     = 5
ALL_LEDS_ON  = (1 << NUM_LEDS) - 1   # 0x1F
ALL_LEDS_OFF = 0x00

# On Windows a G923 exposes multiple HID collections with one VID/PID.  The
# RPM LEDs accept reports through the joystick collection (MI_00), rather than
# the first collection returned by hid_open(vid, pid).
LED_USAGE_PAGE = 0x01
LED_USAGE = 0x04


class _HidDeviceInfo(ctypes.Structure):
    pass


_HidDeviceInfo._fields_ = [
    ("path", ctypes.c_char_p),
    ("vendor_id", ctypes.c_ushort),
    ("product_id", ctypes.c_ushort),
    ("serial_number", ctypes.c_wchar_p),
    ("release_number", ctypes.c_ushort),
    ("manufacturer_string", ctypes.c_wchar_p),
    ("product_string", ctypes.c_wchar_p),
    ("usage_page", ctypes.c_ushort),
    ("usage", ctypes.c_ushort),
    ("interface_number", ctypes.c_int),
    ("next", ctypes.POINTER(_HidDeviceInfo)),
]


def _hidapi_dll_path() -> str:
    """
    Resolve path to hidapi.dll.
    - PyInstaller .exe: DLL is extracted to sys._MEIPASS
    - Script mode: look next to the script, then rely on PATH
    """
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "hidapi.dll")  # type: ignore[attr-defined]
    # Script mode: look next to the script first
    beside = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hidapi.dll")
    if os.path.exists(beside):
        return beside
    return "hidapi.dll"  # fall back to PATH


def load_hidapi() -> ctypes.CDLL:
    """Load hidapi.dll and declare the function signatures we need."""
    path = _hidapi_dll_path()
    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        raise OSError(f"Cannot load hidapi.dll: {exc}") from exc

    # hid_init() → int
    lib.hid_init.restype  = ctypes.c_int
    lib.hid_init.argtypes = []

    # hid_exit() → int
    lib.hid_exit.restype  = ctypes.c_int
    lib.hid_exit.argtypes = []

    # hid_open(vendor_id, product_id, serial_number=NULL) → hid_device*
    lib.hid_open.restype  = ctypes.c_void_p
    lib.hid_open.argtypes = [ctypes.c_ushort, ctypes.c_ushort, ctypes.c_wchar_p]

    # HID collection enumeration and path-specific opening.
    lib.hid_enumerate.restype  = ctypes.POINTER(_HidDeviceInfo)
    lib.hid_enumerate.argtypes = [ctypes.c_ushort, ctypes.c_ushort]
    lib.hid_free_enumeration.restype  = None
    lib.hid_free_enumeration.argtypes = [ctypes.POINTER(_HidDeviceInfo)]
    lib.hid_open_path.restype  = ctypes.c_void_p
    lib.hid_open_path.argtypes = [ctypes.c_char_p]

    # hid_close(device) → void
    lib.hid_close.restype  = None
    lib.hid_close.argtypes = [ctypes.c_void_p]

    # hid_write(device, data, length) → int
    lib.hid_write.restype  = ctypes.c_int
    lib.hid_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]

    lib.hid_init()
    return lib


def open_wheel(lib: ctypes.CDLL):
    """Open the HID collection that accepts Logitech RPM LED reports.

    The G923 PlayStation wheel exposes vendor collections plus a joystick
    collection.  The latter (usage page 0x01, usage 0x04 / MI_00) is the one
    proven to accept the F8 12 LED output report.  Retain the old VID/PID path
    as a fallback for other supported wheels.
    """
    for pid in WHEEL_PIDS:
        devices = lib.hid_enumerate(LOGITECH_VID, pid)
        node = devices
        try:
            while node:
                info = node.contents
                if info.usage_page == LED_USAGE_PAGE and info.usage == LED_USAGE:
                    handle = lib.hid_open_path(info.path)
                    if handle:
                        return handle
                node = info.next
        finally:
            if devices:
                lib.hid_free_enumeration(devices)

        # Backward-compatible fallback for older wheel variants.
        handle = lib.hid_open(LOGITECH_VID, pid, None)
        if handle:
            return handle
    return None


def _send_led_report(lib: ctypes.CDLL, handle, bitmask: int) -> None:
    """Write the 8-byte LED control output report (report-ID 0x00 + 7 bytes)."""
    report = bytes([0x00, 0xF8, 0x12, bitmask & 0xFF, 0x00, 0x00, 0x00, 0x00])
    lib.hid_write(handle, report, len(report))


# ---------------------------------------------------------------------------
# RPM → LED BITMASK
# ---------------------------------------------------------------------------

def rpm_to_bitmask(current_rpm: float, min_rpm: float, max_rpm: float) -> int:
    """
    Convert RPM to a 5-bit LED bitmask.
    LEDs light progressively from left (bit 0) to right (bit 4).
    """
    if max_rpm <= min_rpm:
        return ALL_LEDS_OFF
    if current_rpm <= min_rpm:
        return ALL_LEDS_OFF
    if current_rpm >= max_rpm:
        return ALL_LEDS_ON
    ratio = (current_rpm - min_rpm) / (max_rpm - min_rpm)
    n_lit = max(1, round(ratio * NUM_LEDS))
    return (1 << n_lit) - 1


# ---------------------------------------------------------------------------
# FORZA PACKET PARSING
# ---------------------------------------------------------------------------

DASH_FORMAT = (
    "<iI"        # [0]  IsRaceOn (s32), TimestampMS (u32)
    "fff"        # [2]  EngineMaxRpm, EngineIdleRpm, CurrentEngineRpm
    "fff"        # [5]  AccelerationX/Y/Z
    "fff"        # [8]  VelocityX/Y/Z
    "fff"        # [11] AngularVelocityX/Y/Z
    "fff"        # [14] Yaw, Pitch, Roll
    "ffff"       # [17] NormalizedSuspensionTravel FL/FR/RL/RR
    "ffff"       # [21] TireSlipRatio FL/FR/RL/RR
    "ffff"       # [25] WheelRotationSpeed FL/FR/RL/RR
    "iiii"       # [29] WheelOnRumbleStrip FL/FR/RL/RR
    "ffff"       # [33] WheelInPuddleDepth FL/FR/RL/RR
    "ffff"       # [37] SurfaceRumble FL/FR/RL/RR
    "ffff"       # [41] TireSlipAngle FL/FR/RL/RR
    "ffff"       # [45] TireCombinedSlip FL/FR/RL/RR
    "ffff"       # [49] SuspensionTravelMeters FL/FR/RL/RR
    "iiii"       # [53] CarOrdinal, CarClass, CarPerformanceIndex, DrivetrainType
    "i"          # [57] NumCylinders
    "fff"        # [58] PositionX/Y/Z
    "fff"        # [61] Speed, Power, Torque
    "ffff"       # [64] TireTemp FL/FR/RL/RR
    "fff"        # [68] Boost, Fuel, DistanceTraveled
    "fff"        # [71] BestLap, LastLap, CurrentLap
    "f"          # [74] CurrentRaceTime
    "H"          # [75] LapNumber (u16)
    "B"          # [76] RacePosition (u8)
    "BBBBB"      # [77] Accel, Brake, Clutch, HandBrake, Gear (u8)
    "bbb"        # [82] Steer, NormalizedDrivingLine, NormalizedAIBrakeDifference (s8)
)

IDX_IS_RACE_ON      = 0
IDX_ENGINE_MAX_RPM  = 2
IDX_ENGINE_IDLE_RPM = 3
IDX_CURRENT_RPM     = 4
IDX_CAR_ORDINAL     = 53
IDX_ACCEL           = 77
IDX_GEAR            = 81

SIZE_FH5_FH6  = 323
SIZE_FH5_FH6B = 324   # FH5 variant (+1 byte at end, same structure)
SIZE_FM2023   = 331

GAME_LABELS = {
    SIZE_FH5_FH6:  "FH5 / FH6",
    SIZE_FH5_FH6B: "FH5 / FH6",
    SIZE_FM2023:   "FM2023",
}


def patch_and_parse(data: bytes):
    """
    Remove the 12-byte FH4/FH5/FH6 gap (bytes 232–243), unpack the struct.
    Returns None if the packet size is not recognised.
    """
    size = len(data)
    if size not in (SIZE_FH5_FH6, SIZE_FH5_FH6B, SIZE_FM2023):
        return None

    patched = data[:232] + data[244:323]

    try:
        vals = struct.unpack_from(DASH_FORMAT, patched)
    except struct.error:
        return None

    return {
        "game":          GAME_LABELS[size],
        "is_race_on":    bool(vals[IDX_IS_RACE_ON]),
        "current_rpm":   float(vals[IDX_CURRENT_RPM]),
        "max_rpm":       float(vals[IDX_ENGINE_MAX_RPM]),
        "idle_rpm":      float(vals[IDX_ENGINE_IDLE_RPM]),
        "car_ordinal":   int(vals[IDX_CAR_ORDINAL]),
        "accel":         int(vals[IDX_ACCEL]),
        "gear":          int(vals[IDX_GEAR]),
    }


# ---------------------------------------------------------------------------
# LED STATE LOGIC  (pure — no side effects, fully testable)
# ---------------------------------------------------------------------------

LED_OFF       = "off"
LED_NORMAL    = "normal"
LED_BLINK_ON  = "blink_on"
LED_BLINK_OFF = "blink_off"


def compute_led_state(
    current_rpm: float,
    max_rpm: float,
    blink_phase: bool,
    last_blink: float,
    now: float,
    blink_thresh: float,
    blink_interval: float,
) -> tuple:
    if current_rpm >= blink_thresh:
        if now - last_blink >= blink_interval:
            blink_phase = not blink_phase
            last_blink  = now
        action = LED_BLINK_ON if blink_phase else LED_BLINK_OFF
    else:
        blink_phase = False
        action      = LED_NORMAL

    return action, blink_phase, last_blink


# ---------------------------------------------------------------------------
# HID LED APPLICATION
# ---------------------------------------------------------------------------

def apply_led_action(lib: ctypes.CDLL, handle, action: str,
                     current_rpm: float, min_rpm: float, max_rpm: float) -> None:
    if action == LED_OFF or action == LED_BLINK_OFF:
        _send_led_report(lib, handle, ALL_LEDS_OFF)
    elif action == LED_BLINK_ON:
        _send_led_report(lib, handle, ALL_LEDS_ON)
    else:  # LED_NORMAL
        _send_led_report(lib, handle, rpm_to_bitmask(current_rpm, min_rpm, max_rpm))


# ---------------------------------------------------------------------------
# UDP FORWARDER
# ---------------------------------------------------------------------------

def forward_packet(sock: socket.socket, data: bytes,
                   targets: list) -> None:
    """Rebroadcast raw UDP packet to every (host, port) in targets."""
    for host, port in targets:
        try:
            sock.sendto(data, (host, port))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# DYNAMIC REDLINE DETECTION
# ---------------------------------------------------------------------------

class RedlineDetector:
    """
    Detects the actual rev limiter by observing RPM behavior.
    The user is expected to pin the throttle in a low gear until it bounces.
    """
    def __init__(self, cached_cars=None):
        self.car_data = {} # car_ordinal -> {max_seen, bounces, is_locked, last_rpm, nominal_max_rpm}
        self.cached_cars = cached_cars or {}

    def get_limiter(self, car_ordinal, current_rpm, accel, game_max_rpm):
        if car_ordinal not in self.car_data:
            if car_ordinal in self.cached_cars and self.cached_cars[car_ordinal].get("redline", 0) > 0:
                c = self.cached_cars[car_ordinal]
                self.car_data[car_ordinal] = {
                    "max_seen": c["redline"],
                    "bounces": 3,
                    "is_locked": True,
                    "last_rpm": 0.0,
                    "nominal_max_rpm": c.get("nominal_max_rpm", game_max_rpm)
                }
            else:
                self.car_data[car_ordinal] = {
                    "max_seen": 0.0,
                    "bounces": 0,
                    "is_locked": False,
                    "last_rpm": 0.0,
                    "nominal_max_rpm": game_max_rpm
                }
        
        d = self.car_data[car_ordinal]

        # Ensure nominal_max_rpm is populated if it was 0
        if d["nominal_max_rpm"] <= 0 and game_max_rpm > 0:
            d["nominal_max_rpm"] = game_max_rpm

        # 1. Check for material nominal limit change (engine swap/upgrade)
        if game_max_rpm > 0 and d["nominal_max_rpm"] > 0:
            if abs(game_max_rpm - d["nominal_max_rpm"]) > 250:
                self.reset(car_ordinal)
                self.car_data[car_ordinal] = {
                    "max_seen": 0.0,
                    "bounces": 0,
                    "is_locked": False,
                    "last_rpm": 0.0,
                    "nominal_max_rpm": game_max_rpm
                }
                return game_max_rpm

        # If already locked, just return it
        if d["is_locked"]:
            return d["max_seen"]

        # Only calibrate if throttle is pinned (>= 250/255)
        if accel < 250:
            if not d["is_locked"]:
                d["max_seen"] = 0.0
                d["bounces"] = 0
            d["last_rpm"] = current_rpm
            return game_max_rpm

        if current_rpm > d["max_seen"]:
            d["max_seen"] = current_rpm
            d["bounces"] = 0 # reset bounce counter when we find a new peak
        
        # Detect bounce: RPM was very high, but suddenly dropped while pinning throttle
        if d["last_rpm"] > d["max_seen"] * 0.98 and current_rpm < d["last_rpm"] - 40:
            d["bounces"] += 1
            if d["bounces"] >= 3:
                d["is_locked"] = True
        
        d["last_rpm"] = current_rpm
        return d["max_seen"] if d["is_locked"] else game_max_rpm

    def is_locked(self, car_ordinal):
        return self.car_data.get(car_ordinal, {}).get("is_locked", False)

    def reset(self, car_ordinal):
        if car_ordinal in self.car_data:
            del self.car_data[car_ordinal]
        if car_ordinal in self.cached_cars:
            self.cached_cars[car_ordinal]["redline"] = 0.0


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    # Run GUI by default unless --cli is specified, or we are running unit tests
    is_testing = any(x in sys.modules for x in ("pytest", "unittest"))
    if "--cli" not in sys.argv and not is_testing:  # pragma: no cover
        import forza_wheel_leds_gui
        forza_wheel_leds_gui.run_gui()
        return

    # --- Load config ---
    cfg_path = _config_path()
    cfg = load_config(cfg_path)

    udp_port          = cfg["udp_port"]
    led_min_rpm_ratio = cfg["led_min_rpm_ratio"]
    blink_offset_low_gear_rpm = cfg["blink_offset_low_gear_rpm"]
    blink_offset_high_gear_rpm = cfg["blink_offset_high_gear_rpm"]
    use_auto_redline  = cfg["use_auto_redline"]
    blink_hz          = cfg["blink_hz"]
    blink_interval    = 1.0 / blink_hz
    forward_targets   = cfg["forward_targets"]

    if "cars" not in cfg:
        cfg["cars"] = {}
    cfg_source = "config.ini" if os.path.exists(cfg_path) else "defaults"
    detector = RedlineDetector(cfg["cars"])

    print("=" * 58)
    print("  forza-wheel-leds  |  Logitech G29 / G920 / G923 RPM LEDs")
    print("=" * 58)
    print(f"  Version        : 1.5.0")
    print(f"  Config         : {cfg_source}")
    print(f"  Listening on   : {UDP_IP}:{udp_port}")
    print(f"  LED min RPM    : {int(led_min_rpm_ratio * 100)} % of limiter")
    print(f"  Blink offset   : {blink_offset_low_gear_rpm} (gears 1-3) / {blink_offset_high_gear_rpm} (gears 4+) RPM before limiter  ({blink_hz:.0f} Hz)")
    print("-" * 58)
    print("  Live Controls:")
    print("    UP / DOWN    : Adjust LED min %")
    print("    LEFT / RIGHT : Adjust Blink offset RPM (Both)")
    print("    R            : Reset redline for current car")
    print("    S            : Save current settings to config.ini")
    print("=" * 58)
    print()

    # --- Load hidapi.dll ---
    try:
        lib = load_hidapi()
    except OSError as exc:
        print(f"[ERROR] {exc}")
        print()
        print("        The .exe release bundles hidapi.dll automatically.")
        print("        If running the .py script, place hidapi.dll next to it.")
        print("        Download from: https://github.com/libusb/hidapi/releases")
        print()
        input("  Press Enter to close this window \u2026")
        sys.exit(1)

    # --- Open wheel ---
    handle = open_wheel(lib)
    if handle is None:
        print("[WARN] No supported Logitech wheel detected (G29 / G920 / G923).")
        print("       Make sure the wheel is plugged in via USB.")
        print("       LEDs will activate once the wheel is connected.")
        print()
    else:
        print("[OK]   Logitech wheel connected via USB HID.")
        print()

    # --- UDP socket ---
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, udp_port))
    sock.settimeout(0.1) # shorter timeout for more responsive keyboard handling

    print(f"[OK]   UDP socket bound to {UDP_IP}:{udp_port}")
    print()
    print("  Waiting for Forza telemetry \u2026")
    print()

    last_game   = ""
    blink_phase = False
    last_blink  = 0.0
    key_signal  = ""
    active_car_ordinal = None
    active_nominal_max = 0.0

    try:
        while True:
            # --- Handle Keyboard Input ---
            while msvcrt.kbhit():
                raw = msvcrt.getch()
                # print(f"\n[DEBUG] Key: {repr(raw)}") # Uncomment to see raw codes
                
                # Identify key robustly
                if isinstance(raw, bytes) and raw in (b'\x00', b'\xe0'):
                    suffix = msvcrt.getch()
                    # print(f"[DEBUG] Suffix: {repr(suffix)}")
                    if suffix.lower() == b'h': key_signal = "up"
                    elif suffix.lower() == b'p': key_signal = "down"
                    elif suffix.lower() == b'k': key_signal = "left"
                    elif suffix.lower() == b'm': key_signal = "right"
                else:
                    try:
                        # Decode if bytes, otherwise handle as string
                        char = raw.decode('ascii', errors='ignore').lower() if isinstance(raw, bytes) else str(raw).lower()
                        if char == 'r': key_signal = "reset"
                        elif char == 's': key_signal = "save"
                        elif char == 'q': raise KeyboardInterrupt
                    except:
                        continue

                # Apply actions
                if key_signal == "up":
                    led_min_rpm_ratio = round(min(0.95, led_min_rpm_ratio + 0.05), 2)
                    print(f"\n[INFO] LED Min: {int(led_min_rpm_ratio*100)}%")
                    key_signal = ""
                elif key_signal == "down":
                    led_min_rpm_ratio = round(max(0.1, led_min_rpm_ratio - 0.05), 2)
                    print(f"\n[INFO] LED Min: {int(led_min_rpm_ratio*100)}%")
                    key_signal = ""
                elif key_signal == "right":
                    blink_offset_low_gear_rpm = min(2000, blink_offset_low_gear_rpm + 50)
                    blink_offset_high_gear_rpm = min(2000, blink_offset_high_gear_rpm + 50)
                    print(f"\n[INFO] Blink Offset: -{blink_offset_low_gear_rpm} (low) / -{blink_offset_high_gear_rpm} (high) RPM")
                    key_signal = ""
                elif key_signal == "left":
                    blink_offset_low_gear_rpm = max(0, blink_offset_low_gear_rpm - 50)
                    blink_offset_high_gear_rpm = max(0, blink_offset_high_gear_rpm - 50)
                    print(f"\n[INFO] Blink Offset: -{blink_offset_low_gear_rpm} (low) / -{blink_offset_high_gear_rpm} (high) RPM")
                    key_signal = ""
                elif key_signal == "save":
                    cfg["led_min_rpm_ratio"] = led_min_rpm_ratio
                    cfg["blink_offset_low_gear_rpm"] = blink_offset_low_gear_rpm
                    cfg["blink_offset_high_gear_rpm"] = blink_offset_high_gear_rpm
                    if active_car_ordinal is not None and detector.is_locked(active_car_ordinal):
                        cfg["cars"][active_car_ordinal] = {
                            "redline": detector.car_data[active_car_ordinal]["max_seen"],
                            "nominal_max_rpm": active_nominal_max,
                            "led_min_rpm_ratio": led_min_rpm_ratio,
                            "blink_offset_low_gear_rpm": blink_offset_low_gear_rpm,
                            "blink_offset_high_gear_rpm": blink_offset_high_gear_rpm,
                            "blink_hz": blink_hz
                        }
                    save_config(cfg_path, cfg)
                    print(f"\n[OK]   Settings saved to {CONFIG_FILENAME}")
                    key_signal = ""

            try:
                # Use a slightly longer timeout if no packets are coming,
                # but keep it short enough for UI responsiveness.
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue

            if forward_targets:
                forward_packet(sock, data, forward_targets)

            packet = patch_and_parse(data)
            if packet is None:
                continue

            if packet["car_ordinal"] > 0 and packet["car_ordinal"] != active_car_ordinal:
                active_car_ordinal = packet["car_ordinal"]
                cars = cfg.get("cars", {})
                if active_car_ordinal in cars:
                    c = cars[active_car_ordinal]
                    led_min_rpm_ratio = c.get("led_min_rpm_ratio", cfg["led_min_rpm_ratio"])
                    blink_offset_low_gear_rpm = c.get("blink_offset_low_gear_rpm", cfg["blink_offset_low_gear_rpm"])
                    blink_offset_high_gear_rpm = c.get("blink_offset_high_gear_rpm", cfg["blink_offset_high_gear_rpm"])
                    blink_hz = c.get("blink_hz", cfg["blink_hz"])
                    print(f"\n[INFO] Loaded settings for car {active_car_ordinal}: Min LED {int(led_min_rpm_ratio*100)}%, Blink Offset -{blink_offset_low_gear_rpm}/-{blink_offset_high_gear_rpm} RPM, Blink Hz {blink_hz}")
                else:
                    led_min_rpm_ratio = cfg["led_min_rpm_ratio"]
                    blink_offset_low_gear_rpm = cfg["blink_offset_low_gear_rpm"]
                    blink_offset_high_gear_rpm = cfg["blink_offset_high_gear_rpm"]
                    blink_hz = cfg["blink_hz"]
                blink_interval = 1.0 / blink_hz

            active_nominal_max = packet["max_rpm"]

            if packet["game"] != last_game:
                print(f"\n[INFO] Game detected: {packet['game']}")
                last_game = packet["game"]

            if handle is None:
                handle = open_wheel(lib)
                if handle is not None:
                    print("\n[OK]   Logitech wheel connected via USB HID.")

            # Process 'reset' signal once car_ordinal is known
            if key_signal == "reset":
                detector.reset(active_car_ordinal)
                print(f"\n[INFO] Redline reset for car {active_car_ordinal}")
                key_signal = ""

            if not packet["is_race_on"] or packet["max_rpm"] <= 0:
                if handle is not None:
                    apply_led_action(lib, handle, LED_OFF, 0, 0, 0)
                print("  In menu \u2014 LEDs off \u2026                   ", end="\r")
                continue

            # Redline logic
            limiter = packet["max_rpm"]
            calib_str = ""
            if use_auto_redline:
                was_locked = packet["car_ordinal"] in cfg.get("cars", {})
                limiter = detector.get_limiter(packet["car_ordinal"], packet["current_rpm"], 
                                             packet["accel"], packet["max_rpm"])
                is_locked = detector.is_locked(packet["car_ordinal"])
                if is_locked:
                    calib_str = "[CALIBRATED]"
                    current_cached = cfg.get("cars", {}).get(packet["car_ordinal"], {})
                    if not was_locked or abs(current_cached.get("redline", 0.0) - limiter) > 1.0:
                        cfg["cars"][packet["car_ordinal"]] = {
                            "redline": limiter,
                            "nominal_max_rpm": packet["max_rpm"],
                            "led_min_rpm_ratio": led_min_rpm_ratio,
                            "blink_offset_low_gear_rpm": blink_offset_low_gear_rpm,
                            "blink_offset_high_gear_rpm": blink_offset_high_gear_rpm,
                            "blink_hz": blink_hz
                        }
                        save_config(cfg_path, cfg)
                        print(f"\n[OK]   Auto-saved calibration for car {packet['car_ordinal']} to config.ini")
                else:
                    calib_str = "[CALIBRATING...]"

            min_rpm      = limiter * led_min_rpm_ratio
            active_offset = blink_offset_low_gear_rpm if packet["gear"] <= 3 else blink_offset_high_gear_rpm
            blink_thresh = max(min_rpm + 100, limiter - active_offset)

            action, blink_phase, last_blink = compute_led_state(
                current_rpm    = packet["current_rpm"],
                max_rpm        = limiter,
                blink_phase    = blink_phase,
                last_blink     = last_blink,
                now            = time.time(),
                blink_thresh   = blink_thresh,
                blink_interval = blink_interval,
            )

            if handle is not None:
                apply_led_action(lib, handle, action,
                                 packet["current_rpm"], min_rpm, limiter)

            blink_msg = " *** REDLINE ***" if action in (LED_BLINK_ON, LED_BLINK_OFF) else ""
            gear_str  = "R" if packet["gear"] == 0 else str(packet["gear"])
            
            # Update live info line
            settings_str = f"Min {int(led_min_rpm_ratio*100)}% | Blink -{blink_offset_low_gear_rpm}/-{blink_offset_high_gear_rpm}"
            print(
                f"  RPM {packet['current_rpm']:6.0f} / {limiter:5.0f} {calib_str}"
                f" | Gear {gear_str}"
                f" | {settings_str}{blink_msg}    ",
                end="\r",
            )



    except KeyboardInterrupt:
        print("\n[INFO] Shutting down …")
    finally:
        try:
            if handle is not None:
                _send_led_report(lib, handle, ALL_LEDS_OFF)
                lib.hid_close(handle)
            lib.hid_exit()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        print("[INFO] LEDs off. Socket closed.")
        print()
        input("  Press Enter to close this window …")


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except Exception as exc:
        print(f"\n[FATAL] Unexpected error: {exc}")
        import traceback
        traceback.print_exc()
        print()
        input("  Press Enter to close this window …")
