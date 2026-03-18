"""Tests for Redis distributed lock utility."""

from unittest.mock import MagicMock, patch

from app.core.redis_lock import acquire_lock, release_lock


class TestRedisLock:
    def test_acquire_and_release(self):
        mock_redis = MagicMock()
        mock_redis.set.return_value = True
        with patch("app.core.redis_lock._get_redis", return_value=mock_redis):
            assert acquire_lock("test_key", timeout=30) is True
            mock_redis.set.assert_called_once_with("test_key", "1", nx=True, ex=30)

            release_lock("test_key")
            mock_redis.delete.assert_called_once_with("test_key")

    def test_acquire_fails_when_held(self):
        mock_redis = MagicMock()
        mock_redis.set.return_value = False
        with patch("app.core.redis_lock._get_redis", return_value=mock_redis):
            assert acquire_lock("test_key") is False

    def test_redis_unavailable_falls_back_to_open(self):
        with patch("app.core.redis_lock._get_redis", return_value=None):
            assert acquire_lock("test_key") is True

    def test_redis_error_falls_back_to_open(self):
        mock_redis = MagicMock()
        mock_redis.set.side_effect = Exception("Connection refused")
        with patch("app.core.redis_lock._get_redis", return_value=mock_redis):
            assert acquire_lock("test_key") is True

    def test_release_no_redis(self):
        with patch("app.core.redis_lock._get_redis", return_value=None):
            release_lock("test_key")  # Should not raise
