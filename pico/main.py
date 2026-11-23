# pico/main.py
# Always-on, low-power Tesla Powerwall 3 dashboard for Raspberry Pi Pico W (RP2040)
# running Pimoroni MicroPython v1.25.0 + Pimoroni Pico Display Pack 2.8" (320x240).
#
# Zero flash writes: 24-hour trend history (288 x 5-min buckets) in fixed RAM arrays (1.1 KB).
# Buttons (press A/B/X again to toggle between Primary and Detail views):
#   A (GP12) : State of Charge (%) trend  <->  Battery & Thermal vitals
#   B (GP13) : Stored Energy (kWh) trend  <->  Grid, AC Inverter & Safety vitals
#   X (GP14) : Live Power Flow aggregate  <->  Solar Strings A-F breakdown
#   Y (GP15) : Cycle backlight (Auto-Dim -> Bright -> Screen Off)
#
# ComponentsQuery (strings & vitals) is polled ONLY while on a detail view.

import array
import gc
import time
import network
from machine import Pin, WDT
from picographics import PicoGraphics, DISPLAY_PICO_DISPLAY_2, PEN_P4
from pimoroni import RGBLED

import config
import tedapi_query
from powerwall_api import Powerwall3Client, RateLimitError, AuthError

# --- Display Initialization (PEN_P4 = 38.4 KB RAM; 16-color custom palette) ---
display = PicoGraphics(
    display=DISPLAY_PICO_DISPLAY_2,
    pen_type=PEN_P4,
    rotate=config.DISPLAY_ROTATE,
)
display.set_font("bitmap8")
WIDTH, HEIGHT = display.get_bounds()

C_BG = display.create_pen(10, 14, 22)
C_PANEL = display.create_pen(20, 28, 42)
C_GRID = display.create_pen(42, 54, 74)
C_TEXT = display.create_pen(240, 244, 250)
C_MUTED = display.create_pen(140, 155, 175)
C_CYAN = display.create_pen(0, 220, 255)
C_MAGENTA = display.create_pen(235, 90, 235)
C_GREEN = display.create_pen(45, 220, 120)
C_AMBER = display.create_pen(255, 190, 40)
C_RED = display.create_pen(255, 75, 75)
C_BLUE = display.create_pen(70, 150, 255)
C_AXIS = display.create_pen(110, 125, 148)
C_CYAN_DIM = display.create_pen(0, 58, 74)
C_MAG_DIM = display.create_pen(68, 24, 76)

# Display Pack 2.8" wires RGB LED to GP26, GP27, GP28 (zeroed at boot to stop dim glow)
led = RGBLED(26, 27, 28)
led.set_rgb(0, 0, 0)

btn_a = Pin(12, Pin.IN, Pin.PULL_UP)
btn_b = Pin(13, Pin.IN, Pin.PULL_UP)
btn_x = Pin(14, Pin.IN, Pin.PULL_UP)
btn_y = Pin(15, Pin.IN, Pin.PULL_UP)

