"""Tests for PayPalPlugin class."""
import pytest
from flask import Blueprint

from plugins.paypal import PayPalPlugin
from src.plugins.base import BasePlugin
from src.plugins.payment_provider import PaymentProviderPlugin


class TestPayPalPluginMetadata:
    """Test PayPalPlugin metadata and class hierarchy."""

    @pytest.fixture
    def plugin(self):
        """Create a PayPalPlugin instance."""
        return PayPalPlugin()

    def test_metadata_name(self, plugin):
        """plugin.metadata.name should be 'paypal'."""
        assert plugin.metadata.name == "paypal"

    def test_metadata_version(self, plugin):
        """plugin.metadata.version should be '1.0.0'."""
        assert plugin.metadata.version == "1.0.0"

    def test_metadata_author(self, plugin):
        """plugin.metadata.author should be 'VBWD Team'."""
        assert plugin.metadata.author == "VBWD Team"

    def test_inherits_payment_provider(self, plugin):
        """PayPalPlugin must be a PaymentProviderPlugin."""
        assert isinstance(plugin, PaymentProviderPlugin)

    def test_inherits_base_plugin(self, plugin):
        """PayPalPlugin must be a BasePlugin."""
        assert isinstance(plugin, BasePlugin)

    def test_get_blueprint_returns_blueprint(self, plugin):
        """get_blueprint should return a Flask Blueprint."""
        bp = plugin.get_blueprint()
        assert isinstance(bp, Blueprint)

    def test_get_url_prefix(self, plugin):
        """get_url_prefix should return '/api/v1/plugins/paypal'."""
        assert plugin.get_url_prefix() == "/api/v1/plugins/paypal"

    def test_no_dependencies(self, plugin):
        """Plugin should declare no dependencies."""
        assert plugin.metadata.dependencies == []

    def test_on_enable_no_error(self, plugin):
        """on_enable should not raise any exception."""
        plugin.on_enable()

    def test_on_disable_no_error(self, plugin):
        """on_disable should not raise any exception."""
        plugin.on_disable()
