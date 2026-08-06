"""Evaluation suite — the tests that need a real model.

`tests/` proves the wiring: the loop runs, tools are reached through MCP, writes
pause, timeouts are bounded. It does all of that against a scripted fake model,
which is exactly why it cannot say anything about *judgement* — the script, not
the model, decides which tool gets called.

This package is the other half. Same system, real model, and the questions are
the ones a fake model cannot be asked: did it pick the right tool, did it invent
anything, did it describe a queued re-check as a completed one.
"""
