#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
launcher=$script_dir/Start-ChessTable.sh
source "$launcher"

fixture=$(mktemp -d)
trap 'rm -rf -- "$fixture"' EXIT
mkdir -p "$fixture/serial/by-id"
touch "$fixture/ttyACM0"
ln -s ../../ttyACM0 "$fixture/serial/by-id/usb-generic"
if result=$(discover_serial_port "$fixture" "$fixture/sys" 2>/dev/null); then
    printf 'Expected one unidentified USB serial device to require --port, got %s\n' "$result" >&2
    exit 1
fi

mkdir -p "$fixture/sys/class/tty/ttyACM0/device"
printf '2341\n' > "$fixture/sys/class/tty/ttyACM0/device/idVendor"
printf '0043\n' > "$fixture/sys/class/tty/ttyACM0/device/idProduct"
result=$(discover_serial_port "$fixture" "$fixture/sys")
[[ $result == "$fixture/serial/by-id/usb-generic" ]]

# A labelled Uno is selected even with another USB serial device attached.
touch "$fixture/ttyUSB0"
ln -s ../../ttyACM0 "$fixture/serial/by-id/usb-Arduino_Uno"
rm -rf -- "$fixture/sys"
result=$(discover_serial_port "$fixture" "$fixture/sys")
[[ $result == "$fixture/serial/by-id/usb-Arduino_Uno" ]]

rm -- "$fixture/serial/by-id/usb-Arduino_Uno"
if result=$(discover_serial_port "$fixture" "$fixture/sys" 2>/dev/null); then
    printf 'Expected two unidentified USB serial devices to require --port, got %s\n' "$result" >&2
    exit 1
fi

ln -s ../../ttyUSB0 "$fixture/serial/by-id/usb-Arduino_Uno_B"
ln -s ../../ttyACM0 "$fixture/serial/by-id/usb-Arduino_Uno_A"
if result=$(discover_serial_port "$fixture" "$fixture/sys" 2>/dev/null); then
    printf 'Expected a multiple-Uno error, got %s\n' "$result" >&2
    exit 1
fi

# Dry-run creates no checkpoint and never starts the host.
output=$(bash "$launcher" --dry-run --gui-port 49765)
[[ $output == *'Dry run: host was not started.'* ]]
state=$(printf '%s\n' "$output" | sed -n 's/^Checkpoint: //p')
[[ -n $state && ! -e $state ]]

output=$(bash "$launcher" --dry-run --gui-port 49765 --enable-pocketmage \
    --pocketmage-bind 127.0.0.1 --pocketmage-port 49767)
[[ $output == *'PocketMage API: http://127.0.0.1:49767 (trusted LAN, no token)'* ]]
[[ $output == *'Dry run: host was not started.'* ]]
if bash "$launcher" --dry-run --gui-port 49765 --enable-pocketmage \
    --pocketmage-port 49765 >/dev/null 2>&1; then
    printf 'Expected colliding PocketMage/GUI ports to fail.\n' >&2
    exit 1
fi
if bash "$launcher" --dry-run --enable-pocketmage \
    --pocketmage-bind invalid >/dev/null 2>&1; then
    printf 'Expected an invalid PocketMage bind address to fail.\n' >&2
    exit 1
fi

if bash "$launcher" --resume runs/missing.json --dry-run --gui-port 49765 >/dev/null 2>&1; then
    printf 'Expected resume without explicit mode to fail.\n' >&2
    exit 1
fi

# The daily wrapper chooses physical + Aina, finds Stockfish under the Linux
# home, and remains read-only with --dry-run. /dev/null is an explicit test
# placeholder; no controller is opened because the host never starts.
test_home=$fixture/home
mkdir -p "$test_home/.config/chess-table" "$test_home/Documents/Stockfish"
printf 'test-only-token\n' > "$test_home/.config/chess-table/aina.token"
output=$(HOME="$test_home" bash "$script_dir/Play-ChessTable.sh" \
    --port /dev/null --dry-run --gui-port 49765 --aina-port 49766)