# --- Fixed-Size In-RAM Ring Buffers (Zero Flash Wear, Zero Persistence) ---
# Default: 24h at 5-min buckets = 288 samples (1,152 bytes RAM total).
HIST_HOURS = getattr(config, "HISTORY_HOURS", 24)
HIST_STEP_S = getattr(config, "HISTORY_STEP_S", 300)
CAP = max(2, int((HIST_HOURS * 3600) // HIST_STEP_S))
hist_soc = array.array("H", [0] * CAP)      # tenths of % (0..1000)
hist_wh = array.array("H", [0] * CAP)       # tens of Wh  (0..655350 Wh)
hist_count = 0
hist_head = 0
hist_bucket_ms = None

# --- UI & Runtime State ---
VIEW_SOC = 0
VIEW_KWH = 1
VIEW_FLOW = 2
VIEW_BATT_DETAIL = 3
VIEW_GRID_DETAIL = 4
VIEW_STRINGS_DETAIL = 5
view_mode = VIEW_SOC

BL_AUTO = 0
BL_BRIGHT = 1
BL_OFF = 2
bl_mode = BL_AUTO
last_input_ms = time.ticks_ms()
current_bl = -1.0

latest = {
    "soc_pct": None,
    "remaining_wh": None,
    "full_wh": float(config.DEFAULT_CAPACITY_WH),
    "batt_w": None,
    "solar_w": None,
    "load_w": None,
    "site_w": None,
    "grid_ok": None,
    "site_shutdown": None,
    "exp_lim_w": None,
    "time_str": "--:--",
}
comp_data = None
comp_last_ms = None
comp_poll_pending = False
cooldown_until_ms = None

status_msg = "BOOTING PW3..."
status_is_err = False
consec_errors = 0


def is_detail_view():
    return view_mode in (VIEW_BATT_DETAIL, VIEW_GRID_DETAIL, VIEW_STRINGS_DETAIL)


def set_backlight(level):
    global current_bl
    if abs(level - current_bl) > 0.01:
        display.set_backlight(level)
        current_bl = level


def update_backlight(now_ms):
    if bl_mode == BL_OFF:
        set_backlight(0.0)
    elif bl_mode == BL_BRIGHT:
        set_backlight(config.BACKLIGHT_ACTIVE)
    else:
        idle_ms = time.ticks_diff(now_ms, last_input_ms)
        if idle_ms > config.BACKLIGHT_DIM_AFTER_S * 1000:
            set_backlight(config.BACKLIGHT_IDLE)
        else:
            set_backlight(config.BACKLIGHT_ACTIVE)


def update_led():
    if status_is_err and consec_errors >= 3:
        led.set_rgb(40, 12, 0)              # subtle amber: offline / poll error
    elif (
        config.LOW_SOC_ALERT_PCT > 0
        and latest["soc_pct"] is not None
        and latest["soc_pct"] < config.LOW_SOC_ALERT_PCT
    ):
        led.set_rgb(45, 0, 0)               # subtle red: low SOC alert
    else:
        led.set_rgb(0, 0, 0)                # off when healthy


def push_history(soc_pct, rem_wh, now_ms=None):
    global hist_count, hist_head, hist_bucket_ms
    if now_ms is None:
        now_ms = time.ticks_ms()
    s_val = int(max(0.0, min(100.0, soc_pct)) * 10.0 + 0.5)
    w_val = int(max(0.0, min(655350.0, rem_wh)) / 10.0 + 0.5)
    step_ms = HIST_STEP_S * 1000
    if (
        hist_count == 0
        or hist_bucket_ms is None
        or time.ticks_diff(now_ms, hist_bucket_ms) >= step_ms
    ):
        hist_soc[hist_head] = s_val
        hist_wh[hist_head] = w_val
        hist_head = (hist_head + 1) % CAP
        if hist_count < CAP:
            hist_count += 1
        hist_bucket_ms = now_ms
    else:
        # Keep the live rightmost bucket synced with the latest 30s reading
        last_idx = (hist_head - 1) % CAP
        hist_soc[last_idx] = s_val
        hist_wh[last_idx] = w_val


def iter_history(arr):
    if hist_count < CAP:
        for i in range(hist_count):
            yield arr[i]
    else:
        for i in range(CAP):
            yield arr[(hist_head + i) % CAP]


def fmt_kw(watts):
    if watts is None:
        return "--.- kW"
    return "{:+.2f} kW".format(watts / 1000.0)


def draw_text_right(text, right_x, y, scale=2):
    w = display.measure_text(text, scale)
    display.text(text, right_x - w, y, scale=scale)


def draw_header(title, big_val_str, big_color):
    display.set_pen(C_PANEL)
    display.rectangle(0, 0, WIDTH, 46)
    display.set_pen(C_TEXT)
    display.text("POWERWALL 3", 8, 6, scale=2)
    display.set_pen(C_MUTED)
    display.text(title, 8, 26, scale=2)

    display.set_pen(big_color)
    draw_text_right(big_val_str, WIDTH - 8, 9, scale=3)


def draw_footer():
    display.set_pen(C_PANEL)
    display.rectangle(0, HEIGHT - 20, WIDTH, 20)
    display.set_pen(C_RED if status_is_err else C_GREEN)
    display.circle(10, HEIGHT - 10, 4)
    display.set_pen(C_RED if status_is_err else C_MUTED)
    display.text(status_msg[:22], 20, HEIGHT - 15, scale=1)

    lbl_a = "A:BAT" if view_mode == VIEW_SOC else "A:SOC"
    lbl_b = "B:GRD" if view_mode == VIEW_KWH else "B:kWh"
    lbl_x = "X:STR" if view_mode == VIEW_FLOW else "X:PWR"
    right_info = "{}  {} {} {}".format(latest["time_str"] or "--:--", lbl_a, lbl_b, lbl_x)
    display.set_pen(C_MUTED)
    draw_text_right(right_info, WIDTH - 6, HEIGHT - 15, scale=1)


def _fmt_span_tick(hours_ago):
    if hours_ago >= 1.0:
        if abs(hours_ago - int(hours_ago + 0.5)) < 0.05:
            return "-{}h".format(int(hours_ago + 0.5))
        return "-{:.1f}h".format(hours_ago)
    return "-{}m".format(int(hours_ago * 60.0 + 0.5))


def draw_chart(is_soc):
    if is_soc:
        val = latest["soc_pct"]
        big = "{:.1f}%".format(val) if val is not None else "--.-%"
        color = C_CYAN
        fill_col = C_CYAN_DIM
        if val is not None and val < config.LOW_SOC_ALERT_PCT:
            color = C_RED
        draw_header("State of Charge", big, color)
        max_val = 100.0
    else:
        rem = latest["remaining_wh"]
        full = latest["full_wh"] or float(config.DEFAULT_CAPACITY_WH)
        big = "{:.2f}kWh".format(rem / 1000.0) if rem is not None else "--.-kWh"
        color = C_MAGENTA
        fill_col = C_MAG_DIM
        draw_header("Stored Energy", big, color)
        max_val = max(1.0, full / 1000.0)

    x0, y0 = 42, 56
    x1, y1 = WIDTH - 10, HEIGHT - 32
    pw = x1 - x0
    ph = y1 - y0
    span_h = (CAP * HIST_STEP_S) / 3600.0

    # Shaded area fill first so gridlines & curve stay crisp on top
    if hist_count > 0:
        src = hist_soc if is_soc else hist_wh
        scale_div = 10.0 if is_soc else 100.0
        step_den = CAP - 1 if CAP > 1 else 1
        offset = CAP - hist_count

        prev_x = None
        prev_y = None
        display.set_pen(fill_col)
        for idx, raw_v in enumerate(iter_history(src)):
            ratio = (raw_v / scale_div) / max_val
            if ratio < 0.0:
                ratio = 0.0
            elif ratio > 1.0:
                ratio = 1.0
            px = x0 + ((offset + idx) * pw) // step_den
            py = y1 - int(ratio * ph)
            if prev_x is not None and px > prev_x:
                dx = px - prev_x
                dy = py - prev_y
                for step in range(1, dx + 1):
                    fx = prev_x + step
                    fy = prev_y + (dy * step) // dx
                    if fy < y1:
                        display.line(fx, fy, fx, y1 - 1)
            elif py < y1:
                display.line(px, py, px, y1 - 1)
            prev_x, prev_y = px, py

    # Horizontal reference gridlines (0/25/50/75/100%, labeled at 0/50/100%)
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = y1 - int(frac * ph)
        display.set_pen(C_GRID)
        display.line(x0, gy, x1, gy)
        if frac in (0.0, 0.5, 1.0):
            lbl_val = max_val * frac
            lbl = "{:.0f}".format(lbl_val) if max_val >= 10 else "{:.1f}".format(lbl_val)
            display.set_pen(C_MUTED)
            draw_text_right(lbl, x0 - 4, gy - 4, scale=1)

    # Vertical time gridlines & labels (-24h, -18h, -12h, -6h, now)
    for frac in (0.25, 0.5, 0.75):
        gx = x0 + int(frac * pw)
        display.set_pen(C_GRID)
        display.line(gx, y0, gx, y1)
        tick_lbl = _fmt_span_tick(span_h * (1.0 - frac))
        tw = display.measure_text(tick_lbl, 1)
        display.set_pen(C_MUTED)
        display.text(tick_lbl, gx - tw // 2, y1 + 3, scale=1)

    display.set_pen(C_AXIS)
    display.line(x0, y0, x0, y1)
    display.line(x0, y1, x1, y1)

    display.set_pen(C_MUTED)
    display.text(_fmt_span_tick(span_h), x0, y1 + 3, scale=1)
    draw_text_right("now", x1, y1 + 3, scale=1)

    if hist_count == 0:
        display.set_pen(C_MUTED)
        display.text("Waiting for PW3 data...", x0 + 36, y0 + ph // 2 - 8, scale=1)
        return

    # Vivid top curve + live "now" dot
    prev_x = None
    prev_y = None
    display.set_pen(color)
    for idx, raw_v in enumerate(iter_history(src)):
        ratio = (raw_v / scale_div) / max_val
        if ratio < 0.0:
            ratio = 0.0
        elif ratio > 1.0:
            ratio = 1.0
        px = x0 + ((offset + idx) * pw) // step_den
        py = y1 - int(ratio * ph)
        if prev_x is not None:
            display.line(prev_x, prev_y, px, py, 2)
        else:
            display.circle(px, py, 2)
        prev_x, prev_y = px, py

    if prev_x is not None:
        display.set_pen(C_TEXT)
        display.circle(prev_x, prev_y, 3)


def draw_power_flow():
    soc = latest["soc_pct"]
    big = "{:.1f}%".format(soc) if soc is not None else "--.-%"
    draw_header("Live Power Flow", big, C_GREEN if (soc or 0) >= 20 else C_RED)

    rem_kwh = (latest["remaining_wh"] / 1000.0) if latest["remaining_wh"] is not None else 0.0
    full_kwh = (latest["full_wh"] / 1000.0) if latest["full_wh"] else 13.5

    bx, by, bw, bh = 10, 56, WIDTH - 20, 22
    display.set_pen(C_PANEL)
    display.rectangle(bx, by, bw, bh)
    if soc is not None:
        fill_w = int((max(0.0, min(100.0, soc)) / 100.0) * (bw - 4))
        bar_col = C_GREEN if soc >= 35 else (C_AMBER if soc >= 20 else C_RED)
        display.set_pen(bar_col)
        if fill_w > 0:
            display.rectangle(bx + 2, by + 2, fill_w, bh - 4)
    display.set_pen(C_TEXT)
    display.text("{:.2f} / {:.2f} kWh".format(rem_kwh, full_kwh), bx + 6, by + 7, scale=1)

    cards = (
        ("SOLAR", latest["solar_w"], C_AMBER, 10, 88),
        ("HOME", latest["load_w"], C_BLUE, 165, 88),
        ("BATTERY", latest["batt_w"], C_CYAN, 10, 146),
        ("GRID", latest["site_w"], C_MAGENTA, 165, 146),
    )
    cw, ch = 145, 50
    for name, watts, col, cx, cy in cards:
        display.set_pen(C_PANEL)
        display.rectangle(cx, cy, cw, ch)
        display.set_pen(col)
        display.rectangle(cx, cy, 4, ch)
        display.text(name, cx + 10, cy + 6, scale=1)
        display.set_pen(C_TEXT)
        display.text(fmt_kw(watts), cx + 10, cy + 22, scale=2)

    grid_str = "GRID: OK" if latest["grid_ok"] is not False else "GRID: ISLAND"
    free_kb = gc.mem_free() // 1024
    display.set_pen(C_MUTED)
    display.text("{}   RAM: {}KB free".format(grid_str, free_kb), 10, 204, scale=1)


def _short_state(val, max_len=14):
    if not val:
        return "--"
    s = str(val).strip()
    if not s:
        return "--"
    # Strip any "<PREFIX>_State_" or "<PREFIX>_STATE_" segment (e.g. RSD_State_Armed)
    pos = s.lower().find("_state_")
    if pos >= 0 and pos + 7 < len(s):
        s = s[pos + 7:]
    else:
        for prefix in (
            "PCH_AcMode_",
            "AcMode_",
            "PWS_ProdSwitch_",
            "ProdSwitch_",
            "PWS_SelfTest_",
            "SelfTest_",
            "PWS_RSD_",
            "RSD_TEST_",
            "RSD_",
            "Pv_",
        ):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
    # If still longer than max_len and underscore-delimited, drop leading prefix tokens
    while len(s) > max_len and "_" in s:
        tail = s.split("_", 1)[1]
        if not tail:
            break
        s = tail
    s = s.rstrip("_")
    return s[:max_len] if s else "--"


def _draw_detail_card(cx, cy, cw, ch, col, title, main_str, sub_str):
    display.set_pen(C_PANEL)
    display.rectangle(cx, cy, cw, ch)
    display.set_pen(col)
    display.rectangle(cx, cy, 4, ch)
    display.text(title, cx + 10, cy + 5, scale=1)
    display.set_pen(C_TEXT)
    display.text(main_str[:11], cx + 10, cy + 18, scale=2)
    display.set_pen(C_MUTED)
    display.text(sub_str[:21], cx + 10, cy + 42, scale=1)


def _draw_warning_bar(sev_col, left_text, right_text=""):
    """Car-warning-light status bar: always visible, green/amber/red by severity."""
    bx, by, bw, bh = 10, 184, WIDTH - 20, 28
    display.set_pen(C_PANEL)
    display.rectangle(bx, by, bw, bh)
    display.set_pen(sev_col)
    display.rectangle(bx, by, 4, bh)
    display.circle(bx + 13, by + 14, 4)
    display.text(left_text[:25], bx + 22, by + 10, scale=1)
    if right_text:
        draw_text_right(right_text[:20], bx + bw - 8, by + 10, scale=1)


def _sev_color(sev):
    if sev >= 2:
        return C_RED
    if sev == 1:
        return C_AMBER
    return C_GREEN


def draw_batt_detail():
    c = comp_data
    tmax = c["pack_temp_max"] if c else None
    big = "{:.1f}C".format(tmax) if tmax is not None else "--.-C"
    if tmax is None:
        hdr_col = C_GREEN
    elif tmax >= 50.0:
        hdr_col = C_RED
    elif tmax >= 42.0:
        hdr_col = C_AMBER
    else:
        hdr_col = C_GREEN
    draw_header("Battery & Thermal", big, hdr_col)

    if c is None:
        display.set_pen(C_MUTED)
        display.text("Polling PW3 vitals...", 92, 125, scale=1)
        return

    tmin = c["pack_temp_min"]
    shunt = c["shunt_temp"]
    dt = (tmax - tmin) if (tmax is not None and tmin is not None) else None
    temp_main = "{:.1f} C".format(tmax) if tmax is not None else "--.- C"
    tmin_s = "{:.1f}".format(tmin) if tmin is not None else "--"
    shunt_s = "{:.1f}".format(shunt) if shunt is not None else "--"
    if tmax is not None and tmax >= 50.0:
        pack_sev = 2
    elif (tmax is not None and tmax >= 42.0) or (dt is not None and dt >= 7.0):
        pack_sev = 1
    else:
        pack_sev = 0
    _draw_detail_card(
        10, 54, 145, 58, _sev_color(pack_sev), "PACK TEMP MAX", temp_main,
        "MIN {}C  SH {}C".format(tmin_s, shunt_s),
    )

    amb = c["inv_ambient_temp"]
    amb_main = "{:.1f} C".format(amb) if amb is not None else "--.- C"
    if amb is not None and amb >= 65.0:
        amb_sev = 2
    elif amb is not None and amb >= 55.0:
        amb_sev = 1
    else:
        amb_sev = 0
    _draw_detail_card(
        165, 54, 145, 58, _sev_color(amb_sev), "INV AMBIENT", amb_main,
        "HVP: " + _short_state(c["hvp_state"], 15),
    )

    fa = c["fan_rpm_a"]
    fb = c["fan_rpm_b"]
    da = c["fan_duty_a"]
    db = c["fan_duty_b"]
    if fa is not None and fb is not None:
        fan_main = "{} RPM".format(int((fa + fb) * 0.5 + 0.5))
    elif fa is not None:
        fan_main = "{} RPM".format(int(fa + 0.5))
    else:
        fan_main = "-- RPM"
    fan_max = None
    if da is not None and db is not None:
        fan_max = da if da >= db else db
    elif da is not None:
        fan_max = da
    elif db is not None:
        fan_max = db
    if fan_max is not None and fan_max >= 90.0:
        fan_sev = 2
    elif fan_max is not None and fan_max >= 75.0:
        fan_sev = 1
    else:
        fan_sev = 0
    fa_s = str(int(fa + 0.5)) if fa is not None else "--"
    fb_s = str(int(fb + 0.5)) if fb is not None else "--"
    da_s = "{}%".format(int(da + 0.5)) if da is not None else "--"
    db_s = "{}%".format(int(db + 0.5)) if db is not None else "--"
    _draw_detail_card(
        10, 118, 145, 58, _sev_color(fan_sev), "COOLING FANS", fan_main,
        "A:{}({}) B:{}({})".format(fa_s, da_s, fb_s, db_s),
    )

    batt_dc = c["pch_batt_w"] if c["pch_batt_w"] is not None else latest["batt_w"]
    if batt_dc is None:
        batt_col = C_MUTED
        bdir = "--"
    elif batt_dc < -50.0:
        batt_col = C_GREEN
        bdir = "CHG"
    elif batt_dc > 50.0:
        batt_col = C_CYAN
        bdir = "DIS"
    else:
        batt_col = C_MUTED
        bdir = "IDLE"
    if c["slow_pv_sum_w"] is not None:
        batt_sub = "{}  PV:{:.2f}kW".format(bdir, c["slow_pv_sum_w"] / 1000.0)
    else:
        batt_sub = "{}  ST:{}".format(bdir, _short_state(c["pch_state"], 11))
    _draw_detail_card(
        165, 118, 145, 58, batt_col, "BATT DC PWR", fmt_kw(batt_dc), batt_sub,
    )

    oob_a = c["temp_oob"]
    oob_b = c["temp_oob_chg"]
    if oob_a is not None or oob_b is not None:
        oob_total = int((oob_a or 0) + (oob_b or 0))
        oob_s = str(oob_total)
        oob_sev = 2 if oob_total > 0 else 0
    else:
        oob_total = None
        oob_s = "--"
        oob_sev = 0

    therm_sev = max(pack_sev, amb_sev, fan_sev, oob_sev)
    if oob_sev == 2:
        left_lbl = "OOB TEMP WARN!"
    elif therm_sev == 2:
        left_lbl = "OVER-TEMP WARN!"
    elif therm_sev == 1:
        left_lbl = "THERMAL: WARM"
    else:
        left_lbl = "THERMAL: OK"
    dt_s = "{:.1f}C".format(dt) if dt is not None else "--"
    _draw_warning_bar(_sev_color(therm_sev), left_lbl, "dT:{}  OOB:{}".format(dt_s, oob_s))


def draw_grid_detail():
    c = comp_data
    vab = c["ac_v_ab"] if c else None
    big = "{:.1f}V".format(vab) if vab is not None else "--.-V"
    hdr_col = C_GREEN if latest["grid_ok"] is not False else C_RED
    draw_header("Grid & AC Vitals", big, hdr_col)

    if c is None:
        display.set_pen(C_MUTED)
        display.text("Polling PW3 vitals...", 92, 125, scale=1)
        return

    van = c["ac_v_an"]
    vbn = c["ac_v_bn"]
    dv = abs(van - vbn) if (van is not None and vbn is not None) else None
    v_main = "{:.1f} V".format(vab) if vab is not None else "--.- V"
    l1_s = "{:.1f}".format(van) if van is not None else "--"
    l2_s = "{:.1f}".format(vbn) if vbn is not None else "--"
    if vab is not None and (vab >= 254.0 or vab <= 222.0):
        v_sev = 2
    elif (vab is not None and (vab >= 250.0 or vab <= 228.0)) or (dv is not None and dv >= 4.0):
        v_sev = 1
    else:
        v_sev = 0
    _draw_detail_card(
        10, 54, 145, 58, _sev_color(v_sev), "AC SPLIT-PHASE", v_main,
        "L1:{}V  L2:{}V".format(l1_s, l2_s),
    )

    freq = c["ac_freq"]
    f_main = "{:.2f}Hz".format(freq) if freq is not None else "--.--Hz"
    if freq is not None and (freq >= 60.5 or freq <= 59.3):
        f_sev = 2
    elif freq is not None and (freq >= 60.2 or freq <= 59.8):
        f_sev = 1
    else:
        f_sev = 0
    grid_sub = "GRID: OK" if latest["grid_ok"] is not False else "GRID: ISLAND"
    _draw_detail_card(165, 54, 145, 58, _sev_color(f_sev), "FREQUENCY", f_main, grid_sub)

    ac_w = c["ac_power_w"] if c["ac_power_w"] is not None else c["pch_batt_w"]
    _draw_detail_card(
        10, 118, 145, 58, C_MAGENTA, "INVERTER AC", fmt_kw(ac_w),
        "MODE: " + _short_state(c["ac_mode"], 14),
    )

    site_w = latest["site_w"]
    if site_w is None:
        grid_col = C_MUTED
        gdir = "--"
    elif site_w < -50.0:
        grid_col = C_GREEN
        gdir = "EXP"
    elif site_w > 50.0:
        grid_col = C_AMBER
        gdir = "IMP"
    else:
        grid_col = C_CYAN
        gdir = "IDLE"
    load_w = latest["load_w"]
    home_s = "{:.2f}kW".format(load_w / 1000.0) if load_w is not None else "--.-kW"
    _draw_detail_card(
        165, 118, 145, 58, grid_col, "GRID FLOW", fmt_kw(site_w),
        "{} HOME:{}".format(gdir, home_s),
    )

    # Bottom car-warning-light bar: Export Limit Headroom + Grid Compliance
    exp_lim = latest["exp_lim_w"]
    if exp_lim is None:
        cfg_lim = getattr(config, "EXPORT_LIMIT_W", 0)
        if cfg_lim and cfg_lim > 0:
            exp_lim = float(cfg_lim)
    export_w = -site_w if (site_w is not None and site_w < 0.0) else 0.0

    if exp_lim is not None and exp_lim > 0.0:
        exp_pct = int((export_w / exp_lim) * 100.0 + 0.5)
        left_lbl = "EXP:{:.1f}/{:.1f}kW ({}%)".format(
            export_w / 1000.0, exp_lim / 1000.0, exp_pct
        )
        exp_sev = 2 if exp_pct >= 95 else (1 if exp_pct >= 80 else 0)
    elif exp_lim == 0:
        left_lbl = "EXP LIM: 0kW"
        exp_sev = 2 if export_w >= 200.0 else (1 if export_w >= 50.0 else 0)
    else:
        left_lbl = "EXP LIM: --"
        exp_sev = 0

    shut_sev = 2 if latest["site_shutdown"] is True else 0
    comp_sev = max(v_sev, f_sev, shut_sev)
    if shut_sev == 2:
        right_lbl = "SHUTDOWN!"
    elif v_sev == 2:
        right_lbl = "V OUT!"
    elif f_sev == 2:
        right_lbl = "CURTAIL!" if (freq is not None and freq >= 60.5) else "FREQ!"
    elif v_sev == 1:
        if vab is not None and vab >= 250.0:
            right_lbl = "V HIGH"
        elif vab is not None and vab <= 228.0:
            right_lbl = "V LOW"
        else:
            right_lbl = "dV:{:.1f}V".format(dv)
    elif f_sev == 1:
        right_lbl = "FREQ WARN"
    else:
        right_lbl = "OK dV:{:.1f}V".format(dv) if dv is not None else "GRID OK"

    _draw_warning_bar(_sev_color(max(exp_sev, comp_sev)), left_lbl, right_lbl)


def draw_strings_detail():
    c = comp_data
    dc_sum = c["strings_dc_w"] if c else None
    ac_solar = latest["solar_w"]
    hdr_w = dc_sum if (dc_sum is not None and dc_sum > 0.0) else ac_solar
    big = "{:.2f}kW".format(hdr_w / 1000.0) if hdr_w is not None else "--.-kW"
    draw_header("Solar Strings", big, C_AMBER)

    if c is None:
        display.set_pen(C_MUTED)
        display.text("Polling PW3 strings...", 88, 125, scale=1)
        return

    # Sub-summary: DC sum of strings A-F vs AC aggregate SOLAR power
    dc_kw = dc_sum / 1000.0
    if ac_solar is not None:
        if dc_sum >= 100.0 and ac_solar >= 0.0:
            eff = int(min(100.0, (ac_solar / dc_sum) * 100.0) + 0.5)
            summary = "DC:{:.2f}kW  AC:{:.2f}kW  EFF:{}%".format(
                dc_kw, ac_solar / 1000.0, eff
            )
        else:
            summary = "DC SUM:{:.2f}kW   AC SOLAR:{:.2f}kW".format(
                dc_kw, ac_solar / 1000.0
            )
    else:
        summary = "DC SUM:{:.2f}kW".format(dc_kw)
    display.set_pen(C_MUTED)
    display.text(summary, 10, 50, scale=1)

    bar_x = 32
    bar_w = 132
    for idx, srec in enumerate(c["strings"]):
        ry = 64 + idx * 25
        sw = srec["w"] or 0.0
        sv = srec["v"]
        si = srec["i"]
        st = srec["state"] or ""
        is_active = ("Active" in st) or sw >= 5.0

        display.set_pen(C_PANEL)
        display.rectangle(8, ry, WIDTH - 16, 22)

        # String letter badge A..F
        if sw >= 10.0:
            lbl_col = C_AMBER
        elif is_active:
            lbl_col = C_CYAN
        else:
            lbl_col = C_MUTED
        display.set_pen(lbl_col)
        display.text(srec["id"], 13, ry + 4, scale=2)

        # Proportional share bar
        display.set_pen(C_GRID)
        display.rectangle(bar_x, ry + 3, bar_w, 8)
        if dc_sum > 1.0 and sw > 0.0:
            frac = sw / dc_sum
            if frac > 1.0:
                frac = 1.0
            fill_w = int(frac * bar_w)
            if fill_w > 0:
                display.set_pen(C_AMBER)
                display.rectangle(bar_x, ry + 3, fill_w, 8)
            pct = int(frac * 100.0 + 0.5)
        else:
            pct = 0

        # Compact electrical info below bar (V / A or state)
        display.set_pen(C_MUTED)
        if is_active and (sv is not None or si is not None):
            va_str = "{:.0f}V  {:.1f}A".format(sv or 0.0, si or 0.0)
        elif st:
            va_str = _short_state(st, 16)
        else:
            va_str = "--"
        display.text(va_str, bar_x, ry + 13, scale=1)

        # Right-aligned power (kW) + % share of total solar
        display.set_pen(C_TEXT if sw >= 10.0 else C_MUTED)
        draw_text_right("{:.2f}kW".format(sw / 1000.0), 274, ry + 4, scale=2)
        display.set_pen(C_AMBER if pct > 0 else C_MUTED)
        draw_text_right("{:2d}%".format(pct), WIDTH - 12, ry + 8, scale=1)


def draw_ui():
    display.set_pen(C_BG)
    display.clear()
    if view_mode == VIEW_SOC:
        draw_chart(True)
    elif view_mode == VIEW_KWH:
        draw_chart(False)
    elif view_mode == VIEW_FLOW:
        draw_power_flow()
    elif view_mode == VIEW_BATT_DETAIL:
        draw_batt_detail()
    elif view_mode == VIEW_GRID_DETAIL:
        draw_grid_detail()
    else:
        draw_strings_detail()
    draw_footer()
    display.update()


# --- Wi-Fi Management (CYW43439 Reliable Connect + Diagnostics) ---

_WIFI_ERR_NAMES = {
    -1: "WIFI FAIL (-1)",
    -2: "NO AP (-2)",
    -3: "BAD PWD (-3)",
    0: "WIFI IDLE (0)",
    1: "JOINING (1)",
    2: "NO DHCP IP (2)",
}
_wifi_ever_tried = False


def init_wifi():
    try:
        network.country(config.WIFI_COUNTRY)
    except Exception:
        pass
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    time.sleep_ms(300)  # let CYW43439 firmware finish booting before connect
    try:
        # Keep PM disabled during association/DHCP for maximum reliability
        wlan.config(pm=getattr(network.WLAN, "PM_NONE", 0xA11140))
    except Exception:
        pass
    return wlan


def _log_wifi_scan(wlan, target_ssid):
    try:
        nets = wlan.scan()
        found = None
        for row in nets:
            ssid = row[0].decode("utf-8", "ignore")
            if ssid == target_ssid:
                found = row
                break
        if found:
            print(
                "WiFi scan: FOUND '{}' ch={} rssi={}dBm sec={}".format(
                    target_ssid, found[2], found[3], found[4]
                )
            )
        else:
            seen = [r[0].decode("utf-8", "ignore") for r in nets if r[0]]
            print("WiFi scan: '{}' NOT FOUND. Visible 2.4GHz SSIDs: {}".format(target_ssid, seen[:10]))
    except Exception as e:
        print("WiFi scan error:", e)


def ensure_wifi(wlan, wdt_feed):
    global status_msg, status_is_err, _wifi_ever_tried
    if wlan.isconnected():
        return True

    status_msg = "CONNECTING WIFI..."
    status_is_err = True
    draw_ui()

    if _wifi_ever_tried:
        try:
            wlan.disconnect()
        except Exception:
            pass
        time.sleep_ms(300)
    _wifi_ever_tried = True

    try:
        wlan.config(pm=getattr(network.WLAN, "PM_NONE", 0xA11140))
    except Exception:
        pass

    print("WiFi: connecting to '{}'...".format(config.WIFI_SSID))
    wlan.connect(config.WIFI_SSID, config.WIFI_PASS)

    deadline = time.ticks_add(time.ticks_ms(), 20000)
    st = wlan.status()
    while True:
        if wdt_feed:
            wdt_feed()
        st = wlan.status()
        if st == 3 or wlan.isconnected():
            break
        if st < 0:
            status_msg = _WIFI_ERR_NAMES.get(st, "WIFI ERR ({})".format(st))
            status_is_err = True
            print("WiFi failed immediately:", status_msg)
            _log_wifi_scan(wlan, config.WIFI_SSID)
            return False
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            status_msg = "TIMEOUT " + _WIFI_ERR_NAMES.get(st, "({})".format(st))
            status_is_err = True
            print("WiFi timed out. Last status={} ({})".format(st, status_msg))
            _log_wifi_scan(wlan, config.WIFI_SSID)
            return False
        time.sleep_ms(250)

    if config.WIFI_POWERSAVE:
        try:
            wlan.config(pm=getattr(network.WLAN, "PM_POWERSAVE", 0x000011))
        except Exception:
            pass

    ip = wlan.ifconfig()[0]
    status_msg = "WIFI " + ip
    status_is_err = False
    print("WiFi connected! IP:", ip, "ifconfig:", wlan.ifconfig())
    return True


def main():
    global view_mode, bl_mode, last_input_ms, status_msg, status_is_err, consec_errors
    global comp_data, comp_last_ms, comp_poll_pending, cooldown_until_ms

    set_backlight(config.BACKLIGHT_ACTIVE)
    draw_ui()

    # Brief grace window before arming WDT so Thonny can always interrupt over USB
    if config.ENABLE_WDT:
        time.sleep_ms(2500)
        wdt = WDT(timeout=8000)
        wdt_feed = wdt.feed
    else:
        wdt_feed = None

    wlan = init_wifi()
    client = Powerwall3Client(
        host=config.PW_HOST,
        port=config.PW_PORT,
        gw_password=config.GW_PASSWORD,
        timeout_s=config.SOCKET_TIMEOUT_S,
        soc_scale_to_app=config.SOC_SCALE_TO_APP,
        utc_offset_hours=config.UTC_OFFSET_HOURS,
        graphql_payload=getattr(tedapi_query, "GRAPHQL_PAYLOAD", b""),
        query_text=tedapi_query.QUERY_TEXT,
        query_sig=tedapi_query.QUERY_SIG,
        query_vars=tedapi_query.QUERY_VARS,
        is_real_signed_query=tedapi_query.IS_REAL_SIGNED_QUERY,
        components_payload=getattr(tedapi_query, "COMPONENTS_PAYLOAD", b""),
        components_query_text=getattr(tedapi_query, "COMPONENTS_QUERY_TEXT", b""),
        components_query_sig=getattr(tedapi_query, "COMPONENTS_QUERY_SIG", b""),
        components_query_vars=getattr(tedapi_query, "COMPONENTS_QUERY_VARS", b"{}"),
    )

    gc.collect()
    print("Boot complete. Free RAM:", gc.mem_free(), "bytes")

    prev_btn = [1, 1, 1, 1]
    next_poll_ms = time.ticks_ms()
    comp_fresh_ms = min(15, config.POLL_INTERVAL_S) * 1000

    def _run_components_poll():
        global comp_data, comp_last_ms, comp_poll_pending, cooldown_until_ms
        global status_msg, status_is_err, consec_errors
        try:
            c = client.poll_components(wdt_feed=wdt_feed)
            comp_data = c
            comp_last_ms = time.ticks_ms()
            comp_poll_pending = False
            if c.get("time_str") and latest["time_str"] == "--:--":
                latest["time_str"] = c["time_str"]
            status_msg = "OK (VITALS)"
            status_is_err = False
            print(
                "VITALS: DC={:.0f}W VAB={}V FREQ={}Hz TMAX={}C OOB={}/{} FREE={}B".format(
                    c["strings_dc_w"],
                    c["ac_v_ab"],
                    c["ac_freq"],
                    c["pack_temp_max"],
                    c["temp_oob"],
                    c["temp_oob_chg"],
                    gc.mem_free(),
                )
            )
            return True
        except RateLimitError as e:
            consec_errors += 1
            comp_poll_pending = False
            cooldown_until_ms = time.ticks_add(
                time.ticks_ms(), config.RATE_LIMIT_COOLDOWN_S * 1000
            )
            status_msg = "429 COOLDOWN 5m"
            status_is_err = True
            print("Vitals rate limited:", e)
            return False
        except AuthError as e:
            consec_errors += 1
            comp_poll_pending = False
            status_msg = "AUTH ERR: " + str(e)
            status_is_err = True
            print("Vitals auth error:", e)
            return False
        except Exception as e:
            comp_poll_pending = False
            status_msg = "VITALS: " + str(e)[:14]
            status_is_err = True
            print("Vitals poll error:", e)
            return False

    while True:
        if wdt_feed:
            wdt_feed()
        now = time.ticks_ms()

        # 1. Edge-triggered debounced button handling (press A/B/X again to toggle detail)
        cur_btn = [btn_a.value(), btn_b.value(), btn_x.value(), btn_y.value()]
        dirty = False
        for i in range(4):
            if prev_btn[i] == 1 and cur_btn[i] == 0:
                last_input_ms = now
                if bl_mode == BL_OFF and i != 3:
                    bl_mode = BL_AUTO
                elif i == 0:
                    view_mode = VIEW_BATT_DETAIL if view_mode == VIEW_SOC else VIEW_SOC
                elif i == 1:
                    view_mode = VIEW_GRID_DETAIL if view_mode == VIEW_KWH else VIEW_KWH
                elif i == 2:
                    view_mode = VIEW_STRINGS_DETAIL if view_mode == VIEW_FLOW else VIEW_FLOW
                elif i == 3:
                    bl_mode = (bl_mode + 1) % 3
                if is_detail_view():
                    if comp_last_ms is None or time.ticks_diff(now, comp_last_ms) >= comp_fresh_ms:
                        comp_poll_pending = True
                else:
                    comp_poll_pending = False
                dirty = True
        prev_btn = cur_btn
        update_backlight(now)

        if dirty:
            draw_ui()

        in_cooldown = (
            cooldown_until_ms is not None
            and time.ticks_diff(cooldown_until_ms, now) > 0
        )

        # 2. Immediate on-demand ComponentsQuery poll when user toggles into a detail view
        if comp_poll_pending and is_detail_view() and not in_cooldown:
            if ensure_wifi(wlan, wdt_feed):
                status_msg = "POLLING VITALS..."
                status_is_err = False
                draw_ui()
                _run_components_poll()
                if cooldown_until_ms is not None and time.ticks_diff(cooldown_until_ms, time.ticks_ms()) > 0:
                    next_poll_ms = cooldown_until_ms
                update_led()
                draw_ui()
            else:
                comp_poll_pending = False
                update_led()
                draw_ui()

        # 3. Cooperative Powerwall 3 poll (never inside a Timer ISR)
        if time.ticks_diff(now, next_poll_ms) >= 0:
            if ensure_wifi(wlan, wdt_feed):
                try:
                    data = client.poll(wdt_feed=wdt_feed)
                    latest["soc_pct"] = data["soc_pct"]
                    latest["remaining_wh"] = data["remaining_wh"]
                    latest["full_wh"] = data["full_wh"]
                    latest["batt_w"] = data["batt_w"]
                    latest["solar_w"] = data["solar_w"]
                    latest["load_w"] = data["load_w"]
                    latest["site_w"] = data["site_w"]
                    latest["grid_ok"] = data["grid_ok"]
                    latest["site_shutdown"] = data["site_shutdown"]
                    latest["exp_lim_w"] = data["exp_lim_w"]
                    if data["time_str"]:
                        latest["time_str"] = data["time_str"]

                    push_history(latest["soc_pct"], latest["remaining_wh"])

                    consec_errors = 0
                    cooldown_until_ms = None
                    status_msg = "OK (TEDAPI)"
                    status_is_err = False
                    delay_ms = config.POLL_INTERVAL_S * 1000
                    print(
                        "SOC={:.1f}% REM={:.2f}kWh BATT={}W SOLAR={}W EXPLIM={}W FREE={}B".format(
                            latest["soc_pct"],
                            latest["remaining_wh"] / 1000.0,
                            latest["batt_w"],
                            latest["solar_w"],
                            latest["exp_lim_w"],
                            gc.mem_free(),
                        )
                    )

                    # Refresh ComponentsQuery ONLY while staying on a detail view
                    if is_detail_view():
                        _run_components_poll()
                        if cooldown_until_ms is not None and time.ticks_diff(cooldown_until_ms, time.ticks_ms()) > 0:
                            delay_ms = config.RATE_LIMIT_COOLDOWN_S * 1000
                except RateLimitError as e:
                    consec_errors += 1
                    status_msg = "429 COOLDOWN 5m"
                    status_is_err = True
                    delay_ms = config.RATE_LIMIT_COOLDOWN_S * 1000
                    cooldown_until_ms = time.ticks_add(time.ticks_ms(), delay_ms)
                    print("Rate limited:", e)
                except AuthError as e:
                    consec_errors += 1
                    status_msg = "AUTH ERR: " + str(e)
                    status_is_err = True
                    delay_ms = 60000
                    print("Auth error:", e)
                except Exception as e:
                    consec_errors += 1
                    status_msg = "ERR: " + str(e)[:18]
                    status_is_err = True
                    backoff_s = min(120, 10 * (1 << min(consec_errors, 4)))
                    delay_ms = backoff_s * 1000
                    print("Poll error:", e)
            else:
                consec_errors += 1
                delay_ms = 15000

            next_poll_ms = time.ticks_add(time.ticks_ms(), delay_ms)
            update_led()
            draw_ui()

        time.sleep_ms(50)


if __name__ == "__main__":
    main()
