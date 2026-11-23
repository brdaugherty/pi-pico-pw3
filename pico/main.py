# pico/main.py
# Always-on Tesla Powerwall 3 dashboard for Raspberry Pi Pico W (RP2040, v1.25.0)
# + Pimoroni Pico Display Pack 2.8" (320x240, PEN_P4, zero flash writes).
#
# Buttons (press A/B/X again to toggle Primary <-> Detail):
#   A (GP12) : SoC + Batt In/Out 24h <->  Battery & Thermal (OOB warning light)
#   B (GP13) : Grid In/Out 24h       <->  Grid & AC Vitals (ieee20305 export limit)
#   X (GP14) : Live Power Flow       <->  Solar Strings A-F breakdown
#   Y (GP15) : Backlight cycle (Auto-Dim -> Bright -> Off)

import gc
import config
from picographics import PicoGraphics, DISPLAY_PICO_DISPLAY_2, PEN_P4

# Allocate 38.4 KB framebuffer FIRST (before imports) to prevent RP2040 heap fragmentation
display = PicoGraphics(
    display=DISPLAY_PICO_DISPLAY_2, pen_type=PEN_P4, rotate=config.DISPLAY_ROTATE
)
display.set_font("bitmap8")
WIDTH, HEIGHT = display.get_bounds()
gc.collect()

import array
import time
import network
from machine import Pin, WDT
from pimoroni import RGBLED

import tedapi_query
from powerwall_api import Powerwall3Client, RateLimitError, AuthError

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

led = RGBLED(26, 27, 28)
led.set_rgb(0, 0, 0)

btn_a = Pin(12, Pin.IN, Pin.PULL_UP)
btn_b = Pin(13, Pin.IN, Pin.PULL_UP)
btn_x = Pin(14, Pin.IN, Pin.PULL_UP)
btn_y = Pin(15, Pin.IN, Pin.PULL_UP)

