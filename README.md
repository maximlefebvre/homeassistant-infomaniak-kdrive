# Infomaniak kDrive Backup Agent for Home Assistant

## Description
This integration allows you to sync and save your Home Assistant backups directly to Infomaniak kDrive.

## Features
- API Token Connection (no need of WebDAV): 
- Simplified Input: Simply paste the full kDrive folder URL (e.g., https://ksuite.infomaniak.com/02468/kdrive/app/drive/12345/files/67890).
- Full Metadata: Each backup is stored as two files — the archive `suggested_filename__id-<backup_id>.tar` and a small sidecar `<backup_id>.metadata.json` holding the complete Home Assistant metadata (date, folders, add-ons, encryption, size…).
- Accurate Sizing: Size comes from the sidecar; older backups still fall back to HEAD/GET verification.
- Retention Policy: Retention settings are aligned with your Home Assistant configuration, oldest backups first.

## Upgrading from 0.5.x
Backups made with earlier versions stored their metadata inside the filename
(`…__id-<id>__ver-<version>__prot-<true|false>.tar`). They keep working: they are
still listed, downloadable and restorable, and a sidecar is written for them
automatically in the background the first time the backup list is opened.
No existing file is ever renamed or deleted during that migration.

## Installation
1. Add the following custom repositories into HACS, in selecting Integration as type : https://github.com/maximlefebvre/homeassistant-infomaniak-kdrive
2. Search for "Infomaniak kDrive Backup Agent" into HACS, click on it, and then click on the button "Download"
3. Restart Home Assistant.
4. Go on Integration, click on "Add integration", search for "Infomaniak kDrive Backup Agent"
5. Add your token API and the URL of you folder in kDrive

## Configuration
- API Token: Enter your generated API token, generated at this address with "Drive" as scope selected : https://manager.infomaniak.com/v3/ng/profile/user/token/list
- Folder URL: Enter the folder URL as shown in the example below.

## URL Example
```
https://ksuite.infomaniak.com/02468/kdrive/app/drive/12345/files/67890
```
The integration will automatically extract `drive_id=1234` and `folder_id=67890` from this link.
