# pico/config.py
# Configuration for Tesla Powerwall 3 + Raspberry Pi Pico W (RP2040, MicroPython v1.25.0)
# + Pimoroni Pico Display Pack 2.8" (zero flash writes / RAM-only history).

# --- Powerwall 3 Built-In Wi-Fi AP ---
# SSID & password are on the QR sticker on the side or behind the front cover
# of your leader Powerwall 3.
# For desk testing with tools/mock_powerwall.py, temporarily use your home Wi-Fi.
WIFI_SSID = "TeslaPW_XXXXXX"
WIFI_PASS = "YOUR_PW3_WIFI_PASSWORD"
WIFI_COUNTRY = "US"              # Two-letter ISO code (e.g., "US", "GB", "AU", "DE")

# --- Powerwall 3 TEDAPI Endpoint ---
PW_HOST = "192.168.91.1"         # Powerwall 3 AP gateway IP (or PC LAN IP for mock)
PW_PORT = 443                    # 443 for real Powerwall 3; 8443 for mock_powerwall.py
GW_PASSWORD = WIFI_PASS          # TEDAPI Basic Auth password (same as AP password)

# --- Polling & Telemetry ---
POLL_INTERVAL_S = 30             # 30-60s recommended (avoid <15s to prevent PW3 Wi-Fi/429 issues)
SOCKET_TIMEOUT_S = 12            # RP2040 software TLS + PW3 response needs 8-12s headroom
RATE_LIMIT_COOLDOWN_S = 300      # 5-minute cooldown if gateway returns HTTP 429/503
SOC_SCALE_TO_APP = True          # True = Tesla App SoC ((raw - 5) / 0.95); False = raw physical SoC
DEFAULT_CAPACITY_WH = 13500      # Fallback until first nominalFullPackEnergyWh reading (auto-scales)
HISTORY_HOURS = 24               # Trend chart span in hours (e.g. 12 or 24; RAM-only, no flash writes)
HISTORY_STEP_S = 300             # Chart bucket interval in seconds (300s = 5 min -> 288 pts for 24h = 1.1 KB RAM)
EXPORT_LIMIT_W = 0               # Optional fallback grid-export cap in W if utility doesn't send ieee20305 (0 = auto only)

# --- Clock Display (uses PW3 local system.time + auto DST; fallback to HTTP Date) ---
UTC_OFFSET_HOURS = 0             # Fallback offset if system.time is absent (e.g., -8 PST)

# --- Always-On Low-Power & Display Pack 2.8" Settings ---
WIFI_POWERSAVE = False           # Keep False for PW3 AP stability (CYW43 PM_POWERSAVE can drop packets)
BACKLIGHT_ACTIVE = 0.75          # Brightness after button press (0.0 - 1.0)
BACKLIGHT_IDLE = 0.45            # Dimmed idle brightness (Pimoroni uses gamma PWM; <0.30 can look off)
BACKLIGHT_DIM_AFTER_S = 30       # Seconds of inactivity before auto-dimming
LOW_SOC_ALERT_PCT = 20.0         # Subtle red LED alert below this SOC % (0 disables)
DISPLAY_ROTATE = 0               # 0 or 180 for landscape orientation

# --- Hardware Watchdog (RP2040 max ~8.3s) ---
# Keep False while testing in Thonny. Set True for unattended 24/7 operation.
# (main.py waits 2.5s at boot before arming WDT so Thonny can still interrupt on USB plug-in.)
ENABLE_WDT = False
