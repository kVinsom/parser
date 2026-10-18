import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.build_release import POLICY, build


class ReleaseBundleTests(unittest.TestCase):
    def test_only_release_inputs_are_included_and_personal_policy_is_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = {
                "README.md": "release",
                "shared/pyproject.toml": "[project]\nname = 'vpars-shared'",
                "shared/vpars_shared/sizes.py": "# shared rules",
                "services/telegram-bot/vpars_bot/runtime.py": "# runtime",
                "services/telegram-bot/.env.example": "TELEGRAM_BOT_TOKEN=",
                "services/telegram-bot/.env": "private token",
                "services/telegram-bot/.env.backup": "private token",
                "services/telegram-bot/.artifacts/private.py": "private data",
                "services/telegram-bot/.wallet-profiles/session.json": "private session",
                "services/telegram-bot/.venv/lib/module.py": "dependency",
                "services/parser/.generated/vinted_rpc/access_pb2.py": "generated",
                "services/parser/tests/fixtures/FR.html": "fixture",
                "services/access-gateway/bypass/proto/vinted_rpc/access.proto": "contract",
                "services/worker/vpars_worker/__pycache__/main.pyc": "bytecode",
                "docs/WORK_LOG.md": "historical private diagnostics",
                ".release-checks/before/README.md": "backup",
                POLICY: '{"one_minute_user_ids": [12345]}',
            }
            for name, data in inputs.items():
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(data, encoding="utf-8")
            output = root / "dist/release.zip"
            build(root, output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "README.md", "services/telegram-bot/vpars_bot/runtime.py",
                    "services/telegram-bot/.env.example", "services/parser/tests/fixtures/FR.html",
                    "services/access-gateway/bypass/proto/vinted_rpc/access.proto", POLICY,
                    "MANIFEST.json",
                    "shared/pyproject.toml", "shared/vpars_shared/sizes.py",
                })
                self.assertEqual(json.loads(archive.read(POLICY)), {"applies_to": "all_users", "max_age_seconds": 60})
                self.assertNotIn(b"private", b"".join(archive.read(p) for p in archive.namelist()))
            self.assertEqual((root / POLICY).read_text(), inputs[POLICY])
            with self.assertRaises(FileExistsError):
                build(root, output)


if __name__ == "__main__":
    unittest.main()
