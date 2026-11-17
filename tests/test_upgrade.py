import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nightweaver.cli import with_live_status
from nightweaver.engine import Run
from nightweaver.store import Store, default_home


class UpgradeTest(unittest.TestCase):
    def test_existing_runtime_and_configuration_remain_in_place(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"LOCALAPPDATA": folder}, clear=True):
            local = Path(folder)
            self.assertEqual(default_home(), local / "Nightweaver")
            old = Store(local / "Rookline")
            (old.root / "providers.json").write_text('{"deepseek":{"api_key_file":"private.txt"}}')
            self.assertEqual(default_home().resolve(), old.root)
            self.assertTrue((Store().root / "providers.json").exists())
            os.environ["ROOKLINE_HOME"] = str(local / "original-override")
            self.assertEqual(default_home(), local / "original-override")
            os.environ["NIGHTWEAVER_HOME"] = str(local / "new-override")
            self.assertEqual(default_home(), local / "new-override")

    def test_legacy_branch_is_preserved_and_new_runs_get_new_namespace(self):
        with TemporaryDirectory() as folder:
            store = Store(Path(folder) / "runtime")
            run_id = store.create({"repository": folder}, "a" * 40)
            self.assertEqual(Run(store, run_id).ref, f"refs/heads/nightweaver/{run_id}")
            state = store.get(run_id)["state"]
            state.pop("result_ref")
            with store.connect() as db:
                db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(state), run_id))
            self.assertEqual(Run(store, run_id).ref, f"refs/heads/rookline/{run_id}")
            self.assertEqual(with_live_status(store, store.get(run_id))["result_branch"], f"rookline/{run_id}")


if __name__ == "__main__":
    unittest.main()
