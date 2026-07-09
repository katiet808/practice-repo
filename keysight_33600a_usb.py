"""
Front-panel-style GUI for the Keysight/Agilent 33600A Trueform Series
Waveform Generator, over USB via PyVISA.

Unlike the real instrument -- where you press "Waveforms", then
"Parameters", then "Units", then "Mod" as separate soft-key menus --
every control here lives on one screen and applies immediately (or
on Enter / on click), so there's no menu-diving.

Setup (same as the console script this is built on):
    1. Install Keysight IO Libraries Suite: https://www.keysight.com/find/iolib
    2. pip install pyvisa
    3. Connect the 33600A over USB and power it on.

Run:
    python keysight_33600a_gui.py
"""

from typing import Optional, TYPE_CHECKING
import io
import base64
import tkinter as tk
from tkinter import ttk, messagebox

if TYPE_CHECKING:
    # Only seen by type checkers (Pylance/mypy) -- always a real module here,
    # so annotations below don't trip "variable not allowed in type
    # expression". Has no effect at runtime.
    import pyvisa as pyvisa_types

try:
    import pyvisa
except ImportError:
    pyvisa = None  # GUI still opens so you can see the layout; Connect will error clearly

try:
    import numpy as np
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    MATPLOTLIB_OK = True
except ImportError:
    MATPLOTLIB_OK = False  # waveform icons + live preview are skipped gracefully


# --------------------------------------------------------------------------
# Instrument driver (same SCPI wrapper as the console version, extended with
# modulation + a couple of query helpers the GUI needs to read back state)
# --------------------------------------------------------------------------

def find_usb_instrument(rm: "pyvisa_types.ResourceManager") -> str:
    resources = rm.list_resources()
    usb = [r for r in resources if r.upper().startswith("USB")]
    if not usb:
        raise RuntimeError(
            "No USB VISA resource found. Check that Keysight IO Libraries "
            "Suite is installed, that the 33600A is plugged in and powered "
            "on, and confirm it appears in Keysight Connection Expert."
        )
    return usb[0]


class Waveform33600A:
    """SCPI wrapper for the 33600A. write()/query() are exposed directly for
    anything not wrapped here -- see the SCPI Programming Reference
    (manual p.184) for the full command set."""

    def __init__(self, resource_string: Optional[str] = None, timeout_ms: int = 5000):
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed. Run: pip install pyvisa")
        self.rm = pyvisa.ResourceManager()
        resource_string = resource_string or find_usb_instrument(self.rm)
        self.inst = self.rm.open_resource(resource_string)
        self.inst.timeout = timeout_ms
        self.idn = self.query("*IDN?")

    def close(self) -> None:
        try:
            self.inst.close()
            self.rm.close()
        except Exception:
            pass

    # -- low level --------------------------------------------------
    def write(self, cmd: str) -> None:
        self.inst.write(cmd)

    def query(self, cmd: str) -> str:
        return self.inst.query(cmd).strip()

    def check_errors(self) -> str:
        return self.query("SYST:ERR?")

    def reset(self) -> None:
        self.write("*RST")
        self.write("*CLS")

    # -- waveform shape (APPLy subsystem) ----------------------------
    def _set_waveform(self, code, freq_hz, amp_vpp, offset_v=0.0, param_key=None, param_val=None, channel=1):
        self.write(f"SOUR{channel}:APPL:{code} {freq_hz},{amp_vpp},{offset_v}")
        if param_key and param_val is not None:
            self.write(f"SOUR{channel}:{param_key} {param_val}")

    def set_sine(self, freq_hz, amp_vpp, offset_v=0.0, channel=1):
        self._set_waveform("SIN", freq_hz, amp_vpp, offset_v, channel=channel)

    def set_square(self, freq_hz, amp_vpp, offset_v=0.0, duty_pct=None, channel=1):
        self._set_waveform("SQU", freq_hz, amp_vpp, offset_v, "FUNC:SQU:DCYC", duty_pct, channel=channel)

    def set_ramp(self, freq_hz, amp_vpp, offset_v=0.0, symmetry_pct=None, channel=1):
        self._set_waveform("RAMP", freq_hz, amp_vpp, offset_v, "FUNC:RAMP:SYMM", symmetry_pct, channel=channel)

    def set_pulse(self, freq_hz, amp_vpp, offset_v=0.0, duty_pct=None, channel=1):
        self._set_waveform("PULS", freq_hz, amp_vpp, offset_v, "FUNC:PULS:DCYC", duty_pct, channel=channel)

    def set_amplitude_unit(self, unit_code: str, channel=1):
        """unit_code: VPP | VRMS | DBM. Selects how the Amplitude number in
        APPLy (and the front panel) is interpreted. This must be sent before
        APPLy/VOLT for it to take effect on the next amplitude write."""
        self.write(f"SOUR{channel}:VOLT:UNIT {unit_code}")

    def set_triangle(self, freq_hz, amp_vpp, offset_v=0.0, channel=1):
        """The 33600A has no separate APPLy:TRIangle -- a triangle is a ramp with symmetry forced to 50%."""
        self._set_waveform("RAMP", freq_hz, amp_vpp, offset_v, "FUNC:RAMP:SYMM", 50, channel=channel)

    def set_prbs(self, bit_rate_hz, amp_vpp, offset_v=0.0, channel=1):
        self._set_waveform("PRBS", bit_rate_hz, amp_vpp, offset_v, channel=channel)

    def set_noise(self, amp_vpp, offset_v=0.0, channel=1):
        self.write(f"SOUR{channel}:APPL:NOIS {amp_vpp},{offset_v}")

    def set_dc(self, offset_v=0.0, channel=1):
        self.write(f"SOUR{channel}:APPL:DC 1,1,{offset_v}")

    def set_arbitrary(self, sample_rate_sa_s, amp_vpp, offset_v=0.0, channel=1):
        self._set_waveform("ARB", sample_rate_sa_s, amp_vpp, offset_v, channel=channel)

    # -- extra parameters ---------------------------------------------
    def set_phase(self, deg, channel=1):
        self.write(f"SOUR{channel}:PHAS {deg}")

    def sync_phase(self, channel=1):
        self.write(f"SOUR{channel}:PHAS:SYNC")

    # -- output -----------------------------------------------------
    def output(self, on: bool, channel=1):
        self.write(f"OUTP{channel} {'ON' if on else 'OFF'}")

    def set_load(self, ohms_or_inf, channel=1):
        self.write(f"OUTP{channel}:LOAD {ohms_or_inf}")

    # -- modulation ---------------------------------------------------
    def set_modulation(self, mod_type: str, enabled: bool, depth_or_dev: float,
                        shape: str = "SIN", channel: int = 1):
        """mod_type: AM | FM | PM | FSK | PWM. depth_or_dev meaning depends on mod_type."""
        mod_type = mod_type.upper()
        pfx = f"SOUR{channel}:{mod_type}"
        mod_cmds = {"AM": ("DEPT", True), "FM": ("DEV", True), "PM": ("DEV", True), 
                    "FSK": ("FREQ", False), "PWM": ("DEV", True)}
        if mod_type in mod_cmds:
            cmd, use_shape = mod_cmds[mod_type]
            if use_shape:
                self.write(f"{pfx}:INT:FUNC {shape}")
            self.write(f"{pfx}:{cmd} {depth_or_dev}")
        else:
            raise ValueError(f"Unknown modulation type: {mod_type}")
        self.write(f"{pfx}:STAT {'ON' if enabled else 'OFF'}")

    # -- alternate level representation (High/Low vs Amplitude/Offset) ------
    def set_level_high_low(self, high_v, low_v, channel=1):
        """Directly set the output High and Low levels. This is SCPI's
        alternate representation to Amplitude+Offset -- setting these
        overrides whatever amplitude/offset was last sent via APPLy, and
        the instrument keeps the two representations in sync internally."""
        self.write(f"SOUR{channel}:VOLT:HIGH {high_v}")
        self.write(f"SOUR{channel}:VOLT:LOW {low_v}")

    # -- read-only pulse timing ----------------------------------------------
    def query_pulse_timing(self, channel=1):
        """Read back the instrument's actual Pulse Width, Lead Edge (rise)
        and Trail Edge (fall) times, in seconds. Returns None if unavailable
        (e.g. current waveform isn't Pulse, or the query fails)."""
        try:
            width = float(self.query(f"SOUR{channel}:FUNC:PULS:WIDT?"))
            lead = float(self.query(f"SOUR{channel}:FUNC:PULS:TRAN:LEAD?"))
            trail = float(self.query(f"SOUR{channel}:FUNC:PULS:TRAN:TRA?"))
            return width, lead, trail
        except Exception:
            return None


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

