"""
Root conftest.py — tells pytest that the project root is on sys.path,
so `from app.cleaning import ...` works without installing the package.
"""
import sys
import os

# Add the project root to sys.path so `app` is importable as a package
sys.path.insert(0, os.path.dirname(__file__))
