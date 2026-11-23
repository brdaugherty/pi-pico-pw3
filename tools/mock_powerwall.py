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


def _sig(name, value=None, text_value=None, bool_value=None):
    return {
        "name": name,
        "value": value,
        "textValue": text_value,
        "boolValue": bool_value,
        "timestamp": "2026-11-22T20:00:00Z",
    }


def simulated_components(st):
    t = time.time() - START_TIME
    dc_total = st["solar_w"] / 0.96 if st["solar_w"] > 0 else 0.0
    # 4 active roof planes (A-D) + 2 unused standby inputs (E-F)
    weights = (0.34, 0.28, 0.23, 0.15, 0.0, 0.0)
    voltages = (318.0, 312.0, 298.0, 285.0, 0.0, 0.0)
    pch_signals = []
    for idx, ch in enumerate(("A", "B", "C", "D", "E", "F")):
        w = dc_total * weights[idx]
        if weights[idx] > 0:
            v = round(voltages[idx] + 4.0 * math.sin(t / 40.0 + idx), 1)
            i = round(w / v, 2) if v > 0 else 0.0
            state = "Pv_Active" if w > 5.0 else "Pv_Standby"
        else:
            v = 0.0
            i = -3.6e-16  # realistic PW3 night/unused floating-point noise
            state = "Pv_Standby"
        pch_signals.append(_sig(f"PCH_PvState_{ch}", 1 if state == "Pv_Active" else 0, state))
        pch_signals.append(_sig(f"PCH_PvVoltage{ch}", v))
        pch_signals.append(_sig(f"PCH_PvCurrent{ch}", i))

    v_an = round(120.6 + 0.4 * math.sin(t / 25.0), 1)
    v_bn = round(120.8 + 0.4 * math.cos(t / 25.0), 1)
    v_ab = round(v_an + v_bn, 1)
    freq = round(60.00 + 0.02 * math.sin(t / 15.0), 2)
    fan_a = round(1280 + 180 * math.sin(t / 50.0), 1)
    fan_b = round(fan_a + 9.0, 1)
    duty_a = round(16.0 + 4.0 * math.sin(t / 50.0), 1)
    duty_b = round(duty_a + 0.4, 1)

    pch_signals.extend(
        [
            _sig("PCH_SlowPvPowerSum", round(dc_total, 1)),
            _sig("PCH_AcVoltageAB", v_ab),
            _sig("PCH_AcVoltageAN", v_an),
            _sig("PCH_AcVoltageBN", v_bn),
            _sig("PCH_AcFrequency", freq),
            _sig("PCH_AcRealPowerAB", round(-st["batt_w"], 1)),
            _sig("PCH_BatteryPower", round(st["batt_w"], 1)),
            _sig("PCH_AcMode", 1, "PCH_AcMode_GridFollowing"),
            _sig("PCH_State", 1, "PCH_State_Running"),
            _sig("PCH_AmbientTemp", round(44.2 + 2.5 * math.sin(t / 90.0), 1)),
            _sig("PCH_FanSpeed_A", fan_a),
            _sig("PCH_FanSpeed_B", fan_b),
            _sig("PCH_FanDuty_A", duty_a),
            _sig("PCH_FanDuty_B", duty_b),
        ]
    )

    return {
        "components": {
            "pch": [{"signals": pch_signals, "activeAlerts": []}],
            "hvp": [
                {
                    "partNumber": "1707000-00-A",
                    "serialNumber": "TG00000000MOCK",
                    "signals": [
                        _sig("HVP_State", 1, "HVP_State_Connected"),
                        _sig("HVP_PackTempMin", round(33.4 + 1.2 * math.sin(t / 80.0), 1)),
                        _sig("HVP_PackTempMax", round(37.8 + 1.5 * math.sin(t / 80.0), 1)),
                        _sig("HVP_ShuntTemperature", round(39.1 + 1.3 * math.sin(t / 80.0), 1)),
                    ],
                    "activeAlerts": [],
                }
            ],
            "pws": [
                {
                    "signals": [
                        _sig("PWS_RSD_State", 1, "RSD_State_Armed"),
                        _sig("PWS_ProdSwitch_State", 1, "PWS_SWITCH_STATE_CLOSED"),
                        _sig("PWS_SelfTest", 1, "PWS_SelfTest_Passed"),
                    ],
                    "activeAlerts": [],
                }
            ],
            "baggr": [
                {
                    "signals": [
                        _sig("BAGGR_State", 1, "BAGGR_State_Running"),
                        _sig("BAGGR_NumBatteriesConnected", 1),
                        _sig("BAGGR_NumBatteriesExpected", 1),
                    ],
                    "activeAlerts": [],
                }
            ],
            "bms": [
                {
                    "signals": [
                        _sig("BMS_nominalEnergyRemaining", round(st["rem_wh"] / 1000.0, 3)),
                        _sig("BMS_nominalFullPackEnergy", round(st["full_wh"] / 1000.0, 3)),
                        _sig("BMS_LOG_tempOutOfBounds", 0),
                        _sig("BMS_LOG_tempOutOfBoundsCharge", 0),
                    ],
                    "activeAlerts": [],
                }
            ],
        }
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
                is_components = (
                    b"ComponentsQuery" in body
                    or b"pchSignalNames" in body
                    or b"PCH_PvVoltageA" in body
                )
                if is_components:
                    payload_json = json.dumps(simulated_components(st))
                else:
                    # Include realistic esCan padding (~12 KB uncompressed) to test RP2040 streaming
                    local_iso = time.strftime("%Y-%m-%dT%H:%M:%S-08:00", time.localtime())
                    payload_json = json.dumps(
                        {
                            "system": {"time": local_iso},
                            "esCan": {"bus": {"PINV": [{"isMIA": True, "PINV_Vout": 0}] * 300}},
                            "control": {
                                "islanding": {"contactorClosed": True, "gridOK": True},
                                "siteShutdown": {"isShutDown": False, "reasons": []},
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
                            "ieee20305": {
                                "controls": {
                                    "defaultControl": {"opModExpLimW": 5000},
                                    "activeControls": {"opModExpLimW": 4500},
                                }
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
