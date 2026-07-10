# Laintas CLI

Laintas CLI is an autonomous AI agent for Linux terminals. It can execute direct shell commands, route natural-language tasks through the Laintas agent loop, manage persistent PTY sessions, and connect local terminals to Helpwo.

## Install

The installer detects the Linux CPU architecture and downloads the matching native binary:

```bash
curl -fsSL https://cli.laintas.com/install.sh | bash
```

Supported native targets:

- `amd64` / `x86_64`
- `arm64` / `aarch64`

The standalone binary requires glibc 2.28 or newer. The Debian package is currently published for amd64. The source package is available when a native binary is not suitable.

After installation:

```bash
laintas-cli
```

## Verify A Download

Before installing a manually downloaded archive, check its architecture:

```bash
uname -m
file /usr/local/bin/laintas-cli
```

`x86_64` must use the amd64 archive. `aarch64` must use the arm64 archive. An architecture mismatch produces `Exec format error`; it is not fixed by changing file permissions.

Release checksums are published in `SHA256SUMS.txt` with every GitHub release.

## Install From Source

Source installation requires Python 3.10 or newer:

```bash
unzip laintas-cli_source.zip
cd laintas-cli-source
python3 -m pip install -r requirements.txt
python3 laintas_cli.py
```

## Development

Create a virtual environment and install the development dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest
```

The download page is a separate Vite application in `laintas_cli_download/`:

```bash
cd laintas_cli_download
npm install
npm run dev
```

## Releases

Releases are created by `.github/workflows/release.yml`. Each release contains:

- Linux amd64 standalone binary
- Linux arm64 standalone binary
- Linux amd64 Debian package
- Linux-compatible source package
- SHA256 checksums and the source update manifest

The release workflow builds native binaries in architecture-specific containers and publishes architecture-specific filenames. Do not rename them to a generic `linux.tar.gz` name.

## License

MIT. See `LICENSE` when present in the distribution.

## Contributors

This repository is maintained by Laintas contributors. Parts of the release packaging and documentation workflow were developed with OpenAI Codex assistance.
