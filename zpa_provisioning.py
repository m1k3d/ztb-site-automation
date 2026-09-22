#!/usr/bin/env python3
"""
zpa_provisioning.py
- Creates ZPA App Connector Provisioning Keys
- Add support all tenant types
- Configures ZTB sites with the generated keys
"""

import sys
import json
import requests
import base64
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from automation_config import Settings, normalize_base

# Import zpa_login to ensure we can get a token
try:
    import zpa_login
except ImportError:
    # If running from same dir, this should work. 
    # If not, we might need to adjust sys.path or rely on env vars.
    pass

def get_zpa_headers(token: str) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

def get_customer_id(token: str) -> str:
    """
    Extracts the Customer ID (custId) from the JWT token.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return ""
        payload = parts[1]
        padding = len(payload) % 4
        if padding:
            payload += "=" * (4 - padding)
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
        return str(claims.get("custId", ""))
    except Exception as e:
        print(f"❌ Failed to extract Customer ID from token: {e}", file=sys.stderr)
        return ""

def get_enrollment_cert_id(base_url: str, customer_id: str, token: str, cert_name: str = "Connector") -> Optional[str]:
    """
    Fetches the enrollment certificate ID by name.
    Default is "Connector" which is the standard signing certificate.
    """
    # Try v2 endpoint first, then v1
    endpoints = [
        f"{base_url}/mgmtconfig/v2/admin/customers/{customer_id}/enrollmentCert",
        f"{base_url}/mgmtconfig/v1/admin/customers/{customer_id}/enrollmentCert",
    ]
    
    headers = get_zpa_headers(token)
    
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            
            data = resp.json()
            certs = data.get("list", []) if isinstance(data, dict) else data
            
            if not isinstance(certs, list):
                continue
            
            # Look for the certificate by name
            for cert in certs:
                if cert.get("name", "").lower() == cert_name.lower():
                    return cert.get("id")
            
            # A named certificate is a requirement, not a suggestion.
            
        except Exception as e:
            if "404" not in str(e):
                print(f"   ⚠️  Error fetching from {url}: {e}", file=sys.stderr)
            continue
    
    print(f"   ⚠️  Failed to fetch enrollment certificates from all endpoints", file=sys.stderr)
    return None

def get_app_connector_group_id(base_url: str, customer_id: str, token: str, group_name: Optional[str] = None) -> Optional[str]:
    """
    Fetches the App Connector Group ID by name.
    If no group_name is provided, returns the first available group.
    """
    # Try v2 endpoint first, then v1
    endpoints = [
        f"{base_url}/mgmtconfig/v2/admin/customers/{customer_id}/appConnectorGroup",
        f"{base_url}/mgmtconfig/v1/admin/customers/{customer_id}/appConnectorGroup",
    ]
    
    headers = get_zpa_headers(token)
    
    for url in endpoints:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            
            data = resp.json()
            groups = data.get("list", []) or data
            
            if not isinstance(groups, list):
                continue
            
            if not groups:
                print(f"   ⚠️  No App Connector Groups found", file=sys.stderr)
                return None
            
            # If group_name is specified, look for it
            if group_name:
                for group in groups:
                    if group.get("name", "").lower() == group_name.lower():
                        return group.get("id")
                print(f"   ⚠️  App Connector Group '{group_name}' not found, using: {groups[0].get('name')}", file=sys.stderr)
            
            # Return the first group as fallback
            return groups[0].get("id")
            
        except Exception as e:
            if "404" not in str(e):
                print(f"   ⚠️  Error fetching from {url}: {e}", file=sys.stderr)
            continue
    
    print(f"   ⚠️  Failed to fetch App Connector Groups from all endpoints", file=sys.stderr)
    return None

def get_geo_location(city: str, country: str) -> Tuple[str, str]:
    """
    Get latitude and longitude for a city/country.
    Returns (lat, long) as strings. Default to "0.0", "0.0" on failure.
    """
    if not city:
        return "0.0", "0.0"
        
    try:
        # Use OpenStreetMap Nominatim API (no key required for low volume)
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": f"{city}, {country}",
            "format": "json",
            "limit": 1
        }
        headers = {"User-Agent": "ztb-automation-script"}
        resp = requests.get(url, params=params, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return str(data[0].get("lat", "0.0")), str(data[0].get("lon", "0.0"))
    except Exception as e:
        print(f"   ⚠️  Geocoding failed for {city}, {country}: {e}", file=sys.stderr)
    
    return "0.0", "0.0"




def create_app_connector_group(base_url: str, customer_id: str, token: str, name: str, city: str, country: str, dry_run: bool = False, *, enrollment_cert_id: str = "") -> Optional[str]:
    """
    Creates an App Connector Group using the standard mgmtconfig endpoint.
    Returns the group ID.
    """
    if not str(enrollment_cert_id or "").strip():
        raise ValueError("App Connector Group requires the resolved enrollment certificate ID")
    if dry_run:
        print(f"   [DRY-RUN] Would create App Connector Group: name='{name}', location='{city}, {country}'")
        return "dry-run-group-id-123"

    # Use mgmtconfig endpoint (usually on config.<cloud>)
    # Shared configuration normalizes base_url to config.<cloud>.
    url = f"{base_url}/mgmtconfig/v1/admin/customers/{customer_id}/appConnectorGroup"
    headers = get_zpa_headers(token)
    
    lat, lon = get_geo_location(city, country)
    location_str = f"{city}, {country}" if city and country else (city or country or "Unknown")
    
    # Map country to Code if possible
    country_code = "NL" # generic default from original code
    c_lower = country.lower()
    if c_lower in ("united states", "usa", "us"): country_code = "US"
    elif c_lower in ("united kingdom", "uk", "gb"): country_code = "GB"
    elif c_lower in ("germany", "de"): country_code = "DE"
    elif c_lower in ("france", "fr"): country_code = "FR"
    elif c_lower in ("australia", "au"): country_code = "AU"
    elif c_lower in ("canada", "ca"): country_code = "CA"
    elif c_lower in ("india", "in"): country_code = "IN"
    elif c_lower in ("japan", "jp"): country_code = "JP"
    elif c_lower in ("singapore", "sg"): country_code = "SG"
    elif c_lower in ("switzerland", "ch"): country_code = "CH"

    payload = {
        "name": name,
        # The API calls this signingCertId in validation errors, but the
        # request field is enrollmentCertId (also required on the group).
        "enrollmentCertId": str(enrollment_cert_id),
        "description": f"Auto-created for {name}",
        "enabled": True,
        "cityCountry": location_str,
        "countryCode": country_code,
        "latitude": lat,
        "longitude": lon,
        "location": location_str,
        "dnsQueryType": "IPV4_IPV6",
        "upgradeDay": "SUNDAY",
        "upgradeTimeInSecs": "7200",
        "overrideVersionProfile": True,
        "versionProfileId": "0", # Default / Recommended
        "lssAppConnectorGroup": False,
        "wafDisabled": False
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        print(f"   ✅ Created App Connector Group: {name} ({location_str})")
        return str(data.get("id"))
    except Exception as e:
        print(f"❌ Failed to create App Connector Group: {e}", file=sys.stderr)
        if hasattr(e, 'response') and e.response:
             print(f"   Response: {e.response.text}", file=sys.stderr)
        return None

def create_provisioning_key(base_url: str, customer_id: str, token: str, name: str, group_id: str, enrollment_cert_id: str, max_usage: int = 2, dry_run: bool = False) -> str:
    """
    Creates an App Connector Provisioning Key using the standard mgmtconfig endpoint.
    Association Type: CONNECTOR_GRP
    """
    if dry_run:
        print(f"   [DRY-RUN] Would create ZPA Provisioning Key: name='{name}', maxUsage={max_usage}")
        return "dry-run-key-12345"

    # Use mgmtconfig endpoint
    url = f"{base_url}/mgmtconfig/v1/admin/customers/{customer_id}/associationType/CONNECTOR_GRP/provisioningKey"
    headers = get_zpa_headers(token)
    
    payload = {
        "name": name,
        "maxUsage": str(max_usage),
        "enrollmentCertId": enrollment_cert_id,
        "zcomponentId": group_id,
        "associationType": "CONNECTOR_GRP"
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        # The key is usually in 'provisioningKey' or simply returned as the key string in some versions,
        # but standard API returns an object with 'key' or 'provisioningKey'.
        # Docs say: { "provisioningKey": "..." } or similar.
        # Let's check a few common fields.
        key = data.get("provisioningKey") or data.get("key")
        
        if key:
            print(f"   ✅ Created provisioning key: {name}")
            return key
        else:
            print(f"❌ Provisioning key not found in response: {data.keys()}", file=sys.stderr)
            return ""
            
    except Exception as e:
        print(f"❌ Failed to create ZPA Provisioning Key: {e}", file=sys.stderr)
        if hasattr(e, 'response') and e.response:
             print(f"   Response: {e.response.text}", file=sys.stderr)
        return ""

def update_ztb_site_zpa(ztb_session: requests.Session, ztb_api_base: str, cluster_id: int, name: str, provisioning_key: str, dry_run: bool = False) -> bool:
    """
    Updates the ZTB Site/Gateway with the ZPA Provisioning Key.
    POST /api/v3/appconnector/config
    """
    if dry_run:
        print(f"   [DRY-RUN] Would update ZTB Cluster {cluster_id} with ZPA Key")
        return True

    url = f"{ztb_api_base}/api/v3/appconnector/config"
    params = {"refresh_token": "enabled"}
    payload = {
        "cluster_id": cluster_id,
        "name": name,
        "provision_key": provisioning_key
    }
    
    try:
        resp = ztb_session.post(url, params=params, json=payload, timeout=30)
        if resp.status_code in (200, 201, 204):
            print(f"✅ Updated ZTB Cluster {cluster_id} with ZPA Key")
            return True
        else:
            print(f"❌ Failed to update ZTB Cluster {cluster_id}: {resp.status_code} {resp.text}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"❌ Error updating ZTB Cluster: {e}", file=sys.stderr)
        return False

@dataclass(frozen=True)
class ZPAContext:
    base_url: str
    customer_id: str
    token: str = field(repr=False)
    enrollment_cert_id: str


def prepare_zpa(config: Settings) -> ZPAContext:
    """Authenticate and resolve requirements without creating groups or keys."""
    errors = config.errors(require_ztb=False, require_zpa=True)
    if errors:
        raise ValueError("; ".join(errors))
    token, _ = zpa_login.zpa_login(config=config, write_env=True, quiet=True)
    base = normalize_base(config.zpa_base_url, zpa=True)
    token_customer = get_customer_id(token)
    if config.zpa_customer_id and token_customer and config.zpa_customer_id != token_customer:
        raise ValueError("ZPA_CUSTOMER_ID does not match the authenticated token customer")
    customer_id = config.zpa_customer_id or token_customer
    if not customer_id:
        raise ValueError("ZPA_CUSTOMER_ID: required when the token has no custId")
    certificate = get_enrollment_cert_id(base, customer_id, token, config.zpa_enrollment_cert_name)
    if not certificate:
        raise ValueError(f"ZPA_ENROLLMENT_CERT_NAME: could not resolve '{config.zpa_enrollment_cert_name}'")
    return ZPAContext(base, customer_id, token, certificate)


def provision_zpa_for_site(row: Dict[str, str], ztb_session: requests.Session, ztb_api_base: str, cluster_id: int, dry_run: bool = False, *, config: Optional[Settings] = None, context: Optional[ZPAContext] = None) -> bool:
    """
    Main orchestrator function for a single site row.
    """
    site_name = row.get("site_name")
    if not site_name:
        print("⚠️ Skipping ZPA provisioning: No site_name", file=sys.stderr)
        return False

    print(f"🚀 Starting ZPA Provisioning for {site_name}...")

    # The engine resolves requirements for the whole batch before creating sites.
    try:
        context = context or prepare_zpa(config or Settings.load())
    except Exception as exc:
        print(f"ZPA preflight failed: {exc}", file=sys.stderr)
        return False
    zpa_base, customer_id = context.base_url, context.customer_id
    token, enrollment_cert_id = context.token, context.enrollment_cert_id

    # 4. Create App Connector Group (with location)
    # Extract city/country from row, defaulting if missing
    city = row.get("city", "").strip()
    country = row.get("country", "").strip()
    
    # Use site name for the group name
    group_name = site_name
    
    group_id = create_app_connector_group(zpa_base, customer_id, token, group_name, city, country, dry_run=dry_run, enrollment_cert_id=enrollment_cert_id)
    if not group_id:
        print(f"❌ Failed to create App Connector Group", file=sys.stderr)
        return False

    # 5. Create Provisioning Key
    # Use site name as key name for traceability
    key_name = site_name 
    prov_key = create_provisioning_key(zpa_base, customer_id, token, key_name, group_id, enrollment_cert_id, dry_run=dry_run)
    if not prov_key:
        return False
    
    print("   🔑 Generated ZPA Key (redacted)")

    # 6. Update ZTB (No need to look up site_id anymore, we use cluster_id passed in)
    return update_ztb_site_zpa(ztb_session, ztb_api_base, cluster_id, site_name, prov_key, dry_run=dry_run)
