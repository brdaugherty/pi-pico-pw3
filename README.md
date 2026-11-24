# Powerwall 3 Monitor — Raspberry Pi Pico W (RP2040, v1.25.0) + Pimoroni Display Pack 2.8"

Self-contained, zero-C-compilation MicroPython dashboard for **Tesla Powerwall 3** (`TEDAPI` over `192.168.91.1:443`), tailored for **Pimoroni MicroPython `picow v1.25.0` (RP2040)** with **zero flash writes** (RAM-only history).

## Files

- `pico/config.py` — Powerwall 3 AP credentials (`TeslaPW_XXXXXX`), poll interval, UTC offset, backlight, and watchdog toggle.
- `pico/powerwall_api.py` — Lean Powerwall 3 TEDAPI HTTPS/1.1 + Protobuf client (`DeviceControllerQuery` + on-demand `ComponentsQuery`) with two-phase gzip handling (closes TLS + `gc.collect()` before `deflate.DeflateIO` so mbedTLS 44 KB and GZIP 32 KB never overlap on RP2040) and O(1)-RAM streaming field extraction.
- `pico/tedapi_query.py` + `pico/tedapi.bin` — Signed TEDAPI GraphQL payloads (`DeviceControllerQuery` + `ComponentsQuery`, generated via `tools/export_tedapi_query.py`); `tedapi.bin` is streamed directly from flash (0 B payload RAM on RP2040).
- `pico/main.py` — Cooperative always-on dashboard: `PEN_P4` (38.4 KB framebuffer allocated first before imports), 24-hour in-RAM trend charts (288 × 5-min buckets, 2.8 KB RAM, zero flash wear), press-again-to-toggle A/B/X primary & detail views (`A`: SoC % + Batt In/Out kW ↔ Battery/Thermal + OOB warning light, `B`: Grid In/Out kW ↔ Grid/AC + `ieee20305` export-limit warning light, `X`: Power Flow [default] ↔ Solar Strings A–F, `Y`: Backlight short-press / Wi-Fi Setup AP hold 1.5 s), on-demand `ComponentsQuery` polling only on detail views, local `system.time` clock (auto-DST), GP26–28 RGB LED, auto-dimming backlight, Wi-Fi auto-reconnect, and WDT with 2.5 s USB interrupt grace window.
- `pico/wifi_setup.py` — On-demand SoftAP captive portal (`PicoPW3-Setup` at `http://192.168.4.1`, imported only when entering Setup so it uses 0 B RAM during normal operation). Scans nearby `TeslaPW_*` networks, lets you pick the AP and enter the QR sticker password from a phone/browser, saves to `/wifi.json` on flash, and reboots cleanly.
- `tools/export_tedapi_query.py` — One-time PC script to export the verified Tesla-signed `DeviceControllerQuery` and `ComponentsQuery` payloads from `pypowerwall` into `pico/tedapi.bin` + `pico/tedapi_query.py`.
- `tools/mock_powerwall.py` — Self-signed TLS Powerwall 3 TEDAPI simulator (both `DeviceControllerQuery` and `ComponentsQuery` with strings A–F, temps, fans, AC/grid vitals, plus `--gzip`) for PC bench testing.

## Quick Start (Already on Pimoroni `picow v1.25.0`)

1. On your PC, generate `pico/tedapi.bin` and `pico/tedapi_query.py`:
   ```bash
   pip install -U pypowerwall
   python3 tools/export_tedapi_query.py
   ```
2. Optional bench test on PC (try both without and with `--gzip`):
   ```bash
   python3 tools/mock_powerwall.py --port 8443 --password TESTPASS --gzip
   ```
3. Upload all files in `pico/` (`main.py`, `wifi_setup.py`, `powerwall_api.py`, `config.py`, `tedapi_query.py`, and `tedapi.bin`) to `/` on your Pico W via **Thonny**.
4. Configure Wi-Fi from your phone (no `config.py` editing needed): on first boot with placeholder credentials — or anytime by **holding Button Y for 1.5 s** — the Pico starts an open hotspot **`PicoPW3-Setup`**. Join it on your phone, open **`http://192.168.4.1`** (or tap the captive portal popup), select your `TeslaPW_XXXXXX` network, enter the QR sticker password, and tap **Save & Reboot**.
5. Once verified against your Powerwall 3, you can enable the hardware watchdog (`ENABLE_WDT`) either in the Advanced Settings section of the setup page or in `pico/config.py`.
