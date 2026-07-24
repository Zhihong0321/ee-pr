# Local Data Mover Manager: R2 → pCloud

## 1. Objective

Build a localhost-only web dashboard that safely migrates Cloudflare R2 objects older than 90 days into a pCloud Public Folder, updates the corresponding PostgreSQL file URL, and then deletes the original R2 object.

The enforced sequence for every file is:

`Discover → Copy → Checksum verify → Public URL verify → PostgreSQL update → Reverify → R2 delete`

The application defaults to dry-run mode. A single-file test must pass before batch migration is enabled.

## 2. Application Architecture

- Implement a local Node.js/TypeScript backend with a browser dashboard bound only to `127.0.0.1`.
- Keep PostgreSQL, R2, and pCloud credentials in the local backend through environment variables. Never return credentials to the browser or place them in logs.
- Use a local SQLite database as a durable migration journal. Do not require changes to the production PostgreSQL schema.
- Separate discovery, migration, verification, database mutation, and deletion into explicit services with a shared state machine.
- Default to sequential processing. Batch concurrency remains disabled until the single-file live test and recovery tests pass.

### Dashboard sections

- **Connections:** Test PostgreSQL, R2, pCloud API, and unauthenticated Public Folder access.
- **Discovery:** Preview eligible objects and their matched PostgreSQL rows without changing anything.
- **Queue:** Select an individual file or an approved batch.
- **Active Job:** Show copy, checksum, public-access, database-update, and R2-deletion progress.
- **History:** Show completed, failed, conflicted, orphaned, and cleanup-pending items with receipts.
- **Settings:** Configure the 90-day threshold, approved R2 prefix, pCloud destination, SQL mapping, and safe batch limits.

## 3. Required Access and Configuration

### PostgreSQL

- Use a read-only credential during schema discovery and dry runs.
- Before a live migration, provide a separate narrowly scoped role with:
  - `SELECT` on the required table and columns.
  - `UPDATE` only on the approved file-URL column.
  - No permission to insert, delete, alter schemas, or update unrelated columns.
- Discover and lock the following mapping before implementation:
  - table name;
  - primary-key column;
  - current file-URL column;
  - relationship between the database row and R2 object key;
  - any tenant, ownership, or status predicates required in every query.

### Cloudflare R2

- Provide the account ID, bucket name, access-key ID, and secret.
- Scope credentials to list, read, head, and delete only the approved bucket or prefix.
- Use the S3-compatible API for listing, streaming, metadata checks, and deletion.

### pCloud

- Provide a pCloud OAuth access token and the correct API hostname for the account's US or EU data region.
- Use Public Folder ID `32517376417`.
- Configure the visible `filedn.com` Public Folder base URL.
- Use a dedicated production destination such as `Public/R2-Archive`.
- Use `Public/R2-Archive-Test` for proof-of-concept operations.
- A Public Folder link by itself is insufficient for uploading; the backend requires authenticated pCloud API access.

### Local secrets

- Load secrets from environment variables or a local ignored secrets file.
- Never persist secrets in SQLite, browser storage, job receipts, screenshots, or application logs.
- Redact database hosts, tokens, access keys, query parameters, and authorization headers from errors.

## 4. Local Migration Journal

Each migration item records:

- local migration ID;
- PostgreSQL table, primary key, and original URL;
- R2 bucket, object key, ETag, size, content type, and `LastModified`;
- pCloud folder ID, file ID, destination path, size, checksum, and public URL;
- current state, attempt count, timestamps, and bounded error details;
- database affected-row count and read-back result;
- public verification status and HTTP evidence;
- R2 deletion request and post-deletion verification.

### State machine

Normal states:

`discovered → copying → copied → verified → db_updated → deleting → completed`

Failure or review states:

- `failed`: a retryable or terminal operation failed before the database switch.
- `conflict`: the database row or pCloud destination no longer matches the discovered state.
- `pcloud_orphan`: upload succeeded but PostgreSQL was not updated.
- `cleanup_pending`: PostgreSQL points to pCloud, but R2 deletion failed.

State transitions must be durable and idempotent. Restarting the manager must reconcile external state before resuming.

