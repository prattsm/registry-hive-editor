# Fixture matrix

No real user or operating-system hives are committed. Unit fixtures and backend doubles cover:

- supported, opaque, empty, malformed, overflow, and unknown value data;
- invalid and out-of-range FILETIME values with raw-evidence retention;
- case-insensitive key/value lookup and duplicate detection;
- transactional replacement, verification failure, delete failure, and rollback;
- atomic export failure and same-file/hard-link rejection;
- deep/wide traversal hot paths, search caps, cancellation, and stale-job handling; and
- disk-indexed comparison and streamed reports.

Redistributable integration fixtures may be generated from platform APIs. They must be synthetic
and cover clean create/open/edit/save/reopen behavior, corrupt/truncated input, uncommon value types,
and native backend interoperability. Never add evidence or hives copied from a real installation.
