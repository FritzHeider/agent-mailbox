import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE_CLI = str(Path(__file__).parent.parent / "bridge.py")


class TestAgentMailbox(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_mailbox_")
        self.root = Path(self.test_dir) / "mailbox"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def run_cmd(self, *args, check=True):
        cmd = [sys.executable, BRIDGE_CLI, "--root", str(self.root)] + list(args)
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and res.returncode != 0:
            raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(cmd)}\nStderr: {res.stderr}\nStdout: {res.stdout}")
        return res

    def test_full_roundtrip_lifecycle(self):
        # 1. Init
        self.run_cmd("init")
        self.assertTrue((self.root / "inbox").is_dir())
        self.assertTrue((self.root / "outbox").is_dir())
        self.assertTrue((self.root / "seen").is_dir())

        # 2. Send message into inbox (simulating incoming message from remote agent)
        send_res = self.run_cmd(
            "send",
            "--who", "ClientAgent",
            "--to", "ServerAgent",
            "--subject", "Task Dispatch Request",
            "--body", "Please run unit tests on suite alpha.",
            "--status", "request",
            "--inbox",
            "--json"
        )
        send_data = json.loads(send_res.stdout)
        orig_id = send_data["payload"]["id"]
        filename = send_data["filename"]
        inbox_file = self.root / "inbox" / filename
        self.assertTrue(inbox_file.is_file())

        # 3. Poll inbox
        poll_res = self.run_cmd("poll", "--json")
        poll_data = json.loads(poll_res.stdout)
        self.assertEqual(len(poll_data), 1)
        self.assertEqual(poll_data[0]["id"], orig_id)
        self.assertFalse(poll_data[0]["seen"])

        # 4. Read message
        read_res = self.run_cmd("read", orig_id, "--json")
        read_data = json.loads(read_res.stdout)
        self.assertEqual(read_data["id"], orig_id)
        self.assertEqual(read_data["status"], "request")
        self.assertEqual(read_data["who"], "ClientAgent")

        # 5. Reply to message
        reply_res = self.run_cmd(
            "reply", orig_id,
            "--who", "ServerAgent",
            "--body", "Suite alpha tests passed with 0 errors.",
            "--status", "reply",
            "--json"
        )
        reply_data = json.loads(reply_res.stdout)
        reply_id = reply_data["payload"]["id"]
        self.assertEqual(reply_data["payload"]["in_reply_to"], orig_id)
        self.assertEqual(reply_data["payload"]["to"], "ClientAgent")
        self.assertTrue((self.root / "outbox" / reply_data["filename"]).is_file())

        # 6. Ack incoming message
        ack_res = self.run_cmd("ack", orig_id, "--who", "ServerAgent", "--json")
        ack_data = json.loads(ack_res.stdout)
        self.assertEqual(ack_data["filename"], filename)
        seen_file = self.root / "seen" / f"{filename}.seen"
        self.assertTrue(seen_file.is_file())

        # 7. Double Ack (Idempotency)
        ack_res2 = self.run_cmd("ack", orig_id, "--who", "ServerAgent", "--json")
        self.assertEqual(ack_res2.returncode, 0)

        # 8. Poll unseen only should now be empty
        poll_unseen = self.run_cmd("poll", "--unseen-only", "--json")
        self.assertEqual(json.loads(poll_unseen.stdout), [])

    def test_security_symlink_rejection(self):
        self.run_cmd("init")
        # Create a legitimate file outside
        secret_file = Path(self.test_dir) / "secret.txt"
        secret_file.write_text("sensitivedata")

        # Symlink into inbox
        symlink_path = self.root / "inbox" / "20261108T120000000000Z_00000000-0000-4000-8000-000000000000.json"
        os.symlink(secret_file, symlink_path)

        res = self.run_cmd("read", symlink_path.name, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Security violation: symlinks are strictly rejected", res.stderr)

    def test_security_schema_mismatch_rejection(self):
        self.run_cmd("init")
        # Message where filename UUID does not match body UUID
        filename = "20261108T120000000000Z_11111111-1111-4111-8111-111111111111.json"
        bad_payload = {
            "schema_version": 1,
            "id": "22222222-2222-4222-8222-222222222222",  # Mismatched!
            "who": "Alice",
            "to": "Bob",
            "when": "2026-11-08T12:00:00.000000Z",
            "subject": "Test",
            "body": "Test",
            "status": "info",
            "in_reply_to": None
        }
        target = self.root / "inbox" / filename
        target.write_text(json.dumps(bad_payload))

        res = self.run_cmd("read", filename, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("does not match filename UUID", res.stderr)

    def test_security_file_size_limit(self):
        self.run_cmd("init")
        huge_body = "A" * 66000
        res = self.run_cmd(
            "send",
            "--who", "Alice",
            "--to", "Bob",
            "--subject", "Oversized message",
            "--body", huge_body,
            check=False
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Invalid 'body'", res.stderr)

    def test_corrupted_json_rejection(self):
        self.run_cmd("init")
        filename = "20261108T120000000000Z_33333333-3333-4333-8333-333333333333.json"
        target = self.root / "inbox" / filename
        target.write_text("{this is not valid json")

        res = self.run_cmd("read", filename, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Malformed JSON", res.stderr)

    def test_verify_command(self):
        self.run_cmd("init")
        send_res = self.run_cmd(
            "send",
            "--who", "Auditor",
            "--to", "Target",
            "--subject", "Audit Probe",
            "--body", "Integrity check test.",
            "--json"
        )
        path = json.loads(send_res.stdout)["path"]
        verify_res = self.run_cmd("verify", path)
        self.assertIn("VERIFIED", verify_res.stdout)
        self.assertIn("Schema : v1 OK", verify_res.stdout)


if __name__ == "__main__":
    unittest.main()
