"""Parse user-owned connection files in memory; never load process settings."""

from io import StringIO
from dotenv.parser import parse_stream
from automation_config import Settings, credential

MAX_CONNECTION_FILE = 64 * 1024
FIELDS = {'ZTB_API_BASE', 'ZIA_API_BASE', 'API_KEY', 'ZPA_ENABLED', 'ZPA_BASE_URL',
          'ZPA_CLIENT_ID', 'ZPA_CLIENT_SECRET', 'ZPA_CUSTOMER_ID', 'ZPA_ENROLLMENT_CERT_NAME'}
# Older CLI files contain refreshed tokens. Always authenticate using the supplied keys.
IGNORED = {'BEARER', 'ZPA_BEARER', 'ZPA_BEARER_EXPIRES_AT', 'ZTB_REFERER_PATH'}


def parse_connection_file(content):
    if not isinstance(content, str) or not content.strip():
        raise ValueError('Choose a populated UTF-8 .env or .txt connection file.')
    try:
        size = len(content.encode('utf-8'))
    except UnicodeError:
        raise ValueError('Choose a plain-text UTF-8 connection file.') from None
    if size > MAX_CONNECTION_FILE:
        raise ValueError('Connection files must be 64 KB or smaller.')
    if '\x00' in content:
        raise ValueError('Choose a plain-text UTF-8 connection file.')
    values = {}
    for item in parse_stream(StringIO(content.lstrip('\ufeff'))):
        if item.error:
            raise ValueError('Invalid connection file syntax. Use the example and check quotes and KEY=value lines.')
        if item.key is None:
            continue
        if item.key not in FIELDS | IGNORED:
            raise ValueError('Unsupported setting in connection file. Use only settings from the example.')
        if item.key in values or item.value is None:
            raise ValueError('Each connection setting must appear once and use KEY=value syntax.')
        values[item.key] = item.value.strip()
    def get(key, default=''):
        return values.get(key, default)
    config = Settings(ztb_api_base=get('ZTB_API_BASE') or get('ZIA_API_BASE'), api_key=credential(get('API_KEY')),
        zpa_enabled=get('ZPA_ENABLED'), zpa_base_url=get('ZPA_BASE_URL'),
        zpa_client_id=credential(get('ZPA_CLIENT_ID')), zpa_client_secret=credential(get('ZPA_CLIENT_SECRET')),
        zpa_customer_id=credential(get('ZPA_CUSTOMER_ID')),
        zpa_enrollment_cert_name=get('ZPA_ENROLLMENT_CERT_NAME') or 'Connector')
    if config.errors():
        raise ValueError('Connection file needs a valid HTTPS ZTB_API_BASE and a populated API_KEY.')
    enabled = config.zpa_enabled.lower()
    if enabled not in ('', 'true', '1', 'yes', 'y', 'false', '0', 'no', 'n'):
        raise ValueError('Set ZPA_ENABLED to true or false.')
    use_zpa = enabled in ('true', '1', 'yes', 'y') or (not enabled and bool(config.zpa_client_id or config.zpa_client_secret))
    if use_zpa and config.errors(require_ztb=False, require_zpa=True):
        raise ValueError('Complete the ZPA API URL, client ID and client secret, or set ZPA_ENABLED=false.')
    connection = {'tenant_url': config.ztb_api_base, 'api_key': config.api_key}
    zpa = {key: getattr(config, 'zpa_' + key) for key in
           ('base_url', 'client_id', 'client_secret', 'customer_id', 'enrollment_cert_name')} if use_zpa else None
    return connection, zpa
