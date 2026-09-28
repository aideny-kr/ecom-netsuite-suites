"""Regression tests for revision binding and fail-closed review receipts."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import packet


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "app.py").write_text("before\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "app.py").write_text("after\n")
        self.git("commit", "-qam", "change")
        self.head = self.git("rev-parse", "HEAD")
        self.brief = {
            "problem": "Daily work stalls after a recoverable read.",
            "acceptance": ["Resume the same checkpoint without duplicate spend."],
            "configuration": ["Inc schedule; finite retries remain."],
            "risks": ["Lock order"],
            "evidence": ["Focused regression: see CI artifact."],
            "implementer_model": "gpt-6-astra",
            "tier": "T2",
        }

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True
        ).strip()

    def prepare(self):
        return packet.prepare(
            self.repo, "example/repo", self.base, self.head, self.brief
        )

    def review(self, p):
        return {
            "base_sha": self.base,
            "head_sha": self.head,
            "packet_sha256": p["packet_sha256"],
            "reviewer_model": "claude-opus-5-5",
            "verdict": "pass",
            "angles": packet.ANGLES,
            "findings": [],
            "report": "https://example.invalid/review-artifact",
        }

    def test_deterministic_packet_and_receipt(self):
        p, patch = self.prepare()
        self.assertIn("+after", patch)
        self.assertEqual(p, self.prepare()[0])
        receipt = packet.receipt(p, self.review(p))
        packet.validate_receipt(p, receipt)

    def test_new_head_invalidates_review_even_same_tree(self):
        p, _ = self.prepare()
        r = packet.receipt(p, self.review(p))
        self.git("commit", "--allow-empty", "-qm", "new revision")
        self.head = self.git("rev-parse", "HEAD")
        with self.assertRaises(packet.Invalid):
            packet.validate_receipt(self.prepare()[0], r)

    def test_base_advance_invalidates_review(self):
        p, _ = self.prepare()
        r = packet.receipt(p, self.review(p))
        self.base = self.head
        with self.assertRaises(packet.Invalid):
            packet.validate_receipt(self.prepare()[0], r)

    def test_changed_brief_invalidates_review(self):
        p, _ = self.prepare()
        r = packet.receipt(p, self.review(p))
        self.brief["configuration"].append("Different customer mode")
        with self.assertRaises(packet.Invalid):
            packet.validate_receipt(self.prepare()[0], r)

    def test_dirty_checkout_rejected_by_local_guard(self):
        (self.repo / "app.py").write_text("unreviewed")
        with self.assertRaises(packet.Invalid):
            packet.require_clean(self.repo)

    def test_untracked_source_rejected(self):
        (self.repo / "new.py").write_text("untracked")
        with self.assertRaises(packet.Invalid):
            packet.require_clean(self.repo)

    def test_same_model_alias_cannot_claim_independence(self):
        p, _ = self.prepare()
        r = self.review(p)
        r["reviewer_model"] = "GPT-6-ASTRA"
        with self.assertRaises(packet.Invalid):
            packet.receipt(p, r)

    def test_tool_name_is_not_model_identity(self):
        p, _ = self.prepare()
        for model in ["codex", "claude", "", "unknown"]:
            with self.subTest(model=model), self.assertRaises(packet.Invalid):
                packet.receipt(p, {**self.review(p), "reviewer_model": model})

    def test_missing_angle_incomplete_and_unresolved_block_fail(self):
        p, _ = self.prepare()
        for edit in [
            {"angles": ["correctness"]},
            {"verdict": "incomplete"},
            {
                "findings": [
                    {
                        "id": "M1",
                        "severity": "major",
                        "status": "open",
                        "reason": "deadlock",
                    }
                ]
            },
            {
                "findings": [
                    {
                        "id": "M1",
                        "severity": "major",
                        "status": "deferred",
                        "reason": "",
                    }
                ]
            },
        ]:
            with self.subTest(edit=edit), self.assertRaises(packet.Invalid):
                packet.receipt(p, {**self.review(p), **edit})

    def test_disposition_is_preserved(self):
        p, _ = self.prepare()
        r = self.review(p)
        r["findings"] = [
            {
                "id": "M1",
                "severity": "major",
                "status": "deferred",
                "reason": "Explicitly accepted bounded limitation; see report.",
            }
        ]
        receipt = packet.receipt(p, r)
        self.assertEqual(receipt["findings"], r["findings"])

    def test_duplicate_json_keys_and_duplicate_sections_rejected(self):
        with self.assertRaises(packet.Invalid):
            packet.parse_json('{"verdict":"fail","verdict":"pass"}')
        body = packet.section("brief", self.brief)
        with self.assertRaises(packet.Invalid):
            packet.extract(body + body, "brief")

    def test_roundtrip_body_and_missing_receipt(self):
        body = packet.section("brief", self.brief)
        self.assertEqual(packet.extract(body, "brief"), self.brief)
        with self.assertRaises(packet.Invalid):
            packet.extract(body, "receipt")

    def test_tampered_packet_rejected(self):
        p, _ = self.prepare()
        r = packet.receipt(p, self.review(p))
        p["head_sha"] = "a" * 40
        with self.assertRaises(packet.Invalid):
            packet.validate_receipt(p, r)

    def test_oversize_brief_and_unknown_fields_rejected(self):
        for edit in [{"problem": "x" * 16001}, {"override": "skip review"}]:
            self.brief.update(edit)
            with self.assertRaises(packet.Invalid):
                self.prepare()
            self.brief.pop("override", None)

    def test_revision_options_are_not_executed(self):
        with self.assertRaises(packet.Invalid):
            packet.prepare(self.repo, "example/repo", "--help", self.head, self.brief)

    def test_renamed_tooling_cannot_escape_tier_guard(self):
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/gate.py").write_text('print("gate")\n')
        self.git("add", ".")
        self.git("commit", "-qm", "tooling base")
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "docs").mkdir()
        self.git("mv", "scripts/gate.py", "docs/gate.md")
        self.git("commit", "-qm", "rename")
        self.head = self.git("rev-parse", "HEAD")
        for tier in ["T0", "T1"]:
            self.brief["tier"] = tier
            with self.subTest(tier=tier), self.assertRaises(packet.Invalid):
                self.prepare()

    def test_code_cannot_claim_docs_only_exemption(self):
        self.brief["tier"] = "T0"
        with self.assertRaises(packet.Invalid):
            self.prepare()

    def test_tooling_cannot_downgrade_to_t1(self):
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts/gate.py").write_text('print("gate")')
        self.git("add", ".")
        self.git("commit", "-qm", "tooling")
        self.head = self.git("rev-parse", "HEAD")
        self.brief["tier"] = "T1"
        with self.assertRaises(packet.Invalid):
            self.prepare()

    def test_t1_light_review_keeps_existing_tier(self):
        self.brief["tier"] = "T1"
        p, _ = self.prepare()
        r = self.review(p)
        r["angles"] = packet.LIGHT_ANGLES
        packet.validate_receipt(p, packet.receipt(p, r))

    def test_failed_review_cannot_be_overwritten_by_missing_findings(self):
        p, _ = self.prepare()
        r = self.review(p)
        del r["findings"]
        with self.assertRaises(packet.Invalid):
            packet.receipt(p, r)


if __name__ == "__main__":
    unittest.main()
