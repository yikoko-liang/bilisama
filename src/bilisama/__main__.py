"""Allow `python -m bilisama` alongside the console script."""

from bilisama.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
