"""
Root conftest.py – makes the project root available on sys.path so all
test modules can import `agent`, `auth`, `database`, etc. without
needing their own sys.path manipulation.
"""
import sys
import os

# Ensure the project root is always on the path for every test session
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