BG = "#1b1e22"
PANEL = "#25292e"
DISPLAY_BG = "#0a1f14"
DISPLAY_FG = "#5CFF9E"
ACCENT = "#3b82f6"
FG = "#e6e6e6"
MUTED = "#9aa0a6"
ON_GREEN = "#22c55e"
OFF_RED = "#ef4444"

FREQ_UNITS = {"\u00b5Hz": 1e-6, "mHz": 1e-3, "Hz": 1, "kHz": 1e3, "MHz": 1e6}
AMP_UNITS = {"Vpp": "VPP", "Vrms": "VRMS", "dBm": "DBM"}
# Plain-voltage units (numeric multipliers) -- used for Offset always, and
# for Amplitude when the panel is in "High/Low" level-entry mode, since
# High/Low levels are literal volts rather than Vpp/Vrms/dBm.
LEVEL_UNITS = {"mV": 1e-3, "V": 1.0}

# Keysight 33600A maximum voltage specification: ±20V (10V Vpp max amplitude)
MAX_VOLTAGE_PEAK = 20.0  # ±20V maximum output swing
LEVEL_MODES = ["Ampl/Offset", "High/Low"]

# 3x3 grid, row-major, matching the requested layout
WAVEFORMS = ["Sine", "Square", "Ramp",
             "Pulse", "Arb", "Triangle",
             "Noise", "PRBS", "DC"]

MOD_TYPES = ["AM", "FM", "PM", "FSK", "PWM"]

# Display name -> SCPI INT:FUNC code for the internal modulation source shape
MOD_SHAPES = {
    "Sine": "SIN",
    "Square": "SQU",
    "Triangle": "TRI",
    "UpRamp": "RAMP",
    "DnRamp": "NRAM",
    "Noise": "NOIS",
    "PRBS": "PRBS",
    "Arb": "ARB",
}


# --------------------------------------------------------------------------
# Waveform preview math + icon rendering (display only -- purely illustrative,
# not scaled to the real frequency; used for both the button icons and the
# "Expected Output" panel)
# --------------------------------------------------------------------------

