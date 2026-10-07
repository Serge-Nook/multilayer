from pathlib import Path


def logo_path() -> Path:
    return Path(__file__).parent / "assets" / "multilayer.svg"
