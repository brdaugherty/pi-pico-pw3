# pico/main.py
# Always-on, low-power Tesla Powerwall 3 dashboard for Raspberry Pi Pico W (RP2040)
# running Pimoroni MicroPython v1.25.0 + Pimoroni Pico Display Pack 2.8" (320x240).
#
# Zero flash writes: 120-sample trend history is held in fixed-size RAM arrays (480 bytes).
# Buttons:
#   A (GP12) : State of Charge (%) trend graph
#   B (GP13) : Stored Energy (kWh) trend graph
#   X (GP14) : Live Power Flow overview (Solar / Home / Battery / Grid)
#   Y (GP15) : Cycle backlight (Auto-Dim -> Bright -> Screen Off)

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

# Display Pack 2.8" wires RGB LED to GP26, GP27, GP28 (zeroed at boot to stop dim glow)
led = RGBLED(26, 27, 28)
led.set_rgb(0, 0, 0)

btn_a = Pin(12, Pin.IN, Pin.PULL_UP)
btn_b = Pin(13, Pin.IN, Pin.PULL_UP)
btn_x = Pin(14, Pin.IN, Pin.PULL_UP)
btn_y = Pin(15, Pin.IN, Pin.PULL_UP)

# --- Fixed-Size In-RAM Ring Buffers (Zero Flash Wear, Zero Persistence) ---
CAP = config.HISTORY_SAMPLES
hist_soc = array.array("H", [0] * CAP)      # tenths of % (0..1000)
hist_wh = array.array("H", [0] * CAP)       # tens of Wh  (0..655350 Wh)
hist_count = 0
hist_head = 0

# --- UI & Runtime State ---
VIEW_SOC = 0
VIEW_KWH = 1
VIEW_FLOW = 2
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
    "time_str": "--:--",
}
status_msg = "BOOTING PW3..."
status_is_err = False
consec_errors = 0


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


def push_history(soc_pct, rem_wh):
    global hist_count, hist_head
    hist_soc[hist_head] = int(max(0.0, min(100.0, soc_pct)) * 10.0 + 0.5)
    hist_wh[hist_head] = int(max(0.0, min(655350.0, rem_wh)) / 10.0 + 0.5)
    hist_head = (hist_head + 1) % CAP
    if hist_count < CAP:
        hist_count += 1


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

    right_info = "{}  A:SOC B:kWh X:PWR".format(latest["time_str"] or "--:--")
    display.set_pen(C_MUTED)
    draw_text_right(right_info, WIDTH - 6, HEIGHT - 15, scale=1)


def draw_chart(is_soc):
    if is_soc:
        val = latest["soc_pct"]
        big = "{:.1f}%".format(val) if val is not None else "--.-%"
        color = C_CYAN
        if val is not None and val < config.LOW_SOC_ALERT_PCT:
            color = C_RED
        draw_header("State of Charge", big, color)
        max_val = 100.0
        unit = "%"
    else:
        rem = latest["remaining_wh"]
        full = latest["full_wh"] or float(config.DEFAULT_CAPACITY_WH)
        big = "{:.2f}kWh".format(rem / 1000.0) if rem is not None else "--.-kWh"
        color = C_MAGENTA
        draw_header("Stored Energy", big, color)
        max_val = max(1.0, full / 1000.0)
        unit = "kWh"

    x0, y0 = 42, 56
    x1, y1 = WIDTH - 10, HEIGHT - 32
    pw = x1 - x0
    ph = y1 - y0

    for frac in (0.0, 0.5, 1.0):
        gy = y1 - int(frac * ph)
        display.set_pen(C_GRID)
        display.line(x0, gy, x1, gy)
        lbl_val = max_val * frac
        lbl = "{:.0f}".format(lbl_val) if max_val >= 10 else "{:.1f}".format(lbl_val)
        display.set_pen(C_MUTED)
        draw_text_right(lbl, x0 - 4, gy - 4, scale=1)

    display.set_pen(C_AXIS)
    display.line(x0, y0, x0, y1)
    display.line(x0, y1, x1, y1)

    span_min = (CAP * config.POLL_INTERVAL_S) // 60
    display.set_pen(C_MUTED)
    display.text("-{}m".format(span_min), x0, y1 + 3, scale=1)
    draw_text_right("now ({})".format(unit), x1, y1 + 3, scale=1)

    if hist_count == 0:
        display.set_pen(C_MUTED)
        display.text("Waiting for PW3 data...", x0 + 36, y0 + ph // 2 - 8, scale=1)
        return

    src = hist_soc if is_soc else hist_wh
    scale_div = 10.0 if is_soc else 100.0
    step_den = CAP - 1 if CAP > 1 else 1
    offset = CAP - hist_count

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


def draw_ui():
    display.set_pen(C_BG)
    display.clear()
    if view_mode == VIEW_SOC:
        draw_chart(True)
    elif view_mode == VIEW_KWH:
        draw_chart(False)
    else:
        draw_power_flow()
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
    )

    gc.collect()
    print("Boot complete. Free RAM:", gc.mem_free(), "bytes")

    prev_btn = [1, 1, 1, 1]
    next_poll_ms = time.ticks_ms()

    while True:
        if wdt_feed:
            wdt_feed()
        now = time.ticks_ms()

        # 1. Edge-triggered debounced button handling
        cur_btn = [btn_a.value(), btn_b.value(), btn_x.value(), btn_y.value()]
        dirty = False
        for i in range(4):
            if prev_btn[i] == 1 and cur_btn[i] == 0:
                last_input_ms = now
                if bl_mode == BL_OFF and i != 3:
                    bl_mode = BL_AUTO
                elif i == 0:
                    view_mode = VIEW_SOC
                elif i == 1:
                    view_mode = VIEW_KWH
                elif i == 2:
                    view_mode = VIEW_FLOW
                elif i == 3:
                    bl_mode = (bl_mode + 1) % 3
                dirty = True
        prev_btn = cur_btn
        update_backlight(now)

        if dirty:
            draw_ui()

        # 2. Cooperative Powerwall 3 poll (never inside a Timer ISR)
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
                    if data["time_str"]:
                        latest["time_str"] = data["time_str"]

                    push_history(latest["soc_pct"], latest["remaining_wh"])

                    consec_errors = 0
                    status_msg = "OK (TEDAPI)"
                    status_is_err = False
                    delay_ms = config.POLL_INTERVAL_S * 1000
                    print(
                        "SOC={:.1f}% REM={:.2f}kWh BATT={}W SOLAR={}W FREE={}B".format(
                            latest["soc_pct"],
                            latest["remaining_wh"] / 1000.0,
                            latest["batt_w"],
                            latest["solar_w"],
                            gc.mem_free(),
                        )
                    )
                except RateLimitError as e:
                    consec_errors += 1
                    status_msg = "429 COOLDOWN 5m"
                    status_is_err = True
                    delay_ms = config.RATE_LIMIT_COOLDOWN_S * 1000
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
