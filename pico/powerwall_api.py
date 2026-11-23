# pico/powerwall_api.py
# Ultra-lean Tesla Powerwall 3 TEDAPI HTTPS/1.1 + Protobuf client
# Tuned for Raspberry Pi Pico W (RP2040, 264 KB SRAM, MicroPython v1.25.0).
# Also runs on CPython 3 for desktop testing against tools/mock_powerwall.py.
#
# RP2040 Memory Strategy:
#   - Zero external libraries, zero custom C firmware, zero flash writes.
#   - Explicit gc.collect() before every TLS handshake.
#   - NEVER overlaps mbedTLS (~44 KB) with deflate.DeflateIO GZIP (~32 KB):
#     * Uncompressed responses: stream-scanned directly off TLS in 1 KB chunks (~2 KB RAM).
#     * Gzipped responses (~1.7-4 KB compressed): buffered into a tiny bytearray,
#       TLS+TCP sockets are CLOSED and gc.collect()ed FIRST, then decompressed
#       and stream-scanned in 512 B chunks.

import binascii
import gc
import io
import socket
import ssl

try:
    import deflate
except ImportError:
    deflate = None
    try:
        import zlib
    except ImportError:
        zlib = None


class PowerwallError(Exception):
    pass


class RateLimitError(PowerwallError):
    pass


class AuthError(PowerwallError):
    pass


# --- Minimal Protobuf Wire Encoder (TEDAPI /tedapi/v1) ---

def _pb_varint(value):
    out = bytearray()
    v = int(value)
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _pb_field_varint(field_num, value):
    return _pb_varint((field_num << 3) | 0) + _pb_varint(value)


def _pb_field_bytes(field_num, payload):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return _pb_varint((field_num << 3) | 2) + _pb_varint(len(payload)) + payload


def build_tedapi_query_request(
    din, query_text=b"", query_sig=b"", variables_json=b"{}", graphql_payload=b""
):
    """
    Build wire-exact TEDAPI Message / AuthEnvelope protobuf for POST /tedapi/v1:
      1 (MessageEnvelope):
        1: 1                         (deliveryChannel = LOCAL_HTTPS)
        2: { 3: 1 }                  (sender.local = INSTALLER)
        3: { 1: <din> }              (recipient.din)
        16 (GraphQLMessages):        (raw graphql_payload from export_tedapi_query.py,
                                      or encoded from query_text/query_sig for mock)
      2 (Tail / ExternalAuth):
        1: 1                         (value = 1 / EXTERNAL_AUTH_TYPE_PRESENCE)
    """
    if graphql_payload:
        gql_msg = graphql_payload
    else:
        query_inner = _pb_field_varint(1, 1) + _pb_field_bytes(2, query_text)
        vars_inner = _pb_field_bytes(1, variables_json)
        gql_send = (
            _pb_field_varint(1, 2)
            + _pb_field_bytes(2, query_inner)
            + _pb_field_bytes(3, query_sig)
            + _pb_field_bytes(4, vars_inner)
        )
        gql_msg = _pb_field_bytes(1, gql_send)

    sender = _pb_field_varint(3, 1)
    recipient = _pb_field_bytes(1, din)
    envelope = (
        _pb_field_varint(1, 1)
        + _pb_field_bytes(2, sender)
        + _pb_field_bytes(3, recipient)
        + _pb_field_bytes(16, gql_msg)
    )
    tail = _pb_field_varint(1, 1)
    return _pb_field_bytes(1, envelope) + _pb_field_bytes(2, tail)


# --- TLS & HTTP/1.1 Transport (MicroPython v1.25.0 RP2040 compatible) ---

def _is_ipv4(host):
    parts = host.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit():
            return False
    return True


def _wrap_tls(sock, host):
    """Wrap socket in TLS with CERT_NONE (Powerwall 3 uses a self-signed cert)."""
    sni = None if _is_ipv4(host) else host
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if hasattr(ctx, "check_hostname"):
        ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if sni:
        return ctx.wrap_socket(sock, server_hostname=sni)
    return ctx.wrap_socket(sock)


def _send_all(stream, data):
    mv = memoryview(data)
    total = len(data)
    sent = 0
    while sent < total:
        if hasattr(stream, "write"):
            n = stream.write(mv[sent:])
        else:
            n = stream.send(mv[sent:])
        if n is None or n <= 0:
            raise PowerwallError("Socket write failed")
        sent += n


