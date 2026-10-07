import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from app.android_contacts import (
    AndroidContactError, _find_adb, _has_data_row, _one_device,
    _raw_id_for_marker, add_android_contact,
)


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

    def test_marker_lookup_requires_one_exact_row(self):
        with patch("app.android_contacts._shell", return_value="Row: 0 _id=42, sync1=viber_cli_abc"):
            self.assertEqual(_raw_id_for_marker("adb", "ABC", "viber_cli_abc"), 42)
        with patch("app.android_contacts._shell", return_value="No result found."):
            self.assertIsNone(_raw_id_for_marker("adb", "ABC", "viber_cli_abc"))
        with patch("app.android_contacts._shell", return_value="Row: 0 _id=42, sync1=other"):
            with self.assertRaises(AndroidContactError):
                _raw_id_for_marker("adb", "ABC", "viber_cli_abc")

    def test_readback_requires_exact_name_and_number(self):
        rows = ["Row: 0 mimetype=vnd.android.cursor.item/name, data1=Shop, LLC | SJT-42, raw_contact_id=42"]
        self.assertTrue(_has_data_row(rows, "vnd.android.cursor.item/name", "Shop, LLC | SJT-42", 42))
        self.assertFalse(_has_data_row(rows, "vnd.android.cursor.item/name", "Shop, LLC | SJT-4", 42))

    def test_silent_insert_succeeds_only_after_readback(self):
        name = "Test Company | SJT-1"
        phone = "+381641234567"
        data_rows = (
            f"Row: 0 mimetype=vnd.android.cursor.item/name, data1={name}, raw_contact_id=42\n"
            f"Row: 1 mimetype=vnd.android.cursor.item/phone_v2, data1={phone}, raw_contact_id=42"
        )

        def shell(adb, serial, words):
            return data_rows if words[1] == "query" else ""

        with patch("app.android_contacts._find_adb", return_value="adb"), \
             patch("app.android_contacts._one_device", return_value="ABC"), \
             patch("app.android_contacts._raw_id_for_marker", side_effect=[None, 42, 42]), \
             patch("app.android_contacts._shell", side_effect=shell) as command:
            add_android_contact(name, phone)
        inserts = [call for call in command.call_args_list if call.args[2][1] == "insert"]
        self.assertEqual(len(inserts), 3)

    def test_missing_data_row_triggers_cleanup(self):
        with patch("app.android_contacts._find_adb", return_value="adb"), \
             patch("app.android_contacts._one_device", return_value="ABC"), \
             patch("app.android_contacts._raw_id_for_marker", side_effect=[None, 42, None]), \
             patch("app.android_contacts._shell", side_effect=lambda a, b, words: "No result found." if words[1] == "query" else "") as command:
            with self.assertRaisesRegex(AndroidContactError, "name could not be verified"):
                add_android_contact("Test | SJT-1", "+381641234567")
        self.assertTrue(any(call.args[2][1] == "delete" for call in command.call_args_list))


if __name__ == "__main__":
    unittest.main()
