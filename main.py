#!/usr/bin/env python3
"""Main entrypoint for the AI key-rotating proxy."""
import os
import sys

# Ensure root directory is in sys.path
_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

from ai_proxy.__main__ import main

if __name__ == "__main__":
    main()
