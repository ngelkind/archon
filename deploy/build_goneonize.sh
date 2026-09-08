#!/usr/bin/env bash
# SECURITY.md hardening (M5): rebuild neonize's Go shared library from source
# and replace the prebuilt binary that ships in the wheel, removing all trust
# in the prebuilt blob. whatsmeow is hash-verified via go.sum by the Go
# toolchain itself.
#
# ALSO injects a small SendPeerMessage export (see below) that neonize does not
# expose: it sends a message to our OWN phone with whatsmeow's Peer=true flag
# (category:"peer", privacy_sensitive), which is required to issue a
# HISTORY_SYNC_ON_DEMAND request. That is the only route to recover view-once
# media WITHOUT posting anything the sender can see. The export mirrors the
# existing SendMessage export exactly and uses only whatsmeow public APIs
# (Client.SendPeerMessage), so it adds no new trust surface.
#
# Run on the VM (Linux aarch64), after `uv sync`:
#   bash deploy/build_goneonize.sh
set -euo pipefail

NEONIZE_TAG="0.4.3.post0"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "Cloning neonize @ $NEONIZE_TAG ..."
git clone --depth 1 --branch "$NEONIZE_TAG" https://github.com/krypton-byte/neonize "$WORK/neonize" \
  || git clone --depth 1 https://github.com/krypton-byte/neonize "$WORK/neonize"

cd "$WORK/neonize/goneonize"

# --- CRITICAL: match neonize's expected goneonize version, or the Python loader
#     (neonize/_binder.py load_goneonize) rejects our build ("Invalid Version")
#     and silently RE-DOWNLOADS the prebuilt .so from GitHub, overwriting it.
#     The source hardcodes an older version string; force it to the tag so our
#     build is accepted AND the runtime auto-download never triggers. ---
echo "Pinning goneonize version string to $NEONIZE_TAG ..."
sed -i "s/version := \"[^\"]*\"/version := \"$NEONIZE_TAG\"/" version.go
grep -q "version := \"$NEONIZE_TAG\"" version.go \
  || { echo "ERROR: failed to pin version in version.go" >&2; exit 1; }

# --- Inject the SendPeerMessage export (idempotent: fresh clone each run) ---
# TODO(TOS-REVIEW): WhatsApp — injects a SendPeerMessage export into a patched whatsmeow/neonize build (reads media without a visible receipt) — review before launch
if grep -q "//export SendPeerMessage" main.go; then
  echo "SendPeerMessage already present, skipping injection."
else
  echo "Injecting SendPeerMessage export into main.go ..."
  # NOTE: cgo only honors `//export` when it stands alone directly above the
  # func with NO other comment lines in the same block. Keep this bare.
  cat >> main.go <<'GOEOF'

//export SendPeerMessage
func SendPeerMessage(id *C.char, messageByte *C.uchar, messageSize C.int) *C.struct_BytesReturn {
	client := clients[C.GoString(id)]
	return_ := defproto.SendMessageReturnFunction{}
	var message waE2E.Message
	if err := proto.Unmarshal(getByteByAddr(messageByte, messageSize), &message); err != nil {
		return_.Error = proto.String(err.Error())
		return ProtoReturnV3(&return_)
	}
	sendresponse, err := client.SendPeerMessage(context.Background(), &message)
	if err != nil {
		return_.Error = proto.String(err.Error())
		return ProtoReturnV3(&return_)
	}
	return_.SendResponse = utils.EncodeSendResponse(sendresponse)
	return ProtoReturnV3(&return_)
}
GOEOF
fi

# --- Android-phone identity spoof (makes WhatsApp deliver view-once media to
#     this companion, same as a real phone/tablet; verified route, Baileys
#     PR #2201). Two of the three fields (UserAgent.Platform, WebInfo) are
#     hardcoded WEB in whatsmeow and only settable in Go; DeviceProps.PlatformType
#     is also set on the Archon side. Separate file = own import block. ---
echo "Writing android_spoof.go ..."
# IMPORTANT: this must NOT run in a package init(). At c-shared dlopen time the
# Go runtime is not ready for proto reflection/allocation, and doing this in
# init() SIGBUS-crashes the process the moment Python imports neonize. Instead
# it's a normal func called from the Neonize (client-creation) export, where the
# runtime is fully up. See the sed injection below.
cat > android_spoof.go <<'GOEOF'
package main

