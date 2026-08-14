# Infomaniak kDrive Backup Agent for Home Assistant

## Description
This integration allows you to sync and save your Home Assistant backups directly to Infomaniak kDrive.

## Features
- API Token Connection: no WebDAV, and no OAuth application to register — a single API token is all you need.
- Simplified Input: Simply paste the full kDrive folder URL (e.g., https://ksuite.infomaniak.com/02468/kdrive/app/drive/12345/files/67890).
- Full Metadata: Each backup is stored as two files sharing the same name — the archive `suggested_filename__id-<backup_id>.tar` and a small sidecar `suggested_filename__id-<backup_id>.metadata.json` holding the complete Home Assistant metadata: date, folders, add-ons, encryption, size, and the flag Home Assistant uses to tell its own automatic backups apart from manual ones.
- Works Without a Local Copy: Because that metadata travels with the backup, backups show up correctly even when the "System" location is unchecked and kDrive is the only place they are stored.
- Retention Policy: Retention settings are aligned with your Home Assistant configuration, oldest backups first.

## Upgrading from 0.5.x
Backups made with earlier versions stored their metadata inside the filename
(`…__id-<id>__ver-<version>__prot-<true|false>.tar`), which could only carry the
id, the Home Assistant version and the encryption flag. They keep working: they
are still listed, downloadable and restorable, and a sidecar is written for them
automatically in the background the first time the backup list is opened.

That migration does not guess. It reads the real metadata from `backup.json`
inside the archive itself, fetching only its first 256 KiB. Older backups
therefore recover their exact date, folders, add-ons and automatic-backup flag —
none of which the filename ever carried.

If the archive cannot be read at that moment, the integration falls back to what
the filename encodes and marks the sidecar `"migrated_from": "filename"`. Such
sidecars are incomplete, so they are detected and rebuilt from the archive on a
later run; the corrected values appear on the next refresh of the backup list.

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

## License
Licensed under the [Apache License 2.0](LICENSE).
