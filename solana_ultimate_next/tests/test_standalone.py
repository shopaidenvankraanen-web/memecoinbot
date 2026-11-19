"""Offline verification of the single-file distribution."""
import ast
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import testbot_complete as bot
from tests.review import FORBIDDEN_CALLS, FORBIDDEN_IMPORTS, TELEGRAM_IMPORTS, host_nodes
from tests.test_offline import LEGACY, NOW, quote
from ultimate.storage import MIGRATIONS, Store


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory(prefix=".test-standalone-", dir=bot.ROOT)
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.script = self.work / "testbot_complete.py"
        shutil.copyfile(bot.ROOT / self.script.name, self.script)

    def cli(self, *args, isolated_code=None):
        command = [sys.executable, "-IB", str(self.script)]
        if isolated_code is not None:
            command = [sys.executable, "-IB", "-c", isolated_code, str(self.script)]
        result = subprocess.run([*command, *map(str, args)], cwd=self.work,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_syntax_import_and_safety_boundary(self):
        source = self.script.read_bytes()
        compile(source, str(self.script), "exec")
        tree = ast.parse(source)
        host = host_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                if node in host and node.module in TELEGRAM_IMPORTS:
                    self.assertEqual({item.name for item in node.names},
                                     TELEGRAM_IMPORTS[node.module])
                    continue
                self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)
                if node.module == "http.client":
                    self.assertEqual({item.name for item in node.names},
                                     {"HTTPException", "HTTPSConnection"})
                else:
                    self.assertNotIn(node.module.split(".")[0], FORBIDDEN_IMPORTS)
            elif isinstance(node, ast.Import):
                for item in node.names:
                    self.assertIn(item.name, sys.stdlib_module_names)
                    self.assertNotIn(item.name.split(".")[0], FORBIDDEN_IMPORTS)
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                if name == "run_polling" and node in host:
                    continue
                self.assertNotIn(name, FORBIDDEN_CALLS)
        self.cli(isolated_code="""
import runpy
import sys

def reject_side_effects(event, args):
    if event.startswith(("socket.", "sqlite3.connect", "subprocess.")):
        raise AssertionError(event)

sys.addaudithook(reject_side_effects)
runpy.run_path(sys.argv[1])
""")
        self.assertEqual(list(self.work.iterdir()), [self.script])

    def test_cli_in_clean_folder(self):
        for filename in ("capture.json", "frame.json", "telegram.json"):
            shutil.copyfile(bot.ROOT / "examples" / filename, self.work / filename)
        # Run the copied CLI with a process-wide network guard. Asyncio's local
        # socketpair remains available, but connections and DNS are blocked.
        def offline(*args):
            return self.cli(*args, isolated_code="""
import runpy
import sys

def offline_only(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise AssertionError(event)

sys.addaudithook(offline_only)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
""")

        status = json.loads(offline("status"))
        self.assertEqual(status["database"]["integrity"], ["ok"])
        self.assertEqual(status["paper"]["cash_cents"], 2000)
        self.assertTrue((self.work / "runtime/testbot-paper.sqlite").is_file())
        self.assertTrue(json.loads(offline("ingest", "capture.json")))
        self.assertTrue(json.loads(offline("replay")))
        self.assertEqual(set(json.loads(offline("command", "frame.json", "/help"))),
                         set(LEGACY) | {"pause", "resume"})
        self.assertIn("valuation", json.loads(offline(
            "telegram-replay", "telegram.json", "--user-id", 42)))
        self.assertIn("risk_at_entry", json.loads(offline("paper-analysis")))
        self.assertIsInstance(json.loads(offline("evaluate", 1700003600)), dict)
        self.assertIn("portfolios", json.loads(offline("backtest", 1700003600)))
        backup = offline("backup").strip()
        recovered = json.loads(offline("recover", backup, "recovered.sqlite"))
        self.assertEqual(recovered["integrity"], ["ok"])
        self.assertEqual(recovered["foreign_key_violations"], 0)
        self.assertEqual(recovered["version"], len(MIGRATIONS))

    def test_database_compatibility_with_modular_versions(self):
        self.assertEqual(bot.MIGRATIONS, MIGRATIONS)
        for version in range(1, len(MIGRATIONS) + 1):
            with self.subTest(version=version):
                path = self.work / f"version-{version}.sqlite"
                with closing(sqlite3.connect(path)) as db, db:
                    for migration in MIGRATIONS[:version]:
                        for statement in migration:
                            db.execute(statement)
                    db.execute("UPDATE account SET cents=1800")
                    db.execute(f"PRAGMA user_version={version}")
                store = bot.Store(path)
                self.assertEqual(store.status()["version"], len(MIGRATIONS))
                self.assertEqual(store.status()["integrity"], ["ok"])
                self.assertEqual(bot.Paper(store).report()["cash_cents"], 1800)
                self.assertIsNotNone(bot.Paper(store).enter(quote(), NOW))
                self.assertEqual(Store(path).rows("SELECT stake FROM positions"), [{"stake": 200}])
                self.assertEqual(store.status()["foreign_key_violations"], 0)

    def test_four_euro_stake_limit(self):
        store = bot.Store(self.work / "limit.sqlite")
        paper = bot.Paper(store)
        with self.assertRaises(ValueError):
            bot.Policy(stake=401)
        self.assertIsNone(paper.enter(quote(), NOW, 401))
        self.assertEqual(paper.report()["cash_cents"], 2000)
        self.assertEqual(store.rows("SELECT * FROM audit"), [])
        self.assertIsNotNone(paper.enter(quote(), NOW, 400))
        self.assertEqual(store.rows("SELECT stake,entry_fee FROM positions"),
                         [{"stake": 400, "entry_fee": 4}])
        with self.assertRaises(sqlite3.IntegrityError):
            with store.transaction(write=True) as db:
                db.execute("UPDATE positions SET stake=401")
        self.assertEqual(store.rows("SELECT stake FROM positions"), [{"stake": 400}])


if __name__ == "__main__":
    unittest.main()