[[ $output == *'Mode: physical'* ]]
[[ $output == *'Aina API: http://127.0.0.1:49766/v1/status'* ]]
[[ $output == *"Stockfish: $test_home/Documents/Stockfish"* ]]
[[ $output == *'Dry run: host was not started.'* ]]

mv -- "$test_home/Documents/Stockfish" "$test_home/Documents/stockfish"
output=$(HOME="$test_home" bash "$script_dir/Play-ChessTable.sh" \
    --port /dev/null --dry-run --gui-port 49765 --aina-port 49766)
[[ $output == *"Stockfish: $test_home/Documents/stockfish"* ]]

output=$(HOME="$test_home" bash "$script_dir/Play-ChessTable.sh" \
    --port /dev/null --dry-run --engine /opt/custom-stockfish \
    --gui-port 49765 --aina-port 49766)
[[ $output == *'Stockfish: /opt/custom-stockfish'* ]]

# Stub usbipd-win and the Linux device tree. The dry run must not call attach;
# an actual attach invocation is exercised only against this local stub.
usb_root=$fixture/wsl-dev
sys_root=$fixture/wsl-sys
mkdir -p "$usb_root"
cat > "$fixture/usbipd-state.json" <<'JSON'
{"Devices":[{"BusId":"3-2","ClientIPAddress":null,"InstanceId":"USB\\VID_2341&PID_0043\\TEST"}]}
JSON
usbipd.exe() {
    case $1 in
        state)
            cat "$fixture/usbipd-state.json" ;;
        attach)
            printf '%s\n' "$*" > "$fixture/attach-call"
            printf 'usbipd: info: attached\n'
            touch "$usb_root/ttyACM0"
            mkdir -p "$sys_root/class/tty/ttyACM0/device"
            printf '2341\n' > "$sys_root/class/tty/ttyACM0/device/idVendor"
            printf '0043\n' > "$sys_root/class/tty/ttyACM0/device/idProduct" ;;
        *) return 1 ;;
    esac
}
export WSL_DISTRO_NAME=MockDistro
python=$script_dir/computer/.venv/bin/python
result=$(auto_attach_wsl_uno "$python" 1 "$usb_root" "$sys_root")
[[ $result == pending:3-2 && ! -e $fixture/attach-call ]]
result=$(auto_attach_wsl_uno "$python" 0 "$usb_root" "$sys_root")
[[ $result == "$usb_root/ttyACM0" ]]
[[ $(cat "$fixture/attach-call") == 'attach --busid 3-2 --wsl MockDistro' ]]
rm -- "$fixture/attach-call"
cat > "$fixture/usbipd-state.json" <<'JSON'
{"Devices":[{"BusId":"2-6","ClientIPAddress":null,"InstanceId":"USB\\VID_27C6&PID_550C\\OTHER"}]}
JSON
if result=$(auto_attach_wsl_uno "$python" 0 "$usb_root" "$sys_root" 2>/dev/null); then
    printf 'Expected unrelated Windows USB device to remain untouched, got %s\n' "$result" >&2
    exit 1
fi
[[ ! -e $fixture/attach-call ]]

# A WSL-only fallback finds the Windows user's Stockfish folder by querying
# USERPROFILE, then converting that exact path. Native Linux skips this route.
windows_home=$fixture/windows-home
mkdir -p "$windows_home/Documents/stockfish"
powershell.exe() {
    printf 'C:\\Users\\ChessPlayer\r\n'
}
wslpath() {
    [[ $1 == -u && $2 == 'C:\Users\ChessPlayer' ]]
    printf '%s\n' "$windows_home"
}
result=$(find_windows_stockfish_in_wsl)
[[ $result == "$windows_home/Documents/stockfish" ]]
if WSL_DISTRO_NAME='' find_windows_stockfish_in_wsl >/dev/null; then
    printf 'Expected Windows Stockfish lookup to be skipped outside WSL.\n' >&2
    exit 1
fi

printf 'Linux launcher checks passed.\n'
