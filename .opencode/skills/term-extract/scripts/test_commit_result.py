"""验证读完整性，尤其分片末尾不能冒充完整单元。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from commit_result import coverage_report


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.index = self.directory / "index.json"
        self.index.write_text(json.dumps({"units": [{"unit_id": "u0", "tag": "<C9>", "source_length": 10, "source_text": "0123456789"}]}), encoding="utf-8")
        self.hash = hashlib.sha256(self.index.read_bytes()).hexdigest()
        self.receipts = self.directory / "read_receipts.jsonl"

    def record(self, spans, fingerprint=None):
        receipt = {"index_sha256": fingerprint or self.hash, "coverage_kind": "full_original", "parts": [{"unit_id": "u0", "part_start": start, "part_end": end, "total_length": 10} for start, end in spans]}
        self.receipts.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

    def test_last_part_is_not_complete(self):
        self.record([(5, 10)])
        self.assertFalse(coverage_report(self.index, self.receipts)["complete"])

    def test_adjacent_parts_complete_original(self):
        self.record([(0, 4), (4, 10)])
        self.assertTrue(coverage_report(self.index, self.receipts)["complete"])

    def test_interior_gap_blocks_commit(self):
        self.record([(0, 4), (5, 10)])
        self.assertFalse(coverage_report(self.index, self.receipts)["complete"])

    def test_receipts_from_other_index_are_rejected(self):
        self.record([(0, 10)], "old-index")
        self.assertFalse(coverage_report(self.index, self.receipts)["complete"])


if __name__ == "__main__":
    unittest.main()
