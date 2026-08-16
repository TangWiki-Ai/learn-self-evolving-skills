"""Thin CLI presentation boundary for Foundation diagnostics."""

from ses.foundation.doctor import build_parser, main

__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
