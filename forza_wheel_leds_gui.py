"""
forza_wheel_leds_gui.py
-------------------------
A sleek, modern Tkinter GUI for forza-wheel-leds.
Features real-time LED simulator canvas, configuration sliders,
blink speed selector (10Hz / 15Hz), auto-redline toggle,
wheel test sweep routine, and a thread-safe telemetry listener.
"""

import configparser
import ctypes
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import forza_wheel_leds as fwl

# ---------------------------------------------------------------------------
# COLOR PALETTE (Dark Mode Aesthetics)
# ---------------------------------------------------------------------------
BG_DARK = "#18181b"       # Zinc-900 (deep dark background)
BG_CARD = "#27272a"       # Zinc-800 (card/frame background)
BG_ENTRY = "#3f3f46"      # Zinc-700 (inputs)
FG_LIGHT = "#f4f4f5"      # Zinc-100 (primary text)
FG_MUTED = "#a1a1aa"      # Zinc-400 (secondary text)
ACCENT_BLUE = "#3b82f6"   # Blue-500 (interactive highlight)
ACCENT_BLUE_HOVER = "#60a5fa"
ACCENT_GREEN = "#10b981"  # Emerald-500 (running / positive state)
ACCENT_RED = "#ef4444"    # Red-500 (stopped / alert state)

LED_OFF_COLOR = "#3f3f46" # Zinc-700 (unlit LED)
LED_ON_GREEN = "#10b981"  # Emerald-500
LED_ON_YELLOW = "#f59e0b" # Amber-500
LED_ON_RED = "#ef4444"    # Red-500