## 5. Discovery Workflow

1. List R2 objects only within the approved bucket or prefix.
2. Calculate age from R2 `LastModified` in UTC.
3. Select only objects strictly older than 90 days.
4. Derive or look up the corresponding PostgreSQL record using the approved mapping.
5. Require exactly one matching database row.
6. Confirm the database URL still represents the same R2 object.
7. Capture the original database URL and R2 metadata in the local journal.
8. Derive a deterministic pCloud destination from the R2 object key.
9. Reject absolute paths, `..` traversal, invalid encoding, reserved names, and keys outside the approved prefix.
10. Show the proposed pCloud path, public URL, SQL target, and eventual R2 deletion in the dry-run preview.

Discovery must not upload, update PostgreSQL, or delete anything.

## 6. Per-File Migration Workflow

### Step 1: Preflight

- Re-read the PostgreSQL row and R2 object metadata.
- Confirm the database URL, R2 size, object key, and modification time still match discovery.
- Confirm the object is still older than 90 days.
- Stop with `conflict` if any protected value changed.

### Step 2: Copy to pCloud

- Stream the object from R2 directly into the configured pCloud folder without saving a complete temporary local copy.
- Preserve the logical relative path, filename, content type, and relevant modification time.
- Compute SHA-1 while streaming because pCloud exposes SHA-1 checksums in both account regions.
- Use a deterministic destination path so retries do not create duplicate files.
- If the destination already exists:
  - reuse it only when size and SHA-1 match;
  - otherwise stop with `conflict`; never overwrite automatically.

### Step 3: Verify the pCloud copy

- Call pCloud metadata/checksum APIs using the returned file ID.
- Require exact byte-size equality.
- Require exact SHA-1 equality.
- Record the pCloud file ID, checksum, and metadata in SQLite.
- Do not continue if either verification fails.

### Step 4: Verify public access

- Construct the permanent `filedn.com` URL from the configured Public Folder base URL and URL-encoded relative destination path.
- Validate the URL with a new HTTP client that sends no pCloud token, R2 credentials, application cookies, or authorization headers.
- Follow HTTPS redirects with a bounded redirect count.
- Attempt `HEAD`; if unsupported, issue a small ranged `GET`.
- Accept only an expected public response such as `200` or `206`.
- Confirm content length when the server provides it.
- For the initial small-file test, download the entire public file and compare its SHA-1.
- Do not update PostgreSQL or delete R2 if public verification fails.

### Step 5: Update PostgreSQL

- Begin a short transaction.
- Perform a compare-and-set update targeting the exact primary key:
  - update only the approved URL column;
  - require the current value to equal the originally discovered R2 URL;
  - set it to the verified pCloud public URL.
- Require exactly one affected row.
- Read the row back inside the transaction and confirm the exact pCloud URL.
- Commit only after the read-back succeeds.
- If zero or multiple rows are affected, roll back and mark `conflict`.

### Step 6: Reverify after the database switch

- Read the PostgreSQL row again using a fresh query.
- Repeat the unauthenticated public URL check.
- Do not delete R2 unless both checks pass.

### Step 7: Delete from R2

- Delete the exact bucket/key captured in the migration journal.
- Never derive the deletion target from the newly written pCloud URL.
- Confirm R2 no longer returns the object.
- Mark the item `completed` only after post-deletion confirmation.
- If deletion fails, retain the pCloud database URL and mark `cleanup_pending` for deletion-only retry.

## 7. Failure and Recovery Rules

- **R2 read failure:** no pCloud upload, database update, or deletion.
- **pCloud upload/checksum failure:** no database update and no R2 deletion.
- **Public URL failure:** no database update and no R2 deletion.
- **pCloud destination conflict:** do not overwrite; require manual review.
- **PostgreSQL compare-and-set failure:** leave R2 untouched and mark the uploaded pCloud copy as `pcloud_orphan` if newly created.
- **PostgreSQL update succeeds but R2 deletion fails:** retain the valid pCloud URL and retry only the original R2 deletion.
- **Crash before PostgreSQL update:** reconcile the pCloud destination; reuse a matching copy or stop on conflict.
- **Crash after PostgreSQL update:** re-read PostgreSQL and reverify pCloud before considering R2 deletion.
- **Crash after R2 deletion:** if PostgreSQL points to pCloud, public verification passes, and R2 is absent, mark completed.
- Never automatically restore an R2 URL after the R2 object has been deleted.

