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


Hi,

Google Drive, Microsoft OneDrive, ... are great solutions to externalise backup of Home Assistant and be sure to be reliable if your device has an issue. But I'm using Infomaniak kSuite, so I was a bit frustrated to do not have my backup on the same place than my usual files.

I have started to develop an integration to let my kDrive becoming my external emplacement for my Home Assistant backup, based on the API.

It could be used with HACS and this GitHub repo : https://github.com/maximlefebvre/homeassistant-infomaniak-kdrive

This is not perfect, I still need :

* To support files larger than 1GB
* To delete the delete file in the Trash too, to limit the space used
* To change the code to make more Home Assistant, with the usage of PyPI

And if it could 