# ---------------------------------------------------------------------------
# TELEMETRY WORKER THREAD
# ---------------------------------------------------------------------------
class TelemetryWorker(threading.Thread):
    def __init__(self, gui_queue: queue.Queue, initial_settings: dict):
        super().__init__()
        self.gui_queue = gui_queue
        
        # Thread safety settings
        self.lock = threading.Lock()
        self.settings = initial_settings.copy()
        
        self.stop_event = threading.Event()
        self.test_requested = threading.Event()
        self.reset_car_ordinal = None
        
        self.daemon = True  # Ensure thread dies if main GUI exits

    def update_settings(self, new_settings: dict):
        with self.lock:
            self.settings.update(new_settings)

    def request_reset(self, car_ordinal: int):
        with self.lock:
            self.reset_car_ordinal = car_ordinal

    def trigger_test_sweep(self):
        self.test_requested.set()

    def run(self):
        # 1. Load HIDAPI DLL
        try:
            lib = fwl.load_hidapi()
        except OSError as exc:
            self.gui_queue.put({"type": "error", "message": f"DLL load failed: {exc}"})
            return

        # 2. Open Logitech wheel
        handle = fwl.open_wheel(lib)
        self.gui_queue.put({
            "type": "wheel_status",
            "connected": handle is not None
        })

        # 3. Socket configuration
        sock = None
        current_port = None
        
        # Detector for auto-redline
        self.detector = fwl.RedlineDetector(self.settings.get("cars", {}))
        
        # Telemetry loop state
        last_game = ""
        blink_phase = False
        last_blink = 0.0
        last_packet_time = 0.0
        idle_update_tick = 0.0
        active_car_ordinal = None

        try:
            while not self.stop_event.is_set():
                # Read local thread-safe copy of settings
                with self.lock:
                    udp_port = self.settings["udp_port"]
                    led_min_rpm_ratio = self.settings["led_min_rpm_ratio"]
                    blink_offset_low_gear_rpm = self.settings["blink_offset_low_gear_rpm"]
                    blink_offset_high_gear_rpm = self.settings["blink_offset_high_gear_rpm"]
                    use_auto_redline = self.settings["use_auto_redline"]
                    blink_hz = self.settings["blink_hz"]
                    forward_targets = self.settings["forward_targets"]
                    cars_cache = self.settings.get("cars", {}).copy()
                    self.detector.cached_cars = self.settings.get("cars", {})
                    
                    reset_car = self.reset_car_ordinal
                    self.reset_car_ordinal = None

                blink_interval = 1.0 / blink_hz

                # Check if port changed
                if sock is None or current_port != udp_port:
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass
                    
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    try:
                        sock.bind(("0.0.0.0", udp_port))
                        sock.settimeout(0.05)
                        current_port = udp_port
                        self.gui_queue.put({
                            "type": "socket_status",
                            "status": "listening",
                            "port": udp_port
                        })
                    except OSError as exc:
                        self.gui_queue.put({
                            "type": "error",
                            "message": f"Could not bind to port {udp_port}: {exc}"
                        })
                        sock = None
                        current_port = None
                        # Stop the worker thread to let user fix it
                        break

                # Check if wheel handle is disconnected and try to reconnect
                if handle is None:
                    handle = fwl.open_wheel(lib)
                    if handle is not None:
                        self.gui_queue.put({"type": "wheel_status", "connected": True})

                # Check if test requested
                if self.test_requested.is_set():
                    if handle is not None:
                        self.gui_queue.put({"type": "status_text", "text": "Testing LEDs..."})
                        self._play_test_sweep(lib, handle)
                    self.test_requested.clear()
                    self.gui_queue.put({"type": "status_text", "text": "Test completed"})

                # Read telemetry package
                data = None
                if sock is not None:
                    try:
                        data, addr = sock.recvfrom(2048)
                    except socket.timeout:
                        pass
                    except OSError:
                        # Socket might have closed on port rebind
                        pass

                now = time.time()

                if data:
                    if forward_targets:
                        fwl.forward_packet(sock, data, forward_targets)

                    packet = fwl.patch_and_parse(data)
                    if packet is not None:
                        last_packet_time = now
                        
                        # Reset calibration if requested
                        if reset_car is not None:
                            self.detector.reset(active_car_ordinal)
                            self.gui_queue.put({
                                "type": "status_text", 
                                "text": f"Redline reset for car {active_car_ordinal}"
                            })

                        # Check for car change
                        if packet["car_ordinal"] > 0 and packet["car_ordinal"] != active_car_ordinal:
                            active_car_ordinal = packet["car_ordinal"]
                            self.gui_queue.put({
                                "type": "car_changed",
                                "car_ordinal": active_car_ordinal
                            })

                        # Check if game menu or paused
                        in_menu = not packet["is_race_on"] or packet["max_rpm"] <= 0
                        if in_menu:
                            if handle is not None:
                                fwl.apply_led_action(lib, handle, fwl.LED_OFF, 0, 0, 0)
                            self.gui_queue.put({
                                "type": "telemetry",
                                "game": packet["game"],
                                "current_rpm": 0.0,
                                "max_rpm": 0.0,
                                "gear": 0,
                                "car_ordinal": active_car_ordinal,
                                "calib_status": "In Menu",
                                "led_mask": 0,
                                "in_menu": True
                            })
                            continue

                        # Redline logic
                        limiter = packet["max_rpm"]
                        calib_status = "Disabled"
                        
                        if use_auto_redline:
                            was_locked = packet["car_ordinal"] in cars_cache
                            limiter = self.detector.get_limiter(
                                packet["car_ordinal"], packet["current_rpm"], 
                                packet["accel"], packet["max_rpm"]
                            )
                            is_locked = self.detector.is_locked(packet["car_ordinal"])
                            if is_locked:
                                calib_status = "Calibrated"
                                current_cached = cars_cache.get(packet["car_ordinal"], {})
                                if not was_locked or abs(current_cached.get("redline", 0.0) - limiter) > 1.0:
                                    self.gui_queue.put({
                                        "type": "auto_save_calibration",
                                        "car_ordinal": packet["car_ordinal"],
                                        "redline": limiter,
                                        "nominal_max_rpm": packet["max_rpm"]
                                    })
                            else:
                                calib_status = "Calibrating..."

                        if packet["game"] != last_game:
                            last_game = packet["game"]

                        min_rpm = limiter * led_min_rpm_ratio
                        active_offset = blink_offset_low_gear_rpm if packet["gear"] <= 3 else blink_offset_high_gear_rpm
                        blink_thresh = max(min_rpm + 100, limiter - active_offset)

                        action, blink_phase, last_blink = fwl.compute_led_state(
                            current_rpm=packet["current_rpm"],
                            max_rpm=limiter,
                            blink_phase=blink_phase,
                            last_blink=last_blink,
                            now=now,
                            blink_thresh=blink_thresh,
                            blink_interval=blink_interval,
                        )

                        # Compute LED mask to show in GUI
                        led_mask = fwl.ALL_LEDS_OFF
                        if action == fwl.LED_BLINK_ON:
                            led_mask = fwl.ALL_LEDS_ON
                        elif action == fwl.LED_NORMAL:
                            led_mask = fwl.rpm_to_bitmask(packet["current_rpm"], min_rpm, limiter)

                        # Write to G29 wheel
                        if handle is not None:
                            try:
                                fwl.apply_led_action(lib, handle, action,
                                                     packet["current_rpm"], min_rpm, limiter)
                            except OSError:
                                # Connection lost
                                handle = None
                                self.gui_queue.put({"type": "wheel_status", "connected": False})

                        # Update GUI
                        self.gui_queue.put({
                            "type": "telemetry",
                            "game": packet["game"],
                            "current_rpm": packet["current_rpm"],
                            "max_rpm": limiter,
                            "gear": packet["gear"],
                            "car_ordinal": packet["car_ordinal"],
                            "calib_status": calib_status,
                            "led_mask": led_mask,
                            "in_menu": False
                        })
                else:
                    # No telemetry packet received
                    # If idle for more than 2 seconds, shut down LEDs
                    if now - last_packet_time > 2.0:
                        if handle is not None:
                            fwl.apply_led_action(lib, handle, fwl.LED_OFF, 0, 0, 0)
                        
                        if now - idle_update_tick > 0.5:
                            idle_update_tick = now
                            self.gui_queue.put({
                                "type": "telemetry_idle",
                                "wheel_connected": handle is not None
                            })

            # End of loop
        finally:
            # Clean up
            try:
                if handle is not None:
                    fwl._send_led_report(lib, handle, fwl.ALL_LEDS_OFF)
                    lib.hid_close(handle)
                lib.hid_exit()
            except Exception:
                pass
            
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
                
            self.gui_queue.put({
                "type": "socket_status",
                "status": "stopped",
                "port": udp_port
            })

    def _play_test_sweep(self, lib, handle):
        """Play a smooth left-to-right sweep, then blink all 3 times, then off."""
        # 1. Progressive sweep
        steps = [0x01, 0x03, 0x07, 0x0F, 0x1F]
        for val in steps:
            if self.stop_event.is_set():
                return
            fwl._send_led_report(lib, handle, val)
            time.sleep(0.08)

        # 2. Blink 3 times
        for _ in range(3):
            if self.stop_event.is_set():
                return
            fwl._send_led_report(lib, handle, fwl.ALL_LEDS_OFF)
            time.sleep(0.15)
            if self.stop_event.is_set():
                return
            fwl._send_led_report(lib, handle, fwl.ALL_LEDS_ON)
            time.sleep(0.15)

        # 3. Off
        fwl._send_led_report(lib, handle, fwl.ALL_LEDS_OFF)


