"""Deployment configuration supplied by the process environment."""
import os

from .common import SubscriptionError

MAX_PROVIDERS = 200


def env_int(name, default, minimum, maximum):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        number = int(value)
        if minimum <= number <= maximum:
            return number
    except ValueError:
        pass
    raise SubscriptionError(f'{name} must be an integer between {minimum} and {maximum}')


def admin_key():
    value = os.environ.get('PARSER_ADMIN_TOKEN', '')
    if not value or not value.strip():
        raise SubscriptionError('set PARSER_ADMIN_TOKEN in the deployment .env before starting the app')
    if len(value) > 256 or value != value.strip() or not value.isprintable():
        raise SubscriptionError('PARSER_ADMIN_TOKEN must contain 1–256 printable characters without surrounding whitespace')
    return value