def _waveform_curve(shape: str, amplitude: float = 1.0, offset: float = 0.0,
                     duty_pct: float = 50.0, n: int = 400):
    if not MATPLOTLIB_OK:
        return [], []
    t = np.linspace(0, 2, n)          # ~2 illustrative cycles
    ph = t % 1.0
    duty = max(1.0, min(99.0, duty_pct)) / 100.0
    half = amplitude / 2.0

    if shape == "Sine":
        y = offset + half * np.sin(2 * np.pi * t)
    elif shape == "Square":
        y = offset + np.where(ph < duty, half, -half)
    elif shape == "Ramp":
        y = offset + amplitude * (ph - 0.5)
    elif shape == "Triangle":
        y = offset + half * (4 * np.abs(ph - 0.5) - 1)
    elif shape == "Pulse":
        y = offset + np.where(ph < duty, half, -half)
    elif shape == "Noise":
        rng = np.random.default_rng(42)
        y = offset + rng.uniform(-half, half, size=n)
    elif shape == "PRBS":
        rng = np.random.default_rng(7)
        bits = rng.integers(0, 2, size=16)
        idx = np.minimum((t * 8).astype(int), 15)
        y = offset + np.where(bits[idx] == 1, half, -half)
    elif shape == "DC":
        y = np.full_like(t, offset)
    elif shape == "Arb":
        y = offset + half * (0.6 * np.sin(2 * np.pi * t)
                              + 0.3 * np.sin(6 * np.pi * t + 1.0)
                              + 0.2 * np.sin(11 * np.pi * t))
    else:
        y = offset + half * np.sin(2 * np.pi * t)
    return t, y


_icon_cache = {}


def get_waveform_icon(shape: str):
    """Small PNG icon (rendered via matplotlib, cached) shown on each
    waveform button -- same dark/green LCD styling as the rest of the GUI."""
    if shape in _icon_cache:
        return _icon_cache[shape]
    fig = Figure(figsize=(0.62, 0.28), dpi=100)
    fig.patch.set_facecolor(PANEL)
    ax = fig.add_axes([0.03, 0.10, 0.94, 0.82])
    ax.set_facecolor(PANEL)
    t, y = _waveform_curve(shape, amplitude=1.0, offset=0.0, duty_pct=50.0, n=300)
    ax.plot(t, y, color=DISPLAY_FG, linewidth=1.4)
    ax.set_xlim(t[0], t[-1])
    ax.set_ylim(-1.25, 1.25)
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=PANEL)
    buf.seek(0)
    img = tk.PhotoImage(data=base64.b64encode(buf.read()))
    _icon_cache[shape] = img
    return img


