# pico/powerwall_api.py
# Ultra-lean Tesla Powerwall 3 TEDAPI HTTPS/1.1 + Protobuf client for RP2040 (264 KB SRAM).
# Streams signed payloads directly from flash (tedapi.bin -> 0 B payload RAM),
# never overlaps mbedTLS (~44 KB) with deflate.DeflateIO (~32 KB), and repacks
# results after GC so nothing pins mid-heap.

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


def _pb_varint(v):
    out = bytearray()
    v = int(v)
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _pb_int(f, v):
    return _pb_varint((f << 3) | 0) + _pb_varint(v)


def _pb_bytes(f, p):
    if isinstance(p, str):
        p = p.encode("utf-8")
    return _pb_varint((f << 3) | 2) + _pb_varint(len(p)) + p


def build_tedapi_query_parts(
    din, query_text=b"", query_sig=b"", variables_json=b"{}",
    graphql_payload=b"", payload_slice=None,
):
    """Return tuple of bytes / (file_path, offset, length) slices for zero-copy POST."""
    if payload_slice is not None:
        gql_part = payload_slice
        gql_len = payload_slice[2]
    elif graphql_payload:
        gql_part = graphql_payload
        gql_len = len(graphql_payload)
    else:
        qi = _pb_int(1, 1) + _pb_bytes(2, query_text)
        vi = _pb_bytes(1, variables_json)
        gs = _pb_int(1, 2) + _pb_bytes(2, qi) + _pb_bytes(3, query_sig) + _pb_bytes(4, vi)
        gql_part = _pb_bytes(1, gs)
        gql_len = len(gql_part)

    env_pre = (
        _pb_int(1, 1)
        + _pb_bytes(2, _pb_int(3, 1))
        + _pb_bytes(3, _pb_bytes(1, din))
        + _pb_varint((16 << 3) | 2)
        + _pb_varint(gql_len)
    )
    head = _pb_varint((1 << 3) | 2) + _pb_varint(len(env_pre) + gql_len)
    tail = _pb_bytes(2, _pb_int(1, 1))
    return (head, env_pre, gql_part, tail)


def build_tedapi_query_request(
    din, query_text=b"", query_sig=b"", variables_json=b"{}", graphql_payload=b""
):
    return b"".join(
        build_tedapi_query_parts(
            din, query_text, query_sig, variables_json, graphql_payload
        )
    )


def _is_ipv4(host):
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def _wrap_tls(sock, host):
    sni = None if _is_ipv4(host) else host
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if hasattr(ctx, "check_hostname"):
        ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx.wrap_socket(sock, server_hostname=sni) if sni else ctx.wrap_socket(sock)


def _send_all(stream, data):
    mv = memoryview(data)
    tot = len(data)
    sent = 0
    w = stream.write if hasattr(stream, "write") else stream.send
    while sent < tot:
        n = w(mv[sent:])
        if not n or n <= 0:
            raise PowerwallError("Socket write failed")
        sent += n


def _send_file_slice(stream, path, offset, length):
    buf = bytearray(512)
    mv = memoryview(buf)
    rem = length
    with open(path, "rb") as f:
        f.seek(offset)
        while rem > 0:
            want = 512 if rem > 512 else rem
            n = f.readinto(mv[:want])
            if not n:
                raise PowerwallError("Short tedapi.bin read")
            _send_all(stream, mv[:n])
            rem -= n


def _read_some(stream, n):
    return stream.read(n) if hasattr(stream, "read") else stream.recv(n)


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
    if not date_hdr:
        return None
    for tok in date_hdr.split():
        if len(tok) == 8 and tok[2] == ":" and tok[5] == ":":
            try:
                hh = (int(tok[0:2]) + int(utc_offset_hours)) % 24
                return "{:02d}:{:02d}".format(hh, int(tok[3:5]))
            except ValueError:
                return None
    return None


