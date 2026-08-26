#!/usr/bin/env python3
"""Launcher for ytlib.

Running `python3 ytlib.py <cmd>` makes ytlib the __main__ module, and CPython
never reads or writes a .pyc for __main__ - so its 130 KB of source was parsed
and compiled on every single invocation, about 20ms each time. Importing it
instead goes through the normal machinery, which does use __pycache__.

Measured over 150 interleaved paired runs of `ui`: 47.8ms median as a script
against 28.1ms through here, with the launcher never once slower.
"""
import sys

from ytlib import main

sys.exit(main(sys.argv[1:]))
