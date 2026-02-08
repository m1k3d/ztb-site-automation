# ZPA Provisioning Configuration

## Overview
The `zpa_provisioning.py` script now automatically creates App Connector Provisioning Keys that match your existing configuration pattern.

## How It Works

When `appc_provision=1` in `sites.csv`, the script will:

1. **Authenticate to ZPA** using credentials from `.env`
2. **Fetch Enrollment Certificate** (default: "Connector" signing certificate)
3. **Fetch App Connector Group** (auto-selects first available or uses specified group)
4. **Create Provisioning Key** with:
   - Name: `{site_name}` (e.g., "Branch1")
   - Maximum Usage: `2` (default, matches your screenshot)
   - App Connector Group: Auto-detected or specified
   - Signing Certificate: "Connector" (standard)
5. **Configure ZTB Site** with the generated provisioning key

## Required Environment Variables

Add these to your `.env` file:

```bash
# ZPA API Configuration
ZPA_ENABLED=true
ZPA_CLIENT_ID="your-client-id"
ZPA_CLIENT_SECRET="your-client-secret"
ZPA_CUSTOMER_ID="your-customer-id"
# Any ZPA cloud is supported. Examples:
# ZPA_BASE_URL="https://config.private.zscaler.com"
# ZPA_BASE_URL="https://config.zscalerthree.net"
# ZPA_BASE_URL="private.zscaler.com"   # also accepted (auto-normalized)
ZPA_BASE_URL="https://config.private.zscaler.com"
```



## Usage

### In sites.csv

Set `appc_provision=1` for sites that should get ZPA App Connector provisioning:

```csv
site_name,gateway_name,template_name,vlans_file,post,appc_provision
Branch1,Branch1-gw-1,zt600-SA-prod-DHCP-Relay,/path/to/vlans.csv,1,1
```

### Run the Script

```bash
# Dry run to preview
python3 bulk_create.py --dry-run

# Execute provisioning
python3 bulk_create.py
```

## What Gets Created

For each site with `appc_provision=1`:

1. **ZPA Provisioning Key**:
   - Name: Same as site name (e.g., "Branch1")
   - Max Usage: 2
   - App Connector Group: Auto-detected
   - Signing Certificate: "Connector"

2. **ZTB Configuration**:
   - Cluster gets configured with the provisioning key
   - App Connector can register automatically

## Troubleshooting

### "Failed to get enrollment certificate ID"
- Verify ZPA credentials are correct
- Check that you have access to enrollment certificates in ZPA console

### "Failed to get App Connector Group ID"
- Ensure at least one App Connector Group exists in your ZPA tenant
- Or ensure a group is available in the ZPA portal

### "Failed to create ZPA Provisioning Key"
- Check the error message for details
- Verify your ZPA user has permission to create provisioning keys
- Ensure the App Connector Group exists

## Example Output

```
🚀 Starting ZPA Provisioning for Branch1...
   ✅ Created provisioning key with App Connector Group
   🔑 Generated ZPA Key: AbCdEfGhIj...
   ✅ Updated ZTB Cluster 12345 with ZPA Key
```
