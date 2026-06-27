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

# Detect the data disk: largest block device that is NOT the boot disk
# Use size-based selection (biggest non-boot disk) rather than "unpartitioned"
# so this survives re-runs where the data disk was already partitioned.
BOOT_DEV=$(lsblk -no PKNAME "$(findmnt -n -o SOURCE /)" 2>/dev/null | head -1)
DATA_DISK=""
DATA_DISK_SIZE=0
for dev in /dev/sd?; do
  name=$(basename "$dev")
  [ "$name" = "$BOOT_DEV" ] && continue
  size_bytes=$(lsblk -bndo SIZE "$dev" 2>/dev/null || echo 0)
  if [ "$size_bytes" -gt "$DATA_DISK_SIZE" ]; then
    DATA_DISK_SIZE="$size_bytes"
    DATA_DISK="$dev"
  fi
done

if [ -z "$DATA_DISK" ]; then
  echo "ERROR: no data disk found (boot disk: $BOOT_DEV)" >&2
  lsblk >&2
  exit 1
fi
echo "Boot disk: $BOOT_DEV  Data disk: $DATA_DISK ($(( DATA_DISK_SIZE / 1073741824 ))GB)"

# Partition, format, and mount the HDD (idempotent)
mkdir -p /mnt/pbs-storage
if mountpoint -q /mnt/pbs-storage 2>/dev/null; then
  echo "Data disk already mounted at /mnt/pbs-storage"
else
  DATA_PART="${DATA_DISK}1"
  if ! blkid "$DATA_PART" >/dev/null 2>&1; then
    echo "Partitioning and formatting $DATA_DISK..."
    umount "$DATA_PART" 2>/dev/null || true
    wipefs -a "$DATA_DISK"
    parted -s "$DATA_DISK" mklabel gpt
    parted -s "$DATA_DISK" mkpart primary ext4 0% 100%
    sleep 2
    mkfs.ext4 -q -F "$DATA_PART"
  else
    echo "$DATA_PART already formatted, skipping mkfs"
  fi
  DATA_UUID=$(blkid -s UUID -o value "$DATA_PART")
  # Remove any stale /mnt/pbs-storage fstab entries and add by UUID
  sed -i '/\/mnt\/pbs-storage/d' /etc/fstab
  echo "UUID=$DATA_UUID /mnt/pbs-storage ext4 defaults 0 0" >> /etc/fstab
  mount /mnt/pbs-storage
fi

# Register PBS datastore (idempotent: write config directly if create fails)
if ! proxmox-backup-manager datastore list 2>/dev/null | grep -q "^│ main"; then
  proxmox-backup-manager datastore create main /mnt/pbs-storage 2>/dev/null || \
    printf 'datastore: main\n\tpath /mnt/pbs-storage\n' > /etc/proxmox-backup/datastore.cfg
fi

echo "PBS setup complete"