def _read_some(stream, n):
    if hasattr(stream, "read"):
        return stream.read(n)
    return stream.recv(n)


def _read_line(stream, max_len=1024):
    if hasattr(stream, "readline"):
        line = stream.readline()
        return line if line is not None else b""
    buf = bytearray()
    while len(buf) < max_len:
        ch = _read_some(stream, 1)
        if not ch:
            break
        buf.extend(ch)
        if ch == b"\n":
            break
    return bytes(buf)


def _parse_date_hhmm(date_hdr, utc_offset_hours=0):
    """Extract HH:MM from HTTP Date header, e.g. 'Sun, 22 Nov 2026 19:45:12 GMT'."""
    if not date_hdr:
        return None
    for token in date_hdr.split():
        if len(token) == 8 and token[2] == ":" and token[5] == ":":
            try:
                hh = (int(token[0:2]) + int(utc_offset_hours)) % 24
                mm = int(token[3:5])
                return "{:02d}:{:02d}".format(hh, mm)
            except ValueError:
                return None
    return None


class _ChunkedReader:
    """Minimal HTTP/1.1 Transfer-Encoding: chunked reader."""

    def __init__(self, stream):
        self._s = stream
        self._rem = 0
        self._eof = False

    def read(self, n=1024):
        if self._eof:
            return b""
        while self._rem == 0:
            line = _read_line(self._s)
            if not line:
                self._eof = True
                return b""
            size_str = line.split(b";", 1)[0].strip()
            if not size_str:
                continue
            self._rem = int(size_str, 16)
            if self._rem == 0:
                self._eof = True
                return b""
        want = n if n < self._rem else self._rem
        chunk = _read_some(self._s, want)
        if not chunk:
            self._eof = True
            return b""
        self._rem -= len(chunk)
        if self._rem == 0:
            _read_line(self._s)
        return chunk


class _LengthReader:
    """Bounded reader for Content-Length (or read-to-EOF if length is None)."""

    def __init__(self, stream, length):
        self._s = stream
        self._rem = length

    def read(self, n=1024):
        if self._rem is not None:
            if self._rem <= 0:
                return b""
            want = n if n < self._rem else self._rem
            chunk = _read_some(self._s, want)
            if chunk:
                self._rem -= len(chunk)
            return chunk or b""
        return _read_some(self._s, n) or b""


# --- O(1)-RAM Streaming Telemetry Scanner ---

def _extract_number_after(buf, key_bytes, start=0):
    idx = buf.find(key_bytes, start)
    if idx < 0:
        return None
    p = idx + len(key_bytes)
    n = len(buf)
    while p < n and buf[p] in b" \t\r\n:":
        p += 1
    if p >= n:
        return None
    end = p
    while end < n and buf[end] in b"0123456789.-+eE":
        end += 1
    if end == p or end == n:
        return None
    try:
        return float(buf[p:end].decode("ascii"))
    except ValueError:
        return None


def _extract_bool_after(buf, key_bytes):
    idx = buf.find(key_bytes)
    if idx < 0:
        return None
    p = idx + len(key_bytes)
    n = len(buf)
    while p < n and buf[p] in b" \t\r\n:":
        p += 1
    if buf[p:p + 4] == b"true":
        return True
    if buf[p:p + 5] == b"false":
        return False
    return None


def _extract_string_after(buf, key_bytes, start=0):
    idx = buf.find(key_bytes, start)
    if idx < 0:
        return None
    p = idx + len(key_bytes)
    n = len(buf)
    while p < n and buf[p] in b" \t\r\n:":
        p += 1
    if p >= n or buf[p] != 0x22:  # '"'
        return None
    p += 1
    end = p
    while end < n:
        ch = buf[end]
        if ch == 0x5C:  # '\\'
            end += 2
            continue
        if ch == 0x22:  # '"'
            return buf[p:end].decode("utf-8", "ignore")
        end += 1
    return None


