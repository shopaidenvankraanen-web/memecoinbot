"""Static check of the network boundary and shipped source."""
import ast

from ultimate.storage import ROOT

FORBIDDEN_IMPORTS = {"socket", "urllib", "http", "requests", "aiohttp", "httpx",
                     "subprocess", "dotenv", "telegram", "solana", "solders"}
FORBIDDEN_CALLS = {"run_polling", "create_connection", "send_transaction", "sign_transaction",
                   "load_dotenv", "system", "Popen"}
TELEGRAM_IMPORTS = {
    "telegram.error": {"Conflict"},
    "telegram.ext": {"Application", "CommandHandler"},
    "telegram.request": {"HTTPXRequest"},
}


def host_nodes(tree):
    return {node for function in tree.body if isinstance(function, ast.FunctionDef)
            and function.name == "telegram_host" for node in ast.walk(function)}


def main():
    for path in sorted((ROOT / "ultimate").glob("*.py")):
        tree = ast.parse(path.read_bytes())
        host = host_nodes(tree) if path.name == "host.py" else set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(name.name.split(".")[0] not in FORBIDDEN_IMPORTS for name in node.names), path
            if isinstance(node, ast.ImportFrom) and not node.level and node.module:
                if path.name == "live.py" and node.module == "http.client":
                    assert {name.name for name in node.names} == {"HTTPException", "HTTPSConnection"}
                elif node in host and node.module in TELEGRAM_IMPORTS:
                    assert {name.name for name in node.names} == TELEGRAM_IMPORTS[node.module]
                else:
                    assert node.module.split(".")[0] not in FORBIDDEN_IMPORTS, path
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                if name == "run_polling" and node in host:
                    continue
                assert name not in FORBIDDEN_CALLS, path
    print("Network and polling isolation, signing, shell and dotenv AST review passed")


if __name__ == "__main__":
    main()
