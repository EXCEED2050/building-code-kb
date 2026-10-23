"""Project paths shared by the web app, ingestion and maintenance commands.

KB_HOME selects a data workspace when the package is installed elsewhere.
Relative environment overrides are always relative to that workspace, not cwd.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(os.environ.get("KB_HOME", Path(__file__).resolve().parents[2])).expanduser().resolve()
load_dotenv(ROOT / ".env", override=False)


def configured(name: str, default: str) -> Path:
    path = Path(os.environ.get(name) or default).expanduser()
    return path if path.is_absolute() else ROOT / path


DATA = ROOT / "data"
CATALOG_DIR = DATA / "catalog"
MANIFESTS = DATA / "manifests"
CATALOG = configured("KB_CATALOG", "data/catalog/catalog.csv")
CHUNKS = configured("KB_CHUNKS", "data/chunks")
SOURCES = DATA / "sources"
WORD_DIR = configured("KB_WORD_DIR", "data/sources/word")
MINERU_DIR = configured("KB_MINERU_DIR", "data/processed/mineru")
OCR_DIR = configured("KB_OCR_DIR", "data/processed/ocr")
VAR = ROOT / "var"
MODELS = configured("KB_MODELS_DIR", "var/models")
INDEX = configured("KB_INDEX_DIR", "var/index/chroma")
PAGE_CACHE = configured("KB_PAGE_CACHE", "var/cache/pages")
REVIEWS = VAR / "review"
QUEUES = VAR / "queues"
PUBLIC = ROOT / "examples/public"
CONFIG = ROOT / "config"