def _parse_iso_hhmm(iso_str, utc_offset_hours=0):
    if not iso_str:
        return None
    ti = iso_str.find("T")
    if ti < 0:
        ti = iso_str.find(" ")
    if ti < 0 or len(iso_str) < ti + 6 or iso_str[ti + 3] != ":":
        return None
    try:
        hh = int(iso_str[ti + 1:ti + 3])
        mm = int(iso_str[ti + 4:ti + 6])
        if iso_str.endswith("Z"):
            hh = (hh + int(utc_offset_hours)) % 24
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return "{:02d}:{:02d}".format(hh, mm)
    except ValueError:
        return None
    return None


class _ChunkedReader:
    def __init__(self, s):
        self._s = s
        self._rem = 0
        self._eof = False

    def read(self, n=512):
        if self._eof:
            return b""
        while self._rem == 0:
            line = _read_line(self._s)
            if not line:
                self._eof = True
                return b""
            sz = line.split(b";", 1)[0].strip()
            if not sz:
                continue
            self._rem = int(sz, 16)
            if self._rem == 0:
                self._eof = True
                return b""
        chunk = _read_some(self._s, n if n < self._rem else self._rem)
        if not chunk:
            self._eof = True
            return b""
        self._rem -= len(chunk)
        if self._rem == 0:
            _read_line(self._s)
        return chunk


class _LengthReader:
    def __init__(self, s, length):
        self._s = s
        self._rem = length

    def read(self, n=512):
        if self._rem is not None:
            if self._rem <= 0:
                return b""
            chunk = _read_some(self._s, n if n < self._rem else self._rem)
            if chunk:
                self._rem -= len(chunk)
            return chunk or b""
        return _read_some(self._s, n) or b""


def _extract_num(buf, key, start=0):
    i = buf.find(key, start)
    if i < 0:
        return None
    p = i + len(key)
    n = len(buf)
    while p < n and buf[p] in b" \t\r\n:":
        p += 1
    end = p
    while end < n and buf[end] in b"0123456789.-+eE":
        end += 1
    if end == p or end == n:
        return None
    try:
        return float(buf[p:end].decode("ascii"))
    except ValueError:
        return None


def _extract_bool(buf, key):
    i = buf.find(key)
    if i < 0:
        return None
    p = i + len(key)
    n = len(buf)
    while p < n and buf[p] in b" \t\r\n:":
        p += 1
    if buf[p:p + 4] == b"true":
        return True
    if buf[p:p + 5] == b"false":
        return False
    return None


def _extract_str(buf, key, start=0):
    i = buf.find(key, start)
    if i < 0:
        return None
    p = i + len(key)
    n = len(buf)
    while p < n and buf[p] in b" \t\r\n:":
        p += 1
    if p >= n or buf[p] != 0x22:
        return None
    p += 1
    e = p
    while e < n:
        c = buf[e]
        if c == 0x5C:
            e += 2
            continue
        if c == 0x22:
            return buf[p:e].decode("utf-8", "ignore")
        e += 1
    return None


