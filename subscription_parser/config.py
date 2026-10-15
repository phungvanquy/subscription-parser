"""Deployment configuration supplied by the process environment."""
import os

from .common import SubscriptionError


def admin_key():
    value = os.environ.get('PARSER_ADMIN_TOKEN', '')
    if not value or not value.strip():
        raise SubscriptionError('set PARSER_ADMIN_TOKEN in the deployment .env before starting the app')
    if len(value) > 256 or value != value.strip() or not value.isprintable():
        raise SubscriptionError('PARSER_ADMIN_TOKEN must contain 1–256 printable characters without surrounding whitespace')
    return value