import (
	"go.mau.fi/whatsmeow/proto/waCompanionReg"
	"go.mau.fi/whatsmeow/proto/waWa6"
	"go.mau.fi/whatsmeow/store"
)

// applyAndroidIdentity presents this companion as an Android phone at login so
// WhatsApp's server delivers the real view-once <enc> stanza (mediaKey +
// directPath) instead of an "unavailable" stub. All three identity fields a
// real Android companion sends must change together: UserAgent.Platform=ANDROID,
// no WebInfo, DeviceProps.PlatformType=ANDROID_PHONE + Android OS string. Called
// from the Neonize export after neonize merges the caller's props.
func applyAndroidIdentity() {
	store.BaseClientPayload.UserAgent.Platform = waWa6.ClientPayload_UserAgent_ANDROID.Enum()
	store.BaseClientPayload.WebInfo = nil
	store.DeviceProps.PlatformType = waCompanionReg.DeviceProps_ANDROID_PHONE.Enum()
	store.SetOSInfo("Android", [3]uint32{13, 0, 0})
}
GOEOF

# Inject a call to applyAndroidIdentity() right after neonize merges caller
# props (runtime fully up), before whatsmeow.NewClient. Idempotent.
if ! grep -q "applyAndroidIdentity()" main.go; then
  echo "Injecting applyAndroidIdentity() call into Neonize export ..."
  sed -i 's/\tproto.Merge(store.DeviceProps, &deviceProps)/\tproto.Merge(store.DeviceProps, \&deviceProps)\n\tapplyAndroidIdentity()/' main.go
fi
if ! grep -q "applyAndroidIdentity()" main.go; then
  echo "ERROR: failed to inject applyAndroidIdentity() call into main.go" >&2
  exit 1
fi

# CRLF-proof the sources we wrote/appended: a trailing \r breaks cgo's //export
# directive parsing (silent: the symbol just won't be exported). Harmless for
# the rest of the Go source, so strip it everywhere.
sed -i 's/\r$//' main.go android_spoof.go

echo "Building shared library (this verifies go.sum hashes)..."
# Build the whole package (main.go + helpers.go + android_spoof.go + ...): all
# are `package main`, so `.` compiles every .go file in the directory.
GOFLAGS=-mod=readonly go build -trimpath -buildmode=c-shared -o neonize.so .

echo "Verifying SendPeerMessage symbol is exported ..."
if command -v nm >/dev/null 2>&1; then
  # Capture first (no pipe): `nm | grep -q` dies with SIGPIPE 141 under
  # `set -o pipefail` because grep -q closes the pipe early — a false negative.
  SYMS=$(nm -D neonize.so 2>/dev/null || true)
  case "$SYMS" in
    *SendPeerMessage*) echo "  OK: SendPeerMessage present in .so" ;;
    *) echo "  ERROR: SendPeerMessage symbol missing" >&2; exit 1 ;;
  esac
fi

SITE=$(cd /opt/archon/app && ~/.local/bin/uv run python -c \
  "import neonize, pathlib; print(pathlib.Path(neonize.__file__).parent)")
TARGET=$(ls "$SITE"/neonize-*.so 2>/dev/null | head -1)
if [ -z "$TARGET" ]; then
  echo "ERROR: could not find the bundled neonize .so in $SITE" >&2
  exit 1
fi
if [ ! -f "$TARGET.prebuilt.bak" ]; then
  cp "$TARGET" "$TARGET.prebuilt.bak"
fi
cp neonize.so "$TARGET"
echo "Replaced $TARGET with source-built library (backup: .prebuilt.bak)"
echo "Record this in SECURITY.md:"
sha256sum neonize.so
