#!/bin/bash
# Sets up rclone and a sync service for PBS to Google Drive
set -e

echo "Installing rclone..."
curl -sSf https://rclone.org/install.sh | bash

mkdir -p /root/.config/rclone/

if [ -n "$RCLONE_CONFIG_GDRIVE_TOKEN" ]; then
    cat > /root/.config/rclone/rclone.conf << EOC
[gdrive]
type = drive
client_id = $RCLONE_CONFIG_GDRIVE_CLIENT_ID
client_secret = $RCLONE_CONFIG_GDRIVE_CLIENT_SECRET
token = $RCLONE_CONFIG_GDRIVE_TOKEN
EOC
fi

cat > /usr/local/bin/pbs-sync-gdrive.sh << 'EOS'
#!/bin/bash
pgrep -x "rclone" > /dev/null && exit 0
rclone sync /mnt/pbs-storage/main gdrive:pbs-backups --bwlimit "10M"
EOS

chmod +x /usr/local/bin/pbs-sync-gdrive.sh

cat > /etc/systemd/system/pbs-sync-gdrive.service << 'EOSS'
[Unit]
Description=Sync PBS offsite

[Service]
Type=oneshot
ExecStart=/usr/local/bin/pbs-sync-gdrive.sh
EOSS

cat > /etc/systemd/system/pbs-sync-gdrive.timer << 'EOST'
[Unit]
Description=Run PBS offsite sync nightly

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOST

systemctl daemon-reload || true
systemctl enable pbs-sync-gdrive.timer || true
systemctl start pbs-sync-gdrive.timer || true
