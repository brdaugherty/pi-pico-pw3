# pico/main.py - Tesla Powerwall 3 dashboard (Pico W RP2040 v1.25.0 + Display Pack 2.8")
# Buttons (press A/B/X again to toggle Primary <-> Detail):
#   A (GP12): SoC + Batt In/Out 24h <-> Battery & Thermal (OOB warning light)
#   B (GP13): Grid In/Out 24h       <-> Grid & AC Vitals (ieee20305 export limit)
#   X (GP14): Live Power Flow       <-> Solar Strings A-F  [default]
#   Y (GP15): Backlight cycle (Auto-Dim -> Bright -> Off)

print("PW3: start")
import gc
import config as cfg
from picographics import PicoGraphics, DISPLAY_PICO_DISPLAY_2, PEN_P4

# Allocate 38.4 KB framebuffer FIRST (before imports) to prevent RP2040 heap fragmentation
display = PicoGraphics(display=DISPLAY_PICO_DISPLAY_2, pen_type=PEN_P4, rotate=cfg.DISPLAY_ROTATE)
D = display
D.set_font("bitmap8")
W, H = D.get_bounds()
gc.collect()
print("PW3: display ok")

import array
import time
import network
from machine import Pin, WDT
from pimoroni import RGBLED

import tedapi_query as tq
from powerwall_api import Powerwall3Client, RateLimitError, AuthError

t_ms, t_add, t_diff = time.ticks_ms, time.ticks_add, time.ticks_diff
C_BG = D.create_pen(10, 14, 22)
C_PANEL = D.create_pen(20, 28, 42)
C_GRID = D.create_pen(42, 54, 74)
C_TEXT = D.create_pen(240, 244, 250)
C_MUTED = D.create_pen(140, 155, 175)
C_CYAN = D.create_pen(0, 220, 255)
C_MAGENTA = D.create_pen(235, 90, 235)
C_GREEN = D.create_pen(45, 220, 120)
C_AMBER = D.create_pen(255, 190, 40)
C_RED = D.create_pen(255, 75, 75)
C_BLUE = D.create_pen(70, 150, 255)
C_AXIS = D.create_pen(110, 125, 148)
C_CYAN_DIM = D.create_pen(0, 58, 74)

led = RGBLED(26, 27, 28)
led.set_rgb(0, 0, 0)
btns = tuple(Pin(p, Pin.IN, Pin.PULL_UP) for p in (12, 13, 14, 15))

