"""SQLite plumbing shared by every store: one connection helper and one migration runner.

Pragmas live in :mod:`.connect`; schemas are ordered steps keyed by ``PRAGMA user_version``
(:mod:`.migrations`), one module per schema (Architecture §6.1, §6.2).
"""
