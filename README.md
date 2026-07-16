# Infomaniak kDrive Backup Agent for Home Assistant

## Description
This integration allows you to sync and save your Home Assistant backups directly to Infomaniak kDrive.

## Features
- API Token Connection (no need of WebDAV): 
- Simplified Input: Simply paste the full kDrive folder URL (e.g., https://ksuite.infomaniak.com/02468/kdrive/app/drive/12345/files/67890).
- Enriched Filenames: Backups are saved as suggested_filename__id-<backup_id>__ver-<ha_version>__prot-<true|false>.tar.
- Accurate Sizing: Real file size verification via HEAD/GET requests.
- Retention Policy: Retention settings are aligned with your Home Assistant configuration.

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
