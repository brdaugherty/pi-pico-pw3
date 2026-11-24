# pico/wifi_setup.py
# On-demand SoftAP captive portal for configuring Powerwall 3 Wi-Fi credentials
# without editing config.py. Imported ONLY when entering Setup (long-press Y or
# unconfigured first boot) so it consumes 0 B RAM during normal dashboard operation.

import gc
import json
import os
import select
import socket
import time
import network
import machine

AP_SSID = "PicoPW3-Setup"
SETTINGS_FILE = "wifi.json"
TIMEOUT_MS = 300000  # 5 min inactivity auto-exit


def _esc(s):
    s = str(s)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _urldecode(s):
    s = s.replace("+", " ")
    out = bytearray()
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "%" and i + 2 < n:
            try:
                out.append(int(s[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.extend(ch.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "ignore")


def _parse_form(body):
    res = {}
    if not body:
        return res
    for pair in body.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            res[_urldecode(k)] = _urldecode(v)
    return res


def _load_existing():
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_settings(data):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    try:
        os.rename(tmp, SETTINGS_FILE)
    except OSError:
        try:
            os.remove(SETTINGS_FILE)
        except OSError:
            pass
        os.rename(tmp, SETTINGS_FILE)


def _scan_aps(wdt=None):
    best = {}
    sta = network.WLAN(network.STA_IF)
    try:
        if not sta.active():
            sta.active(True)
            time.sleep_ms(250)
        if wdt:
            wdt()
        try:
            if sta.isconnected():
                sta.disconnect()
        except Exception:
            pass
        for row in sta.scan():
            if wdt:
                wdt()
            try:
                ssid = row[0].decode("utf-8", "ignore").strip()
                rssi = int(row[3])
            except Exception:
                continue
            if not ssid:
                continue
            if ssid not in best or rssi > best[ssid]:
                best[ssid] = rssi
    except Exception as e:
        print("Setup scan error:", e)
    try:
        sta.active(False)
    except Exception:
        pass
    time.sleep_ms(200)
    if wdt:
        wdt()

    def _sort_key(item):
        s, rssi = item
        is_pw = s.startswith("TeslaPW_") or s.startswith("TEG-")
        return (0 if is_pw else 1, -rssi)

    aps = sorted(best.items(), key=_sort_key)
    return aps[:15]


def _start_ap(wdt=None):
    ap = network.WLAN(network.AP_IF)
    for kw in (
        {"essid": AP_SSID, "security": 0},
        {"essid": AP_SSID, "authmode": 0},
        {"ssid": AP_SSID, "security": 0},
    ):
        try:
            ap.config(**kw)
            break
        except Exception:
            pass
    ap.active(True)
    for _ in range(30):
        if wdt:
            wdt()
        if ap.active():
            break
        time.sleep_ms(100)
    ip = ap.ifconfig()[0]
    if not ip or ip == "0.0.0.0":
        ip = "192.168.4.1"
    return ap, ip


def _build_dns_reply(pkt, ip_bytes):
    if len(pkt) < 12:
        return None
    # Walk QNAME from offset 12
    idx = 12
    n = len(pkt)
    while idx < n and pkt[idx] != 0:
        idx += 1 + pkt[idx]
    q_end = idx + 5  # null byte + QTYPE(2) + QCLASS(2)
    if q_end > n:
        return None
    hdr = pkt[0:2] + b"\x81\x80" + pkt[4:6] + b"\x00\x01\x00\x00\x00\x00"
    ans = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + ip_bytes
    return hdr + pkt[12:q_end] + ans


def _render_form(aps, cur, err=""):
    cur_ssid = cur.get("ssid", "")
    has_pw = bool(cur.get("pass", ""))
    opts = []
    seen = set()
    for ssid, rssi in aps:
        seen.add(ssid)
        tag = " [Powerwall]" if (ssid.startswith("TeslaPW_") or ssid.startswith("TEG-")) else ""
        sel = " selected" if (ssid == cur_ssid or (not cur_ssid and not opts)) else ""
        opts.append('<option value="{}"{}>{}{} ({} dBm)</option>'.format(_esc(ssid), sel, _esc(ssid), tag, rssi))
    if cur_ssid and cur_ssid not in seen:
        opts.insert(0, '<option value="{}" selected>{} (saved)</option>'.format(_esc(cur_ssid), _esc(cur_ssid)))
    if not opts:
        opts.append('<option value="">(No networks scanned - type below)</option>')

    err_html = '<div class="err">{}</div>'.format(_esc(err)) if err else ""
    pw_ph = "Leave blank to keep saved password" if has_pw else "Powerwall QR sticker password"
    soc_chk = " checked" if cur.get("soc_app", True) else ""
    wdt_chk = " checked" if cur.get("wdt", False) else ""

    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Powerwall 3 Wi-Fi Setup</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;background:#0a0e16;color:#f0f4fa;margin:0;padding:16px}"
        ".card{max-width:420px;margin:0 auto;background:#141c2a;padding:18px;border-radius:12px}"
        "h1{font-size:20px;margin:0 0 6px;color:#00dcff}p{color:#8c9baf;font-size:13px;margin:0 0 14px}"
        "label{display:block;font-size:13px;margin:12px 0 4px;color:#c8d4e6}"
        "input,select{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #2a364a;background:#0a0e16;color:#f0f4fa;font-size:15px}"
        ".chk{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:13px}.chk input{width:auto}"
        "details{margin-top:14px;border-top:1px solid #2a364a;padding-top:10px}summary{cursor:pointer;color:#8c9baf;font-size:13px}"
        "button{width:100%;margin-top:16px;padding:12px;border:0;border-radius:8px;background:#00b8d9;color:#071018;font-weight:700;font-size:16px;cursor:pointer}"
        ".err{background:#4a151b;color:#ff8a8a;padding:10px;border-radius:8px;margin-bottom:10px;font-size:13px}"
        "</style></head><body><div class=card>"
        "<h1>Powerwall 3 Setup</h1>"
        "<p>Select your TeslaPW_XXXXXX Wi-Fi network and enter the QR sticker password.</p>"
        + err_html
        + "<form method=POST action=/save>"
        "<label>Scanned Wi-Fi Networks</label><select name=ssid>"
        + "".join(opts)
        + "</select>"
        "<label>Or Type SSID Manually (optional)</label>"
        "<input name=ssid_manual placeholder='Overrides dropdown if filled'>"
        "<label>Wi-Fi / Gateway Password</label>"
        "<input id=pw type=password name=pass autocomplete=off placeholder='" + pw_ph + "'>"
        "<label class=chk><input type=checkbox onclick=\"document.getElementById('pw').type=this.checked?'text':'password'\">Show password</label>"
        "<details><summary>Advanced Settings (optional)</summary>"
        "<label>Powerwall IP</label><input name=pw_host value='" + _esc(cur.get("pw_host", "192.168.91.1")) + "'>"
        "<label>Port</label><input name=pw_port type=number value='" + str(cur.get("pw_port", 443)) + "'>"
        "<label>Poll Interval (s)</label><input name=poll_s type=number value='" + str(cur.get("poll_s", 30)) + "'>"
        "<label>Export Limit Fallback (W, 0=auto)</label><input name=exp_lim_w type=number value='" + str(cur.get("exp_lim_w", 0)) + "'>"
        "<label class=chk><input type=checkbox name=soc_app value=1" + soc_chk + ">Scale SoC to Tesla App</label>"
        "<label class=chk><input type=checkbox name=wdt value=1" + wdt_chk + ">Enable Hardware Watchdog (24/7)</label>"
        "</details>"
        "<button type=submit>Save &amp; Reboot</button>"
        "</form></div></body></html>"
    )


def _render_saved(ssid):
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Saved</title>"
        "<style>body{font-family:system-ui,sans-serif;background:#0a0e16;color:#f0f4fa;padding:24px;text-align:center}"
        ".card{max-width:380px;margin:20px auto;background:#141c2a;padding:22px;border-radius:12px}"
        "h1{color:#2ddc78;margin:0 0 10px}p{color:#c8d4e6;font-size:14px}</style></head><body><div class=card>"
        "<h1>Settings Saved!</h1>"
        "<p>Network: <b>" + _esc(ssid) + "</b></p>"
        "<p>Your Pico W is rebooting now and will connect automatically. You can close this page.</p>"
        "</div></body></html>"
    )


def _read_http_request(conn):
    conn.settimeout(2.0)
    buf = b""
    hdr_end = -1
    while hdr_end < 0 and len(buf) < 4096:
        chunk = conn.recv(512)
        if not chunk:
            break
        buf += chunk
        hdr_end = buf.find(b"\r\n\r\n")
    if hdr_end < 0:
        return None, None, ""
    head = buf[:hdr_end].decode("utf-8", "ignore")
    body = buf[hdr_end + 4 :]
    lines = head.split("\r\n")
    if not lines:
        return None, None, ""
    parts = lines[0].split(" ")
    if len(parts) < 2:
        return None, None, ""
    method, path = parts[0].upper(), parts[1].split("?", 1)[0]
    clen = 0
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            if k.strip().lower() == "content-length":
                try:
                    clen = min(2048, max(0, int(v.strip())))
                except ValueError:
                    clen = 0
    while len(body) < clen:
        chunk = conn.recv(min(512, clen - len(body)))
        if not chunk:
            break
        body += chunk
    return method, path, body[:clen].decode("utf-8", "ignore")


def _send_http(conn, status, body, ctype="text/html; charset=utf-8", extra_hdr=""):
    data = body.encode("utf-8") if isinstance(body, str) else body
    hdr = (
        "HTTP/1.1 {}\r\n"
        "Content-Type: {}\r\n"
        "Content-Length: {}\r\n"
        "Cache-Control: no-store\r\n"
        "{}"
        "Connection: close\r\n\r\n"
    ).format(status, ctype, len(data), extra_hdr)
    conn.sendall(hdr.encode("ascii"))
    if data:
        conn.sendall(data)


def _draw_screen(D, pens, ip, aps, saved_ssid, note, is_ok=False):
    C_BG, C_PANEL, C_TEXT, C_MUTED, C_CYAN, C_GREEN, C_AMBER, C_RED = pens
    W, H = D.get_bounds()
    D.set_pen(C_BG)
    D.clear()

    # Header
    D.set_pen(C_PANEL)
    D.rectangle(0, 0, W, 46)
    D.set_pen(C_TEXT)
    D.text("POWERWALL 3", 8, 6, scale=2)
    D.set_pen(C_CYAN)
    D.text("Wi-Fi Setup AP", 8, 26, scale=2)
    D.set_pen(C_GREEN if is_ok else C_AMBER)
    lbl = "SAVED" if is_ok else "SETUP"
    D.text(lbl, W - 8 - D.measure_text(lbl, 2), 14, scale=2)

    # Step 1 card
    D.set_pen(C_PANEL)
    D.rectangle(10, 54, W - 20, 44)
    D.set_pen(C_CYAN)
    D.rectangle(10, 54, 4, 44)
    D.text("1. JOIN WI-FI ON PHONE (OPEN)", 20, 60, scale=1)
    D.set_pen(C_TEXT)
    D.text(AP_SSID, 20, 74, scale=2)

    # Step 2 card
    D.set_pen(C_PANEL)
    D.rectangle(10, 106, W - 20, 44)
    D.set_pen(C_AMBER)
    D.rectangle(10, 106, 4, 44)
    D.text("2. OPEN BROWSER (OR POPUP)", 20, 112, scale=1)
    D.set_pen(C_TEXT)
    D.text("http://" + ip, 20, 126, scale=2)

    # Scan summary + live note
    pw_cnt = sum(1 for s, _ in aps if s.startswith("TeslaPW_") or s.startswith("TEG-"))
    D.set_pen(C_MUTED)
    D.text("Scanned: {} APs ({} TeslaPW)".format(len(aps), pw_cnt), 12, 160, scale=1)
    cur_lbl = saved_ssid if (saved_ssid and saved_ssid != "TeslaPW_XXXXXX") else "(none)"
    D.text("Current: " + cur_lbl[:28], 12, 174, scale=1)
    D.set_pen(C_GREEN if is_ok else C_AMBER)
    D.text(note[:38], 12, 194, scale=1)

    # Footer
    D.set_pen(C_PANEL)
    D.rectangle(0, H - 20, W, 20)
    D.set_pen(C_MUTED)
    D.text("A:Rescan   B/Y:Cancel & Reboot", 12, H - 15, scale=1)
    D.update()


def _current_defaults(cfg):
    ex = _load_existing()
    ssid = ex.get("ssid", getattr(cfg, "WIFI_SSID", ""))
    if ssid == "TeslaPW_XXXXXX":
        ssid = ""
    pw = ex.get("pass", getattr(cfg, "WIFI_PASS", ""))
    if pw == "YOUR_PW3_WIFI_PASSWORD":
        pw = ""
    return {
        "ssid": ssid,
        "pass": pw,
        "pw_host": ex.get("pw_host", getattr(cfg, "PW_HOST", "192.168.91.1")),
        "pw_port": int(ex.get("pw_port", getattr(cfg, "PW_PORT", 443))),
        "poll_s": int(ex.get("poll_s", getattr(cfg, "POLL_INTERVAL_S", 30))),
        "exp_lim_w": int(ex.get("exp_lim_w", getattr(cfg, "EXPORT_LIMIT_W", 0))),
        "soc_app": bool(ex.get("soc_app", getattr(cfg, "SOC_SCALE_TO_APP", True))),
        "wdt": bool(ex.get("wdt", getattr(cfg, "ENABLE_WDT", False))),
    }


def _close_quiet(obj):
    if obj is not None:
        try:
            obj.close()
        except Exception:
            pass


def run_portal(D, btns, cfg, pens, wdt=None):
    """Run SoftAP captive portal until user saves, cancels (B/Y), or times out; then reboot."""
    t_ms, t_diff = time.ticks_ms, time.ticks_diff
    D.set_backlight(getattr(cfg, "BACKLIGHT_ACTIVE", 0.75))
    cur = _current_defaults(cfg)

    def _wait_release():
        t0 = t_ms()
        while any(b.value() == 0 for b in btns) and t_diff(t_ms(), t0) < 2000:
            if wdt:
                wdt()
            time.sleep_ms(30)

    _draw_screen(D, pens, "192.168.4.1", [], cur["ssid"], "Scanning Wi-Fi networks...")
    _wait_release()
    aps = _scan_aps(wdt)

    ap, ip = _start_ap(wdt)
    ip_bytes = bytes(int(x) for x in ip.split("."))
    print("Setup AP up: '{}' at http://{}".format(AP_SSID, ip))
    _draw_screen(D, pens, ip, aps, cur["ssid"], "Waiting for phone connection...")

    dns_sock = None
    http_sock = None
    poller = select.poll()
    try:
        try:
            dns_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            dns_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            dns_sock.bind(("0.0.0.0", 53))
            dns_sock.setblocking(False)
            poller.register(dns_sock, select.POLLIN)
        except Exception as e:
            print("DNS bind warning:", e)
            _close_quiet(dns_sock)
            dns_sock = None

        http_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        http_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        http_sock.bind(("0.0.0.0", 80))
        http_sock.listen(3)
        http_sock.setblocking(False)
        poller.register(http_sock, select.POLLIN)

        last_act = t_ms()
        saved = False
        y_rel = btns[3].value() == 1
        while not saved:
            if wdt:
                wdt()
            now = t_ms()
            if not y_rel and btns[3].value() == 1:
                y_rel = True
            # Cancel on B (1) or Y (3 after release)
            if btns[1].value() == 0 or (y_rel and btns[3].value() == 0):
                print("Setup cancelled by user.")
                _draw_screen(D, pens, ip, aps, cur["ssid"], "Cancelled. Rebooting...")
                break
            # Rescan on A (0)
            if btns[0].value() == 0:
                _draw_screen(D, pens, ip, aps, cur["ssid"], "Rescanning Wi-Fi...")
                try:
                    ap.active(False)
                except Exception:
                    pass
                time.sleep_ms(150)
                aps = _scan_aps(wdt)
                ap, ip = _start_ap(wdt)
                ip_bytes = bytes(int(x) for x in ip.split("."))
                _wait_release()
                last_act = t_ms()
                _draw_screen(D, pens, ip, aps, cur["ssid"], "Rescan complete. Waiting...")
                continue
            if t_diff(now, last_act) >= TIMEOUT_MS:
                print("Setup timed out; rebooting.")
                _draw_screen(D, pens, ip, aps, cur["ssid"], "Timed out. Rebooting...")
                break

            events = poller.poll(100)
            for sock, _ev in events:
                if dns_sock is not None and sock is dns_sock:
                    try:
                        pkt, addr = dns_sock.recvfrom(512)
                        rep = _build_dns_reply(pkt, ip_bytes)
                        if rep:
                            dns_sock.sendto(rep, addr)
                    except Exception:
                        pass
                elif sock is http_sock:
                    conn = None
                    try:
                        conn, addr = http_sock.accept()
                        last_act = t_ms()
                        method, path, body = _read_http_request(conn)
                        if not method:
                            continue
                        if method == "GET" and path == "/":
                            _draw_screen(D, pens, ip, aps, cur["ssid"], "Browser connected (" + str(addr[0]) + ")")
                            gc.collect()
                            _send_http(conn, "200 OK", _render_form(aps, cur))
                        elif method == "POST" and path == "/save":
                            form = _parse_form(body)
                            ssid = (form.get("ssid_manual") or form.get("ssid") or "").strip()
                            pw = form.get("pass", "")
                            if not pw and cur.get("pass"):
                                pw = cur["pass"]
                            if not ssid:
                                _send_http(conn, "200 OK", _render_form(aps, cur, "Please select or enter a Wi-Fi SSID."))
                            elif not pw:
                                _send_http(conn, "200 OK", _render_form(aps, cur, "Please enter the Wi-Fi / Powerwall password."))
                            else:
                                def _pint(k, d, lo, hi):
                                    try:
                                        return max(lo, min(hi, int(form.get(k, d))))
                                    except ValueError:
                                        return d
                                new_cfg = {
                                    "ssid": ssid,
                                    "pass": pw,
                                    "pw_host": (form.get("pw_host") or "192.168.91.1").strip() or "192.168.91.1",
                                    "pw_port": _pint("pw_port", 443, 1, 65535),
                                    "poll_s": _pint("poll_s", 30, 15, 300),
                                    "exp_lim_w": _pint("exp_lim_w", 0, 0, 100000),
                                    "soc_app": form.get("soc_app") == "1",
                                    "wdt": form.get("wdt") == "1",
                                }
                                save_settings(new_cfg)
                                _send_http(conn, "200 OK", _render_saved(ssid))
                                _draw_screen(D, pens, ip, aps, ssid, "Saved! Rebooting...", is_ok=True)
                                print("Saved wifi.json for SSID:", ssid)
                                saved = True
                        else:
                            # Captive portal redirect for OS probe URLs
                            _send_http(conn, "302 Found", "", extra_hdr="Location: http://{}/\r\n".format(ip))
                    except Exception as e:
                        print("HTTP error:", e)
                    finally:
                        _close_quiet(conn)
                        gc.collect()
    finally:
        _close_quiet(http_sock)
        _close_quiet(dns_sock)
        for _ in range(15):
            if wdt:
                wdt()
            time.sleep_ms(100)
        try:
            ap.active(False)
        except Exception:
            pass
        machine.reset()
