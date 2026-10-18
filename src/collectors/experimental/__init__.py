"""Experimental / non-functional collector code, quarantined out of the
active collector registry (src.collectors.registry.CollectorRegistry).

Nothing under this package is imported by src.collectors.registry, so
nothing here can ever run as part of a real pipeline collection cycle —
config/sources.yaml has no way to reach it. Code lives here instead of being
deleted when it documents a real (if currently non-working) integration
attempt worth preserving for reference.
"""
