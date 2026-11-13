# unlockcodex — Codex Continuity Guard

A small local gateway for **ChatGPT Codex Desktop on Windows** that lets you keep working in the same chat when you
switch models or run out of your ChatGPT allowance.

- **Your own models in the Codex model picker.** Add any provider that offers an OpenAI *Responses* API endpoint
  (your key, your plan) next to the built-in GPT models.
- **Same chat, different model.** When a chat moves to another model, the outgoing model writes a short hand-off and
  the visible history travels with it. Nothing is edited in Codex's own chat files.
- **Automatic fallback.** When your ChatGPT allowance is used up, new turns go to the model you chose as fallback,
  after a visible warning.
- **No stuck chats.** A request that fails before the model produced anything (oversized input, a provider timeout
  during hand-off, a dropped stream) is archived automatically instead of leaving the chat paused.

It runs only on `127.0.0.1`, starts at logon, and ships with **no keys, no accounts and no model list** — you add
your own during installation.

> Not affiliated with OpenAI or any model provider. Read [What you should know](#what-you-should-know) before installing.

## Requirements

- Windows 10 or 11 (x64)
- Codex Desktop from the Microsoft Store, signed in with ChatGPT and opened at least once
- Python 3.11 from python.org (the installer offers to install it with `winget`)
- At least one provider with an OpenAI-compatible **Responses** endpoint (`https://…/v1/responses`) and an API key

## Install

```powershell
git clone https://github.com/1988avinash/unlockcodex.git
cd unlockcodex
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer then:

1. creates a private Python virtualenv in `%LOCALAPPDATA%\CodexContinuityGuard` and installs one hash-pinned
   dependency (`zstandard`);
2. **opens `providers.json` in Notepad** — replace the example with your own provider(s) and model(s), save, close;
3. **asks for each provider's API key** (hidden input). Keys are encrypted with Windows DPAPI for your Windows
   account and stored outside the repository; they are never written to a config file or printed;
4. starts the gateway, adds two lines to `%USERPROFILE%\.codex\config.toml` so Codex Desktop uses it, and registers
   a start-at-logon shortcut.

Finally, **quit and reopen Codex Desktop**. Your models appear in the model picker.

### providers.json

```json
{
  "port": 8788,
  "providers": {
    "my-provider": { "endpoint": "https://api.my-provider.com/v1/responses" }
  },
  "models": [
    { "provider": "my-provider", "model": "model-id", "name": "My Model",
      "context_window": 200000, "image_input": false, "auto_fallback": true }
  ]
}
```

| Field | Meaning |
|---|---|
| `providers.<id>.endpoint` | The provider's HTTPS Responses API URL. One endpoint per provider id. |
| `models[].provider` / `model` | Which provider, and the exact model id it expects. The provider's Responses reply must name that same id in its `model` field: the gateway rejects a reply from a different model, so a provider that answers with a dated or renamed alias will not work. |
| `models[].name` | Label shown in Codex (optional). |
| `models[].context_window` | The model's context size in tokens. Take it from your provider's documentation. |
| `models[].image_input` | `true` if the model accepts images (default `false`). |
| `models[].auto_fallback` | `true` on the one model used automatically when your ChatGPT allowance runs out (default: the first model). |
| `models[].protocol` | Leave out. Set `"responses-lite"` only if your provider implements Codex's lite tool format. |

The provider ids `minimax`, `mimo` and `alibaba` get provider-specific request tuning. Any other id is treated as a
generic Responses provider: reasoning effort is limited to low/medium/high and the hand-off request carries no
OpenAI-only fields.

## Everyday use

```powershell
& "$env:LOCALAPPDATA\CodexContinuityGuard\guard.cmd" status
```

| Command | What it does |
|---|---|
| `guard status` | Health, configured models, whether a key is stored, paused chats. |
| `guard set-key <provider>` | Store or replace a provider's API key. |
| `guard stop` / `guard start` | Stop or start the gateway. |
| `guard reconcile` | Archive any paused request so the affected chat can continue. |
| `guard ensure` | Repair the gateway if a Codex Desktop update stopped it. Runs by itself at logon. |
| `guard uninstall` | Stop, remove Codex's routing lines and the logon shortcuts. |

**Adding models or updating:** edit `%LOCALAPPDATA%\CodexContinuityGuard\providers.json` (or `git pull`) and run
`install.ps1` again. Models can be added, renamed and retuned at any time. They cannot be removed, and the
`auto_fallback` model cannot be changed, once chats may be using them; to start over, run `uninstall.ps1 -Purge`
and install again.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1          # keep chat state and keys
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Purge   # delete everything
```

Only the two routing lines are removed from `config.toml`; everything else in that file is left as it is.
Then restart Codex Desktop.

## What you should know

- **Where your data goes.** When you pick one of your models, that provider receives the visible conversation
  (messages, tool calls and results) for that chat. Your ChatGPT sign-in token is forwarded only to OpenAI's own
  Codex endpoint, never to another provider.
- **What it reads.** Your ChatGPT *account id* from `%USERPROFILE%\.codex\auth.json` (read-only, to bind the gateway
  to your account) and Codex's model list from `models_cache.json`. It does not modify `auth.json` or any chat file.
- **What it changes.** Two lines in `config.toml` (`openai_base_url`, `model_catalog_json`), one Startup shortcut,
  and files under `%LOCALAPPDATA%\CodexContinuityGuard`. A copy of the original `config.toml` is kept there.
- **Hidden context does not transfer.** Encrypted reasoning and compacted context from one provider cannot be read by
  another. The gateway tells you when it continues with visible history only.
- **Integrity checks are strict on purpose.** The service refuses to start if its own code, its virtualenv, or the
  pinned Python or Codex executable changes.
- **Codex Desktop updates repair themselves.** Codex updates itself, which changes the runtime the service pins.
  A second logon entry (`Codex Continuity Guard Repair`) waits for the normal start; if the service is still down
  and *only* the Codex runtime changed (a file in the Microsoft Store's protected package folder), it re-pins it and
  restarts, and records this in `ensure.log`. The same step refreshes the model list that Codex Desktop shows.
  If anything else changed (the virtualenv, the Python runtime, the release code) it does **not** trust it, leaves
  the service down, and logs the reason; `guard status` shows the last line. Run `install.ps1` after reviewing such
  a change. After a Codex update, restart Codex Desktop once so it reads the refreshed model list.
- **Upgrades are rehearsed first.** When you add models and run `install.ps1` again, the change is first tried on
  consistent copies of your chat state while the running service is left alone; if it cannot be applied, nothing is
  stopped or changed.
- **Provider terms are yours to check.** Some plans restrict automated or third-party client use.
- **Status.** Built and used daily on one Windows 11 machine, through Codex Desktop 26.1103 to 26.1106. The `minimax`, `mimo` and
  `alibaba` profiles were exercised against live endpoints; generic providers are covered by tests only. Codex
  Desktop updates can change its internal API and break this until it is updated. An experimental bridge for a
  signed-in agent CLI is present in the source but is not installable.
- **Automatic retries are off by design.** If a paid request's outcome is genuinely uncertain, the chat pauses and
  `guard reconcile` clears it; the gateway never silently re-sends a request.

## For AI coding agents

Installing this for a user from Claude Code, Codex or another agent? Follow [AGENT-INSTALL.md](AGENT-INSTALL.md).
Never ask the user to paste an API key into the chat.

## Development

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install zstandard==0.25.0 -r requirements-dev.txt
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONPATH = "$PWD\runtime\continuity-v4;$PWD\runtime\continuity-v3;$PWD\runtime\continuity-v2"
.\.venv\Scripts\python -B -m unittest discover -s runtime\continuity-v4 -p "test_*.py"
.\.venv\Scripts\python -B -m unittest discover -s runtime\continuity-v2 -p "test_*.py"
.\.venv\Scripts\python -B -m unittest test_guardctl
```

`runtime/` holds the gateway (`continuity-v4` is the service; `-v2` and `-v3` are the libraries it builds on).
`guardctl.py` is the installer and operator tool. The tests use local mock providers and never contact a real one.

## License

[MIT](LICENSE)
