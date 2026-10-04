"""Keep connection credentials out of portable configuration and saved drafts."""
import json
import re
import unicodedata


def is_credential_field(key):
    normalized = re.sub(r'[^a-z]', '', unicodedata.normalize('NFKC', key).casefold())
    return (normalized in {'auth', 'bearer', 'credential', 'credentials', 'connection', 'connections', 'creds'}
            or any(part in normalized for part in (
                'authorization', 'bearer', 'password', 'passwd', 'secret', 'token', 'apikey',
                'privatekey', 'provisioningkey', 'credential')))


def clean_values(value):
    """Return a copy without credential fields, including JSON stored in CSV cells.

    Values in ordinary configuration fields are not guessed to be credentials.
    Unchanged JSON text retains its exact representation for CSV round trips.
    """
    if isinstance(value, dict):
        return {key: clean_values(item) for key, item in value.items()
                if not isinstance(key, str) or not is_credential_field(key)}
    if isinstance(value, list):
        return [clean_values(item) for item in value]
    if isinstance(value, str) and value.lstrip().startswith(('{', '[')):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return value
        cleaned = clean_values(parsed)
        if cleaned != parsed:
            return json.dumps(cleaned, ensure_ascii=False)
    return value


def reject_credential_fields(value):
    if clean_values(value) != value:
        # Never echo a supplied key or value: even a header may contain a secret.
        raise ValueError('CSV contains credential fields. Remove API keys, tokens, passwords, '
                         'and secrets, then import again. Enter credentials only in the connection dialog.')
