#!/usr/bin/env python3
"""
Agent Mailbox Protocol (AMP) — Reference Implementation & CLI Engine
Zero-dependency, asynchronous, file-based message passing for AI agents.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
MAX_FILE_SIZE = 65536
MAX_SUBJECT_LEN = 200
MAX_BODY_LEN = 16000
MAX_IDENT_LEN = 100

UUID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FILENAME_REGEX = re.compile(r"^(\d{8}T\d{6}\d{6}Z)_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.json$")
ISO_TS_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
VALID_STATUSES = {"info", "request", "reply", "blocked", "done"}


class MailboxError(Exception):
    """Base exception for mailbox violations and protocol errors."""
    pass


def get_default_identity() -> str:
    return os.environ.get("AGENT_IDENTITY", os.environ.get("USER", "agent"))


def get_default_root() -> str:
    return os.environ.get("AGENT_MAILBOX_ROOT", "./mailbox")


def format_utc_now() -> Tuple[str, str]:
    now = datetime.datetime.now(datetime.timezone.utc)
    iso_str = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    fname_str = now.strftime("%Y%m%dT%H%M%S%fZ")
    return iso_str, fname_str


def fname_ts_to_iso(fname_ts: str) -> str:
    # 20261108T140530123456Z -> 2026-11-08T14:05:30.123456Z
    return f"{fname_ts[0:4]}-{fname_ts[4:6]}-{fname_ts[6:8]}T{fname_ts[9:11]}:{fname_ts[11:13]}:{fname_ts[13:15]}.{fname_ts[15:21]}Z"


def ensure_mailbox_structure(root_path: str) -> Tuple[Path, Path, Path]:
    root = Path(root_path).resolve()
    inbox = root / "inbox"
    outbox = root / "outbox"
    seen = root / "seen"
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    seen.mkdir(parents=True, exist_ok=True)
    return inbox, outbox, seen


def atomic_publish_file(dest_dir: Path, filename: str, content_bytes: bytes) -> Path:
    if len(content_bytes) > MAX_FILE_SIZE:
        raise MailboxError(f"Content exceeds maximum allowed size ({len(content_bytes)} > {MAX_FILE_SIZE} bytes)")

    final_path = dest_dir / filename
    if final_path.exists():
        raise MailboxError(f"Destination file already exists (no overwrite permitted): {final_path}")

    tmp_path = dest_dir / f".tmp_{uuid.uuid4().hex}.json"

    try:
        with open(tmp_path, "wb") as f:
            f.write(content_bytes)
            f.flush()
            os.fsync(f.fileno())

        # os.link fails if final_path exists on POSIX, ensuring atomic no-overwrite
        os.link(tmp_path, final_path)
    finally:
        if tmp_path.exists():
            os.unlink(tmp_path)

    # Sync directory metadata
    dir_fd = os.open(str(dest_dir), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    return final_path


def validate_message_dict(data: Dict[str, Any], expected_id: Optional[str] = None, expected_ts: Optional[str] = None) -> None:
    expected_fields = {"schema_version", "id", "who", "to", "when", "subject", "body", "status", "in_reply_to"}
    actual_fields = set(data.keys())
    if actual_fields != expected_fields:
        missing = expected_fields - actual_fields
        extra = actual_fields - expected_fields
        raise MailboxError(f"Schema mismatch: missing {missing or 'none'}, extra {extra or 'none'}")

    if data["schema_version"] != SCHEMA_VERSION:
        raise MailboxError(f"Unsupported schema_version: {data['schema_version']} (expected {SCHEMA_VERSION})")

    msg_id = data["id"]
    if not isinstance(msg_id, str) or not UUID_REGEX.match(msg_id):
        raise MailboxError(f"Invalid id UUID: {msg_id}")
    if expected_id and msg_id != expected_id:
        raise MailboxError(f"Message ID ({msg_id}) does not match filename UUID ({expected_id})")

    for fld in ("who", "to"):
        val = data[fld]
        if not isinstance(val, str) or len(val) < 1 or len(val) > MAX_IDENT_LEN:
            raise MailboxError(f"Invalid '{fld}': must be non-empty string <= {MAX_IDENT_LEN} chars")

    when = data["when"]
    if not isinstance(when, str) or not ISO_TS_REGEX.match(when):
        raise MailboxError(f"Invalid 'when' timestamp format: {when} (must be YYYY-MM-DDTHH:MM:SS.ffffffZ)")
    if expected_ts and when != expected_ts:
        raise MailboxError(f"Message timestamp ({when}) does not match filename timestamp ({expected_ts})")

    subject = data["subject"]
    if not isinstance(subject, str) or len(subject) < 1 or len(subject) > MAX_SUBJECT_LEN:
        raise MailboxError(f"Invalid 'subject': must be 1-{MAX_SUBJECT_LEN} chars")

    body = data["body"]
    if not isinstance(body, str) or len(body) < 1 or len(body) > MAX_BODY_LEN:
        raise MailboxError(f"Invalid 'body': must be 1-{MAX_BODY_LEN} chars")

    status = data["status"]
    if status not in VALID_STATUSES:
        raise MailboxError(f"Invalid 'status': {status} (expected one of {VALID_STATUSES})")

    in_reply_to = data["in_reply_to"]
    if status == "reply":
        if not isinstance(in_reply_to, str) or not UUID_REGEX.match(in_reply_to):
            raise MailboxError(f"Status is 'reply', but 'in_reply_to' is not a valid UUID: {in_reply_to}")
    else:
        if in_reply_to is not None:
            if not isinstance(in_reply_to, str) or not UUID_REGEX.match(in_reply_to):
                raise MailboxError(f"Invalid 'in_reply_to': must be null or canonical UUID")


def safe_read_file(file_path: Path) -> Tuple[Dict[str, Any], bytes]:
    # Check traversal
    if ".." in file_path.parts:
        raise MailboxError(f"Path traversal detected in file path: {file_path}")

    # lstat for symlink check
    try:
        st = os.lstat(file_path)
    except FileNotFoundError:
        raise MailboxError(f"File not found: {file_path}")

    if stat.S_ISLNK(st.st_mode):
        raise MailboxError(f"Security violation: symlinks are strictly rejected ({file_path})")

    if not stat.S_ISREG(st.st_mode):
        raise MailboxError(f"Security violation: only regular files allowed ({file_path})")

    if st.st_size > MAX_FILE_SIZE:
        raise MailboxError(f"File exceeds maximum size ({st.st_size} > {MAX_FILE_SIZE} bytes)")

    # Validate filename pattern
    fn_match = FILENAME_REGEX.match(file_path.name)
    if not fn_match:
        raise MailboxError(f"Filename does not match contract YYYYMMDDTHHMMSSffffffZ_<UUID>.json: {file_path.name}")

    fn_ts, fn_id = fn_match.groups()
    expected_iso_ts = fname_ts_to_iso(fn_ts)

    with open(file_path, "rb") as f:
        raw_bytes = f.read()

    try:
        text = raw_bytes.decode("utf-8")
        data = json.loads(text)
    except UnicodeDecodeError as e:
        raise MailboxError(f"File is not valid UTF-8: {e}")
    except json.JSONDecodeError as e:
        raise MailboxError(f"Malformed JSON: {e}")

    validate_message_dict(data, expected_id=fn_id, expected_ts=expected_iso_ts)
    return data, raw_bytes


def find_message_file(search_dirs: List[Path], query: str) -> Path:
    # Query can be full filename, basename, or UUID
    clean_query = os.path.basename(query).strip()
    for d in search_dirs:
        if not d.exists():
            continue
        # Direct filename check
        target = d / clean_query
        if target.is_file():
            return target
        # Check by UUID suffix
        for item in d.glob("*.json"):
            if clean_query in item.name:
                return item
    raise MailboxError(f"Message '{query}' not found in {search_dirs}")


# --- CLI Commands ---

def cmd_init(args: argparse.Namespace) -> int:
    inbox, outbox, seen = ensure_mailbox_structure(args.root)
    print(f"Initialized agent mailbox at: {args.root}")
    print(f"  Inbox : {inbox}")
    print(f"  Outbox: {outbox}")
    print(f"  Seen  : {seen}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    _, outbox, _ = ensure_mailbox_structure(args.root)
    dest_dir = outbox if not args.inbox else (Path(args.root).resolve() / "inbox")

    msg_id = str(uuid.uuid4())
    iso_ts, fname_ts = format_utc_now()
    filename = f"{fname_ts}_{msg_id}.json"

    body = args.body
    if body == "-" or not body:
        body = sys.stdin.read().strip()
    if not body:
        print("Error: Message body cannot be empty", file=sys.stderr)
        return 1

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": msg_id,
        "who": args.who,
        "to": args.to,
        "when": iso_ts,
        "subject": args.subject,
        "body": body,
        "status": args.status,
        "in_reply_to": args.in_reply_to
    }

    validate_message_dict(payload)
    content_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    saved_path = atomic_publish_file(dest_dir, filename, content_bytes)

    if args.json:
        print(json.dumps({"path": str(saved_path), "filename": filename, "payload": payload}))
    else:
        print(f"Published message {msg_id} -> {saved_path.name}")
        print(f"  To     : {payload['to']}")
        print(f"  Subject: {payload['subject']}")
        print(f"  Status : {payload['status']}")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    inbox, outbox, seen = ensure_mailbox_structure(args.root)
    target_dir = inbox if not args.outbox else outbox

    files = sorted([f for f in target_dir.glob("*.json") if not f.name.startswith(".tmp")])
    results = []

    for f in files:
        seen_file = seen / f"{f.name}.seen"
        is_seen = seen_file.exists()

        if args.unseen_only and is_seen:
            continue

        try:
            data, _ = safe_read_file(f)
            results.append({
                "filename": f.name,
                "id": data["id"],
                "who": data["who"],
                "to": data["to"],
                "when": data["when"],
                "status": data["status"],
                "subject": data["subject"],
                "in_reply_to": data.get("in_reply_to"),
                "seen": is_seen,
                "valid": True
            })
        except Exception as e:
            results.append({
                "filename": f.name,
                "valid": False,
                "error": str(e),
                "seen": is_seen
            })

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    dir_label = "inbox" if not args.outbox else "outbox"
    print(f"Mailbox {dir_label} ({len(results)} messages):")
    if not results:
        print("  (empty)")
        return 0

    for r in results:
        seen_mark = "[SEEN]" if r["seen"] else "[NEW] "
        if not r["valid"]:
            print(f"  {seen_mark} {r['filename']} -> INVALID: {r['error']}")
        else:
            in_rep = f" (re: {r['in_reply_to'][:8]}..)" if r["in_reply_to"] else ""
            print(f"  {seen_mark} {r['when']} | {r['status']:<7} | {r['who']} -> {r['to']} | {r['subject']}{in_rep}")
            print(f"         UUID: {r['id']} ({r['filename']})")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    inbox, outbox, seen = ensure_mailbox_structure(args.root)
    file_path = find_message_file([inbox, outbox], args.file)
    data, raw_bytes = safe_read_file(file_path)

    seen_file = seen / f"{file_path.name}.seen"
    is_seen = seen_file.exists()

    if args.json:
        output = dict(data)
        output["_metadata"] = {
            "filename": file_path.name,
            "path": str(file_path),
            "size": len(raw_bytes),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "seen": is_seen
        }
        print(json.dumps(output, indent=2))
    else:
        print(f"=== Message: {data['id']} ===")
        print(f"File      : {file_path.name} ({len(raw_bytes)} bytes)")
        print(f"Status    : {data['status']} {'[SEEN]' if is_seen else '[UNSEEN]'}")
        print(f"From      : {data['who']}")
        print(f"To        : {data['to']}")
        print(f"Timestamp : {data['when']}")
        if data.get("in_reply_to"):
            print(f"In-Reply  : {data['in_reply_to']}")
        print(f"Subject   : {data['subject']}")
        print("--- Payload Body (Untrusted Data) ---")
        print(data["body"])
        print("=====================================")
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    inbox, outbox, _ = ensure_mailbox_structure(args.root)
    target_path = find_message_file([inbox], args.file)
    orig_data, _ = safe_read_file(target_path)

    msg_id = str(uuid.uuid4())
    iso_ts, fname_ts = format_utc_now()
    filename = f"{fname_ts}_{msg_id}.json"

    subject = args.subject or (f"Re: {orig_data['subject']}" if not orig_data['subject'].startswith("Re:") else orig_data['subject'])
    body = args.body
    if body == "-" or not body:
        body = sys.stdin.read().strip()
    if not body:
        print("Error: Reply body cannot be empty", file=sys.stderr)
        return 1

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": msg_id,
        "who": args.who,
        "to": orig_data["who"],
        "when": iso_ts,
        "subject": subject[:MAX_SUBJECT_LEN],
        "body": body,
        "status": args.status,
        "in_reply_to": orig_data["id"]
    }

    validate_message_dict(payload)
    content_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    saved_path = atomic_publish_file(outbox, filename, content_bytes)

    if args.json:
        print(json.dumps({"path": str(saved_path), "filename": filename, "payload": payload}))
    else:
        print(f"Published reply {msg_id} -> {saved_path.name}")
        print(f"  In-Reply-To: {orig_data['id']}")
        print(f"  To         : {payload['to']}")
        print(f"  Subject    : {payload['subject']}")
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    inbox, outbox, seen = ensure_mailbox_structure(args.root)
    file_path = find_message_file([inbox, outbox], args.file)

    _, raw_bytes = safe_read_file(file_path)
    file_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    seen_path = seen / f"{file_path.name}.seen"

    iso_ts, _ = format_utc_now()
    seen_record = {
        "filename": file_path.name,
        "sha256": file_sha256,
        "acked_at": iso_ts,
        "acked_by": args.who
    }

    content_bytes = json.dumps(seen_record, indent=2).encode("utf-8")

    # Atomic write of seen record
    tmp_path = seen / f".tmp_{uuid.uuid4().hex}.seen"
    with open(tmp_path, "wb") as f:
        f.write(content_bytes)
        f.flush()
        os.fsync(f.fileno())

    # Replace seen record if already exists (idempotent ack update)
    os.replace(tmp_path, seen_path)

    if args.json:
        print(json.dumps(seen_record))
    else:
        print(f"Acknowledged: {file_path.name} (SHA-256: {file_sha256[:12]}...)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    file_path = Path(args.file).resolve()
    try:
        data, raw_bytes = safe_read_file(file_path)
        sha = hashlib.sha256(raw_bytes).hexdigest()
        print(f"VERIFIED: {file_path.name}")
        print(f"  Size   : {len(raw_bytes)} bytes")
        print(f"  SHA-256: {sha}")
        print(f"  UUID   : {data['id']}")
        print(f"  When   : {data['when']}")
        print(f"  Schema : v{data['schema_version']} OK")
        return 0
    except Exception as e:
        print(f"FAILED: {file_path} -> {e}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Mailbox Protocol (AMP) Engine")
    parser.add_argument("--root", default=get_default_root(), help="Mailbox root directory (default: ./mailbox or AGENT_MAILBOX_ROOT)")
    parser.add_argument("--who", default=get_default_identity(), help="Local agent identity (default: USER or AGENT_IDENTITY)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser("init", help="Initialize mailbox folder structure")
    p_init.set_defaults(func=cmd_init)

    # send
    p_send = subparsers.add_parser("send", help="Atomically compose and publish an outgoing message")
    p_send.add_argument("--who", default=get_default_identity(), help="Author identity (defaults to top-level --who)")
    p_send.add_argument("--to", required=True, help="Recipient identifier")
    p_send.add_argument("--subject", required=True, help="Message subject")
    p_send.add_argument("--body", required=True, help="Message body (or '-' to read from stdin)")
    p_send.add_argument("--status", choices=list(VALID_STATUSES), default="info", help="Status state (default: info)")
    p_send.add_argument("--in-reply-to", default=None, help="UUID of target message if replying")
    p_send.add_argument("--inbox", action="store_true", help="Publish directly to inbox (for test injectors / mock senders)")
    p_send.add_argument("--json", action="store_true", help="Output result as JSON")
    p_send.set_defaults(func=cmd_send)

    # poll
    p_poll = subparsers.add_parser("poll", help="List messages and inspect unread status")
    p_poll.add_argument("--unseen-only", action="store_true", help="Only show unacknowledged messages")
    p_poll.add_argument("--outbox", action="store_true", help="Poll outbox instead of inbox")
    p_poll.add_argument("--json", action="store_true", help="Output listing as JSON")
    p_poll.set_defaults(func=cmd_poll)

    # read
    p_read = subparsers.add_parser("read", help="Safely validate and display a message payload")
    p_read.add_argument("file", help="Filename or UUID fragment of the message")
    p_read.add_argument("--json", action="store_true", help="Output full validated JSON payload")
    p_read.set_defaults(func=cmd_read)

    # reply
    p_reply = subparsers.add_parser("reply", help="Compose and publish a reply linked to an incoming message")
    p_reply.add_argument("file", help="Target filename or UUID in inbox")
    p_reply.add_argument("--who", default=get_default_identity(), help="Author identity (defaults to top-level --who)")
    p_reply.add_argument("--body", required=True, help="Reply body (or '-' to read from stdin)")
    p_reply.add_argument("--subject", default=None, help="Reply subject (defaults to 'Re: <original subject>')")
    p_reply.add_argument("--status", choices=["reply", "done", "blocked"], default="reply", help="Reply status (default: reply)")
    p_reply.add_argument("--json", action="store_true", help="Output result as JSON")
    p_reply.set_defaults(func=cmd_reply)

    # ack
    p_ack = subparsers.add_parser("ack", help="Idempotently record a hash-bound acknowledgment in seen/")
    p_ack.add_argument("file", help="Filename or UUID of target message")
    p_ack.add_argument("--who", default=get_default_identity(), help="Acknowledger identity (defaults to top-level --who)")
    p_ack.add_argument("--json", action="store_true", help="Output ack record as JSON")
    p_ack.set_defaults(func=cmd_ack)

    # verify
    p_verify = subparsers.add_parser("verify", help="Audit file integrity and schema compliance")
    p_verify.add_argument("file", help="Path to file to audit")
    p_verify.set_defaults(func=cmd_verify)

    parsed = parser.parse_args()
    try:
        return parsed.func(parsed)
    except MailboxError as e:
        print(f"Mailbox Protocol Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected Error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
