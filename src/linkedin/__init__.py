"""LinkedIn background scanner package.

Producer-buffer-consumer architecture:
  LinkedInScanner  → producer  (runs daily on schedule)
  linkedin_staging → buffer    (persistent SQLite inbox)
  run_pipeline()   → consumer  (snapshotted at pipeline start)
"""