def _scan_stream_for_metrics(reader, m, chunk_size=512, wdt_feed=None):
    w = b""
    meters = (
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
            if w:
                w += b" "
            else:
                break
        else:
            w += chunk

        if m["remaining_wh"] is None:
            v = _extract_num(w, b'"nominalEnergyRemainingWh"')
            if v is not None:
                m["remaining_wh"] = v
        if m["full_wh"] is None:
            v = _extract_num(w, b'"nominalFullPackEnergyWh"')
            if v is not None:
                m["full_wh"] = v
        if m["grid_ok"] is None:
            b = _extract_bool(w, b'"gridOK"')
            if b is not None:
                m["grid_ok"] = b
        if m.get("site_shutdown") is None:
            b = _extract_bool(w, b'"isShutDown"')
            if b is not None:
                m["site_shutdown"] = b
        if m.get("sys_time") is None:
            sp = w.find(b'"system"')
            if sp >= 0:
                ts = _extract_str(w, b'"time"', sp)
                if ts:
                    m["sys_time"] = ts
        for ktok, dkey in ((b'"defaultControl"', "exp_lim_default_w"), (b'"activeControls"', "exp_lim_active_w")):
            if m.get(dkey) is None:
                p = w.find(ktok)
                if p >= 0:
                    hi = w.find(b"}", p)
                    if hi > p:
                        v = _extract_num(w[p:hi + 1], b'"opModExpLimW"')
                        if v is not None:
                            m[dkey] = v
        for tok, mk in meters:
            if m[mk] is None:
                p = w.find(tok)
                if p >= 0:
                    lo = w.rfind(b"{", 0, p)
                    hi = w.find(b"}", p)
                    if lo >= 0 and hi > p:
                        v = _extract_num(w[lo:hi + 1], b'"realPowerW"')
                        if v is not None:
                            m[mk] = v
        if not chunk:
            break
        if len(w) > 320:
            w = w[-320:]


_LETTERS = ("A", "B", "C", "D", "E", "F")
_INTERN = {
    "Pv_Active": "Pv_Active",
    "Pv_Standby": "Pv_Standby",
    "Pv_Active_Parallel": "Pv_Active_Parallel",
}
_COMP_NUM = {
    "HVP_PackTempMin": ("pack_temp_min", 1),   # 1=min
    "HVP_PackTempMax": ("pack_temp_max", 2),   # 2=max
    "HVP_ShuntTemperature": ("shunt_temp", 2),
    "BMS_LOG_tempOutOfBounds": ("temp_oob", 3),        # 3=sum
    "BMS_LOG_tempOutOfBoundsCharge": ("temp_oob_chg", 3),
    "PCH_AmbientTemp": ("inv_ambient_temp", 0),        # 0=set
    "PCH_FanSpeed_A": ("fan_rpm_a", 0),
    "PCH_FanSpeed_B": ("fan_rpm_b", 0),
    "PCH_FanDuty_A": ("fan_duty_a", 0),
    "PCH_FanDuty_B": ("fan_duty_b", 0),
    "PCH_AcVoltageAB": ("ac_v_ab", 0),
    "PCH_AcVoltageAN": ("ac_v_an", 0),
    "PCH_AcVoltageBN": ("ac_v_bn", 0),
    "PCH_AcFrequency": ("ac_freq", 0),
    "PCH_AcRealPowerAB": ("ac_power_w", 0),
    "PCH_BatteryPower": ("pch_batt_w", 0),
    "PCH_SlowPvPowerSum": ("slow_pv_sum_w", 0),
}
_COMP_TXT = {
    "HVP_State": "hvp_state",
    "PCH_AcMode": "ac_mode",
    "PCH_State": "pch_state",
}


def _new_components_dict():
    d = {
        "strings": [{"id": c, "state": None, "v": None, "i": None, "w": 0.0} for c in _LETTERS],
        "strings_dc_w": 0.0,
        "_seen_signals": 0,
    }
    for k, _ in _COMP_NUM.values():
        d[k] = None
    for k in _COMP_TXT.values():
        d[k] = None
    return d


def _repack(v):
    """Clone scalars/containers into the bottom of heap after TLS/DeflateIO GC."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, float):
        return v + 0.0
    if isinstance(v, str):
        return _INTERN.get(v) or v.encode("utf-8").decode("utf-8")
    if isinstance(v, list):
        return [_repack(x) for x in v]
    if isinstance(v, dict):
        return {k: _repack(val) for k, val in v.items() if k != "_seen_signals"}
    return v


def _apply_comp_obj(obj, comp):
    name = _extract_str(obj, b'"name"')
    if not name:
        return
    if name.startswith("PCH_Pv"):
        comp["_seen_signals"] += 1
        ch = name[-1]
        if ch in _LETTERS:
            sr = comp["strings"][ord(ch) - 65]
            if name.startswith("PCH_PvVoltage"):
                v = _extract_num(obj, b'"value"')
                if v is not None:
                    sr["v"] = v if v > 0.0 else 0.0
            elif name.startswith("PCH_PvCurrent"):
                v = _extract_num(obj, b'"value"')
                if v is not None:
                    sr["i"] = v if v > 0.0 else 0.0
            elif name.startswith("PCH_PvState_"):
                tv = _extract_str(obj, b'"textValue"')
                if tv is not None:
                    sr["state"] = _INTERN.get(tv, tv)
        return
    ns = _COMP_NUM.get(name)
    if ns is not None:
        comp["_seen_signals"] += 1
        dk, mode = ns
        v = _extract_num(obj, b'"value"')
        if v is not None:
            cur = comp[dk]
            if cur is None or mode == 0:
                comp[dk] = v
            elif mode == 1 and v < cur:
                comp[dk] = v
            elif mode == 2 and v > cur:
                comp[dk] = v
            elif mode == 3:
                comp[dk] = cur + v
        return
    tk = _COMP_TXT.get(name)
    if tk is not None:
        comp["_seen_signals"] += 1
        tv = _extract_str(obj, b'"textValue"')
        if tv is not None and comp[tk] is None:
            comp[tk] = tv


def _scan_stream_for_components(reader, comp, chunk_size=512, wdt_feed=None):
    w = b""
    while True:
        if wdt_feed:
            wdt_feed()
        chunk = reader.read(chunk_size)
        if chunk:
            w += chunk
        pos = 0
        while True:
            hi = w.find(b"}", pos)
            if hi < 0:
                break
            lo = w.rfind(b"{", pos, hi)
            if lo >= 0:
                _apply_comp_obj(w[lo:hi + 1], comp)
            pos = hi + 1
        if pos > 0:
            w = w[pos:]
        if not chunk:
            break
        if len(w) > 384:
            lo = w.rfind(b"{")
            w = w[lo:] if (lo >= 0 and len(w) - lo <= 384) else w[-384:]


def _scan_gzip_bytes(gz_buf, target, scanner_fn=_scan_stream_for_metrics, wdt_feed=None):
    if deflate is not None:
        bio = io.BytesIO(gz_buf)
        ds = deflate.DeflateIO(bio, deflate.GZIP)
        try:
            scanner_fn(ds, target, chunk_size=512, wdt_feed=wdt_feed)
        finally:
            try:
                ds.close()
            except Exception:
                pass
            ds = None
            bio = None
    elif zlib is not None:
        raw = zlib.decompress(bytes(gz_buf), 16 + zlib.MAX_WBITS)
        scanner_fn(io.BytesIO(raw), target, chunk_size=1024, wdt_feed=wdt_feed)
    else:
        raise PowerwallError("No deflate module")


def scale_soc_to_app(raw_soc):
    s = (raw_soc - 5.0) / 0.95
    return 0.0 if s < 0.0 else (100.0 if s > 100.0 else s)


_KEEP_HDRS = ("date", "content-length", "content-encoding", "transfer-encoding")


class Powerwall3Client:
    def __init__(
        self,
        host="192.168.91.1",
        port=443,
        gw_password="",
        timeout_s=12,
        soc_scale_to_app=True,
        utc_offset_hours=0,
        payload_file="",
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
        self._gql_slice = None
        self._comp_slice = None
        self._load_payload_index(payload_file)

        self._din = None
        cred = ("Tesla_Energy_Device:" + gw_password).encode("utf-8")
        self._basic_hdr = "Basic " + binascii.b2a_base64(cred).strip().decode("ascii")

    def _load_payload_index(self, path):
        if not path:
            return
        try:
            with open(path, "rb") as f:
                hdr = f.read(4)
            if len(hdr) == 4:
                gl = (hdr[0] << 8) | hdr[1]
                cl = (hdr[2] << 8) | hdr[3]
                if gl > 0:
                    self._gql_slice = (path, 4, gl)
                if cl > 0:
                    self._comp_slice = (path, 4 + gl, cl)
        except OSError:
            pass

    def _https_request(
        self, method, path, content_type=None, body_parts=None,
        metrics=None, scanner_fn=None, wdt_feed=None,
    ):
        gc.collect()
        if wdt_feed:
            wdt_feed()
        if scanner_fn is None:
            scanner_fn = _scan_stream_for_metrics

        raw_sock = None
        tls_sock = None
        reader = None
        hdrs = {}
        gz_buf = None
        small_body = b""
        phase = "connect"

        body_len = 0
        if body_parts:
            for p in body_parts:
                body_len += p[2] if isinstance(p, tuple) else len(p)

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
            rl = [
                "{} {} HTTP/1.1".format(method, path),
                "Host: " + self.host,
                "Authorization: " + self._basic_hdr,
                "Accept: */*",
                "Accept-Encoding: identity",
                "Connection: close",
            ]
            if content_type:
                rl.append("Content-Type: " + content_type)
            if body_len:
                rl.append("Content-Length: {}".format(body_len))
            _send_all(tls_sock, ("\r\n".join(rl) + "\r\n\r\n").encode("ascii"))
            rl = None
            if body_parts:
                for p in body_parts:
                    if isinstance(p, tuple):
                        _send_file_slice(tls_sock, p[0], p[1], p[2])
                    elif p:
                        _send_all(tls_sock, p)
            if wdt_feed:
                wdt_feed()

            phase = "hdr"
            sline = _read_line(tls_sock)
            if not sline:
                raise PowerwallError("Empty HTTP response")
            sp = sline.split(None, 2)
            if len(sp) < 2:
                raise PowerwallError("Bad HTTP status")
            status = int(sp[1])

            while True:
                line = _read_line(tls_sock)
                if not line or line in (b"\r\n", b"\n"):
                    break
                if b":" in line:
                    k, v = line.split(b":", 1)
                    ks = k.strip().lower().decode("ascii", "ignore")
                    if ks in _KEEP_HDRS:
                        hdrs[ks] = v.strip().decode("ascii", "ignore")

            if status in (429, 503):
                raise RateLimitError("HTTP {}".format(status))
            if status in (401, 403):
                raise AuthError("HTTP {}".format(status))
            if status != 200:
                raise PowerwallError("HTTP {}".format(status))

            phase = "body"
            te = hdrs.get("transfer-encoding", "").lower()
            ce = hdrs.get("content-encoding", "").lower()
            cls = hdrs.get("content-length")
            cl = int(cls) if cls and cls.isdigit() else None
            reader = _ChunkedReader(tls_sock) if "chunked" in te else _LengthReader(tls_sock, cl)

            if metrics is not None:
                if "gzip" in ce:
                    if cl is not None and 0 < cl <= 24576:
                        gz_buf = bytearray(cl)
                        mv = memoryview(gz_buf)
                        got = 0
                        while got < cl:
                            if wdt_feed:
                                wdt_feed()
                            c = reader.read(512 if (cl - got) > 512 else (cl - got))
                            if not c:
                                break
                            n = len(c)
                            mv[got:got + n] = c
                            got += n
                        mv = None
                        if got < cl:
                            gz_buf = gz_buf[:got]
                    else:
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
                        scanner_fn(reader, metrics, chunk_size=512, wdt_feed=wdt_feed)
                    except OSError:
                        ok = cl is None and (
                            (metrics.get("remaining_wh") is not None and metrics.get("full_wh"))
                            or metrics.get("_seen_signals", 0) > 0
                        )
                        if not ok:
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
        except (OSError, MemoryError) as e:
            raise PowerwallError("{}@{}: {}".format(phase, path, e))
        finally:
            reader = None
            if tls_sock is not None:
                try:
                    tls_sock.close()
                except Exception:
                    pass
                tls_sock = None
            if raw_sock is not None:
                try:
                    raw_sock.close()
                except Exception:
                    pass
                raw_sock = None
            gc.collect()

        if gz_buf is not None:
            phase = "gzip"
            try:
                _scan_gzip_bytes(gz_buf, metrics, scanner_fn=scanner_fn, wdt_feed=wdt_feed)
            except (OSError, MemoryError) as e:
                raise PowerwallError("{}@{}: {}".format(phase, path, e))
            finally:
                gz_buf = None
                gc.collect()

        return hdrs, small_body

    def _ensure_din(self, wdt_feed=None):
        if self._din:
            return self._din
        _, body = self._https_request("GET", "/tedapi/din", wdt_feed=wdt_feed)
        din = body.decode("utf-8", "ignore").strip()
        if not din:
            raise PowerwallError("Empty DIN")
        self._din = din
        return din

    def poll(self, wdt_feed=None):
        if self.host == "192.168.91.1" and not self.is_real_signed_query:
            raise PowerwallError("Run export_tedapi_query.py")
        if self.host == "192.168.91.1" and not (self._gql_slice or self.graphql_payload):
            raise PowerwallError("Upload tedapi.bin")

        din = self._ensure_din(wdt_feed=wdt_feed)
        parts = build_tedapi_query_parts(
            din, self.query_text, self.query_sig, self.query_vars,
            self.graphql_payload, self._gql_slice,
        )
        m = {
            "remaining_wh": None, "full_wh": None, "batt_w": None, "solar_w": None,
            "load_w": None, "site_w": None, "grid_ok": None, "site_shutdown": None,
            "exp_lim_default_w": None, "exp_lim_active_w": None, "sys_time": None,
        }
        hdrs, _ = self._https_request(
            "POST", "/tedapi/v1", "application/octet-stream",
            body_parts=parts, metrics=m, scanner_fn=_scan_stream_for_metrics, wdt_feed=wdt_feed,
        )
        parts = None
        rem = m["remaining_wh"]
        full = m["full_wh"]
        if rem is None or not full:
            m = None
            gc.collect()
            raise PowerwallError("Missing Wh fields")

        raw_soc = (rem / full) * 100.0
        exp_lim = m["exp_lim_active_w"]
        if exp_lim is None:
            exp_lim = m["exp_lim_default_w"]
        m["soc_raw"] = raw_soc
        m["soc_pct"] = scale_soc_to_app(raw_soc) if self.soc_scale_to_app else raw_soc
        m["exp_lim_w"] = exp_lim
        m["time_str"] = (
            _parse_iso_hhmm(m["sys_time"], self.utc_offset_hours)
            or _parse_date_hhmm(hdrs.get("date"), self.utc_offset_hours)
        )
        hdrs = None
        gc.collect()
        out = _repack(m)
        m = None
        gc.collect()
        return out

    def poll_components(self, wdt_feed=None):
        if self.host == "192.168.91.1" and not (self._comp_slice or self.components_payload):
            raise PowerwallError("Upload tedapi.bin")

        din = self._ensure_din(wdt_feed=wdt_feed)
        parts = build_tedapi_query_parts(
            din, self.components_query_text, self.components_query_sig,
            self.components_query_vars, self.components_payload, self._comp_slice,
        )
        tmp = _new_components_dict()
        hdrs, _ = self._https_request(
            "POST", "/tedapi/v1", "application/octet-stream",
            body_parts=parts, metrics=tmp, scanner_fn=_scan_stream_for_components, wdt_feed=wdt_feed,
        )
        parts = None
        if tmp["_seen_signals"] == 0:
            tmp = None
            gc.collect()
            raise PowerwallError("No component signals")

        dc = 0.0
        for sr in tmp["strings"]:
            w = (sr["v"] or 0.0) * (sr["i"] or 0.0)
            sr["w"] = w
            dc += w
        tmp["strings_dc_w"] = dc
        tmp["time_str"] = _parse_date_hhmm(hdrs.get("date"), self.utc_offset_hours)
        hdrs = None
        gc.collect()
        out = _repack(tmp)
        tmp = None
        gc.collect()
        return out
