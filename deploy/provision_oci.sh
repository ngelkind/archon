#!/usr/bin/env bash
# Provision the Oracle free-tier VM for Archon. Run from Git Bash on Windows
# (or any POSIX shell) AFTER ~/.oci/config is set up with the owner's API key.
#
# Creates: VCN + subnet + internet gateway with SSH-only ingress, two SSH
# keypairs (login + CI deploy), and launches a VM.Standard.A1.Flex
# (2 OCPU / 12GB, Ubuntu 24.04 aarch64) with deploy/cloud-init.yaml.
#
# A1 capacity errors are common: this script retries across every
# availability domain in the region until a launch succeeds.
set -euo pipefail

COMPARTMENT_ID="${COMPARTMENT_ID:?export COMPARTMENT_ID=<tenancy or compartment OCID>}"
DISPLAY_NAME="archon"
OCPUS="${OCPUS:-2}"
RAM_GB="${RAM_GB:-12}"
RETRY_SLEEP="${RETRY_SLEEP:-300}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

oci() { command oci --output json "$@"; }

echo "== SSH keys =="
[ -f ~/.ssh/archon_vm ] || ssh-keygen -t ed25519 -f ~/.ssh/archon_vm -N "" -C archon-login
[ -f ~/.ssh/archon_deploy ] || ssh-keygen -t ed25519 -f ~/.ssh/archon_deploy -N "" -C archon-ci

echo "== Network =="
VCN_ID=$(oci network vcn list -c "$COMPARTMENT_ID" --display-name archon-vcn \
  | python -c "import json,sys; d=json.load(sys.stdin)['data']; print(d[0]['id'] if d else '')")
if [ -z "$VCN_ID" ]; then
  VCN_ID=$(oci network vcn create -c "$COMPARTMENT_ID" --cidr-blocks '["10.0.0.0/16"]' \
    --display-name archon-vcn --wait-for-state AVAILABLE \
    | python -c "import json,sys; print(json.load(sys.stdin)['data']['id'])")
fi
echo "VCN: $VCN_ID"

IG_ID=$(oci network internet-gateway list -c "$COMPARTMENT_ID" --vcn-id "$VCN_ID" \
  | python -c "import json,sys; d=json.load(sys.stdin)['data']; print(d[0]['id'] if d else '')")
if [ -z "$IG_ID" ]; then
  IG_ID=$(oci network internet-gateway create -c "$COMPARTMENT_ID" --vcn-id "$VCN_ID" \
    --is-enabled true --display-name archon-ig --wait-for-state AVAILABLE \
    | python -c "import json,sys; print(json.load(sys.stdin)['data']['id'])")
fi

RT_ID=$(oci network route-table list -c "$COMPARTMENT_ID" --vcn-id "$VCN_ID" \
  | python -c "import json,sys; print(json.load(sys.stdin)['data'][0]['id'])")
oci network route-table update --rt-id "$RT_ID" --force --route-rules \
  "[{\"destination\": \"0.0.0.0/0\", \"networkEntityId\": \"$IG_ID\"}]" >/dev/null

SL_ID=$(oci network security-list list -c "$COMPARTMENT_ID" --vcn-id "$VCN_ID" \
  | python -c "import json,sys; print(json.load(sys.stdin)['data'][0]['id'])")
# SSH-only ingress; all egress.
oci network security-list update --security-list-id "$SL_ID" --force \
  --ingress-security-rules '[{"protocol": "6", "source": "0.0.0.0/0", "tcpOptions": {"destinationPortRange": {"min": 22, "max": 22}}}]' \
  --egress-security-rules '[{"protocol": "all", "destination": "0.0.0.0/0"}]' >/dev/null

SUBNET_ID=$(oci network subnet list -c "$COMPARTMENT_ID" --vcn-id "$VCN_ID" \
  | python -c "import json,sys; d=json.load(sys.stdin)['data']; print(d[0]['id'] if d else '')")
if [ -z "$SUBNET_ID" ]; then
  SUBNET_ID=$(oci network subnet create -c "$COMPARTMENT_ID" --vcn-id "$VCN_ID" \
    --cidr-block 10.0.0.0/24 --display-name archon-subnet --wait-for-state AVAILABLE \
    | python -c "import json,sys; print(json.load(sys.stdin)['data']['id'])")
fi
echo "Subnet: $SUBNET_ID"

echo "== Image (Ubuntu 24.04 aarch64) =="
IMAGE_ID=$(oci compute image list -c "$COMPARTMENT_ID" \
  --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
  --shape "VM.Standard.A1.Flex" --sort-by TIMECREATED --sort-order DESC \
  | python -c "import json,sys; print(json.load(sys.stdin)['data'][0]['id'])")
echo "Image: $IMAGE_ID"

ADS=$(oci iam availability-domain list -c "$COMPARTMENT_ID" \
  | python -c "import json,sys; print(' '.join(a['name'] for a in json.load(sys.stdin)['data']))")
echo "ADs: $ADS"

SSH_KEYS="$(cat ~/.ssh/archon_vm.pub)
$(cat ~/.ssh/archon_deploy.pub)"

echo "== Launch (retrying across ADs until A1 capacity is found) =="
while true; do
  for AD in $ADS; do
    echo "$(date -Is) trying $AD ..."
    set +e
    RESULT=$(oci compute instance launch \
      --availability-domain "$AD" -c "$COMPARTMENT_ID" \
      --shape VM.Standard.A1.Flex \
      --shape-config "{\"ocpus\": $OCPUS, \"memoryInGBs\": $RAM_GB}" \
      --image-id "$IMAGE_ID" --subnet-id "$SUBNET_ID" \
      --assign-public-ip true --display-name "$DISPLAY_NAME" \
      --metadata "{\"ssh_authorized_keys\": $(python -c "import json,sys;print(json.dumps(open(0).read()))" <<<"$SSH_KEYS")}" \
      --user-data-file "$SCRIPT_DIR/cloud-init.yaml" 2>&1)
    CODE=$?
    set -e
    if [ $CODE -eq 0 ]; then
      INSTANCE_ID=$(echo "$RESULT" | python -c "import json,sys; print(json.load(sys.stdin)['data']['id'])")
      echo "LAUNCHED: $INSTANCE_ID"
      echo "Waiting for RUNNING..."
      oci compute instance get --instance-id "$INSTANCE_ID" >/dev/null
      sleep 60
      IP=$(oci compute instance list-vnics --instance-id "$INSTANCE_ID" \
        | python -c "import json,sys; print(json.load(sys.stdin)['data'][0]['public-ip'])")
      echo ""
      echo "=========================================="
      echo "VM public IP: $IP"
      echo "ssh -i ~/.ssh/archon_vm ubuntu@$IP"
      echo "=========================================="
      exit 0
    fi
    echo "$RESULT" | grep -qi "capacity" && echo "  out of capacity in $AD" || echo "$RESULT" | tail -3
  done
  echo "No capacity in any AD; sleeping ${RETRY_SLEEP}s (Ctrl+C to stop)..."
  sleep "$RETRY_SLEEP"
done
