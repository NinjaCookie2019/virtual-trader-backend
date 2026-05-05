import unittest
from zoneinfo import ZoneInfo

from app.services.dhan_gateway import DhanGateway


class StrikeLogicTests(unittest.TestCase):
    def test_call_strike_uses_upper_bucket_for_spot(self):
        self.assertEqual(DhanGateway._calculate_otm_strike(24000.00, "CALL", 50), 24000)
        self.assertEqual(DhanGateway._calculate_otm_strike(24004.10, "CALL", 50), 24050)
        self.assertEqual(DhanGateway._calculate_otm_strike(24050.00, "CALL", 50), 24050)
        self.assertEqual(DhanGateway._calculate_otm_strike(24051.00, "CALL", 50), 24100)

    def test_put_strike_uses_lower_bucket_for_spot(self):
        self.assertEqual(DhanGateway._calculate_otm_strike(24050.00, "PUT", 50), 24050)
        self.assertEqual(DhanGateway._calculate_otm_strike(24004.10, "PUT", 50), 24000)
        self.assertEqual(DhanGateway._calculate_otm_strike(23990.00, "PUT", 50), 23950)
        self.assertEqual(DhanGateway._calculate_otm_strike(24352.90, "PUT", 50), 24350)

    def test_oi_confirmation_strike_uses_breakout_level(self):
        self.assertEqual(DhanGateway._calculate_oi_confirmation_strike(24480.65, "CALL", 50), 24500)
        self.assertEqual(DhanGateway._calculate_oi_confirmation_strike(24004.75, "PUT", 50), 24000)
        self.assertEqual(DhanGateway._calculate_oi_confirmation_strike(24241.25, "PUT", 50), 24200)
        self.assertEqual(DhanGateway._calculate_oi_confirmation_strike(24352.90, "PUT", 50), 24350)
        self.assertEqual(DhanGateway._calculate_oi_confirmation_strike(24350.00, "PUT", 50), 24350)

    def test_extract_change_oi_from_direct_or_previous_oi(self):
        self.assertEqual(DhanGateway._extract_change_oi({"change_oi": 120}), 120)
        self.assertEqual(DhanGateway._extract_change_oi({"oi": 280, "previous_oi": 100}), 180)

    def test_parse_dhan_token_validity_as_ist(self):
        parsed = DhanGateway.parse_token_validity("30/03/2025 15:37")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M"), "2025-03-30 15:37")

    def test_extract_access_token_from_renew_response(self):
        self.assertEqual(DhanGateway.extract_access_token({"accessToken": " new-token "}), "new-token")
        self.assertEqual(DhanGateway.extract_access_token({"data": {"access_token": "nested-token"}}), "nested-token")


if __name__ == "__main__":
    unittest.main()
