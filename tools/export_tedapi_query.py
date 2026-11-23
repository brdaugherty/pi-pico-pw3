#!/usr/bin/env python3
"""
tools/export_tedapi_query.py

One-time PC helper that extracts the exact Tesla-signed TEDAPI GraphQL payload
from your installed `pypowerwall` package (offline — no Powerwall connection
required) and writes `tedapi_query.py` ready to upload to your Pico W in Thonny.

Usage:
    pip install -U pypowerwall
    python3 export_tedapi_query.py
"""

import ast
import base64
import json
import os
import sys


# Hardware-validated PW3 extra signals appended to V2024_06 ComponentsQuery
# unsigned *SignalNames variables (gateway ignores unknown names safely).
EXTRA_COMPONENT_SIGNALS = {
    "pchSignalNames": (
        "PCH_AmbientTemp",
        "PCH_FanSpeed_A",
        "PCH_FanSpeed_B",
        "PCH_FanDuty_A",
        "PCH_FanDuty_B",
        "PCH_SlowPvPowerSum",
        "PCH_AcRealPowerAB",
        "PCH_BatteryPower",
    ),
    "hvpSignalNames": (
        "HVP_PackTempMin",
        "HVP_PackTempMax",
        "HVP_ShuntTemperature",
    ),
    "pwsSignalNames": (
        "PWS_RSD_State",
    ),
}


# --- Minimal Protobuf Wire Parser & Encoder (zero extra dependencies) ---

def _read_varint(buf, pos):
    val = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
    raise ValueError("Truncated varint")


def _iter_pb_fields(buf):
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            val, pos = _read_varint(buf, pos)
            yield field_num, wire_type, val
        elif wire_type == 1:
            yield field_num, wire_type, buf[pos:pos + 8]
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(buf, pos)
            yield field_num, wire_type, buf[pos:pos + length]
            pos += length
        elif wire_type == 5:
            yield field_num, wire_type, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")


def _pb_varint(v):
    out = bytearray()
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _pb_field_bytes(fnum, data):
    return _pb_varint((fnum << 3) | 2) + _pb_varint(len(data)) + data


def _pb_field_int(fnum, val):
    return _pb_varint((fnum << 3) | 0) + _pb_varint(val)


def _pb_encode_field(fnum, wtype, val):
    tag = _pb_varint((fnum << 3) | wtype)
    if wtype == 0:
        return tag + _pb_varint(val)
    if wtype == 2:
        return tag + _pb_varint(len(val)) + val
    return tag + val


def _extract_field16_from_tedapi_msg(pb_bytes):
    """
    Extract raw field 16 (GraphQLMessages) bytes from a serialized TEDAPI
    Message / AuthEnvelope request.
    """
    for fnum, wtype, val in _iter_pb_fields(pb_bytes):
        if fnum == 1 and wtype == 2:
            # Inside MessageEnvelope
            for efnum, ewtype, eval_ in _iter_pb_fields(val):
                if efnum == 16 and ewtype == 2:
                    return eval_
    return None


def _merge_extra_signal_vars(vars_json_bytes):
    """Append hardware-validated PW3 extra signals to unsigned *SignalNames lists."""
    try:
        obj = json.loads(vars_json_bytes.decode("utf-8"))
    except Exception:
        return vars_json_bytes
    if not isinstance(obj, dict) or "pchSignalNames" not in obj:
        return vars_json_bytes

    changed = False
    for key, extras in EXTRA_COMPONENT_SIGNALS.items():
        lst = obj.get(key)
        if isinstance(lst, list):
            for sig in extras:
                if sig not in lst:
                    lst.append(sig)
                    changed = True
    if not changed:
        return vars_json_bytes
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def _ensure_extra_component_signals(f16_bytes):
    """
    For V2024_06 ComponentsQuery payloads (field 1 -> field 4 -> field 1 JSON),
    append any missing PW3 thermal/fan/RSD signal names without modifying the
    signed query text or ECDSA signature.
    """
    try:
        outer = list(_iter_pb_fields(f16_bytes))
    except Exception:
        return f16_bytes

    out_outer = bytearray()
    modified = False
    for ofnum, owtype, oval in outer:
        if ofnum == 1 and owtype == 2:
            inner = list(_iter_pb_fields(oval))
            out_inner = bytearray()
            for ifnum, iwtype, ival in inner:
                if ifnum == 4 and iwtype == 2:
                    vfields = list(_iter_pb_fields(ival))
                    out_v = bytearray()
                    for vfnum, vwtype, vval in vfields:
                        if vfnum == 1 and vwtype == 2:
                            merged = _merge_extra_signal_vars(vval)
                            if merged != vval:
                                modified = True
                            out_v.extend(_pb_encode_field(vfnum, vwtype, merged))
                        else:
                            out_v.extend(_pb_encode_field(vfnum, vwtype, vval))
                    out_inner.extend(_pb_field_bytes(4, bytes(out_v)))
                else:
                    out_inner.extend(_pb_encode_field(ifnum, iwtype, ival))
            out_outer.extend(_pb_field_bytes(1, bytes(out_inner)))
        else:
            out_outer.extend(_pb_encode_field(ofnum, owtype, oval))

    return bytes(out_outer) if modified else f16_bytes


