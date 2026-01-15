
# Infomaniak kDrive Backup Agent for Home Assistant

## Description
Cette intégration permet de sauvegarder vos backups Home Assistant vers **Infomaniak kDrive**.

## Fonctionnalités
- Connexion avec **token API**, que vous pouvez créer à cette adresse : https://manager.infomaniak.com/v3/, puis via Mon Profil, Développeur, Tokens API, puis Créer un token en choississant le scope "Drive".
- Saisie simplifiée : collez l'**URL complète du dossier kDrive** (ex: https://ksuite.infomaniak.com/all/kdrive/app/drive/12345/files/67890).
- Nom enrichi des fichiers: `suggested_filename__id-<backup_id>__ver-<ha_version>__prot-<true|false>.tar`.
- Taille réelle via HEAD/GET.
- Rétention alignée sur Home Assistant.

## Installation
1. Copiez le dossier `custom_components/infomaniak_kdrive` dans votre configuration Home Assistant.
2. Ajoutez le logo Infomaniak (optionnel) dans `logo.png`.
3. Redémarrez Home Assistant.

## Configuration
- **Token API**: saisissez votre token API et l'URL du dossier.
- **URL du dossier** : saissisez l'url comme l'exemple ci-dessous.

## Exemple d'URL
```
https://ksuite.infomaniak.com/all/kdrive/app/drive/12345/files/67890
```
Cela donnera `drive_id=12345` et `folder_id=67890`.
