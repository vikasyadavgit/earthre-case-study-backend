"""
SLA Monitoring — data cleaning logic.

Pure pandas, no AWS/Lambda knowledge — kept independently testable.
Each function does exactly one job; clean_dataframe() runs them in order.
"""
