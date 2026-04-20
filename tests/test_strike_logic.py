import unittest

from app.services.dhan_gateway import DhanGateway


class StrikeLogicTests(unittest.TestCase):
    def test_call_strike_moves_one_step_above_spot(self):
        self.assertEqual(DhanGateway._calculate_otm_strike(25000, "CALL", 50), 25050)
        self.assertEqual(DhanGateway._calculate_otm_strike(25023, "CALL", 50), 25050)

    def test_put_strike_moves_one_step_below_spot(self):
        self.assertEqual(DhanGateway._calculate_otm_strike(25000, "PUT", 50), 24950)
        self.assertEqual(DhanGateway._calculate_otm_strike(25023, "PUT", 50), 25000)


if __name__ == "__main__":
    unittest.main()

