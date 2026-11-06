"""Optional OS credential storage; never fall back to plaintext files."""

SERVICE = "reborn-workflow"


def backend():
    import keyring

    store = keyring.get_keyring()
    module = type(store).__module__
    if not module.startswith(("keyring.backends.macOS", "keyring.backends.Windows",
                              "keyring.backends.SecretService")):
        raise RuntimeError("An OS-protected credential store is required.")
    return store


def load():
    try:
        store = backend()
        return [store.get_password(SERVICE, account)
                for account in ("client_id", "client_secret")]
    except Exception:
        # An unavailable/locked store is not proof that credentials do not exist.
        return [None, None]


def save(client_id, secret):
    if not client_id.strip() or not secret.strip():
        raise ValueError("Both credential fields are required.")
    store = backend()
    old = [store.get_password(SERVICE, a) for a in ("client_id", "client_secret")]
    try:
        store.set_password(SERVICE, "client_id", client_id)
        store.set_password(SERVICE, "client_secret", secret)
    except Exception:
        for account, value in zip(("client_id", "client_secret"), old):
            try:
                if value is None:
                    store.delete_password(SERVICE, account)
                else:
                    store.set_password(SERVICE, account, value)
            except Exception:
                pass
        raise RuntimeError("Credential storage failed; no plaintext fallback is allowed.") from None
