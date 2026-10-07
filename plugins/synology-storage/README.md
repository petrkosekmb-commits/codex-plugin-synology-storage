# Synology Storage for Codex

A lightweight, dependency-free plugin for working with Synology NAS files from
your computer. Includes an English/Czech usage guide, a Codex skill and a Python
CLI. No software is installed on the NAS. Suitable for older devices such as
DS213j when their shares are already accessible through the operating system.
This is an independent community project, not an official Synology product.

## Features

- Check mounted-share availability and reported free space.
- List files or search names with bounded, case-insensitive glob matching.
- Create folders and copy individual files with SHA256 read-back verification.
- Compare two files without changing them.
- Read an optional JSON status file from an existing backup process.
- Read-only roots by default; preview writes before applying them.
- No overwrite, deletion, credential storage, telemetry, or background scheduler.

This is a **skill and script plugin**, not an MCP server. Python 3.10+ and a local
Codex environment with shell access are required. The OS handles SMB credentials.
Connect the NAS share before using the plugin. Windows UNC paths are supported;
Linux and macOS can use an existing mounted filesystem path.

## Install in Codex

From the repository root, register the bundled marketplace and install:

```sh
codex plugin marketplace add .
codex plugin add synology-storage@synology-storage-community
```

Start a new Codex chat after installation. The repository contains the plugin
under `plugins/synology-storage`. Change into that directory before running the
CLI examples below. The entry point is `.codex-plugin/plugin.json` and the skill
is under `skills/`. The plugin installs locally; no NAS credentials are requested.

The CLI also works independently of Codex:

```sh
python scripts/storage.py --help
python -m unittest discover -s tests -v
```

## Configure locally

Copy `config.example.json` to `~/.config/synology-storage/config.json` **outside
the repository**, then replace the example paths with your existing mounted
folders. On Windows `~` is your user profile. For macOS/Linux change the local
example to an absolute path such as `/mnt/local-transfer`. Set `writable: true`
only on roots where you want writes. Scope roots to the smallest useful folder.

Alternatively set `SYNOLOGY_STORAGE_CONFIG` or pass `--config /absolute/config.json`.
Use the OS credential manager to connect the share; never put a password here.
The configuration is portable between plugin updates because it stays outside
the source and the plugin cache.

```sh
python scripts/storage.py status --root nas
python scripts/storage.py list --root nas
python scripts/storage.py search --root nas --pattern '*.pdf' --limit 50
python scripts/storage.py mkdir --root nas --path reports
python scripts/storage.py mkdir --root nas --path reports --apply
python scripts/storage.py copy --root local --path report.pdf --to-root nas --to-path reports/report.pdf
python scripts/storage.py --timeout 300 copy --root local --path report.pdf --to-root nas --to-path reports/report.pdf --apply
python scripts/storage.py verify --root local --path report.pdf --to-root nas --to-path reports/report.pdf
```

Global options (`--config`, `--timeout`) precede the command. Copy/mkdir commands
without `--apply` only preview. Copy requires an existing parent directory and a
new destination filename. Verification rereads both files, so allow more time
for large files or slow networks. Exit codes: 0 success/preview, 1 different
hashes, 2 failure. JSON output is intended for local use and can contain private
filenames. `truncated` or `skipped` means a listing/search may be incomplete.

## Existing backup status

Optionally add `sync_status_file` with an absolute path to an external JSON file.
Recognized fields: `phase`, `started`, `updated`, `copied`, `freed_bytes`, `errors`.
Run `python scripts/storage.py sync-status`. The plugin reports age/staleness
(default 1800 seconds; override with `--max-age`), and the number of errors.
It does not echo error messages, run backup tasks, drain offline queues, or
prove that every backup is current. Use `verify` for individual file comparisons.

## Limits and safety

SMB access does not reveal SMART health, RAID health, disk temperatures, or DSM
update status. These are explicitly reported as unknown. No DSM administration,
port forwarding or insecure TLS bypass is included. Capacity comes from the
mounted filesystem and may reflect quotas rather than physical drive capacity.

Traversal, alternate data streams, symlinks and Windows reparse points below a
root are rejected. Configured roots and their administrators must be trusted:
path checks are not an OS sandbox and do not defend against malicious concurrent
filesystem replacement. Use OS/NAS permissions for access control.

A file copy creates its destination exclusively, never overwrites it, and never
deletes the source. Destination visibility is not atomic. A timeout, disconnect,
or failed verification can leave an incomplete destination, which is retained
for inspection. Do not treat it as verified or retry over it. Reconnect, compare
the files, and choose a new name if needed. No automatic deletion is performed.
The default command timeout is 60 seconds; a killed write may need inspection.
Search is bounded to avoid expensive scans on older NAS hardware.

## Česky

Plugin běží na počítači a používá již připojenou síťovou složku. NAS nemusí
spouštět žádnou novou aplikaci. Umí hledat soubory, zjistit volné místo,
vytvářet složky a kopírovat s ověřením SHA256. Heslo přebírá prostřednictvím
připojení operačního systému, do pluginu se nezadává.

Vlastní nastavení uložte mimo repozitář do
`~/.config/synology-storage/config.json`. U cílových složek povolte zápis
pomocí `writable: true`. Stávající zálohování a offline frontu plugin nemění.
Příklady zadání: „Najdi na NAS PDF s fakturami“, „Ověř kopii tohoto souboru“,
„Kolik místa zbývá?“ nebo „Zkontroluj stav mého zálohování“.

Zdraví disků a RAID tato verze nečte. Při chybě může zůstat neúplný cílový
soubor; zdroj se nikdy nemaže. Veřejně nesdílejte místní konfiguraci ani výpisy.

## Development and privacy

Tests use temporary local folders and never require a real NAS or credentials.
Before publishing, inspect tracked files and commit metadata. Keep configuration,
NAS addresses, personal paths, credentials and real operational logs out of Git.
Tested behavior is filesystem-level; DSM-specific compatibility depends on the
OS share connection. See [OpenAI plugin documentation](https://developers.openai.com/plugins)
and [Synology file access documentation](https://www.synology.com/en-global/dsm/feature/file_sharing).

## License

MIT. See [LICENSE](LICENSE).

## Podpora projektu

Pokud vám projekt pomáhá, můžete podpořit jeho další vývoj a údržbu na [Buy Me a Coffee](https://buymeacoffee.com/kojakcio). Děkuji za podporu.
