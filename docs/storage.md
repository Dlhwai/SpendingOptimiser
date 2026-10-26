# Local storage and recoverable older detail

Open the **Storage** tab under **Inspect saved data** for measured bytes, exact paths,
archive counts, a manual compression check and account-scoped month restoration.
There is no daemon, scheduler, cloud storage, extraction or AI involved.

## Three-month working default

The current **Asia/Singapore calendar month plus the preceding two** remain hot,
not a rolling 90-day window. For example, in May the working window starts March
1 at 00:00 Singapore time. Future-dated data is not eligible. Mail uses the same
Gmail receipt timestamp/month boundaries as Connect Gmail, not the untrusted Date
header. Financial months still use canonical transaction event dates.

All canonical ledger records, older revisions, observations, public evidence,
accounts/cards/rules and correction history remain ordinary uncompressed records
for **all** months. Historical statistics are not frozen snapshots: Overview uses
the existing `monthly_report` and canonical heads on every read. Late imports,
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
never at exactly that amount. Checks occur after successful imports, manual
reviews and saved-mail page/retry writes, plus the explicit storage-page action.
Page views do not compress, fetch mail, read `.env` or mutate the
archive. No startup/background scan runs. A database that grows while idle will
be measured on the storage page and processed on the next write/manual check.

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
or manual check can retry it even with **0 newly compressed payloads**. The UI
reports whether compaction completed and how many corrupt/unsupported payloads
were skipped by a manual check. If usage remains above the trigger, it says so
honestly. Nothing is purged and current working data is not evicted to reach 1 GB.

## Reading and restoring

Older monthly financial statistics and transaction detail need no restoration.
Saved mail can also read older compressed candidates: payloads are verified and
decoded locally in memory. **Restore saved mail month** in Saved mail or Storage
persists *all content versions* of that account's selected month back as text in
one transaction. This is separate from **Restore saved connection**, which reads
authorization from `.env`. Archive restoration makes no Google/model
requests and does not access credentials. Repeating it restores zero additional
versions. Disconnect still hides mail without removing it; reconnect the same
stable Google identity to read or restore it. Stale account forms are rejected.

Restoring older mail may increase storage. There is no configurable pinning/cache
platform: another write or explicit check above the trigger can compress it again.
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
Primary imports/reviews/mail pages commit **before** maintenance. A later SQLite
or filesystem maintenance failure preserves the successful operation and shows a
separate **Storage maintenance could not finish** warning, not **Request not
saved**. Do not replay the saved operation to retry maintenance: check disk space
and access, then use **Check storage / compress eligible detail** in Storage &
archives. A successful manual check clears the warning. Pre-commit write failures
still roll back the failed operation; earlier completed mail pages remain saved.
VACUUM runs only after compression commits, using SQLite's own transactional
recovery.

Corrupt archives produce an explicit error, not an empty result. Restoration
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
