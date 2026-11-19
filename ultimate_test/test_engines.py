"""Isolated offline regression tests: python -m unittest discover -s ultimate_test -p 'test_*.py'"""
import tempfile
import unittest
from pathlib import Path

try:
    from ultimate_test.paper_engine import PaperEngine, PaperConfig
    from ultimate_test.risk_engine import evaluate_token
except ModuleNotFoundError:
    from paper_engine import PaperEngine, PaperConfig
    from risk_engine import evaluate_token


class RiskTests(unittest.TestCase):
    def test_unknown_security_not_safe(self):
        pair = {"chainId":"solana","priceUsd":"0.01",
                "liquidity":{"usd":100000},"volume":{"h24":20000}}
        r = evaluate_token(pair)
        self.assertIn("mint authority",r.missing_evidence)
        self.assertNotEqual(r.classification,"LOW")

    def test_low_liquidity_blocked(self):
        r = evaluate_token({"chainId":"solana","priceUsd":"0.1",
            "liquidity":{"usd":100},"volume":{"h24":10000}})
        self.assertFalse(r.eligible_for_paper)

    def test_mint_authority_blocked(self):
        r = evaluate_token({"chainId":"solana","priceUsd":"1",
            "liquidity":{"usd":100000},"volume":{"h24":10000}},
            token_security={"mint_authority_enabled":True})
        self.assertFalse(r.eligible_for_paper)

    def test_nan_price_blocked(self):
        r = evaluate_token({"chainId":"solana","priceUsd":"nan",
            "liquidity":{"usd":100000},"volume":{"h24":10000}})
        self.assertFalse(r.eligible_for_paper)


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = [1000]
        self.db_path = Path(self.tmp.name) / "paper.db"
        self.engine = PaperEngine(self.db_path,clock=lambda:self.t[0])

    def tearDown(self):
        self.tmp.cleanup()

    def test_entry_and_restart_persist(self):
        event = self.engine.enter("mint","COIN",1)
        self.assertIsNotNone(event)
        self.assertAlmostEqual(self.engine.cash(),18)
        reopened = PaperEngine(self.db_path)
        self.assertEqual(len(reopened.open_trades()),1)
        self.assertAlmostEqual(reopened.cash(),18)

    def test_max_position_and_duplicate(self):
        self.assertIsNone(self.engine.enter("a","A",1,5))
        self.assertIsNotNone(self.engine.enter("a","A",1))
        self.assertIsNone(self.engine.enter("a","A",1))
        self.assertEqual(len(self.engine.open_trades()),1)

    def test_exit_fees_and_no_double_close(self):
        self.engine.enter("a","A",1)
        events = self.engine.mark({"a":1.5})
        self.assertEqual(len(events),1)
        self.assertEqual(self.engine.mark({"a":1.5}),[])
        self.assertGreater(self.engine.cash(),18)
        self.assertEqual(self.engine.report()["closed_positions"],1)

    def test_missing_price_does_not_close(self):
        self.engine.enter("a","A",1)
        self.t[0] += 86400
        self.assertEqual(self.engine.mark({}),[])
        self.assertEqual(len(self.engine.open_trades()),1)

    def test_stop_loss(self):
        self.engine.enter("a","A",1)
        self.assertEqual(self.engine.mark({"a":0.7})[0].reason,"stop_loss")

    def test_max_open(self):
        for i in range(3):
            self.assertIsNotNone(self.engine.enter(str(i),"X",1))
        self.assertIsNone(self.engine.enter("new","X",1))


if __name__ == "__main__":
    unittest.main()