def _scan_stream_for_metrics(reader, metrics, chunk_size=1024, wdt_feed=None):
    """
    Scan a protobuf-wrapped TEDAPI JSON stream in small chunks with a 192-byte
    sliding window. Extracts only the required Powerwall 3 metrics in O(1) RAM.
    """
    window = b""
    meter_keys = (
        (b'"BATTERY"', "batt_w"),
        (b'"SOLAR"', "solar_w"),
        (b'"LOAD"', "load_w"),
        (b'"SITE"', "site_w"),
    )
    while True:
        if wdt_feed:
            wdt_feed()
        chunk = reader.read(chunk_size)
        if not chunk:
            if window:
                window += b" "
            else:
                break
        else:
            window += chunk

        if metrics["remaining_wh"] is None:
            v = _extract_number_after(window, b'"nominalEnergyRemainingWh"')
            if v is not None:
                metrics["remaining_wh"] = v

        if metrics["full_wh"] is None:
            v = _extract_number_after(window, b'"nominalFullPackEnergyWh"')
            if v is not None:
                metrics["full_wh"] = v

        if metrics["grid_ok"] is None:
            b = _extract_bool_after(window, b'"gridOK"')
            if b is not None:
                metrics["grid_ok"] = b

        for loc_tok, mkey in meter_keys:
            if metrics[mkey] is None:
                pos = window.find(loc_tok)
                if pos >= 0:
                    lo = window.rfind(b"{", 0, pos)
                    hi = window.find(b"}", pos)
                    if lo >= 0 and hi > pos:
                        v = _extract_number_after(
                            window[lo:hi + 1], b'"realPowerW"'
                        )
                        if v is not None:
                            metrics[mkey] = v

        if not chunk:
            break
        if len(window) > 192:
            window = window[-192:]


_STRING_LETTERS = ("A", "B", "C", "D", "E", "F")

_COMP_NUM_SIGNALS = {
    "HVP_PackTempMin": ("pack_temp_min", "min"),
    "HVP_PackTempMax": ("pack_temp_max", "max"),
    "HVP_ShuntTemperature": ("shunt_temp", "max"),
    "PCH_AmbientTemp": ("inv_ambient_temp", "set"),
    "PCH_FanSpeed_A": ("fan_rpm_a", "set"),
    "PCH_FanSpeed_B": ("fan_rpm_b", "set"),
    "PCH_FanDuty_A": ("fan_duty_a", "set"),
    "PCH_FanDuty_B": ("fan_duty_b", "set"),
    "BAGGR_NumBatteriesConnected": ("batt_connected", "set"),
    "BAGGR_NumBatteriesExpected": ("batt_expected", "set"),
    "PCH_AcVoltageAB": ("ac_v_ab", "set"),
    "PCH_AcVoltageAN": ("ac_v_an", "set"),
    "PCH_AcVoltageBN": ("ac_v_bn", "set"),
    "PCH_AcFrequency": ("ac_freq", "set"),
    "PCH_AcRealPowerAB": ("ac_power_w", "set"),
    "PCH_BatteryPower": ("pch_batt_w", "set"),
    "PCH_SlowPvPowerSum": ("slow_pv_sum_w", "set"),
}

_COMP_TEXT_SIGNALS = {
    "HVP_State": "hvp_state",
    "BAGGR_State": "baggr_state",
    "PCH_AcMode": "ac_mode",
    "PCH_State": "pch_state",
    "PWS_RSD_State": "rsd_state",
    "PWS_ProdSwitch_State": "prod_switch",
    "PWS_SelfTest": "self_test",
}


def _new_components_dict():
    return {
        "strings": [
            {"id": ch, "state": None, "v": None, "i": None, "w": 0.0}
            for ch in _STRING_LETTERS
        ],
        "strings_dc_w": 0.0,
        "slow_pv_sum_w": None,
        "pack_temp_min": None,
        "pack_temp_max": None,
        "shunt_temp": None,
        "inv_ambient_temp": None,
        "fan_rpm_a": None,
        "fan_rpm_b": None,
        "fan_duty_a": None,
        "fan_duty_b": None,
        "hvp_state": None,
        "baggr_state": None,
        "batt_connected": None,
        "batt_expected": None,
        "ac_v_ab": None,
        "ac_v_an": None,
        "ac_v_bn": None,
        "ac_freq": None,
        "ac_mode": None,
        "pch_state": None,
        "ac_power_w": None,
        "pch_batt_w": None,
        "rsd_state": None,
        "prod_switch": None,
        "self_test": None,
        "alerts": [],
        "_seen_signals": 0,
    }


