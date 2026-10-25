"""Synthetic storage walkthrough using the real desktop routes and fake dependencies."""

import argparse
import tempfile

import uvicorn
from monthly_fixture import monthly_document
from ui_preview import preview_app

from spending_optimiser.storage import GB


def storage_app(directory, *, remain_above=False):
    app = preview_app(directory)
    app.state.ledger.import_document(monthly_document().model_dump_json())
    service = app.state.gmail
    service.connection = service.oauth.connection()
    service.load_month("2025-02", 1, service.connection_id, "continue")
    # A disposable sparse, non-WAL sidecar exercises the actual decimal-GB
    # measurement without writing a gigabyte of fictional mail. No normal DB uses
    # this fixture. SQLite is in its default DELETE journal mode, not WAL mode.
    storage = app.state.storage
    sidecar = storage.paths[2]
    with sidecar.open("wb") as out:
        out.truncate(GB + 1_000_000)
    app.state.storage_result = storage.compress_if_needed()
    if not remain_above:
        sidecar.unlink(missing_ok=True)
    service.notice = "Synthetic saved mail compressed locally. No real mail or credentials used."
    service.notice_month = "2025-02"
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8898)
    parser.add_argument("--remain-above", action="store_true")
    args = parser.parse_args()
    if args.port == 8765 or not 1024 <= args.port <= 65535:
        parser.error("Use an unused loopback port from 1024-65535, never 8765")
    with tempfile.TemporaryDirectory(prefix="spending-storage-preview-") as directory:
        app = storage_app(directory, remain_above=args.remain_above)
        print(f"SYNTHETIC ONLY: http://127.0.0.1:{args.port}/storage?month=2025-02", flush=True)
        print(
            "Sparse synthetic sidecar tests logical-byte threshold. All state removed on exit.",
            flush=True,
        )
        uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
