"""Credential boundary errors shared without importing database or application state."""


class AuthSeederError(Exception):
    """The run has no verified, isolated SuiteCloud account/role authorization."""
