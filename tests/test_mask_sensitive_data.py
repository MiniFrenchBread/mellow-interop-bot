import unittest
from unittest.mock import Mock
import sys
import os

# Add the src directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config.mask_sensitive_data import (
    mask_sensitive_data,
    mask_url_credentials,
    mask_source_sensitive_data,
    mask_all_sensitive_config_data,
)
from config.read_config import Config, SourceConfig, SafeGlobal, Deployment


class TestMaskSensitiveData(unittest.TestCase):

    def test_mask_sensitive_data_basic(self):
        """Test basic masking of sensitive data"""
        message = "Error with key: 0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        sensitive = "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"

        result = mask_sensitive_data(message, sensitive)

        # Should not contain the full sensitive value
        self.assertNotIn(sensitive, result)
        # Should contain masked version (first 4 chars + asterisks)
        self.assertIn("0xab" + "*" * 62, result)

    def test_mask_sensitive_data_short_value(self):
        """Test that short values are not masked"""
        message = "Error with key: short"
        sensitive = "short"

        result = mask_sensitive_data(message, sensitive)

        # Should remain unchanged for short values
        self.assertEqual(message, result)

    def test_mask_sensitive_data_none(self):
        """Test that None values don't cause errors"""
        message = "Error occurred"

        result = mask_sensitive_data(message, None)

        # Should remain unchanged
        self.assertEqual(message, result)

    def test_mask_sensitive_data_empty_string(self):
        """Test that empty strings don't cause errors"""
        message = "Error occurred"

        result = mask_sensitive_data(message, "")

        # Should remain unchanged
        self.assertEqual(message, result)


class TestMaskUrlCredentials(unittest.TestCase):

    def test_mask_url_with_api_key_in_query(self):
        """Test masking URL with API key in query parameters"""
        message = "Failed to connect to https://mainnet.infura.io/v3/abc123def456?apikey=secret123"
        url = "https://mainnet.infura.io/v3/abc123def456?apikey=secret123"

        result = mask_url_credentials(message, url)

        # Should not contain the full URL
        self.assertNotIn(url, result)
        # Should contain masked version with domain visible
        self.assertIn("https://mainnet.infura.io/***", result)

    def test_mask_url_with_auth_credentials(self):
        """Test masking URL with authentication in URL"""
        message = "Failed to connect to https://user:password@example.com/rpc"
        url = "https://user:password@example.com/rpc"

        result = mask_url_credentials(message, url)

        # Should not contain the full URL with credentials
        self.assertNotIn("user:password", result)

    def test_mask_url_with_api_key_in_path(self):
        """Test masking URL with API key in path"""
        message = "RPC error: https://mainnet.infura.io/v3/abc123def456"
        url = "https://mainnet.infura.io/v3/abc123def456"

        result = mask_url_credentials(message, url)

        # Should contain domain but mask the rest
        self.assertIn("https://mainnet.infura.io/***", result)

    def test_mask_url_simple_no_credentials(self):
        """Test that simple URLs without credentials are not masked"""
        message = "Failed to connect to https://example.com"
        url = "https://example.com"

        result = mask_url_credentials(message, url)

        # Should remain unchanged for simple URLs
        self.assertEqual(message, result)

    def test_mask_url_none(self):
        """Test that None URL doesn't cause errors"""
        message = "Error occurred"

        result = mask_url_credentials(message, None)

        # Should remain unchanged
        self.assertEqual(message, result)

    def test_mask_comma_separated_masks_single_leaked_endpoint(self):
        """A real error contains only ONE of several configured endpoints; it must still be masked."""
        configured = (
            "https://primary.example.com/v3/KEY1," "https://backup.example.com/v3/KEY2"
        )
        # Transport error mentions only the endpoint that actually failed.
        message = "Failed to connect to https://backup.example.com/v3/KEY2"

        result = mask_url_credentials(message, configured)

        self.assertNotIn("KEY2", result)
        self.assertIn("https://backup.example.com/***", result)

    def test_mask_comma_separated_masks_all_endpoints(self):
        """When several endpoints appear in one message, each is masked independently."""
        configured = (
            "https://primary.example.com/v3/KEY1, " "https://backup.example.com/v3/KEY2"
        )
        message = (
            "Tried https://primary.example.com/v3/KEY1 then "
            "https://backup.example.com/v3/KEY2"
        )

        result = mask_url_credentials(message, configured)

        self.assertNotIn("KEY1", result)
        self.assertNotIn("KEY2", result)
        self.assertIn("https://primary.example.com/***", result)
        self.assertIn("https://backup.example.com/***", result)


