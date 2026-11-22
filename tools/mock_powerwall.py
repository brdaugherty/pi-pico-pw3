#!/usr/bin/env python3
"""
tools/mock_powerwall.py

Realistic HTTPS (self-signed TLS) Tesla Powerwall 3 TEDAPI simulator for PC bench testing.
Supports both uncompressed and `--gzip` (Content-Encoding: gzip) TEDAPI responses so you
can verify both RP2040 memory paths before connecting to your real Powerwall 3.

Usage:
    python3 tools/mock_powerwall.py --port 8443 --password TESTPASS
    python3 tools/mock_powerwall.py --port 8443 --password TESTPASS --gzip
"""

import argparse
import base64
import gzip
import http.server
import json
import math
import os
import ssl
import subprocess
import tempfile
import time

START_TIME = time.time()


def _pb_varint(v):
    out = bytearray()
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v & 0x7F)
    return bytes(out)


def _pb_bytes(field_num, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _pb_varint((field_num << 3) | 2) + _pb_varint(len(data)) + data


def _pb_int(field_num, val):
    return _pb_varint((field_num << 3) | 0) + _pb_varint(val)


def wrap_tedapi_response(din, json_str):
    recv_inner = _pb_int(1, 1) + _pb_bytes(2, json_str)
    gql_msg = _pb_bytes(2, recv_inner)
    env = (
        _pb_int(1, 1)
        + _pb_bytes(2, _pb_bytes(1, din))
        + _pb_bytes(3, _pb_int(3, 1))
        + _pb_bytes(16, gql_msg)
    )
    return _pb_bytes(1, env) + _pb_bytes(2, _pb_int(1, 1))


def simulated_state(capacity_wh=13500.0):
    t = time.time() - START_TIME
    raw_soc = 62.5 + 32.5 * math.sin(t / 45.0)
    rem_wh = round((raw_soc / 100.0) * capacity_wh, 1)
    solar_w = round(max(0.0, 4200.0 * math.sin(t / 60.0 + 0.5)), 1)
    load_w = round(1450.0 + 400.0 * math.cos(t / 30.0), 1)
    batt_w = round(load_w - solar_w, 1)
    site_w = round(25.0 * math.sin(t / 20.0), 1)
    return {
        "rem_wh": rem_wh,
        "full_wh": capacity_wh,
        "solar_w": solar_w,
        "load_w": load_w,
        "batt_w": batt_w,
        "site_w": site_w,
    }


def make_handler(expected_pwd, din, force_gzip=False):
    expected_basic = "Basic " + base64.b64encode(
        f"Tesla_Energy_Device:{expected_pwd}".encode()
    ).decode()

    class MockPW3Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, body, content_type="application/json", use_gzip=False):
            if isinstance(body, str):
                body = body.encode("utf-8")
            if use_gzip:
                body = gzip.compress(body)
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _check_basic(self):
            if self.headers.get("Authorization") != expected_basic:
                self._send(401, '{"code":401,"error":"Unauthorized"}')
                return False
            return True

        def do_GET(self):
            if self.path == "/tedapi/din":
                if not self._check_basic():
                    return
                self._send(200, din, content_type="text/plain")
            else:
                self._send(404, '{"code":404,"error":"Not Found"}')

        def do_POST(self):
            cl = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(cl) if cl > 0 else b""
            if self.path == "/tedapi/v1":
                if not self._check_basic():
                    return
                if not body or body[0] != 0x0A:
                    self._send(400, '{"code":400,"error":"Invalid protobuf envelope"}')
                    return
                st = simulated_state()
                # Include realistic esCan padding (~12 KB uncompressed) to test RP2040 streaming
                payload_json = json.dumps(
                    {
                        "esCan": {"bus": {"PINV": [{"isMIA": True, "PINV_Vout": 0}] * 300}},
                        "control": {
                            "islanding": {"contactorClosed": True, "gridOK": True},
                            "meterAggregates": [
                                {"location": "BATTERY", "realPowerW": st["batt_w"]},
                                {"location": "SITE", "realPowerW": st["site_w"]},
                                {"location": "LOAD", "realPowerW": st["load_w"]},
                                {"location": "SOLAR", "realPowerW": st["solar_w"]},
                            ],
                            "systemStatus": {
                                "nominalEnergyRemainingWh": st["rem_wh"],
                                "nominalFullPackEnergyWh": st["full_wh"],
                            },
                        },
                    }
                )
                pb_resp = wrap_tedapi_response(din, payload_json)
                self._send(
                    200,
                    pb_resp,
                    content_type="application/octet-stream",
                    use_gzip=force_gzip,
                )
            else:
                self._send(404, '{"code":404,"error":"Not Found"}')

    return MockPW3Handler


def generate_self_signed_cert(cert_dir):
    cert_path = os.path.join(cert_dir, "mock_pw3_cert.pem")
    key_path = os.path.join(cert_dir, "mock_pw3_key.pem")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            key_path,
            "-out",
            cert_path,
            "-days",
            "30",
            "-nodes",
            "-subj",
            "/CN=192.168.91.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert_path, key_path


def main():
    parser = argparse.ArgumentParser(description="HTTPS Powerwall 3 TEDAPI Mock Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--password", default="TESTPASS")
    parser.add_argument("--din", default="1707000-00-A--TG00000000MOCK")
    parser.add_argument("--gzip", action="store_true", help="Force Content-Encoding: gzip")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        cert_path, key_path = generate_self_signed_cert(tmpdir)
        server = http.server.ThreadingHTTPServer(
            (args.host, args.port), make_handler(args.password, args.din, args.gzip)
        )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        print(f"Mock Powerwall 3 TEDAPI listening on https://{args.host}:{args.port}")
        print(f"  Password : {args.password}")
        print(f"  DIN      : {args.din}")
        print(f"  Gzip     : {'FORCED ON' if args.gzip else 'OFF'}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    main()
