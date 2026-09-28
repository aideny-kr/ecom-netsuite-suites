import copy
import unittest
from pathlib import Path
from unittest.mock import patch

import github_gate as gate
import packet
import test_packet


class GateTests(unittest.TestCase):
    setUp = test_packet.PacketTests.setUp
    git = test_packet.PacketTests.git
    prepare = test_packet.PacketTests.prepare
    review = test_packet.PacketTests.review

    def api_response(self, pr, latest=None):
        reads = iter([pr, latest or pr])
        return lambda route: (
            {"workflow_runs": []} if "/actions/runs?" in route else next(reads)
        )

    def fixture(self):
        p, diff = self.prepare()
        body = packet.section("brief", self.brief) + packet.section(
            "receipt", packet.receipt(p, self.review(p))
        )
        pr = {
            "base": {"sha": self.base},
            "head": {"sha": self.head},
            "body": body,
            "draft": False,
            "state": "open",
        }
        return p, diff, pr

    def test_current_receipt_passes(self):
        p, diff, pr = self.fixture()
        with (
            patch.object(gate, "api", side_effect=self.api_response(pr)),
            patch.object(gate, "status") as status,
            patch.object(packet, "git"),
            patch.object(packet, "resolve", return_value=self.head),
            patch.object(packet, "prepare", return_value=(p, diff)),
        ):
            self.assertTrue(gate.evaluate("example/repo", 1, self.repo / "out"))
            self.assertEqual(status.call_args.args[2], "success")

    def test_race_body_change_cannot_post_success(self):
        p, diff, pr = self.fixture()
        newer = copy.deepcopy(pr)
        newer["body"] += " changed"
        with (
            patch.object(gate, "api", side_effect=self.api_response(pr, newer)),
            patch.object(gate, "status") as status,
            patch.object(packet, "git"),
            patch.object(packet, "resolve", return_value=self.head),
            patch.object(packet, "prepare", return_value=(p, diff)),
        ):
            self.assertFalse(gate.evaluate("example/repo", 1, self.repo / "out"))
            self.assertEqual(status.call_args.args[2], "pending")

    def test_missing_brief_fails_without_fetching_pr_code(self):
        _, _, pr = self.fixture()
        pr["body"] = "ordinary PR"
        with (
            patch.object(gate, "api", side_effect=self.api_response(pr)),
            patch.object(gate, "status") as status,
            patch.object(packet, "git") as git,
        ):
            self.assertFalse(gate.evaluate("example/repo", 1, self.repo / "out"))
            git.assert_not_called()
            self.assertEqual(status.call_args.args[2], "failure")

    def test_draft_gets_packet_but_no_pass(self):
        p, diff, pr = self.fixture()
        pr["draft"] = True
        with (
            patch.object(gate, "api", side_effect=self.api_response(pr)),
            patch.object(gate, "status") as status,
            patch.object(packet, "git"),
            patch.object(packet, "resolve", return_value=self.head),
            patch.object(packet, "prepare", return_value=(p, diff)),
        ):
            self.assertFalse(gate.evaluate("example/repo", 1, self.repo / "out"))
            self.assertTrue((self.repo / "out/1/packet.json").exists())
            self.assertEqual(status.call_args.args[2], "failure")

    def test_base_push_rechecks_affected_open_prs(self):
        with patch.object(gate, "pages", return_value=[{"number": 7}]) as pages:
            self.assertEqual(
                gate.targets("push", {"ref": "refs/heads/release/foo"}, "a/b"), [7]
            )
            self.assertIn("base=release%2Ffoo", pages.call_args.args[0])

    def test_real_git_fetch_binds_pr_head_not_first_fetched_base(self):
        _, _, pr = self.fixture()
        self.git("remote", "add", "origin", str(self.repo))
        self.git("update-ref", "refs/pull/1/head", self.head)
        with (
            patch.object(gate, "api", side_effect=self.api_response(pr)),
            patch.object(gate, "status") as status,
            patch.object(Path, "cwd", return_value=self.repo),
        ):
            self.assertTrue(gate.evaluate("example/repo", 1, self.repo / "out"))
            self.assertEqual(status.call_args.args[2], "success")

    def test_dispatch_rejects_shell_payload(self):
        with self.assertRaises(packet.Invalid):
            gate.targets(
                "workflow_dispatch", {"inputs": {"pr": "1; echo injected"}}, "a/b"
            )


if __name__ == "__main__":
    unittest.main()