# 24h in-RAM ring buffers (288 x 5-min buckets, 5 series = 2.8 KB, zero flash wear)
HIST_HOURS = getattr(cfg, "HISTORY_HOURS", 24)
HIST_STEP_S = getattr(cfg, "HISTORY_STEP_S", 300)
CAP = max(2, int((HIST_HOURS * 3600) // HIST_STEP_S))
hist_soc = array.array("H", [0] * CAP)
hist_batt_in = array.array("H", [0] * CAP)
hist_batt_out = array.array("H", [0] * CAP)
hist_grid_in = array.array("H", [0] * CAP)
hist_grid_out = array.array("H", [0] * CAP)
hist_count = hist_head = _bk_n = 0
hist_bucket_ms = None
_bk_bi = _bk_bo = _bk_gi = _bk_go = 0.0

VIEW_SOC, VIEW_GRID, VIEW_FLOW = 0, 1, 2
VIEW_BATT_DETAIL, VIEW_GRID_DETAIL, VIEW_STRINGS_DETAIL = 3, 4, 5
view_mode = VIEW_FLOW

BL_AUTO, BL_BRIGHT, BL_OFF = 0, 1, 2
bl_mode = BL_AUTO
last_input_ms = t_ms()
current_bl = -1.0

latest = {
    "soc_pct": None, "remaining_wh": None, "full_wh": float(cfg.DEFAULT_CAPACITY_WH),
    "batt_w": None, "solar_w": None, "load_w": None, "site_w": None,
    "grid_ok": None, "site_shutdown": None, "exp_lim_w": None, "time_str": "--:--",
}
comp_data = comp_last_ms = cd_until = None
comp_pending = status_is_err = False
status_msg = "BOOTING PW3..."
consec_err = 0


def is_detail_view():
    return view_mode >= 3


def set_backlight(lv):
    global current_bl
    if abs(lv - current_bl) > 0.01:
        D.set_backlight(lv)
        current_bl = lv


def update_backlight(now):
    if bl_mode == BL_OFF:
        set_backlight(0.0)
    elif bl_mode == BL_BRIGHT or t_diff(now, last_input_ms) <= cfg.BACKLIGHT_DIM_AFTER_S * 1000:
        set_backlight(cfg.BACKLIGHT_ACTIVE)
    else:
        set_backlight(cfg.BACKLIGHT_IDLE)


def update_led():
    if status_is_err and consec_err >= 3:
        led.set_rgb(40, 12, 0)
    elif cfg.LOW_SOC_ALERT_PCT > 0 and latest["soc_pct"] is not None and latest["soc_pct"] < cfg.LOW_SOC_ALERT_PCT:
        led.set_rgb(45, 0, 0)
    else:
        led.set_rgb(0, 0, 0)


def _w10(w):
    return int(max(0.0, min(655350.0, w)) / 10.0 + 0.5)


def push_history(soc_pct, batt_w, site_w, now_ms=None):
    global hist_count, hist_head, hist_bucket_ms, _bk_n, _bk_bi, _bk_bo, _bk_gi, _bk_go
    if now_ms is None:
        now_ms = t_ms()
    sv = int(max(0.0, min(100.0, soc_pct)) * 10.0 + 0.5)
    bw, sw = batt_w or 0.0, site_w or 0.0
    bi, bo = (-bw if bw < 0.0 else 0.0), (bw if bw > 0.0 else 0.0)
    gi, go = (sw if sw > 0.0 else 0.0), (-sw if sw < 0.0 else 0.0)
    if hist_count == 0 or hist_bucket_ms is None or t_diff(now_ms, hist_bucket_ms) >= HIST_STEP_S * 1000:
        _bk_n, _bk_bi, _bk_bo, _bk_gi, _bk_go = 1, bi, bo, gi, go
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
    hist_batt_in[idx] = _w10(_bk_bi / _bk_n)
    hist_batt_out[idx] = _w10(_bk_bo / _bk_n)
    hist_grid_in[idx] = _w10(_bk_gi / _bk_n)
    hist_grid_out[idx] = _w10(_bk_go / _bk_n)


def iter_history(arr):
    if hist_count < CAP:
        for i in range(hist_count):
            yield arr[i]
    else:
        for i in range(CAP):
            yield arr[(hist_head + i) % CAP]


def fmt_kw(w):
    return "--.- kW" if w is None else "{:+.2f} kW".format(w / 1000.0)


def _f1(v, d="--"):
    return "{:.1f}".format(v) if v is not None else d


def _scol(s):
    return C_RED if s >= 2 else (C_AMBER if s == 1 else C_GREEN)


def _shi(v, w, c):
    return 0 if v is None else (2 if v >= c else (1 if v >= w else 0))


def _rect(c, x, y, w, h):
    D.set_pen(c)
    D.rectangle(x, y, w, h)


def _txt(c, s, x, y, sc=1):
    D.set_pen(c)
    D.text(s, x, y, scale=sc)


def _txtr(c, s, rx, y, sc=1):
    D.set_pen(c)
    D.text(s, rx - D.measure_text(s, sc), y, scale=sc)


def draw_header(title, big_s, col):
    _rect(C_PANEL, 0, 0, W, 46)
    _txt(C_TEXT, "POWERWALL 3", 8, 6, 2)
    _txt(C_MUTED, title, 8, 26, 2)
    _txtr(col, big_s, W - 8, 9, 3)


def draw_footer():
    _rect(C_PANEL, 0, H - 20, W, 20)
    D.set_pen(C_RED if status_is_err else C_GREEN)
    D.circle(10, H - 10, 4)
    _txt(C_RED if status_is_err else C_MUTED, status_msg[:22], 20, H - 15)
    la = "A:BAT" if view_mode == VIEW_SOC else "A:SOC"
    lb = "B:AC" if view_mode == VIEW_GRID else "B:GRD"
    lx = "X:STR" if view_mode == VIEW_FLOW else "X:PWR"
    _txtr(C_MUTED, "{}  {} {} {}".format(latest["time_str"] or "--:--", la, lb, lx), W - 6, H - 15)


def _fmt_span(h):
    if h >= 1.0:
        r = int(h + 0.5)
        return "-{}h".format(r) if abs(h - r) < 0.05 else "-{:.1f}h".format(h)
    return "-{}m".format(int(h * 60.0 + 0.5))


def _nice_kw_max(a, b, fl=4.0):
    pk = 0
    for arr in (a, b):
        for v in iter_history(arr):
            if v > pk:
                pk = v
    pk /= 100.0
    for s in (2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 25.0):
        if s >= fl and s >= pk * 1.05:
            return s
    return max(fl, float(int(pk) + 2))


def _draw_series(arr, x0, y1, pw, ph, sdiv, mv, col, fill=None):
    den = CAP - 1 if CAP > 1 else 1
    off = CAP - hist_count
    xp = yp = None
    D.set_pen(fill if fill is not None else col)
    for i, rv in enumerate(iter_history(arr)):
        r = max(0.0, min(1.0, (rv / sdiv) / mv))
        px = x0 + ((off + i) * pw) // den
        py = y1 - int(r * ph)
        if fill is not None:
            if xp is not None and px > xp:
                dx, dy = px - xp, py - yp
                for st in range(1, dx + 1):
                    fy = yp + (dy * st) // dx
                    if fy < y1:
                        D.line(xp + st, fy, xp + st, y1 - 1)
            elif py < y1:
                D.line(px, py, px, y1 - 1)
        elif xp is not None:
            D.line(xp, yp, px, py, 2)
        else:
            D.circle(px, py, 2)
        xp, yp = px, py
    if fill is None and xp is not None:
        D.circle(xp, yp, 2)


def _draw_grid(x0, y0, x1, y1, lmax, lcol, rmax=None):
    pw, ph = x1 - x0, y1 - y0
    sh = (CAP * HIST_STEP_S) / 3600.0
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = y1 - int(f * ph)
        D.set_pen(C_GRID)
        D.line(x0, gy, x1, gy)
        if f in (0.0, 0.5, 1.0):
            lv = lmax * f
            _txtr(lcol, "{:.0f}".format(lv) if lv == int(lv) else "{:.1f}".format(lv), x0 - 3, gy - 4)
            if rmax is not None:
                rv = rmax * f
                _txt(C_MUTED, "{:.0f}k".format(rv) if rv == int(rv) else "{:.1f}k".format(rv), x1 + 3, gy - 4)
    for f in (0.25, 0.5, 0.75):
        gx = x0 + int(f * pw)
        D.set_pen(C_GRID)
        D.line(gx, y0, gx, y1)
        tl = _fmt_span(sh * (1.0 - f))
        _txt(C_MUTED, tl, gx - D.measure_text(tl, 1) // 2, y1 + 3)
    D.set_pen(C_AXIS)
    D.line(x0, y0, x0, y1)
    D.line(x0, y1, x1, y1)
    if rmax is not None:
        D.line(x1, y0, x1, y1)
    _txt(C_MUTED, _fmt_span(sh), x0, y1 + 3)
    _txtr(C_MUTED, "now", x1, y1 + 3)


def _leg(x, col, lbl):
    _rect(col, x, 50, 6, 6)
    D.text(lbl, x + 9, 49, scale=1)


def draw_chart(is_soc):
    if is_soc:
        v = latest["soc_pct"]
        col = C_RED if (v is not None and v < cfg.LOW_SOC_ALERT_PCT) else C_CYAN
        draw_header("SoC & Batt kW", _f1(v, "--.-") + "%", col)
        bw = latest["batt_w"] or 0.0
        _leg(10, col, "SOC%")
        _leg(74, C_GREEN, "IN:{:.1f}kW".format(-bw / 1000.0 if bw < 0.0 else 0.0))
        _leg(176, C_AMBER, "OUT:{:.1f}kW".format(bw / 1000.0 if bw > 0.0 else 0.0))
        x0, y0, x1, y1 = 30, 61, 286, H - 32
        pw, ph = x1 - x0, y1 - y0
        pm = _nice_kw_max(hist_batt_in, hist_batt_out, 4.0)
        if hist_count > 0:
            _draw_series(hist_soc, x0, y1, pw, ph, 10.0, 100.0, col, C_CYAN_DIM)
        _draw_grid(x0, y0, x1, y1, 100.0, C_CYAN, pm)
        if hist_count == 0:
            _txt(C_MUTED, "Waiting for PW3 data...", x0 + 36, y0 + ph // 2 - 8)
            return
        _draw_series(hist_soc, x0, y1, pw, ph, 10.0, 100.0, col)
        _draw_series(hist_batt_in, x0, y1, pw, ph, 100.0, pm, C_GREEN)
        _draw_series(hist_batt_out, x0, y1, pw, ph, 100.0, pm, C_AMBER)
    else:
        sw = latest["site_w"]
        hc = C_MUTED if sw is None else (C_GREEN if sw < -50 else (C_AMBER if sw > 50 else C_CYAN))
        draw_header("Grid In / Out", "{:+.1f}kW".format(sw / 1000.0) if sw is not None else "--.-kW", hc)
        sv = sw or 0.0
        _leg(10, C_AMBER, "IN:{:.2f}kW".format(sv / 1000.0 if sv > 0.0 else 0.0))
        _leg(128, C_GREEN, "OUT:{:.2f}kW".format(-sv / 1000.0 if sv < 0.0 else 0.0))
        _txtr(C_MUTED, "kW", W - 10, 49)
        x0, y0, x1, y1 = 34, 61, W - 10, H - 32
        pw, ph = x1 - x0, y1 - y0
        pm = _nice_kw_max(hist_grid_in, hist_grid_out, 2.0)
        _draw_grid(x0, y0, x1, y1, pm, C_MUTED)
        if hist_count == 0:
            _txt(C_MUTED, "Waiting for PW3 data...", x0 + 36, y0 + ph // 2 - 8)
            return
        _draw_series(hist_grid_in, x0, y1, pw, ph, 100.0, pm, C_AMBER)
        _draw_series(hist_grid_out, x0, y1, pw, ph, 100.0, pm, C_GREEN)


def draw_power_flow():
    soc = latest["soc_pct"]
    draw_header("Live Power Flow", _f1(soc, "--.-") + "%", C_MUTED if soc is None else (C_GREEN if soc >= 20 else C_RED))
    rk = (latest["remaining_wh"] / 1000.0) if latest["remaining_wh"] is not None else 0.0
    fk = (latest["full_wh"] / 1000.0) if latest["full_wh"] else 13.5
    _rect(C_PANEL, 10, 56, W - 20, 22)
    if soc is not None:
        fw = int((max(0.0, min(100.0, soc)) / 100.0) * (W - 24))
        if fw > 0:
            _rect(C_GREEN if soc >= 35 else (C_AMBER if soc >= 20 else C_RED), 12, 58, fw, 18)
    _txt(C_TEXT, "{:.2f} / {:.2f} kWh".format(rk, fk), 16, 63)
    for nm, wt, col, cx, cy in (
        ("SOLAR", latest["solar_w"], C_AMBER, 10, 88), ("HOME", latest["load_w"], C_BLUE, 165, 88),
        ("BATTERY", latest["batt_w"], C_CYAN, 10, 146), ("GRID", latest["site_w"], C_MAGENTA, 165, 146),
    ):
        _rect(C_PANEL, cx, cy, 145, 50)
        _rect(col, cx, cy, 4, 50)
        D.text(nm, cx + 10, cy + 6, scale=1)
        _txt(C_TEXT, fmt_kw(wt), cx + 10, cy + 22, 2)
    gok = latest["grid_ok"]
    gs = "GRID: --" if gok is None else ("GRID: OK" if gok else "GRID: ISLAND")
    _txt(C_MUTED, "{}   RAM: {}KB free".format(gs, gc.mem_free() // 1024), 10, 204)


def _short(val, mx=14):
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
    while len(s) > mx and "_" in s:
        t = s.split("_", 1)[1]
        if not t:
            break
        s = t
    s = s.rstrip("_")
    return s[:mx] if s else "--"


def _card(cx, cy, col, ttl, ms, ss):
    _rect(C_PANEL, cx, cy, 145, 58)
    _rect(col, cx, cy, 4, 58)
    D.text(ttl, cx + 10, cy + 5, scale=1)
    _txt(C_TEXT, ms[:11], cx + 10, cy + 18, 2)
    _txt(C_MUTED, ss[:21], cx + 10, cy + 42)


def _wbar(sev, ls, rs=""):
    col = _scol(sev)
    _rect(C_PANEL, 10, 184, W - 20, 28)
    _rect(col, 10, 184, 4, 28)
    D.circle(23, 198, 4)
    D.text(ls[:25], 32, 194, scale=1)
    if rs:
        _txtr(col, rs[:20], W - 18, 194)


def draw_batt_detail():
    c = comp_data
    tm = c["pack_temp_max"] if c else None
    ps = _shi(tm, 42.0, 50.0)
    draw_header("Battery & Thermal", _f1(tm, "--.-") + "C", _scol(ps))
    if c is None:
        _txt(C_MUTED, "Polling PW3 vitals...", 92, 125)
        return
    tn, sh = c["pack_temp_min"], c["shunt_temp"]
    dt = (tm - tn) if (tm is not None and tn is not None) else None
    if ps == 0 and dt is not None and dt >= 7.0:
        ps = 1
    _card(10, 54, _scol(ps), "PACK TEMP MAX", _f1(tm, "--.-") + " C", "MIN {}C  SH {}C".format(_f1(tn), _f1(sh)))
    amb = c["inv_ambient_temp"]
    a_s = _shi(amb, 55.0, 65.0)
    _card(165, 54, _scol(a_s), "INV AMBIENT", _f1(amb, "--.-") + " C", "HVP: " + _short(c["hvp_state"], 15))
    fa, fb, da, db = c["fan_rpm_a"], c["fan_rpm_b"], c["fan_duty_a"], c["fan_duty_b"]
    fm = "{} RPM".format(int((fa + fb) * 0.5 + 0.5)) if (fa is not None and fb is not None) else ("{} RPM".format(int(fa + 0.5)) if fa is not None else "-- RPM")
    fx = max(da, db) if (da is not None and db is not None) else (da if da is not None else db)
    fs = _shi(fx, 75.0, 90.0)
    _card(10, 118, _scol(fs), "COOLING FANS", fm, "A:{}({}) B:{}({})".format(
        int(fa + 0.5) if fa is not None else "--", "{}%".format(int(da + 0.5)) if da is not None else "--",
        int(fb + 0.5) if fb is not None else "--", "{}%".format(int(db + 0.5)) if db is not None else "--"))
    bdc = c["pch_batt_w"] if c["pch_batt_w"] is not None else latest["batt_w"]
    bc, bd = (C_MUTED, "--") if bdc is None else ((C_GREEN, "CHG") if bdc < -50 else ((C_CYAN, "DIS") if bdc > 50 else (C_MUTED, "IDLE")))
    bs = "{}  PV:{:.2f}kW".format(bd, c["slow_pv_sum_w"] / 1000.0) if c["slow_pv_sum_w"] is not None else "{}  ST:{}".format(bd, _short(c["pch_state"], 11))
    _card(165, 118, bc, "BATT DC PWR", fmt_kw(bdc), bs)
    oa, ob = c["temp_oob"], c["temp_oob_chg"]
    if oa is not None or ob is not None:
        ot = int((oa or 0) + (ob or 0))
        os_, o_s = str(ot), (2 if ot > 0 else 0)
    else:
        os_, o_s = "--", 0
    ts = max(ps, a_s, fs, o_s)
    lb = "OOB TEMP WARN!" if o_s == 2 else ("OVER-TEMP WARN!" if ts == 2 else ("THERMAL: WARM" if ts == 1 else "THERMAL: OK"))
    _wbar(ts, lb, "dT:{}  OOB:{}".format(_f1(dt) + "C" if dt is not None else "--", os_))


def draw_grid_detail():
    c = comp_data
    vab = c["ac_v_ab"] if c else None
    draw_header("Grid & AC Vitals", _f1(vab, "--.-") + "V", C_GREEN if latest["grid_ok"] is not False else C_RED)
    if c is None:
        _txt(C_MUTED, "Polling PW3 vitals...", 92, 125)
        return
    va, vb = c["ac_v_an"], c["ac_v_bn"]
    dv = abs(va - vb) if (va is not None and vb is not None) else None
    vs = 2 if (vab is not None and (vab >= 254 or vab <= 222)) else (1 if ((vab is not None and (vab >= 250 or vab <= 228)) or (dv is not None and dv >= 4)) else 0)
    _card(10, 54, _scol(vs), "AC SPLIT-PHASE", _f1(vab, "--.-") + " V", "L1:{}V  L2:{}V".format(_f1(va), _f1(vb)))
    fq = c["ac_freq"]
    fs = 2 if (fq is not None and (fq >= 60.5 or fq <= 59.3)) else (1 if (fq is not None and (fq >= 60.2 or fq <= 59.8)) else 0)
    _card(165, 54, _scol(fs), "FREQUENCY", "{:.2f}Hz".format(fq) if fq is not None else "--.--Hz", "GRID: OK" if latest["grid_ok"] is not False else "GRID: ISLAND")
    acw = c["ac_power_w"] if c["ac_power_w"] is not None else c["pch_batt_w"]
    _card(10, 118, C_MAGENTA, "INVERTER AC", fmt_kw(acw), "MODE: " + _short(c["ac_mode"], 14))
    sw, lw = latest["site_w"], latest["load_w"]
    gc_, gd = (C_MUTED, "--") if sw is None else ((C_GREEN, "EXP") if sw < -50 else ((C_AMBER, "IMP") if sw > 50 else (C_CYAN, "IDLE")))
    _card(165, 118, gc_, "GRID FLOW", fmt_kw(sw), "{} HOME:{}".format(gd, "{:.2f}kW".format(lw / 1000.0) if lw is not None else "--.-kW"))
    el = latest["exp_lim_w"]
    if el is None:
        cl = getattr(cfg, "EXPORT_LIMIT_W", 0)
        if cl and cl > 0:
            el = float(cl)
    ew = -sw if (sw is not None and sw < 0.0) else 0.0
    if el is not None and el > 0.0:
        ep = int((ew / el) * 100.0 + 0.5)
        ll, es = "EXP:{:.1f}/{:.1f}kW ({}%)".format(ew / 1000.0, el / 1000.0, ep), _shi(ep, 80, 95)
    elif el == 0:
        ll, es = "EXP LIM: 0kW", _shi(ew, 50.0, 200.0)
    else:
        ll, es = "EXP LIM: --", 0
    ss = 2 if latest["site_shutdown"] is True else 0
    if ss == 2:
        rl = "SHUTDOWN!"
    elif vs == 2:
        rl = "V OUT!"
    elif fs == 2:
        rl = "CURTAIL!" if (fq is not None and fq >= 60.5) else "FREQ!"
    elif vs == 1:
        rl = "V HIGH" if (vab is not None and vab >= 250) else ("V LOW" if (vab is not None and vab <= 228) else "dV:{:.1f}V".format(dv))
    elif fs == 1:
        rl = "FREQ WARN"
    else:
        rl = "OK dV:{:.1f}V".format(dv) if dv is not None else "GRID OK"
    _wbar(max(es, vs, fs, ss), ll, rl)


def draw_strings_detail():
    c = comp_data
    dc = c["strings_dc_w"] if c else None
    acs = latest["solar_w"]
    hw = dc if (dc is not None and dc > 0.0) else acs
    draw_header("Solar Strings", "{:.2f}kW".format(hw / 1000.0) if hw is not None else "--.-kW", C_AMBER)
    if c is None:
        _txt(C_MUTED, "Polling PW3 strings...", 88, 125)
        return
    dck = dc / 1000.0
    if acs is not None and dc >= 100.0 and acs >= 0.0:
        sl = "DC:{:.2f}kW  AC:{:.2f}kW  EFF:{}%".format(dck, acs / 1000.0, int(min(100.0, (acs / dc) * 100.0) + 0.5))
    elif acs is not None:
        sl = "DC SUM:{:.2f}kW   AC SOLAR:{:.2f}kW".format(dck, acs / 1000.0)
    else:
        sl = "DC SUM:{:.2f}kW".format(dck)
    _txt(C_MUTED, sl, 10, 50)
    for i, sr in enumerate(c["strings"]):
        ry = 64 + i * 25
        sw, sv, si, st = sr["w"] or 0.0, sr["v"], sr["i"], sr["state"] or ""
        act = ("Active" in st) or sw >= 5.0
        _rect(C_PANEL, 8, ry, W - 16, 22)
        _txt(C_AMBER if sw >= 10.0 else (C_CYAN if act else C_MUTED), sr["id"], 13, ry + 4, 2)
        _rect(C_GRID, 32, ry + 3, 132, 8)
        pct = 0
        if dc > 1.0 and sw > 0.0:
            fr = min(1.0, sw / dc)
            fw = int(fr * 132)
            if fw > 0:
                _rect(C_AMBER, 32, ry + 3, fw, 8)
            pct = int(fr * 100.0 + 0.5)
        va = "{:.0f}V  {:.1f}A".format(sv or 0.0, si or 0.0) if (act and (sv is not None or si is not None)) else (_short(st, 16) if st else "--")
        _txt(C_MUTED, va, 32, ry + 13)
        _txtr(C_TEXT if sw >= 10.0 else C_MUTED, "{:.2f}kW".format(sw / 1000.0), 274, ry + 4, 2)
        _txtr(C_AMBER if pct > 0 else C_MUTED, "{:2d}%".format(pct), W - 12, ry + 8)


def draw_ui():
    D.set_pen(C_BG)
    D.clear()
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
    D.update()


_WERR = {-1: "WIFI FAIL (-1)", -2: "NO AP (-2)", -3: "BAD PWD (-3)", 0: "WIFI IDLE (0)", 1: "JOINING (1)", 2: "NO DHCP IP (2)"}
_wtried = False


def init_wifi(wdt=None):
    try:
        network.country(cfg.WIFI_COUNTRY)
    except Exception:
        pass
    w = network.WLAN(network.STA_IF)
    if not w.active():
        w.active(True)
        time.sleep_ms(300)
        if wdt:
            wdt()
    try:
        w.config(pm=getattr(network.WLAN, "PM_NONE", 0xA11140))
    except Exception:
        pass
    return w


def ensure_wifi(wlan, wdt, btn_cb=None):
    global status_msg, status_is_err, _wtried
    if wlan.isconnected():
        return True
    status_msg, status_is_err = "CONNECTING WIFI...", True
    draw_ui()
    if _wtried:
        try:
            wlan.disconnect()
        except Exception:
            pass
        time.sleep_ms(300)
    _wtried = True
    try:
        wlan.config(pm=getattr(network.WLAN, "PM_NONE", 0xA11140))
    except Exception:
        pass
    print("WiFi: connecting to '{}'...".format(cfg.WIFI_SSID))
    wlan.connect(cfg.WIFI_SSID, cfg.WIFI_PASS)
    dl = t_add(t_ms(), 20000)
    while True:
        if wdt:
            wdt()
        if btn_cb:
            btn_cb(t_ms())
        st = wlan.status()
        if st == 3 or wlan.isconnected():
            break
        if st < 0:
            status_msg = _WERR.get(st, "WIFI ERR ({})".format(st))
            print("WiFi failed:", status_msg)
            return False
        if t_diff(dl, t_ms()) <= 0:
            status_msg = "TIMEOUT " + _WERR.get(st, "({})".format(st))
            print("WiFi timed out:", status_msg)
            return False
        time.sleep_ms(100)
    if cfg.WIFI_POWERSAVE:
        try:
            wlan.config(pm=getattr(network.WLAN, "PM_POWERSAVE", 0x000011))
        except Exception:
            pass
    ip = wlan.ifconfig()[0]
    status_msg, status_is_err = "WIFI " + ip, False
    print("WiFi connected! IP:", ip)
    return True


def main():
    global view_mode, bl_mode, last_input_ms, status_msg, status_is_err, consec_err
    global comp_data, comp_last_ms, comp_pending, cd_until

    print("Booting PW3...")
    set_backlight(cfg.BACKLIGHT_ACTIVE)
    draw_ui()
    if cfg.ENABLE_WDT:
        time.sleep_ms(2500)
        wdt = WDT(timeout=8000).feed
    else:
        wdt = None

    try:
        print("Init WiFi...")
        wlan = init_wifi(wdt)
        tmo = min(cfg.SOCKET_TIMEOUT_S, 7) if cfg.ENABLE_WDT else cfg.SOCKET_TIMEOUT_S
        ga = lambda n, d=b"": getattr(tq, n, d)
        client = Powerwall3Client(
            cfg.PW_HOST, cfg.PW_PORT, cfg.GW_PASSWORD, tmo,
            cfg.SOC_SCALE_TO_APP, cfg.UTC_OFFSET_HOURS,
            ga("PAYLOAD_FILE", ""), ga("GRAPHQL_PAYLOAD"),
            tq.QUERY_TEXT, tq.QUERY_SIG, tq.QUERY_VARS, tq.IS_REAL_SIGNED_QUERY,
            ga("COMPONENTS_PAYLOAD"), ga("COMPONENTS_QUERY_TEXT"),
            ga("COMPONENTS_QUERY_SIG"), ga("COMPONENTS_QUERY_VARS", b"{}"),
        )
    except Exception as e:
        status_msg, status_is_err = "BOOT: " + str(e)[:16], True
        print("Boot error:", e)
        draw_ui()
        raise
    gc.collect()
    print("Boot complete. Free RAM:", gc.mem_free(), "bytes")

    prev = [1, 1, 1, 1]
    next_poll = t_ms()
    cfresh = min(15, cfg.POLL_INTERVAL_S) * 1000

    def _poll_btn(now):
        global view_mode, bl_mode, last_input_ms, comp_pending
        dirty = False
        for i in range(4):
            v = btns[i].value()
            if prev[i] == 1 and v == 0:
                last_input_ms = now
                if bl_mode == BL_OFF and i != 3:
                    bl_mode = BL_AUTO
                elif i == 0:
                    view_mode = VIEW_BATT_DETAIL if view_mode == VIEW_SOC else VIEW_SOC
                elif i == 1:
                    view_mode = VIEW_GRID_DETAIL if view_mode == VIEW_GRID else VIEW_GRID
                elif i == 2:
                    view_mode = VIEW_STRINGS_DETAIL if view_mode == VIEW_FLOW else VIEW_FLOW
                else:
                    bl_mode = (bl_mode + 1) % 3
                comp_pending = is_detail_view() and (comp_last_ms is None or t_diff(now, comp_last_ms) >= cfresh)
                dirty = True
            prev[i] = v
        update_backlight(now)
        if dirty:
            draw_ui()

    def _run_vitals():
        global comp_data, comp_last_ms, comp_pending, cd_until, status_msg, status_is_err, consec_err
        gc.collect()
        try:
            c = client.poll_components(wdt_feed=wdt)
            comp_data, c = c, None
            gc.collect()
            comp_last_ms, comp_pending = t_ms(), False
            if comp_data.get("time_str") and latest["time_str"] == "--:--":
                latest["time_str"] = comp_data["time_str"]
            status_msg, status_is_err = "OK (VITALS)", False
            print("VITALS: DC={:.0f}W VAB={}V FREQ={}Hz TMAX={}C OOB={}/{} FREE={}B".format(
                comp_data["strings_dc_w"], comp_data["ac_v_ab"], comp_data["ac_freq"],
                comp_data["pack_temp_max"], comp_data["temp_oob"], comp_data["temp_oob_chg"], gc.mem_free()))
        except RateLimitError as e:
            consec_err += 1
            comp_pending = False
            cd_until = t_add(t_ms(), cfg.RATE_LIMIT_COOLDOWN_S * 1000)
            status_msg, status_is_err = "429 COOLDOWN 5m", True
            print("Vitals rate limited:", e)
        except AuthError as e:
            consec_err += 1
            comp_pending = False
            status_msg, status_is_err = "AUTH ERR: " + str(e), True
            print("Vitals auth error:", e)
        except Exception as e:
            comp_pending = False
            gc.collect()
            status_msg, status_is_err = "VITALS: " + str(e)[:14], True
            print("Vitals poll error:", e, "FREE={}B".format(gc.mem_free()))

    while True:
        if wdt:
            wdt()
        now = t_ms()
        _poll_btn(now)
        in_cd = cd_until is not None and t_diff(cd_until, now) > 0
        if comp_pending and is_detail_view() and not in_cd:
            if ensure_wifi(wlan, wdt, _poll_btn):
                status_msg, status_is_err = "POLLING VITALS...", False
                draw_ui()
                _run_vitals()
                if cd_until is not None and t_diff(cd_until, t_ms()) > 0:
                    next_poll = cd_until
            else:
                comp_pending = False
            update_led()
            draw_ui()
        if t_diff(now, next_poll) >= 0:
            if ensure_wifi(wlan, wdt, _poll_btn):
                if status_msg == "BOOTING PW3..." or status_msg.startswith("WIFI "):
                    status_msg, status_is_err = "POLLING PW3...", False
                    draw_ui()
                try:
                    gc.collect()
                    d = client.poll(wdt_feed=wdt)
                    for k in ("soc_pct", "remaining_wh", "full_wh", "batt_w", "solar_w", "load_w", "site_w", "grid_ok", "site_shutdown", "exp_lim_w"):
                        latest[k] = d[k]
                    if d["time_str"]:
                        latest["time_str"] = d["time_str"]
                    d = None
                    gc.collect()
                    push_history(latest["soc_pct"], latest["batt_w"], latest["site_w"])
                    consec_err, cd_until = 0, None
                    status_msg, status_is_err = "OK (TEDAPI)", False
                    delay = cfg.POLL_INTERVAL_S * 1000
                    print("SOC={:.1f}% REM={:.2f}kWh BATT={}W SOLAR={}W EXPLIM={}W FREE={}B".format(
                        latest["soc_pct"], latest["remaining_wh"] / 1000.0,
                        latest["batt_w"], latest["solar_w"], latest["exp_lim_w"], gc.mem_free()))
                    if is_detail_view():
                        _run_vitals()
                        if cd_until is not None and t_diff(cd_until, t_ms()) > 0:
                            delay = cfg.RATE_LIMIT_COOLDOWN_S * 1000
                except RateLimitError as e:
                    consec_err += 1
                    status_msg, status_is_err = "429 COOLDOWN 5m", True
                    delay = cfg.RATE_LIMIT_COOLDOWN_S * 1000
                    cd_until = t_add(t_ms(), delay)
                    print("Rate limited:", e)
                except AuthError as e:
                    consec_err += 1
                    status_msg, status_is_err = "AUTH ERR: " + str(e), True
                    delay = 60000
                    print("Auth error:", e)
                except Exception as e:
                    consec_err += 1
                    gc.collect()
                    status_msg, status_is_err = "ERR: " + str(e)[:18], True
                    delay = min(120, 10 * (1 << min(consec_err, 4))) * 1000
                    print("Poll error:", e, "FREE={}B".format(gc.mem_free()))
            else:
                consec_err += 1
                delay = 15000
            next_poll = t_add(t_ms(), delay)
            update_led()
            draw_ui()
        time.sleep_ms(50)


if __name__ == "__main__":
    main()
