"""Probe scripts executed by the sidecar interpreter, never imported here.

Each writes one JSON object to stdout and always exits 0, so a broken aihf
install surfaces as ``{"ok": false, "error": …}`` rather than a traceback
BharatQuant has to parse.
"""
