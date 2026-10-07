import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from app.android_contacts import AndroidContactError, _find_adb, _has_data_row, _inserted_id, _one_device


class AndroidParsingTests(unittest.TestCase):
    def test_explicit_adb_path_takes_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            adb = Path(directory) / "adb.exe"
            adb.touch()
            with patch.dict("os.environ", {"VIBER_CLI_ADB": str(adb)}):
                self.assertEqual(_find_adb(), str(adb))

    def test_bad_explicit_adb_path_fails_clearly(self):
        with patch.dict("os.environ", {"VIBER_CLI_ADB": r"C:\missing\adb.exe"}):
            with self.assertRaisesRegex(AndroidContactError, "VIBER_CLI_ADB"):
                _find_adb()

    def test_device_selection_rejects_extra_offline_device(self):
        listing = "List of devices attached\nABC device product:x\nDEF offline\n"
        with patch("app.android_contacts._run_adb", return_value=listing):
            with self.assertRaises(AndroidContactError):
                _one_device("adb")

    def test_device_selection_accepts_one_authorized_device(self):
        listing = "* daemon started successfully\nList of devices attached\nABC device product:x\n"
        with patch("app.android_contacts._run_adb", return_value=listing):
            self.assertEqual(_one_device("adb"), "ABC")

    def test_insert_and_readback_require_exact_row(self):
        self.assertEqual(_inserted_id("Inserted row: content://com.android.contacts/raw_contacts/42"), 42)
        with self.assertRaises(AndroidContactError):
            _inserted_id("No row inserted")
        rows = ["Row: 0 mimetype=vnd.android.cursor.item/name, data1=Shop, LLC | SJT-42, raw_contact_id=42"]
        self.assertTrue(_has_data_row(rows, "vnd.android.cursor.item/name", "Shop, LLC | SJT-42", 42))
        self.assertFalse(_has_data_row(rows, "vnd.android.cursor.item/name", "Shop, LLC | SJT-4", 42))


if __name__ == "__main__":
    unittest.main()
