# pico/tedapi_query.py
# Signed GraphQL payload used by TEDAPI (POST /tedapi/v1, MessageEnvelope field 16).
#
# To populate this file for a real Powerwall 3:
#   1. On your PC:  pip install -U pypowerwall
#   2. Run:         python3 export_tedapi_query.py
#   3. Upload the generated tedapi_query.py to your Pico W via Thonny.
#
# The placeholder below works with tools/mock_powerwall.py for bench testing.

IS_REAL_SIGNED_QUERY = False
GRAPHQL_PAYLOAD = b""

QUERY_TEXT = (
    b"query DeviceControllerQuery {\n"
    b"  control {\n"
    b"    systemStatus {\n"
    b"      nominalFullPackEnergyWh\n"
    b"      nominalEnergyRemainingWh\n"
    b"    }\n"
    b"    islanding {\n"
    b"      contactorClosed\n"
    b"      gridOK\n"
    b"    }\n"
    b"    meterAggregates {\n"
    b"      location\n"
    b"      realPowerW\n"
    b"    }\n"
    b"  }\n"
    b"}\n"
)

QUERY_SIG = b"MOCK_ECDSA_SIGNATURE_REPLACE_VIA_EXPORT_SCRIPT"
QUERY_VARS = b"{}"
