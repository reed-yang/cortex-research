# Runtime credentials

Cortex does not source shell profiles or encrypted dotenv files automatically.
Supervised processes start with a replaced environment. Declare each required
credential under `[secret_refs]` in the product config; existing explicit
references take precedence over any ambient shell key.

```toml
[secret_refs]
glm = "age://cortex/GLM_API_KEY"
glm-app-id = "age://cortex/GLM_API_ID"
novita = "age://cortex/NOVITA_API_KEY"
openrouter = "keychain://cortex-research/openrouter-embedding"
```

`age://cortex/GLM_API_KEY` decrypts `~/.config/cortex/secrets.age` with the adjacent
`age-key.txt`. Store names are single safe identifiers, variable names uppercase.
Install age at `/opt/homebrew/bin/age`, `/usr/local/bin/age`, or `/usr/bin/age`.
The identity must be usable noninteractively by the account running Cortex.
Protect both files with owner-only permissions. The resolver limits encrypted
input to 1 MiB and decryption to ten seconds, rejects missing/duplicate/malformed
requested assignments, and reports unavailable credentials without plaintext.
It accepts shell-quoted assignments with an optional `export` prefix; it never
executes shell syntax or expands variables. Rotation is read on the next lookup.
Only explicitly referenced values leave the resolver, not the entire vault.

The engine binds `glm-app-id`, `glm`, `novita`, and `openrouter` to their declared
variables. The model worker and Telegram transport have separate aliases and
bindings. Adding a key does not enable an unsupported engine or change provider
routing. In particular, the separately installed paper-ingestion skill is not
part of the bundled engine's PDF conversion capability.

For a trusted operator command from the checkout:

```sh
.venv/bin/python -m tools.with_engine_secrets -- COMMAND ARGUMENTS
```

Use `--config /absolute/path/config.toml` before `--` for another config. This
wrapper inherits the operator environment and overlays only configured engine
credentials in its child. It does not modify the parent shell or write `.env`.
Do not use it as a model tool or run untrusted commands with it. Managed model
tool children retain their credential-free environment and filesystem policy.

`env://VARIABLE_NAME` remains foreground-only. A missing durable reference or
failed decryption is an error, not an implicit fallback to another provider.
Run `cortex doctor` after updating references. Do not print decrypted stores or
credential values while diagnosing missing keys.
