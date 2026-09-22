import unittest

from location_config import prepare_location_context


class LocationConfigTests(unittest.TestCase):
    def test_new_location_includes_location_template_id(self):
        context = prepare_location_context(
            {
                "site_name": "newsite123",
                "country": "United States",
                "zia_location_name": "newsite123",
                "location_type": "new",
                "location_template_id": "3184987",
            },
            lambda _name: None,
        )
        self.assertEqual(context["location_type"], "new")
        self.assertEqual(context["location_template_id"], 3184987)
        self.assertIsNone(context["existing_location_id"])

    def test_existing_location_uses_resolved_id(self):
        context = prepare_location_context(
            {
                "site_name": "Site1",
                "zia_location_name": "Utrecht-NL-BR-DTLS",
                "location_type": "existing",
            },
            lambda name: 1234 if name == "Utrecht-NL-BR-DTLS" else None,
        )
        self.assertEqual(context["location_type"], "existing")
        self.assertEqual(context["existing_location_id"], 1234)

    def test_none_needs_no_location_fields(self):
        context = prepare_location_context(
            {"site_name": "Site1", "location_type": "none"},
            lambda _name: None,
        )
        self.assertEqual(
            context,
            {"location_type": "none", "existing_location_id": None},
        )

    def test_auto_keeps_legacy_existing_location_behavior(self):
        context = prepare_location_context(
            {
                "site_name": "Site1",
                "country": "Netherlands",
                "zia_location_name": "Known",
                "location_template_id": "999",
            },
            lambda _name: 55,
        )
        self.assertEqual(context["location_type"], "existing")
        self.assertEqual(context["existing_location_id"], 55)

    def test_auto_creates_new_location_when_name_is_unknown(self):
        context = prepare_location_context(
            {
                "site_name": "Site1",
                "country": "Netherlands",
                "zia_location_name": "New",
                "location_template_id": "777",
            },
            lambda _name: None,
        )
        self.assertEqual(context["location_type"], "new")
        self.assertEqual(context["location_template_id"], 777)

    def test_new_location_resolves_human_readable_template_name(self):
        context = prepare_location_context(
            {
                "site_name": "Site1",
                "country": "Netherlands",
                "location_type": "new",
                "location_template_name": "Default Location Template",
            },
            lambda _name: None,
            lambda name: 3184987 if name == "Default Location Template" else None,
        )
        self.assertEqual(context["location_template_id"], 3184987)
        self.assertEqual(
            context["location_template_name"], "Default Location Template"
        )

    def test_new_location_defaults_to_default_template_name(self):
        context = prepare_location_context(
            {
                "site_name": "Site1",
                "country": "Netherlands",
                "location_type": "new",
            },
            lambda _name: None,
            lambda name: 3184987 if name == "Default Location Template" else None,
        )
        self.assertEqual(context["location_template_id"], 3184987)

    def test_new_location_fails_when_template_name_cannot_resolve(self):
        with self.assertRaisesRegex(ValueError, "could not resolve"):
            prepare_location_context(
                {
                    "site_name": "Site1",
                    "country": "Netherlands",
                    "location_type": "new",
                },
                lambda _name: None,
                lambda _name: None,
            )

    def test_existing_location_must_resolve(self):
        with self.assertRaisesRegex(ValueError, "could not resolve"):
            prepare_location_context(
                {
                    "site_name": "Site1",
                    "zia_location_name": "Missing",
                    "location_type": "existing",
                },
                lambda _name: None,
            )

    def test_rejects_unknown_location_type(self):
        with self.assertRaisesRegex(ValueError, "invalid location_type"):
            prepare_location_context(
                {"site_name": "Site1", "location_type": "sometimes"},
                lambda _name: None,
            )

    def test_explicit_id_skips_name_lookup(self):
        def unexpected_lookup(_name):
            self.fail("A CSV ID override must not require a template lookup")
        context = prepare_location_context(
            {"location_type": "new", "country": "Netherlands", "location_template_id": "3219369"},
            unexpected_lookup,
            unexpected_lookup,
        )
        self.assertEqual(context["location_template_id"], 3219369)

    def test_invalid_ids_are_rejected(self):
        for value in ("0", "-1", "1.5", "not-an-id"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare_location_context(
                    {"location_type": "new", "country": "Netherlands", "location_template_id": value},
                    lambda _: None,
                )


if __name__ == "__main__":
    unittest.main()
