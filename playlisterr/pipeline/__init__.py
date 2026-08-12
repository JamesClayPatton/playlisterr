"""The nightly pipeline, one module per step.

users -> history -> catalog -> profile -> pick -> publish, orchestrated by
run.py. Every module here is pure with respect to the outside world: they
take providers as arguments rather than constructing them, which is what
makes the whole pipeline runnable against fixtures.
"""
