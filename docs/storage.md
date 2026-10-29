# Local storage and recoverable older detail

The app compresses older detail by itself, so nothing needs to be run for normal
use. **Saved mail > Recovery & developer tools > Storage diagnostics & lossless
recovery** shows measured bytes, exact paths, archive counts, an optional manual
check and restoration of saved mail for a period. There is no daemon, scheduler,
cloud storage or AI involved.

## Compression is automatic

- **When:** after each successful schema import (including the bundled example),
  manual review or correction, saved-mail page or failed-message retry, and once
  after each automatic processing batch (including partial failures), not after
  each transaction/revision within it. Other
  writes, such as saved AI snapshots, do not trigger it; the next write above or a
  manual check covers them. The check runs after the write is committed, in the
  request's worker thread and never on the web server's event loop.
- **What:** only if logical usage is strictly above 1,000,000,000 bytes, and only
  eligible older detail (see below). Below the trigger nothing is touched.
- **What it does not do:** it never runs when a page is opened (Storage included), at
  startup, on a timer or while the app is idle. A database that grows while idle is
  processed on the next write or manual check.
- **If it fails:** the write stays saved and a separate **Storage maintenance could not
  finish** warning appears. Fix disk space or access, then choose **Check storage now**.
- **Check storage now** in storage diagnostics is optional. It runs the same check on
  demand, for example to retry after a warning. Skipping it deletes nothing, and using
  it deletes nothing.

## Three-month working default

The current **Asia/Singapore calendar month plus the preceding two** remain hot,
not a rolling 90-day window. For example, in May the working window starts March
1 at 00:00 Singapore time. Future-dated data is not eligible. Mail uses the same
Gmail receipt timestamp/month boundaries as Connect Gmail, not the untrusted Date
header. Financial months still use canonical transaction event dates.

This window is a fixed storage policy. It is independent of the period you view or
load: choosing another month or range never moves it, and it never changes what is
compressed.

All canonical ledger records, older revisions, observations, public evidence,
accounts/cards/rules and correction history remain ordinary uncompressed records
for **all** months. Historical statistics are not frozen snapshots: Overview uses
`range_report` and canonical heads on every read. Late imports,
corrections, rejections and imported canonical revisions update old totals, retain
accepted/rejected/proposed distinctions, and keep integer minor units and separate
currencies. No competing ledger or approximate financial arithmetic is introduced.
This conservative default may leave substantial old ledger data uncompressed.

## Measurement and compression trigger

Application-managed usage is the sum of logical file sizes in **bytes** of:

- The selected ledger SQLite database.
- Its `-wal`, `-shm` and `-journal` sidecars, if present.

Absent paths count as zero. This is not filesystem allocated blocks or total disk
usage. All archives stay inside that same database. The `.env` file, application code, downloaded Google client JSON, user backups, browser
storage and OS temporary files are excluded. SQLite's temporary compaction space
is not part of this ongoing measurement and may require additional free disk.

Compression is considered **strictly above 1,000,000,000 bytes (1 decimal GB)**,
never at exactly that amount. Page views do not compress, fetch mail, read `.env` or
change the archive. The Storage page measures the current size every time it opens.

Eligible detail:

- Every version of saved mail whose Singapore receipt month is older than the
  working window, including superseded content, independently for each stable
  Google namespace. Current pointers, message/observation IDs, content digests,
  headers, MIME text parts/locators and warnings remain unchanged.
- Original canonical import receipts only when **all** transaction dates,
  observation timestamps, evidence retrieval dates and rule compilation dates
  are older than the window, there is at least one such date, and no transaction
  has an unknown/alternative date. The canonical ledger rows remain hot.

Saved mail and receipt observations cited by any hot/undated/alternative-date
transaction revision are protected too. Undated/unsupported/corrupt content and
payloads exceeding the supported 64 MiB uncompressed payload size are left intact,
not truncated or silently removed. Only replacements smaller than the UTF-8
original are committed. Retrieval checkpoints and unresolved-message IDs are
never compressed or evicted.

Compression is **not deletion, encryption or a storage cap**. The app attempts all
eligible records, then runs SQLite `VACUUM` if it compressed any payloads or
reclaimable free pages remain. Compaction can fail for insufficient disk space or
locks; committed compressed content remains usable, but physical file size may
not fall yet. After failed or interrupted compaction, a later above-trigger write
or manual check can retry it even with **0 newly compressed payloads**. A manual
check reports whether compaction completed and how many corrupt or unsupported
payloads were skipped. If usage remains above the trigger, the page says so
honestly. Nothing is purged and current working data is not evicted to reach 1 GB.