## 8. Safety Controls

- Start every session in dry-run mode.
- Require explicit confirmation to enable live mode.
- Require a second confirmation summarizing file count and total bytes before a batch that includes immediate deletion.
- Restrict all operations to an allowlisted R2 bucket/prefix and pCloud folder.
- Initially process exactly one file at a time.
- Use database compare-and-set updates to protect against concurrent application changes.
- Make every external operation idempotent.
- Limit retries with exponential backoff and record the final error.
- Provide pause and stop-after-current-file controls; never interrupt a file between database commit and deletion reconciliation.
- Export a JSON/CSV receipt containing identifiers and non-secret verification evidence.

## 9. Testing and Acceptance

### Phase A: Connection tests

- Confirm PostgreSQL read-only discovery.
- Verify the limited writer's permissions without changing a production row.
- Confirm R2 list, head, and read access within the approved prefix.
- Confirm R2 access is denied outside the approved scope where applicable.
- Upload and delete a generated object only inside `R2-Archive-Test`.
- Confirm its `filedn.com` URL works without authentication.

### Phase B: Dry-run discovery

- Discover objects older than 90 days.
- Show database matches and planned destinations without mutations.
- Detect missing rows, duplicate matches, malformed URLs, unsafe paths, destination conflicts, and ineligible object ages.

### Phase C: Non-destructive copy test

- Select one small, non-critical R2 object.
- Copy it into `R2-Archive-Test`.
- Verify pCloud API checksum and unauthenticated public access.
- Do not update PostgreSQL or delete the R2 original.
- Remove only the pCloud test copy after review.

### Phase D: Single live end-to-end test

- Use a disposable or explicitly approved PostgreSQL record and R2 object.
- Execute the complete copy, verification, database update, revalidation, and immediate R2 deletion sequence.
- Confirm the real web application can load the updated URL.
- Save a receipt with timestamps, identifiers, checksums, HTTP results, affected-row count, read-back result, and deletion confirmation.

### Phase E: Recovery tests

- Stop after pCloud upload and confirm safe resume.
- Simulate a PostgreSQL compare-and-set conflict.
- Simulate public URL failure.
- Simulate R2 deletion failure and confirm `cleanup_pending` recovery.
- Restart the local manager at each durable state and confirm reconciliation produces the correct next action.

### Phase F: Batch activation

- Keep batch mode disabled until all previous phases pass.
- Begin with a very small approved batch and sequential processing.
- Review every receipt and compare application behavior before increasing batch size.
- Add limited concurrency only after rate limits, duplicate handling, and recovery behavior are proven.

## 10. Acceptance Criteria

The manager is ready for controlled production use only when:

- no production mutation occurs during dry runs;
- every migrated file has matching R2-stream and pCloud SHA-1 checksums;
- the final pCloud URL is accessible without credentials;
- PostgreSQL updates affect exactly one intended row using compare-and-set semantics;
- R2 deletion occurs only after the committed database URL and public URL are reverified;
- interrupted migrations recover without duplicate uploads, incorrect URL updates, or unsafe deletions;
- every completed migration has a durable, secret-free audit receipt;
- the existing web application successfully serves the migrated file using the updated database URL.

## 11. Assumptions

- Migrated files are intended to be permanently public.
- The PostgreSQL URL column can store a `filedn.com` URL directly.
- Object age is determined by R2 `LastModified` in UTC.
- Immediate R2 deletion after successful database update and revalidation is intentional.
- The user accepts that immediate deletion removes the rollback copy.
- Public URL verification must succeed without pCloud credentials or browser cookies.
- A PostgreSQL read-only credential is sufficient only for discovery; the limited writer is mandatory for a live migration.
- The first implementation and test do not enable unrestricted batch processing.