# ---------------------------------------------------------------------------
# MAIN TKINTER GUI CLASS
# ---------------------------------------------------------------------------
class ForzaLEDsGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("forza-wheel-leds Configuration")
        self.root.geometry("620x580")
        self.root.resizable(False, False)
        
        # Load configuration
        self.cfg_path = fwl._config_path()
        self.settings = fwl.load_config(self.cfg_path)
        if "cars" not in self.settings:
            self.settings["cars"] = {}
        
        # Make sure default blink_hz is either 10.0 or 15.0
        if self.settings["blink_hz"] not in (10.0, 15.0):
            self.settings["blink_hz"] = 10.0

        # Background Thread fields
        self.worker_thread = None
        self.gui_queue = queue.Queue()
        self.wheel_connected = False
        
        # Current telemetry state
        self.car_ordinal = 0
        self.is_running = False

        self._configure_styles()
        self._build_ui()
        
        # Load initially configured settings into variables
        self.port_var.set(str(self.settings["udp_port"]))
        self.ratio_var.set(self.settings["led_min_rpm_ratio"])
        self.ratio_lbl.config(text=f"{int(self.settings['led_min_rpm_ratio'] * 100)} %")
        self.offset_low_var.set(self.settings["blink_offset_low_gear_rpm"])
        self.offset_low_lbl.config(text=f"{self.settings['blink_offset_low_gear_rpm']} RPM")
        self.offset_high_var.set(self.settings["blink_offset_high_gear_rpm"])
        self.offset_high_lbl.config(text=f"{self.settings['blink_offset_high_gear_rpm']} RPM")
        self.auto_redline_var.set(self.settings["use_auto_redline"])
        self.blink_speed_var.set("10" if self.settings["blink_hz"] == 10.0 else "15")

        # Start periodic queue polling
        self.root.after(30, self._poll_queue)
        
        # Set protocol for closing window gracefully
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # Auto-start telemetry thread on open
        self._toggle_running()

    def _configure_styles(self):
        self.root.configure(bg=BG_DARK)
        
        # Standard TTK Style configs
        self.style = ttk.Style()
        self.style.theme_use("clam")
        
        # TTK Frames
        self.style.configure("TFrame", background=BG_DARK)
        self.style.configure("Card.TFrame", background=BG_CARD, borderwidth=1, relief="flat")
        
        # TTK Scrollbar/Widgets
        self.style.configure("TLabel", background=BG_DARK, foreground=FG_LIGHT, font=("Segoe UI", 10))
        self.style.configure("Card.TLabel", background=BG_CARD, foreground=FG_LIGHT, font=("Segoe UI", 10))
        self.style.configure("Header.TLabel", background=BG_DARK, foreground=ACCENT_BLUE, font=("Segoe UI", 16, "bold"))
        self.style.configure("Stats.TLabel", background=BG_CARD, foreground=FG_LIGHT, font=("Consolas", 10))
        
        # TTK Radiobutton
        self.style.configure("TRadiobutton", background=BG_CARD, foreground=FG_LIGHT, font=("Segoe UI", 10), focuscolor=BG_CARD)
        self.style.map("TRadiobutton", background=[("active", BG_CARD)], foreground=[("active", ACCENT_BLUE)])

    def _build_ui(self):
        # 1. Header Frame
        header_frame = ttk.Frame(self.root)
        header_frame.pack(fill="x", padx=20, pady=10)
        
        title_lbl = ttk.Label(header_frame, text="forza-wheel-leds", style="Header.TLabel")
        title_lbl.pack(side="left")
        
        ver_lbl = ttk.Label(header_frame, text="v1.5.0", font=("Segoe UI", 8, "italic"), foreground=FG_MUTED)
        ver_lbl.pack(side="left", padx=8, pady=8)

        self.status_bar_lbl = ttk.Label(header_frame, text="Stopped", font=("Segoe UI", 9, "bold"), foreground=ACCENT_RED)
        self.status_bar_lbl.pack(side="right", pady=5)

        # 2. LED Visualizer Canvas
        self.led_canvas = tk.Canvas(self.root, width=580, height=50, bg=BG_DARK, highlightthickness=0)
        self.led_canvas.pack(padx=20, pady=5)
        self.leds = []
        
        # Draw 5 G29 LED indicators
        led_spacing = 35
        start_x = 290 - (2.5 * led_spacing)
        for i in range(5):
            x0 = start_x + (i * led_spacing)
            y0 = 10
            x1 = x0 + 26
            y1 = y0 + 26
            oval_id = self.led_canvas.create_oval(x0, y0, x1, y1, fill=LED_OFF_COLOR, outline="#18181b", width=2)
            self.leds.append(oval_id)
            
        # Draw labels under LEDs
        self.led_canvas.create_text(290, 43, text="STEERING WHEEL LED EMULATOR", font=("Segoe UI", 7, "bold"), fill=FG_MUTED)

        # 3. Main Split Area
        split_frame = ttk.Frame(self.root)
        split_frame.pack(fill="both", expand=True, padx=20, pady=10)

        # 3a. Left Side: Status / Telemetry (Card style)
        telemetry_frame = ttk.Frame(split_frame, style="Card.TFrame")
        telemetry_frame.pack(side="left", fill="both", expand=True, padx=(0, 10))
        
        # Quick hack to get padding inside card frame
        inner_tel = tk.Frame(telemetry_frame, bg=BG_CARD, padx=15, pady=15)
        inner_tel.pack(fill="both", expand=True)

        ttk.Label(inner_tel, text="TELEMETRY DASHBOARD", font=("Segoe UI", 10, "bold"), foreground=ACCENT_BLUE, background=BG_CARD).pack(anchor="w", pady=(0, 15))

        self.lbl_wheel = self._create_dashboard_line(inner_tel, "Logitech Wheel:", "Checking...")
        self.lbl_game = self._create_dashboard_line(inner_tel, "Active Game:", "None")
        self.lbl_car = self._create_dashboard_line(inner_tel, "Car Ordinal:", "0")
        self.lbl_rpm = self._create_dashboard_line(inner_tel, "Engine RPM:", "0 / 0")
        self.lbl_calib = self._create_dashboard_line(inner_tel, "Calibration:", "Idle")
        
        # Gear Display (Big text)
        gear_container = tk.Frame(inner_tel, bg=BG_CARD)
        gear_container.pack(fill="x", pady=(15, 0))
        ttk.Label(gear_container, text="GEAR", font=("Segoe UI", 9, "bold"), foreground=FG_MUTED, background=BG_CARD).pack(side="left")
        self.lbl_gear_val = tk.Label(gear_container, text="-", font=("Consolas", 32, "bold"), fg=ACCENT_BLUE, bg=BG_CARD)
        self.lbl_gear_val.pack(side="right", padx=10)

        # 3b. Right Side: Settings Form
        settings_frame = ttk.Frame(split_frame, style="Card.TFrame")
        settings_frame.pack(side="right", fill="both", expand=True)
        
        inner_set = tk.Frame(settings_frame, bg=BG_CARD, padx=15, pady=15)
        inner_set.pack(fill="both", expand=True)
        
        ttk.Label(inner_set, text="CONFIGURATION", font=("Segoe UI", 10, "bold"), foreground=ACCENT_BLUE, background=BG_CARD).pack(anchor="w", pady=(0, 15))

        # Port Field
        port_row = tk.Frame(inner_set, bg=BG_CARD)
        port_row.pack(fill="x", pady=4)
        ttk.Label(port_row, text="UDP Port:", background=BG_CARD).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_entry = tk.Entry(port_row, textvariable=self.port_var, width=8, bg=BG_ENTRY, fg=FG_LIGHT, insertbackground=FG_LIGHT, relief="flat", font=("Consolas", 10))
        self.port_entry.pack(side="right")

        # Auto-Redline Checkbox
        self.auto_redline_var = tk.BooleanVar()
        self.auto_redline_chk = tk.Checkbutton(
            inner_set, text="Auto-Detect Redline", variable=self.auto_redline_var,
            bg=BG_CARD, fg=FG_LIGHT, selectcolor=BG_DARK, activebackground=BG_CARD,
            activeforeground=FG_LIGHT, font=("Segoe UI", 10), command=self._push_updated_settings
        )
        self.auto_redline_chk.pack(anchor="w", pady=6)

        # Min RPM Ratio Slider
        ratio_row = tk.Frame(inner_set, bg=BG_CARD)
        ratio_row.pack(fill="x", pady=4)
        ttk.Label(ratio_row, text="First LED Min RPM %:", background=BG_CARD).pack(anchor="w")
        
        ratio_sub = tk.Frame(ratio_row, bg=BG_CARD)
        ratio_sub.pack(fill="x")
        self.ratio_var = tk.DoubleVar()
        self.ratio_scale = tk.Scale(
            ratio_sub, from_=0.50, to=0.95, resolution=0.01, orient="horizontal",
            variable=self.ratio_var, showvalue=False, bg=BG_CARD, highlightthickness=0,
            activebackground=ACCENT_BLUE, troughcolor=BG_DARK, command=self._on_ratio_change
        )
        self.ratio_scale.pack(side="left", fill="x", expand=True)
        self.ratio_lbl = tk.Label(ratio_sub, text="65%", font=("Consolas", 10, "bold"), fg=FG_LIGHT, bg=BG_CARD, width=6)
        self.ratio_lbl.pack(side="right")

        # Blink Offset Low Gear RPM Slider
        offset_low_row = tk.Frame(inner_set, bg=BG_CARD)
        offset_low_row.pack(fill="x", pady=4)
        ttk.Label(offset_low_row, text="Blink Offset (Gears 1-3):", background=BG_CARD).pack(anchor="w")
        
        offset_low_sub = tk.Frame(offset_low_row, bg=BG_CARD)
        offset_low_sub.pack(fill="x")
        self.offset_low_var = tk.IntVar()
        self.offset_low_scale = tk.Scale(
            offset_low_sub, from_=0, to=2000, resolution=50, orient="horizontal",
            variable=self.offset_low_var, showvalue=False, bg=BG_CARD, highlightthickness=0,
            activebackground=ACCENT_BLUE, troughcolor=BG_DARK, command=self._on_offset_low_change
        )
        self.offset_low_scale.pack(side="left", fill="x", expand=True)
        self.offset_low_lbl = tk.Label(offset_low_sub, text="750 RPM", font=("Consolas", 10, "bold"), fg=FG_LIGHT, bg=BG_CARD, width=10)
        self.offset_low_lbl.pack(side="right")

        # Blink Offset High Gear RPM Slider
        offset_high_row = tk.Frame(inner_set, bg=BG_CARD)
        offset_high_row.pack(fill="x", pady=4)
        ttk.Label(offset_high_row, text="Blink Offset (Gears 4+):", background=BG_CARD).pack(anchor="w")
        
        offset_high_sub = tk.Frame(offset_high_row, bg=BG_CARD)
        offset_high_sub.pack(fill="x")
        self.offset_high_var = tk.IntVar()
        self.offset_high_scale = tk.Scale(
            offset_high_sub, from_=0, to=2000, resolution=50, orient="horizontal",
            variable=self.offset_high_var, showvalue=False, bg=BG_CARD, highlightthickness=0,
            activebackground=ACCENT_BLUE, troughcolor=BG_DARK, command=self._on_offset_high_change
        )
        self.offset_high_scale.pack(side="left", fill="x", expand=True)
        self.offset_high_lbl = tk.Label(offset_high_sub, text="500 RPM", font=("Consolas", 10, "bold"), fg=FG_LIGHT, bg=BG_CARD, width=10)
        self.offset_high_lbl.pack(side="right")

        # Blink Speed Selection (10 Hz or 15 Hz)
        speed_row = tk.Frame(inner_set, bg=BG_CARD)
        speed_row.pack(fill="x", pady=(8, 4))
        ttk.Label(speed_row, text="Blink Frequency:", background=BG_CARD).pack(side="left")
        
        self.blink_speed_var = tk.StringVar(value="10")
        r1 = ttk.Radiobutton(speed_row, text="10 Hz", variable=self.blink_speed_var, value="10", command=self._push_updated_settings)
        r2 = ttk.Radiobutton(speed_row, text="15 Hz", variable=self.blink_speed_var, value="15", command=self._push_updated_settings)
        r2.pack(side="right", padx=5)
        r1.pack(side="right")

        # 4. Bottom Buttons Frame
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=20, pady=15)

        # Start/Stop Button
        self.btn_run = tk.Button(
            btn_frame, text="Start Listener", bg=ACCENT_GREEN, fg=FG_LIGHT,
            activebackground=ACCENT_GREEN, activeforeground=FG_LIGHT, font=("Segoe UI", 10, "bold"),
            relief="flat", cursor="hand2", padx=15, command=self._toggle_running
        )
        self.btn_run.pack(side="left")

        # Test Button
        self.btn_test = tk.Button(
            btn_frame, text="Test Wheel LEDs", bg=BG_ENTRY, fg=FG_LIGHT,
            activebackground=ACCENT_BLUE, activeforeground=FG_LIGHT, font=("Segoe UI", 10),
            relief="flat", cursor="hand2", padx=12, command=self._trigger_test
        )
        self.btn_test.pack(side="left", padx=10)

        # Reset Redline Button
        self.btn_reset = tk.Button(
            btn_frame, text="Reset Redline", bg=BG_ENTRY, fg=FG_LIGHT,
            activebackground=ACCENT_BLUE, activeforeground=FG_LIGHT, font=("Segoe UI", 10),
            relief="flat", cursor="hand2", padx=12, command=self._reset_redline
        )
        self.btn_reset.pack(side="left")

        # Save Config Button
        self.btn_save = tk.Button(
            btn_frame, text="Save Config", bg=ACCENT_BLUE, fg=FG_LIGHT,
            activebackground=ACCENT_BLUE_HOVER, activeforeground=FG_LIGHT, font=("Segoe UI", 10, "bold"),
            relief="flat", cursor="hand2", padx=15, command=self._save_settings
        )
        self.btn_save.pack(side="right")

    def _create_dashboard_line(self, parent, label_text, initial_value) -> tk.Label:
        row = tk.Frame(parent, bg=BG_CARD)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label_text, background=BG_CARD, foreground=FG_MUTED).pack(side="left")
        lbl_val = tk.Label(row, text=initial_value, font=("Consolas", 10, "bold"), fg=FG_LIGHT, bg=BG_CARD)
        lbl_val.pack(side="right")
        return lbl_val

    def _on_ratio_change(self, val):
        ratio = float(val)
        self.ratio_lbl.config(text=f"{int(ratio * 100)} %")
        self._push_updated_settings()

    def _on_offset_low_change(self, val):
        offset = int(float(val))
        self.offset_low_lbl.config(text=f"{offset} RPM")
        self._push_updated_settings()

    def _on_offset_high_change(self, val):
        offset = int(float(val))
        self.offset_high_lbl.config(text=f"{offset} RPM")
        self._push_updated_settings()

    def _get_form_settings(self) -> dict:
        """Validate form fields and return a settings dict. Returns None on validation failure."""
        try:
            port = int(self.port_var.get())
            if not (1024 <= port <= 65535):
                raise ValueError("Port must be between 1024 and 65535")
        except ValueError as exc:
            messagebox.showerror("Validation Error", f"Invalid UDP port: {exc}\nPlease enter an integer between 1024 and 65535.")
            return None

        hz = float(self.blink_speed_var.get())

        return {
            "udp_port": port,
            "led_min_rpm_ratio": round(self.ratio_var.get(), 2),
            "blink_offset_low_gear_rpm": int(self.offset_low_var.get()),
            "blink_offset_high_gear_rpm": int(self.offset_high_var.get()),
            "use_auto_redline": self.auto_redline_var.get(),
            "blink_hz": hz,
            "forward_targets": self.settings.get("forward_targets", []),
            "cars": self.settings.get("cars", {})
        }

    def _push_updated_settings(self):
        """Silently push settings to running worker thread without saving to file."""
        if not self.is_running or not self.worker_thread:
            return
        
        cfg = self._get_form_settings()
        if cfg:
            self.worker_thread.update_settings(cfg)

    def _toggle_running(self):
        if self.is_running:
            # Stop the worker thread
            self.btn_run.config(state="disabled")
            self._stop_worker()
        else:
            # Start the worker thread
            cfg = self._get_form_settings()
            if not cfg:
                return
            
            self.is_running = True
            self.btn_run.config(text="Stop Listener", bg=ACCENT_RED, activebackground=ACCENT_RED)
            
            self.worker_thread = TelemetryWorker(self.gui_queue, cfg)
            self.worker_thread.start()

    def _stop_worker(self):
        if self.worker_thread:
            self.worker_thread.stop_event.set()
            # We don't join blocking here to keep the GUI responsive.
            # The polling loop will update UI status when the thread actually exits.
            self.status_bar_lbl.config(text="Stopping...", foreground=FG_MUTED)

    def _trigger_test(self):
        if self.is_running and self.worker_thread:
            self.worker_thread.trigger_test_sweep()
        else:
            # If thread not running, run a one-off sweep in a separate GUI thread to not freeze UI
            def run_quick_sweep():
                self.btn_test.config(state="disabled")
                try:
                    lib = fwl.load_hidapi()
                    handle = fwl.open_wheel(lib)
                    if handle is not None:
                        # Sweep sequence
                        steps = [0x01, 0x03, 0x07, 0x0F, 0x1F]
                        for val in steps:
                            fwl._send_led_report(lib, handle, val)
                            time.sleep(0.08)
                        for _ in range(3):
                            fwl._send_led_report(lib, handle, fwl.ALL_LEDS_OFF)
                            time.sleep(0.15)
                            fwl._send_led_report(lib, handle, fwl.ALL_LEDS_ON)
                            time.sleep(0.15)
                        fwl._send_led_report(lib, handle, fwl.ALL_LEDS_OFF)
                        lib.hid_close(handle)
                    else:
                        self.gui_queue.put({"type": "wheel_status_alert", "msg": "No Logitech wheel detected!"})
                    lib.hid_exit()
                except Exception as exc:
                    self.gui_queue.put({"type": "wheel_status_alert", "msg": f"Test failed: {exc}"})
                finally:
                    self.root.after(0, lambda: self.btn_test.config(state="normal"))
            
            threading.Thread(target=run_quick_sweep, daemon=True).start()

    def _reset_redline(self):
        if self.is_running and self.worker_thread:
            self.worker_thread.request_reset(self.car_ordinal)
        else:
            messagebox.showinfo("Reset Calibration", "Telemetry listener is not running.")

    def _save_settings(self):
        cfg = self._get_form_settings()
        if not cfg:
            return
            
        # Update settings dict
        self.settings.update(cfg)
        
        # If running and we are driving a calibrated car, update its overrides too
        if self.is_running and self.car_ordinal > 0:
            if self.worker_thread and self.worker_thread.detector.is_locked(self.car_ordinal):
                d = self.worker_thread.detector.car_data[self.car_ordinal]
                if "cars" not in self.settings:
                    self.settings["cars"] = {}
                self.settings["cars"][self.car_ordinal] = {
                    "redline": d["max_seen"],
                    "nominal_max_rpm": d["nominal_max_rpm"],
                    "led_min_rpm_ratio": cfg["led_min_rpm_ratio"],
                    "blink_offset_low_gear_rpm": cfg["blink_offset_low_gear_rpm"],
                    "blink_offset_high_gear_rpm": cfg["blink_offset_high_gear_rpm"],
                    "blink_hz": cfg["blink_hz"]
                }

        # Save to file
        fwl.save_config(self.cfg_path, self.settings)
        self._push_updated_settings()
        messagebox.showinfo("Config Saved", f"Settings successfully saved to:\n{os.path.basename(self.cfg_path)}")

    def _update_led_display(self, bitmask: int):
        """Update colors of the 5 circles on the emulator canvas."""
        for i in range(5):
            lit = bool(bitmask & (1 << i))
            if lit:
                if i < 2:
                    color = LED_ON_GREEN
                elif i < 4:
                    color = LED_ON_YELLOW
                else:
                    color = LED_ON_RED
            else:
                color = LED_OFF_COLOR
            self.led_canvas.itemconfig(self.leds[i], fill=color)

    def _poll_queue(self):
        """Drain the queue of messages from the background thread and update widgets."""
        while True:
            try:
                msg = self.gui_queue.get_nowait()
            except queue.Empty:
                break

            msg_type = msg.get("type")
            
            if msg_type == "telemetry":
                self.car_ordinal = msg["car_ordinal"]
                self.lbl_game.config(text=msg["game"])
                self.lbl_car.config(text=str(msg["car_ordinal"]))
                self.lbl_calib.config(text=msg["calib_status"])
                
                if msg["in_menu"]:
                    self.lbl_rpm.config(text="In Menu")
                    self.lbl_gear_val.config(text="-")
                    self._update_led_display(0)
                else:
                    self.lbl_rpm.config(text=f"{msg['current_rpm']:6.0f} / {msg['max_rpm']:5.0f}")
                    gear = msg["gear"]
                    gear_str = "R" if gear == 0 else str(gear)
                    self.lbl_gear_val.config(text=gear_str)
                    self._update_led_display(msg["led_mask"])
                    
            elif msg_type == "car_changed":
                ordinal = msg["car_ordinal"]
                self.car_ordinal = ordinal
                cars = self.settings.get("cars", {})
                if ordinal in cars:
                    c = cars[ordinal]
                    self.ratio_var.set(c["led_min_rpm_ratio"])
                    self.ratio_lbl.config(text=f"{int(c['led_min_rpm_ratio'] * 100)} %")
                    self.offset_low_var.set(c["blink_offset_low_gear_rpm"])
                    self.offset_low_lbl.config(text=f"{c['blink_offset_low_gear_rpm']} RPM")
                    self.offset_high_var.set(c["blink_offset_high_gear_rpm"])
                    self.offset_high_lbl.config(text=f"{c['blink_offset_high_gear_rpm']} RPM")
                    self.blink_speed_var.set("15" if c["blink_hz"] == 15.0 else "10")
                else:
                    self.ratio_var.set(self.settings["led_min_rpm_ratio"])
                    self.ratio_lbl.config(text=f"{int(self.settings['led_min_rpm_ratio'] * 100)} %")
                    self.offset_low_var.set(self.settings["blink_offset_low_gear_rpm"])
                    self.offset_low_lbl.config(text=f"{self.settings['blink_offset_low_gear_rpm']} RPM")
                    self.offset_high_var.set(self.settings["blink_offset_high_gear_rpm"])
                    self.offset_high_lbl.config(text=f"{self.settings['blink_offset_high_gear_rpm']} RPM")
                    self.blink_speed_var.set("15" if self.settings["blink_hz"] == 15.0 else "10")
                self._push_updated_settings()

            elif msg_type == "auto_save_calibration":
                ordinal = msg["car_ordinal"]
                redline = msg["redline"]
                nominal_max_rpm = msg["nominal_max_rpm"]
                if "cars" not in self.settings:
                    self.settings["cars"] = {}
                self.settings["cars"][ordinal] = {
                    "redline": redline,
                    "nominal_max_rpm": nominal_max_rpm,
                    "led_min_rpm_ratio": self.ratio_var.get(),
                    "blink_offset_low_gear_rpm": self.offset_low_var.get(),
                    "blink_offset_high_gear_rpm": self.offset_high_var.get(),
                    "blink_hz": float(self.blink_speed_var.get())
                }
                fwl.save_config(self.cfg_path, self.settings)
                self._push_updated_settings()
                self.status_bar_lbl.config(text="Auto-saved!", foreground=ACCENT_GREEN)

            elif msg_type == "telemetry_idle":
                self.lbl_game.config(text="Waiting...")
                self.lbl_rpm.config(text="-")
                self.lbl_gear_val.config(text="-")
                self.lbl_calib.config(text="Waiting...")
                self._update_led_display(0)
                self.lbl_wheel.config(
                    text="Connected" if msg["wheel_connected"] else "Not Detected",
                    fg=ACCENT_GREEN if msg["wheel_connected"] else ACCENT_RED
                )
                
            elif msg_type == "wheel_status":
                status_str = "Connected" if msg["connected"] else "Not Detected"
                status_color = ACCENT_GREEN if msg["connected"] else ACCENT_RED
                self.lbl_wheel.config(text=status_str, fg=status_color)
                self.wheel_connected = msg["connected"]
                
            elif msg_type == "wheel_status_alert":
                messagebox.showwarning("Wheel Status", msg["msg"])
                
            elif msg_type == "socket_status":
                status = msg["status"]
                port = msg["port"]
                if status == "listening":
                    self.status_bar_lbl.config(text=f"Listening (Port {port})", foreground=ACCENT_GREEN)
                    self.btn_run.config(text="Stop Listener", bg=ACCENT_RED, activebackground=ACCENT_RED, state="normal")
                    self.is_running = True
                elif status == "stopped":
                    self.status_bar_lbl.config(text="Stopped", foreground=ACCENT_RED)
                    self.btn_run.config(text="Start Listener", bg=ACCENT_GREEN, activebackground=ACCENT_GREEN, state="normal")
                    self.is_running = False
                    self.lbl_game.config(text="None")
                    self.lbl_car.config(text="0")
                    self.lbl_rpm.config(text="0 / 0")
                    self.lbl_calib.config(text="Idle")
                    self.lbl_gear_val.config(text="-")
                    self._update_led_display(0)
                    
            elif msg_type == "status_text":
                self.status_bar_lbl.config(text=msg["text"], foreground=ACCENT_BLUE)
                
            elif msg_type == "error":
                messagebox.showerror("Error", msg["message"])
                self._toggle_running() # force stop state

        # Queue poll interval (30ms = ~33 FPS)
        self.root.after(30, self._poll_queue)

    def _on_close(self):
        """Perform cleanup before window closes."""
        self._stop_worker()
        # Wait a brief moment for worker threads to start shutting down, then destroy
        self.root.after(100, self.root.destroy)


def run_gui():
    """Application entry point for GUI mode."""
    # Enable high DPI awareness on Windows if available
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    root = tk.Tk()
    ForzaLEDsGUI(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
