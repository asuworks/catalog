from .common import BaseTest


class AutocompleteViewTest(BaseTest):
    def test_missing_model_name_returns_bad_request(self):
        response = self.get("core:autocomplete", {"search": "test"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Missing model_name query param"})

    def test_unsupported_model_name_returns_bad_request(self):
        response = self.get(
            "core:autocomplete", {"model_name": "publication", "search": "test"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Invalid model_name"})
