import tomllib
from pathlib import Path


def load_config(path="config.toml"):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    a = cfg["allocation"]
    if abs(a["crypto"] + a["nasdaq"] - 1.0) > 1e-9:
        raise ValueError("allocation.crypto + allocation.nasdaq must equal 1.0")
    if cfg["account"]["broker"] not in ("paper", "alpaca"):
        raise ValueError("account.broker must be 'paper' or 'alpaca'")
    if not cfg["crypto"]["symbols"]:
        raise ValueError("crypto.symbols is empty")
    return cfg


ROOT = Path(__file__).resolve().parent.parent