def _apply_component_object(obj, comp):
    name = _extract_string_after(obj, b'"name"')
    if not name:
        return

    # PV strings A..F: PCH_PvVoltageA..F, PCH_PvCurrentA..F, PCH_PvState_A..F
    if name.startswith("PCH_Pv"):
        comp["_seen_signals"] += 1
        last = name[-1]
        if last in _STRING_LETTERS:
            srec = comp["strings"][ord(last) - 65]
            if name.startswith("PCH_PvVoltage"):
                v = _extract_number_after(obj, b'"value"')
                if v is not None:
                    srec["v"] = v if v > 0.0 else 0.0
                return
            if name.startswith("PCH_PvCurrent"):
                v = _extract_number_after(obj, b'"value"')
                if v is not None:
                    srec["i"] = v if v > 0.0 else 0.0
                return
            if name.startswith("PCH_PvState_"):
                tv = _extract_string_after(obj, b'"textValue"')
                if tv is not None:
                    srec["state"] = tv
                return

    num_spec = _COMP_NUM_SIGNALS.get(name)
    if num_spec is not None:
        comp["_seen_signals"] += 1
        dest_key, mode = num_spec
        v = _extract_number_after(obj, b'"value"')
        if v is not None:
            cur = comp[dest_key]
            if cur is None or mode == "set":
                comp[dest_key] = v
            elif mode == "min" and v < cur:
                comp[dest_key] = v
            elif mode == "max" and v > cur:
                comp[dest_key] = v
        return

    text_key = _COMP_TEXT_SIGNALS.get(name)
    if text_key is not None:
        comp["_seen_signals"] += 1
        tv = _extract_string_after(obj, b'"textValue"')
        if tv is not None and comp[text_key] is None:
            comp[text_key] = tv
        return

    # Leaf alert objects in activeAlerts look like {"name":"ALERT_NAME"}
    if b'"value"' not in obj and b'"timestamp"' not in obj and b'"signals"' not in obj:
        alerts = comp["alerts"]
        if len(alerts) < 10 and name not in alerts:
            alerts.append(name)


def _scan_stream_for_components(reader, comp, chunk_size=1024, wdt_feed=None):
    """
    O(1)-RAM streaming scanner for TEDAPI ComponentsQuery responses.
    Processes each leaf JSON object `{...}` once as its closing brace arrives.
    """
    window = b""
    while True:
        if wdt_feed:
            wdt_feed()
        chunk = reader.read(chunk_size)
        if chunk:
            window += chunk

        pos = 0
        while True:
            hi = window.find(b"}", pos)
            if hi < 0:
                break
            lo = window.rfind(b"{", pos, hi)
            if lo >= 0:
                _apply_component_object(window[lo:hi + 1], comp)
            pos = hi + 1

        if pos > 0:
            window = window[pos:]
        if not chunk:
            break
        if len(window) > 512:
            last_open = window.rfind(b"{")
            if last_open >= 0 and len(window) - last_open <= 512:
                window = window[last_open:]
            else:
                window = window[-512:]


def _scan_gzip_bytes(gz_bytes, target, scanner_fn=_scan_stream_for_metrics, wdt_feed=None):
    """Decompress & stream-scan a gzipped TEDAPI response AFTER TLS is closed."""
    if deflate is not None:
        bio = io.BytesIO(gz_bytes)
        dstream = deflate.DeflateIO(bio, deflate.GZIP)
        try:
            scanner_fn(dstream, target, chunk_size=512, wdt_feed=wdt_feed)
        finally:
            try:
                dstream.close()
            except Exception:
                pass
    elif zlib is not None:
        raw = zlib.decompress(gz_bytes, 16 + zlib.MAX_WBITS)
        scanner_fn(io.BytesIO(raw), target, chunk_size=1024, wdt_feed=wdt_feed)
    else:
        raise PowerwallError("Gzip response but no deflate module")


def scale_soc_to_app(raw_soc):
    """Convert Powerwall 3 raw physical SoC to Tesla App user-facing SoC."""
    scaled = (raw_soc - 5.0) / 0.95
    if scaled < 0.0:
        return 0.0
    if scaled > 100.0:
        return 100.0
    return scaled


# --- Powerwall 3 TEDAPI Client ---

