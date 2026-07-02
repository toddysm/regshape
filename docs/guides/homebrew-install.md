# Installing RegShape with Homebrew

RegShape is distributed through a [Homebrew](https://brew.sh) tap so you can
install and update the CLI with a single command on macOS and Linux.

## Install

```bash
brew install toddysm/regshape/regshape
```

This automatically taps `toddysm/regshape` and installs the `regshape` formula.
You can also tap explicitly first:

```bash
brew tap toddysm/regshape
brew install regshape
```

Verify the installation:

```bash
regshape --version
regshape --help
```

## Upgrade

```bash
brew update
brew upgrade regshape
```

## Uninstall

```bash
brew uninstall regshape
brew untap toddysm/regshape
```

## How it works

- The formula lives in the [toddysm/homebrew-regshape](https://github.com/toddysm/homebrew-regshape)
  tap repository.
- RegShape is installed into an isolated Python virtualenv with pinned
  dependency resources, so it does not interfere with your system Python.
- On every RegShape release, the project's release workflow publishes the new
  version to PyPI and then opens a pull request in the tap to bump the formula
  (source URL and checksum) to match. Once that PR is merged, `brew upgrade`
  picks up the new version.

## Troubleshooting

- **`Error: No available formula`** — run `brew update` and ensure the tap is
  present with `brew tap`.
- **Stale version after a release** — the formula bump PR in the tap may not be
  merged yet; check the [tap pull requests](https://github.com/toddysm/homebrew-regshape/pulls).
