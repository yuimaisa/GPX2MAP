import requests
import json
import xml.etree.ElementTree as ET

url = "https://vldb.gsi.go.jp/sokuchi/geomag/math/cgimath/sigmath_m.cgi"
params = {
    "use_of_data": "xml",
    "year": 2026,
    "month": 7,
    "day": 11,
    "lat": 35.6812,
    "lon": 139.7671,
    "alt": 0
}

try:
    response = requests.get(url, params=params, timeout=10)
    print("STATUS:", response.status_code)
    print("CONTENT:", response.text)
except Exception as e:
    print("ERROR:", e)