class TestMaskSourceSensitiveData(unittest.TestCase):

    def setUp(self):
        """Set up test fixtures with mock source config"""
        self.safe_global = SafeGlobal(
            safe_address="0x1234567890123456789012345678901234567890",
            proposer_private_key="0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            api_url="https://safe-api.example.com",
            api_key="safe-api-key-12345678",
            web_client_url="https://safe-web.example.com",
            eip_3770="eth",
        )

        self.source_config = SourceConfig(
            name="Ethereum",
            rpc="https://mainnet.infura.io/v3/abc123def456?apikey=secret123",
            source_core_helper="0x1111111111111111111111111111111111111111",
            deployments=(
                Deployment(
                    name="ETH-BSC",
                    source_core="0x2222222222222222222222222222222222222222",
                    target_core="0x3333333333333333333333333333333333333333",
                ),
            ),
            safe_global=self.safe_global,
        )

    def test_mask_source_rpc_url(self):
        """Test masking source RPC URL"""
        message = f"RPC error: {self.source_config.rpc}"

        result = mask_source_sensitive_data(message, self.source_config)

        # Should not contain the full URL with API key
        self.assertNotIn("abc123def456", result)
        self.assertNotIn("secret123", result)
        # Should contain domain but mask credentials
        self.assertIn("https://mainnet.infura.io/***", result)

    def test_mask_source_proposer_private_key(self):
        """Test masking source proposer private key"""
        private_key = self.source_config.safe_global.proposer_private_key
        message = f"Signing error with key {private_key}"

        result = mask_source_sensitive_data(message, self.source_config)

        # Should not contain the full private key
        self.assertNotIn(private_key, result)
        # Should contain masked version
        self.assertIn("0xab" + "*" * 62, result)

    def test_mask_source_safe_api_key(self):
        """Test masking source safe API key"""
        api_key = self.source_config.safe_global.api_key
        message = f"Safe API error with key {api_key}"

        result = mask_source_sensitive_data(message, self.source_config)

        # Should not contain the full API key
        self.assertNotIn(api_key, result)
        # Should contain masked version
        self.assertIn("safe***", result)

    def test_mask_source_multiple_sensitive_values(self):
        """Test masking multiple sensitive values from source"""
        message = (
            f"Error: RPC {self.source_config.rpc}, "
            f"Private key {self.source_config.safe_global.proposer_private_key}, "
            f"API key {self.source_config.safe_global.api_key}"
        )

        result = mask_source_sensitive_data(message, self.source_config)

        # Should not contain any full sensitive values
        self.assertNotIn("abc123def456", result)
        self.assertNotIn(self.source_config.safe_global.proposer_private_key, result)
        self.assertNotIn(self.source_config.safe_global.api_key, result)

        # Should contain masked versions
        self.assertIn("https://mainnet.infura.io/***", result)
        self.assertIn("0xab" + "*" * 62, result)
        self.assertIn("safe***", result)

    def test_mask_source_with_none_source(self):
        """Test that None source doesn't cause errors"""
        message = "Error occurred"

        result = mask_source_sensitive_data(message, None)

        # Should remain unchanged
        self.assertEqual(message, result)

    def test_mask_source_without_safe_global(self):
        """Test masking source that has no safe_global"""
        source_no_safe = SourceConfig(
            name="Polygon",
            rpc="https://polygon-rpc.infura.io/v3/xyz789",
            source_core_helper="0x7777777777777777777777777777777777777777",
            deployments=(),
            safe_global=None,
        )

        message = f"RPC error: {source_no_safe.rpc}"
        result = mask_source_sensitive_data(message, source_no_safe)

        # Should still mask RPC URL even without safe_global
        self.assertNotIn("xyz789", result)
        self.assertIn("https://polygon-rpc.infura.io/***", result)

    def test_mask_source_with_none_api_key(self):
        """Test masking source with None API key"""
        safe_global_no_api_key = SafeGlobal(
            safe_address="0x1234567890123456789012345678901234567890",
            proposer_private_key="0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            api_url="https://safe-api.example.com",
            api_key=None,
            web_client_url="https://safe-web.example.com",
            eip_3770="eth",
        )

        source = SourceConfig(
            name="Test",
            rpc="https://test-rpc.example.com/v3/secret",
            source_core_helper="0x1111111111111111111111111111111111111111",
            deployments=(),
            safe_global=safe_global_no_api_key,
        )

        message = f"Error with private key {source.safe_global.proposer_private_key}"
        result = mask_source_sensitive_data(message, source)

        # Should still mask private key
        self.assertNotIn(source.safe_global.proposer_private_key, result)
        self.assertIn("0xab" + "*" * 62, result)


