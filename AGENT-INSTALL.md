# Installing Codex Continuity Guard as an AI agent

For Claude Code, Codex, or any coding agent asked to "install unlockcodex" on the user's Windows machine.
The gateway serves **Codex Desktop only**; it does not route Claude Code's own model traffic.

## Rules

- **Never handle the user's API keys.** Do not ask for a key in chat, do not type one into a file or a command.
  Key entry is a step only the user performs, in their own terminal.
- Do not edit `%USERPROFILE%\.codex\auth.json` or any file under `%USERPROFILE%\.codex\sessions`.
- Do not invent provider endpoints, model ids or context windows. Ask the user, or have them fill in the file.
- The installer changes Codex Desktop's `config.toml` and adds a Startup shortcut. Tell the user before you run it.

## Steps

1. **Check prerequisites** (read-only):
   - Windows, and `Get-AppxPackage OpenAI.Codex` returns a package;
   - `%USERPROFILE%\.codex\auth.json` and `models_cache.json` exist (Codex Desktop was opened and signed in);
   - `py -3.11 --version` works. If not, the installer offers `winget install Python.Python.3.11`.

2. **Clone** the repository somewhere the user keeps tools and `cd` into it.

3. **Prepare the provider file.** Ask the user which provider(s) and model(s) they want, with for each: the
   Responses API endpoint URL, the exact model id, the context window in tokens, whether it accepts images, and
   which single model should be the automatic fallback. Write that to
   `%LOCALAPPDATA%\CodexContinuityGuard\providers.json` using the shape in `providers.example.json`
   (create the folder if needed). No keys go in this file.

4. **Hand the key step to the user.** Ask them to run, in their own PowerShell window, from the repository folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

   It prompts once per provider for the API key (hidden input) and completes the installation. If they prefer you
   to run the rest, they can instead store the keys themselves first, then tell you to continue:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Yes     # creates the virtualenv, stops at the missing key
   & "$env:LOCALAPPDATA\CodexContinuityGuard\venv\Scripts\python.exe" -B .\guardctl.py set-key <provider-id>
   ```

   After every provider has a key, you may run `install.ps1 -Yes` (non-interactive) yourself.

5. **Verify**: `& "$env:LOCALAPPDATA\CodexContinuityGuard\guard.cmd" status` must show `healthy`, every model with
   `key stored`, and `Codex: routed through this service`.

6. Tell the user to **quit and reopen Codex Desktop**, then pick one of their models in the model picker.

## Notes for agent shells

- The installer starts the service through WMI, so it works even though agent tool shells run inside a Windows Job
  Object (the service refuses to run inside one). Do not try to start `production_service_v4.py` directly.
- First start hashes the whole virtualenv and can take 20–40 seconds.
- Exit code 0 from `install.ps1` means the service answered its health check and Codex's config was updated.
- To change models later: edit `providers.json`, run `install.ps1` again. Models can be added but not removed.
- To remove everything: `uninstall.ps1 -Purge`.
