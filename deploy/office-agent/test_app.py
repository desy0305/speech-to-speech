from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import app as office


class OfficeAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_dir = tempfile.TemporaryDirectory()
        self.commit_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.workspace_dir.name).resolve()
        self.originals = {
            "WORKSPACE": office.WORKSPACE,
            "STATE_DIR": office.STATE_DIR,
            "ARTIFACT_DIR": office.ARTIFACT_DIR,
            "BACKUP_DIR": office.BACKUP_DIR,
            "COMMIT_DIR": office.COMMIT_DIR,
        }
        office.WORKSPACE = self.workspace
        office.STATE_DIR = self.workspace / ".office-agent"
        office.ARTIFACT_DIR = office.STATE_DIR / "artifacts"
        office.BACKUP_DIR = office.STATE_DIR / "backups"
        office.COMMIT_DIR = Path(self.commit_dir.name).resolve()
        office.pending_intents.clear()
        office.pending_by_request.clear()
        office.completed_requests.clear()

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(office, name, value)
        office.pending_intents.clear()
        office.pending_by_request.clear()
        office.completed_requests.clear()
        self.commit_dir.cleanup()
        self.workspace_dir.cleanup()

    @staticmethod
    def _valid_result(arguments: list[str]) -> dict:
        if arguments[0] in {"create", "set", "add", "remove", "move"}:
            Path(arguments[1]).write_bytes(b"validated-new-document")
            return {"success": True}
        if arguments[0] == "validate":
            return {"success": True, "data": "Validation passed"}
        if arguments[:3] == ["view", arguments[1], "issues"]:
            return {"success": True, "data": {"count": 0, "issues": []}}
        raise AssertionError(f"Unexpected OfficeCLI arguments: {arguments}")

    def test_path_traversal_and_escaping_symlink_are_rejected(self) -> None:
        with self.assertRaises(HTTPException) as traversal:
            office._validate_relative_path("../outside.docx", office_only=True, must_exist=False)
        self.assertEqual(traversal.exception.status_code, 403)
        with self.assertRaises(HTTPException) as control_character:
            office._validate_relative_path("bad\nname.docx", office_only=True, must_exist=False)
        self.assertEqual(control_character.exception.status_code, 400)
        with self.assertRaises(HTTPException) as private_state:
            office._validate_relative_path(".office-agent/backups/private.docx", office_only=True, must_exist=False)
        self.assertEqual(private_state.exception.status_code, 403)

        with tempfile.TemporaryDirectory() as outside_dir:
            outside = Path(outside_dir) / "outside.docx"
            outside.write_bytes(b"outside")
            (self.workspace / "linked.docx").symlink_to(outside)
            with self.assertRaises(HTTPException) as symlink:
                office._validate_relative_path("linked.docx", office_only=True, must_exist=True)
            self.assertEqual(symlink.exception.status_code, 403)

        with self.assertRaises(HTTPException) as missing_directory:
            office._resolve_directory("missing")
        self.assertEqual(missing_directory.exception.status_code, 404)

    def test_validated_copy_fallback_commits_exact_bytes(self) -> None:
        source = self.workspace / "existing.docx"
        source.write_bytes(b"original")
        request = office.ApplyRequest(
            requestId="fallback-request",
            operation="set",
            path=source.name,
            target="/document/body",
            props={"text": "updated"},
        )
        with (
            patch.object(office, "_run_json", side_effect=self._valid_result),
            patch.object(office, "_wait_for_committed_copy", side_effect=[False, True]),
        ):
            result = office._execute_mutation(request)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(source.read_bytes(), b"validated-new-document")
        self.assertTrue(result["backupCreated"])
        self.assertEqual(len(list(office.BACKUP_DIR.glob("*-existing.docx"))), 1)

    def test_failed_post_commit_validation_restores_backup(self) -> None:
        source = self.workspace / "rollback.docx"
        source.write_bytes(b"known-good-original")

        def runner(arguments: list[str]) -> dict:
            if arguments[0] == "set":
                Path(arguments[1]).write_bytes(b"candidate")
                return {"success": True}
            if arguments[0] == "validate":
                return {"success": Path(arguments[1]) != source}
            return {"success": True, "data": {"count": 0, "issues": []}}

        request = office.ApplyRequest(
            requestId="rollback-request",
            operation="set",
            path=source.name,
            target="/document/body",
            props={"text": "candidate"},
        )
        with patch.object(office, "_run_json", side_effect=runner):
            with self.assertRaises(HTTPException) as failure:
                office._execute_mutation(request)

        self.assertEqual(failure.exception.status_code, 422)
        self.assertEqual(source.read_bytes(), b"known-good-original")

    def test_style_issues_do_not_fail_integrity_validation(self) -> None:
        validation = {"success": True, "data": "Validation passed"}
        style_issues = {
            "success": True,
            "data": {"count": 1, "issues": [{"severity": 1, "message": "First-line indent suggested"}]},
        }
        office._assert_document_valid(validation, style_issues)

    def test_approval_is_one_shot_and_request_id_is_idempotent(self) -> None:
        request = office.ApplyRequest(requestId="idempotent-request", operation="create", path="new.docx")

        async def scenario() -> None:
            prepared = await office.prepare_mutation(request)
            with patch.object(
                office,
                "_execute_mutation",
                return_value={"status": "completed", "path": "new.docx", "operation": "create"},
            ):
                result = await office.execute_mutation(office.IntentRequest(intentId=prepared["intentId"]))
            self.assertEqual(result["status"], "completed")
            with self.assertRaises(HTTPException) as replay:
                await office.execute_mutation(office.IntentRequest(intentId=prepared["intentId"]))
            self.assertEqual(replay.exception.status_code, 410)
            duplicate = await office.prepare_mutation(request)
            self.assertEqual(duplicate["status"], "completed")

            expiring = office.ApplyRequest(requestId="expiring-request", operation="create", path="later.docx")
            pending = await office.prepare_mutation(expiring)
            office.pending_intents[pending["intentId"]].expires_at = time.time() - 1
            with self.assertRaises(HTTPException) as expired:
                await office.execute_mutation(office.IntentRequest(intentId=pending["intentId"]))
            self.assertEqual(expired.exception.status_code, 410)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
