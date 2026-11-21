"""Exact active transition owned by this version branch.

This is release data, not a request to discover or automatically choose a newer
version. Historical branches retain their own accepted implementation and pins.
"""
from pathlib import Path


SOURCE = 'v1.35.10'
TARGET = 'v1.36.6'
DIRECTORY = Path(__file__).resolve().parents[1] / TARGET
PINS_PATH = DIRECTORY / 'artifact-pins.json'
MATRIX_PATH = DIRECTORY / 'test-matrix.yaml'
RUN_ID = 'k8s-v13510-to-v1366'
