"""One command entry point; individual modules remain directly runnable."""

from __future__ import annotations

import argparse
import runpy
import sys

COMMANDS = {
    "search": "search.retrieve",
    "ask": "qa.answer",
    "index": "search.index",
    "catalog": "ingest.catalog",
    "chunk": "ingest.batch",
    "ocr": "ingest.ocr",
    "restore": "commands.restore_assets",
    "check-snapshot": "commands.check_snapshot",
    "check-reviews": "commands.check_reviews",
    "check-trial": "commands.check_trial",
    "build-pdfs": "commands.build_source_documents",
    "review": "commands.review",
    "models": "commands.get_models",
    "qr": "commands.qr",
    "queue": "commands.queue",
    "ocr-cloud": "commands.ocr_cloud",
    "ocr-status": "commands.ocrstat",
    "chunk-new": "commands.chunk_new",
    "export-public": "commands.export_public",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="建筑法规知识库命令")
    parser.add_argument("command", choices=COMMANDS)
    args = parser.parse_args(sys.argv[1:2])
    sys.argv = [f"building-kb {args.command}", *sys.argv[2:]]
    runpy.run_module("building_kb." + COMMANDS[args.command], run_name="__main__")


if __name__ == "__main__":
    main()
