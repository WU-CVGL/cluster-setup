import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import line_ha
from render_standby_relay import render

EXAMPLE = Path(__file__).resolve().parents[3] / "xray-ha/standby/config.example.json"


class StandbyTest(unittest.TestCase):
    def setUp(self):
        self.site = line_ha.load(EXAMPLE)
        self.config = {"inbounds": [], "outbounds": [{"protocol": "vless", "tag": "primary",
                       "settings": {"vnext": [{"address": "peer.example.org", "port": 443}]}}],
                       "routing": {"rules": [{"ip": ["geoip:private"], "outboundTag": "direct"}]}}
        self.original = copy.deepcopy(self.config)

    def test_order_change_single_path_and_disable(self):
        for order in (["campus", "remote"], ["remote", "campus"], ["remote"]):
            routes = {p["name"]: p for p in self.site["paths"]}
            self.site["paths"] = [routes[n] for n in order]
            for _ in range(2):
                line_ha.configure(self.config, self.site, "example-line")
                choices = ["primary"] + [line_ha.PREFIX + "via/" + n + "/" for n in order]
                self.assertEqual(self.config["observatory"]["subjectSelector"], choices)
                pools = self.config["routing"]["balancers"]
                self.assertEqual([p["selector"][0] for p in pools], choices[:-1])
                self.assertEqual(pools[-1]["fallbackTag"], choices[-1])
                for out in self.config["outbounds"]:
                    if out["protocol"] == "vless":
                        self.assertEqual(out["settings"], self.original["outbounds"][0]["settings"])
                once = copy.deepcopy(self.config)
                line_ha.configure(self.config, self.site, "example-line")
                self.assertEqual(once, self.config)
        line_ha.configure(self.config, {"lines": {}}, "example-line")
        self.assertEqual(self.original, self.config)

    def test_relays_match_inventory(self):
        direct = json.loads(render(self.site, "campus", "xray"))
        self.assertEqual(direct["inbounds"][0]["settings"]["port"], 443)
        self.assertEqual(direct["outbounds"][0]["protocol"], "blackhole")
        self.assertIn("single = 31001", render(self.site, "remote", "frps"))
        client = render(self.site, "remote", "frpc")
        self.assertIn('localIP = "peer.example.org"', client)
        self.assertIn("remotePort = 31001", client)

    def test_wrong_peer_rejected(self):
        self.site["lines"]["example-line"]["port"] = 444
        with self.assertRaises(ValueError):
            line_ha.configure(self.config, self.site, "example-line")
