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
tikhub = "age://cortex/TIKHUB_API_KEY"
sub2api-gpt = "keychain://cortex-research/sub2api-gpt"
jina = "age://cortex/JINA_API_KEY"
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
routing. `novita` and `glm` reach PDF conversion only through an OCR skill the
operator has accepted ([operator skills](operator-skills.md)); the bundle has
no PDF OCR of its own.

The opt-in [XHS plugin](xhs.md) adds `tikhub`, `sub2api-gpt` and the optional
`jina`. Each XHS child operation resolves only its own aliases:

| Operation | Aliases |
| --- | --- |
| `xhs_list_page`, `xhs_note_detail` | `tikhub` |
| `xhs_download_image` | none |
| `xhs_ocr_image` | `novita`, `glm`, `glm-app-id`; `sub2api-gpt`, omitted when it does not resolve |
| `xhs_identify`, `xhs_resolve_link` | `sub2api-gpt` |
| `blog_fetch` | `jina`, omitted when it does not resolve |

The arXiv operations never resolve the three XHS aliases, so a missing or
broken XHS reference cannot block paper ingestion. A configured XHS reference
that fails to resolve fails that task as `auth` and stops the drain for that
tick; fix it, then run `cortex xhs retry --failed`. Image OCR is first-party
and uses `novita` (DeepSeek-OCR-2), then the Responses model through
`sub2api-gpt` as the backup, then `glm` and `glm-app-id`; it is separate from
the PDF OCR skill.

For a trusted operator command from the checkout:

```sh
.venv/bin/python -m tools.with_engine_secrets -- COMMAND ARGUMENTS
```

Use `--config /absolute/path/config.toml` before `--` for another config. This
wrapper inherits the operator environment and overlays only configured engine
credentials in its child, not the three XHS aliases. It does not modify the
parent shell or write `.env`. Do not use it as a model tool or run untrusted
commands with it. Managed model tool children retain their credential-free
environment and filesystem policy.

`env://VARIABLE_NAME` remains foreground-only. A missing durable reference or
failed decryption is an error, not an implicit fallback to another provider.
Run `cortex doctor` after updating references; it reports an `env://`
reference that the supervised daemon cannot use, including the XHS aliases,
but does not try to resolve those. Do not print decrypted stores or credential
values while diagnosing missing keys.
