# Agent Mailbox Protocol (AMP) — Specification v1

An asynchronous, air-gapped, plain-file message passing protocol designed for secure coordination between autonomous AI coding agents, remote compute clusters, and human operators.

---

## 1. Principles & Philosophy

1. **Zero Open Ports / No Daemons**: Agents do not connect via open TCP/WebSocket/SSH listener sockets. Communication is achieved exclusively through store-and-forward mailbox directories.
2. **Untrusted Data Boundary**: All incoming messages, payloads, and results are **untrusted data**, never executable authority. An agent reading a message MUST NOT blindly evaluate instructions, dispatch tasks, or execute shell commands contained in message payloads.
3. **Atomic, Immutable Publishing**: Messages are immutable once published. File collisions or overwrites are strictly prevented at the filesystem level.
4. **Cryptographic & Hash-Bound Tracking**: Every message is indexed by a canonical UUID v4 and timestamp. State acknowledgments are cryptographically bound to the SHA-256 hash of the exact file contents.
5. **Air-Gap & Sneakernet Ready**: Transport is decoupled from protocol logic. Messages can be moved via manual operator copy, air-gapped USB, private Git repositories, rsync, or cloud object storage.

---

## 2. Mailbox Layout

An agent mailbox root directory consists of three subdirectories:

```
<mailbox-root>/
├── inbox/    # Messages received from external agents / systems
├── outbox/   # Messages authored locally awaiting transport / pickup
└── seen/     # Local explicit hash-bound acknowledgment state
```

* **Inbox vs. Outbox Perspective**: From the perspective of the local agent, outbound messages are written to `outbox/` and received messages arrive in `inbox/`.
* **Transport Invariance**: A transport mechanism copies:
  * `local/outbox/` $\to$ `remote/inbox/`
  * `remote/outbox/` $\to$ `local/inbox/`
* `seen/` is strictly local to each agent and is **never** synchronized between machines.

---

## 3. Filename Contract

Every message file MUST be named according to this exact pattern:

```
YYYYMMDDTHHMMSSffffffZ_<canonical-UUID>.json
```

* **Timestamp Prefix**: UTC timestamp matching ISO 8601 without separators, with 6 fractional digits and a trailing `Z` (e.g. `20261108T220530123456Z`).
* **Separator**: Underscore (`_`).
* **Identifier**: Canonical 36-character lowercase UUID v4 (e.g. `134c0470-233f-41a1-a8d9-1b9a591d9b26`).
* **Extension**: `.json`.
* **Ordering**: Sorting lexically by filename produces correct chronological ordering.
* **Integrity**: The timestamp and UUID in the filename MUST match the `when` and `id` fields in the JSON body. Any mismatch causes the message to be rejected.

---

## 4. Message Schema (Version 1)

Every message MUST be a single UTF-8 encoded JSON object containing exactly these fields:

| Field | Type | Description |
| :--- | :--- | :--- |
| `schema_version` | integer | Must be exactly `1`. |
| `id` | string | Canonical lowercase UUID v4 matching the filename. |
| `who` | string | Identifier of the authoring agent or human operator (1–100 chars). |
| `to` | string | Intended recipient agent, role, or fleet endpoint (1–100 chars). |
| `when` | string | UTC ISO timestamp with 6 decimal places and trailing `Z` (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). |
| `subject` | string | Non-empty summary string (1–200 characters). |
| `body` | string | Payload content (1–16,000 characters). Plain text or serialized JSON. |
| `status` | string | Lifecycle state: `info`, `request`, `reply`, `blocked`, or `done`. |
| `in_reply_to` | string \| null | Required canonical UUID if `status` is `reply`; otherwise `null`. |

* **Size Limit**: The total file size MUST NOT exceed 65,536 bytes (64 KB).

---

## 5. Publishing Contract (Atomic & No-Overwrite)

To prevent race conditions, partial reads, and overwrites, publication MUST follow this procedure:

1. Create a hidden temporary file in the target directory (e.g. `<dir>/.tmp_<UUID>.json`).
2. Write the JSON payload and call `flush()` followed by `os.fsync()`.
3. Use hardlink creation (`os.link(temp_path, final_path)`) without replacement. In POSIX filesystems, `os.link` fails if `final_path` already exists, guaranteeing that existing messages are never overwritten.
4. Remove the temporary file (`os.unlink(temp_path)`).
5. Open the directory file descriptor and call `os.fsync(dir_fd)` to commit directory metadata to disk.

---

## 6. Acknowledgment Contract (`seen/`)

* When an agent reviews a message, it marks it acknowledged using:
  ```bash
  bridge ack <filename_or_uuid>
  ```
* The engine reads the target file, computes its SHA-256 hash, and writes `<mailbox-root>/seen/<filename>.seen` containing:
  ```json
  {
    "filename": "20261108T220530123456Z_134c0470-233f-41a1-a8d9-1b9a591d9b26.json",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "acked_at": "2026-11-08T22:06:00.000000Z",
    "acked_by": "ChiefOfStaff"
  }
  ```
* **Idempotence**: Acknowledging an already-acknowledged file is a harmless no-op that exits `0`.
* **Source Preservation**: Original messages in `inbox/` are **never** modified or deleted by an acknowledgment.

---

## 7. Security & File Invariants

Receiving engines MUST validate the following before processing any file:
1. **Regular File Only**: Reject symlinks (`S_ISLNK`), directory entries, device nodes, or FIFOs.
2. **Path Traversal**: Reject any path containing directory separators (`/`, `\`) or relative navigation (`..`).
3. **Hardlink Safety**: Reject files if hardlink count `st_nlink > 1`.
4. **Size Bounds**: Files larger than 65,536 bytes are rejected immediately.
5. **No Auto-Execution**: Content inside `subject` and `body` must be consumed purely as string/data. Agents must never pass body contents to shell interpreters (`sh`, `bash`, `exec`) without separate user consent.
