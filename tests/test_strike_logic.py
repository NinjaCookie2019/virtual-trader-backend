import unittest
from zoneinfo import ZoneInfo

from app.services.dhan_gateway import DhanGateway


class StrikeLogicTests(unittest.TestCase):
    def test_call_strike_moves_one_step_above_spot(self):
        self.assertEqual(DhanGateway._calculate_otm_strike(25000, "CALL", 50), 25050)
        self.assertEqual(DhanGateway._calculate_otm_strike(25023, "CALL", 50), 25050)

    def test_put_strike_moves_one_step_below_spot(self):
        self.assertEqual(DhanGateway._calculate_otm_strike(25000, "PUT", 50), 24950)
        self.assertEqual(DhanGateway._calculate_otm_strike(25023, "PUT", 50), 25000)

    def test_parse_dhan_token_validity_as_ist(self):
        parsed = DhanGateway.parse_token_validity("30/03/2025 15:37")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M"), "2025-03-30 15:37")

    def test_extract_access_token_from_renew_response(self):
        self.assertEqual(DhanGateway.extract_access_token({"accessToken": " new-token "}), "new-token")
        self.assertEqual(DhanGateway.extract_access_token({"data": {"access_token": "nested-token"}}), "nested-token")


if __name__ == "__main__":
    unittest.main()
