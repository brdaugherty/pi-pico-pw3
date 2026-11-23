# Powerwall 3 Monitor — Raspberry Pi Pico W (RP2040, v1.25.0) + Pimoroni Display Pack 2.8"

Self-contained, zero-C-compilation MicroPython dashboard for **Tesla Powerwall 3** (`TEDAPI` over `192.168.91.1:443`), tailored for **Pimoroni MicroPython `picow v1.25.0` (RP2040)** with **zero flash writes** (RAM-only history).

## Files

- `pico/config.py` — Powerwall 3 AP credentials (`TeslaPW_XXXXXX`), poll interval, UTC offset, backlight, and watchdog toggle.
- `pico/powerwall_api.py` — Lean Powerwall 3 TEDAPI HTTPS/1.1 + Protobuf client (`DeviceControllerQuery` + on-demand `ComponentsQuery`) with two-phase gzip handling (closes TLS + `gc.collect()` before `deflate.DeflateIO` so mbedTLS 44 KB and GZIP 32 KB never overlap on RP2040) and O(1)-RAM streaming field extraction.
- `pico/tedapi_query.py` — Signed TEDAPI GraphQL payloads (`DeviceControllerQuery` + `ComponentsQuery`, populated via `tools/export_tedapi_query.py`).
- `pico/main.py` — Cooperative always-on dashboard: `PEN_P4` (38.4 KB framebuffer), 24-hour in-RAM filled area trend charts (288 × 5-min buckets, 1.1 KB RAM, zero flash wear), press-again-to-toggle A/B/X primary & detail views (`A`: SoC ↔ Battery/Thermal + OOB warning light, `B`: kWh ↔ Grid/AC + `ieee20305` export-limit warning light, `X`: Power Flow ↔ Solar Strings A–F, `Y`: Backlight), on-demand `ComponentsQuery` polling only on detail views, local `system.time` clock (auto-DST), GP26–28 RGB LED, auto-dimming backlight, Wi-Fi auto-reconnect, and WDT with 2.5 s USB interrupt grace window.
- `tools/export_tedapi_query.py` — One-time PC script to export the verified Tesla-signed `DeviceControllerQuery` and `ComponentsQuery` payloads from `pypowerwall` into `pico/tedapi_query.py`.
- `tools/mock_powerwall.py` — Self-signed TLS Powerwall 3 TEDAPI simulator (both `DeviceControllerQuery` and `ComponentsQuery` with strings A–F, temps, fans, AC/grid vitals, plus `--gzip`) for PC bench testing.

## Quick Start (Already on Pimoroni `picow v1.25.0`)

1. On your PC, generate `pico/tedapi_query.py`:
   ```bash
   pip install -U pypowerwall
   python3 tools/export_tedapi_query.py
   ```
2. Optional bench test on PC (try both without and with `--gzip`):
   ```bash
   python3 tools/mock_powerwall.py --port 8443 --password TESTPASS --gzip
   ```
3. Edit `pico/config.py`, then upload all 4 files in `pico/` to `/` on your Pico W via **Thonny**.
4. Once verified against your Powerwall 3, set `ENABLE_WDT = True` in `pico/config.py` for unattended 24/7 operation.
