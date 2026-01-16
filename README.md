# Infomaniak kDrive Backup Agent for Home Assistant

## Description
This integration allows you to sync and save your Home Assistant backups directly to Infomaniak kDrive.

## Features
- API Token Connection: Create your token at https://manager.infomaniak.com/v3/ by navigating to My Profile > Developer > API Tokens, then Create a token with the "Drive" scope selected.
- Simplified Input: Simply paste the full kDrive folder URL (e.g., https://ksuite.infomaniak.com/all/kdrive/app/drive/12345/files/67890).
- Enriched Filenames: Backups are saved as suggested_filename__id-<backup_id>__ver-<ha_version>__prot-<true|false>.tar.
- Accurate Sizing: Real file size verification via HEAD/GET requests.
- Retention Policy: Retention settings are aligned with your Home Assistant configuration.

## Installation
1. Copy the custom_components/infomaniak_kdrive folder into your Home Assistant configuration directory or via HACS
2. Restart Home Assistant.

## Configuration
- API Token: Enter your generated API token.
- Folder URL: Enter the folder URL as shown in the example below.

## URL Example
```
https://ksuite.infomaniak.com/all/kdrive/app/drive/12345/files/67890
```
The integration will automatically extract `drive_id=1234` and `folder_id=67890` from this link.