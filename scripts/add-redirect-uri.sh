#!/usr/bin/env bash
# Adds a sign-in return address (redirect URI) to the PPG Verifier's Microsoft Entra app registration.
# It only ever ADDS: the addresses already there are kept, and a backup of the old list is saved first.
#
# Usage:
#   ./add-redirect-uri.sh                 # adds the Focus Desk branch address
#   ./add-redirect-uri.sh <full-uri>      # adds any other address
#   ./add-redirect-uri.sh --list          # only shows what is there now
#   ./add-redirect-uri.sh --undo <backup-file>   # puts back a saved list
#
# Needs the Azure CLI (on a Mac: brew install azure-cli), or run it in Azure Cloud Shell (shell.azure.com),
# where it is already installed. You sign in with your own PPG admin account; nothing secret is stored.
set -euo pipefail

APP_ID="e5feea81-af82-41ff-a604-68a698a51179"   # the Verifier's application (client) ID, not a secret
DEFAULT_URI="https://ppg-ci-verifier-git-focus-desk-lauren-mc-guire-llc.vercel.app/auth/microsoft/callback"

if ! command -v az >/dev/null 2>&1; then
  echo "The Azure CLI is not installed. On a Mac run:  brew install azure-cli"
  echo "Or open https://shell.azure.com (Bash) and run this script there."
  exit 1
fi

if ! az account show >/dev/null 2>&1; then
  echo "Signing in to Microsoft (a browser window opens)…"
  az login --allow-no-subscriptions >/dev/null
fi

current_list() { az ad app show --id "$APP_ID" --query "web.redirectUris[]" -o tsv | tr -d '\r'; }

show() {
  echo "Sign-in return addresses on the Verifier app now:"
  current_list | sed 's/^/  • /'
}

if [[ "${1:-}" == "--list" ]]; then show; exit 0; fi

if [[ "${1:-}" == "--undo" ]]; then
  file="${2:?Give the backup file, e.g. redirect-uris-backup-20261107-161700.txt}"
  uris=()
  while IFS= read -r line; do [[ -n "$line" ]] && uris+=("$line"); done < "$file"
  [[ ${#uris[@]} -gt 0 ]] || { echo "That backup is empty; nothing changed."; exit 1; }
  az ad app update --id "$APP_ID" --web-redirect-uris "${uris[@]}"
  echo "Put back the list from $file."; show; exit 0
fi

NEW_URI="${1:-$DEFAULT_URI}"
if [[ ! "$NEW_URI" =~ ^https://[^/[:space:]]+/auth/microsoft/callback$ ]]; then
  echo "That does not look like a Verifier sign-in address (https://<host>/auth/microsoft/callback): $NEW_URI"
  exit 1
fi

existing=()
while IFS= read -r line; do [[ -n "$line" ]] && existing+=("$line"); done < <(current_list)

for u in "${existing[@]:-}"; do
  if [[ "$u" == "$NEW_URI" ]]; then
    echo "Already there, nothing to do: $NEW_URI"; exit 0
  fi
done

backup="redirect-uris-backup-$(date +%Y%m%d-%H%M%S).txt"
printf '%s\n' "${existing[@]:-}" > "$backup"
echo "Saved the current list to $backup (use --undo $backup to put it back)."

az ad app update --id "$APP_ID" --web-redirect-uris "${existing[@]:-}" "$NEW_URI"

# confirm it really landed, and that nothing that was there before went missing
after="$(current_list)"
grep -qxF "$NEW_URI" <<<"$after" || { echo "The address did not save. Nothing else was changed."; exit 1; }
for u in "${existing[@]:-}"; do
  [[ -z "$u" ]] || grep -qxF "$u" <<<"$after" || { echo "Warning: $u is missing now. Run: $0 --undo $backup"; exit 1; }
done
echo "Added: $NEW_URI"
show
echo "Microsoft can take a minute or two to pick it up. Then use 'Sign in with Microsoft' on the branch."
