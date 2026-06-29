if [ -n "${GOOGLE_CREDENTIALS:-}" ]; then
    service_account_path="${GOOGLE_SERVICE_ACCOUNT_PATH:-secrets/service_account.json}"
    mkdir -p "$(dirname "${service_account_path}")"
    printf '%s' "${GOOGLE_CREDENTIALS}" > "${service_account_path}"
    chmod 600 "${service_account_path}"
fi
