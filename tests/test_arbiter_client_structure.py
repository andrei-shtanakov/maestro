"""Structural tests for the vendored ArbiterClient.

These verify the module exists and exposes the expected surface. Behavior
is exercised via FakeArbiterClient in later tasks.
"""

from maestro.coordination import arbiter_client


class TestVendoringHeader:
    def test_vendored_from_sha_pinned(self) -> None:
        # ARBITER_VENDORED_FROM_SHA is the single source of truth for the
        # vendored commit; the legacy ARBITER_VENDOR_COMMIT alias was removed
        # in R-06b M4 Copilot polish #5.
        assert arbiter_client.ARBITER_VENDORED_FROM_SHA.startswith("e25ffed")

    def test_required_version_pinned(self) -> None:
        assert arbiter_client.ARBITER_MCP_REQUIRED_VERSION == "0.2.0"


class TestPublicAPI:
    def test_client_class_exists(self) -> None:
        assert hasattr(arbiter_client, "ArbiterClient")
        assert hasattr(arbiter_client, "ArbiterClientConfig")

    def test_dto_classes_exist(self) -> None:
        assert hasattr(arbiter_client, "RouteDecisionDTO")
        assert hasattr(arbiter_client, "OutcomeResultDTO")
