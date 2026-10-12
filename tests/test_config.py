import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import config


class FindCodexTests(unittest.TestCase):
    """launchd의 최소 PATH에서 codex 실행 파일을 찾는 순서: PATH → ~/.local/bin → standalone → nvm."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.object(Path, "home", return_value=self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _touch(self, rel: str) -> Path:
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
        return p

    def test_path_wins(self):
        self._touch(".local/bin/codex")
        with mock.patch("shutil.which", return_value="/opt/bin/codex"):
            self.assertEqual(config._find_codex(), "/opt/bin/codex")

    def test_local_bin_symlink_from_codex_update(self):
        local = self._touch(".local/bin/codex")
        self._touch(".nvm/versions/node/v25.9.0/bin/codex")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(config._find_codex(), str(local))

    def test_standalone_current_without_local_bin(self):
        standalone = self._touch(".codex/packages/standalone/current/bin/codex")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(config._find_codex(), str(standalone))

    def test_nvm_fallback_then_bare_name(self):
        nvm = self._touch(".nvm/versions/node/v25.9.0/bin/codex")
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(config._find_codex(), str(nvm))
        nvm.unlink()
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(config._find_codex(), "codex")


if __name__ == "__main__":
    unittest.main()
