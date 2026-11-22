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
import os
import sys


# --- Minimal Protobuf Wire Parser (zero extra dependencies) ---

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


# --- Strategy 1: Capture exact wire payload directly from pypowerwall.tedapi.TEDAPI ---

def _capture_via_mock_tedapi():
    try:
        import requests
        from pypowerwall.tedapi import TEDAPI
    except Exception:
        return None

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
        for method_name in ("get_status", "get_device_controller", "get_blocks"):
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

    for raw_pb in captured:
        try:
            f16 = _extract_field16_from_tedapi_msg(raw_pb)
            if f16 and (b"DeviceControllerQuery" in f16 or b"nominalFullPackEnergyWh" in f16):
                return f16
        except Exception:
            continue
    return None


# --- Strategy 2: Scan files on disk inside pypowerwall/tedapi/ ---

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


def _scan_files_on_disk(pkg_dir):
    queries = []
    sigs = []

    for root, _, files in os.walk(pkg_dir):
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            try:
                raw = open(fpath, "rb").read()
            except OSError:
                continue

            # Case A: Direct binary file containing protobuf or DER signature
            if _is_valid_ecdsa_p521_der(raw):
                sigs.append((fname, raw))
                continue
            if b"DeviceControllerQuery" in raw and b"nominalFullPackEnergyWh" in raw:
                if fname.endswith((".gql", ".graphql", ".txt")):
                    queries.append((fname, raw))

            # Case B: Parse Python source literals cleanly via AST
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

    if queries and sigs:
        q_bytes = queries[0][1]
        for _, qb in queries:
            if b"$msaComp" not in qb:
                q_bytes = qb
                break
        sig_bytes = sigs[0][1]
        # Build field 16 (GraphQLMessages) in standard V2024_06 wire format
        query_inner = _pb_field_int(1, 1) + _pb_field_bytes(2, q_bytes)
        vars_inner = _pb_field_bytes(1, b"{}")
        gql_send = (
            _pb_field_int(1, 2)
            + _pb_field_bytes(2, query_inner)
            + _pb_field_bytes(3, sig_bytes)
            + _pb_field_bytes(4, vars_inner)
        )
        return _pb_field_bytes(1, gql_send)
    return None


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

    gql_payload = _capture_via_mock_tedapi()
    method = "TEDAPI.get_status() wire capture"
    if not gql_payload:
        gql_payload = _scan_files_on_disk(pkg_dir)
        method = "file/AST scan"

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
        f.write("# Exact protobuf bytes for MessageEnvelope field 16 (GraphQLMessages)\n")
        f.write(f"GRAPHQL_PAYLOAD = {gql_payload!r}\n\n")
        f.write("QUERY_TEXT = b''\n")
        f.write("QUERY_SIG = b''\n")
        f.write('QUERY_VARS = b"{}"\n')

    print(f"Success! Wrote {out_path}")
    print(f"  pypowerwall     : v{version}")
    print(f"  Extraction mode : {method}")
    print(f"  GRAPHQL_PAYLOAD : {len(gql_payload)} bytes")
    print("Upload tedapi_query.py to your Pico W in Thonny alongside main.py, config.py, and powerwall_api.py.")


if __name__ == "__main__":
    main()
