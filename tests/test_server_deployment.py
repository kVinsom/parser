import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from deploy.configure import configure
from deploy.export_local_data import archive_state
from deploy.manage import Server
from deploy.state_archive import export, restore
from tools.build_release import included


class DeploymentTests(unittest.TestCase):
    def test_local_transfer_takes_only_source_checkpoint_from_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "artifacts"
            source.mkdir()
            (source / "source-cooldowns.json").write_text('{"version":1}')
            (source / "monitor.pid").write_text("123")
            (source / "private.log").write_text("not included")
            archive_state(
                root / "worker.tar", source, filenames={"source-cooldowns.json"}
            )
            with tarfile.open(root / "worker.tar") as archive:
                self.assertEqual(archive.getnames(), ["source-cooldowns.json"])

    def test_generated_configuration_is_unique_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env.example").write_text(
                "POSTGRES_PASSWORD=\nWALLET_SESSION_ENCRYPTION_KEY=\n"
            )
            path = configure(root)
            first = path.read_text()
            self.assertNotIn("PASSWORD=\n", first)
            self.assertNotIn("KEY=\n", first)
            with self.assertRaises(FileExistsError):
                configure(root)
            self.assertEqual(path.read_text(), first)

    def test_backup_restores_worker_checkpoint_without_overwriting_existing_state(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "source", Path(folder) / "target"
            source.mkdir()
            target.mkdir()
            data = {"regions": {"FR": {"until": "2030-01-01"}}, "pacing": {"FR": 16}}
            (source / "source-cooldowns.json").write_text(json.dumps(data))
            stream = io.BytesIO()
            export(source, stream)
            stream.seek(0)
            restore(target, stream)
            self.assertEqual(
                json.loads((target / "source-cooldowns.json").read_text()), data
            )
            stream.seek(0)
            with self.assertRaises(ValueError):
                restore(target, stream)

    def test_unsafe_tar_is_rejected_before_writing_any_entry(self):
        for malicious in ("../outside", "/absolute"):
            with self.subTest(name=malicious), tempfile.TemporaryDirectory() as folder:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as archive:
                    archive.addfile(tarfile.TarInfo("normal-file"))
                    archive.addfile(tarfile.TarInfo(malicious))
                data.seek(0)
                with self.assertRaises(ValueError):
                    restore(Path(folder), data)
                self.assertFalse(list(Path(folder).iterdir()))

    def test_existing_database_is_never_overwritten(self):
        server = Server(Path("unused.env"))
        server.running = Mock(return_value=set())
        server.call = Mock(return_value=Mock(stdout="17\n"))
        with self.assertRaisesRegex(RuntimeError, "empty database"):
            server.import_database(Path("unused.dump"))
        self.assertFalse(
            any("pg_restore" in c.args for c in server.call.call_args_list)
        )

    def test_failed_backup_resumes_only_previously_running_services(self):
        with tempfile.TemporaryDirectory() as folder:
            server = Server(Path("unused.env"))
            server.running = Mock(return_value={"worker", "postgres"})

            def call(*args, **kwargs):
                if "pg_dump" in args:
                    raise subprocess.CalledProcessError(1, ["pg_dump"])

            server.call = Mock(side_effect=call)
            with self.assertRaises(subprocess.CalledProcessError):
                server.backup(Path(folder) / "backup")
            server.call.assert_any_call("start", "worker")
            self.assertFalse((Path(folder) / "backup/manifest.json").exists())

    def test_release_contains_server_and_ui_assets_but_no_secrets(self):
        for name in (
            "docs/BOT_BUY_2026-10-12.md",
            "docs/RELEASE_AUDIT_2026-10-13.md",
            "deploy/compose.yaml",
            "deploy/Dockerfile",
            "deploy/state_archive.py",
            "deploy/.env.example",
            "deploy/seccomp-playwright.json",
            "services/telegram-bot/vpars_bot/handoff_assets/app.js",
        ):
            self.assertTrue(included(Path(name)), name)
        for name in (
            "deploy/.env",
            "deploy/private.env",
            "deploy/database.dump",
            ".backups/private.env",
            "deploy/unknown-secrets.json",
        ):
            self.assertFalse(included(Path(name)), name)


if __name__ == "__main__":
    unittest.main()
