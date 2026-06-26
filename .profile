if [ -n "${GOOGLE_CREDENTIALS:-}" ]; then
    printf '%s' "${GOOGLE_CREDENTIALS}" > bot/service_account.json
    chmod 600 bot/service_account.json
fi
