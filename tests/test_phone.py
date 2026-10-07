import unittest

from app.phone import extract_lead_id, normalize_serbian_phone


class PhoneTests(unittest.TestCase):
    def test_normalizes_requested_examples(self):
        for raw in ("0641234567", "381641234567", "+381641234567", "064 123 4567"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_serbian_phone(raw), "+381641234567")

    def test_rejects_other_country_and_ambiguous_values(self):
        for raw in ("+441234567890", "00381641234567", "064abc1234", "064123", "0641234567;rm"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    normalize_serbian_phone(raw)

    def test_extracts_only_one_complete_positive_tag(self):
        self.assertEqual(extract_lead_id("Auto Servis Markovic | SJT-823"), 823)
        for raw in ("SJT-0", "SJT-42x", "XSJT-42", "SJT-3 SJT-4", "Company"):
            with self.subTest(raw=raw):
                self.assertIsNone(extract_lead_id(raw))


if __name__ == "__main__":
    unittest.main()
