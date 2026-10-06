# FIN-C2-063 — Unit Tests: output-authorization & PII policy (src/policy/pii_policy.py)
#
# Real, non-stub tests for the output-authorization controls: name-based
# sensitivity classification, the document-type -> field allowlist +
# authorization decision, and the output PII redaction pass.

from src.policy import pii_policy as P


class TestSensitivityClassification:
    def test_financial_fields_are_not_sensitive(self):
        for name in (
            "account_number",
            "account_holder",
            "statement_date",
            "closing_balance",
            "invoice_number",
            "shipping_address",
        ):
            assert P.is_sensitive_field(name) is False, name

    def test_identity_secret_fields_are_sensitive(self):
        for name in (
            "ssn",
            "social_security_number",
            "date_of_birth",
            "credit_card_number",
            "card_number",
            "pin",
            "national_id",
            "passport_number",
            "my_number",
            "tax_id",
        ):
            assert P.is_sensitive_field(name) is True, name


class TestAuthorizeFields:
    def test_normal_bank_statement_all_allowed(self):
        allowed, denied = P.authorize_fields(
            "bank_statement", ["account_number", "statement_date", "closing_balance"], False
        )
        assert denied == {}
        assert set(allowed) == {"account_number", "statement_date", "closing_balance"}

    def test_off_allowlist_field_denied(self):
        allowed, denied = P.authorize_fields("bank_statement", ["account_number", "salary"], False)
        assert allowed == ["account_number"]
        assert denied == {"salary": P.REASON_NOT_APPROVED}

    def test_sensitive_field_denied_without_authorization(self):
        _, denied = P.authorize_fields("kyc_document", ["full_name", "date_of_birth"], False)
        assert denied == {"date_of_birth": P.REASON_NEEDS_AUTH}

    def test_sensitive_field_allowed_with_authorization(self):
        allowed, denied = P.authorize_fields("kyc_document", ["full_name", "date_of_birth"], True)
        assert denied == {}
        assert set(allowed) == {"full_name", "date_of_birth"}

    def test_other_type_has_no_fixed_allowlist_but_sensitivity_still_applies(self):
        allowed, denied = P.authorize_fields("other", ["memo", "ssn"], False)
        assert allowed == ["memo"]
        assert denied == {"ssn": P.REASON_NEEDS_AUTH}


class TestRedactPII:
    def test_ssn_card_email_are_masked(self):
        text = "note: SSN 123-45-6789 card 4111111111111111 mail a.b@example.com"
        redacted, hits = P.redact_pii(text)
        assert set(hits) == {"us_ssn", "card_number", "email"}
        assert "123-45-6789" not in redacted
        assert "4111111111111111" not in redacted
        assert "a.b@example.com" not in redacted

    def test_financial_values_are_preserved(self):
        text = '{"account_number": "100200300400", "closing_balance": "1,250,000.00", "d": "2026-06-30"}'
        redacted, hits = P.redact_pii(text)
        assert hits == []
        assert redacted == text

    def test_empty_input(self):
        assert P.redact_pii("") == ("", [])
