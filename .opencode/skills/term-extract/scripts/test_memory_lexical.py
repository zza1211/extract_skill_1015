import unittest

from memory_lexical import terms, document_query


class LexicalTests(unittest.TestCase):
    def test_bilingual_traditional_query_matches_explicit_alias(self):
        self.assertIn("alias:quantity", terms("採購數量"))
        self.assertIn("alias:quantity", terms("QUANTITY"))
        self.assertIn("alias:access_point", terms("無線接入點"))
        self.assertIn("alias:access_point", terms("Access Points"))

    def test_does_not_match_embedded_english_or_invent_negation(self):
        self.assertNotIn("alias:switch", terms("switchboard"))
        self.assertNotIn("alias:quantity", terms("quantityless"))
        self.assertIn("alias:separate", terms("不是分散条款"))

    def test_retains_specific_terms_and_normalizes_width(self):
        self.assertIn("sfp-10g", terms("ＳＦＰ-１０Ｇ"))
        self.assertIn("端口", terms("端口密度"))

    def test_features_after_long_administrative_preamble_are_retained(self):
        index = {"units": [{"text": "Tender submission procedure."}] * 200 + [{"text": "Wireless Access Points quantity 12"}]}
        query = document_query(index)
        self.assertIn("access point", query)
        self.assertLessEqual(len(query), 512)


if __name__ == "__main__":
    unittest.main()
