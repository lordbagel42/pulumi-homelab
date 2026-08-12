# Configuring Proxmox Backup Server with Google Drive

This guide explains how to integrate Proxmox Backup Server (PBS) with Google Drive using `rclone`. There are two primary methods: **Syncing** (recommended) and **Mounting**.

## Architecture Overview

1.  **Proxmox Backup Server (PBS)**: Manages deduplicated, compressed backups.
2.  **rclone**: A command-line program to manage files on cloud storage.
3.  **Google Drive**: The remote storage destination.

---

## Prerequisites

-   A running Proxmox Backup Server instance.
-   A Google account with sufficient storage space.
-   `rclone` installed on the PBS host.

---

## Method 1: Local Datastore with Cloud Sync (Recommended)

This method keeps a local copy of your backups for fast restores and uses `rclone sync` to push encrypted/deduplicated chunks to Google Drive.

### 1. Configure rclone Remote
Run `rclone config` and follow the prompts to create a new remote named `gdrive`.
- Choose `drive` as the storage type.
- Follow the OAuth flow to authorize access.

### 2. Create a Sync Script
Create a script (e.g., `/usr/local/bin/pbs-sync.sh`):
```bash
#!/bin/bash
# Sync PBS datastore to Google Drive
rclone sync /mnt/pbs-storage/main gdrive:pbs-backups --progress
```

### 3. Schedule via Systemd or Cron
Add a crontab entry to run the sync nightly:
```bash
0 3 * * * /usr/local/bin/pbs-sync.sh
```

---

## Method 2: Rclone Mount as a Datastore

This method allows PBS to write directly to Google Drive. *Warning: This is slower and can be less stable due to network latency and Google Drive API limits.*

### 1. Install and Configure Rclone
Same as Method 1.

### 2. Set up Rclone Mount Service
Create `/etc/systemd/system/rclone-mount.service`:
```ini
[Unit]
Description=Rclone Mount for Google Drive
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/rclone mount gdrive:pbs-backups /mnt/gdrive-pbs \
    --config /root/.config/rclone/rclone.conf \
    --vfs-cache-mode full \
    --allow-other
ExecStop=/bin/umount /mnt/gdrive-pbs
Restart=always

[Install]
WantedBy=multi-user.target
```

### 3. Add Datastore in PBS
Point PBS to `/mnt/gdrive-pbs`.

---

## Best Practices
- **Encryption**: PBS encrypts data at the client level, so data on Google Drive is already secure.
- **Pruning**: Manage your retention policy in PBS. Rclone sync will eventually delete old chunks from the cloud as PBS prunes them locally.
- **Bandwidth**: Use `--bwlimit` in rclone if the sync consumes too much upload bandwidth during the day.
