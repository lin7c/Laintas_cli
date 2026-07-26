import os
import unittest
from unittest.mock import patch

import webrtc_channel


class WebrtcConfigurationTests(unittest.TestCase):
    def test_gateway_rtc_config_is_flattened_and_turn_is_rejected(self):
        config = {
            "iceServers": [
                {"urls": ["stun:stun.example.com:3478", "turn:turn.example.com:3478"]},
                {"urls": "stun:backup.example.com:3478"},
            ],
        }
        self.assertEqual(
            webrtc_channel.normalize_stun_urls(config),
            ["stun:stun.example.com:3478", "stun:backup.example.com:3478"],
        )

    def test_environment_override_wins(self):
        with patch.dict(
            os.environ,
            {"LAINTAS_ICE_SERVERS": "stun:override.example.com:3478"},
        ):
            self.assertEqual(
                webrtc_channel.configured_ice_servers({
                    "iceServers": [{"urls": "stun:gateway.example.com:3478"}],
                }),
                ["stun:override.example.com:3478"],
            )


if __name__ == "__main__":
    unittest.main()