def _is_valid_ecdsa_p521_der(b):
    if not isinstance(b, bytes) or len(b) < 130 or len(b) > 144:
        return False
    if b[0] != 0x30 or b[1] != 0x81 or b[2] != len(b) - 3:
        return False
    if b[3] != 0x02:
        return False
    r_len = b[4]
    s_off = 5 + r_len
    if s_off + 2 >= len(b) or b[s_off] != 0x02:
        return False
    s_len = b[s_off + 1]
    return (s_off + 2 + s_len) == len(b)


def _build_v2024_field16(query_bytes, sig_bytes, vars_bytes=b"{}"):
    query_inner = _pb_field_int(1, 1) + _pb_field_bytes(2, query_bytes)
    vars_inner = _pb_field_bytes(1, vars_bytes)
    gql_send = (
        _pb_field_int(1, 2)
        + _pb_field_bytes(2, query_inner)
        + _pb_field_bytes(3, sig_bytes)
        + _pb_field_bytes(4, vars_inner)
    )
    return _pb_field_bytes(1, gql_send)


# --- Strategy 1: Capture exact wire payloads directly from pypowerwall.tedapi.TEDAPI ---

def _capture_via_mock_tedapi():
    try:
        import requests
        from pypowerwall.tedapi import TEDAPI
    except Exception:
        return None, None

    captured = []
    dummy_din = "1707000-00-A--TG00000000DUMMY"

    class _FakeResp:
        def __init__(self, status_code=200, content=b""):
            self.status_code = status_code
            self.content = content
            self.text = content.decode("utf-8", "ignore")
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return {}

    orig_request = requests.Session.request

    def fake_request(self, method, url, *args, **kwargs):
        u = str(url)
        m = str(method).upper()
        if m == "GET" and "/tedapi/din" in u:
            return _FakeResp(200, dummy_din.encode("utf-8"))
        if m == "POST" and "/tedapi/v1" in u:
            data = kwargs.get("data")
            if isinstance(data, (bytes, bytearray)):
                captured.append(bytes(data))
            return _FakeResp(200, b"")
        return _FakeResp(200, b"{}")

    requests.Session.request = fake_request

    try:
        try:
            gw = TEDAPI("DUMMY_GW_PASSWORD")
        except TypeError:
            gw = TEDAPI(gw_pwd="DUMMY_GW_PASSWORD")
        gw.din = dummy_din
        for method_name in (
            "get_status",
            "get_device_controller",
            "get_components",
            "get_blocks",
        ):
            fn = getattr(gw, method_name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        requests.Session.request = orig_request

    status_f16 = None
    comp_f16 = None
    for raw_pb in captured:
        try:
            f16 = _extract_field16_from_tedapi_msg(raw_pb)
            if not f16:
                continue
            if status_f16 is None and (
                b"DeviceControllerQuery" in f16 or b"nominalFullPackEnergyWh" in f16
            ):
                status_f16 = f16
            elif comp_f16 is None and (
                b"ComponentsQuery" in f16
                or b"pchSignalNames" in f16
                or b"PCH_PvVoltageA" in f16
            ):
                comp_f16 = _ensure_extra_component_signals(f16)
        except Exception:
            continue
    return status_f16, comp_f16


# --- Strategy 2: Scan query JSON / files on disk inside pypowerwall/tedapi/ ---

def _scan_query_json(pkg_dir):
    status_f16 = None
    comp_f16 = None
    json_path = os.path.join(pkg_dir, "queries", "V2024_06.json")
    if not os.path.isfile(json_path):
        return None, None
    try:
        data = json.loads(open(json_path, "r", encoding="utf-8").read())
    except Exception:
        return None, None

    rec = data.get("device_controller_basic") or data.get("device_controller_full")
    if isinstance(rec, dict) and rec.get("text") and rec.get("code"):
        try:
            q_bytes = rec["text"].encode("utf-8")
            sig_bytes = bytes.fromhex(rec["code"])
            v_bytes = rec.get("b_value", "{}").encode("utf-8")
            status_f16 = _build_v2024_field16(q_bytes, sig_bytes, v_bytes)
        except Exception:
            pass

    crec = data.get("components")
    if isinstance(crec, dict) and crec.get("text") and crec.get("code"):
        try:
            q_bytes = crec["text"].encode("utf-8")
            sig_bytes = bytes.fromhex(crec["code"])
            v_bytes = _merge_extra_signal_vars(crec.get("b_value", "{}").encode("utf-8"))
            comp_f16 = _build_v2024_field16(q_bytes, sig_bytes, v_bytes)
        except Exception:
            pass

    return status_f16, comp_f16


def _scan_files_on_disk(pkg_dir):
    status_f16, comp_f16 = _scan_query_json(pkg_dir)
    if status_f16 and comp_f16:
        return status_f16, comp_f16

    queries = []
    sigs = []

    for root, _, files in os.walk(pkg_dir):
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            try:
                raw = open(fpath, "rb").read()
            except OSError:
                continue

            if _is_valid_ecdsa_p521_der(raw):
                sigs.append((fname, raw))
                continue
            if b"DeviceControllerQuery" in raw and b"nominalFullPackEnergyWh" in raw:
                if fname.endswith((".gql", ".graphql", ".txt")):
                    queries.append((fname, raw))

            if fname.endswith(".py"):
                try:
                    tree = ast.parse(raw.decode("utf-8", "ignore"), filename=fpath)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                        v = node.value
                        if isinstance(v, bytes):
                            if _is_valid_ecdsa_p521_der(v):
                                sigs.append((fname, v))
                            elif b"DeviceControllerQuery" in v and b"nominalFullPackEnergyWh" in v:
                                queries.append((fname, v))
                        else:
                            if "DeviceControllerQuery" in v and "nominalFullPackEnergyWh" in v:
                                queries.append((fname, v.encode("utf-8")))
                            elif 160 <= len(v.strip()) <= 220:
                                try:
                                    dec = base64.b64decode(v.strip(), validate=True)
                                    if _is_valid_ecdsa_p521_der(dec):
                                        sigs.append((fname, dec))
                                except Exception:
                                    pass
                            elif 260 <= len(v.strip()) <= 290:
                                try:
                                    dec = bytes.fromhex(v.strip())
                                    if _is_valid_ecdsa_p521_der(dec):
                                        sigs.append((fname, dec))
                                except Exception:
                                    pass

    if status_f16 is None and queries and sigs:
        q_bytes = queries[0][1]
        for _, qb in queries:
            if b"$msaComp" not in qb:
                q_bytes = qb
                break
        status_f16 = _build_v2024_field16(q_bytes, sigs[0][1], b"{}")

    return status_f16, comp_f16


def _output_path():
    cwd_pico = os.path.join(os.getcwd(), "pico")
    if os.path.isdir(cwd_pico):
        return os.path.join(cwd_pico, "tedapi_query.py")
    script_parent_pico = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pico"
    )
    if os.path.isdir(script_parent_pico):
        return os.path.join(script_parent_pico, "tedapi_query.py")
    return os.path.join(os.getcwd(), "tedapi_query.py")


