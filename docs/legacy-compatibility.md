# Legacy compatibility boundary

BrainCell's normal runtime is Project-only. Pools are live, named ULID
memberships used for explicit read-only Search and Recall; they never contain a
database or copied memory.

The recovery implementation retains only these legacy-compatible inputs:

- `braincell.legacy_recovery` reads the retired `global/braincell.db`, including
  old `pooled_from` provenance, and copies explicitly approved rows into Project
  databases. Apply copies from the same immutable snapshot that produced the
  approved digest and holds the destination mutation lock across preparation,
  backup, copy, verification, and failure cleanup.
- The schema's legacy `pooled_from` columns and the normal Project registry are
  used to route eligible rows. A legacy family catalog is discovered for an
  operator's inventory only; it is not used to attribute or copy rows.

Audit result: the old family/global federation, materialized-pool, and global
database helpers are not called by legacy recovery. They are retired
compatibility residue, including old tests that assert `--federate`, Family
scope, or global-brain behavior. They must not be called by new Project or Pool
workflows, and should be removed only as a separately reviewed retirement after
those stale tests are replaced or removed.

Do not remove recovery's schema/registry dependencies until the command and its
fixture coverage are intentionally retired together. User-facing surfaces must
use Project, Pool, Add to Pool, Search Pool, Recall from Pool, and Decouple from
Pool as defined in `NAMINGS.md`.

Refused preflight attempts remove their unneeded temporary source snapshots.
Accepted recovery keeps its source snapshot and any existing-destination backup
for rollback evidence. `braincell storage --backup-root <directory>` can include
an external recovery directory in a read-only retention report.
