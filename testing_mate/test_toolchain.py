import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from testing_mate.toolchain import (
    MINGW_ROOT_ENV,
    configure_mingw_root,
    configured_mingw_root,
    local_mingw_bin,
    mingw_toolchain_available,
    msys2_root,
    toolchain_path_entries,
)


class ToolchainDiscoveryTests(unittest.TestCase):
    def test_msys_root_selection_uses_mingw64_and_usr_bin_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "msys64"
            (root / "mingw64" / "bin").mkdir(parents=True)
            (root / "usr" / "bin").mkdir(parents=True)
            (root / "usr" / "bin" / "bash.exe").write_text("", encoding="utf-8")

            with patch.dict(os.environ, {MINGW_ROOT_ENV: str(root)}, clear=False):
                self.assertEqual(configured_mingw_root(), root / "mingw64")
                self.assertEqual(msys2_root(), root)
                self.assertEqual(local_mingw_bin(), root / "mingw64" / "bin")
                self.assertEqual(
                    toolchain_path_entries(),
                    [str(root / "mingw64" / "bin"), str(root / "usr" / "bin")],
                )
                self.assertTrue(mingw_toolchain_available())

    def test_configure_accepts_mingw64_bin_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "msys64"
            mingw_bin = root / "mingw64" / "bin"
            mingw_bin.mkdir(parents=True)
            (root / "usr" / "bin").mkdir(parents=True)
            (root / "usr" / "bin" / "bash.exe").write_text("", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=False):
                configure_mingw_root(mingw_bin)
                self.assertEqual(configured_mingw_root(), root / "mingw64")
                self.assertTrue(mingw_toolchain_available())


if __name__ == "__main__":
    unittest.main()