class Powerwall3Client:
    def __init__(
        self,
        host="192.168.91.1",
        port=443,
        gw_password="",
        timeout_s=6,
        soc_scale_to_app=True,
        utc_offset_hours=0,
        graphql_payload=b"",
        query_text=b"",
        query_sig=b"",
        query_vars=b"{}",
        is_real_signed_query=False,
        components_payload=b"",
        components_query_text=b"",
        components_query_sig=b"",
        components_query_vars=b"{}",
    ):
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.soc_scale_to_app = soc_scale_to_app
        self.utc_offset_hours = utc_offset_hours
        self.graphql_payload = graphql_payload
        self.query_text = query_text
        self.query_sig = query_sig
        self.query_vars = query_vars
        self.is_real_signed_query = is_real_signed_query
        self.components_payload = components_payload
        self.components_query_text = components_query_text
        self.components_query_sig = components_query_sig
        self.components_query_vars = components_query_vars

        self._din = None
        raw_cred = ("Tesla_Energy_Device:" + gw_password).encode("utf-8")
        b64 = binascii.b2a_base64(raw_cred).strip().decode("ascii")
        self._basic_hdr = "Basic " + b64

    def _https_request(
        self,
        method,
        path,
        content_type=None,
        body=b"",
        metrics=None,
        scanner_fn=None,
        wdt_feed=None,
    ):
        """
        Execute one HTTPS/1.1 request with strict socket/TLS cleanup.
        If metrics target dict is provided:
          - Uncompressed response is stream-scanned inline via scanner_fn.
          - Gzipped response is buffered (~2-4 KB), TLS is closed & GC'd FIRST,
            then decompressed in a separate peak-memory phase for RP2040 safety.
        """
        gc.collect()
        if wdt_feed:
            wdt_feed()

        if scanner_fn is None:
            scanner_fn = _scan_stream_for_metrics

        raw_sock = None
        tls_sock = None
        resp_headers = {}
        gz_buf = None
        small_body = b""
        phase = "connect"

        try:
            addr = socket.getaddrinfo(self.host, self.port)[0][-1]
            raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw_sock.settimeout(self.timeout_s)
            raw_sock.connect(addr)
            if wdt_feed:
                wdt_feed()

            phase = "tls"
            tls_sock = _wrap_tls(raw_sock, self.host)
            if wdt_feed:
                wdt_feed()

            phase = "send"
            req_lines = [
                "{} {} HTTP/1.1".format(method, path),
                "Host: {}".format(self.host),
                "Authorization: {}".format(self._basic_hdr),
                "Accept: */*",
                "Accept-Encoding: identity",
                "Connection: close",
            ]
            if content_type:
                req_lines.append("Content-Type: {}".format(content_type))
            if body:
                req_lines.append("Content-Length: {}".format(len(body)))
            req_head = ("\r\n".join(req_lines) + "\r\n\r\n").encode("ascii")

            _send_all(tls_sock, req_head)
            if body:
                _send_all(tls_sock, body)
            if wdt_feed:
                wdt_feed()

            phase = "hdr"
            status_line = _read_line(tls_sock)
            if not status_line:
                raise PowerwallError("Empty HTTP response")
            parts = status_line.split(None, 2)
            if len(parts) < 2:
                raise PowerwallError("Bad HTTP status")
            status = int(parts[1])

            while True:
                line = _read_line(tls_sock)
                if not line or line in (b"\r\n", b"\n"):
                    break
                if b":" in line:
                    k, v = line.split(b":", 1)
                    resp_headers[k.strip().lower().decode("ascii", "ignore")] = (
                        v.strip().decode("ascii", "ignore")
                    )

            if status in (429, 503):
                raise RateLimitError("HTTP {}".format(status))
            if status in (401, 403):
                raise AuthError("HTTP {}".format(status))
            if status != 200:
                raise PowerwallError("HTTP {}".format(status))

            phase = "body"
            te = resp_headers.get("transfer-encoding", "").lower()
            ce = resp_headers.get("content-encoding", "").lower()
            cl_str = resp_headers.get("content-length")
            cl = int(cl_str) if cl_str and cl_str.isdigit() else None

            reader = _ChunkedReader(tls_sock) if "chunked" in te else _LengthReader(tls_sock, cl)

            if metrics is not None:
                if "gzip" in ce:
                    # Buffer tiny compressed gzip body (~1.7-6 KB); decompress after TLS closes
                    gz_buf = bytearray()
                    while len(gz_buf) < 24576:
                        if wdt_feed:
                            wdt_feed()
                        try:
                            c = reader.read(512)
                        except OSError:
                            if cl is None and len(gz_buf) > 0:
                                break
                            raise
                        if not c:
                            break
                        gz_buf.extend(c)
                else:
                    try:
                        scanner_fn(reader, metrics, chunk_size=1024, wdt_feed=wdt_feed)
                    except OSError:
                        has_partial = (
                            cl is None
                            and (
                                (metrics.get("remaining_wh") is not None and metrics.get("full_wh"))
                                or metrics.get("_seen_signals", 0) > 0
                            )
                        )
                        if not has_partial:
                            raise
            else:
                buf = bytearray()
                while len(buf) < 512:
                    try:
                        c = reader.read(256)
                    except OSError:
                        if cl is None and len(buf) > 0:
                            break
                        raise
                    if not c:
                        break
                    buf.extend(c)
                small_body = bytes(buf)
        except OSError as e:
            raise PowerwallError("{}@{}: {}".format(phase, path, e))
        finally:
            if tls_sock is not None:
                try:
                    tls_sock.close()
                except Exception:
                    pass
            if raw_sock is not None:
                try:
                    raw_sock.close()
                except Exception:
                    pass
            gc.collect()

        # Phase 2 (TLS RAM already freed!): decompress gzip if gateway compressed it
        if gz_buf is not None:
            _scan_gzip_bytes(bytes(gz_buf), metrics, scanner_fn=scanner_fn, wdt_feed=wdt_feed)
            gz_buf = None
            gc.collect()

        return resp_headers, small_body

    def _ensure_din(self, wdt_feed=None):
        if self._din:
            return self._din
        _, body = self._https_request("GET", "/tedapi/din", wdt_feed=wdt_feed)
        din = body.decode("utf-8", "ignore").strip()
        if not din:
            raise PowerwallError("Empty DIN from /tedapi/din")
        self._din = din
        return din

    def poll(self, wdt_feed=None):
        if self.host == "192.168.91.1" and not self.is_real_signed_query:
            raise PowerwallError("Run export_tedapi_query.py first")

        din = self._ensure_din(wdt_feed=wdt_feed)
        req_pb = build_tedapi_query_request(
            din,
            query_text=self.query_text,
            query_sig=self.query_sig,
            variables_json=self.query_vars,
            graphql_payload=self.graphql_payload,
        )
        metrics = {
            "remaining_wh": None,
            "full_wh": None,
            "soc_raw": None,
            "soc_pct": None,
            "batt_w": None,
            "solar_w": None,
            "load_w": None,
            "site_w": None,
            "grid_ok": None,
            "time_str": None,
        }
        resp_hdrs, _ = self._https_request(
            "POST",
            "/tedapi/v1",
            content_type="application/octet-stream",
            body=req_pb,
            metrics=metrics,
            scanner_fn=_scan_stream_for_metrics,
            wdt_feed=wdt_feed,
        )
        del req_pb
        gc.collect()

        rem = metrics["remaining_wh"]
        full = metrics["full_wh"]
        if rem is None or not full:
            raise PowerwallError("Missing Wh fields in TEDAPI response")

        raw_soc = (rem / full) * 100.0
        metrics["soc_raw"] = raw_soc
        metrics["soc_pct"] = scale_soc_to_app(raw_soc) if self.soc_scale_to_app else raw_soc
        metrics["time_str"] = _parse_date_hhmm(
            resp_hdrs.get("date"), self.utc_offset_hours
        )
        return metrics

    def poll_components(self, wdt_feed=None):
        """
        Poll Powerwall 3 ComponentsQuery (solar strings A-F, pack/inverter temps,
        fan RPM/duty, AC grid voltage/frequency, and PWS/HVP/BAGGR states).
        Called on-demand only when displaying a detail view.
        """
        if self.host == "192.168.91.1" and not self.components_payload:
            raise PowerwallError("Re-run export_tedapi_query.py")

        din = self._ensure_din(wdt_feed=wdt_feed)
        req_pb = build_tedapi_query_request(
            din,
            query_text=self.components_query_text,
            query_sig=self.components_query_sig,
            variables_json=self.components_query_vars,
            graphql_payload=self.components_payload,
        )
        comp = _new_components_dict()
        resp_hdrs, _ = self._https_request(
            "POST",
            "/tedapi/v1",
            content_type="application/octet-stream",
            body=req_pb,
            metrics=comp,
            scanner_fn=_scan_stream_for_components,
            wdt_feed=wdt_feed,
        )
        del req_pb
        gc.collect()

        if comp["_seen_signals"] == 0:
            raise PowerwallError("No component signals in response")

        total_dc = 0.0
        for srec in comp["strings"]:
            v = srec["v"] or 0.0
            i = srec["i"] or 0.0
            w = v * i
            srec["w"] = w
            total_dc += w
        comp["strings_dc_w"] = total_dc
        comp["time_str"] = _parse_date_hhmm(
            resp_hdrs.get("date"), self.utc_offset_hours
        )
        return comp