class ChannelPanel(ttk.Frame):
    """All controls for one output channel, laid flat -- no sub-menus."""

    def __init__(self, master, app, channel: int):
        super().__init__(master, style="Panel.TFrame", padding=12)
        self.app = app
        self.channel = channel
        self.output_on = False

        # ---- mock LCD display -------------------------------------------------
        self.display_var = tk.StringVar(value="SIN   1.000 Vpp   1.000 kHz   0.000 Vdc")
        display = tk.Label(self, textvariable=self.display_var, bg=DISPLAY_BG, fg=DISPLAY_FG,
                            font=("Consolas", 12, "bold"), anchor="w", padx=10, pady=8)
        display.grid(row=1, column=0, columnspan=7, sticky="ew", pady=(0, 10))

        # ---- waveform buttons (formerly behind "Waveforms" softkey) ----------
        wf_frame = ttk.LabelFrame(self, text="Waveform", style="Panel.TLabelframe")
        wf_frame.grid(row=2, column=0, columnspan=6, sticky="ew", pady=4)
        self.wf_var = tk.StringVar(value="Sine")
        for i, wf in enumerate(WAVEFORMS):
            r, c = divmod(i, 3)
            if MATPLOTLIB_OK:
                icon = get_waveform_icon(wf)
                b = ttk.Radiobutton(wf_frame, text=wf, image=icon, compound="top",
                                     value=wf, variable=self.wf_var,
                                     style="WfToggle.TButton", command=self.apply_waveform)
                b.image = icon  # keep a reference so it isn't garbage-collected
            else:
                b = ttk.Radiobutton(wf_frame, text=wf, value=wf, variable=self.wf_var,
                                     style="WfToggle.TButton", command=self.apply_waveform)
            b.grid(row=r, column=c, padx=10, pady=8)  # no sticky -> centered in its cell
        for c in range(3):
            wf_frame.columnconfigure(c, weight=1, uniform="wf")

        # ---- parameters (formerly behind "Parameters" softkey) ---------------
        param_frame = ttk.LabelFrame(self, text="Parameters", style="Panel.TLabelframe")
        param_frame.grid(row=3, column=0, columnspan=6, sticky="ew", pady=4)

        self.freq_var = tk.StringVar(value="1.000")
        self.freq_unit_var = tk.StringVar(value="kHz")
        self.amp_var = tk.StringVar(value="1.000")
        self.amp_unit_var = tk.StringVar(value="Vpp")
        self.offset_var = tk.StringVar(value="0.000")
        self.offset_unit_var = tk.StringVar(value="V")
        self.duty_var = tk.StringVar(value="50.0")
        self.phase_var = tk.StringVar(value="0.0")
        self.level_mode_var = tk.StringVar(value="Ampl/Offset")

        self._param_row(param_frame, 0, "Frequency", self.freq_var,
                         unit_var=self.freq_unit_var, units=list(FREQ_UNITS))

        # Amplitude / High Level row -- built by hand (rather than via
        # _param_row) so the label text and unit list can be swapped when
        # the Level Mode toggle below is flipped.
        self.amp_label = ttk.Label(param_frame, text="Amplitude", style="Panel.TLabel", width=22)
        self.amp_label.grid(row=1, column=0, sticky="w", padx=4, pady=4)
        amp_entry = ttk.Entry(param_frame, textvariable=self.amp_var, width=10, style="Panel.TEntry")
        amp_entry.grid(row=1, column=1, padx=4)
        amp_entry.bind("<Return>", lambda _e: self.apply_waveform())
        self.amp_unit_menu = ttk.OptionMenu(param_frame, self.amp_unit_var, self.amp_unit_var.get(),
                                             *AMP_UNITS.keys(), command=lambda _=None: self.apply_waveform())
        self.amp_unit_menu.grid(row=1, column=2, padx=4)

        # Offset / Low Level row -- offset always gets a V/mV unit dropdown
        # (fixes offset not accepting a milli- prefix), and doubles as the
        # "Low Level" field when Level Mode is High/Low.
        self.offset_label = ttk.Label(param_frame, text="Offset", style="Panel.TLabel", width=22)
        self.offset_label.grid(row=2, column=0, sticky="w", padx=4, pady=4)
        offset_entry = ttk.Entry(param_frame, textvariable=self.offset_var, width=10, style="Panel.TEntry")
        offset_entry.grid(row=2, column=1, padx=4)
        offset_entry.bind("<Return>", lambda _e: self.apply_waveform())
        self.offset_unit_menu = ttk.OptionMenu(param_frame, self.offset_unit_var, self.offset_unit_var.get(),
                                                *LEVEL_UNITS.keys(), command=lambda _=None: self.apply_waveform())
        self.offset_unit_menu.grid(row=2, column=2, padx=4)

        self._param_row(param_frame, 3, "Duty Cycle / Symmetry", self.duty_var, unit_label="%")
        self._param_row(param_frame, 4, "Phase", self.phase_var, unit_label="deg",
                         extra_button=("Sync Phase", self.sync_phase))

        # Level Mode toggle (formerly under the "Units" softkey): switch the
        # Amplitude/Offset row pair between plain Amplitude+Offset entry and
        # direct High Level / Low Level entry.
        ttk.Label(param_frame, text="Level Mode", style="Panel.TLabel", width=22).grid(
            row=5, column=0, sticky="w", padx=4, pady=4)
        self.level_mode_menu = ttk.OptionMenu(param_frame, self.level_mode_var, self.level_mode_var.get(),
                                               *LEVEL_MODES, command=lambda _=None: self.toggle_level_mode())
        self.level_mode_menu.grid(row=5, column=1, columnspan=2, sticky="w", padx=4)

        # ---- modulation (formerly behind "Mod" softkey) -----------------------
        mod_frame = ttk.LabelFrame(self, text="Modulate", style="Panel.TLabelframe")
        mod_frame.grid(row=4, column=0, columnspan=6, sticky="ew", pady=4)

        self.mod_enabled = tk.BooleanVar(value=False)
        self.mod_type_var = tk.StringVar(value="AM")
        self.mod_shape_var = tk.StringVar(value="Sine")
        self.mod_value_var = tk.StringVar(value="50.0")

        ttk.Checkbutton(mod_frame, text="On", variable=self.mod_enabled,
                         style="Panel.TCheckbutton", command=self.apply_modulation
                         ).grid(row=0, column=0, padx=(4, 10))
        ttk.Label(mod_frame, text="Type", style="Panel.TLabel").grid(row=0, column=1)
        ttk.OptionMenu(mod_frame, self.mod_type_var, self.mod_type_var.get(), *MOD_TYPES,
                        command=lambda _=None: self.apply_modulation()).grid(row=0, column=2, padx=6)
        ttk.Label(mod_frame, text="Shape", style="Panel.TLabel").grid(row=0, column=3)
        ttk.OptionMenu(mod_frame, self.mod_shape_var, self.mod_shape_var.get(), *MOD_SHAPES.keys(),
                        command=lambda _=None: self.apply_modulation()).grid(row=0, column=4, padx=6)
        ttk.Label(mod_frame, text="Depth / Deviation", style="Panel.TLabel").grid(row=0, column=5, padx=(10, 4))
        e = ttk.Entry(mod_frame, textvariable=self.mod_value_var, width=8, style="Panel.TEntry")
        e.grid(row=0, column=6)
        e.bind("<Return>", lambda _e: self.apply_modulation())
        ttk.Label(mod_frame, text="(% for AM/PWM, Hz for FM/FSK, deg for PM)",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=7, sticky="w", padx=4)

        # ---- output / load ------------------------------------------------------
        out_frame = ttk.Frame(self, style="Panel.TFrame")
        out_frame.grid(row=5, column=0, columnspan=6, sticky="ew", pady=(10, 0))

        self.run_btn = tk.Button(out_frame, text="RUN \u25b6", bg=ACCENT, fg="white",
                                  font=("Segoe UI", 11, "bold"), relief="flat", padx=18, pady=8,
                                  command=self.run_all)
        self.run_btn.grid(row=0, column=0, padx=(0, 10))

        self.output_btn = tk.Button(out_frame, text=f"OUTPUT {channel}: OFF", bg=OFF_RED, fg="white",
                                     font=("Segoe UI", 10, "bold"), relief="flat", padx=14, pady=8,
                                     command=self.toggle_output)
        self.output_btn.grid(row=0, column=1, padx=(0, 16))

        ttk.Label(out_frame, text="Load (Ω, or 'INF')", style="Panel.TLabel").grid(row=0, column=2)
        self.load_var = tk.StringVar(value="50")
        load_entry = ttk.Entry(out_frame, textvariable=self.load_var, width=8, style="Panel.TEntry")
        load_entry.grid(row=0, column=3, padx=6)
        load_entry.bind("<Return>", lambda _e: self.apply_load())
        ttk.Button(out_frame, text="Set", style="Small.TButton",
                   command=self.apply_load).grid(row=0, column=4)

        # ---- waveform parameters (dynamic based on waveform type) --------
        self.params_frame = ttk.LabelFrame(self, text="Parameters", style="Panel.TLabelframe")
        self.params_frame.grid(row=2, column=6, sticky="nsew", padx=(14, 0), pady=4)
        self.params_frame.columnconfigure(0, weight=1)
        self.params_frame.columnconfigure(1, weight=1)
        
        # Store all possible waveform-specific variables
        self.pulse_width_var = tk.StringVar(value="--")
        self.pulse_lead_var = tk.StringVar(value="--")
        self.pulse_trail_var = tk.StringVar(value="--")
        self.duty_cycle_var = tk.StringVar(value="--")
        self.symmetry_var = tk.StringVar(value="--")
        self.params_labels = {}  # Will store label widgets for dynamic updates

        # ---- expected output preview (middle-to-bottom, right side) -----------
        preview_frame = ttk.LabelFrame(self, text="Expected Output", style="Panel.TLabelframe")
        preview_frame.grid(row=3, column=6, rowspan=3, sticky="nsew", padx=(14, 0), pady=4)

        if MATPLOTLIB_OK:
            self.fig = Figure(figsize=(3.3, 2.7), dpi=100)
            self.fig.patch.set_facecolor(PANEL)
            self.ax = self.fig.add_subplot(111)
            self.canvas = FigureCanvasTkAgg(self.fig, master=preview_frame)
            self.canvas.get_tk_widget().configure(bg=PANEL, highlightthickness=0)
            self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)
            self._update_preview()
        else:
            ttk.Label(preview_frame,
                      text="Install matplotlib + numpy for the\nlive waveform preview:\n"
                           "pip install matplotlib numpy",
                      style="Muted.TLabel", justify="left").pack(padx=10, pady=10)

        self._update_params_display()

        for c in range(6):
            self.columnconfigure(c, weight=1)
        self.columnconfigure(6, weight=2)

    # -- helpers ------------------------------------------------------------
    def _param_row(self, parent, row, label, var, unit_var=None, units=None,
                    unit_label=None, extra_button=None):
        ttk.Label(parent, text=label, style="Panel.TLabel", width=22).grid(
            row=row, column=0, sticky="w", padx=4, pady=4)
        entry = ttk.Entry(parent, textvariable=var, width=10, style="Panel.TEntry")
        entry.grid(row=row, column=1, padx=4)
        entry.bind("<Return>", lambda _e: self.apply_waveform())
        if unit_var is not None and units:
            ttk.OptionMenu(parent, unit_var, unit_var.get(), *units,
                            command=lambda _=None: self.apply_waveform()).grid(row=row, column=2, padx=4)
        elif unit_label:
            ttk.Label(parent, text=unit_label, style="Muted.TLabel").grid(row=row, column=2, sticky="w")
        if extra_button:
            text, cmd = extra_button
            ttk.Button(parent, text=text, style="Small.TButton", command=cmd).grid(
                row=row, column=3, padx=8)

    def _freq_hz(self):
        try:
            return float(self.freq_var.get()) * FREQ_UNITS[self.freq_unit_var.get()]
        except ValueError:
            return 0.0

    def _amp_vpp(self):
        try:
            return float(self.amp_var.get())
        except ValueError:
            return 0.0

    def _offset_v(self):
        try:
            mult = LEVEL_UNITS.get(self.offset_unit_var.get(), 1.0)
            return float(self.offset_var.get()) * mult
        except ValueError:
            return 0.0

    def _amp_vpp_for_preview(self, amp_value, unit):
        """Approximate Vpp conversion for preview (sine-wave relationships)."""
        if unit == "Vrms":
            return amp_value * 2 * (2 ** 0.5)
        elif unit == "dBm":
            vrms = (50 * 10 ** (amp_value / 10) / 1000) ** 0.5
            return vrms * 2 * (2 ** 0.5)
        return amp_value

    def _preview_amp_offset(self):
        """Amplitude/offset for the 'Expected Output' graph only. Unlike
        _effective_amp_offset() (which sends the raw number the instrument
        should interpret via VOLT:UNIT), this converts Vrms/dBm to an
        equivalent Vpp so the preview's height actually changes when you
        switch amplitude units."""
        if self.level_mode_var.get() == "High/Low":
            return self._effective_amp_offset()
        try:
            raw = float(self.amp_var.get())
        except ValueError:
            raw = 0.0
        return self._amp_vpp_for_preview(raw, self.amp_unit_var.get()), self._offset_v()

    def _effective_amp_offset(self):
        """Translate the UI fields into the actual electrical
        (amplitude_vpp, offset_v) the instrument will output, accounting for
        whether the panel is in Ampl/Offset or High/Low entry mode. In
        High/Low mode, the Amplitude field holds the High Level and the
        Offset field holds the Low Level (both plain volts)."""
        if self.level_mode_var.get() == "High/Low":
            mult = LEVEL_UNITS.get(self.amp_unit_var.get(), 1.0)
            try:
                high = float(self.amp_var.get()) * mult
            except ValueError:
                high = 0.0
            low = self._offset_v()
            return high - low, (high + low) / 2.0
        return self._amp_vpp(), self._offset_v()

    def _fmt_seconds(self, v):
        if v is None:
            return "--"
        av = abs(v)
        scales = [(1, " s"), (1e-3, " ms"), (1e-6, " \u00b5s"), (1e-9, " ns")]
        for threshold, unit in scales:
            if av >= threshold:
                return f"{v * (1/threshold if threshold != 1 else 1):.4g}{unit}"
        return f"{v * 1e9:.4g} ns"

    def _validate_voltage(self):
        """Check voltage constraints. Returns (is_valid, error_message).
        In Ampl/Offset mode: checks amplitude + offset don't exceed ±20V.
        In High/Low mode: checks High and Low levels are within ±20V and Low <= High."""
        if self.level_mode_var.get() == "High/Low":
            # High/Low mode: check both levels and that Low <= High
            mult = LEVEL_UNITS.get(self.amp_unit_var.get(), 1.0)
            try:
                high = float(self.amp_var.get() or 0) * mult
                low = self._offset_v()
            except ValueError:
                return False, "ERROR: Invalid voltage values."
            
            if high > MAX_VOLTAGE_PEAK or high < -MAX_VOLTAGE_PEAK:
                return False, f"ERROR: Voltage too high. Maximum is ±{MAX_VOLTAGE_PEAK}V."
            if low > MAX_VOLTAGE_PEAK or low < -MAX_VOLTAGE_PEAK:
                return False, f"ERROR: Voltage too high. Maximum is ±{MAX_VOLTAGE_PEAK}V."
            if low > high:
                return False, "ERROR: Lower voltage higher than high voltage. Fix!"
        else:
            # Ampl/Offset mode: check that the peak-to-peak doesn't exceed ±20V
            amp, off = self._effective_amp_offset()
            peak_high = off + amp / 2.0
            peak_low = off - amp / 2.0
            
            if peak_high > MAX_VOLTAGE_PEAK or peak_low < -MAX_VOLTAGE_PEAK:
                return False, f"ERROR: Voltage too high. Maximum is ±{MAX_VOLTAGE_PEAK}V."
        
        return True, ""

    def _update_display(self):
        wf = self.wf_var.get().upper()[:4]
        if self.level_mode_var.get() == "High/Low":
            fmt = f"{wf:<5} HI {self.amp_var.get():>7} {self.amp_unit_var.get():<3} LO {self.offset_var.get():>7} {self.offset_unit_var.get():<3} {self.freq_var.get():>8} {self.freq_unit_var.get():<4}"
        else:
            fmt = f"{wf:<5} {self.amp_var.get():>7} {self.amp_unit_var.get():<5} {self.freq_var.get():>8} {self.freq_unit_var.get():<4} {self.offset_var.get():>7} {self.offset_unit_var.get()}"
        self.display_var.set(fmt)

    def _update_preview(self):
        """Redraw the 'Expected Output' graph from the current field values.
        Purely illustrative (shape/amplitude/offset), independent of whether
        the instrument is connected."""
        if not MATPLOTLIB_OK:
            return
        wf = self.wf_var.get()
        amp, off = self._preview_amp_offset()
        amp = amp or 1.0
        try:
            duty = float(self.duty_var.get() or 50)
        except ValueError:
            duty = 50.0

        t, y = _waveform_curve(wf, amplitude=amp, offset=off, duty_pct=duty)
        self.ax.clear()
        self.ax.set_facecolor(DISPLAY_BG)
        for spine in self.ax.spines.values():
            spine.set_color(MUTED)
        self.ax.tick_params(colors=MUTED, labelsize=7)
        self.ax.grid(True, color="#1d3a2a", linewidth=0.6)
        self.ax.plot(t, y, color=DISPLAY_FG, linewidth=1.6)
        self.ax.set_xlim(t[0], t[-1])
        span = max(abs(amp) / 2 + abs(off), 0.5)
        self.ax.set_ylim(off - span - 0.1, off + span + 0.1)
        self.ax.set_title(
            f"{wf}   {self.amp_label['text']} {self.amp_var.get()} {self.amp_unit_var.get()}   "
            f"{self.freq_var.get()} {self.freq_unit_var.get()}",
            color=FG, fontsize=8, pad=6,
        )
        self.canvas.draw_idle()

    def _update_params_display(self):
        """Dynamically update parameters panel based on selected waveform."""
        # Clear existing labels/values from the frame
        for widget in self.params_frame.winfo_children():
            widget.destroy()
        self.params_labels.clear()
        
        wf = self.wf_var.get()
        params = []
        
        if wf == "Pulse":
            self.params_frame.configure(text="Pulse Timing (Read-Only)")
            # Query device for actual pulse parameters
            if self.app.gen:
                result = self.app.gen.query_pulse_timing(channel=self.channel)
                if result:
                    width, lead, trail = result
                    self.pulse_width_var.set(self._fmt_seconds(width))
                    self.pulse_lead_var.set(self._fmt_seconds(lead))
                    self.pulse_trail_var.set(self._fmt_seconds(trail))
                else:
                    self._set_pulse_estimates()
            else:
                self._set_pulse_estimates()
            params = [
                ("Pulse Width", self.pulse_width_var),
                ("Lead Edge", self.pulse_lead_var),
                ("Trail Edge", self.pulse_trail_var),
            ]
        elif wf == "Square":
            self.params_frame.configure(text="Square Parameters (Read-Only)")
            try:
                duty = float(self.duty_var.get() or 50)
            except ValueError:
                duty = 50.0
            self.duty_cycle_var.set(f"{duty:.1f}%")
            params = [("Duty Cycle", self.duty_cycle_var)]
        elif wf == "Ramp":
            self.params_frame.configure(text="Ramp Parameters (Read-Only)")
            try:
                symmetry = float(self.duty_var.get() or 50)
            except ValueError:
                symmetry = 50.0
            self.symmetry_var.set(f"{symmetry:.1f}%")
            params = [("Symmetry", self.symmetry_var)]
        elif wf == "Triangle":
            self.params_frame.configure(text="Triangle Parameters (Read-Only)")
            self.symmetry_var.set("50.0% (fixed)")
            params = [("Symmetry", self.symmetry_var)]
        else:
            self.params_frame.configure(text=f"{wf} (Read-Only)")
            ttk.Label(self.params_frame, text="No special", style="Muted.TLabel").grid(
                row=0, column=0, sticky="w", padx=4, pady=2)
            ttk.Label(self.params_frame, text="parameters", style="Muted.TLabel").grid(
                row=1, column=0, sticky="w", padx=4, pady=2)
            return
        
        # Display the parameters
        for i, (lbl, var) in enumerate(params):
            ttk.Label(self.params_frame, text=lbl + ":", style="Panel.TLabel", width=12).grid(
                row=i, column=0, sticky="w", padx=4, pady=2)
            ttk.Label(self.params_frame, textvariable=var, style="Muted.TLabel", width=12).grid(
                row=i, column=1, sticky="w", padx=4, pady=2)
    
    def _set_pulse_estimates(self):
        """Set estimated pulse timing values when not connected."""
        freq = self._freq_hz()
        period = (1.0 / freq) if freq else 0.0
        try:
            duty = float(self.duty_var.get() or 50)
        except ValueError:
            duty = 50.0
        width, edge = period * (duty / 100.0), 8.4e-9
        suffix = " (est.)"
        self.pulse_width_var.set(self._fmt_seconds(width) + suffix)
        self.pulse_lead_var.set(self._fmt_seconds(edge) + suffix)
        self.pulse_trail_var.set(self._fmt_seconds(edge) + suffix)

    # -- level mode toggle ----------------------------------------------------
    def toggle_level_mode(self):
        """Flip between Amplitude/Offset and High/Low entry modes."""
        is_hl = self.level_mode_var.get() == "High/Low"
        labels = ("High Level", "Low Level", LEVEL_UNITS.keys(), "V") if is_hl else ("Amplitude", "Offset", AMP_UNITS.keys(), "Vpp")
        self.amp_label.config(text=labels[0])
        self.offset_label.config(text=labels[1])
        menu = self.amp_unit_menu["menu"]
        menu.delete(0, "end")
        for opt in labels[2]:
            menu.add_command(label=opt, command=lambda v=opt: (self.amp_unit_var.set(v), self.apply_waveform()))
        self.amp_unit_var.set(labels[3])
        # Auto-convert Low level mV->V in High/Low mode
        if is_hl and self.offset_unit_var.get() == "mV":
            try:
                low_v = float(self.offset_var.get() or 0) / 1000.0
                self.offset_var.set(f"{low_v:.3f}")
                self.offset_unit_var.set("V")
            except ValueError:
                pass
        self.apply_waveform()

    # -- actions sent to the instrument --------------------------------------
    def apply_waveform(self):
        self._update_display()
        self._update_preview()
        self._update_params_display()
        if not self.app.gen:
            return
        is_valid, error_msg = self._validate_voltage()
        if not is_valid:
            messagebox.showerror("Voltage Validation Error", error_msg)
            return
        try:
            gen, ch = self.app.gen, self.channel
            if self.level_mode_var.get() == "Ampl/Offset":
                gen.set_amplitude_unit(AMP_UNITS[self.amp_unit_var.get()], channel=ch)
            freq = self._freq_hz()
            amp, off = self._effective_amp_offset()
            duty = float(self.duty_var.get() or 50)
            wf_map = {
                "Sine": lambda: gen.set_sine(freq, amp, off, channel=ch),
                "Square": lambda: gen.set_square(freq, amp, off, duty, channel=ch),
                "Ramp": lambda: gen.set_ramp(freq, amp, off, duty, channel=ch),
                "Pulse": lambda: gen.set_pulse(freq, amp, off, duty, channel=ch),
                "Triangle": lambda: gen.set_triangle(freq, amp, off, channel=ch),
                "Noise": lambda: gen.set_noise(amp, off, channel=ch),
                "PRBS": lambda: gen.set_prbs(freq, amp, off, channel=ch),
                "DC": lambda: gen.set_dc(off, channel=ch),
                "Arb": lambda: gen.set_arbitrary(freq, amp, off, channel=ch),
            }
            wf_map.get(self.wf_var.get(), lambda: None)()
            gen.write(f"SOUR{ch}:PHAS {self.phase_var.get() or 0}")
            if self.level_mode_var.get() == "High/Low":
                mult = LEVEL_UNITS.get(self.amp_unit_var.get(), 1.0)
                high = float(self.amp_var.get() or 0) * mult
                gen.set_level_high_low(high, self._offset_v(), channel=ch)
            self.app.log_error(gen.check_errors())
            self._update_params_display()
        except Exception as exc:
            messagebox.showerror("Instrument error", str(exc))

    def apply_modulation(self):
        if not self.app.gen:
            return
        try:
            self.app.gen.set_modulation(self.mod_type_var.get(), self.mod_enabled.get(),
                float(self.mod_value_var.get() or 0), MOD_SHAPES.get(self.mod_shape_var.get(), "SIN"),
                channel=self.channel)
            self.app.log_error(self.app.gen.check_errors())
        except Exception as exc:
            messagebox.showerror("Instrument error", str(exc))

    def run_all(self):
        """Send every current field on this channel to the instrument in
        one shot -- the explicit 'RUN' action, rather than relying on
        Enter-per-field or button-per-setting."""
        if not self.app.gen:
            messagebox.showwarning("Not connected", "Connect to the instrument first.")
            return
        # Validate voltage constraints before sending to instrument
        is_valid, error_msg = self._validate_voltage()
        if not is_valid:
            messagebox.showerror("Voltage Validation Error", error_msg)
            return
        self.apply_waveform()
        self.apply_modulation()
        self.apply_load()
        self.app.log_error(self.app.gen.check_errors())

    def sync_phase(self):
        if not self.app.gen:
            return
        try:
            self.app.gen.sync_phase(channel=self.channel)
            self.app.log_error(self.app.gen.check_errors())
        except Exception as exc:
            messagebox.showerror("Instrument error", str(exc))

    def apply_load(self):
        if not self.app.gen:
            return
        try:
            self.app.gen.set_load(self.load_var.get().strip() or "50", channel=self.channel)
            self.app.log_error(self.app.gen.check_errors())
        except Exception as exc:
            messagebox.showerror("Instrument error", str(exc))

    def toggle_output(self):
        if not self.app.gen:
            messagebox.showwarning("Not connected", "Connect to the instrument first.")
            return
        self.output_on = not self.output_on
        try:
            self.app.gen.output(self.output_on, channel=self.channel)
            self.app.log_error(self.app.gen.check_errors())
        except Exception as exc:
            messagebox.showerror("Instrument error", str(exc))
            self.output_on = not self.output_on
            return
        state = "ON" if self.output_on else "OFF"
        bg = ON_GREEN if self.output_on else OFF_RED
        self.output_btn.config(text=f"OUTPUT {self.channel}: {state}", bg=bg)


