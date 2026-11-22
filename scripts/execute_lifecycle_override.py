#!/usr/bin/env python3
"""CLI entrypoint alias delegating to `apply_lifecycle_override.main()`."""

from __future__ import annotations

import sys

from apply_lifecycle_override import main

if __name__ == "__main__":
    sys.exit(main())