class TestMaskAllSensitiveConfigData(unittest.TestCase):

    def setUp(self):
        """Set up test fixtures with mock config"""
        self.safe_global = SafeGlobal(
            safe_address="0x1234567890123456789012345678901234567890",
            proposer_private_key="0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
            api_url="https://safe-api.example.com",
            api_key="safe-api-key-12345678",
            web_client_url="https://safe-web.example.com",
            eip_3770="eth",
        )

        self.source_config = SourceConfig(
            name="Ethereum",
            rpc="https://mainnet.infura.io/v3/abc123def456?apikey=secret123",
            source_core_helper="0x1111111111111111111111111111111111111111",
            deployments=(
                Deployment(
                    name="ETH-BSC",
                    source_core="0x2222222222222222222222222222222222222222",
                    target_core="0x3333333333333333333333333333333333333333",
                ),
            ),
            safe_global=self.safe_global,
        )

        self.config = Config(
            telegram_bot_api_key="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz",
            telegram_group_chat_id="-1001234567890",
            telegram_owner_nicknames={
                "alice": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "bob": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
            telegram_proposal_message_prefix="[Proposal]",
            oracle_expiry_threshold_seconds=3600,
            oracle_recent_update_threshold_seconds=300,
            target_rpc="https://bsc-mainnet.infura.io/v3/xyz789?apikey=target_secret",
            target_core_helper="0x4444444444444444444444444444444444444444",
            sources=[self.source_config],
        )

    def test_mask_telegram_bot_api_key(self):
        """Test masking telegram bot API key"""
        message = f"Telegram error with key {self.config.telegram_bot_api_key}"

        result = mask_all_sensitive_config_data(message, self.config)

        # Should not contain the full API key
        self.assertNotIn(self.config.telegram_bot_api_key, result)
        # Should contain masked version
        self.assertIn("1234***", result)

    def test_mask_telegram_group_chat_id(self):
        """Test masking telegram group chat ID"""
        message = f"Failed to send to chat {self.config.telegram_group_chat_id}"

        result = mask_all_sensitive_config_data(message, self.config)

        # Should not contain the full chat ID
        self.assertNotIn(self.config.telegram_group_chat_id, result)
        # Should contain masked version
        self.assertIn("-100***", result)

    def test_mask_telegram_owner_addresses(self):
        """Test masking telegram owner addresses"""
        alice_address = self.config.telegram_owner_nicknames["alice"]
        bob_address = self.config.telegram_owner_nicknames["bob"]
        message = f"Owners: {alice_address} and {bob_address}"

        result = mask_all_sensitive_config_data(message, self.config)

        # Should not contain full addresses
        self.assertNotIn(alice_address, result)
        self.assertNotIn(bob_address, result)
        # Should contain masked versions
        self.assertIn("0xaa" + "*" * 38, result)
        self.assertIn("0xbb" + "*" * 38, result)

    def test_mask_source_rpc_url(self):
        """Test masking source RPC URL with credentials"""
        message = f"RPC error: {self.source_config.rpc}"

        result = mask_all_sensitive_config_data(message, self.config)

        # Should not contain the full URL with API key
        self.assertNotIn("abc123def456", result)
        self.assertNotIn("secret123", result)
        # Should contain domain but mask credentials
        self.assertIn("https://mainnet.infura.io/***", result)

    def test_mask_target_rpc_url(self):
        """Test masking target RPC URL with credentials"""
        message = f"Target RPC error: {self.config.target_rpc}"

        result = mask_all_sensitive_config_data(message, self.config)

        # Should not contain the full URL with API key
        self.assertNotIn("xyz789", result)
        self.assertNotIn("target_secret", result)
        # Should contain domain but mask credentials
        self.assertIn("https://bsc-mainnet.infura.io/***", result)

    def test_mask_proposer_private_key(self):
        """Test masking proposer private key"""
        private_key = self.safe_global.proposer_private_key
        message = f"Signing error with key {private_key}"

        result = mask_all_sensitive_config_data(message, self.config)

        # Should not contain the full private key
        self.assertNotIn(private_key, result)
        # Should contain masked version
        self.assertIn("0xab" + "*" * 62, result)

    def test_mask_safe_api_key(self):
        """Test masking safe API key"""
        api_key = self.safe_global.api_key
        message = f"Safe API error with key {api_key}"

        result = mask_all_sensitive_config_data(message, self.config)

        # Should not contain the full API key
        self.assertNotIn(api_key, result)
        # Should contain masked version
        self.assertIn("safe***", result)

    def test_mask_multiple_sensitive_values(self):
        """Test masking multiple sensitive values in one message"""
        message = (
            f"Error: Telegram key {self.config.telegram_bot_api_key}, "
            f"RPC {self.source_config.rpc}, "
            f"Private key {self.safe_global.proposer_private_key}"
        )

        result = mask_all_sensitive_config_data(message, self.config)

        # Should not contain any full sensitive values
        self.assertNotIn(self.config.telegram_bot_api_key, result)
        self.assertNotIn("abc123def456", result)
        self.assertNotIn(self.safe_global.proposer_private_key, result)

        # Should contain masked versions
        self.assertIn("1234***", result)
        self.assertIn("https://mainnet.infura.io/***", result)
        self.assertIn("0xab" + "*" * 62, result)

    def test_mask_with_none_config(self):
        """Test that None config doesn't cause errors"""
        message = "Error occurred"

        result = mask_all_sensitive_config_data(message, None)

        # Should remain unchanged
        self.assertEqual(message, result)

    def test_mask_with_source_without_safe_global(self):
        """Test masking with source that has no safe_global"""
        source_no_safe = SourceConfig(
            name="Polygon",
            rpc="https://polygon-rpc.example.com",
            source_core_helper="0x7777777777777777777777777777777777777777",
            deployments=(),
            safe_global=None,
        )

        config = Config(
            telegram_bot_api_key="test_key_12345678",
            telegram_group_chat_id="-100123456789",
            telegram_owner_nicknames={},
            telegram_proposal_message_prefix="",
            oracle_expiry_threshold_seconds=3600,
            oracle_recent_update_threshold_seconds=300,
            target_rpc="https://example.com",
            target_core_helper="0x4444444444444444444444444444444444444444",
            sources=[source_no_safe],
        )

        message = f"Error with key test_key_12345678"
        result = mask_all_sensitive_config_data(message, config)

        # Should still mask telegram key even without safe_global
        self.assertNotIn("test_key_12345678", result)
        self.assertIn("test***", result)

    def test_mask_empty_telegram_owner_nicknames(self):
        """Test masking with empty telegram owner nicknames"""
        config = Config(
            telegram_bot_api_key="test_key_12345678",
            telegram_group_chat_id="-100123456789",
            telegram_owner_nicknames={},
            telegram_proposal_message_prefix="",
            oracle_expiry_threshold_seconds=3600,
            oracle_recent_update_threshold_seconds=300,
            target_rpc="https://example.com",
            target_core_helper="0x4444444444444444444444444444444444444444",
            sources=[],
        )

        message = "Error occurred"
        result = mask_all_sensitive_config_data(message, config)

        # Should not crash and return message
        self.assertEqual(message, result)


class TestExecutorKeyMasking(unittest.TestCase):
    """The scheduler pipes failure text through the masker on its way to Telegram.

    That is the one path where a key held in config leaves the machine.
    """

    EXECUTOR_KEY = "0x" + "ab" * 32

    def make_source(self):
        return SourceConfig(
            name="OG",
            rpc="https://rpc.invalid/token",
            source_core_helper="0x" + "11" * 20,
            deployments=(),
            executor_private_key=self.EXECUTOR_KEY,
        )

    def test_the_executor_key_is_masked(self):
        message = f"boom while signing with {self.EXECUTOR_KEY}"

        masked = mask_source_sensitive_data(message, self.make_source())

        self.assertNotIn(self.EXECUTOR_KEY, masked)

    def test_it_is_masked_through_the_whole_config_helper(self):
        config = Config(
            telegram_bot_api_key="",
            telegram_group_chat_id="",
            telegram_owner_nicknames={},
            telegram_proposal_message_prefix="",
            oracle_expiry_threshold_seconds=3600,
            oracle_recent_update_threshold_seconds=0,
            target_rpc="",
            target_core_helper="",
            sources=[self.make_source()],
        )

        masked = mask_all_sensitive_config_data(f"boom {self.EXECUTOR_KEY}", config)

        self.assertNotIn(self.EXECUTOR_KEY, masked)


class TestDeploymentOverrideMasking(unittest.TestCase):
    """A deployment can override the Safe config, keys included.

    The proposal path uses the merged per-deployment config, so an override's
    own key is what appears in an error from it -- and errors reach Telegram.
    """

    OVERRIDE_KEY = "0x" + "cd" * 32
    OVERRIDE_API_KEY = "override-api-key-value"

    def source(self):
        return SourceConfig(
            name="OG",
            rpc="https://rpc.invalid",
            source_core_helper="0x" + "11" * 20,
            deployments=(
                Deployment(
                    name="OG",
                    source_core="0x" + "22" * 20,
                    target_core="0x" + "33" * 20,
                    safe_global=SafeGlobal(
                        safe_address="0x" + "fc" * 20,
                        proposer_private_key=self.OVERRIDE_KEY,
                        api_url="https://api.safe.global",
                        api_key=self.OVERRIDE_API_KEY,
                    ),
                ),
            ),
        )

    def test_an_override_proposer_key_is_masked(self):
        masked = mask_source_sensitive_data(
            "signing failed with " + self.OVERRIDE_KEY, self.source()
        )

        self.assertNotIn(self.OVERRIDE_KEY, masked)

    def test_an_override_api_key_is_masked(self):
        masked = mask_source_sensitive_data(
            "rejected: " + self.OVERRIDE_API_KEY, self.source()
        )

        self.assertNotIn(self.OVERRIDE_API_KEY, masked)


if __name__ == "__main__":
    unittest.main()