class Keysight33600GUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Keysight 33600A - Front Panel Control")
        self.configure(bg=BG)
        self.geometry("1500x820")
        self.gen: Optional[Waveform33600A] = None

        self._build_style()
        self._build_top_bar()          # frozen -- stays put regardless of tab

        notebook = ttk.Notebook(self, style="Chan.TNotebook")
        notebook.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        self.ch1 = ChannelPanel(notebook, self, channel=1)
        self.ch2 = ChannelPanel(notebook, self, channel=2)
        notebook.add(self.ch1, text="CHANNEL 1")
        notebook.add(self.ch2, text="CHANNEL 2")

        self._build_bottom_bar()       # also frozen, always visible below the tabs

    # -- styling --------------------------------------------------------------
    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        styles = {
            "Root.TFrame": (BG,),
            "Panel.TFrame": (PANEL,),
            "Panel.TLabel": (PANEL, FG, ("Segoe UI", 9)),
            "Muted.TLabel": (PANEL, MUTED, ("Segoe UI", 8)),
            "ChanTitle.TLabel": (PANEL, ACCENT, ("Segoe UI", 12, "bold")),
            "Top.TFrame": (BG,),
            "Top.TLabel": (BG, FG, ("Segoe UI", 10)),
            "TopMuted.TLabel": (BG, MUTED, ("Segoe UI", 9)),
        }
        for sname, (bg, *rest) in styles.items():
            kw = {"background": bg}
            if rest:
                fg, *font_spec = rest
                kw["foreground"] = fg
                if font_spec:
                    kw["font"] = font_spec[0]
            style.configure(sname, **kw)
        style.configure("Panel.TLabelframe", background=PANEL, foreground=FG, bordercolor=ACCENT)
        style.configure("Panel.TLabelframe.Label", background=PANEL, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        style.configure("Panel.TEntry", fieldbackground="#111417", foreground=FG)
        # Force a visible cursor when the entry has focus  
        style.map(
            "Panel.TEntry",
            insertcolor=[("focus", "white")],        # caret colour on focus
            insertbackground=[("focus", "white")]   # fallback for systems that honour this
            )
        style.configure("Panel.TCheckbutton", background=PANEL, foreground=FG)
        style.configure("WfToggle.TButton", padding=6)
        style.configure("Small.TButton", padding=4, font=("Segoe UI", 8))
        style.configure("Chan.TNotebook", background=BG, borderwidth=0)
        style.configure("Chan.TNotebook.Tab", background=PANEL, foreground=FG, padding=(24, 10), font=("Segoe UI", 10, "bold"))
        style.map("Chan.TNotebook.Tab", background=[("selected", ACCENT)], foreground=[("selected", "white")])

    def _build_top_bar(self):
        bar = ttk.Frame(self, style="Top.TFrame", padding=10)
        bar.pack(fill="x")

        ttk.Label(bar, text="VISA Resource", style="Top.TLabel").pack(side="left")
        self.resource_var = tk.StringVar(value="(auto-detect USB)")
        entry = ttk.Entry(bar, textvariable=self.resource_var, width=34)
        entry.pack(side="left", padx=6)

        ttk.Button(bar, text="Connect", command=self.connect).pack(side="left", padx=4)
        ttk.Button(bar, text="Disconnect", command=self.disconnect).pack(side="left", padx=4)
        ttk.Button(bar, text="Reset (*RST)", command=self.reset_instrument).pack(side="left", padx=4)

        self.conn_status_var = tk.StringVar(value="Not connected")
        ttk.Label(bar, textvariable=self.conn_status_var, style="TopMuted.TLabel").pack(side="left", padx=16)

    def _build_bottom_bar(self):
        bar = ttk.Frame(self, style="Top.TFrame", padding=(10, 4))
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, text="Error queue:", style="Top.TLabel").pack(side="left")
        self.error_var = tk.StringVar(value="--")
        ttk.Label(bar, textvariable=self.error_var, style="TopMuted.TLabel").pack(side="left", padx=6)

    # -- connection management --------------------------------------------------
    def connect(self):
        if pyvisa is None:
            messagebox.showerror("Missing dependency", "Run: pip install pyvisa")
            return
        try:
            resource = self.resource_var.get().strip()
            self.gen = Waveform33600A(resource_string=resource if not resource.startswith("(") else None)
            self.conn_status_var.set(f"Connected: {self.gen.idn}")
        except Exception as exc:
            self.gen = None
            self.conn_status_var.set("Not connected")
            messagebox.showerror("Connection failed", str(exc))
        for ch in (self.ch1, self.ch2):
            ch._update_params_display()

    def disconnect(self):
        if self.gen:
            self.gen.close()
            self.gen = None
        self.conn_status_var.set("Not connected")
        for ch in (self.ch1, self.ch2):
            ch._update_params_display()

    def reset_instrument(self):
        if not self.gen:
            messagebox.showwarning("Not connected", "Connect to the instrument first.")
            return
        self.gen.reset()
        self.log_error(self.gen.check_errors())
        for ch in (self.ch1, self.ch2):
            ch.output_on = False
            ch.output_btn.config(text=f"OUTPUT {ch.channel}: OFF", bg=OFF_RED)

    def log_error(self, msg: str):
        self.error_var.set(msg)

    def destroy(self):
        self.disconnect()
        super().destroy()


if __name__ == "__main__":
    app = Keysight33600GUI()
    app.mainloop()  