# 24h in-RAM ring buffers (288 x 5-min buckets, 5 series = 2.8 KB, zero flash wear)
HIST_HOURS = getattr(config, "HISTORY_HOURS", 24)
HIST_STEP_S = getattr(config, "HISTORY_STEP_S", 300)
CAP = max(2, int((HIST_HOURS * 3600) // HIST_STEP_S))
hist_soc = array.array("H", [0] * CAP)       # tenths of % (0..1000)
hist_batt_in = array.array("H", [0] * CAP)   # tens of W charging (0..655 kW)
hist_batt_out = array.array("H", [0] * CAP)  # tens of W discharging
hist_grid_in = array.array("H", [0] * CAP)   # tens of W importing
hist_grid_out = array.array("H", [0] * CAP)  # tens of W exporting
hist_count = 0
hist_head = 0
hist_bucket_ms = None
_bk_n = 0
_bk_bi = 0.0
_bk_bo = 0.0
_bk_gi = 0.0
_bk_go = 0.0

VIEW_SOC, VIEW_GRID, VIEW_FLOW = 0, 1, 2
VIEW_BATT_DETAIL, VIEW_GRID_DETAIL, VIEW_STRINGS_DETAIL = 3, 4, 5
view_mode = VIEW_FLOW

BL_AUTO, BL_BRIGHT, BL_OFF = 0, 1, 2
bl_mode = BL_AUTO
last_input_ms = time.ticks_ms()
current_bl = -1.0

latest = {
    "soc_pct": None, "remaining_wh": None, "full_wh": float(config.DEFAULT_CAPACITY_WH),
    "batt_w": None, "solar_w": None, "load_w": None, "site_w": None,
    "grid_ok": None, "site_shutdown": None, "exp_lim_w": None, "time_str": "--:--",
}
comp_data = None
comp_last_ms = None
comp_poll_pending = False
cooldown_until_ms = None
status_msg = "BOOTING PW3..."
status_is_err = False
consec_errors = 0


def is_detail_view():
    return view_mode >= 3


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
    elif time.ticks_diff(now_ms, last_input_ms) > config.BACKLIGHT_DIM_AFTER_S * 1000:
        set_backlight(config.BACKLIGHT_IDLE)
    else:
        set_backlight(config.BACKLIGHT_ACTIVE)


def update_led():
    if status_is_err and consec_errors >= 3:
        led.set_rgb(40, 12, 0)
    elif config.LOW_SOC_ALERT_PCT > 0 and latest["soc_pct"] is not None and latest["soc_pct"] < config.LOW_SOC_ALERT_PCT:
        led.set_rgb(45, 0, 0)
    else:
        led.set_rgb(0, 0, 0)


def _w_to_u16(w):
    return int(max(0.0, min(655350.0, w)) / 10.0 + 0.5)


def push_history(soc_pct, batt_w, site_w, now_ms=None):
    global hist_count, hist_head, hist_bucket_ms, _bk_n, _bk_bi, _bk_bo, _bk_gi, _bk_go
    if now_ms is None:
        now_ms = time.ticks_ms()
    sv = int(max(0.0, min(100.0, soc_pct)) * 10.0 + 0.5)
    bw = batt_w or 0.0
    sw = site_w or 0.0
    bi = -bw if bw < 0.0 else 0.0
    bo = bw if bw > 0.0 else 0.0
    gi = sw if sw > 0.0 else 0.0
    go = -sw if sw < 0.0 else 0.0

    if hist_count == 0 or hist_bucket_ms is None or time.ticks_diff(now_ms, hist_bucket_ms) >= HIST_STEP_S * 1000:
        _bk_n = 1
        _bk_bi, _bk_bo, _bk_gi, _bk_go = bi, bo, gi, go
        idx = hist_head
        hist_head = (hist_head + 1) % CAP
        if hist_count < CAP:
            hist_count += 1
        hist_bucket_ms = now_ms
    else:
        _bk_n += 1
        _bk_bi += bi
        _bk_bo += bo
        _bk_gi += gi
        _bk_go += go
        idx = (hist_head - 1) % CAP

    hist_soc[idx] = sv
    hist_batt_in[idx] = _w_to_u16(_bk_bi / _bk_n)
    hist_batt_out[idx] = _w_to_u16(_bk_bo / _bk_n)
    hist_grid_in[idx] = _w_to_u16(_bk_gi / _bk_n)
    hist_grid_out[idx] = _w_to_u16(_bk_go / _bk_n)


def iter_history(arr):
    if hist_count < CAP:
        for i in range(hist_count):
            yield arr[i]
    else:
        for i in range(CAP):
            yield arr[(hist_head + i) % CAP]


def fmt_kw(w):
    return "--.- kW" if w is None else "{:+.2f} kW".format(w / 1000.0)


def _sev_color(s):
    return C_RED if s >= 2 else (C_AMBER if s == 1 else C_GREEN)


def _sev_hi(v, warn, crit):
    if v is None:
        return 0
    return 2 if v >= crit else (1 if v >= warn else 0)


def draw_text_right(text, right_x, y, scale=2):
    display.text(text, right_x - display.measure_text(text, scale), y, scale=scale)


def draw_header(title, big_str, big_col):
    display.set_pen(C_PANEL)
    display.rectangle(0, 0, WIDTH, 46)
    display.set_pen(C_TEXT)
    display.text("POWERWALL 3", 8, 6, scale=2)
    display.set_pen(C_MUTED)
    display.text(title, 8, 26, scale=2)
    display.set_pen(big_col)
    draw_text_right(big_str, WIDTH - 8, 9, scale=3)


def draw_footer():
    display.set_pen(C_PANEL)
    display.rectangle(0, HEIGHT - 20, WIDTH, 20)
    display.set_pen(C_RED if status_is_err else C_GREEN)
    display.circle(10, HEIGHT - 10, 4)
    display.set_pen(C_RED if status_is_err else C_MUTED)
    display.text(status_msg[:22], 20, HEIGHT - 15, scale=1)
    la = "A:BAT" if view_mode == VIEW_SOC else "A:SOC"
    lb = "B:AC" if view_mode == VIEW_GRID else "B:GRD"
    lx = "X:STR" if view_mode == VIEW_FLOW else "X:PWR"
    display.set_pen(C_MUTED)
    draw_text_right("{}  {} {} {}".format(latest["time_str"] or "--:--", la, lb, lx), WIDTH - 6, HEIGHT - 15, scale=1)


def _fmt_span(h):
    if h >= 1.0:
        return "-{}h".format(int(h + 0.5)) if abs(h - int(h + 0.5)) < 0.05 else "-{:.1f}h".format(h)
    return "-{}m".format(int(h * 60.0 + 0.5))


def _nice_kw_max(arr_a, arr_b, floor_kw=4.0):
    pk = 0
    for v in iter_history(arr_a):
        if v > pk:
            pk = v
    for v in iter_history(arr_b):
        if v > pk:
            pk = v
    pk_kw = pk / 100.0
    for step in (2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 25.0):
        if step >= floor_kw and step >= pk_kw * 1.05:
            return step
    return max(floor_kw, float(int(pk_kw) + 2))


def _draw_series(arr, x0, y1, pw, ph, sdiv, max_v, col, fill_col=None):
    step_den = CAP - 1 if CAP > 1 else 1
    offset = CAP - hist_count
    if fill_col is not None:
        px_p, py_p = None, None
        display.set_pen(fill_col)
        for idx, rv in enumerate(iter_history(arr)):
            r = max(0.0, min(1.0, (rv / sdiv) / max_v))
            px = x0 + ((offset + idx) * pw) // step_den
            py = y1 - int(r * ph)
            if px_p is not None and px > px_p:
                dx, dy = px - px_p, py - py_p
                for st in range(1, dx + 1):
                    fy = py_p + (dy * st) // dx
                    if fy < y1:
                        display.line(px_p + st, fy, px_p + st, y1 - 1)
            elif py < y1:
                display.line(px, py, px, y1 - 1)
            px_p, py_p = px, py
        return
    px_p, py_p = None, None
    display.set_pen(col)
    for idx, rv in enumerate(iter_history(arr)):
        r = max(0.0, min(1.0, (rv / sdiv) / max_v))
        px = x0 + ((offset + idx) * pw) // step_den
        py = y1 - int(r * ph)
        if px_p is not None:
            display.line(px_p, py_p, px, py, 2)
        else:
            display.circle(px, py, 2)
        px_p, py_p = px, py
    if px_p is not None:
        display.set_pen(col)
        display.circle(px_p, py_p, 2)


def _draw_chart_grid(x0, y0, x1, y1, left_max, left_col, right_max=None):
    pw, ph = x1 - x0, y1 - y0
    span_h = (CAP * HIST_STEP_S) / 3600.0
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = y1 - int(f * ph)
        display.set_pen(C_GRID)
        display.line(x0, gy, x1, gy)
        if f in (0.0, 0.5, 1.0):
            lv = left_max * f
            display.set_pen(left_col)
            draw_text_right("{:.0f}".format(lv) if lv == int(lv) else "{:.1f}".format(lv), x0 - 3, gy - 4, scale=1)
            if right_max is not None:
                rv = right_max * f
                rlbl = "{:.0f}k".format(rv) if (rv == int(rv)) else "{:.1f}k".format(rv)
                display.set_pen(C_MUTED)
                display.text(rlbl, x1 + 3, gy - 4, scale=1)
    for f in (0.25, 0.5, 0.75):
        gx = x0 + int(f * pw)
        display.set_pen(C_GRID)
        display.line(gx, y0, gx, y1)
        tl = _fmt_span(span_h * (1.0 - f))
        display.set_pen(C_MUTED)
        display.text(tl, gx - display.measure_text(tl, 1) // 2, y1 + 3, scale=1)
    display.set_pen(C_AXIS)
    display.line(x0, y0, x0, y1)
    display.line(x0, y1, x1, y1)
    if right_max is not None:
        display.line(x1, y0, x1, y1)
    display.set_pen(C_MUTED)
    display.text(_fmt_span(span_h), x0, y1 + 3, scale=1)
    draw_text_right("now", x1, y1 + 3, scale=1)


def _legend_item(x, y, col, label):
    display.set_pen(col)
    display.rectangle(x, y + 1, 6, 6)
    display.text(label, x + 9, y, scale=1)


def draw_chart(is_soc):
    if is_soc:
        v = latest["soc_pct"]
        col = C_RED if (v is not None and v < config.LOW_SOC_ALERT_PCT) else C_CYAN
        draw_header("SoC & Batt kW", "{:.1f}%".format(v) if v is not None else "--.-%", col)
        bw = latest["batt_w"] or 0.0
        bi_kw = (-bw / 1000.0) if bw < 0.0 else 0.0
        bo_kw = (bw / 1000.0) if bw > 0.0 else 0.0
        _legend_item(10, 49, col, "SOC%")
        _legend_item(74, 49, C_GREEN, "IN:{:.1f}kW".format(bi_kw))
        _legend_item(176, 49, C_AMBER, "OUT:{:.1f}kW".format(bo_kw))

        x0, y0, x1, y1 = 30, 61, 286, HEIGHT - 32
        pw, ph = x1 - x0, y1 - y0
        pmax = _nice_kw_max(hist_batt_in, hist_batt_out, 4.0)
        if hist_count > 0:
            _draw_series(hist_soc, x0, y1, pw, ph, 10.0, 100.0, col, fill_col=C_CYAN_DIM)
        _draw_chart_grid(x0, y0, x1, y1, 100.0, C_CYAN, right_max=pmax)
        if hist_count == 0:
            display.set_pen(C_MUTED)
            display.text("Waiting for PW3 data...", x0 + 36, y0 + ph // 2 - 8, scale=1)
            return
        _draw_series(hist_soc, x0, y1, pw, ph, 10.0, 100.0, col)
        _draw_series(hist_batt_in, x0, y1, pw, ph, 100.0, pmax, C_GREEN)
        _draw_series(hist_batt_out, x0, y1, pw, ph, 100.0, pmax, C_AMBER)
    else:
        sw = latest["site_w"]
        hcol = C_MUTED if sw is None else (C_GREEN if sw < -50 else (C_AMBER if sw > 50 else C_CYAN))
        big = "{:+.1f}kW".format(sw / 1000.0) if sw is not None else "--.-kW"
        draw_header("Grid In / Out", big, hcol)
        sval = sw or 0.0
        gi_kw = (sval / 1000.0) if sval > 0.0 else 0.0
        go_kw = (-sval / 1000.0) if sval < 0.0 else 0.0
        _legend_item(10, 49, C_AMBER, "IN:{:.2f}kW".format(gi_kw))
        _legend_item(128, 49, C_GREEN, "OUT:{:.2f}kW".format(go_kw))
        display.set_pen(C_MUTED)
        draw_text_right("kW", WIDTH - 10, 49, scale=1)

        x0, y0, x1, y1 = 34, 61, WIDTH - 10, HEIGHT - 32
        pw, ph = x1 - x0, y1 - y0
        pmax = _nice_kw_max(hist_grid_in, hist_grid_out, 2.0)
        _draw_chart_grid(x0, y0, x1, y1, pmax, C_MUTED)
        if hist_count == 0:
            display.set_pen(C_MUTED)
            display.text("Waiting for PW3 data...", x0 + 36, y0 + ph // 2 - 8, scale=1)
            return
        _draw_series(hist_grid_in, x0, y1, pw, ph, 100.0, pmax, C_AMBER)
        _draw_series(hist_grid_out, x0, y1, pw, ph, 100.0, pmax, C_GREEN)


def draw_power_flow():
    soc = latest["soc_pct"]
    scol = C_MUTED if soc is None else (C_GREEN if soc >= 20 else C_RED)
    draw_header("Live Power Flow", "{:.1f}%".format(soc) if soc is not None else "--.-%", scol)
    rem_k = (latest["remaining_wh"] / 1000.0) if latest["remaining_wh"] is not None else 0.0
    full_k = (latest["full_wh"] / 1000.0) if latest["full_wh"] else 13.5

    display.set_pen(C_PANEL)
    display.rectangle(10, 56, WIDTH - 20, 22)
    if soc is not None:
        fw = int((max(0.0, min(100.0, soc)) / 100.0) * (WIDTH - 24))
        display.set_pen(C_GREEN if soc >= 35 else (C_AMBER if soc >= 20 else C_RED))
        if fw > 0:
            display.rectangle(12, 58, fw, 18)
    display.set_pen(C_TEXT)
    display.text("{:.2f} / {:.2f} kWh".format(rem_k, full_k), 16, 63, scale=1)

    for name, watts, col, cx, cy in (
        ("SOLAR", latest["solar_w"], C_AMBER, 10, 88),
        ("HOME", latest["load_w"], C_BLUE, 165, 88),
        ("BATTERY", latest["batt_w"], C_CYAN, 10, 146),
        ("GRID", latest["site_w"], C_MAGENTA, 165, 146),
    ):
        display.set_pen(C_PANEL)
        display.rectangle(cx, cy, 145, 50)
        display.set_pen(col)
        display.rectangle(cx, cy, 4, 50)
        display.text(name, cx + 10, cy + 6, scale=1)
        display.set_pen(C_TEXT)
        display.text(fmt_kw(watts), cx + 10, cy + 22, scale=2)

    gok = latest["grid_ok"]
    gstr = "GRID: --" if gok is None else ("GRID: OK" if gok else "GRID: ISLAND")
    display.set_pen(C_MUTED)
    display.text("{}   RAM: {}KB free".format(gstr, gc.mem_free() // 1024), 10, 204, scale=1)


def _short_state(val, max_len=14):
    if not val:
        return "--"
    s = str(val).strip()
    if not s:
        return "--"
    p = s.lower().find("_state_")
    if p >= 0 and p + 7 < len(s):
        s = s[p + 7:]
    else:
        for pre in ("PCH_AcMode_", "AcMode_", "Pv_"):
            if s.startswith(pre):
                s = s[len(pre):]
                break
    while len(s) > max_len and "_" in s:
        tail = s.split("_", 1)[1]
        if not tail:
            break
        s = tail
    s = s.rstrip("_")
    return s[:max_len] if s else "--"


def _card(cx, cy, col, title, main_s, sub_s):
    display.set_pen(C_PANEL)
    display.rectangle(cx, cy, 145, 58)
    display.set_pen(col)
    display.rectangle(cx, cy, 4, 58)
    display.text(title, cx + 10, cy + 5, scale=1)
    display.set_pen(C_TEXT)
    display.text(main_s[:11], cx + 10, cy + 18, scale=2)
    display.set_pen(C_MUTED)
    display.text(sub_s[:21], cx + 10, cy + 42, scale=1)


def _warn_bar(sev, left_s, right_s=""):
    col = _sev_color(sev)
    display.set_pen(C_PANEL)
    display.rectangle(10, 184, WIDTH - 20, 28)
    display.set_pen(col)
    display.rectangle(10, 184, 4, 28)
    display.circle(23, 198, 4)
    display.text(left_s[:25], 32, 194, scale=1)
    if right_s:
        draw_text_right(right_s[:20], WIDTH - 18, 194, scale=1)


def draw_batt_detail():
    c = comp_data
    tmax = c["pack_temp_max"] if c else None
    p_sev = _sev_hi(tmax, 42.0, 50.0)
    draw_header("Battery & Thermal", "{:.1f}C".format(tmax) if tmax is not None else "--.-C", _sev_color(p_sev))
    if c is None:
        display.set_pen(C_MUTED)
        display.text("Polling PW3 vitals...", 92, 125, scale=1)
        return

    tmin, shunt = c["pack_temp_min"], c["shunt_temp"]
    dt = (tmax - tmin) if (tmax is not None and tmin is not None) else None
    if p_sev == 0 and dt is not None and dt >= 7.0:
        p_sev = 1
    _card(
        10, 54, _sev_color(p_sev), "PACK TEMP MAX",
        "{:.1f} C".format(tmax) if tmax is not None else "--.- C",
        "MIN {}C  SH {}C".format("{:.1f}".format(tmin) if tmin is not None else "--", "{:.1f}".format(shunt) if shunt is not None else "--"),
    )

    amb = c["inv_ambient_temp"]
    a_sev = _sev_hi(amb, 55.0, 65.0)
    _card(165, 54, _sev_color(a_sev), "INV AMBIENT", "{:.1f} C".format(amb) if amb is not None else "--.- C", "HVP: " + _short_state(c["hvp_state"], 15))

    fa, fb, da, db = c["fan_rpm_a"], c["fan_rpm_b"], c["fan_duty_a"], c["fan_duty_b"]
    fmain = "{} RPM".format(int((fa + fb) * 0.5 + 0.5)) if (fa is not None and fb is not None) else ("{} RPM".format(int(fa + 0.5)) if fa is not None else "-- RPM")
    fmax = max(da, db) if (da is not None and db is not None) else (da if da is not None else db)
    f_sev = _sev_hi(fmax, 75.0, 90.0)
    _card(
        10, 118, _sev_color(f_sev), "COOLING FANS", fmain,
        "A:{}({}) B:{}({})".format(
            int(fa + 0.5) if fa is not None else "--", "{}%".format(int(da + 0.5)) if da is not None else "--",
            int(fb + 0.5) if fb is not None else "--", "{}%".format(int(db + 0.5)) if db is not None else "--",
        ),
    )

    bdc = c["pch_batt_w"] if c["pch_batt_w"] is not None else latest["batt_w"]
    bcol, bdir = (C_MUTED, "--") if bdc is None else ((C_GREEN, "CHG") if bdc < -50 else ((C_CYAN, "DIS") if bdc > 50 else (C_MUTED, "IDLE")))
    bsub = "{}  PV:{:.2f}kW".format(bdir, c["slow_pv_sum_w"] / 1000.0) if c["slow_pv_sum_w"] is not None else "{}  ST:{}".format(bdir, _short_state(c["pch_state"], 11))
    _card(165, 118, bcol, "BATT DC PWR", fmt_kw(bdc), bsub)

    oa, ob = c["temp_oob"], c["temp_oob_chg"]
    if oa is not None or ob is not None:
        otot = int((oa or 0) + (ob or 0))
        os_, o_sev = str(otot), (2 if otot > 0 else 0)
    else:
        os_, o_sev = "--", 0
    tsev = max(p_sev, a_sev, f_sev, o_sev)
    lbl = "OOB TEMP WARN!" if o_sev == 2 else ("OVER-TEMP WARN!" if tsev == 2 else ("THERMAL: WARM" if tsev == 1 else "THERMAL: OK"))
    _warn_bar(tsev, lbl, "dT:{}  OOB:{}".format("{:.1f}C".format(dt) if dt is not None else "--", os_))


def draw_grid_detail():
    c = comp_data
    vab = c["ac_v_ab"] if c else None
    draw_header("Grid & AC Vitals", "{:.1f}V".format(vab) if vab is not None else "--.-V", C_GREEN if latest["grid_ok"] is not False else C_RED)
    if c is None:
        display.set_pen(C_MUTED)
        display.text("Polling PW3 vitals...", 92, 125, scale=1)
        return

    van, vbn = c["ac_v_an"], c["ac_v_bn"]
    dv = abs(van - vbn) if (van is not None and vbn is not None) else None
    v_sev = 2 if (vab is not None and (vab >= 254 or vab <= 222)) else (1 if ((vab is not None and (vab >= 250 or vab <= 228)) or (dv is not None and dv >= 4)) else 0)
    _card(
        10, 54, _sev_color(v_sev), "AC SPLIT-PHASE",
        "{:.1f} V".format(vab) if vab is not None else "--.- V",
        "L1:{}V  L2:{}V".format("{:.1f}".format(van) if van is not None else "--", "{:.1f}".format(vbn) if vbn is not None else "--"),
    )

    fq = c["ac_freq"]
    f_sev = 2 if (fq is not None and (fq >= 60.5 or fq <= 59.3)) else (1 if (fq is not None and (fq >= 60.2 or fq <= 59.8)) else 0)
    _card(165, 54, _sev_color(f_sev), "FREQUENCY", "{:.2f}Hz".format(fq) if fq is not None else "--.--Hz", "GRID: OK" if latest["grid_ok"] is not False else "GRID: ISLAND")

    acw = c["ac_power_w"] if c["ac_power_w"] is not None else c["pch_batt_w"]
    _card(10, 118, C_MAGENTA, "INVERTER AC", fmt_kw(acw), "MODE: " + _short_state(c["ac_mode"], 14))

    sw = latest["site_w"]
    gcol, gdir = (C_MUTED, "--") if sw is None else ((C_GREEN, "EXP") if sw < -50 else ((C_AMBER, "IMP") if sw > 50 else (C_CYAN, "IDLE")))
    lw = latest["load_w"]
    _card(165, 118, gcol, "GRID FLOW", fmt_kw(sw), "{} HOME:{}".format(gdir, "{:.2f}kW".format(lw / 1000.0) if lw is not None else "--.-kW"))

    elim = latest["exp_lim_w"]
    if elim is None:
        cfg_l = getattr(config, "EXPORT_LIMIT_W", 0)
        if cfg_l and cfg_l > 0:
            elim = float(cfg_l)
    exp_w = -sw if (sw is not None and sw < 0.0) else 0.0
    if elim is not None and elim > 0.0:
        epct = int((exp_w / elim) * 100.0 + 0.5)
        llbl = "EXP:{:.1f}/{:.1f}kW ({}%)".format(exp_w / 1000.0, elim / 1000.0, epct)
        e_sev = _sev_hi(epct, 80, 95)
    elif elim == 0:
        llbl = "EXP LIM: 0kW"
        e_sev = _sev_hi(exp_w, 50.0, 200.0)
    else:
        llbl, e_sev = "EXP LIM: --", 0

    s_sev = 2 if latest["site_shutdown"] is True else 0
    if s_sev == 2:
        rlbl = "SHUTDOWN!"
    elif v_sev == 2:
        rlbl = "V OUT!"
    elif f_sev == 2:
        rlbl = "CURTAIL!" if (fq is not None and fq >= 60.5) else "FREQ!"
    elif v_sev == 1:
        rlbl = "V HIGH" if (vab is not None and vab >= 250) else ("V LOW" if (vab is not None and vab <= 228) else "dV:{:.1f}V".format(dv))
    elif f_sev == 1:
        rlbl = "FREQ WARN"
    else:
        rlbl = "OK dV:{:.1f}V".format(dv) if dv is not None else "GRID OK"
    _warn_bar(max(e_sev, v_sev, f_sev, s_sev), llbl, rlbl)


def draw_strings_detail():
    c = comp_data
    dc = c["strings_dc_w"] if c else None
    acs = latest["solar_w"]
    hw = dc if (dc is not None and dc > 0.0) else acs
    draw_header("Solar Strings", "{:.2f}kW".format(hw / 1000.0) if hw is not None else "--.-kW", C_AMBER)
    if c is None:
        display.set_pen(C_MUTED)
        display.text("Polling PW3 strings...", 88, 125, scale=1)
        return

    dck = dc / 1000.0
    if acs is not None and dc >= 100.0 and acs >= 0.0:
        sline = "DC:{:.2f}kW  AC:{:.2f}kW  EFF:{}%".format(dck, acs / 1000.0, int(min(100.0, (acs / dc) * 100.0) + 0.5))
    elif acs is not None:
        sline = "DC SUM:{:.2f}kW   AC SOLAR:{:.2f}kW".format(dck, acs / 1000.0)
    else:
        sline = "DC SUM:{:.2f}kW".format(dck)
    display.set_pen(C_MUTED)
    display.text(sline, 10, 50, scale=1)

    for idx, sr in enumerate(c["strings"]):
        ry = 64 + idx * 25
        sw, sv, si, st = sr["w"] or 0.0, sr["v"], sr["i"], sr["state"] or ""
        act = ("Active" in st) or sw >= 5.0
        display.set_pen(C_PANEL)
        display.rectangle(8, ry, WIDTH - 16, 22)
        display.set_pen(C_AMBER if sw >= 10.0 else (C_CYAN if act else C_MUTED))
        display.text(sr["id"], 13, ry + 4, scale=2)
        display.set_pen(C_GRID)
        display.rectangle(32, ry + 3, 132, 8)
        pct = 0
        if dc > 1.0 and sw > 0.0:
            fr = min(1.0, sw / dc)
            fw = int(fr * 132)
            if fw > 0:
                display.set_pen(C_AMBER)
                display.rectangle(32, ry + 3, fw, 8)
            pct = int(fr * 100.0 + 0.5)
        display.set_pen(C_MUTED)
        va = "{:.0f}V  {:.1f}A".format(sv or 0.0, si or 0.0) if (act and (sv is not None or si is not None)) else (_short_state(st, 16) if st else "--")
        display.text(va, 32, ry + 13, scale=1)
        display.set_pen(C_TEXT if sw >= 10.0 else C_MUTED)
        draw_text_right("{:.2f}kW".format(sw / 1000.0), 274, ry + 4, scale=2)
        display.set_pen(C_AMBER if pct > 0 else C_MUTED)
        draw_text_right("{:2d}%".format(pct), WIDTH - 12, ry + 8, scale=1)


def draw_ui():
    display.set_pen(C_BG)
    display.clear()
    if view_mode == VIEW_SOC:
        draw_chart(True)
    elif view_mode == VIEW_GRID:
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


_WIFI_ERRS = {-1: "WIFI FAIL (-1)", -2: "NO AP (-2)", -3: "BAD PWD (-3)", 0: "WIFI IDLE (0)", 1: "JOINING (1)", 2: "NO DHCP IP (2)"}
_wifi_tried = False


def init_wifi(wdt_feed=None):
    try:
        network.country(config.WIFI_COUNTRY)
    except Exception:
        pass
    w = network.WLAN(network.STA_IF)
    if not w.active():
        w.active(True)
        time.sleep_ms(300)
        if wdt_feed:
            wdt_feed()
    try:
        w.config(pm=getattr(network.WLAN, "PM_NONE", 0xA11140))
    except Exception:
        pass
    return w


def ensure_wifi(wlan, wdt_feed, btn_cb=None):
    global status_msg, status_is_err, _wifi_tried
    if wlan.isconnected():
        return True
    status_msg, status_is_err = "CONNECTING WIFI...", True
    draw_ui()
    if _wifi_tried:
        try:
            wlan.disconnect()
        except Exception:
            pass
        time.sleep_ms(300)
    _wifi_tried = True
    try:
        wlan.config(pm=getattr(network.WLAN, "PM_NONE", 0xA11140))
    except Exception:
        pass
    print("WiFi: connecting to '{}'...".format(config.WIFI_SSID))
    wlan.connect(config.WIFI_SSID, config.WIFI_PASS)
    dl = time.ticks_add(time.ticks_ms(), 20000)
    while True:
        if wdt_feed:
            wdt_feed()
        if btn_cb:
            btn_cb(time.ticks_ms())
        st = wlan.status()
        if st == 3 or wlan.isconnected():
            break
        if st < 0:
            status_msg = _WIFI_ERRS.get(st, "WIFI ERR ({})".format(st))
            print("WiFi failed:", status_msg)
            return False
        if time.ticks_diff(dl, time.ticks_ms()) <= 0:
            status_msg = "TIMEOUT " + _WIFI_ERRS.get(st, "({})".format(st))
            print("WiFi timed out:", status_msg)
            return False
        time.sleep_ms(100)
    if config.WIFI_POWERSAVE:
        try:
            wlan.config(pm=getattr(network.WLAN, "PM_POWERSAVE", 0x000011))
        except Exception:
            pass
    ip = wlan.ifconfig()[0]
    status_msg, status_is_err = "WIFI " + ip, False
    print("WiFi connected! IP:", ip)
    return True


def main():
    global view_mode, bl_mode, last_input_ms, status_msg, status_is_err, consec_errors
    global comp_data, comp_last_ms, comp_poll_pending, cooldown_until_ms

    print("Booting PW3 dashboard...")
    set_backlight(config.BACKLIGHT_ACTIVE)
    draw_ui()
    if config.ENABLE_WDT:
        time.sleep_ms(2500)
        wdt_feed = WDT(timeout=8000).feed
    else:
        wdt_feed = None

    try:
        print("Init WiFi...")
        wlan = init_wifi(wdt_feed)
        sock_tmo = min(config.SOCKET_TIMEOUT_S, 7) if config.ENABLE_WDT else config.SOCKET_TIMEOUT_S
        client = Powerwall3Client(
            host=config.PW_HOST, port=config.PW_PORT, gw_password=config.GW_PASSWORD,
            timeout_s=sock_tmo, soc_scale_to_app=config.SOC_SCALE_TO_APP,
            utc_offset_hours=config.UTC_OFFSET_HOURS,
            payload_file=getattr(tedapi_query, "PAYLOAD_FILE", ""),
            graphql_payload=getattr(tedapi_query, "GRAPHQL_PAYLOAD", b""),
            query_text=tedapi_query.QUERY_TEXT, query_sig=tedapi_query.QUERY_SIG,
            query_vars=tedapi_query.QUERY_VARS, is_real_signed_query=tedapi_query.IS_REAL_SIGNED_QUERY,
            components_payload=getattr(tedapi_query, "COMPONENTS_PAYLOAD", b""),
            components_query_text=getattr(tedapi_query, "COMPONENTS_QUERY_TEXT", b""),
            components_query_sig=getattr(tedapi_query, "COMPONENTS_QUERY_SIG", b""),
            components_query_vars=getattr(tedapi_query, "COMPONENTS_QUERY_VARS", b"{}"),
        )
    except Exception as e:
        status_msg, status_is_err = "BOOT: " + str(e)[:16], True
        print("Boot error:", e)
        draw_ui()
        raise
    gc.collect()
    print("Boot complete. Free RAM:", gc.mem_free(), "bytes")

    prev_btn = [1, 1, 1, 1]
    next_poll_ms = time.ticks_ms()
    comp_fresh_ms = min(15, config.POLL_INTERVAL_S) * 1000

    def _poll_buttons(now):
        global view_mode, bl_mode, last_input_ms, comp_poll_pending
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
                    view_mode = VIEW_GRID_DETAIL if view_mode == VIEW_GRID else VIEW_GRID
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
        for i in range(4):
            prev_btn[i] = cur_btn[i]
        update_backlight(now)
        if dirty:
            draw_ui()

    def _run_vitals():
        global comp_data, comp_last_ms, comp_poll_pending, cooldown_until_ms
        global status_msg, status_is_err, consec_errors
        gc.collect()
        try:
            c = client.poll_components(wdt_feed=wdt_feed)
            comp_data = c
            c = None
            gc.collect()
            comp_last_ms = time.ticks_ms()
            comp_poll_pending = False
            if comp_data.get("time_str") and latest["time_str"] == "--:--":
                latest["time_str"] = comp_data["time_str"]
            status_msg, status_is_err = "OK (VITALS)", False
            print("VITALS: DC={:.0f}W VAB={}V FREQ={}Hz TMAX={}C OOB={}/{} FREE={}B".format(
                comp_data["strings_dc_w"], comp_data["ac_v_ab"], comp_data["ac_freq"],
                comp_data["pack_temp_max"], comp_data["temp_oob"], comp_data["temp_oob_chg"], gc.mem_free()
            ))
            return True
        except RateLimitError as e:
            consec_errors += 1
            comp_poll_pending = False
            cooldown_until_ms = time.ticks_add(time.ticks_ms(), config.RATE_LIMIT_COOLDOWN_S * 1000)
            status_msg, status_is_err = "429 COOLDOWN 5m", True
            print("Vitals rate limited:", e)
        except AuthError as e:
            consec_errors += 1
            comp_poll_pending = False
            status_msg, status_is_err = "AUTH ERR: " + str(e), True
            print("Vitals auth error:", e)
        except Exception as e:
            comp_poll_pending = False
            gc.collect()
            status_msg, status_is_err = "VITALS: " + str(e)[:14], True
            print("Vitals poll error:", e, "FREE={}B".format(gc.mem_free()))
        return False

    while True:
        if wdt_feed:
            wdt_feed()
        now = time.ticks_ms()
        _poll_buttons(now)

        in_cd = cooldown_until_ms is not None and time.ticks_diff(cooldown_until_ms, now) > 0
        if comp_poll_pending and is_detail_view() and not in_cd:
            if ensure_wifi(wlan, wdt_feed, _poll_buttons):
                status_msg, status_is_err = "POLLING VITALS...", False
                draw_ui()
                _run_vitals()
                if cooldown_until_ms is not None and time.ticks_diff(cooldown_until_ms, time.ticks_ms()) > 0:
                    next_poll_ms = cooldown_until_ms
            else:
                comp_poll_pending = False
            update_led()
            draw_ui()

        if time.ticks_diff(now, next_poll_ms) >= 0:
            if ensure_wifi(wlan, wdt_feed, _poll_buttons):
                if status_msg == "BOOTING PW3..." or status_msg.startswith("WIFI "):
                    status_msg, status_is_err = "POLLING PW3...", False
                    draw_ui()
                try:
                    gc.collect()
                    d = client.poll(wdt_feed=wdt_feed)
                    for k in ("soc_pct", "remaining_wh", "full_wh", "batt_w", "solar_w", "load_w", "site_w", "grid_ok", "site_shutdown", "exp_lim_w"):
                        latest[k] = d[k]
                    if d["time_str"]:
                        latest["time_str"] = d["time_str"]
                    d = None
                    gc.collect()
                    push_history(latest["soc_pct"], latest["batt_w"], latest["site_w"])
                    consec_errors = 0
                    cooldown_until_ms = None
                    status_msg, status_is_err = "OK (TEDAPI)", False
                    delay_ms = config.POLL_INTERVAL_S * 1000
                    print("SOC={:.1f}% REM={:.2f}kWh BATT={}W SOLAR={}W EXPLIM={}W FREE={}B".format(
                        latest["soc_pct"], latest["remaining_wh"] / 1000.0,
                        latest["batt_w"], latest["solar_w"], latest["exp_lim_w"], gc.mem_free()
                    ))
                    if is_detail_view():
                        _run_vitals()
                        if cooldown_until_ms is not None and time.ticks_diff(cooldown_until_ms, time.ticks_ms()) > 0:
                            delay_ms = config.RATE_LIMIT_COOLDOWN_S * 1000
                except RateLimitError as e:
                    consec_errors += 1
                    status_msg, status_is_err = "429 COOLDOWN 5m", True
                    delay_ms = config.RATE_LIMIT_COOLDOWN_S * 1000
                    cooldown_until_ms = time.ticks_add(time.ticks_ms(), delay_ms)
                    print("Rate limited:", e)
                except AuthError as e:
                    consec_errors += 1
                    status_msg, status_is_err = "AUTH ERR: " + str(e), True
                    delay_ms = 60000
                    print("Auth error:", e)
                except Exception as e:
                    consec_errors += 1
                    gc.collect()
                    status_msg, status_is_err = "ERR: " + str(e)[:18], True
                    delay_ms = min(120, 10 * (1 << min(consec_errors, 4))) * 1000
                    print("Poll error:", e, "FREE={}B".format(gc.mem_free()))
            else:
                consec_errors += 1
                delay_ms = 15000
            next_poll_ms = time.ticks_add(time.ticks_ms(), delay_ms)
            update_led()
            draw_ui()

        time.sleep_ms(50)


if __name__ == "__main__":
    main()
