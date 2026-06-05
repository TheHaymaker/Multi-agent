"""Specialist agents that propose code changes: PR coordination, quality, tests, perf, types.

They share two primitives in ``base``:
  * :class:`Finding` — a normalized, source-agnostic "here's something to improve".
  * :func:`plan_batches` — packs findings into **small, atomic change batches** (one draft PR
    each, under a line cap) so reviews stay easy and the blast radius of any breaking change
    stays low. The shared :func:`run_code_work` driver runs each batch through the Code
    Surgeon and gates the resulting draft PR through the PolicyGuard.
"""
