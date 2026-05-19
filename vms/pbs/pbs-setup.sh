#!/bin/bash
# Installs Proxmox Backup Server on Debian 12 and configures the HDD datastore.
# Required env vars: PBS_BACKUP_PASSWORD
set -e
export DEBIAN_FRONTEND=noninteractive

# Set root password so Proxmox VE can authenticate via root@pam
echo "root:$PBS_BACKUP_PASSWORD" | chpasswd

# Add Proxmox PBS no-subscription repository
wget -q -O /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg \
  https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg
echo "deb http://download.proxmox.com/debian/pbs bookworm pbs-no-subscription" \
  > /etc/apt/sources.list.d/pbs.list

# Remove enterprise repo if it exists (no subscription; also cleans up partial runs)
rm -f /etc/apt/sources.list.d/pbs-enterprise.list 2>/dev/null || true

apt-get update -y -qq
apt-get install -y -qq proxmox-backup-server parted

# PBS installer may re-add enterprise repo — remove it again
rm -f /etc/apt/sources.list.d/pbs-enterprise.list 2>/dev/null || true

# Wait for PBS API proxy to be ready
echo "Waiting for PBS API..."
for i in $(seq 1 40); do
  if curl -s -k -o /dev/null "https://localhost:8007/api2/json/version"; then
    echo "PBS API ready"
    break
  fi
  echo "  attempt $i/40..."
  sleep 5
done

# Wait for data disk to appear (scsi1 = /dev/sdb)
echo "Waiting for data disk /dev/sdb..."
for i in $(seq 1 30); do
  if [ -b /dev/sdb ]; then
    echo "Data disk found"
    break
  fi
  sleep 2
done

# Partition, format, and mount the HDD (idempotent)
if mountpoint -q /mnt/pbs-storage 2>/dev/null; then
  echo "Data disk already mounted, skipping format"
else
  umount /dev/sdb1 2>/dev/null || true
  wipefs -a /dev/sdb
  parted -s /dev/sdb mklabel gpt
  parted -s /dev/sdb mkpart primary ext4 0% 100%
  sleep 2
  mkfs.ext4 -q -F /dev/sdb1
  mkdir -p /mnt/pbs-storage
  grep -q '/dev/sdb1' /etc/fstab || echo '/dev/sdb1 /mnt/pbs-storage ext4 defaults 0 0' >> /etc/fstab
  mount /mnt/pbs-storage
fi

# Create PBS datastore (idempotent)
proxmox-backup-manager datastore create main /mnt/pbs-storage 2>/dev/null || echo "Datastore already exists"

echo "PBS setup complete"
