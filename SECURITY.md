# Repository Data Boundary

Only source code, skills, generic examples and development documentation belong in Git.

Keep these local and backed up privately:

- `workflow/runs/`: user requirements, confirmations, personal spoken scripts, state, audio, lyrics and exports.
- `rag/data/` and `rag/catalog.json`: private song databases, source snapshots and the real source catalog.
- `memory/`: local project notes.
- `skills/pco-assemble-worship-setlist/assets/reference.docx`: the user-provided populated template.
- Credentials, `.env` files, local agent configuration, caches and OS metadata.

Ignoring these paths does not delete their local contents. `git rm --cached` stops future tracking but does not remove previously published commits.

## Local Setup

For a new checkout, restore your own authorized materials from a private backup. Provide the local template at the path above before document generation. Copy `rag/catalog.example.json` to the ignored `rag/catalog.json` and fill it with your own reviewed source ranges and hashes, then follow `rag/README.md` to ingest those sources. An empty catalog is a schema example, not a populated library. Existing local materials must not be overwritten.

PCO credentials come from the local Keychain or `PCO_CLIENT_ID` / `PCO_SECRET` environment variables. Never put their values in source files, inputs, reports or Git configuration.

## Before Publishing

```sh
python3 -B -m unittest discover -s scripts -p 'test*.py' -v
python3 scripts/check_repository.py
python3 scripts/check_repository.py --history
gitleaks git . --log-opts=--all --redact=100 --no-banner --max-archive-depth 3 --max-decode-depth 3
```

The default policy check inspects Git's index, not untracked local materials. CI checks both the current committed tree and reachable history. Secret scanning uses Gitleaks; the path-policy script is not a credential scanner. CI reports failures; making this check a required branch-protection rule is a separate GitHub administrator setting.

## If Data Was Already Published

1. Revoke/rotate any exposed credentials immediately. Removing a commit does not invalidate them.
2. Preserve needed local data privately, stop tracking it, and add ignore rules.
3. With repository-owner approval, rewrite affected history and push with an explicit expected remote SHA lease. Ordinary deletion commits do not remove old blobs.
4. Recheck all branches/tags. Request GitHub cache cleanup when appropriate, and coordinate with collaborators to avoid reintroducing old history.

History rewriting cannot erase third-party clones, forks or downloads. Do not claim remote cleanup until the remote refs have been verified.
