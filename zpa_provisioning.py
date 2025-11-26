#!/usr/bin/env python3
"""
zpa_provisioning.py
- Creates ZPA App Connector Provisioning Keys
- Configures ZTB sites with the generated keys
"""

import os
import sys
import json
import requests
import base64
from typing import Dict, Any, Optional, Tuple

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
            certs = data.get("list", []) or data
            
            if not isinstance(certs, list):
                continue
            
            # Look for the certificate by name
            for cert in certs:
                if cert.get("name", "").lower() == cert_name.lower():
                    return cert.get("id")
            
            # If not found, return the first one as fallback
            if certs:
                print(f"   ⚠️  Certificate '{cert_name}' not found, using: {certs[0].get('name')}", file=sys.stderr)
                return certs[0].get("id")
            
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

def _get_zpn_base_url(base_url: str) -> str:
    """
    Helper to switch from config.zpatwo.net to api.zpatwo.net for ZPN endpoints if needed.
    """
    if "config.zpatwo.net" in base_url:
        return base_url.replace("config.zpatwo.net", "api.zpatwo.net")
    return base_url

def create_app_connector_group(base_url: str, customer_id: str, token: str, name: str, city: str, country: str, dry_run: bool = False) -> Optional[str]:
    """
    Creates an App Connector Group using the assistantGroup endpoint.
    Returns the group ID (zcomponentId).
    """
    if dry_run:
        print(f"   [DRY-RUN] Would create App Connector Group: name='{name}', location='{city}, {country}'")
        return "dry-run-group-id-123"

    # ZPN endpoints often use api.zpatwo.net instead of config.zpatwo.net
    zpn_base = _get_zpn_base_url(base_url)
    url = f"{zpn_base}/zpn/api/v1/admin/customers/{customer_id}/assistantGroup?scopeId=0"
    headers = get_zpa_headers(token)
    
    lat, lon = get_geo_location(city, country)
    location_str = f"{city}, {country}" if city and country else (city or country or "Unknown")
    
    payload = {
        "enabled": True,
        "isPublic": False,
        "location": location_str,
        "dnsQueryType": "IPV4_IPV6",
        "countryCode": "NL", # Defaulting to NL based on screenshot, but should ideally map from country name
        "dcHostingInfo": "",
        "description": f"Auto-created for {name}",
        "latitude": lat,
        "longitude": lon,
        "name": name,
        "objectType": "ConnectorGroup",
        "overrideVersionProfile": False,
        "praEnabled": False,
        "tcpQuickAckApp": False,
        "tcpQuickAckAssistant": False,
        "tcpQuickAckReadAssistant": False,
        "trustedNetworks": [],
        "upgradeDay": "SUNDAY",
        "upgradeTimeInSecs": "82800",
        "useInDrMode": False,
        "versionProfileToggleBox": "0",
        "wafDisabled": False
    }
    
    # Simple country code mapping (can be expanded)
    if country.lower() in ("united states", "usa", "us"): payload["countryCode"] = "US"
    elif country.lower() in ("united kingdom", "uk", "gb"): payload["countryCode"] = "GB"
    elif country.lower() in ("germany", "de"): payload["countryCode"] = "DE"
    elif country.lower() in ("france", "fr"): payload["countryCode"] = "FR"
    
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
    Creates an App Connector Provisioning Key using the nonce endpoint.
    """
    if dry_run:
        print(f"   [DRY-RUN] Would create ZPA Provisioning Key: name='{name}', maxUsage={max_usage}")
        return "dry-run-key-12345"

    # ZPN endpoints often use api.zpatwo.net instead of config.zpatwo.net
    zpn_base = _get_zpn_base_url(base_url)
    url = f"{zpn_base}/zpn/api/v1/admin/customers/{customer_id}/associationType/ASSISTANT_GRP/nonce?scopeId=0"
    headers = get_zpa_headers(token)
    
    payload = {
        "enabled": True,
        "exportable": True,
        "name": name,
        "maxUsage": str(max_usage),
        "autoSign": 1,
        "nonceAssociationType": "ASSISTANT_GRP",
        "objectType": "ConnectorNonce",
        "signingCertId": enrollment_cert_id,
        "usageCount": 0,
        "zcomponentId": group_id
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        # The key is in the 'nonce' field based on the screenshot response
        # But sometimes it might be 'provisioningKey'. The screenshot shows the response header/preview but not the full body clearly for the key itself, 
        # but the user said "nonce field ... which has the provision key".
        # Let's check both.
        key = data.get("nonce") or data.get("provisioningKey")
        
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
        return ""
    
    # 3. Create the provisioning key
    url = f"{base_url}/mgmtconfig/v1/admin/customers/{customer_id}/associationType/CONNECTOR/provisioningKey"
    headers = get_zpa_headers(token)
    
    payload = {
        "associationType": "CONNECTOR",
        "name": name,
        "maxUsage": max_usage,
        "enrollmentCertId": enrollment_cert_id,
        "zcomponentId": app_connector_group_id
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        prov_key = resp.json().get("provisioningKey", "")
        if prov_key:
            print(f"   ✅ Created provisioning key with App Connector Group")
        return prov_key
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

def provision_zpa_for_site(row: Dict[str, str], ztb_session: requests.Session, ztb_api_base: str, cluster_id: int, dry_run: bool = False) -> bool:
    """
    Main orchestrator function for a single site row.
    """
    site_name = row.get("site_name")
    if not site_name:
        print("⚠️ Skipping ZPA provisioning: No site_name", file=sys.stderr)
        return False

    print(f"🚀 Starting ZPA Provisioning for {site_name}...")

    # 1. Get ZPA Token
    # We assume zpa_login can be called or we have env vars.
    # Let's try to refresh/get token using zpa_login logic
    try:
        token, _ = zpa_login.zpa_login(write_env=True, quiet=True)
    except Exception as e:
        print(f"❌ ZPA Login failed: {e}", file=sys.stderr)
        return False

    zpa_base = os.getenv("ZPA_BASE_URL", "").rstrip("/")
    if not zpa_base:
        print("❌ Missing ZPA_BASE_URL", file=sys.stderr)
        return False

    # 2. Get Customer ID
    customer_id = get_customer_id(token)
    if not customer_id:
        return False

    # 3. Get Enrollment Cert ID
    cert_name = os.getenv("ZPA_ENROLLMENT_CERT_NAME", "Connector")
    enrollment_cert_id = get_enrollment_cert_id(zpa_base, customer_id, token, cert_name)
    if not enrollment_cert_id:
        print(f"❌ Failed to get enrollment certificate ID", file=sys.stderr)
        return False

    # 4. Create App Connector Group (with location)
    # Extract city/country from row, defaulting if missing
    city = row.get("city", "").strip()
    country = row.get("country", "").strip()
    
    # Use site name for the group name
    group_name = site_name
    
    group_id = create_app_connector_group(zpa_base, customer_id, token, group_name, city, country, dry_run=dry_run)
    if not group_id:
        print(f"❌ Failed to create App Connector Group", file=sys.stderr)
        return False

    # 5. Create Provisioning Key
    # Use site name as key name for traceability
    key_name = site_name 
    prov_key = create_provisioning_key(zpa_base, customer_id, token, key_name, group_id, enrollment_cert_id, dry_run=dry_run)
    if not prov_key:
        return False
    
    print(f"   🔑 Generated ZPA Key: {prov_key[:10]}...")

    # 6. Update ZTB (No need to look up site_id anymore, we use cluster_id passed in)
    return update_ztb_site_zpa(ztb_session, ztb_api_base, cluster_id, site_name, prov_key, dry_run=dry_run)
