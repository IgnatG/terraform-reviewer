"""Evaluation harness for the Terraform review agent.

Eval-only code. Kept out of ``src/`` so the per-PR runtime never imports
``agentevals``/``openevals``/``langsmith`` — those live in the ``eval`` optional
dependency group. Run with ``make eval`` (see :mod:`evals.run`).
"""