def main():
    try:
        import pypowerwall
        import pypowerwall.tedapi as tedapi_pkg
    except ImportError:
        sys.exit("pypowerwall is not installed. Run: pip install -U pypowerwall")

    pkg_dir = os.path.dirname(os.path.abspath(tedapi_pkg.__file__))
    version = getattr(pypowerwall, "__version__", "unknown")

    gql_payload, comp_payload = _capture_via_mock_tedapi()
    method = "TEDAPI wire capture"
    if not gql_payload or not comp_payload:
        fb_gql, fb_comp = _scan_files_on_disk(pkg_dir)
        if not gql_payload and fb_gql:
            gql_payload = fb_gql
            method = "query JSON / file scan"
        if not comp_payload and fb_comp:
            comp_payload = fb_comp
            method += " + components JSON"

    if not gql_payload:
        found_files = []
        for root, _, files in os.walk(pkg_dir):
            for f in sorted(files):
                if not f.endswith(".pyc"):
                    found_files.append(os.path.relpath(os.path.join(root, f), pkg_dir))
        print(f"ERROR: Could not extract TEDAPI GraphQL payload from pypowerwall {version}.")
        print(f"Files under {pkg_dir}:")
        for f in found_files:
            print(f"  - {f}")
        sys.exit(1)

    out_path = _output_path()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(
            f"# Auto-generated by export_tedapi_query.py from pypowerwall {version} ({method})\n"
        )
        f.write("IS_REAL_SIGNED_QUERY = True\n\n")
        f.write("# Exact protobuf bytes for MessageEnvelope field 16 (DeviceControllerQuery)\n")
        f.write(f"GRAPHQL_PAYLOAD = {gql_payload!r}\n\n")
        f.write("# Exact protobuf bytes for MessageEnvelope field 16 (ComponentsQuery: strings & vitals)\n")
        f.write(f"COMPONENTS_PAYLOAD = {comp_payload or b''!r}\n\n")
        f.write("QUERY_TEXT = b''\n")
        f.write("QUERY_SIG = b''\n")
        f.write('QUERY_VARS = b"{}"\n')
        f.write("COMPONENTS_QUERY_TEXT = b''\n")
        f.write("COMPONENTS_QUERY_SIG = b''\n")
        f.write('COMPONENTS_QUERY_VARS = b"{}"\n')

    print(f"Success! Wrote {out_path}")
    print(f"  pypowerwall        : v{version}")
    print(f"  Extraction mode    : {method}")
    print(f"  GRAPHQL_PAYLOAD    : {len(gql_payload)} bytes")
    print(f"  COMPONENTS_PAYLOAD : {len(comp_payload) if comp_payload else 0} bytes")
    if not comp_payload:
        print("  WARNING: ComponentsQuery payload was not found; upgrade pypowerwall (`pip install -U pypowerwall`) for Strings/Vitals detail views.")
    print("Upload tedapi_query.py to your Pico W in Thonny alongside main.py, config.py, and powerwall_api.py.")


if __name__ == "__main__":
    main()