## Reading and restoring

Older monthly financial statistics and transaction detail need no restoration.
Saved mail can also read older compressed candidates: payloads are verified and
decoded locally in memory. **Restore saved mail** in Saved mail or Storage persists
*all content versions* received in that account's selected period (a day, a range up
to three calendar months, or a month) back as text in one transaction. This is separate from **Restore saved connection**, which reads
authorization from `.env`. Archive restoration makes no Google/model
requests and does not access credentials. Repeating it restores zero additional
versions. Disconnect still hides mail without removing it; reconnect the same
stable Google identity to read or restore it. Stale account forms are rejected.

Restoring older mail may increase storage. There is no configurable pinning/cache
platform: the next write or manual check above the trigger compresses it again.
`Ledger.import_snapshot(digest)` reads the exact canonical imported receipt whether
stored as text or compressed. Whitespace/key ordering of the originally supplied
JSON was already normalized at import and is not recovered by compression.

## Safety and migration

SQLite database format 1 upgrades transactionally and additively to format 2;
JSON schema remains `1.0`. Existing ledger/history/mail/checkpoints are not
rewritten by that upgrade. A nullable `mail_versions.receipt_month` column keeps
selected-month reads independent of unrelated older compressed content; legacy
text rows are filtered locally until month metadata is saved during compression.
Format 2 allows TEXT or compressed BLOB in `imports.document` and
`mail_versions.payload`. Older app binaries reject format 2 rather than
misinterpreting compressed content. Use the current app to read an upgraded DB;
there is no downgrade operation. Back up privately before upgrading real data.

BLOB format: `SOZ1`, big-endian unsigned 64-bit UTF-8 byte length, 32-byte SHA-256,
then a zlib stream. Packing is verified against the full original before any row
replacement. Reads check the supported format, bounded expansion, exact length,
stream completion, no trailing bytes, SHA-256 and UTF-8. IDs, origins and ledger
rows are never replaced by archive references.

Each compression batch and each restoration is a separate `BEGIN IMMEDIATE`
transaction: interruption commits either all replacements or none, with SQLite
journal recovery. Failure of a compression write cannot delete the only original.
Primary imports/reviews/mail pages/automatic transactions commit **before** maintenance. A later SQLite
or filesystem maintenance failure preserves the successful operation and shows a
separate **Storage maintenance could not finish** warning, not **Request not
saved**. Do not replay the saved operation to retry maintenance: check disk space
and access, then choose **Check storage now** in Storage. A successful manual check
clears the warning. Pre-commit write failures still roll back the failed operation;
earlier completed mail pages remain saved.
VACUUM runs only after compression commits, using SQLite's own transactional
recovery.

Saved-mail and period views flag unreadable or identity-invalid messages while
keeping other saved data usable. Compressed-version counts omit unreadable payloads;
explicit source-version reads fail instead of returning empty source text. Restoration
rolls back instead of discarding an unreadable version; other accounts and the
uncompressed ledger remain usable. A corrupt cold import does not block monthly
calculations. Keep the damaged DB for recovery and restore a known-good backup;
there is no automatic repair, overwrite, deletion or backup mechanism. To back up,
stop the service and copy the DB and any existing SQLite sidecars privately.
Compression is not application-level encryption: disk, files, backups, process
memory and local OS access still require normal protection.

## Reproducible synthetic proof

```sh
uv run --locked pytest tests/test_storage.py -q
uv run --locked python examples/storage_preview.py --port 8898 --remain-above
```

Use an **unused** loopback port, never 8765. Open
`http://127.0.0.1:8898/storage?month=2025-02`, inspect the above-trigger warning,
then Saved mail for February 2025 and restore the older versions. The fixture uses
the real desktop routes, fake Gmail/OAuth/credential dependencies, fictional
multi-currency ledger history and a disposable database. A sparse synthetic
**non-WAL** sidecar contributes over a decimal GB of logical bytes without
allocating a GB of mail. This is a threshold fixture, not a claim about real mail
compression ratios. Without `--remain-above` that fixture sidecar is removed after
seeding compression. Ctrl-C removes all disposable state. No default DB, real
credentials/mail, Google calls or other preview is used.
