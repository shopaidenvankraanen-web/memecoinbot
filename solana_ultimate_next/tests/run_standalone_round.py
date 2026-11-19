"""Run both implementations' regression tests with fresh workspace copies."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
NETWORK_GUARD = """
import runpy
import sys

def offline_only(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise AssertionError(event)

sys.addaudithook(offline_only)
sys.path.insert(0, ".")
runpy.run_module(sys.argv.pop(1), run_name="__main__", alter_sys=True)
"""


def run(directory, module, *args):
    subprocess.run([sys.executable, "-IB", "-c", NETWORK_GUARD, module, *args],
                   cwd=directory, check=True)


def retarget(source):
    """Bind suite imports and mock targets to their single-file equivalents."""
    source = re.sub(r"^from ultimate import (analytics)(, backtest)?$",
                    lambda match: "import testbot_complete as analytics" +
                    ("\nimport testbot_complete as backtest" if match[2] else ""),
                    source, flags=re.MULTILINE)
    source = re.sub(r"^from ultimate\.\w+ import ", "from testbot_complete import ",
                    source, flags=re.MULTILINE)
    return re.sub(r'(")ultimate\.(?:live|host|__main__)\.', r"\1testbot_complete.", source)


def main():
    with TemporaryDirectory(prefix=".test-round-", dir=ROOT) as directory:
        reference = Path(directory) / "reference"
        standalone = Path(directory) / "standalone"
        for destination in (reference, standalone):
            tests = destination / "tests"
            tests.mkdir(parents=True)
            shutil.copyfile(ROOT / "testbot_complete.py", destination / "testbot_complete.py")
            (tests / "__init__.py").touch()
            for name in ("test_offline.py", "test_phase.py", "test_host.py"):
                source = (ROOT / "tests" / name).read_text(encoding="utf-8")
                (tests / name).write_text(
                    retarget(source) if destination == standalone else source, encoding="utf-8")
        shutil.copytree(ROOT / "ultimate", reference / "ultimate",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "examples", reference / "examples")
        for name in ("review.py", "test_standalone.py"):
            shutil.copyfile(ROOT / "tests" / name, reference / "tests" / name)

        print("Modular reference, standalone distribution and safety review", flush=True)
        run(reference, "tests.review")
        run(reference, "unittest", "discover", "-v")
        print("Existing regression suite against the standalone implementation", flush=True)
        run(standalone, "unittest", "discover", "-v")


if __name__ == "__main__":
    main()
