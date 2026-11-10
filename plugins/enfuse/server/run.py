#!/usr/bin/env python3
"""Start enfuse-agent from the plugin folder without installing anything: python3 run.py serve"""
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from enfuse_agent.__main__ import main  # noqa: E402

sys.exit(main())
