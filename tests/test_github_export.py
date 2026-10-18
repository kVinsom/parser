import tempfile
import unittest
from pathlib import Path
from tools.export_github import export


class GithubExportTests(unittest.TestCase):
    def test_source_only_export_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            root.mkdir()
            (root / "README.md").write_text("Public setup")
            (root / ".env").write_text("PASSWORD=private-example-credential")
            private = root / "deploy/proxy-daily-reports"
            private.mkdir(parents=True)
            (private / "report.json").write_text("private usage")
            out = Path(temp) / "github"
            self.assertEqual(export(root,out), 1)
            self.assertEqual({p.name for p in out.iterdir()}, {"README.md", "MANIFEST.json"})
            self.assertTrue((root / ".env").exists())
            with self.assertRaises(FileExistsError):
                export(root,out)

    def test_embedded_known_secret_refuses_before_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            root.mkdir()
            (root / ".env").write_text("PASSWORD=private-example-credential")
            (root / "README.md").write_text("Example private-example-credential")
            out = Path(temp) / "github"
            with self.assertRaises(ValueError) as caught:
                export(root,out)
            self.assertNotIn("private-example-credential", str(caught.exception))
            self.assertFalse(out.exists())

    def test_unknown_private_key_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            root.mkdir()
            (root / "README.md").write_text("-----BEGIN " + "OPENSSH PRIVATE KEY-----")
            with self.assertRaises(ValueError):
                export(root,Path(temp)/"github")

    def test_private_operational_reports_are_not_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            (root / "docs").mkdir(parents=True)
            for name in ("AUTOBUY.md", "RELEASE_AUDIT_2026-10-13.md", "BOT_BUY_2026-10-12.md"):
                (root / "docs" / name).write_text("private-account-history")
            admin = root / "services/telegram-bot/docs/ADMINS.md"
            admin.parent.mkdir(parents=True)
            admin.write_text("private-account-history")
            out = Path(temp) / "github"
            export(root,out)
            self.assertNotIn("private-account-history", "".join(p.read_text() for p in out.rglob("*.md")))
            self.assertEqual((root / "docs/AUTOBUY.md").read_text(), "private-account-history")
